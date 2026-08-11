# 平方根与 DSP 乘法

## 1. 本讲目标

本讲属于「FPGA 数学运算」进阶篇，承接 [u7-l1（定点数与 Q 格式）](u7-l1-number-representation.md)。读完后你应该能够：

- 说清楚「逐位求平方根（digit-by-digit square root）」的数学递推关系，并解释为什么它必须做成多周期状态机；
- 对照 [sqrt_int.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv)（整数版）与 [sqrt.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv)（定点版），讲出两者在迭代次数 `ITER` 上的关键差异及其原因；
- 看懂 [mul.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv) 如何用一行 `a1 * b1` 让综合工具推断出 DSP48 硬核乘法器，并理解它如何完成定点缩放、高斯舍入与溢出检测。

一句话定位：**平方根靠「迭代」一拍出一位、与除法同族；乘法靠「DSP」单拍出结果、但需要补上定点舍入与溢出处理。** 这一对比是本讲的主线。

## 2. 前置知识

本讲默认你已掌握 u7-l1 的概念，这里做一次最小回顾：

- **二进制补码与 `signed`**：同一串比特在无符号与有符号下数值不同；`signed` 关键字改变运算语义（符号扩展、算术比较）。projf 库把算术端口统一标 `signed`。
- **Q 格式定点数**：用 `WIDTH`（总位宽）与 `FBITS`（小数位）约定，整数位 `IBITS = WIDTH − FBITS`（含一位符号）。小数点只是设计者心中的隐含位置，底层全是整数运算。本讲的两个模块（sqrt、mul）都带 `FBITS` 参数。
- **乘法的小数位翻倍**：两个 Q(WIDTH).(FBITS) 数相乘，乘积是 Q(2·WIDTH).(2·FBITS)，小数位翻倍，必须截回原格式。
- **高斯舍入（round half to even，向偶舍入）**：当被丢弃部分恰好等于 0.5 ULP 时，向「最近的偶数」舍入，以消除统计偏差。u7-l2 的除法器也用了同一套舍入。

此外请回忆一个常识：**FPGA 里乘法和除法实现成本天差地别**。Xilinx 7 系列 FPGA 每片内建大量 DSP48E1 切片，自带一个 25×18 位有符号硬件乘法器，做一次乘法只要一拍；而除法、开方没有专用硬核，必须用移位-比较的迭代状态机一拍出一位。本讲正是围绕这一不对称展开。

## 3. 本讲源码地图

本讲四个最小模块全部来自 projf 库的 `lib/maths/` 分区，外加两个现成 testbench：

| 文件 | 作用 | 本讲对应模块 |
|------|------|--------------|
| `ThreePart/projf-explore/lib/maths/sqrt_int.sv` | 整数平方根，逐位迭代 | 4.2 |
| `ThreePart/projf-explore/lib/maths/sqrt.sv` | 定点平方根，多迭代位补小数 | 4.3 |
| `ThreePart/projf-explore/lib/maths/mul.sv` | 有符号定点乘法，高斯舍入，映射 DSP48 | 4.4 |
| `ThreePart/projf-explore/lib/maths/README.md` | maths 分区模块清单与博客索引 | 全讲索引 |
| `ThreePart/projf-explore/lib/maths/xc7/sqrt_int_tb.sv` | 整数平方根的 Vivado/Icarus testbench | 4.2 实践 |
| `ThreePart/projf-explore/lib/maths/xc7/sqrt_tb.sv` | 定点平方根（Q8.8）testbench | 4.3 实践 |
| `ThreePart/projf-explore/lib/maths/test/mul.py` + `mul.mk` | mul 的 cocotb（Python）功能测试 | 4.4 实践 |

贯穿全讲的「迭代求根」数学原理（模块 4.1）不对应单个文件，而是 4.2/4.3 两个 `.sv` 文件共享的算法内核。

---

## 4. 核心概念与源码讲解

### 4.1 逐位求平方根的数学原理（迭代求根）

#### 4.1.1 概念说明

求一个数的平方根，在软件里通常直接调 `sqrt()`；但在硬件里没有「开方硬核」，必须自己算。最经典、最适合硬件的方法是 **逐位求平方根（digit-by-digit square root）**，也叫 **不恢复余数法 / 数字递推法**。它和手工做除法的姿势非常像：**从被开方数的最高位开始，每次取 2 个比特，每拍确定 1 个结果比特**。

