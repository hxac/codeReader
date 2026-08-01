# 仿真环境搭建与用例运行

## 1. 本讲目标

读完本讲，你应当能够：

1. 看懂 Ventus GPGPU（Verilog 版）仿真平台的四个核心文件：`Makefile`、`run.f`、`file_list.f` 以及 `CASE_*` 宏分别负责什么。
2. 独立用 `make run-vcs-4w4t` 这类目标把一个测试用例（如 `tc_gaussian`）编译并仿真跑通。
3. 在仿真日志里找到 `PASSED` / `FAILED` 判定结果与 kernel 执行周期数，并能用 Verdi 打开波形。
4. 理解「换一组 warp/thread 配置再跑」时，为什么必须同步修改 `define.v` 里的 `NUM_THREAD`。

## 2. 前置知识

本讲默认你已经读过：

- **u1-l1 项目定位与硬件架构总览**：知道顶层 `GPGPU_top`、SM 核、CTA 调度、L2、AXI 接口等部件的存在。
- **u1-l2 源码目录结构与模块组织**：知道 `src/`、`testcase/`、`model_list` 的大致分工，以及 `run.f` 通过 `-f` 拉入文件清单的机制。
- **u1-l3 核心配置参数 define.v**：知道 `NUM_SM` / `NUM_WARP` / `NUM_THREAD` 是规模总开关，且 `NUM_THREAD` 仿真前必须确认。

几个本讲会用到的通用概念：

- **RTL 仿真（simulation）**：用软件（这里是 Synopsys VCS）模拟硬件电路，给一段时钟和复位，观察内部信号随时间的变化，最终判断功能对不对。Verilog 代码本身不能「运行」，必须经过「编译（elaborate）→ 生成可执行仿真器 `simv` → 执行」三步。
- **testbench（测试平台）**：一段专门用来「驱动」被测设计（DUT, Design Under Test）的代码，它产生时钟、复位、输入激励，并检查输出。本项目的 DUT 是 `gpgpu_axi_top`。
- **FSDB 波形**：Verdi 使用的波形文件格式（`.fsdb`）。仿真时把信号变化 dump 进 `test.fsdb`，之后用 Verdi 打开逐个时钟查看。
- **宏（`define`）**：编译期符号。`+define+CASE_4W4T` 表示在编译时定义一个名为 `CASE_4W4T` 的宏，代码里用 `\`ifdef CASE_4W4T` 选择对应的分支。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `testcase/` 仿真目录下，外加一个配置文件回顾：

| 文件 | 作用 |
|------|------|
| `testcase/test_gpgpu_axi_top/common/run.f` | **编译选项文件**：告诉 VCS 用哪些编译选项、顶层模块是谁、要编译哪些文件。 |
| `testcase/test_gpgpu_axi_top/common/file_list.f` | **testbench 文件清单**：列出测试平台需要的几个 `.sv`/`.v` 文件。 |
| `testcase/test_gpgpu_axi_top/tc_gaussian/Makefile` | **仿真入口**：封装成 `make run-vcs-4w4t` 等目标，一键调用 VCS。 |
| `testcase/test_gpgpu_axi_top/tc_gaussian/tc.v` | **测试用例主体**：用 `CASE_*` 宏选择软件数据、驱动 GPU、判定 PASSED/FAILED。 |
| `testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv` | **仿真顶层**：例化 DUT、时钟、复位、host、内存模型，并 dump 波形。 |
| `src/define/define.v` | **配置总开关**（u1-l3 已讲）：`NUM_THREAD` 等参数，换配置时需同步修改。 |
| `README.md` | 项目说明，含「开始」「测试用例说明」两节，给出官方命令与周期数参考。 |

> 提示：`testcase/` 下还有一套不带 AXI 的平台 `test_gpgpu_top/`，用法完全一样；本讲以带 AXI 的 `test_gpgpu_axi_top/` 为例。

## 4. 核心概念与源码讲解

### 4.1 run.f：VCS 编译选项与文件组织

#### 4.1.1 概念说明

