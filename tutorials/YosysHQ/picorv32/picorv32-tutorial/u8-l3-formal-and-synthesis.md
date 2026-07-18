# 形式化验证与综合评估

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂根 `Makefile` 里 `make check` 这条形式化验证流水线：它如何用 `yosys` 把 `picorv32.v` 翻译成 SMT 公式，再用 `yosys-smtbmc` 跑「有界模型检验（BMC）」与「时序归纳（induction）」两轮证明。
2. 区分两种「证明谁」的风格：顶层 `make check` 证明 CPU 自带的内联断言；`scripts/smtbmc/` 下 `tracecmp`/`axicheck`/`mulcmp`/`notrap_validop` 用「把 DUT 断言变假设、自己写断言」的方式证明外部性质。
3. 看懂 `scripts/vivado/` 的面积评估（`make area`，small/regular/large 三档）与时序评估（`make table.txt`，二分搜索最短时钟周期），并解释三种核配置在 Slice LUT 与寄存器上的差异来源。
4. 理解 `scripts/yosys/` 的 yosys 综合脚本（`synth_sim.ys` 用于后仿、`synth_gates.ys` 用于门级 ASIC 估算）在整个验证与评估流程中的位置。

本讲是「专家层」最后一讲，把视角从「CPU 跑得对不对」（仿真）提升到「CPU 在数学上不可能错」（形式化）以及「它占多大、跑多快」（综合评估）。

## 2. 前置知识

本讲不再展开 CPU 内部实现，但需要几块「工程方法」层面的基础概念。

### 2.1 仿真 vs 形式化验证

- **仿真（simulation）**：给一个具体的输入激励，跑一遍，看输出对不对。它能覆盖的只是「你想到的那些输入」，是**抽样**。前面 u1-l3、u8-l2 的 `make test_ez`、`make test` 都是仿真。
- **形式化验证（formal verification）**：把电路翻译成一阶逻辑/SMT 公式，让 SMT 求解器（solver）**数学上证明**某个性质对所有输入都成立，或找出一个反例（counter-example）。它是**穷举**——对所有合法输入一次覆盖。

形式化的代价是状态空间爆炸：寄存器一多，求解器就可能跑不出来。所以 picorv32 把形式化拆成「针对单个模块、单条性质」的小问题，而不是整体证明。

### 2.2 BMC 与时序归纳

`yosys-smtbmc` 提供两种互补的证明手段：

- **有界模型检验（BMC, Bounded Model Checking）**：在「前 \(t\) 个时钟周期」内寻找反例。如果求解器说「找不到」，只能保证前 \(t\) 步没问题——**不能**保证永远没问题。
- **时序归纳（temporal induction，`-i` 选项）**：寻找一个 \(k\) 步的不变式（inductive invariant）。若任意连续 \(k\) 步都满足性质就能推出第 \(k+1\) 步也满足，则性质对**任意长度**都成立——这是**完备**的证明。

picorv32 的 `make check` 同时跑这两轮：BMC（`-t 30`）抓浅层 bug，induction（`-t 25 -i`）补完备性。两者都通过，才算「形式化通过」。

### 2.3 断言三件套：assert / assume / restrict

在 SystemVerilog/yosys 的形式化语境里，三个关键字分工不同：

| 关键字 | 语义 | 谁来保证 |
| --- | --- | --- |
| `assert` | 「这个性质必须成立」 | **求解器去证明**，找到反例就算失败 |
| `assume` | 「假设输入满足这个条件」 | 当作对环境的**约束**，只在约束内寻找反例 |
| `restrict` | 同 assume，但是写给综合/验证工具的「硬约束」 | 约束环境 |

`assert` 是「待证命题」，`assume`/`restrict` 是「环境契约」。本讲会看到 picorv32 把这三者用在不同层次上。

### 2.4 FPGA 综合与面积/时序

- **综合（synthesis）**：把 RTL（Verilog）映射成 FPGA 的基本单元（LUT、触发器、进位链等）。
- **LUT（Look-Up Table）**：FPGA 实现组合逻辑的最小单元，7 系列 FPGA 一个 LUT 是 6 输入。「Slice LUTs」就是占用的 LUT 数，是**面积**的核心指标。
- **Slice Registers**：占用的触发器数，反映状态量大小。
- **布局布线（place & route）**：把综合后的单元摆在芯片上并连线；之后才能算真实的**时序**（关键路径有多长）。
- **fmax / 时钟周期**：时序收敛后能跑的最高时钟频率。时钟周期 \(T\) 越短，频率 \(f = 1/T\) 越高。

面积和时序往往此消彼长：加更多并行硬件（如桶形移位器）会增大 LUT 但缩短关键路径、抬高 fmax。picorv32 通过 `parameter` 在这之间做取舍，本讲最后一节会量化这种取舍。

## 3. 本讲源码地图

本讲横跨「构建脚本 + RTL 内嵌断言 + 形式化测试台 + 综合脚本」，涉及的文件较多，按下表分类理解：

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile) | `make check` 形式化流水线、`make test_synth` yosys 后仿综合入口 |
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | `ifdef FORMAL` 内联断言块、`RISCV_FORMAL` 下的 rvfi 可观测接口 |
| [scripts/smtbmc/tracecmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.v) | 双实例 trace 等价比对测试台 |
| [scripts/smtbmc/mulcmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v) | 两个乘法器协处理器（`pcpi_mul` vs `pcpi_fast_mul`）结果一致性测试台 |
| [scripts/smtbmc/axicheck.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/axicheck.v) | AXI4-Lite 主接口协议合规性检查测试台 |
| [scripts/smtbmc/notrap_validop.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/notrap_validop.v) | 「喂合法 ALU 指令绝不陷入 trap」测试台 |
| [scripts/vivado/Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/Makefile) | `make area` / `make table.txt` 综合评估入口 |
| [scripts/vivado/synth_area_top.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v) | small/regular/large 三种核配置的封装顶层 |
| [scripts/vivado/tabtest.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.v) | fmax 评估用的 `picorv32_axi`+`delay4` 延迟链顶层 |
| [scripts/vivado/tabtest.sh](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh) | 二分搜索最短时钟周期的驱动脚本 |
| [scripts/yosys/synth_sim.ys](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/yosys/synth_sim.ys) | yosys 综合脚本，产出供 `make test_synth` 后仿的 `synth.v` |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | Evaluation 章节（fmax 表、面积表）、CPI 性能数据 |

