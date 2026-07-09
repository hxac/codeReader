# 仓库目录结构导览

## 1. 本讲目标

本讲承接 [u1-l1 项目总览](u1-l1-project-overview.md) 与 [u1-l2 构建系统与运行测试](u1-l2-build-and-run.md)。前两讲我们已经知道"Koala-GPGPU 是什么"和"`make test_integer` 怎么把它跑起来"，但还没有把整个仓库的文件分布看清。本讲只解决一件事：**给整个仓库画一张清晰的代码地图，让你今后打开任何一个 `.sv` 或 `.py` 文件，都能立刻知道它属于哪一层、负责什么**。

读完本讲你应该能够：

- 说出仓库的四个核心目录 `rtl/common`、`rtl/sm_core`、`test`、`project/altera` 各自的角色与边界。
- 把仓库里任意一个 `.sv` 文件归到正确的层（公共积木 / 顶层 / SM 流水线 / 综合工程）。
- 区分"目录里有哪些文件"和"仿真时实际编译了哪些文件"——这两者并不完全相等，这是阅读本仓库最容易踩的坑之一。
- 看懂 `project/altera/` 里的 Quartus 工程文件（`.qpf` / `.qsf`）如何把同一份 RTL 指向一块真实的 FPGA 芯片。

> 本讲是"地图课"，几乎不展开任何模块的内部逻辑（那是第 2 单元以后的事）。它的价值在于：先有地图，后面读源码才不会迷路。

## 2. 前置知识

本讲会用到下面几个概念，先用大白话过一遍：

- **SystemVerilog 源文件（`.sv`）**：描述数字电路的源码。本项目所有 RTL 都用 `.sv`，因此编译时必须开 `-g2012`（上一讲已讲）。
- **宏定义头文件（`define.sv`）**：一种特殊的 `.sv`，里面全是 `` `define `` 宏（比如"warp 数量 = 8"），本身**不是一个电路模块**，而是被别的文件用 `` `include `` 引进来"共享参数"。可以类比成 C 语言的 `.h`。
- **`` `include `` 指令**：在 SystemVerilog 里把另一个文件的内容"粘贴"到当前位置。本项目中几乎每个 `.sv` 的第 2 行都有一句 `` `include "define.sv" ``（路径写法因所在目录而异）。
- **Cocotb 测试台（`.py`）**：用 Python 写的数字电路测试程序。上一讲已介绍它通过 VPI 桥接进仿真器。
- **FPGA 与综合（synthesis）**：把 `.sv` 描述的电路"翻译"成真实芯片（FPGA）上的逻辑门和连线的过程。本项目用 Intel **Quartus Prime** 做这件事，目标芯片是 Cyclone V。
- **Quartus 工程文件**：`.qpf`（Quartus Project File，工程元信息）和 `.qsf`（Quartus Settings File，工程"配置清单"——声明用哪块芯片、顶层是谁、要编译哪些源文件）。
- **顶层模块（top-level entity）**：整个电路的最外层。本项目固定是 `gpgpu_top`，仿真和综合都用它。

> 一个贯穿全讲的提醒：本仓库有**两条**把 RTL "喂给工具"的路径——一条是仿真（`Makefile` 体系，上一讲主角），一条是 FPGA 综合（`project/altera/`）。它们引用的 `.sv` 文件清单**不完全相同**，本讲会反复对比这两条路径。

## 3. 本讲源码地图

整个仓库共 **33 个被 git 跟踪的文件**，分布在根目录与四个子区域。先用一张总表建立全景（根目录的构建文件已在 u1-l2 详讲，这里只标注职责）：

```
Koala-GPGPU/
├── Makefile                 # 顶层构建入口（拼装 build/ 并递归 make）—— u1-l2
├── Main.Makefile            # 仿真编译入口（被复制成 build/Makefile）—— u1-l2
├── Common.Makefile          # iverilog/vvp/cocotb 工具链规则 —— u1-l2
├── README.md                # 项目说明：架构树、参数、指令集、测试
├── LICENSE                  # 许可证
│
├── rtl/                     # ★ 所有硬件描述（SystemVerilog）都在这里
│   ├── gpgpu_top.sv         # 顶层外壳：把代码存储器/host 接口转接给 sm_core
│   ├── gpgpu.Makefile       # 顶层源文件清单 + include common 目录
│   ├── common/              # → 4.1 公共硬件积木层（7 个 .sv + 1 Makefile）
│   └── sm_core/             # → 4.2 SM 核心流水线层（10 个 .sv + 1 Makefile）
│
├── test/                    # → 4.3 Cocotb 仿真测试台（5 个 .py）
│
└── project/altera/          # → 4.4 FPGA 综合工程（Quartus：.qpf + .qsf）
```

