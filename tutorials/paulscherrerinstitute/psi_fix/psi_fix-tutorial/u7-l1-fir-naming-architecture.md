# FIR 命名规则与架构选择

## 1. 本讲目标

psi_fix 把 FIR 滤波器做成了一个「大家族」:同一个数学定义

\[ y[n] \;=\; \sum_{k=0}^{N-1} h[k]\,x[n-k] \]

在库里却有十几个不同实体。本讲不深入任何一个 FIR 的 RTL 细节(那是 u7-l2、u7-l3 的事),而是先帮你建立**全景图**。读完本讲你应该能够:

1. 看到任意一个 `psi_fix_fir_...` 的名字,就能机械地把它拆成六个字段,说出它的抽取能力、计算结构、通道组织、系数处理方式和目标工艺。
2. 说清 **ser / par / semi** 三种计算结构在「乘法器数量」与「吞吐率」上的取舍,并能用源码里的常量算出每种结构处理一个样本需要多少个时钟周期。
3. 区分 **conf / fix** 两类系数处理:哪些参数在综合期烧死、哪些在运行时可改,以及为什么这两类需要不同的测试台。
4. 理解为什么**整族 FIR 只共享同一个 Python 位真模型**,而命名变体只描述「RTL 用什么方式算出同一个结果」。

---

## 2. 前置知识

本讲假定你已经掌握以下内容(它们在依赖讲义中讲过):

- **定点格式三元组 `[s,i,f]`** 与位增长规则(u1-l4、u2-l2)。FIR 的累加器格式就是由这些规则推导出来的:输入 `in_fmt` 与系数 `coef_fmt` 相乘,小数位相加;累加 N 个乘积后整数位要预留余量。
- **位真双模型**(u3-l1、u3-l2):每个可综合 VHDL 组件必须配套一个逐位一致的 Python 模型作为黄金参考,测试台读 preScript 生成的文本逐位比对,不一致就打印 `###ERROR###`。
- **滑动平均与差分-累加结构**(u4-l1):mov_avg 已经让你见过「延时线 + 条件累加 + 增益校正」的雏形,FIR 是它的推广(系数不再全是 1)。
- **AXI-S 握手**(u1-l4):`vld/rdy` 同拍为高才完成一次传递;部分 FIR 只有 `vld`、无 `rdy`(无反压)。

一个关键直觉先放在这里:**数学上只有一个 FIR,实现上却有多种资源-吞吐的取舍**。命名规则就是用来标注这些取舍的「速记法」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/FirNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt) | FIR 命名规则的权威定义:六字段模板与每个字段的可选取值 |
| [model/psi_fix_fir.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py) | **全族共享**的位真 Python 模型,与 RTL 实现方式无关 |
| [hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd) | 串行(serial)FIR 样例:时分复用通道、可配置系数 |
| [hdl/psi_fix_fir_dec_ser_nch_chpar_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chpar_conf.vhd) | 串行 FIR 的「并行通道」变体,用于对照 chpar 与 chtdm 的端口差异 |
| [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd) | 并行(parallel)FIR 样例:每抽头一个乘法器(DSP 链) |
| [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd) | 半并行(semi-parallel)FIR 样例:`multipliers_g` 个乘法器折中 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归脚本,记录了各 FIR 测试台的参数矩阵 |

---

## 4. 核心概念与源码讲解

### 4.1 FIR 命名规则

#### 4.1.1 概念说明

psi_fix 的 FIR 实体名字不是随便起的,而是一个**六字段模板**。FirNaming.txt 第一行就给出这个模板:

> `psi_fix_fir_<decimation>_<calculation-handling>_<channels>_<channel-handling>_<coefficient-handling>_<architecture>`

这六个字段分别回答六个正交的问题:

