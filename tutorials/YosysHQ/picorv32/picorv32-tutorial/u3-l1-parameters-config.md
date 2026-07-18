# 模块参数：用 Verilog parameter 配置 CPU

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 PicoRV32 模块开头那二十多个 `parameter` 各自开关的是什么功能、关掉它会损失什么。
- 区分三类参数：**功能开关**（决定有没有某块电路）、**时序/面积取舍**（同一指令集用不同实现换取面积或频率）、**系统级常量**（复位地址、栈地址、中断屏蔽位等 32 位配置）。
- 读懂 `parameter` 在源码里如何驱动 `localparam`、`generate` 和 `if` 分支，从而理解「改一个参数，综合后实际改变了哪些电路」。
- 针对一个面积优先的目标，自己挑出一组参数，并逐项解释它对 LUT 数和 CPI 的影响，与 README 的 small / regular / large 三档配置对照。

本讲只看 CPU 的「外观配置」，不进入译码器和状态机内部。下一篇（u3-l2）才会逐组讲解端口。

## 2. 前置知识

在动手之前，先建立三个直觉。

**第一，什么是 Verilog `parameter`。** `parameter` 是在模块开头声明的「编译期常量」。综合工具在展开模块时会把 `parameter` 的值直接替换进代码，因此 `if (ENABLE_COUNTERS)`、`generate if (ENABLE_MUL)` 这类写法在综合后会被当成常量折叠——开着的分支变成真实电路，关掉的分支被完全删除。这跟 C 语言的 `#ifdef` 很像：不会浪费任何门电路。PicoRV32 正是靠这一机制做到「同一份 `picorv32.v` 综合出从 761 到 2019 LUT 不等的核」。

**第二，面积、频率、性能是三方权衡。** PicoRV32 的定位是「尺寸优先 + 高 fmax」，不是高性能（平均 CPI≈4）。所以它的很多参数都在问同一个问题：**为了把某条路径做快或者把某块电路做小，你愿意付出什么代价？** 例如把移位器从「一次移 1 位」升级为「一次移 4 位」会加速移位但增加 LUT；把 ALU 数据通路切成两拍会提高 fmax 但每条 ALU 指令多花 1 个周期。

**第三，三类参数的边界。** 把参数分三类记忆会很有用：

| 类别 | 代表参数 | 改变它的后果 |
| --- | --- | --- |
| 功能开关 | `ENABLE_COUNTERS`、`ENABLE_MUL`、`ENABLE_IRQ` | 决定某块电路**是否存在**，关掉=不支持该功能（可能触发 trap） |
| 时序/面积取舍 | `TWO_STAGE_SHIFT`、`BARREL_SHIFTER`、`TWO_CYCLE_ALU` | 功能不变，只换**实现方式**，影响 LUT/周期数/fmax |
| 系统级常量 | `PROGADDR_RESET`、`STACKADDR`、`MASKED_IRQ` | 不改变电路结构，只是给若干寄存器一个**复位初值或固定位** |

如果你已经学过 u1-l3（跑通 `testbench_ez`），你应该已经知道 `PROGADDR_RESET` 决定了复位后从哪个地址取指——本讲会把它放回整张参数表里完整讲一遍。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | CPU 全部 RTL。开头 `module picorv32 #(...)` 块集中声明所有 `parameter`；之后用 `localparam`、`generate`、`if` 把这些参数翻译成实际电路。 |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | 「Verilog Module Parameters」「Cycles per Instruction」「Evaluation」三节是参数的权威说明文档，并给出 small/regular/large 三档配置与实测 LUT/频率。 |

记忆要点：参数声明只在 [picorv32.v:62-89](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62-L89) 这一段，**`picorv32_axi` 和 `picorv32_wb` 两个总线变体重复了同一份参数表**并把它们原样转发给内部的 `picorv32`，所以无论你用哪种总线变体，配置方式都一样。

## 4. 核心概念与源码讲解

### 4.1 功能开关参数：ENABLE_* 家族

#### 4.1.1 概念说明

`ENABLE_*` 系列参数回答的是「要不要这块电路」。开=综合进对应的硬件并支持相关指令；关=硬件被删除，遇到对应指令时按情况要么当 nop、要么触发非法指令 trap。这是「用一份源码裁出大中小三种核」最直接的手段。

需要认识的开关按子系统分组如下：

