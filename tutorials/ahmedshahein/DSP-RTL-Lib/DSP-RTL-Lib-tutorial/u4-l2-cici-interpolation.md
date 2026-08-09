# filt_cici 插值滤波器与双时钟

## 1. 本讲目标

学完本讲,你应当能够:

- 说清 CIC **插值器**为何是抽取器的"镜像"——数据流顺序恰好反过来,变成「梳状 → ↑L → 积分器」。
- 看懂 `filt_cici` 如何用 **两个外部时钟** `i_clk`(慢)与 `i_fclk`(快)分别驱动梳状段与「上采样 + 积分器」段。
- 逐行理解 `upsample` 原语如何用"模 L 计数器 + 零填充移位寄存器"实现**零插值(zero-stuffing)**,以及 `gp_phase` 如何选择输出相位。
- 解释为什么**积分器永远跑在快时钟上**,并用测试台 `filt_cici_tb.sv` 验证双时钟的生成与跨域采样。

本讲是 u4-l1(CIC 抽取器)的姊妹篇。两讲共享同一套原语(`dff`、`shift_register`)和同一条 Hogenauer 位宽公式,差别只在"哪段跑快时钟、哪段跑慢时钟、变速器插在哪里"。建议先学完 u4-l1 与 u2-l3(`shift_register`/`upsample` 原语)再读本讲。

## 2. 前置知识

### 2.1 从抽取到插值:为什么要"反过来"

在 u4-l1 中,抽取器 `filt_cicd` 的数据流是:

```
i_data ──► [积分器×N] ──► [↓R] ──► [梳状×N] ──► o_data
            (快 i_clk)       (w_sclk)  (慢 w_sclk)
```

抽取器把变速器(↓R)夹在中间,**积分器在前(快域)、梳状在后(慢域)**。这是 Hogenauer 的关键观察:把降采样插在积分器与梳状之间,梳状就只需处理 \(1/R\) 的样本,整体传递函数仍是长度 \(RM\) 滑动平均的 \(N\) 次幂。

插值器要做的事正好相反——把**低速率**输入变成**高速率**输出(每个输入样本产出 \(L\) 个输出样本)。根据多速率信号处理的 **noble 恒等式(对偶形式)**,插值的正确拓扑是把抽取器的两段交换位置:

```
i_data ──► [梳状×N] ──► [↑L] ──► [积分器×N] ──► o_data
            (慢 i_clk)       (i_fclk)   (快 i_fclk)
```

于是得到贯穿本讲的两条铁律:

1. **积分器永远在高速侧,梳状永远在低速侧。** 抽取器里积分器在输入(快)侧、梳状在输出(慢)侧;插值器里梳状在输入(慢)侧、积分器在输出(快)侧。位置交换,但"积分快、梳状慢"不变。
2. **变速器(↑L / ↓R)永远夹在两段之间。** 零插值(↑L)负责把慢速率信号"撑开"成快速率。

> 为什么积分器非要在快侧?因为积分器 \(1/(1-z^{-1})\) 是个累加器,它的作用是"填满"零插值留下的空隙:在真实样本之间,零不断被累加(加 0,值保持),形成阶梯形的保持波形——这正是插值。它必须以输出速率(快)运行,才能在每个输入样本后产出 \(L\) 个输出。详见 4.4。

### 2.2 零插值(Zero-Stuffing)

把采样率提高 \(L\) 倍,最简单的办法不是"复制",而是**在样本之间插入 \(L-1\) 个零**:

\[ x_{\uparrow L}[n] = \begin{cases} x[k], & n = kL + P \\ 0, & \text{ otherwise} \end{cases} \]

其中 \(P\) 是相位偏移。零插值本身不改变频谱形状(只是频谱出现了 \(L\) 个镜像副本),真正"抹平"成连续波形的是后面的积分器(等效低通)。`upsample` 原语就是干这件事。

### 2.3 术语速查

