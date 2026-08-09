# commutator 换向器（抽取）

## 1. 本讲目标

本讲精读 `filt_ppd`（多相抽取滤波器）中的 `commutator.v`——换向器。学完后你应该能够：

- 看懂用 **一位热码环形计数器（one-hot ring counter）** 实现「串行输入 → 并行字输出」的机制，并区分顺时针（CW）与逆时针（CCW）两种旋转方向；
- 解释 `gp_phase` 如何通过 `shift_register` 给输入加延迟、从而选择不同的抽取相位；
- 理解最巧妙的一招：**直接把环形计数器的某一位当作某个捕获寄存器的时钟**，让 M 条并行支路在 M 个快时钟周期里各自精确捕获一次数据；
- 能手算一个完整周期内 `r_ring_cnt` 的演化，并画出每个 `dff` 捕获输入的时刻表。

本讲承接 u5-l1（多相分解与 `filt_ppd` 顶层），往下钻进顶层的第一个子模块；依赖 u2-l2（`dff` 原语）与 u2-l3（`shift_register` 原语）。

## 2. 前置知识

### 2.1 换向器在多相抽取里干什么

在 u5-l1 里我们说过：多相抽取把一条长 FIR 卷积拆成 M 条短子滤波器，运算降到 \(1/M\) 的慢速率。但现实里输入数据 `i_data` 是**一个个串行到来**的（每个快时钟 `i_clk` 一个样本）。换向器（commutator，物理直译就是「换向开关」）的任务就是：

> 在快时钟域里把连续到来的 M 个串行样本，**按相位分发**到 M 条并行支路的寄存器里；每凑齐 M 个，就在慢时钟 `s_clk` 上吐出一个完整的并行字 `o_data`，交给下游的 `mul_add` 做乘加。

形象地说，换向器是一个**带旋转触点的 M 路分配器**：触点每拍前进一格，把当前输入接到对应的支路寄存器；转满一圈，M 路就各装好一个样本。

### 2.2 用「位」当「时钟」的小技巧

通常我们会用一个二进制计数器加一个译码器来产生 M 个「使能」脉冲。但本模块的做法更直接：把计数器做成 **M 位一位热码**，那么它的每一位天然就是一个「每 M 拍高一次」的脉冲。既然 `dff` 的使能/时钟都是沿触发的，干脆把这一位**接到捕获寄存器的时钟端口**上——位变高的那个沿，正好就是该寄存器该捕获数据的时刻。一个计数器同时扮演了「计数」和「分路时钟」两个角色，省掉了译码器。

这是本讲最值得反复看懂的设计点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [.drl_src_code/filt_ppd/rtl/commutator.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v) | 本讲主角：环形计数器 + 相位对齐 + 按相位捕获，输出并行字与慢时钟。 |
| [.drl_src_code/filt_ppd/rtl/shift_register.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/shift_register.v) | 相位对齐用的延迟线原语（u2-l3 已讲结构，本讲讲它在换向器里的角色）。 |
| [.drl_src_code/filt_ppd/rtl/filt_ppd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v) | 顶层：例化 `commutator` 与 `mul_add`，把 `s_clk` 引出为 `o_sclk`。 |
| [.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv) | 测试台：在 `negedge s_clk` 上比对 RTL 与黄金参考模型，验证换向器相位正确。 |
| [.drl_src_code/filt_ppd/octave/stimuli.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/octave/stimuli.m) | 黄金参考侧：用 `downsample(..., dec-1-phase)` 定义「正确相位」，是理解 `gp_phase` 语义的钥匙。 |

---

## 4. 核心概念与源码讲解

### 4.1 环形计数器 CW/CCW

#### 4.1.1 概念说明

环形计数器（ring counter）是一种**一位热码（one-hot）**状态机：一个 M 位的寄存器，任意时刻最多只有一位是 1，每个时钟沿这个「1」向左或向右移动一格，转满 M 拍回到原位。

换向器用它来做两件事：

1. **分路**：M 位对应 M 条支路，哪一位为 1，当前拍就把数据分配给哪一条支路；
2. **计时**：转满一圈 = 收齐 M 个样本 = 该输出一个并行字，于是用「最后一拍的位置」触发慢时钟脉冲。

