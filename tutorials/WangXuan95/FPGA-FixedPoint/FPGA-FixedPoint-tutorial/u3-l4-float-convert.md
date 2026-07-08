# fxp2float 与 float2fxp：IEEE754 浮点互转（单周期）

## 1. 本讲目标

本讲是专家层「浮点互转」主题的第一篇，聚焦两个**单周期（纯组合逻辑）**模块：

- `fxp2float`：把本库的定点数转换成 IEEE754 单精度浮点数；
- `float2fxp`：把 IEEE754 单精度浮点数转换回本库的定点数。

学完本讲你应当能够：

1. 说出 IEEE754 单精度 `{sign, 8bit exp, 23bit tail}` 的位域结构、隐含前导 1 的含义，以及 `value = (-1)^sign × 1.tail × 2^(exp-127)` 的换算公式；
2. 读懂 `fxp2float` 如何“从高到低扫描第一个 1”来确定指数 `expz = jj+127-WIF`，并收集其后的 23 位作为尾数；
3. 读懂 `float2fxp` 如何用起始下标 `expi = exp2-127+WOF`，把 24 位尾数逐位“安放”到定点输出码的对应比特上，并处理 `ROUND` 舍入与溢出饱和；
4. 理解两个模块对“超大值（指数溢出）”与“超小值（下溢截断）”分别如何饱和或截断；
5. 搭建一个往返（round-trip）自校验环境，并解释为何单周期版本“时序不易收敛、工程中应改用流水线版本”。

本讲只依赖 u1-l2 建立的定点格式与参数命名约定，不涉及流水线（流水线版本 `fxp2float_pipe` / `float2fxp_pipe` 留给下一讲 u3-l5）。

---

## 2. 前置知识

### 2.1 定点数格式速查（来自 u1-l2）

本库的定点数 = **二进制补码整数 ÷ 2^小数位宽**。一个 `(WII, WIF)` 定点数共 `WII+WIF` 位，其中 `WII` 是整数位宽（含 1 位符号），`WIF` 是小数位宽。解码与编码公式为：

\[
v = c \,/\, 2^{W_{IF}}, \qquad c = \text{round}(v \times 2^{W_{IF}})
\]

仿真里把码值还原为浮点的“万能钥匙”是 `$signed(c)*1.0/(1<<WIF)`。

### 2.2 IEEE754 单精度浮点格式

IEEE754 单精度共 32 位，分成三个位域：

| 位域 | 位数 | 含义 |
| :--: | :--: | :-- |
| `sign` | 1 位（bit 31） | 符号：0 正 1 负 |
| `exp` | 8 位（bit 30~23） | 阶码，偏置 bias = 127 |
| `tail` | 23 位（bit 22~0） | 尾数的小数部分 |

关键是**隐含的前导 1**：对于规格化数（`1 ≤ exp ≤ 254`），真实尾数是 `1.tail`，即在小数点左边补一个不占用位域的 `1`。于是真实数值为：

\[
\text{value} = (-1)^{\text{sign}} \times 1.\text{tail} \times 2^{(\text{exp}-127)}
\]

几个特例（本讲会用到）：

- **零值**：`exp=0` 且 `tail=0`，表示 ±0。
- **最大有限值**：`exp=254, tail=0x7FFFFF`，约 \(1.797 \times 10^{38}\) 量级（\( \approx (2-2^{-23})\times 2^{127} \)）。
- **Inf / NaN**：`exp=255`（8 位全 1）。本库把它视作“无法表示的超大值”，直接判溢出。

> 术语约定：本讲把 8 位阶码记作 `exp`/`exp2`，把换算后的“真实指数”记作 \(e = \text{exp}-127\)。注意区分“阶码字段”和“真实指数”。

### 2.3 与本库其它模块的关系

`fxp2float` 和 `float2fxp` 是本库中**仅有的两个不使用 `fxp_zoom` 做收尾**的运算模块（加减乘除开方都例化了 `fxp_zoom`）。它们自己直接完成舍入与溢出饱和，因为目标/源是浮点格式而非定点格式，无法套用 `fxp_zoom` 的“定点位宽搬运”逻辑。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| :-- | :-- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在。本讲只看 `fxp2float`（L874–L923）与 `float2fxp`（L1039–L1097）两段。 |
| [SIM/tb_convert_fxp_float.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v) | 浮点互转的 testbench。它本身就是一条往返链：`fxp1 → fxp2float → float2 → float2fxp → fxp4`，是本讲综合实践的现成脚手架。 |

