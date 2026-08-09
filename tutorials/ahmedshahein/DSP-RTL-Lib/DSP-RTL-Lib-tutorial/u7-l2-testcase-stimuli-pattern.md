# 九测试用例激励设计模式

> 承接 [u7-l1 比特真验证方法论](u7-l1-bittrue-verification.md)。上一篇讲清了「黄金参考模型（GRM）→ 测试台喂激励逐样本比对 → error_count 判定」这条闭环；本篇聚焦闭环的源头——**激励从哪里来、为什么是这 9 个用例、定点信号如何被量化成整数喂给 RTL**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `stimuli.m` 的 `for i = 1:9` + `switch-case` 框架，说出每个用例生成的信号类型与它**针对性验证的缺陷**。
2. 理解「覆盖度导向」的激励设计思想：时域边界（脉冲/阶跃/斜坡）、频域扫描（chirp/单音）、统计覆盖（随机）、真实复合（带噪正弦）如何拼成一套完整的验证集。
3. 掌握 `quantize` / `quantizer` 两个函数如何把浮点信号量化成定点整数，理解 midtread 与 midriser 的差别，以及两补码非对称范围导致的饱和裁剪。
4. 能为一个**新 DSP 模块**仿写出自己的多场景激励集。

## 2. 前置知识

- **激励（stimuli）与响应（response）**：DSP 模块的输入序列叫激励，期望输出序列叫响应。在 DRL 里两者都存成「每行一个十进制整数」的 `.dat` 文本文件。
- **定点整数（fixed-point integer）**：RTL 只认整数。一个 \(W\) 位有符号补码数能表示的范围是 \([-2^{W-1},\ +2^{W-1}-1]\)——注意正负**不对称**，正最大比负最小的绝对值小 1。这个不对称是本讲反复出现的坑。
- **覆盖度（coverage）**：好的测试集不是「信号越多越好」，而是要**刻意**覆盖各种边界与典型场景，让每一类潜在 bug 都至少被一个用例打到。
- **黄金参考模型（GRM）**：用 Octave 写的「标准答案」程序。`stimuli.m` 既是激励发生器，也调用 GRM 生成响应，一身二职。

## 3. 本讲源码地图

本讲以 CIC 抽取滤波器的 `octave/` 目录为范本（全库通用）：

