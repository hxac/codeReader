# 测试激励与黄金参考模型

## 1. 本讲目标

上一讲（u5-l1）我们已经把 testbench 当作"自动阅卷机"拆解过：它逐拍比对硬件输出 `dout` 与一份"黄金数据" `gold`，按 SNR 打分。但那份黄金数据从哪里来？输入激励又是怎么组织的？本讲就回答这两个问题。

学完本讲，你应当能够：

- 说清楚 `SIM/Test_cases/` 下 5 组输入/输出文本文件的命名约定、内部格式与位宽含义。
- 用 `SIM/FFT.py` 这个多级 radix-2 DIF 参考模型，从输入文本生成一组 FFT 输出，并理解它与硬件黄金数据的尺度关系。
- 读懂 C 参考模型 `SIM/FFT_test.c`（与 `SIM/FFT.c` 同源）里的 in-place 位反转 + 蝶形求值实现，并意识到它用的是 DIT、与 Python/硬件的 DIF 互补。
- 用"DC 分量 = 输入之和"这一条铁律，亲手验证黄金数据的正确性与尺度。

## 2. 前置知识

本讲默认你已经读过：

- **u2-l1 radix-2 DIF 算法原理**：知道 32 点 FFT 分 5 级、每级"加减分支 + 乘旋转因子"、输出天然乱序。
- **u5-l1 Testbench 与 SNR 验证方法**：知道 testbench 用 `in_valid`/`out_valid` 握手喂入/采出 32 个样本，并用 SNR≥40 dB（或噪声为 0）判定通过。

两个本讲要用到的基础概念：

- **黄金数据（golden）**：一份"标准答案"。对每一个输入样本，预先算好"正确硬件应当输出什么"，存成文本。仿真时拿硬件实际输出与它逐点比对。
- **定点尺度**：硬件内部把 12 位输入左移 8 位（×256）升到 24 位通路，末端再取高 16 位（÷256）回到整数尺度。一乘一除相消，**净尺度为 ×1**——所以 16 位输出约等于"真实 FFT 幅度"。这一点在本讲会用作验证手段。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `SIM/Test_cases/IN_real_pattern01.txt` | 第 1 组输入实部，32 行整数 | 输入激励样本 |
| `SIM/Test_cases/IN_imag_pattern01.txt` | 第 1 组输入虚部，32 行整数 | 输入激励样本 |
| `SIM/Test_cases/OUT_real_16_pattern01.txt` | 第 1 组黄金输出实部，32 行整数 | 黄金数据样本 |
| `SIM/Test_cases/OUT_imag_16_pattern01.txt` | 第 1 组黄金输出虚部，32 行整数 | 黄金数据样本 |
| `SIM/FFT.py` | radix-2 DIF 五级浮点参考模型 | 黄金数据生成器（算法原型） |
| `SIM/FFT_test.c` / `SIM/FFT.c` | in-place DIT FFT 库（含位反转 + 蝶形） | 算法级交叉参考 |

提示：`SIM/FFT.c` 与 `SIM/FFT_test.c` 内容几乎完全一致，后者仅比前者多 4 行结尾（3 个空行 + `#endif`）。本讲把它们视作同一份 C 参考模型。

## 4. 核心概念与源码讲解

### 4.1 输入/输出 pattern 文件格式

#### 4.1.1 概念说明

testbench 不在代码里硬编码 32 个样本，而是把每个数据集拆成 4 个纯文本文件：

- 输入实部 `IN_real_patternNN.txt`
- 输入虚部 `IN_imag_patternNN.txt`
- 黄金输出实部 `OUT_real_16_patternNN.txt`
- 黄金输出虚部 `OUT_imag_16_patternNN.txt`

其中 `NN` 是数据集编号（`01`~`05`）。每个文件**一行一个十进制整数、共 32 行**，分别对应 32 点 FFT 的 32 个复数样本（实部与虚部各存一份）。文件名里的 `_16` 表示输出按 16 位有符号数存放；输入文件名没有位宽后缀，但按设计规格是 12 位有符号数。

这样设计的好处是：换一组测试激励只要换文本文件，不必改 testbench；黄金数据也能用任何语言（Python/C/MATLAB）离线生成、版本管理。

#### 4.1.2 核心流程

一个数据集在仿真中的生命周期：

