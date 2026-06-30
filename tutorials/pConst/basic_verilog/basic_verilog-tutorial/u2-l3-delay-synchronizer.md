# 静态延迟与同步链：delay

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「静态延迟」在数字电路里是什么，以及为什么几乎每个 FPGA 工程都会用到它。
- 读懂 [`delay.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) 如何用 **一个 `generate` 块** 产出 5 种完全不同的电路（导线直通 / 单级寄存器 / 寄存器链 / Altera 块 RAM FIFO / altshift_taps）。
- 解释 `LENGTH` / `WIDTH` / `TYPE` / `REGISTER_OUTPUTS` 四个参数各自控制什么，以及为什么同一份源码能同时满足「小延迟用触发器、大延迟用块 RAM」两种诉求。
- 理解 Xilinx 工具推断 SRL16E/SRL32E、Quartus 用 altshift_taps 做「移位寄存器替换」的前提条件。
- 明白把 `delay` 当作跨时钟域同步器使用时，为什么必须手写一条 `set_false_path` 约束，以及 `_SYNC_ATTR` 命名约定如何让你「一条约束管住所有同步器」。
- 自己写一个 testbench，验证 `LENGTH=5` 的延迟确实让输出**精确滞后 5 个时钟周期**。

## 2. 前置知识

本讲需要你已具备下列概念（在 u1-l2、u2-l1、u2-l2 中已建立）：

- **时序逻辑与 `always_ff`**：每个 `posedge clk` 更新一次寄存器，用非阻塞赋值 `<=`。
- **参数化端口 `#(parameter ...)`**：靠参数在「不改源码」的前提下改电路规模。
- **同步复位 `nrst`（低有效）**：复位写在 `always_ff` 里、敏感列表只含时钟。
- **`generate` 编译期选择**：`generate if/else` 不是运行时分支，而是「综合前」决定哪段代码变成真实硬件，其余分支根本不存在。
- **派生时钟与时钟使能**（u2-l1）：不要随便用分频出来的时钟去驱动别的 `always_ff`。

两个本讲会用到的新术语，先建立直觉：

- **移位寄存器（shift register）**：一串首尾相接的触发器，每个时钟把本级内容搬到下一级，像「传送带」。延迟链就是一种移位寄存器：数据从传送带头进、从传送带尾出，在带上停留的拍数就是延迟长度。
- **亚稳态（metastability）与同步器（synchronizer）**：当一个异步信号在时钟沿附近变化时，第一级触发器可能输出一段既非 0 也非 1 的「半稳定」电平，要等一会儿才能随机塌缩到合法电平。把信号串两级触发器（两级同步器），能让这种现象塌缩到合法电平的概率大大降低，从而把「不可控」的异步输入变成「几乎不会出错」的同步输入。延迟链天然就是这种「串触发器」结构，所以 `delay` 同时兼任「延迟」和「同步器」两个角色。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| [`delay.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) | 任意信号的静态延迟 / 跨域同步器，多实现版本 v2 | 精读全部 5 个 `generate` 分支 |
| [`delay_tb.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay_tb.sv) | `delay` 的仿真测试平台，波形观察型 | 学习激励写法、读懂 DUT 例化 |
| [`cdc_data.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) | 标准两级同步器——本质就是 `delay` 的封装 | 理解「同步器即延迟链」与 false_path |

> 提示：仓库里 `delay.sv` 的别名也叫 `conveyor.sv`（传送带）、`synchronizer.sv`（同步器），这三个名字描述的是同一种电路，见 INFO 头注释。

## 4. 核心概念与源码讲解

本讲按 4 个最小模块推进：先用最朴素的「寄存器链」建立延迟直觉（4.1），再看 `generate` 如何让一份代码变出多种电路（4.2），接着讲厂商专用的高效实现——SRL 与块 RAM（4.3），最后把延迟链当同步器用并处理 false_path（4.4）。

### 4.1 移位寄存器延迟链

#### 4.1.1 概念说明

「延迟（delay）」要解决的问题是：让一个信号在时间上**整体向后平移 N 个时钟周期**，波形形状不变，只是晚 N 拍出现。最常见的两种用途：

1. **对齐数据通路**：A 路径要算 3 拍、B 路径只要 1 拍，为了让两路结果在同一拍汇合，给 B 路径补 2 拍延迟。
2. **跨时钟域同步**：把异步输入串两级触发器，降低亚稳态风险（4.4 详述）。

实现延迟最直接的办法就是一条**移位寄存器链**：把 N 个触发器首尾相接，输入进第一级，每拍整体右移一位，从最后一级输出。这样输出就比输入晚 N 拍。

输入 `in` 与输出 `out` 之间的延迟关系为

\[
\text{out}(t) = \text{in}\!\left(t - N \cdot T_{\text{clk}}\right), \qquad N = \text{LENGTH}
\]

即延迟的时钟周期数恰好等于链长 `LENGTH`。

#### 4.1.2 核心流程

默认实现（`TYPE="CELLS"`）的执行流程，用伪代码描述：

```
每个 posedge clk:
  若 ~nrst（复位有效）:  所有级清零
  否则若 ena（使能）:
      for i = LENGTH-1 downto 1:   data[i+1] <= data[i]   // 整体上移一位
      data[1] <= in                                      // 新数据进第一级
输出: out = data[LENGTH]                                  // 取最后一级
```

关键点：循环从 `LENGTH-1` 递减到 `1`，目的是**先搬高级、再搬低级**，避免在同一拍内把同一份数据连移两级（非阻塞赋值本身也保证了这点，但写法上仍是从远端开始更直观）。`data[1]` 永远是「刚进来的那一拍」，`data[LENGTH]` 永远是「LENGTH 拍前进来的」。

#### 4.1.3 源码精读

寄存器链实现在 `generate` 的 `else`（即非 Altera 专用分支）里，这是最常用、可移植性最好的实现：

[delay.sv:L190-L205](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L190-L205) —— 默认的「寄存器单元」实现：声明 `LENGTH` 级、每级 `WIDTH` 位的打包数组 `data`，在时钟沿上循环移位，输出取末级。

精简后的核心只有三句：

```verilog
logic [LENGTH:1][WIDTH-1:0] data = '0;   // LENGTH 级，每级 WIDTH 位
always_ff @(posedge clk) begin
  ...
  for(i=LENGTH-1; i>0; i--)
    data[i+1][WIDTH-1:0] <= data[i][WIDTH-1:0];  // 上移
  data[1][WIDTH-1:0] <= in[WIDTH-1:0];           // 进新数据
end
assign out[WIDTH-1:0] = data[LENGTH][WIDTH-1:0]; // 取末级
```

注意打包数组的索引范围是 `[LENGTH:1]`（从 `LENGTH` 到 `1`），而不是常见的 `[0:N-1]`。这样 `data[1]` 是输入端、`data[LENGTH]` 是输出端，语义和「第 1 级、第 LENGTH 级」一一对应，读起来很自然。

模块端口与参数声明在这里：

[delay.sv:L45-L66](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L45-L66) —— 参数 `LENGTH`（链长/延迟拍数）、`WIDTH`（信号位宽）、`TYPE`（实现类型）、`REGISTER_OUTPUTS`（是否给输出加寄存），外加由 `LENGTH` 推导的 `CNTR_W = $clog2(LENGTH)`（块 RAM 分支用作地址/计数位宽，参见 u2-l4）。

#### 4.1.4 代码实践

**目标**：用「脑补波形」验证 `LENGTH=3` 的延迟，建立延迟 = LENGTH 拍的直觉。

**步骤**：纸笔演练。假设 `nrst=1`、`ena=1`，输入序列为 `in = 0,0,5,0,0,0,...`（只有第 2 拍是 5）。逐拍填写下表：

| 拍数 | in | data[1] | data[2] | data[3]=out |
|-----:|----:|--------:|--------:|------------:|
| 0    | 0   | 0       | 0       | 0           |
| 1    | 0   | 0       | 0       | 0           |
| 2    | **5** | 5       | 0       | 0           |
| 3    | 0   | 0       | 5       | 0           |
| 4    | 0   | 0       | 0       | **5**       |

**需要观察的现象**：5 在第 2 拍进入 `data[1]`，经过 3 拍后（第 4 拍）才出现在 `out`。
**预期结果**：`out` 的 5 比输入的 5 晚 `LENGTH=3` 拍。
**说明**：这是纯纸面推演，不依赖任何工具；结论可在第 5 节的综合实践里用仿真确认。

#### 4.1.5 小练习与答案

**练习 1**：如果 `LENGTH=0`，输出相对输入延迟几拍？为什么不在循环里特殊处理？
**答案**：0 拍（纯组合直通）。`LENGTH=0` 时循环 `for(i=-1; i>0; ...)` 一次都不执行，仓库为此专门用了另一个 `generate` 分支直接 `assign out = in`（见 4.2.3），根本不走寄存器链。

**练习 2**：把 `WIDTH` 从 1 改成 8，资源消耗怎么变？
**答案**：链长不变，但每一级从 1 个触发器变成 8 个触发器，总触发器数 = `LENGTH × WIDTH`，按比例线性增长。

---

### 4.2 用 generate 切换多种实现

#### 4.2.1 概念说明

同一个「延迟 N 拍」的功能，在不同场景下最优的实现方式完全不同：

- 延迟很短（几拍）→ 用几个触发器（FF）最划算。
- 延迟很长（几百拍）→ 触发器太贵，应塞进块 RAM（block RAM）。
- Xilinx 器件 → 用 LUT 当移位寄存器（SRL）更省。
- Altera 器件 → 用 altshift_taps 或 scfifo 自动进块 RAM。

如果为每种情况各写一个模块，调用方要记 5 个名字、5 套端口。`delay.sv` 的做法是：**端口和参数完全统一**，内部用 `generate if/else if/else` 在「综合前的展开阶段（elaboration）」挑选实现。对调用方而言，永远是同一个 `delay` 模块，只改 `.TYPE(...)` 就能换底层电路。

要点：`generate` 分支是**编译期**决策。`if (TYPE=="ALTERA_BLOCK_RAM")` 里的 `TYPE` 是参数（常量），综合器在展开时就能算出真假，只保留命中的那个分支变成硬件，其余分支被丢弃。这与运行时的 `if` 完全不同。

#### 4.2.2 核心流程

`generate` 的决策树（`LENGTH` 先分，`TYPE` 再分）：

```
if LENGTH == 0:              → assign out = in               （组合直通）
elif LENGTH == 1:            → 1 个触发器                    （单级延迟）
else (LENGTH >= 2):
    if TYPE=="ALTERA_BLOCK_RAM" 且 LENGTH>=3:
                              → scfifo (块 RAM FIFO)         （Altera 大延迟）
    elif TYPE=="ALTERA_TAPS" 且 LENGTH>=2:
                              → altshift_taps                （Altera 移位抽头）
    else:                     → 寄存器链                      （默认，含 Xilinx SRL 推断）
```

也就是说，参数 `(LENGTH, TYPE)` 一共能命中 5 种电路里的某一种。

#### 4.2.3 源码精读

**`LENGTH==0` 分支**——纯导线，0 延迟、0 触发器：

[delay.sv:L70-L72](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L70-L72) —— `assign out = in`，连时钟都不接，参数化时把链长设为 0 即可让模块「透明」。

**`LENGTH==1` 分支**——单个触发器：

[delay.sv:L74-L84](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L74-L84) —— 一个带同步复位与使能的 `always_ff`，1 拍延迟；这是两级同步器之外的「1 拍打拍」常用写法。

**`else`（`LENGTH>=2`）的总入口**：

[delay.sv:L86-L87](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L86-L87) —— 进入多实现区，第一个子分支判断 `TYPE=="ALTERA_BLOCK_RAM" && LENGTH>=3`。

整个 `generate` 块的收尾：

[delay.sv:L190-L209](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L190-L209) —— `else` 兜底分支即 4.1 讲过的寄存器链；随后 `endgenerate` 闭合，保证最终只有一条分支落地为硬件。

`TYPE` 参数的可选值在头注释里有明确说明：

[delay.sv:L49-L56](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L49-L56) —— `"ALTERA_BLOCK_RAM"` 推断块 RAM FIFO、`"ALTERA_TAPS"` 推断 altshift_taps、其余任意值（含默认 `"CELLS"`）都用触发器实现；`REGISTER_OUTPUTS` 仅对块 RAM 实现有意义，决定末级是否用触发器加一级以改善时序。

#### 4.2.4 代码实践

**目标**：用「阅读 + 对照」理解参数如何路由到不同分支，不依赖任何工具。

**步骤**：

1. 打开 [`delay.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv)。
2. 对下列 4 组参数，判断各自命中哪个 `generate` 分支、最终产生哪种电路：
   - `(LENGTH=0, WIDTH=1)`
   - `(LENGTH=2, WIDTH=1, TYPE="CELLS")`
   - `(LENGTH=10, WIDTH=8, TYPE="ALTERA_BLOCK_RAM")`
   - `(LENGTH=10, WIDTH=8, TYPE="ALTERA_TAPS")`

**预期结果**：依次是「导线直通 / 寄存器链 / scfifo 块 RAM / altshift_taps」。
**说明**：这是源码阅读型实践；第 4 组的 altshift_taps 分支条件是 `LENGTH>=2`，但头注释提示 `tap_distance` 最小为 3（见 4.3.3），实际取值需注意。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `LENGTH=2` 时即使写 `TYPE="ALTERA_BLOCK_RAM"` 也用不上块 RAM？
**答案**：块 RAM 分支的条件是 `TYPE=="ALTERA_BLOCK_RAM" && LENGTH>=3`（[delay.sv:L87](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L87)）。`LENGTH=2` 不满足 `>=3`，会落到 `else` 走寄存器链。注释也写明底层 `scfifo` 的 `LPM_NUMWORDS` 至少要 4。

**练习 2**：如果三个 `generate` 子分支的硬件都被综合进去会怎样？
**答案**：不会发生。`TYPE` 是参数常量，`generate if/else` 是展开期决策，综合器只保留一个命中分支，其余被丢弃。这正是 `generate` 相对运行时 `if` 的核心区别。

---

### 4.3 SRL / block RAM 推断

#### 4.3.1 概念说明

当延迟很长（比如 100 拍、16 位宽）时，寄存器链要消耗 \(100 \times 16 = 1600\) 个触发器，非常浪费。FPGA 上更省的资源是 **块 RAM** 和 **LUT**，厂商提供了把它们当移位寄存器用的机制：

- **Xilinx SRL16E / SRL32E**：一个查找表（LUT）可被配置成一个 16 位或 32 位移位寄存器。16 拍延迟只需 1 个 LUT，远省于 16 个 FF。工具**自动推断**（inference）——你写普通的移位寄存器，工具识别出模式就替换成 SRL。
- **Altera altshift_taps**：Quartus 的「移位寄存器抽头」原语，可把长移位寄存器塞进一个 RAM 块，当「Auto Shift Register Replacement」选项开启时自动替换。
- **Altera scfifo（块 RAM FIFO）**：把延迟实现成一个单时钟 FIFO——持续写入，等 FIFO 填满到 `LENGTH` 字时开始同步读出，读出的数据就比写入晚 `LENGTH` 拍。`USE_EAB="ON"` 强制使用嵌入式阵列块（块 RAM）。

「推断」二字是关键：不是你显式例化原语，而是你写成某种**可识别的模式**，让综合器去匹配。模式越「干净」，越容易被推断。

#### 4.3.2 核心流程

**Xilinx SRL 推断的前提**（来自 INFO 头注释）：

```
要让 Vivado 把寄存器链推断成 SRL16E/SRL32E:
    把 nrst 接常量 1'b1   （不要复位）
    把 ena 接常量 1'b1    （不要使能）
  ⇒ always_ff 退化为纯 "data[i+1] <= data[i]; data[1] <= in;"
  ⇒ 这正是 SRL 能识别的「无复位、无使能的纯移位」模式
```

**Altera 块 RAM FIFO（scfifo）实现延迟的流程**：

```
每个时钟同时:
    写入 in        (wrreq = ena)
    若 FIFO 已填满到 LENGTH 字 (fifo_out_ena): 同时读出 (rdreq)
读出的 q 即比写入晚 LENGTH 拍的数据
REGISTER_OUTPUTS="TRUE" 时再给 q 加一级触发器 → 时序更好，但延迟 +1 由 tap 调整补偿
```

#### 4.3.3 源码精读

**SRL 推断的提示**写在 INFO 头注释：

[delay.sv:L13-L14](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L13-L14) —— 「保持 `nrst=1'b1`、`ena=1'b1` 以便推断 Xilinx 的 SRL16E/SRL32E」。注意源码里 `always_ff` 本身带 `if(~nrst) ... else if(ena)`，只有当这两个端口被绑成常量高电平时，综合器才能把复位/使能逻辑优化掉，露出纯移位模式。

**Altera 块 RAM 分支**——用 `scfifo` 实现延迟：

[delay.sv:L87-L142](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L87-L142) —— 完整的块 RAM 延迟实现。其中：

- [delay.sv:L94-L98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L94-L98) —— `fifo_out_ena` 决定何时开始读：`REGISTER_OUTPUTS="TRUE"` 时等到 `usedw==LENGTH-1`（给末级寄存器预留 1 拍），否则等到 `full`。
- [delay.sv:L100-L126](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L100-L126) —— 例化 `scfifo`，`LPM_NUMWORDS=LENGTH`、`USE_EAB="ON"` 强制块 RAM；`wrreq=ena` 持续写，`rdreq=ena && fifo_out_ena` 条件读。
- [delay.sv:L137-L142](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L137-L142) —— 输出选择：`REGISTER_OUTPUTS="TRUE"` 取寄存后的 `reg_out`，否则直接取 FIFO 的 `q`，并用三元运算避免 first-word-fall-through 提前出数。

**Altera altshift_taps 分支**：

[delay.sv:L144-L188](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L144-L188) —— 用 `altshift_taps` 原语实现。其中：

- [delay.sv:L160-L174](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L160-L174) —— `number_of_taps=1`（单抽头），`tap_distance` 决定延迟长度：`REGISTER_OUTPUTS="TRUE"` 时取 `LENGTH-1`（末级寄存器补 1 拍），否则取 `LENGTH`；头注释标注最小为 3。

> 重要可移植性提示：`ALTERA_BLOCK_RAM` 分支用了 `scfifo`、`ALTERA_TAPS` 分支用了 `altshift_taps`，这二者都是 **Altera/Intel 专有原语**，只能在 Quartus 里综合。**iverilog / ModelSim 仿真通常无法编译这两个分支**（缺少 IP 库）。跨工具仿真请使用默认的 `TYPE="CELLS"`。

#### 4.3.4 代码实践

**目标**：理解「同样的延迟、不同的资源」这件事，准备在第 5 节综合实践里实测。

**步骤**：

1. 在 [`delay.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) 里找到 `LENGTH=1` 单触发器分支与 `LENGTH>=2` 寄存器链分支。
2. 假设要实现 1000 拍、8 位宽的延迟，分别估算三种实现的资源：
   - `CELLS`：约 \(1000 \times 8 = 8000\) 个 FF。
   - `ALTERA_BLOCK_RAM`：1 个块 RAM（存 1000×8 bit）+ 少量控制逻辑。
   - Xilinx SRL（`CELLS` + `nrst=ena=1'b1`）：约 \(8000/16 = 500\) 个 LUT。
3. 写下你的估算，留待用 Quartus 资源报告对照（见第 5 节）。

**预期结果**：长延迟下，块 RAM / SRL 比 FF 节省一到两个数量级的资源。
**说明**：本步为纸面估算，资源数值待本地综合后验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 INFO 提示推断 SRL 时要把 `nrst`、`ena` 都接成 `1'b1`，而源码里明明有复位和使能逻辑？
**答案**：因为参数/端口被常量驱动后，综合器会做常量传播，把 `if(~1'b1) ... else if(1'b1)` 化简成无条件赋值，最终电路里看不到复位/使能，只剩纯移位——这正是 SRL 可识别的模式。若 `nrst`/`ena` 接的是真实可变信号，电路里就多了多路选择逻辑，SRL 推断失败，退回成普通 FF 链。

**练习 2**：`REGISTER_OUTPUTS` 参数在 `ALTERA_BLOCK_RAM` 和 `ALTERA_TAPS` 两个分支里各起什么作用？
**答案**：都是给输出额外加一级触发器以改善时序（fmax 更高），代价是末级不再用块 RAM 实现而占用少量 FF；两个分支都通过把「抽头距离 / 等待计数」减 1 来补偿这一级寄存器带来的额外 1 拍，保证总延迟仍为 `LENGTH`。

---

### 4.4 跨时钟域同步与 false_path

#### 4.4.1 概念说明

把 `delay` 的 `LENGTH` 设成 2、`WIDTH` 设成 1，就得到一个**两级同步器**——这是跨时钟域（CDC, Clock Domain Crossing）处理单线异步信号的标准做法。仓库专门提供了 [`cdc_data.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) 这个封装，它「本质就是 `delay.sv` 的包装」（见其 INFO 头注释）。

但同步器引入一个时序分析的麻烦：第一级触发器的输入来自**另一个异步时钟域**，静态时序分析（STA）无法对这种跨域路径做有意义的建立/保持时间检查——源时钟和目的时钟没有固定相位关系，强行分析只会报一堆「假违例」。解决办法是用 `set_false_path` 把这条路径**排除在时序分析之外**。

仓库用一条很聪明的命名约定让这件事规模化：给每个同步器例化实例的名字加上 `_SYNC_ATTR` 后缀，然后用**一条带通配符的 `set_false_path`** 一次性豁免工程里所有同步器的第一级。

#### 4.4.2 核心流程

两级同步器的工作流程：

```
异步输入 d ──▶ [FF1: data[1]] ──▶ [FF2: data[2]] ──▶ 同步输出 q
                  ↑ 亚稳态可能在这里发生
                  这一级的输入路径必须 set_false_path 排除
              FF2 用同一个 clk 再采一次，给亚稳态留一拍时间塌缩
```

`false_path` 约束的施加（用 `_SYNC_ATTR` 约定）：

```
1. 例化时把同步器实例命名为  xxx_SYNC_ATTR
2. 写一条约束，匹配所有名字含 *_SYNC_ATTR* 的同步器第一级寄存器
   ⇒ 一个工程里不管有多少个同步器，都只需这一条约束
```

#### 4.4.3 源码精读

**`cdc_data` 封装与 false_path 约定**写在 [`cdc_data.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) 的 INFO：

[cdc_data.sv:L6-L20](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L6-L20) —— 说明这是标准两级同步器、是 `delay.sv` 的封装，并给出 Quartus 与 Vivado 的 `set_false_path` 模板，关键是实例名带 `_SYNC_ATTR` 后缀。

封装本体——就是一个 `LENGTH=2` 的 `delay`：

[cdc_data.sv:L43-L55](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L43-L55) —— 例化名为 `data_SYNC_ATTR` 的 `delay`，参数 `LENGTH=2, WIDTH=1, TYPE="CELLS"`。这正是「同步器 = 长度为 2 的延迟链」的直接体现。

两个约束模板（直接抄自上面 INFO 注释）：

```tcl
# Quartus：豁免所有 *_SYNC_ATTR* 同步器的 data[1]（第一级）
set_false_path -to [get_registers {*delay:*_SYNC_ATTR*|data[1]*}]

# Vivado：同样豁免第一级 data_reg[1]
set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]
```

为什么目标是 `data[1]`（第一级）而不是 `data[2]`？因为异步信号正是冲着第一级 FF 去的、亚稳态发生在第一级；`-to data[1]` 把「到达第一级」的路径移出分析。而第一级到第二级（`data[1]→data[2]`）是同一时钟域内的正常路径，**仍要按时序分析**——这恰恰是让第二级可靠采样所必需的。

`delay.sv` 的头注释也专门提醒了这件事：

[delay.sv:L16-L18](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L16-L18) —— 「本模块常用于跨时钟域同步；同步时请用合适的 `set_false_path` 手动把输入数据路径排除出时序分析」。

#### 4.4.4 代码实践

**目标**：把一条异步输入接到同步器，并写出对应的 false_path 约束（约束写作型实践）。

**步骤**：

1. 假设有 32 位外部异步输入 `ext_data[31:0]`，照搬 `cdc_data` 的例化模板（见 [cdc_data.sv:L26-L32](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L26-L32)），例化 32 个同步器。
2. 在工程的 `.sdc`（Quartus）或 `.xdc`（Vivado）里写一条 `set_false_path`，用通配符覆盖这 32 个实例。

**预期结果**：只需一条约束即可覆盖全部 32 条异步输入路径；综合后时序报告里这些路径不再出现违例。
**说明**：约束写法见 4.4.3 的两段 Tcl。完整工程级实践见 u7-l2（时序约束讲义）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `false_path` 要豁免第一级 `data[1]`，却**不能**把第二级 `data[2]` 也豁免？
**答案**：第一级是亚稳态高发点，跨域到达它的路径无法有意义地分析，必须豁免。第二级 `data[2]` 的输入（来自 `data[1]`）是同一时钟域、且正是「让亚稳态塌缩后可靠采样」的关键一拍——它的建立/保持时间必须满足，所以**必须保留**时序分析。若连第二级也豁免，等于放弃了同步器唯一可控的那一拍。

**练习 2**：如果不写 `false_path`，工程能综合通过吗？会有什么后果？
**答案**：能综合、能上板，但时序报告会针对跨域路径报出一堆「无法满足」的假违例，掩盖真正的问题，也可能迫使工具为不可能闭合的路径过度优化、拖慢编译。加 `false_path` 是为了让 STA 聚焦于真正可分析的路径，与电路能否工作无关。

---

## 5. 综合实践

本实践把本讲四个模块串起来：例化 `LENGTH=5, WIDTH=4` 的 `delay`，用**自包含的最小 testbench**（不依赖 `clk_divider`/`c_rand` 等附加模块，便于直接编译）输入一个脉冲，验证输出恰好滞后 5 个时钟周期；再讨论改 `TYPE` 对资源的影响。

### 5.1 实践目标

- 验证 `out(t) = in(t - 5·T_clk)`，即延迟精确等于 `LENGTH`。
- 体会「`TYPE` 改变的是底层资源，不改延迟语义」。
- 区分「可仿真」与「需综合才能对比资源」两类操作。

### 5.2 操作步骤

**第 1 步**：在仓库根目录新建一个 testbench 文件（**示例代码**，不属于原仓库，请自行创建，例如命名为 `delay_practice_tb.sv`）：

```verilog
`timescale 1ns / 1ps          // 单位 1ns，精度 1ps（参见 u1-l3）
module delay_practice_tb();

  logic clk;                  // 100 MHz 时钟：半周期 5ns
  initial begin #0 clk = 1'b1; forever #5 clk = ~clk; end

  logic nrst;                 // 低有效同步复位，12ns 后释放
  initial begin #0 nrst = 1'b0; #12 nrst = 1'b1; end

  logic [3:0] in;
  logic [3:0] out;

  delay #(                     // LENGTH=5, WIDTH=4, 默认 CELLS
    .LENGTH( 5 ),
    .WIDTH ( 4 ),
    .TYPE  ( "CELLS" )         // 仿真请用 CELLS；ALTERA_* 需 Quartus
  ) dut (
    .clk( clk ), .nrst( nrst ), .ena( 1'b1 ),
    .in ( in  ), .out ( out  )
  );

  integer hit;                // 记录 out 出现脉冲的边沿序号
  initial begin
    $dumpfile("delay_practice.vcd");
    $dumpvars;
    in  = 4'h0;
    @(posedge clk);                       // 边沿 1
    while (~nrst) @(posedge clk);         // 等复位释放
    @(posedge clk);  in = 4'hA;           // 边沿 T0：dut 采到 in=A
    @(posedge clk);  in = 4'h0;           // 边沿 T0+1：脉冲只持续 1 拍
    for (hit=1; hit<=8; hit=hit+1) begin
      @(posedge clk);                     // 逐边沿观察
      if (out === 4'hA)
        $display("out=A 检测到，相对 in 注入后的第 %0d 个 posedge", hit);
    end
    $finish;
  end
endmodule
```

**第 2 步**：用 iverilog 编译运行（需 `-g2012` 支持 SystemVerilog）：

```bash
iverilog -g2012 -o delay_practice.vvp delay.sv delay_practice_tb.sv
vvp delay_practice.vvp
```

**第 3 步**：用 GTKWave 打开 `delay_practice.vcd` 观察波形：

```bash
gtkwave delay_practice.vcd &
```

### 5.3 需要观察的现象

- `in` 的脉冲 `4'hA` 只持续 1 个时钟周期。
- `out` 在大约 5 个 `posedge clk` 之后出现同一个 `4'hA`，且也只持续 1 拍（波形形状被完整平移，没有展宽或缩窄）。
- 终端 `$display` 会打印 `out=A 检测到，相对 in 注入后的第 5 个 posedge`（精确边沿计数**待本地验证**：取决于你把「注入拍」记为第 0 还是第 1，结论是两者相差 `LENGTH=5` 拍）。

### 5.4 预期结果

`out` 是 `in` 的精确 5 拍平移，延迟 = `LENGTH`。若把 `.LENGTH(5)` 改成 `.LENGTH(8)`，`out` 的脉冲会相应晚 8 拍出现——延迟随 `LENGTH` 线性变化。

### 5.5 改 TYPE 对比资源（需 Quartus，待本地验证）

把例化参数改成下面三种之一，分别在 **Quartus** 里综合（iverilog 无法处理 Altera 专有原语，本步只能在 Quartus 完成）：

| TYPE | 预期实现 | 预期资源（LENGTH=5, WIDTH=4） |
|------|----------|------------------------------|
| `"CELLS"`（默认） | 5×4=20 个 FF | 20 个 FF |
| `"ALTERA_BLOCK_RAM"` | scfifo 块 RAM | 1 个 MLAB/EAB 块 + 少量 FF（注意 LENGTH=5 满足 ≥3） |
| `"ALTERA_TAPS"` | altshift_taps | 头注释提示 tap_distance 最小为 3，LENGTH=5 可用；资源以 Quartus 报告为准 |

> 说明：`LENGTH=5` 较小，块 RAM 的面积优势体现不出来；要看出块 RAM/SRL 的节省，建议另起一次实验把 `LENGTH` 加大到 64 或 256 再对比。**三种 TYPE 的仿真波形在「延迟拍数」上应完全一致**（差异只在底层资源），这是 `generate` 多实现「同行为、异实现」的最佳印证。

---

## 6. 本讲小结

- `delay` 是一条**移位寄存器延迟链**：输出 = 输入延迟 `LENGTH` 个时钟周期，延迟 \(\text{out}(t)=\text{in}(t-N\cdot T_\text{clk})\)。
- 它用**一个 `generate` 块**按 `(LENGTH, TYPE)` 产出 5 种电路：`LENGTH==0` 导线直通、`LENGTH==1` 单触发器、`LENGTH>=2` 默认寄存器链、`ALTERA_BLOCK_RAM`(scfifo)、`ALTERA_TAPS`(altshift_taps)。
- **Xilinx SRL 推断**的前提是 `nrst=1'b1`、`ena=1'b1`，让复位/使能逻辑被常量传播优化掉，露出纯移位模式；**Altera** 靠 scfifo（块 RAM）或 altshift_taps（移位抽头）实现大延迟，节省 FF。
- `ALTERA_BLOCK_RAM`/`ALTERA_TAPS` 两个分支依赖 Altera 专有原语，**只能在 Quartus 综合**；跨工具仿真用默认 `CELLS`。
- 把 `delay` 设成 `LENGTH=2` 即**两级同步器**，[`cdc_data.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) 就是它的封装。
- 跨域同步时**必须**用 `set_false_path` 豁免第一级（亚稳态那级）；用 `_SYNC_ATTR` 命名约定可让一条约束管住工程里所有同步器，且不能把第二级也豁免。

## 7. 下一步学习建议

- **下一讲 u2-l4（clogb2 与 $clog2）**：本讲里 `CNTR_W = $clog2(LENGTH)` 出现在块 RAM 分支的计数位宽里，去 u2-l4 搞清楚地址位宽与计数位宽的边界差异。
- **进入 u3 单元（时钟域跨越）**：本讲的 4.4 已经触到 CDC 的边缘，下一讲 u3-l1 会正式展开两级同步器、亚稳态与 MTBF；u3-l2 的 `cdc_strobe` 则解决「单拍脉冲跨域」这一 `cdc_data` 处理不了的难题。
- **延伸阅读**：对比 [`delay_tb.sv`](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay_tb.sv) 里 `d1`(CELLS) 与 `d2`(ALTERA_BLOCK_RAM) 两个例化，体会同一 testbench 同时驱动两种实现的做法；时序约束的工程级实战留在 u7-l2。