为什么「每次 2 个比特、每拍 1 个结果比特」？因为一个 N 位的数，其平方根最多只有 N/2 位（\(\sqrt{2^{N}} = 2^{N/2}\)）。所以每处理被开方数的 2 个比特，正好对应根的 1 个比特。

这个方法的吸引力在于：**全程只用减法、比较和移位，完全不需要乘法器**——这对没有 DSP 资源的小 FPGA（或想省 DSP 给别处用的设计）非常友好。

#### 4.1.2 核心流程

设被开方数为 \(X\)。我们维护两个量：

- \(p_k\)：第 \(k\) 步结束后已确定的根（高位部分），共 \(k\) 位；
- \(R_k\)：第 \(k\) 步的 **部分余数**，满足不变式

\[
\text{(已处理的 } 2k \text{ 个高位比特)} \;=\; p_k^{\,2} \;+\; R_k
\]

即「处理过的那部分被开方数 = 已得根的平方 + 余数」。

每一步把被开方数的下 2 个比特 \(b\)「拉下来」（相当于把已处理部分左移 2 位再补 \(b\)），新的部分余数变为：

\[
R'_k \;=\; 4\,R_k \;+\; b
\]

然后判断「下一个根比特能否取 1」。若取 1，新根 \(p_{k+1} = 2p_k + 1\)，要求新余数非负：

\[
R_{k+1} = R'_k - (4p_k + 1) \;\ge\; 0 \;\;?
\]

