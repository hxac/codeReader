# 两级数据同步器：cdc_data

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚什么是**亚稳态（metastability）**，以及为什么跨时钟域的直接连线不可靠。
- 解释**两级同步器（two-stage synchronizer）**为什么能把亚稳态风险压到几乎为零（用 MTBF 公式说明）。
- 在仓库里认出 `cdc_data.sv` 就是 `delay.sv` 的 `LENGTH=2` 封装，并能读懂 `delay` 里那条寄存器链。
- 用 `_SYNC_ATTR` 命名约定，为工程里**所有**同步器只写**一条** `set_false_path` 约束，并解释为什么排除的是“进入第一级触发器的数据路径”。
- 区分 `cdc_data`（搬多 bit 异步**数据**）和 `cdc_strobe`（搬单拍**事件**）解决的是两类不同问题。

## 2. 前置知识

本讲承接 **u2-l3（delay：静态延迟与同步链）**，请确保你已经掌握：

- `delay.sv` 用一个 `generate` 块按 `(LENGTH, TYPE)` 在编译期选择不同实现；默认 `TYPE="CELLS"` 走的是一串触发器（寄存器链）。
- 当 `LENGTH=2` 时，`delay` 就是一个**两级同步器**——这正是 `cdc_data` 做的事。
- 同步器的“输入路径”必须用 `set_false_path` 排除时序分析，这一点 u1-l4、u2-l3 都提到过，本讲把它讲透。

另外你需要一点点直觉：触发器是一个**对时钟沿敏感**的器件，它只在时钟上升沿“采样”D 端、把它送到 Q 端。如果采样瞬间 D 端正好在翻转，触发器可能“拿不定主意”，这就是亚稳态。本讲会从这里开始。

> 小提示：README 里 `cdc_data.sv` 没有标绿圈/红圈（标准难度），而它封装的 [delay.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) 是 🟢 绿圈基础模块。读懂 `delay`，`cdc_data` 就只是“一句话”。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲怎么看 |
|------|------|-----------|
| [cdc_data.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) | 标准两级数据同步器，本质是 `delay.sv` 的封装 | 看它如何例化 `delay`，以及实例名 `data_SYNC_ATTR` 为何故意带后缀 |
| [delay.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) | 通用静态延迟/同步器 | 只看 `TYPE="CELLS"` 的寄存器链（本讲的“两级”就在这里） |
| [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) | 跨域搬运**单拍脉冲**，用格雷计数器 | 做对照：它处理的是“事件”而非“数据”，用 `_FP_ATTR` 而非 `_SYNC_ATTR` |
| [example_projects/vivado_test_prj_template_v3/src/timing.xdc](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc) | Vivado 时序约束模板 | 看里面真实的 `*_SYNC_ATTR/data_reg[1]*` 约束——本讲实践的直接依据 |

---

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**两级同步器**、**亚稳态**、**`_SYNC_ATTR` 约束约定**。三者环环相扣——亚稳态是“病”，两级同步器是“药”，`_SYNC_ATTR` 是“给药（写约束）的统一方式”。

### 4.1 两级同步器

#### 4.1.1 概念说明

当一个信号由 **A 时钟域**产生，却被 **B 时钟域**的触发器直接采样时，二者没有固定的相位关系。B 时钟的上升沿有可能恰好砸在信号翻转的瞬间，于是 B 域那个第一级触发器会进入**亚稳态**：它的 Q 端既不是干净的 0 也不是干净的 1，而是在两者之间“晃”一段时间，最终随机落定到 0 或 1，且这个落定时间不可预测。

如果这个“晃悠的值”立刻被下游逻辑使用，整条数据通路就会出错。**两级同步器**的思路非常朴素：

> 给亚稳态留出**几乎一整个时钟周期**的时间去自行落定，再用第二级触发器重新“干净地”采一次。

