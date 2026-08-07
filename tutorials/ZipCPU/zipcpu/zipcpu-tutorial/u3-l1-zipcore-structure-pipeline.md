# zipcore 总体结构与流水线阶段

## 1. 本讲目标

本讲是「CPU 核心与流水线实现」单元的第一讲，带读者从源码视角建立 ZipCPU 内核 `zipcore` 的整体地图。学完后你应当能够：

- 读懂 `zipcore` 的构建期参数（`OPT_MPY`/`OPT_DIV`/`OPT_CIS`/`OPT_PIPELINED` 等），理解它们如何裁剪出一颗规模不同的 CPU。
- 说出 ZipCPU 五级流水线（取指 / 译码 / 读操作数 / 执行+访存 / 写回）每一级的职责，并把每一级对应到 `zipcore.v` 里真实的信号前缀（`pf_`/`dcd_`/`op_`/`alu_`/`wr_`）。
- 解释一个关键事实：**取指缓存和访存控制器并不在 `zipcore` 内部**，而是通过端口接在外面的「外壳」里，本讲只搭骨架，具体取指/访存模块留到后续讲义。
- 在源码里定位每级的时钟使能（`*_ce`）与停顿（`*_stall`）信号，画出一条从取指到写回的数据流草图。

> 本讲只看「骨架」，不深入 ALU 运算细节、乘除法、冒险停顿的完整推导——这些分别是 u3-l4、u3-l5、u3-l7 的主题。

## 2. 前置知识

阅读本讲前，你应该已经具备以下认知（来自前置讲义）：

- **软核 CPU 与 RTL**：ZipCPU 是用 Verilog 写的 32 位 RISC 软核，`rtl/` 目录是硬件源码（u1-l1）。
- **四种顶层封装**：`zipsystem`/`zipbones`/`zipaxil`/`zipaxi` 四个外壳包裹的是**同一个**内核 `zipcore`（u1-l3）。本讲就钻进这个被反复包裹的内核。
- **寄存器组与状态寄存器 CC**：ZipCPU 有 supervisor/user 两套各 16 个通用寄存器，CC（R14）的低 4 位是 Z/C/N/V 标志，第 5 位 GIE 兼作寄存器组的第 5 位地址（u2-l1）。本讲会看到这两套寄存器在 Verilog 里如何声明。
- **条件执行**：指令位 21–19 的 3 位 `Cnd` 字段决定指令是否真正写回（u2-l4）。本讲会看到这个判断在流水线哪一级完成。

还需要两个通用的硬件设计概念：

- **流水线（pipeline）**：把一条指令的执行拆成若干级，每一时钟周期每级处理一条不同的指令，像工厂流水线一样重叠执行，提高吞吐。代价是级与级之间需要「流水线寄存器」保存中间结果，并要处理冒险（hazard）。
- **时钟使能（clock enable, CE）与停顿（stall）**：在 Verilog 里，流水线一级是否「往前走」常用一个 `*_ce` 信号控制；当后级没准备好时就拉高 `*_stall`，让前级原地等待一个周期。ZipCPU 大量使用这对信号来管理流水线节奏。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲如何使用 |
|------|------|--------------|
| [rtl/core/zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v) | CPU 内核本体，约 6100 行，包含参数、端口、寄存器组、五级流水线、停顿与写回逻辑 | 本讲的主角，所有行号引用都来自这里 |
| [rtl/core/README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/README.md) | `rtl/core/` 目录导读，列出取指/访存/ALU/乘除等模块族 | 帮助理解「哪些模块在 zipcore 之外」 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 与架构规范（LaTeX 源） | 提供规范视角的五级流水线描述与 `OPT_*` 参数含义 |

> 阅读提示：`zipcore.v` 用大段 `// {{{ ... // }}}` 折叠注释和形如 `PIPELINE STAGE #N :: ...` 的标题把代码切成分块。顺着这些标题读，比从头到尾顺序读要清晰得多。

## 4. 核心概念与源码讲解

### 4.1 zipcore 的构建参数列表：用 OPT_* 裁剪出一颗 CPU

#### 4.1.1 概念说明

同一个 `zipcore.v` 源文件，可以综合出「带乘法器、带数据缓存、带用户模式」的完整 CPU，也可以综合出「无乘法、无缓存、无用户模式」的极简 CPU。这种「一份源码、多种形态」靠的是 **构建期参数（parameter）**：它们在综合时就是常量，配合 `generate if (...)` 语法，让综合工具把不需要的分支整段裁掉。

