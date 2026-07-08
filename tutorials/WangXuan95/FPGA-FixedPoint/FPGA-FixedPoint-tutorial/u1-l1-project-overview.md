# 项目概览与首次仿真运行

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应该能够：

- 说清楚 **FPGA-FixedPoint** 是一个什么样的项目、它能做哪些定点数运算。
- 看懂仓库的目录结构：知道可综合的 RTL 代码在哪、仿真测试平台在哪、如何用一条命令把仿真跑起来。
- 在自己的机器上安装 iverilog，运行 `SIM` 目录下的 `.bat` 脚本，看到第一份仿真输出。
- 读懂测试平台（testbench）里那个核心套路：**用 `$signed` + 位移把一串二进制码还原成一个浮点数**，再和软件算出来的结果并排打印对比。
- 认识本讲涉及的四个入门运算模块：`fxp_add`、`fxp_addsub`、`fxp_mul`、`fxp_div`，知道它们的端口长什么样。

> 本讲只做"概览 + 跑通"，不深入每个模块的算法细节。加减乘除的内部原理会在第 2 单元（进阶层）逐篇拆解。

---

## 2. 前置知识

本讲对读者几乎没有 FPGA 工程经验的要求，只要具备下面三点即可：

1. **二进制与补码**：知道计算机里负数通常用"补码"表示，且一个 N 位有符号数的取值范围大约是 \([-2^{N-1},\ 2^{N-1}-1]\)。
2. **一点点 Verilog 语法**：看得懂 `module ... endmodule`、`wire`/`reg`、`always`、`initial`、`$display`、`parameter` 这些关键字。看不懂也没关系，本讲会随讲随解释。
3. **会用命令行**：能在终端里执行一条命令、看懂它的输出。

你**不需要**懂 FPGA 综合、不需要有开发板、也不需要装任何商用的 EDA 软件。本库的全部仿真都用免费开源的 **iverilog** 完成。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| [README.md](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md) | 项目说明书：特性清单、定点数格式、模块清单、仿真方法 |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | **全部 13 个可综合模块**都集中在这一个文件里（本讲的加减乘除模块也都在此） |
| [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) | 单周期加减乘除的测试平台（testbench） |
| [SIM/tb_add_sub_mul_div_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div_run_iverilog.bat) | 一键编译 + 运行上面这个 testbench 的批处理脚本 |

一句话记忆：**RTL 目录放"被测的电路"，SIM 目录放"测试电路的电路"和"启动测试的脚本"**。

---

## 4. 核心概念与源码讲解

### 4.1 FPGA-FixedPoint 是什么

#### 4.1.1 概念说明

**定点数（fixed-point number）** 是介于"整数"和"浮点数"之间的一种数值表示法。它本质上还是一个**整数**，但我们**约定**这个整数代表的真实数值是：

\[
\text{真实值} = \frac{\text{二进制码对应的有符号整数（补码）}}{2^{\text{小数位宽}}}
\]

举个例子：假设"整数位宽 = 8，小数位宽 = 8"，那么二进制码 `0000000100000000` 对应的整数是 256，它代表的定点值就是 \(256 / 2^8 = 1.0\)。

为什么 FPGA 上要用定点数？因为**浮点运算在硬件里又大又慢**，而定点数本质上就是带了一个"隐含的小数点位置"的整数运算，可以用普通的整数加法器、乘法器高效实现。**FPGA-FixedPoint** 就是一套用 Verilog 写好的、拿来即用的定点数运算库。

#### 4.1.2 核心特性

README 在开头就列出了这个库的几大特性：

- 可定制**整数位宽**和**小数位宽**。
- 支持 **加、减、乘、除、开方** 五种运算。
- **溢出检测**：结果超出能表示的范围时，`overflow` 信号拉高，并把输出钳位（饱和）到正最大值或负最小值。
- **舍入控制**：发生截断时，可选是否做四舍五入。
- 可与 **IEEE754 单精度浮点数**互相转换。
- 所有运算都有**单周期（组合逻辑）实现**；时序紧张的运算还提供**流水线实现**。

#### 4.1.3 源码精读