| 术语 | 含义 |
|---|---|
| 插值比 \(L\) | `gp_interpolation_factor`,每个输入样本产出 \(L\) 个输出 |
| 级数 \(N\) | `gp_order`,积分器/梳状各 \(N\) 级 |
| 差分延迟 \(M\) | `gp_diff_delay`,梳状的 \(z^{-M}\) 延迟 |
| 相位 \(P\) | `gp_phase`,选择 \(L\) 个相位中的哪一个携带真实样本 |
| 零插值 | 在样本间插 0,升采样率 |
| 比特真 | RTL 输出与 GRM 逐比特一致 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [.drl_src_code/filt_cici/rtl/filt_cici.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v) | 插值器顶层:梳状段(慢)→ upsample → 积分器段(快),双时钟 `i_clk`/`i_fclk` |
| [.drl_src_code/filt_cici/rtl/upsample.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v) | 零插值上采样器:模 L 计数器 + 零填充移位寄存器 + 可选相位偏移 |
| [.drl_src_code/filt_cici/rtl/shift_register.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/shift_register.v) | 梳状段用的 \(z^{-M}\) 延迟线(u2-l3 已精读,本讲复用) |
| [.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv) | 双时钟测试台:从快时钟 `f_clk` 派生慢时钟 `s_clk`,验证插值节拍 |
| [.drl_src_code/filt_cici/octave/CICFilter.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/octave/CICFilter.m) | 黄金参考模型(GRM):`filter(H^N, upsample(data))` 给出比特真答案 |
| [.drl_src_code/filt_cicd/rtl/filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v) | 抽取器(对照用):积分(快)→↓R→梳状(慢) |

---

## 4. 核心概念与源码讲解

在进入各模块前,先用一张表把插值器与抽取器并排放在一起,这张表是本讲的总纲:

| 维度 | `filt_cicd`(抽取,u4-l1) | `filt_cici`(插值,本讲) |
|---|---|---|
| 数据流顺序 | 积分器 → ↓R → 梳状 | **梳状 → ↑L → 积分器** |
| 积分器时钟 | `i_clk`(快) | **`i_fclk`(快)** |
| 梳状时钟 | `w_sclk`(慢,内部派生) | **`i_clk`(慢,外部输入)** |
| 变速器 | 下采样(丢样本) | **上采样(插零)** |
| 变速器位置 | 积分器与梳状之间 | **梳状与积分器之间** |
| 端口时钟数 | 1 个 `i_clk` + 派生 `w_sclk` | **2 个:`i_clk` + `i_fclk`** |
| 输入速率 / 输出速率 | 快 / 慢 | **慢 / 快** |

注意一个重要区别:抽取器只用一个外部时钟 `i_clk`,慢时钟 `w_sclk` 是内部用环形计数器派生的脉冲;**插值器却需要两个外部时钟** `i_clk`(慢)与 `i_fclk`(快),因为它的快域(积分器)必须以输出速率连续运转,而不能像抽取器那样只靠一个使能脉冲"挑"样本。这个区别是 4.3 的重点。

`filt_cici` 顶层的端口声明就直观体现了双时钟:

[.drl_src_code/filt_cici/rtl/filt_cici.v:6-20](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L6-L20) —— 模块参数与端口。注意端口里同时有 `i_clk`(第 16 行)和 `i_fclk`(第 17 行)两个时钟,这是与 `filt_cicd` 最显眼的不同。

输出位宽公式与抽取器完全一致(增益幅度相同,不因方向改变):

\[ B_{out} = B_{in} + N\cdot\lceil\log_2(L\cdot M)\rceil \]

对应 [.drl_src_code/filt_cici/rtl/filt_cici.v:12](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L12) 的 `gp_oup_width` 默认表达式。位宽推导的细节属于 u4-l3 的范围,本讲只把它当作"既定的、足够宽的通路"来用。

下面按数据流顺序(梳状 → upsample → 积分器)逐一精读,并在 4.3 专门讲双时钟如何协同。

### 4.1 微分器(梳状)级联

#### 4.1.1 概念说明

梳状段(combs)是 CIC 的"微分器",传递函数为 \(1-z^{-RM}\)。但插值器里它运行在**输入(慢)速率**,所以这里的延迟是 \(z^{-M}\)(在慢速率下 \(M\) 拍),写成:

\[ c[n] = c[n-1]\text{ 的反馈差} \quad\Rightarrow\quad y[n] = x[n] - x[n-M] \]

为什么插值器把梳状放在最前、并且跑慢时钟?因为差分只需要在**有真实样本的时刻**做(每个输入样本做一次差分),零是后面才插的。按 noble 恒等式,先在慢域做完梳状差分、再插零,等价于先插零再做"拉伸过"的梳状——前者省掉了在快域处理大量零的开销。

#### 4.1.2 核心流程

\(N\) 级梳状级联,每级 = 一条 `shift_register`(提供 \(z^{-M}\) 延迟)+ 一个减法器:

```
w_data ──►[SR z^-M]──► (−) ──► 差1 ──►[SR z^-M]──► (−) ──► 差2 ──► ... ──► 差N ──► 给 upsample
            ↑________________|       ↑__________________________|
        (第 1 级: 减 w_data 自身)   (第 i 级: 减上一级差分)
        全部 clocked by i_clk (慢)
```

