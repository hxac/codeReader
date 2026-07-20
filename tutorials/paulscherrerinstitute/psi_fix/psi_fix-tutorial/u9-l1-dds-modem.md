# DDS 与非整数比调制解调

## 1. 本讲目标

本讲把前几单元建立的「位真双模型 + 两段式风格 + 协同仿真」方法论，应用到一组真正贴近工程现场的组件上：**数控振荡器 DDS** 与一对互为镜像的**调制 / 解调器**。

学完后你应该能够：

- 说清 `psi_fix_dds_18b` 的**相位累加器 + 查表**结构，以及它如何用「正弦表 + 四分之一圈偏移」同时产出 sin 与 cos。
- 用 `ratio_num_g / ratio_den_g` 两个 generic 解释**非整数采样率比**是如何用「同一张表、不同步长」实现的（一个类 Bresenham 的模运算技巧）。
- 画出 `demod_real2cplx`（实→复，下变频）与 `mod_cplx2real`（复→实，上变频）的**对称数据流**，说清两者在「是否需要滑动平均滤波」上的本质差别。
- 对照 `config.tcl` 的参数矩阵，验证非整数比在回归测试中确实被覆盖。

---

## 2. 前置知识

本讲默认你已经掌握前置讲义建立的几条共同语言：

- **定点格式三元组 [s,i,f]** 与**位增长规则**（u1-l4、u2-l2）：乘法整数位「相加再 +1」、加法整数位 +1。
- **复数 = (I, Q) 一对定点**，复数乘法 `I·sin + Q·cos` 的实/虚部乘加结构（u5-l1）。
- **位真双模型**：每个可综合组件配一个逐位一致的 Python 黄金模型，自检测试台用 `###ERROR###` 判失败（u3-l1、u3-l2）。
- **`psi_fix_lin_approx` 分段线性近似**：sin18b 等函数组件由「一个手写内核 + 一张生成表」组成（u8-l1、u8-l2）。
- **两段式编码风格**：组合进程写 `r_next`、时序进程 `r <= r_next`，用 record 封装流水（u3-l3）。

几个本讲要用到的名词先点一下：

- **DDS（Direct Digital Synthesizer）/ NCO（数控振荡器）**：用「相位累加器 + 正弦查表」数字合成一个频率可调的正弦/余弦波，频率由每拍累加的**相位步进**决定。
- **调制 / 解调（modulation / demodulation）**：把基带信号搬移到载波频率叫**调制（上变频）**，把载波上的信号搬回基带叫**解调（下变频）**。本讲的 mod 用复数 (I,Q) 调出实信号 RF，demod 把实信号解成复数 (I,Q)。
- **TDM（时分复用）**：多个通道轮流共用一根数据线，一个完整轮回叫一个 TDM 帧（u6-l3 已建立此概念）。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下，**VHDL 实现 / Python 位真模型 / 测试台**一一对应：

| 文件 | 作用 |
|:--|:--|
| [hdl/psi_fix_dds_18b.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd) | 18 位 DDS：相位累加 + sin/cos 双查表，支持 TDM 多通道。 |
| [model/psi_fix_dds_18b.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_dds_18b.py) | DDS 的位真黄金模型，用整数 cumsum 仿真相位累加。 |
| [hdl/psi_fix_demod_real2cplx.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd) | 解调器：实输入 → 复输出（下变频），含滑动平均梳状滤波。 |
| [model/psi_fix_demod_real2cplx.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_demod_real2cplx.py) | demod 的位真黄金模型。 |
| [hdl/psi_fix_mod_cplx2real.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd) | 调制器：复输入 → 实输出（上变频），`RF = I·sin(w) + Q·cos(w)`。 |
| [model/psi_fix_mod_cplx2real.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mod_cplx2real.py) | mod 的位真黄金模型。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归脚本：声明三个测试台及其 generic 参数矩阵（含非整数比）。 |
| [Changelog.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md) | 4.0.0 引入非整数比、3.0.0 给 DDS 加 TDM。 |

> 提示：DDS 的 sin/cos 查表内核是 `psi_fix_lin_approx_sin18b_dual`（u8-l2 讲过的 sin18b 双通道版），本讲把它当黑盒，只关心「喂相位、出 18 位 sin/cos」。

---

## 4. 核心概念与源码讲解

### 4.1 DDS 相位累加与正余弦查表

#### 4.1.1 概念说明

DDS（Direct Digital Synthesizer）是 FPGA 里合成正弦波的标准手段，也叫 NCO（Numerically Controlled Oscillator）。它的核心思想极其朴素：

> 把「相位」当成一个无符号小数累加器，每拍加一个固定的**相位步进 phi_step**；再用这个相位去查一张正弦表，就得到该拍的正弦样本。

相位步进越大，每拍转过的角度越多，合成波形的频率越高。若相位累加器总位宽为 W、步进为 Δφ（以「圈」为单位，满 1.0 圈 = 2π），采样率为 fS，则输出频率为：

