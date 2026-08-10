# 定点数运算与钳位函数

## 1. 本讲目标

本讲解决一个核心问题：**FPGA 上没有浮点单元，那项目怎么表示「乘以 0.5」「除以 2」这样的实数运算？**

答案是**定点数**：用整数来「假装」实数，把小数点固定在某个比特位上。学完本讲你应该能够：

1. 说清项目使用的 **1.3.4 定点格式**（1 位符号 + 3 位整数 + 4 位小数）的位权、范围与精度。
2. 区分两个钳位函数 **`apply_clamp`** 与 **`apply_clamp_fixed16`** 的差别，尤其是后者为何要取 `in[11:4]`。
3. 手算主机侧 `send_mult(float)` 如何把一个 `float` 量化成一个 8 位定点字节，并能反推它在硬件里乘了像素多少倍。

本讲承接 [u4-l1 逐像素运算：STATE_PROC_UNARY](u4-l1-unary-operations.md)。u4-l1 已经指出：乘法运算 `COMMAND_APPLY_MULT` 与加法不同，它用的是 `apply_clamp_fixed16` 而不是 `apply_clamp`，「细节留待 u4-l2」。本讲就来填上这个坑。

## 2. 前置知识

### 2.1 为什么 FPGA 不用浮点

CPU/GPU 里有专门的浮点单元（FPU）来处理 `float`/`double`。但在本项目面向的 iCE40 UltraPlus 这种低端 FPGA 上，做一次浮点乘法要消耗大量逻辑单元、还要很多个时钟周期，极其不划算。而图像处理的乘法（亮度调整、卷积）精度要求并不高——8 位像素（0~255）配一个 4 位小数已经完全够用。所以项目选择用**定点数**：用普通整数运算电路（本项目本来就有 8×8→16 位的整数乘法器）来近似实数运算。

### 2.2 定点数的基本思想

定点数 = **把实数放大成一个整数来存，运算完再缩回去**。

例如想表示 0.5，我们约定「小数点后有 4 位」，那么就把它放大 \(2^4 = 16\) 倍来存：

\[ 0.5 \times 16 = 8 \quad\Rightarrow\quad \text{存整数 } 8 \]

运算时，乘法会让放大倍数叠加（两个都放大 16 倍的数相乘，结果放大了 \(16\times16=256\) 倍），所以**运算后要把多余的放大倍数除掉**，本讲要讲的 `in[11:4]` 就是干这件事的。

> 关键直觉：**定点数的「小数点位置」是一个团队约定**，电路本身并不知道哪里是小数点，全靠写代码的人在「该除的地方除、该乘的地方乘」。本讲就是把这个约定讲清楚。

### 2.3 位运算复习

- `in >> 4`：右移 4 位 = 除以 16（向下取整）。
- `in[11:4]`：取出从第 4 位到第 11 位的 8 个比特 = `(in >> 4) & 0xFF`。
- `$signed(x)`：把无符号数当有符号数解释（看最高位是符号位）。

## 3. 本讲源码地图

本讲涉及的关键文件只有两个，外加一个用来跑实验的主机函数：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 核心 HDL 模块 | 两个钳位函数 `apply_clamp` / `apply_clamp_fixed16`、乘法运算分支、`mult_value_param` 寄存器 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 仿真后端 | `send_mult(float, clamp)` 的 float→定点量化循环 |
| software/main.cpp | 主机入口 | `test_multiplication()` 测试函数，作为可运行实践 |

> 注意：硬件后端 [ice40/software/image_processing_ice40.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp) 里的 `send_mult` 用**完全相同**的量化循环（见其 L113 附近的 `val_fixed_4_4`）。本讲只精读仿真后端，但结论对两套后端都成立。

---

## 4. 核心概念与源码讲解

### 4.1 8 位定点格式（1.3.4）

#### 4.1.1 概念说明

项目用一个 8 位字节来存一个实数，约定这 8 个比特的分工是 **1.3.4**：

