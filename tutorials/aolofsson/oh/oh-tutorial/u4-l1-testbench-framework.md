# 通用测试平台架构

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 OH! 统一仿真平台 `dv_top` 的**三段式结构**：仿真控制（clock/reset/start）、被测器件（dut）、激励与监视（driver/monitor）。
- 看懂贯穿整套平台的 **`access` / `packet` / `wait` 握手**——这是 emesh 事务在测试平台里的标准表达方式。
- 解释 `dut_active` / `dut_error` / `dut_done` 等状态接口如何驱动一次仿真的「上电 → 注入激励 → 收响应 → 结束」生命周期。
- 明白**同一个 `dut` 模块名**如何由用户在**编译期**用自己写的 `dut_xxx.v` 替换，从而让一个测试平台外壳服务上百个不同的 IP。
- 识别仓库里测试平台骨架与脚本的历史遗留问题（缺文件、接口不一致、`src/` 路径假设），并把「代码是事实」的阅读原则再次落到具体例子上。

## 2. 前置知识

本讲依赖你在 **u1-l3（仿真环境搭建）** 中已经建立的认知：装好 iverilog 与 gtkwave、用 `source setenv.sh` 设好 `OH_HOME`、走「`build.sh` 编译 → `sim.sh` 运行 → `view.sh` 看波形」三步流程。如果你还没搭起环境，请先回到 u1-l3。

除此之外，下面几个概念会用通俗的话再过一遍，但你只要有个印象就行，细节会在正文里展开：

- **测试平台（testbench）**：一段**不会被综合成真实电路**的 Verilog 代码，它的唯一任务是「陪 DUT 演戏」——给 DUT 喂输入、看 DUT 的输出、判断对错。你可以把它理解成「数字电路的万用表 + 信号发生器 + 示波器」三合一。
- **DUT（Device Under Test，被测器件）**：真正要验证的那块电路，比如一个 GPIO、一个 FIFO、一条 elink 链路。本讲里 DUT 是个抽象角色，具体是谁由你编译时传的文件决定。
- **激励（stimulus）**：喂给 DUT 的一串输入事务（在 OH! 里就是 `.emf` 文件里的若干行）。
- **监视器（monitor）**：站在 DUT 输出口旁边、记录每一笔响应事务的「记录员」。
- **握手（handshake）**：发送方举旗说「我有一笔有效数据」（`access`/`valid`），接收方举旗说「我准备好收了」（`ready`/`~wait`），两边都举旗的那一拍数据才真正成交。这是后面所有事务传输的基础。
- **emesh 事务包**：OH! 自定义的 104 位（`PW = 2*AW+40`，`AW=32`）数据包，里面打包了「写/读、目的地址、数据」等字段。u5-l1 会专门拆它，本讲只需要把它当成「一根 104 位宽的快递包裹」。

> 一句话定位：u1-l3 教你怎么**把仿真跑起来**；本讲（u4-l1）带你**打开引擎盖**，看 `dut.bin` 背后那个统一的测试平台骨架长什么样、各模块怎么连。下一讲 u4-l2 会专门讲 `.emf` 激励文件格式与 `dv_driver` 的回放细节。

## 3. 本讲源码地图

本讲聚焦 `stdlib/testbench/` 目录下的「平台骨架」文件。下表列本讲会精读或引用的关键文件：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `stdlib/testbench/dv_top.v` | 仿真顶层，把控制 / DUT / 驱动三段拼起来 | **核心**，本讲主角 |
| `stdlib/testbench/tb_dut.v` | DUT 包装的**接口契约 stub**（空壳，只列端口） | 定义「DUT 长什么样」的合同 |
| `stdlib/testbench/testbench.v` | 另一套更宽的接口 stub（含 ext/dut 双向包通道） | 对照参考，理解历史接口演变 |
| `stdlib/testbench/dut_template.v` | 一个可直接套用的 `dut` 空模板 | 教你从零写 DUT 包装的起点 |
| `stdlib/testbench/dut_clockdiv.v` | 把 `oh_clockdiv` 包成 `dut` 的实例 | 「真实 IP 接入平台」的最小范例 |
| `gpio/dv/dut_gpio.v` | 把 `gpio` 包成 `dut` 的实例 | 完整外设接入范例 |
| `stdlib/testbench/oh_simctrl.v` | **可运行**的仿真控制器（时钟/复位/结束/超时） | 对照理解 `dv_ctrl` 应有的行为 |
| `stdlib/testbench/dv_random.v` | 含 `module dv_ctrl` 但残缺的桩文件 | 解释「`dv_ctrl` 缺失」这个坑 |
| `stdlib/testbench/dv_driver.v` | 激励回放 + 监视 + 内存模型的总装 | 三段式里的「驱动」段 |
| `stdlib/testbench/libs.cmd` | iverilog 的 `-y`/`+incdir+` 库搜索清单 | 解释 DUT 如何在编译期被找到 |
| `scripts/build.sh` | 简化编译脚本 | 解释「编译期替换 DUT」的命令 |

## 4. 核心概念与源码讲解

本讲的三个最小模块是：**dv_top（顶层三段式）**、**dv_ctrl（仿真控制）**、**dut 包装（固定端口契约）**。三者关系可以用下面这张俯瞰图概括（先看图建立直觉，再逐段读源码）：