| 字段 | 回答的问题 | 可选取值 |
|------|-----------|---------|
| `<decimation>` | 能否做抽取(降采样)? | `dec` = 能;`-` = 不能 |
| `<calculation-handling>` | 用多少乘法器算一个输出? | `ser` / `par` / `semi` |
| `<channels>` | 支持几个通道? | `nch` = 多通道(可配);`1ch` = 单通道 |
| `<channel-handling>` | 多通道的数据怎么进? | `chpar` = 并行进;`chtdm` = 时分复用进 |
| `<coefficient-handling>` | 系数怎么给? | `conf` / `fix` / `confch` / `fixch` |
| `<architecture>` | 是否绑定某家工艺? | `x7` = Xilinx 7 系列;`-` = 厂商无关 |

为什么要把这六件事编码进名字?因为它们决定了**资源、吞吐、端口形态**这三件最重要的事,而且彼此正交(可以自由组合)。有了这套规则,你不用打开文件就能从名字判断一个 FIR 适不适合你的场景;反过来,贡献新组件时也能立刻知道自己该套哪个模板。

#### 4.1.2 核心流程

解读一个 FIR 名字,按以下顺序逐字段拆解:

```
1. 去掉固定前缀 psi_fix_fir_
2. 用下划线把剩余部分切成 token
3. 依次匹配六个字段:
   第 1 个 token → decimation    (dec 或缺省)
   第 2 个 token → calculation   (ser/par/semi)
   第 3 个 token → channels      (nch/1ch)
   第 4 个 token → channel-handling (chpar/chtdm)   [仅 nch 有]
   第 5 个 token → coefficient   (conf/fix/confch/fixch)
   第 6 个 token → architecture  (x7 或缺省)
4. 缺省字段(用 '-' 表示)在名字中直接省略不写
```

注意:`<channels>` 为 `1ch` 时,只有一个通道,自然没有「通道怎么进」的问题,所以 `<channel-handling>` 字段连同它的下划线一起省略——单通道 FIR 的名字会比多通道的短一截。

#### 4.1.3 源码精读

权威定义在 [hdl/FirNaming.txt:1-30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt)。模板在第 3 行,逐字段说明见:

- 抽取字段 [FirNaming.txt:5-7](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L5-L7):`dec` 表示抽取 FIR,省略表示不能抽取。
- 计算字段 [FirNaming.txt:9-12](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L9-L12):定义了 ser/par/semi 三档。
- 通道字段 [FirNaming.txt:14-16](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L14-L16) 与通道组织字段 [FirNaming.txt:18-20](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L18-L20):区分 `chpar`(并行)与 `chtdm`(时分复用)。
- 系数字段 [FirNaming.txt:22-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L22-L25):`conf`/`fix`/`confch`/`fixch` 四档。
- 工艺字段 [FirNaming.txt:28-30](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L28-L30):`x7` 或省略。

把规则套到库里真实存在的实体上验证(这些文件确实存在于 `hdl/` 目录):

| 实体名 | decimation | calculation | channels | channel-handling | coefficient | arch |
|--------|-----------|-------------|----------|------------------|-------------|------|
| `psi_fix_fir_dec_ser_nch_chtdm_conf` | dec | ser | nch | chtdm | conf | - |
| `psi_fix_fir_dec_ser_nch_chpar_conf` | dec | ser | nch | chpar | conf | - |
| `psi_fix_fir_par_nch_chtdm_conf` | - | par | nch | chtdm | conf | - |
| `psi_fix_fir_dec_semi_nch_chtdm_conf` | dec | semi | nch | chtdm | conf | - |

可以看到一个规律:本库目前所有通用 FIR 都是 `nch`(多通道)且省略工艺字段(厂商无关)。`chpar` 与 `chtdm` 的端口差异是肉眼可见的——下面 4.2.3 节会用端口宽度证明这一点。

> ⚠️ 例外:并非所有 FIR 都严格遵循六字段模板。[hdl/psi_fix_fir_3tap_hbw_dec2.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_3tap_hbw_dec2.vhd) 是一个**专用变体**(3 抽头半带、抽取 2),用了描述性名字 `3tap_hbw_dec2` 而非标准模板。它属于 u7-l3 的内容。遇到这类名字时,它通常意味着「为某个高频用例做了硬优化的专用件」。

#### 4.1.4 代码实践

**实践目标**:用六字段模板手工解码一个真实组件名,验证你理解了规则。

