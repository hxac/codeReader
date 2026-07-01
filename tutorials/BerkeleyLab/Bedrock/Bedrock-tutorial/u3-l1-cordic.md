# CORDIC 核：Python 生成的全展开流水线

## 1. 本讲目标

本讲带你读透 Bedrock 的 `cordic` 子系统——一个用 Python 按数据位宽「现场生成」Verilog、并全展开成流水线的坐标旋转计算核。学完后你应该能够：

- 说清 CORDIC 算法的直觉（只做移位与加减法就能做旋转/求模/求角），以及 Bedrock 用「每级一个寄存器、全展开」的硬件实现策略。
- 读懂 `cordicgx.py` 如何按 DPW（数据通路位宽）生成 `cordicg_bN.v`，以及每个旋转级的常数 `atan(2^-i)` 是怎么预计算并烧进生成的 Verilog 里的。
- 读懂 `cstageg.v` 单级与 `addsubg.v` 原子构件，理解 `op` 端口的四种模式（旋转 / 向量 / 未用 / follow）。
- 跑通 `make -C cordic` 回归测试，并用 `cordic_check.py` 解读精度（peak/rms 误差）结果。
- 理解精度与资源的权衡：`width` / `nstg` / `def_op` 三个参数对面积、延迟、误差的影响。

本讲是 DSP 基础模块的第一讲。CORDIC 是后续 `mixer` / `rot_dds`（本振产生）、`fdownconvert`（下变频）等模块的「三角函数引擎」，所以把它放在最前面。

## 2. 前置知识

在开始前，先用通俗语言对齐几个概念：

- **CORDIC**（COordinate Rotation DIgital Computer）：一种只用「移位 + 加减法」就能计算三角函数、旋转、求模、求角的迭代算法。它最初是为没有乘法器的年代设计的，在 FPGA 里依然流行，因为移位和加法在硬件里几乎免费。
- **流水线（pipeline）**：把一个计算拆成多级，每级用一个时钟寄存器锁存中间结果。这样一来，虽然单个数据要经过很多拍才能算完（延迟 = 级数），但每个时钟都能「吃进一个新数据、吐出一个旧结果」，吞吐量是每拍 1 个样本。
- **全展开（fully unrolled）**：不写一个 `for` 循环在硬件里反复使用同一级，而是把每一级都画成独立的硬件，级与级之间用寄存器串起来。Bedrock 的 CORDIC 是全展开的：`nstg` 级 = `nstg` 份硬件。
- **定点（fixed-point）**：用整数表示小数。CORDIC 内部所有数都是固定位宽的整数，相位（angle）用「自然二进制单位」表示——绕一圈（\(2\pi\)）正好对应相位字回绕一次。
- **DPW（Data Path Width，数据通路位宽）**：CORDIC 内部计算用的位宽，比端口位宽 `width` 更宽，用来吃掉中间计算的截断误差。文件名里的 `_b22` 就是 DPW=22。

本讲默认你已经学过 **u2-l1（基于 Make 的 HDL 仿真测试方法）**，知道 `make xxx_check` 这种模式规则、`VFLAGS_$@` 按目标定制参数，以及 iverilog/vvp 的关系。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cordic/README](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/README) | 子系统的权威说明：定位、参数表、`op` 四种模式、精度回归结果、各代 FPGA 上的速度/资源对比。 |
| [cordic/Makefile](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile) | 编排「生成 → 编译 → 仿真 → 校验 → 综合」整条链，定义 `DPW`/`NSTG` 与四个 `_check` 回归目标。 |
| [cordic/cordicgx.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicgx.py) | **核心**：Python 全展开生成器，按 DPW 吐出 `cordicg_bN.v`，预计算 `atan` 常数。 |
| [cordic/cstageg.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cstageg.v) | 单级旋转单元（CORDIC 的一级移位-加减），含 follow 模式的本地状态。 |
| [cordic/addsubg.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/addsubg.v) | 受控加减法器：`control ? a+b : a-b`，是 CORDIC 与各级的原子构件。 |
| [cordic/cordic_wrap.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_wrap.v) | 综合用的最小包装，演示如何实例化 `cordicg_b22`（也是 `.bit` 综合目标的顶层）。 |
| [cordic/cordicg_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicg_tb.v) | 回归测试台：按 `+op=`/`+rmix=` 选择激励，对齐流水线延迟后打印输入/输出对照表。 |
| [cordic/cordic_check.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_check.py) | 后处理校验：用 numpy 的「黄金答案」对比仿真输出，输出 peak/rms 误差并判 PASS/FAIL。 |
| [cordic/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/rules.mk) | 一条模式规则 `cordicg_b%.v`，把「文件名里的数字」传给 `cordicgx.py`。 |

