# 测试、CI 与贡献规范

## 1. 本讲目标

本讲是手册最后一篇，面向想要给 OpenFPGA 提交改动、或在本地验证自己改动的读者。OpenFPGA 是一个由数万行 C++、上千个 XML 架构文件与几百个回归任务组成的工程项目——任何一处源码改动都可能影响 fabric 网表或比特流。因此「怎么验证改动」「CI 怎么把关」「改动怎么符合规范」是二次开发的必修课。

读完本讲，你应当能够：

- 用 `run-task` 与 `run-regression-local` 在本地跑通回归任务，定位失败原因。
- 看懂 `basic_reg_test.sh` 如何把几百个 `run-task` 串成一条回归流水线，并用 `git diff` 守护黄金产出（golden outputs）。
- 理解 `vpr_wrapper` 如何以 **MACRO 命令**方式把 VPR 引擎嵌入 OpenFPGA shell，以及「保留 / 释放 VPR 结果」两种包装的差异。
- 掌握 `make format-cpp/format-xml/format-py` 三件套与 `dev/check-format.sh` 的「格式闸门」机制。
- 用「一个功能任务 + 一个 no_time_stamp 黄金任务 + 回归脚本加两行」的标准范式，为新增功能补一条回归用例。本次「Bus Based MUX」（总线型 mux 共享配置位）就是这一范式的最新实例。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l4**：知道 `openfpga.sh` 的 `run-task` 是 `source` 加载的 bash 函数，它调用 `run_fpga_task.py` 把 `config/task.conf` 切成 job 并跑出 `runXXX` 结果目录。
- **u2-l2**：知道 OpenFPGA 的命令分七大类别，每条命令在注册时声明执行函数；其中 `vpr` 是一条 **MACRO 命令**（自带解析器、shell 把整行参数透传给它）。
- **u4-l2**：知道任务即「含 `config/task.conf` 的目录」，`task.conf` 用 `[ARCHITECTURES] × [BENCHMARKS] × [SCRIPT_PARAM_*]` 笛卡尔积切 job；也知道「no_time_stamp 任务 + `.gitignore` 白名单 + git diff」是用来锁定关键黄金产物的范式。

几个本讲会用到的术语：

- **回归测试（regression test）**：每次代码改动后重跑一组已知「正确」的任务，确认输出没退化。
- **黄金产出（golden output）**：被提交进 git、作为「正确答案」的产物文件；CI 用 `git diff` 比对重新生成的产物与黄金产出，任何差异即判失败。
- **MACRO 命令**：Shell<T> 框架里的一种特殊命令，它不在 OpenFPGA 这一层解析选项，而是把用户输入的整行参数原样转交给底层引擎（这里是 VPR）自己的命令行解析器。
- **CI（持续集成）**：GitHub Actions 上定义的自动化流程，每次 push/PR 自动编译、跑格式检查、跑回归。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [openfpga_flow/regression_test_scripts/basic_reg_test.sh](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh) | 最核心的回归脚本：依次 `run-task` 跑几百个特性任务，结尾用 `git diff` 守护黄金网表 |
| [openfpga/src/vpr_wrapper/vpr_main.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_main.cpp) | VPR 的薄包装：`vpr()`（保留结果）/ `vpr_standalone()`（释放结果）与两个返回 openfpga 退出码的 wrapper |
| [openfpga/src/vpr_wrapper/vpr_command.cpp](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_command.cpp) | 把 `vpr` / `vpr_standalone` 注册为 MACRO 命令 |
| [Makefile](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/Makefile) | 顶层构建入口，含 `format-cpp`/`format-xml`/`format-py`/`format-all` 格式化目标 |
| [.github/workflows/build.yml](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/.github/workflows/build.yml) | 主 CI：变更检测、多编译器构建、回归矩阵 |
| [.github/workflows/format.yaml](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/.github/workflows/format.yaml) | 格式检查 CI，调 `dev/check-format.sh` |
| [dev/check-format.sh](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/dev/check-format.sh) | 「先 format 再 git diff」的格式闸门实现 |
| [openfpga.sh](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga.sh) | `run-regression-local` 函数，本地一键跑回归 |
| [openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf) | 本次新增：总线型 mux 的功能性回归任务 |
| [openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf) | 本次新增：总线型 mux 的黄金比特流分布任务 |

---

## 4. 核心概念与源码讲解

### 4.1 回归测试体系：run-task、basic_reg_test.sh 与 CI 矩阵

#### 4.1.1 概念说明

OpenFPGA 没有传统意义上的「单元测试为主」的体系，而是采用**端到端回归测试**：把几百个真实的小设计（`and2`、`mult_32x32`、各种配置协议、各种 tile 组织……）逐个跑完整条 `vpr → build_fabric → bitstream → 网表/testbench` 流程，只要任意一步报错或自检失败，回归就失败。这套体系的逻辑是：OpenFPGA 的产物（fabric 网表、比特流）正确性很难用断言逐条检验，但「跑通 + 仿真自检 + 黄金产出比对」三者结合，足以覆盖绝大多数回归。

回归测试分三层：

1. **单个任务**：`openfpga_flow/tasks/basic_tests/...` 下的一个含 `config/task.conf` 的目录，由 `run-task` 跑。
2. **回归脚本**：`openfpga_flow/regression_test_scripts/*.sh`，把一组相关的 `run-task` 串成一个脚本。最重要的是 `basic_reg_test.sh`。
3. **CI 矩阵**：`.github/workflows/build.yml` 用 matrix 把多个回归脚本并行跑在不同环境。

