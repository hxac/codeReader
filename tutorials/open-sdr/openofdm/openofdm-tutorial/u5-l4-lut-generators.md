# 查找表生成脚本

## 1. 本讲目标

OpenOFDM 是用 Verilog 写的硬件解码器，但解码链路里有不少「算一次就再也不变」的量——某个角度的正切、某个相位的正余弦、某种速率下的比特交织逆映射。把这些量提前算好、烧进一块只读存储器（ROM）里，运行时只查表，是 FPGA 设计中省乘法器、省时钟周期的标准做法。这些 ROM 的内容不是手写的，而是由三个 Python 脚本离线生成。

本讲要解决的问题是：**这些查找表（LUT，Look-Up Table）是怎么生成的？脚本里的常数和 Verilog 里的定点约定是如何对应的？改一个常数会牵动哪些地方？**

学完本讲，你应当能够：

1. 说清 `gen_atan_lut.py` 如何用「地址 = 256·tan(θ)，值 = 512·θ」把一个比值映射成定点相位；
2. 说清 `gen_rot_lut.py` 如何把 [0, π/4] 的旋转因子 (cos θ, sin θ) 打包成 32 位表项，以及它为何与 atan 表共享相位刻度；
3. 说清 `gen_deinter_lut.py` 的「两级查表 + 22 位指令表项」结构，以及它如何用 erase 位表达去穿孔（de-puncture）；
4. 把三个脚本里的 `SIZE` / `SCALE` 常数和 `verilog/common_defs.v` 里的 `*_SHIFT` 一一对应起来，并推断改动一个常数后的连锁修改清单。

## 2. 前置知识

在进入源码前，先用三段白话把背景铺平。

**为什么要查表。** OFDM 解调里有大量三角函数与位置重排。硬件里算一次 `atan` 或 `sin` 要么用 CORDIC 迭代（耗周期），要么用 DSP 乘法（耗资源）。但如果输入范围有限（比如相位只在 [0, π/4]），就可以把范围内的所有可能结果预先算好存进 ROM，运行时用「输入当地址、读出当结果」一步完成。这就是 LUT。本讲的三个脚本就是给三块 ROM 离线「灌数据」的工具。

**定点小数。** 硬件里没有浮点（至少 OpenOFDM 全程不用），小数靠「放大成整数」来表示。比如要把相位精度做到 1/512 弧度，就把所有相位乘 512 存成整数——那么 π 就表示成 `round(π·512)=1608`。这个「乘 512」在代码里写成左移，于是出现了形如 `ATAN_LUT_SCALE_SHIFT=9` 的宏（2^9=512）。放大倍数改变，π 的整数表示也必须跟着改变，这是本讲反复出现的核心约束。

**`.mif` 与 `.coe`。** Xilinx 的块 RAM（Block Memory）可由初始化文件预置内容。`.mif`（Memory Initialization File）是一行一个二进制值；`.coe`（Coefficient File）是 Xilinx 综合工具偏好的格式，带 `memory_initialization_radix=` 等头信息。本讲的每个脚本都同时产出 `.mif` 和 `.coe` 两份：仿真时 `BLK_MEM_GEN_V4_2` 行为模型读 `.mif`，上板综合时 CORE Generator 读 `.coe`。两个文件内容等价，只是格式不同。