| 文件 | 作用 |
|------|------|
| [`.drl_src_code/filt_cicd/octave/stimuli.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m) | **主角**。9 用例 switch-case + 调用量化、GRM、写文件的总调度。 |
| [`.drl_src_code/filt_cicd/octave/quantizer.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/quantizer.m) | 生成一张「合法量化电平表」（归一化浮点）。 |
| [`.drl_src_code/filt_cicd/octave/quantize.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/quantize.m) | 把任意浮点样本映射到电平表里最近的那一档。 |
| [`.drl_src_code/filt_cicd/octave/gen_defines.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/gen_defines.m) | 把当前用例的参数写成 `defines_<tc>.sv` 宏文件，注入测试台。 |
| [`.drl_src_code/filt_cicd/octave/CICFilter.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m) | CIC 的黄金参考模型，产出比特真响应。 |
| [`dsp_rtl_lib.sh`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh) | 回归循环：对每个用例各编译仿真一次。 |

> 全库 6 个滤波器模块（`filt_cicd/cici/fir/mac/ppd/ppi`）的 `stimuli.m` 都用**完全相同**的 9 用例结构，只是参数名与 GRM 调用不同。信号生成器 `sgen_cordic` 也用 9 用例，唯独 `sgen_nco`（振荡器，无激励输入）只用 4 用例。本讲以 `filt_cicd` 为例。

---

## 4. 核心概念与源码讲解

### 4.1 激励/响应/defines 三件套的生成流程

#### 4.1.1 概念说明

`stimuli.m` 不是「写一个信号就完事」，而是一个**批处理流水线**：它循环 9 次，每次用 `switch` 选一个信号波形，然后一次性产出**三件套**交付物——激励文件、响应文件、参数宏文件。这三件套分别喂给测试台的三个不同环节（详见 u7-l1）。

- **激励 `stimuli_tc_<k>_mat.dat`**：输入序列，每行一个整数。
- **响应 `response_tc_<k>_mat.dat`**：GRM 算出的标准答案，每行一个整数。
- **参数宏 `defines_<k>.sv`**：把 `gp_*` 参数包成 `` `define `` 宏，让「哑」测试台知道位宽与配置。

#### 4.1.2 核心流程

整个 `stimuli.m` 的执行流程如下（伪代码）：

```
读参数 (gp_decimation_factor, gp_order, ...)
推导 gp_oup_width          # 派生参数，不在 .param 里
for testcase = 1 .. 9:
    switch(testcase):
        生成波形 data          # 9 种之一，见 4.2
    构造 defines 结构体
    gen_defines(defines)       # → 写 defines_<tc>.sv 并移走
    yy = CICFilter(..., data)  # 调 GRM 算响应
    dlmwrite(response_tc_<tc>_mat.dat, yy)   # 写响应
    dlmwrite(stimuli_tc_<tc>_mat.dat, data)  # 写激励
mv stimuli_tc_*.dat → sim/testcases/stimuli/
mv response_tc_*.dat → sim/testcases/response/
```

关键点：**激励与响应用同一个 `data`、同一组参数、同一个相位 `gp_phase`** 生成，因而天然对齐——这是比特真比对的前提。

#### 4.1.3 源码精读

**参数与派生位宽**——开篇固定 5 个参数，再推导输出位宽（Hogenauer 公式，详见 u4-l3）：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L7-L12`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L7-L12) —— 设定 CIC 参数并推导 `gp_oup_width`（注意 `gp_oup_width` 是派生表达式，不出现在 `.param` 中，故不会被脚本的 sed 误伤，见 u1-l3）。

**9 次循环与 switch**：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L17-L20`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L17-L20) —— `for i = 1:9` 循环驱动每个用例，`printf` 打印进度，`switch(testcase)` 分派波形。

**生成 defines 宏文件**——把 6 个参数塞进结构体再交给 `gen_defines`：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L111-L120`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L111-L120) —— 构造 `defines` 结构体并调用 `gen_defines`。

而 `gen_defines.m` 把它逐行写成宏（这是测试台拿到位宽的唯一渠道）：

[`.drl_src_code/filt_cicd/octave/gen_defines.m:L5-L12`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/gen_defines.m#L5-L12) —— 写出 `P_DECIMATION`/`P_ORDER`/…/`P_OUP_DATA_W`/`TESTCASE` 等宏，并末尾补一个 `` `define NULL 0``（供测试台判文件句柄，见 u7-l1）。

**调用 GRM 算响应**：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L121-L126`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L121-L126) —— `CICFilter(M, N, R, P, Fs, data)` 返回比特真响应 `yy`，注意第 4 个参数传的是 `gp_phase`，使 GRM 的 `downsample(..., R, P)` 与 RTL 的抽取相位严格一致。

**写文件与搬运**：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L133-L143`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L133-L143) —— `dlmwrite(...,"\n")` 把序列写成每行一个整数；循环结束后用 `system("mv ...")` 把 9 套 `.dat` 搬进 `sim/testcases/{stimuli,response}/`，供测试台用 `$fscanf` 读。

**回归循环**（在 `dsp_rtl_lib.sh` 里）——每出现一个 `stimuli_tc_*.dat` 就编译仿真一次：