#### 4.1.2 核心流程

`basic_reg_test.sh` 的执行流程可以概括为三段：

```text
① source openfpga.sh        # 加载 run-task 等函数、设置 OPENFPGA_PATH
② 一长串 run-task ...        # 按特性分组，依次跑几百个任务
   run-task basic_tests/k4_series/k4n4_frac_mult_busmux $@
   ...
③ git diff 黄金产出          # 回到仓库根，git diff golden_outputs_no_time_stamp/**
   若有变化 → 打印 diff、exit 1
```

注意几个细节：

- 脚本开头有 `set -e`，任何一条命令返回非零都会让脚本立即中止。
- 每个 `run-task` 都透传 `$@`，所以你可以在本地调用时追加参数（如 `--maxthreads 4`、`--debug`）。
- 脚本是**线性顺序执行**的——这既是优点（日志清晰、失败定位容易），也是缺点（一次完整 `basic_reg_test` 要跑很久，CI 上靠 matrix 并行多个回归脚本来分摊）。

#### 4.1.3 源码精读

先看脚本的开头与结构骨架：

```bash
#!/bin/bash
set -e
source openfpga.sh
echo -e "Basic regression tests";
```

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:1-8](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L1-L8) —— `set -e` 决定了「一错即停」的严格语义；`source openfpga.sh` 把 `run-task` 这个 bash 函数加载进当前 shell。

脚本按特性分组，每组用一句 `echo -e` 说明在测什么，下面跟若干 `run-task`。例如 K4 系列：

```bash
echo -e "Testing K4N4 with 32-bit fracturable multiplier";
run-task basic_tests/k4_series/k4n4_frac_mult $@
echo -e "Testing K4N4 with 32-bit fracturable multiplier using a bus-based mux (shared config bit)";
run-task basic_tests/k4_series/k4n4_frac_mult_busmux $@
```

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:193-196](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L193-L196) —— 这就是本次「Bus Based MUX (#2602)」给回归脚本加的两行之一：在原 `k4n4_frac_mult`（普通 32 位乘法器）之后，新增 `k4n4_frac_mult_busmux`，验证带 `bus="true"` 的互连能跑通完整流程。

脚本最关键的部分是结尾的**黄金网表 git-diff 守护**：

```bash
cd ${OPENFPGA_PATH}
git config --global --add safe.directory ${OPENFPGA_PATH}
git log
if git diff --name-status --exit-code -- ':openfpga_flow/tasks/basic_tests/no_time_stamp/*/golden_outputs_no_time_stamp/**'; then
  echo -e "Golden netlist remain unchanged"
else
  echo -e "Detect changes in golden netlists";
  git diff -- ':openfpga_flow/tasks/basic_tests/no_time_stamp/*/golden_outputs_no_time_stamp/**';
  exit 1;
fi
```

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:378-388](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L378-L388) —— 这段是整个回归体系的「定盘星」。`--exit-code` 让 `git diff` 在有差异时返回非零；路径通配 `golden_outputs_no_time_stamp/**` 只比对那些被特意白名单进 git 的黄金产出（其余产物由各自任务目录下的 `.gitignore` 忽略）。只要重新生成的黄金产出与提交进仓库的版本有任何字节级差异，CI 就会失败。

本次新增的第二个回归点就在这段守护的正上方：

```bash
echo -e "Testing bus-based mux shared config bit via golden bitstream distribution";
run-task basic_tests/no_time_stamp/frac_dsp_busmux $@
```

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:365-366](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L365-L366) —— 这是一个 **no_time_stamp 黄金任务**：它不跑仿真，而是生成 `bitstream_distribution.xml` 等不含时间戳的产物并落盘到 `golden_outputs_no_time_stamp/`，然后交给上面那段 `git diff` 守护。设计意图见 4.4 节。

CI 侧，`build.yml` 用 matrix 把回归脚本铺开成多个并行 job：

```yaml
  linux_regression_tests:
    name: linux_regression_tests
    runs-on: ubuntu-22.04
    needs: [linux_build, change_detect]
    strategy:
      fail-fast: false
      matrix:
        config:
          - name: basic_reg_yosys_only_test
          - name: basic_reg_test
          - name: fpga_verilog_reg_test
          ...
    steps:
      ...
      - name: ${{matrix.config.name}}_GCC-11_(Ubuntu 22.04)
        shell: bash
        run: source openfpga.sh && source openfpga_flow/regression_test_scripts/${{matrix.config.name}}.sh --debug --show_thread_logs
```

