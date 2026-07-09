# 构建系统与运行测试

## 1. 本讲目标

本讲承接 [u1-l1 项目总览](u1-l1-project-overview.md)。上一讲我们建立了"Koala-GPGPU 是什么、顶层如何由 `gpgpu_top → sm_core` 组成"的直觉，但还没有真正动手把它跑起来。本讲只解决一件事：**当你在仓库根目录敲下 `make test_integer` 之后，到底发生了什么**。

读完本讲你应该能够：

- 一字一句说清楚 `make test_integer` 触发的"复制源码 → 编译 → 仿真"全过程。
- 看懂 Koala-GPGPU 三层 Makefile 的分工：顶层 `Makefile`（拼装目录）、`Main.Makefile`（声明编译配置与源文件）、`Common.Makefile`（真正调用工具链）。
- 理解 `iverilog -g2012` 编译、`vvp` 运行、cocotb 通过 VPI 驱动 Python 测试台这条仿真工具链是如何串起来的。
- 知道 `build/` 目录如何组织，以及仿真日志和最终的寄存器验证结果（R1/R2/R3）去哪里找。

## 2. 前置知识

在进入源码前，先建立几个本讲会用到的概念：

- **Make 与 Makefile**：`make` 是一个根据"依赖关系"决定执行哪些命令的构建工具。规则写成 `目标: 依赖` + 一行命令。本讲会用到两种特殊语法：
  - **模式规则（pattern rule）**：写成 `test_%:`，其中 `%` 是通配符（称为"干/stem"）。`test_integer`、`test_float` 都能匹配它，stem 分别是 `integer`、`float`。
  - **自动变量**：`$@` 表示当前目标名（如 `test_integer`），`$*` 表示 stem（如 `integer`）。
- **递归 make**：`make -C build $@` 的意思是"切换到 `build/` 目录，在里面重新执行 `make 目标`"。这是本项目的关键技巧——根目录负责搭目录，`build/` 里负责真正编译。
- **iverilog（Icarus Verilog）**：一个开源的 Verilog/SystemVerilog 仿真器。`-g2012` 表示按 SystemVerilog-2012 标准编译（项目大量使用 `.sv` 文件，必须开这个开关）。
- **vvp**：iverilog 编译产物（`sim.vvp`）的解释运行器，由它驱动整个仿真时间推进。
- **cocotb**：一个让你用 Python 写数字电路测试台的框架。它通过 **VPI（Verilog Procedural Interface）** 这种标准接口，把 Python 测试代码"插"进 Verilog 仿真器里，互相读写信号。
- **cocotb-config**：cocotb 自带的查询命令，`cocotb-config --prefix` 返回安装目录，`cocotb-config --libpython` 返回 cocotb 需要链接的 Python 动态库路径。
- **$(CURDIR)**：Make 内置变量，表示 make 正在执行时的工作目录。本项目用它把源文件路径拼成绝对路径。
- **TOPLEVEL**：cocotb 术语，指仿真里的"顶层模块"。本项目固定是 `gpgpu_top`（上一讲已介绍）。

> 阅读建议：本讲几乎不涉及 RTL 内部逻辑，全部是构建脚本和工具链。建议边读边在仓库里对照这几个 `.Makefile` 文件，效果最好。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `Makefile` | 仓库顶层 Makefile。把 RTL 与 test 拷贝进 `build/`，再递归到 `build/` 执行。 |
| `Main.Makefile` | 真正"住"在 `build/` 里负责编译的入口（会被复制成 `build/Makefile`）。声明顶层模块，并 include 其他 Makefile。 |
| `Common.Makefile` | 工具链核心。定义 `test_%` 规则，调用 `iverilog` 和 `vvp + cocotb`。 |
| `rtl/gpgpu.Makefile` | 声明顶层源文件 `gpgpu_top.sv`、include 目录，并向下 include 子模块 Makefile。 |
| `rtl/sm_core/sm_core.Makefile` | 把 9 个 `sm_core` 流水线源文件加入 `VERILOG_SOURCES`。 |
| `rtl/common/common.Makefile` | 把公共基础设施源文件加入 `VERILOG_SOURCES`。 |
| `test/test_integer.py` | cocotb 测试台，加载 kernel、运行仿真、验证 R1/R2/R3。 |
| `test/logger.py` | 日志器，把每个周期的状态与验证结果写到 `test/logs/log_*.txt`。 |