> 提示：`scripts/smtbmc/` 与 `scripts/vivado/` 下的 `*.smt2`、`*.vcd`、`*.log`、`output.*` 都是运行产物（见各自 `.gitignore`），不在仓库里，需要本地跑出来。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：(1) `make check` 的 SMTBMC 流水线；(2) 四类「测试台驱动」的检查器；(3) Vivado 面积/时序评估。

### 4.1 SMTBMC 形式化验证：`make check` 的整条流水线

#### 4.1.1 概念说明

`make check` 想回答的问题是：「**picorv32 自己声明的那些不变量，对所有合法输入是否都成立？**」它证明的对象是 CPU **自己内嵌**的断言，而不是某个外部测试程序。

这套流水线分两段：

1. **前端：`yosys` 把 RTL 翻成 SMT 公式。** 用 `read_verilog -formal` 读入源码（这一步会自动定义 `FORMAL` 宏，激活 picorv32 里 `` `ifdef FORMAL `` 的断言块），`prep` 选定顶层，再用 `assertpmux` 给多路选择器加保护性断言，最后 `write_smt2` 导出 SMT-LIB v2 文件 `check.smt2`。
2. **后端：`yosys-smtbmc` 调用 SMT 求解器做证明。** 求解器（如 yices、boolector、z3）负责真正的可满足性判定。

`-formal` 选项自动定义 `FORMAL` 宏这一点很关键：picorv32 在文件开头用宏把 `assert` 在非形式化编译时变成空语句，避免污染普通仿真与综合。见 [picorv32.v:38-48](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L38-L48)：只有在 `FORMAL` 定义时 `` `assert(expr) `` 才展开成真正的 `assert(expr)`，否则是 `empty_statement`——这正是「同一份 RTL 既跑仿真又做形式化」的关键。

#### 4.1.2 核心流程

`make check` 展开后的完整流程（伪代码）：

```
make check
 └─ make check-yices                      # check: check-yices
    ├─ 1. 构建 check.smt2（若过期）
    │     yosys:
    │       read_verilog -formal picorv32.v   # 定义 FORMAL，激活内联断言
    │       prep -top picorv32 -nordff        # 选顶层，不做 dff→dffe 重映射
    │       assertpmux -noinit; opt -fast; dffunmap
    │       write_smt2 -wires check.smt2      # 导出 SMT 公式（保留 wire 名）
    │
    ├─ 2. BMC：yosys-smtbmc -s yices -t 30 check.smt2
    │     在前 30 拍内找反例；找不到→BMC 通过
    │
    └─ 3. 归纳：yosys-smtbmc -s yices -t 25 -i check.smt2
          找 25 步不变式，证完备性；找到→induction 通过
```

关键点：

- `check-%` 是 Make 的**模式规则**（pattern rule）。`make check-yices` 匹配 `check-%`，于是 `%` 取值 `yices`，`$(subst check-,,$@)` 把目标名 `check-yices` 去掉 `check-` 前缀得到求解器名 `yices`，传给 `-s`。想换求解器只需 `make check-boolector` 或 `make check-z3`。
- **两轮证明缺一不可**：BMC 只能保证「前 N 拍」，induction 才能保证「永远」。任何一轮报反例都算失败。
- 失败时 `--dump-vcd check.vcd` 会写出反例波形，可像普通仿真那样用 GTKWave 打开定位。

#### 4.1.3 源码精读

**Makefile 的 check 规则** —— 整条流水线的入口：

`check: check-yices` 把无参的 `make check` 重定向到默认求解器 yices（[Makefile:87-91](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L87-L91)）。注意 `check-%` 的两条 recipe 分别是 BMC（无 `-i`，`-t 30`）和归纳（带 `-i`，`-t 25`），这就是上面「两轮」的来源。

`check.smt2` 的生成规则用四条 `-p` 命令拼成一条 yosys 调用（[Makefile:93-97](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L93-L97)），其中：

- `prep -top picorv32 -nordff`：`-nordff` 禁止把普通 D 触发器重映射成带使能的 DFFE，保持触发器边界与 RTL 一致，便于求解器建模。
- `assertpmux -noinit`：给 `$mux` 加断言，要求选择信号和分支输入都不能是 X（未定义），借此捕获「缺 default 分支」类的隐患。
- `write_smt2 -wires`：`-wires` 让导出的 SMT 保留 RTL 里的 wire 名（如 `mem_valid`），这样反例波形和源码能对得上号。

**picorv32.v 的内联断言块** —— `make check` 真正证明的内容。整块在 `` `ifdef FORMAL `` 下（[picorv32.v:2102-2166](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2102-L2166)），分三组性质：

第一组——**环境契约（restrict）**，约束求解器只能造「合法」的输入环境（[picorv32.v:2104-2112](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2104-L2112)：

```verilog
reg [3:0] last_mem_nowait;
always @(posedge clk)
    last_mem_nowait <= {last_mem_nowait, mem_ready || !mem_valid};
// stall the memory interface for max 4 cycles
restrict property (|last_mem_nowait || mem_ready || !mem_valid);
// resetn low in first cycle, after that resetn high
restrict property (resetn != $initstate);
```

这里第一条 restrict 表达了 picorv32 对**环境**的硬性要求：外存最多只能让一次事务挂起 4 拍（回顾 u5-l3 的 valid-ready 握手）。`last_mem_nowait` 是一个移位寄存器，记录「最近几拍内存是否在等待」；`|last_mem_nowait` 为真表示「已经有一拍没在等了」从而放宽当前拍。求解器若造出「无限拖延 mem_ready」的环境，会让 CPU 永远卡住——这条 restrict 把这种不合法环境排除掉。第二条限制复位时序：首拍 `resetn` 必须为低（`$initstate` 是 yosys 的「初始上电态」），之后为高。

第二组——**cpu_state 合法性（assert）**，[picorv32.v:2117-2136](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2117-L2136)：证明主状态机（回顾 u4-l2 的八态 one-hot）不会跑到未定义状态。值得注意的是 `cpu_state_ld_rs2` 那一行：

```verilog
if (cpu_state == cpu_state_ld_rs2) ok = !ENABLE_REGS_DUALPORT;
```

单端口寄存器堆（`ENABLE_REGS_DUALPORT=0`）才会进入 `ld_rs2`（回顾 u5-l1），所以双端口时这个状态**根本不应出现**——断言据此精确反映配置。

第三组——**Look-Ahead 接口一致性（assert）**，[picorv32.v:2138-2165](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2138-L2165)。把前一拍的 `mem_la_*`（回顾 u5-l3 的前瞻接口）锁存到 `last_mem_la_*`，下一拍断言真实的 `mem_*` 与之吻合：

```verilog
if (last_mem_la_read) begin
    assert(mem_valid);
    assert(mem_addr == last_mem_la_addr);
    assert(mem_wstrb == 0);