| 比特位 | bit 7 | bit 6 : bit 4 | bit 3 : bit 0 |
| --- | --- | --- | --- |
| 角色 | 符号位 | 整数部分（3 位） | 小数部分（4 位） |
| 权重 | （符号） | \(2^2, 2^1, 2^0 = 4,2,1\) | \(2^{-1}\dots 2^{-4} = 0.5, 0.25, 0.125, 0.0625\) |

也就是说，**第 i 位（\(i=0\dots6\)）的权重是 \(2^{i-4}\)**：

\[ \text{实数值} = \sum_{i=0}^{6} b_i \cdot 2^{i-4} \]

等价地，把字节当成无符号整数读出来再除以 16：

\[ V_{\text{real}} = \frac{\text{字节整数值}}{16} \]

- **精度**：最小步长是 \(2^{-4} = 1/16 = 0.0625\)。
- **范围**：整数部分 3 位，所以 \(|整数| \le 7\)，可表示范围约 **\(-7.0 \sim +7.0\)**（精确地说，作为有符号 1.3.4 补码可达 \(-8.0 \sim +7.9375\)）。

#### 4.1.2 核心流程

定点数在「主机 → 硬件」之间的处理流程：

1. **主机**拿到一个 `float`（比如 0.5），把它量化成一个 8 位定点字节（比如 8）。
2. 主机把这个字节当作普通参数发给硬件（命令 `COMMAND_APPLY_MULT` 的参数之一）。
3. **硬件**把这个字节存进寄存器 `mult_value_param`。
4. 硬件对每个像素做**整数乘法** `mult_value_param * pixel`，得到一个 16 位结果（注意此时结果被「放大了 16 倍」）。
5. 硬件用 `apply_clamp_fixed16` 把多余的 16 倍除掉、并钳位到 0~255，写回存储。

> 重点：**步骤 1（主机放大）和步骤 5（硬件缩小）必须配对**。主机放大几倍（这里是 16），硬件就得缩小几倍。本讲的难点就在于看清这对「放大—缩小」是怎么靠 `in[11:4]` 实现的。

#### 4.1.3 源码精读

`mult_value_param` 是硬件里存放定点乘数的寄存器，宽度正好 8 位：

[hdl/image_processing.v:148](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L148) —— 声明 8 位寄存器 `mult_value_param`，用来缓存主机发来的定点乘数字节。

命令派发时，`COMMAND_APPLY_MULT` 预告要读 2 个参数字节（`counter_read <= 1` 的含义见 u3-l4：第一个参数在 `counter_read==1` 读，第二个在 `counter_read==0` 读）：

[hdl/image_processing.v:278-281](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L278-L281) —— `COMMAND_APPLY_MULT` 分支：跳到读参数状态，并设 `counter_read <= 1`（即准备读 2 个字节：定点乘数 + clamp 标志）。

[hdl/image_processing.v:486-L498](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L486-L498) —— `STATE_APPLY_MULT_READ_PARAM`：第 1 拍把字节存进 `mult_value_param`，第 2 拍取 `comm_data_in[0]` 作为 `clamp`，并把运算交给 `STATE_PROC_UNARY`（`processing_command <= COMMAND_APPLY_MULT`）。

> 注意：`mult_value_param` 被当成**无符号**用（后面乘法里是 `{8'b0, mult_value_param}` 零扩展），所以符号位 bit7 实际未被乘法使用——这与主机侧 `send_mult` 把负数强制清零的做法是对应的（见 4.4）。

#### 4.1.4 代码实践

**实践目标**：亲手把几个实数按 1.3.4 格式量化，建立对位权的直觉。

**操作步骤**：用公式 \(V_{\text{real}} = \text{整数}/16\) 填下表。

