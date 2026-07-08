# fxp2float_pipe 与 float2fxp_pipe：流水线浮点转换

## 1. 本讲目标

本讲是「浮点互转」主题的第二篇，承接 [u3-l4](u3-l4-float-convert.md) 的单周期 `fxp2float` / `float2fxp` 与 [u3-l1](u3-l1-mul-pipe.md) 的流水线范式。学完后你应当：

- 理解为什么单周期浮点转换「关键路径过长、不推荐综合」，从而必须改流水线。
- 看懂 `fxp2float_pipe` 如何把「从高到低扫描找前导 1」改写成「逐级左移规格化 + 指数递减」的 `WII+WIF+2` 级流水线。
- 看懂 `float2fxp_pipe` 如何把「逐位安放尾数」改写成「尾数顶端对齐 + 逐级右移 + 指数递减到 0」的 `WOI+WOF+4` 级流水线。
- 理解 `generate` 根据输出位宽是否容纳得下 24 位尾数而分两条路径的原因。
- 能用单周期版作黄金参考，按正确延迟对齐逐拍自校验，证明流水线版与单周期版功能一致。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义，这里只做一句话回顾）：

- **定点格式与参数命名**（[u1-l2](u1-l2-fixedpoint-format.md)）：定点值 \(= \text{补码整数} / 2^{W_F}\)；`WII/WIF` 是输入整数/小数位宽，`WOI/WOF` 是输出整数/小数位宽。
- **流水线统一范式**（[u3-l1](u3-l1-mul-pipe.md)）：`_pipe` 模块比单周期版多 `rstn/clk`；输出改 `reg` 并用 `initial` 初始化；用 `always @(posedge clk or negedge rstn)` 异步复位、非阻塞赋值 `<=`；用「标量循环变量 → 级间寄存器数组」把循环展开成无气泡流水线。
- **IEEE754 单周期互转**（[u3-l4](u3-l4-float-convert.md)）：单精度浮点 \(= \{\text{sign},\ \text{exp}[7:0],\ \text{tail}[22:0]\}\)，尾数前有隐含的 1；`fxp2float` 扫描前导 1 定指数，`float2fxp` 逐位把尾数安放到定点码。

还有一个贯穿本讲的核心数学工具——**移位保持数值不变的恒等式**：

- 左移 1 位 = 乘 2，右移 1 位 = 除 2。
- 若同时把「尾数左移 1 位」和「指数减 1」，则 \( \text{mantissa}\times 2^{\text{exp}} \) 的数值不变；反之「尾数右移 1 位 + 指数加 1」也不变。

两个流水线模块的全部巧妙之处，都建立在「让尾数移到正确位置、同时用指数反向计数来补偿、从而保持数值不变」这一恒等式之上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在的单一文件。本讲关注其中四个：`fxp2float`、`fxp2float_pipe`、`float2fxp`、`float2fxp_pipe`。 |
| [SIM/tb_convert_fxp_float.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) | 同时例化四个转换模块的 testbench，目前只做 `$display` 打印，是本讲代码实践的改造基础。 |
| [SIM/tb_convert_fxp_float_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float_run_iverilog.bat) | 一键编译运行脚本：`iverilog -g2001 -o sim.out tb_convert_fxp_float.v ../RTL/fixedpoint.v` 再 `vvp -n sim.out`。 |

源码在文件中的位置速查：

| 模块 | 行号 |
|------|------|
| `fxp2float`（单周期，黄金参考） | 874–923 |
| `fxp2float_pipe`（流水线） | 939–1022 |
| `float2fxp`（单周期，黄金参考） | 1039–1097 |
| `float2fxp_pipe`（流水线） | 1113–1251 |

## 4. 核心概念与源码讲解

### 4.1 fxp2float_pipe：把「扫描前导 1」改写成「逐级规格化流水线」

#### 4.1.1 概念说明

单周期 `fxp2float` 的做法是：取绝对值 `inu`，用一个 `for` 循环**从最高位往低位扫描**，找到第一个为 1 的位，它的位置 `jj` 直接决定阶码 \(\text{expz}=jj+127-\text{WIF}\)，其后的 23 位成为尾数（见 [RTL/fixedpoint.v:902-911](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L902-L911)）。

这条「扫描 + 收集」是一个长组合链：最坏情况下要扫过全部 `WII+WIF` 位才能定位前导 1，关键路径很长，时序难收敛。文件头注释也明确标注 `not recommended due to the long critical path`（[RTL/fixedpoint.v:871](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L871)）。

`fxp2float_pipe` 换了一个等价但可流水化的思路——**规格化（normalization）**：

> 不去「找」前导 1 在哪，而是假设它就在最高位，给出一个「最大阶码」初值；然后每拍检查最高位是不是 1，不是就把整个数左移 1 位、阶码减 1，直到最高位变成 1。