- 第 0 级:输入是符号扩展后的 `w_data`,输出 `w_data - 延迟`。
- 第 1..N-1 级:输入是上一级的差分结果 `w_comb_diff[i*width-1 : ]`,输出"上一级差分 − 其延迟"。

#### 4.1.3 源码精读

输入先做 MSB 符号扩展,补齐到统一通路宽度 `gp_oup_width`:

[.drl_src_code/filt_cici/rtl/filt_cici.v:40](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L40) —— `w_data = $signed({{c_fill_width{i_data[...]}}, i_data})`,复制符号位填高位。这与抽取器写法几乎一样,差别仅是这里多套了一层 `$signed()`。

梳状段用 `generate for` 展开 \(N\) 级,关键看时钟端:

[.drl_src_code/filt_cici/rtl/filt_cici.v:45-80](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L45-L80) —— 梳状级联。第 0 级与第 1..N-1 级分别处理,每级例化一个 `shift_register` 做 \(z^{-M}\) 延迟并接一个减法器。

重点对比两处时钟端口:

- [.drl_src_code/filt_cici/rtl/filt_cici.v:56](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L56) —— 第 0 级梳状 `shift_register` 的 `.i_clk(i_clk)`:**慢时钟**。
- [.drl_src_code/filt_cici/rtl/filt_cici.v:71](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L71) —— 后续级的 `.i_clk(i_clk)`:同样是**慢时钟**。

而抽取器 `filt_cicd` 的梳状用的是内部派生的慢脉冲 `w_sclk`(见 [.drl_src_code/filt_cicd/rtl/filt_cicd.v:124](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L124))。两者"梳状在慢域"一致,只是慢时钟的来源不同(插值器从外部 `i_clk` 直接给,抽取器从快时钟内部派生)。

#### 4.1.4 代码实践(源码阅读型)

**目标**:确认梳状段确实只跑慢时钟,并理解减法链。

1. 打开 `filt_cici.v`,定位 `COMB SECTION`(第 42 行注释起)。
2. 数一下 `generate` 块里共有多少个 `shift_register` 例化、多少个 `assign ... = $signed(...) - $signed(...)`。它们应当各等于 `gp_order`。
3. 检查每个 `shift_register` 的 `.i_clk(...)` 端口,确认全部是 `i_clk`,没有任何一个接 `i_fclk`。
4. **预期结果**:`gp_order` 个延迟 + `gp_order` 个减法,全部 clocked by `i_clk`(慢)。这与"梳状在低速侧"一致。

> 待本地验证:如果你装了 iverilog,可用 `iverilog -g2012` 单独 elaborate 一个 `filt_cici` 实例,用 `$display` 打印 `gp_order`、`c_fill_width`,确认参数注入正确。

#### 4.1.5 小练习与答案

**练习 1**:插值器的梳状为什么不跑在快时钟 `i_fclk` 上?

> **答**:梳状只对真实输入样本做差分,每个输入样本做一次即可;输入是慢速率,所以梳状自然跑慢时钟。跑快时钟会让它在零样本上也做无意义运算,浪费功耗,且违反 noble 恒等式要求的拓扑(变速前先做梳状)。

**练习 2**:为什么梳状用 `shift_register`(多级 dff)而不是单个 `dff`?

> **答**:梳状需要 \(z^{-M}\)(`gp_diff_delay`)的差分延迟。`shift_register` 用 `generate` 把 `M` 个 `dff` 级联成延迟线,正好提供 \(z^{-M}\)。单个 `dff` 只能提供 \(z^{-1}\)。

---

### 4.2 upsample 零插值

#### 4.2.1 概念说明

`upsample` 是零插值上采样器,是 u2-l3 介绍过的共享原语。它把一个慢速率输入"撑开"成快速率输出:每 \(L\) 个快时钟周期里,只有 1 个周期输出真实样本,其余 \(L-1\) 个周期输出 0。这正是 2.2 节定义的零插值。

它在 `filt_cici` 里承担"变速器 ↑L"的角色,也是**跨时钟域的桥梁**:输入来自慢域梳状的输出,自身却跑在快时钟 `i_fclk` 上,把慢信号重新"铺"到快时间轴上。

#### 4.2.2 核心流程