这些特性写在 README 的特性列表里：

[README.md:L10-L17](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L10-L17) —— 这一段用条目列出了"运算种类 / 溢出检测 / 舍入控制 / 浮点互转 / 单周期与流水线实现"，是整个库的功能总纲。

README 还给出了一张定点数取值表（以 8 位整数 + 8 位小数为例）：

[README.md:L27-L37](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L27-L37) —— 表里清楚展示了"二进制码 → 整数补码 → 定点值"的换算。比如 `0111111111111111` 的补码是 32767，定点值就是 \(32767/2^8 \approx 127.996\)，这是该格式下的**正最大值**；而 `1000000000000000` 的补码是 -32768，定点值 \(-32768/2^8 = -128.0\)，是**负最小值**。这张表是理解后续一切溢出、饱和行为的钥匙。

#### 4.1.4 代码实践

**目标**：亲手验证"定点值 = 补码整数 ÷ 2^小数位宽"这个换算关系。

**步骤**：

1. 打开 README 的取值表（[README.md:L27-L37](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L27-L37)）。
2. 随便挑两行（比如 `0001010111000011 → 5571 → 21.76171875`），自己算一遍：\(5571 / 2^8 = 21.76171875\)。
3. 再挑一个负数行 `1001010110100110`，先把它当成 16 位补码还原成整数 -27226，再除以 \(2^8\)，应该得到 -106.3515625。

**预期结果**：你的手算结果与表中第三列完全一致。这就建立了"一串二进制码 = 一个带小数点的数"的直觉。

#### 4.1.5 小练习与答案

**练习 1**：在"8 位整数 + 8 位小数"格式下，二进制码 `0000000000000001` 代表的定点值是多少？它有什么特殊含义？

> **答案**：补码整数 = 1，定点值 = \(1/2^8 = 0.00390625\)。它是该格式下能表示的**正最小值**（即精度/分辨率）。

**练习 2**：如果把小数位宽从 8 提高到 16（整数位宽仍为 8），表示范围和精度分别会怎样变化？

> **答案**：整数位宽没变，所以**表示范围不变**；但小数位宽变大，分辨率变成 \(1/2^{16}\)，**精度变高**（能分辨更小的数）。

---

### 4.2 仓库目录结构

#### 4.2.1 概念说明

拿到一个新项目，第一件事永远是**搞清楚目录是怎么组织的**。这个仓库的结构极其简洁，全部文件可以一眼看尽。

#### 4.2.2 核心流程

仓库自顶向下只有三层：

```
FPGA-FixedPoint/
├── README.md            ← 项目说明（先读这个）
├── LICENSE              ← 开源协议
├── RTL/                 ← 可综合的电路代码（被测对象）
│   └── fixedpoint.v     ← 全部 13 个模块都在这一个文件里
└── SIM/                 ← 仿真代码与脚本（测试工具）
    ├── tb_add_sub_mul_div.v              ← 测试平台源码
    ├── tb_add_sub_mul_div_run_iverilog.bat ← 一键运行脚本
    ├── tb_fxp_mul_div_pipe.v + .bat      ← 流水线乘除的测试
    ├── tb_fxp_sqrt.v + .bat              ← 开方的测试
    └── tb_convert_fxp_float.v + .bat     ← 定点/浮点互转的测试
```

注意一个重要约定：**SIM 目录里每个 testbench 都配了一个同名的 `_run_iverilog.bat` 脚本**。你不用记复杂的编译命令，跑哪个测试就双击对应的 `.bat` 即可。

#### 4.2.3 源码精读

用 `git ls-files` 列出仓库跟踪的全部文件，正好印证上面的结构（共 11 个文件）：

```text
LICENSE
README.md
RTL/fixedpoint.v
SIM/tb_add_sub_mul_div.v
SIM/tb_add_sub_mul_div_run_iverilog.bat
SIM/tb_convert_fxp_float.v
SIM/tb_convert_fxp_float_run_iverilog.bat
SIM/tb_fxp_mul_div_pipe.v
SIM/tb_fxp_mul_div_pipe_run_iverilog.bat
SIM/tb_fxp_sqrt.v
SIM/tb_fxp_sqrt_run_iverilog.bat
```

