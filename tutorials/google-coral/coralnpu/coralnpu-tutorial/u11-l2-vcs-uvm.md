# VCS 与 UVM 验证

> 本讲承接 [u11-l1 Verilator 仿真流程深入](u11-l1-verilator-flow.md)。Verilator 是开源、无授权、但**无微架构时序近似**的仿真器；本讲转向它的「重武器」对岸——商业 **VCS** 与基于它的 **UVM** 验证平台。前者用于跑通功能、回归海量程序；后者用于对 RTL 做深度的**定向 + 随机 + 覆盖率**回归。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **VCS 在 Bazel 中默认被关闭** 的原因，并能用 `--config=vcs` 把它打开，配置好 `VCS_HOME`/`LM_LICENSE_FILE` 等环境变量。
2. 区分 `rules/vcs.bzl` 中的两条规则 `vcs_testbench_test`（纯 SV 单元测试台）与 `vcs_binary`（带 C++ 协同仿真库的全核仿真器），并看懂它们如何被 `tags=["vcs"]` 与 tag 过滤机制控制。
3. 理解 `tests/uvm/` 这一**全核 UVM 平台**如何用一组 agent + 协同仿真 checker，把每个程序跑过 RTL 并与参考模型逐条比对。
4. 读懂 `hdl/verilog/rvv/sve/rvv_backend_tb/` 这一**后端块级 UVM 平台**的 agent / scoreboard / coverage 三件套，并说明它如何对 RVV 后端做**定向 + 随机**回归。

## 2. 前置知识

- **RTL / DUT**：寄存器传输级硬件、被测设计。本讲有两类 DUT：全核 `RvvCoreMiniVerificationAxi`（`tests/uvm/`），以及裸后端 `rvv_backend`（`rvv_backend_tb/`）。
- **仿真器（Simulator）**：把 RTL 跑起来的引擎。CoralNPU 同时支持 **VCS**（Synopsys 商业、精确、需授权）与 **Verilator**（开源、将 RTL 编译成 C++、CI 主力）。本讲重点 VCS。
- **UVM（Universal Verification Methodology）**：基于 SystemVerilog 的验证方法学库。核心三件套：
  - **Agent**：封装一个接口的「驱动（driver）+ 监视器（monitor）+ 序列器（sequencer）」，对 DUT 施加激励并采集事务。
  - **Scoreboard**：把 DUT 的实际输出与「参考模型（predictor / reference model）」的预期输出逐条比对，不一致就报 `UVM_ERROR`。
  - **Coverage（覆盖率）**：用 `covergroup` 统计功能点（指令种类、边界值组合）是否被覆盖到，量化「测得够不够」。
- **协同仿真（Co-simulation / Cosim）**：让一个外部参考模型（如 ISS 指令集模拟器）与 RTL 同步执行、逐条退休指令比对。CoralNPU 用自家的 **MPACT**（ISA 级）与开源 **Spike**。
- **定向测试 vs 随机测试**：定向（directed）= 人为枚举边界操作数与指令，针对性验证；随机（random / constrained-random）= 用 UVM sequence 生成大量随机指令流，靠覆盖率与参考模型兜底，冲击隐蔽 bug。
- **Bazel tag 过滤**：`build_tag_filters` / `test_tag_filters` 决定哪些目标参与构建/测试。CoralNPU 用它把「需要商业 EDA 授权」的目标默认排除。详见 u1-l3。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [doc/simulation.md](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/simulation.md) | VCS 启用的官方说明：环境变量、`--config=vcs`、CCACHE 排错 |
| [.bazelrc](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc) | 默认用 tag 过滤关掉 VCS；`:vcs` 配置反转过滤 |
| [rules/vcs.bzl](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl) | 两条 VCS Bazel 规则：`vcs_testbench_test`、`vcs_binary` |
| [tests/vcs_sim/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/vcs_sim/BUILD) | 用 `template_rule` 批量产出 8 个 `vcs_binary` 全核仿真器 |
| [hdl/verilog/rvv/design/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/BUILD) | `vcs_testbench_test` 的真实用法：`aligner_tb`、`multififo_tb` |
| [tests/uvm/](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/) | **全核 UVM 平台**：tb_top、env、agents、cosim checker、Makefile |
| [tests/uvm/tb/coralnpu_tb_top.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv) | 全核 UVM 顶层：例化 DUT、接口、时钟复位、ELF 后门加载 |
| [tests/uvm/env/coralnpu_env_pkg.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/env/coralnpu_env_pkg.sv) | 全核 env：装配 5 个 agent/checker |
| [hdl/verilog/rvv/sve/rvv_backend_tb/Makefile](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/Makefile) | **后端块级 UVM** 的 VCS 构建与回归 Makefile |
| [rvv_backend_tb/env/rvv_backend_env.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv) | 后端 env：rvs_agent + lsu_agent + scoreboard + cov + 参考模型 |
| [rvv_backend_tb/src/rvv_scoreboard.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_scoreboard.sv) | 后端 scoreboard：退休/VRF/访存三检查器 |
| [rvv_backend_tb/src/rvv_cov.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_cov.sv) | 后端 coverage：`covergroup cg_rx_trans` |
| [rvv_backend_tb/regress*.list](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/) | 三份回归清单：定向 / 随机 / 边界 |
| [utils/run_uvm_regression.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/utils/run_uvm_regression.py) | 全核 UVM 回归的 Python 驱动（CI 用 Verilator 跑） |

---

## 4. 核心概念与源码讲解

### 4.1 VCS 的启用与 Bazel 过滤机制

#### 4.1.1 概念说明

VCS 是 Synopsys 的商业逻辑仿真器，编译 RTL 后产出可执行 `simv`。它精确、支持 UVM 1.2、支持 FSDB 波形与全覆盖率（line/cond/branch/tgl），但**需要付费授权**，多数开发者工作站没有。CoralNPU 因此把 VCS 相关的 Bazel 目标**默认关掉**，只有显式带 `--config=vcs` 才启用。这套「默认关、按需开」的机制完全靠 **Bazel 的 tag 过滤** 实现，是理解本讲的钥匙。