```
                      ┌─────────────── dv_top (顶层) ───────────────┐
                      │                                              │
   ┌──────────┐  clk1/clk2   ┌────────────┐   access_in / packet_in  ┌───────────┐
   │ dv_ctrl  │ ───────────▶ │   dut      │ ◀─────────────────────── │ dv_driver │
   │ (控制段) │  nreset      │ (被测段)    │                          │ (驱动段)  │
   │          │  start ─────▶│            │ ──▶ access_out/packet_out │           │
   └──────────┘              └────────────┘ ◀──────────────────────── └───────────┘
         ▲                       │   dut_active/wait_out          dut_wait(反压)
         │ stim_done ◀───────────┘            clkout
         └─ 也收 dut_active/stim_done 用于决定何时 $finish
```

三段之间的「语言」就是 `access`/`packet`/`wait` 三件套：驱动段把事务包推进 `dut`，`dut` 把响应包吐回驱动段的监视器；控制段则只管时钟、复位、启停和收尾。

---

### 4.1 dv_top：三段式仿真顶层

#### 4.1.1 概念说明

`dv_top` 是 OH! 给所有 IP 准备的**通用仿真外壳**。它的设计哲学是「**外壳不变，内核可换**」：时钟怎么打、激励怎么注入、响应怎么收、仿真何时结束——这些和具体 IP 无关的杂事全部固定在 `dv_top` 及其两侧的 `dv_ctrl`、`dv_driver` 里；而真正要测的电路，则通过一个**名字永远叫 `dut`** 的模块接入。

这样做的好处显而易见：写一百个 IP，只需要写一百份小小的 `dut_xxx.v` 包装，而不用每个 IP 都重新发明一遍时钟发生器和波形 dump。`dv_top` 本身是一个**无端口**的顶层模块（`module dv_top();`），因为它已经是仿真树的最顶端，不需要再对外暴露任何信号。

#### 4.1.2 核心流程

`dv_top` 的执行流程可以这样描述（伪代码）：

```text
仿真启动 (time = 0)
   │
   ├── dv_ctrl 段：初始化 nreset=0，启动 clk1/clk2 周期翻转
   │       ── 等若干周期 ──▶ 拉高 nreset（释放复位）
   │       ── 再等若干周期 ──▶ 拉高 start（允许驱动段开始注入激励）
   │
   ├── dv_driver 段：看到 start 后，从 test_0.emf 逐行回放事务
   │       每个 cycle：把 (stim_access, stim_packet) 送到 dut 的 (access_in, packet_in)
   │       若 dut 拉高 wait_in（反压）则暂停推进
   │       同时：监视 dut 的 (access_out, packet_out)，记录响应
   │       全部回放完 ──▶ stim_done = 1
   │
   ├── dut 段：在时钟驱动下处理每笔事务，产生响应
   │
   └── dv_ctrl 段：检测到 stim_done（与 test_done）──▶ 等一个超时余量 ──▶ $finish
```

这里有一个关键设计：**激励注入与响应采集是同时进行的**，不是「先全部注入、再统一采」。`access`/`packet`/`wait` 握手保证两者按节拍对齐——这就是为什么这套平台能仿真带读返回、带反压的真实外设（如 GPIO 的读寄存器、SPI 的收发）。

#### 4.1.3 源码精读

先看 `dv_top` 顶部的参数与关键线网声明：

[stdlib/testbench/dv_top.v:5-8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L5-L8) 定义了顶层四个静态参数：`N=1`（同时传输的事务通道数，简单测试固定为 1）、`IDW=12`（coreid 位宽）、`AW=32`（地址位宽）、以及由地址宽派生的 **`PW = 2*AW+40 = 104`**（emesh 包总宽，u5-l1 会详拆）。

> 注意 `PW` 是个派生量：它由 `AW` 算出来，而不是独立硬编码。这意味着只要改 `AW`，包宽会自动跟着变——这是 OH! 把「地址宽」作为系统主参数的体现。

接着看面向 `dut` 的一组线网：

[stdlib/testbench/dv_top.v:17-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L17-L20) 声明了 `dut_active`、`dut_wait[N-1:0]`、`dut_access[N-1:0]`、`dut_packet[N*PW-1:0]`。注意它们都按 `N` 缩放——当 `N>1` 时，这是一组并行的事务通道（一个微型片上网络），`N=1` 时退化为单通道。

然后是 `dv_ctrl` 的例化（**三段式的第一段：控制**）：

[stdlib/testbench/dv_top.v:49-60](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L49-L60) 例化 `dv_ctrl`，它向平台输出 `nreset / clk1 / clk2 / start / vdd / vss`（时钟、复位、启动、电源），并从平台回收 `dut_active`（DUT 已就绪）和 `stim_done`（激励回放完毕）两个状态，用于决定何时结束仿真。`test_done` 这里被写死成 `1'b1`（注释 `//optimize later` 说明这是占位简化）。

第二段是 `dut`（**三段式的第二段：被测件**）：