```
                ┌── w_load (r_cnt==0)? 选 i_data(真实样本)
i_data ─►(mux)──┤
                └── 否则选 w_zero_insertion[底字] (恒 0)
                  │
                  ▼ (可选 gp_phase 级延迟, 选相位)
                  └──► o_data (快速率, 大部分为 0)
```

- 一个模 \(L\) 计数器 `r_cnt` 在快时钟下循环计数 `0,1,2,...,L-1,0,...`。
- `w_load = (r_cnt==0)`:`r_cnt` 为 0 的那一拍装载真实样本,其余拍选零。
- 一条"零填充移位寄存器"不断被喂入 0,装满后各级恒为 0,从而在非装载相提供稳定的零。
- 可选的相位偏移链(`gp_phase` 个 dff)把整条流延后 \(P\) 拍,选择哪个相位携带真实样本。

#### 4.2.3 源码精读

模块参数与端口,注意 `gp_nr_stages` 在插值器里被实例化成插值比 \(L\):

[.drl_src_code/filt_cici/rtl/upsample.v:6-17](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L6-L17) —— 参数 `gp_data_width`/`gp_nr_stages`(=L)/`gp_phase`,标准五端口。

关键的多路选择器——`w_load` 为真选真实样本,否则选零填充寄存器的最低字:

[.drl_src_code/filt_cici/rtl/upsample.v:33](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L33) —— `w_upsample_data = (w_load) ? i_data : w_zero_insertion[gp_data_width-1:0]`。

零填充移位寄存器:恒 0 喂入,装满后全是零。最末级直接常量赋 0:

[.drl_src_code/filt_cici/rtl/upsample.v:73-93](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L73-L93) —— `j` 从 0 到 `gp_nr_stages-2` 展开 `L-1` 级 dff;最后一级(`j==gp_nr_stages-2`)用 `assign ... = 'd0` 把零灌进链的顶端。

模 L 计数器与装载标志:

[.drl_src_code/filt_cici/rtl/upsample.v:95-116](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L95-L116) —— 计数器 `r_cnt` 在快时钟下循环;[第 114 行](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L114) `w_load = (r_cnt==0)`。每 \(L\) 个快时钟产生一次装载脉冲。

相位偏移处理有一个值得注意的边界:

[.drl_src_code/filt_cici/rtl/upsample.v:20-21](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L20-L21) —— `c_phase_offset = (gp_phase==0) ? 1 : gp_phase`。当 `gp_phase=0` 时强制取 1,因为零深度的移位寄存器(位宽为 0 的线网)是非法的;此时第 41 行直接把 `w_upsample_inp` 旁路到输出,等价于"不偏移"。

最后,看 `filt_cici` 如何实例化它——**注意时钟端接的是 `i_fclk`(快)**:

[.drl_src_code/filt_cici/rtl/filt_cici.v:86-97](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L86-L97) —— upsample 实例,`gp_nr_stages` 设为 `gp_interpolation_factor`(=L),`.i_clk(i_fclk)`(第 93 行)接快时钟。输入 `w_upsample_inp` 是梳状最后一级的差分([第 85 行](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L85)),输出 `r_int_inp` 喂给积分器。

#### 4.2.4 代码实践(跟踪型)

**目标**:手工追踪 `gp_nr_stages=4`、`gp_phase=0` 时 `upsample` 一个完整周期(4 拍)的输出。

1. 设输入序列 `i_data = [A, B, C, ...]`,每个慢时钟换一个值(对快时钟而言,输入在 4 拍内保持不变)。
2. 复位后 `r_cnt=4`;第一个使能的快时钟沿:`4 < (4-1)=3` 为假,`r_cnt<=0`。随后沿依次 `0→1→2→3→0→...`。
3. 逐拍填表(`w_load = (r_cnt==0)`,`o_data = w_load ? i_data : 0`):

| 快时钟拍 | r_cnt(沿后) | w_load | o_data |
|---|---|---|---|
| 1 | 0 | 1 | A |
| 2 | 1 | 0 | 0 |
| 3 | 2 | 0 | 0 |
| 4 | 3 | 0 | 0 |
| 5 | 0 | 1 | B |
| ... | ... | ... | ... |

4. **预期结果**:输出为 `[A,0,0,0,B,0,0,0,...]`,正是零插值因子 4、相位 0 的结果。
5. **待本地验证**:用 iverilog 给 `upsample` 写一个 5 行的最小测试台(快时钟 + 慢换输入),用 `$monitor` 打印 `r_cnt`/`o_data`,核对上表。

#### 4.2.5 小练习与答案

**练习 1**:`gp_phase=0` 时 `c_phase_offset` 为什么取 1 而不是 0?