---

## 4. 核心概念与源码讲解

## 4.1 fxp2float：从定点到 IEEE754

### 4.1.1 概念说明

`fxp2float` 要解决的问题：给定一个 `(WII, WIF)` 定点数，输出与之数值最接近的 IEEE754 单精度浮点数。

它**不依赖** `fxp_zoom`，因为输出格式（浮点位域）与输入格式（补码定点）结构完全不同。核心思路是“归一化”：

1. 取绝对值，把有符号定点数归约成无符号幅值 `inu`；
2. 找到 `inu` 中最高的那个 `1`（前导 1），它决定了数值的数量级，也就决定了浮点的阶码；
3. 前导 1 之后的 23 位，正好可以作为浮点的尾数 `tail`（前导 1 本身就是 IEEE754 那个“隐含的 1”，不需要存）；
4. 拼上符号位，处理零值与“太大放不下”两种特例。

### 4.1.2 核心流程

设前导 1 位于 `inu` 的第 `jj` 位（从 0 起编号）。因为定点真值幅值 \( = \text{inu}/2^{W_{IF}} \)，而 `inu` 落在 \([2^{jj}, 2^{jj+1})\)，所以真值幅值落在：

\[
2^{jj-W_{IF}} \leq |\text{value}| < 2^{jj-W_{IF}+1}
\]

写成 IEEE754 规格化形式 \(1.\text{tail}\times 2^{e}\)，则真实指数 \(e = jj - W_{IF}\)，对应阶码：

\[
\text{expz} = (jj - W_{IF}) + 127 = jj + 127 - W_{IF}
\]

这就是模块里 `expz = jj+127-WIF` 的来历。

伪代码流程：

```
sign = in 的符号位
inu  = |in|                      // 取绝对值（补码取反加一）
if inu == 0:  输出 ±0.0          // 特例：零值
else:
    jj   = inu 中最高位 1 的位置
    expz = jj + 127 - WIF        // 由前导 1 算阶码
    tail = inu[jj-1 : jj-23]     // 前导 1 之后的 23 位（不足补 0）
    if expz >= 255:              // 太大，单精度装不下
        expz = 254; tail = 0x7FFFFF   // 饱和到最大有限浮点
    输出 {sign, expz, tail}
```

注意一个细节：`fxp2float` 对尾数是**直接截断**（取前导 1 之后的前 23 位，丢弃更低位的比特），不做四舍五入。没有 `ROUND` 参数。

### 4.1.3 源码精读

模块端口只有输入定点 `in` 和输出 32 位浮点 `out`，参数仅有 `WII` 和 `WIF`（单目运算）：

[RTL/fixedpoint.v:L874-L882](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L874-L882) — 定义 `fxp2float` 的端口：输入 `[WII+WIF-1:0] in`，输出 `[31:0] out`，`initial out=0` 避免仿真出现 `x`。

第一步，提取符号并取绝对值：

[RTL/fixedpoint.v:L886-L887](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L886-L887) — `sign` 取最高位（定点符号位）；`inu = sign ? (~in)+ONEI : in`，负数则补码取反加一得到幅值，正数原样。`inu` 后续被当作**无符号**整数使用（即使补码取反加一在“最负值”处回绕，作为无符号幅值其前导 1 的位置仍然正确）。

第二步是核心扫描循环，**同时完成两件事**：遇到第一个 1 时记录阶码，其后把后续位收集进尾数：

[RTL/fixedpoint.v:L902-L911](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L902-L911) — `for(jj=WII+WIF-1; jj>=0; jj=jj-1)` 从最高位往最低位扫。关键三行：

- L903-L906：`if(flag && ii>=0)` 表示“已经找到前导 1，且尾数还没收满 23 位”，就把当前位 `inu[jj]` 写进 `tail[ii]`（`ii` 从 22 递减），这正是“前导 1 之后的位”。
- L907-L910：`if(inu[jj])` 当前位是 1；`if(~flag)` 表示这是**第一个** 1，于是 `expz = jj+127-WIF` 算出阶码，并把 `flag` 置 1。

  > 为什么先判 `flag` 再置 `flag`？因为当 `jj` 落到前导 1 那一位时 `flag` 还是 0，L903 的收尾数分支被跳过——前导 1 本身不入尾数（它对应 IEEE754 隐含的 1）。紧接着 L907 置 `flag=1`，从**下一轮**起开始收尾数。顺序设计得很巧。

