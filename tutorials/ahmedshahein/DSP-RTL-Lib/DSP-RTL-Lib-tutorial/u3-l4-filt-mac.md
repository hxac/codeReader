# filt_mac — 资源共享型 MAC FIR

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `filt_mac` 为什么能用**一个乘法器 + 一个累加器**实现完整 FIR 卷积，以及它为此付出的吞吐代价。
- 读懂 `filt_mac` 的三大机制：**静止延迟线 + 抽头计数器**、**单乘加累加器**、**`done`/`load` 节拍握手**。
- 理解对称预加与中间抽头特判（`c_even_odd_symm`）如何在串行架构里复用。
- 能够估算给定抽头数下 `filt_mac` 产出一个有效样本所需的时钟周期数，并与 `filt_fir` 的并行多乘法器架构做面积—吞吐取舍的对比。

## 2. 前置知识

本讲承接以下已建立的认知（不再重复证明，只引用结论）：

- **FIR 卷积**（u3-l1）：\(y[n]=\sum_{k=0}^{N-1} h[k]\cdot x[n-k]\)，硬件由「延迟线 + 乘法 + 加法」构成。
- **并行拓扑 TF/DF**（u3-l2）：`filt_fir` 用 `gp_tf_df` 在编译期二选一，部署 **N 个并行乘法器**，吞吐 **1 样本/时钟**，面积随抽头数线性增长。
- **对称系数**（u3-l3）：线性相位 FIR 满足 \(h[k]=h[N-1-k]\)，可先「预加」\(x[n-k]+x[N-1-k]\) 再相乘。
- **`dff` 原语与定点位宽**（u2-l1、u2-l2）：异步低有效复位、同步高有效使能、补码定点、`$signed`/`$clog2` 的位宽推导。

本讲要回答的核心问题是：**如果我的数据率很慢、但硅片面积很贵，能不能把 N 个乘法器压缩成 1 个？** 答案就是 `filt_mac`——它站在 u3-l2「面积换速度」谱系的**另一端**：用吞吐换面积。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [.drl_src_code/filt_mac/rtl/filt_mac.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v) | 本讲主角。资源共享型（串行）FIR，单乘法器 + 单累加器分时复用。 |
| [.drl_src_code/filt_mac/octave/gen_coeffs.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/octave/gen_coeffs.m) | Octave 黄金参考模型（GRM）侧的系数生成器，把量化整数写成 `filt_coeff.v`。对称时只写半数系数，与 RTL 严格对齐。 |
| [.drl_src_code/filt_mac/sim/testbench/filt_mac_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/sim/testbench/filt_mac_tb.sv) | 测试台。其中 `CLK_CYCLES` 一行是本讲「每样本周期数」结论的权威出处。 |
| [.drl_src_code/filt_fir/rtl/filt_fir.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v) | 对照组。并行多乘法器 FIR，用于面积/吞吐对比。 |
| [.drl_param/filt_mac_1.param](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_param/filt_mac_1.param) | 默认回归参数：8 位输入、17 抽头、12 位系数、对称。 |

> **术语提示**：MAC = Multiply-ACCumulate（乘加）。一个「MAC 单元」= 一个乘法器后接一个累加器。并行 FIR 有 N 个 MAC 并排同时算；`filt_mac` 只有 **1 个 MAC**，靠分时复用串行算完 N（或 ⌈N/2⌉）次。

---

## 4. 核心概念与源码讲解

### 4.1 延迟线与抽头计数器

#### 4.1.1 概念说明

并行 `filt_fir` 里，N 个乘法器在同一时刻分别读取延迟线的 N 个抽头，于是延迟线必须**每个时钟移位一次**，让新样本流过所有抽头。

`filt_mac` 只有一个乘法器，一个时钟只能算一个抽头。要算完整卷积，乘法器必须**逐拍轮询**延迟线的不同位置：

\[
\text{第 0 拍算 } h[0]\cdot x[n],\quad \text{第 1 拍算 } h[1]\cdot x[n-1],\quad \dots
\]

这带来一个关键约束：**在轮询的这几拍里，延迟线的内容必须保持静止**——否则还没轮询到后面的抽头，前面的样本就被新数据冲掉了。所以 `filt_mac` 的延迟线**不是每拍移位**，而是**每个输出样本移位一次**（算完整组卷积才推进）。

