# 定点数与缩放约定

## 1. 本讲目标

OpenOFDM 是一个可综合的 FPGA 设计，整条解码链路里没有一个浮点数——所有三角函数、复数除法、星座判决都在**定点整数**上完成。本讲集中回答一个贯穿全项目的问题：

> 「这些整数到底代表什么？放大了多少倍？跨模块时小数点对齐在哪里？」

学完本讲你应该能够：

- 读懂 `common_defs.v` 里 `ATAN_LUT_LEN_SHIFT` / `ATAN_LUT_SCALE_SHIFT` / `ROTATE_LUT_SCALE_SHIFT` / `CONS_SCALE_SHIFT` 四组宏的含义与相互依赖。
- 解释 `common_params.v` 中 `PI = 1608` 这类「整数 PI」是怎么来的，并理解注释里 `3217`、`3217*2` 备选值的含义。
- 看清 `phase → rotate`（相位链）和 `equalizer → demodulate`（星座链）两条数据通路是如何通过共享的 shift 约定完成定点小数位对齐的。
- 掌握「改一个 shift 必须连锁改哪些地方」的工程约束，能预测把 `CONS_SCALE_SHIFT` 从 10 改成 11 会牵动哪些常数。

## 2. 前置知识

### 2.1 为什么 FPGA 要用定点数

软件里我们写 `float`，硬件里几乎从不这样做。原因有三：

1. **可综合性**：IEEE 754 浮点运算器体积大、时序难收敛，在 Spartan 3A-DSP 这类资源有限的 FPGA 上不划算。
2. **资源**：定点运算可以退化为「整数加减 + 移位」，直接映射到 LUT 和 DSP48A 硬核，几乎零成本。
3. **确定性**：定点没有舍入模型差异，Verilog 仿真和上板行为完全一致——这对 OpenOFDM「Python 浮点参考 ↔ Verilog 定点」的逐阶段交叉验证（见 u5-l1/u5-l2）至关重要。

### 2.2 「缩放」是什么意思

定点数用一个小技巧表示小数：**把真实值乘以一个固定的 2 的幂，存成整数**。例如想用整数表示 0.75，可以放大 \(2^2=4\) 倍存成 3；用完后再右移 2 位（除以 4）还原。

- 「放大 \(2^n\) 倍」在 Verilog 里就是左移 `<<n`。
- 「还原」就是右移 `>>n`，或者直接取数据线的高 \(n\) 段（截位）。
- 这个 \(n\) 就是本讲反复出现的 **shift**（移位数）。

数学上，一个被放大 \(2^S\) 倍的定点值 \(X_{\text{int}}\) 对应真实值：

\[
x_{\text{real}} = X_{\text{int}} \cdot 2^{-S}
\]

两个定点数要做加减，**它们的 \(S\) 必须相同**（小数点对齐）；做乘法时，结果的 \(S\) 是两个输入 \(S\) 之和，需要再右移回来。本讲的核心就是追踪 OpenOFDM 各模块的 \(S\) 是怎么约定和传递的。

> 关键术语：**shift（移位/缩放位数）**、**scale（放大倍数，等于 \(2^{\text{shift}}\)）**、**定点刻度（fixed-point format）**、**Qm.n 格式**（本讲不严格用 Q 格式，但思想一致）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `verilog/common_defs.v` | 全局缩放宏的唯一权威定义，5 行决定了全项目的定点刻度。 |
| `verilog/common_params.v` | 把数学常数（π 等）按 `ATAN_LUT_SCALE_SHIFT` 折算成定点整数。 |
| `verilog/phase.v` | 相位提取：复数 → 定点相位（atan 查表），消耗 `ATAN_LUT_*` 两组 shift。 |
| `verilog/rotate.v` | 相位旋转：按定点相位查 sin/cos 表再复数乘，消耗 `ROTATE_LUT_*` 两组 shift。 |
| `verilog/demodulate.v` | 星座判决：消费 `CONS_SCALE_SHIFT` 派生的判决门限。 |
| `verilog/equalizer.v` | 信道均衡：产生 `CONS_SCALE_SHIFT` 刻度的归一化星座点，与 demodulate 对齐。 |

辅助参考（本讲会交叉引用，但不是主角）：离线生成查找表的 `scripts/gen_atan_lut.py` 与 `scripts/gen_rot_lut.py`，它们必须和 `common_defs.v` 的 shift 保持一致（详见 u5-l4）。

---

## 4. 核心概念与源码讲解

### 4.1 定点数与缩放：OpenOFDM 的总体约定

#### 4.1.1 概念说明

OpenOFDM 没有统一的「全局定点格式」，而是**按功能分了三条相互独立的缩放链**，每条链有自己的 shift：

1. **相位链（atan / rotate）**：把弧度值 \(\theta\) 放大成整数，供相位估计与旋转校正使用。
2. **星座链（cons）**：把归一化星座点（理想幅度为 1）放大成整数，供解调判决。
3. （查找表深度本身用 `LEN_SHIFT` 表示地址位数，与上面的「值缩放」是两回事，初学最易混淆。）