第三步，特例处理：零值与阶码超限饱和：

[RTL/fixedpoint.v:L913-L918](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L913-L918) — 若 `expz < 255`：`inu==0` 时输出阶码 0（即 ±0.0），否则取 `expz` 低 8 位；若 `expz >= 255`（数值过大，单精度最大阶码只能到 254），强制 `expt=254, tail=0x7FFFFF`，饱和成“最大有限浮点数”。

最后拼接输出：

[RTL/fixedpoint.v:L920](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L920) — `out = {sign, expt, tail}`，按 IEEE754 位域拼成 32 位浮点。

### 4.1.4 代码实践

**实践目标**：用一个具体定点值手动走一遍 `fxp2float`，再上仿真核对。

**操作步骤**：

1. 取 testbench 的配置 `WII=16, WIF=16`，输入 `fxp1 = 0x00201551`。
2. 手算：
   - 符号位 = 0（正数），`inu = 0x00201551 = 2105169`。
   - 真值 \( = 2105169 / 2^{16} \approx 32.1242 \)。
   - `0x00200000 = 2^21`，故前导 1 在 `jj=21`。
   - 阶码 `expz = 21 + 127 − 16 = 132`（十六进制 `0x84`）。
3. 打开 [SIM/tb_convert_fxp_float.v:L40-L46](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L40-L46) 看 `fxp2float` 的例化；再用 [SIM/tb_convert_fxp_float_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float_run_iverilog.bat) 里的命令编译运行：

   ```
   iverilog -g2001 -o sim.out SIM/tb_convert_fxp_float.v RTL/fixedpoint.v
   vvp -n sim.out
   ```

**需要观察的现象**：testbench 在 [SIM/tb_convert_fxp_float.v:L88-L96](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L88-L96) 用 `$signed(fxp1)*1.0/(1<<WIF)` 把定点码打印成浮点，并打印 `float2` 的十六进制。

**预期结果**：当 `fxp1=0x00201551` 时，打印的 `fxp1≈32.1242`，`float2` 的阶码字节（bit 30~23）应为 `0x84`（即 132）。

**待本地验证**：具体尾数字节需以本地 iverilog 输出为准（作者环境未运行）。

### 4.1.5 小练习与答案

**练习 1**：`WII=16, WIF=16` 时，定点输入 `0x00000010`（真值 \(16/2^{16}=2^{-12}\)）经 `fxp2float` 后阶码是多少？

**答案**：前导 1 在 `jj=4`（`0x10 = 2^4`），`expz = 4 + 127 − 16 = 115`。阶码字段为 115。

**练习 2**：为什么 `fxp2float` 不需要 `ROUND` 参数，而 `float2fxp` 需要？

**答案**：`fxp2float` 收集尾数时多余的低比特直接丢弃（截断），不做四舍五入，所以没有 `ROUND`；`float2fxp` 把浮点尾数安放到定点时，低于 LSB 的第一位可能需要进位（影响 0.5 LSB 处的舍入），因此提供 `ROUND` 开关。

**练习 3**：若输入定点值真值幅值超过 \(2^{127}\)，`fxp2float` 会输出什么？

**答案**：此时 `expz ≥ 255`，命中 L915-L918 的饱和分支，输出阶码 254、尾数 `0x7FFFFF`，即“最大有限浮点数”，符号位仍随 `sign`。

---

## 4.2 float2fxp：从 IEEE754 到定点

### 4.2.1 概念说明

`float2fxp` 是 `fxp2float` 的逆运算：输入 32 位 IEEE754 浮点，输出 `(WOI, WOF)` 定点码与 `overflow` 标志。

核心思路是**逐位安放**：浮点尾数（含隐含 1）是 24 位定点小数 `1.tail`，真实数值 \( = \text{val} \times 2^{(\text{exp}-127-23)} \)（其中 `val = {1, tail}` 共 24 位）。要把它变成 `WOF` 位小数的定点码（码值 \( = \text{真值} \times 2^{W_{OF}} \)），只需把 `val` 的每一位放到输出码的正确比特位置上。