[.github/workflows/build.yml:1095-1144](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/.github/workflows/build.yml#L1095-L1144) —— 每个 matrix 条目对应一个回归脚本文件名（`basic_reg_test` → `basic_reg_test.sh`），CI 用 `source` 执行它。`fail-fast: false` 保证一个 job 失败不会取消其他 job，便于一次性看到所有问题。job 先下载 `linux_build` 产出的二进制 artifact（`download-artifact`），再 `chmod +x` 后跑回归。

#### 4.1.4 代码实践

1. **实践目标**：在本地跑通回归脚本中的一小段，体验 `run-task` → 结果目录 → 失败定位的完整链路。
2. **操作步骤**：
   - 先 `source openfpga.sh`（必须在仓库根目录，见 u1-l3）。
   - 单跑本次新增的总线型 mux 任务：`run-task basic_tests/k4_series/k4n4_frac_mult_busmux`。
   - 跑完后用 `goto-task basic_tests/k4_series/k4n4_frac_mult_busmux` 进入结果目录，查看生成的 fabric 网表与比特流。
3. **需要观察的现象**：终端会打印 job 的调度与每个命令的输出；`run*/latest/` 下应出现 `and2/Min_route_chan_width/` 之类的目录。
4. **预期结果**：任务以 `Status: Passed` 结束。若失败，先看 `run*/latest/**/openfpga_out.log` 的报错。
5. **待本地验证**：完整 `basic_reg_test.sh` 耗时极长（数百任务），本地一般只挑子集跑；若想一键全跑，用 `run-regression-local`（见 4.1 末尾的 `openfpga.sh`）。

> 本地一键全量回归的入口是 [openfpga.sh:99-102](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga.sh#L99-L102) 的 `run-regression-local` 函数：它 `cd ${OPENFPGA_PATH}` 后 `bash .github/workflows/*reg_test.sh`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `basic_reg_test.sh` 要在结尾做 `git diff`，而不是在每个 `run-task` 内部断言产物正确？
**答案**：`run-task` 只能验证「流程跑通 + 仿真自检通过」，但无法判断「配置位数是否退化了」。黄金产出比对可以抓住那些「流程仍能跑通、但 fabric 结构/比特流悄然变化」的回归（例如总线型 mux 退化回 32 个独立配置位——流程不报错，但配置位数变了）。把黄金产出提交进 git，再用 `git diff` 比对，是最廉价且确定性的「结构稳定性」守护。

**练习 2**：CI 的 `linux_regression_tests` 为什么用 matrix 而不是在一个 job 里串行跑所有回归脚本？
**答案**：全部回归脚本串行跑会非常久。matrix 把 `basic_reg_test` / `fpga_verilog_reg_test` / `fpga_bitstream_reg_test` 等分配到多个并行 job，整体墙钟时间约等于最慢的那个脚本；`fail-fast: false` 还能保证一个失败不连累其余，便于一次性收集所有失败日志。

---

### 4.2 VPR wrapper：以 MACRO 命令集成 VPR 引擎

#### 4.2.1 概念说明

OpenFPGA 复用 VPR 做综合后的「打包/布局/布线」（pack/place/route），但二者不是两个独立进程——OpenFPGA 把 VPR 的 `main` 流程**作为库函数**直接嵌入自己的 shell，由 `vpr` 命令触发。这样做的好处是：VPR 跑完后，它的 device context（布局布线结果、rr graph）**留在同一进程内存里**，OpenFPGA 后续命令（`link_openfpga_arch`、`build_fabric`）可以直接读取，无需落盘再读。

`vpr` 在 OpenFPGA shell 里是一条 **MACRO 命令**：它不向 Shell<T> 框架注册任何 `Command` 选项，因为 VPR 有自己的一套命令行解析器（`vpr_init` 读 `t_options`）。Shell<T> 把用户敲的整行参数原样透传给 VPR。这就是为什么你在脚本里看到的 `vpr` 用法和直接调 VPR 二进制一模一样。

#### 4.2.2 核心流程

VPR 集成有两个并列的包装函数，区别在于「跑完是否释放 VPR 数据」：

```text
vpr 命令          → vpr_wrapper    → vpr()            → 不调 vpr_free_all，结果留在内存
vpr_standalone 命令 → vpr_standalone_wrapper → vpr_standalone() → 调 vpr_free_all，释放内存
```

- **`vpr`（常规）**：保留 VPR 结果。因为后续 `build_fabric` 等命令要读 device context，所以**不能**释放。
- **`vpr_standalone`（独立）**：跑完即释放，用于「只想单独跑一次 VPR、不接 OpenFPGA 后处理」的场景（如 `basic_tests/vpr_standalone` 任务）。

两个 wrapper 都做同一件事：把 VPR 的退出码翻译成 OpenFPGA shell 的退出码（`SUCCESS_EXIT_CODE` → `CMD_EXEC_SUCCESS`，其余 → `CMD_EXEC_FATAL_ERROR`）。

#### 4.2.3 源码精读

先看 wrapper 如何翻译退出码：

```cpp
/* A wrapper to return proper codes for openfpga shell */
int vpr_wrapper(int argc, char** argv) {
  if (SUCCESS_EXIT_CODE != vpr(argc, argv)) {
    return openfpga::CMD_EXEC_FATAL_ERROR;
  }
  return openfpga::CMD_EXEC_SUCCESS;
}
```

[openfpga/src/vpr_wrapper/vpr_main.cpp:98-104](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_main.cpp#L98-L104) —— 这是 OpenFPGA 与 VPR 两种退出码体系的衔接点。VPR 返回 `SUCCESS_EXIT_CODE`/`ERROR_EXIT_CODE`/`UNIMPLEMENTABLE_EXIT_CODE` 等，而 OpenFPGA shell 用 `CMD_EXEC_SUCCESS`/`CMD_EXEC_FATAL_ERROR`/`CMD_EXEC_MINOR_ERROR`（见 u2-l1）。注意 `vpr()` 的注释明确写「VPR program without clean up」。

`vpr()` 本体就是 VPR 原 `main.cpp` 的镜像，关键差异在结尾——它**故意注释掉了** `vpr_free_all`：

```cpp
    /* TODO: move this to the end of flow
     * free data structures
     */
    /* vpr_free_all(Arch, vpr_setup); */

    VTR_LOG("VPR succeeded\n");
```

[openfpga/src/vpr_wrapper/vpr_main.cpp:66-71](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_main.cpp#L66-L71) —— 这正是「保留 VPR 结果」的实现：`t_arch* Arch = new t_arch` 在堆上分配（见 [vpr_main.cpp:44](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_main.cpp#L44)），跑完不释放，OpenFPGA 后续命令就能通过 VPR 的全局 context（`g_vpr_ctx`）读到布局布线结果。而 `vpr_standalone()` 在每个出口都调了 `vpr_free_all(Arch, vpr_setup)`（见 [vpr_main.cpp:137](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_main.cpp#L137)）。

再看命令注册——为什么说它是 MACRO 命令：

```cpp
void add_vpr_commands(openfpga::Shell<OpenfpgaContext>& shell) {
  ShellCommandClassId vpr_cmd_class = shell.add_command_class("VPR");
  Command shell_cmd_vpr("vpr");
  ShellCommandId shell_cmd_vpr_id =
    shell.add_command(shell_cmd_vpr,
                      "Start VPR core engine to pack, place and route a BLIF "
                      "design on a FPGA architecture; Note that this command "
                      "will keep VPR results!");
  shell.set_command_class(shell_cmd_vpr_id, vpr_cmd_class);
  shell.set_command_execute_function(shell_cmd_vpr_id, vpr::vpr_wrapper);
```

[openfpga/src/vpr_wrapper/vpr_command.cpp:11-24](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_command.cpp#L11-L24) —— 注意 `Command shell_cmd_vpr("vpr")` 这一句只给了名字，**没有调用任何 `add_option`**（对比 u10-l4 里普通命令会加一堆选项）。这正是 MACRO 命令的特征：Shell<T> 知道这条命令的参数不归自己管，于是把整行透传给执行函数 `vpr::vpr_wrapper(int argc, char** argv)`——它的签名正好是 `main` 风格的 argc/argv，VPR 拿去自己解析。`vpr_standalone` 的注册完全对称（[vpr_command.cpp:29-37](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga/src/vpr_wrapper/vpr_command.cpp#L29-L37)）。这条命令是「VPR」命令类（`add_command_class("VPR")`），也是七大命令组里最早注册的一个（见 u2-l2）。

#### 4.2.4 代码实践

1. **实践目标**：对比 `vpr` 与 `vpr_standalone` 两条命令，理解「保留 vs 释放」对后续流程的影响。
2. **操作步骤**：
   - 在交互 shell 里先跑 `vpr_standalone`（带一个最小的 arch + blif 参数，参考 `basic_tests/vpr_standalone` 的 task.conf），观察它完成。
   - 紧接着尝试 `link_openfpga_arch`，看是否报「找不到 device context」之类的错。
   - 再开一次 shell，这次用 `vpr` 跑同样的 arch + blif，再 `link_openfpga_arch`，应能成功。
3. **需要观察的现象**：`vpr_standalone` 之后接 `link_openfpga_arch` 应失败或拿不到布局布线结果；`vpr` 之后则正常。
4. **预期结果**：验证「常规流程必须用 `vpr`（保留结果）」这一约束。
5. **待本地验证**：VPR 需要正确的 arch/blif 路径，建议直接复用 `basic_tests/vpr_standalone` 任务里的参数。

#### 4.2.5 小练习与答案

**练习 1**：`vpr` 命令为什么不在 `Command` 对象上加任何选项？
**答案**：因为 VPR 有自己完整的命令行解析器（`vpr_init` 解析 `t_options`）。如果在 OpenFPGA 这层再定义一遍选项，既要重复维护、又可能与 VPR 的语义不一致。MACRO 命令模式让 Shell<T> 透传整行参数，由 VPR 自己解析，是更干净、更不易脱节的集成方式。

**练习 2**：为什么 `vpr()` 用 `new t_arch`（堆分配）而 `vpr_standalone()` 用 `t_arch Arch`（栈分配）？
**答案**：`vpr()` 跑完不释放，需要 VPR 数据在函数返回后仍然存活，供 OpenFPGA 后续命令读取，所以用堆分配延长生命周期；`vpr_standalone()` 跑完立即 `vpr_free_all` 并返回，数据无需跨函数存活，栈分配更安全（自动随栈帧回收）。这是两种包装在内存策略上的根本差异。

---

### 4.3 代码格式化工具链与 CI 格式闸门

#### 4.3.1 概念说明

OpenFPGA 是多人协作的大型项目，混用 C++、XML、Python 三种语言，必须靠自动化工具统一代码风格，否则 review 时会陷入「空格/缩进」之争。项目用三件套：

- **C/C++**：`clang-format-14`，规则由仓库根的 `.clang-format` 文件定义（`--style=file`）。
- **XML**：`xmllint --format`，作用在 `vpr_arch` 与 `openfpga_arch` 目录。
- **Python**：`black --line-length 100`，作用在 `openfpga_flow/scripts`。

这三者都被封装成 Makefile 目标，并且有一道「格式闸门」CI（`format.yaml`）在每次 PR 上强制检查。

#### 4.3.2 核心流程

本地格式化的流程：

```text
make format-cpp   # 改写 libs/ 与 openfpga/ 下的源文件
make format-xml   # 改写两套架构 XML
make format-py    # 改写流程脚本
make format-all   # 以上三者
```

CI 格式闸门的流程（`dev/check-format.sh`）：

```text
① 要求工作区干净（git status 无改动）
② make format<类型>     # 重新格式化
③ git diff 统计改动了多少行
   若 > 0 → FAILED：打印 diff、git reset --hard、exit 1
   若 = 0 → OK
```

注意第①步要求「工作区干净」——因为闸门的判定手段就是 `git diff`，如果提交前工作区本来就有未提交改动，diff 会混入这些改动，判定就不可信了。

#### 4.3.3 源码精读

Makefile 里三个格式化目标的定义：

```makefile
format-cpp:
# Format all the C/C++ files under this project, excluding submodules
	for f in `find libs openfpga -iname *.cpp -o -iname *.hpp -o -iname *.c -o -iname *.h`; \
	do \
	${CLANG_FORMAT_EXEC} --style=file -i $${f} || exit 1; \
	done
```

[Makefile:108-113](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/Makefile#L108-L113) —— `find libs openfpga` 只扫这两个目录，**刻意排除了子模块**（`vtr-verilog-to-routing`、`yosys` 等第三方代码不归 OpenFPGA 管风格）。`-i` 表示原地改写，`--style=file` 让 clang-format 读 `.clang-format`。可执行文件名由变量 `CLANG_FORMAT_EXEC` 控制，默认 `clang-format-14`（[Makefile:58](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/Makefile#L58)）。

```makefile
format-xml:
	for f in `find openfpga_flow/vpr_arch openfpga_flow/openfpga_arch -iname *.xml`; \
	do \
	XMLLINT_INDENT="  " && ${XML_FORMAT_EXEC} --format $${f} --output $${f} || exit 1; \
	done

format-py:
	for f in `find openfpga_flow/scripts -iname *.py`; \
	do \
	${PYTHON_FORMAT_EXEC} $${f} --line-length 100 || exit 1; \
	done

format-all: format-cpp format-xml format-py
```

[Makefile:115-130](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/Makefile#L115-L130) —— XML 只格式化两套架构目录（不碰 task.conf 这类 INI，也不碰黄金产出的 xml）；Python 用 black 并锁 100 列行宽。

再看格式闸门的实现：

```bash
clean=$(git status -s -uno | wc -l) #Short ignore untracked
file_pattern="*.cpp *.c *.hpp *.h *.py *.xml"

if [ $clean -ne 0 ]; then
    echo "Current working tree was not clean! This tool only works on clean checkouts"
    exit 2
else
    ...
    make format"$1" > /dev/null 2>&1
    valid_format=$(git diff ${file_pattern}| wc -l)
    if [ $valid_format -ne 0 ]; then
        echo "FAILED"
        ...
        git diff ${file_pattern}
        echo "Run 'make format$1' to apply these changes"
        git reset --hard > /dev/null
        exit 1
    fi
```

[dev/check-format.sh:3-29](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/dev/check-format.sh#L3-L29) —— 三个要点：① `git status -s -uno` 必须为空（`-uno` 忽略未跟踪文件）；② 跑 `make format$1`（`$1` 是 `-cpp`/`-xml`/`-py`）；③ 用 `git diff` 看格式化是否改动了文件，有改动就 `FAILED` 并 `git reset --hard` 恢复、退出 1。换句话说，**正确的提交应当是「format 之后 git diff 为空」**。

CI 侧，独立的 `format.yaml` 工作流用 matrix 对三种语言各跑一次闸门：

```yaml
    strategy:
      matrix:
        config:
          - name: "C/C++"
            code_type: "-cpp"
          - name: "XML"
            code_type: "-xml"
          - name: "Python"
            code_type: "-py"
    ...
      - name: Check format
        run: ./dev/check-format.sh ${{ matrix.config.code_type }}
```

[.github/workflows/format.yaml:25-58](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/.github/workflows/format.yaml#L25-L58) —— 注意它和 `build.yml` 是两个独立 workflow：格式检查不依赖编译，跑得快、可单独失败；且 `paths-ignore` 排除了纯文档改动（`**.md`、`docs/**`），避免改文档触发格式 CI。

#### 4.3.4 代码实践

1. **实践目标**：亲手触发并修复一次格式问题，走通「format → 提交」的标准流程。
2. **操作步骤**：
   - 在某个 C++ 源文件（例如 `openfpga/src/base/main.cpp`）里故意把缩进弄乱或加多余空格，**先不要 commit**。
   - 跑 `./dev/check-format.sh -cpp`，应报 `FAILED` 并提示 `Run 'make format-cpp'`（注意：因为它会 `git reset --hard`，建议先 `git stash` 保留你的实验改动）。
   - 改为直接跑 `make format-cpp`，再用 `git diff` 查看它自动修复了什么。
   - 修复后 `git diff` 应为空（在你没引入其它逻辑改动的前提下）。
3. **需要观察的现象**：`check-format.sh` 在脏工作区上会直接 `exit 2`；`make format-cpp` 会原地改写源文件。
4. **预期结果**：格式化后 `git diff` 仅显示风格修正，无逻辑变化。
5. **待本地验证**：若本地没有 `clang-format-14`，可用 `CLANG_FORMAT_EXEC=clang-format make format-cpp` 指向系统已有的版本（版本差异可能产生不同结果，CI 以 14 为准）。

#### 4.3.5 小练习与答案

**练习 1**：`dev/check-format.sh` 为什么要求工作区必须干净？
**答案**：它的判定逻辑是「format 之后 git diff 有没有改动」。如果工作区本来就有未提交的逻辑改动，这些改动也会出现在 diff 里，导致 `wc -l` 非零而误报 FAILED，且最后的 `git reset --hard` 还会把你的逻辑改动冲掉。要求干净工作区是为了让 diff 只反映「格式化带来的差异」，保证判定可信、且 `reset --hard` 安全。

**练习 2**：为什么 XML 格式化只覆盖 `vpr_arch` 与 `openfpga_arch` 两个目录，而不覆盖 `golden_outputs_no_time_stamp/` 下的 xml？
**答案**：架构 XML 是人手维护、需要统一风格的源文件；而黄金产出 xml（如 `fabric_bitstream.xml`、`bitstream_distribution.xml`）是程序生成的、作为回归比对基准的产物，其格式由生成器决定，不能被 xmllint 改写——否则会破坏黄金比对的确定性。

---

### 4.4 贡献规范与新增回归任务的标准范式（含 busmux 案例）

#### 4.4.1 概念说明

当你给 OpenFPGA 加了一个新特性（比如本次的「总线型 mux」），光改源码是不够的——还要让 CI 能替你**永久守护**这个特性，否则后续重构很容易悄悄把它弄坏。OpenFPGA 经过长期实践沉淀出一套「新增回归任务」的标准范式，README 也指向了官方贡献指南：

> Please read the [contributor guidelines](https://openfpga.readthedocs.io/en/master/dev_manual/contributor_guide/) if you would like to contribute to OpenFPGA.

范式可以归纳为「**一个功能任务 + 一个黄金任务 + 回归脚本加两行**」。本次 PR #2602「Bus Based MUX」就是教科书式地按这个范式落地的，我们用它作为贯穿案例。

#### 4.4.2 核心流程

新增一个特性回归的标准步骤：

```text
① 写一个「功能任务」：能跑通完整流程（含 testbench 自检），证明特性端到端可用。
   → 落在 basic_tests/<特性分类>/，如 k4_series/k4n4_frac_mult_busmux

② 写一个「黄金任务」：用 no_time_stamp 脚本生成无时间戳的关键产物，
   用 .gitignore 白名单只把要锁定的产物提交进 git。
   → 落在 basic_tests/no_time_stamp/<特性>，如 frac_dsp_busmux

③ 在 basic_reg_test.sh 里加两行 run-task：一行指向功能任务，一行指向黄金任务。

④ （可选）补对应的 vpr_arch / openfpga_arch 文件，并跑 make format-xml 规范化。
```

为什么要「功能 + 黄金」两个任务？因为它们守护的是两种不同的回归：

- **功能任务**守护「能不能跑通」——流程报错或仿真不自检通过就失败。
- **黄金任务**守护「结构对不对」——流程能跑通、但产物（如配置位数）退化时，`git diff` 黄金产出会失败。

#### 4.4.3 源码精读

**第一步：功能任务 `k4n4_frac_mult_busmux`**

```ini
[GENERAL]
run_engine=openfpga_shell
...
fpga_flow=vpr_blif

[OpenFPGA_SHELL]
openfpga_shell_template=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_shell_scripts/fix_device_example_script.openfpga
openfpga_arch_file=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_arch/k4_frac_N4_adder_chain_mem1K_frac_dsp32_40nm_frame_openfpga.xml
...
openfpga_vpr_device_layout=4x4

[ARCHITECTURES]
arch0=${PATH:OPENFPGA_PATH}/openfpga_flow/vpr_arch/k4_frac_N4_tileable_adder_chain_mem1K_frac_dsp32_busmux_40nm.xml

[BENCHMARKS]
bench0=${PATH:OPENFPGA_PATH}/openfpga_flow/benchmarks/micro_benchmark/and2/and2.blif
```

[openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf:11-36](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf#L11-L36) —— 注意它和已有的 `k4n4_frac_mult` 任务**几乎完全相同**：用同一份 openfpga_arch、同一个 `and2` 基准、同一个 shell 模板，**唯一区别是 `arch0` 指向带 `busmux` 后缀的 VPR arch**（`..._frac_dsp32_busmux_40nm.xml`）。这份 VPR arch 里把乘法器 slice 的 `a2a` 互连写成了 `<mux bus="true"/>`（32 位宽的总线 mux）。文件开头的注释把这层关系说得很清楚：

```ini
# This task exercises bus-based multiplexer (<mux bus="true"/>) support.
# The architecture is identical to k4n4_frac_mult except that the
# mult_32x32_slice 'a2a' interconnect is a 32-bit-wide bus mux. In OpenFPGA the
# 32 single-bit muxes that VPR expands the bus mux into must share a single
# configuration memory (one shared config bit) instead of 32 separate ones.
```

[openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf:4-8](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/k4_series/k4n4_frac_mult_busmux/config/task.conf#L4-L8) —— 这就是 u6-l3/u7-l2 讲过的总线型 mux：32 个单比特 mux 共享 1 个配置位。功能任务的作用是确认「带 bus mux 的 arch 能跑通完整 fabric 构建与比特流生成」。

**第二步：黄金任务 `frac_dsp_busmux`**

```ini
[OpenFPGA_SHELL]
openfpga_shell_template=${PATH:OPENFPGA_PATH}/openfpga_flow/openfpga_shell_scripts/report_bitstream_distribution_no_time_stamp_example_script.openfpga
...
openfpga_output_dir=${PATH:TASK_DIR}/golden_outputs_no_time_stamp
```

[openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf:23-28](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf#L23-L28) —— 关键在两点：① 用 `report_bitstream_distribution_no_time_stamp_example_script.openfpga` 这个**无时间戳**模板生成产物（时间戳会让每次产出的 git diff 都不同，无法比对）；② `openfpga_output_dir` 直接写到 `golden_outputs_no_time_stamp/`，即被 git 跟踪的黄金目录。它的注释点明了守护意图：

```ini
# The generated bitstream_distribution.xml records the per-block config-bit counts and is
# committed as a golden file; basic_reg_test.sh git-diffs it, so any regression
# that reverted to 32 separate config bits would change the DSP grid block's
# bit count and fail CI.
```

[openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf:2-11](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/config/task.conf#L2-L11) —— 这是黄金任务的精髓：如果哪天有人改坏了总线型 mux、让它退化回 32 个独立配置位，DSP 块的配置位数就会从「1」变回「32」，`bitstream_distribution.xml` 随之改变，`basic_reg_test.sh` 末尾的 `git diff` 守护就会让 CI 失败。流程本身不会报错，但黄金比对能抓住。

**白名单 `.gitignore`**：黄金目录用「先全忽略、再逐个 `!` 放行」的方式，只锁定真正想比的产物：

```gitignore
/*
!/.gitignore
!/bitstream_distribution.xml
!/fabric_bitstream.xml
!/lb
/lb/*
!/lb/logical_tile_mult_32_mode_mult_32x32__mult_32x32_slice.v
```

[openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/golden_outputs_no_time_stamp/.gitignore:11-17](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/tasks/basic_tests/no_time_stamp/frac_dsp_busmux/golden_outputs_no_time_stamp/.gitignore#L11-L17) —— `/*` 先忽略目录下一切，再用 `!` 把三个关键产物放行：`bitstream_distribution.xml`（每块配置位数）、`fabric_bitstream.xml`（fabric 比特流路径，`--path_only`）、以及 DSP tile 的网表 `...mult_32x32_slice.v`（验证共享存储器只出现一次）。这样 `git diff` 只比这三个文件，不会被 fabric 网表里的大量无关文件干扰。

**第三步：回归脚本加两行**——已在 4.1.3 引用过：

```bash
echo -e "Testing K4N4 with 32-bit fracturable multiplier using a bus-based mux (shared config bit)";
run-task basic_tests/k4_series/k4n4_frac_mult_busmux $@
...
echo -e "Testing bus-based mux shared config bit via golden bitstream distribution";
run-task basic_tests/no_time_stamp/frac_dsp_busmux $@
```

[openfpga_flow/regression_test_scripts/basic_reg_test.sh:195-196](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L195-L196) 与 [openfpga_flow/regression_test_scripts/basic_reg_test.sh:365-366](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/regression_test_scripts/basic_reg_test.sh#L365-L366) —— 功能任务插在 K4 系列乘法器那一段（紧挨原 `k4n4_frac_mult`），黄金任务插在 no_time_stamp 那一段（紧挨其它黄金任务）。归类放置是为了让脚本可读。

**额外提醒——CI 变更检测**：CI 用 `change_detect` job 判断「源码是否改动」，决定是否触发耗时的回归矩阵：

```yaml
      - name: Check for source code changes
        id: changes
        run: |
          git diff origin/master HEAD --name-status -- . ':!openfpga_flow' ':!docs'
          if git diff origin/master HEAD --name-status --exit-code -- . ':!openfpga_flow' ':!docs'; then
```

[.github/workflows/build.yml:53-57](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/.github/workflows/build.yml#L53-L57) —— `':!openfpga_flow' ':!docs'` 表示只看 `openfpga_flow` 与 `docs` 之外的源码改动。这就是为什么「只改 task.conf / 黄金产出 / 文档」的 PR 不会触发完整回归矩阵——但 `basic_reg_test.sh` 本身位于 `openfpga_flow/`，所以单纯改回归脚本也不会触发 `linux_regression_tests`，只会跑 `docker_regression_tests`（用预编译镜像）。这是一个容易被忽略的 CI 行为细节。

#### 4.4.4 代码实践

1. **实践目标**：仿照 busmux 范式，为一个已有特性「补一个黄金任务」，走通「功能 + 黄金」完整范式。
2. **操作步骤**：
   - 复制 `k4n4_frac_mult_busmux/config/task.conf`，把任务目录改名成一个新的实验任务（如 `_my_busmux_copy`），改 `bench0` 换成另一个 micro_benchmark（如 `or2`）。
   - `source openfpga.sh` 后 `run-task _my_busmux_copy`，确认跑通。
   - 再复制 `frac_dsp_busmux` 任务为 `_my_frac_dsp_busmux`，运行后查看 `golden_outputs_no_time_stamp/bitstream_distribution.xml` 中 DSP 块（`mult_32x32_slice`）的配置位数，确认是「共享后的 1 份」而非 32 份。
   - 对新加的 task.conf 与（若有）新 arch 跑 `make format-xml`。
3. **需要观察的现象**：`bitstream_distribution.xml` 里 DSP tile 的总线 mux 只贡献 1 个共享配置位；若你在 arch 里去掉 `bus="true"`，配置位数会暴涨。
4. **预期结果**：能复现「bus mux 共享配置位」带来的位数差异，理解黄金任务为何能抓住这种退化。
5. **待本地验证**：黄金产物的具体位数值依赖实现细节，以本地实跑结果为准。

#### 4.4.5 小练习与答案

**练习 1**：功能任务和黄金任务都用 `and2` 这个最简单的基准，而不是真正用到乘法器的基准。为什么这样也能守护总线型 mux？
**答案**：总线型 mux 是**架构级**特性——只要 VPR arch 里声明了 `<mux bus="true"/>`，OpenFPGA 在 `build_fabric` 与 `build_architecture_bitstream` 时就会走总线型 mux 的代码路径，无论被综合的设计是否真正用到乘法器。fabric 网表与比特流分布会包含 DSP tile 的总线 mux 结构。所以即便基准是 `and2`，黄金产出里的 DSP 块配置位数仍能反映「共享 vs 独立」的差异。用最简基准是为了让回归跑得快。

**练习 2**：黄金任务的 `.gitignore` 为什么用「`/*` 全忽略 + `!` 逐个放行」而不是反过来「逐个忽略」？
**答案**：fabric 网表会生成大量文件（每个 tile 一份），且文件名/数量会随架构变化。若用「逐个忽略」，每新增一个产物就得记得加一行忽略，极易遗漏，导致把不该提交的产物提交进 git、污染黄金比对。「全忽略 + 白名单放行」是白名单策略：默认不提交任何产物，只有显式放行的才进 git，更安全、更稳定，新增产物自动被忽略。

---

## 5. 综合实践

把本讲四块知识串起来，完成一次「mini 贡献」演练：

**任务**：假设你刚给 OpenFPGA 加了一个小改动（例如在 `build_grid_bitstream.cpp` 里给总线型 mux 的选择位打印一行调试日志），请按规范完成验证与提交准备。

**步骤**：

1. **编译**：`make compile`（或 `cmake_goals=openfpga make compile` 缩小范围，见 u1-l3）。
2. **格式化**：`make format-cpp`，确认 `git diff` 只有你预期的改动（没有意外的风格修正）。
3. **格式闸门**：`./dev/check-format.sh -cpp`，应输出 `OK`（注意先保证工作区只有已暂存的改动，或先 commit 你的改动再跑）。
4. **单点回归**：`source openfpga.sh && run-task basic_tests/k4_series/k4n4_frac_mult_busmux`，确认你的改动没破坏总线型 mux 流程。
5. **黄金守护**：`run-task basic_tests/no_time_stamp/frac_dsp_busmux`，然后 `cd ${OPENFPGA_PATH}` 跑 `git diff -- 'openfpga_flow/tasks/basic_tests/no_time_stamp/*/golden_outputs_no_time_stamp/**'`，应无输出（黄金产出未变）。
6. **反思**：如果你的改动是「纯加日志」，黄金产出应纹丝不动；若 `git diff` 有变化，说明你的改动意外影响了 fabric 结构或比特流，需要排查。

这个流程就是 CI 在每次 PR 上替你做的事——本地先跑一遍，能极大减少来回沟通成本。

## 6. 本讲小结

- OpenFPGA 采用**端到端回归测试**：`run-task` 跑单个任务，`basic_reg_test.sh` 串起几百个任务，CI 用 matrix 并行跑多个回归脚本。
- `basic_reg_test.sh` 的「定盘星」是结尾的 `git diff golden_outputs_no_time_stamp/**`：它抓住那些「流程能跑通、但产物退化」的隐蔽回归。
- VPR 通过 `vpr_wrapper` 以 **MACRO 命令**嵌入 shell——不加任何选项，整行参数透传给 VPR 自己的解析器；`vpr` 保留结果、`vpr_standalone` 释放结果，两种包装用堆/栈分配配合「是否调 `vpr_free_all`」实现。
- 代码风格三件套 `make format-cpp/format-xml/format-py`（clang-format-14 / xmllint / black），CI 由 `dev/check-format.sh` 的「format 后 git diff」闸门强制。
- 新增回归的标准范式是「**一个功能任务 + 一个 no_time_stamp 黄金任务 + 回归脚本加两行 run-task**」；本次「Bus Based MUX」用 `k4n4_frac_mult_busmux`（功能）+ `frac_dsp_busmux`（黄金，白名单锁定 `bitstream_distribution.xml` 等）精确示范了这套范式。
- CI 的 `change_detect` 只对 `openfpga_flow`/`docs` 之外的源码改动触发完整回归矩阵；纯改任务/脚本的 PR 走预编译镜像的 docker 回归。

## 7. 下一步学习建议

- **想给 shell 加新命令**：读 u10-l4（Shell<T> 框架），然后参照 `openfpga_setup_command_template.h` 注册一条只读命令，并用本讲的范式为它补一个回归任务。
- **想深入回归调度细节**：读 [openfpga_flow/scripts/run_fpga_task.py](https://github.com/lnis-uofu/OpenFPGA/blob/97c06e27a112c255112c48d4007b2b3a16267371/openfpga_flow/scripts/run_fpga_task.py) 的 `--maxthreads` 信号量与 `runXXX` 编号逻辑（u4-l2 已铺垫）。
- **想理解总线型 mux 的源码实现**：回到 u6-l3（`add_module_pb_bus_mux_interc`）与 u7-l2（`build_grid_bitstream` 的共享选择位约束），把本讲的回归任务与被测源码对应起来。
- **正式贡献前**：务必阅读 README 指向的官方 [contributor guidelines](https://openfpga.readthedocs.io/en/master/dev_manual/contributor_guide/) 与 [向后兼容指南](https://openfpga.readthedocs.io/en/master/dev_manual/back_compatible/)，前者规范流程，后者告诉你如何不破坏旧用户。