这三条链各自闭环，**只在共享同一个查找表或同一根数据线时才需要对齐**。理解这一点，就不会被一堆 shift 绕晕。

#### 4.1.2 核心流程

定点缩放在项目里的工作流可以概括为四步：

1. **定义**：在 `common_defs.v` 用 `define` 给每个 shift 起名字、定数值。
2. **派生**：把数学常数（π）和判决门限用这些 shift 表达出来（`common_params.v` 的 PI、`demodulate.v` 的门限）。
3. **使用**：模块内部用 `<<` / `>>` / 位切片来「放大」「还原」数据，保持小数点对齐。
4. **同步**：离线 Python 脚本生成查找表时，必须采用与 RTL 相同的 shift，否则查表值对不上。

#### 4.1.3 源码精读

全局缩放定义集中在这 5 行（这也是本讲最核心的一段代码）：

缩放宏定义（[verilog/common_defs.v:1-9](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L1-L9)）：

```verilog
`define ATAN_LUT_LEN_SHIFT          8
// changing this requires changing PI definition in common_params.v accordingly
`define ATAN_LUT_SCALE_SHIFT        9

`define ROTATE_LUT_LEN_SHIFT        `ATAN_LUT_SCALE_SHIFT
`define ROTATE_LUT_SCALE_SHIFT      11

`define CONS_SCALE_SHIFT            10
```

逐行解读：

- `ATAN_LUT_LEN_SHIFT = 8`：atan 查找表有 \(2^8 = 256\) 个表项，地址 8 位。**这是「表有多深」，不是「值放大多少」。**
- `ATAN_LUT_SCALE_SHIFT = 9`：相位值放大 \(2^9 = 512\) 倍存放。**这才是相位链的值缩放。** 注意第 2 行注释已经警告：改它必须同步改 `common_params.v` 里的 PI。
- `ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT`：rotate 查找表的地址位数**直接等于** atan 的值缩放位数——这是两条查找表共享同一根「相位轴」的关键（见 4.4）。
- `ROTATE_LUT_SCALE_SHIFT = 11`：rotate 表里 sin/cos 值放大 \(2^{11} = 2048\) 倍。
- `CONS_SCALE_SHIFT = 10`：星座点放大 \(2^{10} = 1024\) 倍。

#### 4.1.4 代码实践

**实践目标**：建立「shift = 二进制移位位数」的直觉。

**操作步骤**：

1. 打开 `verilog/common_defs.v`，把 5 个宏的数值和它们对应的 \(2^n\) 写成一张表。
2. 用 Python（或手算）验证每个 shift 对应的放大倍数。

**预期结果**（这张表请自己填完再对照）：

| 宏 | shift n | 放大倍数 \(2^n\) | 控制的是 |
|----|---------|------------------|----------|
| `ATAN_LUT_LEN_SHIFT` | 8 | 256 | atan 表深度（地址位数） |
| `ATAN_LUT_SCALE_SHIFT` | 9 | 512 | 相位值缩放 |
| `ROTATE_LUT_LEN_SHIFT` | 9 | 512 | rotate 表地址位数 |
| `ROTATE_LUT_SCALE_SHIFT` | 11 | 2048 | sin/cos 值缩放 |
| `CONS_SCALE_SHIFT` | 10 | 1024 | 星座点缩放 |

**需要观察的现象**：注意 `ROTATE_LUT_LEN_SHIFT` 与 `ROTATE_LUT_SCALE_SHIFT` 数值不同（9 vs 11），一个是地址、一个是值——这是本讲最常见的踩坑点。

#### 4.1.5 小练习与答案

**练习 1**：为什么 OpenOFDM 不直接用 `real` 类型做相位计算？

> **答**：`real` 不可综合，且浮点运算在 Spartan 3A-DSP 上资源消耗大、时序难收敛；定点整数运算可映射到 DSP48A 与移位，资源省、仿真与上板行为一致，利于交叉验证。

**练习 2**：「放大 \(2^9\) 倍」在 Verilog 里怎么写？「再缩回原刻度」又怎么写？

> **答**：放大用 `x << 9`；缩回用 `x >> 9`，或等价地取数据线的高位段（例如从 32 位积里取 `[26:11]`，见 rotate.v）。

---

### 4.2 common_defs.v：三组缩放 shift 的精确定义

#### 4.2.1 概念说明

上一节给出了全景，本节把三组 shift 的**含义与相互依赖**讲透。三组分别是：

- **ATAN 组**：`ATAN_LUT_LEN_SHIFT`（表深）+ `ATAN_LUT_SCALE_SHIFT`（相位值缩放）。
- **ROTATE 组**：`ROTATE_LUT_LEN_SHIFT`（表地址位）+ `ROTATE_LUT_SCALE_SHIFT`（sin/cos 值缩放）。
- **CONS 组**：只有一个 `CONS_SCALE_SHIFT`（星座点缩放）。