| 实数 \(V\) | 定点字节（\(V \times 16\)） | 二进制 | bit7 | bit6:4 (整数) | bit3:0 (小数) |
| --- | --- | --- | --- | --- | --- |
| 0.5 | 8 | `0000_1000` | 0 | `001`(=1? 待你判断) | `1000` |
| 1.0 | ? | ? | ? | ? | ? |
| 2.0 | ? | ? | ? | ? | ? |
| 0.25 | ? | ? | ? | ? | ? |

**需要观察的现象 / 预期结果**：

- 0.5 → 8：bit3=1（权重 \(2^{3-4}=2^{-1}=0.5\)），其余为 0。✓（注意上表里整数部分 `001` 其实是错的——0.5 的整数部分是 0，bit3 属于小数部分。请你订正它，这正是练习的目的）。
- 1.0 → 16 = `0001_0000`：bit4=1（权重 \(2^0=1\)）。
- 2.0 → 32 = `0010_0000`：bit5=1（权重 \(2^1=2\)）。
- 0.25 → 4 = `0000_0100`：bit2=1（权重 \(2^{-2}=0.25\)）。

> 待本地验证：你可以在一张纸上算，也可以写个小 C 程序 `printf("%d\n", (int)(0.5f*16));` 对照。

#### 4.1.5 小练习与答案

**练习 1**：1.3.4 格式能表示的最大正实数是多少？最小正步长是多少？

**答案**：最大正实数 = 整数部分全 1（=7）+ 小数部分全 1（=\(15/16=0.9375\)）= **7.9375**；最小正步长 = **0.0625**（即 1/16）。

**练习 2**：为什么项目选 4 位小数，而不是 8 位？

**答案**：因为像素只有 8 位（0~255），乘数的小数精度高过 \(1/16\) 对最终像素值的四舍五入几乎没有影响；而且位数越多，乘法器越宽、越费逻辑。4 位是「精度够用、电路够省」的折中。

---

### 4.2 `apply_clamp`：整数结果的钳位

#### 4.2.1 概念说明

`apply_clamp` 解决一个朴素问题：**运算结果可能超出 0~255 的像素范围，怎么办？**

比如加法 `100 + 200 = 300`，但一个像素最大只能存 255。项目采用**饱和（saturation）钳位**策略：

- 结果 > 255 → 截到 255。
- 结果 < 0 → 截到 0。
- 否则 → 取结果的低 8 位。

当 `clamp_en == 0`（不钳位）时，直接取低 8 位（相当于让结果自然回绕溢出）。

#### 4.2.2 核心流程

```
输入: in[15:0]（16 位运算结果）, clamp_en
if clamp_en 且 $signed(in) > 255 :  返回 255
if clamp_en 且 $signed(in) < 0   :  返回 0
否则                              :  返回 in[7:0]
```

注意它的输入是 16 位、输出是 8 位，**不做缩放**——因为加法、减法的结果本身就落在「像素尺度」上（两个 8 位数相加，结果最多到 9 位，符号看 `add_value`）。

#### 4.2.3 源码精读

[hdl/image_processing.v:150-L163](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L150-L163) —— `apply_clamp` 函数全文：

```verilog
function [7:0] apply_clamp;
input [15:0] in;
input clamp_en;
begin
   apply_clamp = in[7:0];                         // 默认取低 8 位
   if(clamp_en == 1 && $signed(in) > 255)         // 太大 → 255
      apply_clamp = 255;
   if(clamp_en == 1 && $signed(in) < 0)           // 太小 → 0
      apply_clamp = 0;
end
endfunction
```

它被 `COMMAND_APPLY_ADD`、`COMMAND_BINARY_ADD/SUB/MULT` 以及卷积的「结果叠加」步骤使用——这些都是**结果天然在像素尺度**的场景。

#### 4.2.4 代码实践

**实践目标**：通过阅读 u4-l1 讲过的加法分支，确认 `apply_clamp` 用在「不放大」的场景。