1. testbench 用 `$fopen` 打开 4 个文件。
2. 循环 32 次，每次从 `IN_real`/`IN_imag` 各读一行，拼成复数样本 `din_r/din_i` 喂给 DUT，同时拉高 `in_valid`。
3. 32 个样本喂完后，从 `OUT_real_16`/`OUT_imag_16` 读出对应黄金值，与硬件 `dout` 逐点比对、累计 SNR。
4. 关闭文件，进入下一个数据集 `NN`。

#### 4.1.3 源码精读

先看第 1 组输入实部的前几行（每行一个 12 位有符号整数）：

[SIM/Test_cases/IN_real_pattern01.txt:1-5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/IN_real_pattern01.txt#L1-L5) — 输入实部，第 1 个样本是 0，其余是带符号整数，列对齐用前导空格补齐。

逐行核对全部 32 个值，最大约 `2043`、最小约 `-1781`，正好落在 12 位有符号数范围 \([-2048,\,2047]\) 内——说明这组激励是按 12 位输入动态范围精心设计的。

再看第 1 组黄金输出实部的前几行：

[SIM/Test_cases/OUT_real_16_pattern01.txt:1-5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/OUT_real_16_pattern01.txt#L1-L5) — 黄金输出实部，第 1 个样本（频率索引 0，即 DC 分量）是 `-1365`。

输出全部 32 个值最大约 `12794`、最小约 `-7306`，落在 16 位有符号数范围 \([-32768,\,32767]\) 内，符合 16 位输出规格。

**一条可以亲手验证的铁律**：FFT 的 DC 分量（频率索引 0）等于所有输入样本之和。把 `IN_real_pattern01.txt` 的 32 个实部手算相加：

\[
X[0]_{real}=\sum_{n=0}^{31} x_r[n]=0-1700-1605+412+\cdots+1502=-1365
\]

结果恰好等于 `OUT_real_16_pattern01.txt` 第 1 行的 `-1365`。这一条同时验证了三件事：

- 黄金输出是**自然顺序**（第 1 行就是 DC，不是位反转后的某个值）。
- 黄金输出是**真实幅度尺度**（DC 是精确整数求和，没有任何缩放残留）——印证了 §2 说的"硬件 ×256 与 ÷256 相消、净 ×1"。
- 数据通路从输入文本到输出文本是自洽的。

#### 4.1.4 代码实践

1. **目标**：确认 5 组输入/输出文件的格式一致性、行数与位宽范围。
2. **步骤**：
   - 打开任意一个 `IN_real_patternNN.txt`，数一下行数是否为 32。
   - 用编辑器列选或脚本，统计该文件所有整数的最大/最小值。
   - 对 `OUT_real_16_patternNN.txt` 重复一遍。
3. **观察现象**：输入值落在 \([-2048,2047]\)，输出值落在 \([-32768,32767]\)。
4. **预期结果**：输入动态范围占满 12 位、输出动态范围未溢出 16 位，说明激励设计合理。
5. 若你用脚本自动统计，注意跳过空行与前导空格；若只是人工查看，待本地验证具体数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么输入文件名没有 `_12` 后缀、输出文件名却有 `_16`？

**参考答案**：输入位宽（12 位）由 DUT 端口 `din[11:0]` 隐式固定，文件名不必再标；输出位宽（16 位）则需要显式标注 `_16`，便于在多字长方案之间区分黄金数据文件（README 提到字长是可调的设计参数）。

**练习 2**：如果把某个 `IN_real_patternNN.txt` 误存成 31 行，仿真会怎样？

**参考答案**：testbench 按固定 32 拍喂入，少一行会导致读到文件尾（`$fscanf` 返回异常或读到 0），第 32 个样本被当成 0，从而 SNR 大幅下降、该组判定失败。

---

### 4.2 Python 参考模型生成黄金数据

#### 4.2.1 概念说明

`SIM/FFT.py` 是一个用纯 Python 写的 **radix-2 DIF 五级浮点 FFT**。它是本项目算法的"纸面原型"：读入一组文本输入，按 5 级蝶形算下去，最后做位反转还原，打印出 32 个频域结果。它的结构和硬件五级流水线一一对应，是理解"黄金数据从哪来"的最直接入口。

需要强调：`FFT.py` 用的是**浮点 + 量化旋转因子**（旋转因子只保留 5 位小数），而硬件 ROM 用的是**8 位小数定点**旋转因子。因此 `FFT.py` 是"算法参考"，它给出的结果与 16 位黄金数据在 DC 处精确相等、在其它频率上会有少量定点量化差异——这个差异正是 §4.2.4 实践要测量的对象。

#### 4.2.2 核心流程

`FFT.py` 的执行流程（与 u2-l1 讲过的 radix-2 DIF 完全同构）：

1. 从 `IN_real_pattern01.txt` / `IN_imag_pattern01.txt` 读入 32 个复数样本。
2. 硬编码 16 个旋转因子 `w[k]`、`w_i[k]`（即 \(W_{32}^k\) 的实部/虚部）。
3. 逐级 stage1→stage5：每级先算"和"（加法分支）与"差"（减法分支），再把"差"乘旋转因子（4 乘 2 加）。
4. stage5 之后做一次位反转，把乱序结果写回自然顺序的 `final_ans_r/final_ans_i`。
5. 打印各级中间结果（中间级 ×64 显示）与最终结果（原始幅度显示）。

#### 4.2.3 源码精读

读入两个输入文本（注意是裸文件名、相对当前工作目录解析）：

[SIM/FFT.py:36-42](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L36-L42) — 读入实部到列表 `mem`；下面紧接着 [SIM/FFT.py:44-50](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L44-L50) 读入虚部到 `img`。

旋转因子表（\(W_{32}^k=\cos(2\pi k/32)-j\sin(2\pi k/32)\) 的浮点近似）：

[SIM/FFT.py:51-61](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L51-L61) — 实部 `w[0..8]` 显式给出，`w[9..15]` 由对称性 `w[8+i] = -w[8-i]` 生成；[SIM/FFT.py:63-73](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L63-L73) 给出虚部 `w_i`。注意 `w_i` 全为负或零，对应 DFT 定义里的负指数。

stage1 的核心三段——加法分支、减法分支、乘旋转因子（4 乘 2 加）：

[SIM/FFT.py:76-86](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L76-L86) — 前 16 个样本是"和"（`mem[i]+mem[i+16]`，对应偶数频率），后 16 个是"差乘旋转因子"（`minus_r*w - minus_i*w_i` 等，对应奇数频率）。这正是 DIF 蝶形"先加减、后乘旋转因子"的特征。后续 stage2~5 结构完全一致，只是分组粒度逐级减半，例如 [SIM/FFT.py:103-114](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L103-L114) 是 stage2。

末尾的位反转还原（u2-l3 详细讲过）：

[SIM/FFT.py:189-196](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L189-L196) — 把 stage5 的乱序结果按下标 `r = int('{:05b}'.format(i)[::-1], 2)`（5 位反转）写回 `final_ans`，再以**原始幅度**打印（`int(final_ans_r[i])`，不再 ×64）。

> 关于显示尺度：stage1~5 的中间结果用 `int(x*64)` 打印（[SIM/FFT.py:88-89](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT.py#L88-L89) 等），仅是便于观察中间级的定点化幅度；最终 `final_ans` 用原始幅度打印，因此可以直接与 16 位黄金数据同尺度比较。

#### 4.2.4 代码实践

1. **目标**：用 `FFT.py` 生成第 1 组输出，与 `OUT_real_16_pattern01.txt` 比对，统计最大误差。
2. **步骤**：
   ```bash
   cd SIM/Test_cases
   python3 ../FFT.py > my_out.txt
   ```
   （`FFT.py` 用裸文件名 `open("IN_real_pattern01.txt")`，故必须在 `Test_cases/` 目录下运行，让它能找到输入文件。）
3. **观察现象**：
   - 标准输出先打印 stage1~5（×64 尺度），最后一段 `final answers` 是 32 行 `i : <real> <imag> i`（原始尺度）。
   - 抽取 `final answers` 的 32 个实部，逐行减去 `OUT_real_16_pattern01.txt` 的对应值，取绝对值最大者。
4. **预期结果**：
   - DC（`i=0`）一行的实部应当**精确等于** `-1365`（与黄金数据第 1 行一致，因为 DC 是纯加法、无旋转因子量化误差）。
   - 其它频率会出现小幅误差（几个 LSB 量级），来源是 `FFT.py` 的 5 位小数旋转因子 vs 硬件 ROM 的 8 位定点旋转因子。
5. **判断**：若最大误差在几个 LSB 以内，则属定点量化允许范围；这一步的具体数值**待本地验证**（取决于你提取脚本是否去掉了 `i : ` 前缀和 ` i` 后缀）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 DC 分量在 `FFT.py` 与黄金数据之间一定精确相等？

**参考答案**：DC（频率索引 0）一路走"加法分支"，5 级都不乘旋转因子（或乘 1），等价于把所有输入求和。求和是精确整数运算，没有浮点旋转因子介入，所以两边都精确得到 \(\sum x[n]\)。

**练习 2**：如果把 `FFT.py` 第 191 行的位反转注释掉，`final_ans` 会怎样？

**参考答案**：`final_ans[i] = stage5[i]`，输出将保持 DIF 天然的位反转乱序，第 0 行不再是 DC 而是某个高频分量，与黄金数据顺序错位、SNR 极差。

---

### 4.3 C 语言 in-place FFT 参考

#### 4.3.1 概念说明

`SIM/FFT_test.c`（与同源的 `SIM/FFT.c`）是一份 **in-place（原地）FFT 库实现**，提供与 Python 不同的算法视角。关键区别：

- `FFT.py` / 硬件用的是 **DIF（频率抽取）**：自然顺序输入 → 位反转输出。
- `FFT_test.c` 用的是 **DIT（时间抽取）**：先做位反转置换（shuffle），再做蝶形求值（evaluate）→ 自然顺序输出。

两者算的是同一个 DFT，殊途同归，因此 C 模型可以对 Python/硬件结果做算法级交叉校验。

> 诚实说明：本仓库**没有**提供 `fft.h` 头文件，C 文件里也没有 `main()` 函数。也就是说，这份 C 模型**不能直接从仓库编译运行**——它缺 `complex_f`/`complex_d` 结构体、`fft_dir`/`FFT_FORWARD` 常量以及 `complex_mul_re/im` 辅助函数的定义，也没有读文本、调 FFT、打印结果的驱动代码。它的定位是"标准 in-place FFT 算法的可读参考"，而不是可一键跑通的黄金生成器。本讲把它当算法参考来读。

#### 4.3.2 核心流程

`FFT_test.c` 的入口 `ffti_f` 把整个过程拆成两步：

\[
\text{ffti\_f}(\text{data},\,\log_2 N,\,\text{dir}) = \text{shuffle}(\text{data}) \;\triangleright\; \text{evaluate}(\text{data})
\]

1. **位反转置换 `ffti_shuffle_f`**：用一种"同步计数器增量"技巧，原地交换元素，把数组重排成位反转顺序。
2. **蝶形求值 `ffti_evaluate_f`**：对 \(r=1\ldots \log_2 N\) 共 \(\log_2 N\) 级，每级用旋转因子 \(W_m^k\) 做 DIT 蝶形（上支 \(u+t\)、下支 \(u-t\)）。

#### 4.3.3 源码精读

入口函数，先 shuffle 后 evaluate：

[SIM/FFT_test.c:14-18](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_test.c#L14-L18) — in-place FFT 的两阶段骨架：`ffti_shuffle_f` 做位反转置换，`ffti_evaluate_f` 做蝶形求值。

位反转置换的核心技巧——用"最低有效零位"递推下一个位反转下标：

[SIM/FFT_test.c:97-131](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_test.c#L97-L131) — `lszb=~i & (i+1)` 找 `i` 的最低有效零位，`mszb=Nd2/lszb` 把它"位反转"成最高位侧，再用 `bits = Nm1 & ~(mszb-1); j ^= bits;` 翻转相应高位，得到下一个目标下标 `j`。这避免了每次都做完整 5 位反转，等价于一个位反转计数器。`if (j > i)` 时才交换，保证每对只换一次。

DIT 蝶形求值的核心循环：

[SIM/FFT_test.c:182-201](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_test.c#L182-L201) — 每个蝶形：`u=data[i_e]`（上支），`t = Wmk * data[i_o]`（下支乘旋转因子），然后 `data[i_e]=u+t`、`data[i_o]=u-t`。注意它把**下支**乘旋转因子再做加减——这是 DIT 的特征（对照 DIF 是"先加减、后乘旋转因子"）。`Wmk` 通过 `Wmk = Wmk * Wm` 递推，避免每拍重算三角函数。

此外文件还提供了递归版 `fftr_f`（[SIM/FFT_test.c:212-288](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_test.c#L212-L288)），同样属 DIT，可作交叉参考。

#### 4.3.4 代码实践（源码阅读型）

由于缺 `fft.h` 与 `main()`，本实践为源码阅读型。

1. **目标**：追踪 C 模型对一个 8 点序列做位反转后的下标序列。
2. **步骤**：
   - 取 \(N=8\)、\(\log_2 N=3\)，手算 `ffti_shuffle_f` 里 `i=0..7` 时的 `j` 序列：从 `j=0` 起，逐步套用 `lszb→mszb→bits→j^=bits`。
3. **观察现象**：`j` 的取值序列应形如 `0,4,2,6,1,5,3,7`（即 3 位反转下标的递进序）。
4. **预期结果**：这正是 8 点 DIT 位反转置换后的下标顺序，与 u2-l3 的位反转概念一致（只是这里用它来**置换输入**，而 DIF 用它来**整理输出**）。
5. 具体每一位的推导**待本地验证**（建议在纸上画 3 位二进制辅助）。

#### 4.3.5 小练习与答案

**练习 1**：DIT 与 DIF 在蝶形上最直观的区别是什么？

**参考答案**：DIT 蝶形是"下支先乘旋转因子，再加减"（`u+t, u-t`，t 含旋转因子）；DIF 蝶形是"先加减，差支再乘旋转因子"。等价但数据流方向相反。

**练习 2**：为什么说 `FFT_test.c` 不能直接编译？

**参考答案**：它 `#include "fft.h"`，但仓库里没有这个头文件，缺少 `complex_f`/`fft_dir` 等类型定义；同时没有 `main()`，无法链接成可执行程序。需要自行补齐头文件与驱动才能运行。

---

### 4.4 数据集一致性

#### 4.4.1 概念说明

`SIM/Test_cases/` 下共有 **5 组**完整数据集（`pattern01`~`pattern05`），每组 4 个文件（输入实/虚部、输出实/虚部），共 20 个文本文件。5 组输入对应不同频率成分的信号（直流、单频正弦、多频混合等），目的是从多个角度"考"硬件：单一频率看谱线位置、多频率看动态范围、不同幅度看是否溢出。

"一致性"指：5 组数据集必须遵守同一套约定——同样的 32 行格式、同样的位宽（输入 12 位、输出 16 位）、同样的自然顺序、同样的真实幅度尺度，testbench 才能用同一个循环无差别地处理它们。

#### 4.4.2 核心流程

testbench 的数据集循环（u5-l1 已详述）外层 `for(i=0..4)` 切换文件名，内层固定 32 拍喂入/采出。一致性保证了：

\[
\forall\, NN\in\{01..05\}:\quad \text{格式}(\text{pattern}NN)=\text{格式}(\text{pattern}01)
\]

只要任意一组违反约定（行数不对、尺度不符、顺序错位），该组 SNR 就会失败，从而被一眼定位。

#### 4.4.3 源码精读

5 组输入实部文件的命名一致性（首组与末组）：

[SIM/Test_cases/IN_real_pattern01.txt:1-1](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/IN_real_pattern01.txt#L1-L1) 与 [SIM/Test_cases/IN_real_pattern05.txt:1-1](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/IN_real_pattern05.txt#L1-L1) — 两组输入实部文件首行格式完全一致（前导空格对齐的带符号整数）。

对应的黄金输出实部：

[SIM/Test_cases/OUT_real_16_pattern01.txt:1-1](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/OUT_real_16_pattern01.txt#L1-L1) 与 [SIM/Test_cases/OUT_real_16_pattern05.txt:1-1](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/OUT_real_16_pattern05.txt#L1-L1) — 两组黄金输出实部同样遵守 16 位、自然顺序、真实幅度的同一约定。

DC 铁律在 5 组上都应成立：对任意 `NN`，`OUT_real_16_patternNN.txt` 第 1 行都应等于 `IN_real_patternNN.txt` 的 32 行之和。这是检验每组数据自洽的最快方法。

#### 4.4.4 代码实践

1. **目标**：批量验证 5 组数据集的格式与 DC 自洽性。
2. **步骤**：
   - 对 `NN=01..05`，分别统计 `IN_real_patternNN.txt` 与 `OUT_real_16_patternNN.txt` 的行数与极值。
   - 对每组，手算或脚本计算 `sum(IN_real_patternNN)`，与 `OUT_real_16_patternNN.txt` 第 1 行比对。
3. **观察现象**：每组输入均为 32 行、范围在 12 位内；输出均为 32 行、范围在 16 位内；DC 项精确等于输入之和。
4. **预期结果**：5 组全部满足一致性约定，DC 铁律在每组上都成立。
5. 具体每组的 DC 数值**待本地验证**（第 1 组已验证为 `-1365`）。

#### 4.4.5 小练习与答案

**练习 1**：如果第 3 组的 `OUT_real_16_pattern03.txt` 第 1 行不等于其输入之和，可能是什么问题？

**参考答案**：最可能是该组黄金数据生成时出错（如旋转因子表错配、位反转遗漏、或尺度没对齐）；也可能是该文件被误编辑。由于 DC 不依赖旋转因子，它是最敏感的自洽性哨兵。

**练习 2**：为什么需要 5 组、而不是 1 组测试激励？

**参考答案**：单组激励只能覆盖少数频率成分，容易"恰好通过"。多组激励覆盖直流、单频、多频、不同幅度与符号模式，能更全面地暴露蝶形、旋转因子、位反转、定点溢出等环节的隐藏错误。

---

## 5. 综合实践

把本讲四条主线串成一个端到端的小任务：**用 Python 参考模型复算一组 FFT，并自洽性校验黄金数据**。

1. **准备**：进入 `SIM/Test_cases/`，确认 5 组共 20 个文件齐全。
2. **手算校验**：选第 1 组，把 `IN_real_pattern01.txt` 的 32 个实部相加，确认等于 `OUT_real_16_pattern01.txt` 第 1 行 `-1365`（DC 铁律）。
3. **模型复算**：运行 `python3 ../FFT.py`，从输出末尾的 `final answers` 段提取 32 个实部。
4. **误差统计**：与 `OUT_real_16_pattern01.txt` 逐行相减，记录最大绝对误差与出现位置；判断是否在定点量化允许范围（几个 LSB）。
5. **算法交叉**：阅读 `FFT_test.c` 的 `ffti_shuffle_f` + `ffti_evaluate_f`，说明它用 DIT 也能得到同一个 DC（输入之和），从而与 DIF 的 `FFT.py` 互相印证。
6. **结论**：写一段话，回答——黄金数据的尺度是什么？Python 浮点模型与 16 位黄金数据的差异主因是什么？DC 为什么在所有模型间都精确相等？

> 备注：若你在本地用 Verilog 仿真器跑过 testbench，可进一步把硬件 `dout` 与黄金数据比对，预期 SNR 为 `infinity`（噪声为 0），即硬件逐位复现黄金数据——这正是黄金数据被设计成"硬件位精确参考"的体现。

## 6. 本讲小结

- `SIM/Test_cases/` 下 5 组数据集，每组 4 个文本文件（输入实/虚部、输出实/虚部），每文件 32 行十进制整数，输入 12 位、输出 16 位有符号。
- 黄金输出是**自然顺序、真实幅度尺度**——已用"DC = 输入之和 = `-1365`"亲手验证（硬件 ×256/÷256 相消，净 ×1）。
- `SIM/FFT.py` 是 radix-2 DIF 五级浮点参考模型，读入文本、5 级蝶形 + 位反转还原后以原始幅度打印，是黄金数据的算法原型。
- `FFT.py` 与 16 位黄金数据在 DC 处精确相等、在其它频率有少量定点量化差异（5 位小数 vs 8 位定点旋转因子）。
- `SIM/FFT_test.c`/`FFT.c` 是 in-place **DIT** FFT 库（先位反转置换、再蝶形求值），与 DIF 的 Python/硬件殊途同归，提供算法级交叉校验；但仓库缺 `fft.h` 与 `main()`，不可直接编译运行。
- 5 组数据集遵守同一格式约定，DC 铁律是检验每组自洽的最快哨兵。

## 7. 下一步学习建议

- 本讲聚焦"数据从哪来"，下一步建议回到硬件主线：阅读 **u3-l1（顶层 FFT 模块结构）** 与 **u4-l1（五级流水线数据流串讲）**，对照黄金数据看一遍一个 32 点样本从 `din` 到 `dout` 的完整通路。
- 若你对算法细节仍想深挖，可重温 **u2-l1（radix-2 DIF 原理）** 与 **u2-l3（位反转）**，把 `FFT.py` 的五级结构与硬件五级流水线一一对应。
- 若你关心黄金数据如何被 ASIC 流程复用，可预习 **u6-l1（综合流程）**：综合后产出的门级网表 + SDF 会用同一套黄金数据做门级仿真（GATE 模式），验证时序签核后功能仍然正确。
