# sgen_nco — NCO 相位累加与象限重构

## 1. 本讲目标

本讲精读 DSP-RTL-Lib（DRL）的数控振荡器（NCO，Numerically Controlled Oscillator）模块 `sgen_nco`。学完后你应当能够：

- 说清「相位累加器 + 频率控制字（FCW）」如何用纯数字电路产生频率可调的正弦/余弦波（即 DDS 原理）。
- 看懂 RTL 为什么只存**八分之一周期**（第一象限的一半，即 `[0, π/4)`）的两张小表（sin 表 + cos 表），就能复原一整个周期的正弦与余弦。
- 逐位解释 `w_ctrl` 这 3 个比特如何同时决定输出的**符号**、**正余弦互换**与**地址镜像**。
- 对照 Octave 黄金参考模型 `nco.m`，解释 RTL 用「单象限 ROM + 象限重构」为何能与存全表的 GRM 逐样本吻合（比特真），并说清测试台为何放宽容差到 ±1 LSB。

## 2. 前置知识

本讲默认你已掌握 u2-l1（定点数与位宽推导、`$signed`、`$clog2`）以及 u1-l3（`.param` 注入、`defines.sv` 宏注入、GRM 比特真闭环）。再补充三个本讲要用到的小概念：

- **DDS（直接数字频率合成，Direct Digital Synthesizer）**：用累加器把「频率」变成「相位斜坡」，再用查表把「相位」变成「波形幅度」。它是 NCO 的核心思想。
- **相位累加器（phase accumulator）**：一个每拍自增的寄存器，值随时间线性增长（像锯齿波），它的数值被解释为相位角。溢出回绕等价于相位走过一个完整周期 `2π`。
- **波形的对称性**：正弦/余弦在一个周期内有大量对称关系，例如
  - \(\sin(\pi-\theta)=\sin(\theta)\)（关于 π/2 对称，即「镜像」）
  - \(\sin(\pi+\theta)=-\sin(\theta)\)（后半周期取反，即「符号」）
  - \(\cos(\theta)=\sin(\pi/2-\theta)\)（正余弦互换）

  本讲的「象限重构」本质上就是用这几条恒等式，把任意角度折回到 `[0, π/4)` 这个小区间去查表。

> 关于标题：本讲大纲把这一技术称作「四分之一波长 ROM + 象限重构」，这是该类技术的通称。DRL 的具体实现**更进一步**——它只存八分之一周期 `[0, π/4)`，并用 sin、cos **两张表**联合表达。后面 4.2 节会解释这样做的代价与收益。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `.drl_src_code/sgen_nco/` 下：

| 文件 | 作用 |
|---|---|
| `rtl/sgen_nco.v` | NCO 的可综合 RTL：相位累加器、控制位译码、sin/cos 重构 case 表 |
| `octave/gen_sinusoidal_rom.m` | 生成两张 ROM 表 `nco_sin_rom.v` / `nco_cos_rom.v`（构建产物，不入库） |
| `octave/nco.m` | 黄金参考模型（GRM）：用「全周期表」算出比特真标准答案 |
| `octave/stimuli.m` | 串联 GRM、生成激励/响应/defines、搬动 ROM 表的脚本 |
| `octave/gen_defines.m` | 把参数写成 `defines_N.sv` 宏文件，供测试台注入 |
| `sim/testbench/sgen_nco_tb.sv` | SystemVerilog 测试台：喂 FCW、逐样本比对、±1 LSB 容差判定 |
| `.drl_param/sgen_nco_1.param` | 参数模板：`gp_rom_width=8, gp_rom_depth=5, gp_phase_accu_width=16` |

## 4. 核心概念与源码讲解

### 4.1 相位累加器与频率控制字（FCW）

#### 4.1.1 概念说明

NCO 的第一性问题：**怎么用数字电路产生一个频率可调的正弦波？**

答案是 DDS。把一个 `N` 位寄存器当作「相位」，每个时钟让它加一个固定步长 `FCW`（Frequency Control Word，频率控制字）。寄存器的值就像一个不断上涨的锯齿，每过 `2^N / FCW` 拍溢出回绕一次——回绕一次就是波形走过一个完整周期 `2π`。于是输出频率为

\[
f_o = \frac{\mathrm{FCW}}{2^N}\cdot f_s
\]

其中 `N = gp_phase_accu_width`（相位累加器位宽，默认 16），`f_s` 是时钟频率。FCW 越大，相位跑得越快，输出频率越高。调节 FCW 即可调频，这就是「数控」二字的来源。

#### 4.1.2 核心流程