## 4. 核心概念与源码讲解

### 4.1 顶层 Makefile：把仓库"拼装"进 build/

#### 4.1.1 概念说明

仓库根目录的 `Makefile` 并不直接编译任何代码。它的唯一职责是：**准备一个干净、扁平、自包含的 `build/` 目录**，把所有需要的 RTL、测试脚本和编译用的 Makefile 都拷进去，然后"换班"——递归调用 `build/` 里的 Makefile 来干真正的活。

为什么要拷贝一份而不是就地编译？因为本项目的源文件路径都用 `$(CURDIR)` 拼接（见 4.2），需要一个确定的工作目录；同时把 RTL 和 test 摊平到一个目录里，能让 include 路径、源文件列表都变得简单可预测。这是一种常见的小型硬件项目构建惯例。

#### 4.1.2 核心流程

当你执行 `make test_integer` 时，顶层 `Makefile` 命中模式规则 `test_%:`（stem = `integer`），按顺序执行：

1. `mkdir build` —— 新建空目录。
2. `cp rtl/*.* build` —— 把 `rtl/` 下**带点号**的文件拷进去。注意 `*.*` 只匹配文件名里含 `.` 的条目，因此 `rtl/gpgpu_top.sv` 和 `rtl/gpgpu.Makefile` 会被拷贝，而子目录 `common/`、`sm_core/`（名字里没有点）不会被这个命令拷贝，需要下一步单独处理。
3. `cp -r rtl/common build/common` —— 拷贝公共模块目录。
4. `cp -r rtl/sm_core build/sm_core` —— 拷贝 SM 核心目录。
5. `cp -r test build/test` —— 拷贝整个 test 目录（含 `.py` 测试台）。
6. `cp Main.Makefile build/Makefile` —— **关键一步**：把 `Main.Makefile` 改名为 `build/` 里的 `Makefile`，让递归 make 能找到它。
7. `cp Common.Makefile build/Common.Makefile` —— 工具链 Makefile 也带过去。
8. `make -C build test_integer` —— 切到 `build/`，重新执行同一个目标名，这次由 `build/Makefile`（即 `Main.Makefile`）接管。

```
make test_integer            # 仓库根目录
   │
   │  命中顶层 Makefile 的 test_% 规则
   ▼
mkdir build + cp 一堆文件      # 拼装 build/
   │
   ▼
make -C build test_integer    # 递归进入 build/
   │
   ▼
由 build/Makefile (=Main.Makefile) 接管 → 进入 4.2 / 4.3
```

`clean` 目标则是反过来：`rm -rf build`，把整个构建目录连同产物一并删掉。

#### 4.1.3 源码精读

顶层 Makefile 全文很短，核心就是 `test_%` 和 `clean` 两条规则：