> **答**:代码用 `(gp_phase==0)?1:gp_phase`。若取 0,则 `w_phase_offset` 的位宽为 `0*gp_data_width-1:0` 即 `[-1:0]`,是非法的零宽度线网。取 1 让相位偏移链退化为 1 级(实际通过第 41 行直接旁路),既避免非法位宽又等价于"无偏移"。

**练习 2**:零填充移位寄存器为什么深度是 `gp_nr_stages-1`(即 \(L-1\))而不是 \(L\)?

> **答**:\(L\) 个快拍里 1 拍装载真实样本、\(L-1\) 拍插零,因此只需 \(L-1\) 个"零相位"位置;装载由多路选择器直接选 `i_data` 完成,不需要额外寄存器存真实样本。多 1 级反而会引入不必要的延迟。

---

### 4.3 双时钟 i_clk / i_fclk 的协同

#### 4.3.1 概念说明

插值器有两段工作在不同的速率:梳状按输入速率(慢)算,积分器按输出速率(快)算。`filt_cici` 用两个外部时钟来表达这件事——`i_clk`(慢)和 `i_fclk`(快),且约定 \(f_{i\_fclk} = L \cdot f_{i\_clk}\)。这与抽取器"单时钟 + 内部派生慢脉冲"的做法不同:抽取器快域是连续的输入流,只需偶尔用脉冲挑出下采样点;插值器的快域(积分器)必须连续运转来产出 \(L\) 个输出,所以干脆从外面直接给一个快时钟。

#### 4.3.2 核心流程

两个时钟的分工:

```
i_clk  (慢, 输入速率) ──────► 驱动: 梳状段 (shift_register)
i_fclk (快, = L × 慢)  ──────► 驱动: upsample + 积分器段 (dff)

跨域: 梳状输出(慢, 在 i_clk 更新) ──► upsample 输入(在 i_fclk 采样)
      信号在 L 个 i_fclk 周期内保持稳定 → 对快时钟是准静态, 无需同步器
```

测试台负责按 \(L\) 的比例从快时钟 `f_clk` 派生出慢时钟 `s_clk`,再分别接到 DUT 的 `i_clk`/`i_fclk`。

#### 4.3.3 源码精读

先确认 RTL 两侧的时钟分配——这是本讲最关键的一组对照:

- 梳状段跑慢时钟 `i_clk`:[.drl_src_code/filt_cici/rtl/filt_cici.v:56](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L56) 与 [第 71 行](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L71)。
- upsample 跑快时钟 `i_fclk`:[.drl_src_code/filt_cici/rtl/filt_cici.v:93](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L93)。
- 积分器跑快时钟 `i_fclk`:[.drl_src_code/filt_cici/rtl/filt_cici.v:113](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L113) 与 [第 126 行](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L126)。

再看测试台如何生成这两个时钟。`CLK_PERIOD` 是慢时钟周期:

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:8](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L8) —— `time CLK_PERIOD = 400`。

快时钟 `f_clk` 的周期 = `CLK_PERIOD / P_INTERPOLATION`(半周期再除以 2):

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:48](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L48) —— `always f_clk = #(CLK_PERIOD/(P_INTERPOLATION*2)) ~f_clk`。即 `f_clk` 频率是慢时钟的 \(L\) 倍。

慢时钟 `s_clk` 由一个模 \(L\) 计数器派生——高半段置 1、低半段置 0:

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:50-57](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L50-L57) —— 在 `f_clk` 上升沿计数 `r_cnt`(0..L-1 循环),`assign s_clk = (r_cnt<L/2)?1:0`,得到一个周期 = \(L\) 个 `f_clk` 的方波。

最后两个时钟分别接到 DUT,并通过 `fork` 错开 2ns 避免竞争:

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:59-64](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L59-L64) —— `i_fclk = f_clk`、`i_clk = s_clk`(各延 2ns)。

DUT 例化处印证了端口映射:

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:121-135](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L121-L135) —— `.i_clk(s_clk)`(第 131 行)、`.i_fclk(i_fclk)`(第 132 行)。

节拍也按速率分离:激励在慢时钟读入,响应在快时钟比对——

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:94-104](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L94-L104) —— 激励在 `posedge i_clk`(慢)用 `$fscanf` 读一个输入样本(输入是慢速率)。

[.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv:112-119](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L112-L119) —— 响应在 `negedge f_clk`(快)把 DUT 输出与 GRM 答案逐样本比对(输出是快速率,故每 \(L\) 拍比一次的实际上是 \(L\) 个快拍各比一次)。

