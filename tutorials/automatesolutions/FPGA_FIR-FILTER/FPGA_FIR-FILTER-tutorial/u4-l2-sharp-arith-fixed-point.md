# 滤波运算 sharp_arith 的定点乘加与饱和截断

## 1. 本讲目标

本讲深入到 `sharp_slice` 内部最关键的运算核 `sharp_arith`。这是把「一维 7 抽头 FIR」真正算出来的地方：读入 7 个抽头，做一次定点乘加，再把结果四舍五入、限幅成一个 0~255 的像素值送出。

学完本讲你应该能够：

- 看懂 `sharp_arith.vhd` 里那行定点乘加表达式 `sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;`，并能把每一项对应回 u2-l2 设计的系数 `[1,0,-9,48,-9,0,1]/32`。
- 说清为什么要在除以 32 之前先 `+16`，也就是「定点四舍五入」的原理。
- 掌握把运算结果饱和截断（clamp）到 0~255 的限幅逻辑，理解为什么锐化运算会算出负数或超过 255 的值。

本讲只讲「一个像素的一维卷积怎么算」，不涉及抽头从哪里来（那是 u3-l3 的数据流和 u4-l1 的行存储）。

## 2. 前置知识

在进入源码前，先确认几个基础概念。

**定点数与浮点数。** Octave 里 `fir1` 设计出来的系数是小数（如 `-0.2719`），FPGA 直接处理小数代价大。定点化的思路是：把所有系数同乘一个 2 的幂（本项目乘 32），变成整数运算；最后再把累加结果除以同一个 2 的幂「缩回来」。本项目里除以 32 就是右移 5 位，硬件几乎不花钱。这一点在 u2-l2 已经讲过。

**VHDL 的整数除法。** VHDL 里 `integer / integer` 是**向零截断**（truncate toward zero），不是四舍五入。例如 `1000/32 = 31`、`1020/32 = 31`、`-17/32 = 0`。这直接决定了我们为什么需要 `+16` 来做四舍五入（见 4.2）。

**为什么是 0~255。** 每个颜色通道是一个 8 位像素，取值范围 `0..255`。任何超出这个范围的运算结果都无法直接表示，必须限幅——否则仿真会报 range violation，综合后的行为也不可预期。

**复用一句口诀。** 锐化核系数和为 32、除以 32 后直流增益为 1：

\[
G(0)=\frac{1+0-9+48-9+0+1}{32}=\frac{32}{32}=1
\]

这意味着对一块亮度恒定的平坦区域，输出恒等于输入（不改平均亮度）；变化只发生在有边缘的地方。这一点会在后面的例子里反复印证。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sharp_arith.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd) | 本讲主角：7 抽头 FIR 的定点乘加 + 四舍五入 + 饱和截断，是 `sharp_slice` 里被例化两次（垂直、水平）的运算核。 |
| [sharp_filter_coefficients.m](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m) | 系数的「出生证明」：用 `fir1` 设计高通核、再 `round(32*...)` 定点化，得到硬件里硬编码的整数系数。 |
| [sharp_slice.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd) | 上下文：展示 `sharp_arith` 如何被例化、7 个抽头怎么连进去（u3-l3 已详述，本讲只借一眼）。 |
| [sharp_image_filter.m](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_image_filter.m) | 软件参考：`imfilter` 用同一组系数做卷积，是硬件应逼近的「标准答案」。 |

## 4. 核心概念与源码讲解

先对 `sharp_arith` 建立一个整体印象。它的实体很简单：一个时钟、一个复位、7 个抽头输入、1 个像素输出。

```vhdl
entity sharp_arith is
  port ( clk       : in  std_logic;
         reset     : in  std_logic;
         tap_m3    : in  integer range 0 to 255;
         tap_m2    : in  integer range 0 to 255;
         tap_m1    : in  integer range 0 to 255;
         tap_00    : in  integer range 0 to 255;
         tap_p1    : in  integer range 0 to 255;
         tap_p2    : in  integer range 0 to 255;
         tap_p3    : in  integer range 0 to 255;
         data_out  : out integer range 0 to 255);
end sharp_arith;
```

实体见 [sharp_arith.vhd:L10-L21](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L10-L21)：7 个抽头按相对于窗口中心的位置命名，`m` 表示 minus（中心之前）、`p` 表示 plus（中心之后）、`00` 是中心抽头；`data_out` 被声明为 `integer range 0 to 255`，这一句本身就要求我们**必须**把结果限幅到 8 位范围，否则仿真时会触发范围违例。

