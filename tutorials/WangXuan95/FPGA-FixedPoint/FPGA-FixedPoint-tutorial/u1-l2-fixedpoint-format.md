# 定点数格式与统一参数命名

## 1. 本讲目标

本讲承接 [u1-l1](./u1-l1-project-overview.md)，在已经跑通首次仿真、知道这个库「能做什么」的基础上，回答一个更根本的问题：**这些模块里流动的 `ina`、`inb`、`out` 这些二进制码，到底代表什么数？**

学完本讲你应该能够：

- 用「补码整数 ÷ \(2^{\text{小数位宽}}\)」这一条公式，在**一个二进制码**和**一个真实数值**之间双向换算，并读懂 README 的取值表。
- 说清楚**整数位宽**决定表示范围、**小数位宽**决定精度这一对关系，并能手算某一组位宽配置下的最大值、最小值与分辨率。
- 记住全库统一的参数命名约定（`WOI/WOF`、`WII/WIF`、`WIIA/WIFA/WIIB/WIFB`、`ROUND`），拿到任意一个模块都能看懂它的 `parameter` 列表。
- 理解 `ROUND` 参数控制「截断时是否四舍五入」，并在 `fxp_zoom` 源码里定位到实现这一行为的几行关键代码。

本讲只讲**格式本身**，不深入 `fxp_zoom` 的溢出饱和细节（那是 [u1-l3](./u1-l3-fxp-zoom.md) 的主题），也不讲加减乘除的算法（那是第二单元的主题）。

## 2. 前置知识

- **二进制补码（two's complement）**：计算机里表示带符号整数最常见的方式。一个 \(n\) 位补码数，最高位是符号位（0 表非负、1 表负），它的整数值是「按位取反再加 1」可还原。本讲你只要记住：一个 16 位补码码值 `0xFF60`，作为有符号整数看是 \(-160\)，作为无符号看是 \(65376\)。
- **Verilog 的 `$signed`**：把一个 `reg/wire` 当作**有符号数**来参与运算。本库所有 testbench 都靠 `$signed(code)*1.0/(1<<WOF)` 把一串二进制码「翻译」回浮点数打印出来，这是阅读本库仿真的万能钥匙（见 [u1-l1](./u1-l1-project-overview.md)）。
- **Verilog `parameter`**：模块实例化时可以覆盖的「编译期常量」，用来定制位宽。本库所有模块的位宽都用 `parameter` 暴露，命名高度统一。
- **定点数 vs 浮点数**：浮点数（如 IEEE754）的小数点位置会随数值大小「浮动」，用一个指数来记录；定点数则**把小数点位置固定死**，所有数共用同一套「几位整数、几位小数」的约定。FPGA 上定点数因为只需要整数运算单元、时序好、资源省，是数字信号处理的主流选择。

## 3. 本讲源码地图

本讲只涉及两个文件，且只看其中与「格式」直接相关的部分：

| 文件 | 本讲关注的内容 |
| :--- | :--- |
| [README.md](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md) | 「定点数格式」一节的换算定义与取值表；统一参数命名约定；`fxp_mul` 的参数列表示例。 |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | `fxp_mul`（乘法，双目运算的命名范本）与 `fxp_zoom`（位宽变换核心，`ROUND` 舍入逻辑所在）两个模块的端口与参数定义。 |

> 提示：本库全部 13 个可综合模块都集中在 `RTL/fixedpoint.v` 这一个文件里，所以本讲的「源码精读」会频繁引用它的不同行段。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：① 定点数的换算公式；② 位宽如何决定范围与精度；③ 全库统一的参数命名；④ `ROUND` 舍入控制。

### 4.1 定点数值 = 补码整数 ÷ 2^小数位宽

#### 4.1.1 概念说明

一句话定义（这是本库的唯一约定）：

> 一个定点数的真实值，等于**它那串二进制码作为有符号补码整数时的值**，除以 **\(2^{\text{小数位宽}}\)**。

也就是说，二进制码本身就是一个普通的补码整数；我们只是**人为约定**「把它的最低几位当成小数部分」。这个约定不改任何二进制位，只改我们**怎么读**它。