**操作步骤**：打开 [hdl/image_processing.v:516-L520](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L516-L520)，这是 `COMMAND_APPLY_ADD` 分支。

**需要观察的现象 / 预期结果**：

```verilog
temp_calc = {8'b0, data_read[7:0]} + add_value;   // 像素 + 加数
data_write[7:0] <= apply_clamp(temp_calc, clamp); // 直接钳位，不缩放
```

加数 `add_value` 是 16 位整数（可正可负），像素是 8 位，相加结果最多 16 位，**没有额外的放大倍数**，所以直接 `apply_clamp` 取低 8 位即可。

**预期结果**：你能解释为什么加法用 `apply_clamp` 而乘法要用 `apply_clamp_fixed16`——因为加法不放大、乘法放大了 16 倍。

#### 4.2.5 小练习与答案

**练习 1**：若 `in = 16'd300`、`clamp_en = 1`，`apply_clamp` 返回多少？若 `clamp_en = 0` 呢？

**答案**：`clamp_en=1` → 300 > 255 → 返回 **255**；`clamp_en=0` → 取 `in[7:0]` = 300 的低 8 位 = `300 = 0x12C` → `0x2C` = **44**（发生了溢出回绕）。

---

### 4.3 `apply_clamp_fixed16`：定点结果的缩放与钳位

#### 4.3.1 概念说明

`apply_clamp_fixed16` 是本讲的**核心**。它与 `apply_clamp` 长得几乎一样，唯一的差别是：

- 默认返回值从 `in[7:0]` 改成了 **`in[11:4]`**。
- 钳位判断从 `$signed(in)` 改成了 **`$signed(in[15:4])`**。

这两个改动都是为了处理**乘法产生的 16 位定点结果**。

回忆 4.1：乘法 `mult_value_param * pixel` 里，`mult_value_param` 是「被放大了 16 倍」的定点数，所以乘积被**额外放大了 16 倍**。要把结果还原成正常像素尺度，就要**除以 16**，也就是**右移 4 位**。`in[11:4]` 正是「右移 4 位后再取 8 位」。

#### 4.3.2 核心流程

设 `mult_value_param = 16 × V`（V 是真实乘数），像素为 P：

\[ \text{temp\_calc} = (16 V) \times P = 16 \cdot (V P) \]

这是 16 位整数（因为 \(16V \le 127\)、\(P \le 255\)，乘积 \(\le 32385\)，落在 16 位内）。要还原：

\[ \text{结果} = \frac{\text{temp\_calc}}{16} = V \cdot P \]

而 `in[11:4]`（取第 4~11 位）等价于 `(temp_calc >> 4) & 0xFF`，即除以 16 后取低 8 位：

\[ \text{in[11:4]} = \left\lfloor \frac{\text{temp\_calc}}{16} \right\rfloor = \lfloor V \cdot P \rfloor \]

> **这就是 `in[11:4]` 的全部含义**：它把被放大了 16 倍的乘积**右移 4 位除以 16**，还原成像素尺度的真实乘积。

至于钳位为何用 `in[15:4]`（12 位）而不是 `in[11:4]`（8 位）：因为 `in[11:4]` 只有 8 位，一旦 \(V \cdot P > 255\) 它就会**溢出截断**而看不出「超了」；所以判断饱和要用更宽的 `in[15:4]`（12 位有符号）来正确检测越界（\(>255\) 或 \(<0\)）。

#### 4.3.3 源码精读

[hdl/image_processing.v:165-L178](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L165-L178) —— `apply_clamp_fixed16` 函数全文：

```verilog
function [7:0] apply_clamp_fixed16;
input [15:0] in;
input clamp_en;
begin
   apply_clamp_fixed16 = in[11:4];                       // 关键：右移4位=除以16
   if(clamp_en == 1 && $signed(in[15:4]) > 255)          // 用12位检测上溢
      apply_clamp_fixed16 = 255;
   if(clamp_en == 1 && $signed(in[15:4]) < 0)            // 用12位检测下溢
      apply_clamp_fixed16 = 0;
end
endfunction
```