end
```

这把「look-ahead 提前一拍预告的事务」与「真实发生的事务」逐字段绑定，是验证前瞻接口契约（地址、数据、写掩码一致；读事务 `mem_wstrb` 必为 0）的核心性质。

#### 4.1.4 代码实践

**实践目标**：亲手跑通 `make check`，区分 BMC 与归纳两轮的输出，并理解失败时如何拿反例定位。

**操作步骤**：

1. 确认环境装有 `yosys`、`yosys-smtbmc` 和某个 SMT 求解器（如 yices2）。在项目根目录执行：

   ```bash
   make check-yices      # 或 make check（等价）
   ```

2. 观察输出：先是一段 `yosys` 生成 `check.smt2` 的日志，随后 `yosys-smtbmc` 会逐拍打印它在检查的步数。

**需要观察的现象**：

- BMC 阶段（无 `-i`）：求解器展开 30 个时间步，逐步 `Checking assertions in step N`。若全程无反例，跑完第 30 步即通过。
- 归纳阶段（带 `-i`）：求解器尝试构造不变式，通过后给出归纳证明成立的提示。
- 若某轮失败：会打印 `Counter-example ...` 并写出 `check.vcd`。

**预期结果**：在一个未改动的 picorv32 上，两轮都应通过（具体求解器耗时与机器相关，**待本地验证**）。若你故意把 `picorv32.v` 里某条 `assert` 改成 `assert(0)`（仅做实验，勿提交），则应观察到求解器立刻给出反例并生成 `check.vcd`——可用 `gtkwave check.vcd` 查看从哪一拍、哪个信号开始违反。

> 约束：本实践不修改任何已提交源码；若做了「故意触发反例」的实验，结束后务必 `git checkout picorv32.v` 还原。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `make check` 必须同时跑 BMC（`-t 30`）和归纳（`-t 25 -i`），只跑 BMC 行不行？

**参考答案**：不行。BMC 只在前 30 拍内找反例，找不到只能说明「前 30 拍没问题」，不能保证第 31 拍及以后也安全；它是**不完备**的。归纳（`-i`）通过构造不变式给出**完备**证明。两者互补：BMC 擅长快速抓浅层 bug，归纳补全长期正确性。

**练习 2**：`make check-z3` 会不会重新综合 `check.smt2`？为什么？

**参考答案**：通常不会。`check.smt2: picorv32.v` 的依赖只列了 `picorv32.v`，只要它比 `picorv32.v` 新，Make 就跳过生成、直接复用已有的 SMT 文件，只是把 `-s z3` 换成 z3 求解器跑两轮证明。这说明「翻译成 SMT」与「用哪个求解器证明」是解耦的两步。

**练习 3**：`make check` 读 picorv32.v 时用了 `-formal`，这会激活文件里哪段代码？那段代码在普通 `make test` 仿真时是什么状态？

**参考答案**：激活 [picorv32.v:38-48](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L38-L48) 的 `FORMAL` 宏与 [picorv32.v:2102-2166](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2102-L2166) 的内联断言块。普通仿真 `make test` 不传 `-formal`，`FORMAL` 未定义，`` `assert(expr) `` 展开成 `empty_statement`，断言块被 `` `endif `` 跳过，对仿真功能零影响。

### 4.2 四类检查器：tracecmp / mulcmp / axicheck / notrap_validop

#### 4.2.1 概念说明

`make check` 证明的是「CPU 自己说自己对」。但很多时候我们想证明的是**外部性质**——比如「两个不同配置的核执行轨迹完全一致」「乘法器协处理器与快速乘法器结果相同」「AXI 主接口永远遵守协议」。这些性质 CPU 自己没法用内联断言表达，需要一个**独立的 testbench** 来写。

`scripts/smtbmc/` 下的脚本采用了与 `make check` 截然不同的读入风格：

```
read_verilog -formal -norestrict -assume-asserts ../../picorv32.v
read_verilog -formal <testbench>.v
```

两个选项的**净效果**是关键：

- `-assume-asserts`：把 picorv32 内部的 `assert(...)` 当作 `assume(...)`。也就是说，CPU 自带的断言不再是「待证命题」，而变成「**假定成立的契约**」——求解器相信 CPU 遵守自己的接口约定。
- `-norestrict`：忽略 picorv32 内部的 `restrict`（环境约束），改由 **testbench 自己**来规定合法环境。

于是「证明谁」就翻转了：被证明的是 **testbench 里写的 `assert`**，而被测核（DUT）只提供「按接口契约工作」的假设。这是「**以 DUT 为契约，证明测试台性质**」的模式，非常适合写「等价比对」「协议合规」类检查。

四个检查器各自的证明目标：

| 检查器 | 被证性质 | 求解器 |
| --- | --- | --- |
| `tracecmp` | 两个相同配置的核，在相同输入下 trace 完全一致 | yices |
| `mulcmp` | `picorv32_pcpi_mul` 与 `picorv32_pcpi_fast_mul` 对任意输入给出相同结果 | yices |
| `axicheck` | `picorv32_axi` 的 AXI4-Lite 主接口永远合规（单事务在途、valid 稳定等） | boolector |
| `notrap_validop` | 喂给 CPU 的若是合法 ALU 指令，绝不进入 `trap` | yices |

#### 4.2.2 核心流程

每个 `.sh` 脚本结构完全一致，以 [tracecmp.sh](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.sh) 为例：

```
yosys:
  read_verilog -formal -norestrict -assume-asserts ../../picorv32.v   # DUT 当契约
  read_verilog -formal tracecmp.v                                     # 测试台（含待证 assert）
  prep -top testbench -nordff
  write_smt2 -wires tracecmp.smt2