也就是说，把一根异步线先打一拍（第一级，可能亚稳态），再打一拍（第二级，落定后再采），第二级的输出就可以被本域安全使用。`cdc_data` 就是这个“打两拍”。

#### 4.1.2 核心流程

把 `d`（异步输入）搬到 `clk` 域，两拍流程：

```text
异步 d ──▶ [第1级触发器 data[1]] ──▶ [第2级触发器 data[2]] ──▶ q (干净)
              ↑ 亚稳态发生在这里          ↑ 一个周期后再采，已落定
```

- **第 0 拍**：`d` 上出现一个新值。
- **第 1 个 clk 上升沿**：第一级 `data[1]` 采样 `d`——这一级**可能**进入亚稳态。
- 在两个上升沿之间，亚稳态有将近一个时钟周期的时间自行衰减、落定到 0/1。
- **第 2 个 clk 上升沿**：第二级 `data[2]` 采样已经落定的 `data[1]`，得到一个干净值；从 `q` 输出。

代价是**两个时钟周期的延迟**（这是 `cdc_data` 头注释里“2 clock cycles propagation delay”一类约定的来源，也是 `delay` 的 `LENGTH=2` 的直接含义）。收益是亚稳态传到下游的概率从“可能”变成“几乎不可能”（见 4.2 的 MTBF）。

#### 4.1.3 源码精读

`cdc_data` 的全部实现只有一件事：例化一个 `LENGTH=2` 的 `delay`，并把实例名取成 `data_SYNC_ATTR`。

[cdc_data.sv:L36-L57](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L36-L57) —— `cdc_data` 模块体就是把 `delay` 例化成两级同步器：

```systemverilog
module cdc_data(
  input clk,
  input nrst,
  input d,
  output q
);

delay #(
    .LENGTH( 2 ),               // 两级 = 两级同步器
    .WIDTH( 1 ),                // 单 bit；多位用数组例化（见综合实践）
    .TYPE( "CELLS" ),           // 走寄存器链，不推断块 RAM
    .REGISTER_OUTPUTS( "FALSE" )
) data_SYNC_ATTR (              // ← 实例名故意带 _SYNC_ATTR 后缀
    .clk( clk ),
    .nrst( nrst ),
    .ena( 1'b1 ),
    .in( d ),
    .out( q )
);
```

要点：`LENGTH=2` 决定了它就是两级同步器；实例名 `data_SYNC_ATTR` 不是随便取的——这个后缀是 4.3 节“一条约束管所有同步器”的关键。

`LENGTH=2` 走的是 `delay` 里 `TYPE="CELLS"` 的寄存器链分支。[delay.sv:L190-L206](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L190-L206) 是真正的“两级触发器”：

```systemverilog
logic [LENGTH:1][WIDTH-1:0] data = '0;          // LENGTH=2 ⇒ data[2], data[1]
always_ff @(posedge clk) begin
  integer i;
  if( ~nrst ) begin
    data <= '0;
  end else if( ena ) begin
    for(i=LENGTH-1; i>0; i--) begin
      data[i+1][WIDTH-1:0] <= data[i][WIDTH-1:0]; // data[2] <= data[1]
    end
    data[1][WIDTH-1:0] <= in[WIDTH-1:0];          // data[1] <= in （第一级，采异步输入）
  end
end
assign out[WIDTH-1:0] = data[LENGTH][WIDTH-1:0];  // out = data[2] （第二级）
```

把 `LENGTH=2` 代入：

- `data[1] <= in` —— **第一级**，直接采异步 `in`/`d`，是亚稳态发生地。
- `data[2] <= data[1]` —— **第二级**，一个周期后采已落定的 `data[1]`。
- `out = data[2]` —— 干净输出。

综合后工具会把这两个触发器命名为 `data_reg[1]`、`data_reg[2]`（Vivado）或 `data[1]`、`data[2]`（Quartus）。**记住 `data[1]` = 第一级**，4.3 节的约束就要精确瞄准它。