它的两个使用点在乘法运算分支里，对高、低两个像素分别处理（u4-l1 讲过「一个 16 位字装 2 个像素」）：

[hdl/image_processing.v:540-L545](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L540-L545) —— `COMMAND_APPLY_MULT` 分支：

```verilog
temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[7:0]};   // 低像素 × 定点乘数
data_write[7:0]  <= apply_clamp_fixed16(temp_calc, clamp);     // 除以16还原+钳位
temp_calc = {8'b0, mult_value_param}*{8'b0, data_read[15:8]};  // 高像素 × 定点乘数
data_write[15:8] <= apply_clamp_fixed16(temp_calc, clamp);
```

可以看到，**两个像素独立做乘法、独立调 `apply_clamp_fixed16`**。这就是定点乘法的全貌。

> 同一个函数在卷积里也用：[hdl/image_processing.v:723](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L723) 和 [hdl/image_processing.v:772](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L772)。卷积核也是定点数（1.3.4 有符号），9 次乘加后同样多了一层 16 倍放大，所以也靠 `apply_clamp_fixed16` 还原——这部分细节留到 u5 单元。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`in[11:4]` 等价于除以 16」这一核心论断。

**操作步骤**：设乘数 V = 0.5（则 `mult_value_param = 8`），像素 P = 200。

1. 算硬件乘积：`temp_calc = 8 × 200 = 1600`。
2. 把 1600 写成 16 位二进制：\(1600 = \text{0x0640} = \texttt{0000\_0110\_0100\_0000}\)。
3. 取 `temp_calc[11:4]`（即第 11 到第 4 位）：

| bit15..bit12 | **bit11..bit8** | **bit7..bit4** | bit3..bit0 |
| --- | --- | --- | --- |
| 0000 | 0110 | 0100 | 0000 |

`[11:4]` 把上表中间两段（bit11..8 和 bit7..4）拼起来 = `0110_0100` = **100**。

**需要观察的现象 / 预期结果**：

- `temp_calc[11:4] = 100`。
- 而直接算 \(1600 / 16 = 100\)。两者完全一致，证明 `in[11:4]` == 除以 16。
- 而 \(200 \times 0.5 = 100\)，正是「像素乘以 0.5」的预期结果。✓

**预期结果**：你能向自己解释清楚——「为什么取 8 位 `[11:4]` 而不是 `[7:0]`？因为乘积多带了 4 位小数放大，必须丢掉最低 4 位（右移 4）才能还原像素尺度。」

#### 4.3.5 小练习与答案

**练习 1**：为什么钳位判断用 `$signed(in[15:4])`（12 位）而不是 `in[11:4]`（8 位）？

**答案**：因为 `in[11:4]` 只有 8 位，若真实结果 \(V \cdot P\) 超过 255，`in[11:4]` 会回绕（比如 256 变成 0），从而「看不出」越界。用更宽的 12 位 `in[15:4]`（值 \(= V \cdot P\)，范围足够大）才能正确检测 \(>255\) 或 \(<0\) 并饱和。

**练习 2**：`apply_clamp_fixed16` 与 `apply_clamp` 的区别，用一句话概括是什么？

**答案**：`apply_clamp` 假设输入已在像素尺度，直接取低 8 位；`apply_clamp_fixed16` 假设输入是「放大了 16 倍的定点乘积」，先右移 4 位（取 `[11:4]`）除以 16 还原，再钳位。

---

### 4.4 `send_mult`：主机侧 float → 定点量化循环

#### 4.4.1 概念说明

主机程序里，用户调用的是 `send_mult(0.5f, true)`——传的是 `float`。但硬件只收字节。所以 `send_mult` 必须**把 float 量化成 1.3.4 定点字节**。