> 小贴士：本目录里的 `top_rules.mk` / `bottom_rules.mk` 是「脱离 Bedrock 单独玩」的极简版；在 Bedrock 上下文里它们会被 `build-tools/` 下的同名文件覆盖（见 Makefile 第 3–5 行的 `-include ../dir_list.mk` 与 `include $(BUILD_DIR)/top_rules.mk`）。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：① CORDIC 原理与 `op` 四种模式；② 单级旋转单元 `cstageg` 与 `addsubg`；③ Python 全展开生成器 `cordicgx.py`；④ Make 生成与仿真编排；⑤ 回归测试与精度校验。

### 4.1 CORDIC 原理与 op 四种模式

#### 4.1.1 概念说明

CORDIC 的核心想法很朴素：**任何角度都可以表示成一串预定的、越来越小的「基础角度」之和**，而且这些基础角度的 tangent 恰好是 2 的幂（\( \tan\alpha_i = 2^{-i} \)）。于是「乘以 \(\tan\alpha_i\)」就退化成「右移 \(i\) 位」，整个旋转只需要移位和加减法。

每级迭代做三件事（旋转模式，drive \(z\to 0\)）：

\[
\begin{aligned}
x_{i+1} &= x_i - \sigma_i \cdot 2^{-i} \cdot y_i \\
y_{i+1} &= y_i + \sigma_i \cdot 2^{-i} \cdot x_i \\
z_{i+1} &= z_i - \sigma_i \cdot \arctan(2^{-i})
\end{aligned}
\]

其中方向位 \(\sigma_i\in\{-1,+1\}\) 决定这一级顺时针还是逆时针转。迭代收敛后会带来一个固定的幅度增益：

\[
K_n = \prod_{i=0}^{n-1}\sqrt{1+2^{-2i}} \approx 1.64676
\]

这个 1.64676 会反复出现在源码里（校验脚本、调用方都要预先除掉它）。

Bedrock 的 CORDIC 通过 2 位 `op` 端口选择四种工作模式，且 **`op` 允许逐拍变化**——同一个流水线核可以交替做极坐标→直角坐标（P→R）和直角坐标→极坐标（R→P）。

#### 4.1.2 核心流程

按 `op` 分四种模式（对应 README 的说明）：

| `op` | 名字 | 行为 | 驱动哪个量到 0 |
| --- | --- | --- | --- |
| 0 | 旋转 P→R | 把 (x,y) 旋转 `phasein` 角度 | `phaseout` → 0 |
| 1 | 向量 R→P | 求 (x,y) 的模与角 | `yout` → 0 |
| 2 | 未用 | 保留 | — |
| 3 | Follow | 旋转角取「上一次操作的负值」，输入 phase 被忽略 | — |

- **P→R（op=0）**：令 `yin=0` 即得普通旋转；输出 `xout/yout` = 旋转后的向量，并被增益 1.64676 放大。
- **R→P（op=1）**：令 `phasein=0` 即得 `atan2(y,x)` 与模长。
- **Follow（op=3）**：用「反向重放上一次旋转」实现两遍 CORDIC 的效果，但只需一遍的延迟——这是 v25 加入的功能，硬件代价约为每级一个逻辑单元。

#### 4.1.3 源码精读

`op` 模式表与逐拍可变的说明在 README：