- **若成立**：这一位根 = 1，\(p_{k+1} = 2p_k + 1\)，\(R_{k+1} = R'_k - (4p_k+1)\)；
- **若不成立**：这一位根 = 0，\(p_{k+1} = 2p_k\)，\(R_{k+1} = R'_k\)（余数不变）。

式子里的 \((4p_k+1)\) 正是判别核心。用伪代码概括：

```
R = 0; p = 0
for k = 0 .. N/2-1:
    b = 取出下 2 个高位比特
    R = 4*R + b            # 拉下 2 比特（左移）
    if R >= 4*p + 1:       # 能减吗？
        R = R - (4*p + 1)  # 够减：本位根 = 1
        p = 2*p + 1
    else:                  # 不够减：本位根 = 0
        p = 2*p            # 余数 R 不变
return p, R                # 根与最终余数
```

**关键观察**：判别式里出现的是 \(4p_k+1\)，而 \(4p_k\) 就是把 \(p_k\) 左移 2 位——这正是硬件里 `{q, 2'b01}` 的来历（见 4.2.3）。整个递推只用「移位、减法、比较符号位」，所以能做成面积很小的迭代数据通路。

#### 4.1.3 一个完整的手算追踪（rad = 81）

被开方数 81 = `0101_0001`，从最高位每 2 位一组：`01`、`01`、`00`、`01`。逐位求根：

| 步 | 拉下的 2 比特 \(b\) | 拉下后余数 \(R'=4R_{prev}+b\) | 判别 \(4p+1\) | 够减？ | 本位根 | 新根 \(p\) | 新余数 \(R\) |
|----|------|------|------|------|------|------|------|
| 0 | `01` (1)  | 1  | 4·0+1 = 1   | 1 ≥ 1 是 | 1 | 1  | 0 |
| 1 | `01` (1)  | 1  | 4·1+1 = 5   | 1 ≥ 5 否 | 0 | 2  | 1 |
| 2 | `00` (0)  | 4  | 4·2+1 = 9   | 4 ≥ 9 否 | 0 | 4  | 4 |
| 3 | `01` (1)  | 17 | 4·4+1 = 17  | 17 ≥ 17 是| 1 | 9  | 0 |

结果：根 \(p = 9\)，余数 \(R = 0\)，即 \(\sqrt{81} = 9\) 且开尽。验证 \(9^2 = 81\) ✓。这正是 4.2 实践中 testbench 的一个激励（`rad = 8'b01010001`），也是硬件每一拍要做的事。

#### 4.1.4 代码实践

**目标**：用上面递推关系，手算 \(\sqrt{90}\) 并判断余数，为读硬件实现建立直觉。

**操作步骤**：

1. 90 = `0101_1010`，2 位一组：`01`、`01`、`10`、`10`。
2. 按 4.1.3 的表格逐行填出 \(R'\)、判别 \(4p+1\)、本位根、新 \(p\)、新 \(R\)。
3. 收敛后写出根与余数。

**需要观察的现象**：根应当落在 9 与 10 之间（整数开方取下整），余数恰好反映 \(90 - 9^2\)。

**预期结果**：\(\sqrt{90}\) 整数根 = 9，余数 = \(90 - 81 = 9\)。（待本地验证：可以用 Python `isqrt(90)` 得 9 对照。）

#### 4.1.5 小练习与答案

**练习 1**：为什么逐位开方每步处理 2 个被开方数比特、却只产生 1 个根比特？  
**答**：因为根的最大位宽是被开方数位宽的一半（\(\sqrt{2^N} = 2^{N/2}\)）。2 比特输入恰好对应 1 比特输出，保持数据流平衡。

**练习 2**：把判别条件 \(R' \geq 4p+1\) 改写成「移位 + 减法 + 看符号位」的形式。  
**答**：计算 `test = R' − {p, 2'b01}`（即 \(R' − (4p+1)\)），看 `test` 的符号位（最高位）：为 0 表示非负、够减、本位根取 1；为 1 表示负、不够减、本位根取 0。这正是 4.2.3 硬件里的 `test_res` 判别。

**练习 3**：这个算法最坏要做多少拍才能算完一个 `WIDTH` 位整数？  
**答**：`WIDTH/2` 拍（`ITER = WIDTH>>1`）。因为根最多 `WIDTH/2` 位，每拍定 1 位。

---

### 4.2 sqrt_int：整数平方根的硬件实现

#### 4.2.1 概念说明

[sqrt_int.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv) 把 4.1 的递推关系落地为 Verilog。它只接受 **无符号整数** 被开方数，输出整数根与余数。模块接口遵循 projf maths 库统一的「启动-握手」范式：`start` 拉高启动，`busy` 表示计算中，`valid` 拉高表示 `root`/`rem` 可用——和 u7-l2 的除法器完全同构，方便上层用同一套时序逻辑串起来。

#### 4.2.2 核心流程

模块用 **组合递推 + 时序寄存存结果** 的双 `always` 写法（SystemVerilog 风格，见 [u5-l1](u5-l1-verilog-library-overview.md)）：

1. `start` 一来：把被开方数装入移位寄存器 `{ac, x}`（`ac` 是累加器/余数，`x` 是被开方数副本），清零根 `q` 与计数器 `i`，拉高 `busy`。
2. 每个时钟沿（`busy` 期间）：组合逻辑算出「试探减法」`test_res = ac − {q, 2'b01}`；
   - 非负 → 这一位根 = 1，`q` 左移补 1，`ac` 取减后的余数；
   - 为负 → 这一位根 = 0，`q` 左移补 0，`ac` 不变；
   - 无论哪种，`{ac, x}` 整体左移 2 位，把 `x` 的下 2 个比特「挤」进 `ac` 工作区（对应 4.1 的「拉下 2 比特」）。
3. 当 `i == ITER-1`（迭代到位）：`busy` 拉低、`valid` 拉高，锁存最终 `root = q_next`、`rem = ac_next[WIDTH+1:2]`（末次左移要除以 4 还原）。

关键点：`ac`（累加器）比被开方数 **宽 2 位**，既为了容纳符号判别，也为了容纳「拉下 2 比特」时不溢出。

#### 4.2.3 源码精读

模块参数与端口——`WIDTH` 默认 8，输入 `rad`、输出 `root`/`rem` 同宽：

[sqrt_int.sv:8-16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L8-L16) —— 声明 `WIDTH` 位被开方数与同宽的根/余数，以及 `start/busy/valid` 握手。

核心数据通路寄存器与迭代次数：

[sqrt_int.sv:18-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L18-L24) —— `ac` 宽 `WIDTH+2` 位（注释明确 "2 bits wider"），`ITER = WIDTH>>1`（根的位数），计数器 `i` 用 `$clog2(ITER)` 自动算位宽。

试探减法与根位决策（组合逻辑）：

```verilog
test_res = ac - {q, 2'b01};                 // 4.1 里的 R' − (4p+1)
if (test_res[WIDTH+1] == 0) begin           // 看最高位(符号位)：0 即非负
    {ac_next, x_next} = {test_res[WIDTH-1:0], x, 2'b0};  // 够减：余数取 test_res
    q_next = {q[WIDTH-2:0], 1'b1};          // 本位根 = 1（左移补 1）
end else begin
    {ac_next, x_next} = {ac[WIDTH-1:0], x, 2'b0};        // 不够减：余数不变
    q_next = q << 1;                         // 本位根 = 0（左移补 0）
end
```

这正是 4.1.5 练习 2 的答案。完整片段见 [sqrt_int.sv:26-35](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L26-L35)。

注意 `{ac_next, x_next} = {..., x, 2'b0}` 这一句同时做了两件事：把 `x` 左移 2 位（`x_next = x<<2`，吐出最高 2 比特），并把吐出的 2 比特拼到 `ac_next` 末尾——即硬件版的「拉下下 2 个比特到余数工作区」。

时序控制（启动、迭代、收尾）：

[sqrt_int.sv:37-57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt_int.sv#L37-L57) —— `start` 时初始化 `{ac,x} <= {0, rad, 2'b0}`；`busy` 中每拍推进 `i`、更新 `x/ac/q`；到 `i==ITER-1` 锁存 `root=q_next`、`rem=ac_next[WIDTH+1:2]`（末次整体左移了 2 位，取 `[WIDTH+1:2]` 相当于除以 4 还原真实余数）。

#### 4.2.4 代码实践

**目标**：用 Icarus Verilog 跑现成 testbench，验证整数平方根功能（本讲主线实践之一）。

**操作步骤**：

```bash
cd ThreePart/projf-explore/lib/maths
# -g2012 开启 SystemVerilog 子集（logic/always_comb/$clog2 需要）
iverilog -g2012 -o sqrt_int_sim.vvp xc7/sqrt_int_tb.sv sqrt_int.sv
vvp sqrt_int_sim.vvp
```

`xc7/sqrt_int_tb.sv` 的激励来自 [sqrt_int_tb.sv:29-57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_int_tb.sv#L29-L57)，依次送入 0、1、121、81、90、255。

**需要观察的现象**：`$monitor` 打印的 `sqrt(rad) = root (rem=...) (V=...)`。每个 `start` 脉冲后约 `ITER+1` 拍 `valid` 变 1、`root/rem` 更新。

**预期结果**（WIDTH=8，待本地验证）：

| rad | root | rem |
|-----|------|-----|
| 0   | 0  | 0 |
| 1   | 1  | 0 |
| 121 | 11 | 0 |
| 81  | 9  | 0 |
| 90  | 9  | 9 |
| 255 | 15 | 30 |

若你用的是 Vivado，可用 `xc7/vivado/` 下的波形配置查看 `ac/q/x` 每拍的变化。

#### 4.2.5 小练习与答案

**练习 1**：把 `WIDTH` 改成 16 后，`ITER` 与计算延迟如何变化？  
**答**：`ITER = 16>>1 = 8`，需要 8 个时钟拍（加上启动/收尾各约 1 拍）才能出结果。位宽翻倍，延迟也大致翻倍——这是迭代算法的固有代价。

**练习 2**：为什么 `ac` 要比 `rad` 宽 2 位（`[WIDTH+1:0]`）？  
**答**：一是 `test_res = ac − {q,2'b01}` 需要一个符号位来判正负；二是「拉下 2 比特」时 `ac` 要在原有基础上再并入 `x` 吐出的 2 位而不丢失最高位，故预留 2 位余量。

**练习 3**：`rem <= ac_next[WIDTH+1:2]` 为什么要取 `[WIDTH+1:2]` 而不是整个 `ac_next`？  
**答**：收尾这一拍 `{ac,x}` 整体多左移了 2 位（即乘了 4），余数被放大 4 倍。取 `[WIDTH+1:2]`（丢掉最低 2 位）正好抵消这次移位，得到真实余数。

---

### 4.3 sqrt：从整数到定点平方根

#### 4.3.1 概念说明

[sqrt.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv) 几乎与 `sqrt_int` 逐字相同，只多了 `FBITS` 参数，支持 **定点小数** 平方根。这是本讲最巧妙的一处：**它没有改算法，只改了迭代次数 `ITER`，就让整数开方升级为定点开方**。

原理：被开方数 `rad` 是 Q(WIDTH).(FBITS) 格式，代表值 \(x = \text{rad} / 2^{FBITS}\)。我们想要的根也是 Q(WIDTH).(FBITS)，即 \(r = \text{root}/2^{FBITS}\) 满足 \(r^2 \approx x\)。等价地：

\[
\text{root}^{\,2} \;\approx\; \text{rad} \times 2^{FBITS}
\]

也就是说，定点开方 = 对「`rad` 后面追加 `FBITS` 个 0」的整数开方。追加 `FBITS` 个 0 相当于把被开方数当成 `WIDTH + FBITS` 位的整数来处理，于是根有 \((WIDTH+FBITS)/2\) 位，这正是 `ITER = (WIDTH+FBITS)>>1` 的来历。

#### 4.3.2 核心流程

数据通路、试探减法、握手时序与 `sqrt_int` **完全一致**（参见 4.2.2），唯一区别在初始化与迭代计数：

- 端口多了 `parameter FBITS`（默认 0，即退化为整数版）；
- [sqrt.sv:26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L26) —— `ITER = (WIDTH+FBITS) >> 1`，比整数版多 `FBITS/2` 拍。

那「追加 FBITS 个 0」在硬件里怎么体现？看初始化：`{ac, x} <= {{WIDTH{1'b0}}, rad, 2'b0};`——`rad` 装入 `x` 后，每拍左移 2 位。整数版做到 `WIDTH/2` 拍时 `x` 已被移空、根算完；定点版继续多做 `FBITS/2` 拍，此时 `x` 吐出的全是 0（等效于给 `rad` 追加 0），结果根的低 `FBITS` 位自然就是小数部分。

#### 4.3.3 源码精读

定点版端口与迭代次数：

[sqrt.sv:8-19](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L8-L19) —— 比 `sqrt_int` 多一个 `FBITS` 参数。

关键差异行：

```verilog
localparam ITER = (WIDTH+FBITS) >> 1;  // sqrt_int 里是 WIDTH>>1
```

[sqrt.sv:26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L26) —— 唯一的算法性差异。其余 [组合递推](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L29-L38) 与 [时序控制](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sqrt.sv#L40-L60) 与 `sqrt_int` 逐字相同。

工程美感：**用「迭代次数」这一单一参数，把整数开方推广到任意 Q 格式定点开方**，而数据通路硬件零改动。这是参数化设计的典范。

#### 4.3.4 代码实践

**目标**：跑定点版 testbench，体会 Q8.8 开方的数值含义。

**操作步骤**：

```bash
cd ThreePart/projf-explore/lib/maths
iverilog -g2012 -o sqrt_sim.vvp xc7/sqrt_tb.sv sqrt.sv
vvp sqrt_sim.vvp
```

testbench 配置见 [sqrt_tb.sv:10-13](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_tb.sv#L10-L13)：`WIDTH=16, FBITS=8`，缩放因子 `SF = 2^-8`，即 Q8.8（8 位整数 + 8 位小数）。激励见 [sqrt_tb.sv:32-46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/xc7/sqrt_tb.sv#L32-L46)。

**需要观察的现象**：`$monitor` 用 `$itor(rad*SF)` 把定点数还原成浮点打印，你能直接读到 `sqrt(232.5625) ≈ 15.25`、`sqrt(0.25) = 0.5`、`sqrt(2.0) ≈ 1.414`（受 Q8.8 精度限制）这类结果。

**预期结果**（待本地验证）：

| rad（Q8.8） | 实数值 | root 实数值 |
|-------------|--------|-------------|
| `1110_1000_1001_0000` | 232.5625 | ≈15.25 |
| `0000_0000_0100_0000` | 0.25     | 0.5 |
| `0000_0010_0000_0000` | 2.0      | ≈1.414（Q8.8 下的近似） |

注意延迟比整数版长：`ITER = (16+8)/2 = 12` 拍。

#### 4.3.5 小练习与答案

**练习 1**：为什么定点版 `ITER = (WIDTH+FBITS)/2` 而不是 `WIDTH/2`？  
**答**：定点开方等价于对 `rad·2^FBITS` 做整数开方，被开方数有效位变成 `WIDTH+FBITS`，根位数随之变成 `(WIDTH+FBITS)/2`，故需相应迭代次数。

**练习 2**：`FBITS=0` 时 `sqrt.sv` 和 `sqrt_int.sv` 行为是否一致？  
**答**：是的。`FBITS=0` 时 `ITER = WIDTH>>1`，与整数版完全相同；`sqrt.sv` 可看作 `sqrt_int` 的超集。

**练习 3**：Q8.8 下 \(\sqrt{2}\) 为什么得不到精确值？  
**答**：\(\sqrt{2}\) 是无理数，而 Q8.8 只能表示 \(1/256\) 的整数倍，结果必然是量化近似（约 1.4140625）。这是定点数固有的精度限制，与算法无关。

---

### 4.4 mul：有符号定点乘法与 DSP48 映射

#### 4.4.1 概念说明

[mul.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv) 做 **有符号定点乘法**，带高斯舍入与溢出检测。它与开方/除法形成鲜明对比——**乘法本身在硬件里只有一行 `prod <= a1 * b1`**，因为 Xilinx FPGA 的 DSP48E1 切片内建 25×18 位有符号乘法器，综合工具会把这一行直接推断成一个 DSP48，而不是用 LUT 拼出一个乘法器。

那为什么 mul.sv 还要写一个 4 状态机（IDLE/CALC/TRUNC/ROUND），而不是直接 `assign val = a*b`？因为它要在「裸乘法」之外补齐三件定点运算必须做的事：

1. **定点缩放**：乘积小数位翻倍，要截回原 Q 格式；
2. **高斯舍入**：截掉的低位不是简单丢弃，而是 round half to even；
3. **溢出检测**：两个 Q 格式数相乘可能超出表示范围，要报告 `ovf`。

#### 4.4.2 核心流程

状态机 `enum {IDLE, CALC, TRUNC, ROUND}`（见 [mul.sv:41](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L41)）：

1. **IDLE**：等 `start`。到来时寄存 `a1<=a, b1<=b`，记录符号是否相反 `sig_diff`，进入 CALC。
2. **CALC**（1 拍）：`prod <= a1 * b1` —— 这一行被推断为 DSP48，得到 `2*WIDTH` 位全精度有符号乘积。进入 TRUNC。
3. **TRUNC**（1 拍）：从全精度乘积里截取 WIDTH 位窗口 `prod[MSB:LSB]`（保 `IBITS` 位整数 + `FBITS` 位小数），同时抽出舍入位 `rbits`、舍入判定 `round`、偶数判定 `even`。进入 ROUND。
4. **ROUND**（1 拍）：按高斯舍入修正 `val`，做溢出检测，拉高 `done`（单拍）与 `valid`/`ovf`，回 IDLE。

整个乘法 **算 3 拍出结果**（不计 IDLE），其中真正的乘法只占 1 拍（DSP 单拍），其余 2 拍是定点善后。对比 u7-l2 的除法器 CALC 状态要循环 `ITER` 次——这就是「有 DSP 硬核」与「无硬核靠迭代」的本质差别。

#### 4.4.3 源码精读

参数与窗口计算：

[mul.sv:25-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L25-L30) —— 关键三式：

```verilog
localparam IBITS = WIDTH - FBITS;        // 输入整数位(含符号)
localparam MSB   = 2*WIDTH - IBITS - 1;  // 截取窗口上界 = WIDTH+FBITS-1
localparam LSB   = WIDTH - IBITS;        // 截取窗口下界 = FBITS
localparam HALF  = {1'b1, {FBITS-1{1'b0}}};  // 0.5 ULP 判别值 = 2^(FBITS-1)
```

`prod[MSB:LSB]` = `prod[WIDTH+FBITS-1 : FBITS]`，正好 WIDTH 位，保留乘积顶部的 `IBITS` 个整数位 + `FBITS` 个小数位，丢弃最低 `FBITS` 位（成为舍入依据）。

真正的乘法（映射 DSP48 的那一行）：

[mul.sv:45-48](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L45-L48) —— `prod <= a1 * b1;`。两个 `signed [WIDTH-1:0]` 相乘得 `signed [2*WIDTH-1:0]`。`signed` 关键字是让综合器用 DSP 的 **二进制补码乘法器**；若漏写，工具可能推断无符号乘法或退化为 LUT 实现。

截取与舍入位抽取：

[mul.sv:49-56](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L49-L56) —— 取窗口 `prod_t`、被丢弃位 `rbits = prod[FBITS-1:0]`、舍入位 `round = prod[FBITS-1]`（0.5 那一位）、偶数判定 `even = ~prod[FBITS]`（结果最低位为 0 即偶数）。

高斯舍入决策与溢出检测：

```verilog
// 高斯舍入：round 到最近偶数
val <= (round && !(even && rbits == HALF)) ? prod_t + 1 : prod_t;

// 溢出：结果符号与输入符号一致，且截掉的高位是合法符号扩展(全0或全1)
if (sig_diff == prod_t[WIDTH-1+:1] &&
    (prod[2*WIDTH-1:MSB+1] == '0 || prod[2*WIDTH-1:MSB+1] == '1)) begin
    valid <= 1; ovf <= 0;
end else begin
    valid <= 0; ovf <= 1;
end
```

[mul.sv:57-75](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L57-L75) —— 舍入逻辑：只有当被丢弃部分 > 0.5（`round` 位为 1 且不是恰好 0.5），或 =0.5 但结果为奇数时才进位；恰好 0.5 且结果为偶数则不进位（向偶舍入）。溢出检测要求截掉的高位要么全 0、要么全 1（合法的符号扩展），否则说明整数位不够放、溢出。

**一个高斯舍入实例**（WIDTH=8, FBITS=4，Q4.4）：取 `a = 0.125`（=2）、`b = 0.25`（=4）。乘积 `prod = 8 = 0b...1000`。窗口 `prod[11:4] = 0`，`rbits = prod[3:0] = 1000 = 8 = HALF`，`round = 1`，`even = ~prod[4] = 1`。决策 `round && !(even && rbits==HALF)` = `1 && !(1&&1)` = `0`，故 `val = prod_t = 0`。即 \(0.125 \times 0.25 = 0.03125\) 恰好在 0 与 0.0625 的正中间，按「向偶舍入」取 0（0 是偶数）。这就是高斯舍入区别于「四舍五入」的地方。

#### 4.4.4 代码实践

**目标 1（功能）**：跑现成 cocotb 测试验证 mul 行为；**目标 2（综合）**：用综合报告确认 `*` 被映射到 DSP48 而非 LUT（本讲主线实践之二）。

**操作步骤（功能测试）**：

```bash
cd ThreePart/projf-explore/lib/maths/test
# 需要 Python + cocotb + Icarus Verilog；mul.mk 把参数设为 WIDTH=9 FBITS=4
make -f mul.mk
```

[mul.mk](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.mk) 用 `COMPILE_ARGS += -Pmul.WIDTH=9 -Pmul.FBITS=4` 给 Icarus 传参，对应 Python 激励在 [mul.py](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/test/mul.py)（用 spfpm 做定点黄金参考）。

**需要观察的现象（功能）**：测试报告里每个用例的硬件 `val` 与 Python 定点参考值一致，包括需要进位和恰好 0.5 的边界用例。

**操作步骤（综合验证 DSP）**：

1. 在 Vivado 里新建工程，加入 `lib/maths/mul.sv`，器件选一个 7 系列（如 xc7a100t）。
2. 设为 top（或包一层 top 把端口引出），跑综合（Synthesis）。
3. 打开 **Utilization Report → DSPs**，或在 Tcl 控制台 `report_utilization`。

**需要观察的现象（综合）**：

- **预期**：1 个 `DSP48E1` 被使用（对于 WIDTH ≤ 18 的情况），LUT 几乎不增加。
- **对照实验**：把 `mul.sv` 里的 `prod <= a1 * b1;` 临时改成 `prod <= (a1 + b1);`（加法）再综合，DSP 用量应降为 0、LUT 增加——以此确认是乘法 `*` 触发了 DSP 推断。
- 也可以在 Tcl 里 `set_property HDL_ATTRIBUTE` 或看综合日志里 "multiplier" 相关的推断信息。

**预期结果**：综合报告显示使用了 1 个 DSP48（待本地验证；具体数量随 WIDTH 而变，WIDTH>18 会级联多个 DSP）。这一步证明：**在 RTL 里写 `signed` 乘法，工具自动推断 DSP 硬核**——这正是 mul 只需 1 拍做乘法的物理基础。

> ⚠️ 注意：修改源码做对照实验后请还原，本讲义不允许把改动留在仓库里。

#### 4.4.5 小练习与答案

**练习 1**：为什么 mul.sv 的 CALC 状态只停 1 拍，而 u7-l2 除法器的 CALC 要循环很多次？  
**答**：乘法有 DSP48 硬核，单周期给出全精度结果；除法/开方没有硬核，只能移位-比较一拍出一位，必须多周期迭代。这是「有/无专用硬核」的根本差异。

**练习 2**：把 `a1 * b1` 的 `signed` 去掉会怎样？  
**答**：`signed` 决定了综合器使用补码乘法器（DSP48 的有符号模式）并保证负数语义正确。去掉后端口可能被当无符号处理，负数乘法结果错误，且可能影响 DSP 推断方式。projf 库统一标 `signed` 正是为了避免此坑（见 u7-l1）。

**练习 3**：截取窗口 `prod[MSB:LSB]` 里，`MSB` 与 `LSB` 各由谁决定？为什么这样取？  
**答**：`LSB = FBITS`（丢掉翻倍后多余的小数位），`MSB = WIDTH+FBITS-1`（保证窗口宽 WIDTH 位，且顶部保留 `IBITS` 个整数位含符号）。这样乘积被截回与输入相同的 Q(WIDTH).(FBITS) 格式。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「定点向量运算」小调研：

1. **开方链**：选定 `WIDTH=16, FBITS=8`（Q8.8）。输入 `rad = 4.0`（=`0x0400`）。先 **手算** 期望根（=2.0=`0x0200`），再用 `sqrt.sv` 仿真验证（参考 4.3.4 命令），比对 `root` 与手算值，记录余数。
2. **乘法链**（同样用 Q8.8）：取 `a = 1.5`（=`0x0180`=384）、`b = 2.0`（=`0x0200`=512），手算期望积（=3.0=`0x0300`=768）；再用自写 testbench（实例化 `mul #(.WIDTH(16), .FBITS(8))`）验证 `val`、确认无溢出（`ovf=0`）。注意 4.4.4 的 cocotb 测试用的是 Q5.4（WIDTH=9,FBITS=4），与此处的 Q8.8 不同，别混用参数。
3. **对比延迟**：在同一时钟下，记录 `sqrt` 需要多少拍（`ITER=12`）、`mul` 需要多少拍（IDLE→CALC→TRUNC→ROUND，约 3 拍）。把两者延迟比填入一张表，体会「迭代算法 vs DSP 单拍」的成本差。
4. **（选做）综合对比**：把 `sqrt.sv`、`mul.sv` 分别综合，对比 Utilization Report：`mul` 占 1 个 DSP48、几乎零 LUT；`sqrt` 占若干 LUT/FF、零 DSP。用数据印证「乘法吃 DSP、开方吃逻辑资源」。

**交付**：一张表，含 `rad/ab`、期望值、仿真实测值、延迟（拍数）、资源类型（DSP/LUT）。

## 6. 本讲小结

- **逐位求平方根**用「每步拉下 2 比特、判别 \(R' \geq 4p+1\)、定 1 位根」的递推，只需移位/减法/比较、不用乘法器；一个 `WIDTH` 位整数需 `WIDTH/2` 拍。
- **sqrt_int.sv** 用 `{ac, x}` 联合移位寄存器 + 试探减法 `test_res = ac − {q,2'b01}` 落地该算法；`ac` 宽 2 位以容纳符号判别与移位。
- **sqrt.sv** 与整数版硬件零改动，只把 `ITER` 从 `WIDTH/2` 改成 `(WIDTH+FBITS)/2`，靠「多做 `FBITS/2` 拍、移入 0」实现定点开方——参数化设计的典范。
- **mul.sv** 的核心乘法只有一行 `prod <= a1 * b1`，靠 `signed` 让工具推断 **DSP48 硬核**，单拍出全精度乘积；状态机的其余状态用于定点缩放、高斯舍入与溢出检测。
- **乘法 vs 开方/除法**的本质：前者有 DSP 硬核（1 拍、占 DSP 资源），后者无硬核靠迭代（多拍、占 LUT/FF）——这是 FPGA 数学运算最核心的资源-延迟权衡直觉。
- **高斯舍入**（round half to even）在 mul 与 u7-l2 除法器中一致使用，消除「四舍五入」的统计偏差。

## 7. 下一步学习建议

- **横向扩展 maths 库**：本讲只读了 sqrt/mul，建议接着读 [lfsr.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/lfsr.sv)（伪随机）与 [sine_table.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/sine_table.sv)（ROM 查表），对应大纲 u7-l4，把 maths 分区读完。
- **向上承接 demo**：u7-l5 的 Mandelbrot 集会大量用到本讲的定点乘法（复数 \(z^2\)）与 u7-l1 的定点表示，是用真实图形场景检验本讲知识的最佳去处。
- **深入 DSP48**：若对 mul 的 DSP 映射感兴趣，可读 Xilinx UG479（7 系列 DSP48E1 Slice）。理解了预加器、累加器、级联，就能解释为什么更大的乘法会自动级联多个 DSP。
- **对比除法**：回头重读 u7-l2 的 [div.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv)，把它与本讲的 mul/sqrt 放在一起，你会清楚地看到「迭代（div/sqrt）vs 单拍（mul）」这条主线如何贯穿整个 maths 分区。