README 也专门有一节讲仿真文件清单：

[README.md:L185-L194](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L185-L194) —— 这张中文表把 4 个 testbench 各自测什么列得清清楚楚：`tb_add_sub_mul_div.v` 测单周期加减乘除、`tb_fxp_mul_div_pipe.v` 测流水线乘除、`tb_fxp_sqrt.v` 测开方、`tb_convert_fxp_float.v` 浮点互转。

README 的"各模块名称与功能"表则把 RTL 里的模块和功能一一对应：

[README.md:L172-L181](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L172-L181) —— 可以看到每种运算都有"单周期版本"和（部分有）"流水线版本"，流水线级数也在表里。本讲只关心单周期的加减乘除四行。

#### 4.2.4 代码实践

**目标**：亲手确认目录结构，避免后面找不到文件。

**步骤**：

1. 在仓库根目录执行 `git ls-files`（或 `ls -R`），把文件清单打印出来。
2. 用编辑器打开 `RTL/fixedpoint.v`，搜索 `module ` 关键字，数一下共有多少个 `module`。
3. 确认 `SIM` 目录下是否每个 `.v` 测试平台都对应一个 `_run_iverilog.bat`。

**预期结果**：`RTL/fixedpoint.v` 里有 13 个模块（`fxp_zoom, fxp_add, fxp_addsub, fxp_mul, fxp_mul_pipe, fxp_div, fxp_div_pipe, fxp_sqrt, fxp_sqrt_pipe, fxp2float, fxp2float_pipe, float2fxp, float2fxp_pipe`，见文件头注释 [RTL/fixedpoint.v:L3](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L3)）；`SIM` 下 4 个 testbench 各配一个 `.bat`。

#### 4.2.5 小练习与答案

**练习 1**：你想验证"开方运算"是否正确，应该运行哪个脚本？

> **答案**：`SIM/tb_fxp_sqrt_run_iverilog.bat`（它编译并运行 `tb_fxp_sqrt.v`）。

**练习 2**：为什么全部可综合模块都塞在 `fixedpoint.v` 这一个文件里，而不是每个模块一个文件？

> **答案**：这是作者的有意取舍——这个库本身就是单一主题（定点运算）的紧凑集合，单文件便于阅读、便于被其他工程直接整文件包含（include）或编译。模块之间靠 Verilog 的模块名解析相互调用，不需要拆文件。

---

### 4.3 四个入门运算模块：fxp_add / fxp_addsub / fxp_mul / fxp_div

#### 4.3.1 概念说明

本讲的"最小模块"是四个单周期运算模块。先记住一个关键事实：**它们的参数（parameter）接口是统一的**。无论加减乘除，端口形状都一样：

- 输入两路操作数 `ina`、`inb`（除法里叫 `dividend`、`divisor`）；
- 输出一路结果 `out`；
- 输出一路溢出标志 `overflow`；
- 用一组 `parameter` 描述输入输出的整数位宽、小数位宽，以及是否舍入（`ROUND`）。

参数命名约定（全库统一，README 有详细说明 [README.md:L140-L144](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L140-L144)）：

| 参数 | 含义 |
| :--- | :--- |
| `WOI` / `WOF` | 输出的**整数**位宽 / **小数**位宽 |
| `WII` / `WIF` | （单目运算）输入的整数 / 小数位宽 |
| `WIIA`/`WIFA`、`WIIB`/`WIFB` | （双目运算）操作数 A、B 各自的整数 / 小数位宽 |
| `ROUND` | 截断时是否四舍五入（1=舍入，0=截断） |

#### 4.3.2 核心流程

这四个模块在本讲只需要建立一个"全景印象"，内部算法留到第 2 单元细讲：

- `fxp_add`：把两路输入对齐到统一位宽 → 做有符号加法 → 把和收敛回输出位宽（顺带检测溢出）。
- `fxp_addsub`：和 `fxp_add` 几乎一样，但多了一个 `sub` 控制位：`sub=0` 做加法、`sub=1` 做减法（把 `inb` 取反加一变成 `-inb` 再相加）。
- `fxp_mul`：直接做 `$signed(ina) * $signed(inb)` 得到全精度积，再把积收敛回输出位宽。
- `fxp_div`：用循环逐位试探商（"恢复余数法"思想），算完后再处理舍入和溢出。