项目的做法非常「硬件工程师」：**从高位到低位，逐位试凑**。这其实就是把十进制小数转二进制小数的标准「乘 2 取整」法，只不过这里是「减权重」法。

#### 4.4.2 核心流程

```
val_fixed_4_4 = 0
value_buf = value
for i = 6 down to 0:                     // 遍历 bit6..bit0（跳过符号位 bit7）
    weight = 2^(i-4)                     // 该位的权重
    if value_buf >= weight:
        value_buf -= weight              // 减去这一份
        val_fixed_4_4 += (1 << i)        // 把这一位置 1
return val_fixed_4_4
```

每一位的权重（\(i\) 从 6 到 0）：

| i | 6 | 5 | 4 | 3 | 2 | 1 | 0 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 权重 \(2^{i-4}\) | 4 | 2 | 1 | 0.5 | 0.25 | 0.125 | 0.0625 |

可以看到：i=4 是个位（权重 1），i>4 是整数部分，i<4 是小数部分。**bit7（符号位）始终不动**，所以负数会被先前的 `if(value<0) val=0` 挡掉——主机不支持「乘以负数」（注释写明：「no sense to multiply an image by negative val」）。

#### 4.4.3 源码精读

[simulation/image_processing_simulation.cpp:104-L128](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L104-L128) —— 仿真后端的 `send_mult` 全文：

```cpp
void Image_processing_simulation::send_mult(float value, bool clamp){
   uint8_t val_fixed_4_4 = 0;
   if( value < 0 ){
      val_fixed_4_4 = 0;                       // 负数无意义，按 0 处理
   }

   float value_buf = value;
   // 遍历 7 位（4 位带符号小数，所以第 8 位会是 0）
   for (int i = 6; i >= 0; i--) {
      if( value_buf >= pow(2, i-4) ) {         // 该位能放下吗？
         value_buf -= pow(2, i-4);             // 放下了，减掉
         val_fixed_4_4 += 1<<i;                // 置该位
      }
   }

   fifo_in.push(Operation(true, COMMAND_APPLY_MULT, 0));   // 发操作码
   fifo_in.push(Operation(false, COMMAND_NONE, val_fixed_4_4)); // 发定点字节
   fifo_in.push(Operation(false, COMMAND_NONE, clamp));    // 发 clamp 标志

   for (size_t i = 0; i < 10; i++) { main_loop_clk(); }
}
```

对照 [u2-l2 命令协议](u2-l2-command-protocol.md)：这里发出的字节流是「操作码 `COMMAND_APPLY_MULT` + 1 字节定点乘数 + 1 字节 clamp 标志」，正好对应硬件 `STATE_APPLY_MULT_READ_PARAM` 要读的 2 个参数字节。两套后端（仿真 / iCE40）打包出的 `val_fixed_4_4` 字节完全相同（见 [ice40/software/image_processing_ice40.cpp:113](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L113) 附近同样的循环）。

#### 4.4.4 代码实践

**实践目标**：手算 `send_mult(0.5f, true)` 得到的定点字节，并端到端验证它在硬件里等价于「像素乘以 0.5」。

**操作步骤（逐位试凑）**：value_buf = 0.5

| i | 权重 \(2^{i-4}\) | 判断 `0.5 >= 权重`？ | 动作 | value_buf 剩余 | val_fixed |
| --- | --- | --- | --- | --- | --- |
| 6 | 4 | 否 | 跳过 | 0.5 | 0 |
| 5 | 2 | 否 | 跳过 | 0.5 | 0 |
| 4 | 1 | 否 | 跳过 | 0.5 | 0 |
| 3 | 0.5 | **是** | 减去 0.5，置 bit3 | 0.0 | **8** |
| 2 | 0.25 | 否（已为 0） | 跳过 | 0 | 8 |
| 1 | 0.125 | 否 | 跳过 | 0 | 8 |
| 0 | 0.0625 | 否 | 跳过 | 0 | 8 |