「顺时针（CW）」与「逆时针（CCW）」就是「1」移动的两个方向。它们算的是同一条卷积，只是支路编号顺序相反——这一点会被 `mul_add` 里的系数矩阵编排（见 u5-l3）反向补偿回来，保证总响应不变。

#### 4.1.2 核心流程

一位热码的位宽是 **M 本身**（不是 \(\lceil\log_2 M\rceil\)），因为我们直接用每一位当分路时钟。设 \(M =\) `gp_decimation_factor`：

- **复位**：`r_ring_cnt = 0`（全 0，特殊「种子态」）。
- **第一拍**（检测到全 0）：种入第一个「1」。
  - CW：种入最高位 → `100...0`；
  - CCW：种入最低位 → `000...1`。
- **之后每拍**：把唯一的「1」平移一格。
  - CW：向低位移（`1000 → 0100 → 0010 → 0001 → 1000`）；
  - CCW：向高位移（`0001 → 0010 → 0100 → 1000 → 0001`）。
- **完成检测** `w_done`：取「最后一拍」那一位。
  - CW 的最后一拍是 `0001`，故 `w_done = r_ring_cnt[0]`；
  - CCW 的最后一拍是 `1000`，故 `w_done = r_ring_cnt[M-1]`。
- **慢时钟** `o_clk`：把 `w_done` 寄存一拍得到 `r_done`，于是 `o_clk` 每 M 个快周期输出一个 1 周期宽的脉冲，频率恰为 \(f_{i\_clk}/M\)。

#### 4.1.3 源码精读

参数与端口里，`gp_ccw` 选方向，`gp_decimation_factor` 既是支路数 M 又是计数器位宽，`o_clk` 是慢时钟脉冲输出：

[.drl_src_code/filt_ppd/rtl/commutator.v:L6-L20](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L6-L20) — 定义换向器的 5 个参数与端口；注意 `o_data` 宽度是 `M*gp_idata_width`（M 路并行字），`o_clk` 是慢时钟脉冲。

关键常量 `c_cnt_width = gp_decimation_factor`——计数器位宽直接等于 M，这是一位热码的标志（对比 `shift_register` 用 `$clog2` 的普通二进制计数器）：

[.drl_src_code/filt_ppd/rtl/commutator.v:L22-L33](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L22-L33) — `c_cnt_width = gp_decimation_factor`，`r_ring_cnt` 是 M 位寄存器；`r_done` 用来寄存完成标志。

CCW 方向的环形计数器（本讲实践的配置）：

[.drl_src_code/filt_ppd/rtl/commutator.v:L126-L147](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L126-L147) — CCW 计数器：全 0 时种入 `bit[0]`，否则做 `bit[0]<=bit[M-1]; bit[M-1:1]<=bit[M-2:0]` 的左移回环，把「1」从低位推向高位；`r_done <= w_done` 把完成标志打一拍。

CW 方向的环形计数器（方向相反，结构对称）：

[.drl_src_code/filt_ppd/rtl/commutator.v:L42-L63](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L42-L63) — CW 计数器：全 0 时种入最高位，否则做右移回环，把「1」从高位推向低位。

两个方向的完成检测与慢时钟输出：

[.drl_src_code/filt_ppd/rtl/commutator.v:L201](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L201) — CCW 的 `w_done = r_ring_cnt[M-1]`（最后一拍是 `1000`）。

[.drl_src_code/filt_ppd/rtl/commutator.v:L119](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L119) — CW 的 `w_done = r_ring_cnt[0]`（最后一拍是 `0001`）。

[.drl_src_code/filt_ppd/rtl/commutator.v:L205](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L205) — `assign o_clk = r_done;` 慢时钟就是打了一拍的完成标志。

#### 4.1.4 代码实践

**实践目标**：手算 CCW、\(M=4\) 下 `r_ring_cnt` 的一个完整周期演化，定位每个捕获 `dff` 触发的时刻。

**操作步骤**（纯纸笔，无需工具）：