ZipCPU 的参数几乎都以 `OPT_` 开头，可粗分为三类：

1. **架构类**：决定流水线深度、寄存器文件实现、缓存大小（`OPT_PIPELINED`、`OPT_DISTRIBUTED_REGS`、`OPT_DCACHE` 等）。
2. **指令集类**：决定支持哪些指令（`OPT_MPY` 乘法、`OPT_DIV` 除法、`OPT_SHIFTS` 移位、`OPT_CIS` 压缩指令、`OPT_LOCK` 总线锁）。
3. **封装/调试类**：决定对外接口形态（`OPT_DBGPORT` 调试端口、`OPT_TRACE_PORT` 跟踪端口、`OPT_PROFILER` 性能剖析、`OPT_USERMODE` 双模式）。

理解参数的最好方式是记住一句话：**参数不是运行时开关，而是综合时剪刀**。`OPT_MPY = 0` 不是「运行时禁用乘法」，而是「综合出的电路里根本没有乘法器」，遇到乘法指令会触发非法指令异常。

#### 4.1.2 核心流程

参数生效的典型链路：

```text
parameter OPT_DIV = 1
        │
        ├── (Stage 4) generate if (OPT_DIV != 0) 实例化除法器 thedivide
        │              否则把 div_* 输出绑成常数 0
        │
        ├── (Stage 3) op_valid_div <= (OPT_DIV)&&(dcd_DIV)&&...   // 译码出的除法才有效
        │
        └── (规范)  spec.tex: 若 OPT_DIV=0，除法指令 → 非法指令错
```

即：参数 → `generate` 选择性实例化子模块 → 各级使能信号带上参数与运算 → 规范层定义语义。参数之间也有依赖，例如 `OPT_PIPELINED_BUS_ACCESS` 默认就跟随 `OPT_PIPELINED`。

#### 4.1.3 源码精读

参数列表紧随 `module zipcore #(` 之后，是一长串 `parameter` 声明：

- [rtl/core/zipcore.v:L41-L61](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L41-L61) —— zipcore 的全部构建参数。几个最关键的：

  - `ADDRESS_WIDTH=30`：地址宽度按「字地址」算 30 位（即 32 位字节地址的低 2 位不参与寻址）。
  - `RESET_ADDRESS=32'h010_0000`：复位后 PC 的起始地址。
  - `OPT_MPY=0`：乘法实现选择。0=无乘法（非法指令）；1–4=需 1–4 周期的硬件乘法；>4=无 DSP 时的移位-加法软乘法（约 33 周期）。**默认是 0**，说明裸内核默认不带货乘法器。
  - `OPT_DIV=1`、`OPT_SHIFTS=1`：默认带除法器、带任意位数移位。
  - `IMPLEMENT_FPU=0`：浮点单元目前是占位（实验性），默认关闭。
  - `OPT_CIS=1'b1`：压缩指令子集（Compressed Instruction Set），默认开启。
  - `OPT_PIPELINED=1'b1` 与 `OPT_PIPELINED_BUS_ACCESS=(OPT_PIPELINED)`：默认是流水线模式，且总线访问也默认流水化（后者默认跟随前者）。
  - `OPT_USERMODE=1'b1`：默认支持 supervisor/user 双模式（于是寄存器组有 32 个）。
  - `OPT_DBGPORT`/`OPT_TRACE_PORT`/`OPT_PROFILER`：调试/跟踪/剖析端口，默认只开调试端口。

参数之间依赖的一个例子，见 `OPT_PIPELINED_BUS_ACCESS` 直接以 `OPT_PIPELINED` 作为默认值，以及把地址宽度派生出局部参数 `AW`：

- [rtl/core/zipcore.v:L52-L63](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L52-L63) —— `OPT_PIPELINED_BUS_ACCESS=(OPT_PIPELINED)` 与 `localparam AW=ADDRESS_WIDTH`。

参数如何「裁剪电路」的典型示例是除法器：`generate if (OPT_DIV != 0)` 才实例化 `div`，否则把所有 `div_*` 输出绑成常数零：

- [rtl/core/zipcore.v:L1527-L1563](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1527-L1563) —— 除法器可选实例化（`thedivide`）与关闭时的零值兜底分支。

规范层对这些参数有权威解释，建议对照阅读：

