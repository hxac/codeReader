# 测试与回归流程

> 本讲对应大纲：`u13-l1`，专家层。前置讲义：`u1-l4`（首次运行：用 blackbox.sh 跑通 demo）。
> 当前 HEAD：`d76b7f24e658867ab57e3942d7c648c3e6af072d`

## 1. 本讲目标

Vortex 是一个「SimX 仿真器 + RTL + FPGA」三套实现必须逐拍对齐的全栈 GPU（见 `u7-l4` 的 model_parity 主线）。这意味着它的测试体系不是可有可无的附属品，而是**整个工程纪律的地基**——任何 RTL 改动都必须有测试证明它没有破坏 SimX↔RTL 一致性。

本讲聚焦于「如何跑测试、测试是如何组织的」这条**测试基础设施**主线，学完后你应当能够：

1. 说出 `tests/` 下十一类测试套件各自的定位，以及一个最小回归测试的「三件套」结构。
2. 用 `make -C tests/regression run-simx` / `run-rtlsim` 跑通默认回归套件，并解释其背后的 `TESTS`/`EXCLUDE`/`run-<backend>` 模式规则机制。
3. 用 `ci/regression.sh --all` / `--test <selector>` 驱动 CI v2 的 pytest 目录，理解它是 `pytest ci` 的薄封装。
4. 读懂 `ci/testcases/*.yaml` 声明式目录的字段（`via`/`drivers`/`shape`/`check`/`tier`/`known_issue`），理解「一个测试用例是 N 维空间中的一个点」这一核心模型。

> 本讲**只讲测试的组织与运行**；具体的 model_parity / perf_gate 判定细节、CI 门控纪律将在 `u13-l4`（持续集成与 model_parity 门控）深入，调试技巧在 `u13-l2` 展开。

## 2. 前置知识

阅读本讲前，你应当已经（来自 `u1-l3`、`u1-l4`、`u2-l1`）：

- 在 `build/` 目录里运行过 `../configure`，知道所有 `ci/*.sh` 脚本都是 `ci/*.sh.in` 模板被 configure 烘焙了绝对路径后的产物（例如 `@XLEN@`、`@VORTEX_HOME@` 占位符会被替换）。
- 用过 `ci/blackbox.sh --driver=simx --app=demo`，知道它把人类友好的旋钮（`--cores`/`--warps`/`--threads`）翻译成 `VX_CFG_*` 宏塞进 `CONFIGS` 变量。
- 知道「配置必须在驱动侧与应用侧同时生效」这条纪律：`CONFIGS` 改了，应用和驱动都要用同一套宏重新编译。

几个本讲会用到的术语：

- **driver（驱动后端）**：程序的执行后端，有 `simx`（C++ 仿真，最便宜）、`rtlsim`（Verilator 仿真 RTL，较贵）、`opae`/`xrt`（FPGA）。成本从低到高。
- **CONFIGS**：一组 `-DVX_CFG_*` 宏，临时覆盖 `VX_config.toml` 的硬件基线（如 `-DVX_CFG_NUM_THREADS=8`）。
- **tier（运行层级）**：一个测试「何时该跑」的轴，取值 `smoke`（每次 push）/`full`（PR + 每晚）/`nightly`（每晚/每周）。
- **目录（catalog）**：`ci/testcases/*.yaml` 里声明的全部测试用例集合，是 CI 与本地跑测试的**单一真相来源**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/testing.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/testing.md) | 测试入门文档：如何跑应用、跑回归、创建自己的回归测试 |
| [tests/regression/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/Makefile) | 回归套件的总 Makefile：`TESTS` 主表 + `run-<backend>` 聚合目标 |
| [ci/regression.sh.in](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in) | CI v2 本地测试入口模板（configure 后生成 `ci/regression.sh`） |
| [ci/testcases/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/) | 声明式测试目录，~35 个 YAML 文件，每文件一个 category |
| [ci/testcase.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py) | 目录的数据模型 + 规划器 CLI（lint/matrix/select） |
| [ci/conftest.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py) | pytest 钩子：把每个用例变成带 marker 的参数化测试项 |
| [ci/test_runner.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py) | 真正执行用例的 `test_case()`，shell 调用 blackbox/make 并断言退出码 |
| [docs/designs/continuous_integration.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md) | CI 架构设计文档：N 维模型、引擎、event×tier×driver 矩阵 |

## 4. 核心概念与源码讲解

### 4.1 tests 套件组织与单个回归测试的结构

#### 4.1.1 概念说明

Vortex 的全部可执行测试程序放在仓库的 [tests/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/) 下，按**被测对象的性质**分成十一类一级套件：