为什么这等价？因为「左移 1 位（乘 2）+ 阶码减 1」保持浮点数值不变。当最高位最终被「顶」到 1 时，阶码也刚好递减到了正确值。这正好把「扫描 `WII+WIF` 位」展开成了「最多 `WII+WIF` 拍逐级左移」，每一拍都是一小段组合逻辑，自然适合做流水线。

#### 4.1.2 核心流程

设输入定点码 `in` 的绝对值幅值为 `inu`（`WII+WIF` 位），要转成 IEEE754 单精度。

**数值恒等式（不变量）**：在每一级寄存器中，都满足

\[
\text{value} \;=\; \text{inu} \times 2^{\,\text{exp}-127-(\text{WII}+\text{WIF}-1)}
\]

- **第 0 级（索引 `WII+WIF`）**：捕获 `inu=|in|`，置 `exp = WII+127-1`（假设前导 1 在最高位 `WII+WIF-1` 时的阶码）。代入不变量得 \(\text{value}=|in|\times 2^{-\text{WIF}}\)，正是定点幅值，不变量成立。
- **第 1～`WII+WIF` 级（索引 `WII+WIF-1` … 0）**：每级判断上一级 `inu` 的最高位 `inu[WII+WIF-1]`：
  - 若为 1：已规格化，`inu`、`exp` 原样下传。
  - 若为 0：`inu <<= 1`，`exp -= 1`（且 `exp` 已为 0 时不再减，表示下溢）。由恒等式，数值不变。
- **末级**：此时 `inu[0]` 最高位必为 1（幅值非 0 时），即 `1.xxx` 形式；取其高 24 位作尾数（最高位即隐含的 1，不进尾数），`exp[0]` 即最终阶码；处理上溢饱和与下溢置零。

伪代码：

```
sign = in 的符号位
inu  = |in|
exp  = WII + 127 - 1                 // 前导1假设在最高位时的阶码
repeat (WII+WIF) 次:                 // 每次迭代 = 1 级流水线
    if inu 的最高位 == 1:
        inu, exp 保持不变             // 已规格化
    else:
        inu = inu << 1                // 左移
        exp = (exp!=0) ? exp-1 : 0    // 阶码补偿，保数值不变；到0停（下溢）
// 末级：打包 {sign, exp, tail=inu的高24位去掉隐含1}
```

总级数 = 1（首级捕获）+ `WII+WIF`（逐级规格化）+ 1（末级打包）= `WII+WIF+2`。

#### 4.1.3 源码精读

**端口与级间寄存器数组**（[RTL/fixedpoint.v:939-960](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L939-L960)）：

```verilog
module fxp2float_pipe #(
    parameter WII = 8, parameter WIF = 8
)(
    input  wire               rstn,
    input  wire               clk,
    input  wire [WII+WIF-1:0] in,
    output wire        [31:0] out        // 注意 out 是 wire
);
reg              sign [WII+WIF :0];      // 级间寄存器数组，下标=级号
reg         [9:0] exp  [WII+WIF :0];
reg [WII+WIF-1:0] inu  [WII+WIF :0];
...
assign out = {signo, expo, valo[22:0]};  // 末级寄存器组合输出
```

`sign/exp/inu` 三个数组就是 u3-l1 强调的「标量 → 级间寄存器数组」，下标即流水线级号，数据从高索引向低索引（`ii+1 → ii`）逐级前移。`out` 是 `wire`，由末级寄存器 `signo/expo/valo` 拼接驱动。

**首级捕获 + 逐级规格化主循环**（[RTL/fixedpoint.v:970-994](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L970-L994)）：

```verilog
sign[WII+WIF] <= in[WII+WIF-1];                       // 首级：符号
exp [WII+WIF] <= WII+127-1;                           // 首级：最大阶码初值
inu[WII+WIF]  <= in[WII+WIF-1] ? (~in)+ONEI : in;     // 首级：取绝对值
for(ii=WII+WIF-1; ii>=0; ii=ii-1) begin
    sign[ii] <= sign[ii+1];
    if(inu[ii+1][WII+WIF-1]) begin                    // 最高位已是1：已规格化
        exp[ii] <= exp[ii+1];
        inu[ii] <= inu[ii+1];
    end else begin                                    // 最高位是0：左移+阶码减1
        if(exp[ii+1]!=0) exp[ii] <= exp[ii+1] - 10'd1;
        else            exp[ii] <= exp[ii+1];         // exp已到0：停（下溢冲零）
        inu[ii] <= (inu[ii+1] << 1);
    end
end
```

这就是 4.1.2 伪代码的直译。注意 `exp` 用 10 位有符号数（`reg [9:0]`），足够覆盖减法过程中的中间值。

**尾数收集的 generate 分支**（[RTL/fixedpoint.v:996-1003](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L996-L1003)）：

