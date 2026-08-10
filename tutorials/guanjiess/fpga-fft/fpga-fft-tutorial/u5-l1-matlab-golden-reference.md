# MATLAB 黄金参考：DIF/DIT 迭代实现与中间结果

## 1. 本讲目标

本讲是「验证、仿真与平台移植」单元的第一篇。前面几个单元我们把硬件流水线从 `fft_16k` 一路拆到 `fft_2`，已经知道它输出的是 **定点、且按 bit-reverse 倒序** 的结果。问题是：**怎么证明这一大串硬件算对了？**

答案就是本讲的主题——**用 MATLAB 写一份「黄金参考（golden reference）」**：一份我们完全信任的、浮点双精度的 FFT 实现，拿来逐点、逐级地和硬件输出比对。

学完本讲你应当能够：

1. 读懂 `matlab/` 下四个脚本，理解它们各自扮演的角色（O(N²) 的 DFT 基线、迭代 DIF、迭代 DIT、误差校验）。
2. 把 MATLAB 迭代 FFT 的 **外层 `for level` 循环** 一一对应到硬件流水线的 **逐级 stage**，理解「软件一次外层循环 = 硬件一级 fft_N 模块」。
3. 看懂 `X_FFT_middle_result` 这个「逐级中间结果矩阵」是如何保存每一级状态的，并能挑出其中某一行，作为后续与硬件某一级（如 `fft_8`）仿真输出逐拍比对的黄金数据。
4. 区分 DIF（先蝶形后倒序）与 DIT（先倒序后蝶形），并掌握用 MATLAB 内置 `fft()` 做误差校验的方法。

---

## 2. 前置知识

本讲建立在 **u1-l3（算法基础）** 之上，那里已经推导过 DFT 定义、旋转因子、Cooley-Tukey 分治与 bit-reverse。这里只做最小回顾，并把概念接到「验证」这件事上。

- **DFT 定义**：\( X(k)=\sum_{n=0}^{N-1} x(n)\,W_N^{\,nk} \)，其中旋转因子 \( W_N=e^{-j2\pi/N} \)。直接按定义算是 \( O(N^2) \)。
- **DIF 与 DIT**：两种等价的 Cooley-Tukey 迭代路线。
  - **DIF（Decimation In Frequency，频率抽取）**：先做蝶形（和/差），再做 bit-reverse 倒序。**本项目的硬件走的就是 DIF 路线**（见 u1-l4）。
  - **DIT（Decimation In Time，时域抽取）**：先把输入 bit-reverse 倒序，再做蝶形。
- **bit-reverse 倒序**：把下标的二进制位反转重排，例如 N=4 时下标 \(0,1,2,3\)（二进制 00,01,10,11）倒序后变成 \(0,2,1,3\)（00,10,01,11）。
- **为什么要黄金参考**：硬件用定点数（Q1.16，见 u2-l3）、且输出被 SDF 流水线和倒序打乱，光看波形很难判断「算对没有」。我们需要一份**浮点、自然序、可信**的参考结果作为标尺。
- **MATLAB 下标从 1 开始**：而本讲脚本里 `index = 0:N-1` 用的是 0 基下标，所以倒序后会出现 `+1` 的修正，这是 MATLAB 验证脚本的常见套路。

---

## 3. 本讲源码地图

本讲涉及的关键文件都在 `matlab/` 目录下：

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `matlab/DFT_original.m` | 函数式 DFT 基线 | 用矩阵乘法一次性算 \( y = W\cdot x \)，\( O(N^2) \)，但**严格贴合 DFT 定义**，是最干净的「真值」。 |
| `matlab/fft_testing.m` | 双重循环 DFT 基线 | 用 `for k / for n` 两层循环朴素实现 DFT，并与内置 `fft()` 比对误差。 |
| `matlab/FFT_iterative_DIF.m` | **迭代 DIF（本讲主角）** | 逐级蝶形 + 最后倒序，逐级把中间结果存进 `X_FFT_middle_result`。**这是硬件流水线的直接黄金参考。** |
| `matlab/FFT_iterative_DIT.m` | 迭代 DIT（镜像对照） | 先倒序再逐级蝶形，`N=16384` 与硬件满点数一致，末尾有与内置 `fft()` 的误差校验。 |
| `matlab/FFT_figures.m` | 辅助可视化 | 画出 N=16 的旋转因子在单位圆上的分布，帮助直观理解。 |

