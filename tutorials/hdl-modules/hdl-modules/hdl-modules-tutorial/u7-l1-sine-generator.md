# 正弦发生器（DDS/NCO）

## 1. 本讲目标

本讲带你从零读懂 hdl-modules 里的 `sine_generator` 模块——一个用纯 VHDL 实现的正弦/余弦波形发生器，业界常称作 **DDS（直接数字合成器，Direct Digital Synthesizer）** 或 **NCO（ numerically controlled oscillator，数控振荡器）**。读完本讲你应当能够：

- 说清楚「相位累加 + 查表」这条 DDS 主干是怎么把一个 `phase_increment` 数字翻译成模拟意义上的输出频率的，并能用项目自带的 `get_phase_increment` 函数算出它。
- 解释为什么 ROM 只存一个象限（\([0, \pi/2)\)）就能还原完整 \([0, 2\pi)\) 的正弦和余弦——即四分之一象限对称（quarter-wave symmetry）。
- 区分整数相位模式与分数相位模式，理解「分数相位截断」为何会带来杂散（spur），以及项目提供的两条互相独立的补救路径：**相位抖动（dither）** 与 **一阶泰勒展开（Taylor expansion）**。
- 看懂项目如何用 VUnit + Python 后检查把 SFDR/SNDR 纳入回归，以及 netlist 构建如何把 LUT/FF/DSP/RAM 资源也纳入回归。

本讲是专家层（advanced）讲义，假定你已经读过 `u2-l2`（common 基础包，尤其是 `types_pkg` 的 `unsigned_vec_t`）和 `u2-l3`（math 模块，尤其是 `saturate_signed` 饱和）。

## 2. 前置知识

在进入源码前，先用最直白的方式建立几个直觉。

**（1）相位累加 = 用整数加法画圆。** 一个固定点（fixed-point）的相位累加器每拍加上一个常量 `phase_increment`，溢出回绕。把它想象成在圆周上等步长前进：步长越大，转一圈越快，输出频率越高。频率只由「步长 / 圆周总刻度」决定。

**（2）查表 = 把相位映射成幅度。** 正弦不是线性函数，硬件里最省事的做法是预先把若干个相位的正弦值存进 ROM，运行时用相位当地址去读。ROM 越深（地址位越宽），相位分辨率越高；ROM 字越宽（数据位越宽），幅度量化噪声越小。

**（3）量化带来噪声，截断带来杂散。** 量化误差是「铺开的」随机噪声；而把分数相位的小数部分一刀切掉（截断）会留下周期性的误差，周期性误差在频谱上表现为一根根尖刺（spur）。衡量质量有两个常用指标：

- **SFDR（无杂散动态范围，Spurious-Free Dynamic Range）**：基频功率与最大杂散/噪声峰之比，单位 dB。尖刺越矮，SFDR 越高。
- **SNDR（信噪失真比，Signal-to-Noise-and-Distortion）**：基频功率与「除基频外一切」功率之比。

两者都可以折算成「等效位数 **ENOB**」：\(\text{ENOB} = (\text{dB} - 1.76) / 6.02\)。本模块的 Python 后检查正是用这个公式（见后文 `calculate_enob`）。

**（4）抖动把尖刺摊成噪声，泰勒展开把误差算回来。** 这是本讲的两个关键技巧：
- **抖动（dither）**：给相位故意加一点伪随机量，让本来集中在某几根尖刺上的能量被「摊平」成底噪——SFDR 升，SNDR 略降。
- **一阶泰勒展开**：截断丢掉的小数部分并非无用，用它对查表结果做一次一阶修正（查表值 + 斜率 × 误差），既升 SFDR 又升 SNDR，代价是多花几个 DSP 乘法器。

记牢这四条直觉，下面读源码会很顺。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `modules/sine_generator/` 下：