> 一个贯穿全库的细节：**加减乘除都靠同一个底层模块 `fxp_zoom` 来"对齐位宽 / 截断舍入 / 检测溢出"**。`fxp_zoom` 是下一讲（u1-l3）的主角，本讲你只要知道"它在背后兜底"即可。

#### 4.3.3 源码精读

先看 `fxp_add` 的端口和核心逻辑：

[RTL/fixedpoint.v:L110-L132](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L110-L132) —— `module fxp_add` 的 parameter 列表（`WIIA/WIFA/WIIB/WIFB/WOI/WOF/ROUND`）、输入输出端口，以及最关键的一行：`wire signed [...] res = $signed(inaz) + $signed(inbz);`，即把对齐后的两路输入当成**有符号数**相加。注意它先用 `max` 求出公共位宽 `WII/WIF`，并把中间结果位宽 `WRI` 设成 `WII+1`（多 1 位用来容纳加法可能的进位，[RTL/fixedpoint.v:L125-L128](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L125-L128)）。

`fxp_addsub` 比 `fxp_add` 多了减法控制。看这一行：

[RTL/fixedpoint.v:L211](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L211) —— `wire [...] inbv = sub ? (~inbe)+ONE : inbe;`。当 `sub=1` 时把 `inb` 按位取反再加一（即求补码的相反数 `-inb`），然后送进加法器——这就是"用加法器做减法"的经典手法。

再看 `fxp_mul`，它最简洁：

[RTL/fixedpoint.v:L293-L308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L293-L308) —— 积的整数位宽 `WRI=WIIA+WIIB`、小数位宽 `WRF=WIFA+WIFB`（两个 N 位、M 位数相乘，积的位宽就是各部位宽之和），核心就一行 `res = $signed(ina) * $signed(inb)`，然后交给 `fxp_zoom` 收敛回 `WOI/WOF`。

最后是 `fxp_div`，它最复杂，本讲只看它的"取绝对值"和"逐位试探"骨架：

[RTL/fixedpoint.v:L427-L431](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L427-L431) —— 先算出结果符号 `sign = 被除数符号 ^ 除数符号`，再把被除数和除数都转成正数（`udividend`、`udivisor`），最后再按 `sign` 把符号补回去。

[RTL/fixedpoint.v:L459-L471](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L459-L471) —— 一个 `for(shamt=WOI-1; shamt>=-WOF; ...)` 循环，从高位到低位逐位试探商的每一位：若"当前余数 + 除数左移 shamt 位"仍不超过被除数，就把商的这一位置 1。这就是"恢复余数法"的核心。算法细节第 2 单元会专门讲。

#### 4.3.4 代码实践

**目标**：在 testbench 里观察这四个模块是如何被例化（instance）的。

**步骤**：

1. 打开 [SIM/tb_add_sub_mul_div.v:L29-L91](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L29-L91)。
2. 你会看到四个例化块：`fxp_add_i`、`fxp_addsub_i`、`fxp_mul_i`、`fxp_div_i`，它们**共用同一组输入 `ina`、`inb`**，各自把结果送到 `oadd/osub/omul/odiv` 和溢出标志 `oaddo/osubo/omulo/odivo`。
3. 注意 `fxp_addsub_i` 比其他三个多接了一根 `.sub(1'b1)`（[L56](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L56)），所以它输出的是减法结果 `osub`。

**预期结果**：你能指出每个例化块的"模块名 + 例化名 + 输出信号名"的对应关系，明白这个 testbench 是在**用同一份输入同时跑四种运算**。

#### 4.3.5 小练习与答案

**练习 1**：`fxp_add` 和 `fxp_addsub` 的端口有什么区别？

> **答案**：`fxp_addsub` 多了一个 1 位的输入 `sub`（[RTL/fixedpoint.v:L197](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L197)），`sub=0` 加、`sub=1` 减；`fxp_add` 没有这根线，只能做加法。