#### 4.1.4 代码实践

**目标**：在仿真里看清“两拍延迟”这件事。

1. 新建一个最小 testbench `cdc_data_tb.sv`，例化 `cdc_data`，`clk` 用 `initial forever #5 clk=~clk;`（100MHz 仿真时钟），`nrst` 拉高。
2. 在 `clk` 的某个上升沿**之后**（比如 `#3`，刻意做出非同步关系）驱动 `d` 发生 `0→1` 跳变。
3. 用 `$monitor` 或波形观察 `d`、`data[1]`（若层次可见）、`q`。
4. **观察现象**：`q` 比 `d` 晚**两个** `clk` 上升沿才翻转。
5. **预期结果**：`d` 跳变沿 → 第 1 个上升沿后 `data[1]` 跟着变 → 第 2 个上升沿后 `q` 才变。

> 说明：本仓库没有提供 `cdc_data_tb.sv`（同步器的行为在仿真里几乎“无聊”，真正的价值在时序/可靠性层面）。因此本实践属于“源码阅读 + 自建最小仿真”型，验证的是**延迟**而非亚稳态——亚稳态是器件物理现象，纯数字 RTL 仿真不会自发产生它。

#### 4.1.5 小练习与答案

**Q1**：把 `cdc_data` 内部 `delay` 的 `LENGTH` 从 2 改成 3，输出延迟变成几拍？这样有意义吗？

> **答**：3 拍。理论上有：在某些高频或低速工艺下，三级同步器能进一步抬高 MTBF（多一级就多一个周期的“落定时间”）。代价是多一拍延迟、多一个触发器。一般两级已足够，只有极端可靠场景才上三级。

**Q2**：为什么 `WIDTH` 设成 1，而不是直接 32？

> **答**：因为**多 bit 数据不能整体同步**——逐 bit 两级同步后，各位之间会丢掉原本的“同时翻转”关系（各位落定时间不同，可能差一拍）。所以多 bit 异步**总线**要用“32 个单 bit 同步器数组”来同步**逐位稳定**的值，而不是一个 32 位宽的 `delay`。这正是头注释里 `[31:0]` 数组例化的原因（见综合实践）。

---

### 4.2 亚稳态

#### 4.2.1 概念说明

亚稳态是触发器的物理属性：当 D 端在建立/保持时间窗内发生变化，内部交叉耦合的反相器对无法立刻决定输出，电压停在 0/1 之间的非法电平，经过一段随机时间后才衰减到合法电平。这段“衰减时间”\(t_r\) 越长，最终传到下游造成错误的概率越低。

这就引出衡量同步器好坏的核心指标：**MTBF（平均无故障时间）**。同步器的目标就是把 MTBF 推到“工程上等于无穷大”（比如 > 1 万年）。

#### 4.2.2 核心流程

经典同步器 MTBF 公式：

\[
\text{MTBF} \;=\; \frac{e^{\,t_r/\tau}}{f_{\text{clk}}\cdot f_{\text{data}}\cdot T_0}
\]

其中：

- \(t_r\)：留给亚稳态衰减的**可用时间**（下一级采样前的余量）。
- \(\tau\)、\(T_0\)：取决于工艺、电压、温度的常数。
- \(f_{\text{clk}}\)、\(f_{\text{data}}\)：采样时钟频率与数据翻转频率。

关键在于 \(t_r\) 在**指数**上。两级同步器之所以有效，正是因为它把可用衰减时间 \(t_r\) 从“一段组合逻辑 + 建立时间”提升到**几乎整整一个时钟周期**。即便 \(t_r\) 只增长几倍，由于它在 \(e^{t_r/\tau}\) 的指数位，MTBF 会**指数级**改善——从“每小时出错一次”变成“上万年才出错一次”。