### 4.2.2 核心流程

`val` 的最高位 `val[23]`（隐含的 1）代表真值 \(2^{(\text{exp}-127)}\)。定点输出码的位 `p` 代表真值 \(2^{(p-W_{OF})}\)。令二者相等：

\[
p - W_{OF} = \text{exp} - 127 \;\Rightarrow\; p = \text{exp} - 127 + W_{OF}
\]

记这个起始位置为 `expi = exp2 - 127 + WOF`。随后 `val` 每降低一位，`expi` 递减 1：`val[23]→expi`、`val[22]→expi-1`、……、`val[0]→expi-23`。

伪代码流程：

```
{sign, exp2, tail} = in          // 拆位域
val = {1'b1, tail}               // 补上隐含的前导 1，得到 24 位尾数
expi = exp2 - 127 + WOF          // 最高尾数位要安放的输出码位置
out  = 0; overflow = 0; round = 0

if exp2 == 255:                  // Inf / NaN
    overflow = 1
else if in[30:0] != 0:           // 非零（跳过 ±0）
    for ii = 23 downto 0:
        if val[ii]:              // 该尾数位为 1 才安放
            if expi >= WOI+WOF-1:        // 落在符号位或更高 → 溢出
                overflow = 1
            else if expi >= 0:           // 落在码字范围内 → 置位
                out[expi] = 1
            else if ROUND and expi == -1: // 恰好低出 LSB 一位 → 舍入位
                round = 1
            // expi < -1：太小，直接丢弃（下溢截断）
        exppi = expi - 1
    if round: out = out + 1      // 四舍五入进位

// 结果是“幅值码”，最后按符号处理：
if overflow:                     // 饱和钳位
    sign==1 ? out = {1, 0...0} (负最小) : out = {0, 1...1} (正最大)
else if sign:                    // 负数：补码取反加一
    out = (~out) + 1
```

几个要点：

- **`ROUND` 的几何含义**：`expi == -1` 表示该位正好落在“LSB 再低一位”，即 0.5 LSB 处。`ROUND=1` 时把它记进 `round`，循环结束后 `out = out+1` 实现四舍五入。
- **溢出判定按位宽而非按数值**：只要任一尾数位需要落到符号位（位置 `WOI+WOF-1`）或更高，就判 `overflow`。
- **下溢（太小）**：所有位都落到负位置（`expi < 0`）且无舍入位时，`out` 保持 0，相当于静默截断为 0。

### 4.2.3 源码精读

端口：输入 32 位浮点 `in`，输出 `[WOI+WOF-1:0] out` 与 `overflow`，参数 `WOI/WOF/ROUND`（注意这里用 `O` 不是 `I`，因为输出是定点）：

[RTL/fixedpoint.v:L1039-L1049](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1039-L1049) — `float2fxp` 端口定义。

拆位域、补隐含 1、算起始位置 `expi`：

[RTL/fixedpoint.v:L1063-L1066](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1063-L1066) — `{sign, exp2, val[22:0]} = in` 一次拆出三段；`val[23] = 1'b1` 强行补上隐含前导 1；`expi = exp2-127+WOF` 是 32 位**有符号**（`reg signed [31:0]`），所以能表示负的下标。

Inf/NaN 特判：

[RTL/fixedpoint.v:L1067-L1068](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1067-L1068) — `if(&exp2)` 即 8 位阶码全 1（=255），直接 `overflow=1`；`else if(in[30:0]!=0)` 排除 ±0（全 0 输入直接得 `out=0`）。

逐位安放主循环：

[RTL/fixedpoint.v:L1069-L1080](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1069-L1080) — `for(ii=23; ii>=0; ii=ii-1)` 从高位尾数扫到低位。`if(val[ii])` 该位为 1 才处理，三分支：`expi>=WOI+WOF-1` 溢出、`expi>=0` 置 `out[expi]`、`ROUND && expi==-1` 置 `round`。每轮结尾 `expi = expi-1` 把游标左移一位（注意 Verilog 阻塞赋值在 `always @(*)` 中按顺序执行）。

舍入进位：

[RTL/fixedpoint.v:L1081](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1081) — `if(round) out=out+1`，把 0.5 LSB 处的进位加上。

饱和与取补收尾：

