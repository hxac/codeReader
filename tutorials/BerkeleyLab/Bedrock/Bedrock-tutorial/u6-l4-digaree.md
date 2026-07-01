# digaree：大型 DSP 应用工程

> 本讲对应大纲 `u6-l4`，依赖 `u3-l4`（CIC/FIR/IIR 滤波与定点定标）。阅读本讲前，你应已了解定点数的位宽/定标概念，以及 Bedrock「Make + iverilog + Python」的统一测试手势（见 `u1-l2`、`u2-l1`）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **digaree**（Digital Arithmetic Execution Engine）是什么：一个「单数据类型、无分支、每条指令固定周期」的**定点协处理器**，定位在「快速但简单的 FPGA 逻辑」与「慢但通用的 CPU」之间。
- 看懂 digaree 的**代码生成链**：用 Python（`cgen_srf.py` 等）把一段复数算式翻译成 C 形式的「汇编」`ops.h`，再用 `sched.py` 调度成 Verilog ROM `ops.vh`，最后被 `sf_user.v` `` `include `` 进指令存储器。
- 理解 digaree 的**定点数学**：18 位端口、22 位内部、`sf_inv` 查表 + Newton 迭代求 1/x，以及 `pfloat.py` 如何把 SI 单位的浮点物理量定标成定点寄存器初值并做校验。
- 读懂**执行核** `sf_main.v`（ALU + 双端口寄存器堆 + 5 级流水）和它的**程序/外设层** `sf_user.v` / `sf_user_wrap.v`（PC、指令 ROM、主机参数注入）。
- 自己跑一遍 `make -C dsp/digaree` 的子集，把「Python 描述算法 → 生成 Verilog → C 与 Verilog 交叉验证」这条**复杂工程自测范式**走通。

## 2. 前置知识

digaree 把「软件工程里的编译器思路」搬进了硬件设计。在看源码前，先对齐几个概念：

- **定点数（scaled fixed-point）**：用一个整数寄存器来表示小数。比如约定「寄存器值 ÷ 2¹⁷ = 真实值」，那么整数 `65536` 就代表 `0.5`。 digaree 故意不用浮点，因为浮点的舍入误差**依赖数据**（denormal/对阶），而作者要的是「可预测、恒定的噪声水平」（见 [dsp/digaree/README:21-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L21-L24)）。
- **ALU 与寄存器堆**：ALU 是算术逻辑单元（这里只有 `mul`/`add`/`sub`/`inv` 四种运算）；寄存器堆是一组暂存中间结果的存储。 digaree 用「一写口、两读口」的 32 项寄存器堆，每个时钟节拍读两个源、写一个目的。
- **指令字与微码 ROM**：把每条运算编码成一段二进制（指令字），按地址存在只读存储器（ROM）里，靠程序计数器（PC）逐拍取出执行——和 CPU 取指执行同构，但极简。
- **代码生成（code generation）**：人不直接写成百上千条指令字，而是用 Python 描述高层算式（如「复数乘法」），由脚本展开成底层指令序列。 digaree 的 `cgen_*.py` 就是这种「领域专用语言 + 编译器」。
- **位精确（bit-accurate）仿真**：用 C 写一份与硬件**每一位都一致**的行为模型，跑同样的指令流，再把 C 的输出和 Verilog 仿真输出逐行 `cmp` 比对——如果完全相同，就证明硬件实现没跑偏。这是 digaree 自测的核心保险。

> 一个贯穿全讲的关键认知：digaree 是 Bedrock 里**把"软件编译流水线"内嵌进硬件工程**的典范。理解它，比读懂单个 Verilog 模块更重要的是看清「描述 → 生成 → 仿真 → 比对」这条链。

## 3. 本讲源码地图

digaree 全部位于 `dsp/digaree/`，且**与 Bedrock 其它子系统解耦**（见 [dsp/digaree/README:1-2](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L1-L2)、[dsp/digaree/Makefile:1-2](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile#L1-L2)），自带独立的 `Makefile`，用 `make -C dsp/digaree` 自测。

| 文件 | 作用 | 归属的最小模块 |
|---|---|---|
| [dsp/digaree/README](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README) | 设计哲学与使用说明，本讲的权威向导 | 代码生成 / 定点 |
| [dsp/digaree/cgen_srf.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py) | 用 Python 描述「SRF 腔体 detune/淬灭检测」算式，产出 C 形式汇编 `ops.h` | cgen_*.py 代码生成 |
| [dsp/digaree/cgen_lib.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py) | 原语库：`mul`/`add`/`sub`/`cpx_mul`/`full_inv` 等，供各 `cgen_*.py` 复用 | cgen_*.py 代码生成 |
| [dsp/digaree/sched.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sched.py) | 调度器/寄存器分配器：把 `ops.h` 排成 `ops.vh` 的 Verilog ROM | cgen_*.py 代码生成 |
| [dsp/digaree/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/rules.mk) | 定义 `ops.h`/`ops.vh` 的生成规则与 `OPS_STYLE` 风格切换 | cgen_*.py 代码生成 |
| [dsp/digaree/pfloat.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/pfloat.py) | SI 单位浮点物理模型 → 18 位定点定标，输出初值数据并支持与 `sim1` 对比校验 | pfloat.py 定点 |
| [dsp/digaree/initgen_srf.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/initgen_srf.py) | 合成模型版本的初值生成器（产出 `init.dat`，符号名形式） | pfloat.py 定点 |
| [dsp/digaree/sf_main.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v) | **执行核**：`sf_mul`/`sf_add`/`sf_inv`/`sf_alu` + 寄存器堆 + `sf_main` 顶层 | sf_main 顶层 |
| [dsp/digaree/sf_user.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v) | 在 `sf_main` 上加 PC、指令 ROM（`` `include "ops.vh" ``）、参数注入、饱和计数 | sf_user 顶层 |
| [dsp/digaree/sf_user_wrap.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user_wrap.v) | 两种参数接入包装：`sf_user_pmem`（DPRAM）/`sf_user_preg`（并行寄存器组） | sf_user 顶层 |
| [dsp/digaree/sim1.c](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sim1.c) | C 位精确（非周期精确）仿真器，消费 `ops.h`，用于交叉校验 | 自测 |
| [dsp/digaree/main_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/main_tb.v) / [user_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/user_tb.v) / [inverse_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/inverse_tb.v) | 三套测试台 | 自测 |
| [dsp/digaree/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile) | 编排「生成 → 编译 → 仿真 → 比对」的全流程 | 自测 |