```verilog
generate if(23>WII+WIF-1) begin                       // 输入不足24位
    always @ (*) begin
        vall = 0;
        vall[23:23-(WII+WIF-1)] = inu[0];             // 顶端对齐，低位补0
    end
end else begin                                        // 输入≥24位
    always @ (*) vall = inu[0][WII+WIF-1:WII+WIF-1-23]; // 截取高24位
end endgenerate
```

`vall` 是 24 位规格化尾数，`vall[23]` 是隐含的前导 1。当输入位宽 `WII+WIF < 24` 时低位补零（精度受限），否则截取高 24 位。这与单周期版收集 23 位尾数的语义一致。

**末级打包与饱和**（[RTL/fixedpoint.v:1005-1020](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1005-L1020)）：

```verilog
signo <= sign[0];
if(exp[0]>=10'd255) begin                             // 阶码上溢（仅 WII 极大时可达）
    expo <= 8'd255; valo <= 24'hFFFFFF;
end else if(exp[0]==10'd0 || ~vall[23]) begin         // 下溢或幅值为0：冲零
    expo <= 8'd0; valo <= 0;
end else begin                                        // 正常
    expo <= exp[0][7:0]; valo <= vall;
end
```

`~vall[23]` 表示「没有规格化成功」（幅值为 0 时 `inu` 全 0，左移再多也顶不出 1），此时冲零，与单周期版 `(inu==0)?0:...` 的语义对应。阶码上溢分支（`exp>=255`）只有在 `WII` 极大（\(\geq 129\)）时才可能触达，对常规配置是冗余的安全护栏。

#### 4.1.4 代码实践

**目标**：用一个具体数值，亲手验证 `fxp2float_pipe` 的「逐级左移规格化」与单周期版结果一致。

**操作步骤**（源码阅读 + 仿真观察，待本地验证）：

1. 在 [SIM/tb_convert_fxp_float.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) 现有配置 `WII=16, WIF=16` 下，手算输入 `fxp1 = 32'h00010000`（即定点值 \(1.0\)）的期望浮点：幅值 `inu=0x00010000`，前导 1 在第 16 位，阶码 \(=16+127-16=127\)，尾数全 0 → IEEE754 = `0x3F800000`。
2. 把这条向量加入 `initial` 激励块（仿照 [SIM/tb_convert_fxp_float.v:102](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L102) 的写法）。
3. 运行 `SIM/tb_convert_fxp_float_run_iverilog.bat`。

**需要观察的现象**：日志里 `float2`（单周期）与 `float3`（流水线）两列。

**预期结果**：`float2` 在该输入出现的当拍就给出 `0x3F800000`；`float3` 由于 `WII+WIF+2=34` 拍延迟，在 34 拍后才给出相同的 `0x3F800000`。手算规格化轨迹：`exp` 从 `16+127-1=142` 起，`inu` 需左移 \(31-16=15\) 次把第 16 位顶到最高位，`exp` 同步减 15 到 127，与阶码 127 吻合。

#### 4.1.5 小练习与答案

**练习 1**：若输入 `inu` 的前导 1 本来就在最高位（第 `WII+WIF-1` 位），流水线还会左移吗？`exp` 还会递减吗？

**答案**：不会左移、不会递减。首级 `exp=WII+127-1` 恰是前导 1 在最高位时的正确阶码，之后每一级都命中 `if(inu[ii+1][WII+WIF-1])` 分支，原样下传，`exp[0]=WII+127-1`。

**练习 2**：为什么级间寄存器 `exp` 要声明成 10 位 `reg [9:0]` 而不是 8 位？

**答案**：首级 `exp=WII+127-1` 可能超过 8 位上限 255（当 `WII` 较大时），且递减过程需要一个能容纳该初值的有符号/宽位中间表示；末级再判 `exp>=255` 并截到 8 位 `expo`。8 位不足以安全表示这个中间范围。

**练习 3**：输入为 0 时，`fxp2float_pipe` 的输出是什么？走的是哪条分支？

**答案**：输出 `{sign, 8'd0, 23'd0}`（±0）。`inu` 全 0，左移再多最高位也顶不出 1，末级 `~vall[23]` 为真，命中 `expo<=0; valo<=0` 分支冲零。

---

### 4.2 float2fxp_pipe：把「逐位安放」改写成「尾数右移 + 指数递减流水线」

#### 4.2.1 概念说明

单周期 `float2fxp` 的做法是：补回隐含 1 得到 24 位尾数 `val`，用一个 `for(ii=23; ii>=0; ...)` 循环，对每一位 `val[ii]`，按起始位置 `expi = exp-127+WOF` 把它「安放」到定点码 `out[expi]` 上，`expi` 每步减 1（见 [RTL/fixedpoint.v:1060-1082](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1060-L1082)）。这又是一条 24 级的长组合链，注释同样标 `not recommended due to the long critical path`（[RTL/fixedpoint.v:1036](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1036)）。