yosys-smtbmc -s yices --smtc tracecmp.smtc --dump-vcd output.vcd tracecmp.smt2
```

与 `make check` 的两个差异：

1. 顶层是 `testbench`（不是 `picorv32`），testbench 内部实例化一个或多个 picorv32。
2. 多了 `--smtc tracecmp.smtc`：`.smtc` 是 yosys-smtbmc 的**约束/性质文件**，用 SMT-LIR 表达额外的 `assume`/`assert`，作用在求解层而非 RTL 层。

#### 4.2.3 源码精读

**(A) tracecmp：双实例 trace 等价比对**

[tracecmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.v) 实例化两个**完全相同配置**的 `picorv32`（`cpu_0`、`cpu_1`，见 [tracecmp.v:52-108](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.v#L52-L108)），各连一份独立内存 `mem_0`/`mem_1`。证明思路是「**差异证明**」：两个核本应行为完全一致，所以用一个 `trace_balance` 计数器把两者的 trace 流对齐——

```verilog
always @* begin
    trace_balance = trace_balance_q;
    if (trace_valid_0) trace_balance = trace_balance + 1;
    if (trace_valid_1) trace_balance = trace_balance - 1;
end
```

（见 [tracecmp.v:42-50](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.v#L42-L50)）。两核各发一条 trace 时 `balance` 一加一减抵消；只有一边发时 balance 偏移。真正待证的性质写在 [tracecmp.smtc:11-12](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.smtc#L11-L12)：

```
always -1
assert (=> (= [trace_balance] #x00) (= [trace_data_0] [trace_data_1]))
```

含义：每当两边的 trace 流重新对齐（balance 回到 0），这两条 trace 数据必须相等。`always -1` 表示「考察上一拍的 balance 与本拍的 trace」（回顾 u8-l2：trace_data 是 36 位 = 4 位标志 + 32 位载荷）。配上 `initial` 段里「初始内存相同、寄存器堆相同」（[tracecmp.smtc:1-4](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp.smtc#L1-L4)），就构成了「同输入→同行为」的完备证明。这种「等价比对」是验证「两个实现语义相同」的标准技巧。

> 衍生：[tracecmp2.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp2.v)、[tracecmp3.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/tracecmp3.v) 把思想推进一步：让两核**配置不同**（一个 `TWO_STAGE_SHIFT`、一个 `TWO_CYCLE_ALU`，见 tracecmp3.v 的 cpu0/cpu1），却共享内存与 trace，证明「不同微架构实现给出相同的可观测行为」——这是验证「同一 ISA、不同参数取舍」语义等价的有力证据。

**(B) mulcmp：乘法器一致性**

[mulcmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v) 同时实例化 `picorv32_pcpi_mul`（多周期进位保留乘法）与 `picorv32_pcpi_fast_mul`（单周期硬乘法器），喂以**任意**输入：

```verilog
wire [31:0] pcpi_insn = $anyconst;
wire [31:0] pcpi_rs1 = $anyconst;
wire [31:0] pcpi_rs2 = $anyconst;
```

（见 [mulcmp.v:11-13](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v#L11-L13)）。`$anyconst` 是 yosys 的形式化原语，表示「任意但**常数**」的值——对一次证明而言是固定的，但可取任意值，于是求解器会对所有可能的 `(insn, rs1, rs2)` 组合负责。待证性质是两个乘法器都 ready 时结果一致（[mulcmp.v:59-62](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v#L59-L62)）：

```verilog
if (pcpi_ready_0 && pcpi_ready_1) begin
    assert(pcpi_wr_0 == pcpi_wr_1);
    assert(pcpi_rd_0 == pcpi_rd_1);
end
```

回顾 u6-l1：这两个协处理器实现的是**同一条 M 扩展指令集**，只是周期数与面积不同（fast_mul 用专用 DSP、mul 用移位累加）。mulcmp 形式化地证明了「快的和慢的结果比特一致」，于是可以放心地按 fmax/面积需求二选一。

**(C) axicheck：AXI4-Lite 主接口协议合规**

[axicheck.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/axicheck.v) 实例化 `picorv32_axi`（[axicheck.v:32-64](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/axicheck.v#L32-L64)），把它的五对 AXI 通道（AW/W/B/AR/R）全部接到 testbench，逐条断言 AXI 协议规则。回顾 u7-l1 的适配器：原生接口被桥接成 AXI4-Lite。这里证明的就是「桥接后的主端口永远合规」。

最典型的几条断言（[axicheck.v:149-183](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/axicheck.v#L149-L183)）：

```verilog
if ($past(mem_axi_awvalid && !mem_axi_awready)) begin
    assert(mem_axi_awvalid);               // valid 一旦拉高，在 ready 前 must 保持
    assert($stable(mem_axi_awaddr));
    assert($stable(mem_axi_awprot));
end
```

这正对应 AXI 协议的铁律：**valid 一旦置起，在对应的 ready 出现之前，valid、地址、控制信号都必须保持稳定**。`$past(expr)` 取上一拍的值，`$stable(expr)` 等价于 `expr == $past(expr)`，`$fell(expr)` 检测下降沿。注意 axicheck 对**主端口**用 `assert`（要求核自己做到），对**从端口**（bvalid/rvalid 等输入）用 `assume`（假设外界遵守），分工严格。

为防止求解器造出「无限不 ready」的恶意环境导致假性失败，[axicheck.v:70-91](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/axicheck.v#L70-L91) 给每个通道加了 4 位超时计数器与 `restrict(timeout_xxx != 15)`——限定每笔事务最多拖延 15 拍。这与 picorv32.v 内联块的 `last_mem_nowait` 思路一致，但这里因为读 picorv32 时用了 `-norestrict`，必须由 testbench 自己重新提供这套环境约束。

**(D) notrap_validop：合法指令不陷入**

[notrap_validop.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/notrap_validop.v) 把任意取来的指令**约束成合法的 ALU 指令**，然后证明 CPU 不会进 `trap`。关键在 [notrap_validop.v:31-36](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/notrap_validop.v#L31-L36)：

```verilog
if (mem_instr && mem_ready && mem_valid) begin
    assume(opcode_valid(mem_rdata));
    assume(!opcode_branch(mem_rdata));
    assume(!opcode_load(mem_rdata));
    assume(!opcode_store(mem_rdata));
end
```

`opcode_*` 是 [opcode.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/opcode.v) 提供的 Verilog 函数（用 opcode/funct3/funct7 字段识别指令类别）。这套 `assume` 把指令空间限定为「合法、且不是分支/访存」——也就是纯 ALU 计算。随后 [notrap_validop.v:41-42](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/notrap_validop.v#L41-L42) 断言：

```verilog
if (resetn)
    assert(!trap);
```

回顾 u4-l2：`trap` 是不可恢复的死锁，触发原因之一是「非法指令 + `CATCH_ILLINSN`」。notrap_validop 证明的就是「只要喂合法 ALU 指令，就绝不会因译码失败而 trap」——形式化地排除了 ALU 数据通路里隐藏的非法指令陷阱。

#### 4.2.4 代码实践

**实践目标**：任选一个 smtbmc 检查器跑通，对比它与 `make check` 输出的差异，体会「证明 testbench 性质」与「证明 DUT 自带断言」的区别。

**操作步骤**：

1. 进入 `scripts/smtbmc/` 目录，运行其中之一（以 mulcmp 为例，它证明目标最直观）：

   ```bash
   cd scripts/smtbmc
   bash mulcmp.sh        # 需要本机有 yosys、yosys-smtbmc、yices
   ```

2. 若没有 boolector，`axicheck.sh` 会失败——可改用 `bash tracecmp.sh` 或 `bash notrap_validop.sh`（它们用 yices）。

**需要观察的现象**：

- 脚本先生成 `mulcmp.smt2`（DUT 当契约读入），再调用 `yosys-smtbmc -s yices -t 100`，展开 100 个时间步逐拍检查。
- 注意 mulcmp 用的 `-t 100` 远大于 `make check` 的 30——因为多周期乘法器（回顾 u6-l1：MUL 指令约 40 周期）需要足够长的窗口才能让两个协处理器都完成一次运算并比对。

**预期结果**：求解器应在 100 步内找不到反例，即「两个乘法器对所有输入结果一致」成立（具体耗时**待本地验证**）。生成的 `output.vcd` 在成功时通常为空/平凡，在失败时含反例波形。

**源码阅读型实践（无需求解器）**：若本机没有 SMT 求解器，改为阅读 [mulcmp.v:57-86](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v#L57-L86)。请回答：为什么它在 `pcpi_ready_0` 之后还要存一份 `pcpi_wr_ref/pcpi_rd_ref` 并再次 `assert(... == pcpi_wr_ref)`？——这是为了覆盖「两个乘法器 ready 时机不同步」的情形：先完成的那个把结果存进 `_ref`，等另一个也 ready 时与 `_ref` 比对，从而即便两者不在同一拍完成也能比对结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么这些 `.sh` 脚本读 picorv32.v 时要用 `-assume-asserts`？不用会怎样？

**参考答案**：用 `-assume-asserts` 是把 picorv32 自带的 `assert` 当作「假定成立的契约」，让求解器相信 DUT 遵守接口，从而专注证明 testbench 写的外部性质。如果不用（即保留为 `assert`），求解器会同时试图证明 picorv32 内部断言——但 testbench 提供的环境（如 axicheck 的 `restrict`）可能与 picorv32 内联块的 `restrict` 不一致，导致「证不下来」或证错对象。模式规则上，「DUT 当契约 + testbench 当命题」需要这一步。

**练习 2**：axicheck 里对 `mem_axi_awvalid`（主端口输出）用 `assert`，对 `mem_axi_bvalid`（从端口输入）用 `assume`。为什么方向相反？

**参考答案**：`picorv32_axi` 是 AXI **主**设备。主端口的 valid/addr/data 是核**自己产生**的，必须遵守协议，所以用 `assert` 证明「核做到了」。从端口的 bvalid/rvalid/ready 是**外界（Slave）给核**的，核无法控制，只能「假设外界也守规矩」，所以用 `assume` 约束环境。形式化里 `assert` 永远针对「设计自身的输出/内部」，`assume` 针对「外界的输入」。

**练习 3**：tracecmp 为什么要用 `trace_balance` 计数器，而不是直接断言「每拍的 trace_data_0 == trace_data_1」？

**参考答案**：因为两个核的 trace 可能在**时间上错位**——某一拍只有 cpu_0 发 trace、下一拍才轮到 cpu_1。直接逐拍比对会把这种合理的时间偏移误判为不等。`trace_balance` 是一个「待对齐计数」：一边发 trace 就 +1、另一边发就 -1，只有当它回到 0（表示两边的 trace 流重新对齐）时才断言两条 trace 数据相等。这是处理「等价但不一定同步」的流式比对的通用技巧。

### 4.3 Vivado 面积/时序评估

#### 4.3.1 概念说明

形式化回答「对不对」，综合评估回答「**多大、多快**」。picorv32 是「尺寸优先」的核（回顾 u1-l1），所以面积（LUT/寄存器）和主频（fmax）是它的核心卖点，README 的 Evaluation 章节直接把这两组数据当作卖点展示。

`scripts/vivado/` 提供三类评估：

1. **面积评估（`make area`）**：对 small/regular/large 三档配置做面积优先的综合，比较 Slice LUT 与寄存器。回答「不同 parameter 组合占多少资源」。
2. **时序评估（`make table.txt`）**：在多款 Xilinx 器件/速度档上，用**二分搜索**找到能收玫时序的最短时钟周期，换算出 fmax。回答「能跑多快」。
3. **单点综合（`make synth_area` / `make synth_speed`）**：对单个配置做综合或完整的布局布线，作为快速试错。

回顾 u3-l1：参数分「功能开关」「时序/面积取舍」「系统级常量」三类。面积评估本质上是把第一、二类参数的「开关组合」量化成 LUT 数。

#### 4.3.2 核心流程

**面积评估**：

```
make area
 ├─ synth_area_small     → 综合 top_small    → synth_area_small.log
 ├─ synth_area_regular   → 综合 top_regular  → synth_area_regular.log
 └─ synth_area_large     → 综合 top_large    → synth_area_large.log
 最后 grep 'Slice LUTs' 三份日志，得到 README 那张表
```

每个 `synth_area_*` 匹配 [Makefile:29-34](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/Makefile#L29-L34) 的 `synth_%` 模式规则，调用 Vivado 批处理跑对应的 `.tcl`，再用 `-grep -B4 -A10 'Slice LUTs'` 把利用率报告里那一段抠出来。三档配置的差别**全部**来自 [synth_area_top.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v) 里给 `picorv32` 传的不同 parameter。

**时序评估（fmax）**：

```
make table.txt
 ├─ 对每个 tab_<ip>_<device>_<grade>/ 调 tabtest.sh
 │    └─ 二分搜索：调整 create_clock -period，综合+布局布线，看 Slack 是 MET 还是 VIOLATED
 └─ table.sh 汇总所有 results.txt，换算成 "周期 ns (频率 MHz)" 表
```

核心是 [tabtest.sh](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh) 的二分搜索（见下文源码精读）。

#### 4.3.3 源码精读

**三档配置——面积差异的根源**

[synth_area_top.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v) 顶层定义了三个仅参数不同的封装。对照 README（[README.md:725-732](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L725-L732)）的文字描述与代码：

- **small**（[synth_area_top.v:14-20](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v#L14-L20)）：

  ```verilog
  picorv32 #(
      .ENABLE_COUNTERS(0),     // 关掉 cycle/instret 计数器
      .LATCHED_MEM_RDATA(1),   // 让外部锁存 mem_rdata
      .TWO_STAGE_SHIFT(0),     // 移位用最朴素逐位
      .CATCH_MISALIGN(0),      // 不检查对齐错误
      .CATCH_ILLINSN(0)        // 不检查非法指令
  ) picorv32 ( ... );
  ```

  把能关的都关了——没有计数器、没有对齐/非法指令捕获，只留「能跑基本 RV32I」的最小电路。这对应 README 的 761 LUT / 442 寄存器。

- **regular**（[synth_area_top.v:53-69](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v#L53-L69)）：全用默认参数，额外把 Look-Ahead 接口的 `mem_la_*` 端口引出。默认配置启用计数器、对齐/非法指令捕获，对应 917 LUT / 583 寄存器。

- **large**（[synth_area_top.v:106-112](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v#L106-L112)）：

  ```verilog
  picorv32 #(
      .COMPRESSED_ISA(1), .BARREL_SHIFTER(1),
      .ENABLE_PCPI(1), .ENABLE_MUL(1), .ENABLE_IRQ(1)
  ) picorv32 ( ... );
  ```

  开启压缩指令、桶形移位器、PCPI、乘法、中断——功能最全，对应 2019 LUT / 1085 寄存器。

三档之间 LUT 从 761 跳到 2019（约 2.65 倍），**差异完全由开启的可选特性决定**，CPU 主体逻辑相同。这正是 u3-l1 「参数决定面积」结论的实测证据。桶形移位器（回顾 u5-l2）与乘法协处理器（回顾 u6-l1）是 large 比 regular 多出约 1100 LUT 的主要贡献者。

**综合脚本**

[synth_area_small.tcl](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_small.tcl)（regular/large 同构）是一段极简的 Vivado Tcl：

```tcl
read_verilog ../../picorv32.v
read_verilog synth_area_top.v
read_xdc synth_area.xdc
synth_design -part xc7k70t-fbg676 -top top_small
opt_design -sweep -propconst -resynth_seq_area
opt_design -directive ExploreSequentialArea   ;# 面向时序逻辑面积的优化指令
report_utilization
report_timing
```

注意两点：目标是 Kintex-7 `xc7k70t`；连用了两次 `opt_design`，第二次带 `ExploreSequentialArea` 指令——这是 Vivado 专门针对触发器/状态机面积的优化档位，呼应了「面积优先」的评估意图。`synth_area.xdc` 只给一个很松的 20 ns 时钟约束（[synth_area.xdc:1](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area.xdc#L1)），因为面积评估不关心能不能跑快。

> 另一个入口 `make synth_area`（匹配 `synth_%`，跑 [synth_area.tcl](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area.tcl)）综合的是**裸 `picorv32_axi`**，与 `make area` 的三档 top 不是一回事——前者是「AXI 变体单点面积」，后者是「原生核三档对比」。

**fmax 的二分搜索**

时序评估的核心算法在 [tabtest.sh](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh) 里。它把「时钟周期」编码成一个两位数（如 `24` 表示 2.4 ns），从一个保守起点出发，根据每次 Vivado 的 `Slack` 是 `MET`（还有余量，可缩短周期）还是 `VIOLATED`（已违规，需放长周期）做二分（[tabtest.sh:61-86](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh#L61-L86)）：

```
speed=20, step=16           # 起点 2.0ns，步长 1.6ns
while countdown > 0:
    synth_case(speed)        # 跑一次完整综合+布局布线，带 phys_opt_design -retime 等
    if Slack == VIOLATED:    speed += step/2; step /= 2     # 太快，放慢
    elif Slack == MET:       speed -= step/2; step /= 2; best=speed   # 还能更快
    if step == 0:            countdown--; 重置 step 继续逼近