**练习 2**：为什么 `fxp_mul` 里积的位宽是 `WIIA+WIIB` 和 `WIFA+WIFB`，而不是和输入一样宽？

> **答案**：两个数相乘，结果的位数会增长。整数部分位数 = 两个整数位数之和，小数部分同理。所以必须先用更宽的位宽保存全精度积，再由 `fxp_zoom` 截断/舍入回目标位宽。

**练习 3**：`fxp_div` 为什么要先把操作数变成正数？

> **答案**：恢复余数法的逐位试探只在"正数 ÷ 正数"下逻辑最简单。所以先取绝对值算出"商的绝对值"，再用事先算好的 `sign` 把符号补回去，避免对负数做复杂的大小比较。

---

### 4.4 读懂 testbench：把定点码还原成浮点数的"软件参考"技巧

#### 4.4.1 概念说明

这是本讲**最重要**的一个套路，后面所有 testbench 都在用它。

硬件模块输入输出的是一串二进制码（定点码），但人脑很难直接看出 `0x0FFFFF` 代表多少。所以 testbench 在打印时，会做一个**反向换算**：把二进制码当成有符号整数，再除以 \(2^{\text{小数位宽}}\)，还原成一个我们能看懂的浮点数。

同时，testbench 还会用**软件（Verilog 表达式本身）**算一遍"正确答案"作为参考（SW-result），和**硬件模块的实际输出**（HW-result）并排打印，让你一眼看出对不对。

#### 4.4.2 核心流程

打印一行加法对比的过程：

1. 用 `$signed(ina)*1.0/(1<<WIFA)` 把输入 `ina` 还原成浮点值（`WIFA` 是 `ina` 的小数位宽）。
2. 用同样的式子还原 `inb`。
3. 把两者相加，得到**软件参考结果** SW-result。
4. 用 `$signed(oadd)*1.0/(1<<WOF)` 把硬件输出 `oadd` 还原成浮点值，得到 **硬件结果** HW-result。
5. 两者并排打印；若溢出标志 `oaddo` 为 1，则在 HW-result 后面追加 `(o)` 标记。

#### 4.4.3 源码精读

核心就在 `test` 这个 task 里的四条 `$display`。以加法那条为例：

[SIM/tb_add_sub_mul_div.v:L102-L108](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L102-L108) —— 这一行同时打印了输入 a、输入 b、SW-result、HW-result，以及溢出标记。拆开看里面的几个表达式：

- `( $signed(ina)*1.0)/(1<<WIFA)` ：`$signed(ina)` 把 `ina` 当成有符号整数；`*1.0` 把它转成实数（浮点）；`(1<<WIFA)` 就是 \(2^{WIFA}\)。三者组合 = `ina` 还原后的定点浮点值。
- SW-result = `(还原后的 a) + (还原后的 b)`，纯软件计算。
- HW-result = `( $signed(oadd)*1.0)/(1<<WOF)` ，把硬件输出 `oadd` 还原。
- `oaddo ? "(o)" : ""` ：溢出标志为真就打印 `(o)`。

减、乘、除三条 `$display`（[L109-L128](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L109-L128)）结构完全一样，只是中间运算符换成 `-`、`*`、`/`，输出信号换成 `osub/omul/odiv`、溢出标志换成 `osubo/omulo/odivo`。

`$signed` 是关键：如果不用它，Verilog 会把一个高位为 1 的码当成无符号大正数，负数就会打印错。`*1.0` 则是触发"整数转实数"的标准写法。

#### 4.4.4 代码实践

**目标**：手动模拟一遍 testbench 的换算，确认自己真的看懂了这个套路。

**步骤**：