- [doc/src/spec.tex:L3351-L3457](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3351-L3457) —— spec 的「ZipCPU Parameters」清单，逐条解释 `OPT_PIPELINED`、`OPT_MPY`、`OPT_DIV`、`OPT_SHIFTS`、`OPT_CIS`、`OPT_LOCK` 等的含义与代价。例如它明确指出 `OPT_MPY=3` 是 Xilinx 7 系的主力配置（输入寄存→乘→输出寄存共 3 拍），而 `OPT_SHIFTS=0` 会「悄悄执行错误的指令」而非报非法指令。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是把参数和它「剪掉的电路」对应起来。

1. **实践目标**：亲手验证「参数 = 综合期剪刀」。
2. **操作步骤**：
   - 打开 [rtl/core/zipcore.v:L1527-L1563](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1527-L1563)，找到除法器的 `generate if (OPT_DIV != 0)` 分支与其 `else` 分支。
   - 同样在文件里搜索 `IMPLEMENT_FPU`，对比浮点单元的 `generate` 分支（注意它在非形式化编译时基本是占位）。
   - 在 [doc/src/spec.tex:L3401-L3417](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3401-L3417) 读 `OPT_MPY` 各取值的周期数说明。
3. **需要观察的现象**：`generate if` 的两个分支里，关闭分支总是把输出信号（如 `div_busy`、`div_result`）绑成常数，并用 `unused_xxx` 伪赋值安抚 Verilator 的「未使用信号」告警。
4. **预期结果**：你能填出下面这张表（待本地核对）：

   | 参数取值 | 综合结果 | 相关非法指令 |
   |---------|----------|--------------|
   | `OPT_DIV=0` | 无除法器，`div_*` 恒为常数 | 除法指令 → 非法指令 |
   | `OPT_MPY=0` | 无乘法器 | 乘法指令 → 非法指令 |
   | `OPT_CIS=0` | 译码器不含压缩指令逻辑 | 压缩指令按普通字处理 |

5. **运行说明**：本实践无需运行综合，纯源码阅读即可完成；若想真实验证裁剪效果，可用 Verilator 分别以不同 `OPT_*` 编译并对比 `rtl/obj_dir/` 产物大小（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说把 `OPT_PIPELINED` 设为 0 并不会让单条指令执行得更快？

**参考答案**：`OPT_PIPELINED=0` 主要简化的是流水线**停顿逻辑**并删掉各级间重复的流水线寄存器，使任一时刻流水线里最多只有一条指令。它减少的是逻辑面积，并不改变单条指令走完各级所需的时钟数。spec 在 [L3351-L3359](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3351-L3359) 明确说「it doesn't fundamentally affect the pace of a single instruction's execution」。

**练习 2**：参数 `OPT_DCACHE` 默认是 1，但 `zipcore.v` 里搜不到 `dcache` 的实例化，为什么？

**参考答案**：因为数据缓存（以及取指缓存）属于「访存/取指模块族」，由**外壳**（`zipwb`/`zipaxil` 等）实例化并通过端口连给 `zipcore`，内核本身只提供内存请求/结果端口。`OPT_DCACHE` 实际作用于外壳一侧的模块选择（详见 u3-l6）。

---

### 4.2 内部寄存器组与五级流水线阶段

#### 4.2.1 概念说明

`zipcore` 的核心是一个**五级流水线**。规范在引言里用一句话点明了各级：

> stages for **Prefetch**, **Decode**, **Read-Operand**, a combined stage containing the **ALU**, **Memory**, and **Divide** units, and then the final **Write-back** stage.

也就是说，第 4 级是一个「合级」——ALU、访存、除法（以及未来的 FPU）四条执行通路并排，由同级的写回逻辑择一提交。`zipcore.v` 用带编号的注释标题把每一级的变量声明分块，这是阅读这份大文件最好的「路标」。

理解流水线有两个抓手：

1. **数据流**：一条指令从取指走到写回，每一级把它「需要传给下一级」的信息存进流水线寄存器（带 `r_` 前缀或前级信号命名）。
2. **控制流**：每一级有一个时钟使能 `*_ce`（决定是否前移）和一个停顿 `*_stall`（后级反压），还有一个全局 `master_ce`/`master_stall`。一条指令能不能往前走，取决于这一串使能与停顿的与或结果。

还有一个**容易踩坑的关键点**：`zipcore` **内部并不实例化取指缓存和访存控制器**。它只实例化了 `idecode`（译码器）、`cpuops`（ALU）、`div`（除法器）三个子模块；取指和访存通过端口（`i_pf_*`/`o_pf_*`、`o_mem_*`/`i_mem_*`）接到「外面」——实际由外壳里的 `pfcache`/`prefetch`、`memops`/`pipemem`/`dcache` 等模块驱动。代码里有两处明确说明这一点。

#### 4.2.2 核心流程