| 文件 | 作用 |
| --- | --- |
| [src/sine_generator.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd) | **顶层**。做相位累加，可选地叠加抖动，然后把相位交给 `sine_calculator`。 |
| [src/sine_generator_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator_pkg.vhd) | 工具包。提供 `get_phase_width` 与 `get_phase_increment` 两个纯函数，把「时钟频率 / 目标频率」翻译成相位位宽与相位步长。 |
| [src/sine_calculator.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd) | **计算核心**。实例化 `sine_lookup` 查表；若开启泰勒展开，再用 `taylor_expansion_core` 做一阶修正。 |
| [src/sine_lookup.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd) | **查表 ROM**。只存 \([0, \pi/2)\) 一个象限的正弦样本，靠四象限对称还原全程正弦/余弦。 |
| [src/taylor_expansion_core.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/taylor_expansion_core.vhd) | 泰勒展开核心。计算 \(f(a) + e \cdot f'(a)\)，全部塞进 DSP48。 |
| [module_sine_generator.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py) | 测试与构建脚本。在 `setup_vunit` 里枚举 generic 矩阵并挂频谱后检查；在 `get_build_projects` 里给 netlist 构建配资源 checker。 |
| [test/tb_sine_generator.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/test/tb_sine_generator.vhd) | 顶层 testbench。用 `get_phase_increment` 算步长，把结果样本导出成 `sine.raw` / `cosine.raw`。 |

数据流（从上到下）：

```text
input_phase_increment ──▶ sine_generator (相位累加 [+ 可选抖动])
                                  │ phase
                                  ▼
                          sine_calculator ──┬─▶ sine_lookup (查表, latency=3)
                                            │       │ lookup_sine, lookup_cosine
                                            │       ▼
                                            └─▶ [可选] taylor_expansion_core (latency=2)
                                                    │ 一阶修正
                                                    ▼
                                          result_sine / result_cosine
```

依赖关系上，本模块复用了 `common`（`types_pkg` 的 `unsigned_vec_t`、`attribute_pkg` 的 `use_dsp`）、`math`（`math_pkg` 的 `num_bits_needed`、`saturate_signed`）和 `lfsr`（`lfsr_fibonacci_multi`，用于抖动），与 `u2-l2`、`u2-l3` 一脉相承。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① 顶层相位累加与频率控制；② 四分之一象限查表；③ 分数相位与抖动；④ 一阶泰勒展开。

### 4.1 顶层：相位累加与频率控制

#### 4.1.1 概念说明

DDS 的心脏是一个**相位累加器**：一个无符号整数寄存器，每拍加上一个常量步长 `phase_increment`，溢出自然回绕。由于正弦是周期函数，而定点数的溢出回绕也是「周期」的，二者天然对应——累加器的值就是当前相位，回绕就是「相位越过 \(2\pi\)」。

输出频率完全由这个步长决定。设累加器位宽为 `phase_width`，则满量程为 \(2^{\text{phase\_width}}\)，对应一整圈 \(2\pi\)。每个时钟走 `phase_increment` 步，所以每拍走过的圈数比例为：

\[
\frac{f_{\text{sine}}}{f_{\text{clk}}} = \frac{\text{phase\_increment}}{2^{\text{phase\_width}}}
\]

反解出步长就得到项目里写明的公式：

\[
\text{phase\_increment} = \text{int}\!\left(\frac{f_{\text{sine}}}{f_{\text{clk}}} \times 2^{\text{phase\_width}}\right)
\]

注意 **Nyquist 条件**：正弦频率必须小于时钟频率的一半，否则无法重建。这等价于「步长的最高位不能被置 1」——项目正是利用这一点省掉了一个 LUT（见 4.1.3）。

#### 4.1.2 核心流程

1. 用户给定时钟频率与目标正弦频率，用 `get_phase_width` 算出累加器位宽，再用 `get_phase_increment` 算出步长。
2. 顶层 `sine_generator` 每拍把步长加进累加器（最高位不参与加法），溢出回绕。
3. 累加结果作为相位，交给 `sine_calculator`。
4. `result_valid` 与 `input_valid` 对齐（经若干拍流水线延迟）。

相位位宽的构成是理解整个模块的钥匙：

\[
\text{phase\_width} = \text{memory\_address\_width} + 2 + \text{phase\_fractional\_width}
\]

其中 `+2` 是「象限指示位」（因为只存一个象限，需要 2 位来选四个象限），`phase_fractional_width` 是可选的小数位（>0 即进入分数相位模式）。

#### 4.1.3 源码精读

**位宽与步长函数**——两个纯函数，建议在实例化时直接调用，避免手算出错：

- [`get_phase_width` 返回 `memory_address_width + 2 + phase_fractional_width`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator_pkg.vhd#L44-L50)：注释里点明「+2 for the quadrant indicator, given that only one quadrant is stored in memory」。
- [`get_phase_increment` 用浮点算 ratio×2^phase_width 再 round](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator_pkg.vhd#L52-L73)：内部还做了两条断言——频率必须小于 Nyquist（`sine_frequency_hz < clk/2`），且步长不能为 0。注意它会**截断到最近的整数步长**，因此实际频率与目标频率之间会有量化误差，频率分辨率由 `phase_width` 决定。

**顶层实体**——generic 全家桶在这里：

- [sine_generator 的 generic 声明](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L103-L148)：`memory_data_width`（ROM 字宽，影响 SNDR/SFDR）、`memory_address_width`（ROM 地址位宽，影响频率分辨率与分数模式下的 SFDR）、`phase_fractional_width`（>0 开启分数相位模式）、`enable_sine`/`enable_cosine`、`enable_phase_dithering`、`enable_first_order_taylor`、`initial_phase`（可设初相位，便于多路正交）。注意 `initial_phase` 的位宽直接用 `get_phase_width(...)` 计算，端口 `input_phase_increment` 复用同一范围。

**相位累加进程**——本模块最核心的几行：

- [`phase_counter` 进程](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L200-L221)：注释明确写道「the top bit may not be used since that would break the Nyquist criterion. Hence we can remove the top bit from the addition, saving one LUT」——`phase_increment_to_add` 比累加器窄 1 位，砍掉的就是最高位。同时 `assert input_phase_increment(input_phase_increment'high) /= '1'` 守护 Nyquist；并注释「Overflow is expected and desired. Fixed-point numbers are periodic just like the sine function」——溢出回绕是有意为之。

**性能上限自检**——顶层用一个 `get_result_enob` 函数静态算出「当前配置下结果最多能到多少 ENOB」，并断言 ROM 字宽别成了瓶颈：

- [`assert_widths_block` 里的 `get_result_enob`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L163-L196)：整数相位模式给 `memory_data_width + 1`；分数 + 抖动给 `memory_address_width + 4`；分数 + 泰勒给 `2*(memory_address_width + 1)`；纯分数给 `memory_address_width + 1`。随后断言 `memory_data_width >= result_enob - 1`，否则报「Memory data width is limiting performance」。

#### 4.1.4 代码实践

实践目标：用项目自带函数把「频率」翻译成「步长」，验证公式与 Nyquist。

操作步骤：

1. 打开 `tb_sine_generator.vhd`，看它如何用 `get_phase_width` + `get_phase_increment` 算出 `input_phase_increment`：[tb_sine_generator.vhd:51-58](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/test/tb_sine_generator.vhd#L51-L58)。时钟频率取 `clk_frequency_hz = 2**27`（见 module 脚本）。
2. 手算一组：取 `clk_frequency_hz = 2**27`、`sine_frequency_hz = 2**22`、`memory_address_width = 8`、`phase_fractional_width = 0`。则 `phase_width = 8 + 2 + 0 = 10`，`phase_increment = int(2**22 / 2**27 × 2**10) = int(2**-5 × 2**10) = 2**5 = 32`。
3. 反验频率：\(f_{\text{sine}} = 32 / 2^{10} × 2^{27} = 2^{22}\)，与目标一致。
4. 故意把 `sine_frequency_hz` 设成大于 `clk/2`，重新跑 `get_phase_increment`，观察断言报「Cannot synthesize this sine wave」。

需要观察的现象 / 预期结果：步骤 3 的反算频率应与目标完全相等（因为这里步长恰好整除）；步骤 4 应在仿真 elaboration 阶段就因断言失败而终止。其余非整除组合会有量化误差，**待本地验证**具体数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `phase_increment_to_add` 比累加器窄 1 位？省下的是什么？
**答案**：因为 Nyquist 条件要求 `sine_frequency_hz < clk/2`，等价于步长最高位恒为 0。砍掉这一位不改变正确结果，却让加法器少 1 位、省 1 个 LUT。

**练习 2**：把 `phase_fractional_width` 从 0 调到 8（其余不变），`phase_width` 与频率分辨率怎么变？
**答案**：`phase_width` 增加 8，满量程 \(2^{\text{phase\_width}}\) 增大 \(2^8\) 倍，相邻可表示频率的间隔（分辨率）变细到原来的 \(1/256\)。

**练习 3**：`initial_phase` 有什么实际用途？
**答案**：让多个 `sine_generator` 实例用相同步长但不同 `initial_phase`，从而输出相位错开（例如 \(\pi/2\)）的多路正弦，用于 I/Q 调制等场景。

### 4.2 查表核心：四分之一象限 ROM

#### 4.2.1 概念说明

`sine_lookup` 解决的问题是：怎样用最小的 ROM 还原 \([0, 2\pi)\) 整周期的正弦（以及余弦）？答案是利用**对称性只存一个象限**。

正弦在四个象限里高度对称：第一象限的值「反过来」就是第二象限的值；第三、四象限只是加个负号。数学上，用 \(\bar\theta\) 表示地址的按位取反（即「反序读取」），有：

\[
\sin(\theta) \;\longleftrightarrow\; \sin(\bar\theta) \text{（反序）}, \qquad \text{负号由象限决定}
\]

`u6` 文档里给出的全部恒等式（节选）说明：四个象限的正弦和余弦都能用 \([0, \pi/2)\) 内的正弦值算出来——要么原序、要么反序读，再加个正负号。这样 ROM 深度直接砍到 \(1/4\)。

#### 4.2.2 核心流程

1. 把输入相位（位宽 `memory_address_width + 2`）的最高 2 位当作**象限号**，其余 `memory_address_width` 位当作**象限内相位**。
2. 根据象限号决定正弦/余弦地址是「原序」还是「反序（按位取反）」。
3. 用地址查 ROM（只存第一象限、幅度为正的无符号样本），用 BRAM 输出寄存器打一拍改善时序。
4. 根据象限号决定是否对结果取负，得到有符号输出，范围 \([-A, A]\)，其中 \(A = 2^{\text{memory\_data\_width}} - 1\)。
5. 全程把 `valid` 信号按固定延迟（`latency = 3`）打拍对齐。

ROM 内容在精化期（elaboration）由一个函数算出并固化为常量数组，综合后变成 Block RAM。

#### 4.2.3 源码精读

**实体与延迟属性**——

- [sine_lookup 实体](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L141-L172)：`enable_sine`/`enable_cosine`/`enable_minus_sine`/`enable_minus_cosine` 四个开关控制算哪些信号；`attribute latency of sine_lookup : entity is 3` 把延迟固化为 3 拍，供上层（`sine_calculator`）引用 `sine_lookup'latency` 做流水对齐。

**ROM 初始化函数**——精化期纯函数，注释里写明了定点约定：

- [`calculate_sine_quadrant_table`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L200-L227)：相位步长 `index_phase_increment = (π/2) / memory_depth`，并**偏移半个 LSB** `phase_offset = index_phase_increment / 2`（为了让象限镜像对称）。幅度满量程 `fix_point_scale = 2**memory_data_width - 1`。逐点算 `round(sin(phase) × scale)` 存成无符号。

**BRAM 读出（带输出寄存器）**——

- [memory 进程](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L235-L250)：两级 `memory_sine_m1` → `memory_sine`，注释「Read with the BRAM output register enabled, for better timing on the output side」——这是把数据吸进 BRAM 的硬输出寄存器（与 u4-l1 FIFO 的 `output_register` 思路一致）。

**象限解码与取负**——查表是否「反序」、结果是否取负，都由象限号决定：

- [象限提取](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L272-L273)：`input_quadrant` 取最高 2 位，`input_quadrant_phase` 取其余位。
- [`assign_address` 进程](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L277-L304)：象限 1 或 3 时，正弦地址取反（反序）、余弦地址原序；否则反之。这正是「反序 = 按位取反」的实现。
- [`assign_result` 进程](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L327-L368)：正弦在象限 2、3 取负；余弦在象限 1、2 取负。`minus_*` 版本是符号取反的「搭子」，方便用户直接拿到 \(\pi\) 偏移的信号。

**性能说明**——头注释明确：本实体唯一的噪声源是把正弦值定点化存进 ROM 带来的幅度量化噪声，因此结果的 SNDR 与 SFDR 都等于 `memory_data_width + 1` ENOB（[文档段](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L121-L130)）。

#### 4.2.4 代码实践

实践目标：验证四象限对称确实「只用第一象限」就还原了全程波形。

操作步骤：

1. 读 `sine_lookup` 的 [ROM 初始化函数](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_lookup.vhd#L200-L227)，手动用 Python 复现：`depth=2**memory_address_width`，对 `memory_address_width=4, memory_data_width=8` 打印 `round(sin((i+0.5)*π/2/depth) × 127)` 的 16 个值。它们应全是非负、单调增、最后一个接近 127。
2. 读 `tb_sine_lookup.vhd`（同目录）与 module 脚本里的 [`lookup_post_check`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L230-L303)：它会把 sine / cosine / minus_sine / minus_cosine 四路谱线两两比较，断言「四路 SNDR 完全相等」，并断言 ENOB ≈ `memory_data_width + 1`。
3. 运行该测试（命令形如 `python tools/simulate.py sine_generator.tb_sine_lookup`，**待本地验证**确切过滤器名）。

需要观察的现象 / 预期结果：四路信号频谱完全一致（仅相位差 \(\pi/2\) 的整数倍），SNDR ENOB 落在 `(memory_data_width+1) × [0.99, 1.01]` 区间。步骤 1 的手算值应与综合期 ROM 内容吻合。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ROM 要偏移半个 LSB（`phase_offset = index_phase_increment / 2`）？
**答案**：为了让采样点关于 \(0、\pi/2、\pi、3\pi/2\) 镜像对称，这样象限间的「反序读取 + 取负」才能严格成立，避免接缝处的跳变尖刺。

**练习 2**：同时启用正弦和余弦，为什么需要**两个** ROM 读口？
**答案**：因为同一拍里正弦地址和余弦地址往往不同（一个原序一个反序），需要分别寻址，即两个读端口；但这不增加存储，只增加读逻辑（头注释原话）。

**练习 3**：BRAM 输出寄存器（`memory_sine` 那一拍）带来的代价与收益各是什么？
**答案**：多一拍延迟、可能多占 BRAM 内部的输出寄存器级；收益是输出时序路径变短，利于高频综合收敛。

### 4.3 分数相位：截断误差与相位抖动

#### 4.3.1 概念说明

当 `phase_fractional_width > 0`，累加器带着小数位，频率分辨率变细。但查表地址只能用整数位——`sine_calculator` 会**截断**小数部分。被截掉的小数部分决定了「这一拍本应落在两个 ROM 点之间的什么位置」，丢掉它等于把相位量化粗化了。结果是周期性的相位误差，在频谱上表现为杂散尖刺，把 SFDR 钉死在大约 `memory_address_width + 1` ENOB。

**相位抖动（dither）** 是补救办法之一：用一个伪随机数（来自 `lfsr` 模块的最大长度 LFSR）加到累加器的**小数部分**上，再进查表。这个抖动量均匀分布在「0 到将近 1 个地址 LSB」之间。它的作用不是消除误差，而是**把集中的尖刺能量摊成均匀底噪**——于是最大尖刺（SFDR）下降，但总噪声（SNDR）略升。源码头注释配了一张对比图 `dithering_zoom.png` 直观展示。

抖动与泰勒展开**互斥**：抖动会把泰勒展开赖以修正的「可预测的小数误差」打成噪声，反而毁掉泰勒的精度。顶层用 `assert ... severity failure` 禁止同时开启二者。

#### 4.3.2 核心流程

1. 累加器得到带小数的相位（在顶层 `sine_generator`）。
2. 若 `enable_phase_dithering`：用 `lfsr_fibonacci_multi` 生成 `phase_fractional_width` 位伪随机数，加到累加器上（可能溢出到整数位，从而 ±1 LSB 地址，这是期望行为）。
3. 把（可能抖动过的）相位交给 `sine_calculator`；后者截断小数位作查表地址。
4. 频谱后检查会比对「开抖动 vs 不开抖动」的 SFDR/SNDR 变化。

#### 4.3.3 源码精读

**抖动的生成与叠加**——全在顶层的条件 `generate` 里：

- [顶层断言：抖动需 ≥1 位小数、且 ROM 别太小](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L230-L237)：`memory_address_width >= 6`，否则抖动效果不好。
- [实例化 `lfsr.lfsr_fibonacci_multi`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L241-L253)：`minimum_lfsr_length => 10`、`output_width => phase_fractional_width`。这里复用了 `u6-l3` 讲过的 LFSR 模块——`minimum_lfsr_length` 保证序列足够长、随机性够。
- [`assign_phase` 进程：把抖动加到累加器上](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L259-L269)：注释明确「this might overflow, but this is desired behavior」（在最后一个地址附近会因抖动来回跳，正常）。
- [不开抖动时直接透传](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L273-L278)：`phase <= phase_accumulator`。
- [抖动与泰勒互斥的断言](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_generator.vhd#L157-L159)：「Dithering will ruin the performance of Taylor expansion. Do not enable both.」

**`sine_calculator` 如何切分整数/小数相位**——

- [整数位与小数位的切片](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L240-L244)：`input_phase_integer` 取高位（含象限位）作查表输入，`input_phase_fractional` 取小数位（仅泰勒展开会用，见 4.4）。
- [把整数相位喂给 `sine_lookup`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L222-L238)：注意 `enable_sine => enable_sine or enable_first_order_taylor`——开泰勒时即使不要正弦输出，也得查正弦（当余弦的导数用），余弦同理。
- [不开泰勒时直接输出查表值](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L466-L483)：`result_valid <= lookup_valid`，正余弦按需透传。这条路径下，分数相位的 SFDR 就被截断限制在 `memory_address_width + 1` ENOB。

#### 4.3.4 代码实践

实践目标：用现成测试观察抖动对 SFDR/SNDR 的一升一降。

操作步骤：

1. 在 `module_sine_generator.py` 的 [`_setup_generator_tests`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L116-L155) 里，看 `enable_phase_dithering` 与 `enable_first_order_taylor` 的双重循环如何枚举出「不开 / 开抖动 / 开泰勒」三组配置。
2. 读期望 KPI 函数 [`get_expected_kpi`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L910-L933)：分数相位基线 `sndr_enob = sfdr_enob = memory_address_width + 1`；开抖动 `sndr_enob -= 0.5`、`sfdr_enob += 3`。即抖动预期让 SFDR 多约 3 ENOB、SNDR 少约 0.5 ENOB。
3. 跑两组同参数配置：`Config(integer_increment="000010000", fractional_increment="1000")`，分别在 `enable_phase_dithering=False/True` 下生成（**待本地验证**测试过滤字符串）。比较 `SineGeneratorResult` 打印出的 SNDR/SFDR。

需要观察的现象 / 预期结果：开抖动后频谱上的杂散尖刺明显压低（SFDR↑），但底噪整体抬高（SNDR↓）；两组的基频峰值频率不变（抖动不改平均频率）。

#### 4.3.5 小练习与答案

**练习 1**：抖动量为什么只加在「小数部分」宽度上，而不是全相位？
**答案**：抖动的目的是打乱「截断小数位」造成的周期性误差，幅度只需覆盖将近 1 个地址 LSB，所以用 `phase_fractional_width` 位的均匀随机数即可；更大没有额外收益，只会徒增噪声。

**练习 2**：抖动让 SFDR 升、SNDR 降，这「值不值」由谁决定？
**答案**：由应用决定。若系统对「最大杂散尖刺」敏感（如频谱纯净度），值得；若对「总噪声」敏感，则不值。源码把选择权交给用户的 generic（头注释原话「the choice is left to the user」）。

**练习 3**：为什么 `memory_address_width < 6` 时不让开抖动？
**答案**：ROM 太小时地址 LSB 对应的相位跨度大，抖动在「最后一个地址」附近会频繁溢出回绕，随机化效果差，SFDR 提升不稳定（源码注释提到 4 位时偶有 0.5 dB 偏低）。

### 4.4 一阶泰勒展开：把截断误差算回来

#### 4.4.1 概念说明

抖动是「把误差藏进噪声」，泰勒展开则是「把误差算回来」。利用泰勒级数在查表点 \(a\)（整数相位，即 ROM 地址）附近展开真实相位 \(x = a + e\)（\(e\) 是被截断的小数部分）：

\[
f(x) \approx f(a) + f'(a) \cdot (x - a) = f(a) + f'(a) \cdot e
\]

对正弦 \(f(\theta)=A\sin(B\theta+C)\)，导数 \(f'(\theta)=A B \cos(B\theta+C)\)。其中 \(B\) 是查表的相位步长：

\[
B = \frac{\pi/2}{2^{\text{memory\_address\_width}}}
\]

于是修正项 = `查表余弦 × B × 小数相位误差`。注意：**修正正弦要用余弦作导数，修正余弦要用正弦作导数**（差一个符号）。这正好解释了 4.3 里「开泰勒时即使不要正弦也得查正弦」。

泰勒展开同时提升 SNDR 和 SFDR，代价是几个 DSP48 乘法器。期望精度翻倍：`sfdr_enob = 2 × (memory_address_width + 1)`。

#### 4.4.2 核心流程

`sine_calculator` 把一阶泰勒分成两段，并复用专门的 `taylor_expansion_core`：

1. **第一段（在 `sine_calculator`）**：算 `error_factor = 小数相位 × scale_factor`，其中 `scale_factor` 是定点化的 \(\pi/2\)（16 位小数）。这一拍**比查表结果早一拍**算好并打拍对齐。
2. **第二、三段（在 `taylor_expansion_core`，`latency=2`）**：`derivative_term = error_factor × derivative`（查表余弦/正弦）；把 `查表正弦` 左移到与导数项小数点对齐；相加（余弦修正时相减）；最后用 `math.saturate_signed` 饱和到结果位宽。

每一步都用 `attribute use_dsp of ... is "yes"`（来自 `u2-l2` 的 `attribute_pkg`）把乘法和加法钉进 DSP48，并有一堆 `assert` 保证操作数宽度（≤25、≤18、<48）正好贴合 DSP48E1/E2 的 25×18+48 结构。

#### 4.4.3 源码精读

**`sine_calculator` 里的第一段**——

- [scale_factor 定点化](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L261-L267)：`scale_factor_real = (π/2) × 2**16`，round 成整数——17 位，注释说「maps nicely to a DSP48E1 (25x18) or DSP48E2 (27x18)」。\(\pi/2\) 已经包含了上面公式里的 \(B\) 中「\(\pi/2\)」部分，而「\(2^{-\text{address\_width}}\)」部分用移位实现（注释「realize as a shift」）。
- [误差因子宽度与裁剪](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L273-L295)：把 `phase_error` 裁到 ≤24 位、`error_factor` 裁到 ≤25 位以贴合 DSP48，并算出剩余小数位数 `error_factor25_fractional_width` 供第二段对齐用。
- [第一段乘法 + 提前一拍对齐](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L350-L414)：`error_factor_unsigned <= phase_error24 * scale_factor;`，并用 `phase_error24_pipe` 把误差值打 `lookup_latency - 1` 拍，使其与查表结果同拍到达核心。`attribute use_dsp` 标注两个中间信号。
- [正弦泰勒核心实例化](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L418-L439)：`input_value => lookup_sine`、`input_derivative => lookup_cosine`、`minus_derivative => false`。
- [余弦泰勒核心实例化](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/sine_calculator.vhd#L443-L464)：`input_value => lookup_cosine`、`input_derivative => lookup_sine`、`minus_derivative => true`——余弦导数是负正弦，故取减。

**`taylor_expansion_core` 的第二、三段**——

- [实体与延迟 2](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/taylor_expansion_core.vhd#L35-L56)：头注释警告「This is an internal core. The interface might change without notice.」
- [对齐用的小数点填充](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/taylor_expansion_core.vhd#L108-L116)：`value_term_padding` 在低位填 `100...0`，实现「+0.5 舍入」以提升精度。
- [第二段：导数项乘法](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/taylor_expansion_core.vhd#L174-L188)：`derivative_term <= derivative18 * input_error_factor;`，同时把 `value` 项打拍以利用 DSP48 输入寄存器。
- [第三段：相加/相减并饱和](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/src/taylor_expansion_core.vhd#L247-L285)：`minus_derivative` 为真则 `sum <= value_term - derivative_term`，否则相加。注释指出求和几乎在每个峰值处都会溢出，故实例化 `math.saturate_signed`（承接 `u2-l3`）把结果钳到 `result_width` 位，溢出极小、SFDR 仍达标。

#### 4.4.4 代码实践

实践目标：对比「分数基线 vs 开泰勒」的 SFDR，并对照资源回归看 DSP 代价。

操作步骤：

1. 读资源回归配置（在 `get_build_projects` 里）：
   - 分数 + 抖动、`address=8, frac=5`：[54 LUT / 51 FF / 0 DSP / 1 RAMB18](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L447-L459)。
   - 分数 + 泰勒、`address=8, frac=5`：[100 LUT / 38 FF / 2 DSP / 1 RAMB18](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L474-L486)。
2. 读期望 KPI：开泰勒 `sfdr_enob = 2 × (memory_address_width + 1)`（[`get_expected_kpi`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L910-L933) 第 924-926 行），相比分数基线（`address+1`）翻倍。
3. 跑两组同地址宽度的分数配置，一组 `enable_first_order_taylor=False`、一组 `True`，比较打印出的 SFDR ENOB 与 netlist 资源（**待本地验证**）。

需要观察的现象 / 预期结果：开泰勒后 SFDR 约翻倍（按 ENOB 计），代价是 +2 个 DSP（地址宽 8 时）；同时 `result_valid` 比 baseline 多约 `2 + 1` 拍（核心延迟 2 + 第一段对齐 1）。

#### 4.4.5 小练习与答案

**练习 1**：为什么修正正弦用余弦作导数、修正余弦却要把导数取负？
**答案**：\(\sin' = \cos\)，所以修正正弦的导数项是 \(+\cos \cdot e\)；而 \(\cos' = -\sin\)，修正余弦的导数项是 \(-\sin \cdot e\)，对应 `minus_derivative => true`。

**练习 2**：`scale_factor` 为什么固定用 16 位小数？
**答案**：经验值（源码「trial-and-error」），在所有性能模式下都不构成瓶颈；同时让 `scale_factor` 恰为 17 位，完美贴合 DSP48 的 18 位乘数输入。

**练习 3**：泰勒结果为什么要过一道 `saturate_signed`？
**答案**：两个定点评价值相加在峰值附近会略微溢出结果位宽；直接截断会产生尖刺，而饱和把溢出钳到满量程，溢出量极小、SFDR 仍满足预期（源码注释原话）。

## 5. 综合实践

把四个最小模块串起来，完成一项端到端验证。项目已经为你备好了所有脚手架（testbench + Python 后检查），你要做的是「读懂配置 → 跑两组对比 → 解释结果」。

**任务**：用现成的 `tb_sine_generator`，针对同一个目标频率，分别在「整数相位」「分数相位 + 泰勒」两种模式下生成正弦样本，验证输出频率符合公式，并解释 SFDR 差异的来源。

**操作步骤**：

1. 选定参数：`clk_frequency_hz = 2**27`。整数模式取 `memory_address_width=8, phase_fractional_width=0`，选一个整数步长（如 `integer_increment="00010"` 对应的配置）。分数模式取 `memory_address_width=8, phase_fractional_width=5, enable_first_order_taylor=True`。
2. 看 [`_setup_generator_tests` 如何由 `integer_increment`/`fractional_increment` 反算 `sine_frequency_hz`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L87-L102)：`sine_frequency_hz = clk × phase_increment_int / 2**phase_width`，并据此算 coherent sampling 的样本数（[`get_coherent_sampling_count`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L562-L594)），保证 FFT 无泄漏。
3. 看 testbench [把样本写进 `sine.raw`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/test/tb_sine_generator.vhd#L83-L117)，再看 Python 端 [`load_simulation_data` 用 numpy 读回](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L353-L362)。
4. 运行两组仿真（命令形如 `python tools/simulate.py sine_generator.tb_sine_generator`，**待本地验证**），观察 `SineGeneratorResult` 打印的「peak frequency / SNDR / SFDR / THD」四行 KPI（[`get_status_string`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L981-L1003)）。
5. （可选）给 `tools/simulate.py` 传 `--inspect` 让后检查用 matplotlib 画出时域波形与频谱（代码里 `if inspect:` 分支）。

**预期结果与判据**：

- 两组的 **peak frequency** 都应落在 `sine_frequency_hz × [0.9999, 1.0001]` 区间内（[`SineGeneratorResult.__init__` 的断言](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/sine_generator/module_sine_generator.py#L890-L895)）——这验证了「相位累加 → 输出频率」的公式。
- 整数模式的 SFDR/SNDR ≈ 19 ENOB（受 `memory_data_width=18` 限制，见 `get_expected_kpi` 的整数分支）；分数 + 泰勒模式的 SFDR ≈ `2×(8+1)=18` ENOB、SNDR ≈ `2×9−0.6=17.4` ENOB。
- 解释：整数模式没有截断误差，唯一噪声是 ROM 字宽量化；分数模式本会被截断限制在 `address+1=9` ENOB，泰勒展开把误差算回来，使 SFDR 翻倍到 18 ENOB，代价是 netlist 里多出 2 个 DSP（见 4.4 实践）。

若手头没有 Vivado/仿真器，步骤 1-3 的「读源码、手算期望值」部分仍可独立完成；只有跑仿真那一步标注为「待本地验证」。

## 6. 本讲小结

- `sine_generator` 是一个 DDS/NCO：核心是**相位累加器**，每拍加 `phase_increment`，溢出回绕即对应正弦周期；输出频率 \(f_{\text{sine}}/f_{\text{clk}} = \text{phase\_increment}/2^{\text{phase\_width}}\)，用 `get_phase_increment` 算，受 Nyquist 约束（最高位恒 0，可省 1 个 LUT）。
- `phase_width = memory_address_width + 2 + phase_fractional_width`，「+2」是象限指示位，`phase_fractional_width > 0` 进入分数相位模式。
- `sine_lookup` 用**四分之一象限对称**只存 \([0, \pi/2)\) 的正弦样本，靠「反序读取 + 象限取负」还原全程正余弦；BRAM 输出寄存器改善时序；整数相位模式下 SNDR = SFDR = `memory_data_width + 1` ENOB。
- 分数相位模式下，查表前**截断小数位**会引入周期性杂散，把 SFDR 钉在约 `memory_address_width + 1` ENOB。
- **相位抖动**（LFSR 伪随机加到小数相位）把杂散摊成底噪：SFDR↑、SNDR↓，与泰勒互斥。
- **一阶泰勒展开**（`查表值 + 导数 × 误差`，全塞进 DSP48）把截断误差算回来：SFDR 与 SNDR 都翻倍，代价是几个 DSP；末尾用 `saturate_signed` 钳位。
- 项目用 VUnit 枚举 generic 矩阵 + Python FFT 后检查把 SFDR/SNDR 纳入回归，用 netlist 构建的 `build_result_checkers` 把 LUT/FF/DSP/RAM 也纳入回归——既保性能又保面积。

## 7. 下一步学习建议

- **横向复用**：本模块重用了 `lfsr`（抖动）、`math`（`saturate_signed`、`num_bits_needed`）、`common`（`use_dsp`、`unsigned_vec_t`）。如果想夯实这些基础，回头看 `u6-l3`（LFSR）和 `u2-l3`（math）。
- **验证方法论**：本讲的频谱后检查是「VUnit + numpy/scipy 后检查」的范本，`u8-l1`/`u8-l2` 会系统讲 BFM 与 VUnit testbench 模式；`u8-l3` 讲 netlist 资源回归（本讲的 `get_build_projects` 就是其中一例）。
- **进阶阅读**：直接读 `modules/sine_generator/doc/sine_generator.rst`（网站文档源），里面有不同 `memory_address_width` / 分数位宽下的 SFDR/SNDR 性能曲线图，以及相位抖动的系统级视角（`sine_phase_dithering`）。也可以阅读被注释掉的「极端位宽」测试配置（`module_sine_generator.py` 末尾），体会性能上限验证的方法。
- **动手扩展**：尝试给 `sine_calculator` 增加一种「把误差项也存 ROM」的替代实现（头注释提到的 future generic），对比 DSP vs BRAM 的资源取舍。