依赖关系是本节的灵魂：

- `ROTATE_LUT_LEN_SHIFT` 被直接赋值为 `ATAN_LUT_SCALE_SHIFT`——**硬绑定**。
- 三组之间的值缩放（9 / 11 / 10）彼此**独立**，没有算术关系，可以分别调（但调任一个都要连锁修改下游，见 4.5）。

#### 4.2.2 核心流程

ATAN 组解决「如何把一个复数变成定点相位」：

\[
\theta \in [0, \pi/4) \;\longrightarrow\; \text{LUT 地址} = \lfloor 256 \cdot \tan\theta \rfloor \;\longrightarrow\; \text{表值} = \mathrm{round}(512 \cdot \theta)
\]

即「地址用 \(2^8\) 把 \(\tan\theta\) 放大、值用 \(2^9\) 把 \(\theta\) 放大」。两个 2 的幂不同，是因为地址要覆盖 \([0,1)\) 的比值范围，而值要给相位足够分辨率（见 4.1.4 注释里的「adjacent LUT values can be distinguished」）。

ROTATE 组解决「如何按定点相位查出旋转因子」：

\[
\theta \in [0, \pi/4) \;\longrightarrow\; \text{地址} = \text{定点}\theta\text{的低 9 位} \;\longrightarrow\; (\cos\theta, \sin\theta) \times 2048
\]

地址位数 = 相位定点值的位数（\(2^9\)），所以 `ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT`。

#### 4.2.3 源码精读

`phase.v` 里 ATAN 组的使用方式——注意两条 wire 的位宽分别取自两个不同的 shift：

atan 地址与表值位宽（[verilog/phase.v:34-38](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L34-L38)）：