五级流水线的数据流与信号前缀对应关系：

```text
            ┌────────────────────── zipcore 内部 ──────────────────────┐
外部的       │  #1 Prefetch      #2 Decode      #3 Read-Operand         │
pfcache  ───►│  pf_pc/new_pc ──► dcd_*  ──────► op_*  (读 regset)        │
(在外壳里)   │                                                  │        │
             │  #4 ALU / Memory / Divide（合级，四路并行）       │        │
             │     cpuops(ALU)  ◄─┐                              │        │
             │     div          ◄─┤ set_cond 决定是否真正写回     │        │
             │     访存请求 ──────┘                              │        │
             │     o_mem_* ──► 外部的 memops/dcache ──► i_mem_*  │        │
             │                                                  ▼        │
             │  #5 Write-back（四级可提交：ALU/Mem/Div/FPU）  wr_*        │
             │     regset[wr_reg_id] <= wr_gpreg_vl                      │
             └──────────────────────────────────────────────────────────┘
```

要点：

- 信号前缀基本就是阶段名缩写：`pf_`（prefetch）、`dcd_`（decoded）、`op_`（operand）、`alu_`（ALU 级）、`mem_`（访存级）、`wr_`（write-back）。读源码时看到一个陌生信号，先看前缀就知道它属于哪一级。
- 第 4 级的「条件执行」在这里落地：`set_cond = ((op_F[7:4] & op_Fl[3:0]) == op_F[3:0])`，即把指令的 3 位条件码（在 op 级展开成 7 位掩码/值 `op_F`）与当前标志 `op_Fl` 比对，决定这条指令的结果是否允许写回。
- 第 5 级**不允许停顿**——结果一旦就绪必须当周期写回（注释明确这么说）。所以反压都体现在前四级。

#### 4.2.3 源码精读

**(a) 寄存器组声明**

- [rtl/core/zipcore.v:L166](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L166) —— `reg [31:0] regset [0:(OPT_USERMODE)? 31:15];`。注意数组上界依赖 `OPT_USERMODE`：开用户模式时是 32 个（supervisor 组 0–15 + user 组 16–31，第 5 位地址即 GIE），关闭时只有 16 个。这正是 u2-l1 讲的「GIE 兼作寄存器地址第 5 位」的硬件落地。标志位 `flags`/`iflags`（用户/监管各一套 Z/C/N/V）声明在 [L170](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L170)。

**(b) 五级的变量声明分块（最重要的路标）**

`zipcore.v` 用形如 `PIPELINE STAGE #N :: 名称` 的注释把每级变量圈起来。这五块是阅读全文件时的「锚点」：

- [rtl/core/zipcore.v:L190-L213](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L190-L213) —— **STAGE #1 :: Prefetch**。声明取指 PC（`pf_pc`）、`new_pc`、`clear_pipeline`。注意 `clear_pipeline = new_pc`，即「新 PC」会触发整条流水线的冲刷。
- [rtl/core/zipcore.v:L215-L245](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L215-L245) —— **STAGE #2 :: Instruction Decode**。声明 `dcd_opn`（操作码）、`dcd_A/dcd_B/dcd_R`（源/目的寄存器号）、`dcd_F`（条件码）、`dcd_I`（立即数）、`dcd_ALU/dcd_M/dcd_DIV/dcd_FP`（这条指令走哪条执行通路）、`dcd_early_branch` 等。
- [rtl/core/zipcore.v:L249-L291](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L249-L291) —— **STAGE #3 :: Read Operands**。声明 `op_valid`、`op_Av/op_Bv`（读出的两个操作数）、`op_R`（目的寄存器号，跨级传到写回）、`op_F`（条件码掩码）、`op_pipe` 等。
- [rtl/core/zipcore.v:L294-L337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L294-L337) —— **STAGE #4 :: ALU / Memory**。声明 `alu_result/alu_flags/alu_valid`、`mem_ce/mem_stalled`、`div_*`、`fpu_*`、`set_cond`、`wr_index`（选择写回源）等。
- [rtl/core/zipcore.v:L340-L363](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L340-L363) —— **STAGE #5 :: Write-back**。声明 `wr_reg_ce`、`wr_reg_id`、`wr_flags_ce`、`wr_gpreg_vl/wr_spreg_vl` 等。

**(c) 全局使能与各级停顿**