[`dsp_rtl_lib.sh:L448-L469`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh#L448-L469) —— `for i in $FILES` 遍历 9 个激励文件，每次把对应的 `defines_$f.sv` 软链成 `defines.sv` 再编译运行。这就是「9 用例 = 9 次独立仿真」的来历。

#### 4.1.4 代码实践

**目标**：亲眼确认一个用例会同时产出三个文件。

**步骤**：
1. 在装好 Octave 的环境，进入某个已生成的模块 `octave/` 目录。
2. 单独跑 `octave --no-gui --silent stimuli.m`（脚本顶部的 `close all; clear; clc` 使其可独立运行）。
3. 观察终端：应依次打印 9 次 `### INFO: Running test-case <k>`，外加 defines/response/stimuli 三类 `### INFO`。

**需要观察的现象**：循环结束后，当前目录先出现 `stimuli_tc_1_mat.dat … stimuli_tc_9_mat.dat` 等，随即被 `mv` 搬走；`../sim/testcases/stimuli/` 下应出现 9 个激励 + 9 个 `defines_<k>.sv`，`../sim/testcases/response/` 下 9 个响应。

**预期结果**：用 `wc -l` 比对某个用例的激励与响应行数——响应行数 ≈ 激励行数 / `gp_decimation_factor`（抽取器下采样，所以输出更短）。例如用例 1 激励 256 行（`nr_samples=2^8`），R=17 时响应约 15 行。

> 若环境无 Octave，此项标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `stimuli.m` 要把激励和响应写在**同一个循环**里，而不是分两个脚本？

**参考答案**：保证激励、响应、defines 三者共用同一份 `data` 与同一组参数（尤其 `gp_phase`），避免两个脚本各自重算 `data` 时随机种子（用例 6/8/9 用了固定 `rand("state",...)`）或时序错位导致激励与响应对不齐。一次循环产出三件套，是比特真对齐的最可靠方式。

**练习 2**：`defines_<k>.sv` 文件名里的 `<k>` 是怎么被测试台读到的？

**参考答案**：`<k>` 就是 `testcase`。`gen_defines` 把它写成 `` `define TESTCASE <k> ``，测试台再用 `` `TESTCASE `` 拼出 `stimuli_tc_<k>_mat.dat` 的完整路径（见 [filt_cicd_tb.sv:L53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L53)）。回归脚本则通过软链 `defines_$f.sv → defines.sv` 把对应那一份喂给本次编译。

---

### 4.2 九测试用例的场景覆盖度设计

#### 4.2.1 概念说明

这 9 个用例不是随手挑的，而是按「**覆盖度导向**」精心排布：先打时域边界，再扫频域，再加统计随机，最后上真实复合信号。每一个都针对一类潜在缺陷。理解这张表，就理解了「怎么为 DSP 模块设计验证集」。

#### 4.2.2 核心流程

下表把 9 个用例归类到 4 个覆盖维度：

| 用例 | 信号类型 | 覆盖维度 | 针对性验证的缺陷 |
|:---:|---|---|---|
| 1 | 稀疏脉冲（全零 + 4 个孤立 1） | 时域·脉冲 | 脉冲响应正确性；复位/初值（绝大多数样本为 0，能暴露残留初值）；精确样本对齐 |
| 2 | 近似直流（全 1 + 几处 0 凹槽） | 时域·阶跃/直流 | 直流增益与稳态响应；凹槽处的阶跃上下冲处理 |
| 3 | 上升斜坡（遍历全部 \(2^W\) 个码） | 时域·满量程 | 每个输入码字至少经过一次；单调性与端点处理 |
| 4 | 下降斜坡 | 时域·满量程 | 与 3 反向，捕获方向相关 bug；二者合成三角波 |
| 5 | 对数 chirp（扫频 \(f_1\to f_s/2\)） | 频域·宽带 | 全带频率响应；混叠、CIC droop（高频跌落）等频域误差 |
| 6 | 均匀随机（固定种子 42） | 统计·随机 | 大量随机跳变组合，撞出罕见输入序列 bug；可复现 |
| 7 | 单音正弦（\(f_o=113\) Hz） | 频域·单音 | 带内单音，输出可解析预测；干净的频域衰减/相移检查 |
| 8 | 正弦 + 噪声（\(A_n=0.2\)） | 真实·复合 | 滤波器对带外噪声的抑制；SNR 行为 |
| 9 | 正弦 + 噪声 + 直流 + 相位 | 真实·最复杂 | 叠加性（线性度）、直流处理、相位一起验；最接近真实信号 |

注意用例 3、4 的特殊性：它们**直接枚举** \(2^W\) 个合法码字，不经过量化。对 CIC（`gp_inp_width=2`）只有 4 个样本 `[-2,-1,0,1]`；对 FIR（`p_data_width=7`）则是 128 个样本 `[-64,…,63]`。这是一种「**输入码字穷举**」——保证每个可能的输入整数都至少被处理一次。

#### 4.2.3 源码精读

**用例 1：稀疏脉冲**——全零背景上戳 4 个孤立的 1：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L21-L27`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L21-L27) —— `data=zeros(...)` 后在索引 2/100/180/220 处置 1。绝大多数样本为 0，能敏锐暴露寄存器初值或复位残留。

**用例 2：近似直流带凹槽**——全 1 序列里挖掉若干点：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L29-L36`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L29-L36) —— `data=ones(...)`，再把若干区间置 0。恒定 1 考验直流稳态增益，凹槽考验阶跃过渡。