> 提醒：这三个脚本都是 **Python 2** 语法（用了 `print '...'` 语句，见 [scripts/gen_atan_lut.py:36-38](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_atan_lut.py#L36-L38)）。在只有 Python 3 的环境里直接运行会报 `SyntaxError`，需要用 `python2`（或 `python2.7`）解释器执行。这一点在本讲的代码实践中很重要。

## 3. 本讲源码地图

本讲涉及的关键文件分三类：生成脚本（本讲主角）、定点约定（脚本与 RTL 的「契约」）、消费方 RTL（验证表项确实这么用）。

| 文件 | 角色 | 关键内容 |
|---|---|---|
| [scripts/gen_atan_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_atan_lut.py) | 生成器 | 生成 `atan_lut.mif/.coe`，地址=tan 量化、值=定点相位 |
| [scripts/gen_rot_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_rot_lut.py) | 生成器 | 生成 `rot_lut.mif/.coe`，存 (cos θ, sin θ) 旋转因子 |
| [scripts/gen_deinter_lut.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py) | 生成器 | 生成 `deinter_lut.mif/.coe`，两级查表的交织逆映射 |
| [verilog/common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) | 定点契约 | `ATAN_LUT_LEN_SHIFT`/`ATAN_LUT_SCALE_SHIFT`/`ROTATE_LUT_*` 宏 |
| [verilog/common_params.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v) | 定点契约 | `PI=1608` 等定点 π 定义（含被注释的连锁示例） |
| [verilog/phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) | atan 消费方 | 用除法算出地址、查 `atan_lut` 还原相位 |
| [verilog/rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) | rotate 消费方 | 按相位查 `rot_lut` 取 cos/sin 做复数旋转 |
| [verilog/deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) | deinter 消费方 | 两级查 `deinter_lut` 驱动双口 RAM 重排比特 |
| [verilog/coregen/atan_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v)、[rot_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v)、[deinter_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v) | ROM 行为模型 | Xilinx 块 RAM 封装，声明位宽/深度并挂载 `.mif` |
| [scripts/decode.py](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py) | 算法参考 | `deinterleave()` 给 `gen_deinter_lut.py` 提供交织置换公式 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，分别对应三个生成脚本。每个脚本自成一体，但它们共享同一套定点刻度，所以最后会有一个关于「常数一致性」的综合讨论。

### 4.1 atan 相位查表：gen_atan_lut.py

#### 4.1.1 概念说明

`atan_lut` 服务于 [verilog/phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) 模块——它要把一个复数样本 `(I, Q)` 变成定点相位 θ（用于频偏估计，见前置讲义 u2-l3）。直接在硬件里算 `atan(Q/I)` 代价高，于是改成查表。

关键观察：任何一个复数，都可以通过「取绝对值 + 交换使大者为邻边」折叠到第一象限的前 1/8 扇区 [0, π/4]。在这个扇区里：

\[ \tan(\theta) = \frac{\min(|I|,|Q|)}{\max(|I|,|Q|)} \in [0, 1] \]

也就是说，折叠后相位 θ 完全由比值 `min/max` 决定，且这个比值落在 [0,1]。把 [0,1] 均匀量化成 256 档，每档对应一个 θ。于是 ROM 的「地址」就是 256·tan(θ)，「内容」就是 θ 放大 512 倍后的整数。这样硬件只需算一次除法拿到地址、查一次表拿到相位，没有任何迭代。

#### 4.1.2 核心流程

生成脚本的逻辑非常短，核心只有四行：

1. 把地址索引 `i`（0 到 255）映射回比值 `key = i/256`，即 `tan(θ)`；
2. 计算 `val = round(atan(key) · 512) = round(θ · 512)`，即定点相位；
3. 把 `val` 写成 9 位二进制，一行一个，存进 `atan_lut.mif`；
4. 同时输出 `.coe` 给综合工具。

数学上：

\[
\text{addr}(i) = i, \quad \tan(\theta_i) = \frac{i}{256}, \quad \text{LUT}[i] = \mathrm{round}\!\left(\atan\!\left(\frac{i}{256}\right) \cdot 512\right) \approx \mathrm{round}(\theta_i \cdot 512)
\]

其中 256 = 2^8 是表深度（`SIZE`），512 = 2^9 是相位放大倍数（`SCALE`）。注意 `SCALE = SIZE·2`，这一关系是后续连锁修改的根源。

#### 4.1.3 源码精读

先看脚本的常数与主循环。[scripts/gen_atan_lut.py:13-14](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_atan_lut.py#L13-L14) 定义了两个决定一切的全局量：

```python
SIZE = 2**8     # = 256，表项数 = 地址深度
SCALE = 512     # = 2^9，相位值放大倍数
```

主循环在 [scripts/gen_atan_lut.py:32-37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_atan_lut.py#L32-L37)，其中真正「算」的就两行：

```python
key = float(i)/SIZE                       # i/256，当作 tan(θ)
val = int(round(math.atan(key)*SCALE))    # θ·512，定点相位
...
f.write('{0:09b}\n'.format(val))          # 写成 9 位二进制
```

`{0:09b}` 把值格式化成 9 位二进制（不足前补 0）。这就是为什么 `atan_lut.mif` 的每一行正好是 9 位（实测首行 `000000000`、第二行 `000000010`=2，对应 atan(1/256)·512≈2.0）。

再看 Verilog 侧的「契约」。[verilog/common_defs.v:1-3](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L1-L3) 把脚本里的两个常数固化成移位位数：

```verilog
`define ATAN_LUT_LEN_SHIFT          8     // 对应 SIZE=2^8
// changing this requires changing PI definition in common_params.v accordingly
`define ATAN_LUT_SCALE_SHIFT        9     // 对应 SCALE=2^9=512
```

注意第 2 行那句注释——它直接点明了 `ATAN_LUT_SCALE_SHIFT` 与 PI 定义的强耦合，是本讲「连锁修改」问题的官方提示。

接着看消费方 [verilog/phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v)。地址和数据位宽直接来自上面两个宏（[verilog/phase.v:34-37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L34-L37)）：

```verilog
wire [`ATAN_LUT_LEN_SHIFT-1:0] atan_addr;     // [7:0]，8 位地址
wire [`ATAN_LUT_SCALE_SHIFT-1:0] atan_data;   // [8:0]，9 位数据
assign atan_addr = quotient[`ATAN_LUT_LEN_SHIFT-1:0];
```

地址 `atan_addr` 来自一次除法（[verilog/phase.v:64-75](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L64-L75)）。被除数是 `min`，除数是 `max[31:8]`（即 max 右移 8 位 = max/256）：

```verilog
.dividend(min),
.divisor({{(`ATAN_LUT_LEN_SHIFT-8){1'b0}}, max[31:`ATAN_LUT_LEN_SHIFT]}),
```

于是商 = `min / (max/256) = 256·(min/max) = 256·tan(θ)`，低 8 位正是 ROM 地址。折叠到第一扇区的 `max`/`min` 计算在 [verilog/phase.v:108-116](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L108-L116)，查表实例化在 [verilog/phase.v:85-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L85-L89)。

最后看 ROM 行为模型 [verilog/coregen/atan_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v)。它的端口位宽必须和生成数据严格对齐——地址 8 位、数据 9 位、深度 256（[verilog/coregen/atan_lut.v:47-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v#L47-L48) 与 [verilog/coregen/atan_lut.v:82-86](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v#L82-L86)），并用 `.C_INIT_FILE_NAME("atan_lut.mif")`（[verilog/coregen/atan_lut.v:77](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/atan_lut.v#L77)）挂载脚本生成的 `.mif`。三方（脚本、宏、ROM）必须位数一致，否则仿真读出的就是错位数据。

#### 4.1.4 代码实践

**实践目标**：亲手重生成 `atan_lut.mif`，确认脚本常数与 Verilog 宏一致，并推断「改 SIZE」的连锁修改清单。

**操作步骤**：

1. 备份现有表：`cp verilog/coregen/atan_lut.mif /tmp/atan_lut.mif.bak`；
2. 在仓库根目录运行（注意是 Python 2）：
   ```bash
   python2 scripts/gen_atan_lut.py --out verilog/coregen/atan_lut.mif
   ```
3. 用 `diff /tmp/atan_lut.mif.bak verilog/coregen/atan_lut.mif` 比较新旧文件；
4. 数一下行数：`wc -l < verilog/coregen/atan_lut.mif`。

**需要观察的现象**：

- 新生成的 `.mif` 应有 **256 行**，每行 **9 位** 二进制；
- 首行 `000000000`（θ=0）、第二行 `000000010`（=2，因为 atan(1/256)·512 ≈ 2.0）、第三行 `000000100`（=4）；
- `diff` 应无输出——即重新生成的内容与仓库里现存的完全一致（说明表是由这个脚本确定性生成的）。

**预期结果**：256 行 9 位，`diff` 为空。脚本还会同时生成 `verilog/coregen/atan_lut.coe`。

**连锁修改清单（核心思考题）**：若把 `SIZE` 从 2^8 改成 2^9（即把表变精细一倍），并保持 `SCALE = SIZE*2` 的约定，需要同步改动：

| 改动点 | 原值 | 新值 | 原因 |
|---|---|---|---|
| `gen_atan_lut.py` `SIZE` | 256 | 512 | 你主动改的 |
| `gen_atan_lut.py` `SCALE` | 512 | 1024 | `SCALE=SIZE*2` |
| `common_defs.v` `ATAN_LUT_LEN_SHIFT` | 8 | 9 | 地址深度 = 2^LEN_SHIFT |
| `common_defs.v` `ATAN_LUT_SCALE_SHIFT` | 9 | 10 | 相位刻度 = 2^SCALE_SHIFT |
| `common_params.v` `PI` | 1608 | 3217 | π = round(π·2^SCALE_SHIFT)，**注意源码里被注释的 `3217` 正是这个值** |
| `common_defs.v` `ROTATE_LUT_LEN_SHIFT` | 9 | 10 | 它等于 `ATAN_LUT_SCALE_SHIFT`（见 4.2） |
| `gen_rot_lut.py` `ATAN_LUT_SCALE` | 512 | 1024 | rotate 表深度依赖它 |
| coregen IP `atan_lut.v` / `rot_lut.v` | — | 重新生成 | 地址位宽、深度都变了，必须用 Xilinx CORE Generator 重做 `.xco` |

特别留意 [verilog/common_params.v:4-6](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L4-L6) 里那几行被注释的 PI：

```verilog
// localparam PI =             3217;    //  = PI*(1<<`ATAN_LUT_SCALE_SHIFT)
// localparam PI =             3217*2;
localparam PI =             1608;       //  = PI*(1<<`ATAN_LUT_SCALE_SHIFT)
```

这等于作者把「刻度翻倍后 PI 取什么值」直接写在了源码里，是上面那张表最硬的证据。

> 若你的环境没有 `python2`，本实践为「待本地验证」；但仍可只读地用 Python 3 重写 `print` 语句后离线推演常数关系，不改动仓库源码。

#### 4.1.5 小练习与答案

**练习 1**：为什么地址用 `256·tan(θ)` 而不是直接用 θ 作地址？

**参考答案**：因为硬件能直接算出的是比值 `min/max = tan(θ)`（一次除法即可），而 θ 本身正是要求解的未知量。把已知量 `tan(θ)` 当地址、把未知量 θ 当内容，才能做到「一次除法 + 一次查表」。

**练习 2**：表的最大地址是 255，对应 θ≈atan(255/256)≈44.85°，离 45°(π/4) 还差一点。这会有问题吗？

**参考答案**：在 [phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) 的折叠逻辑里，θ=π/4 恰好是第一扇区的边界，边界附近 1/256 的量化误差（约 0.15°）远小于后续模块（如导频细频偏）的容差，工程上可接受；若需更高精度，就按 4.1.4 的清单把 SIZE 调大。

---

### 4.2 rotate 旋转因子查表：gen_rot_lut.py

#### 4.2.1 概念说明

`rot_lut` 服务于 [verilog/rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v)——把复数样本 `C = I + jQ` 旋转一个相位 θ（频偏校正用，见 u2-l3）。数学上就是乘以 `e^{jθ} = cos θ + j·sin θ`：

\[
C' = (I + jQ)\cdot(\cos\theta + j\sin\theta)
\]

硬件实现这一乘法只需要 `(cos θ, sin θ)` 这对常数因子。θ 的范围是 [-π, π]，但和 atan 表一样，可以先把 θ 折叠进 [0, π/4]，只在这个小范围内存 (cos θ, sin θ)，再用象限还原把结果摆回正确位置。这样 8 个扇区共用一份 [0, π/4] 的表，省下近 8 倍存储。

#### 4.2.2 核心流程

这里有一个和 atan 表不同的关键设计：**rotate 表的「地址精度」沿用了 atan 表的相位刻度**。因为 `rotate.v` 的输入 `phase` 本身就是 `phase.v` 输出的定点相位（放大 512 倍），地址必须能在同一个刻度下索引。

1. 计算一个扇区里有多少个刻度：`MAX = round(π/4 · 512) = 402`；
2. 但 ROM 深度必须是 2 的幂（地址是整数位宽），于是向上取整：`SIZE = 2^ceil(log2(402)) = 512`（多出来的 ~110 项是冗余，但换取了干净的 9 位地址）；
3. 对每个地址 `i`，反算相位 `θ_i = (i/MAX)·(π/4)`，存 `I = round(cos θ_i · 2048)`、`Q = round(sin θ_i · 2048)`；
4. 把 `(I, Q)` 打包成一个 32 位字：高 16 位是 I、低 16 位是 Q。

\[
\text{MAX} = \mathrm{round}\!\left(\frac{\pi}{4}\cdot 512\right) = 402, \quad \text{SIZE} = 2^{\lceil \log_2 402 \rceil} = 512
\]

\[
\text{entry}[i] = \big(\mathrm{round}(\cos\theta_i \cdot 2048) \ll 16\big) \;\big|\; \mathrm{round}(\sin\theta_i \cdot 2048), \quad \theta_i = \frac{i}{402}\cdot\frac{\pi}{4}
\]

其中 2048 = 2^11（`SCALE`），是 cos/sin 的放大倍数；512 这个「相位刻度」就是 atan 表的 `ATAN_LUT_SCALE_SHIFT`。这就是 [verilog/common_defs.v:5](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L5) 写 `ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT` 的原因——两张表共享同一根相位坐标轴。

#### 4.2.3 源码精读

脚本的常数在 [scripts/gen_rot_lut.py:14-15](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_rot_lut.py#L14-L15)：

```python
ATAN_LUT_SCALE = 512     # 相位刻度，必须与 common_defs.v 的 ATAN_LUT_SCALE_SHIFT 一致
SCALE = 2048             # cos/sin 放大倍数 = 2^11
```

派生出深度（[scripts/gen_rot_lut.py:27-28](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_rot_lut.py#L27-L28)）：

```python
MAX = int(round(math.pi/4*ATAN_LUT_SCALE))      # 402，每个扇区的刻度数
SIZE = int(2**math.ceil(math.log(MAX, 2)))       # 512，向上取整到 2 的幂
```

主循环（[scripts/gen_rot_lut.py:32-39](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_rot_lut.py#L32-L39)）算 cos/sin 并打包：

```python
key = float(i)/MAX*math.pi/4                 # 反算 θ_i
I = int(round(math.cos(key)*SCALE))          # cos θ · 2048
Q = int(round(math.sin(key)*SCALE))          # sin θ · 2048
val = (I<<16) + Q                            # 高16位I、低16位Q 拼成 32 位
f.write('{0:032b}\n'.format(val))            # 写成 32 位二进制
```

对照 `verilog/coregen/rot_lut.mif`：首行 `00001000000000000000000000000000`，拆开 I=`0000100000000000`=2048、Q=0，正是 θ=0 时 (cos 0·2048, sin 0·2048) = (2048, 0)；第二行 Q 变成 4，对应 sin(π/4/402)·2048 ≈ 4.0，与脚本一致。

Verilog 契约在 [verilog/common_defs.v:5-6](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L5-L6)：

```verilog
`define ROTATE_LUT_LEN_SHIFT        `ATAN_LUT_SCALE_SHIFT   // =9，深度 512
`define ROTATE_LUT_SCALE_SHIFT      11                      // cos/sin 放大 2^11
```

消费方 [verilog/rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v) 用 `ROTATE_LUT_LEN_SHIFT` 截取地址（[verilog/rotate.v:48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L48)），用 `ROTATE_LUT_SCALE_SHIFT` 从乘积里抽回 16 位结果（[verilog/rotate.v:45-46](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L45-L46)）：

```verilog
assign rot_addr = actual_phase[`ROTATE_LUT_LEN_SHIFT-1:0];
assign out_i = p_i[`ROTATE_LUT_SCALE_SHIFT+15:`ROTATE_LUT_SCALE_SHIFT];   // 右移 11 位还原
assign out_q = p_q[`ROTATE_LUT_SCALE_SHIFT+15:`ROTATE_LUT_SCALE_SHIFT];
```

`out_i = p_i[26:11]` 等价于把 32 位乘积右移 11 位，正好抵消生成时 ×2048 的放大。表项拆包在 [verilog/rotate.v:49-50](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L49-L50)（`raw_rot_i = rot_data[31:16]`、`raw_rot_q = rot_data[15:0]`），象限折叠与还原分别在 [verilog/rotate.v:107-119](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L107-L119) 与 [verilog/rotate.v:126-159](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L126-L159)。

ROM 行为模型 [verilog/coregen/rot_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v) 声明地址 9 位、数据 32 位、深度 512（[verilog/coregen/rot_lut.v:50-54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L50-L54)、[verilog/coregen/rot_lut.v:88-91](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L88-L91)），并且是**双口** RAM（`clka/addra/douta` + `clkb/addrb/doutb`，[verilog/coregen/rot_lut.v:40-54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L40-L54)）。这一点与前置 u2-l3 讲过的资源共享对应：`rot_lut` 用双口同时服务 `sync_long` 和 `equalizer` 两个模块，A 口与 B 口各读各的地址，省下一整块 ROM。

#### 4.2.4 代码实践

**实践目标**：重生成 `rot_lut.mif`，验证表项打包格式，并理解它为何与 atan 表共享相位刻度。

**操作步骤**：

1. 备份：`cp verilog/coregen/rot_lut.mif /tmp/rot_lut.mif.bak`；
2. 运行（Python 2）：
   ```bash
   python2 scripts/gen_rot_lut.py --out verilog/coregen/rot_lut.mif
   ```
3. 看脚本 stdout 里打印的 `SIZE = 512, scale = 2048`；
4. 手算验证首项与第二项：
   - θ=0：cos 0·2048 = 2048、sin 0·2048 = 0 → `val = 2048<<16 | 0`；
   - θ=π/4/402：sin·2048 ≈ round(2048·sin(π/1628)) ≈ 4；
5. 把首行 32 位串拆成高 16 / 低 16 对比你的手算值。

**需要观察的现象**：

- `.mif` 共 **512 行**，每行 **32 位**；
- 首行 I=2048、Q=0；Q 随地址单调递增，到地址 401 附近 Q 接近 2048、I 接近 1448（即 θ≈π/4 时 cos·2048≈1448、sin·2048≈1448——因 2048·sin(π/4)=2048·0.707≈1448）；
- 地址 402~511 是冗余项（超过一个扇区的刻度），内容会超出 [0, π/4]，但 `rotate.v` 的折叠逻辑保证 `actual_phase` 永远 < 402，不会读到这些项。

**预期结果**：512 行 32 位，stdout 打印 `SIZE = 512, scale = 2048`，与 `rot_lut.v` 的 `C_READ_DEPTH_A=512`、`C_READ_WIDTH_A=32` 完全吻合。

**思考题（待本地验证）**：如果把 `SCALE` 从 2048 改成 4096（提高 cos/sin 精度），需要同步改什么？答案是 `common_defs.v` 的 `ROTATE_LUT_SCALE_SHIFT` 11→12，以及 `rotate.v` 里 `p_i[ROTATE_LUT_SCALE_SHIFT+15:...]` 的切片会自动跟随宏变化（无需改 RTL），但 cos/sin 乘积可能超出 16 位，需要核算 `complex_mult` 输出位宽是否仍够。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SIZE = 512` 而不是正好 402？

**参考答案**：ROM 地址是 9 位整数，深度必须是 2 的幂。402 向上取整到最近的 2 的幂是 512，多出的项虽浪费约 21% 存储，但换来了「地址位宽 = 整数 = log2 深度」的简洁性，使 `rot_addr` 可以直接用 `actual_phase[8:0]` 截取。

**练习 2**：`rot_lut` 为什么是双口 RAM，而 `atan_lut` 是单口？

**参考答案**：`atan_lut` 在同一时刻只被 `phase` 模块的一个实例读（虽然该实例在 `sync_short`/`equalizer` 间分时复用，但任一拍只有一个输入）；而 `rot_lut` 同时被 `sync_long` 和 `equalizer` 两个模块读（见 u2-l3 资源共享），双口让两者各用一口并行访问，省下第二块 ROM。

---

### 4.3 deinter 交织逆映射查表：gen_deinter_lut.py

#### 4.3.1 概念说明

`deinter_lut` 服务于 [verilog/deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v)（见前置 u3-l4）。解交织是把一个 OFDM 符号内被发射端打乱的比特按原始顺序还原。它和前两张表有本质区别：前两张存的是「数值」（相位、cos/sin），这张存的是「指令」——每一行表项告诉硬件「去 RAM 的哪个地址、取哪个比特、是不是穿孔空位、要不要输出、是不是结束了」。

而且不同速率（802.11a 的 6/9/12/18/24/36/48/54 Mbps 与 802.11n MCS 0–7）的交织规则各不相同。脚本要为 16 种速率各生成一段子表，再用一个 32 项的「目录」把它们索引起来——这就是「两级查表」。

#### 4.3.2 核心流程

整体结构是一个一维 ROM，逻辑上分两层：

```
+----------------+
|  32 项目录     |   ← 用 {ht, rate[3:0]} 索引，存「子表起始偏移」
+----------------+
|  6 Mbps 子表   |   ← offset=32
+----------------+
|  9 Mbps 子表   |   ← offset=32+len(6Mbps子表)
+----------------+
       ......
+----------------+
|  MCS 7 子表    |
+----------------+
|  填充到 2 的幂 |   ← 凑成 2048 项（11 位地址）
+----------------+
```

**第一级查表**：硬件用 `lut_key = {6'b0, ht, rate[3:0]}`（共 11 位）读目录，拿到该速率子表的起始地址。

**第二级查表**：把刚拿到的起始地址写回 `lut_key`，开始逐项递增 `lut_key` 读子表，每读一项执行一条「指令」，直到读到 `done=1` 的结束行。

每个 22 位表项的字段布局（见 [scripts/gen_deinter_lut.py:15-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L15-L29)）：

| 位 | 字段 | 含义 |
|---|---|---|
| 21 | erase[0] (null_a) | 第一个输出比特是去穿孔空位 |
| 20 | erase[1] (null_b) | 第二个输出比特是去穿孔空位 |
| 19:14 | addra | 从双口 RAM A 口读的地址 |
| 13:8 | addrb | 从双口 RAM B 口读的地址 |
| 7:5 | bita | addra 那个 6 比特字里取第几位 |
| 4:2 | bitb | addrb 那个 6 比特字里取第几位 |
| 1 | out_stb | 本拍是否产出有效输出 |
| 0 | done | 本子表是否结束 |

**去穿孔（de-puncture）**：非 1/2 码率（3/4、2/3、5/6）在发射端删掉了部分卷积码比特。解交织时，被删位置要补一个「空比特」并标 erase，直送 Viterbi 当「未知」处理（见 u3-l5）。脚本用 `puncture` 计数器按码率节奏在合适位置插入 `(1<<21)` 或 `(1<<20)` 标记。

#### 4.3.3 源码精读

先看目录索引规则。[scripts/gen_deinter_lut.py:53-62](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L53-L62) 定义了 802.11a 的 4 位速率码（`RATE_BITS`），[scripts/gen_deinter_lut.py:65-84](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L65-L84) 列出 16 种 (rate, mcs, ht)。目录项的计算在 [scripts/gen_deinter_lut.py:196-199](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L196-L199)：

```python
if ht:
    idx = (1<<4) + mcs       # HT: 目录下标 = 16 + mcs（16..23）
else:
    idx = int(RATE_BITS[rate], 2)   # legacy: 目录下标 = 速率码（8..15）
header[idx] = offset        # 把子表起始偏移写进目录
```

这与消费方 [verilog/deinterleave.v:111](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L111) 完全吻合：`lut_key <= {6'b0, ht, rate[3:0]}`。对 legacy，`{0, rate[3:0]}` 就是速率码；对 HT，`{1, mcs}` = 16+mcs。

每个表项由 `do_rate()` 生成（[scripts/gen_deinter_lut.py:87-179](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L87-L179)）。核心是先从参考解码器拿到交织置换序列（[scripts/gen_deinter_lut.py:88-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L88-L89)）：

```python
idx_map = decode.Decoder(None).deinterleave(None, rate=rate, mcs=mcs, ht=ht)
seq = [t[1] for t in idx_map]    # seq[k] = 第 k 个原始比特对应的交织后位置
```

这里 `decode.deinterleave(None, ...)` 在 `in_bits=None` 时返回 `(原始位置, 交织后位置)` 元组列表（[scripts/decode.py:509-513](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/decode.py#L509-L513)）。也就是说，`gen_deinter_lut.py` 直接复用 Python 参考解码器的交织公式（见 u5-l1），保证「硬件查表」与「浮点参考」用的是同一套置换规则——这是交叉验证能成立的前提。

把置换序列封装成 22 位指令的逻辑在 [scripts/gen_deinter_lut.py:112-167](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L112-L167)。基准字段（[scripts/gen_deinter_lut.py:115-124](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L115-L124)）：

```python
addra = seq[i]/n_bpsc          # 交织位置 → RAM 地址
bita  = seq[i]%n_bpsc          # 该地址 6 比特字里的位号
...
base = (addra<<14) + (addrb<<8) + (bita<<5) + (bitb<<2) + (1<<1)   # out_stb=1
```

去穿孔插入 erase 标记的分支在 [scripts/gen_deinter_lut.py:126-166](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L126-L166)（按 `1/2`、`3/4`、`2/3`、`5/6` 四种节奏用 `puncture` 计数器周期性插入 `(1<<20)`/`(1<<21)`）。每段子表末尾追加一行「复位行」把 `addra` 拉回半子载波数并置 `done`（[scripts/gen_deinter_lut.py:169-177](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L169-L177)）：

```python
if ht:
    mask = (26<<14) + 1     # HT: addra=26（=52/2），done=1
else:
    mask = (24<<14) + 1     # legacy: addra=24（=48/2），done=1
data.append(mask)
data.extend([0]*4)          # 哨兵填充
```

注意这里的 `+1` 置的是 bit 0（done），而正常数据行的 `+(1<<1)` 置的是 bit 1（out_stb）——结束行不输出、只标记结束。

最后整体深度向上取整到 2 的幂（[scripts/gen_deinter_lut.py:207-210](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/scripts/gen_deinter_lut.py#L207-L210)）：

```python
total = int(2**math.ceil(math.log(offset, 2)))   # 凑成 2048
lut.extend([0]*(total-offset))                   # 尾部补 0
```

实测 `deinter_lut.mif` 正好 2048 行、每行 22 位，与 ROM 行为模型 [verilog/coregen/deinter_lut.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v) 的地址 11 位、数据 22 位、深度 2048（[verilog/coregen/deinter_lut.v:47-48](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v#L47-L48)、[verilog/coregen/deinter_lut.v:83-86](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/deinter_lut.v#L83-L86)）一致。

消费方 [verilog/deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v) 把 22 位字段一一拆回使用，字段切片与脚本的 `<<` 完全镜像（[verilog/deinterleave.v:34-49](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L34-L49)）：

```verilog
assign erase[0] = lut_out_delayed[21];      // 对应脚本的 (1<<21)
assign erase[1] = lut_out_delayed[20];      // 对应脚本的 (1<<20)
wire [5:0] lut_addra = lut_out[19:14];      // 对应 addra<<14
wire [5:0] lut_addrb = lut_out[13:8];       // 对应 addrb<<8
wire [2:0] lut_bita = lut_out_delayed[7:5]; // 对应 bita<<5
wire [2:0] lut_bitb = lut_out_delayed[4:2]; // 对应 bitb<<2
assign output_strobe = ... & lut_out_delayed[1];  // out_stb
wire lut_done = lut_out[0];                 // done
```

两级查表的状态机在 [verilog/deinterleave.v:107-155](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L107-L155)：`S_INPUT` 写满一个符号后置 `lut_key={ht,rate}` 转入 `S_GET_BASE`；`S_GET_BASE` 读目录拿偏移（[verilog/deinterleave.v:125-133](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L125-L133)）；`S_OUTPUT` 逐拍 `lut_key+1` 走子表并驱动双口 RAM（[verilog/deinterleave.v:135-152](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L135-L152)），直到 `lut_done`。

#### 4.3.4 代码实践

**实践目标**：重生成 `deinter_lut.mif`，肉眼核对目录偏移，并追踪一种速率的子表。

**操作步骤**：

1. 运行（Python 2，且需在同目录可 `import decode`，故必须在 `scripts/` 目录下执行或把 `scripts` 加入路径）：
   ```bash
   cd scripts
   python2 gen_deinter_lut.py --out ../verilog/coregen/deinter_lut.mif
   ```
2. 观察 stdout 打印的 `[rate=6, mcs=0] -> 32`、`[rate=9, mcs=0] -> ...` 等偏移；
3. 用文本工具看 `deinter_lut.mif` 的第 32 行起（第一个子表，6 Mbps 1/2 码率）；
4. 选 6 Mbps 子表的第一行 22 位串，按 4.3.2 的字段表拆成 erase/addra/addrb/bita/bitb/out_stb/done 七段。

**需要观察的现象**：

- 文件共 **2048 行**，每行 **22 位**；
- 前 32 行是目录，其中下标 0~7 和 24~31 为全 0（没有对应速率），下标 8~15 是 8 个 legacy 速率的偏移，下标 16~23 是 MCS 0~7 的偏移；
- 6 Mbps（1/2 码率）子表里**没有** erase 标记（bit 21、20 恒 0），因为它不需要去穿孔；而 9 Mbps（3/4 码率）子表里能找到 `1` 打头的位串（bit 21 或 20 置位）；
- 每个子表最后一行的 bit 0（done）=1、bit 1（out_stb）=0。

**预期结果**：stdout 的偏移与目录区（前 32 行）解码出的值一致；6 Mbps 子表无 erase，9/18/36 Mbps（3/4）与 48 Mbps（2/3）子表有 erase 行。

**思考题（待本地验证）**：`do_rate()` 末尾为何要写 `addra=26`（HT）或 `addra=24`（legacy）的复位行？结合 [verilog/deinterleave.v:92](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L92)（`addra <= num_data_carrier>>1`）可知：子表结束后要把 RAM 写指针复位到「半子载波数」，为下一个符号的 `S_INPUT` 阶段重新开始写入做准备（RAM 的写入与读出在地址空间上是折叠使用的）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 deinter 表用「两级查表」而不是一张大表直接由速率索引？

**参考答案**：因为不同速率的子表长度差异很大（1/2 码率短、5/6 码率长），且数量乘起来不是 2 的幂。用一个 32 项的小目录存「每种子表的起始偏移」，再让子表顺序拼接、整体凑成 2048 项的 2 的幂，既让地址位宽保持整数（11 位），又让任意速率都能用「先查目录、再顺序走子表」的统一硬件逻辑处理。

**练习 2**：表项里 `erase[0]`（bit 21）和 `erase[1]`（bit 20）分别表示什么？

**参考答案**：硬件每拍输出 2 个比特（`out_bits[1:0]`，见 [deinterleave.v:43-44](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v#L43-L44)）。`erase[0]` 标记第一个输出比特是去穿孔补的空位，`erase[1]` 标记第二个。被标的比特不来自真实数据，会被 [ofdm_decoder.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/ofdm_decoder.v) 直接送进 Viterbi 当「未知」软判决处理，从而把 3/4、2/3、5/6 码率的码流还原回 1/2 节奏。

---

## 5. 综合实践：LUT 常数一致性自检

把三个脚本和 Verilog 侧的定点约定串起来做一次「一致性体检」。这是真实工程里改 LUT 时最容易踩坑的地方——脚本里改了常数却忘了同步 RTL，会导致仿真读出全错位的表项。

**任务**：制作一张「常数溯源表」，验证以下五个量在四处（脚本、`common_defs.v`、`common_params.v`、coregen ROM 深度/位宽）完全自洽。

| 量 | 脚本里的体现 | RTL 里的体现 | 期望值 |
|---|---|---|---|
| atan 表深度 | `gen_atan_lut.py` `SIZE` | `ATAN_LUT_LEN_SHIFT`、`atan_lut.v` 深度 | 256 |
| atan 相位刻度 | `gen_atan_lut.py` `SCALE` | `ATAN_LUT_SCALE_SHIFT`、`phase.v` 数据位宽 | 512 |
| rotate 相位刻度 | `gen_rot_lut.py` `ATAN_LUT_SCALE` | `ROTATE_LUT_LEN_SHIFT`、`rot_lut.v` 地址位宽 | 512 |
| rotate 幅值刻度 | `gen_rot_lut.py` `SCALE` | `ROTATE_LUT_SCALE_SHIFT` | 2048 |
| 定点 π | （由 SCALE 派生） | `common_params.v` `PI` | 1608 |
| deinter 表深度 | `gen_deinter_lut.py` `total` | `deinter_lut.v` 地址位宽/深度 | 2048 |

**操作步骤**：

1. 用 `Read`/`grep` 打开 [common_defs.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v) 与三个脚本，逐行核对上表每一格；
2. 验证两条派生关系：
   - `SCALE = SIZE*2`（atan）：512 = 256·2 ✓
   - `ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT`：9 = 9 ✓（这就是 rotate 与 atan 共享相位轴的硬证据）
3. 验证 `PI = round(π · 2^ATAN_LUT_SCALE_SHIFT)`：round(π·512) = round(1608.49…) = 1608 ✓；
4. 用 `wc -l` 数三个 `.mif` 的行数，确认分别等于 256/512/2048，与三个 coregen ROM 的 `C_READ_DEPTH_A` 一致。

**预期结果**：上表所有格子自洽，三条派生关系成立，三个 `.mif` 行数与 ROM 深度吻合。

**延伸思考**：假如你想把整套定点精度提高一倍（atan 与 rotate 共同升级），按 4.1.4 的连锁清单，`PI` 会从 1608 变成 3217——而 [common_params.v:4](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_params.v#L4) 那行被注释掉的 `3217` 恰好就是答案。这说明作者在源码里已经预留了「精度翻倍」的预演，本实践等于在重现作者当年的推算。

## 6. 本讲小结

- 三个脚本的本质都是「把运行时要算的量离线算好、灌进 ROM」：atan 表把比值 `tan(θ)` 映射成定点相位，rotate 表存 (cos θ, sin θ) 旋转因子，deinter 表存「去哪取哪位比特」的 22 位指令。
- 定点刻度是脚本与 RTL 的硬契约：`SIZE=2^ATAN_LUT_LEN_SHIFT`、`SCALE=2^ATAN_LUT_SCALE_SHIFT`，且 `SCALE=SIZE*2`；改一个常数会沿 `common_defs.v → common_params.v (PI) → coregen ROM` 一路连锁。
- rotate 表与 atan 表**共享相位坐标轴**——`ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT`，因为 `rotate.v` 的输入相位就是 `phase.v` 输出的定点相位。
- rotate 表深度用 `2^ceil(log2(MAX))` 凑成 2 的幂（402→512），浪费少量项换取整数地址位宽；它是双口 RAM 以同时服务 `sync_long` 与 `equalizer`。
- deinter 表是「两级查表 + 22 位指令」：32 项目录按 `{ht, rate[3:0]}` 索引子表偏移，子表逐项驱动双口 RAM 重排比特，并用 erase 位表达去穿孔空位。
- `gen_deinter_lut.py` 直接复用 `decode.deinterleave()` 的交织公式，从根上保证「硬件查表」与「Python 参考解码器」使用同一套置换规则——这是 u5-l2 交叉验证能成立的隐含前提。

## 7. 下一步学习建议

- **走向交叉验证**：本讲强调了 deinter 表复用 `decode.py` 的公式，下一站建议读 [u5-l2 交叉验证框架 test.py]——看 `test.py` 如何把 Python 期望与 Verilog 落盘逐阶段对账，其中 DEINTER 阶段的比对正是建立在本讲「同一套置换规则」之上。
- **回看消费方**：若对某张表的用途还想加深，重读 [phase.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v)（u2-l3）、[rotate.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v)（u2-l3）、[deinterleave.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/deinterleave.v)（u3-l4）的状态机，对照本讲的字段表会豁然开朗。
- **尝试扩展**：参考 [u6-l1 定点数与缩放约定]，亲手按本讲 4.1.4 的连锁清单把整套精度翻倍推演一遍（不必真的综合），体会定点设计里「牵一发动全身」的工程取舍。
- **阅读官方文档**：[docs/source/verilog.rst](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/docs/source/verilog.rst) 的 Phase 与 Rotation 两节给出了与本讲互补的图示（八象限折叠图）与数学推导，值得对照阅读。