- [rtl/core/zipcore.v:L373-L374](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L373-L374) —— `master_ce`：全局时钟使能，halt/写 CC 挂起/break/sleep 时关闭。
- [rtl/core/zipcore.v:L596-L605](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L596-L605) —— `master_stall`：把所有需要停顿的情形（除法忙、ALU 忙、内存忙、单步已走、非法、break、中断等）汇成一个总停顿。
- 各级停顿：第 2 级 `dcd_stalled`（[L388-L396](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L388-L396)）、第 3 级 `op_stall`（[L449-L472](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L449-L472)）、第 4 级 ALU 的 `alu_ce`/`alu_stall`（[L516-L522](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L516-L522)）与访存 `mem_ce`（[L553-L554](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L553-L554)）、`mem_stalled`（[L559-L589](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L559-L589)）。冒险停顿的具体推导见 u3-l7。

**(d) 取指与访存「在内核之外」的两处证据**

- [rtl/core/zipcore.v:L624-L637](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L624-L637) —— 内核只输出请求地址 `o_pf_request_address`、新 PC `o_pf_new_pc`、就绪握手 `o_pf_ready`，并接收外部送回的 `i_pf_instruction`/`i_pf_valid`。真正去内存取指的缓存逻辑不在这里。
- [rtl/core/zipcore.v:L1919-L1928](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1919-L1928) —— 注释直言 `This logic is now managed outside the ZipCore`；内核把访存请求 `o_mem_ce/o_mem_addr/o_mem_data/o_mem_op/o_mem_reg` 发给外部控制器，再接收 `i_mem_valid/i_mem_result/i_mem_wreg` 回送的结果。

**(e) 第 4 级：实例化的三个子模块 + 条件执行**

- [rtl/core/zipcore.v:L649-L695](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L649-L695) —— 实例化译码器 `idecode instruction_decoder(...)`（第 2 级的主力）。
- [rtl/core/zipcore.v:L1506-L1523](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1506-L1523) —— 实例化 ALU `cpuops doalu(...)`（第 4 级 ALU 通路）。
- [rtl/core/zipcore.v:L1534-L1545](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1534-L1545) —— 实例化除法器 `thedivide`（第 4 级除法通路）。FPU 在 [L1568-L1594](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1568-L1594) 基本是占位。
- [rtl/core/zipcore.v:L1599](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1599) —— `set_cond = ((op_F[7:4]&op_Fl[3:0])==op_F[3:0]);`：条件执行的最终判决。`op_F` 的高 4 位是「关心哪些标志」，低 4 位是「这些标志应等于什么」，与当前标志 `op_Fl` 按位与再比较。`op_F` 由 op 级把 3 位 `dcd_F`（即指令的 `Cnd` 字段）展开成 7 位掩码/值，见 [L1018-L1027](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1018-L1027)（8 种条件 `.Z/.NZ/.LT/.GE/.C/.NC/.V` 及无条件）。

**(f) 第 5 级：写回是「四级可提交」**

- [rtl/core/zipcore.v:L2225-L2236](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2225-L2236) —— 注释明确：写回级**不允许停顿**，结果就绪必须当周期提交。
- [rtl/core/zipcore.v:L2262-L2269](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2262-L2269) —— `wr_reg_ce`：调试写、内存返回、ALU/除法/FPU 有效，任一发生即触发写回。
- [rtl/core/zipcore.v:L1677-L1708](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1677-L1708) —— `wr_index`：3 位选择写回值来自哪一路（内存/ALU/除法/FPU/调试）。
- [rtl/core/zipcore.v:L2332-L2340](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2332-L2340) —— `wr_gpreg_vl`：按 `wr_index` 从 `dbg_val/i_mem_result/div_result/fpu_result/alu_result` 里挑出要写的值。
- [rtl/core/zipcore.v:L2354-L2367](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2354-L2367) —— 真正写寄存器组：`if (wr_reg_ce) regset[wr_reg_id] <= wr_gpreg_vl;`。标志位的写回在 [L2397-L2403](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2397-L2403) 与 [L2453-L2468](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2453-L2468)（`flags`/`iflags` 两套）。

#### 4.2.4 代码实践

这是本讲的主实践，对应任务要求：**找出每级对应的 always 块/信号前缀，画信号流草图，并标注 `OPT_CIS` 控制什么**。

1. **实践目标**：建立「信号前缀 ↔ 流水线级」的反射，并用一张草图把五级串起来。
2. **操作步骤**：
   - 在 `zipcore.v` 里依次跳转到 [L190](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L190)、[L215](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L215)、[L249](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L249)、[L294](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L294)、[L340](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L340) 这五个 `PIPELINE STAGE #N` 注释块。
   - 在每块里挑一个带 `r_` 前缀、在 `always @(posedge i_clk)` 里被赋值的寄存器（例如 `r_op_R`、`r_alu_pc`），确认它是「从上一级流到这一级」的流水线寄存器。
   - 搜索 `OPT_CIS`，定位它出现的 `generate` 块。
   - 用纸笔或文本画一张从 `i_pf_instruction` → `dcd_*` → `op_*` → `alu_*/mem/div` → `wr_*` → `regset` 的草图。