> **跨域安全性**:梳状输出在 `i_clk`(慢)沿才更新,在两次慢沿之间它对 \(L\) 个 `i_fclk` 周期保持不变,因此 upsample 在快时钟采样它时看到的是一个"准静态"信号,不需要异步握手或同步器——这是一个天然安全的慢→快跨域。

#### 4.3.4 代码实践(参数推演型)

**目标**:推算双时钟周期关系,验证 \(f_{i\_fclk}=L\cdot f_{i\_clk}\)。

1. 设插值比 `P_INTERPOLATION = 4`、`CLK_PERIOD = 400`(单位 ns)。
2. 由第 48 行,`f_clk` 半周期 = `400/(4*2) = 50`ns,故 `f_clk` 周期 = 100ns,频率 = 10MHz。
3. `s_clk` 周期 = \(4\) 个 `f_clk` 周期 = 400ns,频率 = 2.5MHz。
4. **预期结果**:`f_clk` 频率 / `s_clk` 频率 = 10/2.5 = 4 = \(L\)。即 `i_fclk`(快)恰好是 `i_clk`(慢)的 4 倍,与"每个慢样本产 4 个快输出"吻合。
5. **待本地验证**:把 `P_INTERPOLATION` 改成 8,重新算 `f_clk` 周期,确认倍数关系仍为 \(L\)。

#### 4.3.5 小练习与答案

**练习 1**:为什么插值器需要两个外部时钟,而抽取器 `filt_cicd` 只用一个 `i_clk`?

> **答**:抽取器的快域(积分器)直接吃连续的快速率输入,慢域脉冲 `w_sclk` 可以从快时钟内部用环形计数器派生;插值器的快域(积分器)必须连续运转产出 \(L\) 个输出,而输入却是慢速率,无法从单一时钟"挑"出连续的快节拍,所以从外部直接提供独立的快时钟 `i_fclk` 最简洁。

**练习 2**:梳状输出跨到 upsample(快域)为何不需要同步器?

> **答**:梳状在慢时钟 `i_clk` 更新,其输出在 \(L\) 个 `i_fclk` 周期内保持稳定。对快时钟而言这是一个准静态(慢变化)信号,任意一个快沿采样都能拿到已稳定的值,不存在亚稳态风险,故无需同步器。

---

### 4.4 积分器级联

#### 4.4.1 概念说明

积分器(integrator)传递函数 \(1/(1-z^{-1})\),本质是"累加器":\(y[n]=y[n-1]+x[n]\)。\(N\) 级级联后传递函数为 \(1/(1-z^{-1})^{N}\)。

在插值器里,积分器跑在**快时钟 `i_fclk`** 上,吃的是 upsample 送来的零插值信号(大量 0 中夹着真实差分样本)。它把零插值的"稀疏脉冲"积分成"阶梯保持"波形:

- 遇到 0 时:累加值不变(加 0)→ 输出保持上一值;
- 遇到真实样本时:累加值跳变 → 输出阶梯上升/下降。

这种"保持 + 跳变"的阶梯正是对零插值信号的等效低通滤波,把离散输入"抹"成连续的插值波形。这就是积分器必须以输出(快)速率运行的物理原因——只有快速率累加,才能在真实样本之间"填"出 \(L-1\) 个保持值,产出 \(L\) 个输出。

#### 4.4.2 核心流程

\(N\) 级积分器级联,每级 = 一个加法器 + 一个反馈 `dff`(累加):

```
r_int_inp ──►(+)──►[dff z^-1]──► r_int_dly[0] ──►(+)──►[dff]──► r_int_dly[1] ──► ... ──► o_data
              ▲      (i_fclk)         │             ▲      (i_fclk)        │
              └───────────────────────┘             └──────────────────────┘
              (第 0 级: 加 r_int_inp + 自身反馈)    (第 i 级: 加上一级和 + 自身反馈)
              全部 clocked by i_fclk (快)
```

#### 4.4.3 源码精读

积分器段同样用 `generate for` 展开 \(N\) 级:

[.drl_src_code/filt_cici/rtl/filt_cici.v:102-132](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L102-L132) —— 积分器级联。第 0 级(第 107 行)做 `r_int_inp + r_int_dly[0]`,即"输入 + 自身上一拍";后续级(第 120 行)做"上一级和 + 自身反馈"。每级一个 `dff` 把和存回 `r_int_dly`。

关键的时钟端口——**两级积分器的 dff 都接快时钟**:

