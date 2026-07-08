# 仿真、测试台与综合验证

## 1. 本讲目标

本讲是「工具链、二次开发与验证」单元的第二篇，也是全书的收尾篇。上一篇 u18-l1 解决了「模块怎么写出来、怎么进网页和包」；本讲解决最后一个工程问题：**写出来的模块，怎么验证它是对的？**

具体地，读完本讲你应该能够：

1. 说清 `Simulation_Clock` 用 `===`（恒等运算符）实现「无竞争仿真时钟」的原理，并知道它为何不能在 Verilator 里跑。
2. 理解 `Synthesis_Harness_Input` / `Synthesis_Harness_Output` 这对「综合测试桩」要解决的问题（引脚不够、逻辑被优化掉、时序估计失真），以及它们各自用移位/归约把多根线压到一根线的手法。
3. 读懂 `tests/` 目录里那个用 **VUnit**（Python 驱动 SystemVerilog）写的自检测试台，并能仿照它为自己的模块搭一个最小测试台。

本讲有一条**必须先说清的事实纠偏**：本讲规格与上一篇结尾把 `tests/` 的框架称作「cocotb」，但仓库里**实际并不存在 cocotb**——`tests/` 下用的是 **VUnit**（一个同样「Python 编排 + SystemVerilog 被测件 + 断言自检」的框架）。两者角色相似，cocotb 是它的「表亲」。本讲如实讲解 VUnit 的真实代码，并在用到 cocotb 一词时标注清楚，绝不编造仓库里不存在的 cocotb 调用。

## 2. 前置知识

本讲假设你已经建立以下认知（来自前置讲义）：

- **u3-l1 赋值风格、三元运算符与逻辑设计**：组合块 `always @(*)` 用阻塞赋值 `=`、时钟块 `always @(posedge clock)` 用非阻塞赋值 `<=`，两者不可混用；这是本讲理解「时钟竞争」与测试台里各种赋值的基础。
- **u18-l1 文档与实例生成工具链**：本书所有文件平铺在根目录、一模块一文件；`verilinter` 用 Verilator + Icarus 双 linter 检查；`Register_Pipeline` 是流水线寄存器构建块。本讲的两个综合测试桩内部就实例化了 `Register_Pipeline`。

另外，本讲会涉及几个仿真与综合名词，先说清楚：

- **仿真（simulation）**：用软件（如 Icarus Verilog、Verilator、ModelSim）模拟电路在时钟驱动下的逐拍行为，看波形、查功能对错。本讲第一、三个模块属此范畴。
- **综合（synthesis）**：把 Verilog 翻译成 FPGA 上的真实硬件（查找表 LUT、触发器、布线），并做静态时序分析（STA, Static Timing Analysis）估算最高频率。本讲第二个模块（综合测试桩）属此范畴。
- **竞争（race condition）**：仿真器在某个仿真时刻有多件事「理论上同时发生」但执行先后未定义，导致结果不可复现。本讲第一节的核心就是消灭一种时间零点的竞争。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 位置 | 作用 |
|------|------|------|
| `Simulation_Clock.v` | 仓库根目录 | 用 `===` 惯用法生成无竞争的仿真时钟 |
| `Synthesis_Harness_Input.v` | 仓库根目录 | 用移位寄存器把多路设计输入串行喂入，供单独综合 |
| `Synthesis_Harness_Output.v` | 仓库根目录 | 用 XOR 归约把多路设计输出压成一位，供单独综合 |
| `tests/Counter_Gray_Tb.sv` | `tests/` | VUnit 的 SystemVerilog 自检测试台（Gray 码往返校验） |
| `tests/Counter_Gray_Tb.py` | `tests/` | VUnit 的 Python 编排脚本（编译、参数化、批量跑） |
| `verilog.html` | 仓库根目录 | 「Simulated Clock Generation」一节是 `Simulation_Clock` 的规范出处 |

`tests/` 是本书**唯一**的非根目录子目录（u1-l2 讲过），专门放示例测试台。注意：`tests/Counter_Gray_Tb.py` 里还引用了另一个 `Counter_Gray_SV_Tb.sv`，但**该文件并未随仓库发布**（`tests/` 下只有上面这两个文件）——这是仓库现状的一个诚实事实，照搬脚本直接运行会在那一步报缺文件（待本地验证）。

## 4. 核心概念与源码讲解

### 4.1 仿真时钟：Simulation_Clock 的无竞争惯用法

#### 4.1.1 概念说明

任何时序电路的仿真都离不開一个「时钟」。最朴素的写法是声明一个 `reg clock`，再用一个 `always` 块每隔半个周期把它翻转一次。但这套写法藏着一个经典陷阱：**时间零点的竞争**。

问题出在「初始化」上。在 Verilog 仿真里，一个 `reg` 在声明时如果不赋初值，它的初值是 `X`（未知）。当你写 `reg clock = 1'b0;` 时，这其实是一次 `X -> 0` 的跳变——而 `X -> 0` 正是一个**下降沿**，它会发生在仿真时间零点。与此同时，别的寄存器（比如 `reg foo = 1'b0;`）也在时间零点做 `X -> 0`。于是下面这段代码就有歧义：