- **计数器**：`ENABLE_COUNTERS`、`ENABLE_COUNTERS64`——控制 `RDCYCLE[H]`、`RDTIME[H]`、`RDINSTRET[H]` 这类性能计数器指令。
- **寄存器堆规模**：`ENABLE_REGS_16_31`（是否实现 x16..x31，关掉即向 RV32E 靠拢）、`ENABLE_REGS_DUALPORT`（寄存器堆 1 个还是 2 个读端口）。
- **协处理器 / M 扩展**：`ENABLE_PCPI`、`ENABLE_MUL`、`ENABLE_FAST_MUL`、`ENABLE_DIV`。
- **中断**：`ENABLE_IRQ`、`ENABLE_IRQ_QREGS`、`ENABLE_IRQ_TIMER`。
- **压缩指令**：`COMPRESSED_ISA`——开启 RISC-V C 扩展。
- **杂项**：`ENABLE_TRACE`、`REGS_INIT_ZERO`、`LATCHED_MEM_RDATA`。

#### 4.1.2 核心流程

参数到电路的翻译分三层，自顶向下：

1. **声明层**：在 [picorv32.v:62-89](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62-L89) 集中给出所有参数及默认值。
2. **派生层**：用 `localparam` 把几个参数算成具体的位宽/规模，例如寄存器堆大小、PCPI 是否需要。
3. **实现层**：用 `generate if` 实例化整块 IP（如乘法核），或在 `always` 块里用 `if (ENABLE_XXX)` 让综合器折叠掉死分支。

最典型的派生关系在寄存器堆规模上。三个 `localparam` 把 `ENABLE_REGS_16_31` 和 `ENABLE_IRQ` 算成寄存器堆的真实容量与索引位宽：

```verilog
localparam integer irqregs_offset = ENABLE_REGS_16_31 ? 32 : 16;
localparam integer regfile_size = (ENABLE_REGS_16_31 ? 32 : 16) + 4*ENABLE_IRQ*ENABLE_IRQ_QREGS;
localparam integer regindex_bits = (ENABLE_REGS_16_31 ? 5 : 4) + ENABLE_IRQ*ENABLE_IRQ_QREGS;
```

即：开 `ENABLE_REGS_16_31` 有 32 个通用寄存器，关掉只剩 16 个（x0..x15）；开中断且开 q 寄存器（`ENABLE_IRQ_QREGS`）则额外加 4 个用于中断上下文的 q0..q3。`regfile_size` 直接决定下面这行实际分配多少个 32 位寄存器：

```verilog
reg [31:0] cpuregs [0:regfile_size-1];
```

所以「关 `ENABLE_REGS_16_31`」并不是把高 16 个寄存器闲置，而是**根本不分配它们**，省下整整一半寄存器存储。

#### 4.1.3 源码精读

**计数器开关**直接出现在译码器里，用来决定要不要识别 `RDCYCLE` 等指令：

```verilog
instr_rdcycle  <= (...) && ENABLE_COUNTERS;
instr_rdcycleh <= (...) && ENABLE_COUNTERS && ENABLE_COUNTERS64;
```

见 [picorv32.v:1080-1084](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1080-L1084)。当 `ENABLE_COUNTERS=0` 时，这几个一位译码信号恒为 0，`RDCYCLE` 一类指令不会被识别，于是落入非法指令处理路径（见 4.2 节 `CATCH_ILLINSN`），最终触发 trap。README 也提醒：严格来说这些指令对 RV32I 不是可选的，但调试完成后通常用不到（[README.md:151-160](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L151-L160)）。

**协处理器 / M 扩展**用 `generate` 整块实例化，是最清晰的「开=多一块 IP」的例子：

```verilog
generate if (ENABLE_FAST_MUL) begin : ... picorv32_pcpi_fast_mul ... end
   else if (ENABLE_MUL)        begin : ... picorv32_pcpi_mul      ... end
generate if (ENABLE_DIV)       begin : ... picorv32_pcpi_div      ... end
```

见 [picorv32.v:272-305](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L272-L305)。注意优先级：`ENABLE_FAST_MUL` 与 `ENABLE_MUL` 同时开时，快速乘法器优先（README 在 [README.md:257-258](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L257-L258) 说明 `ENABLE_MUL` 会被忽略）。而这三个开关连同 `ENABLE_PCPI` 又被汇成一个派生信号：