**结果**：`val_fixed_4_4 = 8`（即 0x08，二进制 `0000_1000`）。主机实际发出 `COMMAND_APPLY_MULT`、`8`、`1`(clamp=true) 三个字节。

**端到端验证**（结合 4.3）：硬件里 `mult_value_param = 8`，对像素 P：

\[ \text{结果} = \frac{8 \times P}{16} = \frac{P}{2} = 0.5 \times P \]

例如 P=200 → 结果 100；P=255 → 结果 127（\(255 \times 8 / 16 = 127.5\)，取整 127）。**确实实现了「乘以 0.5」**。✓

**需要观察的现象 / 预期结果**：你应当得出 `send_mult(0.5f)` → 字节 `8`，并且能解释「8 在硬件里怎么变成 ×0.5」的完整链路：主机放大 16 倍存（0.5×16=8）→ 硬件整数乘 → `apply_clamp_fixed16` 除以 16 还原。

> 待本地验证：若你想看真实输出，可在 `software/main.cpp` 里启用乘法测试（见综合实践）。

#### 4.4.5 小练习与答案

**练习 1**：手算 `send_mult(1.5f)` 得到的定点字节。

**答案**：value_buf=1.5。i=4(权重1)：1.5≥1 → 减 1，置 bit4，val=16，剩 0.5。i=3(权重0.5)：0.5≥0.5 → 减 0.5，置 bit3，val=16+8=**24**，剩 0。所以字节 = **24**（0x18）。验证：\(24/16 = 1.5\) ✓。

**练习 2**：`send_mult(8.0f)` 会得到什么？这说明了什么问题？

**答案**：value_buf=8.0。i=6(权重4)：置 bit6，val=64，剩 4。i=5(权重2)：置 bit5，val=96，剩 2。i=4(权重1)：置 bit4，val=112，剩 1。i=3(权重0.5)：置 bit3，val=120，剩 0.5……继续到 i=0 共置满低 4 位，val=127，剩约 0.0625 但已无更低位的 i 可表示——实际循环只到 i=0，最终 val=127，剩 0.0625 无法表示。也就是说 **8.0 超出了 1.3.4 的表示范围（最大约 7.9375），被「夹」到了 127**。这说明该格式无法表示 8.0 及以上的乘数，是设计上的固有限制。

---

## 5. 综合实践

把本讲的四个最小模块（定点格式、`apply_clamp`、`apply_clamp_fixed16`、`send_mult`）串起来，做一个**端到端可运行**的实验：在仿真模式下跑一次乘法运算，观察图像变暗（×0.5）的效果。

### 步骤 1：启用乘法测试

打开 [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp)，在 `main()` 的测试区（约 [L254-L260](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L254-L260) 附近）把其它 `test_*` 注释掉、把 `test_multiplication` 打开：

```cpp
   test_multiplication(image_input, image_output, img_proc);   // 取消注释
   // test_simple_edge_detection(image_input, image_output, img_proc);
```

### 步骤 2：阅读测试函数，确认调用链

[software/main.cpp:153-L165](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L153-L165) 是 `test_multiplication`，它走的正是 u1-l5 讲过的「三明治」套路：

```cpp
send_params → send_image → switch_buffers → send_mult(0.5f, true)
            → wait_end_busy → switch_buffers → read_image
```

注意 [L160](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L160) 的 `send_mult(0.5f, true)` —— 这就是你在 4.4 手算的那次调用。

### 步骤 3：构建并运行（仿真模式）

参考 [u1-l3 构建并运行：Verilator 仿真模式](u1-l3-build-run-simulation.md)：

```bash
./build_simulation.sh
./obj_dir/Vimage_processing     # 生成 output.dat
./run_gnuplot.sh                # 用 gnuplot 看灰度图
```

> 待本地验证：本环境不一定装了 verilator/gnuplot，若无法运行，改为「源码阅读型实践」——跳到步骤 4。

### 步骤 4：源码阅读型验证（无需运行）