**用例 3、4：双向满量程斜坡**——枚举全部码字：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L38-L42`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L38-L42) —— 升序 `-2^(W-1) : 2^(W-1)-1` 与降序 `2^(W-1)-1 : -1 : -2^(W-1)`，正反两遍走完整个码空间。

**用例 6：固定种子随机**——可复现的统计覆盖：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L54-L59`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L54-L59) —— `rand("state",42)` 锁死随机种子，`floor` 把 \([-2^{W-1},2^{W-1})\) 上的均匀分布取整。**固定种子**保证每次跑 GRM 结果一致（否则比特真比对会随机失败）。

**用例 7：纯单音 + 饱和裁剪**——这是重点，看裁剪那行：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L61-L70`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L61-L70) —— 正弦缩放成整数后，第 70 行 `data(data==2^(gp_inp_width-1)) = 2^(gp_inp_width-1)-1` 把等于 \(+2^{W-1}\) 的样本砍成 \(+2^{W-1}-1\)。原因：振幅 \(A=1\) 的正弦在峰值处经 \(2^{W-1}\) 缩放后得到 \(+2^{W-1}\)，而 \(W\) 位有符号补码的正最大只能到 \(+2^{W-1}-1\)，不裁就会溢出（回绕成负数）。这是**两补码非对称范围**的直接体现。

**用例 9：最复杂复合信号**——叠加音、噪声、直流、相位：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L90-L107`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L90-L107) —— 含相位 `phi=pi/3`、直流偏置 `dc=0.1`、噪声幅度 `An=0.25`、非整数频率 `fo=173.38943`（避免周期与采样窗整数重合，制造更一般的样本序列），并同样调用 `quantize` 量化、末行裁剪。

#### 4.2.4 代码实践

**目标**：理解「同一波形，不同模块」。证明 9 用例是全库通用模板。