**操作步骤**:

1. 打开 [hdl/FirNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt),把模板记在旁边。
2. 取实体名 `psi_fix_fir_par_nch_chtdm_conf`(对应 [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd))。
3. 去掉前缀 `psi_fix_fir_`,得到 `par_nch_chtdm_conf`。
4. 切成 token:`par` / `nch` / `chtdm` / `conf`。

**需要观察的现象 / 预期结果**:对照模板逐字段填表——

| 字段 | 取值 | 含义 |
|------|------|------|
| decimation | (缺省) | 不能抽取 |
| calculation | `par` | 并行计算,每抽头一个乘法器 |
| channels | `nch` | 多通道 |
| channel-handling | `chtdm` | 通道时分复用进入 |
| coefficient | `conf` | 系数运行时可配置 |

注意这里**只有 4 个 token 却对应 6 个字段**:`decimation` 缺省被省略,`architecture` 缺省也被省略。这就是「缺省不写」带来的迷惑点——字段位置要靠「已知 calculation 永远在第 2 位」这类先验来对齐。打开文件头部的描述段 [psi_fix_fir_par_nch_chtdm_conf.vhd:10-14](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L10-L14),它用自然语言重述了同样的约束,可作对照。

#### 4.1.5 小练习与答案

**练习 1**:实体 `psi_fix_fir_dec_ser_nch_chtdm_conf` 中,哪个字段告诉你「系数在运行时可以通过接口改写」?

**答案**:`conf`(coefficient-handling 字段)。`conf` = coefficients can be written;`fix` = coefficients are fixed。详见 [FirNaming.txt:22-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L22-L25)。

**练习 2**:`chpar` 与 `chtdm` 都是多通道组织方式,单通道(1ch)FIR 为什么既不写 `chpar` 也不写 `chtdm`?

**答案**:单通道只有一个数据流,不存在「多通道如何排布」的问题,`<channel-handling>` 字段连同它的前导下划线一起省略,所以 1ch 组件的名字会比 nch 组件少一截。

---

### 4.2 串行 / 并行 / 半并行架构

#### 4.2.1 概念说明

`<calculation-handling>` 字段回答的核心问题是:**算出一个输出样本,需要多少个乘法器、花多少个时钟周期?** 这是最经典的「资源 ↔ 吞吐」折中。FIR 要做 N 次乘加(MAC),理论上总「乘法工作量」是固定的,差别只在于你用多少个乘法器**并行**地搬这些工作量:

- **`ser`(serial,串行)**:只用 **1 个乘法器**,把 N 个抽头一个接一个地算。资源最省,吞吐最低。
- **`par`(parallel,并行)**:**每个抽头配 1 个乘法器**,共 N 个,一个时钟就能喂完所有抽头。吞吐最高,资源最贵。
- **`semi`(semi-parallel,半并行)**:用 `multipliers_g` 个乘法器(介于 1 和 N 之间),分几轮算完。**可调的折中点**。

FirNaming.txt 把 `semi` 戏称为 "Everything between"(介于两者之间的一切),见 [FirNaming.txt:9-12](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L9-L12)。

#### 4.2.2 核心流程

设抽头数为 \(N\),半并行的乘法器数为 \(M\),则三种结构处理**一个输出样本**所需的理论周期数:

\[
\text{Cycles}_{\text{ser}} \;\approx\; N \;(\text{+ 抽取开销})
\]

\[
\text{Cycles}_{\text{semi}} \;=\; \left\lceil \frac{N}{M} \right\rceil
\]

\[
\text{Cycles}_{\text{par}} \;=\; 1 \quad(\text{每个样本恒定 } 1 \text{ 周期,延迟为流水级数})
\]

对应的吞吐(样本/时钟)与乘法器占用:

| 结构 | 乘法器数 | 周期/样本 | 吞吐 | 适用场景 |
|------|---------|----------|------|---------|
| ser | 1 | ≈ N | 最低 | 资源极紧、采样率低 |
| semi | M (可配) | ⌈N/M⌉ | 可调 | 大多数实际折中 |
| par | N | 1 | 最高 | 高采样率、资源充裕 |