> 小提示：`reset` 出现在端口里，但在本实体的 `behave` 架构中并未使用——运算进程只挂在时钟上（见 [sharp_arith.vhd:L29-L45](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L29-L45)）。`reset` 主要是为了和 `sharp_slice` 的例化接口保持一致而保留的。

下面按三个最小模块逐一拆解。

---

### 4.1 定点乘加运算

#### 4.1.1 概念说明

FIR 滤波的本质是「用一组固定系数对邻域像素做加权求和」。7 抽头意味着用 7 个相邻像素，每个乘一个系数后相加。定点 FIR 与浮点 FIR 的唯一区别是：系数预先放大成整数（本项目乘 32），累加完再统一除以 32 缩回。这样做的好处是整条运算链都用整数算术，硬件实现简单、可复现。

`sharp_arith` 就是这个「加权求和」的硬件实现。它在 `sharp_slice` 里被例化两次：先对 7 个垂直抽头做一次（垂直滤波），再对 7 个水平抽头做一次（水平滤波），两次串联等价于一次 7×7 二维卷积——这就是 u2-l1/u3-l3 讲过的可分离滤波。

#### 4.1.2 核心流程

给定 7 个抽头 `tap_m3, tap_m2, tap_m1, tap_00, tap_p1, tap_p2, tap_p3` 和系数 `[1,0,-9,48,-9,0,1]/32`，理想卷积为：

\[
y=\frac{1\cdot t_{m3}+0\cdot t_{m2}-9\cdot t_{m1}+48\cdot t_{00}-9\cdot t_{p1}+0\cdot t_{p2}+1\cdot t_{p3}}{32}
\]

两个关键观察：

1. **`tap_m2`、`tap_p2` 的系数为 0**，乘出来恒为 0，所以表达式里直接把它们省略。这正是 u2-l2 里 `fir1` 在 ±2 处 sinc 天然过零带来的「免费简化」——硬件少做两次乘法，但端口上仍然保留这两个输入（接进来不用），保持实体通用。
2. **累加用全精度整数先算，最后才除以 32**。中间不逐步舍入，避免多次量化误差累积，只在最后做一次四舍五入（见 4.2）。这是定点信号处理的正确做法：**accumulate first, scale last**。

把省略 0 系数的项展开后，硬件里实际计算的就是（注意这里先不写 `+16`，留到 4.2）：

\[
N = t_{m3}-9\cdot t_{m1}+48\cdot t_{00}-9\cdot t_{p1}+t_{p3}
\]

最终输出由 \( N \) 除以 32 得到。

#### 4.1.3 源码精读

源码里有一行注释直接标注了系数来源：

```vhdl
-- filter coefficients [1;0;-9;48;-9;0;1]/32
```

见 [sharp_arith.vhd:L27](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L27)——这一行就是 u2-l2 用 `round(32*fir1(8,0.5,"high"))` 算出来、再叠加恒等核得到的整数系数，软硬件在此对齐。

核心的乘加表达式在第 34 行（暂时先把 `+16` 当作「待讲」）：

```vhdl
sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;
```

见 [sharp_arith.vhd:L34](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34)。逐项对应：

| 表达式片段 | 对应系数 | 对应抽头 |
| --- | --- | --- |
| `tap_m3` | +1 | `tap_m3` |
| （省略） | 0 | `tap_m2` |
| `- (9*tap_m1)` | -9 | `tap_m1` |
| `+ (48*tap_00)` | +48 | `tap_00`（中心） |
| `- (9*tap_p1)` | -9 | `tap_p1` |
| （省略） | 0 | `tap_p2` |
| `+ tap_p3` | +1 | `tap_p3` |

可以看到：7 抽头里实际只出现 5 个，对应 5 次乘法（其中 `*1` 还可被综合工具优化成连线）；`tap_m2`/`tap_p2` 虽接进端口却不参与运算。这与 u3-l3 里 `sharp_slice` 把 `v_tap(1)`/`v_tap(5)` 接到 `tap_m2`/`tap_p2` 是一致的——接进来是为了实体通用，不用是为了省硬件。

系数的「出生证明」在 Octave 脚本里：

```matlab
fir1(8,0.5, "high")
round(32*fir1(8,0.5, "high"))
```