即使不运行，你也可以在脑中跑一遍端到端链路，完成下表（这会把本讲全部知识串起来）：

| 阶段 | 谁在做 | 数值/字节 | 用到的本讲知识 |
| --- | --- | --- | --- |
| `send_mult(0.5f)` 量化 | 主机 C++ | float 0.5 → 字节 **8** | 4.4 试凑循环 |
| 字节送进硬件 | 通信接口 | `mult_value_param <= 8` | 4.1 寄存器 |
| 整数乘法（以像素 200 为例） | `STATE_PROC_UNARY` | `temp_calc = 8×200 = 1600` | 4.3 放大 16 倍 |
| 还原+钳位 | `apply_clamp_fixed16` | `1600[11:4] = 100`，未越界 | 4.3 除以 16 |
| 写回存储 | 运算 FSM | `data_write[7:0] <= 100` | u4-l1 两拍流水 |

**预期结果**：整幅图的每个像素都变成原来的一半（×0.5），图像明显变暗；`clamp=true` 保证不会出现奇怪的溢出值。你能用一句话说出：「主机把 0.5 放大 16 倍成整数 8，硬件做完整数乘法后再用 `[11:4]` 除回 16，就等价于浮点乘以 0.5。」

### 进阶：改乘数观察

把 [main.cpp:160](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L160) 改成 `send_mult(0.25f, true)`，按 4.4 的方法手算预期字节（答案：4），并预测图像会比 ×0.5 更暗。若能运行，对照 `output.dat` 验证。

## 6. 本讲小结

- 项目用 **1.3.4 定点格式**（1 符号 + 3 整数 + 4 小数，精度 \(1/16\)）表示实数，本质是「**把实数乘 16 存成整数**」。
- **`apply_clamp`** 用于加法/减法等「结果本身在像素尺度」的场景：直接取 `in[7:0]`，需要时饱和到 0~255。
- **`apply_clamp_fixed16`** 用于乘法/卷积等「结果被放大了 16 倍」的场景：取 `in[11:4]` = **右移 4 位 = 除以 16** 来还原像素尺度，钳位判断用更宽的 12 位 `in[15:4]` 以正确检测越界。
- **`send_mult`** 用「从高位到低位减权重」的试凑循环把 `float` 量化成定点字节；负数被强制清零，bit7 符号位实际不用。
- **主机放大 16 倍 ↔ 硬件除以 16** 是一对必须配对的操作：`send_mult(0.5f)` → 字节 8 → 硬件 `8×P` → `[11:4]` → \(P/2\)，端到端等价于浮点乘 0.5。
- 同一个 `apply_clamp_fixed16` 还服务卷积——卷积核也是 1.3.4 定点数，乘加后同样要除以 16 还原。

## 7. 下一步学习建议

本讲把「乘法运算」的定点机制讲透了，但乘法只是 `STATE_PROC_UNARY` 里的一个分支。接下来有两个方向：

1. **横向**：进入 [u4-l3 双图运算：STATE_PROC_BINARY](u4-l3-binary-operations.md)，看两个缓冲之间的 add/sub/mult 如何复用 `apply_clamp`，以及取绝对差 `absolute_diff` 的实现。它仍在本单元「处理运算的状态机实现」之内。
2. **纵向**：进入 [u5 3x3 卷积引擎](u5-l1-convolution-overview.md)。卷积核是 9 个 1.3.4 **有符号**定点数，9 次乘加后结果同样被放大 16 倍，最终也靠 `apply_clamp_fixed16` 还原——你会再次见到本讲的 `in[11:4]`，但这次它要处理负数（边缘检测核有负权重），是本讲知识的进阶应用。

无论走哪条路，记住本讲的核心直觉：**定点数就是「带固定放大倍数的整数」，谁放大了，运算后就得由谁缩小回来**——`in[11:4]` 就是项目里那个「缩小回来」的动作。