为什么这样设计？因为硬件里只有整数加法器、整数乘法器。把小数点位置在编译期固定下来后，定点加减乘除就可以**直接复用整数运算电路**，只需要在最后做位宽对齐与截断——这正是本库 `fxp_zoom` 干的事。

#### 4.1.2 核心流程

设一个定点数配置有 \(W_I\) 位整数位宽（含 1 位符号位）和 \(W_F\) 位小数位宽，总宽度 \(W_I+W_F\) 位。记它的二进制码作为补码整数的值为 \(c\)，则它表示的真实值为 \(v\)：

\[ v = \frac{c}{2^{W_F}} \quad\Longleftrightarrow\quad c = \mathrm{round}\!\left(v \times 2^{W_F}\right) \]

- **解码（码 → 值）**：\(v = c / 2^{W_F}\)，也就是把补码整数除以 \(2^{W_F}\)。
- **编码（值 → 码）**：\(c = \mathrm{round}(v \times 2^{W_F})\)，把要表示的值乘以 \(2^{W_F}\) 再取整（若落在网格上则精确，否则四舍五入到最近的码）。

以 README 取值表里「8 位整数 + 8 位小数」（即 \(W_I=8, W_F=8\)，总 16 位）为例，\(2^{W_F}=256\)：

- 码 `0000000100000000` → 补码整数 \(c=256\) → 值 \(v=256/256=1.0\)。
- 码 `1111111100000000` → 补码整数 \(c=-256\) → 值 \(v=-256/256=-1.0\)。
- 码 `0000000000000001` → \(c=1\) → 值 \(v=1/256=0.00390625\)（正方向最小步进）。
- 码 `0111111111111111` → \(c=32767\) → 值 \(v=32767/256=127.99609375\)（正最大）。

#### 4.1.3 源码精读

README 中文版的「定点数格式」一节就是这条公式的权威出处：

- [README.md:124](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L124) ——「定点数由若干位整数和若干位小数组成。其值 = **该二进制码对应的整数补码** 除以 **2^小数位数**」，这正是上面的公式。

紧随其后的取值表给出了多组可对照的样例（8 位整数 + 8 位小数）：

- [README.md:128-138](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L128-L138) —— 对照表，列出「二进制码 / 整数补码 / 定点数值」三列，建议逐行用 \(v=c/256\) 验算一遍。

这条公式在仿真里长什么样？看 testbench 里反复出现的 `$signed(x)*1.0/(1<<W)`：

- [SIM/tb_add_sub_mul_div.v:103](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L103) —— `($signed(ina)*1.0)/(1<<WIFA)`，把输入码 `ina` 按 \(W_I=WIFA\) 的小数位宽解码成浮点数打印。`1<<WIFA` 就是 \(2^{WIFA}\)，`$signed` 保证按补码读。这就是「解码」公式的直接翻译。

#### 4.1.4 代码实践

**实践目标**：亲手走通「值 → 码 → 值」的往返，建立对公式的肌肉记忆。

**操作步骤**（手算部分，配置取 \(W_I=10, W_F=6\)，即 \(2^{W_F}=64\)，总 16 位）：

1. 对 \(3.14\)：\(c=\mathrm{round}(3.14\times 64)=\mathrm{round}(200.96)=201=16\)'h00C9。
2. 对 \(-2.5\)：\(c=\mathrm{round}(-2.5\times 64)=\mathrm{round}(-160)=-160\)。16 位补码：\(65536-160=65376=16\)'hFF60。
3. 对 \(127.99\)：\(c=\mathrm{round}(127.99\times 64)=\mathrm{round}(8191.36)=8191=16\)'h1FFF。

然后用下面的**最小 testbench**（示例代码，不依赖任何 RTL，纯粹验证解码公式）把这三个码翻译回浮点数：