\[ f_{out} = \Delta\phi \cdot f_S, \qquad \Delta\phi = \frac{\text{phi\_step}}{2^{W}} \]

`psi_fix_dds_18b` 的特点：

- 相位累加器默认格式 `phase_fmt_g = (0,0,31)`，即 **32 位无符号**，把「圈数」放在 [0,1) 区间——这正是 u1-l4 讲过的「无符号小数」用法。
- 输出 **18 位** sin 与 cos（`SinOutFmt_c = (1,0,17)`），由 `lin_approx_sin18b_dual` 查表实现，误差小于 1 LSB（详见组件文档）。
- **cos 不用第二张表**：因为 \(\sin(x+\pi/2)=\cos(x)\)，而 \(\pi/2\) = 0.25 圈，所以把相位加 0.25 圈再查同一张 sin 表就得到 cos。
- 支持 **TDM 多通道**：多个 DDS 共用一份硬件，通道轮流时分复用。
- 频率/相位/重启均可**运行时**改变，且第一个样本严格从相位 0 开始（可复现）。

#### 4.1.2 核心流程

DDS 的数据流（对应文档里「总流水延迟 10 拍」）：

```
phi_step_i ──┐
             ▼
        ┌──────────┐  restart_i / FirstSplCnt 强制首样本相位=0
        │ 相位累加  │  PhaseAccu(k) = PhaseAccu(k-1) + phi_step  (无符号自然回绕=绕圈)
        └────┬─────┘
             │  + phi_offset_i  (Stage 1)
             ▼
        ┌──────────┐
        │  相位对齐  │  resize 到查表输入格式 SinInFmt=(0,0,20)   (Stage 2)
        └────┬─────┘
             ├── 直送 ──────────► sin 查表 ──► dat_sin_o (18b)
             └── + 0.25 圈 ────► sin 查表 ──► dat_cos_o (18b)
                                              (vld_o = 流水末级有效)
```

要点：

1. **累加用无符号加法，溢出即回绕**：32 位无符号加法自然模 2³²，等价于相位绕圈，不需要专门的取模电路。
2. **TDM 用延时线拆通道**：相位累加器的反馈经过一个 `tdm_channels_g` 拍的延时线，使每个 TDM 时隙各自维护自己的累加值——用一条移位寄存器代替 N 个累加器寄存器组。
3. **FirstSplCnt 保证首样本为 0**：复位后前 `tdm_channels_g` 个有效样本强制相位 0（每个通道各一个 0），之后才正常累加；`restart_i` 重新触发该机制。

#### 4.1.3 源码精读

**实体接口**（[hdl/psi_fix_dds_18b.vhd:15-34](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L15-L34)）：注意 `phi_step_i` / `phi_offset_i` 都是**运行时**输入（不是 generic），位宽等于相位累加器宽度。

**关键常量**（[hdl/psi_fix_dds_18b.vhd:39-41](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L39-L41)）：

```vhdl
constant SinOutFmt_c : psi_fix_fmt_t := (1, 0, 17);            -- 18 位 sin/cos 输出
constant SinInFmt_c  : psi_fix_fmt_t := (0, 0, 20);            -- 查表输入相位（无符号 20 位）
constant CosOffs_c   : ...           := psi_fix_from_real(0.25, phase_fmt_g);  -- cos = sin(φ+0.25圈)
```

**两段式 record + 流水**（[hdl/psi_fix_dds_18b.vhd:44-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L44-L53)）：用 `VldIn : std_logic_vector(0 to 9)` 把有效信号随数据逐级平移 10 级（u3-l3 的 valid 数组流水范式）；`_0/_1/_2` 后缀标流水级。

**相位累加 + FirstSplCnt**（[hdl/psi_fix_dds_18b.vhd:92-106](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L92-L106)）——这是 DDS 的心脏：

```vhdl
if vld_i = '1' then
  if restart_i = '1' or r.FirstSplCnt_0 /= 0 then
    v.PhaseAccu_0 := (others => '0');          -- 首样本/重启: 强制相位 0
  else
    v.PhaseAccu_0 := psi_fix_add(PhaseAccu, phase_fmt_g,
                                 phi_step_i, phase_fmt_g,
                                 phase_fmt_g);  -- 否则: 累加步进 (无符号自然回绕)
  end if;
  if r.FirstSplCnt_0 /= 0 then
    v.FirstSplCnt_0 := r.FirstSplCnt_0 - 1;     -- 倒数 tdm_channels_g 个零相位样本
  end if;
end if;
```

**相位偏置 + sin/cos 双查表相位**（[hdl/psi_fix_dds_18b.vhd:110-119](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L110-L119)）：`PhaseCos_2` 比 `PhaseSin_2` 多加一个 `CosOffs_c`(0.25 圈)，两者送进**同一个** `lin_approx_sin18b_dual` 内核的两路端口。