#### 4.1.2 核心流程

1. 开发者先设好环境变量（`VCS_HOME`、`LM_LICENSE_FILE`，并把 `VCS_HOME/bin` 与 `linux64/lib` 加进 `PATH`/`LD_LIBRARY_PATH`）。
2. 每个 VCS 目标在 BUILD 里都自带 `tags = ["vcs"]`（由规则宏自动加，见 4.2）。
3. `.bazelrc` 默认用 `build_tag_filters="-vcs,..."` 把带 `vcs` 标签的目标**排除**，所以 `bazel test //...` 不会碰它们。
4. 加 `--config=vcs` 后，过滤条件被改写为 `build_tag_filters="vcs"`——**只保留** vcs 目标，于是它们参与构建/测试。
5. 真正的 VCS 调用由 `rules/vcs.bzl` 在 action 里发起：`vcs -full64 -sverilog ...` 编译出 `simv`，再由 Bazel 当作可执行测试/二进制跑起来。

#### 4.1.3 源码精读

**第一步：官方文档要求的环境变量。** simulation.md 列出四件套：

- 见 [doc/simulation.md:5-18](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/simulation.md#L5-L18) —— `VCS_HOME` 指向 VCS 安装根，`LM_LICENSE_FILE` 指授权，并把 `linux64/lib` 与 `bin` 接到 `LD_LIBRARY_PATH`/`PATH`。缺任一项，VCS 都会因找不到库或拿不到授权而失败。

**第二步：默认排除 + 按需开启。** `.bazelrc` 顶部注释写明动机——「most workstations will not have licenses」：

- 见 [.bazelrc:42-43](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L42-L43) —— 默认 `build --build_tag_filters="-vcs,-synthesis,-power"`，前缀 `-` 表示「排除」。`test` 命令同理（[.bazelrc:56-57](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L56-L57)）。注意这里还把 `VCS_HOME`/`LM_LICENSE_FILE` 等通过 `--action_env` 透传进构建动作（[.bazelrc:44-54](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L44-L54)），这样 VCS action 内部能读到授权。
- 见 [.bazelrc:84-89](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L84-L89) —— 这就是 `:vcs` 配置块。它把过滤改成「只含 vcs」，于是 `bazel test --config=vcs //...` 只会跑 vcs 目标。`synthesis`、`power` 也有同样模式的 `:synthesis`/`:power` 配置（[.bazelrc:91-103](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L91-L103)），分别对应综合与功耗分析这类同样需商业 EDA 的流程。

> **直觉**：tag 过滤像一道闸门。默认闸门拦住「vcs」水流；`--config=vcs` 不是「打开 vcs 那道门」，而是「换一块只放行 vcs 的滤网」。所以带 `--config=vcs` 时，普通（无 vcs 标签的）目标反而**不会**被构建——这是它和「追加启用」语义的区别，初学容易踩坑。

**第三步：CCACHE 排错。** Bazel 沙箱里 home 目录只读，ccache 会报「Failed to create temporary file」：

- 见 [doc/simulation.md:37-45](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/simulation.md#L37-L45) —— 用 `--action_env=CCACHE_DISABLE=1` 关掉 ccache 即可。这是 VCS 流程里最常见的「环境正常却编译失败」原因。

#### 4.1.4 代码实践

**目标**：在「没有 VCS 授权」的情况下，仅凭 Bazel 查询验证 tag 过滤确实在起作用。

1. 在仓库根读 `.bazelrc`，确认默认 `build_tag_filters` 含 `-vcs`。
2. 不带任何 config，查询所有带 `vcs` 标签的测试目标，再对比「实际会被 `bazel test //...` 选中的目标数」：

   ```bash
   # (a) 仓库里有哪些目标自带 vcs 标签
   bazel query 'attr(tags, "vcs", kind(".*_test rule", //...))'
   # (b) 默认配置下 bazel test 会选中哪些（应不含任何 vcs 目标）
   bazel query 'tests(//...)'
   ```

3. **观察现象**：(a) 会列出 `aligner_tb`、`multififo_tb`、`simulators_smoke_test` 等；(b) 里**不应**出现它们。
4. **预期结果**：两条命令的差集，恰好是「被 `-vcs` 过滤掉的目标」。若想确认 `--config=vcs` 反转过滤，可加 `--config=vcs` 重跑 (b)，此时应**只剩** vcs 目标（构建它们会因无授权而失败，但查询本身不需要授权）。
5. VCS 仿真器的真实编译/运行需授权，**待本地验证**（在装好 VCS 与授权的机器上：`bazel --action_env=CCACHE_DISABLE=1 test --config=vcs //hdl/verilog/rvv/design:aligner_tb`）。

#### 4.1.5 小练习与答案

**Q1**：为什么 CoralNPU 不在 BUILD 里直接删掉 vcs 目标，而是用 tag 过滤「默认关」？
**答**：因为这些目标本身正确且必要，只是在无授权机器上跑不起来。用 tag 过滤让「有授权的人一行 `--config=vcs` 即可启用、无授权的人不被打扰」，比维护两套 BUILD 更干净，也让 CI（用 Verilator）与本地（用 VCS）能共用同一份目标定义。

**Q2**：执行 `bazel build --config=vcs //hdl/verilog/rvv/design:multififo_tb` 时，会不会顺带构建 `//tests/cocotb:xxx` 这类普通目标？
**答**：不会。`--config=vcs` 把 `build_tag_filters` 改成 `"vcs"`，于是只有带 vcs 标签的目标（及其依赖）被纳入；普通 cocotb 目标无此标签，会被滤掉。

---

### 4.2 Bazel 中的两条 VCS 规则

#### 4.2.1 概念说明

`rules/vcs.bzl` 定义两条规则，对应两种「用 VCS 仿真」的场景：

- **`vcs_testbench_test`**：给一个**纯 SystemVerilog 单元测试台**（self-checking testbench）配一个 DUT，编译成 `simv` 并当作 Bazel 测试。适合 `Aligner_tb.sv`、`MultiFifo_tb.sv` 这种独立小模块的快速验证。
- **`vcs_binary`**：更重——除了 SV RTL，还能链 C++ 静态库（**协同仿真库**），并生成一个用户友好的 runner 脚本把 `--binary=xxx` 之类参数翻译成 VCS plusarg。全核仿真器（如 `rvv_core_mini_verification_axi_sim`）用它，把 MPACT 协同仿真库与 RTL 编到一起。

两条规则的宏都自动给目标打上 `tags=["vcs"]`，于是都受 4.1 的过滤机制管控。

#### 4.2.2 核心流程

`vcs_testbench_test` 流程：

```
deps(VerilogInfo) ──collect_verilog_files──▶ 过滤掉 .dat/.mem
                                            ▼
                            vcs -full64 -sverilog <所有 .sv> -o <module>
                                            ▼
                              产出 simv（名为 module）+ <module>.daidir
                                            ▼
                            作为可执行测试（test=True）由 Bazel 执行
```

`vcs_binary` 流程更复杂：

```
verilog_deps + verilog_srcs ──▶ 收集 SV 文件（pkg.sv/defs_* 排前）
deps(CcInfo)                ──▶ 收集 C++ 静态库与 .o，objcopy 去掉 .sframe 段
                                            ▼
            生成 link 脚本：vcs -full64 -sverilog -kdb +define+VCS
                           -debug_access+all -timescale=1ns/1ps -cflags -I..
                           <pkg 先、其余后> <C++ 库/对象> -o <name>_simv
                                            ▼
            另生成 runner 脚本：<name>，把 --binary/--cycles/--trace 翻成 plusarg
            并 grep 掉 Synopsys 的版权噪声
                                            ▼
            runner 是对外可执行入口；真正的 simv 在 runfiles 里
```

#### 4.2.3 源码精读

**`vcs_testbench_test` 实现**：收集依赖的 verilog 文件、跳过 `.dat/.mem`、拼出 `vcs` 命令：

- 见 [rules/vcs.bzl:20-54](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L20-L54) —— `vcs_binary_output` 取 `module` 属性同名（即 testbench 顶层模块名），`vcs_daidir_output` 是 VCS 的编译数据库目录；命令固定为 `vcs -full64 -sverilog`（[rules/vcs.bzl:33-37](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L33-L37)）。规则属性里 `deps` 必须提供 `VerilogInfo`、`module` 必填、`test=True`（[rules/vcs.bzl:56-71](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L56-L71)）。
- 见 [rules/vcs.bzl:73-74](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L73-L74) —— 对外宏 `vcs_testbench_test` 把 `["vcs"]` 拼到 `tags` 前面，这就是 4.1 里 tag 过滤能命中它的原因。

**真实用法**：在 RVV 设计库里给两个基础模块各配一个 testbench：

- 见 [hdl/verilog/rvv/design/BUILD:134-154](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/design/BUILD#L134-L154) —— `aligner_tb` 依赖 `:aligner`，`multififo_tb` 依赖 `:multififo`，顶层模块名分别是 `Aligner_tb`/`MultiFifo_tb`。这正是 simulation.md 给的示例在仓库里的落点。

**`vcs_binary` 实现**：处理 C++ 依赖、剥离 `.sframe`、生成 runner：

- 见 [rules/vcs.bzl:78-151](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L78-L151) —— 关键三步：(1) 从 `CcInfo` 依赖里收集 include 路径、静态库与对象文件；(2) 用 `objcopy --remove-section=.sframe` 剥离每个库/对象（这呼应 `.bazelrc` 里 GCC 15 + BFD 链接器的 `.sframe` 兼容处理）；(3) 拼 `vcs` 命令，含 `+define+VCS`、`-debug_access+all`、`-kdb`、`+notimingcheck`、`-timescale=1ns/1ps`，并把 `*pkg.sv`/`defs_*` 文件排在最前（SystemVerilog 包必须先编译）。
- 见 [rules/vcs.bzl:179-210](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L179-L210) —— 生成的 runner 脚本把命令行 `--binary=X`/`--cycles=N`/`--trace` 翻译成 VCS plusarg `+binary=X`/`+cycles=N`/`+trace`，并在尾部 `grep -v` 掉 Synopsys 版权横幅等噪声，让输出干净。
- 见 [rules/vcs.bzl:252-253](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L252-L253) —— 同样自动打 `["vcs"]` 标签。

**全核仿真器批量生成**：`tests/vcs_sim/BUILD` 用 u11-l1 介绍过的 `template_rule` 把 `vcs_binary` 实例化 8 份：

- 见 [tests/vcs_sim/BUILD:20-84](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/vcs_sim/BUILD#L20-L84) —— 每个变体指定不同的 `verilog_deps`（不同 Chisel 顶层）与 `+define+DUT_MODULE=...`。例如 `rvv_core_mini_verification_axi_sim` 带上 `+define+VLEN_128 +define+ZVE32F_ON +define+TB_SUPPORT +define+USE_GENERIC`（[tests/vcs_sim/BUILD:69-78](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/vcs_sim/BUILD#L69-L78)），并依赖 `//hdl/verilog:sram_backdoor`（DPI 后门 SRAM，u6-l2/u11-l1 讲过）。
- 见 [tests/vcs_sim/BUILD:86-97](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/vcs_sim/BUILD#L86-L97) —— `simulators_smoke_test` 是一个 `sh_test`，也带 `tags=["vcs"]`，用其中一个仿真器加载 `nop_test.elf` 做「能跑起来」的冒烟验证。这是 VCS 全核流程在 Bazel 里的最小入口。

#### 4.2.4 代码实践

**目标**：从 BUILD 反推一条 vcs 目标「会以什么命令被编译」。

1. 打开 [tests/vcs_sim/BUILD](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/vcs_sim/BUILD)，挑 `rvv_core_mini_verification_axi_sim`。
2. 列出它的 `build_args`（5 个 `+define+`）与 `verilog_deps`（哪个 Chisel `_cc_library_verilog` 目标）。
3. 对照 [rules/vcs.bzl:138-151](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L138-L151)，**手写**出这条目标最终 `vcs` 命令里会出现的开关（应有 `-full64 -sverilog -kdb +define+VCS -debug_access+all +notimingcheck -timescale=1ns/1ps`，加上 5 个 `build_args` 的 define）。
4. **观察 / 预期**：`+define+VLEN_128` 决定了 VLEN（u7 系列讲过 RTL 当前按 VLEN_128 构建），`+define+TB_SUPPORT` 打开 testbench 专用观测端口。若把 `VLEN_128` 换成别的值，仿真器与 RTL 参数会不匹配——这正是 define 在 VCS 流程里承担「配置旋钮」的角色。
5. 真实编译需授权，**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：`vcs_testbench_test` 与 `vcs_binary` 都自动打 `vcs` 标签，但一个 `test=True`、一个 `executable=True`。这带来什么行为差异？
**答**：`vcs_testbench_test` 是测试目标，会被 `bazel test` 当作用例执行并通过返回码判定 PASS/FAIL；`vcs_binary` 是可执行二进制，用 `bazel run` 启动，常作为「仿真器」被其他测试（如 `simulators_smoke_test`）或脚本（如 `run_uvm_regression.py`）当工具调用，自身不充当「通过/失败」判定的测试。

**Q2**：为什么 `vcs_binary` 要把 `*pkg.sv` 与 `defs_*` 文件排在编译列表最前？
**答**：SystemVerilog 的 `package` 必须先于使用它的模块被编译，否则 VCS 报「unknown identifier」。把包定义与宏头提前，避免依赖文件名字典序带来的随机编译失败。

---

### 4.3 tests/uvm：全核 UVM 平台与协同验证

#### 4.3.1 概念说明

`tests/uvm/` 是一个**完整 SoC 级**的 UVM 验证环境，DUT 是 `RvvCoreMiniVerificationAxi`（RVV 核的「验证变体」——多暴露了 RVVI 追踪端口，u9-l2 讲过）。它的思路与 cocotb（u2-l4）同构：测试台扮演「外部主机」，经 AXI 把程序/数据灌进 DUT、启动核、等停机、查结果；区别在于它用 **UVM** 组织激励，并挂了一个**协同仿真 checker**，把 RTL 每条退休指令与 MPACT 参考模型（可选再叠加 Spike）逐条比对。这套平台由 `Makefile` 驱动，可切 VCS 或 Verilator（`SIMULATOR=verilator`）。

#### 4.3.2 核心流程

```
                 ┌─ coralnpu_axi_master_agent  ─▶ 驱动 DUT slave 口（写程序/CSR）
coralnpu_env ────┼─ coralnpu_axi_slave_agent   ─▶ 响应 DUT master 口（模拟外部存储）
                 ├─ coralnpu_irq_agent         ─▶ 驱动 irq/te 控制信号
                 ├─ coralnpu_rvvi_agent (被动) ─▶ 采样 RVVI 退休流（参考 u9-l2）
                 └─ coralnpu_cosim_checker     ─▶ MPACT + Spike 协同比对

tb_top:  例化 DUT + 接口 ─▶ 时钟/复位 ─▶ DPI 后门加载 ELF ─▶ 监测 tohost ─▶ run_test()
```

回归层面，`utils/run_uvm_regression.py` 用 `bazel query` 把所有 `coralnpu_v2_binary` 程序捞出来，编译成 ELF，逐个喂给这套 UVM 平台跑，结果汇总成 CSV。

#### 4.3.3 源码精读

**顶层 testbench**：例化 DUT、连接口、加载 ELF：

- 见 [tests/uvm/tb/coralnpu_tb_top.sv:38-41](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv#L38-L41) —— AXI 参数：地址 32 位、数据 **128 位**、ID 6 位（与 u3-l2 一致）。
- 见 [tests/uvm/tb/coralnpu_tb_top.sv:66-68](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv#L66-L68) —— RVVI 虚接口参数 `VLEN=128, RETIRE=8`（每拍最多退休 8 条，u9-l2）。
- 见 [tests/uvm/tb/coralnpu_tb_top.sv:74-187](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv#L74-L187) —— 例化 `RvvCoreMiniVerificationAxi u_dut`，把 `master_axi_if` 接到核的 slave 口、`slave_axi_if` 接到核的 master 口、`irq_if` 接控制/状态信号。
- 见 [tests/uvm/tb/coralnpu_tb_top.sv:249-311](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv#L249-L311) —— `tohost` 监视：每拍查 `io_dbus`/`io_ebus` 是否写到 `tohost` 地址且 bit0 为 1（程序成功结束的经典 RISC-V 约定，呼应 u2-l3 的 mailbox/`mpause` 停机语义），命中则触发 `tohost_written_event`。
- 见 [tests/uvm/tb/coralnpu_tb_top.sv:317-342](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv#L317-L342) —— 把各虚接口经 `uvm_config_db` 下发给 agent，第 319 行从 DUT 内部层级 `u_dut.core.score.rvvi.rvviTraceBlackBox.rvvi` 取出 RVVI 接口（与 u9-l2 的 RvviTrace 对接），最后 `run_test()` 启动 UVM。

**环境（env）**：装配 agent 与 checker：

- 见 [tests/uvm/env/coralnpu_env_pkg.sv:34-42](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/env/coralnpu_env_pkg.sv#L34-L42) —— `coralnpu_env` 持有五个组件：主/从 AXI agent、IRQ agent、被动 RVVI agent、cosim checker。注释点明各自职责（master 驱动 DUT slave 口、slave 响应 DUT master 口、cosim 对接 MPACT）。
- 见 [tests/uvm/env/coralnpu_env_pkg.sv:50-62](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/env/coralnpu_env_pkg.sv#L50-L62) —— `build_phase` 用工厂创建这五个组件。

**文件清单与启动序列**：编译顺序由 `coralnpu_dv.f` 固定：

- 见 [tests/uvm/coralnpu_dv.f:33-56](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/coralnpu_dv.f#L33-L56) —— 先编译 DPI 后门 `sram_backdoor.cc`（u6-l2 的 backdoor 加载基础），再各接口，再按依赖顺序的 package，最后 `coralnpu_tb_top.sv`。
- 测试包里的 `coralnpu_kickoff_write_seq` 用三条 AXI 写复现了 u3-l5 的启动序列：见 [tests/uvm/tests/coralnpu_test_pkg.sv:42-110](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tests/coralnpu_test_pkg.sv#L42-L110) —— 写 `0x30004`（PC_START）、写 `0x30000=1`（开钟保持复位）、写 `0x30000=0`（释放复位）。注意 128 位总线上的字节选通（`strb`）要把 32 位数据摆到正确车道（地址 `...4` 用 `16'h00F0`，地址 `...0` 用 `16'h000F`）。

**回归驱动**：`run_uvm_regression.py` 把这一切自动化：

- 见 [utils/run_uvm_regression.py:132-189](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/utils/run_uvm_regression.py#L132-L189) —— `get_targets` 用 `bazel query kind(coralnpu_v2_binary, //...)` 枚举所有 CoralNPU 程序，按 `DENYLIST`（[utils/run_uvm_regression.py:38-90](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/utils/run_uvm_regression.py#L38-L90)）剔除已知不兼容项（如 RVV 异常 MPACT 暂不支持、需外部中断的测试）。
- 见 [utils/run_uvm_regression.py:314-409](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/utils/run_uvm_regression.py#L314-L409) —— `run_uvm` 调 `make -C tests/uvm run TEST_ELF=<elf>`，并用正则在输出里捞 `UVM_FATAL/UVM_ERROR`（排除汇总行「: 0」）来判 PASS/FAIL；遇 `1-800-VERILOG`（授权失败）自动重试 3 次。

> **直觉**：这套平台把 cocotb 的「Python 当主机」换成了「UVM 当主机 + MPACT 当裁判」。RTL 与参考模型同跑同一份 ELF，退休指令逐条对账——RTL 少写、多写、值错任何一类都会被 cosim checker 抓出（u9-l2 的 RVVI 比对正是 checker 的数据源）。

#### 4.3.4 代码实践

**目标**：理清全核 UVM 的激励与比对通路（无需授权，纯阅读）。

1. 在 [coralnpu_env_pkg.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/env/coralnpu_env_pkg.sv) 里数出 env 装配了几个 agent，分别接到 DUT 哪一侧。
2. 在 [coralnpu_tb_top.sv:322-336](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/uvm/tb/coralnpu_tb_top.sv#L322-L336) 里确认：master agent 的虚接口接到 `master_axi_if`（驱动 DUT **slave** 口），而 RVVI 虚接口同时下发给 `m_cosim_checker` 与 `m_rvvi_agent`——说明退休流是「checker 比对」与「被动监视」共享的数据源。
3. 在 [run_uvm_regression.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/utils/run_uvm_regression.py) 里追踪一个 ELF 的生命周期：`get_targets` → `build_targets` → `get_elf_source_path` → `run_uvm`（`make run TEST_ELF=...`）。
4. **预期结果**：能画出「程序 ELF → AXI master 灌入 → DUT 执行 → RVVI 退休流 → cosim checker 与 MPACT 比对 → UVM_ERROR/PASS」的完整链路。
5. 真实跑通需 VCS（或 Verilator + UVM-1.2 移植）与 MPACT，**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：`tests/uvm/` 的 cosim 比对，比的是「指令退休」还是「总线事务」？
**答**：主要是指令退休——RVVI 接口（u9-l2）每拍报告退休指令及其 GPR/FPR/VPR/CSR 写，cosim checker 据此与 MPACT（可选 Spike）逐条对账。这比对「总线事务」更接近功能正确性，因为乱序执行下同一总线序列可能对应不同退休顺序。

**Q2**：`run_uvm_regression.py` 为什么维护一个 `DENYLIST`？
**答**：部分测试在功能上正确，但参考模型（MPACT）尚未支持对应语义（如某些 RVV 异常、bf16），或测试依赖 UVM 后门加载器尚不支持的机制（如加载到外部 `.extdata`）。这些若不剔除会因「参考模型对不上」而误报 FAIL，故列入黑名单待后续修复。

---

### 4.4 rvv_backend_tb：后端块级 agent/scoreboard/coverage 三件套与定向+随机回归

#### 4.4.1 概念说明

`hdl/verilog/rvv/sve/rvv_backend_tb/` 是**只针对 RVV 后端**（`rvv_backend`，即 u7 系列讲的向量/矩阵执行引擎）的块级 UVM 平台。它不跑整核程序，而是用 UVM sequence 直接向后端的命令接口（`rvs_interface`）**注入向量指令**，再用一个**SystemVerilog 参考模型** `rvv_behavior_model`（mdl）按 RVV ISA 语义算出预期结果，由 scoreboard 把 DUT 与 mdl 逐条退休比对。这是教科书式的 UVM「agent + scoreboard + coverage」三件套，也是 CoralNPU 对 RVV 后端做**定向 + 随机 + 边界**回归的主战场。它自带一份 VCS `Makefile`，独立于 Bazel。

#### 4.4.2 核心流程

```
                      UVM sequence (alu/smoke/directed/random)
                                   ▼
                         rvs_agent (rvs_drv 驱动 rvs_if)
                          │            │
                  inst_ap │            │ rt_ap（退休流）
                          ▼            ▼
              rvv_behavior_model    rvv_scoreboard
               (mdl: 软件算预期)     rt_checker / vrf_checker / mem_access_checker
                          │            ▲
                  rt_ap ──┴────────────┘  （DUT 退休 vs mdl 退休）

   lsu_agent (lsu_drv): 驱动/观测访存，mem_ap ──▶ scoreboard.mem_access_checker
   rvv_cov: inst_ap/rt_ap ──▶ covergroup cg_rx_trans (alu_inst × vxsat 交叉覆盖)
```

回归用 Makefile 调 VCS，按 `regress.list`（定向）/`regress_random.list`（随机）/`regress_corner.list`（边界）三份清单选测试名，逐个 `+UVM_TESTNAME=<test>` 跑，可选 `coverage=on` 收覆盖率。

#### 4.4.3 源码精读

**环境装配**：`rvv_backend_env` 把三件套与参考模型接到一起：

- 见 [rvv_backend_tb/env/rvv_backend_env.sv:6-13](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv#L6-L13) —— env 持有：`rvs_agt`（指令驱动 agent）、`lsu_agt`（访存 agent）、`scb`（scoreboard）、`cov`（覆盖率）、`mdl`（参考模型）。
- 见 [rvv_backend_tb/env/rvv_backend_env.sv:44-76](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv#L44-L76) —— `connect_phase` 用 UVM analysis port 把数据流接好。关键连线：
  - `rvs_mon.inst_ap` 同时连给 `mdl.inst_imp`（参考模型跟着消费指令）与 `lsu_drv.inst_imp`（访存驱动也需知道当前指令）——[rvv_backend_env.sv:47-48](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv#L47-L48)。
  - 退休比对：DUT 侧 `rvs_mon.rt_ap → scb.rvs_imp`，参考模型侧 `mdl.rt_ap → scb.mdl_imp`——[rvv_backend_env.sv:58-59](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv#L58-L59)。这就是「DUT 退休 vs mdl 退休」的对照入口。
  - VRF 比对、访存比对、覆盖率同理——[rvv_backend_env.sv:66-75](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv#L66-L75)。

**Agent**：`rvs_agent` 是主动 agent（sequencer+driver+monitor+独立的 vrf_monitor）：

- 见 [rvv_backend_tb/env/rvs_agent.sv:6-14](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvs_agent.sv#L6-L14) —— `UVM_ACTIVE` 时创建 `rvs_sqr`/`rvs_drv`，并常驻 `rvs_mon`/`vrf_mon`。driver 从 sequencer 拉事务驱动 `rvs_if`，monitor 采样接口产出 analysis 事务。
- 见 [rvv_backend_tb/env/lsu_agent.sv:6-10](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/lsu_agent.sv#L6-L10) —— `lsu_agent` 持 `lsu_drv`（内含行为级存储模型 `mem`）与 `lsu_mon`，负责后端访存的激励与观测。

**Scoreboard**：三类检查器并行跑：

- 见 [rvv_backend_tb/src/rvv_scoreboard.sv:7-13](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_scoreboard.sv#L7-L13) —— 用 `uvm_analysis_imp_decl` 声明 7 个分析端口（rvs/mdl/rvs_vrf/mdl_vrf/lsu_mem/mdl_mem/scb_ctrl），把 DUT 与参考模型两路数据分别入队。
- 见 [rvv_backend_tb/src/rvv_scoreboard.sv:91-98](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_scoreboard.sv#L91-L98) —— `main_phase` fork 三个并行检查器：`rt_checker`（退休比对）、`vrf_checker`（向量寄存器堆全量比对）、`mem_access_checker`（访存比对）。
- 见 [rvv_backend_tb/src/rvv_scoreboard.sv:163-317](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_scoreboard.sv#L163-L317) —— `rt_checker` 是核心：DUT 与 mdl 的退休事务**按序**成对出队，逐一比对 VRF 写（索引、字节使能 strobe、数据）、XRF 写、vxsat（饱和标志）、trap（异常种类）。注意第 200-203 行把 RTL 的**字节使能**展开成位使能再比对——呼应 u7-3 讲的「VRF 字节级写使能支持 tail/mask 语义」。
- 见 [rvv_backend_tb/src/rvv_scoreboard.sv:414-469](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_scoreboard.sv#L414-L469) —— `final_phase` 做收尾：全量比对 `lsu_drv.mem` 与 `mdl.mem`、检查所有队列已清空（防止漏比对）、核对 DUT 与 mdl 执行的指令数一致。

**Coverage**：用 `covergroup` 量化功能覆盖：

- 见 [rvv_backend_tb/src/rvv_cov.sv:6-21](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_cov.sv#L6-L21) —— `rvv_cov` 持 `inst_rx_cov_imp`（退休侧）与 `inst_tx_cov_imp`（注入侧）两个 analysis 端口，由事件触发覆盖采样。
- 见 [rvv_backend_tb/src/rvv_cov.sv:41-47](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_cov.sv#L41-L47) —— `covergroup cg_rx_trans` 采样 `alu_inst`（ALU 指令种类）与 `vxsat[0]`（是否饱和）两个 coverpoint，并做 `cross`（交叉覆盖，统计「每条指令在饱和/非饱和两种情形下都出现过」）。这是块级功能覆盖率的范例；完整覆盖率模型还分布在 [include/rvv_zve32x_coverage.svh](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/include/rvv_zve32x_coverage.svh) 与 `hdl/rvv_interface_cov.sv`。

**参考模型**：`rvv_behavior_model` 用 SV 实现 RVV 语义：

- 见 [rvv_backend_tb/src/rvv_behavior_model.sv:24-60](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_behavior_model.sv#L24-L60) —— 它维护 `xrf`/`vrf`、vcsr（vma/vta/vsew/vlmul/vl/vstart/vxrm/vxsat）等架构状态，并按 SEW 宽度参数化实例化 `alu_processor`（如 `alu_08_08_08`、`alu_16_16_16`），逐条消费 monitor 送来的指令、算出预期 VRF/XRF/vxsat/trap，再经 `rt_ap` 回送 scoreboard。

**Makefile 与回归清单**：VCS 构建参数与三种回归：

- 见 [rvv_backend_tb/Makefile:51-80](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/Makefile#L51-L80) —— `scfg` 选「微架构配置」：`rvv_backend`（默认，`ISSUE_3_READ_PORT_6`）、`rvv_backend_i2rp4`/`i2rp6`/`i3rp6`（不同发射宽度/读端口数，对应 u7-2 的结构冒险参数），每种通过 `+define+ISSUE_x_READ_PORT_y` 切换。RTL 还统一带 `+define+TB_SUPPORT +define+ASSERT_ON +define+RVV_CONFIG_SVH`。
- 见 [rvv_backend_tb/Makefile:118-123](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/Makefile#L118-L123) —— `coverage=on` 时加 `-cm cond+line+branch+tgl`（条件/行/分支/翻转覆盖率）并去掉 `TB_SUPPORT`（避免 testbench 代码计入 DUT 覆盖率）。
- 见 [rvv_backend_tb/Makefile:162-197](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/Makefile#L162-L197) —— `VCOMP` 编译命令（`vcs -full64 -sverilog`、UVM、断言、timescale、`-top rvv_backend_top`）；`VSIM` 仿真命令带 `+UVM_TESTNAME`、`+ntb_random_seed`（随机种子，可复现）、`+UVM_MAX_QUIT_COUNT`、`+UVM_TIMEOUT`。
- 三份回归清单：[regress.list](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/regress.list)（**定向**：按指令类别 `alu_vaddsub_test`/`alu_vmul_test`/`lsu_*` 等枚举）、[regress_random.list](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/regress_random.list)（**随机**：`alu_random_test`/`rvv_random_test` 等大随机流）、[regress_corner.list](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/regress_corner.list)（**边界**：`alu_div_zero_test`/`alu_large_vstart_test`/`rvv_reset_test` 等）。

**定向 vs 随机的写法**：测试类里两种风格并存：

- 见 [rvv_backend_tb/tests/rvv_backend_test.sv:21-22](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/tests/rvv_backend_test.sv#L21-L22) —— 基类定 `direct_inst_num=1000`、`random_inst_num=50000`（`qualify` 模式下缩减，[rvv_backend_test.sv:54-60](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/tests/rvv_backend_test.sv#L54-L60)）。
- 见 [rvv_backend_tb/tests/rvv_backend_test.sv:410-466](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/tests/rvv_backend_test.sv#L410-L466) —— `alu_vaddsub_test` 是典型「定向 + 随机」混合：先用 `run_inst_iter` **遍历**边界操作数（VV/VX/VI 三种源、带/不带掩码），再用 `run_inst_rand` 对单条指令做大量随机，最后 `run_rand_with_set` 在指令集合内随机混合。`rvs_last_sequence` 收尾保证 ROB 排空、触发最终比对。

> **直觉**：定向测试保证「每个指令的每个边界都被显式打到」（覆盖广度 + 已知 corner）；随机测试用海量随机组合 + 参考模型兜底，去撞「人想不到的组合」（覆盖深度）。两者互补，正是 UVM 方法学的精髓。scoreboard 用同一套比对逻辑同时服务二者，参考模型 `rvv_behavior_model` 是「正确性真相源」。

#### 4.4.4 代码实践

**目标**：梳理 agent/scoreboard/coverage 三件套，并区分三类回归（纯阅读）。

1. **三件套配线**：在 [rvv_backend_env.sv:44-76](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/env/rvv_backend_env.sv#L44-L76) 里，列出每条 `connect` 语句把「谁的哪个 port」连到「谁的哪个 imp」，画成表格。重点标出退休比对那对（`rvs_mon.rt_ap`↔`scb.rvs_imp`、`mdl.rt_ap`↔`scb.mdl_imp`）。
2. **scoreboard 比对维度**：在 [rvv_scoreboard.sv:163-317](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_scoreboard.sv#L163-L317) 里，数出 `rt_checker` 比对了哪几个维度（答案：VRF 索引/strobe/数据、XRF 索引/数据、vxsat、trap 的多个子字段）。
3. **三类回归归类**：把 `regress.list` / `regress_random.list` / `regress_corner.list` 各挑 2 个测试名，对照 [rvv_backend_test.sv](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/tests/rvv_backend_test.sv) 里对应的类，判断它偏定向（`run_inst_iter`）还是偏随机（`run_inst_rand`/`alu_random_seq`）。
4. **预期结果**：能口述「一条 `vadd` 指令从 sequence → rvs_drv 驱动 → DUT 执行 → rvs_mon 采退休 → scoreboard 与 mdl 比对 → 命中即报 `UVM_ERROR`」的完整路径。
5. 真实跑回归需 VCS 授权，**待本地验证**（例如 `cd hdl/verilog/rvv/sve/rvv_backend_tb && make scfg=rvv_backend sim test=alu_vaddsub_test coverage=on`）。

#### 4.4.5 小练习与答案

**Q1**：scoreboard 里 DUT 与参考模型的退休事务为什么要「按序成对」比对，而不是各跑各的最后比寄存器堆快照？
**答**：按序成对比对能定位到「具体哪一条指令、哪一个寄存器、哪一字节」出错（见 `rt_checker` 里详尽的 `uvm_error` 信息），调试效率高；而快照比对只能告诉你「某处不一致」，定位困难。按序比对还能在指令数不一致时立即 `uvm_fatal`（`rt_queue_mdl.size() != rt_queue_rvs.size()`），及早暴露「DUT 多退休/少退休/跑飞」的严重问题。

**Q2**：`coverage=on` 时 Makefile 为什么要 `filter-out +define+TB_SUPPORT`？
**答**：`TB_SUPPORT` 会把 testbench 专用代码编进设计。收 DUT 覆盖率时若连 testbench 代码一起统计，会把 testbench 分支也算进分母，稀释真实 DUT 覆盖率。故收覆盖率时去掉它，让 `-cm` 只度量 RVV 后端 RTL 本身。

**Q3**：随机回归怎么保证「可复现」？
**答**：Makefile 用 `+ntb_random_seed=$(seed)`（[Makefile:103-107](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/Makefile#L103-L107)）。失败时记下种子，下次用同样种子即可复现同一组随机指令流，定位 bug。

---

## 5. 综合实践

**任务**：把本讲四块内容串成一条「从启用 VCS 到读懂一条 RVV 回归用例」的完整链路。假设你拿到一台装好 VCS 与授权的机器，请按下列步骤在仓库里走查（无法实际执行的步骤标注假设）：

1. **启用 VCS**：按 [doc/simulation.md:5-18](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/simulation.md#L5-L18) 导出四个环境变量；用 `bazel query 'attr(tags, "vcs", tests(//...))'` 确认能查到 vcs 目标；说明为什么必须加 `--config=vcs` 才会真正构建它们（引用 [.bazelrc:42-43](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L42-L43) 与 [.bazelrc:84-89](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/.bazelrc#L84-L89)）。

2. **跑一个 Bazel VCS 单元测试**：执行 `bazel --action_env=CCACHE_DISABLE=1 test --config=vcs //hdl/verilog/rvv/design:aligner_tb`。对照 [rules/vcs.bzl:20-54](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/rules/vcs.bzl#L20-L54) 解释这条命令在 action 里实际跑的 `vcs` 命令长什么样、产物 `aligner` 与 `aligner.daidir` 各是什么。（**待本地验证**）

3. **梳理两个 UVM 平台的分工**：画一张对照表，区分 `tests/uvm/`（全核、DUT=`RvvCoreMiniVerificationAxi`、参考模型=MPACT/Spike、驱动=Python `run_uvm_regression.py`、输入=ELF 程序）与 `rvv_backend_tb/`（块级、DUT=`rvv_backend`、参考模型=`rvv_behavior_model`、驱动=Makefile+UVM sequence、输入=向量指令流）。指出哪个平台做「定向+随机+覆盖率」回归（答案：后者）。

4. **追踪一次后端比对**：任选 `regress.list` 里的 `alu_vaddsub_test`，从 [rvv_backend_test.sv:410-466](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/tests/rvv_backend_test.sv#L410-L466) 出发，写一段「指令从 sequence 到 scoreboard 报错」的文字追踪，标注它先做定向遍历（`run_inst_iter`）再做随机（`run_inst_rand`），并说明 `rvv_behavior_model` 在其中扮演的角色。

5. **收覆盖率**：写出后端块级平台收覆盖率的命令（`make scfg=rvv_backend coverage=on sim test=alu_vaddsub_test`），并解释 `-cm cond+line+branch+tgl` 与 [rvv_cov.sv:41-47](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/verilog/rvv/sve/rvv_backend_tb/src/rvv_cov.sv#L41-L47) 的功能覆盖（`cg_rx_trans`）分别是代码覆盖与功能覆盖两类。（**待本地验证**）

**验收标准**：能不看讲义，向同伴讲清「`--config=vcs` 起什么作用、两条 vcs 规则的区别、两个 UVM 平台各自比什么、定向与随机如何互补」。

## 6. 本讲小结

- CoralNPU 默认用 `.bazelrc` 的 tag 过滤（`-vcs`）**关掉** VCS 目标，因为多数工作站无授权；`--config=vcs` 把过滤改写为「只含 vcs」，从而启用它们，配合 `VCS_HOME`/`LM_LICENSE_FILE` 等环境变量与 `CCACHE_DISABLE=1` 排错。
- `rules/vcs.bzl` 提供两条规则：`vcs_testbench_test`（纯 SV 单元测试台、`test=True`）与 `vcs_binary`（链 C++ 协同仿真库、生成 runner 脚本、`executable=True`）；两者都自动打 `tags=["vcs"]`。
- `tests/uvm/` 是**全核 UVM 平台**，DUT 为 `RvvCoreMiniVerificationAxi`，由 5 个 agent/checker（AXI 主/从、IRQ、被动 RVVI、cosim）组成，用 MPACT（可选 Spike）对每条退休指令做协同比对，由 `run_uvm_regression.py` 驱动海量 ELF 回归（CI 用 Verilator 跑）。
- `rvv_backend_tb/` 是**后端块级 UVM 平台**，标准「agent（rvs/lsu）+ scoreboard + coverage」三件套，用 `rvv_behavior_model` 作参考模型，对退休/VRF/访存三类做逐条比对，是 RVV 后端定向+随机+边界回归的主战场。
- 三份回归清单对应三种策略：`regress.list`（定向遍历指令与边界）、`regress_random.list`（大随机指令流，靠种子可复现）、`regress_corner.list`（除零/复位/大 vstart 等已知 corner）；覆盖率分代码覆盖（`-cm`）与功能覆盖（`covergroup`）。

## 7. 下一步学习建议

- **续读硬件可观测性**：本讲的 cosim 比对与 coverage 依赖 RVVI 追踪接口，建议回看 [u9-l2 RVVI 指令追踪与仿真观测]——理解退休流数据从何而来。
- **续读 RVV 后端实现**：scoreboard 比对的 VRF 字节使能、stripmining、MAC 外积等，分别在 u7-3、u7-6、u7-4 讲过；读懂这些能让回归用例的设计意图更清晰。
- **下一讲** [u11-l3 cocotb 回归测试体系] 将转向 CoralNPU 规模最大的 Python 回归（`tests/cocotb`），与本讲的 UVM 回归形成「Python 轻量回归 vs SystemVerilog 重量回归」的对照；之后再进入 [u11-l4 FPGA 构建与比特流生成]。
- **上手实践**：若有 VCS 授权，先把 `aligner_tb`/`multififo_tb` 跑通（最小闭环），再尝试 `tests/vcs_sim` 的全核仿真器加载一个 ELF，最后挑战 `rvv_backend_tb` 的定向回归并开启覆盖率。