一拍同步器（直接采完就用）的问题在于：第一级的亚稳态若没在“到下游触发器建立时间之前”落定，错误的半电平就会被下游当成 0 或 1 采样，错误传播。第二级把“落定时间”撑满一个周期，几乎必然落定，所以下游采到的是确定值。

#### 4.2.3 源码精读

本仓库用 `cdc_data` 处理**数据**，用 `cdc_strobe` 处理**事件**。两者的亚稳态应对策略不同，对比能加深理解。

`cdc_data`：对**相对静态的多 bit 数据**，逐位用两级触发器采样（本节已讲）。

`cdc_strobe` 则面对一个更刁钻的问题：要搬的是**单拍脉冲**。脉冲只有一个时钟宽度，直接用两级同步器很可能被“漏采”（脉冲正好落在两次采样之间）。它的做法是把脉冲转换成一个**会翻转的位**再搬。[cdc_strobe.sv:L82-L92](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L82-L92) 用一个 2 位格雷计数器实现：

```systemverilog
// 2 bit gray counter, it must NEVER be reset
logic [1:0] gc_FP_ATTR = '0;
always @(posedge clk1 or posedge arst) begin
  ...
    if( strb1_ed ) begin
      gc_FP_ATTR[1:0] <= {gc_FP_ATTR[0],~gc_FP_ATTR[1]}; // incrementing counter
    end
  ...
end
```

每来一个脉冲，`gc_FP_ATTR` 按格雷码走一格（格雷码每次只变 1 bit，所以单 bit 同步器足够）；clk2 域把计数器值搬过去，比较前后两次值是否变化来还原脉冲（[cdc_strobe.sv:L96-L108](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L96-L108)）。这就把“搬脉冲”转化为“搬状态翻转”——后者对亚稳态鲁棒。注意它的实例/对象名带的是 `_FP_ATTR` 后缀（false path attribute），与 `cdc_data` 的 `_SYNC_ATTR` 区分。

一句话总结对比：

| 模块 | 搬运对象 | 机制 | 命名后缀 |
|------|---------|------|---------|
| `cdc_data` | 多 bit 异步**数据**（相对静态） | 两级触发器 | `_SYNC_ATTR` |
| `cdc_strobe` | 单拍**脉冲/事件** | 2 位格雷计数器 + 跨域比较 | `_FP_ATTR` |

`cdc_strobe` 的细节是下一讲 u3-l2 的主题，这里只需记住：**`cdc_data` 解决的是数据同步问题，别拿它去同步脉冲。**

#### 4.2.4 代码实践

**目标**：用数学体会“多一级”对 MTBF 的杠杆。

1. 设某工艺 \(\tau=50\text{ps}\)、\(T_0=0.1\text{s}\)、\(f_{\text{clk}}=f_{\text{data}}=10^8\text{Hz}\)（约 100MHz）。
2. 一拍方案：假设可用 \(t_r\approx 1\text{ns}\)；两拍方案：\(t_r\approx 10\text{ns}\)（约一个周期）。
3. 代入 MTBF 公式手算两个值。
4. **预期结果**：两拍方案的 MTBF 会比一拍方案高出**好几个数量级**。
5. **待本地验证**：具体数字取决于你代入的工艺常数，但“指数级改善”这个结论一定成立。

#### 4.2.5 小练习与答案

**Q1**：亚稳态最终会落定到 0 还是 1？落定值和原来的输入有关吗？

> **答**：落定值是**随机**的，与输入原本想表达的逻辑值**无关**。同步器并不“修正”这个值，它只是给足时间让电平稳定到合法 0/1，并保证**下游**采到的是一个确定（哪怕随机）的电平。对于单 bit 控制信号，随机一拍通常无害；对于多 bit 数据，正因为各 bit 随机落定、还可能错位，才不能直接整体同步总线。

**Q2**：提高时钟频率会让同步器 MTBF 变好还是变差？为什么？