> 提示：`matlab/rotators/` 子目录当前为空，旋转因子的硬件量化值（如 `cos45°→46341`）在 `src/RotatorMemory8.v` 等处，已在 u2-l3 / u3-l1 讲过，本讲不再重复。

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要「黄金参考」：从 DFT 原始定义出发

#### 4.1.1 概念说明

「黄金参考」指的是一个**我们假设它永远正确**的参照实现。在验证硬件 FFT 时，它的地位类似「标准答案」：

- 它应当**严格贴合数学定义**，不引入任何优化带来的额外假设；
- 它最好用**浮点双精度**，这样它的误差只来自浮点舍入（约 \(10^{-12}\) 量级），远小于硬件定点量化误差；
- 它要足够**简单可读**，让人一眼相信它没错。

最贴合「严格定义」的，就是直接按 \( X(k)=\sum_n x(n)W^{nk} \) 算的 O(N²) DFT。它慢，但慢得理直气壮——因为它是定义本身。`DFT_original.m` 和 `fft_testing.m` 就是两个这样的基线，它们存在的意义不是「跑得快」，而是「让 FFT 这种优化算法有一个可对标的真值」。

#### 4.1.2 核心流程

把 DFT 写成矩阵形式，定义矩阵 \( W \)，其第 \( (k,n) \) 个元素为 \( W_N^{\,nk} \)：

\[
X = W\cdot x,\qquad W_{k,n}=W_N^{\,nk}=e^{-j2\pi nk/N}
\]

于是整个 DFT 就是一次 \( N\times N \) 矩阵乘向量，复杂度 \( O(N^2) \)。这正是 `DFT_original.m` 的写法。而 `fft_testing.m` 则把同一个式子拆成两层 `for` 循环逐项累加，本质完全相同。两者算出来的结果，都应当与 MATLAB 内置 `fft()` 在浮点误差内一致。

#### 4.1.3 源码精读

`DFT_original.m` 用矩阵乘一次算完，关键是构造那个 \( N\times N \) 的旋转因子矩阵：

```matlab
N = length(source);
Wn = exp(-1j* 2 * pi / N);
n = 0 : N-1;            k = n';
power_coefficient = k * n;          % N×N 矩阵，元素 = k*n
W = Wn^(power_coefficient);         % 旋转因子矩阵 W_N^(nk)
y = W * source;                     % 一次矩阵乘 = 整个 DFT
```