[stdlib/testbench/dv_top.v:67-84](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L67-L84) 例化名为 `dut` 的模块，传入 `PW`、`N` 两个参数。它从 `dv_ctrl` 收 `clk1/clk2/nreset/vdd/vss`，从 `dv_driver` 收激励 `(stim_access, stim_packet, stim_wait)`，并把响应 `(dut_active, clkout, dut_wait, dut_access, dut_packet)` 回送给驱动段。**这里例化的模块名固定写作 `dut`，但它具体是什么电路，由你编译时传的 `dut_xxx.v` 决定**——这正是「编译期替换」的关键（详见 4.3 节）。

第三段是 `dv_driver`（**三段式的第三段：驱动+监视**）：

[stdlib/testbench/dv_top.v:95-114](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L95-L114) 例化 `dv_driver`，参数 `NAME("test")` 指定激励文件名前缀（运行时会被软链成 `test_0.emf`，见 u1-l3 的 `sim.sh`）。它向 `dut` 输出激励 `(stim_access, stim_packet, stim_wait)` 与完成标志 `stim_done`，并从 `dut` 回收 `(dut_access, dut_packet, dut_wait)` 用于监视。注意 `clkin` 接 `clk1`（驱动激励用主时钟），`clkout` 接 dut 回送的时钟（监视响应时跟 DUT 的输出节拍）。

顶部的 `/*AUTOWIRE*/` 块也值得一看：