> [cordic/README:52-69](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/README#L52-L69) — 定义四种 `op` 模式，并强调 `op`「允许逐拍变化，可自由交织 R→P 与 P→R」；`def_op` 参数设置 `op` 端口的初值，常数用例可借它让综合器裁掉无用资源。

相位端口的「自然二进制单位」约定很关键——绕字一圈就是 \(2\pi\)：

> [cordic/README:40-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/README#L40-L50) — 相位用自然二进制单位（字回绕 = \(2\pi\) 回绕，因此可按有符号也可按无符号理解）；X/Y 恒为有符号；模块**不检测也不饱和溢出**，溢出会回绕成无意义值，所以调用方必须自己保证不溢出。

follow 模式的硬件代价与动机：

> [cordic/README:101-110](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/README#L101-L110) — v24→v25 把 `op` 从 1 位扩成 2 位以启用 follow 模式；follow 把「两遍 CORDIC」压成一遍，延迟减半、舍入误差更小；若 `op[1]` 恒接 0，综合器会自动剥离这部分硬件。

一个真实的调用方示例：DSP 里的 `rot_dds`（本振/DDS）用 P→R 模式把相位累加值变成正余弦，并预先把幅度除以 1.64676 以抵消 CORDIC 增益：

> [dsp/rot_dds.v:44-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v#L44-L48) — 实例化 `cordicg_b22 #(.nstg(20), .width(18), .def_op(0))`，`opin=2'b00`（P→R），`yin=0`，注释解释了为何要把幅度缩到 `2^17/1.64676=79594` 以下以避免溢出。

#### 4.1.4 代码实践

**目标**：用眼睛把 README 的模式表和真实调用方对上。

1. 打开 `cordic/README` 第 52–62 行的 `op` 模式表。
2. 打开 `dsp/rot_dds.v` 第 44–48 行。
3. 回答：`rot_dds` 用的是哪种模式？为什么 `yin` 接 `18'd0`？为什么幅度要先除以 1.64676？

**预期结果**：模式 0（P→R 旋转）；`yin=0` 是因为只想要「把相位旋转成 cos/sin」而不是旋转一个任意向量；预先除以 1.64676 是为了抵消 CORDIC 固有增益、避免输出溢出回绕。

#### 4.1.5 小练习与答案

**练习 1**：若调用方希望同时得到 (x,y) 的模长和角度，应设 `op` 为多少？`phasein` 该接什么？
**答案**：`op=1`（R→P 向量模式），`phasein` 接 0（若接非零，则该值会被加到输出角度上）。

**练习 2**：为什么 README 强调模块「不检测也不饱和溢出」对调用方是个陷阱？
**答案**：因为全展开流水线里溢出会直接回绕成无意义值且无法被发现，仿真看起来「有数」其实是错的；调用方必须自己保证 \(x^2+y^2\) 不超过满量程除以增益（README 给出 \(32767/1.64676 \approx 19897\) 这样的约束）。

---

### 4.2 单级旋转单元 cstageg 与原子构件 addsubg

#### 4.2.1 概念说明

CORDIC 的每一级都长得几乎一样：对 x/y 做一次「移位后加减」，对 z（相位累加器）做一次「加减一个预定的 atan 常数」。Bedrock 把这一级抽成一个可复用模块 `cstageg`，把最底层的「受控加减法」再抽成一个 12 行的小模块 `addsubg`。

`addsubg` 只做一件事：

\[
\text{sum} = \text{control} \;?\; (a+b) : (a-b)
\]

也就是用一个 `control` 位选择加还是减。CORDIC 的方向位 \(\sigma_i\) 就映射到这个 `control` 上。把乘 \(2^{-i}\) 实现成「右移 \(i\) 位 + 符号扩展」，于是整级没有任何乘法器。

#### 4.2.2 核心流程

`cstageg` 每拍做（伪代码）：

```
shifted_y = 算术右移 yin by shift 位（符号扩展）
shifted_x = 算术右移 xin by shift 位（符号扩展）
control   = 按 op 模式与输入符号决定方向（follow 模式用本地保存的上次方向）
xv = xin ± shifted_y     # addsubg ax，control 选符号
yv = yin ± shifted_x     # addsubg ay，~control 选符号
zv = zin ± ain           # addsubg az，ain 是本级 atan(2^-shift) 常数
在 posedge clk 把 xv/yv/zv/opout 锁存   # 一级 = 一个流水线寄存器
```

方向位的计算是理解四种模式的关键：

- `control_l = opin[0] ? ~yin[width-1] : zin[zwidth-1]`
  - `opin[0]=0`（P→R 旋转）：方向由 z 的符号决定 → 驱动 z→0。
  - `opin[0]=1`（R→P 向量）：方向由 y 的符号取反决定 → 驱动 y→0。
- `control = opin[1] ? ~control_h : control_l`
  - `opin[1]=1`（follow）：用本地保存的上次方向 `control_h` 取反，忽略当前输入方向。

#### 4.2.3 源码精读

`addsubg` 全部内容（注意端口是按位连接，参数 `size` 决定位宽）：

> [cordic/addsubg.v:3-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/addsubg.v#L3-L12) — 一个 `assign sum = control ? (a+b) : (a-b)`，是 CORDIC 各级唯一的数据运算构件。

`cstageg` 的方向逻辑（实现四种模式与 follow）：

> [cordic/cstageg.v:25-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cstageg.v#L25-L32) — `control_l` 按模式从 y 或 z 的符号取方向；`control` 在 follow 模式下改用本地保存的 `control_h`；三个 `addsubg` 分别算 x、y、z，其中 x/y 的第二操作数是「算术右移 + 符号扩展」。

`cstageg` 的流水线寄存器（每级一拍）：

> [cordic/cstageg.v:33-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cstageg.v#L33-L39) — 在 `posedge clk` 把组合结果 `xv/yv/zv` 与 `opin` 锁存为 `xout/yout/zout/opout`，同时把 `control_l` 存进 `control_h` 供下一拍的 follow 模式使用。

#### 4.2.4 代码实践

**目标**：在源码里验证「移位 + 符号扩展」确实取代了乘法器。

1. 阅读 [cordic/cstageg.v:30-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cstageg.v#L30-L31)。
2. 解释 `{{(shift){yin[width-1]}},yin[width-1:shift]}` 这个拼接表达式在做什么。

**预期结果**：它把 `yin` 算术右移 `shift` 位——高位用 `yin` 的符号位 `yin[width-1]` 复制 `shift` 份填充，低 `width-shift` 位取 `yin` 的高位部分。这正是「乘 \(2^{-shift}\)」的定点实现，完全没有乘法器。

#### 4.2.5 小练习与答案

**练习 1**：`cstageg` 里 x 和 y 的 `addsubg` 为什么一个用 `control`、另一个用 `~control`？
**答案**：因为旋转矩阵里 x 与 y 的更新方向相反（一个加移位项、一个减移位项），用一个原码、一个反码恰好实现这种「耦合」更新。

**练习 2**：follow 模式需要在每级保存什么本地状态？
**答案**：保存上一拍的方向位 `control_l` 到 `control_h`，follow 模式下用 `~control_h` 作为本次方向，从而实现「反向重放上次旋转」。

---

### 4.3 Python 全展开生成器 cordicgx.py

#### 4.3.1 概念说明

CORDIC 每级要加减的那个「预定 atan 常数」\(\arctan(2^{-i})\) 是固定的，可以在写代码之前就用 Python 算好，直接写死进 Verilog。更进一步，既然每级结构几乎一样、只是移位量 `shift` 和常数不同，干脆用 Python 把整个全展开流水线「打印」出来——这就是 `cordicgx.py`。

这样做的好处：

- **位宽可调**：DPW 不同，常数和位宽都不同；用代码生成比手写更可靠。
- **零隐藏状态**：v27 把原本藏在 `.vh` include 文件里的位宽/级数显式编进模块名（`cordicg_b22`）和参数（`nstg`），让大工程更容易集成。
- **生成物不入库**：`cordicg_b22.v` 由 Make 规则现场生成，不进源码 tar 包。

> 提醒：这是「示例代码生成器」而非「示例代码」——它生成的 Verilog 是真实上板/综合用的产物。

#### 4.3.2 核心流程

`cordicgx.py` 接收命令行第一个参数作为 DPW，第二个参数作为输出文件名，然后按顺序打印：

1. 模块头与参数表：`width=19`、`nstg=DPW-1`、`def_op=0`，端口 `opin[1:0]`、`xin/yin[width-1:0]`、`phasein[width:0]`（相位比数据宽 1 位）。
2. 输入缓冲级（routing）。
3. 第 0 级（旋转 0 或 180°，特殊处理）与第 1 级（移位量为 0，`cstageg` 不能直接表达，因此手写）。
4. **核心循环**：对 `i = 1 .. DPW-2`，预计算常数
   \[
   a_i = \mathrm{round}\!\left(\frac{\arctan(2^{-i})}{2\pi}\cdot 2^{\mathrm{DPW}+1}\right)
   \]
   并实例化一个 `cstageg`，把 \(a_i\) 当字面量烧进去。
5. 末尾的「四舍五入而非截断」输出级：从内部宽位取高 `width` 位并加上舍入位。

生成出来的是一个把所有级平铺、级间带寄存器的超大模块——「全展开」即此意。

#### 4.3.3 源码精读

模块名把 DPW 编进名字，参数表给出三个可调参数：

> [cordic/cordicgx.py:30-34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicgx.py#L30-L34) — `module cordicg_b%d` 把数据通路位宽编进模块名；默认 `width=19`、`nstg=DPW-1`、`def_op=0`。

端口定义（注意相位比数据宽 1 位）：

> [cordic/cordicgx.py:36-47](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicgx.py#L36-L47) — `xin/yin` 是 `[width-1:0]`，而 `phasein/phaseout` 是 `[width:0]`（多 1 位），呼应 README「相位端口比 X/Y 端口宽一位」。

「核心循环」预计算 atan 常数并实例化 `cstageg`：

> [cordic/cordicgx.py:108-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicgx.py#L108-L115) — `for ix in range(1, dpw-1)` 逐级生成 `cstageg`；第 110 行用 `numpy.floor(numpy.arctan((0.5)**ix)/(2*pi)*2**(dpw+1)+.5)` 把 \(\arctan(2^{-ix})\) 换算成相位字的整数刻度并四舍五入，再以 `%2d'd%-9ld` 格式烧成 Verilog 字面量。

末尾的舍入输出（作者注释承认它略费硬件但效果最好）：

> [cordic/cordicgx.py:121-127](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicgx.py#L121-L127) — 取 `xn[nstg-1]` 的高 `width` 位，并加上一位舍入位（`+ xfinal[dpw-width]`），实现「round, not truncate」，降低输出误差。

> 注：输出取自 `xn[nstg-1]`，所以 `nstg` 这个参数实际上决定了「用掉前多少级」；减小 `nstg` 会缩短流水线（延迟更小、资源更省），但精度下降。

#### 4.3.4 代码实践

**目标**：亲眼看到 Python 把常数「算出来再写死」。

1. 单独运行生成器并把结果打到屏幕：
   ```bash
   python3 cordic/cordicgx.py 22 | sed -n '80,120p'
   ```
2. 在输出里找到形如 `cstageg #(  1, 23, 22, def_op) cs 1 (` 的行，以及它第 5 个端口实参（那个 `23'd...` 字面量）。
3. 用 Python 自己验算第 1 级的常数：
   ```bash
   python3 -c "import numpy; print(int(numpy.floor(numpy.arctan(0.5)/(2*numpy.pi)*2**23+0.5)))"
   ```
   对比它是否等于生成 Verilog 里 `cs 1` 的那个字面量。

**预期结果**：两者一致——这就是「Python 预计算、Verilog 字面量烧死」的证据。（如本地未装 numpy，可记为「待本地验证」。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cordicgx.py` 要把 DPW 编进模块名 `cordicg_b22`，而不是用一个参数？
**答案**：因为位宽不同会导致内部连线宽度、常数刻度、生成级数全都不同，用参数难以在一份 Verilog 里表达；把位宽编进名字意味着「每个位宽一份独立生成的、自洽的」模块，且消除了 v27 之前 `.vh` include 的隐藏状态。

**练习 2**：把 DPW 从 22 调大到 28，常数 \(a_i\) 的数值刻度会怎么变？
**答案**：相位字刻度从 \(2^{23}\) 变成 \(2^{29}\)（公式里是 \(2^{\mathrm{DPW}+1}\)），同一个 \(\arctan(2^{-i})\) 会被表示成更大的整数，相位分辨率更高、精度更好，但资源也更多。

---

### 4.4 Make 生成与仿真编排

#### 4.4.1 概念说明

`cordicgx.py` 是「怎么生成」，`Makefile` 是「在什么时机、用什么参数生成并仿真」。本模块把生成、编译、仿真、校验四件事用 Make 串成一条依赖链——这正是 u2-l1 讲的 Make 测试方法学在 cordic 子系统里的具体落地。

一个关键细节：**位宽是由「目标文件名的数字」控制的，不是由 `DPW` 变量控制的**。模式规则 `cordicg_b%.v` 用自动变量 `$*`（stem，即文件名里的那个数字）作为参数传给 `cordicgx.py`。`DPW` 只决定「默认回归测试用哪个位宽」。

#### 4.4.2 核心流程

`make -C cordic` 的依赖链（简化）：

```
cordicgx.py ──(规则 cordicg_b%.v, $*→dpw)──▶ cordicg_b22.v
cordicg_b22.v + cstageg.v + addsubg.v ──(iverilog, VFLAGS_cordicg_tb)──▶ cordicg_tb
cordicg_tb ──(vvp +op=0/+op=1/+rmix=1/+op=3)──▶ cordic_{ptor,rtop,bias,fllw}.dat
.dat ──(python3 cordic_check.py)──▶ PASS/FAIL
```

四个回归目标覆盖四种模式：`ptor`（P→R）、`rtop`（R→P）、`bias`（下变频偏置）、`fllw`（follow）。

#### 4.4.3 源码精读

默认回归配置与「按目标定制」的 VFLAGS 钩子：

> [cordic/Makefile:14-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L14-L19) — `DPW=22`、`NSTG=20`；`CORDIC_BASE_V` 列出生成器产物与两个手写依赖；`VFLAGS_cordicg_tb = -DDPW=$(DPW) -pnstg=$(NSTG)` 把 DPW/nstg 通过宏与参数覆盖传进 testbench（这是 u2-l1 讲的 `VFLAGS = ${VFLAGS_$@}` 钩子的实例）。

顶层 `all` 聚合四个回归 check：

> [cordic/Makefile:7](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L7) — `all` 依赖四个 `_check` 目标加 `perf.png`，所以 `make` 一条命令就跑完四种模式的回归。

用 plusargs 选择激励（同一份 `cordicg_tb` 跑四种模式）：

> [cordic/Makefile:21-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L21-L31) — 四个 `.dat` 目标共享同一个 `cordicg_tb`，仅靠 vvp 的 `+op=0/+op=1/+rmix=1/+op=3` 命令行参数切换激励，输出重定向到不同 `.dat`。

「位宽编在文件名里」的生成模式规则：

> [cordic/rules.mk:1-2](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/rules.mk#L1-L2) — `cordicg_b%.v: cordicgx.py`，配方 `$(PYTHON) $< $* $@`：`$*` 是 stem（目标名里 `cordicg_b` 与 `.v` 之间的数字），`$@` 是目标文件名。**控制位宽的是这个 stem，不是 `DPW`。**

综合目标（非功能，只量速度/资源）：

> [cordic/Makefile:57-71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/Makefile#L57-L71) — 用 `cordic_wrap.v` 作顶层，跨四代 Xilinx 芯片（S3/S6/A7/K7）综合出 `.bit`，仅为测量最大速度与 LUT 用量（README 第 86–99 行给出对比表）。

#### 4.4.4 代码实践

**目标**：验证「位宽由目标文件名 stem 决定，与 `DPW` 无关」这一反直觉事实。

1. 先生成一份默认的 b22：
   ```bash
   make -C cordic cordicg_b22.v
   head -5 cordic/cordicg_b22.v
   ```
2. 再生成一份 b18（注意是改目标名，不是传 `DPW=18`）：
   ```bash
   make -C cordic cordicg_b18.v
   head -5 cordic/cordicg_b18.v
   ```
3. 对比两份文件开头的 `module cordicg_bNN` 与端口宽度。

**预期结果**：`cordicg_b18.v` 里模块名是 `cordicg_b18`，内部数据通路 18 位，atan 常数刻度为 \(2^{19}\)；`cordicg_b22.v` 是 22 位、刻度 \(2^{23}\)。`DPW` 变量对这两个生成目标**没有任何影响**——它只用于默认回归 testbench（`cordicg_tb`）。> 待本地验证：若你误用 `make -C cordic cordicg_b22.v DPW=18`，会发现生成的仍是 b22（stem=22），位宽并未改变。

#### 4.4.5 小练习与答案

**练习 1**：`make -C cordic cordicg_b28.v` 会生成什么样的模块？需要先改 `DPW` 吗？
**答案**：会生成 `cordicg_b28`（28 位数据通路、`nstg` 默认 27、常数刻度 \(2^{29}\)）；**不需要**改 `DPW`，因为位宽来自目标名的 stem。

**练习 2**：为什么 `VFLAGS_cordicg_tb` 里要同时传 `-DDPW=$(DPW)` 和 `-pnstg=$(NSTG)`？
**答案**：`-DDPW=$(DPW)` 让 testbench 里 ``cordicg_b`DPW`` 宏展开成正确的模块名（如 `cordicg_b22`）；`-pnstg=$(NSTG)` 覆盖 testbench 的 `nstg` 参数，使流水线延迟对齐 `cordic_delay`。

---

### 4.5 回归测试 cordicg_tb 与精度校验 cordic_check.py

#### 4.5.1 概念说明

CORDIC 是数值密集型模块，光「能跑」不够，必须证明「算得准」。Bedrock 的做法是**仿真与黄金答案分离**：

- `cordicg_tb.v`：跑大量随机/扫描激励，对齐流水线延迟后，把「输入 × CORDIC 输出」逐拍打印成文本表（不做任何判断）。
- `cordic_check.py`：读这个文本表，用 numpy 的 `cos/sin/arctan2/sqrt` 算出「理想答案」，对比后输出 peak/rms 误差并判 PASS/FAIL。

这样硬件只管吐数据、判断逻辑全在 Python，便于调阈值和换激励。

#### 4.5.2 核心流程

testbench 的数据流：

```
按 +op=/+rmix= 选激励模式
  └─ 在 always @(posedge clk) 里持续喂 xin/yin/phasein（op=1 时构造螺旋轨迹，op=3 时交织 follow/vector）
同时用 cordic_delay=nstg 深的环形缓冲延迟「输入快照」
  └─ $display 打印 [延迟后的输入 | 当拍 CORDIC 输出 | op]，列对齐即代表延迟匹配
```

校验脚本对每行：按 `op` 选公式（P→R 用旋转矩阵 + 增益 1.64676；R→P 用 `atan2`；follow 用 cos/sin），算误差，累加 peak/rms，最后与阈值比较。

#### 4.5.3 源码精读

testbench 的参数与 plusargs 解析：

> [cordic/cordicg_tb.v:14-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicg_tb.v#L14-L32) — `width=18`、`nstg=20`；用 `$value$plusargs("op=%d", op)` / `rmix=%d` 接收命令行激励选择，`$test$plusargs("vcd")` 控制是否 dump 波形。

DUT 实例化与流水线延迟对齐：

> [cordic/cordicg_tb.v:92-117](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordicg_tb.v#L92-L117) — `localparam cordic_delay = nstg`；用 32 深环形数组 `pp/xp/yp[31:0]` 把输入延迟 `nstg` 拍，再与当拍 `xout/yout/pout` 同行打印；README 也声明「Latency is nstg cycles」，两者一致。

校验脚本的增益常数与 PASS/FAIL 阈值：

> [cordic/cordic_check.py:3](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_check.py#L3) — `scale = 1.64676`，即 CORDIC 固有增益 \(K_n\)，对比 P→R 输出时要乘上它才得到理想值。

> [cordic/cordic_check.py:75-83](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/cordic_check.py#L75-L83) — peak 误差 > 0.035% 或 rms 误差 > 0.005% 即 FAIL；否则 PASS。README 第 126–160 行给出默认配置的实测：P→R peak 1.25 bit / rms 0.36 bit，R→P peak 1.06 bit / rms 0.36 bit，follow peak 3.27 bit / rms 0.41 bit，全部 PASS。

README 给出的理论下限帮你理解这些数字好不好：

> [cordic/README:163-165](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/cordic/README#L163-L165) — peak 误差理论下限 0.5 bit，rms 下限 \(1/\sqrt{12}\approx 0.29\) bit；实测 rms 0.36 bit 已非常接近下限。

#### 4.5.4 代码实践

**目标**：跑完整回归，读懂每个模式的精度数字。

1. 跑全部四种模式：
   ```bash
   make -C cordic clean all
   ```
2. 阅读输出里四段 `Check of ...` 后面的 `peak error` / `rms error` 行。
3. 回答：哪个模式误差最大？为什么？

**预期结果**：follow 模式（`cordic_fllw_check`）peak 误差最大（约 3.27 bit），因为 follow 等效于两遍 CORDIC 的合并，误差累积；P→R 与 R→P 的 rms 都约 0.36 bit，接近理论下限 0.29 bit。> 待本地验证：若缺少 iverilog/python3-numpy，对应步骤会失败而非被跳过（cordic 的这些目标是硬依赖）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 testbench 要用 `cordic_delay=nstg` 深的环形缓冲延迟输入？
**答案**：因为 CORDIC 是 nstg 级全展开流水线，输出比输入晚 nstg 拍；只有把输入快照延迟同样拍数再与输出同行打印，校验脚本才能逐行配对比较。

**练习 2**：把 `nstg` 从 20 减小到 10，`cordic_check.py` 报的误差会变大还是变小？
**答案**：变大。级数减少意味着角度收敛更粗糙、atan 近似更少，精度下降；同时资源与延迟减小。这正是 README 强调的「DPW 与 nstg 是精度/资源的权衡旋钮」。

---

## 5. 综合实践

把本讲的知识串起来：动手生成一个**非默认位宽**的 CORDIC，验证它的模块名/端口宽度/atan 常数刻度都正确，再换一个 `nstg` 跑回归看精度变化。

**操作步骤**：

1. 生成一个 DPW=18 的核，并查看它的接口与第 1 级常数：
   ```bash
   make -C cordic cordicg_b18.v
   sed -n '1,40p' cordic/cordicg_b18.v        # 看模块名、端口宽度
   grep -m1 'cs 1 (' cordic/cordicg_b18.v     # 看第 1 级 cstageg 的常数
   ```
2. 用 Python 验证这个常数的刻度是 \(2^{19}\)（DPW=18 → \(2^{\mathrm{DPW}+1}=2^{19}\)）：
   ```bash
   python3 -c "import numpy; print(int(numpy.floor(numpy.arctan(0.5)/(2*numpy.pi)*2**19+0.5)))"
   ```
3. 用更少的级数跑回归，观察精度变化（临时改 `NSTG`）：
   ```bash
   make -C cordic clean
   make -C cordic cordic_ptor_check NSTG=12
   make -C cordic cordic_ptor_check NSTG=20
   ```
4. 把两次的 `peak error` / `rms error` 抄下来对比。

**需要观察的现象**：

- `cordicg_b18.v` 的模块名是 `cordicg_b18`，`xin/yin` 是 18 位，`phasein` 是 19 位；第 1 级常数的数值刻度与第 2 步的 Python 输出一致。
- `NSTG=12` 的 peak/rms 误差明显大于 `NSTG=20`（级数少 → 精度差），但若你去看综合资源，前者更省。

**预期结果**：你会直观看到「位宽由文件名 stem 决定」「atan 常数由 Python 预烧」「nstg 是精度/资源旋钮」三件事在同一份生成物里如何体现。若 `NSTG=12` 仍能 PASS，说明即使级数减半，默认阈值下精度仍达标——这本身就是 CORDIC 收敛快的一个证据。> 任何一步因缺工具（iverilog/numpy）失败时记为「待本地验证」。

## 6. 本讲小结

- **CORDIC 用移位 + 加减法做旋转/求模/求角**，每级加减一个预定的 \(\arctan(2^{-i})\) 常数，固有增益 \(K_n\approx 1.64676\) 需调用方自行抵消。
- **`cordicgx.py` 是全展开生成器**：按 DPW 把所有级平铺打印成 `cordicg_bN.v`，atan 常数用 numpy 预算后烧成字面量；位宽编进模块名，消除了旧版的隐藏配置。
- **`cstageg.v` 是单级旋转单元**，`addsubg.v` 是底层 `control ? a+b : a-b` 原子构件；方向位 `control` 由 `op` 模式与输入符号共同决定，follow 模式靠每级保存的 `control_h` 实现。
- **`op` 端口 4 种模式**（旋转 P→R / 向量 R→P / 未用 / follow）可逐拍变化；`width`/`nstg`/`def_op` 是精度、延迟（≈nstg 拍）、资源的权衡旋钮。
- **位宽由 Make 目标名的 stem 控制**（`make cordicg_b18.v`），不是 `DPW`；`DPW` 只决定默认回归 testbench 用哪个核。
- **测试与校验分离**：`cordicg_tb.v` 只吐「延迟对齐的输入/输出对照表」，`cordic_check.py` 用 numpy 黄金答案算 peak/rms 误差并判 PASS/FAIL；默认配置 rms 误差 0.36 bit，接近理论下限 0.29 bit。

## 7. 下一步学习建议

下一讲 **u3-l2（混频器、DDS 与复数乘法）** 会用到本讲的 CORDIC：`rot_dds` 用 P→R 模式把相位累加器变成正交本振（cos/sin），再喂给 `mixer` 做实数混频、`complex_mul` 做复数乘法。建议带着这两个问题继续：

1. `rot_dds` 里 `cordicg_b22` 的输出（cos/sin）是如何被 `mixer` 当作本振使用的？
2. CORDIC 的固有增益 1.64676 在 `rot_dds`/`mixer` 的定点定标里是如何被预先除掉的？

如果你想更深入 CORDIC 本身，推荐读 README 引用的 Ray Andraka 教程 <http://andraka.com/cordic.php> 与 Wikipedia 条目，再回头对照 `cstageg.v` 的方向逻辑逐级手推一遍收敛过程。