详见 [matlab/DFT_original.m:L1-L10](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/DFT_original.m#L1-L10)，这段用 `k*n` 的外积一举生成全部 \( nk \) 幂次，再 `W*source` 得到 DFT，写法非常紧凑。

`fft_testing.m` 则是朴素的「定义直译」式双循环，并自带一次误差校验：

```matlab
for k=1:N
    for n=1:N
        X1(k) = X1(k) + x1(n)*Wn^(n*k);   % 逐项累加 X(k)=Σ x(n)W^(nk)
    end
end
X1_fft = fft(x1);                          % MATLAB 内置 fft 当标尺
Deviation = X1_fft - X1;                    % 逐点差
Total_Deviation = sum(Deviation.^2);        % 平方误差总和
```

详见 [matlab/fft_testing.m:L11-L18](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/fft_testing.m#L11-L18)。`Total_Deviation` 理论上只剩浮点舍入残差（极小），这正是「黄金参考成立」的标志。

#### 4.1.4 代码实践

1. **实践目标**：确认 O(N²) DFT 基线本身可信，可作为后续黄金参考的真值来源。
2. **操作步骤**：在 MATLAB 中打开 `matlab/fft_testing.m`，直接运行（无需改参数，`N=100`）。
3. **观察现象**：在工作区查看 `Total_Deviation` 的数值。
4. **预期结果**：`Total_Deviation` 应在 \(10^{-20}\sim10^{-24}\) 量级（纯浮点舍入），说明这份朴素 DFT 与内置 `fft()` 完全一致。
5. 若结果偏大：检查是否误改了 `Wn` 的符号（应为 \(e^{-j2\pi/N}\)，负号不能丢）。

#### 4.1.5 小练习与答案

**练习**：把 `fft_testing.m` 里的 `N` 从 100 改成 1024，运行时间会怎样变化？`Total_Deviation` 量级会怎样变化？

**参考答案**：运行时间约放大 \( (1024/100)^2\approx 105 \) 倍（DFT 是 \( O(N^2) \)），会明显变慢；`Total_Deviation` 仍停留在浮点舍入量级，因为误差来源只是浮点累加，与 N 无关——这正说明 O(N²) DFT 仍是可信真值，只是太慢。

---

### 4.2 迭代 DIF：双层 for 循环 = 硬件逐级流水

#### 4.2.1 概念说明

`FFT_iterative_DIF.m` 是本讲的**主角**，因为硬件流水线走的就是 DIF。它的核心思想可以用一句话概括：

> **MATLAB 的外层 `for level` 循环，每迭代一次，就对应硬件流水线上的一级 `fft_N` 模块。**

软件用一个串行的外层循环「逐级」处理数据；硬件则把每一级做成一个独立电路，让数据「流」过这一串电路。两者在数学上完全等价，只是一个是时间上串行、一个是空间上流水。理解这个对应关系，是把 MATLAB 当硬件黄金参考的关键。

DIF 的流程是**先蝶形、后倒序**：输入是自然序，经过 `log₂N` 级蝶形后，输出是 bit-reverse 倒序，最后再倒序一次还原成自然序。

#### 4.2.2 核心流程

记 `levels = log₂N`。DIF 的外层循环变量 `level` 从 0 到 `levels-1`，每一级的关键量是：

\[
\text{len} = 2^{\,\text{levels}-\text{level}},\qquad \text{mid}=\text{len}/2,\qquad W_{\text{len}}=e^{-j2\pi/\text{len}}
\]

注意 `len` 随 `level` 增大而**减半**：第 0 级 `len=N`（蝶形跨度最大），最后一级 `len=2`（跨度最小）。每一级对每个长度为 `len` 的小段做一次 DIF 蝶形：

\[
A = x(p+k) + x(p+k+\text{mid})
\]
\[
B = \bigl(x(p+k) - x(p+k+\text{mid})\bigr)\cdot W_{\text{len}}^{\,k}
\]

即「先加减、再乘旋转因子」——这正是 DIF，也正是硬件「蝶形 `butterfly.v` 先出和/差、再用 `multiplier.v` 乘旋转因子」的顺序（见 u2-l1、u2-l2）。

**MATLAB 与硬件的级对应关系**（以硬件满配置 N=16384、`levels=14` 为例）：

| MATLAB DIF `level` | `len=2^(levels-level)` | 硬件对应级（流水线位置） | `X_FFT_middle_result` 行号 |
| :--: | :--: | :--: | :--: |
| （蝶形前） | — | （输入端） | 第 1 行 |
| 0 | 16384 | `fft_16k`（第一级） | 第 2 行 |
| 1 | 8192 | `fft_8k` | 第 3 行 |
| … | … | … | … |
| 11 | 8 | `fft_8` | 第 13 行 |
| … | … | … | … |
| 13 | 2 | `fft_2`（最后一级） | 第 15 行 |

读法：**最大的 `len`（=N）对应流水线最前面的 `fft_16k`，最小的 `len`（=2）对应最后的 `fft_2`**，与硬件「大点数层在前、小点数层在后」的数据流方向（见 u1-l4）完全吻合。

#### 4.2.3 源码精读

参数与信号生成段：采样率 1000Hz，1024 点，信号是 50Hz+120Hz 正弦加白噪声。详见 [matlab/FFT_iterative_DIF.m:L8-L14](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L8-L14) 的参数定义与第 16~18 行的信号生成。

逐级蝶形的核心是这一段双层循环：

```matlab
levels              = log2(N);
X_FFT_middle_result = zeros(levels+1, N);
X_FFT_middle_result(1,:) = x;                 % 第 1 行 = 原始输入
for level = 0:levels-1                        % 外层：每一级 = 一级硬件 stage
    len = 2^(levels-level);
    mid = len / 2;
    Wn  = exp(-1j * 2 * pi / len);
    for pos = 1:len:N                         % 内层 1：N/len 个小段
        for k = 0:mid-1                       % 内层 2：每段做 mid 个蝶形
            A = x(pos+k) + x(pos+k+mid);                  % 上支：和
            B = (x(pos+k) - x(pos+k+mid))*Wn^(k);         % 下支：差 × 旋转因子
            x(pos+k)     = A;
            x(pos+k+mid) = B;
        end
    end
    X_FFT_middle_result(level+2,:) = x;       % 保存这一级结束后的整段状态
end
```

详见 [matlab/FFT_iterative_DIF.m:L32-L45](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L32-L45)。三点要点：

1. `A = 和`、`B = (差)×Wn^k`，对应硬件蝶形上支 D（直送下一级）与下支 B（乘旋转因子后再送出）。
2. `x` 是**原地（in-place）**更新的，所以每一级结束后 `x` 就是「这一级的输出 / 下一级的输入」。
3. `X_FFT_middle_result(level+2,:) = x` 把每级结束的状态整行保存，行号 = `level+2`（第 1 行留给了原始输入）。

最后是倒序还原：

```matlab
normal_index  = index;                    % 0:N-1
reversed_index = bitrevorder(normal_index); % bit-reverse 重排下标
X_FFT(reversed_index+1) = x;              % 把倒序的 x 放回自然序位置
```

详见 [matlab/FFT_iterative_DIF.m:L47-L52](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L47-L52)。注意 `+1` 是因为 MATLAB 下标从 1 开始，而 `reversed_index` 是 0 基的。`bitrevorder` 是 MATLAB 内置函数，返回下标的二进制位反转排列。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「外层循环逐级收敛」的过程，并理解每一级输出的频谱含义。
2. **操作步骤**：打开 `matlab/FFT_iterative_DIF.m`，直接运行（默认 `N=1024`）。脚本会画三幅子图：含噪时域信号、DIF 频谱。
3. **观察现象**：第二幅子图应在 50Hz 和 120Hz 处出现两个明显尖峰。
4. **预期结果**：频谱峰值位置精确落在 50Hz、120Hz，幅值约为对应正弦幅度的 \(N/2\) 倍量级（受噪声与窗影响略有偏差）。
5. **进阶**：在第 44 行后加一句 `disp(level)`，观察外层循环确实跑了 `log2(N)=10` 次，与 1024 点需要 10 级蝶形一致。

#### 4.2.5 小练习与答案

**练习**：对照本节给的「级对应表」，要找硬件 `fft_8`（`len=8`）这一级的黄金参考，应当看 `X_FFT_middle_result` 的第几行（在 `N=1024` 的脚本里）？

**参考答案**：`len=8 ⇒ levels-level=3 ⇒ level=levels-3=10-3=7`，对应行号 `level+2=9`。即 `X_FFT_middle_result(9,:)` 是 `fft_8` 这一级输出状态的黄金参考。

---

### 4.3 X_FFT_middle_result：逐级中间结果与硬件逐拍比对

#### 4.3.1 概念说明

`X_FFT_middle_result` 是一个 `(levels+1) × N` 的矩阵，是这套脚本最有验证价值的产物。它的每一行就是「某一时刻整条数据的状态快照」：

- 第 1 行：原始输入 `x`（自然序）。
- 第 `level+2` 行：经过第 `level` 级蝶形后的整段数据。
- 第 `levels+1` 行（最后一行）：全部蝶形完成、但**尚未做最终倒序**的状态——也就是 **bit-reverse 倒序的最终结果**。

最后这一点特别重要：**硬件流水线的最终输出（在用户外部做倒序重排之前）正是 bit-reverse 倒序的**（见 u1-l4、u5-l4）。所以 `X_FFT_middle_result` 的**最后一行**与硬件 `fft_top` 的原始（未倒序）输出在「值的集合」上应当一一对应。

#### 4.3.2 核心流程

用 `X_FFT_middle_result` 做硬件比对的总体思路：

1. **选定要比对的硬件级**：例如 `fft_8`，它对应 `len=8`，即 `level=levels-3`，行号 `=levels-1`（见 4.2.5 的算法）。
2. **取出该行**：`ref = X_FFT_middle_result(row, :);` 这就是该级的「黄金状态向量」。
3. **比对硬件输出**：理想情况下，硬件该级顺序输出的 N 个复数，其**取值集合**应与 `ref` 一致。
4. **注意顺序问题**：硬件的 SDF 流水线是按特定时序节拍（受 `select`、反馈延时驱动）吐出结果的，**输出在时间上的排列顺序不一定等于 MATLAB 数组的下标顺序**。所以逐拍比对前，需要先确定硬件输出的「时间序 → MATLAB 下标序」的映射（通常涉及一次 bit-reverse 或段内重排）。这一步必须结合波形确认，不能想当然。

> ⚠️ 待本地验证：硬件每级输出在时间上的精确排列顺序，取决于 SDF 的反馈与 `select` 时序（见 u3-l2、u3-l3），需要用仿真波形逐拍核对后才能给出确定的下标映射。本讲只保证「取值集合」层面的正确性参照。

#### 4.3.3 源码精读

中间结果矩阵的初始化与逐行写入，DIF 版只有关键三句：

```matlab
X_FFT_middle_result = zeros(levels+1, N);
X_FFT_middle_result(1,:) = x;                 % 第 1 行：输入
...
X_FFT_middle_result(level+2,:) = x;           % 每级结束写入一行
```

详见 [matlab/FFT_iterative_DIF.m:L28-L31](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L28-L31)（初始化）与 [matlab/FFT_iterative_DIF.m:L44-L44](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIF.m#L44)（逐行保存）。DIT 版写法几乎一样，只是行号是 `level+1`（因为 DIT 的 `level` 从 1 开始），见 [matlab/FFT_iterative_DIT.m:L54-L54](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L54)。

#### 4.3.4 代码实践（本讲指定的核心实践）

1. **实践目标**：导出某一级的 `X_FFT_middle_result` 中间向量，作为后续与硬件 `fft_8` 仿真输出逐拍比对的黄金参考数据。
2. **操作步骤**：
   - 打开 `matlab/FFT_iterative_DIF.m`，在脚本末尾（第 52 行之后）追加：
     ```matlab
     target_row = log2(N) - 1;          % fft_8 对应的行：level=levels-3 → 行=levels-1
     ref_fft8 = X_FFT_middle_result(target_row, :);   % 该级黄金参考向量
     save('ref_fft8.mat', 'ref_fft8');  % 存盘供 testbench 比对脚本读取
     disp(['fft_8 golden ref row = ', num2str(target_row)]);
     ```
   - 运行脚本。
3. **观察现象**：命令行打印出对应的行号；工作区出现 `ref_fft8`（1×1024 复数向量）和 `ref_fft8.mat` 文件。
4. **预期结果**：对于 `N=1024`，打印 `fft_8 golden ref row = 9`，与 4.2.5 的手算一致；`ref_fft8` 即第 9 行整段数据。
5. **关于「第 3 级」的说明**：任务描述里的「第 3 级」是举例。若你确实想取行号 3，它对应 `len=2^(levels-1)=512`（即 `fft_512` 那一级），并非 `fft_8`。要让参考向量对上 `fft_8`，必须按 `len=8 ⇒ 行=levels−1` 选取。**至于 `ref_fft8` 与硬件 `fft_8` 仿真输出在时间轴上的逐拍对齐，需在仿真波形中确认下标映射，待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`X_FFT_middle_result` 最后一行（第 `levels+1` 行）的数据，相对于自然序最终结果 `X_FFT` 是什么关系？

**参考答案**：最后一行是「全部蝶形完成、但未做最终 bit-reverse 倒序」的状态，即**按 bit-reverse 倒序排列**的最终结果；`X_FFT` 则是它再做一次倒序后的自然序版本。因此最后一行正好对应**硬件流水线未倒序的原始输出**。

**练习 2**：为什么用 `X_FFT_middle_result` 比对硬件，比只用最终 `X_FFT` 更有价值？

**参考答案**：最终 `X_FFT` 只能验证整条流水线总分对不对；而 `X_FFT_middle_result` 每一行对应一级，**一旦某级出错能立刻定位是哪一级 `fft_N` 算错**，把「整链路对不对」细化成「每一级对不对」，调试效率高得多。

---

### 4.4 迭代 DIT 与误差校验：先倒序后蝶形 + 对齐 MATLAB 内置 fft

#### 4.4.1 概念说明

`FFT_iterative_DIT.m` 是 DIF 的「镜像」：**先把输入 bit-reverse 倒序，再逐级蝶形，输出直接是自然序**。它在本讲有两个用处：

1. **交叉验证**：DIF 和 DIT 是两条独立路径，若它们结果一致，说明实现正确（互为印证）。
2. **满点数对齐**：DIT 脚本默认 `N=16384`，正好等于硬件 `fft_top` 的满点数（14 级）；而 DIF 脚本默认 `N=1024`。所以**想拿到与硬件满配置逐级对应的黄金参考，应当把 DIF 的 `N` 也改成 16384**（或直接用 DIT 的中间结果）。

DIT 还在末尾自带了一段与 MATLAB 内置 `fft()` 的误差校验，这是判断「我的实现有没有 bug」的兜底手段。

#### 4.4.2 核心流程

DIT 的关键区别在于**旋转因子先乘、蝶形后加减**，且输入是倒序的：

\[
A = x'(p+k),\qquad B = x'(p+k+\text{mid})\cdot W_{\text{len}}^{\,k}
\]
\[
x'(p+k)=A+B,\qquad x'(p+k+\text{mid})=A-B
\]

其中 \( x' \) 是 bit-reverse 倒序后的输入。注意 `len=2^level`（DIT 里 `level` 从 1 开始，`len` 随级**翻倍**，与 DIF 的减半相反）。

误差校验流程：

\[
\text{answer}=\text{fft}(x_{\text{原始}}),\qquad \text{deviation}=X_{\text{FFT}}-\text{answer},\qquad \text{variance}=\text{var}(\text{deviation})
\]

若 `variance > 1` 就报警「误差太大」。由于都是浮点双精度，正常情况下 `variance` 应在 \(10^{-24}\) 量级，阈值 1 只是一个非常宽松的「明显出错」闸门。

#### 4.4.3 源码精读

DIT 先做输入倒序（注意它把倒序结果存进新变量 `x_bit_reversed`，**原始 `x` 被保留**，供末尾 `fft(x)` 校验用）：

```matlab
normal_index   = index;
reversed_index = bitrevorder(normal_index);
x_bit_reversed = x(reversed_index+1);     % 先倒序，再蝶形
```

详见 [matlab/FFT_iterative_DIT.m:L31-L34](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L31-L34)。

逐级蝶形（`level` 从 1 开始，`len=2^level`，先乘旋转因子再加减）：

```matlab
for level = 1:levels
    len = 2^level;  mid = len/2;
    Wn = exp(-1j * 2 * pi / len);
    for pos = 1:len:N
        for k = 0:mid-1
            A = x_bit_reversed(pos+k);
            B = x_bit_reversed(pos+k+mid)*Wn^(k);   % 先乘旋转因子
            x_bit_reversed(pos+k)     = A + B;       % 再加减
            x_bit_reversed(pos+k+mid) = A - B;
        end
    end
    X_FFT_middle_result(level+1,:) = x_bit_reversed;
end
X_FFT = x_bit_reversed;        % DIT 输出已是自然序，无需再倒序
```

详见 [matlab/FFT_iterative_DIT.m:L41-L57](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L41-L57)。

误差校验段：

```matlab
answer    = fft(x);              % 用【原始自然序】x 当标尺
deviation = X_FFT - answer;
variance  = var(deviation);
if variance > 1
    fprintf('误差太大了，不对')
end
```

详见 [matlab/FFT_iterative_DIT.m:L65-L71](https://github.com/guanjiess/fpga-fft/blob/411062734ac2bdc2d08968c639bb7bec630a9e10/matlab/FFT_iterative_DIT.m#L65-L71)。注意 `answer = fft(x)` 用的是**原始 `x`**（DIT 没有覆盖它），而 `X_FFT` 是 DIT 的自然序输出，两者应当一致。

#### 4.4.4 代码实践

1. **实践目标**：三方互证——朴素 DFT、迭代 DIF、迭代 DIT、内置 `fft()` 四者结果一致。
2. **操作步骤**：
   - 把 `FFT_iterative_DIF.m` 的 `N` 临时改成一个便于手算的小值，例如注释掉原信号生成，改用 `x = [1 2 -3 -1]; N = 4;`（同时把依赖 `t/f` 的绘图段注释掉）。
   - 在脚本末尾加：`dif_nat = X_FFT;`（DIF 最终自然序结果）、`dit = run_dit_or_paste;`、`ref = fft(x);`，分别打印 `max(abs(dif_nat-ref))`、以及与 DIT 结果的差。
3. **观察现象**：三个差值都极小。
4. **预期结果**：所有差值在 \(10^{-12}\) 以内，说明 DIF、DIT、内置 `fft()` 完全一致。
5. 若 `dif_nat` 与 `ref` 偏差大：多半是漏了第 49~52 行的 bit-reverse 还原步骤。

> 说明：`FFT_iterative_DIF.m` 本身没有内置 `fft()` 校验段（只有绘图），校验逻辑只在 DIT 脚本里。所以「三方互证」需要你手动把 DIF 结果拿来和 `fft(x)` 比。

#### 4.4.5 小练习与答案

**练习**：DIF 脚本里 `x` 在蝶形循环中被**原地覆盖**，而 DIT 脚本却把倒序结果存进 `x_bit_reversed`、保留了原始 `x`。这个差异为什么对 DIT 的误差校验是必要的？

**参考答案**：DIT 末尾要用 `answer = fft(x)` 作为标尺，必须访问**原始自然序输入**；若像 DIF 那样原地覆盖 `x`，原始输入就丢了，无法再做比对。所以 DIT 特意把倒序结果另存为 `x_bit_reversed`，把原始 `x` 留给校验用。

---

## 5. 综合实践

把本讲知识串起来，搭一个最小的「黄金参考」工作流（纯 MATLAB，N=8，便于人工核对）：

1. **造输入**：`x = [1 2 -3 -1 4 -5 6 -7]; N = 8;`
2. **三条独立路径**：
   - 真值：`X_ref = fft(x);`
   - 矩阵 DFT：`X_mat = DFT_original(x.');`（注意 `DFT_original` 要求列向量，见其第 2 行注释）
   - 迭代 DIF：把 `FFT_iterative_DIF.m` 的核心循环搬来，`N=8`、`levels=3`，得到 `X_FFT_middle_result`（4×8）与最终 `X_FFT`。
3. **逐级核对**：打印 `X_FFT_middle_result`，确认它有 `levels+1=4` 行；手算第 1 级（`len=8, mid=4`）的 4 个蝶形，核对第 2 行是否正确。
4. **定位 fft_8 黄金参考**：对 `fft_8` 而言，`len=8` 出现在 `level=0`（N=8 时只有这一种跨度），其黄金状态向量即 `X_FFT_middle_result(2,:)`。
5. **三方一致性**：确认 `X_ref`、`X_mat`、`X_FFT` 三者最大模差 \(<10^{-12}\)。
6. **倒序观察**：打印 `X_FFT_middle_result` 的最后一行与 `X_FFT`，体会「最后一行是 bit-reverse 倒序、`X_FFT` 是自然序」的关系——这正是硬件输出（倒序）与期望结果（自然序）之间的那道「倒序墙」。

> 这个 N=8 的工作流，可以直接喂给下一讲 `u5-l2` 的 `tb/fft_8_tb.v` 仿真做硬件逐拍比对（具体下标映射需结合波形确认）。

---

## 6. 本讲小结

- **黄金参考**是验证硬件 FFT 的标尺：选一个严格贴定义、浮点双精度的可信实现，作为「标准答案」。`DFT_original.m`、`fft_testing.m` 就是这样的 O(N²) 基线。
- **外层循环 = 硬件逐级**：`FFT_iterative_DIF.m` 的 `for level` 循环每迭代一次，就对应硬件流水线的一级 `fft_N` 模块；最大 `len=N` 对应最前的 `fft_16k`，最小 `len=2` 对应最后的 `fft_2`。
- **`X_FFT_middle_result` 是逐级快照**：`(levels+1)×N` 矩阵，第 1 行是输入，第 `level+2` 行是第 `level` 级输出，最后一行是**未倒序**的最终结果——与硬件原始（未倒序）输出对应。
- **DIF vs DIT**：DIF 先蝶形后倒序（硬件走这条）、DIT 先倒序后蝶形；两者数学等价，可互为印证。注意 DIF 脚本 `N=1024`、DIT 脚本 `N=16384`，与硬件满配置对齐时要统一 N。
- **误差校验**：用 `fft(x)` 当标尺，`var(deviation)` 应在 \(10^{-24}\) 量级；阈值 1 只是宽松的出错闸门。
- **取值对、顺序待核**：MATLAB 行向量与硬件该级输出的**取值集合**一致，但**时间排列顺序**需结合 SDF 波形（`select`、反馈延时）确认下标映射，不能直接逐拍等同。

---

## 7. 下一步学习建议

- **下一讲 u5-l2（仿真与 testbench）**：把本讲导出的 `ref_fft8.mat` 接到 `tb/fft_8_tb.v`、`src/data_gen.v` 的仿真流程里，做硬件输出与黄金参考的逐拍（取值）比对——这是黄金参考真正发挥价值的地方。
- **u5-l3（平台移植）**：当硬件定点量化误差需要评估时，可把本讲的浮点 `Wn` 与 `src/RotatorMemory8.v` 的 Q1.16 量化值（如 46341）逐一比对，量化「定点 vs 浮点」的误差预算。
- **延伸阅读**：`matlab/FFT_figures.m` 画旋转因子单位圆分布，帮助直观理解 `Wn` 的对称性；`scheme/FFT.md`（见 u1-l3）是这套迭代算法的推导出处。