`float2fxp_pipe` 的等价改写思路是**对偶的移位**：

> 不去「逐位安放」，而是先把整个 24 位尾数一次性放到定点码的**最高位**（顶端对齐），算出一个「需要右移多少位才能落到正确位置」的计数 `expinit`（它 ≤ 0）；然后每拍右移 1 位、计数加 1，直到计数归零，尾数就刚好落在正确的定点位置上。

为什么这等价？因为「右移 1 位（除 2）+ 指数加 1」保持数值不变（与 4.1 的恒等式对偶）。当计数从 `expinit` 递增到 0 时，恰好右移了 \(|\text{expinit}|\) 位，尾数的最高位（隐含 1）正好落到它该在的定点比特上。

#### 4.2.2 核心流程

设输入浮点 `{sign, exp, val}`（`val` 为补回隐含 1 后的 24 位尾数），目标定点码（`WOI,WOF`）。

**数值恒等式（不变量）**：在每一级寄存器中，

\[
\text{定点码} \;=\; \text{outs} \times 2^{\,\text{exps}}
\]

- **第 1 级（组合→寄存器 `outinit/expinit`）**：把 `val` 顶端对齐写进 `outinit`（`val[23]` 落在 `outinit[WOI+WOF-1]`），此时 `outinit` 表示的码值 \(= \text{val}\times 2^{\text{WOI}-1-23}\times 2^{?}\)；并算出
  \[
  \text{expinit} \;=\; \text{exp} - (\text{WOI}-1) - 127 \;=\; \text{exp} - \text{WOI} - 126
  \]
  它正是「把顶端对齐的尾数搬到正确位置所需的右移位数取负」。可验证：最终码值应为 \(\text{val}\times 2^{\text{exp}-127-23+\text{WOF}}\)，而 \(\text{outinit}\times 2^{\text{expinit}} = \text{val}\times 2^{\text{WOI}+\text{WOF}-24}\times 2^{\text{exp}-\text{WOI}-126} = \text{val}\times 2^{\text{exp}-127-23+\text{WOF}}\)，不变量成立。
- **第 2～`WOI+WOF+1` 级（数组 `outs/exps`，索引 `WOI+WOF` … 0）**：每级判断 `exps`：
  - 若 `exps != 0`：`outs >>= 1`（连同移出的最低位存入 `rounds` 作舍入保护位），`exps += 1`。
  - 若 `exps == 0`：尾数已就位，原样下传（`rounds` 也保持）。
- **倒数第 2 级**：依 `rounds` 做 ROUND 四舍五入；按 `sign` 取补码恢复符号。
- **倒数第 1 级**：依符号位做上溢/下溢饱和钳位，输出 `out/overflow`。

伪代码：

```
{sign, exp, val[22:0]} = in ;  val[23] = |exp          // 补隐含1（exp=0时为0）
outinit = val 顶端对齐到 out[WOI+WOF-1]                // 一次性放好
expinit = exp - WOI - 126                               // 需右移 |expinit| 位（≤0）
repeat (WOI+WOF) 次:                                    // 每次迭代 = 1 级
    if exps != 0:
        {outs, rounds} = {1'b0, outs}                   // 右移1位，低位落入rounds
        exps = exps + 1
    else:
        outs, rounds 保持                               // 已就位
// 倒数第2级：ROUND 四舍五入 + 取补码恢复符号
// 倒数第1级：上溢/下溢饱和
```

总级数 = 1（首级 setup）+ (`WOI+WOF`+1)（数组级间）+ 1（舍入/符号）+ 1（饱和输出）= `WOI+WOF+4`。

#### 4.2.3 源码精读

**输入组合逻辑**（[RTL/fixedpoint.v:1129-1135](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1129-L1135)）：

```verilog
assign {sign,exp,val[22:0]} = in;
assign val[23] = |exp;        // 隐含的前导1：exp!=0时为1，exp=0（零/非规格化）时为0
```

注意此处与单周期版 `val[23]=1'b1`（[RTL/fixedpoint.v:1064](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1064)）略有差异：流水线版用 `|exp`，把 `exp=0` 的非规格化/零输入直接当成幅值 0 处理，对常规输入（`exp≥1`）二者完全一致。

**第 1 级：尾数顶端对齐 + generate 分支**（[RTL/fixedpoint.v:1142-1161](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1142-L1161)）：

```verilog
generate if(WOI+WOF-1>=23) begin                        // 输出≥24位：全尾数装得下
    always @ (posedge clk or negedge rstn)
        ... begin
            outinit <= 0;
            outinit[WOI+WOF-1:WOI+WOF-1-23] <= val;     // 24位val顶端对齐
            roundinit <= 1'b0;                          // 无截断，无需舍入
        end
end else begin                                          // 输出<24位：尾数被截断
    always @ (posedge clk or negedge rstn)
        ... begin
            outinit <= val[23:23-(WOI+WOF-1)];          // 只取高 WOI+WOF 位
            roundinit <= ( ROUND && val[23-(WOI+WOF-1)-1] ); // 截掉的最高位作舍入保护
        end
end endgenerate
```