```verilog
localparam WITH_PCPI = ENABLE_PCPI || ENABLE_MUL || ENABLE_FAST_MUL || ENABLE_DIV;
```

见 [picorv32.v:169](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L169)。`WITH_PCPI` 在核心里用来决定是否启用「未识别指令派发给协处理器」的整条逻辑——只开 `ENABLE_MUL` 而不开 `ENABLE_PCPI` 时，外部 PCPI 接口仍不可用，但内部乘法核照常工作（[README.md:245-264](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L245-L264)）。

**寄存器初始化** `REGS_INIT_ZERO` 用一个 `initial` 块把所有寄存器清零，主要用于仿真和形式化验证：

```verilog
if (REGS_INIT_ZERO) begin
    for (i = 0; i < regfile_size; i = i+1)
        cpuregs[i] <= 0;
end
```

见 [picorv32.v:206-211](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L206-L211)。FPGA 上 `initial` 块会被综合成上电初值，所以它在真实硬件里也有效。

#### 4.1.4 代码实践

**实践目标：** 验证 `ENABLE_REGS_16_31` 与 `ENABLE_IRQ`/`ENABLE_IRQ_QREGS` 如何改变寄存器堆的真实大小。

**操作步骤：**

1. 打开 [picorv32.v:165-167](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L165-L167)，记下 `regfile_size` 的公式。
2. 对四种组合手算 `regfile_size` 与 `regindex_bits`：
   - A：`ENABLE_REGS_16_31=1, ENABLE_IRQ=0`（默认、无中断）
   - B：`ENABLE_REGS_16_31=0, ENABLE_IRQ=0`（RV32E 风格）
   - C：`ENABLE_REGS_16_31=1, ENABLE_IRQ=1, ENABLE_IRQ_QREGS=1`（默认中断配置）
   - D：`ENABLE_REGS_16_31=1, ENABLE_IRQ=1, ENABLE_IRQ_QREGS=0`（中断用 gp/tp 而非 q 寄存器）
3. 把结果填进表格。

**需要观察的现象 / 预期结果：**

| 组合 | regfile_size | regindex_bits |
| --- | --- | --- |
| A | 32 | 5 |
| B | 16 | 4 |
| C | 36（32+4） | 6 |
| D | 32 | 5 |

由此可直观看到：关 `ENABLE_REGS_16_31` 直接砍掉一半寄存器（B 行）；开 q 寄存器会多 4 个寄存器并把索引加宽 1 位（C 行）；改用 gp/tp 存中断上下文则不增加寄存器（D 行，对应 [README.md:271-277](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L271-L277)）。这一步是纯源码阅读，无需综合即可完成。

#### 4.1.5 小练习与答案

**练习 1：** 如果只想用外部 PCPI 协处理器实现自定义指令、又不想让核内多出乘除法 IP，`ENABLE_PCPI`、`ENABLE_MUL`、`ENABLE_FAST_MUL`、`ENABLE_DIV` 该如何设？

**答案：** 设 `ENABLE_PCPI=1`，其余三个为 0。此时 `WITH_PCPI=1`，核心会把未识别指令派发到外部 PCPI 接口，但不会实例化任何内置乘除法核（[picorv32.v:169](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L169)、[README.md:239-243](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L239-L243)）。

**练习 2：** 为什么 `ENABLE_IRQ=0` 时，`ENABLE_IRQ_QREGS` 和 `ENABLE_IRQ_TIMER` 无论设什么都没用？

**答案：** 因为 `regfile_size`、`regindex_bits` 公式里都乘了 `ENABLE_IRQ`，译码器里 `instr_timer <= ... && ENABLE_IRQ && ENABLE_IRQ_TIMER`（[picorv32.v:1093](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1093)）等条件也都被 `ENABLE_IRQ` 短路。README 同样说明这两个子开关在 `ENABLE_IRQ=0` 时恒为关闭（[README.md:279](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L279)、[README.md:285](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L285)）。

### 4.2 时序/面积取舍参数：TWO_*、BARREL_SHIFTER、CATCH_*

#### 4.2.1 概念说明

这一组参数**不改变指令集**（该支持的指令照样支持），只改变「同一条指令用什么电路实现」。它们是面积 ↔ 周期数 ↔ fmax 三方博弈的主战场：