1. 复位时累加器清零。
2. 使能后，每个上升沿 `r_phase_accu <= r_phase_accu + i_fcw`（自然回绕 = 走完一个周期）。
3. 把累加器的高位当作「相位查表地址」送给后续的 ROM 重构逻辑（4.2、4.3）。
4. 累加器的低位（被丢弃）只是相位量化误差，不影响查表结果。

#### 4.1.3 源码精读

模块端口与参数见 [sgen_nco.v:6-17](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L6-L17)：参数有三个——`gp_rom_width`（ROM 幅度位宽，默认 8）、`gp_rom_depth`（地址位宽，默认 5，**注意：仿真时会被测试台改写，见 4.2.3**）、`gp_phase_accu_width`（相位累加器位宽，默认 16）。端口沿用全库约定（u1-l4）：`i_rst_an` 异步低有效复位、`i_ena` 同步高有效使能、`i_clk` 上升沿；输入 `i_fcw` 是相位步长，输出 `o_sin/o_cos` 为带符号的 `gp_rom_width+1` 位（多 1 位放符号）。

相位累加器本身只有几行，见 [sgen_nco.v:43-53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L43-L53)：

```verilog
always @(posedge i_clk or negedge i_rst_an)
  begin: p_phase_accu
    if (!i_rst_an)
      r_phase_accu <= {gp_phase_accu_width{1'b0}};
    else if (i_ena)
      r_phase_accu <= r_phase_accu + i_fcw;
  end
```

这是 u2-l2 `dff` 那套三段式骨架的「累加器变体」：复位清零优先、使能才自增、否则隐式保持。`r_phase_accu + i_fcw` 的进位自然溢出，等价于相位模 `2π`。整段逻辑没有任何乘法器——频率合成完全是加法完成的。

GRM 一侧用同一公式定义 FCW，见 [nco.m:10](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L10)：`fcw = floor(M*fo/fs)`，其中 `M = 2^gp_phase_accu_width`（[nco.m:9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L9)），与上式 `\mathrm{FCW}=f_o/f_s\cdot 2^N` 完全一致。RTL 与 GRM 用同一个 FCW、同一个 `N`，这是比特真的第一颗锁扣。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证 FCW 与输出频率的对应关系。