关键直觉:`semi` 是一条**连续滑动条**,调整 `multipliers_g` 就能在「ser 一端」和「par 一端」之间任意滑动,直到刚好满足你的吞吐需求为止。这是它存在的根本理由。

#### 4.2.3 源码精读

**并行(par)——每抽头一个 DSP**。并行 FIR 用一条长度为 `taps_g` 的 DSP 链,每个抽头占用一个乘加单元:

```vhdl
-- hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:59-63
signal DspDataChainI : InData_a(0 to taps_g - 1);   -- 数据逐级下传
signal DspDataChainO : InData_a(0 to taps_g - 1);
signal DspAccuChain  : AccuChain_a(0 to taps_g - 1) -- 每级累加
                                 := (others => (others => '0'));
signal DspVldChain   : std_logic_vector(1 to taps_g);
```

数组下标范围 `0 to taps_g - 1` 直接证明了「乘法器数量 = 抽头数」。`DspAccuChain` 把每级的乘积边传边加,形成流水化的累加;valid 也走一条 `DspVldChain` 平移链 [psi_fix_fir_par_nch_chtdm_conf.vhd:79](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L79)。文件头部描述明确写了 "one multiplier per tap" [psi_fix_fir_par_nch_chtdm_conf.vhd:11](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L11)。

**串行(ser)——单乘法器逐拍复用**。串行 FIR 把抽头存进 RAM,用计数器 `TapCnt_1` 逐拍读出,单个乘法器轮流与每个抽头相乘累加。它的 generics 里没有 `multipliers_g`,只有 `max_taps_g`(决定 RAM 深度)[psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:32-34](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L32-L34)。文件头描述写明 "Filter is calculated serially (one tap after the other)" [psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:10-11](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L10-L11)。串行结构需要 RAM 存历史样本,且数据存储深度需为 `max_taps_g + max_ratio_g`(见注释 [psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:17](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L17) 与常量 [psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:65-69](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L65-L69))。

**半并行(semi)——可配乘法器数**。semi 引入 `multipliers_g`,并把「每轮算多少抽头」「总共几轮」全部写成由它推导的常量:

```vhdl
-- hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:70,80
constant TapsPerStage_c     : natural := integer(ceil(real(taps_g) / real(multipliers_g)));
...
constant CyclesPerCalc_c    : integer := integer(ceil(real(taps_g) / real(multipliers_g)));
```

`CyclesPerCalc_c` 正是上面公式里的 \(\lceil N/M \rceil\)——它就是 semi 处理一个样本的计算周期数,直接读自源码 [psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:80](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L80)。文件头描述 "calculated semi-parallel" [psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:10-11](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L10-L11),generics 中 `multipliers_g : natural := 4` [psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:34](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L34)。

**附带验证:`chpar` 与 `chtdm` 的端口差异**。`<channel-handling>` 字段最直接的体现就是输入端口 `dat_i` 的位宽:

- `chpar`(并行通道):`dat_i` 位宽 = `psi_fix_size(in_fmt_g) * channels_g`,即所有通道的数据**拼成一总线**同时进 [psi_fix_fir_dec_ser_nch_chpar_conf.vhd:42](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chpar_conf.vhd#L42)。
- `chtdm`(时分复用):`dat_i` 位宽 = `psi_fix_size(in_fmt_g)`,只有**一个样本**宽,通道轮流占用这根线 [psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:45](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L45)。

#### 4.2.4 代码实践

**实践目标**:用源码常量算出 semi 在一组真实参数下的「周期/样本」,体会 `multipliers_g` 的滑动条效果。

**操作步骤**:

1. 打开 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:80](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L80),记下 `CyclesPerCalc_c = ceil(taps_g / multipliers_g)`。
2. 打开回归脚本 [sim/config.tcl:229-237](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L229-L237),取出 semi 测试台的多组参数。例如其中一组是 `taps_g=48, multipliers_g=8, ratio_g=3`。
3. 代入公式计算 \(\lceil 48/8 \rceil = 6\)。
4. 再取另一组 `taps_g=160, multipliers_g=40`,计算 \(\lceil 160/40 \rceil = 4\)。