```verilog
// 示例代码：纯解码验证，无需编译 RTL/fixedpoint.v
`timescale 1ps/1ps
module tb_format ();
    localparam WOI = 10;   // 整数位宽（含符号位）
    localparam WOF = 6;    // 小数位宽

    reg [WOI+WOF-1:0] code_v1 = 16'h00C9;  //  3.14  ->  201
    reg [WOI+WOF-1:0] code_v2 = 16'hFF60;  // -2.5   -> -160
    reg [WOI+WOF-1:0] code_v3 = 16'h1FFF;  // 127.99 -> 8191

    initial begin
        $display("code=%h  decoded=%f", code_v1, ($signed(code_v1)*1.0)/(1<<WOF));
        $display("code=%h  decoded=%f", code_v2, ($signed(code_v2)*1.0)/(1<<WOF));
        $display("code=%h  decoded=%f", code_v3, ($signed(code_v3)*1.0)/(1<<WOF));
        $finish;
    end
endmodule
```

运行（iverilog 已安装的前提下）：

```bash
iverilog -g2001 -o sim tb_format.v
vvp sim
```

**需要观察的现象**：三行打印分别还原出三个浮点数。

**预期结果**：`3.140625`、`-2.500000`、`127.984375`。其中 \(3.14\) 还原成 \(3.140625\)、\(127.99\) 还原成 \(127.984375\)，是因为 \(201/64\) 与 \(8191/64\) 是这些十进制值在 \(1/64\) 网格上的最近邻；而 \(-2.5\) 恰好落在网格上，所以精确还原。如果某行对不上，回头检查你的手算码值或正负号。

#### 4.1.5 小练习与答案

**练习 1**：同样是 \(3.14\)，若改用 \(W_F=8\)（即除以 256），编码后的补码整数 \(c\) 是多少？

**答案**：\(c=\mathrm{round}(3.14\times 256)=\mathrm{round}(803.84)=804\)。

**练习 2**：README 表里码 `1001010110100110` 标注值为 \(-106.3515625\)，用公式验算（8 位整数 + 8 位小数）。

**答案**：该码作为 16 位补码整数 \(c=-27226\)，\(v=-27226/256=-106.3515625\)。✓

---

### 4.2 整数位宽与小数位宽：范围与精度

#### 4.2.1 概念说明

定点数的两个位宽参数各管一件事：

- **整数位宽 \(W_I\)**（含 1 位符号位）→ 决定**能表示多大、多小的数（范围）**。
- **小数位宽 \(W_F\)** → 决定**相邻两个码之间的最小步长（精度/分辨率）**。

二者是**独立的旋钮**，可以分别调。总位宽 \(W_I+W_F\) 决定寄存器/线网宽度，也大致决定资源开销。

#### 4.2.2 核心流程

一个 \(W_I+W_F\) 位补码码值 \(c\) 的整数取值范围是：

\[ -2^{(W_I+W_F-1)} \le c \le 2^{(W_I+W_F-1)} - 1 \]

两边同除以 \(2^{W_F}\)，得到定点数值 \(v\) 的范围与分辨率：

\[
\boxed{\;-\,2^{(W_I-1)} \;\le\; v \;\le\; 2^{(W_I-1)} - 2^{-W_F}\;},\qquad
\text{分辨率} = 2^{-W_F}
\]

也就是说：

- 把 \(W_I\) 调大 1 位，表示范围翻倍（代价是更宽的寄存器）。
- 把 \(W_F\) 调大 1 位，精度提高一倍（步长减半），但**不改变范围**。
- 范围只跟 \(W_I\) 有关，精度只跟 \(W_F\) 有关——这是一个非常实用的设计直觉。

以 \(W_I=8, W_F=8\) 为例：范围 \([-128,\ 127.99609375]\)，分辨率 \(2^{-8}=0.00390625\)，与 README 取值表的「正最大 / 负最小 / 正最小」三行完全吻合。

#### 4.2.3 源码精读

「位宽决定宽度」这件事在源码里体现得最直接：**端口宽度就是用 `WII+WIF`、`WOI+WOF` 这种加法写出来的**。看 `fxp_mul` 的端口声明：

- [RTL/fixedpoint.v:287-290](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L287-L290) —— `input wire [WIIA+WIFA-1:0] ina`、`[WIIB+WIFB-1:0] inb`、`output wire [WOI+WOF-1:0] out`。输入码宽 = 整数位宽 + 小数位宽，输出同理。改任何一个位宽参数，对应线网宽度会自动跟着变。

再看乘法里「整数位宽相加、小数位宽相加」这条关键性质：

- [RTL/fixedpoint.v:293-294](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L293-L294) —— `localparam WRI = WIIA + WIIB;` 与 `WRF = WIFA + WIFB;`。两个 \(W_I+W_F\) 位数相乘，**乘积的整数位宽 = 两数整数位宽之和、小数位宽 = 两数小数位宽之和**。所以全精度乘积是 \((W_{IIA}+W_{IIB})+(W_{IFA}+W_{IFB})\) 位。这条结论是理解定点乘法（[u2-l2](./u2-l2-mul.md)）的基石，本讲先建立直觉。

#### 4.2.4 代码实践

**实践目标**：在脑子里建立「范围 vs 精度」两个旋钮的独立手感。

**操作步骤**（源码阅读 + 推算，无需运行）：

1. 读 [RTL/fixedpoint.v:293-294](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L293-L294)，确认 `WRI/WRF` 的定义。
2. 取默认配置 `WIIA=WIFA=WIIB=WIFB=WOI=WOF=8`，回答三问：
   - 输入 `ina` 表示的值域是多少？ → \([-128,\ 127.99609375]\)，分辨率 \(1/256\)。
   - 全精度乘积 `res` 的位宽是多少？ → \(WRI+WRF=(8+8)+(8+8)=32\) 位。
   - 乘积值域是多少？ → 整数部分 \(WRI=16\) 位，范围量级为 \(\pm 2^{15}\approx \pm 32768\)。
3. 假设只把 `WOF` 从 8 调到 12（其它不变），问：输出范围变了吗？输出精度变了吗？

**需要观察/思考的现象**：第 3 步里范围应**不变**（仍由 `WOI=8` 决定），精度应**提高**到 \(2^{-12}\)。这印证「范围看整数位宽、精度看小数位宽」。

**预期结果**：第 3 步结论——范围不变、精度变细。若你推断范围也变了，说明把整数位宽和小数位宽的作用搞混了，回看本节公式。

#### 4.2.5 小练习与答案

**练习 1**：\(W_I=10, W_F=6\) 时，能表示的最大正值、最小负值、分辨率各是多少？

**答案**：最大正值 \(2^9-2^{-6}=511.984375\)；最小负值 \(-2^9=-512\)；分辨率 \(2^{-6}=0.015625\)。

**练习 2**：保持总位宽 16 位不变，把配置从「8 整数 + 8 小数」改成「10 整数 + 6 小数」，范围和精度分别怎么变？

**答案**：范围从 \([-128,127.996]\) 扩大到 \([-512,511.984]\)（整数位多了 2 位，范围 ×4）；精度从 \(0.00390625\) 变粗到 \(0.015625\)（小数位少了 2 位，精度 /4）。这就是「范围与精度相互权衡」的经典取舍。

---

### 4.3 全库统一的参数命名约定

#### 4.3.1 概念说明

本库的位宽不是写死的，而是全部用 `parameter` 暴露出来，可以在实例化时覆盖。更关键的是：**全库的参数命名高度统一**，记住一套规则就能读懂任意模块。命名规则按「输入/输出」+「整数/小数」+「单目/双目」三维展开。

#### 4.3.2 核心流程

把命名拆成下表（最值得背下来的一张表）：

| 参数 | 含义 | 出现在 |
| :--- | :--- | :--- |
| `WOI` | **W**idth **O**utput **I**nteger —— 输出定点数的**整数**位宽 | 所有模块 |
| `WOF` | **W**idth **O**utput **F**raction —— 输出定点数的**小数**位宽 | 所有模块 |
| `WII` / `WIF` | **W**idth **I**nput **I**nteger / **F**raction —— **单目**运算输入的整数/小数位宽 | 单目模块（`fxp_zoom`、`fxp_sqrt`、`fxp2float`） |
| `WIIA` / `WIFA` | 输入操作数 **A** 的整数/小数位宽 | 双目模块（`fxp_add`、`fxp_addsub`、`fxp_mul`、`fxp_div`） |
| `WIIB` / `WIFB` | 输入操作数 **B** 的整数/小数位宽 | 双目模块 |
| `ROUND` | 截断时是否四舍五入（1=是，0=否） | 除浮点转换的 `fxp2float` 外，基本都有 |

记忆口诀：**`W`=Width，`O/I`=Output/Input，`I/F`=Integer/Fraction，末尾的 `A/B`=操作数 A/B**。两路输入位宽可以**不同**（如 `WIIA=10,WIFA=11` 而 `WIIB=8,WIFB=12`），库内部会用 `fxp_zoom` 自动对齐——这正是 [u1-l3](./u1-l3-fxp-zoom.md) 的主题。

#### 4.3.3 源码精读

README 用 `fxp_mul` 当作命名约定的官方示例，每个参数都带中文注释：

- [README.md:149-164](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L149-L164) —— `fxp_mul` 的参数与端口声明，注释逐一说明 `WIIA/WIFA/WIIB/WIFB/WOI/WOF/ROUND` 的含义，是入门最佳参考。

对照看真实源码里的同名模块（双目运算的命名范本）：

- [RTL/fixedpoint.v:278-291](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L278-L291) —— `fxp_mul` 的 `parameter` 列表与端口，参数名与 README 示例完全一致；注意双目模块用 `WIIA/WIFA/WIIB/WIFB`。

再看单目运算的命名范本 `fxp_zoom`（位宽变换，只有一个输入）：

- [RTL/fixedpoint.v:22-32](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L22-L32) —— `fxp_zoom` 的 `parameter` 列表用 `WII/WIF`（单数输入）+ `WOI/WOF`（输出）+ `ROUND`。对比 `fxp_mul` 即可看出「单目用 `WII/WIF`，双目用 `WIIA…/WIIB…`」的差别。

#### 4.3.4 代码实践

**实践目标**：不看本讲，凭命名规则独立读懂一个新模块的位宽配置。

**操作步骤**（源码阅读）：

1. 打开 [RTL/fixedpoint.v:690-700](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L690-L700)（`fxp_sqrt` 模块头）。
2. 仅凭参数名，回答：它是单目还是双目？输入位宽由哪几个参数决定？输出位宽由哪几个参数决定？
3. 再打开 [RTL/fixedpoint.v:186-200](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L186-L200)（`fxp_addsub` 模块头），找出它比 `fxp_add` 多出来的那个**控制位**端口（提示：与加减切换有关）。

**需要观察的现象**：你应该无需读注释就能从 `WII/WIF` vs `WIIA…/WIIB…` 判断单/双目，并定位到 `sub` 这个控制位。

**预期结果**：`fxp_sqrt` 是单目（用 `WII/WIF`）；`fxp_addsub` 多了一个 `input wire sub`（`0`=加、`1`=减）。如果对不上，重读本节命名表。

#### 4.3.5 小练习与答案

**练习 1**：一个模块同时出现 `WIIA` 和 `WII`，可能吗？

**答案**：不会出现在「同一个 `parameter` 列表」里。双目模块用 `WIIA/WIIB`，单目模块用 `WII`。但双目模块的**函数体内部**可能用 `localparam WII = (WIIA>WIIB)?WIIA:WIIB;` 派生出一个公共 `WII`（见 [fxp_add 第 125 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L125)），这是内部对齐用的，不是对外参数。

**练习 2**：`fxp2float` 模块**没有** `ROUND` 参数，而 `float2fxp` 有。结合「谁会产生截断」想一想为什么？

**答案**：`float2fxp`（浮点→定点）需要把无限精度的浮点塞进有限的小数位，会产生截断，所以要 `ROUND` 控制；`fxp2float`（定点→浮点）是把定点值精确映射到一个更宽的浮点表示，不丢精度，故无需 `ROUND`。可在 [fxp2float 第 874-880 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L874-L880) 与 [float2fxp 第 1039-1047 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1039-L1047) 对照确认。

---

### 4.4 ROUND：截断时的四舍五入

#### 4.4.1 概念说明

定点运算常常要把一个**位宽较宽**的中间结果塞进**位宽较窄**的输出，比如乘法全精度积有 \((W_{IFA}+W_{IFB})\) 位小数，要截断到输出 `WOF` 位小数。截断低几位时，有两种选择：

- **直接截断（向零取整）**：丢弃多余低位，等价于向最近且更靠近零的网格点取整，误差最大接近 1 个 LSB。
- **四舍五入（round to nearest）**：看被丢弃的最高位（「舍入位」），为 1 就向远离零的方向进 1，误差最大半个 LSB。

`ROUND` 参数就控制这件事：`ROUND=1` 四舍五入（默认），`ROUND=0` 直接截断。注意：`ROUND` 只在**发生小数位截断**（输出小数位宽 < 中间结果小数位宽）时才起作用；如果位宽匹配或扩展，它不产生任何影响。

#### 4.4.2 核心流程

设中间结果小数位宽为 \(W_F\)、输出小数位宽为 \(W_F'\)，且 \(W_F' < W_F\)（要丢掉 \(W_F-W_F'\) 位）。把码值右移 \(W_F-W_F'\) 位：

\[ c_{\text{out}} = \begin{cases} \big\lfloor c / 2^{W_F-W_F'} \big\rfloor & \text{若 } \texttt{ROUND}=0 \\[2pt] \big\lfloor c / 2^{W_F-W_F'} + \tfrac{1}{2} \big\rfloor & \text{若 } \texttt{ROUND}=1 \text{（且舍入位}=1\text{ 时进一）} \end{cases} \]

所谓「舍入位」就是被丢弃的最高一位（即原码的第 \(W_F-W_F'-1\) 位）。`ROUND=1` 时它为 1 就进一；但有一个**饱和保护**：当保留部分已是「正最大」（`0111…1`）时，再进一会翻转符号位变成「负最小」，此时**不进一**，避免越界。这个保护在源码里体现为一个稍长的布尔条件。

#### 4.4.3 源码精读

`fxp_zoom` 用 `generate if(WOF<WIF)` 处理「需要截断小数位」的情况，三种分支正好对应 `ROUND` 的取舍：

- [RTL/fixedpoint.v:41-54](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L41-L54) —— `WOF<WIF` 分支：
  - `ROUND==0`（第 42-43 行）：`inr = in[WII+WIF-1:WIF-WOF]`，纯截断，取高位、丢低位。
  - `ROUND==1`（第 44-48 行）：先取高位，再判断 `in[WIF-WOF-1]`（**舍入位**，被丢弃的最高一位）为 1 且不触发正最大饱和时，`inr=inr+1` 进一。
  - 其中 `~inr[WII+WOF-1] & (&inr[WII+WOF-2:0])` 正是「保留值是正最大」的检测，取反后与舍入位相与，实现「舍入位为 1 且非正最大才进一」。

`fxp_mul` 把 `ROUND` 透传给内部的 `fxp_zoom`，所以乘积的舍入行为由调用者决定：

- [RTL/fixedpoint.v:298-308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L298-L308) —— `fxp_mul` 内部例化 `fxp_zoom`，`.ROUND(ROUND)` 把外层参数透传进去；由于乘积小数位宽 `WRF=WIFA+WIFB` 几乎总大于输出 `WOF`，这里会发生截断，`ROUND` 直接影响乘积精度。

#### 4.4.4 代码实践

**实践目标**：用一次「心算仿真」感受 `ROUND=0` 与 `ROUND=1` 的差别。

**操作步骤**（推演，无需运行）：

1. 设想一个 `fxp_zoom` 实例：`WII=1, WIF=2, WOI=1, WOF=0`（输入 3 位、输出 1 位，丢掉 2 位小数，最简化场景）。
2. 输入码 `in = 3'b011`：作为 3 位补码 \(c=3\)，定点值 \(v=3/2^2=0.75\)。
3. 分别按 `ROUND=0` 和 `ROUND=1` 推算输出码 `out`（1 位）：
   - `ROUND=0`：截断 → \(\lfloor 3/4 \rfloor = 0\) → `out=1'b0`。
   - `ROUND=1`：舍入位 = 第 0 位 = 1 → 进一 → \(\lfloor 3/4 + 1/2 \rfloor = 1\) → `out=1'b1`。