VCS 编译一个大型 Verilog 项目时，需要告诉它很多信息：支持哪种语法版本、顶层模块叫什么、源码文件在哪里、头文件（`define.v`）去哪里找。这些信息如果全敲在命令行里会非常长，所以项目把它们写进一个**编译选项文件** `run.f`，再用 `vcs ... -f run.f` 一次性读入。可以把 `run.f` 理解成「VCS 编译的配方表」。

`run.f` 里每行是一个编译选项或一条指令；以 `//` 开头的是注释。它把整个仿真编译拆成五块：编译选项、顶层、testbench、include 路径、RTL 源码。

#### 4.1.2 核心流程

`run.f` 的执行（被 VCS 读取）流程：

```text
vcs -f run.f
   │
   ├─ 读 L1~L13：语法/调试选项（sverilog、+notimingcheck、+nospecify …）
   ├─ 读 L18：-top test_gpu_axi_top        ← 顶层模块名
   ├─ 读 L23：-f ../common/file_list.f     ← 拉入 testbench 文件清单
   ├─ 读 L28：+incdir+../../../src/define/ ← 告诉编译器 define.v 在哪
   └─ 读 L33：-f ../../../src/gpgpu_top/model_list ← 拉入全部 RTL 源码
```

关键是两处 `-f` 形成了**文件清单的嵌套**：`run.f` → `file_list.f`（testbench）和 `run.f` → `model_list`（RTL），下一节会展开。

#### 4.1.3 源码精读

先看编译选项段（语法版本与调试开关）：

[run.f:L1-L13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L1-L13) —— `-sverilog` 让 VCS 支持 SystemVerilog；`+notimingcheck` / `+nospecify` 关闭时序检查和 specify 延迟，让仿真只看功能不看时序（功能仿真常用）；`-debug_access+all` 为 Verdi 调试保留全部信号可见性。注释掉的 `+define+T28_MEM` 是 28nm 工艺宏存储器开关，综合/流片时才用，普通仿真不开。

顶层与文件组织段：