同时，乘法器需要一个「地址」来告诉它这一拍该读哪个抽头——这就是**抽头计数器** `r_count_coeff`。

#### 4.1.2 核心流程

```
每个使能时钟沿：
  1. 抽头计数器 r_count_coeff 自增（或回绕）
  2. 若 r_count_coeff 已到末位（w_done=1）：
       - 延迟线整体右移 1 位，新样本 i_data 进入 [0]
       - 下一拍计数器归零，开始新一轮卷积
     否则：
       - 延迟线保持不动（关键！）
```

#### 4.1.3 源码精读

延迟线用一个**显式数组**而非 `dff` 级联实现，这是因为 MAC 需要**随机访问任意抽头**（`r_delay_line[r_count_coeff]`），而 u2-l3 的 `shift_register` 原语只暴露末级、中间级藏在打包线里、无法按索引读。

延迟线与计数器声明：

[.drl_src_code/filt_mac/rtl/filt_mac.v:28-31](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L28-L31) — `r_delay_line` 是长度为 `gp_coeff_length` 的样本数组；`r_count_coeff` 是抽头地址计数器。

延迟线移位块——注意移位被 `w_done` 门控：

[.drl_src_code/filt_mac/rtl/filt_mac.v:44-60](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L44-L60) — 只有 `i_ena && w_done` 时才把 `i_data` 灌入 `[0]` 并用 `for` 循环把各级上移一位；其余时钟沿数组原样保持，供 MAC 轮询。

抽头计数器——它才是每个使能时钟都动的：

[.drl_src_code/filt_mac/rtl/filt_mac.v:63-74](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L63-L74) — 复位时置为全 1（`{c_count_width{1'b1}}`，见下文「暖机」说明）；使能后若未到 `c_coeff_size-1` 则自增，到末位则归零，形成 `0 → 1 → … → c_coeff_size-1 → 0` 的循环。

计数器位宽由 `$clog2` 自动推导：

[.drl_src_code/filt_mac/rtl/filt_mac.v:22-26](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L22-L26) — `c_count_width = $clog2(c_coeff_size)` 恰好够表示 `0 … c_coeff_size-1`；`c_coeff_size` 在对称模式下取 `⌈N/2⌉`，故计数器只数半数抽头。

> **关于复位值「全 1」的暖机（warm-up）**：复位后 `r_count_coeff` 是全 1（例如 `c_coeff_size=9` 时为 `4'b1111=15`）。第一个使能时钟沿上 `w_done` 因此已经为 1，但由于 `15 ≥ c_coeff_size`，乘法器输入 B 被选通为 0（见 4.2.3），这一拍乘出 0、输出寄存器锁存 0，是一个无害的启动拍；随后计数器归零进入正常轮询。这是「先归零再工作」的常见启动套路，理解即可，无需特别处理。

#### 4.1.4 代码实践（源码阅读型）

**目标**：看清「延迟线静止、计数器流动」这一对反差。

**步骤**：
1. 打开 [filt_mac.v 的延迟线块（L44-L60）](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L44-L60)，确认移位条件是 `if(w_done)`，而非 `if(i_ena)`。
2. 对比 [filt_fir.v 的直接型延迟线](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L122-L140)，确认那里每个 `dff` 每个使能时钟都在移位（无条件 `i_ena` 采样）。

**需要观察的现象**：
- `filt_mac`：`r_delay_line` 内容在连续 9 个时钟（17 抽头对称时）里**完全不变**，只在第 9 拍一次性更新。
- `filt_fir`：延迟线**每个时钟都变**。

**预期结果**：你能用一句话概括——「并行架构延迟线是流水的，串行架构延迟线是池塘」。这正是两种架构在时序上的根本区别。

**待本地验证**：若你有 iverilog，可在两个模块上各加一段 `$monitor` 打印延迟线首尾两级，观察上述节拍差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `filt_mac` 不复用 u2-l3 的 `shift_register` 原语做延迟线？
> **答**：因为 MAC 每拍要按 `r_count_coeff` **随机索引**读取任意一级（`r_delay_line[r_count_coeff]`），而 `shift_register` 只暴露末级输出、中间级埋在打包线里，无法按索引寻址。

**练习 2**：`c_coeff_size` 在 `gp_symm=1`、`gp_coeff_length=17` 时等于多少？计数器一个完整循环数几拍？
> **答**：`c_coeff_size = ⌊17/2⌋ + 17%2 = 8 + 1 = 9`，计数器循环 `0…8` 共 **9 拍**。