- `TWO_STAGE_SHIFT`（默认 1）：移位分两级，先移 4 位再移 1 位，加速移位但略增面积。
- `BARREL_SHIFTER`（默认 0）：换成桶形移位器，让移位和普通 ALU 运算一样快，面积再大一些。
- `TWO_CYCLE_COMPARE`（默认 0）：给比较/分支路径插一拍寄存器，缩短关键路径、抬高 fmax，但条件分支多 1 周期。
- `TWO_CYCLE_ALU`（默认 0）：给整个 ALU 数据通路插一拍，fmax 进一步抬高，但每条用 ALU 的指令多 1 周期。
- `CATCH_MISALIGN` / `CATCH_ILLINSN`（默认均为 1）：保留「地址未对齐」和「非法指令」的检测电路；关掉则省面积，但相应错误不再触发 trap（行为改变，需谨慎）。

`TWO_CYCLE_ALU` 和 `TWO_CYCLE_COMPARE` 特别适合配合综合工具的 **retime / register balancing** 使用——README 在两处都加了同样备注（[README.md:209-210](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L209-L210)、[README.md:218-219](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L218-L219)）。

#### 4.2.2 核心流程

以移位为例，最能体现「同一指令、多种实现」。`cpu_state_shift` 状态用一个循环变量 `reg_sh`（剩余移位量）驱动迭代：

```
进入 cpu_state_shift，reg_sh = 移位量 s
while (reg_sh != 0):
    if TWO_STAGE_SHIFT 且 reg_sh >= 4:
        reg_op1 移动 4 位；reg_sh -= 4
    else:
        reg_op1 移动 1 位；reg_sh -= 1
把 reg_op1 写回寄存器，回到 fetch
```

`TWO_STAGE_SHIFT` 决定「能不能一次跨 4 位」。设移位量为 \(s\)（\(0 \le s \le 31\)），开启两级移位时，移位状态的迭代次数为：

\[
N_{\text{shift}}(s) = \left\lfloor \frac{s}{4} \right\rfloor + (s \bmod 4)
\]

其最大值在 \(s=31\) 时取到，\(N_{\text{shift}}(31)=7+3=10\)。把约 4 拍的基础开销（取指、读寄存器、进入 exec/shift、写回）加上去，正好对应 README 给出的「shift operations 4-14 周期」（[README.md:354](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L354)）。若换成 `BARREL_SHIFTER`，移位被并入 ALU 单周期完成，不再有 `cpu_state_shift` 的多拍循环，于是「移位耗时与普通 ALU 运算相同」（[README.md:362-363](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L362-L363)）。

> 说明：上面的迭代次数 \(N_{\text{shift}}(s)\) 可直接从源码公式推出；「基础开销约 4 拍」是 README 实测的近似值，精确到拍数待本地验证。

#### 4.2.3 源码精读

**两级移位**的核心就在 `cpu_state_shift` 的三分支判断里：

```verilog
cpu_state_shift: begin
    ...
    if (reg_sh == 0) begin
        reg_out <= reg_op1; ... cpu_state <= cpu_state_fetch;     // 移完，写回
    end else if (TWO_STAGE_SHIFT && reg_sh >= 4) begin
        case (1'b1)                                                // 一次移 4 位
            instr_slli || instr_sll:  reg_op1 <= reg_op1 << 4;
            instr_srli || instr_srl:  reg_op1 <= reg_op1 >> 4;
            instr_srai || instr_sra:  reg_op1 <= $signed(reg_op1) >>> 4;
        endcase
        reg_sh <= reg_sh - 4;
    end else begin
        case (1'b1)                                                // 一次移 1 位
            ...
        endcase
        reg_sh <= reg_sh - 1;
    end
end
```

见 [picorv32.v:1829-1852](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1829-L1852)。把 `TWO_STAGE_SHIFT` 设为 0，中间那个 `else if` 永远不成立，于是所有移位都退化为「一次 1 位」，面积更小但 \(s=31\) 时要迭代 31 次。

**桶形移位器**则在 ALU 的组合逻辑里把移位并入单周期：

```verilog
BARREL_SHIFTER && (instr_sll || instr_slli): ...   // 左移并入 ALU
BARREL_SHIFTER && (instr_srl || instr_srli || instr_sra || instr_srai): ...
```

见 [picorv32.v:1280-1282](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1280-L1282)。注意 `is_slli_srli_srai && !BARREL_SHIFTER` 才会进入 `cpu_state_shift`（[picorv32.v:1704](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1704)）——开了桶形移位器后移位指令直接走 ALU，根本不进移位状态机。