本讲"源码地图"对应的几个关键文件：

| 文件 | 作用 |
|------|------|
| `README.md` | 仓库唯一的"说明书"，包含架构树、关键参数、指令集与测试/综合说明。 |
| `rtl/common/common.Makefile` | 声明仿真时要编译哪些公共积木（只列了 4 个 `.sv`）。 |
| `rtl/sm_core/sm_core.Makefile` | 声明仿真时要编译哪些 SM 流水线文件（列了 10 个 `.sv`）。 |
| `project/altera/koala_gpu.qsf` | Quartus 工程配置清单：芯片型号、顶层、要综合的全部源文件。 |
| `project/altera/koala_gpu.qpf` | Quartus 工程元信息（版本、修订名）。 |

下面四个小节（4.1–4.4）逐层展开这四个目录。

## 4. 核心概念与源码讲解

### 4.1 rtl/common：公共硬件积木层

#### 4.1.1 概念说明

`rtl/common/` 存放的是**与 SM 流水线无关的、通用的底层硬件小工具**——独热码转二进制、仲裁器、同步 FIFO 这类"砖块"。它们不关心 GPU 业务，任何一个数字电路项目都可能用到；正因为通用，所以被单独放在 `common/`，供 `sm_core` 里的多个流水级复用。

这是典型的"**基础设施与业务逻辑分层**"思想：把可复用的底层单元抽出来，避免在每个流水级里重复造轮子。第 2 单元会逐个精读这些积木，本讲只需知道它们的存在与归类。

#### 4.1.2 核心流程

`rtl/common/` 目录下的 8 个条目可以分成三类：

```
rtl/common/
├── common.Makefile        # 声明"仿真编译清单"（只列 4 个 .sv）
├── define.sv              # 【特殊】全局宏定义头，被所有模块 `include
│
├── oh2bin.sv              # 独热码(one-hot)转二进制 —— 通用编码工具
├── rr_arb.sv              # 循环优先级(round-robin)仲裁器
├── fixed_pri_arb_base.sv  # 固定优先级仲裁器（基础版）
├── fixed_pri_arb.sv       # 固定优先级仲裁器（封装版）★当前仿真未编译
├── sync_fifo.sv           # 同步 FIFO（基础版）
└── sync_fifo_count.sv     # 带计数的同步 FIFO  ★当前仿真未编译
```

这里有一个**最重要的认知**：目录里有 **7 个 `.sv`**，但仿真时真正被 `iverilog` 编译的只有 **4 个**（见 4.1.3）。`define.sv` 通过 `` `include `` 被间接拉入，而 `fixed_pri_arb.sv`、`sync_fifo_count.sv` 当前并未进入顶层仿真编译清单——它们要么是预留的备用实现，要么只被 FPGA 综合用到（见 4.4）。这就是上一讲提到的"**清单不完全等于目录**"现象，在 `common/` 这一层体现得最明显。

#### 4.1.3 源码精读