**需要观察的现象 / 预期结果**:同一份 semi RTL,只改 `multipliers_g`,处理周期就从 6 变到 4(甚至更多档)。这正是「semi 是 ser 与 par 之间的滑动条」的直接证据。

> 说明:本实践是源码阅读 + 手算型,不需要运行仿真即可得出结论。`config.tcl` 中 `clk_per_spl_g` 是**测试台**的激励节拍(每多少拍喂一个样本),不要与 DUT 内部的 `CyclesPerCalc_c` 混淆;两者关系详见 u7-l2。

#### 4.2.5 小练习与答案

**练习 1**:某应用采样率不高,FPGA 上 DSP 资源非常紧张,应该选 ser、semi 还是 par?为什么?

**答案**:选 `ser`。它只用 1 个乘法器,代价是处理一个样本约需 N 个周期,适合采样率低、资源极紧的场景。

**练习 2**:给定 `taps_g=48`、要求每 4 个时钟至少产出一个样本,`multipliers_g` 至少要配多少?这落在 ser/par 的哪一端附近?

**答案**:需要 \(\lceil 48/M \rceil \le 4\),即 \(M \ge 12\),至少配 12。48 个抽头用 12 个乘法器,处于滑条中段偏 par 一端(par 全开是 48 个,ser 是 1 个)。

---

### 4.3 系数处理(conf / fix)

#### 4.3.1 概念说明

`<coefficient-handling>` 字段回答:**滤波器系数在什么时候确定下来?** 这决定了组件有没有「系数写入接口」,也决定了测试方式不同。