---

### 4.2 单乘加累加器

#### 4.2.1 概念说明

整条卷积被「时间展开」：原本 N 个乘法器同一拍并行完成的乘法，现在由 1 个乘法器在 N（对称时 ⌈N/2⌉）拍内串行完成；原本一棵加法树，现在换成**一个累加寄存器** `r_add_oup`，每拍把新乘积加进去：

\[
r\_add\_oup \leftarrow r\_add\_oup + h[k]\cdot (\text{预加样本})
\]

算完整组抽头后，`r_add_oup` 里就是完整的卷积结果，锁进输出寄存器，然后清零开始下一个样本。

对称预加（u3-l3）在这里**真正省下了乘法次数**：因为只有一个乘法器，把镜像对先加再乘，让乘法从 N 次降到 ⌈N/2⌉ 次——这直接成比例地提高了吞吐（周期数减半）。这与并行 `filt_fir` 不同：`filt_fir` 用对称只省系数 ROM、不省乘法器（仍是 N 个）。

#### 4.2.2 核心流程

```
每个使能时钟沿（计数器值为 k）：
  A = 对称预加：  若 k 是中间抽头 → r_delay_line[k]
                  否则            → r_delay_line[k] + r_delay_line[N-1-k]
  B = 系数：      c_coeff[k]
  乘积 = A * B
  r_add_oup <= r_add_oup + 乘积        （累加）
  若 w_done（k 到末位）：
       r_data   <= r_add_oup + 末位乘积   （最终结果）
       r_add_oup <= 0                     （清零，为下一样本准备）
```

#### 4.2.3 源码精读

**对称预加 + 中间抽头特判**（这是 u3-l3 的串行版落地）：

[.drl_src_code/filt_mac/rtl/filt_mac.v:77-89](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L77-L89) — 对称模式下，乘法器输入 A 由三目运算选择：当 `!c_even_odd_symm && k==c_coeff_size-1` 时取**中间抽头**单独（无镜像），否则取**镜像对的预加和**。非对称模式则直接取 `r_delay_line[k]`。

中间抽头标志 `c_even_odd_symm` 的定义（承接 u3-l3）：

[.drl_src_code/filt_mac/rtl/filt_mac.v:23](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L23) — `c_even_odd_symm = gp_symm && (gp_coeff_length 为偶数)`。**为 1（偶数长度）**：没有无配对中间抽头，全程镜像预加；**为 0（奇数长度）**：存在一个无配对的中间抽头，需在最后一拍特判。17 抽头是奇数，故 `c_even_odd_symm=0`，最后一拍取 `r_delay_line[8]` 单独乘。

> 预加和可能比单个样本多 1 bit（两个 gp_inp_width 位数相加），所以 `w_mul_inp_a` 声明为 `[gp_inp_width:0]`（即 gp_inp_width+1 位），见 [filt_mac.v:34](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L34)，与 u3-l3「预加和需比样本多 1 bit 防溢出」一致。

**系数选择、相乘、累加**：

[.drl_src_code/filt_mac/rtl/filt_mac.v:91-98](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L91-L98) — `w_mul_inp_b` 用 `r_count_coeff` 选系数；超出范围选 0；`w_mul_oup` 是有符号乘；`w_add_oup = w_mul_oup + r_add_oup` 是累加。注意这里没有 `o_data` 的位宽树推导差异——单累加器每拍加一次。

**累加寄存器（清零由 `w_done` 触发）**：

[.drl_src_code/filt_mac/rtl/filt_mac.v:101-112](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L101-L112) — `w_done` 时清零（为下一卷积准备），否则持续累加 `w_add_oup`。

**位宽：与 `filt_fir` 的关键差异**。串行累加与并行加法树的位宽推导不同：

[.drl_src_code/filt_mac/rtl/filt_mac.v:25-26](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L25-L26) — `filt_mac`：`c_add_oup_width = c_mul_oup_width + gp_coeff_length`，即累加器比乘积多 **N 位**（宽松上界）。

对比 [filt_fir.v:21-22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L21-L22) — `filt_fir`：`c_add_oup_width = c_mul_oup_width + $clog2(gp_coeff_length)`，加法树只多 **⌈log₂N⌉ 位**（紧界）。