见 [sharp_filter_coefficients.m:L11-L13](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m#L11-L13)：第一行打印浮点高通核，第二行打印乘 32 四舍五入后的整数核。u2-l2 已详细推导：高通核中心约为 16，叠加恒等核（+32）得到锐化核的 48，最终量化成 `[1,0,-9,48,-9,0,1]`。

#### 4.1.4 代码实践（手算验证 DC 增益）

**实践目标：** 用一组恒定输入验证「平坦区域输出＝输入」，亲手确认直流增益为 1。

**操作步骤：**

1. 取 7 个抽头全部等于 100（模拟一块亮度均匀的平坦区域）。
2. 代入表达式分子（含 `+16`）：\( 100 - 9\cdot100 + 48\cdot100 - 9\cdot100 + 100 + 16 = 3216 \)。
3. 除以 32：\( 3216 / 32 = 100.5 \)，VHDL 向零截断得 `100`。

**需要观察的现象：** 输入是 100，输出也是 100——平坦区域亮度不变。

**预期结果：** 输出 `data_out = 100`，等于输入。这印证了「系数和为 32、直流增益为 1」：对任何恒定输入 \( c \)，分子都是 \( 32c + 16 \)，除以 32 后得到 \( c \)（`.5` 被截断）。这也是为什么这个滤波器只增强边缘、不改整体亮度。

**待本地验证：** 你也可以在仿真器里给 `sharp_arith` 喂一组恒定输入，观察 `data_out` 是否等于输入。

#### 4.1.5 小练习与答案

**练习 1.** 如果把中心系数从 48 改成 32（其它不变），平坦区域恒定输入 100 时输出会变成多少？直流增益变成多少？

**参考答案：** 分子变为 \( 100 - 900 + 32\cdot100 - 900 + 100 + 16 = 2616 \)，\( 2616/32 = 81.75 \to 81 \)。直流增益 \( = (1+0-9+32-9+0+1)/32 = 16/32 = 0.5 \)，所以平坦区域会被整体压暗到约一半。

**练习 2.** 为什么端口里有 `tap_m2`/`tap_p2`，表达式里却不出现？

**参考答案：** 因为它们的系数为 0，乘出来恒为 0，省略可减少硬件乘法。保留端口是为了让实体通用、便于将来改成系数非零的滤波器（见第 5 节综合实践和 u6-l3）。

---

### 4.2 四舍五入（+16）

#### 4.2.1 概念说明

除以 32 在硬件里是「右移 5 位」，本质是向零截断，会系统性地把结果往小里取。对正值来说，截断等价于向下取整（floor），平均会偏低约半个最低位（0.5 LSB）。直接截断会让整张图都偏暗一点点。

解决办法是经典的「定点四舍五入」：**在除以 32 之前先加 16**（即除数的一半）。这样截断就等价于四舍五入，消除了系统性偏置。

#### 4.2.2 核心流程

设未舍入的分子为 \( N \)。直接除：

\[
\text{trunc}(N/32)
\]

是向零截断。加 16 再除：

\[
\text{trunc}\!\left(\frac{N+16}{32}\right) \approx \text{round}(N/32)
\]

对 \( N \ge 0 \) 这就是标准的「四舍五入」：

\[
\text{round}(N/32) = \left\lfloor \frac{N+16}{32} \right\rfloor
\]

原理很简单：\( 16 = 32/2 \)，加上「半除数」就把分界点从「0」挪到了「半 LSB」，于是 \( N \bmod 32 \ge 16 \) 时进位、否则舍去。

举几个数（分子 \( N \) 指去掉 `+16` 后的累加值）：

| 分子 N | N/32 真值 | 不加 16（截断） | 加 16 后 | 是否进位 |
| --- | --- | --- | --- | --- |
| 15 | 0.47 | 0 | (15+16)/32=0 | 否（<16） |
| 17 | 0.53 | 0 | (17+16)/32=1 | 是（≥16） |
| 32 | 1.00 | 1 | (32+16)/32=1 | —（恰好整除） |
| 50 | 1.56 | 1 | (50+16)/32=2 | 是 |

可以看到：大约一半的像素（\( N \bmod 32 \ge 16 \)）会因为 `+16` 而「亮 1 个 LSB」。整图平均因此比纯截断亮约 0.5 LSB，更贴近真实值。

> 对负数的细微不对称：VHDL 向零截断，所以对负分子加 16 后偏向「向零取整」而非严格的四舍五入（例如 \( N=-17 \)：\( (-17+16)/32 = 0 \)，而最近整数其实是 -1）。这是一种可接受的简化，对成片亮度的影响极小。

#### 4.2.3 源码精读

`+16` 就嵌在那行表达式里：

```vhdl
sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;
```

见 [sharp_arith.vhd:L34](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34) 中的 `+ 16`。注意它的位置：在括号内、除以 32 之前——这正是「先加半除数、再截断」的标准写法。括号外的 `/ 32` 是最后一步缩放，二者一起构成「缩放 + 舍入」。

顺带看一眼承载结果的变量声明：

```vhdl
variable sum     	    		 : integer range -512 to 511;
```

见 [sharp_arith.vhd:L30](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L30)：`sum` 存的是**除以 32 之后**的结果，范围 `-512..511` 是一个干净的 10 位有符号范围。把 4.1.2 里的两个极端代入（抽头取 0 或 255 的最不利组合），实际极值约为 \( +398 \)（中心及两端全亮、负系数位全黑）和 \( -142 \)（中心及两端全黑、负系数位全亮），都舒适地落在 `[-512, 511]` 之内并留有裕量。注意：括号里的整数累加用全精度 32 位 `integer` 算，不存在中间溢出；`range` 只约束最终赋给 `sum` 的那个值。

#### 4.2.4 代码实践（手算对比截断 vs 舍入）

**实践目标：** 找一组真实输入，亲手比较「有 `+16`」和「无 `+16`」的差异。

**操作步骤：**

1. 取抽头 `tap_m3=10, tap_m1=30, tap_00=40, tap_p1=50, tap_p3=70`（`tap_m2/tap_p2` 任意，不参与）。
2. 算分子（不含 `+16`）：\( N = 10 - 9\cdot30 + 48\cdot40 - 9\cdot50 + 70 = 1280 \)。
3. 无 `+16`：\( 1280/32 = 40 \)（恰好整除，两者相同）。
4. 改一个让 \( N \bmod 32 \ne 0 \) 的输入，例如把 `tap_p3` 从 70 改成 88，则 \( N = 1298 \)。
   - 无 `+16`：\( 1298/32 = 40 \)（截断，真值 40.56）。
   - 有 `+16`：\( (1298+16)/32 = 44.0 \to 44 \)？待你手算确认——重点观察 \( N \bmod 32 \) 是否 ≥ 16。

**需要观察的现象：** 当 \( N \bmod 32 \ge 16 \) 时，有 `+16` 的结果比无 `+16` 大 1；当 \( N \bmod 32 < 16 \) 时，两者相同。

**预期结果：** `+16` 把截断变成了四舍五入，平均让结果偏亮约 0.5 LSB，更接近真实卷积值。

**待本地验证：** 上面第 4 步的算术请自行复核。

#### 4.2.5 小练习与答案

**练习 1.** 如果除数改成 64（系数放大 64 倍定点化），「四舍五入」应该加多少？

**参考答案：** 加 32，即除数的一半（\( 64/2 \)）。规律是：除以 \( 2^k \) 时，加 \( 2^{k-1} \) 实现四舍五入。

**练习 2.** 为什么不直接用 `round()` 函数，而要手写 `+16` 再除？

**参考答案：** 硬件实现里，`+16` 是一次普通加法、`/32` 是右移 5 位，都是廉价操作；而调用浮点 `round` 在 FPGA 上代价高昂。定点信号处理惯用「加半除数再截断」这一位运算友好的等价写法。

---

### 4.3 饱和截断限幅

#### 4.3.1 概念说明

乘加和舍入之后，`sum` 可能落在 `[-142, 398]` 区间，但像素必须是 `0..255`。两个方向都会越界：

- **正向越界（过冲）：** 在暗→亮的强边缘处，中心像素很亮、邻居很暗时，`48*亮 - 9*暗` 可以把结果顶到 255 以上。锐化本质是放大高频，边缘过冲是正常现象。
- **负向越界（振铃）：** 在亮→暗的边缘处，`-9*亮` 可能把结果拉到负值。

如果不处理：一方面 `data_out` 被声明为 `integer range 0 to 255`，赋一个越界值会让仿真直接报错；另一方面综合后的硬件会环绕（wrap），把 256 显示成 0、把 -1 显示成 255，画面出现奇怪的色块。**饱和截断（saturation / clamp）** 就是把越界值「压」回边界：超过 255 的按 255 处理、低于 0 的按 0 处理。

#### 4.3.2 核心流程

限幅逻辑就是一个三分支判断：

```
若 sum > 255 ：输出 255        （正向饱和）
若 sum < 0   ：输出 0          （负向饱和）
否则         ：输出 sum        （落在范围内，原样输出）
```

它保证 `data_out` 永远在 `0..255`，既满足端口范围约束，又避免环绕带来的视觉伪影。注意它只发生在 `sum` 已经算完、舍入完之后——是流水线的最后一步「安全网」。

#### 4.3.3 源码精读

限幅紧跟在乘加之后：

```vhdl
-- limit to range 0 to 255
if ( sum > 255 ) then
  data_out <= 255;
elsif ( sum < 0 ) then
  data_out <= 0;
else
  data_out <= sum;
end if;
```

见 [sharp_arith.vhd:L37-L43](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L37-L43)。注释 `limit to range 0 to 255` 点明了意图。把它和 4.1.3 的乘加、4.2.3 的 `+16` 串起来看，整个 `sharp_arith` 的运算进程就是「三步走」：

```vhdl
process
  variable sum : integer range -512 to 511;
begin
  wait until rising_edge(clk);
  sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;  -- ①乘加 ②舍入
  -- ③ 限幅
  if ( sum > 255 ) then ... elsif ( sum < 0 ) then ... else ... end if;
end process;
```

完整进程见 [sharp_arith.vhd:L29-L45](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L29-L45)。两点值得注意：

- 进程用 `wait until rising_edge(clk)` 而非敏感信号列表，是一个**时钟进程**，输出 `data_out` 被寄存一拍。所以每例化一个 `sharp_arith` 就引入 1 拍流水线延迟——这正是 u3-l2 里 `sharp_slice` 通路延迟（顶层 `delay=6`）的来源之一。
- `sum` 是 `variable`（变量），在进程内立即更新；`data_out` 是端口信号，`<=` 在时钟沿生效。变量先算出舍入结果、再由 `if` 决定写什么进 `data_out`，时序一拍内完成。

#### 4.3.4 代码实践（手算过冲与振铃）

**实践目标：** 用两组边缘输入，亲手看到「为什么会越界、限幅如何兜底」。

**操作步骤：**

1. **过冲场景**（暗→亮强边缘）：取 `tap_m3=0, tap_m1=0, tap_00=255, tap_p1=255, tap_p3=255`。
   分子 \( = 0 - 0 + 48\cdot255 - 9\cdot255 + 255 + 16 = 10216 \)，\( 10216/32 = 319.25 \to 319 \)。
   `sum = 319 > 255`，限幅后 `data_out = 255`。
2. **振铃场景**（亮→暗强边缘）：取 `tap_m3=255, tap_m1=255, tap_00=0, tap_p1=0, tap_p3=0`。
   分子 \( = 255 - 9\cdot255 + 0 - 0 + 0 + 16 = -2024 \)，\( -2024/32 = -63.25 \to -63 \)（向零截断）。
   `sum = -63 < 0`，限幅后 `data_out = 0`。

**需要观察的现象：** 两个场景的 `sum` 都越过了 `0..255`，但限幅把它们分别钉死在 255 和 0。

**预期结果：** 过冲输出 255（强边缘的亮侧更亮但不超过白）、振铃输出 0（暗侧压到黑）。如果没有限幅，`data_out` 端口范围约束会让仿真报错，或综合后出现环绕伪影。

**待本地验证：** 可在仿真器里构造这两个抽头组合，观察 `sum` 与 `data_out`。

#### 4.3.5 小练习与答案

**练习 1.** 如果把限幅完全删掉、直接 `data_out <= sum;`，仿真会发生什么？

**参考答案：** `data_out` 是 `integer range 0 to 255`，当 `sum` 为 319 或 -63 时赋值会触发仿真器的范围违例（range violation），通常报错并停止。即使强行综合，硬件也会环绕，画面出现错误的亮度跳变。

**练习 2.** 把正向饱和上限从 255 调到 200，平坦区域（全 100 输入）的输出会变吗？

**参考答案：** 不会。平坦区域 `sum = 100`，落在 `0..200` 内，限幅不触发，仍是 100。只有 `sum > 200` 的亮区/边缘过冲才会被压到 200。

---

## 5. 综合实践

本练习把三个模块串起来，亲手感受「舍入」和「限幅」对成片的影响。需要用仿真跑出 PPM 图像，仿真流程详见 u5-l1（图像测试台 `sim_sharp.vhd`）和 u5-l2（自校验测试台 `sim_sharp_self-checking.vhd`）。

> 注意：以下实验要求你**临时修改源码**做对比学习。请先用 `git stash` 或复制备份 `sharp_arith.vhd`，做完后恢复，避免污染工程。

**实验背景：** 用 [Verification/sim_sharp.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd) 或 [sim_sharp_self-checking.vhd](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd) 作为测试台，对一张测试图跑出锐化结果 PPM（注意 `sim_sharp.vhd` 里有硬编码路径，需按 u5-l1 改成本地路径）。

**实验 (a)：去掉 `+16`（验证四舍五入）**

1. 把第 34 行改成 `sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3) / 32;`。
2. 重新仿真，对比输出 PPM 与原始（含 `+16`）版本。
3. **预期现象：** 视觉上几乎看不出差别（差异只有 1 个 LSB）。但若用自校验测试台，会报出大量「1 LSB」级别的 mismatch；若统计输出图平均亮度，会比含 `+16` 版本略低约 0.5 LSB。这印证了 4.2 的结论：`+16` 把截断变成四舍五入，去除系统性偏暗。

**实验 (b)：把饱和上限改成 200（验证限幅）**

1. 把第 38 行 `data_out <= 255;` 改成 `data_out <= 200;`（只改正向饱和上限）。
2. 重新仿真，对比输出 PPM。
3. **预期现象：** 这次差异**很明显**——所有原本被锐化顶到 200 以上的亮区和强边缘过冲，全被压平在 200，高光区域明显变暗、细节丢失。自校验测试台会在亮区报出大量、大幅度的 mismatch。这印证了 4.3 的结论：限幅不是可有可无的装饰，它直接决定高光区的表现；上限设得越低，过冲被削得越狠。

**对比结论：** 舍入（`+16`）影响的是「平均偏半个 LSB」的精度细节，肉眼难辨但可量化；限幅（饱和上限）影响的是「过冲能亮到什么程度」的可见表现，改动立竿见影。两者都是定点像素运算不可或缺的收尾步骤。

> 待本地验证：上述两个实验的精确 mismatch 数量和直方图变化取决于你用的测试图，请以本地仿真结果为准。

## 6. 本讲小结

- `sharp_arith` 用一行表达式完成 7 抽头 FIR 的定点乘加：`sum := (tap_m3 - 9*tap_m1 + 48*tap_00 - 9*tap_p1 + tap_p3 + 16) / 32`，系数 `[1,0,-9,48,-9,0,1]/32` 来自 u2-l2 的 `round(32*fir1(...))`。
- `tap_m2`/`tap_p2` 系数为 0，端口保留但不参与运算，省下两次乘法——这是 sinc 在 ±2 过零带来的「免费简化」。
- 累加用全精度整数先算、最后才除以 32（accumulate first, scale last），避免中间量化误差累积。
- `+16` 是除数的一半，把「向零截断」变成「四舍五入」，消除约 0.5 LSB 的系统性偏暗。
- `sum` 被声明为 `integer range -512 to 511`，舒适覆盖实际极值约 `[-142, 398]` 并留有裕量。
- 锐化的负系数会在边缘处产生过冲（>255）和振铃（<0），`if/elsif/else` 把它们饱和截断到 `0..255`，既满足 `data_out` 的范围约束，又避免环绕伪影。整个运算挂在时钟上，每个 `sharp_arith` 引入 1 拍流水线延迟。

## 7. 下一步学习建议

- **进入验证单元（U5）：** 现在你已经懂得硬件在算什么，接下来学 u5-l1（PPM 图像测试台）和 u5-l2（自校验逐像素比对），亲手跑通本讲「综合实践」里的两个实验，用 mismatch 计数量化舍入与限幅的影响。
- **回顾数据流：** 若对「7 个抽头怎么凑出来」还不够清晰，回到 u3-l3（`sharp_slice` 的垂直/水平抽头链）和 u4-l1（`sharp_linemem` 行存储）。
- **向二次开发延伸：** 学完 U5 后看 u6-l3，那里会演示如何修改 `sharp_arith` 的系数与 `sharp_slice` 的抽头结构，把锐化核改成平滑核或边缘检测核——本讲的乘加表达式和限幅逻辑正是改动的核心落点。