[run.f:L15-L39](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/run.f#L15-L39) —— `-top test_gpu_axi_top` 指定仿真顶层（即 testbench 顶层，不是硬件顶层 `GPGPU_top`）；`-f ../common/file_list.f` 拉入 testbench 清单；`+incdir+../../../src/define/` 给 `\`include "define.v"` 提供搜索路径；`-f ../../../src/gpgpu_top/model_list` 把 `src/gpgpu_top/` 下全部 RTL 一次性编进来。

#### 4.1.4 代码实践

**目标**：弄清 `run.f` 如何同时把 testbench 和 RTL 两大堆文件喂给 VCS。

**步骤**：

1. 打开 `testcase/test_gpgpu_axi_top/common/run.f`。
2. 找到 `-f ../common/file_list.f` 这行，记下它（指向下一节的清单）。
3. 找到 `-f ../../../src/gpgpu_top/model_list`，用编辑器打开 `src/gpgpu_top/model_list`，看看它列了多少个 `.v` 文件（第一行就是 `define.v`）。

**需要观察的现象**：`model_list` 是一个很长的文件路径列表，它把整个 GPGPU 的 RTL 都串了起来——这就是为什么 `run.f` 只需一行 `-f model_list` 就能编进全部硬件源码。

**预期结果**：你会在 `model_list` 里看到 `cta_top/`、`sm/`、`l2cache/` 等 u1-l2 讲过的子目录下的文件，印证了「仿真编译 = testbench 清单 + RTL 清单」的结构。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `run.f` 里的 `+incdir+../../../src/define/` 这行删掉，编译时会在哪一步报错？
**答案**：会在编译包含 `\`include "define.v"` 的文件时报「找不到 define.v」错误，因为编译器失去了查找该头文件的搜索路径。

**练习 2**：`run.f` 里有两行被注释的 `+define+T28_MEM` 和 `-f ../../../t28_mem/model_list`，它们为什么不打开？
**答案**：`T28_MEM` 是 28nm 工艺专用的宏存储器模型（替代行为级 SRAM），用于综合/流片评估；普通功能仿真用行为级模型即可，故默认注释关闭。

---

### 4.2 file_list.f：testbench 文件清单

#### 4.2.1 概念说明

`file_list.f` 是 `run.f` 通过 `-f` 拉入的**第二级文件清单**，专门列出仿真平台（testbench）需要的文件。它和 `model_list`（RTL 清单）分工明确：`model_list` 管被测硬件，`file_list.f` 管测试环境。

注意清单里有一条 `./tc.v`——它用的是**相对路径**，指当前测试用例目录下的 `tc.v`。这正是不同用例（`tc_gaussian`、`tc_vecadd`…）能共用同一套 `common/` 平台、却各自加载不同测试逻辑的关键。

#### 4.2.2 核心流程

```text
file_list.f（common/）列出 6 个文件：
  host_inter.sv     ← 模拟主机：经 AXI4-Lite 派发 workgroup、加载程序数据
  test_gpu_axi_top.sv ← 仿真顶层：例化 DUT/时钟/复位/host/内存
  gen_rst.v         ← 产生复位信号
  gen_clk.v         ← 产生时钟信号
  axi_ram.sv        ← AXI 外部内存模型（存放 kernel 指令与数据）
  ./tc.v            ← 当前用例目录下的测试主体（每个用例各一份）
```

#### 4.2.3 源码精读

完整清单只有 6 行：

[file_list.f:L1-L6](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/file_list.f#L1-L6) —— 前 5 行是公共平台文件（位于 `common/`），最后一行 `./tc.v` 是用例私有文件。

时钟与复位生成器（两个最简单的平台组件）：

[gen_clk.v:L4-L20](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/gen_clk.v#L4-L20) —— `PERIOD = 10.0`，配合 testbench 的 `timescale 1ns/1ps`，表示时钟周期 10ns（半周期 5ns 翻转一次），对应 100MHz 仿真时钟。

[gen_rst.v:L6-L18](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/gen_rst.v#L6-L18) —— `RST_CYCLE_N = 2`，复位在最初 2 个时钟上升沿保持低电平（`rst_n=0`），之后拉高，即「上电复位 2 拍」。

#### 4.2.4 代码实践

**目标**：验证不同用例共用 `common/` 平台、仅替换 `tc.v`。

**步骤**：

1. 列出 `testcase/test_gpgpu_axi_top/` 下的用例目录（`tc_gaussian`、`tc_vecadd`、`tc_matadd`、`tc_nn`、`tc_bfs`），确认每个目录里都有自己的 `tc.v`。
2. 对比 `tc_gaussian/tc.v` 和 `tc_vecadd/tc.v` 的开头，看 `CASE_*` 宏定义和 `softdata/` 路径有何不同。

**需要观察的现象**：两个 `tc.v` 顶部声明的 `CASE` 选项不同（gaussian 是 `2W8T/1W16T/4W4T/4W8T`，vecadd 是 `8W4T/4W8T/4W16T/4W32T`），加载的软件数据子目录也不同。

**预期结果**：你会理解——同一份 `file_list.f` + `run.f` 平台，通过替换 `./tc.v` 就能跑不同算法用例，平台是可复用的。

#### 4.2.5 小练习与答案

**练习 1**：`file_list.f` 里 `./tc.v` 的 `./` 指的是哪个目录？
**答案**：指 VCS 的**当前工作目录**，即执行 `make` 时所在的用例目录（如 `testcase/test_gpgpu_axi_top/tc_gaussian/`），所以每个用例加载的是自己目录下的 `tc.v`。

**练习 2**：仿真时钟频率是多少？由哪两个量共同决定？
**答案**：100MHz。由 `gen_clk.v` 里的 `PERIOD = 10.0` 和 testbench 的 `timescale 1ns/1ps` 共同决定：10ns 周期 → 100MHz。（注意这只是仿真频率，与硬件综合频率 620MHz 无关。）

---

### 4.3 Makefile：仿真目标与 VCS 命令

#### 4.3.1 概念说明

直接敲一长串 `vcs ...` 命令容易出错，也不便切换不同 warp/thread 配置。项目用一个 `Makefile` 把每种配置封装成一个 **make 目标**（target），例如 `make run-vcs-4w4t`。Makefile 的本质就是「给一长串命令起个短名字」。

每个目标的命令结构相同，只有末尾的 `+define+CASE_xWyT` 不同——这正是切换配置的唯一开关。

#### 4.3.2 核心流程

```text
make run-vcs-4w4t
   └─ 实际执行：
      vcs -full64 -LDFLAGS -Wl,--no-as-needed -R \
          -sverilog -timescale=1ns/1ps \
          -f ../common/run.f \         ← 读取编译配方
          -debug_access+all \
          +fsdb+functions \            ← 启用 FSDB 波形函数
          -l simv.log \                ← 编译日志写到 simv.log
          +define+CASE_4W4T            ← 唯一随目标变化的开关
```

VCS 执行这条命令会先编译生成仿真器 `simv`，然后自动运行 `simv` 开始仿真。

四个常用目标：`run-vcs-4w4t` / `run-vcs-2w8t` / `run-vcs-4w8t` / `run-vcs-1w16t`（仿真），`verdi`（看波形），`clean`（清理中间文件）。执行 `make` 或 `make help` 可列出全部目标。

#### 4.3.3 源码精读

`tc_gaussian` 的 Makefile 全貌：

[tc_gaussian/Makefile:L2-L10](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/Makefile#L2-L10) —— `help` 目标打印可用命令清单。

各配置目标，注意末尾 `+define+CASE_*` 的差异：

[tc_gaussian/Makefile:L12-L22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/Makefile#L12-L22) —— 四个仿真目标，命令模板完全相同，仅 `CASE` 宏不同。`+fsdb+functions` 让仿真过程中能调用 `$fsdbDumpvars` 等 FSDB 函数把波形写入 `test.fsdb`；`-l simv.log` 把编译/运行日志重定向到 `simv.log`。

Verdi 与清理目标：

[tc_gaussian/Makefile:L24-L28](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/Makefile#L24-L28) —— `make verdi` 用 `run.f` 打开 Verdi 并加载 `./test.fsdb` 波形；`make clean` 删除 `simv*`、`csrc`、`*.fsdb` 等中间产物。

> ⚠️ **重要**：不同用例的 Makefile 目标集合不同。`tc_gaussian` 有 `run-vcs-4w4t`，但 `tc_vecadd` **没有** `4w4t`，它提供的是 `run-vcs-8w4t` / `4w8t` / `4w16t` / `4w32t`（见 [tc_vecadd/Makefile:L12-L22](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_vecadd/Makefile#L12-L22)）。所以「换配置再跑」时，要先确认该用例的 Makefile 里有没有对应目标。

#### 4.3.4 代码实践

**目标**：跑通一个用例，找到日志文件位置。

**步骤**：

1. 进入 `testcase/test_gpgpu_axi_top/tc_gaussian/`。
2. 确认 `src/define/define.v` 里 `NUM_THREAD` 当前值（见 4.4 节，必须与所选 CASE 的 `T` 一致）。
3. 执行 `make run-vcs-4w4t`。
4. 仿真结束后，打开当前目录下的 `simv.log` 查看编译与运行输出。

**需要观察的现象**：终端（或 `simv.log` 末尾）会打印大字 `PASSED` 或 `FAILED`，以及 `All kernels need : N cycles`。

**预期结果**：高斯消元 4w4t 配置应输出 `PASSED`，周期数约 11537（README 表格参考值，待本地验证具体数值）。

#### 4.3.5 小练习与答案

**练习 1**：`make run-vcs-4w4t` 与 `make run-vcs-4w8t` 两条命令，哪一部分不同？为什么？
**答案**：只有末尾的 `+define+CASE_4W4T` 与 `+define+CASE_4W8T` 不同。它切换编译期宏，使 `tc.v` 选择不同的软件数据子目录（`softdata/4x4` vs `softdata/4x8`）。

**练习 2**：为什么 `make verdi` 要在 `make run-vcs-*` 之后执行？
**答案**：`run-vcs-*` 仿真时通过 `$fsdbDumpfile` 把波形写进 `test.fsdb`；Verdi 需要读取这个已生成的 `.fsdb` 文件，所以必须先仿真产生波形，再 `make verdi` 打开。

---

### 4.4 CASE 宏：测试配置切换与结果判定

#### 4.4.1 概念说明

`CASE_xWyT` 是整个仿真的「配置开关」，名字里的 `W` 表示 warp 维度、`T` 表示 thread 维度。它做三件事：

1. **选择软件数据**：每个 CASE 对应一个预编译好的 kernel 二进制目录（`softdata/4x4`、`softdata/4x8` 等），里面是给 GPU 跑的指令（`.metadata`）和数据（`.data`）。
2. **决定 `NUM_THREAD`**：kernel 的向量寄存器长度 `VLEN = NUM_THREAD × 32` 是按某个 thread 数编译的，硬件 `NUM_THREAD` 必须与之匹配，否则结果错乱。所以 **`T` 的值 = `define.v` 里的 `NUM_THREAD`**。
3. **选择判定分支**：`tc.v` 里用 `\`ifdef CASE_*` 选择对应的「正确答案」和 `PASSED/FAILED` 打印分支。

> 这是本讲最容易踩坑的点：**换 CASE 必须同步改 `NUM_THREAD`**。README 和 `tc.v` 都反复强调过。

#### 4.4.2 核心流程

```text
make run-vcs-4w4t  (+define+CASE_4W4T)
        │
        ├─ tc.v: init_test_file 选 softdata/4x4 下的 metadata/data 文件名
        │
        ├─ test_main 循环（共 FILE_NUM 个 kernel）:
        │     init_mem  → 把 .data 经 AXI 写进 axi_ram（预加载指令/数据）
        │     drv_gpu   → host_inter 经 AXI4-Lite 派发 workgroup
        │     exe_finish→ 等待 kernel 执行完，统计 kernel_cycles
        │
        └─ print_result:
              把 axi_ram 里的硬件输出与软件黄金参考比对
              全对 → PASSED 任务（打印 ASCII 大字）
              有错 → FAILED 任务
              打印 "All kernels need : N cycles"
```

`CASE_4W8T` 比其他 CASE 多加载 2 个文件（`FILE_NUM=8` 而非 6），因为它跑的是五元方程组，比四元的多一组数据。

#### 4.4.3 源码精读

`tc.v` 顶部的 CASE 选择注释与宏：

[tc.v:L15-L19](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L15-L19) —— 注释明确提醒「Select gaussian test case, remember modify `define NUM_THREAD at the same time」（选择用例时，记得同时修改 `NUM_THREAD`）。

`FILE_NUM` 随 CASE 变化：

[tc.v:L31-L35](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L31-L35) —— `CASE_4W8T` 时 `FILE_NUM=8`，其余为 6。

仿真主流程（`initial` 块驱动）：

[tc.v:L50-L58](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L50-L58) —— 等 100 拍后初始化文件、跑 `test_main`、再等 100 拍结束。

`test_main` 循环驱动并累计周期：

[tc.v:L133-L153](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L133-L153) —— 每个 kernel 依次 `init_mem`（预加载内存）、`\`drv_gpu`（派发）、`\`exe_finish`（等完成），并把 `\`kernel_cycles` 累加进 `sum_cycles`。

`CASE_4W4T` 的判定分支（其余 CASE 结构相同）：

[tc.v:L366-L374](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L366-L374) —— 当矩阵和数组全部比对正确（`&matrix_4_pass && &array_4_pass`）时调用 `PASSED`，否则 `FAILED`。

最终周期数打印：

[tc.v:L385-L389](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/tc_gaussian/tc.v#L385-L389) —— `All kernels need : %p cycles` 输出累计的 `sum_cycles`。

`PASSED` / `FAILED` 本身是定义在仿真顶层的 task（打印 ASCII 艺术字大字）：

[test_gpu_axi_top.sv:L251-L273](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/testcase/test_gpgpu_axi_top/common/test_gpu_axi_top.sv#L251-L273) —— `PASSED` 与 `FAILED` 两个 task 仅负责 `$display` 打印大字，本身不做判定；判定逻辑在 `tc.v` 的 `print_result` 里。

配置总开关回顾（u1-l3 已详述）：

[define.v:L5-L13](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L5-L13) —— `NUM_SM=2`、`NUM_WARP=4'b1000`(=8)、`NUM_THREAD=4`(默认，仅快速仿真用)、`NUM_LANE=NUM_THREAD`。换到 `8T` 配置时需把 `NUM_THREAD` 改为 8。

#### 4.4.4 代码实践（本讲主实践）

**目标**：用两组配置（4w4t 与 4w8t）分别仿真 `tc_gaussian`，对比 `PASSED/FAILED` 与周期数，体会「换配置必须改 `NUM_THREAD`」。

**步骤**：

1. **第一组：4w4t**。确认 `src/define/define.v` 中 `NUM_THREAD` 为 `4`，进入 `testcase/test_gpgpu_axi_top/tc_gaussian/` 执行：
   ```shell
   make run-vcs-4w4t
   ```
   记录 `simv.log` 末尾的 `PASSED`/`FAILED` 与 `All kernels need : N cycles`。

2. **第二组：4w8t**。先把 `define.v` 的 `NUM_THREAD` 改为 `8`（这是必须的一步，否则 kernel 的 VLEN 与硬件不符），再执行：
   ```shell
   make clean
   make run-vcs-4w8t
   ```
   记录新的 `PASSED`/`FAILED` 与周期数。

3. （可选）用 `make verdi` 打开 `test.fsdb`，在波形里找到 `clk`、`rst_n`，确认上电复位 2 拍的时序。

**需要观察的现象**：
- 两组都应输出 `PASSED`。
- 周期数不同：4w4t 约 11537，4w8t 约 15940（README 参考值，待本地验证）——注意 4w8t 跑的是五元方程组，数据量更大，所以周期更长，并非单纯「线程多就快」。
- 若第二组忘了改 `NUM_THREAD`，很可能得到 `FAILED` 或异常周期数——这就是配置不一致的典型症状。

**预期结果**：理解 `CASE` 宏、`NUM_THREAD`、软件数据三者必须三方一致，仿真才有意义。

> 关于 `W` 维度：`define.v` 中 `NUM_WARP` 默认为 8（`4'b1000`），README 仅要求改 `NUM_THREAD`。`CASE` 名里的 `W` 主要描述 kernel 使用的 warp 数，硬件 `NUM_WARP=8` 通常足以容纳；若某用例需要更小的 warp 数才能复现，需本地验证是否要同步调整 `NUM_WARP`（待本地确认）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CASE_4W4T` 跑 `NUM_THREAD=8` 会失败？
**答案**：`CASE_4W4T` 加载的 kernel 二进制是按 `NUM_THREAD=4`（VLEN=128）编译的；若硬件 `NUM_THREAD=8`（VLEN=256），一条向量指令处理的元素数和软件预期不一致，访存与计算结果都会错位，导致比对失败。

**练习 2**：`PASSED` 这个大字是哪里打印的？判定「通过」的条件又是在哪里算出来的？
**答案**：大字由 `test_gpu_axi_top.sv` 里的 `PASSED` task 打印；但「是否调用它」的判定条件（硬件输出 == 软件黄金参考）是在 `tc.v` 的 `print_result` task 里算出来的。

**练习 3**：`tc.v` 里 `sum_cycles` 是怎么得到的？
**答案**：`test_main` 循环中每个 kernel 执行 `\`exe_finish` 后，把 `\`kernel_cycles`（由 `host_inter` 统计的单个 kernel 周期数）累加进 `sum_cycles`，最后在 `print_result` 一次性打印总和。

## 5. 综合实践

**任务**：以 `tc_gaussian` 为对象，亲手完成「读配置 → 编译仿真 → 看结果 → 换配置再跑 → 看波形」的完整闭环，并把四个核心文件串起来理解。

**操作清单**：

1. **读懂配方**：打开 `common/run.f`，用笔标出「顶层、testbench 清单、include 路径、RTL 清单」四段对应的行号；再打开 `common/file_list.f` 数清楚 testbench 由几个文件组成。
2. **核对配置**：打开 `src/define/define.v`，记下 `NUM_SM` / `NUM_WARP` / `NUM_THREAD` 当前值，并对照 `tc.v` 里 `CASE_4W4T` 注释，确认 `NUM_THREAD` 与所选 CASE 匹配。
3. **首次仿真**：在 `tc_gaussian/` 执行 `make run-vcs-4w4t`，从 `simv.log` 中抄下 `PASSED/FAILED` 和周期数。
4. **切换配置**：把 `NUM_THREAD` 改为 8，`make clean && make run-vcs-4w8t`，再抄一组结果，与第 3 步对比。
5. **看波形**：执行 `make verdi`，在 Verdi 中加载 `test.fsdb`，观察 `u_gen_clk.clk` 的周期与 `u_gen_rst.rst_n` 的复位时长，验证你对 `gen_clk`/`gen_rst` 参数的理解。
6. **画一张关系图**：把 `Makefile` → `run.f` → `file_list.f` / `model_list` → 各 `.v`/`.sv` 文件这条「命令到源码」的包含链画出来，作为本讲的产出。

**预期收获**：你能独立搭建并运行仿真，并彻底搞清楚一条 `make` 命令背后到底编译了哪些文件、`CASE` 宏如何贯通「软件数据—硬件配置—结果判定」三处。

## 6. 本讲小结

- 仿真编译由 `run.f` 统一调度：它声明顶层 `test_gpu_axi_top`，并通过 `-f` 嵌套拉入 `file_list.f`（testbench）与 `model_list`（全部 RTL），用 `+incdir+` 定位 `define.v`。
- `file_list.f` 列出 6 个平台文件，其中 `./tc.v` 是用例私有文件——这是多用例共用 `common/` 平台的关键。
- `Makefile` 把冗长的 VCS 命令封装成 `make run-vcs-xWyT` 等目标，目标间唯一差异是末尾的 `+define+CASE_xWyT` 宏。
- `CASE_xWyT` 宏三合一：选软件数据目录、决定 `NUM_THREAD`（`T` 值）、选 `PASSED/FAILED` 判定分支；**换 CASE 必须同步改 `define.v` 的 `NUM_THREAD`**。
- 仿真结果在 `simv.log`：`PASSED`/`FAILED` 由 `tc.v` 的 `print_result` 比对硬件输出与黄金参考后决定，周期数由 `host_inter` 统计的 `kernel_cycles` 累加得到。
- 波形在 `test.fsdb`，由 `test_gpu_axi_top.sv` 的 `$fsdbDumpvars` 产生，用 `make verdi` 打开。

## 7. 下一步学习建议

- **下一讲 u1-l5（顶层模块 GPGPU_top 与系统数据流）**：从仿真顶层 `test_gpu_axi_top` 进一步深入到硬件顶层 `GPGPU_top`/`gpgpu_axi_top`，看主机请求如何接入 CTA 调度再分发到各 SM。
- **进阶阅读**：若想搞清 `host_inter` 如何加载 metadata、驱动 AXI4-Lite 派发 workgroup 并等待 `wf_done`，可提前浏览 `common/host_inter.sv`（这套机制的完整讲解在 **u8-l1 仿真测试框架与 testbench**）。
- **动手延伸**：尝试在 `tc_vecadd`（结构最简单的向量加法）上跑通一组配置，对照 README 的「测试用例说明」表，验证你记录的周期数与官方参考值是否接近。