1. 打开 `stimuli.m` 的测试用例 1（[stimuli.m:16-21](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/stimuli.m#L16-L21)）：`fo=150, fs=10e3`。
2. 手算 `fcw = floor(150/10000 * 2^16) = floor(983.04) = 983`。
3. 推算一个完整周期需要的时钟拍数 `= 2^16 / 983 ≈ 66.67` 拍；由于拍数必须取整，实际周期会有极轻微的频率误差（相位量化噪声），这是 DDS 的固有特性。
4. **现象/预期**：用 `./dsp_rtl_lib.sh -d sgen_nco`（或在装好 iverilog+octave 的环境跑 `-demo`）生成激励后，应能在 `stimuli_tc_1_mat.dat` 里看到每行都是 `983`（恒定的 FCW 激励）。

> 待本地验证：若环境无 iverilog/octave，可仅完成上面的手算与文件阅读。

#### 4.1.5 小练习与答案

**练习 1**：若想让输出频率翻倍，FCW 应如何变化？相位累加器位宽 `N` 增大会带来什么好处？

**答案**：FCW 直接翻倍即可（频率与 FCW 成正比）。增大 `N` 会提高相位分辨率，使频率步进更细 `\Delta f = f_s/2^N`、量化噪声更小，代价是累加器与 ROM 地址逻辑变宽。

---

### 4.2 八分之一周期 ROM：只存 `[0, π/4)` 的两张表

#### 4.2.1 概念说明

有了相位斜坡，下一步是「把相位映射成幅度」。最朴素的做法是存一整张「一个周期的正弦表」，但这样 ROM 很大。利用正余弦的对称性，可以把表大幅压缩：

- 第一道压缩：正弦在后半周期 `[π, 2π)` 是前半周期取反 → 只需存 `[0, π)`，靠「符号位」复原。
- 第二道压缩：正弦在 `[π/2, π)` 是 `[0, π/2)` 的镜像 → 只需存四分之一周期 `[0, π/2)`，靠「地址镜像」复原。

到这一步是经典的「四分之一波长 ROM」。DRL **再切一刀**：把 `[0, π/2)` 对半切成两个八分之一周期 `[0, π/4)`，分别存一张 **sin 表**和一张 **cos 表**。两张表各覆盖 `[0, π/4)`，但一条是 `\sin` 样本、一条是 `\cos` 样本。这样设计的妙处在 4.3 会显现：8 个八分之一周期（octant）恰好对应 3 个控制比特，sin/cos 互换与镜像可以写成极干净的异或逻辑。

存储量上，两张 octant 表的总深度 = 一张四分之一波长表的深度，所以并不更费存储，换来了更对称、更易推导的地址译码。

#### 4.2.2 核心流程

ROM 表是**构建产物**：仿真前由 Octave 现场生成、再 `\`include` 钩进 RTL。流程（见 `stimuli.m` 末尾）：

1. `gen_sinusoidal_rom(param)` 计算 `[0, π/4)` 上的 sin/cos 样本并量化成整数，写成 `nco_sin_rom.v` / `nco_cos_rom.v`。
2. 这两个 `.v` 文件里是一串 `assign nco_sin_rom[k] = ...;` 语句。
3. RTL 用 `\`include "nco_sin_rom.v"` 把它们拼进数组声明（[sgen_nco.v:40-41](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L40-L41)）。

样本生成公式见 [gen_sinusoidal_rom.m:14-22](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/gen_sinusoidal_rom.m#L14-L22)：

```matlab
sin_samples = A * sin( (pi/4) * [0:ROM_DEPTH-1]./ROM_DEPTH + Phase) + Offset;
cos_samples = A * cos( (pi/4) * [0:ROM_DEPTH-1]./ROM_DEPTH + Phase) + Offset;
sin_max     = A * sin( pi/4 + Phase) + Offset;     % 边界 π/4 处的值
cos_max     = A * cos( pi/4 + Phase) + Offset;
sin_samples_int = round( (2^(ROM_WIDTH-1)-1) * sin_samples );   % 量化
```

关键点：

- 采样区间是 `(π/4)·[0:ROM_DEPTH-1]/ROM_DEPTH`，即把 `[0, π/4)` 均匀切成 `ROM_DEPTH` 段。
- `ROM_ADDR_WIDTH = param.gp_rom_depth - 3`（[gen_sinusoidal_rom.m:3](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/gen_sinusoidal_rom.m#L3)），`ROM_DEPTH = 2^ROM_ADDR_WIDTH`。默认 `gp_rom_depth=5` 时，octant 表深度 = `2^(5-3) = 4`，即每张表只存 4 个样本。
- 量化用 `(2^(ROM_WIDTH-1)-1)`，即 `gp_rom_width=8` 时乘 127。这是一个**对称满量程**：范围 `[-127, +127]`，刻意避开补码中 `|最小值| = 最大值+1` 的不对称（详见 4.4.3）。
- `sin_max/cos_max` 单独存 `\sin(π/4)=\cos(π/4)=0.7071` 这一个边界值，用于 4.3 的镜像越界处理。

#### 4.2.3 源码精读：`gp_rom_depth` 的「双重身份」（重要细节）

这是本讲最容易踩坑的地方，请仔细看。`gp_rom_depth` 在不同地方含义不同：

- **在 `.param` 与 `stimuli.m` 里**，`gp_rom_depth = 5`，表示**一整个周期的相位分辨率**（全周期采样数 = `2^5 = 32`）。GRM `nco.m` 正是用它建一张 32 样本的全周期表（[nco.m:11](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L11)、[nco.m:15](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L15)）。
- **在 RTL 仿真里**，它被改写成 `gp_rom_depth - 3 = 2`，因为 RTL 物理上只存一个 octant（1/8 周期），地址位宽 = `5 - 3 = 2`（存 4 个样本）。这个改写发生在 `gen_defines.m` 把宏 `P_ROM_DEPTH` 写成 `param.gp_rom_depth-3`（[stimuli.m:64](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/stimuli.m#L64)），测试台再用 `.gp_rom_depth(\`P_ROM_DEPTH)` 覆盖 RTL 的参数默认值（[sgen_nco_tb.sv:129-133](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/sim/testbench/sgen_nco_tb.sv#L129-L133)）。

这里的 `-3` 不是魔法：它正是相位累加器顶端那 3 个用于「选 octant」的比特（4.3 节的 `w_ctrl`）。即

\[
\text{全周期地址位宽} = \text{octant 地址位宽} + 3\quad(\text{3 位选象限/八分之一周期})
\]

> 注意（文档漂移）：RTL 模板 [sgen_nco.v:8](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L8) 里 `gp_rom_depth` 的默认值是 5。若不加测试台覆盖直接例化，`c_rom_depth=32` 但生成的 ROM 只有 4 条 `assign`，数组大部分悬空——这是个「裸用即坏」的 wart。仿真链路自洽，全靠测试台把参数改回 2。这与 u1-l3 提到的「`.param` / `defines` / 测试台覆盖」三级注入机制一致。

RTL 里 ROM 数组与本地常量见 [sgen_nco.v:20-25](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L20-L25)：`c_rom_depth = 2**gp_rom_depth`，两张表 `nco_sin_rom`、`nco_cos_rom` 各 `c_rom_depth` 项，外加标量 `nco_sin_max`、`nco_cos_max`（边界值，由生成器一并写出）。

#### 4.2.4 代码实践（可运行）

**目标**：亲眼看到生成器吐出什么样的表。

1. 进入 `.drl_src_code/sgen_nco/octave/`，启动 octave，手动构造 param 并调用生成器：
   ```octave
   param.gp_rom_depth = 5; param.gp_rom_width = 8;
   param.ampl = 1; param.offset = 0; param.phase = 0;
   gen_sinusoidal_rom(param);
   ```
2. 打开生成的 `nco_sin_rom.v`，应看到 4 条 `assign nco_sin_rom[0..3] = ...;` 加一条 `assign nco_sin_max = 90;`。
3. **预期结果**：`nco_sin_rom[0]=0, [1]=25, [2]=49, [3]=69`（即 `round(127·sin(0))=0`、`round(127·sin(π/16))=25`、`round(127·sin(π/8))=49`、`round(127·sin(3π/16))=69`），`nco_sin_max = round(127·sin(π/4)) = round(89.8) = 90`。

> 待本地验证：精确整数值以你本机 octave 输出为准（round 行为在不同版本偶有 ±1 差异）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 octant 表只存 `[0, π/4)` 而不是 `[0, π/2)`？为什么还要额外存一个 `sin_max/cos_max` 标量？

**答案**：存 `[0, π/4)` 并同时存 sin、cos 两张表，使 8 个 octant 与 3 个控制比特一一对应，译码逻辑最简（4.3）。`sin_max/cos_max` 存的是 octant 右端点 `π/4` 处的值，用于镜像地址 `+1` 越过表末时的兜底（4.3.3）。

---

### 4.3 象限重构 case 表：3 个比特复原整周期

#### 4.3.1 概念说明

这是整个 NCO 最巧妙的部分。相位累加器一共 16 位，RTL 把它拆成三段：

| 比特段（默认 16 位累加器） | 位宽 | 作用 |
|---|---|---|
| `[15:13]`（顶端 3 位） | 3 | **octant 选择** → `w_ctrl`，决定符号/互换/镜像 |
| `[12:11]`（中间 `gp_rom_depth=2` 位） | 2 | **octant 内地址** → `w_addr`（可被镜像取反） |
| `[10:0]`（剩余低位） | 11 | **丢弃**（相位量化误差） |

顶端 3 位把一整个周期 `[0, 2π)` 等分成 **8 个 octant**，每个跨 `π/4`。对任意相位，只要知道它落在哪个 octant、在 octant 内的哪个位置，就能用 4.2 的两张小表 + 符号/互换/镜像三种操作拼出 `\sin` 与 `\cos`。

三种操作的物理含义：

- **符号（sign）**：输出取正还是取负（如 `[π,2π)` 内 sin 为负）。
- **正余弦互换（sinorcos）**：查 sin 表还是 cos 表（如利用 `\sin(\pi/2-\theta)=\cos(\theta)`）。
- **镜像（mirror）**：octant 内地址是否按位取反（对应 `\sin(\pi-\theta)=\sin(\theta)` 这类「关于中点对称」的关系）。

#### 4.3.2 核心流程：3 个控制位如何派生

`w_ctrl` 取顶端 3 位见 [sgen_nco.v:55](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L55)。随后用极简的异或/同或派生出各控制信号，见 [sgen_nco.v:59-65](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L59-L65)：

```verilog
assign w_sin_sign       = w_ctrl[2];                 // 顶端位 = sin 的符号
assign w_sin_sinorcos   = w_ctrl[1]  ^ w_ctrl[0];    // 选 sin/cos 表
assign w_cos_sign       = w_ctrl[2]  ^ w_ctrl[1];    // cos 的符号
assign w_cos_sinorcos   = w_ctrl[1] ~^ w_ctrl[0];    // (XNOR)
assign w_sin_cos_mirror = w_ctrl[0];                 // 地址镜像
```

地址生成（含镜像）见 [sgen_nco.v:56-57](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L56-L57)：`mirror=0` 时直接取 octant 内地址，`mirror=1` 时按位取反（对 2 位即 `a → 3-a`）：

```verilog
assign w_addr = (!w_sin_cos_mirror) ?  r_phase_accu[gp_phase_accu_width-1-3 -: gp_rom_depth] :
                                      ~r_phase_accu[gp_phase_accu_width-1-3 -: gp_rom_depth];
```

把 8 个 octant 的控制信号列成真值表（`b2 b1 b0 = w_ctrl[2] w_ctrl[1] w_ctrl[0]`，相位区间按累加器顶端 3 位均分 `2π`）：

| octant | b2 b1 b0 | 相位区间 | sin: 符号/表/镜像 | cos: 符号/表/镜像 |
|---|---|---|---|---|
| 0 | 0 0 0 | `[0, π/4)`     | + / sin / 否 | + / cos / 否 |
| 1 | 0 0 1 | `[π/4, π/2)`   | + / cos / 是 | + / sin / 是 |
| 2 | 0 1 0 | `[π/2, 3π/4)`  | + / cos / 否 | − / sin / 否 |
| 3 | 0 1 1 | `[3π/4, π)`    | + / sin / 是 | − / cos / 是 |
| 4 | 1 0 0 | `[π, 5π/4)`    | − / sin / 否 | − / cos / 否 |
| 5 | 1 0 1 | `[5π/4, 3π/2)` | − / cos / 是 | − / sin / 是 |
| 6 | 1 1 0 | `[3π/2, 7π/4)` | − / cos / 否 | + / sin / 否 |
| 7 | 1 1 1 | `[7π/4, 2π)`   | − / sin / 是 | + / cos / 是 |

读法：octant 0 的 sin 直接查 sin 表（正、不镜像）；octant 1 的 sin 改查 cos 表且地址镜像（利用 `\sin(\pi/4+\beta)=\cos(\pi/4-\beta)`）；octant 4 的 sin 与 octant 0 完全一样、只是符号取反（后半周期）……依次类推。**整张表的对称性正是 octant 切分的回报**——符号、互换、镜像都和顶端 3 位呈简单异或关系。

#### 4.3.3 源码精读：sin/cos 重构 case 表

sin 重构见 [sgen_nco.v:70-83](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L70-L83)，cos 重构见 [sgen_nco.v:85-98](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L85-L98)。两者结构相同，以 sin 为例（选择子是 `{w_sin_sign, w_sin_sinorcos, w_sin_cos_mirror}`）：

```verilog
case ({w_sin_sign,w_sin_sinorcos,w_sin_cos_mirror})
  0: w_sin =            $signed({1'b0,nco_sin_rom[w_addr]});                 // octant 0: +sin[a]
  3: w_sin = (!w_sel_sin_max) ? $signed({1'b0,nco_cos_rom[w_addr+1'b1]})    // octant 1: +cos[镜像+1]
                              : $signed({1'b0,nco_cos_max});
  2: w_sin =            $signed({1'b0,nco_cos_rom[w_addr]});                 // octant 2: +cos[a]
  ...
  4: w_sin = -2'sd1 * $signed({1'b0,nco_sin_rom[w_addr]});                   // octant 4: -sin[a]
  ...
endcase
```

几个要点：

1. **符号**：正号用 `{1'b0, rom_value}` 前补 0（u2-l1 的符号扩展）；负号用 `-2'sd1 * ...` 做补码取负。输出是 `gp_rom_width+1` 位带符号数，多出的 1 位放符号。
2. **`+1` 与越界兜底**：octant 1/3/5/7（镜像分支）查表时用 `w_addr+1`。当 `w_addr == c_rom_depth-1`（镜像后地址指向表末，对应 octant 最左端 `a=0`）时，`+1` 会越过表界，于是用 `w_sel_sin_max` 选 `sin_max/cos_max` 兜底（[sgen_nco.v:67-68](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L67-L68)）。这个边界值正是 4.2 单独存的 `\sin(π/4)`。
3. **cos 的镜像位复用 sin 的**：cos case 的选择子是 `{w_cos_sign, w_cos_sinorcos, w_sin_cos_mirror}`（[sgen_nco.v:87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L87)），第三位直接复用 `w_sin_cos_mirror`——因为 sin 与 cos 在同一相位下的镜像需求是绑定的。

最后两行 `assign o_sin = w_sin; assign o_cos = w_cos;`（[sgen_nco.v:100-101](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/rtl/sgen_nco.v#L100-L101)）把组合结果送出。整条 recon 路径是**纯组合**，唯一的状态就是相位累加器。

#### 4.3.4 代码实践（手工追踪，承接练习任务）

**目标**：亲手验证 `w_ctrl` 三位如何驱动一次完整重构。

设 `gp_phase_accu_width=16`，取一个落在 octant 1 的相位：令 `r_phase_accu = 16'b0010_0000_0000_0000`（= 8192，对应相位正好 `π/4`，octant 1 的左端点）。

1. `w_ctrl = r_phase_accu[15:13] = 3'b001` → 查上表 octant 1：sin 为「+ / cos / 镜像」。
2. `mirror = w_ctrl[0] = 1` → `w_addr = ~r_phase_accu[12:11] = ~2'b00 = 2'b11 = 3`。
3. `w_addr == c_rom_depth-1`（3）成立 → `w_sel_sin_max = 1` → sin 走兜底分支 `+nco_cos_max`。
4. **预期结果**：`o_sin = cos_max = round(127·cos(π/4)) = 90`。数学检验：`\sin(π/4) = 0.7071`，`round(127·0.7071)=90` ✓。
5. 再取 `r_phase_accu = 16'b0100_0000_0000_0000`（= 16384，相位 `π/2`，octant 2 左端点）：`w_ctrl=3'b010` → octant 2「+ / cos / 不镜像」，`w_addr=0` → `o_sin = cos_rom[0] = round(127·cos(0)) = 127`。即 `\sin(π/2)=1` → 127 ✓。

**需要观察的现象**：octant 1 用 cos 表 + 镜像算出了正弦值——这就是「正余弦互换 + 镜像」的实证。

#### 4.3.5 小练习与答案

**练习 1**：octant 6（`[3π/2, 7π/4)`）的 sin 为何是「− / cos / 不镜像」？写出对应的恒等式。

**答案**：设 `\theta\in[0,π/4)`，octant 6 对应相位 `\phi = 3π/2+\theta`。`\sin(3π/2+\theta) = -\cos(\theta)`。所以符号取负、查 cos 表、地址不镜像，得 `-cos_rom[a]`，与 case 表 octant 6（`-2'sd1 * cos_rom[w_addr]`）一致。

**练习 2**：为什么 cos 的 case 选择子第三位直接复用 `w_sin_cos_mirror`，而不是另立一个 `w_cos_mirror`？

**答案**：同一相位下，sin 与 cos 的「是否镜像」由同一个 octant 决定（镜像源自相位在 octant 内的左右半侧，与具体是 sin 还是 cos 无关），故镜像位共享。差异只体现在「符号」和「查哪张表」上，这两者已分别由 `w_cos_sign`、`w_cos_sinorcos` 表达。

---

### 4.4 nco.m 黄金模型与比特真

#### 4.4.1 概念说明

GRM（黄金参考模型）的角色是给 RTL 提供「标准答案」。NCO 的 GRM `nco.m`（作者署名 Kadhiem Ayob，源自 dsprelated）走的是**与 RTL 完全相反的策略**：它不压缩，直接存**一整个周期**的正余弦表，用相位顶端 5 位去查。这套「笨办法」实现简单、显然正确，适合作参照。本节回答两个问题：RTL 的压缩重构与 GRM 的全表查表，为什么能在每个样本上吻合？以及为什么测试台偏偏放 ±1 LSB 容差？

#### 4.4.2 核心流程

GRM 的核心循环见 [nco.m:18-27](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L18-L27)：

```matlab
ptr   = 0;
for i = 1:n
    ptr             = mod(ptr + fcw, M);    % 相位累加器（与 RTL 同公式）
    addr            = floor(ptr/2^lsb);     % 丢掉低位 lsb 位
    addr(addr >= k) = addr - k;             % 地址回绕（防御性）
    osin(i)         = lut_sin(addr+1);      % 查全周期表
    ocos(i)         = lut_cos(addr+1);
end
```

其中 `M = 2^16`（[nco.m:9](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L9)）、`k = 2^5 = 32`（[nco.m:11](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L11)）、`lsb = log2(M)-log2(k) = 16-5 = 11`（[nco.m:14](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L14)）。也就是说：GRM 用 `ptr` 的顶端 5 位（`floor(ptr/2^11)`，丢弃同样的 11 位低位）去查一张 32 样本的全周期表 `lut_sin[j] = round(A·sin(j·π/16))`（[nco.m:15](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L15)）。

#### 4.4.3 源码精读：比特真的两颗锁扣与一个 1-LSB 陷阱

**锁扣一：相同相位点。** RTL 把 `[15:13]` 当 octant、`[12:11]` 当 octant 内地址、丢弃 `[10:0]`；GRM 取 `[15:11]` 当全表地址、丢弃 `[10:0]`。两者丢弃的低位完全相同（都是 11 位），因此**采样在完全相同的相位点上**：GRM 的 5 位地址 `j = octant×4 + octant内地址`，正是 RTL 的 octant 与 octant 内地址拼接。

**锁扣二：三角恒等式对实数精确成立。** 以 octant 1 为例，RTL 用 `cos_rom` 重构 `\sin(π/4+\beta)`。数学上 `\sin(π/4+\beta) = \cos(π/4-\beta)` 是**精确恒等式**（实数层面无误差）。于是对同一个真实数值 `v`，`round(127·v)` 在两条路径上给出**同一个整数**——这是「单 octant 表重构 = 全表查表」能逐比特吻合的数学根基。同理 `\sin(π-\theta)=\sin(\theta)`、`\sin(π+\theta)=-\sin(\theta)` 也都精确成立。

**1-LSB 陷阱：量化尺度不一致。** 这是本讲一个诚实的重要发现。RTL 侧（`gen_sinusoidal_rom.m`）量化用 `(2^(ROM_WIDTH-1)-1) = 127`（[gen_sinusoidal_rom.m:19](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/gen_sinusoidal_rom.m#L19)），而 GRM 侧（`nco.m`）用 `A = ampl·2^(ROM_WIDTH-1) = 128`（[nco.m:8](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/nco.m#L8)）。以 `\sin(π/2)=1` 为例：

- GRM：`round(128·1) = 128`
- RTL（octant 2，`cos_rom[0]`）：`round(127·1) = 127`

两者差 1。这正是 RTL 刻意用 127（保持对称满量程 `[-127,+127]`，回避补码 `|最小|>最大` 的不对称）而 GRM 用 128 造成的系统差。测试台因此把判定容差设为 ±1 LSB（见下）。严格意义上，NCO 不是「零误差比特真」，而是「±1 LSB 比特真」。

**测试台的容差判定**见 [sgen_nco_tb.sv:104-113](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/sim/testbench/sgen_nco_tb.sv#L104-L113)：

```verilog
always @(negedge i_clk) begin: ASSERT_RTL_vs_MATLAB
  if (i_rst_an && i_ena) begin
    diff     = o_sin_rtl - o_sin_mat;
    abs_diff = `ABS(diff);
    if ( abs_diff > 1) begin
      $error("### RTL = %d, MAT = %d", o_sin_rtl, o_sin_mat); error_count <= error_count + 1;
    end
  end
end
```

注意它只比对 `o_sin`（`abs_diff > 1` 才报错），在 `negedge i_clk` 采样以避开组合翻转。这与 filt_cicd 等模块的「严格相等」不同，是 NCO 验证的一个特点。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解测试台的节拍，并定位「容差从何而来」。

1. 读 [sgen_nco_tb.sv:86-96](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/sim/testbench/sgen_nco_tb.sv#L86-L96)：激励 `i_fcw` 在 `posedge s_clk` 读入；响应比对在 `negedge i_clk`。`s_clk` 比 `i_clk` 早 1 个时间单位（`assign #1 i_clk = s_clk`，[sgen_nco_tb.sv:56](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/sim/testbench/sgen_nco_tb.sv#L56)），保证「先喂 FCW、累加器更新、再在下降沿采样比对」。
2. 在 `nco.m` 把 `A` 改成 `param.ampl*(2^(param.gp_rom_width-1)-1)`（即 127），重跑 GRM，预期 `abs_diff` 全部归零——以此验证「±1 容差 solely 来自 127 vs 128」。
3. **预期结果**：改尺度后误差应消失（或仅剩极个别 round 半数边界）。

> 待本地验证：第 2 步需能跑通 octave + iverilog 回归。

#### 4.4.5 小练习与答案

**练习 1**：假如把 RTL 的量化尺度也改成 128（与 GRM 一致），会有什么副作用？

**答案**：`\sin(π/2)=1` 时幅度 `round(128·1)=128`，在 8 位补码里 `128` 会溢出（`1000_0000` 表示 `-128`）。RTL 输出虽是 9 位（带符号位）能放下 128，但若下游按 8 位消费就会出错。用 127 正是为了保持对称满量程、避免补码不对称陷阱，代价就是与用 128 的 GRM 差 1 LSB。

**练习 2**：GRM `nco.m` 里 `addr(addr >= k) = addr - k;` 这一行在什么情况下才会触发？为什么 RTL 不需要这一句？

**答案**：`ptr` 经 `mod(.., M)` 后最大 `M-1`，`floor((M-1)/2^lsb)` 顶多为 `floor((2^16-1)/2^11) = 31 = k-1`，正常不会 `≥ k`，所以这是**防御性**写法（防极端取整/参数组合）。RTL 一侧用 octant（顶端 3 位天然在 0..7）+ octant 内地址（天然在表界内）+ 镜像/兜底，地址结构上不会越界，故不需要等价语句。

---

## 5. 综合实践

把本讲四个最小模块串起来，做一次「端到端相位追踪」。

**任务**：参数取默认值（`gp_rom_width=8, gp_rom_depth=5→octant 表深 4, gp_phase_accu_width=16`），`fcw=983`（`fo=150Hz, fs=10kHz`）。追踪前若干拍，手工填出下表并核对 GRM：

| 拍 n | r_phase_accu（十进制） | w_ctrl | octant | w_addr（镜像后） | o_sin（RTL，手算） | o_sin（GRM） | 差值 |
|---|---|---|---|---|---|---|---|

步骤：

1. 第 0 拍 `r_phase_accu=0` → octant 0、`w_addr=0` → `o_sin=sin_rom[0]=0`。
2. 第 1 拍 `r_phase_accu=983` → `[15:13]=000`（octant 0，因 983 < 8192）、`[12:11]` 取 983 的 bit12/11。`983 = 0b0011_1101_0111`，`[12:11]=00`？请按实际位权计算（`983>>11 = 0`，故 octant 内地址仍为 0）→ `o_sin=0`。体会相位量化：FCW 太小时，连续多拍落在同一表项。
3. 跳到 `r_phase_accu` 首次进入 octant 1（即 ≥ `2^13 = 8192`）的那一拍，按 4.3.4 的方法手算 `o_sin`。
4. 用 `./dsp_rtl_lib.sh -d sgen_nco`（或 `-demo`）跑回归，打开 `response_tc_1_rtl.dat` 与 `response_tc_1_mat.dat` 比对，验证你的手算与仿真一致、且 `abs_diff ≤ 1`。

**预期**：手算值与 RTL 仿真一致；RTL 与 GRM 差值恒 ≤ 1（满量程点处 = 1，其余多为 0）。

> 待本地验证：仿真结果以本机工具链为准。

## 6. 本讲小结

- NCO = 「相位累加器 + 查表」：`r_phase_accu` 每拍加 `i_fcw`，自然溢出回绕 = 走完一个周期，输出频率 `f_o = \mathrm{FCW}\cdot f_s / 2^N`，全程无乘法器。
- ROM 被压到**八分之一周期** `[0, π/4)`，用 sin、cos 两张 4 样本小表联合表达；总存储与一张四分之一波长表相当，换来更对称的地址译码。
- 相位累加器顶端 3 位 `w_ctrl` 选 8 个 octant，靠简单异或/同非派生出**符号、正余弦互换、镜像**三种操作，case 表据此用小表复原全周期 sin/cos——纯组合，唯一状态是累加器。
- 镜像分支用 `w_addr+1` 配合 `sin_max/cos_max` 兜底处理 octant 边界 `π/4` 的越界。
- `gp_rom_depth` 有「双重身份」：`.param`/GRM 里 =5 是全周期分辨率；RTL 仿真被测试台改写成 `5-3=2`（octant 地址位宽），`-3` 即顶端选 octant 的 3 位。
- 比特真靠两颗锁扣（同相位点采样 + 三角恒等式精确）+ 一个已知 1-LSB 陷阱（RTL 量化用 127、GRM 用 128），故测试台判 ±1 LSB 容差，NCO 属「±1 LSB 比特真」。

## 7. 下一步学习建议

- **横向对比另一种波形生成思路**：本单元 u6-l1/u6-l2 的 `sgen_cordic` 用「移位相加旋转」生成正余弦，完全不查表。对比 CORDIC 与 NCO 的资源/精度/延迟取舍，是巩固本讲的好方法。
- **吃透验证方法学**：本讲的 ±1 LSB 容差是特例，建议接着读 u7-l1（比特真验证方法论）与 u7-l2（九测试用例激励设计），看清 `stimuli.m` 如何为 NCO 设计 4 个测试用例（不同 `fo`、含多频切换的 case 4）。
- **继续读源码**：精读 `stimuli.m`（[stimuli.m:62-92](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/sgen_nco/octave/stimuli.m#L62-L92)）看 ROM 表如何被生成、改名、搬进 `sim/testcases/stimuli/` 供 `\`include`；这是 u1-l3「构建产物不入库」机制在 NCO 的具体落地。