3. **需要观察的现象**：
   - 每一级的核心寄存器前缀稳定，跨级传递的信号在下一级被重新命名（如 `dcd_R` → op 级的 `r_op_R` → `alu_reg`）。
   - `OPT_CIS` 出现在 `op_phase` 与 `alu_phase` 两个 `generate` 块里。
4. **预期结果**：你的草图应能回答——「一条 ADD 指令的结果，从 `alu_result` 到 `regset[...]` 中间经过了哪几个信号？」答案链路：`op_Av/op_Bv` → `cpuops` 算出 `alu_result`（[L1506](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1506)）→ `set_cond` 通过（[L1599](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1599)）→ `wr_index` 选 ALU 路（[L2332](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2332)）→ `wr_reg_ce` 拉高 → `regset[wr_reg_id] <= wr_gpreg_vl`（[L2357](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2357)）。
5. **关于 `OPT_CIS`**：它控制**压缩指令的两拍执行**。一条压缩指令（CIS）把两条子指令塞进一个 32 位字，译码器用 `dcd_phase` 标识当前是「前半」还是「后半」，内核据此把它当成两条指令分两拍送进执行级。`OPT_CIS=1` 时才有 `op_phase`（[L1338-L1358](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1338-L1358)）与 `alu_phase`（[L1635-L1653](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1635-L1653)）跟踪；`OPT_CIS=0` 时这两信号恒为 0，CPU 不认识压缩指令。`CPU_PHASE_BIT=13`（[L143](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L143)）是 CC 里标记「处于 CIS 后半」的位。
6. **运行说明**：纯源码阅读即可完成；如需可视化，可参考 u5-l3 用 Verilator 跑 `zipsys_tb` 并 dump 波形，观察 `dcd_phase` 在压缩指令上拉高的现象（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：信号 `op_R`、`alu_reg`、`wr_reg_id` 三者是什么关系？

**参考答案**：它们是「目的寄存器号」在三个相邻流水级的「化身」。译码级得到 `dcd_R`；读操作数级把它锁存为 `r_op_R`（对外即 `op_R`，[L860/L876](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L856-L876)）；进入执行级时再锁成 `alu_reg`（[L1658-L1674](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1658-L1674)），用于除法/FPU 这类多周期操作记住「结果要写回哪个寄存器」；到写回级由 `wr_reg_id`（[L2310-L2318](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2310-L2318)，内存返回时也可被 `i_mem_wreg` 覆盖）最终选定写入位置。这是典型的「同一份信息逐级打拍」。

**练习 2**：为什么第 5 级写回被设计成「不允许停顿」？

**参考答案**：因为 ALU/除法/访存的结果在 `*_valid` 拉高的那个周期才有效，若写回级停顿一拍，结果就会丢失（这些单元下一拍可能已经处理下一条指令）。所以代码用注释（[L2232](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2232)）强调「results are written back at all cost」——sleep、halt、调试模式都不能阻止写回。所有反压因此只能体现在前四级。

**练习 3**：`zipcore` 内部实例化了哪几个子模块？为什么没有 `pfcache`、`memops`？

**参考答案**：功能上只实例化了 `idecode`、`cpuops`、`div` 三个（外加 FPU 占位）。没有取指/访存模块，是因为取指缓存和访存控制器被刻意留在「外壳」一侧（`zipwb`/`zipaxil` 等），通过 `i_pf_*`/`o_mem_*` 端口与内核相连（见 [L624](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L624) 与 [L1919](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1919) 的注释）。这样同一份内核可以搭配不同总线协议和缓存策略——这正是四种顶层封装能包裹同一个 `zipcore` 的根本原因（承接 u1-l3）。

---

### 4.3 spec.tex 的 Pipeline Operation 子节：规范视角的五级流水线

#### 4.3.1 概念说明

读 RTL 源码能知道「怎么做」，但读规范能知道「为什么这么做、对外承诺了什么」。`spec.tex` 里对流水线的描述有两处：

1. **引言的架构清单**（编译进 PDF）：一句话点名五级。
2. **「Pipeline Operation」专节**（当前被 `\iffalse ... \fi` 注释掉，未编译进 PDF）：对每一级职责、冒险、停顿周期的详细叙述。