这条 `generate` 是本讲的一个学习重点：**分支条件 `WOI+WOF-1>=23`（即 `WOI+WOF>=24`）判断「定点输出能否装下完整 24 位尾数」**。装得下时直接顶端对齐、无需舍入；装不下时只取高位、并把第一个被截掉的位存进 `roundinit` 作为四舍五入的保护位。

**第 1 级：指数计数 `expinit`**（[RTL/fixedpoint.v:1163-1173](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1163-L1173)）：

```verilog
signinit <= sign;
if( exp==8'd255 || {24'd0,exp}>WOI+126 )               // Inf/NaN 或 必然溢出
    expinit <= 0;                                       // 不再右移，让末级饱和
else
    expinit <= {24'd0,exp} - (WOI-1) - 127;             // = exp - WOI - 126
```

`expinit` 即 4.2.2 中的右移计数取负。对 `exp==255`（Inf/NaN）或 `exp>WOI+126`（幅值必然超出定点范围）的溢出情形，强行置 `expinit=0`（不右移），让尾数留在顶端，从而在末级触发饱和。

**移位主循环（级间寄存器数组）**（[RTL/fixedpoint.v:1176-1206](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1176-L1206)）：

```verilog
reg [WOI+WOF-1:0] outs [WOI+WOF :0];
...
for(ii=0; ii<WOI+WOF; ii=ii+1) begin
    signs[ii] <= signs[ii+1];
    if(exps[ii+1]!=0) begin                            // 还需右移
        {outs[ii], rounds[ii]} <= {1'b0, outs[ii+1]};  // 右移1位，LSB落入rounds
        exps[ii] <= exps[ii+1] + 1;                    // 计数向0靠拢
    end else begin                                     // 已就位
        {outs[ii], rounds[ii]} <= {outs[ii+1], rounds[ii+1]};
        exps[ii] <= exps[ii+1];
    end
end
signs[WOI+WOF] <= signinit;  rounds[WOI+WOF] <= roundinit;
exps[WOI+WOF]  <= expinit;   outs[WOI+WOF]   <= outinit;
```

`{1'b0, outs[ii+1]}` 赋给 `{outs[ii], rounds[ii]}`，效果是 `outs[ii] = outs[ii+1]>>1`（逻辑右移）、`rounds[ii]=outs[ii+1][0]`（移出的最低位）。`rounds` 始终跟踪「刚刚落到 LSB 之下那一位」，正是四舍五入的保护位。

**倒数第 2 级：舍入 + 符号恢复**（[RTL/fixedpoint.v:1208-1226](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1208-L1226)）：

```verilog
outt = outs[0];
if(ROUND & rounds[0] & ~(&outt))                       // 保护位=1且不回绕：进位
    outt = outt + 1;
if(signs[0]) begin
    signl <= (outt!=0);
    outt  = (~outt) + ONEO;                            // 负数取补码
end else
    signl <= 1'b0;
outl <= outt;
```

`~(&outt)` 防止 `0x7F…F` 进位回绕成 `0x80…0`（会翻符号）。这与单周期版 `if(round) out=out+1; ... if(sign) out=(~out)+ONEO;`（[RTL/fixedpoint.v:1081](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1081) 与 [RTL/fixedpoint.v:1092-1093](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1092-L1093)）语义一致。

**倒数第 1 级：上溢/下溢饱和**（[RTL/fixedpoint.v:1228-1249](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1228-L1249)）：

```verilog
out <= outl;  overflow <= 1'b0;
if(signl) begin                                        // 负数：MSB应为1
    if(~outl[WOI+WOF-1]) begin                         // 幅值过大撑不到负最小→溢出
        out[WOI+WOF-1] <= 1'b1;  out[WOI+WOF-2:0] <= 0;  overflow <= 1'b1;  // 钳到负最小
    end
end else begin                                         // 正数：MSB应为0
    if(outl[WOI+WOF-1]) begin                          // 幅值过大→溢出
        out[WOI+WOF-1] <= 1'b0;  out[WOI+WOF-2:0] <= {(WOI+WOF){1'b1}};  overflow <= 1'b1; // 钳到正最大
    end
end
```

这是全库统一的「按最终符号位检测溢出并饱和」收尾（与 `fxp_div_pipe`、`fxp_sqrt_pipe` 末级同源）：正数结果 MSB 却是 1 → 超过正最大 → 钳到正最大；负数结果 MSB 却是 0 → 超过负最小幅值 → 钳到负最小。

#### 4.2.4 代码实践

**目标**：手算一个浮点→定点的转换，追踪 `float2fxp_pipe` 的「右移 + 计数」轨迹。

**操作步骤**（源码阅读型，待本地验证）：