**两拍 ALU** 用 `generate` 在数据通路上插入一级寄存器：

```verilog
generate if (TWO_CYCLE_ALU) begin ... end
```

见 [picorv32.v:1229](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1229)，并通过 `alu_wait`/`alu_wait_2` 在状态机里多等一拍（[picorv32.v:1807](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1807)）。`TWO_CYCLE_COMPARE` 走类似机制，但只对比较/分支指令生效。

**CATCH_*** 关掉的是「错误检测」电路。例如未对齐访存检测：

```verilog
if (CATCH_MISALIGN && resetn && (mem_do_rdata || mem_do_wdata)) begin ... end
```

见 [picorv32.v:1922](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1922)。`CATCH_MISALIGN=0` 时这段比较与 trap 逻辑被折叠掉，省 LUT，但未对齐访问不再报错（[README.md:225-228](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L225-L228)）。`CATCH_ILLINSN` 同理，但 README 特别提醒：即便关掉它，`EBREAK` 仍会 trap，只是不再以中断形式上报（[README.md:230-237](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L230-L237)）。

#### 4.2.4 代码实践

**实践目标：** 用源码公式预测不同移位配置下，一条 `srli x1, x1, 13` 的移位迭代次数。

**操作步骤：**

1. 在 [picorv32.v:1829-1852](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1829-L1852) 找到 `cpu_state_shift`。
2. 取 \(s=13\)，分别按三种配置套公式：
   - `TWO_STAGE_SHIFT=1`：\(\lfloor 13/4 \rfloor + (13 \bmod 4) = 3 + 1 = 4\) 次迭代。
   - `TWO_STAGE_SHIFT=0`：每次移 1 位，13 次迭代。
   - `BARREL_SHIFTER=1`：不进 `cpu_state_shift`，0 次迭代（并入 ALU 单周期）。

**需要观察的现象 / 预期结果：**

迭代次数依次为 4、13、0。可见两级移位把最坏情况从「移多少位就多少拍」压缩到「≈ 移位量/4」，而桶形移位器则彻底消灭了多拍循环——这就是 README 面积表里 small（关 `TWO_STAGE_SHIFT`）到 large（开 `BARREL_SHIFTER`）LUT 跨度变大的原因之一。精确周期数待本地用 `testbench` + trace 验证。

#### 4.2.5 小练习与答案

**练习 1：** `TWO_CYCLE_ALU=1` 会让哪些指令变慢？为什么说它和 retime 配合最有效？

**答案：** 所有走 ALU 的指令（加减、逻辑、含 `BARREL_SHIFTER` 时的移位）都多花 1 周期。它在 ALU 组合路径中插入了一级寄存器，缩短了关键路径从而抬高 fmax；如果综合流程再开启 retime/register balancing，工具会把这级寄存器沿着组合路径前后移动到最优位置，收益最大（[README.md:212-219](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L212-L219)）。

**练习 2：** 在「绝对不允许漏掉非法指令」的安全场景下，`CATCH_ILLINSN` 能不能为了省面积设为 0？

**答案：** 不能。设为 0 会移除非法指令检测电路，未识别指令不再触发 trap（`EBREAK` 除外）。安全场景必须保持默认 1（[README.md:230-237](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L230-L237)）。PicoRV32 的 small 配置之所以敢关掉它，是因为该配置假设固件已充分调试、不会遇到非法指令。

### 4.3 中断与复位地址参数：系统级 32 位配置

#### 4.3.1 概念说明

这一组都是 32 位 `parameter`，它们**不改变电路结构**，只给若干寄存器规定复位初值或固定位。理解它们的关键是「复位那一刻发生了什么」。

- `PROGADDR_RESET`（默认 0）：复位后 PC 的初值，即第一条指令地址。
- `PROGADDR_IRQ`（默认 0x10）：中断处理程序入口地址。
- `STACKADDR`（默认 0xffffffff）：若不为 0xffffffff，复位时把 `x2`（栈指针 sp）初始化为该值；为 0xffffffff 则不初始化 sp。
- `MASKED_IRQ`（默认 0）：32 位掩码，某位为 1 表示该中断**永久禁用**（软件无法打开）。
- `LATCHED_IRQ`（默认 0xffffffff）：32 位掩码，某位为 1 表示该中断为**边沿触发**（锁存到 pending）；为 0 表示**电平触发**。