> **答**：变**差**。频率提高 ⇒ 周期变短 ⇒ 可用衰减时间 \(t_r\) 变小，而 \(t_r\) 在指数上，MTBF 急剧下降。这正是高速设计里更依赖可靠同步器（甚至三级）的原因。

---

### 4.3 `_SYNC_ATTR` 约束约定

#### 4.3.1 概念说明

写完同步器，时序工具却会报错：它看到有一条路径“终点是 `data[1]` 的 D 端”，而这条路径的源头是另一个时钟域，没有公共时钟基准，于是无法满足建立/保持时间，给出一条时序违例。

但这是**预期之内**的：这条路径本就是异步的，我们**故意**不要求它满足同步时序。我们需要明确告诉工具：“这条路径不要分析，CDC 我已经用两级同步器处理好了。”方式就是 `set_false_path`。

问题在于：一个工程里可能有几十个同步器实例，难道要逐个写约束？仓库的约定是——**给每个同步器实例名加 `_SYNC_ATTR` 后缀**，于是**一条**带通配符的约束就能匹配全部。这就是 `cdc_data` 里实例名取 `data_SYNC_ATTR` 的根本原因。

#### 4.3.2 核心流程

“命名即契约”的工作流：

```text
写 RTL：  delay/cdc_data 实例名统一带 _SYNC_ATTR 后缀
   │
   ▼
写约束：  一条 set_false_path -to <匹配 *_SYNC_ATTR/第一级寄存器*>
   │
   ▼
综合：    工具把第一级寄存器命名为 data_reg[1]/data[1]
   │
   ▼
匹配：    通配符命中所有同步器的第一级 → 全部豁免
```

两个最容易搞错的点（务必记住）：

1. **排除的是“进入第一级触发器”的路径**，不是第一级到第二级之间的路径。因为亚稳态发生在第一级 `data[1]`，而被分析的正是“异步源 → `data[1]` D 端”这条路径。第一级到第二级（`data[1]`→`data[2]`）**在同一时钟域内**，是正常路径，**必须**满足时序——正是它要按时钟节拍可靠地把落定值传下去。
2. 用 `-to`（指向第一级寄存器），而不是 `-from`。对比 `cdc_strobe` 用的是 `-from ... _FP_ATTR`（[cdc_strobe.sv:L26-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L26-L33)），方向和后缀都不同，二者不可混用。

#### 4.3.3 源码精读

`cdc_data` 的头注释直接给出了两个工具的约束模板。[cdc_data.sv:L6-L21](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L6-L21)：