以 8 位输入、12 位系数、17 抽头为例：

| | 乘积位宽 | 累加/加法增长 | 输出位宽 |
|---|---|---|---|
| `filt_fir`（并行树） | 20 | +⌈log₂17⌉ = +5 | **25** |
| `filt_mac`（串行累加） | 20 | +N = +17 | **37** |

`filt_mac` 用了更宽的累加器（保守留余量，绝对防溢出），多出的高位是符号扩展。这不影响正确性——卷积的**数值**相同，只是被符号扩展进更宽的字段，故仍是比特真的（数值相等即比对通过）。这也呼应 u2-l1「`$clog2` 与 `ceil(log2)` 等价、是比特真根基」：只要 RTL 与 GRM 用同一宽度公式，两侧就对齐。GRM 侧由 [gen_defines.m:9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/octave/gen_defines.m#L9) 用 `gp_data_width+gp_coeff_width+gp_coeff_length` 与 RTL 完全一致。

#### 4.2.4 代码实践

**目标**：用 GRM 的系数生成器，亲手生成一份 `filt_coeff.v`，理解「对称只写半数」与 RTL 的镜像下标如何配合。

**步骤**：
1. 阅读 [gen_coeffs.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/octave/gen_coeffs.m)：注意 [L5-L9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/octave/gen_coeffs.m#L5-L9) 当 `symm==1` 时 `filt_len = ceil(length(b)/2)`，只写半数系数。
2. 若本机有 Octave，执行下面**示例代码**生成一组 17 抽头对称系数：

```octave
% 示例代码：生成 17 抽头对称低通 FIR 的系数文件
b  = fir1(16, 0.25);            % 17 抽头（阶数 16）低通，天然对称
b  = round(2^11 * b / max(abs(b)));  % 量化到 12 位整数（示例，非项目脚本）
q  = 12;  symm = 1;
gen_coeffs(b, q, symm);         % 调用项目的 gen_coeffs.m
```

3. 打开生成的 `filt_coeff.v`，数一下 `assign c_coeff[i]` 的行数。

**需要观察的现象**：`c_coeff` 只有 **9 行**（`i = 0…8`），而非 17 行。

**预期结果**：`filt_coeff.v` 只写 `c_coeff[0]…c_coeff[8]`。RTL 在 [filt_mac.v:83](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L83) 用镜像下标 `gp_coeff_length-1-r_count_coeff`（即 `16-k`）取另一半样本，与这 9 个系数配对，重建完整 17 抽头卷积。`c_coeff[8]` 正是奇数长度的中间抽头。

#### 4.2.5 小练习与答案

**练习 1**：对称预加在 `filt_mac` 里省了什么？在 `filt_fir` 里又省了什么？为何不同？
> **答**：`filt_mac` 只有一个乘法器，预加把乘法次数从 17 降到 9，**直接省了一半乘法时间**（吞吐翻倍）；`filt_fir` 有 17 个并行乘法器，预加只省系数 ROM（系数数组从 17 缩到 9），**不省乘法器**，吞吐不变。

**练习 2**：为什么 `filt_mac` 没有 `gp_tf_df`（TF/DF 拓扑选择）参数？
> **答**：TF/DF 是**并行**架构中寄存器插在哪里的选择；`filt_mac` 只有一个乘法器、一条串行数据通路，不存在「乘加链 vs 延迟线加法树」的拓扑分歧，故无需该参数。

---

### 4.3 done/load 节拍控制

#### 4.3.1 概念说明

既然一个输出样本要花多拍才能算完，模块就需要一个**节拍信号**告诉外界（和内部各寄存器）：「这一组卷积算完了，结果是有效的」。这个信号就是 `o_done`（内部同名线 `w_done`）。

`w_done` 是整模块的心跳，它同时驱动四件事：

1. **延迟线推进**（下一组样本就位）；
2. **累加器清零**（为下一组卷积归零）；
3. **输出寄存器锁存**（把最终和打入 `o_data`）；
4. **告知外界**（`o_done` 脉冲，测试台据此喂下一个样本并比对结果）。

#### 4.3.2 核心流程

```
w_done = (r_count_coeff >= c_coeff_size - 1)   // 计数器到末位即为完成
每个 c_coeff_size 拍产生 1 个 w_done 脉冲 → 吞吐 = 1 样本 / c_coeff_size 拍
```

#### 4.3.3 源码精读

`w_done` 的产生：

[.drl_src_code/filt_mac/rtl/filt_mac.v:127](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L127) — 计数器到达 `c_coeff_size-1` 即拉高，每轮卷积末尾产生一个脉冲。

输出寄存器只在 `w_done` 时锁存（否则保持上一个结果）：

[.drl_src_code/filt_mac/rtl/filt_mac.v:115-124](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L115-L124) — 锁存 `w_add_oup`（此刻已累加完末位乘积，是完整卷积和）。

`o_done` 直接连出：

[.drl_src_code/filt_mac/rtl/filt_mac.v:130-131](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/rtl/filt_mac.v#L130-L131) — `o_done = w_done`，外界可直接用作「结果有效」握手。

**测试台如何用 `o_done` 控制节拍**——这是「每样本周期数」的权威出处：

[filt_mac_tb.sv:8-10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/sim/testbench/filt_mac_tb.sv#L8-L10) — 
```systemverilog
localparam CLK_CYCLES = (`P_SYMM) ? `DIV2(`P_COEFF_L) : `P_COEFF_L;
time F_CLK_PERIOD = 50;
time S_CLK_PERIOD = 50*CLK_CYCLES;
```
其中 [gen_defines.m:13](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/octave/gen_defines.m#L13) 定义 `` `DIV2(N) = (N/2)+(N%2) = ⌈N/2⌉ ``。于是：
- 对称 17 抽头：`CLK_CYCLES = ⌈17/2⌉ = 9`，慢时钟 `s_clk` 周期 = 9 个快时钟。
- 非对称 17 抽头：`CLK_CYCLES = 17`。

测试台每 `s_clk` 喂一个新样本、比对一次输出（[filt_mac_tb.sv:116-143](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/sim/testbench/filt_mac_tb.sv#L116-L143)），并用 `o_done` 启动响应文件读入（[filt_mac_tb.sv:80-84](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/sim/testbench/filt_mac_tb.sv#L80-L84)）。这从外部印证了 `filt_mac` 的吞吐恰好是 1 样本 / `CLK_CYCLES` 拍。

#### 4.3.4 代码实践（回答本讲核心问题）

**目标**：估算 17 抽头对称 `filt_mac` 产出一个有效样本所需时钟周期数，并与 `filt_fir` 对比。

**推导**：
- 对称 17 抽头 → `c_coeff_size = ⌈17/2⌉ = 9` → 计数器一个循环 9 拍 → **9 个快时钟产 1 个有效样本**。
- 非对称 17 抽头 → `c_coeff_size = 17` → **17 个快时钟产 1 个有效样本**。

**对照测试台验证**：`CLK_CYCLES = 9`（对称），`S_CLK_PERIOD = 50 × 9 = 450 ns`，而快时钟 `F_CLK_PERIOD = 50 ns`，恰好 9:1。✓

**与 `filt_fir` 的吞吐对比**：

| 配置：8 位输入、17 抽头、对称 | `filt_fir`（并行） | `filt_mac`（串行） |
|---|---|---|
| 乘法器数量 | 17 | **1** |
| 加法器 | 加法树（≈16 个） | **1** 累加器 |
| 每样本时钟数 | **1** | **9** |
| 最大采样率（同 f_clk） | f_clk | **f_clk / 9** |
| 输出位宽 | 25 | 37 |
| 拓扑选择 `gp_tf_df` | 有 | 无 |

**结论**：`filt_mac` 用约 **1/17 的乘法器**、**1/16 的加法器**，换取了 **9 倍**的处理时间。当数据率远低于时钟频率、且面积/功耗敏感时（如低速传感器前端、窄带信道选择），这是划算的；当需要满吞吐时则应选 `filt_fir`。

#### 4.3.5 小练习与答案

**练习 1**：若把 17 抽头对称配置改为非对称（`gp_symm=0`），每样本周期数变成多少？为什么对称能加速？
> **答**：变成 17 拍。对称通过预加把乘法次数减半（17 → 9），单乘法器串行架构下乘法次数直接等于周期数，故吞吐翻倍近一倍。

**练习 2**：`w_done` 在一个输出周期内高电平持续几拍？它若持续多拍高会不会导致延迟线多次移位？
> **答**：因为 `r_count_coeff` 到末位后下一拍立即归零，`w_done`（`r_count_coeff >= c_coeff_size-1`）每周期**只高 1 拍**（暖机首拍除外）。只高 1 拍保证延迟线、清零、锁存每个样本各触发一次。若假设它高多拍，延迟线会连推多拍、破坏卷积——这正说明计数器「到顶即归零」的设计是必需的。

---

## 5. 综合实践

**任务**：在草稿纸上为 17 抽头对称 `filt_mac` 手动追踪一个完整输出样本的内部状态，并完成面积—吞吐选型报告。

**步骤**：

1. **初始化**：设延迟线 `r_delay_line[0…16]` 已存有样本 `x[0…16]`（任取小整数，如 `x[k]=k-8`），`r_add_oup=0`。
2. **逐拍追踪**（共 9 拍，`r_count_coeff = 0…8`）：
   - 每拍写出 `w_mul_inp_a`（注意 k=8 是中间抽头，取 `r_delay_line[8]`；其余 k 取 `r_delay_line[k]+r_delay_line[16-k]`）、`w_mul_inp_b = c_coeff[k]`、乘积、累加后的 `r_add_oup`。
   - 第 9 拍（k=8）：记录 `r_data` 锁存的最终值，并确认 `r_add_oup` 被清零。
3. **核验**：把最终 `r_data` 与手算的 \(\sum_{k=0}^{16} h[k]\,x[16-k]\) 比较（注意 RTL 在 k 与 16-k 间镜像，等价于完整卷积），应当相等。
4. **选型报告**：写一段话，给出一个「应选 `filt_mac` 而非 `filt_fir`」的应用场景（提示：考虑采样率 vs 时钟频率的比值、面积预算、是否对称可线性相位）。

**预期结果**：你将直观看到「9 拍串行累加 = 1 次完整卷积」，并理解为什么 `filt_mac` 的延迟线在这 9 拍里纹丝不动。

**待本地验证**：用 iverilog 编译 `filt_mac`（参数取 `filt_mac_1.param`），喂一个单位脉冲，用 `$dumpvars` 观察波形，确认 `o_done` 每 9 个 `i_clk` 脉冲一次、且脉冲当拍 `o_data` 更新为脉冲响应的下一个抽头值。

---

## 6. 本讲小结

- `filt_mac` 用**1 个乘法器 + 1 个累加器**分时复用实现 FIR，把 N 个并行乘法器压缩成 1 个，是「面积—吞吐」谱系上**用吞吐换面积**的一端。
- **延迟线静止、计数器流动**：单 MAC 需逐拍轮询抽头，故延迟线只在每个样本（`w_done`）移位一次，而非每拍移位；延迟线用显式数组以支持按 `r_count_coeff` 随机索引。
- **对称预加在串行架构里真正省时间**：乘法次数从 N 降到 ⌈N/2⌉，吞吐近乎翻倍；奇数长度的中间抽头由 `c_even_odd_symm` 在末位特判。
- **`w_done`/`o_done` 是节拍心跳**：同时驱动延迟线推进、累加器清零、输出锁存与外界握手，每 `c_coeff_size` 拍产生一个有效样本。
- **17 抽头对称配置每样本 9 拍**（非对称 17 拍），由测试台 `CLK_CYCLES` 权威定义；`filt_mac` 的累加器位宽用保守的 `+N`（区别于 `filt_fir` 的 `+⌈log₂N⌉`），多出的高位是符号扩展，不影响比特真。

## 7. 下一步学习建议

- **进入多相滤波器（单元 5）**：多相分解（`filt_ppd`/`filt_ppi`）是「并行 + 分相」的另一条优化路线，用换向器（commutator）把长 FIR 拆成 M 路并行支路，兼顾吞吐与效率。建议先读 [filt_ppd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v)，对比它与 `filt_mac` 在「如何用少量硬件算长卷积」上的不同哲学。
- **验证方法学（单元 7）**：本讲多次引用测试台与 GRM 的节拍对齐，建议学习 [u7-l1 比特真验证方法论](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_mac/sim/testbench/filt_mac_tb.sv)，系统理解「GRM 生成激励/响应 → TB 逐样本比对 → `error_count` 判定」的闭环。
- **二次开发**：若想自行扩展（如改为半并行的 2-MAC 架构），可参考 [u7-l3 dev 模式脚手架](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/dsp_rtl_lib.sh)，以本讲的「延迟线 + 计数器 + MAC + done」四件套为模板改造。