1. 设 `gp_ccw=1`、`gp_decimation_factor=4`，所以 `r_ring_cnt` 是 4 位。
2. 从复位态 `0000` 开始，逐个 `i_clk` 上升沿套用 L126-L147 的规则：全 0 则种 `bit[0]`，否则左移回环。
3. 同时记录每一拍**哪一位发生了 0→1 的上升沿**（这一位就是「该拍点亮的捕获时钟」）。
4. 再算 `w_done = r_ring_cnt[3]` 与 `r_done = o_clk`（打一拍）。

**预期结果**（时刻表，"after edge" 表示该沿之后寄存器的值）：

| 快时钟沿 # | `r_ring_cnt`（沿后） | 本沿上升的位 | 该拍触发的捕获寄存器 | `w_done`(`bit3`) | `r_done`=`o_clk`（沿后） |
| :--: | :--: | :--: | :--: | :--: | :--: |
| 复位 | `0000` | — | — | 0 | 0 |
| 1 | `0001` | `bit[0]` | lane 0 | 0 | 0 |
| 2 | `0010` | `bit[1]` | lane 1 | 0 | 0 |
| 3 | `0100` | `bit[2]` | lane 2 | 0 | 0 |
| 4 | `1000` | `bit[3]` | lane 3 | 1 | 0 |
| 5 | `0001` | `bit[0]` | lane 0（下一周期） | 0 | **1 ← s_clk 脉冲** |
| 6 | `0010` | `bit[1]` | lane 1 | 0 | 0 |

**需要观察的现象**：

- 「1」每拍左移一位，4 拍走完一圈 `0001→0010→0100→1000→0001`；
- 每拍恰好有一个捕获寄存器被它自己的位时钟触发，4 拍内 4 条支路各捕获一次；
- `o_clk` 在第 5 拍（最后一拍捕获之后的下一拍）出一个 1 周期宽脉冲——这正是 \(f_{i\_clk}/4\) 的慢时钟。

> 说明：上表是「快时钟沿级别」的寄存器演化，完全由 L126-L147 决定，是确定性的。至于「第 k 个输入样本到底落进哪条 lane」还牵涉测试台里 `i_data = #5 r_data` 的输入延迟（见 4.3.4），精确到纳秒的样本编号建议用 VCD 波形确认（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`gp_decimation_factor=4` 时，为什么 `r_ring_cnt` 是 4 位而不是 2 位（\(\lceil\log_2 4\rceil\)）？

**答**：因为本设计把每一位**直接当作一条支路的捕获时钟**。一位热码天然给出 M 个互不重叠的「每 M 拍高一次」脉冲，正好驱动 M 个捕获寄存器；若用 2 位二进制计数器，还得外加一个 2-4 译码器才能得到这 M 个分路使能，反而更费。

**练习 2**：CCW、\(M=4\) 下，`o_clk` 多久脉冲一次？脉冲频率与 `i_clk` 是什么关系？

**答**：每 4 个 `i_clk` 周期脉冲一次（首个脉冲因 `r_done` 打一拍而延后 1 拍出现）。脉冲频率为 \(f_{i\_clk}/4\)，即抽取后的慢速率，与抽取因子 \(M=4\) 一致。

**练习 3**：把 `gp_ccw` 从 1 改成 0，环形计数器的状态序列会怎样变？`w_done` 取自哪一位？

**答**：CW 方向下「1」从高位向低位走：`1000→0100→0010→0001→1000`；最后一拍是 `0001`，故 `w_done = r_ring_cnt[0]`（见 L119）。两种方向算同一条卷积，支路顺序相反，由 `mul_add` 的系数编排补偿。

---

### 4.2 相位对齐 shift_register

#### 4.2.1 概念说明

「抽取相位」是说：在每 M 个串行样本里，到底从第几个开始算作一个并行字的「第 0 相」。同一个滤波器、同一组系数，相位选得不同，输出序列就整体平移几个样本。这就是 `gp_phase` 参数控制的事。

换向器实现相位选择的方式非常朴素：**在输入路径上插一条深度为 `gp_phase` 的移位寄存器**，把 `i_data` 延迟 `gp_phase` 个快周期后再喂给捕获寄存器。延迟多少，样本到支路的分配就整体旋转多少——等价于换了抽取相位。