```verilog
reg clock = 1'b0;        // 时间零点：一次 X->0（下降沿！）
reg foo   = 1'b0;        // 时间零点：也是一次 X->0
always begin
    #HALF_PERIOD clock = ~clock;   // 生成时钟
end
always @(negedge clock) begin
    bar <= foo;          // 时间零点的下降沿触发时，foo 还是 X 吗？
end
```

仿真器**无法保证**「时间零点的下降沿」和「foo 的初始化」谁先处理，于是 `bar` 可能在第一个周期被赋成 `X`——这不是设计本意，且不同仿真器、不同版本结果可能不同，极难排查。

`Simulation_Clock` 模块用一个聪明的惯用法（作者标注 credit 属于 Claire Wolf）根治这个问题：**让时钟保持未初始化的 `X`，并用恒等运算符 `===` 而非相等运算符 `==` 来翻转它**。关键区别是：

- `==` 把 `X` 当作「假」；
- `===`（identity，恒等）要求**逐位精确匹配**，`X === 1'b0` 的结果是 `1'b0`（假），而 `X === X` 才是真。

利用这一点，把翻转写成 `clock = (clock === 1'b0)`，就能让时钟的第一次有意义跳变被**推迟半个周期**，从而与时间零点的各种寄存器初始化彻底错开。这个惯用法的规范出处就是 [verilog.html 的 Simulated Clock Generation 一节](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L916-L955)。

#### 4.1.2 核心流程

`Simulation_Clock` 的执行过程可以画成：

```text
声明 output reg clock，不赋初值 → 初值为 X
   │
   ▼
always 块：先等 HALF_PERIOD（不在时间零点动手）
   │
   ▼
第一次赋值：clock = (X === 1'b0) = 1'b0
   （一次 X->0，但发生在 HALF_PERIOD，不是时间零点）
   │
   ▼
此后每 HALF_PERIOD：
   clock = (clock === 1'b0)
   - 当前 0 → (0===0)=1  → 上跳（posedge）
   - 当前 1 → (1===0)=0  → 下跳（negedge）
   即稳定地 0/1 翻转
```

第一个上升沿出现在 \( 2 \times \text{HALF\_PERIOD} = \text{CLOCK\_PERIOD} \) 处，此时所有寄存器的时间零点初始化早已结算完毕，竞争被消解。

模块还附带了两个用起来很方便的「拍数计数」小惯用法（以注释形式给出）：`WAIT_CYCLES(n)` 用 `repeat (n) @(posedge clock)` 等待 n 拍，`UNTIL_CYCLE(n)` 用一个 `cycle` 计数器配合 `wait` 等到第 n 拍。**只用这两个宏、不用 `#` 延迟来计时**的话，整个测试台甚至不需要 `timescale` 指令也能按正确速率跑。

#### 4.1.3 源码精读

模块本体非常短，先看头部的两条重要 NOTE：[Simulation_Clock.v:9-16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L9-L16) 说明两件事——这段代码**不能在 Verilator 里跑**（Verilator 只仿真可综合 Verilog，不支持延迟赋值 `#`，要在 C++ 测试台里生成时钟，或改用 Icarus Verilog）；以及这个时钟**不能直接当作周期性数据信号使用**（会在事件队列里引发竞争，作者表示尚未找到解法）。

模块定义与端口只有一个参数和一个 `output reg clock`：[Simulation_Clock.v:53-59](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L53-L59)。注意 `clock` 没有 `initial` 赋初值，这正是惯用法成立的前提（让它停在 `X`）。

核心的「无竞争翻转」只有一行：[Simulation_Clock.v:63-65](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L63-L65)

```verilog
always begin
    #HALF_PERIOD clock = (clock === 1'b0);
end
```

注意这里用的是**阻塞赋值 `=`**（在 `always` 块里、且本就是用于生成测试激励的「不可综合」仿真代码，符合 u3-l1 讲过的「组合/激励用阻塞」精神；而真正采样数据的 `always @(posedge clock)` 仍应用非阻塞 `<=`）。`HALF_PERIOD` 是由参数算出的局部常量：[Simulation_Clock.v:61](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L61)。

两个配套的拍数计数宏以注释形式给出：[Simulation_Clock.v:75-83](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L75-L83)，分别是 `WAIT_CYCLES`（`repeat` 等拍）和 `UNTIL_CYCLE`（计数器 + `wait`）。它们让你摆脱裸 `#` 延迟、用「周期数」来表达测试时序。

模块顶部的 `` `timescale 1ns / 1ps ``（[Simulation_Clock.v:51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L51)）给 `#HALF_PERIOD` 这类延迟提供了时间单位。

#### 4.1.4 代码实践

**目标**：用 Icarus Verilog 跑一个最小的 `Simulation_Clock`，在波形里亲眼看到「第一个上升沿出现在一个完整 CLOCK_PERIOD 之后，而非时间零点」。

**操作步骤**：

1. 确认本机装有 `iverilog`（Icarus Verilog）与波形查看器（如 `gtkwave`）。**不要用 Verilator**——模块头部 NOTE 明确它不支持延迟赋值（待本地验证是否已安装）。