4. 把 `in` 改成 `3'b010`（\(v=0.5\)）再推一遍：舍入位 = 第 0 位 = 0，两种模式都得到 `out=1'b0`。

**需要观察的现象**：对 \(0.75\)，`ROUND=0` 输出 0（误差 0.75），`ROUND=1` 输出 1（误差 0.25，更接近真值）；对 \(0.5\)，两者都输出 0。

**预期结果**：`ROUND=1` 的最大截断误差约半个 LSB，`ROUND=0` 的最大截断误差接近 1 个 LSB。这正是工程上默认 `ROUND=1` 的原因。若想真实跑一遍，可参照 4.1.4 的 testbench 套路，单独例化 `fxp_zoom` 并把 `ROUND` 设为 0/1 对比 `$signed(out)` 的打印（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fxp_add` 在把两路输入对齐时，内部那两个 `fxp_zoom`（`ina_zoom`、`inbz`）都把 `.ROUND(0)` 写死，而最后还原输出的 `res_zoom` 才用 `.ROUND(ROUND)`？见 [RTL/fixedpoint.v:134-168](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L134-L168)。

**答案**：输入对齐阶段只是把两路数凑到公共位宽、参与加法，任何提前的舍入都会引入**额外误差**并污染最终结果；只有到最后「收敛到输出位宽」这一步才舍入，误差只发生一次、可控。所以中间过程一律 `ROUND=0`（不丢精度），只在输出端按用户要求舍入。

**练习 2**：若 `WOF == WIF`（输出小数位宽等于输入），`ROUND` 还有意义吗？

**答案**：没有。`fxp_zoom` 在 `WOF==WIF` 分支（[第 55-56 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L55-L56)）只做整数位宽的扩展/截断，根本不进 `WOF<WIF` 的舍入分支，`ROUND` 参数被忽略。所以「舍入只在截断小数位时发生」。

---

## 5. 综合实践

把本讲四条主线（换算公式、范围与精度、统一命名、`ROUND`）串成一个**贯穿性验证**：用 `fxp_mul` 验证定点格式在输入与输出之间是自洽的——也就是「乘以 1.0 后，解码回来的值应等于原值」。

**实践目标**：同时完成两件事——(a) 用 4.1.4 手算的三个码值喂给 `fxp_mul`，(b) 用 `$signed(out)*1.0/(1<<WOF)` 解码输出，确认它与输入解码值一致，从而验证「同一个 `$signed` 解码套路对输入、输出通用」以及「乘 1.0 不改变定点格式」。

**操作步骤**：

1. 配置：`WIIA=WIIB=WOI=10`，`WIFA=WIFB=WOF=6`，`ROUND=1`（输入输出同格式，且让乘积小数位 `WRF=12` 截断到 `WOF=6`）。
2. 把 `ina` 设为 4.1.4 手算的三个码值之一（如 `3.14`→`16'h00C9`），`inb` 设为代表 **1.0** 的码值：\(1.0\times 64 = 64 =\) `16'h0040`。
3. 用 `$signed(ina)*1.0/(1<<WOF)` 打印输入解码值，用 `$signed(out)*1.0/(1<<WOF)` 打印输出解码值，并排比较。
4. 对 `3.14`、`-2.5`、`127.99` 三组各跑一次。

**参考 testbench 骨架**（示例代码）：

```verilog
// 示例代码：需与 RTL/fixedpoint.v 一起编译
`timescale 1ps/1ps
module tb_mul_format ();
    localparam WOI = 10, WOF = 6;

    reg  [WOI+WOF-1:0] ina = 0;
    reg  [WOI+WOF-1:0] inb = 16'h0040;   // 代表 1.0
    wire [WOI+WOF-1:0] out;
    wire               overflow;

    fxp_mul #(
        .WIIA(WOI), .WIFA(WOF), .WIIB(WOI), .WIFB(WOF),
        .WOI (WOI), .WOF (WOF), .ROUND(1'b1)
    ) dut (
        .ina(ina), .inb(inb), .out(out), .overflow(overflow)
    );

    task show;
        input [WOI+WOF-1:0] code;
    begin
        ina = code; #10000;
        $display("ina_decoded=%f  out_decoded=%f  overflow=%b",
                 ($signed(ina)*1.0)/(1<<WOF),
                 ($signed(out)*1.0)/(1<<WOF),
                 overflow);
    end
    endtask

    initial begin
        show(16'h00C9);  //  3.14
        show(16'hFF60);  // -2.5
        show(16'h1FFF);  // 127.99
        $finish;
    end
endmodule
```