- `gp_phase = 0`：不延迟，`d_data = i_data`，直接捕获；
- `gp_phase > 0`：例化一个 `shift_register`，把 `i_data` 延迟 `gp_phase` 拍得到 `d_data`。

这条延迟线复用的正是 u2-l3 讲过的 `shift_register` 原语（级联 `dff` + 完成标志）。

#### 4.2.2 核心流程

1. 编译期判断 `gp_phase`：
   - 等于 0 → `assign d_data = i_data;`（一根线，零开销）；
   - 大于 0 → 例化 `shift_register #(.gp_nr_stages(gp_phase))`，输入 `i_data`、输出 `d_data`。
2. `shift_register` 内部把 `gp_phase` 个 `dff` 串成延迟线，输出末级 = \(z^{-\text{gp\_phase}}\)（见 u2-l3）。
3. 于是下游捕获寄存器拿到的 `d_data` 比 `i_data` 晚了 `gp_phase` 拍，相当于把样本流相对换向器旋转方向**反向偏移**了 `gp_phase` 步，从而选中了不同的抽取相位。
4. 在黄金参考模型（GRM）一侧，对应的是 `downsample(..., M, M-1-gp_phase)`——`gp_phase` 越大，下采样起点越靠前（详见 stimuli.m）。

#### 4.2.3 源码精读

`gp_phase==0` 与 `gp_phase>0` 的二选一（CCW 分支；CW 分支 L65-L82 完全对称）：

[.drl_src_code/filt_ppd/rtl/commutator.v:L149-L166](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L149-L166) — `gp_phase==0` 时 `assign d_data = i_data;`；否则例化 `shift_register`，深度=`gp_phase`，把 `i_data` 延迟后输出 `d_data`，并把延迟线「已填满」标志接到 `w_ena`。

`shift_register` 自身就是 u2-l3 的延迟线原语——`gp_nr_stages` 个 `dff` 级联，输出末级实现 \(z^{-N}\)：