第二处虽然在当前 PDF 里看不到，但它的文字与当前代码仍然吻合，是理解设计意图的宝贵资料——只是读者要心里有数：它是「源码里的草稿」，不是正式发布的契约。

#### 4.3.2 核心流程

规范把第 4 级描述成「四条并排的执行 track」：ALU（简单指令）、MemOps（load/store）、Divide、（未来的）FPU。并给出几条关键承诺：

- **不支持乱序执行**：内存单元一停，所有指令都停；除法/浮点忙时同理。
- **store 可与非访存指令并发，load 不行**：load 必须等结果写回寄存器堆才能放后续指令进去读。
- **条件码在 ALU/除法/FPU 完成时设置，访存不设置条件码**。
- **写回是「四入口」**：ALU、内存、除法、FPU 都可能提交结果。

这些承诺在 4.2 节的源码里都有对应实现。

#### 4.3.3 源码精读

- [doc/src/spec.tex:L298-L306](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L298-L306) —— 引言里对五级流水线的权威一句话描述，并指向架构图 `fig:cpu`。这是最简明的「分级定义」。
- [doc/src/spec.tex:L1552-L1607](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1552-L1607) —— **「Pipeline Operation」专节全文**（注意首行 `\iffalse`、末尾 `\fi` 在 L1787，当前未编译进 PDF）。它逐级展开了五级的职责，其中明确写第 5 级「quad-entrant: either the ALU, the memory, the divide, or the FPU may commit a result」——与代码里 `wr_index` 的四路选择一一对应。
- [doc/src/spec.tex:L1618-L1764](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1618-L1764) —— **「Pipeline Stalls」子节**：列举各种停顿情形，例如取指缓存耗尽、分支后 4 拍重载、读寄存器同时加立即数需 1 拍间隔、读 CC 前需标志稳定、load 后整条流水停顿等。这些是 u3-l7 冒险停顿的主题，本讲只做对照。

把规范与代码对照的一个例子：规范说「条件码在 ALU/除法/FPU 完成时设置，访存不设置」。代码里 `wr_flags_ce`（[L2397-L2403](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2397-L2403)）的 `casez(wr_index)` 只在 `3'b010`(ALU)/`3'b011`(div)/`3'b1??`(fpu) 时给 `wr_flags` 赋值，`3'b001`(内存) 落到 `default: wr_flags = 0`——访存确实不改条件码。

#### 4.3.4 代码实践

1. **实践目标**：把规范的「承诺」逐条对应到代码信号。
2. **操作步骤**：
   - 读 [spec.tex:L1552-L1607](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1552-L1607)，把每一条对某级的描述抄成一行。
   - 在 `zipcore.v` 里为每行找一个信号或 `generate` 块作为证据。
3. **需要观察的现象**：规范的每条描述都能找到代码锚点；找不到的就要警惕「规范与代码漂移」（毕竟这一节被 `\iffalse` 了）。
4. **预期结果**（待本地核对）：

   | 规范描述（spec.tex） | 代码证据（zipcore.v） |
   |----------------------|----------------------|
   | Prefetch：缓存缺失时产生 stall | 由外部 prefetch 模块产生，内核经 `i_pf_valid` 感知（[L624](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L624)） |
   | Read Operands：源操作数未就绪且有立即数时停顿 | `dcd_B_stall` 中 `(!dcd_zI)&&...`（[L1430-L1473](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1430-L1473)） |
   | 访存不设置条件码 | `wr_flags` 的 `default`（[L2429-L2434](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L2429-L2434)） |
   | 写回四入口 | `wr_index`（[L1677-L1708](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1677-L1708)） |
5. **运行说明**：纯阅读对照，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`spec.tex` 的「Pipeline Operation」节为什么被 `\iffalse` 包起来？读它时要注意什么？

**参考答案**：作者在节首注释 `%% Do I still need this section?` 并标注 FIXME，说明这节描述（尤其涉及某些 prefetch 和 MMU 加入后会过时）尚未与最新实现严格对齐，故暂时退出编译。读它时要把每条结论回到代码里验证，不能当作正式契约照搬；权威的一句话定义仍以引言 [L298-L306](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L298-L306) 为准。

**练习 2**：规范说「store 可与后续非访存指令并发，load 不行」。请用代码里 `mem_stalled` 的逻辑解释为什么 load 会拖住整条流水线。