1. 沿用 testbench 的 `WOI=15, WOF=18`（输出 33 位）。取一个软件参考：浮点值 \(= 1.0\)，即 IEEE754 `0x3F800000`，则 `exp=127`，`val=1.0`（`val[23]=1`，其余 0）。
2. 推算流水线轨迹：
   - `expinit = 127 - 15 - 126 = -14`，即需右移 14 位。
   - `outinit`：因 `WOI+WOF-1 = 32 >= 23`，走第一条 generate 分支，`val`（`0x800000`）顶端对齐到 `outinit[32:9]`，即 `outinit = 0x100000000`（`val << 9`）。
   - 经过 14 级右移：`outs` 从 `0x100000000` 变为 `0x100000000 >> 14 = 0x40000`（即 \(1<<18\)），`exps` 从 `-14` 加到 0。
   - 末级不溢出，`out = 0x40000`。还原为定点值：\((\$signed(0x40000))/(2^{18}) = 262144/262144 = 1.0\)。✓
3. 把 `float2fxp_i` 的输入临时改成 `float2 = 32'h3F800000`（或在激励里让 `fxp1` 取一个能产生 `float2=0x3F800000` 的值），运行仿真。

**需要观察的现象**：`fxp4`（单周期）与 `fxp5`（流水线）两列的数值。

**预期结果**：`fxp4` 在当拍给出 `0x40000`（\(=1.0\)）；`fxp5` 经 `WOI+WOF+4=37` 拍延迟后给出相同的 `0x40000`，`overflow5` 为 0。

#### 4.2.5 小练习与答案

**练习 1**：`expinit` 为何取 `exp-(WOI-1)-127`？请用「顶端对齐所需的右移位数」解释。

**答案**：`outinit` 把 `val[23]` 放在 `out[WOI+WOF-1]`，而它正确位置应是 `out[exp-127+WOF]`。两者差 \((WOI+WOF-1)-(exp-127+WOF)=WOI+126-exp\) 位，即需右移 `WOI+126-exp` 位。取负得 `exp-WOI-126 = exp-(WOI-1)-127`，正是 `expinit`。

**练习 2**：generate 的两条分支分别何时命中？testbench 的 `WOI=15,WOF=18` 走哪条？

**答案**：条件 `WOI+WOF-1>=23`（即输出 ≥ 24 位）走「全尾数对齐、不舍入」分支；否则走「截断 + 保护位」分支。`15+18=33 ≥ 24`，命中第一条，`roundinit` 恒为 0。

**练习 3**：为什么 `rounds[ii]` 在「仍在右移」时被赋成 `outs[ii+1][0]`，而「已就位」时改为保持 `rounds[ii+1]`？

**答案**：仍在右移时，每拍移出的最低位就是「当前 LSB 之下一位」，是潜在的舍入保护位，故持续刷新；一旦 `exps==0` 尾数就位，不再有新位移出，需要把最后一个保护位原样带到末级做 ROUND 判断，故改为保持。

---

### 4.3 单周期版作黄金参考与流水线等价性验证

#### 4.3.1 概念说明

`fxp2float` 与 `float2fxp`（单周期）虽因关键路径长不推荐综合，但它们功能正确、无需时钟，是天然的**黄金参考模型（golden reference）**。验证两个 `_pipe` 模块最省力的方法正是 [u3-l1](u3-l1-mul-pipe.md) 与 [u3-l2](u3-l2-div-pipe.md) 反复用过的套路：

> 用同一组输入同时驱动「单周期版」与「流水线版」；把单周期版的输出延迟 `latency` 拍（用移位寄存器做延迟线），再与流水线版输出逐拍比对。若两者在所有有效拍上都相等，则证明流水线版与单周期版功能等价、且延迟对齐正确、无气泡。

`fxp2float_pipe` 与 `float2fxp_pipe` 是库中除 `fxp2float/float2fxp` 外**唯二不调用 `fxp_zoom` 的模块**——它们直接搬动 IEEE754 位域，不经过定点中间格式，所以末级的「符号恢复 + 上下溢出饱和」是各自手写的，这与加减乘除开方依赖 `fxp_zoom` 兜底的风格不同。

#### 4.3.2 核心流程

两个流水线模块的延迟分别为：

\[
\text{latency}(fxp2float\_pipe) = \text{WII}+\text{WIF}+2
\]

\[
\text{latency}(float2fxp\_pipe) = \text{WOI}+\text{WOF}+4
\]

自校验流程：

1. 每拍把当前输入压入一条深度 = `latency` 的延迟线（移位寄存器）。
2. 同一拍把当前输入送进单周期版，得到「当拍参考结果」。
3. 取延迟线中 `latency` 拍前那个输入对应的单周期参考结果，与流水线版「当拍输出」比对。
4. 设 `pass/fail` 计数器，仿真结束 `$display` 汇总。