> 规格里出现的 `dsp/digaref/sf_main.v` 是笔误，正确路径是 `dsp/digaree/sf_main.v`。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 代码生成链**、**4.2 定点数学与求逆**、**4.3 执行核与程序/外设层**。

### 4.1 digaree 是什么：Python 驱动的代码生成链

#### 4.1.1 概念说明

digaree 的全称是 **Digital Arithmetic Execution Engine**（数字算术执行引擎）。它解决的问题是：在 LLRF（低电平 RF 控制）里，有些算式既不算太简单（无法用几级流水线乘加直接搭），也不算太复杂（不值得请一颗通用 CPU），例如：

- **SRF 超导腔的 detune（失谐）与淬灭检测**：从腔体、前向、反射三路复数采样，实时算出失谐频率、衰减率、功率失衡（见 [dsp/digaree/cgen_srf.py:1-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py#L1-L12)）。
- **RF 失真补偿（IP3）**：见 `cgen_ip3.py`（README 注明此路已「bit-rotted」，即年久失修）。

作者的取舍极其激进，[dsp/digaree/README:12-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L12-L19) 借 Saint-Exupéry 的话说「完美不在于无可增，而在于无可减」：**只有一种数据类型、没有分支指令、每条指令耗时相同**；除法、复数簿记、甚至指令位在流水线各级的对齐，全部由编译阶段（Python）处理。结果是用「几百个逻辑单元 + 1 个乘法器」做到「接近每拍一次乘加」。

#### 4.1.2 核心流程：从算式到 Verilog ROM

digaree 的构建是一条**三级流水式的代码生成链**：

```text
 cgen_srf.py  ──(描述算式, 调用原语)──▶  ops.h   (C99 形式的"汇编")
      │                                  │
      │ 依赖 cgen_lib.py 的原语           │ 被两路消费：
      │                                  ├─▶ sched.py ──▶ ops.vh (Verilog case 项) ──▶ sf_user.v `include
      │                                  └─▶ sim1.c    ──▶ sim1   (C 位精确仿真)
 sched.py 做两件事: 寄存器分配 + 指令调度(列表调度, pipe_len=5)
 最终: ops.vh 被包进 sf_user.v 的 `case(pc)` 里, 成为指令 ROM
```

1. **描述算法**：`cgen_srf.py` 像写高级语言一样声明输入变量（`given`）、持久状态（`cpx_persist`），再用原语（`cpx_sub`/`cpx_mul`/`cpx_inv_conj`/`set_result`）一行行写出算式。它输出的 `ops.h` 既是合法 C99，也是调度器的输入（见 [dsp/digaree/cgen_srf.py:10-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py#L10-L12)）。
2. **调度**：`sched.py` 读 `ops.h`，跟踪每个变量的「何时生效」「何时用完」，为每条指令挑选最早的可用 PC 槽，最终把结果排成 Verilog 的 `case(pc)` 项 `ops.vh`。生成规则定义在 [dsp/digaree/rules.mk:18-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/rules.mk#L18-L22)：`ops.h` 由 `cgen$(OPS_STYLE).py` 产出，`ops.vh` 由 `sched.py ops.h` 产出（顶层 `Makefile` 通过 `include rules.mk` 复用，见 [Makefile:47-49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile#L47-L49)）。
3. **织入硬件**：`sf_user.v` 用 `` `include "ops.vh" `` 把这张指令表嵌进 PC 驱动的 ROM。

README 自豪地指出，这条链的 Python 仅约 204 行（非空非注释），比任何商用 CPU 工具链都小几个数量级（[dsp/digaree/README:33-41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L33-L41)）。

#### 4.1.3 源码精读

**(a) 用 Python 写算式** —— `cgen_srf.py` 的状态变量计算段，是整段算法的核心（求复数 `a`，给出 detune 频率与衰减率）：

[dsp/digaree/cgen_srf.py:53-60](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py#L53-L60)：这一段先求 `1/V` 的共轭（`cpx_inv_conj`）、把差分变成 `dV/dt`（`cpx_scale`）、算驱动项 `K·beta`（`cpx_mul`）、做差（`cpx_sub`）、最后乘 `1/V`（`cpx_mul_conj`）得到 `a`，并用 `set_result("ab", ...)` 把结果送到输出端口 `a_o/b_o`。每个原语末尾的整数是**移位参数**（结果再乘 \(2^{\text{shift}}\)），这是定点程序里就地定标的手段。

特别注意 [dsp/digaree/cgen_srf.py:74-79](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py#L74-L79)：末尾把腔体电压历史 `v1..v4` 整体向后挪一拍（`v4←v3←v2←v1←v`），注释明确写 **"order of execution matters, unlike everything else here"**——这是整段里唯一对顺序敏感的地方（其余靠 `sched.py` 自由调度）。

**(b) 原语库把高层算式展开成 mul/add** —— 复数乘法在硬件上没有单条指令，`cgen_lib.py` 把它拆成 4 个实数乘 + 加减：

[dsp/digaree/cgen_lib.py:91-99](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py#L91-L99) 实现了 \((a+jb)(c+jd)\)：先算 4 个积 `t1=ar·br`、`t2=ar·bi`、`t3=ai·br`、`t4=ai·bi`，再 `实部=t1-t4`、`虚部=t2+t3`。`shift2` 给最后的加减做定标。所有原语最终都落到 [dsp/digaree/cgen_lib.py:52-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py#L52-L68) 的 `add`/`sub`/`mul` 三个叶子函数，它们 `print` 出 C 语句——这就是 `ops.h` 的由来。

**(c) 生成规则与风格切换** —— [dsp/digaree/rules.mk:6-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/rules.mk#L6-L22)：用 `OPS_STYLE` 变量在 `_srf`（腔体）与 `_ip3`（失真）两套算法间切换，`cgen$(OPS_STYLE).py` 决定走哪个生成器；`ops.h` 由 `cgen` 产出，`ops.vh` 由 `sched.py ops.h` 产出。`DATA_LEN`/`CONSTS_LEN` 还顺手定义了「流式输入几个、主机参数几个」。

**(d) ops.vh 被谁 include** —— 这是本讲实践任务要回答的关键问题：[dsp/digaree/sf_user.v:52-58](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L52-L58) 在 `always` 块的 `case(pc)` 里 `` `include "ops.vh" ``，于是 `cgen_srf.py`（经 `sched.py`）生成的每一条指令，就成了 `pc` 取某值时 `inst` 寄存器的赋值——也就是 ROM 的一项。

#### 4.1.4 代码实践：亲眼看见「Python 变成 Verilog」

**目标**：走通 `cgen → sched → include` 的前半段，亲眼看见同一个算法在 C 与 Verilog 两种形态下的样子。

**步骤**：

1. 进入目录并生成 `ops.h`（纯 Python，必然成功）：
   ```bash
   make -C dsp/digaree ops.h
   ```
2. 用编辑器或 `less dsp/digaree/ops.h` 打开，对照 [dsp/digaree/cgen_srf.py:53-60](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py#L53-L60) 找到 `a_r =` / `a_i =` 对应的那几行 C 语句，确认每一行 `mul(...)`/`sub(...)` 都带一个 shift 参数。
3. 再生成 Verilog 形式：
   ```bash
   make -C dsp/digaree ops.vh
   ```
4. 打开 `dsp/digaree/ops.vh`，确认它是 `127: inst <= 21'b...;` 之类的 `case` 项；并在文件末尾留意注释里对「31 个寄存器、128 条指令」 envelope 的检查提示（对应 [dsp/digaree/README:89-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L89-L96)）。
5. 回到 [dsp/digaree/sf_user.v:52-58](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L52-L58)，确认这张表是被 `` `include "ops.vh" `` 嵌入 `case(pc)` 的。

**需要观察的现象**：`ops.h` 是扁平的 C 赋值序列（顺序由你书写顺序决定），而 `ops.vh` 是带 PC 地址的 Verilog `case` 项（顺序由 `sched.py` 重排过）。

**预期结果**：`ops.h` 与 `ops.vh` 都能无错生成；`ops.vh` 的每一项都是 21 位二进制字面量 `21'b...`，对应 `sf_main` 的指令字格式（见 4.3.3）。

**待本地验证**：若你的 `OPS_STYLE` 被改过（例如切到 `_ip3`），生成的 `ops.h` 行数与变量名会不同，可自行对比。

#### 4.1.5 小练习与答案

**练习 1**：`cgen_srf.py` 里 `given` 声明的变量分两组（`k/r/v` 与 `beta/invT/...`），它们在硬件上分别从哪里进入？
> **答案**：前 6 个（`k_r/k_i/r_r/r_i/v_r/v_i`，对应 `DATA_LEN=6`）是**流式测量值**，在计算开始的头几拍由 `meas` 端口鱼贯而入；后 8 个（`consts_len=8`，见 `rules.mk:9`）是**主机可设参数**，在紧接着的几拍由参数存储（DPRAM 或寄存器组）经 `h_data` 注入（见 4.4.3 的 `meas_mux`）。

**练习 2**：为什么 `cgen_srf.py` 末尾的 `cpx_copy("v4","v3")...` 顺序不能由 `sched.py` 自由重排？
> **答案**：因为这 4 行是「把历史电压整体后移一拍」的**状态更新**，存在读后写的数据依赖（`v3` 既是 `v4` 的新值来源、又要在下一行被读给 `v2`），自由重排会破坏历史窗口。作者因此在注释里专门标注（[cgen_srf.py:74-75](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_srf.py#L74-L75)）。

### 4.2 定点数学与 1/x 求逆

#### 4.2.1 概念说明

digaree 的「单数据类型」是一套**带定标的定点数**。关键参数（见 [dsp/digaree/sf_main.v:169-173](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L169-L173)）：

- `pw = 18`：对外端口位宽（与 FPGA 硬件乘法器 18×18 匹配）。
- `extra = 4`：内部多 4 位保护位，故内部数据位宽 `dw = pw + extra = 22`。
- `mw = 18`：乘法器输入位宽。

定点数没有「除法」指令。 digaree 需要除法的地方（如求 `1/V`），用**两步近似**：先查表给一个粗略的 `1/a`，再用 **Newton 迭代**精化。这套机制由 `sf_inv`（硬件）和 `cgen_lib.full_inv`/`inv_iter`（软件展开）配合完成。

`pfloat.py` 则负责「定标」：把 SI 单位（伏特、瓦特、1/秒）的物理量，换算成 18 位寄存器能装下的整数，并写出一套初值文件，供仿真与上板使用。

#### 4.2.2 核心流程：查表 + Newton 迭代求 1/a

Newton 法求倒数是经典数值方法。设要求 \(y = 1/a\)，迭代式为：

\[
y_{n+1} = y_n\,(2 - a\,y_n)
\]

每迭代一次，有效位数大致翻倍。 digaree 的实现：

1. **`sf_inv` 查表**：取输入 `a` 绝对值的高 9 个非符号位（`iscale=9`），按「前导 1」用 `casez` 选一个粗略的 `1/a`（形如 `9'b11???????: r1 <= 2;`）。这本质是对 \(\lfloor \log_2 |a| \rfloor\) 分段，给出 \(2^{-\lfloor\log_2|a|\rfloor-1}\) 量级的初值（见 [dsp/digaree/sf_main.v:71-102](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L71-L102)）。
2. **`full_inv` 展开 2 次 `inv_iter`**：在 `cgen_lib.py` 里把 Newton 迭代展开成 `mul/sub` 序列，初值经两次精化（[dsp/digaree/cgen_lib.py:155-159](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py#L155-L159)），其中常数 `"two"`（主机设为 1/16，对应定点下的「2」）就是迭代式里的 `2`。

整套定点定标（频率、电压、功率如何映射到整数）在 `pfloat.py` 里显式定义：用满量程 `fs = 2^{17}`（对应 18 位有符号寄存器），频率量子 `fq`，得到「满量程频率」`ffs = fq·fs·2π`（[dsp/digaree/pfloat.py:40-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/pfloat.py#L40-L48)）。

#### 4.2.3 源码精读

**(a) 硬件查表求倒数** —— [dsp/digaree/sf_main.v:60-106](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L60-L106)：`sf_inv` 模块。`abs_a` 取绝对值高位；`casez` 用通配 `?` 按「最高有效 1 落在第几位」选初值 `r1`（数越小、`1/a` 越大，所以 `r1` 从 2 一路递增到 1023）；最后用 `sign_a` 恢复符号。这是用 LUT 换乘法器的典型手法。

**(b) 软件侧的 Newton 展开** —— [dsp/digaree/cgen_lib.py:143-159](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py#L143-L159)：`inv_iter` 实现单步 \(y \leftarrow y(2 - ay)\)（用 `mul`/`sub`，shift=3 做定标），`full_inv` 调一次 `inv` 取初值、再调两次 `inv_iter` 精化。[dsp/digaree/cgen_lib.py:162-171](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py#L162-L171) 的 `cpx_inv_conj` 把 `full_inv` 包成「求 \(1/|a|^2\) 再乘 `a`」的复数倒数，正是 `cgen_srf.py:55` 求 `1/V` 所用的原语。

**(c) 定标与初值生成** —— [dsp/digaree/pfloat.py:40-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/pfloat.py#L40-L51)：用 SI 单位算出腔体物理量（`omega0`、`Q1`、`beta` 等），再除以满量程得到定点小数。注意第 51 行 `ai = a/ffs*fs*16`——把期望结果 `a` 换算成 22 位内部表示（`*16` 即 `<<4`，对应 `extra=4` 的保护位），这正是后续比对的「黄金答案」。[dsp/digaree/pfloat.py:131-136](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/pfloat.py#L131-L136) 的 `xprint` 还做溢出自检：若定标后整数超出 `±fs`，直接 `exit(1)`，避免把一个「装不下」的常数静默塞进硬件。

**(d) 直接激励 `sf_alu` 的求逆测试** —— [dsp/digaree/inverse_tb.v:19-37](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/inverse_tb.v#L19-L37)：把 `op` 固定为 `5`（即 `inv`），让 `a` 以 `a += 7*a/300` 的指数式扫描，再用一个 3 拍移位寄存器 `a_sr` 把输入延迟对齐到结果输出（因为 ALU 有流水延迟），逐拍打印 `a_d, r`，供 `inverse_check` 画 `1/x` 曲线比对。

#### 4.2.4 代码实践：观察查表求逆的精度

**目标**：用现成的测试激励，看 `sf_inv` + Newton 迭代后的 `1/x` 与理想值差多少。

**步骤**：

1. 编译并跑求逆测试台（产出成对的 `输入, 输出`）：
   ```bash
   make -C dsp/digaree inverse_tb
   cd dsp/digaree && ./inverse_tb | head -20
   ```
2. 观察输出两列整数 `a_d r`，心算 `a_d * r` 是否近似常数（理想 `1/a × a = 常数`）。
3. 若环境有图形界面，可 `make -C dsp/digaree inverse_check` 让 `invcheck.py` 画图；**注意**：`invcheck.py` 末尾会调用 `pyplot.show()` 弹窗，在无显示的终端/CI 里会**阻塞挂起**，此时改用上面的命令行方式即可。

**需要观察的现象**：`a_d` 越小，`r` 越大（倒数关系）；由于查表只取高 9 位，精度有限，`cgen_lib.full_inv` 的注释自评「accuracy is mediocre at the moment (0.4%)」（[cgen_lib.py:153-154](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/cgen_lib.py#L153-L154)）。

**预期结果**：`a_d * r` 在一个大致恒定的值附近抖动，抖动幅度反映查表 + 2 次迭代的残差。

**待本地验证**：精确的误差百分比取决于你的 `dw`/`mw` 配置，建议以本地 `inverse_check` 的图为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sf_inv` 用 `casez` + 通配 `?`，而不是一个 `for` 循环生成查表？
> **答案**：分段值（2、3、4、6、8、12…）是按 \(2^{-\lfloor\log_2 x\rfloor-1}\) 手工/脚本预算的非均匀表，不是线性映射，`for` 循环不直接适用。源码注释也指出「In theory can be parameterized with a for loop; this version hard-coded for the case iscale == 8」（[sf_main.v:82-84](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L82-L84)）。

**练习 2**：`pfloat.py` 里 `xprint` 为什么要做 `xi >= fs` 的溢出检查？
> **答案**：18 位有符号寄存器只能表示 `[-fs, fs)`（`fs=2^{17}`）。若某个物理常数定标后超出此范围，硬件根本装不下，会静默截断成错误值；`exit(1)` 让这种「定标错误」在生成阶段就暴露，而不是在仿真里 debug（[pfloat.py:132-135](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/pfloat.py#L132-L135)）。

### 4.3 sf_main：执行核（ALU + 寄存器堆 + 5 级流水）

#### 4.3.1 概念说明

`sf_main.v` 是 digaree 的「CPU 核」，但它比任何真实 CPU 都简单：

- **没有取指单元**：指令字 `inst` 由外部（`sf_user`）每拍喂入。
- **只有 4 种运算**：`mul`/`add`/`sub`/`inv`（`add`/`sub` 共享数据通路，靠 `sub` 位切换）。
- **固定的 5 级流水**：每条指令都是「读 → 算 → 移位 → 饱和 → 写回」5 拍，没有气泡、没有冒险处理（依赖由 `sched.py` 在软件侧规避）。

文件里实际定义了 4 个模块，自底向上：`sf_mul`（乘）、`sf_add`（加减）、`sf_inv`（查表倒数，见 4.2）、`sf_alu`（把前三者合一 + 移位饱和），最顶层 `sf_main` 再加上寄存器堆与指令解码。

#### 4.3.2 核心流程：21 位指令字与 5 级流水

指令字 `inst` 共 21 位，字段切分见 [dsp/digaree/sf_main.v:214-219](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L214-L219)：

| 字段 | 位段 | 含义 |
|---|---|---|
| `ra_a` | `[4:0]` | 源 A 寄存器号（0–31） |
| `ra_b` | `[9:5]` | 源 B 寄存器号 |
| `wa` | `[14:10]` | 目的寄存器号 |
| `op` | `[17:15]` | 运算码（4=mul, 5=inv, 6=add, 7=sub, 1/2=输出） |
| `sv` | `[19:18]` | 移位量（0–3） |
| `set` | `[20]` | 为 1 时把外部 `meas` 写入 `wa`（注入测量值） |

5 级流水（注释见 [sf_main.v:204-211](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L204-L211)）：

```text
cycle 1: 读寄存器堆 (ra_a, ra_b) → a, b
cycle 2: ALU 运算 (mul/add/inv 并行, 选其一)
cycle 3: 移位器 (sv 选 0/1/2/3 档右移)
cycle 4: 饱和 (溢出钳位到 ±满量程, 置 sat_happened)
cycle 5: 写回寄存器堆 wa
```

新值最早可在第 6 拍被读出，故 `sched.py` 的 `pipe_len = 5`（[sched.py:7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sched.py#L7)）——调度器与硬件严格对齐。

#### 4.3.3 源码精读

**(a) 乘法器 `sf_mul`** —— [dsp/digaree/sf_main.v:12-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L12-L32)：先把 22 位输入截到乘法器宽度 `mw=18`（`a_trunc = a[dw-1:dw-mw]`，丢掉低位保护位），做有符号乘得 36 位 `r1`，再取其高 `dw+4` 位作为结果——这等价于「乘完右移」，是定点乘法就地定标的标准写法。

**(b) ALU 合一 + 移位饱和 `sf_alu`** —— [dsp/digaree/sf_main.v:110-166](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L110-L166)：三个运算单元 `mul`/`add`/`inv` **并行实例化**，靠 `vo_mul`/`vo_add`/`vo_inv` 三个有效位选通（`mux = vo_mul ? r_mul : vo_add ? r_add : r_inv`）；`sv` 用 `case` 选 4 档移位；最后用宏 `SAT`/`UNSAT`（[sf_main.v:154-157](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L154-L157)）做饱和钳位并输出 `sat_happened`（饱和事件，供上层计数）。

**(c) 双端口寄存器堆** —— [dsp/digaree/sf_main.v:224-238](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L224-L238)：用两份 32 项 RAM（`rf_a`/`rf_b`）存同一份数据，实现「1 写口、2 读口」。注意 `(* ram_style = "distributed" *)` 属性，作者明确要求综合成分布式 RAM（LUT），代价小、时序好；若工程变大、LUT 不够，可改 `block`（见 [README:90-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L90-L96)）。`set` 位为 1 时，`d_in` 取外部 `meas`（左移 `extra` 位对齐内部 22 位），否则取 ALU 输出。

**(d) 结果输出** —— [dsp/digaree/sf_main.v:244-268](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L244-L268)：`op==1` 时把两个源寄存器值锁存到 `a_o/b_o` 并拉高 `ab_update`；`op==2` 同理锁存 `c_o/d_o`。取值时 `a[dw-1:extra]`——把内部 22 位砍回 18 位对外。对应 `cgen_srf.py` 里 `set_result("ab", ...)` / `set_result("cd", ...)`。

#### 4.3.4 代码实践：手搓一段指令序列跑起来

**目标**：用 `main_tb` 里**预置的手写指令序列**（不依赖 `ops.vh`），直观看到「指令字 → 寄存器写回」的过程。

**步骤**：

1. 编译并运行（`main_tb` 是「Non-checking」测试台，不校验数值，只演示数据流，**总是打印 PASS**）：
   ```bash
   make -C dsp/digaree main_tb
   cd dsp/digaree && ./main_tb | head -30
   ```
2. 对照 [dsp/digaree/main_tb.v:28-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/main_tb.v#L28-L52)：前 5 条指令（`21'b1_00_000_00001_00000_00000` 等）`set=1`，把 `meas` 注入寄存器 1–4；之后 `op=4`(mul)/`op=6`(add)/`op=5`(inv) 等。把每条二进制按 4.3.2 的字段表手动拆开，验证你拆出的 `ra_a/ra_b/wa/op/sv/set` 与注释一致。
3. 观察仿真打印的 `cc: r[..] <= ...`，确认写回发生在指令发出后约 5 拍（流水延迟）。

**需要观察的现象**：每条运算指令发出后，`r[wa]` 的更新出现在约 5 个时钟后；`set=1` 的注入指令立即把 `meas` 写入。

**预期结果**：测试台末尾打印 `PASS`（因为它只演示、不校验）。

**说明**：`main_tb.v` 用 `` `ifdef FOO `` 切换两套指令序列——`FOO` 段是手写的、固定的小程序；另一段才会 `` `include "ops.vh" ``（见 [main_tb.v:27-55](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/main_tb.v#L27-L55)）。默认走 `FOO`，正好让初学者摆脱调度器的黑盒。

#### 4.3.5 小练习与答案

**练习 1**：指令 `21'b0_00_100_00000_00011_00010`（[main_tb.v:39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/main_tb.v#L39)）做了什么？
> **答案**：按字段表拆：`set=0, sv=00, op=100(=4 → mul), wa=00000, ra_b=00011, ra_a=00010`。即「读 r[2] 与 r[3]，相乘，移位 0 档，写回 r[0]」。

**练习 2**：为什么 `sf_alu` 把 `mul`/`add`/`inv` 三个单元**同时**实例化、而不是按 `op` 动态选一个？
> **答案**：硬件里「始终算三个、用 mux 选一个」省去了「选完再算」的串行延迟，时序更好；而代价只是多两个小单元（`add` 几乎免费，`inv` 是纯查表 LUT）。只有乘法器是真正占资源的，而它本就只有 1 个（[README:5-10](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L5-L10) 说「一个乘法器」）。

### 4.4 sf_user 与 sf_user_wrap：程序 ROM、PC 与主机参数接入

#### 4.4.1 概念说明

`sf_main` 只是「裸算核」，不知道「按什么顺序执行指令」「数据从哪来」。`sf_user.v` 在它外面套上**程序控制层**：

- **程序计数器（PC）**：7 位，`trigger` 触发后从 0 跑到 127，停在全 1。
- **指令 ROM**：`case(pc)` 里 `` `include "ops.vh" ``，每个 PC 对应一条 21 位指令。
- **数据/参数注入**：前 `data_len` 拍灌测量值，紧接着 `consts_len` 拍从参数存储读主机设定值。
- **饱和事件计数**：统计运行期间发生了多少次溢出饱和。
- **`(* external *)` 端口**：`h_addr`/`h_data` 标了 `external` magic 注释——这正是 `u2-l3` 讲的 `newad.py` 寄存器映射接口，digaree 借此接入 Bedrock 的 localbus/地址空间。

`sf_user_wrap.v` 再提供两种「参数怎么喂」的包装，方便不同上层调用。

#### 4.4.2 核心流程：PC 驱动的取指与参数多路选择

```text
trigger ──▶ pc=0 ──▶ pc 每拍 +1 ──▶ pc=127 停 (step = ~(&pc))
            │
            ├─ pc < data_len        : meas_mux ← meas    (流式测量值)
            ├─ data_len ≤ pc < data_len+consts_len : meas_mux ← h_data (主机参数)
            └─ pc 全程 : inst ← ROM[pc]  (ops.vh 里对应项)
 h_addr = pc + 1 - data_len   ← 在参数窗口内给出参数 ROM 地址
```

参数窗口的设计很巧妙：`h_addr = pc + 1 - data_len`（[sf_user.v:44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L44)），让外部参数存储提前一拍准备好数据，`meas_mux` 再在当拍选入——形成一个「PC 自带的地址发生器」。

#### 4.4.3 源码精读

**(a) PC 与运行控制** —— [dsp/digaree/sf_user.v:32-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L32-L44)：`pc` 复位到 127（停机态），`trigger` 把它清 0 启动；`step = ~(&pc)` 保证跑到 127 后停；`run/run1` 标记运行中，`trace_strobe = run1` 给上层做波形采样。

**(b) 参数多路选择** —— [dsp/digaree/sf_user.v:46-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L46-L50)：`choose_const` 判定当前 PC 是否落在参数窗口内，`meas_mux` 据此选 `h_data`（参数）或 `meas`（测量），再喂给 `sf_main` 的 `meas`。`(* rom_style = "distributed" *)` 把指令 ROM 也指定为分布式 RAM。

**(c) external 端口与饱和计数** —— [dsp/digaree/sf_user.v:13-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L13-L17)：`h_addr`/`h_data` 上的 `(* external *)` 注释承接 `newad.py`（见 `u2-l3`），让 digaree 的参数存储能被 localbus 自动分配地址、被主机读写。[sf_user.v:73-80](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L73-L80) 的 `sat_cnt` 在每个 `trigger` 把累计饱和数锁存到 `sat_report`，供主机事后查询「这次运行有没有溢出」。

**(d) 两种参数包装** —— [dsp/digaree/sf_user_wrap.v:40-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user_wrap.v#L40-L70) 的 `sf_user_pmem` 用一颗双口 RAM（`sf_dpram`）：host 在 `h_clk` 写，`sf_user` 在 `sf_clk` 读——天然跨时钟域。[sf_user_wrap.v:73-113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user_wrap.v#L73-L113) 的 `sf_user_preg` 则把参数「摊平」成一组寄存器（`param_in`），靠 `rd_addr` 选通读出（注释明确「This is a multiplexer」），适合参数较少、要省一颗 RAM 的场景。文件开头 [sf_user_wrap.v:7-14](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user_wrap.v#L7-L14) 警告：参数在运行中被改写会导致「采样到不一致的参数集」，需要调用方保证原子性——这与 `u4` 时钟域跨越的担忧一脉相承。

**(e) 测试台切换两种包装** —— [dsp/digaree/user_tb.v:5-6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/user_tb.v#L5-L6) 用参数 `PMEM=1/0` 在 `sf_user_pmem`/`sf_user_preg` 间切换，对应 Makefile 里 [user_mem_tb/user_reg_tb](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile#L53-L59) 两条规则（用 `-Puser_tb.PMEM=...` 传参）。测试台 [user_tb.v:54-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/user_tb.v#L54-L70) 读 `init2.dat`，按行首字母分发：`p`=持久态、`s`=流式测量、`h`=主机参数。

#### 4.4.4 代码实践：对比两种参数包装

**目标**：用同一份初值数据，分别驱动 DPRAM 版与寄存器组版，确认两者行为一致。

**步骤**：

1. 先生成初值数据（由 `initgen_srf.py` + `init_xindex.py` 产出符号名→数字的 `init2.dat`）：
   ```bash
   make -C dsp/digaree init.dat init2.dat
   ```
2. 分别编译两种包装的测试台：
   ```bash
   make -C dsp/digaree user_mem_tb user_reg_tb
   ```
3. 各跑一次，观察它们打印的 `r[..] <= ...` 序列：
   ```bash
   cd dsp/digaree && ./user_mem_tb | head -15
   ./user_reg_tb | head -15
   ```

**需要观察的现象**：两条打印序列应当**逐行相同**——因为两种包装只是「参数怎么喂」不同，喂进去的数据和执行的程序一样，结果自然一致。

**预期结果**：两组 `r[wa] <= ...` 完全一致；`PMEM` 切换只改变参数存储实现（DPRAM vs 寄存器组 + mux）。

**待本地验证**：若 `init2.dat` 缺失或行格式不符（非 `p/s/h` 开头），`user_tb` 会打印 `input error`，请检查 `init.dat` 是否成功生成。

#### 4.4.5 小练习与答案

**练习 1**：`sf_user_pmem` 与 `sf_user_preg` 各自适合什么场景？
> **答案**：`pmem` 用双口 RAM，适合参数较多、且 host 与引擎**时钟域不同**的场景（双口 RAM 天然跨域）；`preg` 用并行寄存器组 + mux，适合参数少、追求省资源、且参数可视为准静态（作者在 [sf_user_wrap.v:86-88](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user_wrap.v#L86-L88) 假设参数准静态、直接在 `sf_clk` 域重定时）。

**练习 2**：为什么 `sf_user` 把指令 ROM 设成只读（`rom_style=distributed`，且作者说「不打算运行时改程序」）？
> **答案**：digaree 的算法在「编译期」就由 `cgen`+`sched` 完全确定，运行时无需自修改；只读 ROM 可用分布式 RAM 高效实现、时序好、面积小（[sf_user.v:52-54](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L52-L54)）。要换算法，改 `cgen_srf.py` 重新 `make` 即可。

## 5. 综合实践：把「改算法 → 重生成 → C 与 Verilog 交叉验证」走一遍

这是本讲的主线任务，对应大纲指定的实践。**目标**：亲手验证 digaree 的「Python 描述 → 硬件 → 双仿真比对」闭环，并回答两个问题——(1) 这个工程用到哪些 Python 生成脚本？(2) `cgen_srf.py` 生成的 Verilog 片段最终被哪个 `.v` 文件 include/实例化？

**步骤**：

1. **理清生成脚本家谱**。阅读 [dsp/digaree/README](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README) 与 [rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/rules.mk)，列出本工程的 Python 脚本并分类：
   - **算法生成器**：`cgen_srf.py`（SRF 腔体）/ `cgen_ip3.py`（RF 失真）/ `cgen_tst.py`（测试），共享原语库 `cgen_lib.py`。
   - **调度器**：`sched.py`（把 `ops.h` 排成 `ops.vh`）。
   - **初值/定标**：`initgen_srf.py`、`initgen_ip3.py`、`initgen_tst.py`、`pfloat.py`、`init_xindex.py`、`paramh.py`。
   - **校验/分析**：`invcheck.py`、`inver1.py`、`sqrt1.py`、`accuracy.py`、`fitter_test.py`、`find_decay_slope.py`、`detune_coeff_calc.py`、`proc_usertrace.py`。
2. **生成产物并追踪归属**。运行：
   ```bash
   make -C dsp/digaree ops.h ops.vh
   ```
   然后回答关键问题：`cgen_srf.py` 产出 `ops.h`，经 `sched.py` 调度成 `ops.vh`，而 `ops.vh` 被 [dsp/digaree/sf_user.v:52-58](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_user.v#L52-L58) 在 `case(pc)` 中 `` `include "ops.vh" `` —— 所以最终是 **`sf_user.v`** 实例化（include）了 `cgen_srf.py` 间接生成的 Verilog 片段；`sf_user` 再把 `inst` 喂给 [dsp/digaree/sf_main.v:240-242](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sf_main.v#L240-L242) 实例化的 `sf_alu` 去执行。
3. **跑双仿真并比对**。这是 digaree 自测的灵魂——C 与 Verilog 必须逐操作一致：
   ```bash
   make -C dsp/digaree sim1            # 编译 C 位精确仿真器(需 gcc)
   make -C dsp/digaree main_tb user_mem_tb
   ```
   `sim1` 消费 `ops.h`（C 形式），Verilog 侧消费 `ops.vh`（同一算法的另一形态）。若想看完整 `all` 目标：
   ```bash
   make -C dsp/digaree                 # 等价于 all 目标
   ```
4. **读懂 `all` 聚合了哪些自检**（见 [Makefile:40-41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile#L40-L41)）：`inverse_check`/`ilookup_check`（求逆精度）、`match_mem`/`match_reg`（C↔Verilog 逐行 `cmp`，[Makefile:151-153](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile#L151-L153)）、`accuracy`（精度统计）、`detune_cw`/`detune_pulse_test`/`decay_slope_test`（用真实腔体数据拟合 detune 系数，比对 `.gold` 黄金文件）、`sqrt_check1/2`。

**需要观察的现象 / 预期结果**：

- `ops.h` 与 `ops.vh` 无错生成；`ops.vh` 是一串 `NN: inst <= 21'b...;` 的 `case` 项。
- `sim1` 与 `user_*_tb` 都能跑完并打印结果。
- **重要提醒**：`inverse_check`、`accuracy`、`plot` 等目标会调用 `matplotlib` 的 `pyplot.show()` 弹窗（见 [pfloat.py:154](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/pfloat.py#L154) 末尾），**在无图形界面的终端/CI 里会阻塞挂起**。建议在无显示环境只跑到第 3 步的子集，或给 matplotlib 配 `Agg` 后端。
- `match_*` 目标在 Makefile 里被写成「无依赖空目标」（[Makefile:162-163](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/Makefile#L162-L163)），实际是否触发 `cmp` 受 GNU Make 显式规则优先级影响——**待本地验证**：建议手动 `make sim1.results` 与 `make user_mem.results` 后自行 `cmp` 这两个文件，确认 C 与 Verilog 输出逐行一致。
- 若本机缺 `../run2.dat`（真实腔体波形，见 [README.run2](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README.run2)，**不属于源码**），`test.dat`/`plot` 会失败——但这不影响 `all` 主目标，因为 `all` 不依赖它们。

**交付物**：一张「Python 脚本 → 产物 → 被谁消费」的对照表，以及对「`cgen_srf.py` 的产物最终被 `sf_user.v` include、被 `sf_main.v` 执行」的一句话结论。

## 6. 本讲小结

- **digaree 是「单数据类型、无分支、定周期」的定点协处理器**，定位在 FPGA 逻辑与通用 CPU 之间，专门跑 RF 失真校正 / SRF 腔体 detune 这类「几十次乘加」的算式。
- **核心是代码生成链**：`cgen_srf.py`（描述算式）→ `ops.h`（C 汇编）→ `sched.py`（寄存器分配 + 列表调度）→ `ops.vh`（Verilog ROM）→ 被 `sf_user.v` `` `include `` 进 `case(pc)`。
- **定点数学靠查表 + Newton 迭代**：`sf_inv` 用 `casez` 按 \(\lfloor\log_2\rfloor\) 分段给初值，`full_inv` 展开 2 次 `inv_iter` 精化；`pfloat.py` 负责 SI 浮点 → 18 位定点的定标与溢出自检。
- **执行核 `sf_main` 是 5 级流水**：21 位指令字解码出 `ra_a/ra_b/wa/op/sv/set`，双端口寄存器堆（分布式 RAM）+ 并行的 `mul/add/inv` 三单元 + 移位饱和；`sched.py` 的 `pipe_len=5` 与硬件严格对齐。
- **`sf_user`/`sf_user_wrap` 是程序控制与外设层**：PC（0→127）、指令 ROM、`meas_mux` 参数注入、饱和计数、`(* external *)` 端口（接入 `newad.py`/localbus）；两种参数包装 `sf_user_pmem`（双口 RAM 跨域）/`sf_user_preg`（寄存器组 + mux）。
- **自测范式是「C 与 Verilog 双仿真逐行比对」**：`sim1.c` 消费 `ops.h` 做位精确仿真，`match_*` 用 `cmp` 校验两者一致；真实数据由 `detune_*_test`/`decay_slope_test` 比对 `.gold` 黄金文件。 digaree 与 Bedrock 其它子系统**解耦**，自带独立 Makefile，是「复杂工程如何自测」的范本。

## 7. 下一步学习建议

- **想看 digaree 怎么被用于真实 RF 控制？** 结合 `u6-l3`（cmoc LLRF 控制器）阅读，digaree 正是为这类「腔体 detune/淬灭」在线计算而生的算力补充。
- **想理解指令调度的来源？** README 点名借鉴了 Milkymist PFPU 的 gfpus 调度器；可对照 [sched.py:37-64](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/sched.py#L37-L64) 的 `afunc` 类，体会「列表调度（list scheduling）」如何用 `becomes_valid`/`last_used` 两张表做软件流水。
- **想深入定点与代码生成的数学？** 阅读 `tuning_dsp4.tex`（需 `make tuning_dsp4.pdf`）——它是 SRF detune 算法的完整推导；再看 `cgen_lib.py` 里 `cpx_triad`/`cpx_sqr` 等更高阶原语如何由 `mul/add` 组合而成。
- **想扩展一个自己的算法？** 按 README 第 [74-80](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/digaree/README#L74-L80) 行的指引：仿照 `cgen_srf.py` 写一个 `cgen_foo.py`，配一个 `initgen_foo.py` 提供初值与黄金答案，`make ops.vh` 后确认指令数 ≤ 128、寄存器数 ≤ 31，然后用本讲的「双仿真比对」自检。
- **承接 Bedrock 主线：** digaree 的 `(* external *)` 端口直接对接 `u2-l3`（newad.py）与 `u2-l2`（localbus），是「自定义算力如何挂上 Bedrock 片上总线」的实例，值得回头对照阅读。