回看 u1-l3：`testbench_ez` 用的就是默认 `PROGADDR_RESET=0`，所以 CPU 从地址 0 取第一条指令；u2-l1 的 `sections.lds` 把 `start.o` 强制排在最前，正是为了让复位向量落在地址 0 与 `PROGADDR_RESET` 对齐。

#### 4.3.2 核心流程

复位与中断入口都集中在 `!resetn` 分支里。伪代码：

```
if (!resetn):
    reg_pc       <= PROGADDR_RESET          // 第一条指令地址
    reg_next_pc  <= PROGADDR_RESET
    irq_mask     <= ~0                      // 复位时屏蔽所有中断
    if (~STACKADDR):                         // STACKADDR != 0xffffffff
        把 STACKADDR 写入 x2（sp）
    cpu_state   <= cpu_state_fetch
```

注意 `if (~STACKADDR)` 这个判断：`STACKADDR` 默认是 `32'hffffffff`，按位取反得 0，条件为假，于是**不**初始化 sp；只要把它设成任意非 0xffffffff 的值，条件就为真，CPU 在复位时自动把 sp 设好。README 提醒：RISC-V 调用约定要求 sp 16 字节对齐（RV32I 软浮点为 4 字节，[README.md:321-327](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L321-L327)）。

中断屏蔽则分两层：

- **硬掩码 `MASKED_IRQ`**：在 `maskirq` 指令写 `irq_mask` 时按位 OR 上去，使那些位永远为 1（永远禁用）：
  `irq_mask <= cpuregs_rs1 | MASKED_IRQ;`
- **触发方式 `LATCHED_IRQ`**：计算 pending 时与 `LATCHED_IRQ` 相与，只对「1 的位」做锁存：
  `next_irq_pending = ENABLE_IRQ ? irq_pending & LATCHED_IRQ : 'bx;`
  最后再统一清掉硬掩蔽位：`irq_pending <= next_irq_pending & ~MASKED_IRQ;`

#### 4.3.3 源码精读

**复位块**完整对应 [picorv32.v:1457-1483](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1457-L1483)，关键几行：

```verilog
if (!resetn) begin
    reg_pc <= PROGADDR_RESET;
    reg_next_pc <= PROGADDR_RESET;
    ...
    irq_mask <= ~0;
    ...
    if (~STACKADDR) begin
        latched_store <= 1; latched_rd <= 2; reg_out <= STACKADDR;   // 复位即"写 x2 = STACKADDR"
    end
    cpu_state <= cpu_state_fetch;
end
```

这里 `latched_rd <= 2; reg_out <= STACKADDR;` 借用了通用的「写回寄存器」机制，让复位后第一个 fetch 周期顺手把 `STACKADDR` 写进 x2。`irq_mask <= ~0` 表示复位时所有中断被屏蔽，必须由软件用 `maskirq` 指令主动打开。

中断入口地址 `PROGADDR_IRQ` 则在响应中断、切换 `irq_state` 时被用作 PC：

```verilog
ENABLE_IRQ && irq_state[0]: begin ... current_pc = PROGADDR_IRQ; ... end
```

见 [picorv32.v:1506-1507](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1506-L1507)。这就是「中断到来 → 跳到 `PROGADDR_IRQ`」的硬件实现。

**`MASKED_IRQ` 的硬屏蔽**在 `maskirq` 自定义指令的处理里：

```verilog
ENABLE_IRQ && instr_maskirq: begin
    ...
    reg_out <= irq_mask;                 // 旧掩码作为返回值
    irq_mask <= cpuregs_rs1 | MASKED_IRQ; // 写入新掩码，但 MASKED_IRQ 的位永远置 1
end
```

见 [picorv32.v:1678-1686](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1678-L1686)。也就是说，软件可以把某位写 0 试图打开中断，但 `MASKED_IRQ` 中为 1 的位会被强制拉回 1，从而**永久禁用**。

**`LATCHED_IRQ` 的边沿/电平选择**与最终的 pending 更新：

```verilog
next_irq_pending = ENABLE_IRQ ? irq_pending & LATCHED_IRQ : 'bx;   // 只锁存 LATCHED_IRQ 为 1 的位
...
irq_pending <= next_irq_pending & ~MASKED_IRQ;                      // 再剔除硬屏蔽位
```