四个取值 [FirNaming.txt:22-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt#L22-L25):

| 取值 | 系数何时定 | 各通道系数 | 有运行时写入接口? |
|------|-----------|-----------|------------------|
| `fix` | 综合期烧死 | 所有通道相同 | 否 |
| `conf` | 运行时可改 | 所有通道相同 | 是 |
| `fixch` | 综合期烧死 | 每通道不同 | 否 |
| `confch` | 运行时可改 | 每通道不同 | 是 |

为什么要把这件事单列一个字段?因为它直接影响:**端口数量**(conf 多出 `coef_wr_i/coef_addr_i/...` 接口)、**资源**(fix 可被综合工具优化成常数乘法器或直接吸收进 DSP)、**灵活性**(运行中换滤波器特性,如协议切换)和**测试策略**。

#### 4.3.2 核心流程

psi_fix 让 `conf` 与 `fix` **共用同一个 RTL**,通过一个 generic `use_fix_coefs_g` 在综合期二选一,避免维护两份代码:

```
综合期:
  use_fix_coefs_g = true  → 系数来源 = coefs_g (综合期常量,无写入接口逻辑)
  use_fix_coefs_g = false → 系数来源 = 系数 RAM,运行时经 coef_* 接口写入

复位期:
  无论哪种模式,先用 coefs_g 把系数 RAM 初始化(给 conf 一个上电默认值)
```

注意命名规则里名字仍叫 `..._conf`(因为实体的「能力」是可配置),而 `use_fix_coefs_g` 是综合期开关——名字描述能力,generic 选择是否启用。这套设计在三个多通道 FIR 里完全一致。

#### 4.3.3 源码精读

**`use_fix_coefs_g` 开关**。三个多通道 FIR 的 generics 都带同一个开关,例如 semi [psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:39](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L39)、ser(chtdm)[psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:37](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L37) 和 par [psi_fix_fir_par_nch_chtdm_conf.vhd:35](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L35)。

**conf 多出的系数写入接口**。以 semi 为例,实体比 fix 多出三个端口(`coef_wr_i`/`coef_addr_i`/`coef_wr_dat_i`),允许运行时逐个改写系数:

```vhdl
-- hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:52-55
coef_wr_i     : in  std_logic := '0';              -- Coefficient write enable
coef_addr_i   : in  std_logic_vector(...) := ...;  -- 要写哪个系数
coef_wr_dat_i : in  std_logic_vector(...) := ...;  -- 写入的系数值
```

**复位期初始化**。无论 `use_fix_coefs_g` 取何值,复位时都把 `coefs_g` 灌进系数寄存器/RAM,给 conf 模式一个上电默认值。par 的实现最直观:

```vhdl
-- hdl/psi_fix_fir_par_nch_chtdm_conf.vhd:89-96 (节选)
if rst_i = '1' then
  if CoefRstDone = '0' then
    CoefWe <= (others => '1');
    for i in 0 to taps_g - 1 loop
      CoefReg(i) <= psi_fix_from_real(coefs_g(i), coef_fmt_g);   -- 上电默认系数
    end loop;
  end if;
```

**系数存的载体:系数 RAM**。conf 模式的系数通常存在 `psi_fix_param_ram` 里(双口参数 RAM,见 u4-l2),A 口在配置时钟域写系数、B 口在数据时钟域读系数,实现「不停机换系数」。ser(chtdm)就显式声明了系数 RAM 深度常量 `CoefMemDepthApplied_c` [psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:69](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L69)。

**测试策略差异**。因为 conf/fix 共用 RTL,回归脚本对同一组件跑两轮:一轮固定系数(`use_fix_coefs_g=true`)、一轮运行时改写系数,分别用不同测试台覆盖。见 [sim/config.tcl:202-205](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L202-L205) 与 [sim/config.tcl:215-218](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L215-L218) 的 `_fix_coef_tb`。

#### 4.3.4 代码实践

**实践目标**:在源码里确认「conf 与 fix 共用同一份 RTL,只差一个 generic」。

**操作步骤**:

1. 打开 [hdl/psi_fix_fir_par_nch_chtdm_conf.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd)。
2. 在文件中定位 generic `use_fix_coefs_g`(第 35 行)和复位期初始化循环(第 89–96 行)。
3. 打开 [sim/config.tcl:220-227](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L220-L227),观察 par 测试台的参数矩阵里同时出现 `use_fix_coefs_g=false` 与 `use_fix_coefs_g=true`。

**需要观察的现象 / 预期结果**:你会发现同一个实体名 `psi_fix_fir_par_nch_chtdm_conf` 在回归里被两种系数模式各跑一次,证明「能力是 conf,但是否真用运行时配置由 generic 决定」。

> 说明:本实践为源码阅读型,无需运行仿真。如果想进一步确认 `coefs_g` 的定点量化,可阅读 `psi_fix_from_real` 的用法(见 u2-l1、u2-l3)。

#### 4.3.5 小练习与答案

**练习 1**:`fixch` 与 `fix` 的区别是什么?代价是什么?

**答案**:`fixch` 允许每个通道有**不同**的系数(综合期烧死),`fix` 则所有通道共用同一组系数。代价是 `fixch` 需要为每个通道独立存储一份系数,资源随通道数线性增长。

**练习 2**:为什么 conf 模式的系数通常用双口参数 RAM(见 u4-l2)而非普通寄存器数组?

**答案**:双口 RAM 允许「配置时钟域写、数据时钟域读」两个端口并发访问,从而在不停下数据流的前提下换系数;且深度大时 RAM 比寄存器数组省得多。普通寄存器数组既无法跨时钟域、又占大量逻辑资源。

---

### 4.4 全族共享的位真模型

#### 4.4.1 概念说明

讲完命名,有一个贯穿全族的结论必须点明:**不管 RTL 用 ser、par 还是 semi,整族 FIR 只共享一个 Python 位真模型 `psi_fix_fir.py`。** 命名变体描述的是「RTL 用什么方式算」,而位真模型描述的是「数学上结果应该是什么」——只要最终输出逐位一致,中间用几个乘法器、几个周期都是 RTL 的自由。

这一点是 psi_fix 位真哲学的精髓:黄金参考与实现解耦。

#### 4.4.2 核心流程

模型的位真契约如下(对应 [model/psi_fix_fir.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py)):

```
输入 inFmt、outFmt、coefFmt
  → accuFmt = (1, outFmt.I+1, inFmt.F+coefFmt.F)   # 累加器格式,保证不溢出
  → 把输入/系数量化到位真格式
  → 用 scipy.signal.lfilter 做理想卷积
  → 舍入到 roundFmt
  → 按 decimRate 抽取
  → 饱和到 outFmt
```

模型显式假设「累加器永不回绕,量化只发生在输出」,见其 docstring [psi_fix_fir.py:18-23](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py#L18-L23)。

#### 4.4.3 源码精读

累加器格式定义 [psi_fix_fir.py:40](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py#L40):

```python
self.accuFmt = psi_fix_fmt_t(1, outFmt.i + 1, inFmt.f + coefFmt.f)
```

这与 ser/par 的 RTL 常量完全一致,例如 par 的 `AccuFmt_c` [psi_fix_fir_par_nch_chtdm_conf.vhd:55](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_par_nch_chtdm_conf.vhd#L55) 和 ser 的 `AccuFmt_c` [psi_fix_fir_dec_ser_nch_chtdm_conf.vhd:73](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd#L73)——这就是位真契约在两侧对齐的痕迹。

滤波+抽取主逻辑 [psi_fix_fir.py:71-82](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_fir.py#L71-L82):

```python
res = lfilter(coefs, 1, inp)                                   # 理想卷积
resRnd = psi_fix_resize(res, self.accuFmt, self.roundFmt, psi_fix_rnd_t.round)
resDec = resRnd[::decimRate]                                   # 抽取
outp = psi_fix_resize(resDec, self.roundFmt, self.outFmt, psi_fix_rnd_t.trunc, psi_fix_sat_t.sat)
```

> 一个值得注意的细节:semi 的 RTL 累加器 `AccuFmt_c` 多预留了 `log2ceil(multipliers_g)` 个整数位 [psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:78](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L78),比模型的 `accuFmt` 更宽。这是因为 semi 把多个乘法器的部分和并行相加,RTL 谨慎地加了余量。由于模型假设累加器不溢出、最终在输出处统一量化,只要两者都不回绕,输出依然逐位一致——位真比对发生在输出,而非累加器内部。

#### 4.4.4 代码实践

**实践目标**:确认「同一个模型服务于多种 RTL」。

**操作步骤**:

1. 在 `testbench/` 下找到四个 FIR 测试台目录(`..._dec_ser_nch_chpar_conf_tb`、`..._dec_ser_nch_chtdm_conf_tb`、`..._par_nch_chtdm_conf_tb`、`..._dec_semi_nch_chtdm_conf_tb`)。
2. 各自打开其 `Scripts/preScript.py`,看它们 import 的是不是同一个 `psi_fix_fir` 类。

**需要观察的现象 / 预期结果**:四个不同架构的测试台,preScript 都从同一个 `model/psi_fix_fir.py` 实例化黄金参考。这就是「数学唯一、实现多样」在工程上的落地。具体的 preScript 文本生成机制见 u3-l2,u7-l2 会把这条协同仿真链彻底走通。

#### 4.4.5 小练习与答案

**练习**:如果某天有人为 semi 写了一个**不同**的 Python 模型(而不是复用 `psi_fix_fir.py`),会破坏什么原则?

**答案**:会破坏「黄金参考与实现解耦」的原则。数学上 semi 与 ser/par 算的是同一个卷积,理应共用同一个位真模型;为某架构单写模型不仅重复,还可能让两侧各自的「近似」相互掩盖错误。正确做法是共用模型,让架构差异只体现在 RTL。

---

## 5. 综合实践

**任务**:综合解码 `psi_fix_fir_dec_semi_nch_chtdm_conf` 这个名字,并用源码数据说明 semi 相对 ser/par 的折中点。

请按以下步骤完成并写一份简短报告(纯阅读+手算,不需要跑仿真):

1. **解码名字**:按 4.1 的六字段模板,把 `psi_fix_fir_dec_semi_nch_chtdm_conf` 拆成六行表格,逐字段写出取值与含义。特别注意 `dec` 和缺省的 `architecture` 字段。
2. **定位 generics**:打开 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:29-44](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L29-L44),列出 `channels_g`、`multipliers_g`、`ratio_g`、`taps_g`、`use_fix_coefs_g` 五个 generic 的默认值。
3. **算周期数**:用 [hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd:80](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_fir_dec_semi_nch_chtdm_conf.vhd#L80) 的 `CyclesPerCalc_c` 公式,代入默认 `taps_g=32, multipliers_g=4`,算出处理一个样本的计算周期数。
4. **对比三端**:
   - 若改用 **ser**(同 taps_g=32),周期数大约是多少?(参考 4.2.2 公式)
   - 若改用 **par**,周期数是多少?乘法器数是多少?
   - semi 默认配置落在滑条的哪个位置?
5. **解释折中点**:用自己的话写一段,说明为什么 semi 是「资源-吞吐的滑动条」,以及它在什么场景下比 ser 或 par 更合适。

**预期结果示例**(供自检):

- 名字解码:dec(可抽取)、semi(半并行)、nch(多通道)、chtdm(时分复用进)、conf(系数可配)、arch 缺省(厂商无关)。
- 默认 generic:`channels_g=4, multipliers_g=4, ratio_g=8, taps_g=32, use_fix_coefs_g=false`。
- `CyclesPerCalc_c = ⌈32/4⌉ = 8` 个周期/样本。
- ser 同条件下约 32 个周期/样本、1 个乘法器;par 约 1 周期/样本、32 个乘法器;semi 默认(4 个乘法器)处于滑条中段偏 ser 一端。

---

## 6. 本讲小结

- FIR 的实体名是**六字段模板** `psi_fix_fir_<decimation>_<calc>_<channels>_<channel-handling>_<coef>_<arch>`,缺省字段直接省略不写。FirNaming.txt 是权威定义。
- `<calculation-handling>` 的 **ser/par/semi** 是资源-吞吐的三档:ser 用 1 个乘法器(周期≈N)、par 每抽头 1 个乘法器(1 周期/样本)、semi 用 `multipliers_g` 个乘法器(周期=⌈N/M⌉,可调)。
- `<channel-handling>` 的 **chpar/chtdm** 直接体现在端口位宽:并行通道 `dat_i` 位宽乘以 `channels_g`,时分复用只有单样本宽。
- `<coefficient-handling>` 的 **conf/fix** 由 generic `use_fix_coefs_g` 在综合期二选一,共用同一份 RTL;conf 多出运行时系数写入接口,常以双口参数 RAM 为载体。
- **整族 FIR 共享同一个 Python 位真模型** `psi_fix_fir.py`;命名变体只描述「RTL 怎么算」,黄金参考与实现解耦。
- 累加器格式 `accuFmt=(1, outFmt.I+1, inFmt.F+coefFmt.F)` 在模型与 ser/par RTL 中一致,是位真契约的痕迹;semi 因并行部分和而多预留整数位,但不影响输出逐位一致。

---

## 7. 下一步学习建议

本讲建立了 FIR 全景图与命名速记法,下一讲 **u7-l2「FIR 多通道可配置滤波器」** 会把镜头推进到 RTL 内部,逐一精读 ser/par/semi 三个多通道可配置 FIR 的实现差异:

- ser 的 RAM 抽头存储与 `TapCnt` 逐拍复用机制;
- par 的 DSP 链式累加与 valid 平移链;
- semi 的 `multipliers_g` 调度、`CyclesPerCalc_c` 与系数 RAM 配置;
- 三个组件共用的 `CalcOngoing`/flushing 状态接口,以及 `duty_cycle_g`、`clk_per_spl_g`、`multipliers_g` 如何共同决定样本处理周期。

建议在进入 u7-l2 前,先重读 [hdl/FirNaming.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/FirNaming.txt) 与本讲的源码地图,带着「为什么这个名字对应这种实现」的问题去读 RTL,会事半功倍。如果对系数 RAM 的双口机制不熟,可先复习 u4-l2(param_ram)。