1. 取一组 testbench 里的实际参数：`WIIA=10, WIFA=11`（`ina` 21 位）、`WIIB=8, WIFB=12`（`inb` 20 位）、`WOI=15, WOF=14`（输出 29 位）。这些 localparam 在 [SIM/tb_add_sub_mul_div.v:L16-L21](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L16-L21)。
2. 假设 `ina = 0x001000`（21 位正数，code = 1048576/16... 先别管），其实我们直接取一个简单值：令 `ina` 的码 = `2048`，那么还原值 = \(2048 / 2^{11} = 2048/2048 = 1.0\)。
3. 令 `inb` 的码 = `4096`，还原值 = \(4096 / 2^{12} = 4096/4096 = 1.0\)。
4. 那么加法的 SW-result 应该是 \(1.0 + 1.0 = 2.0\)。

**预期结果**：你能口算出"码 = 2048、小数位宽 = 11"对应定点值 1.0。这说明你已经掌握了 `$signed(x)*1.0/(1<<W)` 这个还原公式的含义。

#### 4.4.5 小练习与答案

**练习 1**：为什么公式里一定要有 `*1.0`？去掉会怎样？

> **答案**：`(1<<WIFA)` 是整数，`$signed(ina)` 也是整数，两个整数相除在 Verilog 里是**整数除法**（直接截断小数）。乘以 `1.0` 后，整个表达式被提升为**实数（real）运算**，才能保留小数部分。

**练习 2**：输出里的 `(o)` 是什么意思？它是怎么被打印出来的？

> **答案**：`(o)` 表示该运算**发生了溢出**，结果被饱和到了极值。它由 `oaddo ? "(o)" : ""`（[L107](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L107)）这样的三元表达式控制：对应运算的 `overflow` 信号为 1 时才追加。

---

### 4.5 跑通第一次仿真：iverilog 工作流

#### 4.5.1 概念说明

**iverilog**（Icarus Verilog）是一个开源的 Verilog 仿真器，能把 Verilog 代码编译并执行，把 `$display` 的内容打印到终端、把波形 dump 到文件。它是本库唯一的仿真工具，跨平台、免费。

#### 4.5.2 核心流程

跑一次仿真分三步（`.bat` 脚本已经帮你打包好了）：

1. **编译**：用 `iverilog -g2001 -o sim.out <testbench> <RTL>` 把 testbench 和被测 RTL 一起编译成一个可执行文件 `sim.out`。`-g2001` 表示使用 Verilog-2001 标准（本库就是按这个标准写的，见 git 提交 `change to Verilog2001`）。
2. **运行**：用 `vvp -n sim.out` 执行它，testbench 里的 `$display` 会把结果打印出来。
3. **看输出**：终端会出现成对的 SW-result / HW-result，以及可能的 `(o)` 标记。

#### 4.5.3 源码精读

一键脚本只有 5 行，非常透明：