见 [picorv32.v:1440](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1440) 与 [picorv32.v:1963](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1963)。配合 README 的解释：某位在 `LATCHED_IRQ` 中为 1 → 即便中断线只拉高 1 拍也会被锁存为 pending（边沿/脉冲触发）；为 0 → 仅当中断线持续高电平才 pending（电平触发，[README.md:303-311](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L303-L311)）。

#### 4.3.4 代码实践

**实践目标：** 给定一组系统级参数，预测复位后 CPU 的初始 PC、sp 与中断屏蔽状态。

**操作步骤：**

1. 假设参数配置为 `PROGADDR_RESET=0x00010000`、`PROGADDR_IRQ=0x00010100`、`STACKADDR=0x00080000`、`MASKED_IRQ=32'h00000006`、`ENABLE_IRQ=1`。
2. 对照 [picorv32.v:1457-1483](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1457-L1483) 与 [picorv32.v:1678-1686](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1678-L1686)，逐项写出复位后的值。

**需要观察的现象 / 预期结果：**

| 项目 | 复位后的值 | 依据 |
| --- | --- | --- |
| `reg_pc`（第一条指令地址） | `0x00010000` | `PROGADDR_RESET` |
| `x2`（sp） | `0x00080000` | `STACKADDR` 非 0xffffffff，触发写 x2 |
| `irq_mask` | `0xffffffff`（全屏蔽） | `irq_mask <= ~0` |
| 永久禁用的中断位 | bit 1、bit 2 | `MASKED_IRQ=0x6 = 0b110` |

进一步推论：复位后软件必须发 `maskirq` 指令把想用的中断位写 0 才能启用，但 bit 1、bit 2 因 `MASKED_IRQ` 永远拉不低。这一步纯源码推导，无需运行；若想验证，可在 `testbench_ez` 顶层用 `defparam` 或实例化参数覆盖改写 `PROGADDR_RESET`/`STACKADDR` 后 `make test_ez` 观察首条取指地址（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1：** 若希望某个外设中断为「电平触发」（只要外设一直拉高就一直请求），`LATCHED_IRQ` 对应位应设成什么？

**答案：** 设成 0。`LATCHED_IRQ` 为 0 的位不做锁存，pending 直接反映中断线的即时电平，即电平触发；为 1 的位才会把单拍脉冲锁存住，即边沿触发（[README.md:303-311](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L303-L311)、[picorv32.v:1440](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1440)）。

**练习 2：** 默认 `PROGADDR_IRQ=0x10`。如果固件的中断处理程序被链接到了别的地址，该改哪里？

**答案：** 改实例化 `picorv32` 时的 `PROGADDR_IRQ` 参数，使它与链接脚本里中断向量的实际地址一致；同时 `PROGADDR_RESET` 要对齐复位向量。这两个地址都是综合期常量，固件地址必须在链接时与之匹配（[picorv32.v:1506-1507](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1506-L1507)、[README.md:313-319](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L313-L319)）。

## 5. 综合实践

**任务：** 为一个「面积最小化、能跑无中断的 RV32I 控制程序」设计一组 `picorv32` 参数，逐项说明取舍，再与 README 的 small/regular/large 三档对照。

**步骤：**

1. **确定功能边界。** 目标是「无中断的 RV32I 控制程序」，所以不需要中断、协处理器、乘除法、压缩指令、trace。可关：`ENABLE_IRQ`、`ENABLE_PCPI/MUL/FAST_MUL/DIV`、`COMPRESSED_ISA`、`ENABLE_TRACE`。
2. **逐项选择并说明影响。** 填写下表（参考 [README.md:725-732](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L725-L732) 的三档定义）：

   | 参数 | 你的选择 | 对面积的影响 | 对性能/行为的影响 |
   | --- | --- | --- | --- |
   | `ENABLE_COUNTERS` | 0 | 删掉 64 位 cycle/instret 计数器，省若干 LUT/FF | `RDCYCLE` 等指令触发 trap；调试完的控制程序可接受 |
   | `ENABLE_REGS_16_31` | 1（保留） | 32 个寄存器 | 关掉只剩 16 个，编译器可用寄存器锐减，代码变长；除非极致面积否则保留 |
   | `ENABLE_REGS_DUALPORT` | 1（保留） | 略增面积 | 关掉后 reg+reg、branch 多 1 拍（见 README CPI 表 CPI(SP) 列） |
   | `TWO_STAGE_SHIFT` | 0 | 删掉「一次移 4 位」的加法器 | 移位变慢（\(s\) 位需 \(s\) 拍），但控制程序移位少时可接受 |
   | `CATCH_MISALIGN` | 0 | 删掉未对齐检测 | 不再对未对齐访存报错；固件保证对齐即可 |
   | `CATCH_ILLINSN` | 0 | 删掉非法指令检测 | 非法指令不报错；固件已调试时可接受 |
   | `LATCHED_MEM_RDATA` | 1 | 让核省去内部锁存 | 要求外部内存在事务后保持 `mem_rdata` 稳定 |