| 套件 | 测什么 |
|------|--------|
| `tests/regression` | 核心 GPU 功能回归（58 个程序：vecadd、sgemm、sort、amo、printf…），最大、最常用 |
| `tests/kernel` | 设备内核运行时自身（hello、fibonacci 等裸内核镜像） |
| `tests/opencl` | 经 PoCL 的 OpenCL 1.2 支持 |
| `tests/vulkan` | 经 mesa-vortex 的 Vulkan 支持 |
| `tests/hip` | 经 chipStar 的 HIP 支持 |
| `tests/graphics` | 图形固定功能流水线（RASTER/TEX/OM） |
| `tests/raytracing` | 硬件光线追踪单元（RTU） |
| `tests/riscv` | RISC-V ISA 合规（含 RVC 压缩指令） |
| `tests/runtime` | 主机运行时 API |
| `tests/mpi` | 多片 GPU 的 MPI 协同 |
| `tests/unittest` | C++ 单元测试 |

其中 `tests/regression` 是日常迭代最常跑的套件，也是本节的主角。

#### 4.1.2 核心流程：一个回归测试的三件套

`docs/testing.md` 在「Creating Your Own Regression Test」一节给出了一份回归测试的标准结构——任何一个 `tests/regression/<name>/` 子目录通常由三个文件组成（见 [docs/testing.md:L45-L59](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/testing.md#L45-L59)，此处用中文转述其约定）：

- **`kernel.cpp`**：GPU 内核代码（设备侧，会被 RISC-V clang 编译成 `.vxbin`）。
- **`main.cpp`**：主机 CPU 代码（打开设备→分配显存→拷入→启动→拷回→校验，对齐 CUDA/OpenCL 主机接口，见 `u3-l1`）。
- **`Makefile`**：定义 CPU 与 GPU 二进制的编译命令。

以 `tests/regression/demo` 为例，目录内确实就是这四件（含一个共享头）：

```
tests/regression/demo/  →  Makefile  common.h  kernel.cpp  main.cpp
```

创建一个新测试的流程是：找一个相似的基线目录 → 复制改名 → `../configure` 同步构建树 → `make -C tests/regression/<name>` 编译 → `./ci/blackbox.sh --driver=simx --app=<name> --debug` 运行。

#### 4.1.3 源码精读：回归套件的总 Makefile

整个 `tests/regression` 套件由一个总 [tests/regression/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/Makefile) 编排。它的核心是一张主表和四份按后端的排除表：

```makefile
# tests/regression/Makefile:L5-L27  ——  主表与排除表
TESTS := basic demo dogfood ... vecadd sgemm ... softmax ...
EXCLUDE :=                          # 全后端通用排除
EXCLUDE_simx   :=                   # 仅 simx 排除
EXCLUDE_rtlsim :=
EXCLUDE_opae   :=
EXCLUDE_xrt    :=
BACKENDS := simx rtlsim opae xrt
```

接着是两个**过滤函数**（Makefile 的 `$(call ...)`），它们决定了「某个后端实际跑哪些测试」：

```makefile
# tests/regression/Makefile:L35-L36
ACTIVE_TESTS  = $(filter-out $(EXCLUDE),$(TESTS))                          # all/clean 用
backend_tests = $(filter-out $(EXCLUDE) $(EXCLUDE_$(1)),$(TESTS))          # run-<backend> 用
```

- `ACTIVE_TESTS` 只扣通用 `EXCLUDE`，供 `all`（编译）和 `clean`（清理）使用。
- `backend_tests` 同时扣通用 `EXCLUDE` 和后端专属 `EXCLUDE_<backend>`，供 `run-<backend>` 使用。
- 注释明确：「`clean` 走完整列表，保证被禁用的树也被清理」（[L32-L34](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/Makefile#L32-L34)）。

最关键的是 `run-<backend>` 聚合目标与 `run-%` 模式规则。`backend_rule` 宏为每个后端生成一条 `run-<backend>: run-<backend>-<test1> run-<backend>-<test2> ...` 的依赖链（[L41-L45](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/tests/regression/Makefile#L41-L45)），而每条 `run-<backend>-<test>` 最终落到这条模式规则：

```makefile
# tests/regression/Makefile:L56-L59
# run-<backend>-<test>: split "<backend>-<test>" on the first dash.
# Subdir names must not contain '-' (none currently do).
run-%:
	$(MAKE) -C $(word 2,$(subst -, ,$*)) run-$(word 1,$(subst -, ,$*))
```

这条规则干的事是：把目标名（如 `run-simx-vecadd`）按第一个 `-` 切成 `simx` 和 `vecadd` 两段，然后等价于执行 `make -C vecadd run-simx`——也就是**下钻到子目录、调用该子目录自己 Makefile 的 `run-simx` 目标**。这正是「`make -C tests/regression run-simx` 能跑完整个套件」的实现原理：一条顶层规则扇出到 58 个子目录。

> 小提示：注释里特别强调「子目录名不得含 `-`」，否则切分会被破坏。目前所有子目录都遵守。

#### 4.1.4 代码实践：盘点 tests 套件

**实践目标**：建立 `tests/` 目录的心智地图，确认「三件套」结构。

**操作步骤**：

1. 在仓库根目录执行 `ls -d tests/*/`，核对是否能看到上面列出的十一类套件。
2. 执行 `ls tests/regression/ | head -30`，浏览回归套件的程序名。
3. 打开 `tests/regression/vecadd/Makefile` 与 `tests/regression/vecadd/main.cpp`，找到它编译主机程序与 `.vxbin` 的规则，以及主机侧「打开设备→校验」的控制流。

**需要观察的现象**：每个回归子目录结构高度一致（`Makefile` + `main.cpp` + `kernel.cpp`），主机程序末尾通常打印 `PASSED!` 或 `FAILED` 并以退出码反映结果。

**预期结果**：你能用一句话说出 vecadd 测的是什么（向量加），以及它的主机程序如何判定通过。**待本地验证**（本讲不替你执行命令）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tests/regression/Makefile` 要把 `EXCLUDE` 拆成「通用」和「按后端」两份，而不是只留一份？

**参考答案**：因为不同后端能力不同。某些测试在便宜的 `simx` 上能跑，但在 FPGA 后端 `opae`/`xrt` 上可能暂不支持或太慢，需要单独排除；而通用 `EXCLUDE` 则用于所有后端都不该跑的测试。`backend_tests` 函数同时扣两份，`ACTIVE_TESTS` 只扣通用一份，从而让 `clean` 仍能清理被禁用的树。

**练习 2**：`make -C tests/regression run-simx` 最终会下钻到子目录执行什么目标？

**参考答案**：执行每个子目录自己的 `run-simx` 目标（由模式规则 `run-%` 把 `run-simx-vecadd` 切成 `make -C vecadd run-simx`）。子目录 Makefile 的 `run-simx` 负责用 simx 驱动实际运行该程序。

---

### 4.2 make run-simx / run-rtlsim：默认回归的两条主干命令

#### 4.2.1 概念说明

`docs/testing.md` 把跑回归压缩成两条最常用的命令（[docs/testing.md:L24-L28](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/testing.md#L24-L28)）：

```bash
$ make -C tests/regression run-simx      # 仿真器后端，快
$ make -C tests/regression run-rtlsim    # RTL 后端，慢但保真
```

这俩命令背后就是上一节讲的 `run-<backend>` 聚合目标：`run-simx` 跑完 `tests/regression` 下（扣除排除表后）所有程序在 simx 上的执行；`run-rtlsim` 同理但走 Verilator 仿真 RTL。`tests/opencl` 套件也有完全对称的 `run-simx`/`run-rtlsim`（[docs/testing.md:L40-L43](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/testing.md#L40-L43)）。

#### 4.2.2 核心流程：CONFIGS 双侧匹配纪律

跑回归看似简单，但有一条来自 `AGENTS.md` §4 的**核心纪律**容易踩坑：**`CONFIGS` 必须在驱动侧和应用侧同时生效**（[AGENTS.md:L77-L82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L77-L82)）。

原因在于：`blackbox.sh`（或 `make run-<backend>`）**只负责重建驱动**。如果你之前用 `-DVX_CFG_NUM_THREADS=4` 编译过应用，现在想让驱动按 8 线程跑，单靠 blackbox 没用——应用二进制里已经写死了 4。正确做法是先**重建应用**再跑：

```bash
# 来自 AGENTS.md §4 的范例
make -C tests/regression/<app> clean
CONFIGS="-DVX_CFG_NUM_THREADS=8 -DVX_CFG_EXT_TCU_ENABLE" make -C tests/regression/<app>
CONFIGS="-DVX_CFG_EXT_TCU_ENABLE" ./ci/blackbox.sh --driver=simx --app=<app> --threads=8
```

另外两条相关纪律（[AGENTS.md:L83-L87](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L83-L87)）：

- `make tests` / `make -C tests/regression` 用**默认宏**编译；非默认配置必须用 `CONFIGS` + 显式按应用重建。
- `ci/regression.sh` 是「已测试配置组合的权威来源」——在自创配置前，先去那里查哪些组合是被验证过的。

#### 4.2.3 源码精读：并行回归脚本的去留

`docs/testing.md` 还提到可以用 `tests/regression/run_parallel.sh` 并行跑回归，把每个测试的日志写到 `tests/regression/logs/`（[docs/testing.md:L30-L38](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/testing.md#L30-L38)）。

> ⚠️ **注意（待确认）**：该脚本在**当前 HEAD 的仓库中并未找到**（全局搜索 `run_parallel.sh` 无结果）。文档与磁盘在此处不一致——以仓库实际文件为准。CI 的真正并行机制是「跨 GitHub matrix 单元并行」（每个单元一棵独立 build 树），而非这个脚本，详见 4.4 节与 `u13-l4`。

#### 4.2.4 代码实践：跑一次默认 simx 回归

**实践目标**：亲手跑通默认回归套件，观察它如何扇出到每个子目录。

**操作步骤**（全部在 `build/` 目录执行）：

1. 先确保已 `../configure` 并 `make` 过基础驱动。
2. 跑一个小切口，验证机制：`make -C tests/regression run-simx-vecadd`（注意这是带测试名的细粒度目标，等价于下钻 `vecadd`）。
3. 再跑完整套件：`make -C tests/regression run-simx`。
4. （可选，较慢）`make -C tests/regression run-rtlsim` 对比 RTL 后端。

**需要观察的现象**：每跑完一个程序，会看到它自己的输出（成功则含 `PASSED!`）；某个程序失败时 `make` 会因非零退出码停止（除非加 `-k`）。

**预期结果**：simx 上默认回归应全部通过；若某个程序失败，记录其名字与报错。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：你改了 `VX_config.toml` 把 `NUM_THREADS` 从 4 调到 8 并重新 `configure`，然后直接 `make -C tests/regression run-simx`，却发现结果与预期不符。最可能的原因是什么？

**参考答案**：应用二进制（`.vxbin` 和主机程序）可能仍是按旧 `NUM_THREADS=4` 编译的。`make run-simx` 只重建/运行驱动，不会自动用新宏重建应用。需要先 `make -C tests/regression clean` 再重建应用，保证 `CONFIGS` 在驱动侧和应用侧一致（AGENTS.md §4 纪律）。

**练习 2**：`run-simx` 和 `run-rtlsim` 在成本与保真度上有何取舍？

**参考答案**：`run-simx` 走 C++ 仿真器，速度快，适合高频迭代和功能验证；`run-rtlsim` 走 Verilator 仿真真实 RTL，慢得多，但能证明功能与**时序**都与实现一致，是 model_parity 的 RTL 一侧依据（见 `u7-l4`）。日常开发先 simx，提交前/ nightly 上 rtlsim。

---

### 4.3 ci/regression.sh：CI v2 的本地测试入口

#### 4.3.1 概念说明

`make run-simx` 只覆盖 `tests/regression` 一个套件的一种切法。Vortex 真正的「全量测试」入口是 [ci/regression.sh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in)（源模板是 `ci/regression.sh.in`，configure 后生成）。它在文件头自述为 **「CI v2 local test runner」**——整个测试套件生活在 pytest 目录（`ci/testcases/*.yaml`）里，而本脚本是进入该目录的**唯一本地入口**（[ci/regression.sh.in:L16-L33](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L16-L33)）。

核心设计：本地跑测试与 GitHub CI 跑测试**用同一份目录、同一个引擎**，从而「无漂移（no drift）」——你在本地复现的就是 CI 在 matrix 单元里扇出执行的东西。

#### 4.3.2 核心流程：三条用户路径

`regression.sh` 提供三条用户路径（见 `show_usage`，[L275-L288](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L275-L288)）：

| 路径 | 作用 |
|------|------|
| `--all` | 跑**整个**目录（所有 category），针对当前 build 树的 XLEN |
| `--test <selector>` | 跑一个切片，`<selector>` 是 pytest marker 表达式（见 4.4） |
| `--clean` | 跑之前先 `make clean && make` |

`--all` 和 `--test` 都是 `pytest ci` 的薄封装，最终分发在这段代码（[L350-L365](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L350-L365)）：

```bash
# ci/regression.sh.in:L354-L361
set +e    # 临时关掉 set -e，避免一条红测试就中断，捕获状态码在下面汇报
if [ -n "$pytest_expr" ]; then
    VX_XLEN=$XLEN python3 -m pytest ci -m "$pytest_expr" --strict-markers
else
    VX_XLEN=$XLEN python3 -m pytest ci --strict-markers     # --all：无 -m = 全部
fi
rc=$?
set -e
```

注意两点：

1. 它通过环境变量 `VX_XLEN=$XLEN` 把当前 build 树的位宽（32 或 64）传给 pytest——用例会据此过滤（见 4.4）。
2. `--test` 的 selector 可以是单个 category（`tensor`）、单个 driver（`rtlsim`）、或它们的布尔组合（`"tensor and simx"`、`"graphics and not rtlsim"`）。`--test` 的参数解析见 [L304-L312](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L304-L312)。

#### 4.3.3 源码精读：开头的两道边界检查

`regression.sh` 一上来（在跑任何测试之前）先做两道**配置边界检查**（[L49-L56](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L49-L56)）：

```bash
# ci/regression.sh.in:L49-L56  ——  跑测试前先守 HW/SW 边界
# Enforce the HW/SW config layering boundary: VX_config.h is HW/sim-private
"@VORTEX_HOME@/ci/check_config_boundary.sh"
# Enforce the sw/ ↔ sim/+hw/ bidirectional isolation boundary.
"@VORTEX_HOME@/ci/check_sw_sim_boundary.sh"
```

这两个脚本正是 `u2-l3` 讲过的两道 CI 守卫：禁止 `sw/`、`tests/` include 私有的 `VX_config.h`，并强制 `sw/{kernel,runtime}` 与 `sim/+hw/` 双向隔离。把它们放在回归脚本最前面，意味着**任何破坏软硬边界隔离的改动，连测试都跑不起来就会先被挡下**。注意 `@VORTEX_HOME@` 是 configure 时刻替换的占位符（承接 `u1-l3`）。

#### 4.3.4 源码精读：多步 host flow 的逃生舱

并非所有测试都能塞进「一条 blackbox 命令」的模具——有些是多步主机流程（`dtm`、`sst`、`gem5`、`cupbop`）。这些保留为脚本内的 shell 函数，目录里的 `via: script` 用例通过**内部** `--run <flow>` 后端调用它们（用户不直接用 `--run`，而是用 `--test <flow>` 经 pytest 路由进来，见 [L286-L287](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L286-L287) 与 [L337-L349](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L337-L349)）。

例如 `dtm()` 函数会先 `make -C sim/simx`、`make -C tests/kernel` 构建 `fibonacci.vxbin`，再用 `ci/dtm_test.py` 直接 spawn 仿真器（[L59-L73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L59-L73)）。这种设计让目录保持声明式（绝大多数用例是干净的 YAML），同时不丢失对复杂流程的表达力。

#### 4.3.5 代码实践：用 regression.sh 跑一个 category

**实践目标**：用 CI v2 入口跑一个测试切片，对比它与 `make run-simx` 的差异。

**操作步骤**（在 `build/` 目录）：

1. 看帮助：`./ci/regression.sh --help`，确认 `--all`/`--test`/`--clean` 三个标志。
2. 跑一个小切片：`./ci/regression.sh --test "regression and simx"`（`regression` 是一个 category marker，`simx` 是 driver marker，见 4.4）。
3. 观察开头两道边界检查是否通过、pytest 收集了多少用例、每个用例的 PASS/FAIL。
4. 对比：同样测回归，`make -C tests/regression run-simx`（纯 Make 扇出）与 `./ci/regression.sh --test regression`（pytest 目录）在覆盖面上的区别。

**需要观察的现象**：pytest 会先 `collect` 用例并打印数量，再逐条执行；结尾打印 `Regression completed!` 与耗时。若边界检查失败，会在最前面就报错退出。

**预期结果**：`--test "regression and simx"` 应收集到 `ci/testcases/regression.yaml` 里 `drivers: [simx]` 的那些用例并全部通过。**待本地验证**。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `regression.sh` 在调用 pytest 前要 `set +e`，跑完又 `set -e`？

**参考答案**：因为脚本顶部 `set -e`（[L36](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L36)）会让任何命令的非零退出码立即终止脚本。但 pytest 在有测试失败时返回非零，我们希望**跑完全部测试再汇报**总状态码（`exit $rc`，[L372](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L372)），而不是第一条失败就中断。所以临时关掉 `set -e` 捕获 `rc`，再恢复。

**练习 2**：用户想跑 `dtm` 流程，应该敲 `./ci/regression.sh --run dtm` 吗？

**参考答案**：不应该。`--run <flow>` 是**内部**后端，供目录里 `via: script` 用例调用，不是用户接口。用户应敲 `./ci/regression.sh --test dtm`，它会经 pytest 路由到对应的 `via: script` 用例，再由该用例触发 `--run dtm`（见 [L286-L287](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/regression.sh.in#L286-L287) 注释）。

---

### 4.4 ci/testcases：声明式测试目录（catalog）

#### 4.4.1 概念说明

`regression.sh` 只是入口，真正的「测试长什么样」全部声明在 [ci/testcases/](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/) 目录下——约 35 个 YAML 文件，每个文件一个 category（`amo.yaml`、`cache.yaml`、`tensor.yaml`、`graphics.yaml`、`model_parity.yaml`、`regression.yaml`…）。

设计文档把这套体系的核心思想概括成一句话：**「一个测试用例是 N 维空间中的一个点」**（[continuous_integration.md:L40-L54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L40-L54)）。这些维度是：

```
category   amo, cache, tensor, graphics, …          (被测功能)
driver     simx | rtlsim | xrtsim | opaesim         (成本轴)
xlen       32 | 64                                   (build 树轴)
config     CONFIGS="-DVX_CFG_…"                      (重建轴)
shape      cores/warps/threads/l2/l3, args           (规模轴)
tier       smoke | full | nightly                    (何时跑轴)
needs      (none) | mpi | sst | gem5                 (环境轴)
touches    该用例覆盖的源码路径                       (选择轴)
```

把这些维度显式化（而不是揉进一行 bash）的好处是：任何一个维度都可以独立地变成**过滤器**（`-m "cache and simx and smoke"`）或 **matrix 维度**（CI 按它扇出单元）。

#### 4.4.2 核心流程：YAML 用例的三种执行方式与两种检查

一个用例靠 `via` 字段选择执行方式，靠 `check` 字段选择额外的断言。它们在数据模型里有白名单约束（[ci/testcase.py:L39-L40](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L39-L40)）：

```python
VALID_VIA   = {"blackbox", "make-run", "script"}
VALID_CHECK = {"model_parity", "perf_gate"}
```

**三种 `via`**（`Spec.run_command` 把它们翻译成实际命令，[ci/testcase.py:L136-L158](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L136-L158)）：

| `via` | 翻译成 | 典型场景 |
|------|--------|---------|
| `blackbox` | `./ci/blackbox.sh --driver=… --app=… <shape flags>` | 绝大多数回归/功能用例 |
| `make-run` | `make -C <dir> <target>` | RISC-V ISA 合规等有专属 make 目标的 |
| `script` | `bash -c "<run>"` | dtm/sst/gem5/cupbop 等多步主机流程 |

以 `ci/testcases/regression.yaml` 为例，能看到 `via: make-run`（跑整棵 `tests/regression` 的 `run-{driver}`）和 `via: blackbox`（跑单个 app 带规模参数）两种混用（[ci/testcases/regression.yaml:L8-L84](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/regression.yaml#L8-L84)）：

```yaml
# ci/testcases/regression.yaml:L9-L20  ——  make-run 与 blackbox 混用
- id: isa-1
  via: make-run
  drivers: [simx]
  dir: tests/regression
  target: run-{driver}            # 展开成 run-simx，正是 4.2 节那条命令
- id: occupancy-1
  via: blackbox
  drivers: [simx]
  app: occupancy
  shape: {threads: 32}            # blackbox 旋钮
```

**两种 `check`**（在普通「退出码为 0」之外追加的断言）：

- `check: model_parity`：**同一 app/args/configs 在 simx 和 rtlsim 上各跑一遍**，断言退休指令**精确相等**、周期数在容差内（默认 5%）。它不被 driver 展开，而是钉在 rtlsim 上由 runner 自己驱动 simx 那条腿（[ci/testcases/model_parity.yaml:L1-L22](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L1-L22)）。这是 `u7-l4` 主线的物理落点。
- `check: perf_gate`：在 rtlsim 上跑，把周期数与检入的黄金基线（`ci/perf/baselines/*.json`）对比，±2% 内才算通过。基线**绝不手改**，只能由人用 `--update-baselines` 重新生成（[ci/testcases/perf_gate.yaml:L1-L17](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/perf_gate.yaml#L1-L17)）。

#### 4.4.3 源码精读：数据模型 Spec 与 marker 生成

每个 YAML 条目被 [ci/testcase.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py) 的 `Spec` 类加载成一个具体用例。`load_all()` 把所有 YAML 汇成一张扁平表（[L207-L212](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L207-L212)）。每个用例的 `markers()` 方法生成它的 pytest marker 集合（[L118-L126](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L118-L126)）：

```python
# ci/testcase.py:L118-L126
def markers(self):
    m = [self.category, self.tier]                 # 如 "regression", "smoke"
    if self.marker_driver:
        m.append(self.marker_driver)               # 如 "simx"
    if self.check:
        m.append(self.check)                       # 如 "model_parity"
    m += ["needs_{}".format(n) for n in self.needs]
    return m
```

这意味着**每个维度的取值都成为一个 marker**，于是 `pytest -m "cache and simx and smoke"` 就能从全表里精确选出「cache 类、simx 后端、smoke 层级」的用例。这就是 4.3 节 `--test <selector>` 的底层原理。

#### 4.4.4 源码精读：pytest 引擎三件套

目录之所以能被 pytest 跑起来，靠的是三个 Python 文件（设计文档称之为「我们拥有的三样东西：测试数据 + 薄 pytest 胶水 + 不变的执行器」，[continuous_integration.md:L58-L69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L58-L69)）：

1. **`ci/testcase.py`**：数据模型 + 规划器 CLI（`lint`/`matrix`/`select`）。纯逻辑、不依赖 pytest，所以 CI 的 plan 任务（无需 build 环境）和 pytest 引擎都能建立在它之上（[L1-L20](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L1-L20)）。`lint` 子命令会校验 `via`/`driver`/`check`/`tolerance` 合法性（[L356-L391](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L356-L391)）。

2. **`ci/conftest.py`**：pytest 钩子。`pytest_configure` 从数据里**动态注册所有 marker**——新增 category/driver 无需改本文件，且 `--strict-markers` 能把拼错的 `-m` 表达式变成报错而非静默空选（[ci/conftest.py:L42-L47](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L42-L47)）。`pytest_generate_tests` 把每个用例变成一个带 marker 的参数化测试项，并按 `applies_to_xlen` 过滤（[L50-L67](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L50-L67)）。带 `known_issue:` 的用例会被套上 `xfail`（[L59-L65](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/conftest.py#L59-L65)）。

3. **`ci/test_runner.py`**：真正的测试体 `test_case()`。它 shell 调用不变的执行器（blackbox/make）并断言退出码为 0；对 `check:` 用例则走 `_model_parity` / `_perf_gate` 的专用断言（[ci/test_runner.py:L94-L104](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L94-L104)）。

不需要 `pyproject.toml` 或 `pytest.ini`——marker 全部在 `conftest.py` 动态注册（[continuous_integration.md:L162](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md)）。一条典型的本地切片命令是：

```bash
VX_XLEN=32 pytest ci -m "cache and simx and smoke" --strict-markers   # 见 conftest.py 文档注释 L8
```

#### 4.4.5 源码精读：tier 与 event×driver 矩阵

`tier` 是「何时跑」轴，三个取值对应 CI 的不同触发频率。设计文档给出 event×driver×tier 矩阵（[continuous_integration.md:L258-L268](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md#L258-L268)）：

| 触发 | 跑哪些 driver | 跑哪些 tier |
|------|--------------|------------|
| push（每次推送） | simx | smoke |
| pull_request | simx, rtlsim | smoke, full |
| schedule（每晚/每周） | 全部 | 全部 |
| workflow_dispatch（手动） | 输入指定 | 输入指定 |

这解释了为什么 `tier` 是用例的一等字段：CI 的 plan 任务读目录数据（`testcase.py matrix`，无需 build 环境），按当前事件类型筛选出该跑的 `(category × driver × xlen)` 单元，每个单元在独立 build 树里执行 `pytest ci -m "<category> and <driver>"`。一次 push 只跑最便宜的 simx/smoke，把昂贵的 ~168 个 rtlsim 用例推迟到 PR 门/每晚——这就是 Vortex 测试体系「用 tier 控制成本」的核心机制。

`known_issue:` 字段用于**已分类的预期失败**：用例仍会 build 并运行（日志可见、意外通过会显示为 XPASS），但它的失败不会让 CI 变红（[ci/test_runner.py:L8-L12](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L8-L12)）。典型例子见 `model_parity.yaml` 里 sgemv 的 cycle 差距（[ci/testcases/model_parity.yaml:L55-L64](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/model_parity.yaml#L55-L64)）。

#### 4.4.6 代码实践：读一个 YAML 并预测 marker 选择

**实践目标**：把「声明式 YAML → pytest marker 选择」这条链走通。

**操作步骤**：

1. 打开 [ci/testcases/regression.yaml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/regression.yaml)，数一数它声明了多少个用例、各用 `via` 什么、`drivers` 有哪些。
2. 预测：`pytest ci -m "regression and simx"` 会选出哪些 `id`？再预测 `pytest ci -m "regression and rtlsim"` 选哪些。
3. 用规划器 CLI 验证（无需 build 环境）：`python3 ci/testcase.py lint` 检查目录合法性；可选 `python3 ci/testcase.py matrix --drivers=simx --tier=smoke` 看矩阵单元。
4. 实跑一个最窄切片：`./ci/regression.sh --test "regression and simx and smoke"`，核对 pytest 收集到的用例数是否与你的预测一致。

**需要观察的现象**：`lint` 应打印 `OK: N test cases across M categories`；`--test` 切片收集的用例数应与你手工数 YAML 的结果一致。

**预期结果**：marker 表达式精确控制了切片范围，验证「N 维空间中的一个点」模型。**待本地验证**。

#### 4.4.7 小练习与答案

**练习 1**：一个 `via: blackbox`、`drivers: [simx, rtlsim]`、`tier: smoke` 的用例，会被 `load_category` 展开成几个 `Spec`？它们各自的 marker 是什么？

**参考答案**：展开成 **2 个** `Spec`（`drivers` 列表每个 driver 一个）。各自的 marker 包含 `{category, smoke, simx}` 与 `{category, smoke, rtlsim}`（见 `markers()`，[testcase.py:L118-L126](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L118-L126)）。注意 `xlen` 不在这里展开——32/64 是外层维度，靠 `applies_to_xlen` 按当前 build 树过滤（build32/build64 是独立的树）。

**练习 2**：`check: model_parity` 的用例为什么**不**在 `drivers:` 里列两个后端？

**参考答案**：因为 model_parity 的语义是「一个用例自己跑 simx 和 rtlsim 两条腿并对比」，而非「同一测试在两个后端各独立跑一次」。`load_category` 检测到 `check:` 就把 driver 钉成 `rtlsim`（它要 elaborate RTL，build/matrix 放置才正确），由 `test_runner._model_parity` 自己再驱动 simx 那条腿（[testcase.py:L198-L201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L198-L201)、[test_runner.py:L47-L60](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L47-L60)）。若在 YAML 里给它写 `drivers:`，`lint` 会报错。

**练习 3**：perf_gate 基线变红了，能不能直接手改 `ci/perf/baselines/*.json` 里的数字让它通过？

**参考答案**：**绝不能**。基线是黄金数据，手改或调大数字是 `AGENTS.md` §4 明令禁止的（[L88](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L88)）。正确做法是定位真实性能回退的根因；若改动确实改变了周期数（如故意的设计变更），则用 `pytest ci -m perf_gate --update-baselines` 重新生成基线，经人工 review JSON diff 后提交。CI 永远不会传 `--update-baselines`。

---

## 5. 综合实践：给回归套件加一个声明式 catalog 用例

把本讲四个模块串起来，完成一个贯穿任务：**为一个已有的回归测试，在 CI 目录里新增一个声明式用例，并分别用 `make`、`regression.sh`、`pytest -m` 三种方式触发它。**

**任务背景**：`tests/regression/vecadd` 已经存在（向量加）。假设你想让 CI 在 simx 上、以 `cores=2` 的规模额外覆盖它一次（与默认规模区分开）。

**步骤**：

1. **理解三件套**（承接 4.1）：读 `tests/regression/vecadd/main.cpp`，确认它如何用 `vx_device_query` 查询规模并打印 `PASSED!`。

2. **手工验证**（承接 4.2）：在 `build/` 跑 `./ci/blackbox.sh --driver=simx --app=vecadd --cores=2 --nohup`，确认它能通过。

3. **新增 catalog 用例**（承接 4.4）：在 [ci/testcases/regression.yaml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcases/regression.yaml) 的 `tests:` 列表末尾仿照 `vecadd-1` 加一条：

   ```yaml
   - id: vecadd-2core
     via: blackbox
     drivers: [simx]
     app: vecadd
     shape: {cores: 2}
     flags: --nohup
   ```

4. **lint 校验**：`python3 ci/testcase.py lint`，应打印 `OK`。

5. **三种触发方式对比**（综合 4.2/4.3/4.4）：
   - `make -C tests/regression run-simx`（纯 Make，看不到你的新 catalog 用例——它只认 `TESTS` 主表）。
   - `./ci/regression.sh --test "regression and simx"`（经 pytest 目录，**应能看到并执行** `vecadd-2core`）。
   - `VX_XLEN=32 pytest ci -m "regression and simx" --strict-markers -k vecadd-2core`（最窄切片，只跑你这条）。

**需要观察的现象与预期结果**：

- `make run-simx` 与 `pytest` 目录是**两套不同的测试清单**：前者是 `tests/regression/Makefile` 的 `TESTS` 表，后者是 `ci/testcases/*.yaml` 目录。新增 catalog 用例只影响后者。
- 三种方式中，只有后两种会跑到你新增的 `vecadd-2core`，且它的 marker 应包含 `regression`、`simx`、`smoke`（默认 tier）。
- 全部用例通过（退出码 0）。

> ⚠️ 本任务要求修改 `ci/testcases/regression.yaml`（属于 CI 配置，非源码）。若你无权修改，可只做步骤 1–2 与 4–5 的「只读预测」部分：手工数 YAML、预测 marker、用 `lint`/`matrix` 验证，不实际新增条目。**命令执行结果待本地验证**。

## 6. 本讲小结

- Vortex 的可执行测试分十一类放在 `tests/` 下；`tests/regression`（58 个程序）是日常迭代主力，每个测试是 `kernel.cpp` + `main.cpp` + `Makefile` 的三件套。
- `make -C tests/regression run-simx` / `run-rtlsim` 通过总 Makefile 的 `run-%` 模式规则扇出到每个子目录；核心纪律是 `CONFIGS` 必须在驱动侧与应用侧同时生效。
- `ci/regression.sh` 是 CI v2 的唯一本地入口，`--all`/`--test <selector>` 都是 `pytest ci` 的薄封装；它跑测试前先强制两道 HW/SW 边界检查，复杂的多步 host flow 保留为 shell 函数经 `via: script` 调用。
- `ci/testcases/*.yaml` 是声明式测试目录，核心理念是「一个用例是 N 维空间（category/driver/xlen/config/shape/tier/needs/touches）中的一个点」；`via` 有 blackbox/make-run/script 三种，`check` 有 model_parity/perf_gate 两种。
- 每个 YAML 维度取值都成为 pytest marker，于是 `-m "cache and simx and smoke"` 能精确切片；引擎由 `testcase.py`（数据模型 + lint/matrix/select）+ `conftest.py`（动态注册 marker、参数化）+ `test_runner.py`（执行与断言）三件套构成，不依赖 `make run-simx` 的 `TESTS` 表。
- `tier`（smoke/full/nightly）控制「何时跑」，配合 event×driver 矩阵让 push 只跑最便宜的 simx/smoke，把昂贵的 rtlsim 推迟到 PR/nightly。

## 7. 下一步学习建议

本讲建立了「测试如何组织与运行」的基础。建议接下来：

- **`u13-l2`（调试追踪与 SimX-as-oracle）**：学 `--debug` 生成 trace、`trace_csv.py` 解析、Perfetto 分析，以及 RTL 卡住时把 SimX 当 oracle 的 trace-diff 调试法。
- **`u13-l3`（性能计数器与 roofline 分析）**：学 `--perf=1` 暴露的调度器利用率/流水线停顿/内存延迟计数器，以及 `perf/roofline.py`。
- **`u13-l4`（持续集成与 model_parity 门控）**：深入本讲提到的 `check: model_parity` / `perf_gate` 判定标准、CI pytest 框架（`conftest.py`/`test_runner.py`）与门控纪律——它是本讲 catalog 体系的自然延续。
- 想动手扩展测试的话，重读本讲的「综合实践」，并阅读 `AGENTS.md` §4 与 [docs/designs/continuous_integration.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md) 全文。