**参考答案**：`mem_stalled`（[L559-L589](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L559-L589)）依赖 `i_mem_busy`/`i_mem_pipe_stalled` 等来自外部访存控制器的信号，而 `master_stall`（[L596-L605](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L596-L605)）又包含 `i_mem_busy`。load 必须把数据写回寄存器堆，后续指令才读到正确值，所以 load 在途时整条流水线停；store 不需要回写结果，外部控制器可以「后台」完成，因此不强制拖住 ALU。具体时序图见规范的 store/load 图（u3-l6、u3-l7 详述）。

---

## 5. 综合实践

把本讲三块知识串起来，完成一份「`zipcore` 速查卡」。

**任务**：针对默认配置（`OPT_PIPELINED=1, OPT_CIS=1, OPT_DIV=1, OPT_MPY=0, OPT_USERMODE=1, OPT_DBGPORT=1`），产出三张小表：

1. **参数—电路形态表**：列出上述 6 个参数各自「开启了哪块电路 / 关闭了哪块电路」，每项给一个 `zipcore.v` 行号证据。
2. **五级流水线—信号前缀表**：每一级写出「代表信号前缀」「该级是否可停顿（给出 stall 信号名）」「该级实例化的子模块（若有）」。
3. **数据通路连线**：写出一条「`LDI R1,5` 立即数装入指令」从取指到写回经过的关键信号序列（提示：`LDI` 是特例格式，不走 ALU 的运算通路，立即数在译码/读操作数级直接形成；可用 `dcd_I`/`op_Bv`/`wr_gpreg_vl` 等信号串起来）。

**验收**：第 1、2 张表应与 4.1、4.2 节给出的行号完全对得上；第 3 张表的信号链路应能在源码里逐段找到。完成后，你应当能在不看讲义的情况下，指着 `zipcore.v` 的任意一段说出「这属于第几级、在做什么」。

> 关于 `LDI` 的细节（它是特例格式、无 `Cnd` 字段）在 u2-l2 已讲过；本实践只要求把它在流水线里的「路径」串起来，不要求复述编码。

## 6. 本讲小结

- `zipcore` 是被四种顶层外壳包裹的同一个内核；它用一长串 `OPT_*` 参数在综合期裁剪电路（乘除、缓存、用户模式、调试端口等均可关闭）。
- 它是五级流水线：**取指 → 译码 → 读操作数 → 执行+访存（合级）→ 写回**；`zipcore.v` 用 `PIPELINE STAGE #N` 注释把每级变量分块，是阅读全文件的路标。
- 信号前缀即阶段名：`pf_`/`dcd_`/`op_`/`alu_`/`mem_`/`wr_`。每一级有 `*_ce`（前移）与 `*_stall`（反压），之上还有全局 `master_ce`/`master_stall`。
- **关键事实**：取指缓存与访存控制器不在 `zipcore` 内部，只实例化了 `idecode`/`cpuops`/`div`；取指与访存经端口接在外壳里——这正是同一内核能搭配多种总线/缓存的原因。
- 条件执行在第 4 级由 `set_cond` 判决，写回级是「ALU/内存/除法/FPU 四入口」且不允许停顿。
- `OPT_CIS` 控制压缩指令的两拍执行，靠 `op_phase`/`alu_phase` 跟踪前后半。
- `spec.tex` 引言的 [L298-L306](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L298-L306) 是权威分级定义；详细的「Pipeline Operation」节当前被 `\iffalse` 注释掉，读时需对照代码。

## 7. 下一步学习建议

本讲只搭了 `zipcore` 的骨架，接下来的讲义会逐级拆开：

- **u3-l2 取指模块族**：钻进 `zipcore` 之外、被外壳实例化的 `prefetch`/`dblfetch`/`pfcache`/`pffifo`，看 `i_pf_instruction`/`i_pf_valid` 背后到底是什么。
- **u3-l3 指令译码 `idecode`**：细看 `dcd_*` 信号是如何从 32 位指令字里抠出来的，以及 CIS 的译码差异。
- **u3-l4 ALU 运算单元 `cpuops`**：看 `op_Av/op_Bv` 进了 `doalu` 之后如何算出 `alu_result` 和 Z/C/N/V。
- **u3-l6 访存模块族**：看 `o_mem_*` 那一头连接的 `memops`/`pipemem`/`dcache`。
- **u3-l7 流水线冒险与停顿**：把本讲点到为止的 `master_stall`/`dcd_*_stall`/`mem_stalled` 完整推导一遍。

建议在进入 u3-l2 之前，先把本讲「综合实践」的三张表做完——它会成为你读后续讲义时的随身地图。