[.drl_src_code/filt_ppd/rtl/shift_register.v:L46-L74](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/shift_register.v#L46-L74) — generate 循环级联 `dff`：第 0 级吃 `i_data`，之后每级吃上一级的 `o_data`，构成移位延迟线。

GRM 侧定义「正确相位」的那一行——它是 `gp_phase` 语义的权威来源：

[.drl_src_code/filt_ppd/octave/stimuli.m:L133](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/octave/stimuli.m#L133) — 黄金响应 `yy = downsample(filter(b,1,data), M, M-1-gp_phase)`；`gp_phase` 直接出现在下采样起点里，RTL 的 `shift_register` 延迟必须与此对齐才能比特真。

> 小提示：commutator 把 `shift_register` 的 `o_shift_done` 接到了内部线 `w_ena`，但在本模块里 `w_ena` 并未被其他逻辑消费——它只是「延迟线已就绪」的一个备用信号，目前属预留连接。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：建立「RTL 延迟 ↔ GRM 相位」的对应关系。

**操作步骤**：

1. 打开 [stimuli.m:L133](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/octave/stimuli.m#L133)，写下 `gp_phase=0,1,2,3`（\(M=4\)）时 `downsample` 的起点 `M-1-gp_phase` 分别是 `3,2,1,0`。
2. 在 RTL 里确认：`gp_phase=2` 时，`commutator` 例化了深度为 2 的 `shift_register`（L154-L165），`d_data` 比 `i_data` 晚 2 拍。
3. 用一句话解释：为什么「输入延迟 2 拍」等价于「下采样起点从 3 变成 1」？

**预期结果**：输入延迟 k 拍，意味着原本在第 0 相捕获的样本现在改由第 k 相之后的位置捕获，相当于把抽取起点向前移动 k 步，故起点由 `M-1` 变为 `M-1-k`，与 GRM 公式一致。这正是 RTL 与 GRM 比特真的相位锁扣。

#### 4.2.5 小练习与答案

**练习 1**：`gp_phase` 的合法取值范围是多少？为什么？

**答**：`0` 到 `gp_decimation_factor-1`（见端口注释 L11）。因为一个并行字只有 M 个相位位置，延迟超过 M 拍等价于延迟 `mod M` 拍，超出部分无意义。

**练习 2**：`gp_phase=0` 时换向器有没有用到 `shift_register`？

**答**：没有。L149-L152 直接 `assign d_data = i_data;`，零延迟、零开销；`shift_register` 只在 `gp_phase>0` 时才被例化（generate 条件综合）。

---

### 4.3 按相位并行捕获

#### 4.3.1 概念说明

到这一步，我们有了「按相位延迟后的输入 `d_data`」和「一位热码环形计数器」。还差最后一件：把每个快周期到来的 `d_data` **真正写进对应支路的寄存器**，并把 M 条支路拼成一个并行字 `o_data`。

本模块的做法再次复用 `dff` 原语：例化 M 个 `dff`，每个的**时钟端口接到环形计数器的不同位**。位 i 在第 i 拍变高，于是第 i 个 `dff` 恰在第 i 拍捕获 `d_data`。M 拍下来，M 个 `dff` 各装一个样本，拼起来就是完整的并行字。

这一步完成后，换向器就完成了「串行 → 并行 + 相位选择」的全部职责，并行字与慢时钟 `s_clk` 一起交给 `mul_add`。

#### 4.3.2 核心流程

1. **捕获层**：generate 循环例化 M 个 `dff`，第 x 个的时钟 = `r_ring_cnt[x]`（CCW，L168-L179）或 `r_ring_cnt[x-1]`（CW，L85-L96），数据输入都是 `d_data`。
2. **拼包**：每个 `dff` 的输出按其支路编号切到 `w_data` 的对应字段（每段 `gp_idata_width` 位），M 段拼成宽度 `M*gp_idata_width` 的并行字。
3. **输出寄存（可选）**：`gp_reg_oup=1` 时再过一层 `dff`（时钟 = `r_done`，即慢时钟脉冲），把 `w_data` 锁成 `o_data`，给出与 `s_clk` 对齐、无毛刺的输出；`gp_reg_oup=0` 时 `o_data = w_data` 直接输出（面积省，但 `o_data` 随捕获异步变化）。
4. **交付下游**：顶层 `filt_ppd` 把 `o_data` 与 `s_clk` 喂给 `mul_add`，`mul_add` 以 `s_clk` 为时钟做多相乘加。

#### 4.3.3 源码精读

CCW 的捕获层——注意 `.i_clk(r_ring_cnt[x])`，**把环形计数器的第 x 位当成了第 x 个捕获寄存器的时钟**：

[.drl_src_code/filt_ppd/rtl/commutator.v:L168-L179](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L168-L179) — 例化 M 个 `dff`，时钟分别接 `r_ring_cnt[0..M-1]`，输入都是 `d_data`，输出拼进 `w_data` 的对应字段；这是「按相位并行捕获」的核心实现。

CW 的捕获层（循环方向相反，把高位映射到并行字的高字段）：

[.drl_src_code/filt_ppd/rtl/commutator.v:L85-L96](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L85-L96) — CW 捕获：`for(x=M; x>0; x=x-1)` 把 `r_ring_cnt[x-1]` 当时钟，从高位向低位填充 `w_data`。

输出寄存层（`gp_reg_oup=1` 时），用 `r_done`（慢时钟）做时钟把并行字锁存为 `o_data`：

[.drl_src_code/filt_ppd/rtl/commutator.v:L181-L199](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L181-L199) — `gp_reg_oup=1` 时再过一层 `dff`（时钟 `r_done`）锁存输出；`=0` 时 `assign o_data = w_data;` 直通。

顶层如何把换向器与 `mul_add` 拼起来——注意 `mul_add` 的时钟就是换向器吐出的 `s_clk`：

[.drl_src_code/filt_ppd/rtl/filt_ppd.v:L31-L61](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L31-L61) — `commutator` 输出并行字 `comm_data` 与慢时钟 `s_clk`；`mul_add` 以 `s_clk` 为时钟做乘加，产出 `o_data`。

测试台在慢时钟的**下降沿**比对 RTL 与 GRM——说明并行字在 `s_clk` 高电平期间已稳定，下降沿是最安全的采样点：

[.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv:L128-L136](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L128-L136) — `always @(negedge s_clk)` 比对 `o_data_rtl != o_data_mat`，不等则 `error_count++`；这是换向器相位与拼包正确性的最终判据。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：数清换向器用了多少个 `dff`，并理解测试台的输入节拍。

**操作步骤**：

1. 对 `gp_decimation_factor=4`、`gp_reg_oup=1`、`gp_phase=0`、`gp_idata_width=8`，数一数 `commutator` 一共例化了多少个 `dff`：
   - 相位对齐：`gp_phase=0` → 0 个；
   - 捕获层：M = 4 个；
   - 输出寄存层：M = 4 个；
   - 合计 8 个 `dff`，每个 8 位。
2. 看 [filt_ppd_tb.sv:L90-L101](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L90-L101)：激励在每个 `posedge i_clk` 用 `$fscanf` 读入 `r_data`，再 `always i_data = #5 r_data;` 延迟 5ns 驱动到 `i_data`。

**需要观察的现象**：

- `i_data` 在 `i_clk` 上升沿之后 5ns 才更新，刻意避开捕获寄存器（被 `r_ring_cnt` 位时钟触发）的动作沿，保证捕获到的是稳定值；
- 因此 4.1.4 表中「第 k 拍的输入样本落进哪条 lane」需结合这 5ns 偏移在波形上确认（待本地验证）。

**预期结果**：`gp_phase=0`、`gp_reg_oup=1`、\(M=4\) 时换向器共用 8 个 `gp_idata_width` 位宽的 `dff`（捕获 4 + 输出锁存 4）。

#### 4.3.5 小练习与答案

**练习 1**：把 `gp_reg_oup` 从 1 改为 0，输出延迟与稳定性会有什么变化？

**答**：`gp_reg_oup=0` 时少一层输出寄存，`o_data` 直接等于 `w_data`，延迟少一个慢时钟周期、面积更省；但 `w_data` 的各 lane 是在各自位时钟上异步更新的，`o_data` 会出现过渡毛刺，采样窗口变差。`=1` 则用 `r_done` 锁存，输出与 `s_clk` 严格对齐、无毛刺，是多相滤波更稳妥的选择。

**练习 2**：捕获层的 M 个 `dff` 都用 `i_ena` 作使能、用 `r_ring_cnt[x]` 作时钟。为什么 `i_ena=0` 时它们都不会捕获？

**答**：`dff` 内部是 `if(!i_rst_an) ... else if(i_ena) r_data<=i_data;`（见 u2-l2）。`i_ena=0` 时即使时钟沿到来，`else if` 不成立，`r_data` 保持原值，所以暂停捕获——这正是「同步高有效使能」约定带来的免费暂停功能。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来——「环形计数器演化 + 相位对齐 + 按相位捕获」——用一个最小独立测试平台把换向器单独跑起来，验证你在 4.1.4 手算的时刻表。

**目标**：单独例化 `commutator`（不接 `mul_add`），喂入已知脉冲序列，用 VCD 波形确认 `r_ring_cnt` 的旋转、每个捕获 `dff` 的触发时刻、以及 `o_clk` 的慢时钟脉冲。

**操作步骤**：

1. 在 `filt_ppd/rtl/` 下新建一个最小测试台 `comm_tb.v`（示例代码，仅用于本练习，非项目原有文件）：

   ```verilog
   `timescale 1ns/1ps
   module comm_tb;
     reg i_rst_an, i_ena, i_clk;
     reg signed [7:0] i_data;
     wire signed [31:0] o_data;   // M*gp_idata_width = 4*8
     wire o_clk;
     integer i;

     commutator #(
       .gp_ccw(1), .gp_idata_width(8), .gp_decimation_factor(4),
       .gp_reg_oup(1), .gp_phase(0)
     ) dut (
       .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
       .i_data(i_data), .o_data(o_data), .o_clk(o_clk)
     );

     initial begin i_clk=0; forever #25 i_clk = ~i_clk; end   // 50ns 周期
     initial begin
       $dumpfile("comm.vcd"); $dumpvars(0, comm_tb);
       i_rst_an=0; i_ena=0; i_data=0;
       #130 i_rst_an=1;
       #205 ;             // 与项目 TB 复位节拍一致
       i_ena=1;
       for (i=1; i<=8; i=i+1) begin       // 喂 8 个可辨识样本
         @(posedge i_clk); i_data = i[7:0];
       end
       #200 $finish;
     end
   endmodule
   ```

2. 编译并仿真（iverilog，命令摘自项目测试台注释 [filt_ppd_tb.sv:L148](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L148) 的同类写法）：

   ```bash
   iverilog -o comm -g2x comm_tb.v commutator.v shift_register.v dff.v
   vvp comm
   ```

3. 用 GTKWave 打开 `comm.vcd`，对照 4.1.4 的时刻表核对：
   - `dut.r_ring_cnt` 是否按 `0001→0010→0100→1000→0001` 旋转；
   - 每个 `REG_COMMUTATOR_INP_DATA` 实例的 `r_data` 是否各在对应位变高的那一拍更新；
   - `o_clk` 是否每 4 个 `i_clk` 出一个 1 周期宽脉冲。

**需要观察的现象与预期结果**：

- `r_ring_cnt` 的演化与本讲 4.1.4 表完全一致；
- 4 个捕获寄存器在 4 个连续快周期内各更新一次，拼出的 `o_data` 每 4 拍刷新一次；
- `o_clk`（`s_clk`）脉冲频率 = \(f_{i\_clk}/4\)。

> 说明：本实践为示例代码，命令的具体输出请在本机 iverilog 环境运行后确认（待本地验证）。若你的机器装了 octave 与 iverilog，更权威的做法是直接用项目脚本跑回归：`./dsp_rtl_lib.sh -d filt_ppd`（参数取自 `.drl_param/filt_ppd_1.param`，默认 `gp_comm_ccw=1`、`gp_comm_phase=0`，正好对应本讲配置），查看各测试用例的 `PASSED`。

## 6. 本讲小结

- 换向器是一个「**串行 → 并行 + 相位选择**」的分配器：在快时钟 `i_clk` 上把连续 M 个样本分发到 M 条支路，每凑齐一组就在慢时钟 `s_clk` 上吐出一个并行字。
- 它用一个 **M 位一位热码环形计数器** 既计数又分路：`gp_ccw` 选「1」的旋转方向（CCW 向高位、CW 向低位），完成标志 `w_done` 取「最后一拍」那一位，打一拍得 `o_clk`（频率恰为 \(f_{i\_clk}/M\)）。
- `gp_phase` 通过例化一条深度为 `gp_phase` 的 `shift_register` 给输入加延迟，等价于选择抽取相位；GRM 侧 `downsample(..., M-1-phase)` 是其权威定义。
- 最巧妙的设计：**把环形计数器的每一位直接接到对应捕获 `dff` 的时钟端口**，让 M 条支路在 M 拍内各精确捕获一次，省掉译码器。
- `gp_reg_oup` 决定是否再过一层 `dff`（时钟 `r_done`）锁存输出，在「省面积」与「输出对齐 `s_clk` 无毛刺」间取舍。
- 测试台在 `negedge s_clk` 比对 RTL 与 GRM，是换向器相位与拼包正确性的最终判据——这一切都建立在 u2-l2 的 `dff` 与 u2-l3 的 `shift_register` 两个原语之上。

## 7. 下一步学习建议

- 本讲只讲了换向器「怎么把数据分发到位」；**乘法器的输入怎么编排** 才能让 CW/CCW 两种方向算出同一条卷积，留待 **u5-l3（mul_add 多相乘加引擎）**——那里会讲系数矩阵化与 TF/DF × CW/CCW 四象限编排，正好补偿本讲换向器的支路顺序。
- 若想看「反向数据流」的换向器（插值器里 mul_add 在前、换向器在后，且用索引输出而非位时钟捕获），请读 **u5-l4（filt_ppi 多相插值）**，对比 PPD 与 PPI 两个 `commutator` 版本的差异。
- 建议同时打开 [.drl_src_code/filt_ppd/rtl/mul_add.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v)，对照本讲的 `w_data` 拼包顺序，预习下一讲将出现的 `c_row_x_col` 系数矩阵。