[RTL/fixedpoint.v:L1083-L1094](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1083-L1094) — 若 `overflow`：负数饱和到 `out[MSB]=1, 其余=0`（负最小 \(=-2^{WOI-1}\)），正数饱和到 `out[MSB]=0, 其余=1`（正最大 \(=2^{WOI-1}-2^{-WOF}\)）；若未溢出但 `sign` 为负，`out = (~out)+ONEO` 补码取反加一，把“幅值码”转回负数补码。

### 4.2.4 代码实践

**实践目标**：手动安放一个浮点值的尾数位，并预测溢出/饱和行为。

**操作步骤**：沿用 testbench 配置 `WOI=15, WOF=18`（输出码 33 位，符号位在位置 32）。

1. **正常值**：取真值 \( \approx 32.1242 \)，对应 `exp2=132`。`expi = 132−127+18 = 23`。最高位 `val[23]` 安放到 `out[23]`，远小于符号位 32，**不溢出**。
2. **超大值（指数接近 254）**：取 `exp2=150`（真值约 \(2^{23}\)）。`expi = 150−127+18 = 41 ≥ 32`，命中 `overflow`。预期 `overflow=1`，正数时 `out` 被钳到正最大（MSB=0、其余=1）。
3. **Inf**：取 `exp2=255`。命中 `&exp2`，`overflow=1`，同样饱和。
4. **极小值（指数接近 0）**：取 `exp2=1`（真值约 \(2^{-126}\)）。`expi = 1−127+18 = -108`，所有尾数位都落到负位置，`out=0`（下溢截断为 0）。

可在 testbench 里直接例化一个独立的 `float2fxp`，把上述 4 个 32 位浮点字面量喂进去观察。例化模板参考 [SIM/tb_convert_fxp_float.v:L60-L68](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L60-L68)。

**需要观察的现象**：`overflow` 在前三种情况下为 1，第四种为 0 且 `out≈0`。

**预期结果**：超大值与 Inf 的 `out` 被钳到正/负极值；极小值 `out` 为 0。**待本地验证**具体码值。

### 4.2.5 小练习与答案

**练习 1**：`WOI=15, WOF=18` 时，`exp2` 大于等于多少就一定溢出？

**答案**：当 `expi = exp2−127+18 ≥ WOI+WOF−1 = 32`，即 `exp2 ≥ 141` 时，最高位会落到符号位，判溢出。

**练习 2**：为什么 `float2fxp` 先把 `out` 当作“幅值码”累加，最后才统一取补，而不是直接处理负数？

**答案**：因为逐位安放逻辑（`out[expi]=1`、舍入进位、溢出判定）都基于“幅值”更简单统一；负数只是符号不同，幅值相同。先算幅值码、末尾按 `sign` 取补（`(~out)+1`），可以复用同一套安放/舍入/饱和逻辑，避免对符号位分段处理。

**练习 3**：`ROUND=0` 与 `ROUND=1` 在 `float2fxp` 里的唯一行为差异是什么？

**答案**：差异只在 `expi==-1` 那一位（0.5 LSB 处）。`ROUND=1` 时它触发 `round=1` 进而对 `out` 加 1（四舍五入）；`ROUND=0` 时该位被丢弃（截断），`out` 不变。其余位的行为完全相同。

---

## 5. 综合实践：往返（round-trip）自校验

本讲的贯穿任务，是把 `fxp2float` 和 `float2fxp` 串成一条往返链并做误差统计——而 testbench 已经搭好了这条链的骨架。

**任务目标**：随机生成若干定点值 → `fxp2float` → `float2fxp` → 比对还原值与原值的误差，统计最大误差；再单独测试指数极大与极小的浮点输入，确认 `overflow` 与饱和/截断行为。

**操作步骤**：