[Makefile:2-10](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Makefile#L2-L10) —— 这是"拼装 + 递归"的完整规则。`$@` 在此处等于目标名 `test_integer`，于是最后一步 `make -C build $@` 把同一个目标名传递给 `build/`。

[Makefile:12-13](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Makefile#L12-L13) —— `clean` 规则，`rm -rf build` 一步清理。

注意第 1 行 `.PHONY: test clean` 把 `test` 和 `clean` 声明为"伪目标"，告诉 make 它们不是文件名，避免和同名文件冲突。

> 一个容易踩的坑：第 4 行 `cp rtl/*.* build` 不会拷贝子目录。如果你以后新增了一个带点号的顶层文件（比如 `rtl/gpgpu_top_tb.sv`），它会自动被拷进 build；但新增一个**不带点号的子目录**则不会被拷贝，需要在这里补一行 `cp -r`。这是读这层 Makefile 时最该记住的细节。

#### 4.1.4 代码实践

**实践：只看拼装，不真正编译（dry-run）。**

1. **实践目标**：在不依赖 iverilog/cocotb 是否安装的前提下，验证"拼装 build/"这一步确实按预期工作。
2. **操作步骤**：
   - 在仓库根目录执行 `make clean`（如果存在旧的 `build/`）。
   - 手动执行顶层规则的前 7 步来观察产物，或者直接执行 `make test_integer`——即便后面编译失败，`build/` 目录通常也已经拼装完成。
3. **需要观察的现象**：`build/` 目录出现，且内部包含 `Makefile`、`Common.Makefile`、`gpgpu.Makefile`、`gpgpu_top.sv`、`common/`、`sm_core/`、`test/`。
4. **预期结果**：可以用 `ls build/` 看到上述条目；`build/Makefile` 的内容应与仓库根的 `Main.Makefile` 完全一致（可用 `diff build/Makefile Main.Makefile` 验证，无输出即一致）。
5. **本地验证状态**：目录拼装部分行为确定，可直接验证；若后续编译报错属正常（见 4.3，需要工具链）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `cp rtl/*.* build` 不会把 `rtl/common` 目录拷进去？
**答**：因为 shell 通配符 `*.*` 只匹配文件名中包含 `.` 的条目，而目录名 `common`、`sm_core` 不含点号，故不被匹配，需要用随后的 `cp -r` 单独拷贝。

**Q2**：`make -C build $@` 中，`$@` 此时是什么值？为什么不用 `$*`？
**答**：`$@` 是完整目标名 `test_integer`。顶层这里要把"完整目标"传给 `build/`，让它在 `build/Makefile` 里再次匹配 `test_%`；如果传 stem `integer`，递归 make 找不到名为 `integer` 的目标。stem `$*` 要等进入 `build/` 后才被用来拼 cocotb 的模块名（见 4.3）。

---

### 4.2 Main.Makefile 与子 Makefile：声明"要编译什么"

#### 4.2.1 概念说明

进入 `build/` 后，接管工作的是 `Main.Makefile`（被复制成 `build/Makefile`）。它本身也很短，只做三件事：声明仿真顶层模块 `gpgpu_top`、声明语言为 verilog、然后通过 `include` 把"源文件清单"和"工具链规则"两份 Makefile 拉进来。

源文件清单分散在几个子 Makefile 里，每个子 Makefile 都往同一个全局变量 `VERILOG_SOURCES` 里**追加（`+=`）**自己负责的文件。这种"分散声明、聚合使用"的写法，让"哪个模块负责哪些 .sv 文件"一目了然。

#### 4.2.2 核心流程

`Main.Makefile` 的 include 链如下：

```
Main.Makefile
  │  TOPLEVEL = gpgpu_top
  ├── include gpgpu.Makefile
  │       ├── VERILOG_SOURCES += gpgpu_top.sv
  │       ├── VERILOG_INCLUDE_DIRS += common
  │       ├── include common/common.Makefile   → 4 个公共 .sv
  │       └── include sm_core/sm_core.Makefile → 9 个 sm_core .sv
  └── include Common.Makefile                  → 工具链 test_% 规则（见 4.3）
```

最终 `VERILOG_SOURCES` 里会收集到：1 个顶层 + 4 个公共 + 9 个 sm_core，共 **14 个 `.sv` 源文件**（均带 `$(CURDIR)` 前缀，即 `build/` 绝对路径）。

#### 4.2.3 源码精读

[Main.Makefile:1-6](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Main.Makefile#L1-L6) —— `TOPLEVEL_LANG ?= verilog` 用 `?=` 表示"若未设置才赋值"，方便外部覆盖；`TOPLEVEL = gpgpu_top` 固定顶层；两行 `include` 聚合源文件清单与工具链。

[rtl/gpgpu.Makefile:1-6](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu.Makefile#L1-L6) —— 第 1 行加入顶层源文件 `gpgpu_top.sv`；第 3 行把 `common` 目录登记为 include 搜索路径（供 `` `include `` 使用）；第 5–6 行继续 include 子模块清单。

[rtl/sm_core/sm_core.Makefile:1-5](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.Makefile#L1-L5) —— 把 SM 核心流水线的 9 个 `.sv`（`sm_core`、`sm_decode`、`sm_fetch`、`sm_inst_buffer`、`sm_warp_scheduler`、`sm_score_board`、`sm_operand_collect`、`sm_issue`、`sm_int_alu`、`sm_writeback`）逐一追加进 `VERILOG_SOURCES`。这 9 个名字与上一讲的结构树一一对应。

[rtl/common/common.Makefile:1](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/common/common.Makefile#L1) —— 加入 4 个公共基础设施源文件：`fixed_pri_arb_base`、`oh2bin`、`rr_arb`、`sync_fifo`。它们是后续每个流水级都会用到的底层单元（第 2 单元会专门讲）。

> 注意：`common.Makefile` 里登记的只有 4 个 `.sv`，但 `rtl/common/` 目录里实际还有 `fixed_pri_arb.sv`、`sync_fifo_count.sv`、`define.sv`。`define.sv` 是通过 `` `include `` 被 `gpgpu_top.sv` 引入的（参数宏定义，不是独立模块），另外两个则当前未被顶层编译——这是阅读这层 Makefile 时值得留意的"清单不完全等于目录"现象。

#### 4.2.4 代码实践

**实践：核对最终源文件清单。**

1. **实践目标**：确认 `VERILOG_SOURCES` 最终到底收集了哪些文件、共多少个。
2. **操作步骤**：在 `build/` 目录执行一次"空跑"展开——
   `make -C build -n test_integer 2>/dev/null | grep iverilog`
   （`-n` 表示只打印命令不执行；`grep iverilog` 只看编译那一行）。
3. **需要观察的现象**：屏幕上会打印出完整的 `iverilog ... -g2012 ... <一长串 .sv 路径>` 命令。
4. **预期结果**：该命令的末尾应列出 14 个 `.sv` 文件路径（均以 `build/` 开头），顶层是 `build/gpgpu_top.sv`，并带 `-s gpgpu_top` 指定顶层。
5. **本地验证状态**：该展开命令不依赖仿真器，仅用 make 的 `-n` 即可验证。

#### 4.2.5 小练习与答案

**Q1**：`VERILOG_INCLUDE_DIRS += $(CURDIR)/common` 最终会变成什么？它被谁使用？
**答**：在 `build/` 里执行时，`$(CURDIR)` 等于 `build/`，所以变成 `build/common`。它被 [Common.Makefile:3-5](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Common.Makefile#L3-L5) 转成 `-I` 参数传给 iverilog，用于解析 `` `include ``。

**Q2**：如果想新增一个 SM 子模块 `sm_fpu.sv`，需要改哪几处？
**答**：至少要把它加入 `rtl/sm_core/sm_core.Makefile` 的 `VERILOG_SOURCES +=`。拷贝由顶层的 `cp -r rtl/sm_core` 自动覆盖，无需改顶层 Makefile。

---

### 4.3 Common.Makefile：iverilog + vvp + cocotb 工具链

#### 4.3.1 概念说明

`Common.Makefile` 是整套构建里"最硬核"的一层——它真正调用编译器和仿真器。它定义的 `test_%` 规则会在 `build/` 里被 `test_integer` 二次命中（stem 重新变成 `integer`），然后执行两步：

1. 用 **iverilog** 把所有 `.sv` 编译成一个仿真镜像 `sim.vvp`。
2. 用 **vvp** 运行这个镜像，并通过 **cocotb 的 VPI 插件**把 Python 测试台挂进去。

理解这一层的关键，是看懂 cocotb 是怎么"找到"测试代码的：环境变量 `MODULE=test.test_integer` 告诉 cocotb"去 `test/` 包里加载 `test_integer` 这个模块"。这里用到的 stem `$*` 此时等于 `integer`，于是 `test.test_$*` 拼成 `test.test_integer`。

#### 4.3.2 核心流程

```
test_integer (build/Makefile 内命中 Common.Makefile 的 test_%)
   │
   │  ① 编译
   ▼
iverilog -o sim.vvp -s gpgpu_top -g2012 [-I build/common]  <14 个 .sv>
   │  产物: build/sim.vvp
   │
   │  ② 运行 + 挂载 cocotb
   ▼
export LIBPYTHON_LOC=$(cocotb-config --libpython)
MODULE=test.test_integer \
vvp -M $(cocotb-config --prefix)/cocotb/libs -m cocotbvpi_icarus  sim.vvp
   │
   ▼
cocotb 加载 test/test_integer.py，驱动仿真，写日志到 test/logs/
```

几个关键参数的含义：

- `-o ./sim.vvp`：编译输出镜像文件名。
- `-s gpgpu_top`（即 `$(TOPMODULE_ARG)`）：显式指定仿真顶层（来自 [Common.Makefile:1](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Common.Makefile#L1)）。
- `-g2012`：开启 SystemVerilog-2012 支持，编译 `.sv` 必需。
- `LIBPYTHON_LOC`：cocotb 需要链接的 Python 动态库，运行前通过 `cocotb-config --libpython` 取得并 export。
- `-M <libs 目录> -m cocotbvpi_icarus`：让 vvp 加载 cocotb 的 Icarus 专用 VPI 模块，这就是 Python 测试台能读写 DUT 信号的"桥梁"。
- `MODULE=test.test_integer`：告诉 cocotb 去哪个 Python 模块里找测试函数。

#### 4.3.3 源码精读

[Common.Makefile:1-5](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Common.Makefile#L1-L5) —— 顶层模块参数与 include 目录转 `-I` 的逻辑。第 1 行把 `-s $(TOPLEVEL)` 存进 `TOPMODULE_ARG`；第 3–5 行只有当 `VERILOG_INCLUDE_DIRS` 非空时，才把它们加进 `COMPILE_ARGS`。

[Common.Makefile:7-11](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Common.Makefile#L7-L11) —— `test_%` 规则本体。第 8 行 `export LIBPYTHON_LOC=$(shell cocotb-config --libpython)`：`$(shell ...)` 在 make 解析时执行 `cocotb-config`，把结果 export 给随后启动的 vvp 子进程。第 10 行是编译，第 11 行是运行，`MODULE=test.test_$*` 用 stem 拼出 Python 测试模块名。

进入 Python 侧后，cocotb 执行 `test/test_integer.py` 里的测试函数：

[test/test_integer.py:14-15](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/test/test_integer.py#L14-L15) —— 用 `@cocotb.test()` 装饰的 `test_integer(dut)` 即被 cocotb 自动发现的测试入口，`dut` 就是顶层 `gpgpu_top` 的句柄。

[test/test_integer.py:18-26](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/test/test_integer.py#L18-L26) —— kernel 代码（即被加载进仿真出来的"代码存储器"的小程序），6 条指令分别是 `MOV32I R1,100`、`MOV32I R2,200`、`MOV R3,R2`、`IADD R1,R1,R2`、`IMUL R1,R1,R2`、`EXIT`。

[test/test_integer.py:66-78](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/test/test_integer.py#L66-L78) —— 仿真跑完后，从 `dut.U_sm_core.U_sm_operand_collect.reg_file[1/2/3]` 读出 R1/R2/R3，写日志并用 `assert` 校验期望值。这条 `dut.U_sm_core.U_sm_operand_collect....` 的层级路径，正是上一讲提到的"例化名 `U_sm_core`、`U_sm_operand_collect`"在测试台里的直接体现。

而日志写到哪里，由日志器决定：

[test/logger.py:5-13](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/test/logger.py#L5-L13) —— `Logger(log_dir="test/logs")`，文件名按启动时间戳命名为 `log_YYYYMMDDHHMMSS.txt`。由于 vvp 的工作目录是 `build/`，所以日志实际落在 **`build/test/logs/log_<时间戳>.txt`**。寄存器验证结果（R1/R2/R3）和每个周期的流水线状态转储（`dump_per_cycle`）都写到这个文件里。

#### 4.3.4 代码实践

**实践：手工拆开工具链命令，逐段理解。**

1. **实践目标**：把"一条 make 命令"还原成你能在终端里手敲的 iverilog / vvp 命令，确认每个参数的来源。
2. **操作步骤**（需已安装 iverilog 与 cocotb，见 README 的 Prerequisites）：
   - `make -C build -n test_integer 2>/dev/null`：打印（不执行）完整命令。
   - 找到 `iverilog ...` 那一行，把它的 `-s`、`-g2012`、`-I`、源文件列表与 [Common.Makefile:10](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Common.Makefile#L10) 逐字对照。
   - 找到 `vvp ...` 那一行，对照 `MODULE=test.test_integer` 与 `-m cocotbvpi_icarus`。
3. **需要观察的现象**：`-n` 输出的两行命令与 `Common.Makefile` 第 10、11 行的模板一一对应；`$@`/`$*`/`$(TOPLEVEL)`/`$(VERILOG_SOURCES)` 都已被替换成具体值。
4. **预期结果**：你能指出 `sim.vvp` 由哪条命令产生、`test.test_integer` 这个模块名来自哪个变量、cocotb 的 VPI 库路径由哪条命令查询得到。
5. **本地验证状态**：`make -n` 的展开可在仅有 make 的环境下完成；真正执行 vvp 需 iverilog+cocotb，安装后即可复现。

#### 4.3.5 小练习与答案

**Q1**：`MODULE=test.test_integer` 是怎么由 `test_integer` 这个目标名变来的？
**答**：`build/Makefile` 内的 `test_%` 规则把 `test_integer` 匹配，stem `$*` = `integer`；第 11 行写的是 `MODULE=test.test_$*`，于是拼成 `test.test_integer`，对应 `test/test_integer.py`。

**Q2**：如果运行时报 `cocotb-config: command not found`，问题出在哪一层？应如何解决？
**答**：出在 `Common.Makefile` 第 8 行的 `$(shell cocotb-config --libpython)`（make 解析时执行）。原因是未安装 cocotb，按 README 用 `pip install cocotb` 安装即可。

**Q3**：为什么要 `export LIBPYTHON_LOC=...` 而不是只在 make 里用？
**答**：因为真正需要这个值的是随后由 make 启动的 **vvp 子进程**（cocotb 的 VPI 库要链接 Python）。`export` 才能把变量传给子进程；若不 export，子进程拿不到。

---

## 5. 综合实践

把本讲三层 Makefile 串起来，完成一次完整的"清理 → 构建 → 找结果"。

**实践目标**：亲手跑通 `make clean && make test_integer`，画出 `build/` 目录树，并从日志里定位最终的 R1/R2/R3 验证结果。

**操作步骤**：

1. 确认环境：按 README 安装 iverilog（12.0+，支持 `-g2012`）与 cocotb（`pip install cocotb`），并保证 `cocotb-config`、`iverilog`、`vvp` 都在 `PATH` 中。
2. 在仓库根目录执行：
   ```bash
   make clean && make test_integer
   ```
3. 仿真结束后，用 `find build -maxdepth 2` 或 `tree build`（若已安装 tree）记录 `build/` 的目录结构。
4. 进入 `build/test/logs/`，找到最新的 `log_<时间戳>.txt`，在其中搜索 `Register Verification`。

**预期产生的 `build/` 目录结构**（由 4.1 的拷贝命令决定）：

```
build/
├── Makefile            # 来自 Main.Makefile（重命名）
├── Common.Makefile
├── gpgpu.Makefile
├── gpgpu_top.sv
├── common/             # 来自 rtl/common
│   ├── common.Makefile
│   ├── define.sv
│   ├── fixed_pri_arb.sv / fixed_pri_arb_base.sv
│   ├── oh2bin.sv
│   ├── rr_arb.sv
│   ├── sync_fifo.sv
│   └── sync_fifo_count.sv
├── sm_core/            # 来自 rtl/sm_core（9 个流水线 .sv + sm_core.Makefile）
├── test/               # 来自 test（5 个 .py）
│   └── logs/           # 仿真运行后生成
│       └── log_YYYYMMDDHHMMSS.txt
└── sim.vvp             # iverilog 编译产物
```

**预期的寄存器验证结果**（直接读自 [test_integer.py:71-78](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/test/test_integer.py#L71-L78) 的日志格式与断言，kernel 见第 18–26 行）：

```
===== Register Verification =====
R1 = 60000 (0x0000ea60)
R2 = 200   (0x000000c8)
R3 = 200   (0x000000c8)
```

数值推导（来自 kernel 指令）：R1 先置 100，经 `IADD R1,R1,R2` 变成 100+200=300，再经 `IMUL R1,R1,R2` 变成 300×200=60000；R2 由 `MOV32I` 置为 200；R3 由 `MOV R3,R2` 等于 200。这三条断言全部通过时，cocotb 才会判定 `test_integer` 成功。

> 注意：本环境未安装 iverilog/cocotb，无法实际执行；上述目录树来自拷贝命令的静态推导，R1/R2/R3 数值来自测试源码的断言与 kernel。**实际运行的终端输出与日志文件名（时间戳）请以本地运行为准（待本地验证）。**

## 6. 本讲小结

- `make test_integer` 是一条**两段式**命令：根目录 `Makefile` 负责把 RTL/test/Makefile 拷进 `build/` 并递归 make，`build/` 里的 `Main.Makefile` 才负责真正编译。
- 三层 Makefile 分工清晰：顶层 `Makefile`（拼装目录）、`Main.Makefile`（声明顶层与聚合源文件）、`Common.Makefile`（调用 iverilog/vvp/cocotb）。
- 源文件清单用 `VERILOG_SOURCES +=` 在多个子 Makefile 里分散追加，最终汇总 1（顶层）+ 4（公共）+ 9（sm_core）= 14 个 `.sv`。
- 仿真工具链是 `iverilog -g2012`（编译出 `sim.vvp`）+ `vvp`（运行）+ `cocotbvpi_icaru` VPI 插件（桥接 Python 测试台），cocotb 通过 `MODULE=test.test_integer` 定位测试代码。
- 仿真产物集中在 `build/`：编译镜像 `sim.vvp`、以及 `build/test/logs/log_<时间戳>.txt`（含每周期流水线转储与最终的 R1/R2/R3 验证结果）。
- `dut.U_sm_core.U_sm_operand_collect.reg_file[...]` 这种层级路径，是 RTL 例化名在 cocotb 测试台里的直接投射——理解它有助于后续阅读测试如何"探针式"地观察内部状态。

## 7. 下一步学习建议

- 下一讲 [u1-l3 仓库目录结构导览](u1-l3-repo-structure.md) 会把本讲提到的 `rtl/common`、`rtl/sm_core`、`test`、`project/altera` 四个目录的边界讲清楚，建议紧接着读，形成完整代码地图。
- 在进入核心流水线之前，第 2 单元会深入 `rtl/common` 的 4 个公共基础设施（`oh2bin`、`rr_arb`、`fixed_pri_arb_base`、`sync_fifo`）——它们正是本讲编译进去的"地基"，先掌握它们能让后续阅读 SM 流水线事半功倍。
- 若你对测试台本身感兴趣，可以先把 `test/memory.py`（仿真出来的代码存储器，请求-响应接口）、`test/dump.py`（每周期状态转储）通读一遍，它们是日后调试流水线最有用的工具，也是本系列末尾"端到端逐周期追踪"那一讲的主角。