```verilog
wire [`ATAN_LUT_LEN_SHIFT-1:0] atan_addr;      // 8 位地址
wire [`ATAN_LUT_SCALE_SHIFT-1:0] atan_data;     // 9 位表值
assign atan_addr = quotient[`ATAN_LUT_LEN_SHIFT-1:0];
wire signed [`ATAN_LUT_SCALE_SHIFT:0] _phase = {1'b0, atan_data};
```

`atan_addr` 是 8 位（256 表项），`atan_data` 是 9 位（值放大 512）。两者来自不同宏，正好对应 4.2.2 里的「地址用 \(2^8\)、值用 \(2^9\)」。

再看 rotate.v 里 ROTATE 组的使用——地址位宽与值切片来自两个不同的宏：

rotate 地址与输出切片（[verilog/rotate.v:45-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/rotate.v#L45-L48)）：

```verilog
assign out_i = p_i[`ROTATE_LUT_SCALE_SHIFT+15:`ROTATE_LUT_SCALE_SHIFT];
assign out_q = p_q[`ROTATE_LUT_SCALE_SHIFT+15:`ROTATE_LUT_SCALE_SHIFT];
assign rot_addr = actual_phase[`ROTATE_LUT_LEN_SHIFT-1:0];
```

- `rot_addr` 取定点相位的低 `ROTATE_LUT_LEN_SHIFT=9` 位 → 地址。
- `out_i` 从 32 位复数乘积 `p_i` 里取 `[26:11]` 这 16 位 → 相当于 `p_i >> 11`，正好抵消 sin/cos 表里 \(2^{11}\) 的放大，把结果缩回 16 位原刻度。

CONS 组在 demodulate.v 里的派生（详见 4.5）：

MAX 与门限派生（[verilog/demodulate.v:17-23](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L17-L23)）：

```verilog
localparam MAX = 1<<`CONS_SCALE_SHIFT;       // 1024
localparam QAM_16_DIV = MAX*2/3;             // 682
localparam QAM_64_DIV_0 = MAX*2/7;           // 292
localparam QAM_64_DIV_1 = MAX*4/7;           // 585
localparam QAM_64_DIV_2 = MAX*6/7;           // 877
```

所有判决门限都从 `MAX = 1<<CONS_SCALE_SHIFT` 派生，这意味着改 `CONS_SCALE_SHIFT` 时门限会**自动按比例缩放**——这正是 4.5 实践的切入点。

#### 4.2.4 代码实践

**实践目标**：确认离线 LUT 生成脚本与 RTL 的 shift 完全一致。

**操作步骤**：

1. 打开 `scripts/gen_atan_lut.py`，找到 `SIZE` 与 `SCALE` 两个常量。
2. 对照 `common_defs.v`，把脚本常量与 RTL 宏一一对应。

**预期结果**：

- `gen_atan_lut.py` 里 `SIZE = 2**8 = 256` ↔ `ATAN_LUT_LEN_SHIFT = 8`。
- `gen_atan_lut.py` 里 `SCALE = 512` ↔ `ATAN_LUT_SCALE_SHIFT = 9`（\(2^9=512\)）。
- `gen_rot_lut.py` 里 `ATAN_LUT_SCALE = 512` 与 `SCALE = 2048` ↔ `ROTATE_LUT_LEN_SHIFT = 9`（地址位）、`ROTATE_LUT_SCALE_SHIFT = 11`（\(2^{11}=2048\)）。

**需要观察的现象**：脚本的常数和 RTL 宏是「两套写法、同一个数」。这是 OpenOFDM 最容易踩的坑——改了一端忘了改另一端，查表值就会整体错位，且不会报错，只在交叉验证时表现为解调比特大面积错误。若本地未装 Python 2 运行环境，此步可作为「源码阅读型实践」，直接对照常数即可（待本地验证脚本可运行性）。

#### 4.2.5 小练习与答案

**练习 1**：`ATAN_LUT_LEN_SHIFT` 和 `ATAN_LUT_SCALE_SHIFT` 都是 8、9 这样接近的数，它们能合并成一个吗？

> **答**：不能。前者是「表有多少项（地址位数）」，后者是「相位值放大几倍」，是两个独立的设计自由度。合并会同时锁死表深度和相位分辨率，丧失调参空间。

**练习 2**：为什么 `ROTATE_LUT_LEN_SHIFT` 直接写成 `` `ATAN_LUT_SCALE_SHIFT ``，而不是写一个独立的 9？

> **答**：因为 rotate 表的地址就是「定点相位（已放大 \(2^9\)）」的低 9 位，二者必须共享同一根相位轴。用宏赋值而非魔法数字，是为了「改 atan 缩放时 rotate 地址位数自动跟随」，消除人为不同步。

---

### 4.3 common_params.v：PI 的整数定点表示

#### 4.3.1 概念说明

数学常数 \(\pi\) 是无理数，定点硬件里只能存它的整数近似。OpenOFDM 把 \(\pi\) 放大 \(2^{\text{ATAN\_LUT\_SCALE\_SHIFT}}\) 倍后取整：

\[
\text{PI} = \mathrm{round}\!\left(\pi \cdot 2^{9}\right) = \mathrm{round}(1608.495) = 1608
\]

于是 `PI = 1608` 这个看似奇怪的数字就出现了。一旦定下这个 PI，所有用到 \(\pi/2\)、\(\pi/4\) 的地方都用「对 PI 做移位」来派生，保证全链路同一个刻度。

#### 4.3.2 核心流程

PI 的派生关系：

\[
\text{DOUBLE\_PI} = 2\cdot\text{PI},\quad \text{PI\_2} = \lfloor \text{PI}/2 \rfloor = \text{PI} \gg 1,\quad \text{PI\_4} = \text{PI} \gg 2,\quad \text{PI\_3\_4} = \text{PI\_2} + \text{PI\_4}
\]

这些派生量在 `phase.v` 的象限还原和 `rotate.v` 的象限折叠里被反复用来把 \([0,\pi/4)\) 的查表值还原成 \([-\pi,\pi)\) 的完整相位。

「改 shift 必改 PI」的约束来自定标公式本身：若 `ATAN_LUT_SCALE_SHIFT` 改成 \(S\)，则

\[
\text{PI} = \mathrm{round}(\pi \cdot 2^{S})
\]

#### 4.3.3 源码精读

PI 及其派生（[verilog/common_params.v:1-10](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L1-L10)）：

```verilog
// localparam PI =             3217;    //  = PI*(1<<`ATAN_LUT_SCALE_SHIFT)
// localparam PI =             3217*2;  //  = PI*(1<<`ATAN_LUT_SCALE_SHIFT)
localparam PI =             1608;       //  = PI*(1<<`ATAN_LUT_SCALE_SHIFT)
localparam DOUBLE_PI =      PI<<1;
localparam PI_2 =           PI>>1;
localparam PI_4 =           PI>>2;
localparam PI_3_4 =         PI_2 + PI_4;
```

三行注释非常关键——作者把三种 `ATAN_LUT_SCALE_SHIFT` 取值下的 PI 都列了出来：

- `1608` 对应 shift = 9：\(\mathrm{round}(\pi \cdot 2^9) = \mathrm{round}(1608.495) = 1608\)（当前启用）。
- `3217` 对应 shift = 10：\(\mathrm{round}(\pi \cdot 2^{10}) = \mathrm{round}(3216.99) = 3217\)。
- `3217*2 = 6434` 对应 shift = 11：\(\mathrm{round}(\pi \cdot 2^{11}) = \mathrm{round}(6433.98) = 6434\)。

这是一份「定点 PI 换算表」的直接证据。派生量用 `>>1` / `>>2` 而不是 `/2` `/4`，是为了强调「在同一刻度下移位」，避免读者误以为又做了一次缩放。

phase.v 用这些派生量把 \([0,\pi/4)\) 的查表值 `_phase` 还原到 8 个象限（[verilog/phase.v:118-127](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L118-L127)），例如 `3'b000: phase <= _phase;`（第一象限原值）、`3'b001: phase <= PI_2 - _phase;`（映射到 \([\pi/4,\pi/2]\)）。所有加减都在 \(2^9\) 刻度下进行，无需重新对齐——这就是「全链路同一刻度」的好处。

#### 4.3.4 代码实践（本讲主实践之一）

**实践目标**：亲手推导 `PI = 1608` 与 `ATAN_LUT_SCALE_SHIFT = 9` 的关系。

**操作步骤**：

1. 用计算器或 Python 算 \(\pi \times 2^9 = \pi \times 512\)。
2. 四舍五入，应得到 1608。
3. 再算 \(\pi \times 2^{10}\) 与 \(\pi \times 2^{11}\)，验证注释里的 3217 与 6434。

**预期结果**：

\[
\pi \times 512 = 1608.495\ldots \to 1608
\]
\[
\pi \times 1024 = 3216.99\ldots \to 3217,\quad \pi \times 2048 = 6433.98\ldots \to 6434 = 3217\times 2
\]

**需要观察的现象**：`PI_2 = PI>>1 = 804`，对应真实值 \(804/512 = 1.5703\)，与 \(\pi/2 \approx 1.5708\) 仅差约 \(5\times10^{-4}\)——这是定点取整引入的微小误差，在相位估计精度内可接受。若本地能跑 `iverilog`，可在 testbench 里 `$display(PI_2)` 观察该值（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：若把 `ATAN_LUT_SCALE_SHIFT` 改成 10，`PI` 应改成多少？

> **答**：3217（正是注释里的第一个备选值）。同时 `gen_atan_lut.py` 的 `SCALE` 要从 512 改成 1024，`phase.v` 的位宽也会跟着加宽。

**练习 2**：`PI_4 = PI>>2` 与 `PI/4` 有何区别？

> **答**：数值上对正整数等价（都是除以 4 取整），但 `>>2` 在硬件上是「无代价移位」，且语义上强调「不改变刻度，只是取同一刻度下的四分之一值」，可读性更好。

---

### 4.4 缩放对齐约定（一）：phase → rotate 相位链

#### 4.4.1 概念说明

相位链是 OpenOFDM 里最精巧的定点对齐案例。它跨两个模块（`phase` 与 `rotate`）、两张查找表（`atan_lut` 与 `rot_lut`），但全程只用**同一根相位轴**：所有相位值都放大 \(2^9\) 倍。

这条链要解决两个子问题：

1. **phase 把复数变成定点相位**：输入是复数样本，输出是 \([-\pi,\pi)\) 的定点整数（刻度 \(2^9\)）。
2. **rotate 按定点相位旋转样本**：输入是定点相位 + 复数样本，查 sin/cos 表（刻度 \(2^{11}\)）做复数乘，再右移 11 位还原。

#### 4.4.2 核心流程

**phase 侧**（复数 → 定点相位）：

\[
\frac{\min(|I|,|Q|)}{\max(|I|,|Q|)} = \tan\theta,\quad \theta \in [0,\pi/4]
\]

为得到 LUT 地址，需要 \(256\cdot\tan\theta\)。OpenOFDM 的做法是：把 `divisor` 设成 `max >> 8`，于是商 = \(\min/(\max/256) = 256\cdot\tan\theta\)，**直接就是 8 位地址**。查表得到 \(512\cdot\theta\) 的 9 位定点值，再按象限还原成 \([-\pi,\pi)\)。

**rotate 侧**（定点相位 → 旋转样本）：

\[
(I + jQ)\cdot(\cos\theta + j\sin\theta)
\]

其中 \((\cos\theta,\sin\theta)\) 从 `rot_lut` 取，表里存的是 \((2048\cos\theta,\;2048\sin\theta)\)。复数乘后积被放大了 \(2^{11}\)，所以输出取 `p_i[26:11]`（即 `>>11`）还原成 16 位。

**对齐关键**：phase 输出的相位是 \(2^9\) 刻度，rotate 用它的低 9 位当地址（`ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT = 9`），于是两张表共享同一根 \(\theta\) 轴。

#### 4.4.3 源码精读

phase.v 里用「除法把比值变成地址」的精巧一行（[verilog/phase.v:64-75](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L64-L75)）：

```verilog
divider div_inst (
    ...
    .dividend(min),
    .divisor({{(`ATAN_LUT_LEN_SHIFT-8){1'b0}}, max[31:`ATAN_LUT_LEN_SHIFT]}),
    ...
    .quotient(quotient),
    ...
);
```

`ATAN_LUT_LEN_SHIFT=8` 时，`divisor = max[31:8] = max/256`，所以 `quotient = min/(max/256) = 256·tanθ`，正好落入 `[0,256)` 的地址区间。这里把 `ATAN_LUT_LEN_SHIFT` 同时用于「地址位宽」和「除法右移量」，是一处很经济的复用。

rotate.v 里按相位折叠到 \([0,\pi/4]\) 再查表（[verilog/rotate.v:107-119](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/rotate.v#L107-L119)）：

```verilog
if (phase_abs <= PI_4) begin
    quadrant <= {phase_delayed[31], 2'b00};
    actual_phase <= phase_abs;
end else if (phase_abs <= PI_2) begin
    ...
```

`actual_phase` 是 \([0,\pi/4]\) 区间、\(2^9\) 刻度的相位，最大约 \(\mathrm{round}(\pi/4\cdot512)=402\)。它被当作地址送进 `rot_lut`（深度 512，见 u5-l4），取出的就是 \((2048\cos\theta,2048\sin\theta)\)。

#### 4.4.4 代码实践

**实践目标**：追踪一次「复数 → 定点相位 → 旋转」的全链路刻度。

**操作步骤**：

1. 假设输入复数 \(I=Q=1000\)（45° 边界，\(\theta=\pi/4\)）。
2. 推算 phase 输出：\(\tan\theta=1\)，地址 = \(256\cdot1=256\)（实际表只到 255，边界近似），表值 ≈ \(\mathrm{round}(\pi/4\cdot512)=402\)，经象限还原后 `phase ≈ 402`（\(\pi/4\) 的定点值）。
3. 推算 rotate：地址 402，查表得 \((2048\cos(\pi/4),2048\sin(\pi/4)) \approx (1448,1448)\)，复数乘后右移 11 位还原。

**预期结果**：phase 与 rotate 之间的「数据线」上流动的是 \(2^9\) 刻度的相位整数；rotate 内部的 sin/cos 是 \(2^{11}\) 刻度，乘完即右移 11 位抵消。两套刻度在 rotate 模块内部完成转换，对外只暴露 \(2^9\) 的相位接口。

**需要观察的现象**：phase 与 rotate 之间的接口（`phase` 信号）只认 \(2^9\) 刻度；只要这个约定不变，模块内部怎么换表都不影响对接。若无法运行仿真，可作为「源码阅读型实践」：在 `dot11.v` 里找到 `phase` 模块的 `phase_out` 连线，确认它直接接到 `sync_long`/`equalizer` 的 `rotate` 实例（详见 u2-l3、u6-l2）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 phase 的 divider 要把 divisor 设成 `max>>8` 而不是直接用 `max`？

> **答**：为了让商 \(= \min/(\max/256) = 256\cdot\tan\theta\)，直接成为 8 位 LUT 地址。若用 `max`，商是 \(\tan\theta\in[0,1]\)，整数除法会恒为 0，无法寻址。

**练习 2**：rotate 的输出为什么要取 `p_i[26:11]` 而不是 `p_i[15:0]`？

> **答**：复数乘积里混入了 sin/cos 表的 \(2^{11}\) 放大，必须右移 11 位（等价取 `[26:11]`）抵消，才能把结果缩回原样本的 16 位刻度。`[15:0]` 会保留放大因子，数值错乱。

---

### 4.5 缩放对齐约定（二）：equalizer → demodulate 星座链与「改 shift 的连锁影响」

#### 4.5.1 概念说明

星座链比相位链简单，但它是**跨模块契约**的典型：`equalizer` 产生归一化星座点，`demodulate` 消费它。两者必须对「最外层星座点幅度等于多少」达成一致——这个共识就是 `CONS_SCALE_SHIFT`。

契约内容：理想情况下，均衡后一个星座点 \(X/H\) 的幅度为 1（归一化）。equalizer 在除法前把分子左移 `CONS_SCALE_SHIFT` 位，于是输出幅度变成 \(2^{\text{CONS\_SCALE\_SHIFT}} = \text{MAX}\)。demodulate 的所有判决门限都以 MAX 为基准派生，因此两边天然对齐。

#### 4.5.2 核心流程

equalizer 的归一化（见 u3-l1）：

\[
\text{norm} = \frac{X\cdot\overline{H}}{|H|^2} \cdot 2^{\text{CONS\_SCALE\_SHIFT}}
\]

其中分子 `prod_i << CONS_SCALE_SHIFT`，分母是 \(|H|^2\)。理想点 \(X/H\) 幅度为 1，故 norm 幅度 \(= 2^{10} = 1024 = \text{MAX}\)。

demodulate 的判决（见 u3-l3）：对 BPSK/QPSK 只判符号位；对 16-QAM 每轴多一个门限 `MAX*2/3`；对 64-QAM 每轴多两个门限 `MAX*2/7`、`MAX*4/7`、`MAX*6/7`。这些比值（2/3、2/7、4/7、6/7）正是星座相邻幅度等级的中点。

**连锁修改原理**：因为门限全部从 `MAX = 1<<CONS_SCALE_SHIFT` 派生，改 shift 时门限数值会自动按比例缩放；但因为 equalizer 与 demodulate 是两个文件，必须确认**两边用的是同一个宏**（都 `include common_defs.v`），否则契约破裂。

#### 4.5.3 源码精读

equalizer 在除法前左移 `CONS_SCALE_SHIFT`（[verilog/equalizer.v:125-126](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L125-L126)）：

```verilog
wire [31:0] prod_i_scaled = prod_i<<`CONS_SCALE_SHIFT;
wire [31:0] prod_q_scaled = prod_q<<`CONS_SCALE_SHIFT;
```

这一行就是「星座链的缩放注入点」。它把归一化结果放大 \(2^{10}\)，使最外层星座点幅度 = MAX。

demodulate 的 64-QAM 判决门限（[verilog/demodulate.v:102-111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/demodulate.v#L102-L111)）：

```verilog
QAM_64: begin
    bits[0] <= ~cons_i_delayed[15];
    bits[1] <= abs_cons_i < QAM_64_DIV_1? 1: 0;
    bits[2] <= abs_cons_i > QAM_64_DIV_0 &&
        abs_cons_i < QAM_64_DIV_2? 1: 0;
    ...
end
```

`QAM_64_DIV_0/1/2` 都是 MAX 的分数，与 equalizer 注入的 \(2^{10}\) 刻度严格匹配。

#### 4.5.4 代码实践（本讲主实践之二）

**实践目标**：把 `CONS_SCALE_SHIFT` 从 10 改成 11，推导 demodulate.v 里受影响的门限常数。

**操作步骤**：

1. 当前 `CONS_SCALE_SHIFT=10`，故 `MAX=1024`。计算四个门限的当前值：
   - `QAM_16_DIV = 1024*2/3 = 2048/3 = 682`
   - `QAM_64_DIV_0 = 1024*2/7 = 2048/7 = 292`
   - `QAM_64_DIV_1 = 1024*4/7 = 4096/7 = 585`
   - `QAM_64_DIV_2 = 1024*6/7 = 6144/7 = 877`
2. 假设改成 `CONS_SCALE_SHIFT=11`，`MAX=2048`，重算四个门限。
3. 检查是否需要手改 `demodulate.v` 里的数字，以及还有哪些地方要同步。

**预期结果**（新值）：

| 常数 | 表达式 | shift=10 | shift=11 |
|------|--------|----------|----------|
| `MAX` | `1<<CONS_SCALE_SHIFT` | 1024 | 2048 |
| `QAM_16_DIV` | `MAX*2/3` | 682 | 1365 |
| `QAM_64_DIV_0` | `MAX*2/7` | 292 | 585 |
| `QAM_64_DIV_1` | `MAX*4/7` | 585 | 1170 |
| `QAM_64_DIV_2` | `MAX*6/7` | 877 | 1755 |

**关键结论**：因为 demodulate.v 的门限**全部用 `MAX*...` 表达式派生**，改 `CONS_SCALE_SHIFT` 后这些 localparam 会**自动重算**，不需要手动改任何数字。真正需要同步检查的是：

1. **equalizer.v 的 `<<CONS_SCALE_SHIFT`**（[第 125-126 行](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L125-L126)）：因为也 `include common_defs.v`，会自动跟随，契约不破。
2. **位宽余量**：`cons_i/cons_q` 是 16 位有符号，MAX 从 1024 涨到 2048 仍远小于 32767，不溢出；equalizer 输出切片 `{norm_i[31], norm_i[14:0]}` 取 15 位幅度，2048 需要 12 位，仍在 15 位内，安全。
3. **若门限是直接写死的魔法数字**（本仓库不是），则必须逐个手改——这正是 OpenOFDM 用 `MAX*...` 派生表达式的好处。

**需要观察的现象**：改完后用 `scripts/test.py`（见 u5-l2）跑交叉验证，若 demod 阶段仍全对，说明缩放契约自洽。本实践**仅做推导与说明，不要求实际修改源码**（本讲禁止改源码）；若本地验证，请在独立副本上修改并跑仿真确认（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 demodulate.v 的门限用 `MAX*2/3` 而不是直接写 `682`？

> **答**：用表达式派生可以让门限随 `CONS_SCALE_SHIFT` 自动缩放，改刻度时不用逐个手改数字，也避免「改了 MAX 忘了改门限」的不一致 bug。682 只是 shift=10 时的展开值。

**练习 2**：若只改 demodulate.v 的 `CONS_SCALE_SHIFT` 而忘了 equalizer.v，会发生什么？

> **答**：实际上两个文件都 `include common_defs.v`，所以不会出现「只改一个」的情况——这正是把宏集中到 common_defs.v 的意义。如果人为破坏这个约定（比如在 demodulate 里局部重定义），判决门限与实际星座幅度会错配一倍，16-QAM/64-QAM 会出现系统性误判，而 BPSK/QPSK（只看符号位）不受影响。

---

## 5. 综合实践：写一份 OpenOFDM 定点缩放契约说明书

把本讲四条线索串起来，完成一份「一页纸」的定点缩放契约文档（写成 Markdown 笔记即可，**不要写入仓库**）：

1. **画三张刻度表**：分别列出相位链（phase/rotate，\(2^9\) 相位 + \(2^{11}\) sin/cos）、星座链（equalizer/demodulate，\(2^{10}\)）、查找表深度（atan \(2^8\)、rotate \(2^9\)）所用的 shift 与放大倍数。
2. **追踪一条跨模块数据**：从 `sync_short` 估出的粗频偏 `phase_offset`（\(2^9\) 刻度），经 `sync_long` 的 `rotate` 旋转样本，到 `equalizer` 的导频细校再次调用 `phase`/`rotate`——标注每个环节的刻度是否一致、在哪里发生了刻度转换（答案：转换只发生在 rotate 内部的 `>>11`）。
3. **做一次「假如」推演**：假设要把相位分辨率提高一倍，即 `ATAN_LUT_SCALE_SHIFT` 从 9 改成 10，列出必须连锁修改的全部位置：
   - `common_params.v` 的 `PI`（1608 → 3217）；
   - `gen_atan_lut.py` 的 `SCALE`（512 → 1024）与 `gen_rot_lut.py` 的 `ATAN_LUT_SCALE`（512 → 1024），相应 rot_lut 深度也会变（`MAX = round(π/4·1024)=805`，`SIZE = 2^ceil(log2(805))=1024`）；
   - `ROTATE_LUT_LEN_SHIFT` 会自动变成 10（因它等于 `ATAN_LUT_SCALE_SHIFT`），于是 rot_lut 地址变 10 位，相关端口位宽（`dot11.v`、`sync_long.v`、`equalizer.v` 的 `rot_addr`）自动跟随；
   - `phase.v` 的 `atan_data` 位宽从 9 位变 10 位。
4. **验证**：用 `scripts/test.py` 跑一个 conducted 24Mbps 样本，确认改完后 SIGNAL/DEMOD/CONV 各阶段比对仍通过（待本地验证；本讲不修改源码，仅在独立副本上尝试）。

完成后，你应该能用一句话回答：**OpenOFDM 的定点刻度由 `common_defs.v` 的 5 个宏决定，相位链共享 \(2^9\) 轴、星座链共享 \(2^{10}\) 刻度，跨模块对齐靠「同名宏 + 派生表达式」自动维持。**

## 6. 本讲小结

- OpenOFDM 全程定点，没有浮点；缩放靠「整数 × \(2^{\text{shift}}\)」，还原靠右移或取高位段。
- `common_defs.v` 定义了三组共 5 个 shift 宏：ATAN 组（LEN=8/SCALE=9）、ROTATE 组（LEN=SCALE=9 绑定、SCALE=11）、CONS 组（10）。
- `common_params.v` 里 `PI=1608` 是 \(\mathrm{round}(\pi\cdot2^9)\)；改 `ATAN_LUT_SCALE_SHIFT` 必须按 \(\mathrm{round}(\pi\cdot2^S)\) 同步改 PI（注释里的 3217、6434 是 S=10、11 的备选）。
- 相位链（phase→rotate）共享 \(2^9\) 相位轴：phase 用 `max>>8` 把比值变成 8 位地址、输出 \(2^9\) 刻度相位；rotate 用其低 9 位查 \(2^{11}\) 刻度的 sin/cos 表，乘完右移 11 位还原。
- 星座链（equalizer→demodulate）共享 \(2^{10}\) 刻度：equalizer 注入 `<<CONS_SCALE_SHIFT`，demodulate 的门限全部从 `MAX=1<<CONS_SCALE_SHIFT` 派生，因此改 shift 时门限自动缩放。
- 「LEN_SHIFT（表深/地址位）」与「SCALE_SHIFT（值缩放）」是两件事，初学最易混；跨文件契约靠把宏集中到 `common_defs.v` + 用派生表达式而非魔法数字来维持。

## 7. 下一步学习建议

- **u6-l2 模块复用与资源优化**：本讲提到 `phase` 被 sync_short/equalizer 分时复用、`rot_lut` 被双口共享，下一讲专门讲这些资源共享如何省 FPGA 面积，并分析对时序的影响。
- **u5-l4 查找表生成脚本**：本讲的 `gen_atan_lut.py` / `gen_rot_lut.py` 在那一讲有完整逐行讲解，包括 `.mif`/`.coe` 双格式与 `SCALE=SIZE*2` 约束。
- **重读 u2-l3（频偏校正）与 u3-l1（信道均衡）**：带着本讲的刻度地图重读这两个模块，你会看到「相位接口 \(2^9\)」「星座接口 \(2^{10}\)」是如何在真实算法里被消费的，理解会从「知道有 shift」升级到「知道为什么是这个数」。