```

注意 [tabtest.sh:36-41](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh#L36-L41) 的 Tcl 脚本用了 `phys_opt_design -retime` 与多次 `place/route` 迭代——这是 Vivado 最激进的时序优化组合（retiming 会跨触发器搬移组合逻辑以平衡延迟）。也就是说，fmax 评估不是「随便综一下」，而是把工具榨干后的最好成绩。

待测顶层 [tabtest.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.v) 用的是 `picorv32_axi` 且只开 `TWO_CYCLE_ALU(1)`（[tabtest.v:75-77](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.v#L75-L77)），与 README「picorv32_axi module with enabled TWO_CYCLE_ALU」完全对应。更讲究的是它在每个 I/O 上串了一串 `delay4`（[tabtest.v:53-73](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.v#L53-L73)）——四级移位寄存器延迟链。这迫使关键路径包含真实的「寄存器→走线→寄存器」延迟，避免把 I/O 直接连到顶层端口时 Vivado 给出「不切实际的虚高 fmax」。

最后 [table.sh](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/table.sh) 把每个 `results.txt` 里的两位数周期换算成频率，生成 README 的 fmax 表。换算公式是：

\[
f_{\text{MHz}} = \frac{1000}{T_{\text{ns}}} = \frac{1000}{\text{speed}/10} = \frac{10000}{\text{speed}}
\]

对应代码 `$((10000 / speed))`（[table.sh:19](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/table.sh#L19)）。例如 Kintex-7 -2 档 `speed=24` → \(10000/24 \approx 416\) MHz，与 README 的 416 MHz 吻合。

**yosys 综合脚本：后仿与门级估算**

`scripts/yosys/` 提供两条 yosys（非 Vivado）综合路径，服务于根 Makefile：

- [synth_sim.ys](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/yosys/synth_sim.ys) 产出 `synth.v`，供 `make test_synth` 做**综合后仿真**——验证「综合后的网表行为是否与 RTL 一致」（根 [Makefile:99-100](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L99-L100)、[Makefile:51-52](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L51-L52)）。脚本里 `chparam` 一次性设好 `COMPRESSED_ISA/ENABLE_MUL/ENABLE_DIV/ENABLE_IRQ/ENABLE_TRACE`，再 `synth` 与 `write_verilog`。

- [synth_gates.ys](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/yosys/synth_gates.ys) 用 `dfflibmap`/`abc -liberty` 把设计映射到某个标准单元库（`.lib`），产出 `.blif`，用于**门级/ASIC 面积估算**——不依赖具体 FPGA，给出「工艺无关」的门数参考。

这两条脚本把 yosys 当作「开源、可脚本化」的综合前端，与 Vivado 的「商用、报告详尽」形成互补。

#### 4.3.4 代码实践

**实践目标**：跑出 small/regular/large 三档面积数据，解释 LUT 与寄存器差异来自哪些 parameter；并理解 README 的 fmax 表是如何被二分搜索算出来的。

**操作步骤（需 Vivado）**：

1. 进入 `scripts/vivado/`，改好 `VIVADO_BASE` 指向本机 Vivado 安装路径，然后：

   ```bash
   cd scripts/vivado
   make area
   ```

2. 终端会依次打印三份日志里 `Slice LUTs` 附近 10 行的利用率报告。

**需要观察的现象**：三档的 Slice LUTs 应分别接近 761 / 917 / 2019，Slice Registers 接近 442 / 583 / 1085（与 [README.md:736-740](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L736-L740) 一致，微小差异源于 Vivado 版本——README 用 2017.3，你用的版本可能不同，**待本地验证**）。

**差异来源分析（源码阅读型，无需 Vivado）**：

对照 [synth_area_top.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v) 与本讲 u3-l1/u5/u6 的知识，逐项解释：

| small→regular 增量 | 增量来源（回顾） |
| --- | --- |
| +约 156 LUT | 开 `ENABLE_COUNTERS`（cycle/instret 64 位计数器）、开 `CATCH_MISALIGN/CATCH_ILLINSN`（对齐与非法指令检测逻辑）、`TWO_STAGE_SHIFT` 由 0→1 |
| +约 141 寄存器 | 64 位 cycle/instret 计数器本身就是大量触发器 |

| regular→large 增量 | 增量来源（回顾） |
| --- | --- |
| +约 1102 LUT | `BARREL_SHIFTER`（u5-l2，把移位并入 ALU 组合逻辑，占大量 LUT）、`ENABLE_MUL`+`ENABLE_PCPI`（u6-l1 的进位保留多周期乘法器）、`COMPRESSED_ISA`（u7-l2 的 16→32 位展开逻辑） |
| +约 502 寄存器 | 乘法器的部分积寄存、PCPI 状态、压缩指令的 `mem_rdata_q` 等状态量 |

**fmax 实践（可选，耗时较长）**：`make table.txt` 会对多款器件做二分搜索，单次完整布局布线可能数分钟。若无 Vivado，可阅读 [tabtest.sh:61-86](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh#L61-L86)，手算验证：若 `speed=24` 时 MET、`speed=22` 时 VIOLATED，best_speed 应为多少？（答：24，对应 2.4 ns / 416 MHz。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `synth_area.xdc` 里只给 20 ns 这么松的时钟约束？

**参考答案**：因为面积评估的目的是量「占多少资源」，不是「能跑多快」。给一个肯定能轻松满足的松约束，让 `opt_design` 在「无时序压力」下纯粹按面积优化（`ExploreSequentialArea` 指令），这样得到的 LUT 数才反映「该配置的最小面积」。如果给紧约束，工具会为了凑时序而复制逻辑、用更宽但更快的实现，面积就会虚高，不再可比。

**练习 2**：tabtest.v 为什么要在每个 I/O 上串 `delay4` 延迟链，而不是把端口直接连到 picorv32_axi？

**参考答案**：为了让测得的 fmax「真实可信」。如果 AXI 信号直连顶层端口，Vivado 可能把 IOB（I/O Block）里的寄存器也算进关键路径，或让端口驱动强度影响结果，给出脱离实际应用的虚高频率。串一串 `delay4`（多级触发器）后，关键路径被强制变成「芯片内部寄存器→组合逻辑→寄存器」，与真实 SoC 里 picorv32 被其他逻辑包围的场景一致，测出的 fmax 才有工程参考价值。

**练习 3**：README 说 Dhrystone 在带 `ENABLE_FAST_MUL/ENABLE_DIV/BARREL_SHIFTER` 时是 0.516 DMIPS/MHz、CPI 4.1；关掉 look-ahead 后掉到 0.305 DMIPS/MHz、CPI 5.232（[README.md:365-373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L365-L373)）。结合 u5-l3 解释为什么关掉 look-ahead 会让 CPI 变差。

**参考答案**：回顾 u5-l3，look-ahead 接口（`mem_la_*`）把下一次内存事务的地址/数据**提前一拍**暴露给外存，使外存能在 `mem_valid` 拉高的同一拍就返回数据（单拍成交）。关掉它后，外存必须等 `mem_valid` 出现才开始读，每次访存多花一拍，而 CPI≈4 的核里访存占比很高，于是平均 CPI 从 4.1 升到 5.2，性能（DMIPS/MHz）随之下降。这是「时序/面积」与「性能」之间的又一处取舍：look-ahead 提速但增加组合路径长度、更难时序收敛。

## 5. 综合实践

把本讲三块内容串成一个端到端的小任务：

**场景**：你要为一个新 FPGA 项目挑选 picorv32 配置，需要同时保证「功能正确」「资源不超预算」「主频够用」。

**任务**：

1. **正确性**：在项目根目录 `make check-yices` 通过；再进入 `scripts/smtbmc` 跑 `bash mulcmp.sh` 与 `bash notrap_validop.sh`，记录是否都无反例。这三项分别覆盖「CPU 自带不变量」「乘法器一致性」「合法 ALU 指令不 trap」。

2. **资源预算**：阅读 [synth_area_top.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/synth_area_top.v)，若你的预算是 1000 LUT 以内且需要中断与乘法，判断应该选 regular 还是 large？如果坚持要乘法但可接受逐位移位（不要桶形），试着手写一个 `top_custom` 模块：复制 `top_large`，把 `BARREL_SHIFTER(1)` 改成 `0`、保留 `ENABLE_MUL(1)` 与 `ENABLE_IRQ(1)`，预测它的 LUT 会比 large（2019）小多少？（参考 u5-l2：桶形移位器是 large 相比 regular 多出 LUT 的大头之一。）

3. **主频**：阅读 [tabtest.sh](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/vivado/tabtest.sh) 与 README 的 fmax 表，回答：若目标器件是 Kintex UltraScale+ `-2` 档、应用要求至少 500 MHz，picorv32_axi + TWO_CYCLE_ALU 能否满足？依据是哪一行数据？（答：能。`xcku3p-ffva676-2-e` 行给出 1.4 ns / 714 MHz，远超 500 MHz。）

4. **汇总**：写一段话，说明你最终选的配置、对应的面积（引用 README 或本地 `make area` 结果）、可达 fmax，以及「为什么形式化与综合评估让这个选择有底气」。

这个任务把「形式化证明正确性 → 综合评估量化面积与时序 → 据 parameter 解释差异 → 做工程决策」串成了 picorv32 真实开发中会走的完整闭环。

## 6. 本讲小结

- `make check` 是一条两段式形式化流水线：`yosys` 用 `read_verilog -formal`（自动定义 `FORMAL` 宏）把 picorv32 内联断言翻成 `check.smt2`，`yosys-smtbmc` 再用 BMC（`-t 30`）抓浅层 bug、用时序归纳（`-t 25 -i`）补完备性，两轮都过才算通过。
- picorv32 的内联断言（[picorv32.v:2102-2166](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2102-L2166)）用 `restrict` 约束环境（如「外存最多拖延 4 拍」）、用 `assert` 证明 cpu_state 合法性与 look-ahead 接口一致性；非形式化编译时这些断言被宏变成空语句，对仿真零影响。
- `scripts/smtbmc/` 的检查器走「DUT 当契约、testbench 当命题」模式：用 `-assume-asserts` 把 CPU 自带断言变成假设、用 `-norestrict` 丢掉内置环境约束，转而证明 testbench 自己写的性质。tracecmp 证「双实例行为等价」、mulcmp 证「两个乘法器结果一致」、axicheck 证「AXI 主接口协议合规」、notrap_validop 证「合法 ALU 指令不 trap」。
- 面积评估 `make area` 的三档差异**完全来自 parameter**：small 761 / regular 917 / large 2019 LUT，桶形移位器与乘法协处理器是 large 多出约 1100 LUT 的主因；这把 u3-l1「参数决定面积」的结论量化成了实测数据。
- 时序评估 `make table.txt` 用**二分搜索**找最短可收敛时钟周期，配合 `phys_opt_design -retime` 等最激进优化与 `delay4` I/O 延迟链，得到可信的 fmax（Kintex-7 约 416 MHz、UltraScale+ 最高 769 MHz）；频率换算公式 \(f_{\text{MHz}}=10000/\text{speed}\)。
- yosys 综合脚本（`synth_sim.ys` 后仿、`synth_gates.ys` 门级 ASIC 估算）作为开源补充，与 Vivado 的商用评估形成「正确性（形式化）+ 资源（面积）+ 性能（fmax）」三位一体的质量保障体系。

## 7. 下一步学习建议

至此，picorv32 学习手册的八讲（从项目总览到 SoC 集成、再到本讲的形式化与综合评估）已完整覆盖。继续深入的建议：

1. **亲手扩展一个 PCPI 协处理器并用形式化验证**：结合 u6-l1 的 PCPI 协议与本讲的 mulcmp 模式，仿照 [mulcmp.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/scripts/smtbmc/mulcmp.v) 写一个「你的协处理器 vs 软件参考模型」的等价比对，用 `yosys-smtbmc` 证明它对所有输入结果正确。这是把本讲方法落到自己设计上的最佳练习。

2. **阅读 RISC-V 形式化规范（`riscv-formal`）**：picorv32 的 `RISCV_FORMAL` 宏与 rvfi 接口（[picorv32.v:123-155](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L123-L155)、[picorv32.v:1977-2099](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1977-L2099)）就是为对接 `riscv-formal` 框架而留的（根 Makefile 的 `test_rvf` 目标即用 `rvfimon.v`）。可进一步学习 riscv-formal 如何用 rvfi 信号做 ISA 级别的完备证明，理解 picorv32 这套「自检断言」之外更强的「ISA 一致性证明」。

3. **跑一次完整的 `make table.txt`**：如果你有 Vivado 与一台性能尚可的机器，跑完整张 fmax 表，体会「二分搜索 + retiming」实际耗时；再尝试改 `TWO_CYCLE_ALU`/`BARREL_SHIFTER` 等参数，观察 fmax 与面积的联动，把 u3-l1/u5/u6 讲的「时序-面积取舍」变成自己的实测手感。

4. **对照 Dhrystone**：阅读 [dhrystone/](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/dhrystone) 目录与 README 的 CPI 表，把「CPI≈4」「0.516 DMIPS/MHz」与本讲的 fmax 联系起来——性能 = CPI × fmax，理解 picorv32 为什么「用较高 CPI 换小面积与高 fmax」是符合其「辅助处理器」定位的合理工程取舍。