运行：

```bash
iverilog -g2001 -o sim RTL/fixedpoint.v tb_mul_format.v
vvp sim
```

**需要观察的现象**：每组的 `ina_decoded` 与 `out_decoded` 应**完全相同**，`overflow=0`。

**预期结果**：三组分别打印 `(3.140625, 3.140625)`、`(-2.5, -2.5)`、`(127.984375, 127.984375)`。原理：乘以 1.0 后值不变，且输入输出采用相同的 \((W_I,W_F)=(10,6)\) 格式，所以解码回来的浮点数必然相等。这说明你已经能用同一条 `$signed` 公式在模块的**入口和出口**之间自如换算——这正是阅读后续所有模块仿真的基础。

> 若结果出现偏差：先检查 `inb` 是否真的代表 1.0（必须是 `1<<WOF=64`，而不是 `1`），再检查位宽参数是否输入输出完全一致。

## 6. 本讲小结

- 定点数值 = **补码整数 ÷ \(2^{\text{小数位宽}}\)**；编解码互为逆运算：\(c=\mathrm{round}(v\cdot 2^{W_F})\)，\(v=c/2^{W_F}\)。
- **整数位宽 \(W_I\)（含符号位）决定范围** \([-2^{W_I-1},\ 2^{W_I-1}-2^{-W_F}]\)；**小数位宽 \(W_F\) 决定精度**（分辨率 \(2^{-W_F}\)）；两者是独立旋钮。
- 全库参数命名统一：`W`=Width / `O,I`=Output,Input / `I,F`=Integer,Fraction / 末尾 `A,B`=操作数 A,B；单目用 `WII/WIF`，双目用 `WIIA…/WIIB…`。
- 端口宽度直接写成 `WII+WIF`、`WOI+WOF`；定点相乘时**积的整数位宽=两整数位宽之和、小数位宽=两小数位宽之和**。
- `ROUND` 控制小数位截断时是否四舍五入（默认 1=四舍五入，误差≤½LSB；0=直接截断，误差<1LSB），只在「输出小数位宽更窄」时生效；`fxp_zoom` 第 41-54 行是它的实现所在。
- 仿真里的万能钥匙是 `$signed(code)*1.0/(1<<WOF)`——它对输入和输出同样适用。

## 7. 下一步学习建议

下一讲 [u1-l3 fxp_zoom：被全库复用的位宽变换核心](./u1-l3-fxp-zoom.md) 将正式钻进 `fxp_zoom` 内部，把本讲提到的「舍入」「整数位截断时的溢出饱和」一次性讲透——它会用本讲建立的「整数位宽/小数位宽」两把旋钮，解释 `generate if(WOF<WIF)` 与 `generate if(WOI<WII)` 两段 `generate` 各自在做什么。

在此之前建议你：

- 重读 [RTL/fixedpoint.v:22-94](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L22-L94)（`fxp_zoom` 全模块），先尝试自己用本讲的公式解释 `inr / ini / outi / outf` 四个寄存器各承载「整数部分」还是「小数部分」。
- 翻看 [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v)，确认你能看懂每一行 `$signed(...)*1.0/(1<<W...)` 在解码哪个端口——如果都能看懂，说明本讲目标已达成。