**自检断言**（[hdl/psi_fix_dds_18b.vhd:65-73](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L65-L73)）：组件内部就检查 sin/cos 两路 valid 必须一致、且与流水末级 `VldIn(9)` 对齐，不符即打 `###ERROR###`——把 u3-l2 的失败约定内置到了组件里。

**TDM 延时线**（[hdl/psi_fix_dds_18b.vhd:188-202](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L188-L202)）：用 `psi_common_delay` 把累加器反馈延时 `tdm_channels_g` 拍，使每个 TDM 通道各自累加自己的 `phi_step`。复位时 `FirstSplCnt_0 <= tdm_channels_g`（[hdl/psi_fix_dds_18b.vhd:137-149](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_dds_18b.vhd#L137-L149)）。

**Python 位真模型**（[model/psi_fix_dds_18b.py:52-77](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_dds_18b.py#L52-L77)）用整数 cumsum 仿真累加，刻意避开浮点误差：

```python
phaseSteps = np.ones(numOfSamples,dtype=np.int64)*psi_fix_get_bits_as_int(phaseStepFix, self.phaseFmt)
phaseSteps[0] = 0                                   # 从 0 开始 (对应 FirstSplCnt)
accumulator = np.cumsum(phaseSteps,dtype=np.int64) + psi_fix_get_bits_as_int(phaseOffsetFix, self.phaseFmt)
accuWrapped = accumulator % 2**psi_fix_size(self.phaseFmt)   # 模 2^W = 绕圈
...
phaseQuantCos = psi_fix_resize(accuPhase+0.25, ...)          # cos = sin(φ+0.25圈)
```

> 这里 `phaseSteps[0] = 0` 对应 VHDL 的 FirstSplCnt「首样本相位 0」；`% 2**size` 对应无符号加法自然回绕。两侧位真一致。

#### 4.1.4 代码实践

**实践目标**：用 DDS 的 Python 黄金模型直观感受「相位步进 → 输出频率」的关系，并验证 cos = sin(φ+0.25 圈)。

**操作步骤**（源码阅读 + 本地运行型）：

1. 打开 [testbench/psi_fix_dds_18b_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_dds_18b_tb/Scripts/preScript.py)，注意它用 `PHASE_STEP0 = 0.12345`、`PHASE_STEP1 = 0.2` 两组步进合成波形。
2. 在 `model/` 目录下写一段最小调用（**示例代码**，非项目原有）：

   ```python
   import sys; sys.path.append("model")
   from psi_fix_dds_18b import psi_fix_dds_18b
   from psi_fix_pkg import psi_fix_fmt_t
   import numpy as np
   m = psi_fix_dds_18b(psi_fix_fmt_t(0,0,31))
   sin0, cos0 = m.Synthesize(0.12345, 10000)   # 步进 0.12345 圈
   ```
3. 对 `sin0` 做 FFT，找到峰值频率 `f_peak`（归一化到 fS）。

**需要观察的现象与预期结果**：

- 频谱峰值应落在 \(f_{out} = 0.12345 \cdot f_S\)（即 0.12345）处。
- 把步进改成 `0.2`（`PHASE_STEP1`），峰值应移到 0.2。
- 比较 `sin0` 与 `cos0`：`cos0[k] ≈ sin0` 在时间上左移 N/4 个样本（四分之一周期），印证 cos = sin(φ+0.25 圈)。
- 若你跑的是 VHDL 回归，DDS 测试台会用 [config.tcl:275-278](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L275-L278) 的 4 组参数（`tdm_channels_g ∈ {1,2}`、`idle_cycles_g ∈ {0,5}`）逐位比对模型输出，无 `###ERROR###` 即位真通过。

> 若本地无 SciPy/Modelsim 环境，FFT 这一步标注「待本地验证」，但「步进越大、频率越高」这一结论可从模型源码直接读出。

#### 4.1.5 小练习与答案

**练习 1**：相位累加器为 `(0,0,31)`，`phi_step` 取满量程的 1/8（即 0.125 圈/拍），输出频率是 fS 的多少？

**答案**：\(f_{out} = 0.125 \cdot f_S = f_S/8\)。

**练习 2**：为什么 DDS 用「无符号」相位格式，而不是有符号？

**答案**：无符号加法溢出天然等于「模 2^W 绕圈」，正好对应相位绕过一整圈回到 0；用有符号则要额外处理 -1.0..+1.0 的不对称范围与回绕逻辑，反而复杂。模型里也强制要求无符号（[model/psi_fix_dds_18b.py:34-35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_dds_18b.py#L34-L35) 会拒绝有符号格式）。

---

### 4.2 非整数比调制解调（ratio_num / ratio_den 的相位步进）

#### 4.2.1 概念说明

demod 与 mod 都需要一个片内**本振（LO）**去乘信号。最朴素的做法是：事先把一个完整周期的 sin/cos 离散成 `ratio_num` 个点存成表，每来一个样本指针 `+1`，转 `ratio_num` 个样本走完一圈，于是本振频率 \(f_{LO} = f_S/\text{ratio\_num}\)。这就是**整数比**情形（`ratio_den = 1`）。

但工程上经常需要**非整数比**：例如时钟 250 MHz、信号 50 MHz 是整数比 5；可若想要 150 MHz 呢？250/150 = 5/3，是分数。4.0.0 版本正是为此引入了 `ratio_num_g / ratio_den_g` 两个 generic（见 [Changelog.md:14](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L14)）。

关键洞察是——**不需要为分数比造一张新表**。同一张 `ratio_num` 点的表，只要把每拍的指针步长从 1 改成 `ratio_den`，本振频率就变成：

\[ f_{LO} = \frac{\text{ratio\_den}}{\text{ratio\_num}} \cdot f_S = \frac{f_S}{\text{ratio\_num}/\text{ratio\_den}} \]

这本质是一个**模运算的相位累加**，思路与「分数分频」「Bresenham 画线」同源：用整数步长在一张离散表上跳着走，靠取模自动绕圈。

- `ratio_num=5, ratio_den=1`：步长 1，f_LO = fS/5。
- `ratio_num=5, ratio_den=3`：步长 3，f_LO = (3/5)·fS（等效比 5/3）。
- `ratio_num=100, ratio_den=3`：步长 3，f_LO = (3/100)·fS，分辨率更细。

> 注：当 `gcd(ratio_num, ratio_den) > 1`，指针只遍历 `ratio_num/gcd` 个点便重复；`gcd=1`（如 100 与 3）则遍历全部 `ratio_num` 点。这正是测试用 `ratio_num=100, ratio_den=3` 的动机——覆盖「遍历全表」的情形。

#### 4.2.2 核心流程

非整数比的核心是那个**模运算相位指针**。以 demod 为例（[hdl/psi_fix_demod_real2cplx.vhd:141-156](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L141-L156)）：

```
每个 vld_i=1 的样本:
    若 cptInt < ratio_num - ratio_den:
        cptInt ← cptInt + ratio_den          # 未越界: 直接步进
    否则:
        cptInt ← ratio_den - (ratio_num - cptInt)   # 越界: 等价 (cptInt+ratio_den) mod ratio_num
```

用数学写就是：

\[ \text{cpt}[k] = (\text{cpt}[0] + k\cdot\text{ratio\_den}) \bmod \text{ratio\_num} \]

那条「越界分支」只是把 `(cptInt + ratio_den) mod ratio_num` 拆成不调用取模的加减法（FPGA 上更省），展开即 `cptInt + ratio_den - ratio_num`，与代码里 `ratio_den - (ratio_num - cptInt)` 完全等价。

整个 demod（实→复）的数据流：

```
dat_i ──► 拆通道 ──► × sin_table[cpt] ──► 滑动平均( taps=ratio_num ) ──► I (dat_inp_o)
                   └► × cos_table[cpt] ──► 滑动平均( taps=ratio_num ) ──► Q (dat_qua_o)
                                        ▲
             cpt 指针: (cpt + ratio_den) mod ratio_num  +  phi_offset_i
```

#### 4.2.3 源码精读

**generic 声明**（[hdl/psi_fix_demod_real2cplx.vhd:30-38](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L30-L38)）：`ratio_num_g`（分子，默认 5）与 `ratio_den_g`（分母，默认 1）。

**系数表规模由 `ratio_num` 决定**（[hdl/psi_fix_demod_real2cplx.vhd:58](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L58)）：

```vhdl
type coef_array_t is array (0 to ratio_num_g - 1) of ...;  -- 表长 = ratio_num
```

**sin/cos 系数生成函数**（[hdl/psi_fix_demod_real2cplx.vhd:60-78](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L60-L78)）：第 i 项 = `sin(2π·i/ratio_num)·scale`，`scale` 预补偿滑动平均的增益（见 4.3）。

**非整数比指针（本讲核心）**（[hdl/psi_fix_demod_real2cplx.vhd:141-156](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L141-L156)）：

```vhdl
if vld_i = '1' then
  if cptInt < ratio_num_g - ratio_den_g then
    cptInt <= cptInt + ratio_den_g;                       -- 步进 ratio_den
  else
    cptInt <= ratio_den_g - (ratio_num_g - cptInt);       -- 模 ratio_num 回绕
  end if;
end if;
```

**相位偏置端口**（[hdl/psi_fix_demod_real2cplx.vhd:158-174](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L158-L174)）：运行时 `phi_offset_i`（位宽 `log2ceil(ratio_num_g)`）加到指针上得到 `cpt_s`，并断言其 `<= ratio_num_g-1`，否则 `###ERROR###`。

**mod 侧同构指针**（[hdl/psi_fix_mod_cplx2real.vhd:122-141](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd#L122-L141)）：`cpt_v` 用完全相同的「步进 ratio_den、模 ratio_num 回绕」逻辑驱动同一张 sin/cos 表。

**Python 黄金模型镜像同一指针**（demod，[model/psi_fix_demod_real2cplx.py:62-65](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_demod_real2cplx.py#L62-L65)）：

```python
phaseSteps = np.ones(inData.size, dtype=np.int64)
phaseSteps[0] = 1 - self.ratio_den                       # 首样本归零
cpt = (phaseOffset + np.cumsum(phaseSteps + self.ratio_den - 1, dtype=np.int64)) % self.ratio_num
```

即 `cpt[k] = (phaseOffset + k·ratio_den) mod ratio_num`，与 VHDL 的指针一一对应。mod 模型（[model/psi_fix_mod_cplx2real.py:60-68](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mod_cplx2real.py#L60-L68)）同理。两侧指针位真一致，是非整数比能逐位验证的前提。

#### 4.2.4 代码实践（本讲主实践任务）

**实践目标**：亲手验证 `ratio_num_g/ratio_den_g` 如何表达非整数比，并对照 `config.tcl` 的测试参数确认回归确实覆盖了非整数比。

**操作步骤**：

1. **算术推演**（纸笔）：填下表，验证「步长 ratio_den、模 ratio_num」给出的本振频率。

   | ratio_num | ratio_den | 指针序列（前 8 个有效样本） | 等效比 ratio_num/ratio_den | f_LO（×fS） |
   |:--:|:--:|:--|:--:|:--:|
   | 5 | 1 | 0,1,2,3,4,0,1,2 | 5 | 0.2 |
   | 5 | 3 | 0,3,1,4,2,0,3,1 | 5/3 | 0.6 |
   | 100 | 3 | 0,3,6,9,…（遍历全表 100 点） | 100/3 | 0.03 |

   提示：第 2 行指针 = `(k·3) mod 5` → 0,3,1,4,2,0,…（验证：3+3=6 mod 5=1，再 +3=4，再 +3=7 mod 5=2…）。

2. **对照 config.tcl**（[sim/config.tcl:309-312](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L309-L312)）：

   ```
   "-gfile_folder_g=$dataDir -gduty_cycle_g=1"                                  # 默认 ratio_num=5, ratio_den=1
   "-gfile_folder_g=$dataDir -gduty_cycle_g=5"
   "-gfile_folder_g=$dataDir -gduty_cycle_g=1" "-gratio_num_g=5"  "-gratio_den_g=3"
   "-gfile_folder_g=$dataDir -gduty_cycle_g=1" "-gratio_num_g=100" "-gratio_den_g=3"
   ```

   确认第 3、4 组就是非整数比（5/3 与 100/3）。

3. **对照 preScript 的激励**（[testbench/psi_fix_demod_real2cplx_tb/Scripts/preScript.py:37-59](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_demod_real2cplx_tb/Scripts/preScript.py#L37-L59)）：注意激励信号频率写成 `np.sin(...*2*pi*1/(ratio_num/ratio_den))`，即信号周期 = `ratio_num/ratio_den` 个样本，与 demod 的本振频率**严格一致**——这样混频后才落在基带（DC）。

**需要观察的现象与预期结果**：

- 步骤 1 的指针序列应与表中「等效比」「f_LO」自洽：本振每拍转 `ratio_den/ratio_num` 圈。
- 步骤 2 应确认：默认 generic 是整数比 5；非整数比由后两组参数注入。
- 步骤 3 解释了为何 preScript 要按 `ratio_num/ratio_den` 设信号频率——保证解调后落在 DC。
- VHDL 回归（若有环境）：4 组参数全部无 `###ERROR###`，说明非整数比在 mod 与 demod 两侧均位真通过。**待本地验证**（若无 Modelsim）。

#### 4.2.5 小练习与答案

**练习 1**：把 `ratio_num=5, ratio_den=3` 代入指针递推，写出前 6 个 `cpt` 值。

**答案**：0, 3, 1, 4, 2, 0（即 `(k·3) mod 5`）。可见 5 个样本走完一圈，本振频率 = 3/5·fS。

**练习 2**：为什么 `ratio_num=100, ratio_den=3` 能给出比 `ratio_num=5, ratio_den=3` 更细的频率分辨率？

**答案**：表长 100 把一个周期切得更细（相位分辨率 2π/100），且 gcd(100,3)=1 使指针遍历全部 100 点，故可用更小的步长比 3/100 表达更接近任意目标频率的本振；而 5 点表只有 5 个离散相位可走。

**练习 3**：那条回绕分支 `cptInt <= ratio_den_g - (ratio_num_g - cptInt)` 在算术上等价于什么？

**答案**：等价于 `(cptInt + ratio_den) mod ratio_num`。展开：`ratio_den - (ratio_num - cptInt) = cptInt + ratio_den - ratio_num`，正是越界（≥ratio_num）后的取模结果。

---

### 4.3 demod 与 mod 的对称结构

#### 4.3.1 概念说明

`demod_real2cplx` 与 `mod_cplx2real` 是一对**互为镜像**的组件，名字里的 `real2cplx` / `cplx2real` 已经点明方向：

| 维度 | demod_real2cplx（解调 / 下变频） | mod_cplx2real（调制 / 上变频） |
|:--|:--|:--|
| 方向 | 实 → 复 | 复 → 实 |
| 输入 | `dat_i`（实信号，TDM 多通道） | `dat_inp_i`(I), `dat_qua_i`(Q) |
| 混频 | 实 × sin → I；实 × cos → Q | I × sin + Q × cos |
| 滤波 | **滑动平均**（taps = ratio_num，梳状低通） | **无**（直接相加） |
| 输出 | `dat_inp_o`(I), `dat_qua_o`(Q) | `dat_o`（实 RF） |
| 系数缩放 | `(1-2^-F)/ratio_num`（预补偿滑动平均增益） | `1-2^-F`（仅防 ±1.0） |
| 相位偏置 | 运行时端口 `phi_offset_i` | 无对外端口 |
| 非整数比 | ratio_num/ratio_den | ratio_num/ratio_den |

两者**共用同一张 sin/cos 表构造法**（`sin(2π·i/ratio_num)·scale`）与**同一个模运算相位指针**（4.2）。差别只在「混频之后 demod 要低通、mod 不要」。

**为什么 demod 要滑动平均、mod 不要？** 这是它们对称结构里最关键的不对称点：

- **解调**把载波 `f_LO` 上的实信号乘以本振，乘积里同时含「差频（落到 DC = 基带）」与「和频（落到 2·f_LO，是镜像）」。要拿出基带 I/Q，必须**低通滤掉和频**。
- demod 巧妙地把这个低通做成长度恰为 `ratio_num` 的**滑动平均**（即 `psi_fix_mov_avg`，u4-l1）。滑动平均的零点落在 `fS/ratio_num = f_LO` 的整数倍上，正好把 2·f_LO 处的镜像压到零点——这就是组件描述（[hdl/psi_fix_demod_real2cplx.vhd:10-16](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L10-L16)）说的「comb-filter of length 1/Fcarrier，zeros at Fcarrier and Fcarrier×2」。代价是：这是个很「软」的低通，所以 demod **只适合窄带、带外噪声小**的信号。
- **调制**是反方向：把基带 I/Q 搬到载波，要的就是和频（RF），不需要滤任何东西，所以 mod 直接 `RF = I·sin(w) + Q·cos(w)` 输出。

#### 4.3.2 核心流程

**demod**（实→复）：

```
dat_i ──拆通道──► data_s ──延时对齐──► × sin[cpt] ──┐
                                         × cos[cpt] ─┤
                                                    ▼
                                    滑动平均(taps=ratio_num, gain_corr=NONE)
                                          ├──► I  (dat_inp_o)
                                          └──► Q  (dat_qua_o)
```

**mod**（复→实，5/6 级可选流水）：

```
dat_inp_i(I) ──► × sin[cpt] ──resize──► ┐
                                        ├──► add ──resize(round/sat)──► RF (dat_o)
dat_qua_i(Q) ──► × cos[cpt] ──resize──► ┘
```

#### 4.3.3 源码精读

**demod 系数格式与缩放**（[hdl/psi_fix_demod_real2cplx.vhd:53-56](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L53-L56)）：

```vhdl
constant coefUnusedBits_c : integer := log2(ratio_num_g);
constant CoefFmt_c   := (1, 0-coefUnusedBits_c, coef_bits_g + coefUnusedBits_c-1);
constant MultFmt_c   := (1, in_fmt_g.I + CoefFmt_c.I, out_fmt_g.F + log2ceil(ratio_num_g) + 2);
constant coef_scale_c := (1.0-2.0**(-real(CoefFmt_c.F)))/real(ratio_num_g);  -- 除 ratio_num 预补偿 mov_avg 增益
```

注意 `coef_scale_c` 除以 `ratio_num`：因为长度为 `ratio_num` 的滑动平均有 `ratio_num` 倍增益，预先把系数缩小 `ratio_num` 倍就抵消了，使整体增益归一。`MultFmt_c` 多留 `log2ceil(ratio_num)+2` 个小数位，把截断误差压到输出 1/4 LSB 以内（注释明说）。

**demod 滑动平均例化**（[hdl/psi_fix_demod_real2cplx.vhd:202-224](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L202-L224)）：复用 u4-l1 的 `psi_fix_mov_avg`，`taps_g => ratio_num_g`、`gain_corr_g => "NONE"`（增益已在系数里补偿），I/Q 各一路。

**mod 系数缩放（不除 ratio_num）**（[hdl/psi_fix_mod_cplx2real.vhd:51](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd#L51)）：

```vhdl
constant coef_scale_c : real := 1.0 - 1.0/2.0**(real(coef_fmt_g.F));   -- 只防 ±1.0, 不除 ratio_num
```

**mod 的 RF = I·sin + Q·cos 流水**（[hdl/psi_fix_mod_cplx2real.vhd:146-204](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd#L146-L204)）：两次 `psi_fix_mult`（trunc/wrap）→ resize 到 `int_fmt_g` → `psi_fix_add` → 末端 resize 到 `out_fmt_g`（round/sat）。中间格式由位增长规则严格推出（[hdl/psi_fix_mod_cplx2real.vhd:78-79](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd#L78-L79)）：

```vhdl
constant MultFmt_c := (1, inp_fmt_g.I + coef_fmt_g.I + 1, coef_fmt_g.F + inp_fmt_g.F);  -- 乘法: 整数位相加+1
constant AddFmt_c  := (1, int_fmt_g.I + 1, int_fmt_g.F);                                 -- 加法: 整数位+1
```

`pl_stages_g ∈ {5,6}` 控制是否插入一级额外寄存器（[hdl/psi_fix_mod_cplx2real.vhd:170-187](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd#L170-L187)）以优化时序（6 级 = 最优时序，见文档）。

**两侧位真镜像**：demod 模型把同样的 `psi_fix_mult` + `psi_fix_mov_avg` 串起来（[model/psi_fix_demod_real2cplx.py:71-76](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_demod_real2cplx.py#L71-L76)）；mod 模型把 `mult → resize → add → resize` 串起来（[model/psi_fix_mod_cplx2real.py:74-88](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mod_cplx2real.py#L74-L88)）。格式常量、round/sat 选择与 VHDL 完全对齐，是位真契约的痕迹。

#### 4.3.4 代码实践

**实践目标**：用「先 mod 再 demod」的回环，直观感受两者是可对冲的镜像操作。

**操作步骤**（源码阅读 + 本地运行型）：

1. 在 `model/` 目录写一段最小回环（**示例代码**，非项目原有）：

   ```python
   import sys; sys.path.append("model")
   import numpy as np
   from psi_fix_mod_cplx2real import psi_fix_mod_cplx2real
   from psi_fix_demod_real2cplx import psi_fix_demod_real2cplx
   from psi_fix_pkg import psi_fix_fmt_t

   ratio_num, ratio_den = 5, 1                 # 整数比
   N = 4000
   t = np.arange(N)
   I = 0.9*np.cos(2*np.pi*0.01*t)              # 慢变基带 I
   Q = 0.9*np.sin(2*np.pi*0.01*t)              # 慢变基带 Q

   mod  = psi_fix_mod_cplx2real(psi_fix_fmt_t(1,1,15), psi_fix_fmt_t(1,1,15),
                                psi_fix_fmt_t(1,1,15), psi_fix_fmt_t(1,1,15), ratio_num, ratio_den)
   rf   = mod.Process(I, Q)                    # 上变频: 复→实

   demod = psi_fix_demod_real2cplx(psi_fix_fmt_t(1,0,15), psi_fix_fmt_t(1,0,16),
                                   25, ratio_num, ratio_den)
   I2, Q2 = demod.Process(rf, np.zeros_like(rf))   # 下变频: 实→复
   ```
2. 比较 `I`/`Q`（基带原信号）与 `I2`/`Q2`（mod→demod 还原），忽略前 `ratio_num` 个暂态样本与固定增益/相位。
3. 把 `ratio_den` 改成 3（非整数比 5/3），重复步骤 2。

**需要观察的现象与预期结果**：

- 整数比下，`I2`/`Q2` 应近似还原 `I`/`Q`（受滑动平均群延迟与量化影响，可能有幅度差与延时）。
- 非整数比 5/3 下同样能还原——证明 4.2 的「同表不同步长」确实表达了正确的本振频率。
- 这一步需要 SciPy/NumPy 环境，若无则标注「待本地验证」，但「mod 与 demod 共享同表同指针、方向相反」可从源码直接读出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 mod 的 `coef_scale_c` 不像 demod 那样除以 `ratio_num`？

**答案**：mod 没有滑动平均，因此没有 `ratio_num` 倍的累加增益需要补偿；只需 `(1-2^-F)` 防止 sin/cos 表出现不可表示的 ±1.0。

**练习 2**：demod 描述里说「只适合窄带、带外噪声小的信号」，根因是什么？

**答案**：demod 的低通是长度 `ratio_num` 的滑动平均，过渡带很宽、阻带衰减有限；带外噪声或信号有显著边带时，无法被这个「软」低通充分滤除，会污染基带 I/Q。需要更强滤波时应在 demod 后再加专用滤波器（如 FIR）。

**练习 3**：mod 的 `pl_stages_g=6` 比 `pl_stages_g=5` 多了哪一级？

**答案**：多了一级在 sin/cos 系数与输入数据上的寄存器（`sin1_s/cos1_s`、`datInp1_s/datQua1_s`，见 [hdl/psi_fix_mod_cplx2real.vhd:164-168](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mod_cplx2real.vhd#L164-L168)），用于改善乘法器前的时序裕量，代价是多一拍延迟。

---

## 5. 综合实践

把本讲三块内容串起来，做一个「**DDS 当本振 → demod 解调 → 对比**」的小调研任务：

1. **选型对比**：本讲的 demod/mod 用**查表法**生成本振（`ratio_num` 点 sin/cos 表），而 u5-l2 的 `psi_fix_cordic_rot` 用 **CORDIC 迭代**生 sin/cos，u8-l3 的 `psi_fix_pol2cart_approx` 用**线性近似表**。请写一段（200 字内）比较三者在本振生成场景下的资源/精度/是否支持任意频率的取舍。提示：查表法频率只能取 \(k\cdot f_S/\text{ratio\_num}\) 的离散值；CORDIC 可任意角度但需迭代周期；线性近似精度受段数限制。

2. **非整数比追踪**：选定 `ratio_num=5, ratio_den=3`，在三个层面追踪同一非整数比：
   - **VHDL**：在 [hdl/psi_fix_demod_real2cplx.vhd:141-156](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_demod_real2cplx.vhd#L141-L156) 手算前 6 个 `cptInt`。
   - **Python 模型**：在 [model/psi_fix_demod_real2cplx.py:62-65](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_demod_real2cplx.py#L62-L65) 用同样公式算 `cpt`。
   - **回归参数**：在 [config.tcl:309-312](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L309-L312) 找到对应 `-gratio_num_g=5 -gratio_den_g=3` 那一组。
   确认三者描述的是同一个 `f_LO = (3/5)·fS`。

3. **写出位真结论**：用自己的话总结——为什么 demod 与 mod 能用「同一份 Python 模型 + 同一套指针公式」在 4 种不同 `ratio_num/ratio_den` 下都逐位通过 `###ERROR###` 比对？（关键词：同表、同指针步进、格式常量两侧对齐、整数位模式比对。）

> 若有 Modelsim/GHDL 环境，可执行 `source ./run.tcl`（或 `runGhdl.tcl`）跑 demod 与 mod 测试台，确认上述 4+5 组参数全部无 `###ERROR###`；否则标「待本地验证」。

---

## 6. 本讲小结

- **DDS = 相位累加器 + 正弦查表**：无符号相位加法天然绕圈；cos 用「相位 + 0.25 圈」复用同一张 sin 表；`FirstSplCnt` 保证首样本相位 0；TDM 用延时线给每通道各自的累加器。
- **非整数比用「同表不同步长」实现**：`ratio_num` 点的 sin/cos 表不变，指针每拍步进 `ratio_den`、模 `ratio_num` 回绕，本振频率 \(f_{LO} = \text{ratio\_den}/\text{ratio\_num} \cdot f_S\)，是类 Bresenham 的模运算技巧。
- **demod 与 mod 是镜像**：demod 实→复、混频后接滑动平均低通（梳状滤波，零点压镜像）；mod 复→实、直接 `I·sin+Q·cos` 无需滤波；两者共用同表、同指针、同位真模型。
- **系数缩放体现功能差异**：demod 的 `coef_scale` 除以 `ratio_num` 预补偿滑动平均增益；mod 不除（无平均）。
- **位真是贯穿的契约**：DDS、demod、mod 的 Python 黄金模型与 VHDL 在指针公式、格式常量、round/sat 选择上完全对齐，`config.tcl` 用参数矩阵（含非整数比 5/3、100/3）覆盖，`###ERROR###` 是唯一失败判据。
- **方法承接**：本讲的查表本振与 u5-l2 的 CORDIC、u8-l3 的线性近似形成「生 sin/cos 的三条路线」，资源/精度取舍是选型关键。

---

## 7. 下一步学习建议

- **继续单元 9**：下一讲 u9-l2 讲 `psi_fix_white_noise` / `psi_fix_noise_awgn`（确定性噪声生成）与 `psi_fix_phase_unwrap`（相位解卷绕）。其中 AWGN 同样依赖查表与定点近似，与本讲的函数近似路线（u8）一脉相承，可对照阅读。
- **回顾查表三路线**：若对「本振/三角函数怎么生成」想形成完整图谱，建议重温 u5-l2（CORDIC 旋转）与 u8-l3（pol2cart_approx 线性近似），把本讲的「ratio_num 点离散表」放进去对比。
- **深入源码**：若想看清 DDS 查表内核，可读 [hdl/psi_fix_lin_approx_sin18b_dual.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_lin_approx_sin18b_dual.vhd)（u8-l2 已讲其生成机制）；若想看清滑动平均如何被 demod 复用，重温 u4-l1 的 `psi_fix_mov_avg`。
- **回归实操**：仿照 u1-l3 的方法，在 `sim/` 下单独跑 `psi_fix_dds_18b_tb`、`psi_fix_demod_real2cplx_tb`、`psi_fix_mod_cplx2real_tb`，观察三种参数矩阵（含非整数比）的实际波形与判定输出。