[stdlib/testbench/dv_top.v:23-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v#L23-L33) 是 **Emacs VERILOG-MODE 自动生成**的线网声明（`clk1/clk2/nreset/start` 来自 `dv_ctrl`，`stim_*` 来自 `dv_driver`）。你在 u1-l4 见过 `AUTOINST`；这里是配套的 `AUTOWIRE`——它会扫描下面所有例化语句，自动为「未声明的输出」补上 `wire` 声明。这也是为什么源码里看不到 `clk1` 的手动 `wire` 声明：它被自动补全了。生成工具不一定现成可用，所以理解「这段是被自动生成的」即可，不必强求复现工具链。

#### 4.1.4 代码实践

**实践目标**：不看答案，凭源码画出 `dv_top` 中三段的信号连接关系图，从而检验你是否真的看懂了数据流向。

**操作步骤**：

1. 打开 [stdlib/testbench/dv_top.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_top.v)，只读 L49–L114 三个例化块。
2. 在纸上画三个方框：`dv_ctrl`、`dut`、`dv_driver`。
3. 对每一对例化块，把**同名信号**当成「一根线」，在方框之间连线，并在连线上标注信号名与方向。
4. 把信号按三类用不同颜色区分：
   - **时钟/复位/电源类**：`clk1`、`clk2`、`nreset`、`vdd`、`vss`、`clkout`、`start`。
   - **激励→DUT 类**：`stim_access`→`access_in`、`stim_packet`→`packet_in`、`wait_in`（注意 `stim_wait` 接到 dut 的 `wait_in`）。
   - **DUT→驱动类**：`access_out`→`dut_access`、`packet_out`→`dut_packet`、`wait_out`→`dut_wait`。

**需要观察的现象**：

- `clk1` 同时接到 `dut.clk1` 和 `dv_driver.clkin`——说明**激励回放和 DUT 用同一个主时钟节拍**。
- `clkout`（DUT 输出）只接到 `dv_driver.clkout`——说明**监视器跟 DUT 的输出时钟走**，这对源同步链路（如 elink）很重要。
- `start` 由 `dv_ctrl` 发出、`dv_driver` 接收——**激励不是仿真一开始就注入，而是要等控制段发令**。
- `stim_done` 由 `dv_driver` 发出、`dv_ctrl` 接收——**仿真结束要等激励回放完**。

**预期结果**：你应该得到一张「`dv_ctrl` 在左、`dut` 在中、`dv_driver` 在右」的三方框图，左边框出时钟/复位/启停/收尾线，中间是一束 `access/packet/wait` 双向事务线。完整运行命令（环境搭好后的 `build.sh`+`sim.sh`）的结果属于 u1-l3 的范畴，本实践**待本地验证**画出图的准确性即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `dv_top` 顶部的 `N` 从 1 改成 2，哪些信号的位宽会变？包总宽 `PW` 会变吗？

**参考答案**：`dut_access`、`dut_wait`、`stim_access`、`stim_wait` 从 1 位变成 2 位；`dut_packet`、`stim_packet` 从 `PW` 位变成 `2*PW` 位（即 208 位）。`PW` 本身**不会变**，因为它只依赖 `AW`（`PW=2*AW+40`），与 `N` 无关。`N` 表达的是「并行通道数」。

**练习 2**：`dv_top` 模块没有端口（`module dv_top();`），那它如何向外界（比如波形文件）输出结果？

**参考答案**：它不需要硬件端口。仿真结果通过两条非综合通道输出：一是内部模块（如 `dv_ctrl`/`oh_simctrl`）调用 `$dumpfile`/`$dumpvars` 把全部信号写进 `waveform.vcd`（用 gtkwave 看）；二是通过 `$display` 在仿真日志里打印 `[OH] DUT TEST PASSED/FAILED` 之类的判结论文字。

---

### 4.2 dv_ctrl：仿真控制（时钟 / 复位 / 启停 / 结束）

#### 4.2.1 概念说明

`dv_ctrl` 是三段式里的「**导演**」：它不碰任何业务事务，只负责四件事——

1. 产生时钟（`clk1`/`clk2`）；
2. 产生复位序列（先保持 `nreset=0` 一段时间，再释放）；
3. 在合适时机发出 `start`，允许驱动段开始注入激励；
4. 监听 `stim_done` / `dut_active`，在激励耗尽后调用 `$finish` 结束仿真（并 dump 波形）。

理解这一段的关键是「**复位与启动是有先后顺序的**」：真实芯片上电后不会立刻开始干活，而是先保持复位、等时钟稳定、再放开复位、再给一个「开始」信号。`dv_ctrl` 把这套上电时序在仿真里复刻出来。

> ⚠️ **代码是事实·重要提醒**：`dv_top.v` 里例化的 `dv_ctrl`，在当前仓库**没有一份完整且能编译的实现**。`grep` 全仓 `module dv_ctrl` 只命中 [stdlib/testbench/dv_random.v:1](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v#L1)，而那份代码是**残缺的桩**（下面会详述）。仓库里**真正完整、能跑**的同类控制器是 `oh_simctrl.v`，但它的端口与 `dv_top` 期望的 `dv_ctrl` 不一致。所以本节我们用 `dv_random.v` 看「`dv_ctrl` 应该长什么样」，用 `oh_simctrl.v` 看「一个能真正工作的控制器是怎么写的」。这和 u1-l2/u1-l3 反复强调的「文档/脚本可能滞后、以源码为准」是同一类问题，请务必带着这个意识读下去。

#### 4.2.2 核心流程

`dv_ctrl` 的预期行为（综合 `dv_random.v` 桩与 `oh_simctrl.v` 的实现来还原）：

```text
time=0: nreset=0, clk 翻转中, start=0
   │  (保持复位 ~TIME_RESET 个时钟)
   ▼
拉高 nreset  (异步复位释放)
   │  (等待 ~TIME_WAIT)
   ▼
拉高 start   (放行激励)
   │
   ▼
持续运行：驱动段回放 .emf，DUT 处理事务
   │
   ▼
stim_done & test_done 同时为真  ──▶  再等一个 TIMEOUT 余量  ──▶  $finish
```

对应的数学含义很朴素：复位段长度 \(T_{reset} = k_{reset} \cdot T_{clk}\)，启动延迟 \(T_{wait} = k_{wait} \cdot T_{clk}\)，其中 \(T_{clk}\) 是时钟周期。仿真总时长被 `TIMEOUT` 兜底，防止 DUT 死锁时仿真永不退出。

#### 4.2.3 源码精读

先看残缺的 `dv_ctrl` 桩（它揭示了**意图**）：

[stdlib/testbench/dv_random.v:18-29](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v#L18-L29) 把 `nreset/clk/start` 声明为 `reg` 并初始化为 0，然后在 `initial` 块里：先等 `CLK_PERIOD*10` 拉高 `nreset`，再等 `CLK_PERIOD*100` 拉高 `start`。这正是上面流程图里「先复位、后启动」的时序。

[stdlib/testbench/dv_random.v:33-37](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v#L33-L37) 是「收尾电路」：`always @* if(stim_done & test_done) #(TIMEOUT) $finish;`——激励和测试都完成后再等一个超时余量就结束仿真。

[stdlib/testbench/dv_random.v:40-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v#L40-L41) 是时钟发生器：`always #(CLK_PHASE) clk = ~clk;`，用一句无限翻转的 `always` 产生方波时钟。

[stdlib/testbench/dv_random.v:45-52](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v#L45-L52) 是波形 dump：`$dumpfile("waveform.vcd"); $dumpvars(0, dv_top);`，并用 `` `ifdef NOVCD `` 提供一个关掉波形的开关。

> ⚠️ 但这份桩**不能直接编译**：它引用了 `CLK_PERIOD`、`CLK_PHASE`、`TIMEOUT` 三个**未定义**的宏/标识符，端口区还有一处不完整的 `input [15:0]`（[dv_random.v:12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v#L12)），而且端口表与 `dv_top` 期望的 `dv_ctrl`（需要 `clk1/clk2/vdd/vss` 等）对不上。这就是为什么说它只是「意图的化石」。

为了看到**一个真正完整、能工作**的控制器长什么样，我们转看 `oh_simctrl.v`。它的端口更宽（三路时钟 + `mode` 状态机 + `dut_fail/dut_done`），但做的事情与 `dv_ctrl` 同构：

[stdlib/testbench/oh_simctrl.v:8-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L8-L15) 用参数把所有时序都参数化：`TIMEOUT`（总超时周期数）、`PERIOD_CLK/PERIOD_FASTCLK/PERIOD_SLOWCLK`（三路时钟周期）、`RANDOM_CLK/RANDOM_DATA`（是否注入随机性，用于压力测试）。

[stdlib/testbench/oh_simctrl.v:59-73](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L59-L73) 是经典的**上电时序** `initial` 块：`#1` 后先置 `nreset=0/clk=0/...`，保持 `TIME_RESET` 个时钟周期复位，拉高 `nreset`，等 `TIME_WAIT` 后置 `mode=3'b001`（load，加载激励），再过 `TIME_LOAD` 后置 `mode=gomode`（go，开始驱动）。注意 `gomode` 由 `RANDOM_DATA` 决定走 `3'b010`（确定性激励）还是 `3'b011`（随机激励）——这是 OH! 用同一套平台既跑定向测试又跑随机压力测试的开关。

[stdlib/testbench/oh_simctrl.v:98-105](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L98-L105) 是三路时钟发生器，结构与 `dv_ctrl` 桩里的 `always #(phase) clk=~clk;` 完全一致，只是有三路、且周期可参数化。

[stdlib/testbench/oh_simctrl.v:111-120](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L111-L120) 是**判结论收尾**：每个时钟上升沿检查 `dut_done`，若完成则再等 `#500`，根据 `dut_fail` 打印 `[OH] DUT TEST PASSED` 或 `[OH] DUT TEST FAILED`，再 `$finish`。这比 `dv_ctrl` 桩多了一层「通过/失败」的语义。

[stdlib/testbench/oh_simctrl.v:125-130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L125-L130) 是**超时兜底**：一个独立的 `initial` 块，`#(TIMEOUT)` 后无条件 `$finish` 并打印 `[OH] DUT TEST TIMEOUT`，防止 DUT 死锁导致仿真挂死。

把两份代码对照看，你能提炼出 OH! 仿真控制器的「**四件套范式**」：`initial` 复位序列 + `always` 时钟翻转 + `always` 完成检测（带 PASSED/FAILED 判结论）+ `initial` 超时兜底。

#### 4.2.4 代码实践

**实践目标**：通过阅读两个控制器的源码，把它们「四件套」的对应关系填进一张表，体会「桩 vs 完整实现」的差距。

**操作步骤**：

1. 并排打开 [stdlib/testbench/dv_random.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dv_random.v) 与 [stdlib/testbench/oh_simctrl.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v)。
2. 仿照下表，把每一件套在两份文件里的**行号**填进去（部分已给出）：

   | 四件套 | `dv_ctrl` 桩（dv_random.v） | `oh_simctrl.v`（完整） |
   | --- | --- | --- |
   | 复位/启动时序 | L23–L29 | L59–L73 |
   | 时钟发生 | L40–L41 | L98–L105 |
   | 完成检测 + 判结论 | L33–L37（无判结论） | L111–L120 |
   | 超时兜底 | （无独立兜底，靠 `TIMEOUT`） | L125–L130 |

3. 思考：为什么 `oh_simctrl` 多了「判结论」和「独立超时」这两件？提示：`dv_ctrl` 桩的收尾逻辑 `if(stim_done & test_done)` 用的是**电平敏感的 `always @*`**，若 `stim_done` 永远不来，仿真就永远不会结束——`oh_simctrl` 的独立 `initial` 超时正是为这个漏洞兜底。

**需要观察的现象**：`dv_ctrl` 桩里 `CLK_PERIOD`/`CLK_PHASE`/`TIMEOUT` 是裸标识符（未在任何 `` `define `` 或 `parameter` 中定义），而 `oh_simctrl` 里同名概念全部是**带默认值的 `parameter`**（`PERIOD_CLK=10` 等）。这就是「能编译」与「不能编译」的分水岭。

**预期结果**：你应该得出结论——`oh_simctrl.v` 是一份可直接复用的、参数化的仿真控制器模板；而 `dv_random.v` 里的 `dv_ctrl` 只是早期残稿。若要在本地真正用 `dv_top` 跑通仿真，需要补一份与 `dv_top` 端口匹配的、行为类似 `oh_simctrl` 的 `dv_ctrl.v`（**待本地验证**：这属于修复性工作，超出本讲阅读目标）。

#### 4.2.5 小练习与答案

**练习 1**：`oh_simctrl` 里复位段长度是「`clk_phase * TIME_RESET`」，其中 `clk_phase = PERIOD_CLK/2`。若 `PERIOD_CLK=10`、`TIME_RESET=50`，复位段实际持续多少纳秒？

**参考答案**：`clk_phase = 10/2 = 5`，复位段 = `5 * 50 = 250` 个时间单位。由于 [oh_simctrl.v:46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L46) 用 `$timeformat(-9, 0, " ns", 20)` 把时间单位设为纳秒（`-9` 即 1e-9 秒），所以是 **250 ns**。

**练习 2**：`oh_simctrl` 为什么要提供 `RANDOM_CLK` 和 `RANDOM_DATA` 两个参数？

**参考答案**：`RANDOM_CLK` 让时钟周期带抖动（[oh_simctrl.v:80-88](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/oh_simctrl.v#L80-L88)），用来压力测试 CDC（跨时钟域）逻辑；`RANDOM_DATA` 让平台走随机激励模式（`gomode=3'b011`），用来做随机化验证。两者都体现了「同一套平台、靠参数切换定向测试/随机测试」的设计。

---

### 4.3 dut 包装：固定端口契约与编译期替换

#### 4.3.1 概念说明

三段式能「外壳不变、内核可换」，靠的是一份**端口契约**：无论你要测的是 GPIO、SPI 还是 elink，包装出来对外都必须长成同一个样子——模块名恒为 `dut`，端口固定。这样 `dv_top` 里那句 `dut #(.PW(PW), .N(N)) dut(...)` 永远成立，不用为每个 IP 改顶层。

这份契约的核心是一对**对称的事务接口**：

- **激励驱动侧**（由 `dv_driver` 送给 `dut`）：`access_in`（有效）、`packet_in`（包）、`wait_in`（反压）。
- **DUT 驱动侧**（由 `dut` 送给 `dv_driver` 的监视器）：`access_out`、`packet_out`、`wait_out`。

再加上公共的时钟复位（`clk1/clk2/nreset`）、电源（`vdd/vss`）、以及 `dut_active`（DUT 就绪）和 `clkout`（DUT 输出时钟）。

> 这里的 `access`/`packet`/`wait` 就是 emesh 协议的握手三件套（u5-l1 详述）。`access`≈valid，`wait`≈「还没 ready」（注意是**高有效表示反压**，即 `wait=1` 表示「请等」），`packet` 是 104 位事务包。理解了这个映射，你就明白为什么所有外设都能接进同一个平台——它们说的都是 emesh 这门「公共语言」。

#### 4.3.2 核心流程

把一个真实 IP 接入平台的步骤是高度模式化的：

```text
1. 复制 dut_template.v，把模块名固定为 dut
2. 按 dut 端口契约声明所有输入输出（access_in/packet_in/wait_in/access_out/...）
3. 把 dv_ctrl 来的 clk1 接成 IP 需要的 clk；assign dut_active=1; assign clkout=clk1
4. 把 IP 的 emesh 接口与平台的 packet_in/packet_out 对接（位宽对齐 PW=104）
5. 处理 IP 特有的物理端口（如 gpio 的 gpio_in/gpio_out）—— tie 或 loopback
6. 用 build.sh 把这份 dut_xxx.v 作为 $1 传给 iverilog，编译期即完成替换
```

第 6 步是「编译期替换」的精髓：iverilog 命令行只传**一个** `dut_xxx.v`，它定义了模块 `dut`；而 `dv_top.v`（以及 `dv_ctrl`/`dv_driver`）通过 `libs.cmd` 的 `-y` 库搜索路径被自动纳入。于是顶层 `dv_top` 例化的那个 `dut`，就指向了你传进来的具体 IP 包装。

#### 4.3.3 源码精读

先看**契约的法定文本**——两份 stub。它们是空模块（无实现），存在的意义就是把「DUT 应该有哪些端口」白纸黑字写下来：

[tb_dut.v:8-35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/tb_dut.v#L8-L35) 是较新的 DUT 包装契约：参数 `PW/N/TARGET`，端口分三组——基础测试接口（`clk/fastclk/slowclk/nreset/go/ctrl`）、环境包接口（`valid/packet/ready`）、DUT 状态与响应（`dut_active/dut_error/dut_done/dut_status` 及 `dut_valid/dut_packet/dut_ready`）。注意它额外显式给出了 `dut_error`/`dut_done`/`dut_status` 三个**状态信号**——这是判结论（PASSED/FAILED）的数据来源。

[testbench.v:8-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/testbench.v#L8-L41) 是另一套更宽、更早期的契约 stub：参数 `PW=256/CW=16/N=32/DEPTH=8192`，端口包含一个 `mode[2:0]`（`0=idle,1=load,2=go,3=rng,4=bypass`，与 `oh_simctrl` 的 `mode` 对应）、一组外部写接口（`ext_*`）和一组 DUT 响应接口（`dut_*`），还有 `dut_status/dut_error/dut_done/dut_fail`。可以看到它是「激励内存 + 状态机驱动」的更完整设想；而当前 `dv_top` 走的是简化路线。两份 stub 并存，正反映了这套平台在演进中接口收窄、收敛到 `access/packet/wait` 三件套的过程。

再看**可直接套用的模板**：

[dut_template.v:1-35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_template.v#L1-L35) 是一个最小可编译的 `dut` 骨架：模块名 `dut`，端口与 `dv_top` 的例化完全对齐（`clk1/clk2/nreset/vdd/vss/access_in/packet_in/wait_in` 进，`dut_active/clkout/wait_out/access_out/packet_out` 出），并把 `dut_active` 写死为 1、`clkout` 接 `clkin1`。你只需在文件末尾加上对你自己 IP 的例化，就完成了一个 DUT 包装。注意 [dut_template.v:32-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_template.v#L32-L33) 引用了未声明的 `clkin1`（应为 `clk1`），这是一处待修小瑕疵——以源码为准时你会经常遇到这类细节。

接着看两个**真实接入范例**，理解「包装」到底做了什么。

最简单的是 `dut_clockdiv.v`：

[dut_clockdiv.v:36-46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_clockdiv.v#L36-L46) 在 `dut` 外壳内例化 `oh_clockdiv`，把平台的 `packet_in[11:8]`（激励包里的 4 位）当成 `divcfg`（分频配置）喂进去，输出 `clkout`。可以看到「包装」的本质：**把平台的 emesh 包字段，翻译成 IP 真正需要的那些具体控制信号**。这里甚至没动用完整的 emesh 译码，只取了包里的几位——对于 stdlib 原语级测试，这是最轻量的接法。

最完整的是 `dut_gpio.v`：

[dut_gpio.v:67-83](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L67-L83) 在 `dut` 外壳内例化 `gpio`，用 `/*AUTOINST*/` 按名连接端口：把 `access_in/packet_in/wait_in` 直连到 gpio 的 emesh 从口，把 gpio 的 `access_out/packet_out/wait_out` 回送给平台。物理引脚 `gpio_in/gpio_out` 被 [dut_gpio.v:69](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v#L69) 接成回环（`gpio_in(gpio_out[AW-1:0])`），让输出能被自己读到，方便测试。同时用 `AUTO_TEMPLATE` 把 `gpio_out/gpio_dir` 扩展成 `[AW-1:0]` 位宽——这正是 u1-l4 讲过的 `AUTO_TEMPLATE` 在真实工程里的用法。这是「完整外设接入平台」的标准范式，u6-l2 会专门拆 gpio 本体。

最后看**编译期替换的命令**：

[scripts/build.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh) 的核心只有一行：`iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f $OH_HOME/scripts/libs.cmd -o dut.bin $1`。`$1` 就是你那份 `dut_xxx.v`（它定义模块 `dut`）；`-f libs.cmd` 把 `dv_top`/`dv_driver`/各 IP 所在目录通过 `-y` 纳入搜索空间；`-DTARGET_SIM=1` 打开仿真专用分支、`-DCFG_ASIC=0` 选 soft(RTL) 实现（回顾 u1-l3、u2-l1）。

[stdlib/testbench/libs.cmd:3-32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/libs.cmd#L3-L32) 列出了所有 `-y` 库目录与 `+incdir+` 头文件目录。`-y` 的作用是「按模块名找文件」：当 iverilog 遇到未定义的模块 `gpio`，就在这些目录里依次找 `gpio.v`、`gpio.def` 等。注意里面有 `../../common/dv`、`../../memory/dv`、`../../accelerator/hdl` 等条目——**这些目录在当前仓库已不存在**（回顾 u1-l2/u1-l3 的历史路径问题），遇到找不到模块的报错时应优先怀疑这里。

> 一个值得留意的细节：`scripts/build.sh` 只把 `$1`（DUT 包装）放到了命令行，**没有显式列出 `dv_top.v`**。要让 `dv_top` 成为仿真根、并正确连进 `dut`/`dv_driver`，实际命令还需要把 testbench 目录也纳入（testbench/README.md 给出的更完整形式是 `iverilog -g2005 -DTARGET_SIM=1 $cfg $core.v $DV -f $LIBS -o $core.bin`，其中 `$DV` 就承载 dv_top 等驱动文件）。这与 u1-l3 的结论一致：**简化脚本与文档化命令之间存在落差，以你本地实际能跑通的命令为准**。

#### 4.3.4 代码实践

**实践目标**：亲手把一个 stdlib 原语包成 `dut`，体会「编译期替换」。

**操作步骤**（以 `oh_counter` 为例，属源码阅读 + 改写型实践）：

1. 打开 [stdlib/testbench/dut_template.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_template.v)，把它另存为 `stdlib/testbench/dut_counter.v`（**只在你自己的工作副本里改，不要提交**）。
2. 参照 [dut_clockdiv.v:36-46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/testbench/dut_clockdiv.v#L36-L46) 的写法，在 `dut` 外壳内例化 `oh_counter`：把 `packet_in` 的某几位接成 counter 的控制信号（如 `en`、加减方向），把 counter 的计数值接到 `packet_out` 的某字段，以便回读。
3. 处理 `clkout/dut_active/wait_out` 的 tie-off（照搬 dut_clockdiv 的 `assign` 即可）。
4. 用 `iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f stdlib/testbench/libs.cmd -o dut.bin stdlib/testbench/dut_counter.v` 尝试编译（**注意**：由于 4.2 节指出的 `dv_ctrl` 缺失与 `libs.cmd` 里的失效路径，这一步很可能报模块找不到的错误——这本身就是本讲要让你体会的现实）。

**需要观察的现象**：

- 编译时 iverilog 报告哪些模块「not found」？是否落在 `common/`、`memory/`、`accelerator/` 这类已不存在的 `-y` 路径上？
- 若报 `dv_ctrl` 找不到，对照 4.2 节你是否能解释原因？

**预期结果**：你会直观感受到「平台骨架是残缺的、需要补齐才能跑」这一现实——这正是本讲反复强调的「代码是事实」。如果你只是想验证「包装契约」本身，可以退而求其次：只编译你的 `dut_counter.v` + `oh_counter.v`（不接 dv_top），用 iverilog 检查端口是否对齐，这一步**预期可通过**。完整端到端运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`dut_clockdiv.v` 把 `packet_in[11:8]` 当作分频配置。为什么它能这样「随手取包里的几位」，而 `dut_gpio.v` 却要把整个 `packet_in[PW-1:0]` 原样接给 gpio？

**参考答案**：因为 `oh_clockdiv` 本身不懂 emesh 协议，它只认一个 4 位的 `divcfg`，所以包装层从激励包里「挑」了几位给它；而 `gpio` 内部自带 emesh 接口（能自己译码 `access/packet/wait`），所以包装层把整包原样透传。前者是「原语级轻量包装」，后者是「外设级完整包装」。

**练习 2**：`dut` 契约里 `wait_in` 和 `wait_out` 都是高有效（=1 表示反压）。如果你设计的新 IP 内部用的是「`ready` 高有效表示可接收」，包装层该怎么转？

**参考答案**：取反即可：`assign ready_to_ip = ~wait_from_platform;`（平台没说「等」就是「可接收」），反过来 `assign wait_out_to_platform = ~ready_from_ip;`。记住 OH! 的约定是 **`wait` 高有效=反压**，与常见的 `ready` 高有效=可接收恰好相反。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「平台考古 + 接线还原」任务：

**任务**：假设你要给团队新同事写一页《OH! 仿真平台速查》，请基于本讲源码完成以下三件事——

1. **画一张完整的 `dv_top` 信号连接图**（综合 4.1.4 的实践）：要求标出 `dv_ctrl → dut`、`dv_ctrl → dv_driver`、`dv_driver ↔ dut` 三组连线的每一根信号名与方向，并用颜色区分「时钟复位类 / 激励→DUT 类 / DUT→监视类」。

2. **写一段「平台现状说明」**（综合 4.2 与 4.3 的「代码是事实」发现）：用 3–5 句话向新同事说明——这套 `dv_top` 平台骨架设计得很漂亮（三段式、emesh 统一接口、编译期替换 DUT），但**当前仓库里它并不能开箱即跑**，至少有这三处坑：(a) `dv_ctrl` 无完整实现（只有 `dv_random.v` 里的残桩）；(b) `oh_simctrl.v` 是完整控制器但端口与 `dv_top` 期望不一致；(c) `libs.cmd` 与 `build_all.sh` 引用了已不存在的 `src/`、`common/`、`memory/`、`accelerator/` 路径。请给出「想真正跑通需要补哪些东西」的建议。

3. **挑一个真实范例做精读**：在 `dut_clockdiv.v` 与 `dut_gpio.v` 中任选其一，逐行说明「平台的 emesh 包字段是如何被翻译成该 IP 的具体控制信号的」，并指出包装层里用到的 `AUTOINST`/`AUTO_TEMPLATE` 各起了什么作用（回顾 u1-l4）。

**自检标准**：

- 第 1 题的图里，`clk1` 应同时连到 `dut.clk1` 和 `dv_driver.clkin`；`stim_done` 应从 `dv_driver` 指向 `dv_ctrl`。
- 第 2 题应明确点出「设计意图」与「当前实现」的落差，而不是笼统说「平台不能用」。
- 第 3 题应能说清「原语级包装取包的若干位」与「外设级包装透传整包」的区别。

完成后，你就具备了阅读任何 OH! IP 的 `dut_xxx.v` 并理解它如何接入仿真平台的能力——这正是下一讲 u4-l2（`.emf` 激励格式与 `dv_driver` 回放）的前提。

## 6. 本讲小结

- `dv_top` 采用**三段式结构**：`dv_ctrl`（仿真控制）+ `dut`（被测件）+ `dv_driver`（激励回放与响应监视），三段之间用统一的 `access`/`packet`/`wait` 握手通信。
- `access`（有效）+ `packet`（104 位 emesh 包）+ `wait`（高有效=反压）是贯穿全平台的**事务三件套**，它就是 emesh 协议在测试平台里的表达；`dut_active`/`dut_done`/`dut_error` 等状态信号负责生命周期与判结论。
- `dut` 是一份**固定端口契约**：无论测什么 IP，包装出来对外都叫 `dut`、端口固定，从而让 `dv_top` 外壳对所有 IP 通用。
- DUT 在**编译期被替换**：`build.sh` 把你写的 `dut_xxx.v` 作为命令行参数传给 iverilog，该文件定义的模块 `dut` 即是被 `dv_top` 例化的那个；`libs.cmd` 用 `-y` 搜索路径把其余平台与 IP 文件纳入。
- 仿真控制器的**四件套范式**是：`initial` 复位/启动时序 + `always` 时钟翻转 + `always`/`initial` 完成检测（带 PASSED/FAILED 判结论）+ `initial` 超时兜底；`oh_simctrl.v` 是这套范式的完整范例。
- **代码是事实**：`dv_top` 例化的 `dv_ctrl` 在当前仓库无完整实现（仅 `dv_random.v` 残桩），`libs.cmd`/`build_all.sh` 还引用了 `src/`、`common/`、`memory/`、`accelerator/` 等已不存在的路径——阅读与复现时务必以实际源码为准。

## 7. 下一步学习建议

- **下一讲 u4-l2（激励驱动与 `.emf` 测试格式）**：本讲把 `packet_in` 当成「一根 104 位的快递包裹」一笔带过，下一讲会拆开它——精读 `.emf` 文件每一行 `data_data_addr_ctrl_access` 的字段含义，看 `dv_driver`/`stimulus` 如何把文本回放成时序，以及 `egen.pl` 如何生成随机激励。
- **顺带读 u5-l1（emesh 包格式与协议）**：本讲的 `access/packet/wait` 握手与 `PW=104` 都源自 emesh；要真正理解「为什么是 104 位、字段怎么排」，u5-l1 是权威出处。
- **动手目标**：在进入 u4-l2 前，确保你能独立画出本讲综合实践第 1 题的 `dv_top` 连接图——这是判断「是否看懂平台骨架」的硬指标。
- **延伸阅读**：想看更复杂外设如何接入同一平台，可先扫一眼 [gpio/dv/dut_gpio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/dv/dut_gpio.v) 与 `elink/hdl/` 下的 `dut_elink.v`（若存在），它们都是「外设级完整包装」的实例。