3. **与 README 三档对照。** 把你的配置和官方三档比对：

   | 档位 | 关键参数 | Slice LUTs | Slice Registers |
   | --- | --- | --- | --- |
   | small | 无 counters、无 two-stage shift、`LATCHED_MEM_RDATA=1`、无 `CATCH_MISALIGN/ILLINSN` | 761 | 442 |
   | regular | 全部默认 | 917 | 583 |
   | large | `ENABLE_PCPI/IRQ/MUL/DIV`、`BARREL_SHIFTER`、`COMPRESSED_ISA` | 2019 | 1085 |

   （数据来自 [README.md:736-740](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L736-L740)）你上面那组「面积最小化」选择几乎就是 README 的 **small** 配置，因此预期 LUT 接近 761 这档。

4. **（可选，待本地验证）实测。** 若本机装有 Vivado 或 yosys，可到 `scripts/vivado/` 跑 `make area`，或在 `testbench_ez.v` 里用参数覆盖实例化你的配置并 `make test_ez`，确认程序仍能跑通（功能没被关掉的特性误伤）。

**预期结果：** 你应当得到一组与 small 档相近的参数，并能口头解释「为什么关 `ENABLE_COUNTERS` 省的是 FF、关 `CATCH_ILLINSN` 省的是比较/LUT、关 `TWO_STAGE_SHIFT` 牺牲的是移位周期数」。

## 6. 本讲小结

- PicoRV32 的全部可配置项集中在 [picorv32.v:62-89](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62-L89) 一段，`picorv32_axi`/`picorv32_wb` 只是转发同一份参数。
- 参数分三类：**功能开关**（`ENABLE_*`、`COMPRESSED_ISA`）决定电路有无；**时序/面积取舍**（`TWO_*`、`BARREL_SHIFTER`、`CATCH_*`）只换实现方式；**系统级常量**（`PROGADDR_*`、`STACKADDR`、`MASKED_IRQ`、`LATCHED_IRQ`）只给复位初值或固定位。
- 派生关系集中体现于 `regfile_size`/`regindex_bits`/`WITH_PCPI` 三个 `localparam`（[picorv32.v:165-169](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L165-L169)），它们把开关翻译成具体位宽与是否实例化整块 IP。
- 移位的实现方式是三方权衡的缩影：两级移位迭代次数 \(N_{\text{shift}}(s)=\lfloor s/4\rfloor+(s\bmod 4)\)，桶形移位器则把它并入 ALU 单周期。
- 复位块（[picorv32.v:1457-1483](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1457-L1483)）把 `PROGADDR_RESET`、`STACKADDR`、`irq_mask<=~0` 一次性落实；`MASKED_IRQ`/`LATCHED_IRQ` 则在运行时分别决定「永久禁用」与「边沿/电平触发」。
- README 的 small/regular/large 三档（761/917/2019 LUT）是参数取舍的官方基准，自己设计的配置都应以它为参照。

## 7. 下一步学习建议

本讲只看了 CPU 的「配置旋钮」，还没有逐根端口看信号方向。建议接着学：

- **u3-l2 端口与四大接口**：把本讲的参数（尤其 `ENABLE_PCPI`/`ENABLE_IRQ`/`ENABLE_TRACE`/`COMPRESSED_ISA`）与它们各自对应的端口簇一一对应起来，画出 CPU 方框图。
- 想看「参数如何影响状态机」可先跳到 **u4-l2 主状态机**，重点对照本讲的 `TWO_CYCLE_ALU`/`TWO_STAGE_SHIFT` 在 `cpu_state_exec`/`cpu_state_shift` 里多出的等待拍。
- 想验证本讲的面积预测，可阅读 `scripts/vivado/`（综合评估）与 `scripts/smtbmc`（形式化），这部分会在 **u8-l3 形式化验证与综合评估** 系统讲解。