[SIM/tb_add_sub_mul_div_run_iverilog.bat:L1-L5](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div_run_iverilog.bat#L1-L5) —— 第 1 行清理旧产物；第 2 行编译（注意它同时包含了 `tb_add_sub_mul_div.v` 和 `../RTL/fixedpoint.v`，前者是测试、后者是被测代码，**两个都要参与编译**）；第 3 行运行；第 4 行清理；第 5 行 `pause` 让窗口停住以便看输出。

testbench 里有几处和"跑起来"相关的细节也值得一看：

[SIM/tb_add_sub_mul_div.v:L9](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L9) —— `` `timescale 1ps/1ps `` 定义仿真时间单位和精度，`#10000` 这样的延时才有意义。

[SIM/tb_add_sub_mul_div.v:L13](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L13) —— `initial $dumpvars(0, tb_add_sub_mul_div);` 把整个 testbench 的所有信号变化导出成波形（默认写到 `dump.vcd`），可以用 GTKWave 打开看波形。

[SIM/tb_add_sub_mul_div.v:L134-L160](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L134-L160) —— `initial` 块里依次调用 `test(...)` 喂入 24 组测试向量，最后 `$finish` 结束仿真。每调用一次 `test`，就会触发一次 `#10000` 的延时、驱动输入、再 `#10000` 让组合逻辑稳定、然后打印四行对比结果。

#### 4.5.4 代码实践

**目标**：跑通第一次仿真，看到真实的 SW-result / HW-result 输出。

**步骤**：

1. 安装 iverilog（Windows 可下载安装包；Linux/macOS 可用包管理器，如 `apt install iverilog` 或 `brew install icarus-verilog`）。安装指南见 README 引用的 [iverilog_usage](https://github.com/WangXuan95/WangXuan95/blob/main/iverilog_usage/iverilog_usage.md)。
2. 进入 `SIM` 目录，双击（或在终端执行）`tb_add_sub_mul_div_run_iverilog.bat`。
3. 如果你不在 Windows，也可以手动等价执行那两行命令：
   ```bash
   cd SIM
   iverilog -g2001 -o sim.out tb_add_sub_mul_div.v ../RTL/fixedpoint.v
   vvp -n sim.out
   ```

**需要观察的现象**：终端会按每组测试向量打印四行（加、减、乘、除），每行包含输入 a、输入 b、SW-result、HW-result，以及可能的 `(o)` 溢出标记。正常情况下 HW-result 应当非常接近 SW-result（误差在 1 个最低位 LSB 以内）；当某次运算溢出时，HW-result 后面会出现 `(o)`。

**预期结果**：终端完整打印出 24 组向量的对比结果，`sim.out` 被生成后又删除，`dump.vcd` 留下波形文件。

> **待本地验证**：本讲义编写环境未安装 iverilog，以上为根据源码和 `.bat` 脚本推断的预期行为。请你在自己机器上实际运行一次，确认输出格式与上述一致。

#### 4.5.5 小练习与答案

**练习 1**：编译命令里为什么必须同时写上 `tb_add_sub_mul_div.v` 和 `../RTL/fixedpoint.v` 两个文件？

> **答案**：testbench 例化了 `fxp_add/fxp_addsub/fxp_mul/fxp_div`，而这些模块定义在 `RTL/fixedpoint.v` 里。iverilog 需要看到"测试代码 + 被测模块定义"才能完成顶层连接，缺一个都会报 "module not found" 错误。

**练习 2**：`-g2001` 这个选项的含义是什么？为什么本库要用它？

> **答案**：`-g2001` 指定按 IEEE 1364-2001（Verilog-2001）标准来编译。本库代码就是按 Verilog-2001 写的（仓库有专门的提交 `change to Verilog2001`，文件头注释也标注了 `Standard: Verilog 2001`），加上这个选项能保证语法被正确识别。

---

## 5. 综合实践

把本讲学到的"目录结构 + testbench 套路 + 跑仿真"串起来，完成下面这个任务（即本讲指定的代码实践）。

### 实践目标

跑通首次仿真；然后在 testbench 里**追加 3 组自定义测试向量**，其中至少一组能触发溢出，亲眼看到 `(o)` 标记。

### 操作步骤

1. **跑通首次仿真**：按 4.5.4 的步骤安装 iverilog 并运行 `tb_add_sub_mul_div_run_iverilog.bat`，确认终端能打印出 SW-result / HW-result 对比。

2. **理解测试向量的位宽**（关键，否则你设计的用例会踩坑）。根据 [SIM/tb_add_sub_mul_div.v:L16-L21](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L16-L21) 的 localparam：
   - `ina`：21 位（`WIIA=10` 整数 + `WIFA=11` 小数），取值范围约 \([-512,\ +512)\)。
   - `inb`：20 位（`WIIB=8` 整数 + `WIFB=12` 小数），取值范围约 \([-128,\ +128)\)。
   - 输出 `out`：29 位（`WOI=15` 整数 + `WOF=14` 小数），取值范围约 \([-16384,\ +16384)\)。

   因此：**加减法几乎不会溢出**（最大结果 ≈ 640，远小于 16384）；但**乘法和除法很容易溢出**（乘积最大 ≈ 512×128 = 65536 > 16384；除法当除数很小时结果会非常大）。

3. **在 `initial` 块末尾（`$finish` 之前）追加 3 组向量**，例如（示例代码，追加到 [SIM/tb_add_sub_mul_div.v:L159](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L159) 之前）：

   ```verilog
   // 示例代码：追加的自定义测试向量
   test('h0FFFFF, 'h7FFFF);   // ina≈+512, inb≈+128 → 乘积≈+65536，预期乘法 omul 溢出
   test('h100000, 'h80000);   // ina=-512,   inb=-128  → 乘积≈+65536，预期乘法 omul 溢出
   test('h0FFFFF, 'h000001);  // ina≈+512, inb≈1/4096 → 商≈+2e6，预期除法 odiv 溢出
   ```

   > 说明：`0x0FFFFF` = 21 位正最大码（值 ≈ +511.998）；`0x100000` = 21 位负最小码（值 = -512.0）；`0x7FFFF` = 20 位正最大码（值 ≈ +127.999）；`0x80000` = 20 位负最小码（值 = -128.0）；`0x000001` 作为 `inb` 时值 = \(1/2^{12} \approx 0.000244\)（极小正数）。

4. **重新仿真**，再次运行 `.bat` 脚本。

### 需要观察的现象

在新追加的三组输出里：

- 前两组的**乘法行**（`*`）后面应出现 `(o)`，且 HW-result 被钳位到正最大值（约 +16383.99），而 SW-result 是约 +65536——两者差异巨大，正是溢出的证据。
- 第三组的**除法行**（`/`）后面应出现 `(o)`，HW-result 钳位到正最大，而 SW-result 是一个极大的数。
- 加法、减法行**不应**出现 `(o)`（因为没超出输出范围）。

### 预期结果

你能在终端看到至少 3 处 `(o)` 标记（2 处乘法 + 1 处除法），并且对应的 HW-result 明显小于 SW-result（被饱和截断）。这就证明你已经：看懂了 testbench、能算出定点值、能预测溢出、能跑通仿真。

> **待本地验证**：上述向量对应的精确数值与是否一定触发溢出，依赖 iverilog 实际运行结果。请以你本机仿真输出为准；若某组未按预期出现 `(o)`，请用 4.4 的换算公式重新核算输入对应的定点值，调整向量直到观察到溢出。

---

## 6. 本讲小结

- **FPGA-FixedPoint** 是一个用 Verilog-2001 写的定点数运算库，支持加减乘除开方、溢出检测、舍入控制、IEEE754 浮点互转。
- 定点数值 = 二进制补码整数 ÷ \(2^{\text{小数位宽}}\)；整数位宽决定范围，小数位宽决定精度。
- 仓库结构极简：`RTL/fixedpoint.v` 一个文件装下全部 13 个可综合模块；`SIM/` 下每个 testbench 配一个 `_run_iverilog.bat` 一键脚本。
- 全库参数命名统一：`WOI/WOF`（输出）、`WIIA/WIFA/WIIB/WIFB`（双目输入）、`ROUND`（舍入）。
- 四个入门模块 `fxp_add / fxp_addsub / fxp_mul / fxp_div` 端口形状一致，内部都靠 `fxp_zoom` 来对齐位宽、舍入、检测溢出。
- testbench 的核心套路是 `$signed(x)*1.0/(1<<W)` 把定点码还原成浮点数，并排打印 SW-result（软件参考）与 HW-result（硬件结果），溢出时追加 `(o)`。

---

## 7. 下一步学习建议

恭喜你跑通了第一次仿真！接下来建议：

1. **先学下一讲 u1-l2《定点数格式与统一参数命名》**：系统地把 `WOI/WOF/WII/WIF/ROUND` 这套参数吃透，并练熟"浮点值 ↔ 二进制码"的手算换算——这是后面所有讲义的基础。
2. **再学 u1-l3《fxp_zoom：被全库复用的位宽变换核心》**：本讲多次提到的 `fxp_zoom` 是加减乘除背后真正的"打工模块"，理解了它的截断/舍入/溢出饱和，你才算真正理解了这个库。
3. 在进入第 2 单元的加减乘除算法细节之前，强烈建议你**先把本讲的仿真跑通、把 `(o)` 标记亲手复现出来**——有一个能跑的验证环境，后面改参数、做实验都会事半功倍。
4. 想提前建立全局印象的话，可以浏览 `RTL/fixedpoint.v` 文件头注释（[L1-L7](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L1-L7)）和 README 的模块清单表（[L172-L181](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L172-L181)），知道还有哪些模块在等着你。