```systemverilog
// INFO --------------------------------------------------------------------------------
// Standard two-stage synchronizer
// CDC stands for "clock data crossing"
//
// In fact, this madule is just a wrapper for dalay.sv
//
// Don`t forget to write false_path constraints for all your synchronizers
//   The best way to do it - is to mark all synchonizer delay.sv instances
//   with "_SYNC_ATTR" suffix. After that, just one constraint is required:
//
// For Quartus:
// set_false_path -to [get_registers {*delay:*_SYNC_ATTR*|data[1]*}]
//
// For Vivado:
// set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]
```

逐字解读这两条：

- **Quartus**：`get_registers {*delay:*_SYNC_ATTR*|data[1]*}`。Quartus 里寄存器全名形如 `delay:data_SYNC_ATTR|data[1]`（模块名:实例名|信号名）。通配模式要求“模块是 `delay`、实例名含 `_SYNC_ATTR`、寄存器是 `data[1]`”三者同时满足，正好选中每个同步器的第一级。
- **Vivado**：`get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}`。`-hier` 递归全层次，`*` 匹配任意层次前缀，定位到名为 `data_SYNC_ATTR` 的实例下的 `data_reg[1]` 单元。

而工程模板里有一条**真实可用**的约束。[timing.xdc:L33-L39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L33-L39)：

```tcl
# all delay.sv instances with "_SYNC_ATTR" suffix name will be considered not
#   a delay, but as a synchronizers
#   see https://www.xilinx.com/support/answers/62136.html for syntax explanation
set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]
set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR[*]/data_reg[1]*}]
```

两条分别覆盖两种例化形态：

- 第 1 条匹配**标量例化**（实例名就是 `xxx_SYNC_ATTR`）。
- 第 2 条匹配**数组例化**（如综合实践里的 `cdc_data CD[31:0]`，内层单元名会带数组下标，形如 `data_SYNC_ATTR[3]/data_reg[1]`）。

`delay.sv` 头注释也专门提醒了这一点。[delay.sv:L16-L19](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L16-L19)：

```systemverilog
// CAUTION: delay module is widely used for synchronizing signals across clock
//   domains. When synchronizing, please exclude input data paths from timing
//   analysis manually by writing appropriate set_false_path SDC constraint
```

注意 `delay` 自己既可做“普通延迟”，也可做“同步器”——工具无法区分。**正因为有了 `_SYNC_ATTR` 后缀这条约定，你才能只豁免用作同步器的那些 `delay`，而不误伤用作普通延迟的 `delay`。** 这就是命名约定的价值。

#### 4.3.4 代码实践

**目标**：动手写出本讲的 false_path 约束，并解释“为什么排除数据路径”。

1. 阅读上面的 `cdc_data.sv` INFO 和 `timing.xdc` 两段源码。
2. 在一份 Vivado `.xdc` 里写下：
   ```tcl
   set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]
   set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR[*]/data_reg[1]*}]
   ```
   在一份 Quartus `.sdc` 里写下：
   ```tcl
   set_false_path -to [get_registers {*delay:*_SYNC_ATTR*|data[1]*}]
   ```
3. **观察现象**（待本地验证，需有对应工程综合）：综合/实现后，原本针对同步器第一级的时序违例（若 `d` 来自另一时钟域）会从报告中消失，但路径仍在网表中——只是被排除分析。
4. **解释为什么排除**：`d` 与 `clk` 无固定相位关系，建立/保持时间不可能被静态时序分析满足；这条路径的“可靠性”不是靠时序收敛保证的，而是靠**两级同步器降低 MTBF**保证的。继续让工具分析它只会产生永远修不好的违例噪声，因此必须显式排除。排除是“诚实地承认它是异步路径”，而不是“掩盖问题”。

#### 4.3.5 小练习与答案

**Q1**：如果把约束误写成 `-to ... data_reg[2]*`（指向第二级），会怎样？

> **答**：会豁免**错误**的路径。被排除的应是“异步源 → 第一级”那条；指向第二级等于放过了真正违例的异步路径，反而把正常的“第一级→第二级”（同域、本应收敛）给排除了。结果：违例依旧（甚至被掩盖），且正常的同域路径不再被检查。**必须瞄准 `data[1]`/`data_reg[1]`。**

**Q2**：工程里另有一个 `delay` 用作纯组合/普通延迟（非同步器），它的实例名没带 `_SYNC_ATTR`。它会受这条约束影响吗？

> **答**：不会。通配模式要求名字里出现 `_SYNC_ATTR`，普通延迟实例名不匹配，因此不会被豁免，仍按正常时序分析。这正是命名约定的妙处——**精确区分“同步器”与“普通延迟”两种用途。**

---

## 5. 综合实践

把三个最小模块串起来，完成规格里要求的核心任务：**用 `cdc_data` 数组同步一个 32 位异步输入，并写出对应的 false_path 约束。**

`cdc_data.sv` 头注释里就给了现成的数组例化模板。[cdc_data.sv:L24-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L24-L33)：

```systemverilog
cdc_data CD [31:0] (
  .clk( {32{clk}} ),
  .nrst( {32{1'b1}} ),
  .d( ext_data[31:0] ),
  .q( synchronized_data[31:0] )
);
```

**操作步骤**：

1. **理解这个数组例化**：`cdc_data CD [31:0]` 一次性生成 32 个单 bit 同步器；`{32{clk}}` 把同一个 `clk` 复制成 32 份接到每个实例的 `clk` 端口；`ext_data[31:0]` 是 32 位异步输入，逐位送进各实例的 `d`；`synchronized_data[31:0]` 是同步到 `clk` 域后的逐位输出。
2. **建工程**：在一个已有 `clk` 的顶层（可借用 `example_projects/vivado_test_prj_template_v3` 的 `main.sv` 骨架），把 `ext_data` 声明为异步输入端口，例化上面的 `cdc_data` 数组。
3. **写约束**（Vivado）：把 `timing.xdc` 里的两条 `_SYNC_ATTR` 约束原样抄进你的 `.xdc`。若用 Quartus，则写 `set_false_path -to [get_registers {*delay:*_SYNC_ATTR*|data[1]*}]`。
4. **综合**（待本地验证，需要本地 IDE）：打开时序报告。

**需要观察与解释**：

- 即便 `ext_data` 相对 `clk` 完全异步，时序报告里**不应**出现以 `*_SYNC_ATTR[*]/data_reg[1]` 为终点的违例——它们被豁免了。
- 解释**为什么这条数据路径要被排除**：因为它是异步的，STA 无法、也不应要求它满足建立/保持；可靠性由两级同步器承担，`_SYNC_ATTR` 约定让工具与设计者在“这是同步器第一级”上达成一致。
- **重要提醒**：本实践**隐含假设 `ext_data` 是“逐位稳定”的值**（如配置寄存器、慢变状态）。如果它是会“多位同时翻转”的值（如自由计数器），逐位同步后各位可能错拍，得到的将是错误值——那种情况需要更复杂的手法（格雷码、握手、异步 FIFO），不能用 `cdc_data` 直接同步。这正是 4.1.5 里强调的边界。

## 6. 本讲小结

- **亚稳态**是触发器在建立/保持窗内遇到翻转时的物理现象，输出会停留非法电平一段时间才随机落定。
- **两级同步器**（`delay` 的 `LENGTH=2`）靠把亚稳态衰减时间撑到近一个周期，指数级抬高 MTBF；`cdc_data` 就是它的封装。
- 在 `delay` 的寄存器链里，`data[1]` 是采异步输入的**第一级**（亚稳态发生地），`data[2]`/`out` 是**第二级**（干净输出）。
- **`_SYNC_ATTR` 命名约定**让工程里所有同步器可被**一条** `set_false_path` 统一豁免，并精确区分“同步器”与“普通延迟”两种 `delay` 用途。
- 约束方向是 **`-to` 第一级**（`data[1]`/`data_reg[1]`），不是 `-from`、不是第二级；这和 `cdc_strobe` 的 `-from ... _FP_ATTR` 形成对照。
- `cdc_data` 同步的是**逐位稳定的多 bit 数据**；多位同时翻转的总线、以及单拍脉冲，都不能用它（脉冲要用 `cdc_strobe`，见下一讲）。

## 7. 下一步学习建议

- **紧接的 u3-l2（cdc_strobe）**：本讲多次对比的脉冲同步器，它会讲清 2 位格雷计数器跨域搬运事件、速率限制（最高“隔一拍一次”）、`_FP_ATTR` 约束，以及为什么脉冲不能直接套用两级同步器。
- **u7-l2（时序约束与收敛）**：本讲只聚焦同步器这一条 `false_path`；`create_clock`、Fmax 提取脚本、以及更系统的 SDC/XDC 写法在那里展开。
- **阅读建议**：对照读 [delay.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv) 的 `CELLS` 分支与 [cdc_data.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) 的封装，体会“一个通用模块 + 一个命名约定 = 一类可靠 CDC 方案”的设计思想；这会帮助你理解本仓库为何能仅用少量原语覆盖大量场景。