- [.drl_src_code/filt_cici/rtl/filt_cici.v:113](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L113) —— 第 0 级积分器 `dff` 的 `.i_clk(i_fclk)`:**快时钟**。
- [.drl_src_code/filt_cici/rtl/filt_cici.v:126](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L126) —— 后续级的 `.i_clk(i_fclk)`:**快时钟**。

输出取最后一级积分器的和(组合输出,不经额外寄存):

[.drl_src_code/filt_cici/rtl/filt_cici.v:134](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L134) —— `o_data = w_int_add[gp_order*gp_oup_width-1 -: gp_oup_width]`。

对照抽取器:那里积分器也跑快时钟,但是"外部 `i_clk`"(见 [.drl_src_code/filt_cicd/rtl/filt_cicd.v:76](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L76))。两边都是"积分器在快域",只是快时钟的名字/来源不同。**积分器溢出问题**已被位宽公式兜住:`gp_oup_width` 预留了 \(N\cdot\lceil\log_2(LM)\rceil\) 位增长,足以容纳最大增益 \((LM)^N\),所以累加器不会溢出(详见 u4-l3)。

GRM 侧给出比特真答案,印证"先插零再滤波"等价于 RTL 的"梳状→↑L→积分器":

[.drl_src_code/filt_cici/octave/CICFilter.m:29-33](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/octave/CICFilter.m#L29-L33) —— 插值分支 `FilteredData = filter(Num_CIC, Den_CIC, upsample(Data, R, P))`:先对数据零插值,再用 CIC 传递函数 \(H^N\) 滤波。这正是 RTL 拓扑的数学等价形式(梳状+积分器合并成一个 LTI 滤波器作用在零插值信号上)。

#### 4.4.4 代码实践(波形推演型)

**目标**:追踪一个零插值脉冲通过单级积分器,看"阶梯保持"如何形成。

1. 设 `gp_order=1`(单级积分器)、`L=4`。upsample 给积分器输入 `r_int_inp = [A, 0, 0, 0, B, 0, 0, 0, ...]`(来自 4.2.4)。
2. 积分器递推 `y[n] = y[n-1] + r_int_inp[n]`,设初值 `y[0]=0`:

| 快拍 n | r_int_inp | y[n](沿后) |
|---|---|---|
| 1 | A | A |
| 2 | 0 | A |
| 3 | 0 | A |
| 4 | 0 | A |
| 5 | B | A+B |
| 6 | 0 | A+B |
| ... | ... | ... |

3. **预期结果**:输出为 `[A,A,A,A,A+B,A+B,...]`——在第 1 拍跳到 A,随后 3 拍保持(因为加 0),第 5 拍再跳到 A+B。这就是"保持 + 跳变"的阶梯,正是零插值信号被积分平滑的结果。
4. **观察要点**:如果积分器改跑慢时钟(每 4 拍才累加一次),它只会输出 `[A, A+B, ...]`,丢失中间 3 个保持值——这就不是插值了。由此直观体会"积分器必须跑快时钟"。
5. **待本地验证**:用 `./dsp_rtl_lib.sh -d` 生成 `filt_cici` 并跑回归,查看 `.vcd` 波形,比对 `r_int_inp`(快域)与 `o_data` 的阶梯形状。

#### 4.4.5 小练习与答案

**练习 1**:为什么插值器的积分器必须跑在快时钟 `i_fclk` 上?

> **答**:积分器要对零插值后的快速率信号逐拍累加,在每个真实样本后用 \(L-1\) 个"加 0"拍保持输出,从而产出 \(L\) 个输出/输入(阶梯保持 = 插值)。若跑慢时钟,每个输入只累加一次,只能产出 1 个输出/输入,无法插值。

**练习 2**:积分器是无限增长的累加器,为什么不会溢出?

> **答**:`gp_oup_width` 按 Hogenauer 公式 \(B_{in}+N\lceil\log_2(LM)\rceil\) 预留了位宽,覆盖最大可能增益 \((LM)^N\)。通路全程都是 `gp_oup_width` 位,累加不会超出该范围(细节见 u4-l3)。

**练习 3**:GRM 的 `CICFilter(...,'i')` 与 RTL 的"梳状→↑L→积分器"为何比特等价?

> **答**:noble 恒等式保证"在慢域做梳状差分、再插零"等价于"先插零、再做拉伸的梳状";而梳状+积分器在快域合并就是完整 CIC 传递函数 \(H^N=(1-z^{-LM})^N/(1-z^{-1})^N\)。GRM 直接用 `filter(H^N, upsample(data))` 算这个等价形式,故与 RTL 逐比特一致。

---

## 5. 综合实践

把本讲四个最小模块串起来,完成下面这个"对比 + 推演"任务。

**任务**:对比 `filt_cicd`(抽取)与 `filt_cici`(插值),画出两者的完整数据流框图,并回答"为什么插值器的积分器跑在快时钟 `i_fclk` 上"。

**操作步骤**:

1. 打开两个顶层文件并列对照:
   - [.drl_src_code/filt_cicd/rtl/filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v)(抽取)
   - [.drl_src_code/filt_cici/rtl/filt_cici.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v)(插值)
2. 在草稿纸上分别画出两幅数据流框图,标注每一段用的是哪个时钟、变速器(↓R / ↑L)插在哪里。参考答案:

   ```
   抽取 cicd:  i_data →[积分×N, i_clk快]→[↓R, w_sclk]→[梳状×N, w_sclk慢]→ o_data
   插值 cici:  i_data →[梳状×N, i_clk慢]→[↑L, i_fclk]→[积分×N, i_fclk快]→ o_data
   ```

3. 列一张表,逐项对比:数据流顺序、积分器时钟、梳状时钟、变速器、端口时钟数、输入/输出速率(可参考第 4 节开头的总纲表,但请用自己的话重写)。
4. 用一段话(100 字以内)回答核心问题:**为什么插值器积分器跑在 `i_fclk`?**
   - 参考要点:积分器要对零插值后的快速率信号逐拍累加;真实样本之间靠"加 0"保持输出(阶梯),从而每个输入产出 \(L\) 个输出;跑慢时钟只能 1 个输出/输入,无法插值。此外快域连续运转需要一个独立的快时钟,故端口上有 `i_clk`/`i_fclk` 两个。
5. **进阶(可选)**:读测试台 [filt_cici_tb.sv:48-64](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/sim/testbench/filt_cici_tb.sv#L48-L64),确认 `i_fclk` 频率是 `i_clk` 的 \(L\) 倍,并解释测试台为何在 `posedge i_clk` 读激励、在 `negedge f_clk` 比对响应。

**预期结果**:两张框图镜像对称(段落顺序相反,但"积分快、梳状慢"一致);对比表能清楚展示差异;核心问题的解释落到"零插值 + 阶梯保持"上。

## 6. 本讲小结

- CIC 插值器是抽取器的**镜像**:数据流由「积分→↓R→梳状」翻转为「**梳状→↑L→积分器**」,这是 noble 恒等式的对偶形式。
- **积分器永远在快域、梳状永远在慢域**——这是贯穿抽取/插值的不变铁律。
- `filt_cici` 用**两个外部时钟**:`i_clk`(慢,驱动梳状)与 `i_fclk`(快,驱动 upsample + 积分器),且 \(f_{i\_fclk}=L\cdot f_{i\_clk}\)。
- `upsample` 用"模 L 计数器 + 零填充移位寄存器"实现**零插值**:`w_load` 每 \(L\) 拍选一次真实样本,其余拍输出 0;`gp_phase` 用额外 dff 选择输出相位。
- 积分器跑快时钟是为了把零插值的稀疏脉冲**积分成阶梯保持波形**,在每个输入后产出 \(L\) 个输出——这正是"插值"的本质。
- 梳状输出跨到快域无需同步器,因为它在 \(L\) 个快周期内保持稳定(准静态慢→快跨域)。
- GRM 用 `filter(H^N, upsample(data))` 给出比特真答案,与 RTL 拓扑数学等价。

## 7. 下一步学习建议

- **u4-l3(CIC 位宽推导与 GRM)**:本讲多次提到 `gp_oup_width` 与增益缩放因子 `SF`,但没有展开。下一讲会从 Hogenauer 公式严格推导位宽,并精读 `CICFilter.m` 与 `stimuli.m` 中的 `SF = 2^(N·ceil(log2(LM)))/(LM)^N`,解释它为何落在 \([1,2)\)。
- **u5(多相滤波器)**:多相滤波器是另一类多速率结构(抽取 `filt_ppd` / 插值 `filt_ppi`),同样有"正向/反向数据流"和换向器。学完 CIC 的双时钟思想后,对比多相的"单时钟 + 换向器并行捕获",会加深对多速率硬件取舍的理解。
- **源码延伸阅读**:对照 [.drl_src_code/filt_cici/rtl/upsample.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v) 与 u2-l3 的讲解,体会同一个 `upsample` 原语如何在 CIC 插值里承担"变速器 + 跨域桥"双重角色。