[rtl/common/common.Makefile:1](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/common/common.Makefile#L1) —— 这一行用 `VERILOG_SOURCES +=` 只追加了 **4 个**公共源文件：`fixed_pri_arb_base.sv`、`oh2bin.sv`、`rr_arb.sv`、`sync_fifo.sv`。注意它**没有**列 `fixed_pri_arb.sv`、`sync_fifo_count.sv`、`define.sv`。也就是说，这 4 块"砖"是当前 SM 流水线实际用到的公共积木。

那么 `define.sv` 是怎么进入编译的？答案是 `` `include ``。它本身不被列入 `VERILOG_SOURCES`，而是被别的文件"粘贴"进来：

[rtl/common/define.sv:1-17](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/common/define.sv#L1-L17) —— 全文只有注释和 `` `define `` 宏，没有任何 `module`。它定义了全局参数：代码存储器地址宽度 32、数据宽度 64、`NUM_WARP=8`（并用 `$clog2` 推出 warp 编号位宽 `DEPTH_WARP`）、`NUM_REG=64`（寄存器号位宽 `DEPTH_REG`）、`REG_DATA_WIDTH=32`。这些正是 [u1-l1](u1-l1-project-overview.md) 提到的"8 warp、64 寄存器、64 位指令、32 位地址"等全局参数的**唯一真身来源**。

`define.sv` 的复用方式很统一——几乎每个 `.sv` 第 2 行都 include 它，只是路径随目录深浅而变：

- `rtl/gpgpu_top.sv` 写作 `` `include "common/define.sv" ``
- `rtl/sm_core/*.sv` 写作 `` `include "../common/define.sv" ``
- `rtl/common/*.sv` 写作 `` `include "define.sv" ``

这些相对路径之所以能被 iverilog 解析，是因为 [rtl/gpgpu.Makefile](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/gpgpu.Makefile#L3) 把 `common` 目录登记成了 include 搜索路径（`VERILOG_INCLUDE_DIRS +=`，上一讲已讲，最终变成 `-I build/common`）。

> 一句话总结 `common/` 的"文件分类法"：**`define.sv` 是参数头（被 include，不是模块）；4 个被编译的 `.sv` 是当前在用的积木；剩下 2 个 `.sv` 是当前仿真未启用、但 Quartus 综合里仍会包含的备用积木。**

#### 4.1.4 代码实践

**实践：核对 common 目录里"清单 vs 目录"的差异。**

1. **实践目标**：亲手验证 `rtl/common/` 里实际有 7 个 `.sv`，而仿真清单只列了 4 个，并找出"漏掉"的那 3 个分别是什么角色。
2. **操作步骤**：
   - 列目录：`ls rtl/common/*.sv`（应看到 7 个）。
   - 看清单：打开 `rtl/common/common.Makefile`，数 `+=` 后面的文件名（应只有 4 个）。
   - 做减法：目录里有、清单里没有的 3 个文件是 `define.sv`、`fixed_pri_arb.sv`、`sync_fifo_count.sv`。
   - 验证 `define.sv` 的身份：`grep -c module rtl/common/define.sv`（应为 `0`，证明它不是模块，而是纯宏头）。
3. **需要观察的现象**：`ls` 显示 7 个 `.sv`；`common.Makefile` 只含 4 个文件名；`define.sv` 不含任何 `module` 关键字。
4. **预期结果**：你能说出"7 个 .sv = 4 个在用积木 + 2 个备用积木 + 1 个宏头（define.sv）"。
5. **本地验证状态**：以上命令只用 `ls`/`grep`/`cat`，不依赖任何工具链，可直接验证。

#### 4.1.5 小练习与答案

**Q1**：为什么 `define.sv` 不出现在 `common.Makefile` 的 `VERILOG_SOURCES` 里，却仍能影响编译结果？
**答**：因为它不是独立编译的模块，而是一个宏定义头，被别的 `.sv` 通过 `` `include `` 粘贴进去。iverilog 在单次编译里，`define 宏会跨文件生效，所以只要任意一个被编译的文件 include 了它，宏就对整个编译单元可见。

**Q2**：如果你新增了一个公共积木 `rtl/common/barrel_shifter.sv`，要让它在仿真里生效，最少要改哪里？
**答**：在 `rtl/common/common.Makefile` 的 `VERILOG_SOURCES +=` 里追加它。拷贝由顶层 `Makefile` 的 `cp -r rtl/common build/common` 自动完成（上一讲），无需改别处。注意如果它需要 `define.sv` 的宏，记得在第 2 行加 `` `include "define.sv" ``。

---

### 4.2 rtl/sm_core：SM 核心流水线层

#### 4.2.1 概念说明

`rtl/sm_core/` 是整个项目的**心脏**。这里存放的是 SM（streaming multiprocessor，流式多处理器）核心流水线的全部 10 个 `.sv` 文件：1 个父模块 `sm_core.sv` 把另外 9 个子流水级例化并串成数据通路。这一层是"业务逻辑层"——每个文件都对应流水线的一个真实阶段，与 [u1-l1](u1-l1-project-overview.md) 给出的架构树一一对应。

与 `common/` 不同，`sm_core/` 的目录与清单**完全一致**：目录里有几个 `.sv`，仿真和综合就编译几个，没有"漏网之鱼"。

#### 4.2.2 核心流程

`sm_core/` 共 11 个条目（1 个 Makefile + 10 个 `.sv`）。10 个 `.sv` 按"父 + 9 子"组织，9 个子模块的命名顺序恰好就是数据流经流水线的顺序：

```
rtl/sm_core/
├── sm_core.Makefile         # 声明 10 个 .sv 的仿真编译清单
│
├── sm_core.sv               # 【父模块】例化下面 9 个子模块，连成流水线
│
│   # —— 数据流方向：从上到下 ——
├── sm_warp_scheduler.sv     # ① 轮询分配 warp 槽位
├── sm_fetch.sv              # ② 跟踪 PC，请求取指
├── sm_decode.sv             # ③ 抽取 64 位指令字段
├── sm_inst_buffer.sv        # ④ 缓存译码后的指令
├── sm_score_board.sv        # ⑤ 检测 RAW 冒险
├── sm_operand_collect.sv    # ⑥ 读寄存器、选操作数（含寄存器堆）
├── sm_issue.sv              # ⑦ 派发到执行单元
├── sm_int_alu.sv            # ⑧ 整数运算执行
└── sm_writeback.sv          # ⑨ 结果写回寄存器堆
```

这 9 个子模块的名字与 [README.md:12-20](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/README.md#L12-L20) 的架构树**逐行对应**。本讲只需记住"目录文件 = README 架构树"这个映射；至于每个阶段内部如何握手、如何处理冒险，是第 4–6 单元的事。

> 注意区分两个数字：README 说"9 个子模块"指的是 9 个流水级；而 `sm_core/` 目录里有 **10 个 `.sv`**，多出来的那 1 个是父模块 `sm_core.sv`。所以仿真编译清单是 10 个文件，不是 9 个。

#### 4.2.3 源码精读

[rtl/sm_core/sm_core.Makefile:1-5](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/rtl/sm_core/sm_core.Makefile#L1-L5) —— 这 5 行把 10 个 `.sv` 全部追加进 `VERILOG_SOURCES`：第 1 行是 `sm_core.sv`（父）+ `sm_decode` + `sm_fetch`，后续行依次补齐 `sm_inst_buffer`、`sm_warp_scheduler`、`sm_score_board`、`sm_operand_collect`、`sm_issue`、`sm_int_alu`、`sm_writeback`。可以看到**目录里的全部 10 个 `.sv` 都在清单里**，与 `common/` 形成对比。

这 10 个文件全部通过 `` `include "../common/define.sv" `` 拿到全局参数（见 4.1.3），因此它们都用 `NUM_WARP=8`、`NUM_REG=64` 这些宏来定总线宽度——这就是为什么 [u1-l1](u1-l1-project-overview.md) 强调"所有信号都带 wid 区分 8 个 warp 上下文"。

对照 README 的架构树可以确认这一层与文档完全对齐：

[README.md:9-21](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/README.md#L9-L21) —— 这段架构树把 `sm_core` 的 9 个子模块及其一句中文职责列得清清楚楚，是阅读 `sm_core/` 目录时最好的"目录说明书"。

#### 4.2.4 代码实践

**实践：把目录文件名与 README 架构树做一次逐行对账。**

1. **实践目标**：确认 `rtl/sm_core/` 的 10 个 `.sv` 与 README 架构树完全吻合，并找出"README 没单独列出、但目录里存在"的那个文件。
2. **操作步骤**：
   - `ls rtl/sm_core/*.sv` 列出全部 `.sv`。
   - 打开 `README.md` 的 Architecture 小节（约第 9–21 行），对照树里的 9 个子模块名。
   - 做差集：目录里比架构树多出来的那个文件就是父模块 `sm_core.sv`。
3. **需要观察的现象**：`ls` 输出 10 个 `.sv`；其中 9 个名字能在 README 架构树里找到，唯独 `sm_core.sv` 不在树的"叶子"位置（它是整棵树的根 `sm_core` 那一行对应的实现文件）。
4. **预期结果**：你能解释"9 个子流水级 + 1 个父模块 = 10 个 `.sv`"，并理解为什么仿真清单是 10 而不是 9。
5. **本地验证状态**：仅需 `ls` 与阅读 README，不依赖工具链，可直接验证。

#### 4.2.5 小练习与答案

**Q1**：`sm_core/` 目录与仿真清单的关系，和 `common/` 有什么本质不同？
**答**：`sm_core/` 是"目录 = 清单"（10 个 `.sv` 全部编译）；`common/` 是"目录 ⊋ 清单"（7 个 `.sv` 里只编译 4 个，另有 `define.sv` 经 include 进入、2 个备用 `.sv` 当前未编译）。

**Q2**：为什么 `sm_core/` 里的文件 include 的路径是 `../common/define.sv`，而 `common/` 自己的文件写的是 `define.sv`？
**答**：因为 `` `include `` 的路径是相对于"该文件所在目录"解析的。`sm_core/` 在 `common/` 的下一级，所以要 `../common/` 退一层再进入；`common/` 内部的文件与 `define.sv` 同目录，直接写文件名即可。

---

### 4.3 test：Cocotb 仿真测试台层

#### 4.3.1 概念说明

`test/` 里没有任何硬件描述，全部是 **Python** 文件，它们组成 cocotb 测试台。上一讲我们追过 `make test_integer` 是如何通过 `MODULE=test.test_integer` 定位到这里来的；本讲把 `test/` 内部 5 个 `.py` 的分工讲清楚。

可以把这 5 个文件理解成一场"仿真话剧"的四个行当：**一个主角**（入口测试）、**两个"对手演员"**（扮演 GPU 外部世界的驱动器）、**两个"幕后记录员"**（日志与状态转储）。

#### 4.3.2 核心流程

```
test/
├── test_integer.py   # 【主角/入口】被 cocotb 自动发现，编排整场测试
├── memory.py         # 【驱动器】扮演"代码存储器"，请求-响应式喂指令
├── host.py           # 【驱动器】扮演"主机"，启动 kernel、等完成
├── dump.py           # 【记录员】逐周期把流水线内部状态转储出来
└── logger.py         # 【记录员】带时间戳的日志文件输出（写 logs/）
```

它们在一次仿真里的协作关系大致是：

```
test_integer.py (入口)
   │
   ├── 用 host.py    → 通过 host/tpc 接口"启动 kernel"
   ├── 用 memory.py  → 通过代码存储器接口"喂指令"（fetch 来取，它就应答）
   │
   └── 仿真推进每一拍
         ├── dump.py  → 抓取流水线内部信号，逐周期转储
         └── logger.py → 把转储与最终验证结果写进 test/logs/log_*.txt
```

其中 `test_integer.py` 是唯一被 cocotb 直接加载的入口（由 `MODULE=test.test_integer` 指定），其余 4 个 `.py` 都是它 `import` 进来使用的"工具"。这也解释了为什么 README 把它们列在同一张表里、却只有 `test_integer.py` 对应"测试入口"。

#### 4.3.3 源码精读

README 的 Test Infrastructure 小节给出了这 5 个文件的一句式职责，是阅读 `test/` 目录最好的索引：

[README.md:78-86](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/README.md#L78-L86) —— 这张表逐行列出 `test_integer.py`（整数流水线验证）、`memory.py`（仿真代码存储器，请求-响应接口）、`host.py`（主机/GPU 接口驱动，负责 kernel 启动与完成）、`dump.py`（逐周期流水线状态转储）、`logger.py`（带时间戳的日志输出）。

其中 `test_integer.py` 既是入口，也包含 kernel 指令序列和最终的 R1/R2/R3 断言（上一讲 4.3 已精读过它的关键行）。本讲只需记住它的"入口"身份，以及它通过 `dut.U_sm_core.U_sm_operand_collect.reg_file[...]` 这种层级路径探针式地读取 SM 内部状态——这条路径里的 `U_sm_core`、`U_sm_operand_collect` 正是 `sm_core.sv`（4.2）里例化子模块时用的实例名。

另一个值得记住的细节是日志落点：[test/logger.py](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/test/logger.py#L5) 把日志目录设为 `test/logs`，而 vvp 的工作目录是 `build/`，所以仿真日志实际写在 `build/test/logs/log_<时间戳>.txt`（上一讲已述）。

#### 4.3.4 代码实践

**实践：给 5 个测试文件画一张"调用关系图"。**

1. **实践目标**：搞清楚 `test_integer.py` 到底 `import` 了哪几个同目录模块，验证"一主四辅"的结构。
2. **操作步骤**：
   - 在 `test/` 下查找导入关系，例如用搜索工具检索 `test_integer.py` 里的 `import` / `from` 语句，看它是否引入了 `memory`、`host`、`dump`、`logger`。
   - 同样检索 `dump.py`、`host.py` 是否相互引用。
3. **需要观察的现象**：`test_integer.py` 出现对 `memory`、`host` 等模块的引用；`logger.py` 较少被别人依赖、更多是"被调用写日志"。
4. **预期结果**：你能画出一张以 `test_integer.py` 为根、其余 4 个 `.py` 为叶的依赖图，印证 4.3.2 的协作关系。
5. **本地验证状态**：本实践是纯源码阅读型，无需运行仿真，可直接完成；具体 import 语句的行号建议在本地打开文件确认。

#### 4.3.5 小练习与答案

**Q1**：为什么 cocotb 只加载 `test_integer.py`，而不直接加载 `memory.py`？
**答**：因为环境变量 `MODULE=test.test_integer`（上一讲）明确告诉 cocotb 去 `test` 包里找 `test_integer` 这个模块作为测试入口。`memory.py` 等只是被 `test_integer.py` 当工具 `import` 的支撑模块，不是测试入口。

**Q2**：如果想新增一个浮点测试，应该在 `test/` 里加什么文件、又要让构建系统怎么"发现"它？
**答**：新增 `test/test_float.py`，在其中用 `@cocotb.test()` 装饰一个测试函数。构建系统的 `test_%` 模式规则（上一讲）会凭目标名 `test_float` 把 stem 拼成 `MODULE=test.test_float`，从而自动定位到新文件——无需改 Makefile（前提是文件名遵循 `test_<stem>.py` 约定）。

---

### 4.4 project/altera：FPGA 综合工程层

#### 4.4.1 概念说明

前面三个目录（`rtl/common`、`rtl/sm_core`、`test`）都是为**仿真**服务的。`project/altera/` 则是为**上真实芯片**服务的：它存放 Intel Quartus Prime 的工程文件，把同一份 RTL 综合到一块 Cyclone V FPGA 上。

这一层**不含任何电路逻辑**，只有两个工程描述文件，但它们揭示了"仿真路径"与"综合路径"的关键差别：综合时，Quartus 需要一份**显式、完整**的源文件清单（用相对芯片目录的路径），而不能像 iverilog 那样靠 `` `include `` 隐式拉入宏头。

#### 4.4.2 核心流程

```
project/altera/
├── koala_gpu.qpf   # 工程元信息：Quartus 版本、修订名(revision=koala_gpu)
└── koala_gpu.qsf   # 工程配置清单：芯片型号、顶层、全部源文件
```

`.qsf` 里最值得关心的三类信息：

1. **目标芯片**：`FAMILY "Cyclone V"` + `DEVICE 5CGTFD9D5F27C7`（与 README 的 FPGA Target 一致）。
2. **顶层实体**：`TOP_LEVEL_ENTITY gpgpu_top`——和仿真顶层完全相同。
3. **源文件清单**：用 `set_global_assignment -name SYSTEMVERILOG_FILE ...` 逐条列出**全部 18 个 `.sv`**，路径写作 `../../rtl/...`（因为 `.qsf` 在 `project/altera/`，要退两级才能到仓库根的 `rtl/`）。

```
              koala_gpu.qsf (在 project/altera/)
                     │
                     │  ../../rtl/...  退两级目录
                     ▼
rtl/common/*.sv (7) + rtl/gpgpu_top.sv (1) + rtl/sm_core/*.sv (10) = 18 个 .sv
```

#### 4.4.3 源码精读

[project/altera/koala_gpu.qpf:25-30](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/project/altera/koala_gpu.qpf#L25-L30) —— `.qpf` 文件本体很短，只记录 Quartus 版本（`"18.1"`）、创建时间与工程修订名（`PROJECT_REVISION = "koala_gpu"`）。它本身不包含任何电路信息，只是告诉 Quartus"这是一个工程，配套的配置去同名 `.qsf` 里找"。

[project/altera/koala_gpu.qsf:39-41](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/project/altera/koala_gpu.qsf#L39-L41) —— 三条最关键的全局设定：芯片家族 `Cyclone V`、具体型号 `5CGTFD9D5F27C7`、顶层实体 `gpgpu_top`。这条"顶层 = gpgpu_top"与仿真的 `TOPLEVEL = gpgpu_top`（上一讲 [Main.Makefile:3](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/Main.Makefile#L3)）保持一致——**两条路径共用同一个顶层**。

[project/altera/koala_gpu.qsf:57-74](https://github.com/yqian4/Koala-GPGPU/blob/9ee908ccd4bb5dd37837aaaf1f69c17db6a96060/project/altera/koala_gpu.qsf#L57-L74) —— 源文件清单，共 **18 条** `SYSTEMVERILOG_FILE` 赋值：7 个 `common`（`rr_arb`、`fixed_pri_arb_base`、`fixed_pri_arb`、`oh2bin`、`sync_fifo`、`sync_fifo_count`、`define.sv`）+ 1 个 `gpgpu_top.sv` + 10 个 `sm_core`（父 + 9 子）。注意这里把 `define.sv`、`fixed_pri_arb.sv`、`sync_fifo_count.sv` **也显式列进去了**——这正是综合与仿真的清单差异所在。

> **仿真 vs 综合清单对比**（本讲最重要的对照）：
>
> | 文件 | 仿真(Makefile) | 综合(Quartus qsf) |
> |------|:---:|:---:|
> | `gpgpu_top.sv` | ✅ | ✅ |
> | `sm_core/*.sv`（10 个） | ✅ | ✅ |
> | `common/` 在用积木（oh2bin/rr_arb/fixed_pri_arb_base/sync_fifo） | ✅ | ✅ |
> | `common/fixed_pri_arb.sv` | ❌ | ✅ |
> | `common/sync_fifo_count.sv` | ❌ | ✅ |
> | `common/define.sv` | 经 `` `include `` | ✅（显式列出） |
>
> 结论：Quartus 把 `common/` 的全部 7 个 `.sv` 都喂给综合，而仿真只编译其中 4 个 + 经 include 拉入 `define.sv`。所以同一个目录，两条路径"看到"的文件集合并不一样。

#### 4.4.4 代码实践

**实践：对比仿真清单与综合清单，找出"只被综合用到"的源文件。**

1. **实践目标**：亲手确认 4.4.3 那张对照表，理解两条路径的清单差异。
2. **操作步骤**：
   - 打开 `project/altera/koala_gpu.qsf`，统计第 57–74 行 `SYSTEMVERILOG_FILE` 的条目数（应为 18）。
   - 把这些路径里的文件名，与 `rtl/common/common.Makefile`（4 个）+ `rtl/sm_core/sm_core.Makefile`（10 个）+ `gpgpu_top.sv`（1 个）做对比。
   - 找出"qsf 有、Makefile 没有"的文件：应为 `common/define.sv`、`common/fixed_pri_arb.sv`、`common/sync_fifo_count.sv`。
3. **需要观察的现象**：qsf 列了 18 个 `.sv`；仿真清单合计 15 个（1 顶层 + 4 common + 10 sm_core）；差集正好是上述 3 个 common 文件。
4. **预期结果**：你能解释"为什么 `fixed_pri_arb.sv` / `sync_fifo_count.sv` 当前在仿真里看不到、却仍然存在于仓库"——它们（至少）是给 FPGA 综合保留的。
5. **本地验证状态**：纯文件阅读与计数，不依赖 Quartus，可直接验证。是否真的被综合实际使用，**待本地用 Quartus 打开工程确认**。

#### 4.4.5 小练习与答案

**Q1**：为什么 `.qsf` 里的源文件路径都写成 `../../rtl/...`？
**答**：因为 `.qsf` 位于 `project/altera/`，而 `rtl/` 在仓库根。从 `project/altera/` 出发，要先用 `../` 退到 `project/`、再用 `../` 退到仓库根，才能进入 `rtl/`——所以是两级 `../../`。

**Q2**：如果在 `rtl/sm_core/` 下新增了 `sm_fpu.sv` 并希望它能被综合，需要在 `project/altera/koala_gpu.qsf` 里做什么？
**答**：需要新增一行 `set_global_assignment -name SYSTEMVERILOG_FILE ../../rtl/sm_core/sm_fpu.sv`。与仿真路径不同，Quartus 不会自动扫描目录，必须逐条显式声明（同时别忘记在 `sm_core.Makefile` 里也加上，以保证仿真也能编译它）。

---

## 5. 综合实践

把本讲四个目录串起来，亲手**制作一张完整的"仓库代码地图"表格**——这是本讲的总练习，也是你日后阅读源码时的随身参考。

**实践目标**：为仓库里所有 33 个被跟踪文件，建立一张"文件 → 所属层 → 一句话职责 → 进入哪条路径（仿真/综合/都进）"的四列表。

**操作步骤**：

1. 用 `git ls-files`（或 `ls -R`）取得完整文件清单。
2. 按下面的骨架填写表格，其中"职责"尽量从 README、各 Makefile、`.qsf` 中提炼，不要凭空编造：

   | 文件 | 所属层 | 一句话职责 | 进入路径 |
   |------|--------|-----------|----------|
   | `rtl/gpgpu_top.sv` | 顶层外壳 | 转接代码存储器/host 接口给 sm_core | 仿真+综合 |
   | `rtl/common/define.sv` | 公共层（宏头） | 全局参数宏（8 warp/64 reg/64b 指令…） | include / 综合显式 |
   | `rtl/common/oh2bin.sv` | 公共层（积木） | 独热码转二进制 | 仿真+综合 |
   | `rtl/sm_core/sm_core.sv` | SM 流水线（父） | 例化并连接 9 个子流水级 | 仿真+综合 |
   | `rtl/sm_core/sm_fetch.sv` | SM 流水线（子） | 跟踪 PC、请求取指 | 仿真+综合 |
   | `test/test_integer.py` | 测试（入口） | cocotb 测试入口，编排仿真与断言 | 仿真 |
   | `project/altera/koala_gpu.qsf` | FPGA 工程 | Quartus 配置清单（芯片/顶层/源文件） | 综合 |
   | … | … | … | … |

3. 完成后，用这张表自检三个问题：
   - 仓库里有几个 `.sv` 文件？几个 `.py`？几个 Quartus 工程文件？（答：18 + 5 + 2，再加 5 个根目录构建/说明文件 = 33，与 `git ls-files` 一致。）
   - 仿真编译了几个 `.sv`？（答：15 个，1 顶层 + 4 common + 10 sm_core。）
   - 综合编译了几个 `.sv`？（答：18 个，比仿真多出 `define.sv` 显式列出、以及 `fixed_pri_arb.sv`、`sync_fifo_count.sv`。）

**预期结果**：你得到一张可以贴在显示器旁的速查表，今后打开任何文件都能在 2 秒内说出它属于哪一层、被哪条路径使用。

> 这是一份"源码阅读型实践"，不依赖 iverilog/cocotb/Quartus，完全可以离线完成。表格里"进入路径"一列是本讲最有价值的产出——它直接体现了"仿真清单 ⊊ 综合清单"这一核心认知。

## 6. 本讲小结

- 仓库 33 个跟踪文件分为四个核心目录：`rtl/common`（公共积木）、`rtl/sm_core`（SM 流水线）、`test`（cocotb 测试台）、`project/altera`（FPGA 综合工程），外加根目录的构建脚本与 README。
- `rtl/common/` 有 7 个 `.sv`，但只有 4 个进入仿真编译清单；`define.sv` 是纯宏头（不含 `module`），经 `` `include `` 被全仓库共享，是"8 warp / 64 reg / 64b 指令"等全局参数的唯一来源。
- `rtl/sm_core/` 有 10 个 `.sv`（父模块 `sm_core.sv` + 9 个子流水级），目录与仿真清单完全一致，9 个子模块与 README 架构树逐行对应。
- `test/` 的 5 个 `.py` 是"一主四辅"结构：`test_integer.py` 是 cocotb 入口，`memory.py`/`host.py` 是扮演 GPU 外部世界的驱动器，`dump.py`/`logger.py` 是逐周期状态转储与日志记录。
- `project/altera/` 的 `.qsf` 用 18 条 `SYSTEMVERILOG_FILE` 把**全部** `.sv`（含 `common/` 的 7 个）喂给 Quartus 综合，目标芯片 Cyclone V `5CGTFD9D5F27C7`，顶层仍是 `gpgpu_top`。
- **核心认知**：仿真路径（Makefile）与综合路径（Quartus `.qsf`）共用同一份 RTL 与同一个顶层，但源文件清单不完全相同——综合比仿真多包含 `common/` 的 2 个备用积木，并把 `define.sv` 显式列入。

## 7. 下一步学习建议

- 第 1 单元到此结束：你现在已拥有"项目是什么、怎么跑、目录如何组织"的完整地图。建议把本讲的"代码地图表"（综合实践）保存好，作为后续所有源码阅读的索引。
- 第 2 单元将从 `rtl/common` 的 4 块在用积木（`oh2bin`、`rr_arb`、`fixed_pri_arb_base`、`sync_fifo`）开始精读——它们正是本讲反复提到的"地基"，先掌握这些底层单元，再读 `sm_core` 流水线会顺畅得多。
- 如果你更关心"怎么验证"，可以先跳读 `test/memory.py` 与 `test/dump.py`：前者是请求-响应式代码存储器模型（理解它就理解了 `sm_fetch` 的取指握手），后者是逐周期状态转储（日后调试流水线的利器），它们会在本系列末尾"端到端逐周期追踪"那一讲再次成为主角。
- 如果你关心 FPGA 落地，可以用 Quartus Prime 打开 `project/altera/koala_gpu.qpf`，对照本讲 4.4 的清单差异，观察综合工具如何处理那几个"仿真未编译、综合才包含"的备用积木。