1. **读懂现成的往返链**。[SIM/tb_convert_fxp_float.v:L31-L81](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L31-L81) 里，`fxp1`（定点）→ `fxp2float_i` → `float2`（浮点）→ `float2fxp_i` → `fxp4`（定点）。注意 `float2fxp` 用的是另一套位宽 `WOI=15, WOF=18`（与输入 `WII=16, WIF=16` 不同），所以 round-trip 误差同时来自两次格式变换。
2. **加误差统计**。在 [SIM/tb_convert_fxp_float.v:L84-L97](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L84-L97) 的 `always @(posedge clk)` 里，每次打印时计算 `err = abs(fxp1_as_float - fxp4_as_float)`，用 `real` 变量记录 `maxerr`，并在 `$finish` 前 `$display` 汇总。
3. **加 pass/fail 计数**。定义 `integer pass=0, fail=0;`，当 `err` 在 1 LSB（即 \(1/2^{W_{OF}}\)）以内且 `overflow4` 符合预期时 `pass++`，否则 `fail++`；仿真结束打印 `PASS=x FAIL=y`。
4. **测试极端指数**。再例化一个独立的 `float2fxp`（参考 L60-L68），喂入手构的 32 位浮点：`exp=254`（接近最大）、`exp=255`（Inf）、`exp=1`（接近最小）、`exp=0 & tail=0`（零），观察 `overflow` 与 `out` 的饱和/截断。

**需要观察的现象**：

- 正常 round-trip：`fxp4` 打印的浮点值应与 `fxp1` 非常接近，`maxerr` 很小（量级在 \(1/2^{W_{OF}}\) 附近）。
- `exp=254/255`：`overflow4=1`，`fxp4` 钳到正最大（`overflow` 标记 `(o)` 出现，见 [SIM/tb_convert_fxp_float.v:L92-L95](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L92-L95) 的 `(o)` 打印）。
- `exp=1`：`fxp4≈0`（下溢截断），`overflow4=0`。

**预期结果**：`fail=0`（在 1 LSB 容差内），`maxerr` 不超过约 \(1/2^{W_{OF}}\)。极端指数的饱和/截断行为符合 4.2.4 的分析。**待本地验证**：具体 `maxerr` 数值与计数结果以本地 iverilog 运行为准。

> 提示：testbench 的激励在 [SIM/tb_convert_fxp_float.v:L100-L147](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_convert_fxp_float.v#L100-L147)，覆盖了正/负、大/小多种定点值，可作为随机化的起点；你也可以把固定字面量换成 `$random` 生成更多样本。

---

## 6. 本讲小结

- `fxp2float`（定点→浮点）的内核是**找前导 1**：前导 1 的位置 `jj` 决定阶码 `expz = jj+127-WIF`，其后 23 位成为尾数，前导 1 对应隐含的 1 不入尾数。
- `fxp2float` 对尾数**直接截断**（无 `ROUND`）；零值输出阶码 0；阶码 `≥255` 时饱和到最大有限浮点（exp=254, tail=0x7FFFFF）。
- `float2fxp`（浮点→定点）的内核是**逐位安放**：起始位置 `expi = exp2-127+WOF`，24 位尾数（含隐含 1）从高位到低位依次落到 `out[expi], out[expi-1], …`。
- `float2fxp` 的 `ROUND` 只影响 `expi==-1`（0.5 LSB）那一位；落到符号位及以上判 `overflow` 并饱和到正最大/负最小；所有位都低于 LSB 时下溢截断为 0。
- `float2fxp` 先把 `out` 当作“幅值码”累加，末尾再按 `sign` 补码取反加一（`(~out)+1`），统一处理符号。
- 两个模块都是**单周期纯组合逻辑**，关键路径长（`fxp2float` 的扫描循环、`float2fxp` 的 24 次逐位安放都是长串联链），时序不易收敛，工程中应改用流水线版本。

---

## 7. 下一步学习建议

- **下一讲 u3-l5** 会把这两个模块流水线化：`fxp2float_pipe`（WII+WIF+2 级，把逐位扫描展开成级联寄存器）与 `float2fxp_pipe`（WOI+WOF+4 级，用 `outs/rounds/exps/signs` 数组配合阶码递减逐级移位安放尾数）。学完本讲对“扫描”和“安放”两套串行循环的理解，正是流水线展开的直接素材。
- 若想巩固本讲，可回看 `fxp_div`（u2-l3）的恢复余数法循环——它与 `fxp2float` 的扫描循环、`float2fxp` 的安放循环同属“`for` 循环实现串行位处理”的单周期范式，对照阅读能加深对“为什么这些模块都需要流水线化”的理解。
- 阅读建议：先重读 [RTL/fixedpoint.v:L874-L923](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L874-L923) 和 [RTL/fixedpoint.v:L1039-L1097](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1039-L1097)，带着本讲的伪代码逐行印证，再进入 u3-l5。
