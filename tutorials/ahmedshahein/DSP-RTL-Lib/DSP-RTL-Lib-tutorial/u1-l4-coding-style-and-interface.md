# 统一编码风格与接口约定

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 DRL 全库统一的**时序四约定**：异步低有效复位、同步高有效使能、仅用上升沿触发器、二进制补码定点运算。
- 一眼读懂 `gp_/c_/r_/w_` 四类**命名前缀**各自的含义，并能区分 `reg` 与 `wire` 关键字和 `r_/w_` 前缀之间的细微差别。
- 把 [`dff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) 当作"标准接口范本"，照它的端口顺序、注释骨架、`always` 模板**复刻**出一个同风格的新寄存器模块。

本讲是后续所有模块讲义的"语法地基"：FIR、CIC、多相、CORDIC 里的每一个触发器、每一段计数器，都遵守这里讲到的约定。

---

## 2. 前置知识

在读本讲之前，你需要大概了解以下几个名词（不要求精通，有印象即可）：

- **触发器（Flip-Flop / DFF）**：数字电路里最基本的"记忆单元"。它有一个时钟输入，每来一个时钟边沿，就把输入端的值"锁存"到输出，并一直保持到下一个边沿。DRL 里只用**上升沿**（电平由 0 跳到 1 的瞬间）触发的触发器。
- **时钟（clock）**：一串周期性的方波，像节拍器一样驱动整个电路一步步前进。DRL 里时钟信号通常叫 `i_clk`。
- **复位（reset）**：让电路回到一个已知初始状态（通常是 0）的操作。"低有效"指信号为 `0` 时才触发复位，为 `1` 时正常工作；"异步"指复位一旦有效就立刻生效，不必等时钟边沿。DRL 里复位信号叫 `i_rst_an`（`_an` = active-low 的缩写）。
- **使能（enable）**：一个"开关"信号。为 `1` 时电路正常采样输入，为 `0` 时电路"原地踏步"保持上一拍的值。DRL 里使能叫 `i_ena`，高有效。
- **Verilog**：一种硬件描述语言，用代码描述数字电路。本讲会读少量 Verilog，但会逐句解释。
- **二进制补码（2's complement）**：计算机里表示带符号整数的标准方式。本讲只需知道"DRL 的算术运算都建立在补码定点数之上"即可，深入内容留到 u2-l1。

如果你已经学过 u1-l1（项目总览）和 u1-l2（仓库结构），会更容易进入状态。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md) | 项目的 `## Coding Style` 章节用四条 bullet 写明了全库时序铁律，是本讲的"权威出处"。 |
| [.drl_src_code/filt_cicd/rtl/dff.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) | 一个参数化的 D 触发器，是全库所有时序单元的**原子构建块**，也是本讲的范本。 |
| [.drl_src_code/filt_cicd/rtl/shift_register.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v) | 用 `generate` 把多个 `dff` 串成移位寄存器，提供了最干净的 `gp_/c_/r_/w_` 命名范例。 |
| [.drl_src_code/filt_cicd/rtl/filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v) | CIC 抽取滤波器顶层，综合演示命名前缀与时序块在真实大模块里的用法。 |
| [.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv) | 测试平台，演示 `i_rst_an/i_ena/i_clk` 在仿真里**如何被驱动**，反向印证时序约定。 |

---

## 4. 核心概念与源码讲解

### 4.1 统一时序约定

#### 4.1.1 概念说明

DRL 是一个由许多模块拼装起来的库（参见 u1-l1 的 mix-and-match 理念）。要让这些模块能够"即插即用"地连在一起，作者定下了一套**全库统一的时序契约**。这套契约写在 [README.md:47-53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L47-L53) 的 `## Coding Style` 章节里，原文是四条 bullet：

> - Asynchronous active low reset（异步、低有效复位）
> - Synchronous active high enable（同步、高有效使能）
> - Rising edge flip-flops are only used（只用上升沿触发器）
> - Arithmetic computations are carried out based on fixed-point 2's complement data type（算术运算基于二进制补码定点数）

把这四条翻译成"设计纪律"，意思是：

1. **所有寄存器都共用同一套"复位—使能—采样"的三段式逻辑骨架**，不管模块多复杂。
2. **复位优先级最高**：只要 `i_rst_an = 0`，寄存器立即清零，连时钟边沿都不必等。
3. **使能是"门控"**：复位无效时，只有 `i_ena = 1` 的那个上升沿，寄存器才更新；否则保持原值。
4. **算术一律按补码定点数处理**，这关系到后续位宽推导（u2-l1 详讲）。

作者在 README 里特别强调：这套风格是为了"在功能验证和形式验证期间方便调试排错"，并请读者**严格遵守**。所以理解它，是读懂全库任何 RTL 的前提。

#### 4.1.2 核心流程

每个时序寄存器的行为都可以用下面这个统一的状态流程来描述：

```
每个 D 触发器在每个时刻都遵循：
┌─────────────────────────────────────────────┐
│ 1. i_rst_an == 0 ?                          │
│       是 → 立即把输出清零（异步，不等时钟） │
│       否 → 进入第 2 步                       │
│                                             │
│ 2. 来了一个 i_clk 上升沿 ?                   │
│       否 → 什么都不做，输出保持              │
│       是 → 进入第 3 步                       │
│                                             │
│ 3. i_ena == 1 ?                             │
│       是 → 把 i_data 锁存到输出（采样）     │
│       否 → 输出保持不变                      │
└─────────────────────────────────────────────┘
```

对应的 Verilog 几乎是"套路化"的一段 `always` 块，伪代码如下：

```verilog
always @(posedge i_clk or negedge i_rst_an)   // 上升沿 或 复位下降沿 触发
  begin: p_xxx                                 // p_ 前缀：过程块标签
    if (!i_rst_an)                             // 第 1 步：复位优先
      r_xxx <= 'd0;
    else if (i_ena)                            // 第 3 步：使能才采样
      r_xxx <= i_data;
    // 使能为 0 时什么都不写，自然"保持"
  end
```

两个关键细节：

- **敏感列表里的 `or negedge i_rst_an`**：正是这一句让复位变成"异步"——只要 `i_rst_an` 出现下降沿（从 1 到 0），`always` 块就立刻被执行，不必等 `i_clk`。
- **`if` 里先判断复位、再判断使能**：这保证了复位永远比使能优先。

#### 4.1.3 源码精读

我们看 DRL 是怎么把这四条约定落到代码里的。最权威的"标准答案"就是 [`dff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) —— 一个参数化的 D 触发器。

先看端口声明（[dff.v:10-14](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L10-L14)），三句话就把四条约定占了三条：

```verilog
input  wire i_rst_an,   // Asynchronous active low reset   → 约定①②：异步低有效复位
input  wire i_ena,      // Synchronous active high enable  → 约定③：同步高有效使能
input  wire i_clk,      // Rising-edge clock               → 约定④：上升沿时钟
```

再看时序本体（[dff.v:19-25](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L19-L25)），这正是 4.1.2 里那段伪代码的真实形态：

```verilog
always @(posedge i_clk or negedge i_rst_an)   // 复位进敏感列表 → 异步
  begin: p_dff
    if (!i_rst_an)        r_data <= 'd0;       // 复位优先，清零
    else if (i_ena)       r_data <= i_data;    // 使能才采样
  end
```

这段 7 行代码就是全库**所有时序逻辑的母版**。再举一个更复杂模块里的例子：CIC 抽取滤波器的环形计数器（[filt_cicd.v:39-55](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L39-L55)）——尽管它做的是"环形移位"而不是简单采样，骨架却完全一样：

```verilog
always @(posedge i_clk or negedge i_rst_an)
  begin: p_ring_counter
    if (!i_rst_an)
      r_count <= 'd0;                         // 复位优先
    else if (i_ena)
      begin ... r_count ... end               // 使能后才推进
  end
```

最后，我们反向印证一下"低有效复位、高有效使能"在仿真里到底怎么驱动。看测试平台 [filt_cicd_tb.sv:31-45](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L31-L45)：

```verilog
initial begin
       i_rst_an = 1'b1;        // 先保持无效（高）
  #170 i_rst_an = 1'b0;        // 拉低 → 有效，触发异步复位
  #205 i_rst_an = 1'b1;        // 再拉高 → 复位结束
end
initial begin
       i_ena = 1'b0;           // 先关使能
  #400 i_ena = 1'b1;           // 后开使能
end
initial i_clk = 1'b0;
always i_clk = #(CLK_PERIOD) ~i_clk;   // 周期翻转 → 产生上升沿
```

可以看到：复位是靠**拉低**生效的，使能是靠**拉高**生效的，时钟靠 `~` 周期翻转自然产生上升沿——和 README 的四条约定一一对应。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试平台，亲手在脑中"跑"一遍复位和使能的时序，确认你理解了"异步低有效复位"和"同步高有效使能"。

**操作步骤**：

1. 打开 [filt_cicd_tb.sv:31-45](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L31-L45)。
2. 注意 `CLK_PERIOD = 50`（ns），所以时钟上升沿出现在 t = 50、100、150、… ns。
3. 在草稿纸上画一条时间轴，标出 `i_rst_an` 和 `i_ena` 在以下时刻的值：t=0、t=170、t=375、t=400。

**需要观察的现象**：

- 复位有效窗口是 t = 170 ~ 375 ns（`i_rst_an` 为 0 的这段时间）。
- 在这个窗口内，即使有 `i_clk` 上升沿，`dff` 的输出也应当是 0——这就是"异步"和"复位优先"。
- 使能在 t = 400 ns 才拉高，所以 t = 400 之前的上升沿即使复位已释放，输出也不会更新——这就是"同步使能门控"。

**预期结果**：你能指出"复位段的起点和终点都不依赖时钟边沿"，而"使能只在时钟上升沿起作用"。这正是 `dff.v` 那段 `always` 块的语义。

> 说明：本实践是"源码阅读型"，不要求运行命令。如果你想亲手验证，可在安装了 Icarus Verilog 的环境用 u1-l3 讲到的 `./dsp_rtl_lib.sh -demo` 跑一次 CIC 回归，再用 GTKWave 打开生成的 `.vcd` 文件观察 `i_rst_an / i_ena / i_clk` 三条波形。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `dff.v` 的敏感列表改成 `always @(posedge i_clk)`（删掉 `or negedge i_rst_an`），复位会变成什么性质？模块行为会有什么变化？

**参考答案**：复位会从"异步"变成"同步"——`i_rst_an` 拉低后，必须等到下一个 `i_clk` 上升沿，输出才会清零，而不能立即清零。这违背了 DRL 的"异步低有效复位"约定，因此在本库里属于不符合编码风格的写法。

**练习 2**：在 `dff.v` 的 `always` 块里，如果把 `if (!i_rst_an)` 和 `else if (i_ena)` 的顺序对调（先判使能再判复位），会出什么问题？

**参考答案**：复位会失去最高优先级——当 `i_rst_an = 0` 且 `i_ena = 1` 同时成立时，模块会去采样输入而不是清零。在真实的复位过程中这会导致寄存器无法回到已知状态，是典型的时序逻辑 bug。所以"复位优先"不仅是风格，更是正确性要求。

---

### 4.2 命名前缀规范

#### 4.2.1 概念说明

DRL 的每个模块动辄有几十个信号：参数、常量、寄存器、内部连线。如果命名随意，读代码的人就得反复跳转才能搞清楚"这个信号到底是什么"。为此，作者给所有标识符加上了**含义前缀**，让你光看名字就知道它的"身份"。全库统一使用下面四类前缀：

| 前缀 | 含义 | Verilog 关键字 | 典型例子 |
| --- | --- | --- | --- |
| `gp_` | **g**eneric **p**arameter，可被上层覆盖的设计参数 | `parameter` | `gp_data_width`、`gp_order` |
| `c_` | **c**onstant，模块内部派生出的常量 | `localparam` | `c_cnt_width`、`c_fill_width` |
| `r_` | **r**egister，"保存在触发器里"的寄存器值 | `reg` 或 `wire`（见 4.2.3 进阶说明） | `r_data`、`r_count` |
| `w_` | **w**ire，组合逻辑产生的内部连线 | `wire` | `w_sclk`、`w_data` |

此外还有两类**块标签**前缀，用于给 `always`/`generate` 块起名，方便仿真和调试时定位：

- `p_`：**p**rocess，`always` 过程块的标签，如 `p_dff`、`p_ring_counter`。
- `g_`：**g**enerate，`generate` 生成块的标签，如 `g_integrator`、`g_comb`。

> 给初学者：Verilog 里 `parameter` 和 `localparam` 的区别在于——`parameter` 可以在例化时被上层改写（所以叫"通用参数"），`localparam` 是模块内部计算出来、不允许上层改写的局部常量（所以叫"常量"）。

#### 4.2.2 核心流程

看到一个陌生的标识符，按下面的"决策树"判断它的前缀即可秒懂其身份：

```
名字以 … 开头？
├─ gp_  → 这是设计参数（parameter），例化时可改，由 .param 注入（见 u1-l3）
├─ c_   → 这是派生常量（localparam），模块内部算出来，不可外部改
├─ r_   → 这是一个"寄存器值"，时序状态
│        ├─ 紧跟在当前模块 always 块里被赋值？ → 用 reg 声明
│        └─ 是某个被例化子模块的输出端口？      → 用 wire 声明（见 4.2.3）
├─ w_   → 这是组合逻辑连线（wire），无记忆
└─ i_ / o_ → 端口方向：i_ 输入，o_ 输出
```

这套前缀带来的直接好处是：读 RTL 时你不必再看声明，光看赋值语句左侧的名字，就知道这一行是在"改寄存器状态"（`r_`）还是"算一根组合线"（`w_`）。

#### 4.2.3 源码精读

最干净的命名范例来自 [`shift_register.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v)。它的声明区被作者用注释明确分成了三段（[shift_register.v:18-25](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L18-L25)）：

```verilog
// CONSTANT DECLARATION
localparam c_cnt_width = $clog2(gp_nr_stages);   // c_ ：派生常量
// REGISTER DECLARATION
reg [c_cnt_width-1:0] r_cnt;                     // r_ ：reg 寄存器
reg                   r_shift_done;              // r_ ：reg 寄存器
// WIRE DECLARATION
wire                  w_shift_done;              // w_ ：组合线
wire [...]            w_data;                    // w_ ：组合线
```

可以看到 `c_/r_/w_` 与 `localparam/reg/wire` 一一对应，非常整齐。`gp_nr_stages`、`gp_data_width` 则出现在模块头的 `parameter` 列表里（[shift_register.v:6-9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L6-L9)），属于 `gp_`。

**进阶说明（`r_` 为什么有时是 `wire`）**：当一个寄存器值不是在本模块的 `always` 块里赋值，而是**来自一个被例化的子模块的输出端口**时，Verilog 语言规定：模块实例的输出只能连到 `wire` 上。所以会出现"`r_` 前缀但用 `wire` 声明"的情况——前缀表达的是语义（"这是一个寄存器值"），关键字表达的是驱动方式。一个典型例子在 CIC 抽取滤波器里（[filt_cicd.v:20-32](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L20-L32)）：

```verilog
// REGISTER DECLARATION
wire w_sclk;                                   // w_：组合选择线（assign 出来的）
wire signed [...] r_comb_inp;                  // r_ 但 wire：来自下采样 dff 实例的输出
reg  [...]         r_count;                    // r_ 且 reg ：本模块 always 里赋值
wire signed [...] r_int_dly;                   // r_ 但 wire：来自积分器 dff 实例的输出
```

其中 `r_comb_inp` 和 `r_int_dly` 虽然声明成 `wire`，但它们接的是 `dff` 实例的 `o_data`（[filt_cicd.v:101-109](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L101-L109)），所以语义上是"寄存器值"；而 `w_sclk` 是 `assign w_sclk = r_count[gp_phase]`（[filt_cicd.v:100](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L100)）算出来的纯组合选择信号，所以是 `w_`。记住一句话：**`r_/w_` 看"有没有记忆"，`reg/wire` 关键字看"怎么被驱动"。**

至于块标签 `p_` 和 `g_`，前面 4.1.3 里 `dff.v` 的 `begin: p_dff`（[dff.v:20](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L20)）就是 `p_` 用法；`filt_cicd.v` 里的 `begin: g_integrator`（[filt_cicd.v:67](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L67)）则是 `g_` 用法。这些标签在仿真器报错或波形层级里能帮你快速定位到具体块。

#### 4.2.4 代码实践

**实践目标**：在不看声明的情况下，仅凭前缀判断信号身份，验证你已经掌握命名约定。

**操作步骤**：

1. 打开 [filt_cicd.v:20-32](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L20-L32)。
2. 对下列 6 个信号，先**只看名字**写出你认为的"身份 + 关键字"，再对照声明核对：
   - `w_sclk`、`r_comb_inp`、`r_count`、`r_int_dly`、`w_data`、`w_int_add`
3. 对每个 `r_` 信号，进一步判断它"在本模块 always 里赋值，还是来自子模块输出"。

**需要观察的现象**：你会发现 `r_count` 是唯一一个 `reg`（在本模块 `p_ring_counter` 块里赋值），其余 `r_` 都来自 `dff`/`shift_register` 实例的输出，因此是 `wire`。

**预期结果**：你能说出"`r_` 前缀统一表示寄存器值，但只有在本模块赋值的才是 `reg`，来自子模块输出的是 `wire`"。

> 说明：本实践为"源码阅读型"，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：在 [shift_register.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v) 中，`c_cnt_width` 为什么用 `localparam` 而不是 `parameter`？

**参考答案**：因为 `c_cnt_width = $clog2(gp_nr_stages)` 是从 `gp_nr_stages` 派生出来的，它的值完全由另一个参数决定，不应当、也不需要被上层单独改写。用 `localparam` 正好表达"我只接受派生、拒绝外部覆盖"的意图；如果误用 `parameter`，上层就可能给它一个与 `gp_nr_stages` 不自洽的值，埋下 bug。

**练习 2**：端口名 `i_data` 和 `o_data` 里的 `i_`/`o_` 属于哪一类前缀？它和 `gp_/c_/r_/w_` 描述的是同一件事吗？

**参考答案**：`i_/o_` 描述的是**端口方向**（input / output），而 `gp_/c_/r_/w_` 描述的是**信号身份/存储性质**。它们是两套正交的维度：一个端口的方向由 `i_/o_` 体现，而它内部的实现信号则用 `gp_/c_/r_/w_` 体现。例如 `o_data` 是输出端口，而它内部由 `r_data` 这个寄存器驱动（`assign o_data = r_data`）。

---

### 4.3 dff 标准接口范本

#### 4.3.1 概念说明

`dff` 是 DRL 里最简单、也最重要的模块——一个参数化的 D 触发器。它之所以重要，是因为**全库所有的时序单元都是用它搭出来的**：移位寄存器是多个 `dff` 串联（见 u2-l3），CIC 的积分器/微分器延迟是 `dff`（见 u4-l1），多相滤波器的换向器也是 `dff`（见 u5-l2）。

更进一步，`dff` 还承担了"**接口范本**"的角色：它的端口顺序、注释骨架、`always` 写法，就是全库每个模块应当模仿的样板。如果你要给 DRL 写一个新模块（参见 u7-l3 的 `-dev` 脚手架），照着 `dff` 写就对了。

值得一提的是，`dff.v` 在仓库里被**原样复制**到了 5 个模块目录下（`filt_cicd/cici/fir/ppd/ppi` 各一份），内容完全一致——这是作者刻意为之，让每个模块目录都能自包含地仿真，而不必跨目录引用公共文件。

#### 4.3.2 核心流程

一个"标准 DRL 模块"从头到尾的骨架是这样的（以 `dff` 为模板）：

```
1. 版权注释头   —— 三段 // ---- 分隔的固定格式
2. module 声明  —— module 名 + #( parameter gp_... ) + ( input/output 端口 )
3. 端口顺序     —— 固定为 i_rst_an, i_ena, i_clk, i_data, o_data
4. 内部声明     —— 用 // CONSTANT/REGISTER/WIRE DECLARATION 分段
5. 时序逻辑     —— always @(posedge i_clk or negedge i_rst_an) 三段式
6. 组合输出     —— assign o_xxx = r_xxx;
7. endmodule
```

端口顺序尤其值得记住：**复位、使能、时钟、数据输入、数据输出** 这个固定顺序，让你在任何模块的例化代码里都能一眼对上号。

#### 4.3.3 源码精读

我们把 [`dff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) 逐段拆开看。

**① 版权注释头**（[dff.v:1-5](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L1-L5)）：用 `// ---` 分隔块标注版权，全库统一格式。

**② 参数与端口**（[dff.v:6-15](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L6-L15)）：

```verilog
module dff #(
  parameter gp_data_width = 8                  // 唯一参数：数据位宽，默认 8
) (
  input  wire                     i_rst_an,    // 异步低有效复位
  input  wire                     i_ena,       // 同步高有效使能
  input  wire                     i_clk,       // 上升沿时钟
  input  wire [gp_data_width-1:0] i_data,      // 输入
  output wire [gp_data_width-1:0] o_data       // 输出
);
```

注意几个范本细节：参数用 `gp_` 前缀并带默认值 `= 8`；端口顺序固定；每个端口右侧都有行尾注释说明语义——这种"端口即文档"的写法全库通用。

**③ 内部寄存器声明**（[dff.v:17](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L17)）：`reg [gp_data_width-1:0] r_data;`——一个 `r_` 前缀的 `reg`，位宽跟随参数。

**④ 时序本体**（[dff.v:19-25](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L19-L25)）：即 4.1.3 分析过的三段式 `always` 块，复位清零、使能采样。

**⑤ 组合输出**（[dff.v:27](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L27)）：`assign o_data = r_data;`——把寄存器值用一根 `wire` 引到输出端口。这是 DRL 的标准收尾：**寄存器内部用 `reg`，输出端口用 `wire`，二者用 `assign` 桥接**。

为了证明这套范本是"活"的，看测试平台是怎么按固定端口顺序例化它的（[filt_cicd_tb.sv:107-120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L107-L120)）——`.i_rst_an / .i_ena / .i_clk / .i_data / .o_data` 一一对齐，和 `dff` 的端口顺序完全一致。

#### 4.3.4 代码实践

**实践目标**：以 `dff.v` 为模板，亲手写一个同风格的 12 位带使能寄存器 `my_dff`，把本讲三件事（时序约定、命名前缀、接口范本）一次落地。

**操作步骤**：

1. 新建一个文件 `my_dff.v`（放在你自己的工作目录，**不要**改动仓库源码）。
2. 完全照搬 `dff.v` 的骨架：版权头、`module`+`#(parameter gp_data_width=12)`、固定端口顺序、`r_data` 声明、三段式 `always`、`assign` 输出。
3. 把默认位宽改成 `12`，并给每一段加上**中文注释**说明它在做什么。

下面是一份**示例代码**（仅供对照，不是仓库原有文件）：

```verilog
// -------------------------------------------------------------------
// 示例代码：my_dff —— 12 位带使能寄存器（仿照 dff.v 风格）
// -------------------------------------------------------------------
module my_dff #(
  parameter gp_data_width = 12                     // 数据位宽，默认 12 位
) (
  input  wire                     i_rst_an,        // 异步低有效复位
  input  wire                     i_ena,           // 同步高有效使能
  input  wire                     i_clk,           // 上升沿时钟
  input  wire [gp_data_width-1:0] i_data,          // 输入数据
  output wire [gp_data_width-1:0] o_data           // 输出数据
);
// -------------------------------------------------------------------
  reg [gp_data_width-1:0] r_data;                  // 寄存器状态（r_ 前缀）
// -------------------------------------------------------------------
  always @(posedge i_clk or negedge i_rst_an)      // 上升沿 + 异步复位
    begin: p_my_dff                                // p_ 前缀：过程块标签
      if (!i_rst_an)
        r_data <= 'd0;                             // 复位：清零
      else if (i_ena)
        r_data <= i_data;                          // 使能：采样输入
    end

  assign o_data = r_data;                          // 组合桥接到输出端口
endmodule
```

**需要观察的现象**：把你的 `my_dff.v` 和原版 [dff.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) 并排对比，确认二者骨架完全一致，只有位宽默认值和注释语言不同。

**预期结果**：你的 `my_dff.v` 满足以下全部检查点——

- 端口顺序为 `i_rst_an, i_ena, i_clk, i_data, o_data`；
- 参数名是 `gp_data_width`，默认值为 `12`；
- 敏感列表包含 `posedge i_clk or negedge i_rst_an`；
- 复位分支写在 `if`、使能分支写在 `else if`；
- 输出用 `assign o_data = r_data;`。

> 说明：本实践不要求运行命令，重点是"风格复刻"。能否编译通过请见第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dff` 内部用 `reg r_data` 存状态，却把输出端口 `o_data` 声明成 `wire`？直接把 `o_data` 声明成 `reg`、在 `always` 里赋值不行吗？

**参考答案**：在 Verilog 里，`always` 块只能驱动 `reg` 型，而 `assign` 和模块输出端口默认是 `wire` 型。`dff` 选择"内部 `reg` + `assign` 桥接到 `wire` 输出端口"的写法，是为了让输出端口保持 `wire` 性质，方便上层直接把它连进各种组合逻辑或 `wire` 网络而不必额外声明。如果硬把 `o_data` 声明成 `reg`，虽然功能也能实现，但就脱离了 DRL 的统一范本，且与全库"`r_` 内部 / `w_/o_` 输出"的约定不一致。

**练习 2**：`dff.v` 被原样复制到 5 个模块目录下。这种"复制而非公共引用"的做法，好处和代价分别是什么？

**参考答案**：好处是每个模块目录**自包含**——只需进入该目录就能独立编译仿真，不必配置跨目录的文件包含路径，也避免了"改了一处公共文件、意外影响所有模块"的风险，非常契合 DRL 现场生成、独立回归的流程（见 u1-l3）。代价是**重复**：如果将来要修 `dff` 的 bug，得同步改 5 份。在 DRL 当前阶段（追求正确性与可复用，尚未做面积/功耗优化），作者认为自包含带来的工程便利大于重复的成本。

---

## 5. 综合实践

把本讲的三块知识串起来：为你在 4.3.4 写的 `my_dff` 配一个**最小测试平台**，用 Icarus Verilog 编译运行，亲眼看到"异步复位优先、同步使能门控"的行为。

**实践目标**：验证 `my_dff` 在复位、使能、保持三种情况下都按预期工作。

**操作步骤**：

1. 确保 `my_dff.v`（4.3.4 的示例代码）已写好。
2. 新建测试平台 `my_dff_tb.v`，内容如下（**示例代码**，非仓库原有文件）：

```verilog
`timescale 1ns/1ps
module my_dff_tb;
  reg         i_clk, i_rst_an, i_ena;
  reg  [11:0] i_data;
  wire [11:0] o_data;

  my_dff #(.gp_data_width(12)) dut (              // 按固定端口顺序例化
    .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
    .i_data(i_data), .o_data(o_data)
  );

  initial i_clk = 1'b0;
  always #5 i_clk = ~i_clk;                       // 10ns 周期，上升沿在 5,15,25,…

  initial begin
    $dumpfile("my_dff.vcd"); $dumpvars(0, my_dff_tb);
    i_rst_an = 1'b0; i_ena = 1'b0; i_data = 12'h000;  // t=0：复位有效
    #12 i_rst_an = 1'b1;                           // t=12：释放复位（ena 仍为 0）
    #10 i_data   = 12'hABC;                        // t=22：给值，但 ena=0 不应采样
    #10 i_ena    = 1'b1;                           // t=32：使能拉高
    #10 i_data   = 12'h123;                        // t=42：换值
    #10 i_ena    = 1'b0;                           // t=52：使能拉低
    #10 i_data   = 12'hFFF;                        // t=62：改值，ena=0 不应采样
    #20 $finish;                                   // t=82：结束
  end

  initial $monitor("t=%0t  rst_an=%b ena=%b data=%h -> o_data=%h",
                   $time, i_rst_an, i_ena, i_data, o_data);
endmodule
```

3. 在安装了 Icarus Verilog 的环境里编译并运行（命令本身来自 README 推荐的工具链）：

```bash
iverilog -o my_dff.vvp my_dff.v my_dff_tb.v
vvp my_dff.vvp
```

**需要观察的现象**：盯住 `$monitor` 打印的 `o_data` 列，重点关注三个时刻：

- **复位段（t < 12，且复位有效期间）**：`o_data` 始终为 `000`，即使 `i_clk` 有上升沿——印证"异步复位优先"。
- **t=25 的上升沿**：此时复位已释放，但 `i_ena=0`，所以 `o_data` 仍是 `000`，`12'hABC` 没被采进来——印证"同步使能门控"。
- **t=35 的上升沿**：这是释放复位后第一个 `i_ena=1` 的上升沿，`o_data` 跳到 `abc`；之后 t=45 跳到 `123`；t=55 之后 `i_ena` 拉低，即便 `i_data` 变成 `fff`，`o_data` 也一直停在 `123`——印证"使能低时保持"。

**预期结果**：`o_data` 的关键序列大致为 `000 →（复位段全 0）→ 000（t=25，ena=0）→ abc（t=35）→ 123（t=45 起，一直保持）`。如果你装了 GTKWave，还可以打开 `my_dff.vcd` 看波形，对照 4.1.2 的状态流程图逐拍核对。

> 说明：精确的打印时间戳取决于仿真器调度，可能与你手算的略有出入，但 `o_data` 的取值序列应当符合上述描述。若环境没有 iverilog，本任务也可降级为"源码阅读型"：逐行推导测试平台在每个上升沿会采到什么值，与上面的预期序列比对——此为**待本地验证**。

---

## 6. 本讲小结

- DRL 用 [README 的四条 Coding Style](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/README.md#L47-L53) 统一了全库时序：**异步低有效复位、同步高有效使能、仅用上升沿触发器、补码定点运算**。
- 这四条落到代码上就是一段固定骨架的 `always @(posedge i_clk or negedge i_rst_an)` 三段式块：复位清零优先、使能才采样、否则保持。
- 命名前缀 `gp_/c_/r_/w_` 让你光看名字就知道信号身份；块标签 `p_/g_` 用于命名 `always`/`generate` 块，方便调试定位。
- 要记住一个细微差别：`r_` 表示"寄存器值"（语义），它在本模块赋值时是 `reg`、来自子模块输出时是 `wire`（语言要求）；`reg/wire` 关键字描述"怎么被驱动"，`r_/w_` 前缀描述"有没有记忆"。
- [`dff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) 是全库的接口范本和原子构建块：固定端口顺序 `i_rst_an, i_ena, i_clk, i_data, o_data`，内部 `reg` + `assign` 桥接到 `wire` 输出。
- 给 DRL 写任何新模块，都应当照 `dff` 的骨架来——这是后续 u7-l3 脚手架生成代码的内在逻辑。

---

## 7. 下一步学习建议

- 想搞清"补码定点数位宽怎么自动推导"，进入 **u2-l1（Verilog 定点数与位宽推导）**，那里会用到本讲的 `gp_`/`c_` 前缀和 `$clog2`。
- 想逐行吃透 `dff` 本身并看它怎么被串成移位寄存器，进入 **u2-l2（dff.v 基础寄存器原语）** 和 **u2-l3（shift_register 与 upsample 原语）**。
- 想看本讲的时序约定在完整模块里如何展开，可以提前翻一眼 **u4-l1（filt_cicd 抽取滤波器）** 里积分器/微分器如何复用 `dff` 和 `shift_register`。