2. 在仓库根目录写一个最小的顶层测试台 `tb_sim_clock.v`（**示例代码**，非仓库原有文件）：

   ```verilog
   `timescale 1ns / 1ps
   module tb_sim_clock;
       wire clock;
       reg [31:0] cycle = 0;

       Simulation_Clock #(.CLOCK_PERIOD(10)) dut (.clock(clock));

       always @(posedge clock) cycle <= cycle + 1;

       initial begin
           $dumpfile("sim_clock.vcd");
           $dumpvars(0, tb_sim_clock);
           #55 $finish;   // 跑 5.5 个周期
       end
   endmodule
   ```

3. 编译并运行（连同 `Simulation_Clock.v` 一起）：

   ```bash
   iverilog -o sim_clock.vvp tb_sim_clock.v Simulation_Clock.v
   vvp sim_clock.vvp
   gtkwave sim_clock.vcd
   ```

**需要观察的现象**：

- `clock` 在 `t=0` 是 `X`（未初始化）。
- 在 `t=5ns`（HALF_PERIOD）首次变为 `0`。
- 在 `t=10ns`（CLOCK_PERIOD）出现**第一个上升沿**——`cycle` 从 0 变 1。
- 之后每 10ns 一个上升沿。

**预期结果**：第一个上升沿出现在 10ns，证明时钟没有在时间零点产生会造成竞争的假边沿。具体数值以本地仿真器输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把翻转写成 `clock = (clock == 1'b0)`（用 `==` 而非 `===`），第一拍会怎样？

> **参考答案**：`==` 把 `X` 当假，所以 `(X == 1'b0)` 在某些仿真器里被当成「真」从而第一拍就把 clock 设成 `1`，破坏了「先停在 X、推迟半个周期再动手」的语义，竞争保护失效。`===` 的精确匹配才是惯用法成立的关键（[Simulation_Clock.v:64](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L64)）。

**练习 2**：为什么 `Simulation_Clock` 不能在 Verilator 下使用？

> **参考答案**：Verilator 只仿真**可综合**的 Verilog，把设计先编译成 C++ 再跑；而 `#HALF_PERIOD` 这类**延迟赋值**不可综合，Verilator 不支持。模块头部 NOTE 给出的替代方案是「在 C++ 测试台里生成时钟」或「改用 Icarus Verilog」（[Simulation_Clock.v:9-12](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L9-L12)）。

---

### 4.2 综合测试桩：Synthesis_Harness_Input / Output

#### 4.2.1 概念说明

`Simulation_Clock` 服务于**仿真**；本节的两个模块服务于**综合**——确切说，是「把一个模块**单独**扔进 CAD 工具（如 Vivado/Quartus），快速看它的综合结果和时序」这种开发期迭代场景。

听起来直白，但单独综合一个模块会遇到两个头疼的问题（注释把它们讲得很清楚，见 [Synthesis_Harness_Input.v:4-19](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Input.v#L4-L19)）：

1. **物理引脚不够、逻辑被扯散**：综合器会把逻辑尽量贴近引脚摆放以缩短 I/O 路径。模块端口一多，引脚很快用光；而且逻辑为了追引脚会被撒满整片 FPGA，**毁掉时序估计**。
2. **未寄存的 I/O 不进 STA**：模块边界上若没有寄存器，那部分组合逻辑不参与静态时序分析，时序估计偏乐观、不准确。

解决思路是把设计**裹进一层寄存器外壳（harness）**：

- **输入侧 `Synthesis_Harness_Input`**：用一个**移位寄存器**，从少数几根引脚（`bit_in` + `bit_in_valid`）串行移入，拼出完整的并行输入字喂给设计。这样引脚不再爆炸，且输入恒为「移位中的随机值」，综合器无法判定某根输入是常数、因而**不会把下游逻辑优化掉**。
- **输出侧 `Synthesis_Harness_Output`**：用一组寄存器捕获设计的全部输出，再**XOR 归约**成一根线输出。注释解释了为何**不**做并行转串行（[Synthesis_Harness_Output.v:21-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Output.v#L21-L25)）：并串转换会让综合器在输出与寄存器间插进多路选择器，**污染时序分析**；而按位 XOR 归约不会引入这种多路器。

注意一个反直觉点：这些测试桩的输入/输出数据**对仿真而言是无意义的随机值**——它们生来就不是为验证功能，而是为「骗过综合器、给出可信的时序估计」。

#### 4.2.2 核心流程

两个测试桩都把「包裹」实现成对 `Register_Pipeline`（u6-l2 讲过的流水线寄存器）的一次实例化：

```text
Synthesis_Harness_Input（并出）：
   bit_in ──► [Register_Pipeline, WORD_WIDTH=1, PIPE_DEPTH=WORD_WIDTH]
              （clock_enable = bit_in_valid，逐位移入）
           ──► word_out[WORD_WIDTH-1:0]  喂给设计

Synthesis_Harness_Output（并入、归约出）：
   设计输出 word_in ──► [Register_Pipeline, WORD_WIDTH=WORD_WIDTH, PIPE_DEPTH=1]
                        （clock_enable = word_in_valid，整体打一拍寄存）
                     ──► word_out ──► (^word_out) ──► bit_out  一根线出
```

两者都带一组**约束属性**（Vivado 的 `IOB="false"`/`DONT_TOUCH="true"`、Quartus 的 `useioff=0`/`preserve`），要求：不要把这些外壳寄存器放进 FPGA 的 I/O 寄存器里，而是让它们**簇集在你的设计周围**——于是设计会整体聚到芯片中央，给出「合理准确」的时序估计；并保持外壳与设计的网表分离、互不重定时（retiming），让估计更保守、更可信。想要更保守，还可以进一步做**逻辑划分**（不让二者重定时）乃至**物理划分/floorplanning**（给设计画一个矩形布局区，把外壳排除在外）。

#### 4.2.3 源码精读

**输入侧**模块定义只有一个 `WORD_WIDTH` 参数（默认 0，符合 u1-l2 的「吵闹失败」约定）：[Synthesis_Harness_Input.v:45-55](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Input.v#L45-L55)。用法是「把你设计所有输入的位宽加起来当 `WORD_WIDTH`，再把所有输入线拼接到 `word_out`」。

核心是把 `Register_Pipeline` 配置成一个**逐位移入的移位寄存器**：[Synthesis_Harness_Input.v:68-86](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Input.v#L68-L86)

```verilog
Register_Pipeline #(
    .WORD_WIDTH     (1),
    .PIPE_DEPTH     (WORD_WIDTH),   // 深度 = 输出字宽：移够 WORD_WIDTH 次拼出整字
    .RESET_VALUES   (WORD_ZERO)
) shift_bit_into_word (
    .clock          (clock),
    .clock_enable   (bit_in_valid),  // 有效时才移位
    .clear          (clear),
    .parallel_load  (1'b0),
    .parallel_in    (WORD_ZERO),
    .parallel_out   (word_out),      // 拼出的并行字，喂给设计
    .pipe_in        (bit_in),        // 串行输入
    .pipe_out       ()
);
```

留意它把 `WORD_WIDTH`（每位的字宽）设成 `1`、把 `PIPE_DEPTH`（级数）设成目标字宽——这正是「一位一位地移、拼成一个宽字」的串入并出用法。注释里 `verilator lint_off PINCONNECTEMPTY` 是因为 `.pipe_out()` 故意悬空不接，要压掉「端口未连接」的 lint 告警（u18-l1 讲过 verilinter 的纪律）。

紧贴实例化上方的四行约束属性：[Synthesis_Harness_Input.v:59-66](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Input.v#L59-L66)，分别面向 Vivado（`IOB="false"` 不放 I/O 缓冲、`DONT_TOUCH="true"` 保持网表独立）与 Quartus（`useioff=0` 不用 I/O 寄存器、`preserve` 不与别处合并寄存器）。这正是 u4-l2 讲过的「源码内约束」——绑定到声明、随实例化自动生效。

**输出侧**结构对称，但归约方式不同：[Synthesis_Harness_Output.v:77-99](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Output.v#L77-L99)。这里 `Register_Pipeline` 的 `WORD_WIDTH` 直接取目标字宽、`PIPE_DEPTH=1`（整体打一拍寄存），随后用一个组合块把整字 XOR 归约成一位：

```verilog
always @(*) begin
    bit_out = ^word_out;   // 按位异或归约：一根线输出
end
```

`bit_out` 是 `output reg`，故按 u2-l1 的规矩在 `initial` 里赋了初值：[Synthesis_Harness_Output.v:62-64](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Output.v#L62-L64)。同样的四行源码内约束也出现在输出侧：[Synthesis_Harness_Output.v:68-75](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Output.v#L68-L75)。

#### 4.2.4 代码实践

**目标**：源码阅读型实践——为一个简单模块（如 `Counter_Binary`）规划「输入位宽求和 + 拼接、输出归约」的综合测试桩接法，体会它如何骗过综合器。

**操作步骤**：

1. 打开 `Counter_Binary.v`，列出它的全部输入端口及其位宽（`clock`、`clear`、`clock_enable`、`load`、`increment` 等——以本地源码为准）。
2. 假设把所有「数据/控制」输入（不含 `clock`）的位宽相加，记为 `W_in`。
3. 在纸上画出接法：`Synthesis_Harness_Input #(.WORD_WIDTH(W_in))` 的 `word_out` 拼接后接到 Counter 的各输入；Counter 的输出（如 `count`、`carry_out`）位宽相加为 `W_out`，接到 `Synthesis_Harness_Output #(.WORD_WIDTH(W_out))` 的 `word_in`。
4. （可选）若本机有 Vivado/Quartus，把这个三件套（输入桩 + Counter + 输出桩）作为一个顶层工程综合，观察布局：设计应聚在芯片中央，而非被扯向引脚。

**需要观察的现象**：

- 输入桩用 `bit_in` 一根线（加 `bit_in_valid`）就能喂满所有输入，引脚不再爆炸。
- 输出桩把全部输出压成 `bit_out` 一根线。
- 综合后设计逻辑不会被当作「常数输入」优化掉（因为输入是移位中的随机值）。

**预期结果**：时序报告覆盖了边界寄存器之间的全部路径，估计比「裸综合」更可信。是否真跑综合以本地 CAD 工具为准（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`Synthesis_Harness_Output` 为什么用 XOR 归约（`^word_out`）输出一位，而不是像输入侧那样做并行转串行？

> **参考答案**：并行转串行会让综合器在「设计输出」与「归约寄存器」之间**插入多路选择器**，这些多路器会进入时序分析、**污染 STA 结果**（[Synthesis_Harness_Output.v:21-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Output.v#L21-L25)）。XOR 归约只产生一个数据相关的一位结果，既压线宽、又不引入多路器，时序估计更干净。

**练习 2**：为什么必须在源码里给外壳寄存器加 `IOB="false"`（Vivado）/ `useioff=0`（Quartus）？

> **参考答案**：否则综合器会把这些寄存器放进 FPGA 的 **I/O 寄存器**（贴近引脚），导致设计逻辑仍被扯向芯片边缘、时序估计失真（[Synthesis_Harness_Input.v:21-24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Synthesis_Harness_Input.v#L21-L24)）。禁止使用 I/O 寄存器后，外壳寄存器才会簇集在设计周围、把设计聚到中央，给出可信估计。

---

### 4.3 Python 驱动的 SystemVerilog 自检测试台（VUnit）

> **关于「cocotb」的说明**：本讲规格把这一节标作「cocotb 测试台」。但仓库 `tests/` 里**实际使用的是 VUnit**，不存在 cocotb 的痕迹。两者都是「Python 编排 + SystemVerilog 被测件 + 自检断言」的验证框架（cocotb 用 Python 协程直接驱动信号、VUnit 用 SystemVerilog 测试台 + Python 做编译/参数化/批量调度）。本节如实讲解 VUnit 的真实代码；如果你想用 cocotb，思路相通，但需自行引入 cocotb 依赖（仓库不提供）。

#### 4.3.1 概念说明

`Simulation_Clock` 解决了「怎么生成时钟」，`Synthesis_Harness` 解决了「怎么单独综合」。但还有一个最基本的问题没回答：**怎么自动地、可复现地检查一个模块的功能对不对？** 这就需要**自检测试台（self-checking testbench）**——它不仅驱动激励、看波形，还在每个关键拍用断言（assertion）自动比对「实际输出」与「期望输出」，对就过、错就报。

`tests/` 目录给出了一个完整范例：用 **VUnit** 验证 `Binary_to_Gray` / `Gray_to_Binary` 这对 Gray 码转换模块。VUnit 的分工是：

- **SystemVerilog 侧（`Counter_Gray_Tb.sv`）**：写被测件（DUT）的实例化、时钟、激励、和自检断言。
- **Python 侧（`Counter_Gray_Tb.py`）**：负责「编译哪些源文件、给测试套哪些参数、批量跑多少种配置」——把工程的构建与回归测试（regression）自动化。

这种「Python 编排 + SV 断言」的范式，让一次 `python Counter_Gray_Tb.py` 就能跑完「宽度 4/5/6 各一种」的全部组合，比手写多份测试台省事得多。

#### 4.3.2 核心流程

整套 VUnit 流程可以画成：

```text
Python 编排（Counter_Gray_Tb.py）
   ├─ vunit.VUnit.from_argv()           解析命令行（选仿真器、选测试等）
   ├─ add_library('lib')                建一个逻辑库
   ├─ add_source_file(...)              把 DUT 源码 + 测试台加进库
   │     （给 Verilog-2001 文件加 -vlog01compat 兼容标志）
   ├─ test_bench('Counter_Gray_Tb')     取出测试台
   ├─ 对每个 test 用 add_config 蝶展参数化（WIDTH=4/5/6, PRINT=1）
   └─ vu.main()                         编译 + 跑全部配置，汇总通过/失败
           │
           ▼
SystemVerilog 测试台（Counter_Gray_Tb.sv）
   ├─ include vunit_defines.svh         引入 VUnit 宏（CHECK_EQUAL 等）
   ├─ module counter_gray_tb(...)       VUnit 要求模块名小写
   ├─ 生成时钟 + 实例化 DUT（Binary_to_Gray / Gray_to_Binary）
   ├─ 往返自检：binary -> gray -> binary，CHECK_EQUAL 还原相等
   └─ full_period 用例：每步断言「恰好变化 1 位」（Gray 码的本质属性）
```

被验证的核心属性有两条，都很有代表性：

1. **往返一致性**（round-trip）：`binary -> gray -> binary` 应回到原值。
2. **Gray 码的单步单变**：相邻两个 Gray 码之间**恰好只有一位不同**——用 `popcount(gray_now ^ gray_last) == 1` 来断言。

#### 4.3.3 源码精读

**SystemVerilog 测试台**开头两行就点明了它是 VUnit 测试台：[tests/Counter_Gray_Tb.sv:1-5](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L1-L5)——`` `default_nettype none ``（u2-l1 纪律）之后 `` `include "vunit_defines.svh" `` 引入 VUnit 的全部宏；注释还提醒「VUnit 要求测试台模块名小写」（这大概是受 VHDL 影响的约束），所以模块叫 `counter_gray_tb`。

VUnit 通过「参数」把配置从 Python 注入到 SV：[tests/Counter_Gray_Tb.sv:8-13](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L8-L13)，其中 `output_path`/`tb_path` 是 VUnit 框架自带的，`WIDTH` 是本测试关心的位宽，`PRINT` 控制是否打印转换过程。注意 `parameter WIDTH;` 没有默认值——它必须由 Python 侧 `add_config` 注入，否则无法独立编译。

测试台自带一个 `popcount` 函数（统计 1 的个数），后面用它来表达「单步单变」：[tests/Counter_Gray_Tb.sv:18-26](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L18-L26)。

时钟生成用的是「朴素翻转 + 非阻塞」，**并非** 4.1 节的 `===` 惯用法：[tests/Counter_Gray_Tb.sv:35-36](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L35-L36)

```verilog
always
    #(CLK_PERIOD/2) clk <= ~clk;
```

这里的时间零点同步问题交给 VUnit 的 setup 阶段处理（见下文 `TEST_SUITE_SETUP` 把 `clk` 钉到 `1'b0`、`TEST_CASE_SETUP` 用 `@(posedge clk)` 对齐后再发激励），而不是靠时钟惯用法本身。这是一个值得对照的点：**本书的可复用 `Simulation_Clock` 模块用 `===` 根治竞争；而这个示例测试台用框架的 setup/同步宏在「测试组织层面」回避了它**——两条路都成立，前提是你清楚自己在哪一层处理同步。

两个 DUT 用**位置连接**（非命名连接）实例化，把 `Binary_to_Gray` 的输出直接喂给 `Gray_to_Binary`，串成往返链：[tests/Counter_Gray_Tb.sv:51-52](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L51-L52)。

**往返自检**只有三行，是整个测试台的核心断言：[tests/Counter_Gray_Tb.sv:57-59](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L57-L59)

```verilog
always@(posedge(clk))
    if (sresetn && binary_valid)
        `CHECK_EQUAL(binary, binary_from_gray);
```

`CHECK_EQUAL` 是 VUnit 宏：每拍只要复位已释放且有有效数据，就比对「原始 binary」与「gray 再转回的 binary」，不等就记一次失败。`sresetn` 是「同步复位释放」的有效高信号。

`full_period` 用例则验证 Gray 码的**单步单变**属性：[tests/Counter_Gray_Tb.sv:104-115](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L104-L115)

```verilog
`TEST_CASE("full_period") begin
    binary          <= '0;
    binary_valid    <= 1'b0;
    @(posedge clk);
    for(integer i=0;i<2**WIDTH+4;i=i+1) begin
        binary          <= binary+1;
        binary_valid    <= 1'b1;
        @(posedge(clk));
        `CHECK_EQUAL(popcount(gray_from_binary ^ gray_last),1);  // 恰好变 1 位
    end
end
```

它跑满一个完整周期再多几拍，每步把 `binary` 加 1，断言「当前 Gray 码与上一拍 Gray 码的异或的 popcount 恰为 1」——这正是 Gray 码的定义性特征。`TEST_CASE_SETUP`（[tests/Counter_Gray_Tb.sv:92-98](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L92-L98)）在每个用例前先拉低复位、发一个空拍、再释放复位并对齐到上升沿，保证起点干净。

**Python 编排脚本**用十几行完成了「编译 + 参数化 + 批量跑」：[tests/Counter_Gray_Tb.py:5-9](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L5-L9) 从命令行参数构造 VUnit 实例并建库；[tests/Counter_Gray_Tb.py:11-24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L11-L24) 把 DUT 源码（`Gray_to_Binary.v`、`Binary_to_Gray.v`、`Gray.sv`）与测试台加进库——注意 Verilog-2001 的 `.v` 文件被加了 `modelsim.vlog_flags` 的 `-vlog01compat`（按 1364-2001 方言编译）；[tests/Counter_Gray_Tb.py:26](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L26) 还把 VUnit 自带的 `vunit_pkg.sv` 加进来（`CHECK_EQUAL` 等宏的实现依赖它）。

参数化「蝶展」的关键循环：[tests/Counter_Gray_Tb.py:30-37](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L30-L37)

```python
for test in counter_gray_tb.get_tests('*'):
    for width in [4,5,6]:
        test.add_config(
            name="W%d"%(width),
            generics={
                'WIDTH':width,
                'PRINT':1
            })
```

对测试台里的**每一个**测试用例，都生成 `W4`/`W5`/`W6` 三个配置（分别注入 `WIDTH=4/5/6`）。于是「宽度」这个维度被自动笛卡尔积展开，无需复制测试台。最后的 `vu.main()`（[tests/Counter_Gray_Tb.py:50](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L50)）负责编译并跑完所有配置，汇总通过/失败。

> **诚实提示**：脚本第 [20-24 行](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L20-L24) 与第 [39-47 行](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L39-L47) 还引用了第二个测试台 `Counter_Gray_SV_Tb.sv` / `counter_gray_sv_tb`，但该文件**并未随仓库发布**（`tests/` 下只有 `Counter_Gray_Tb.py` 与 `Counter_Gray_Tb.sv`）。因此原样运行该脚本会在加载第二个测试台时报缺文件；若要本地复现，需先把这两段引用注释掉（待本地验证）。

#### 4.3.4 代码实践

**目标**：仿照 `tests/`，用 VUnit（或你顺手的仿真器）为一个最简模块写一个自检测试台。这里以本书的 `Register`（u6-l1）为被测件，验证「`clear` 优先于 `clock_enable`」这条「最后赋值胜出」语义。

**操作步骤**（VUnit 风格，**示例代码**，非仓库原有文件）：

1. 新建 `tests/Register_Tb.sv`：

   ```systemverilog
   `default_nettype none
   `include "vunit_defines.svh"

   module register_tb();
       parameter WIDTH = 8;

       reg clk = 1'b0;
       reg clear, clock_enable;
       reg [WIDTH-1:0] data_in;
       wire [WIDTH-1:0] data_out;

       Register #(.WORD_WIDTH(WIDTH)) dut (
           .clock(clk), .clock_enable(clock_enable),
           .clear(clear), .data_in(data_in), .data_out(data_out));

       always #(5) clk = ~clk;   // 朴素时钟（演示用，依赖 setup 对齐）

       `TEST_SUITE begin
           `TEST_CASE("load_and_clear") begin
               clear <= 1'b0; clock_enable <= 1'b1; data_in <= 8'hAA;
               @(posedge clk); @(posedge clk);
               `CHECK_EQUAL(data_out, 8'hAA);          // 加载生效
               clear <= 1'b1;                          // 同拍同时拉 clear
               @(posedge clk);
               `CHECK_EQUAL(data_out, {WIDTH{1'b0}});  // clear 优先，归零
           end
       end
   endmodule
   ```

2. 新建 `tests/Register_Tb.py`（仿照 `Counter_Gray_Tb.py`）：从命令行建 VUnit 实例 → 把 `Register.v` 与 `Register_Tb.sv` 加进库 → 用 `add_config` 蝶展 `WIDTH=8/16` → `vu.main()`。

3. 运行（需本机装好 VUnit 及其支持的仿真器，如 ModelSim/GHDL+Icarus，待本地验证）：

   ```bash
   python3 tests/Register_Tb.py
   ```

**需要观察的现象**：第二条 `CHECK_EQUAL` 通过——说明即便 `clock_enable` 与 `clear` 同拍拉高，`Register` 仍输出 0（clear 胜出），印证了 u6-l1 / u3-l2 讲的「最后赋值胜出」。

**预期结果**：全部用例 PASS。若改写 `clear` 为低却仍期望归零，则第二条断言会 FAIL——这正是自检测试台的价值：错就立刻报，不用肉眼盯波形。具体运行结果以本地环境为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`tests/Counter_Gray_Tb.py` 里，为什么 Verilog-2001 的 `.v` 源文件要加 `-vlog01compat` 标志？

> **参考答案**：本书只用 Verilog-2001 可综合子集（u2-l1）。当仿真器是 ModelSim 等、默认方言可能更偏 SystemVerilog 时，`-vlog01compat`（[tests/Counter_Gray_Tb.py:18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.py#L18)）强制按 1364-2001 方言编译这些 `.v`，避免把 SV 语义误套到 Verilog-2001 代码上。

**练习 2**：`full_period` 用例里的断言 `CHECK_EQUAL(popcount(gray ^ gray_last), 1)` 在验证 Gray 码的什么性质？为什么这个性质重要？

> **参考答案**：它在验证 **Gray 码相邻两值恰好只差一位**（单步单变）。这是 Gray 码的根本用途所在——跨时钟域计数器（如 u14-l2 的 CDC FIFO 指针）正是靠「每次只变一位」来让接收域即使采到跳变瞬间也不会拿到全错的中间值。若某次变化了不止一位，这个断言就会失败。

**练习 3**：本测试台的时钟（`clk <= ~clk`）没有用 4.1 节的 `===` 惯用法，为什么仍然可以工作？

> **参考答案**：它把时间零点同步问题交给了 VUnit 的组织层——`TEST_SUITE_SETUP` 把 `clk` 钉到已知值、`TEST_CASE_SETUP` 在发激励前先 `@(posedge clk)` 对齐（[tests/Counter_Gray_Tb.sv:88-98](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/tests/Counter_Gray_Tb.sv#L88-L98)）。两条路都能回避竞争：可复用的 `Simulation_Clock` 在「时钟惯用法层」根治，VUnit 测试台在「测试组织层」回避。关键是清楚自己在哪里处理了同步。

## 5. 综合实践

本实践把三块内容串成一条「为一个模块同时做功能仿真 + 综合估计」的线。被测件选 `Counter_Binary`（u8-l2），它由 `Adder_Subtractor_Binary` + 多个 `Register` 组成，是个有代表性的小系统。

**实践目标**：给 `Counter_Binary` 配齐两类验证设施——一个用于功能自检的仿真测试台，一个用于时序估计的综合测试桩外壳。

**操作步骤**：

1. **功能仿真（用 4.1 + 4.3）**：
   - 写一个测试台，实例化 `Simulation_Clock` 生成时钟，再实例化 `Counter_Binary`（设好 `WORD_WIDTH`、`INCREMENT` 等），按拍驱动 `clear`/`run`。
   - 用 `WAIT_CYCLES`（[Simulation_Clock.v:75](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Simulation_Clock.v#L75)）计时，避免裸 `#`。
   - 仿照 4.3.4，写自检断言：例如复位后从 0 开始、每拍 `run` 则按 `INCREMENT` 递增、计满后 `carry_out` 拉高。可选用 VUnit（`CHECK_EQUAL` + Python 蝶展 `WORD_WIDTH`）或纯 Icarus（`$display` + 期望值比对）。
   - 用 Icarus 编译运行（`iverilog` + `vvp`），看是否全部 PASS。

2. **综合估计（用 4.2）**：
   - 列出 `Counter_Binary` 的全部输入/输出端口及位宽。
   - 用 `Synthesis_Harness_Input`（串入并出）包住输入侧、`Synthesis_Harness_Output`（XOR 归约）包住输出侧，画/写出顶层连接（参考 4.2.4）。
   - 把这三件套作为顶层工程丢进 CAD 工具综合，观察设计是否聚到芯片中央、时序报告是否覆盖了边界寄存器之间的路径。

3. **交叉思考**：功能仿真证明了「算得对」，综合测试桩证明了「综合后时序可信」。回答一个问题——为什么 `Synthesis_Harness` 的输入输出对功能仿真「无意义」，却对综合「不可或缺」？

**需要观察的现象 / 预期结果**：

- 仿真测试台：所有断言 PASS；`carry_out` 在计满拍按预期拉高。
- 综合测试桩：设计聚于芯片中央，未被当作常数优化掉；引脚占用极小（输入一根 `bit_in`、输出一根 `bit_out`，外加 `clock`/`clear`/`valid`）。
- 能口头说清「功能验证」与「综合/时序验证」是两套互相不可替代的检查。

> 说明：本实践的运行依赖本地已安装 Icarus Verilog（仿真）与 Vivado/Quartus（综合），以及（若走 VUnit 路线）VUnit 及其支持的仿真器。具体输出与命令以本地环境为准，相关步骤标注为「待本地验证」。`Simulation_Clock` 不可在 Verilator 下使用，请勿选 Verilator 跑第一节。

## 6. 本讲小结

- `Simulation_Clock` 用「时钟保持 `X` + `===` 恒等翻转」的惯用法，把第一次有意义跳变推迟半个周期，消解了时间零点「寄存器初始化 vs 首个时钟边沿」的竞争；它依赖延迟赋值 `#`，故**不能在 Verilator 里跑**，需用 Icarus 等支持延时的仿真器。
- `Synthesis_Harness_Input` 用一个 `Register_Pipeline`（串入并出）把少数引脚移位成完整输入字；`Synthesis_Harness_Output` 用一个 `Register_Pipeline`（整体打一拍）再 XOR 归约成一位——二者解决「单独综合时引脚不够、逻辑被优化掉、I/O 不进 STA」三大问题。
- 输出侧刻意用 XOR 归约而非并串转换，是为了不让综合器在输出与寄存器间插进多路器、污染时序分析。
- 两类测试桩都带 `IOB="false"`/`DONT_TOUCH`/`useioff=0`/`preserve` 等**源码内约束**，强制外壳寄存器不进 I/O 寄存器、簇集在设计周围，从而给出可信（且可加划分使之更保守）的时序估计。
- `tests/` 的真实框架是 **VUnit**（非 cocotb）：SystemVerilog 侧用 `CHECK_EQUAL` 等宏做每拍自检断言（验证 Gray 码往返一致与单步单变），Python 侧用 `add_config` 把 `WIDTH` 维度自动蝶展成多套配置批量回归。
- 自检测试台把「盯波形」升级为「断言自动 PASS/FAIL」；`Simulation_Clock` 在时钟惯用法层根治竞争，而 VUnit 测试台在测试组织层（setup + `@(posedge clk)` 对齐）回避竞争——两条路都成立，关键是清楚同步在哪一层处理。

## 7. 下一步学习建议

- **回顾全书**：本讲是手册最后一篇。建议回到 u18-l1 把工具链（`v2h`、`generate_*`、`verilinter`、FuseSoC）与本讲「仿真 + 综合」两套验证合起来看，形成「写模块 → lint → 渲染网页 → 加目录 → 加包 → 仿真验证 → 综合验证 → 发 PR」的完整贡献闭环（`CONTRIBUTING.md` 的四步是它的骨架）。
- **动手贡献**：找一个 `index.html` 里**尚未实现**（无超链接）的规划项，按上述闭环实现它，并仿照本讲 4.3.4 配一个最小自检测试台——这是把全书知识用起来的最佳方式。
- **深入 CDC 验证**：本讲的同步与时钟基础（u13-1、u13-2）是理解 CDC（时钟域穿越）仿真的前提。若有兴趣，可阅读 u14 单元的 CDC FIFO/Flancter 模块，思考它们的正确性为何「靠结构纪律、而非仿真保证」，以及如何为它们写有意义的跨域测试台。