实际更简单的写法（testbench 里激励是「每拍换一个输入」）：把单周期版的输出本身也送进一条深度 = `latency` 的延迟线，其末端就是「`latency` 拍前的参考结果」，直接与流水线版当拍输出比对即可。

#### 4.3.3 源码精读

[testbench](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) 已经把四个模块一起例化了，这是本讲实践的改造基础：

- `fxp2float` 实例输出 `float2`（[SIM/tb_convert_fxp_float.v:40-46](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L40-L46)），`fxp2float_pipe` 输出 `float3`（[SIM/tb_convert_fxp_float.v:49-57](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L49-L57)），二者共用输入 `fxp1`。
- `float2fxp` 输出 `fxp4/overflow4`（[SIM/tb_convert_fxp_float.v:60-68](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L60-L68)），`float2fxp_pipe` 输出 `fxp5/overflow5`（[SIM/tb_convert_fxp_float.v:71-81](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L71-L81)），二者共用输入 `float2`。
- 时钟 50MHz、`rstn` 在第 4 拍拉高（[SIM/tb_convert_fxp_float.v:25-28](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L25-L28)）。
- 现有 `always @(posedge clk)` 块只做 `$display` 打印（[SIM/tb_convert_fxp_float.v:84-97](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L84-L97)），没有比对逻辑——这正是要补上的自校验。
- 激励末尾用 `repeat(WII+WIF+WOI+WOF+8)` 排空流水线（[SIM/tb_convert_fxp_float.v:146-147](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L146-L147)），保证两组延迟都能在 `$finish` 前被采到。

注意现有配置 `WII=16,WIF=16` 与 `WOI=15,WOF=18` 下，两个延迟分别为 34 拍与 37 拍，而排空长度 `16+16+15+18+8=73` 拍足够覆盖。

#### 4.3.4 代码实践

**目标**：把现有「只打印」的 testbench 改造成带 `pass/fail` 计数的自校验 testbench，确认流水线版与单周期版完全一致。

**操作步骤**：

1. 复制 [SIM/tb_convert_fxp_float.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) 为一份新的 testbench（例如 `tb_convert_fxp_float_selfcheck.v`，放回 `SIM/` 即可——本任务只要求不修改源码 `RTL/`，新增测试文件属于讲义配套实践，若你不想新增文件也可直接在脑海里推演）。
2. 声明延迟线与计数器（示例代码，非项目原有）：

```verilog
// 示例代码：自校验延迟线与计数器
localparam LAT2 = WII+WIF+2;     // fxp2float_pipe 延迟 = 34
localparam LAT5 = WOI+WOF+4;     // float2fxp_pipe 延迟 = 37
reg [31:0]        float2_d [0:LAT2-1];   // float2 的延迟线
reg [WOI+WOF-1:0] fxp4_d   [0:LAT5-1];   // fxp4   的延迟线
reg        of4_d [0:LAT5-1];
integer pass2, fail2, pass5, fail5;
integer k;
```

3. 在 `always @(posedge clk)` 里维护延迟线并比对（示例代码）：

```verilog
// 示例代码：移位延迟线 + 逐拍比对
float2_d[0] <= float2;
for(k=1;k<LAT2;k=k+1) float2_d[k] <= float2_d[k-1];
if(float2_d[LAT2-1] !== 32'bx && rstn) begin
    if(float3 === float2_d[LAT2-1]) pass2 = pass2 + 1;
    else begin fail2 = fail2 + 1;
        $display("MISMATCH fxp2float: pipe=%h ref=%h", float3, float2_d[LAT2-1]);
    end
end
// float2fxp 同理，用 fxp4_d / of4_d 与 fxp5 / overflow5 比对
```

4. 在 `$finish` 前加汇总（示例代码）：

```verilog
// 示例代码
$display("==== fxp2float_pipe: pass=%0d fail=%0d", pass2, fail2);
$display("==== float2fxp_pipe: pass=%0d fail=%0d", pass5, fail5);
```

5. 用 `iverilog -g2001 -o sim.out <tb>.v ../RTL/fixedpoint.v && vvp -n sim.out` 编译运行。

**需要观察的现象**：仿真日志末尾的汇总行。

**预期结果**：两组 `fail=0`。若出现 `fail>0`，最常见原因是延迟拍数算错（务必用 `WII+WIF+2` 与 `WOI+WOF+4`）或复位期间把 `x` 也比对了（用 `!== 32'bx` 过滤）。预期结果（fail=0）待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么延迟线深度必须是 `latency`，而不是 `latency-1`？

**答案**：流水线输出在输入之后第 `latency` 个上升沿才稳定。要把「当拍的单周期参考结果」对齐到「当拍的流水线输出」，必须取 `latency` 拍之前的参考值，即延迟线深度 = `latency`（延迟线 `[0]` 是 1 拍前，`[latency-1]` 是 `latency` 拍前）。