**步骤**：
1. 对比 [`filt_fir/octave/stimuli.m:L23-L29`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m#L23-L29) 与 `filt_cicd` 的用例 1。
2. 用 `grep -n "case {" .drl_src_code/*/octave/stimuli.m` 统计每个模块的用例数。

**需要观察的现象**：`filt_fir` 的用例 1 与 `filt_cicd` 的用例 1 文本几乎逐字相同（都是 `zeros` + 4 个 `data(...)=1`），只是参数名从 `gp_inp_width` 换成了 `p_data_width`；6 个滤波器模块各有 9 个 `case`，而 `sgen_nco` 只有 4 个。

**预期结果**：确认 9 用例是一份可复用的模板——你为 CIC 学的覆盖度设计，可直接搬到 FIR/多相。

> 若无可执行环境，本项为「源码阅读型实践」，结论已由 grep 给出。

#### 4.2.5 小练习与答案

**练习 1**：用例 3、4 对 CIC（`gp_inp_width=2`）只有 4 个样本，对 FIR（`p_data_width=7`）有 128 个。为什么用例长度依赖位宽？这有什么好处？

**参考答案**：因为用例 3/4 的写法 `-2^(W-1) : 2^(W-1)-1` 直接枚举 \(2^W\) 个码字，长度就是 \(2^W\)。好处是「输入码字穷举」：保证每一个可能的输入整数都至少经过一次滤波器，专查「某个特定码字触发 bug」的边界缺陷。位宽越大覆盖越全，但样本也越多。

**练习 2**：为什么用例 6/8/9 都要写 `rand("state", <常数>)`？删掉会怎样？

**参考答案**：锁定伪随机种子使激励序列**可复现**。GRM 与 RTL 必须比对同一批随机样本；若不锁种子，每次跑 Octave 生成不同的随机激励，响应也随之变化，比特真比对会不稳定（甚至偶发失败却无法复现）。固定种子把「随机覆盖」与「确定性验证」统一起来。

---

### 4.3 定点量化 quantize / quantizer

#### 4.3.1 概念说明

用例 3/4/6 直接产生整数（枚举码字或 `floor` 取整），但用例 5/7/8/9 产生的是**浮点信号**（chirp、正弦、带噪正弦）。RTL 只吃整数，所以必须先把浮点样本**量化**成 \(W\) 位补码整数。这套量化由两个函数完成：

- `quantizer(Q, QType, DType)` —— 生成一张合法电平表（归一化到 \([-1, \sim0.5]\)）。
- `quantize(r_data, Q, QType, DType)` —— 把每个浮点样本映射到表里**最近**的那一档。

#### 4.3.2 核心流程

量化分两步，关键是「电平表怎么摆」与「最近邻怎么找」：

1. **建表**：对 \(Q\) 位量化器，共 \(2^Q\) 个电平。两种摆法：
   - **midtread（中平）**：在 0 处有一段「平阶」（0 是合法电平）。适合需要精确表示 0 的信号（如静音、直流）。本库默认用它。
   - **midriser（中升）**：0 处「上升跳变」，正负电平关于 0 对称，但没有 0 电平。
2. **映射**：正样本查上半表、负样本查下半表，各取距离最小的电平。
3. **还原整数**：`data = round(2^(W-1) * q_data)`，把归一化电平缩放回 \(W\) 位整数范围。
4. **饱和裁剪**：`data(data==2^(W-1)) = 2^(W-1)-1`，把超出正最大的样本压回。

对 midtread、\(W=2\) 位，电平表与整数的对应：

| 码字（十进制） | 归一化电平 | 说明 |
|:---:|:---:|---|
| \(-2\) | \(-1.0\) | 负最满 |
| \(-1\) | \(-0.5\) | |
| \(0\) | \(0\) | 「平阶」——midtread 特征 |
| \(+1\) | \(+0.5\) | 正最大（注意无 \(+2\)） |

midtread 有符号电平公式（\(j=i-1\)，\(nol=2^Q\)）：

\[
\text{level}_i = \frac{2j - nol}{nol}
\]

电平间距（量化步长）恒为 \(2/nol\)。缩放 \(2^{W-1}\) 后，步长恰好为 1 个整数 LSB——所以量化后的整数无冗余、紧密铺满整个码空间。

#### 4.3.3 源码精读

**`quantizer.m` 建表**——midtread 与 midriser 两支：

[`.drl_src_code/filt_cicd/octave/quantizer.m:L11-L30`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/quantizer.m#L11-L30) —— `nol=2^Q`；第 16 行 midtread 有符号公式 `((2.0*j)-nol)/nol`（含 0 电平），第 25 行 midriser 有符号公式 `(((2.0*j)+1.0)-nol)/nol`（无 0 电平、关于 0 对称）。

**`quantize.m` 最近邻映射**——正负分半查：

[`.drl_src_code/filt_cicd/octave/quantize.m:L3-L13`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/quantize.m#L3-L13) —— 先取电平表 `QLevels`；正样本在第 6-8 行查上半表、负样本在第 9-12 行查下半表，各用 `min(abs(...))` 找最近电平。返回的 `q_data` 仍是归一化浮点。

**调用与缩放**——在 `stimuli.m` 用例 5 中：

[`.drl_src_code/filt_cicd/octave/stimuli.m:L50-L51`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L50-L51) —— `q_data = quantize(data, gp_inp_width, "midtread", "signed")` 得归一化电平，再 `round(2^(W-1)*q_data)` 缩放成整数。两步合起来等价于「均匀 midtread 量化到 \(W\) 位补码」。

**饱和裁剪**——用例 7/8/9 末行（见 4.2.3 的 L70），把 \(+2^{W-1}\) 压回 \(+2^{W-1}-1\)，对应两补码非对称范围。注意用例 5 的裁剪行被**注释掉了**（[L52](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L52)），因为 chirp 经 midtread 量化后最大电平恰为 \(+0.5\)，缩放后是 \(+1\)，不会越界；而用例 7 的 \(A=1\) 正弦不经 `quantize`、直接 `round`，峰值才会撞到 \(+2^{W-1}\)。

#### 4.3.4 代码实践

**目标**：亲手验证量化电平表与整数码的对应关系。

**步骤**：在 Octave 中执行（示例代码）：

```octave
% 示例代码：打印 W=2 位 midtread 有符号电平表
QLevels = quantizer(2, "midtread", "signed");
disp(QLevels);                       % 归一化电平
disp(round(2^(2-1) * QLevels));      % 缩放成整数码
```

**需要观察的现象**：归一化电平为 `[-1, -0.5, 0, 0.5]`；缩放后的整数为 `[-2, -1, 0, 1]`，正好是 2 位有符号补码的全部 4 个码字。

**预期结果**：确认 midtread 含 0 电平、步长为 1 LSB、正最大是 \(+1\)（不是 \(+2\)）。再改成 `quantizer(2,"midriser","signed")`，应得到 `[-0.75, -0.25, 0.25, 0.75]`——无 0、关于 0 对称。

> 若无 Octave，可手算公式验证：midtread \(W=2\) 时 \(nol=4\)，代入 \((2j-4)/4\) 得 \([-1,-0.5,0,0.5]\)。本项结论可纯手算确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DRL 默认用 midtread 而不是 midriser？

**参考答案**：midtread 有 0 电平，能精确表示「无信号」（静音、直流 0、复位后的稳态）。对验证而言，激励里的 0 必须量化成确切的 0，否则会把量化噪声误当成 RTL 误差。midriser 没有 0 电平，连「什么都没有」都无法精确表达，不适合本库这种逐比特严格比对。

**练习 2**：用例 7 为什么要做 `data(data==2^(W-1)) = 2^(W-1)-1` 这步裁剪？不裁会怎样？

**参考答案**：\(A=1\) 正弦峰值经 \(2^{W-1}\) 缩放得 \(+2^{W-1}\)，但 \(W\) 位有符号补码的正最大是 \(+2^{W-1}-1\)（范围非对称）。不裁，\(W=2\) 时 \(+2\) 会被解读成补码的 \(-2\)（符号位为 1），激励在峰值处突然变成大负数，RTL 与 GRM 都会算错。裁剪把它饱和到 \(+2^{W-1}-1\)，是一种有意为之的饱和处理，保证激励始终落在合法码空间内。

---

## 5. 综合实践

**任务**：为一个**新 DSP 模块**（假设是带通滤波器 `filt_bpf`）设计一组 **6 个测试用例**的 `switch-case` 框架，至少含直流、满量程、随机、带通信号，并说明每个用例针对验证什么缺陷。

**示例代码**（仿 `stimuli.m` 风格，仅展示 case 骨架，省略写文件与 GRM 调用）：

```octave
% 示例代码：filt_bpf 的 6 用例激励骨架（非项目原有代码）
for i = 1 : 6,
  testcase = i;
  switch (testcase)
    case {1}   % 直流：验稳态增益与是否如设计般抑制直流（带通应阻断 DC）
      data = ones(1, nr_samples);

    case {2}   % 满量程三角波：穷举全部输入码，查端点与方向相关 bug
      ramp   = -2^(W-1) : 2^(W-1)-1;
      data   = [ramp, fliplr(ramp)];

    case {3}   % 带内单音（落在通带中心）：验通带是否平坦、增益是否正确
      data = round(2^(W-1) * sin(2*pi*f_pass*t));
      data(data==2^(W-1)) = 2^(W-1)-1;   % 饱和裁剪

    case {4}   % 带外单音（落在阻带）：验阻带衰减是否足够
      data = round(2^(W-1) * sin(2*pi*f_stop*t));
      data(data==2^(W-1)) = 2^(W-1)-1;

    case {5}   % 带通信号 = 通带单音 + 宽带噪声：验噪声抑制与信号保真
      rand("state", 42);
      sig = sin(2*pi*f_pass*t);
      nze = 0.2*(2*rand(1,length(t))-1);
      data = round(2^(W-1) * quantize(sig+nze, W, "midtread", "signed"));

    case {6}   % 固定种子随机：统计覆盖，撞罕见输入组合
      rand("state", 42);
      data = floor(-2^(W-1) + 2^W*rand(1, nr_samples));
  endswitch
  % …此处接 gen_defines / GRM / dlmwrite，仿 4.1 流程…
end
```

**说明每个用例针对的缺陷**：

| 用例 | 针对验证的缺陷 |
|:---:|---|
| 1 直流 | 带通滤波器应阻断直流；若输出非零，说明低频抑制不足或存在 DC 泄漏 |
| 2 满量程三角 | 穷举输入码，查溢出/饱和、端点 `±2^(W-1)` 处的符号处理 |
| 3 带内单音 | 通带增益与平坦度；输出幅度应接近输入（按增益缩放） |
| 4 带外单音 | 阻带衰减；输出应显著小于输入，否则阻带抑制不达标 |
| 5 带通+噪声 | 线性叠加性（信号通过、噪声被抑制）与 SNR 改善 |
| 6 随机 | 统计性的广泛覆盖，捕获罕见输入序列触发的 bug |

**验收**：把上述骨架接入 `gen_defines` + 你的 GRM + `dlmwrite`，跑回归，6 个用例均应 PASSED（`error_count==0`）。若带外单音（用例 4）PASSED，即证明阻带衰减符合模型。

> 若无运行环境，本实践可作为「设计评审」交付：重点是 6 个用例的**覆盖度论证表**，而非能否跑通。

## 6. 本讲小结

- `stimuli.m` 是一条**批处理流水线**：`for i=1:9` 循环里每个用例一次性产出「激励 + 响应 + defines 宏」三件套，三者共用同一份 `data` 与参数（尤其 `gp_phase`），这是比特真对齐的前提。
- 9 个用例按**覆盖度导向**排布：时域边界（脉冲/阶跃/斜坡）、频域（chirp 扫频/单音）、统计（随机）、真实复合（带噪正弦），每个都针对一类缺陷；该模板在 6 个滤波器模块间通用。
- 用例 3/4 **枚举全部 \(2^W\) 个码字**做输入穷举；用例 6/8/9 用**固定随机种子**兼顾随机覆盖与可复现性。
- 浮点信号经 `quantize`/`quantizer` 量化成 \(W\) 位补码整数：midtread 有 0 电平（适合精确表示无信号），步长恰为 1 LSB。
- 两补码**非对称范围** \([-2^{W-1},+2^{W-1}-1]\) 要求对峰值做饱和裁剪（`data(data==2^(W-1))=2^(W-1)-1`），否则正满量程会回绕成负数。
- 回归脚本对每个 `stimuli_tc_*.dat` 各编译仿真一次，9 用例 = 9 次独立比特真比对。

## 7. 下一步学习建议

- **学 u7-l3 dev 模式脚手架**：学会用 `dsp_rtl_lib.sh -dev` 生成一个新模块的空 RTL/TB 骨架，然后把本讲的「6 用例 stimuli 骨架」填进去，跑通属于你自己的模块。
- **横向对比 GRM**：精读 [`CICFilter.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m)，理解它如何用整数箱形系数 `ones(1,R*M)` 自乘 \(N\) 次构造 CIC，并与 [`filt_fir/octave/stimuli.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m) 的系数量化对照，体会「同一套激励框架、不同 GRM」的复用。
- **深入量化理论**：若对 midtread/midriser、均匀量化噪声（SQNR）感兴趣，可阅读任意数字信号处理教材的「量化与 SQNR」章节，回头再读 `quantizer.m` 的电平公式会有更深体会。