**练习 2**：`fxp2float_pipe` 末级在 `exp[0]>=255` 时输出 `expo=255, valo=0xFFFFFF`（NaN 编码），而单周期 `fxp2float` 在 `expz>=255` 时饱和为 `expt=254, tail=0x7FFFFF`（最大有限浮点）。这是否会破坏等价性？

**答案**：不会。该分支只在 `WII` 极大（`WII>=129`，即 `WII+127-1>=255`）时才可能触达；对常规配置（如本讲 `WII=16`），`exp[0]` 上限是 `16+127-1=142`，远小于 255，该分支不可达，两模块在所有可达输入上仍完全一致。这正体现了「自校验验证」的价值——它能精确告诉你哪些配置下两版真正等价。

**练习 3**：为什么 `float2fxp_pipe` 和 `fxp2float_pipe` 内部都不再例化 `fxp_zoom`？

**答案**：`fxp_zoom` 是「定点↔定点」的位宽搬运原语；而这两个模块处理的是「定点↔IEEE754 浮点位域」的转换，需要直接操作阶码/尾数/符号位域，不存在「定点中间格式对齐」这一步，所以不经过 `fxp_zoom`，末级的舍入与饱和也由各自手写。

## 5. 综合实践

把本讲三个要点（规格化流水线、移位流水线、自校验）串成一个**往返（round-trip）流水线链**验证：

1. 在改造后的自校验 testbench 中，构造一条数据通路：`fxp1`（定点）→ `fxp2float_pipe`（→`float3`）→ 把 `float3` 接到 `float2fxp_pipe` 的输入（→`fxp5`），形成「定点→浮点→定点」往返。
2. 由于 `float2fxp_pipe` 的输入现在是 `float3`（已被 `fxp2float_pipe` 延迟 34 拍），再叠加 `float2fxp_pipe` 自身 37 拍，总延迟为 71 拍。用一条 71 级延迟线保存原始 `fxp1`，在末端与 `fxp5` 比对。
3. 设计激励覆盖：正值、负值（如 `0xf6360551`）、接近 0 的小值（如 `0x00000010`）、接近溢出的大值（如 `0x7a164399`）、以及 0 本身。
4. 统计 `fxp5` 与（延迟后的）`fxp1` 之间的误差。因为浮点尾数只有 23 位，而定点 `WIF=16`、`WOF=18`，多数值的往返误差应为 0；只有当定点位宽超过 24 位精度时才可能出现非零误差。

**预期**：在 24 位浮点精度够用的输入上，往返 `fail=0`；超出 24 位精度的输入会出现可解释的非零误差。完整现象待本地验证。

## 6. 本讲小结

- `fxp2float_pipe` 把单周期版的「扫描找前导 1」改写成「逐级左移规格化 + 阶码递减」，用 `sign/exp/inu` 级间寄存器数组展开成 `WII+WIF+2` 级流水线，核心是「左移 1 位 + 阶码减 1」保持数值不变的恒等式。
- `float2fxp_pipe` 把单周期版的「逐位安放尾数」改写成「尾数顶端对齐 + 逐级右移 + 指数计数递减到 0」，用 `outs/rounds/exps/signs` 数组展开成 `WOI+WOF+4` 级流水线，核心是「右移 1 位 + 指数加 1」的对偶恒等式。
- `generate` 按「输出位宽 `WOI+WOF` 是否 ≥ 24」分两条路径：装得下 24 位尾数则顶端对齐不舍入，装不下则截断并把保护位存入 `roundinit/rounds` 供 ROUND 使用。
- 两个 `_pipe` 模块末级都手写「符号恢复（取补码）+ 按符号位检测上/下溢出并饱和」的收尾，因为它们与 `fxp2float/float2fxp` 一样是库中唯二不调用 `fxp_zoom` 的模块。
- 验证方法学：用单周期版作黄金参考，其输出经 `latency` 拍延迟线对齐后，逐拍与流水线版比对并统计 `pass/fail`，一步验证「延迟对齐 + 无气泡 + 功能等价」。

## 7. 下一步学习建议

- 阅读 [SIM/tb_convert_fxp_float.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) 的全部激励向量，挑几条手算往返轨迹，巩固对两个恒等式的直觉。
- 回顾 [u3-l6 仿真验证方法学](u3-l6-simulation-testbench.md)（若已生成），把本讲的自校验套路与全库四个 testbench 的共同范式统一起来。
- 进阶挑战：尝试把 `fxp2float_pipe` 的「逐级左移」改成「前导零计数器（CLZ）+ 桶形移位器」的两级高吞吐实现，并与本讲的逐级版本对比面积/频率取舍。
- 若关心综合指标，可把 `fxp2float_pipe` 与 `float2fxp_pipe` 在 FPGA 综合工具中查看其最高时钟频率 `f_max`，体会「关键路径切断 → 频率提升」的实际收益。
