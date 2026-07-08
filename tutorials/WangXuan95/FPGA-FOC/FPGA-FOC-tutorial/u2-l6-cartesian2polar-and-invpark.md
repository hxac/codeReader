# cartesian2polar 与反 Park 变换

## 1. 本讲目标

上一讲我们让 `pi_controller` 输出了转子直角坐标系下的电压指令 \(V_d/V_q\)。本讲解决一个问题：**怎样把这一对直角坐标电压，变成 SVPWM 能直接使用的形式，并最终旋转回定子坐标系去驱动电机？**

学完本讲你应当能够：

- 说清作者为何让 SVPWM 吃「极坐标」输入，而不是更直观的 αβ 直角坐标输入——理解「反 Park 在极坐标下几乎免费」这一架构取舍。
- 读懂 `cartesian2polar.v` 用 `cnt` 倒数计数、`accb/acca` 逐次逼近求 \(\arctan\) 的迭代算法，以及它如何同时算出幅值 \(\rho\)。
- 读懂 `foc_top.v` 里那段「反 Park」`always` 块：为何 \(V_{s\rho}=V_{r\rho}\)、\(V_{s\theta}=V_{r\theta}+\psi\)，以及它与初始化标定为何共用同一段代码。

## 2. 前置知识

在进入源码前，先用三段直觉把概念立起来。

**(1) 直角坐标 vs 极坐标。** 一个二维矢量既可以用 \((x,y)\) 表示，也可以用「幅值 \(\rho\) + 角度 \(\theta\)」表示，二者等价：

\[
\rho=\sqrt{x^2+y^2},\qquad \theta=\arctan(y/x)
\]

`cartesian2polar` 模块干的就是这件事：输入 16 位有符号 \((x,y)\)，输出 12 位幅值 \(\rho\) 和 12 位角度 \(\theta\)。难点在于 FPGA 没有现成的 `sqrt` 和 `arctan`，必须用迭代算法逼近。

**(2) 转子极坐标 vs 定子极坐标。** 回顾 u2-l1：dq 是与转子同转的「转子直角坐标系」，αβ 是与定子固连的「定子直角坐标系」，二者相差一个电角度 \(\psi\)。同理，电压矢量既可以表达在转子极坐标 \((V_{r\rho},V_{r\theta})\)，也可以表达在定子极坐标 \((V_{s\rho},V_{s\theta})\)。**旋转不改变幅值**，只改变角度；而且转子坐标系是定子坐标系旋转 \(\psi\) 得到的，所以从转子极坐标回到定子极坐标，只需把角度加上 \(\psi\)：

\[
V_{s\rho}=V_{r\rho},\qquad V_{s\theta}=V_{r\theta}+\psi
\]

这就是「反 Park 变换」在极坐标下的全部内容——一次加法、一次直通。这也是本讲最关键的一句话。

**(3) 逐次逼近求比值。** `cartesian2polar` 求 \(\theta=\arctan(y/x)\) 的核心，是先把矢量「扳」到第一象限的 45° 扇区内（保证 \(|y|\le|x|\)，于是比值 \(r=|y|/|x|\in[0,1]\)），再用一种类似「二进制天平」的办法逐位逼近这个比值：从大到小依次试探一系列权重，若加上后总量仍不超标就保留，否则丢弃。最终累加结果就编码了比值 \(r\)，再查一张小 ROM 把比值映射成角度。这种思路是 CORDIC 的近亲，但用「查表 + 逐次逼近」替代了传统的旋转迭代。

> 术语提示：本讲里「第一扇区/第一八分圆」指角度落在 \([0,\pi/4]\) 内（\(|y|\le|x|\)）的区域；角度单位沿用全库约定——\(0\sim4095\) 对应 \(0\sim2\pi\)，故 \(\pi/4=512\)，\(\pi/2=1024\)，\(\pi=2048\)。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [RTL/foc/cartesian2polar.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v) | 直角坐标 \((x,y)\) → 极坐标 \((\rho,\theta)\) | 迭代算法、象限还原、幅值修正 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | FOC 顶层 | 例化 `cartesian2polar`；反 Park `always` 块 |
| [SIM/tb_svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v) | 仿真 testbench | 用 `sincos` 当信号源验证 \(\rho\) 与 \(\theta\) |

数据流定位（承接 u2-l5）：PI 输出 \(V_d/V_q\) → `cartesian2polar` 算出转子极坐标 \((V_{r\rho},V_{r\theta})\) → `foc_top` 反 Park 旋转到定子极坐标 \((V_{s\rho},V_{s\theta})\) → 下一讲的 `svpwm` 生成三相 PWM。本讲覆盖中间两步。

## 4. 核心概念与源码讲解

### 4.1 cartesian2polar：直角坐标转极坐标

#### 4.1.1 概念说明

PI 控制器给出的是 \(V_d/V_q\)——转子直角坐标系下的两个电压分量。但本项目的 SVPWM（下一讲详讲）被设计成吃「极坐标」输入 \((V_{s\rho},V_{s\theta})\)。于是存在一个「形状不匹配」：PI 吐直角坐标，SVPWM 吃极坐标。`cartesian2polar` 就是填这个坑的适配器。

**为什么不让 SVPWM 直接吃直角坐标？** 这正是作者的架构取舍，值得想清楚：

- 若 SVPWM 吃直角 αβ，则反 Park 必须做完整的三角乘法：\(V_\alpha=V_d\cos\psi - V_q\sin\psi\)、\(V_\beta=V_d\sin\psi + V_q\cos\psi\)，需要再查一次 sin/cos、做四次乘法。
- 若 SVPWM 吃极坐标，则反 Park 退化为 **\(V_{s\rho}=V_{r\rho},\ V_{s\theta}=V_{r\theta}+\psi\)**——一次加法即可（见 4.2）。代价仅仅是多一个 `cartesian2polar` 模块。
- 附带好处：**开环初始化极其简单**。标定初始机械角度 Φ 时，只需令 \(V_{s\rho}\) 取最大、\(V_{s\theta}=0\)，就能发出一个「角度为 0、幅值最大」的电压矢量，把转子拽到电角度 0 处（见 4.2.3）。若 SVPWM 吃直角坐标，发出「角度 0」矢量同样容易，但极坐标的语义更直白。

所以「多一个直角转极坐标模块」换来「反 Park 几乎免费 + 开环更直观」，是一笔划算的交易。

#### 4.1.2 核心流程

模块用 `cnt` 倒数计数驱动一个 30 拍左右的流水线，分六步完成一次转换：

1. **空闲采样（cnt==0）**：锁存输入 \((i_x,i_y)\)，记录符号 `signx/signy`，求绝对值 `absx/absy`；若 `i_en` 有效则 `cnt<=30` 启动转换。
2. **扳到第一扇区（cnt==30）**：若 \(|x|<|y|\) 则交换两者并置 `signxy=1`，保证后续都在 \([0,\pi/4]\) 内处理。
3. **逐次逼近求比值（cnt==29..5，共 25 拍）**：用「天平法」从大到小试探权重，累加得到 `accb`（逼近目标 32768）和与之同比例的 `acca`（编码了比值 \(r\)）。
4. **查 ROM（cnt==4）**：以 `acca[14:3]` 为地址查 `rom_theta`（角度）和 `rom_a`（幅值修正系数），并做边界钳位。
5. **象限还原（cnt==3, cnt==2）**：依次按 `signxy`、`signx` 把第一扇区的角度反射回正确象限。
6. **输出（cnt==1）**：按 `signy` 处理正负，置 `o_en=1`，输出 `o_rho`（饱和到 12 位）和 `o_theta`。

关键数学关系（推导见 4.1.3）：

\[
\text{acca}\approx 32768\cdot r,\qquad r=\frac{\min(|x|,|y|)}{\max(|x|,|y|)}\in[0,1]
\]

\[
\rho = |x|_{\max}\cdot\frac{1024+a}{1024},\qquad a\approx 1024\bigl(\sqrt{1+r^2}-1\bigr)
\]

于是 \(\rho=|x|_{\max}\sqrt{1+r^2}=\sqrt{|x|^2+|y|^2}\)，正是矢量长度。

#### 4.1.3 源码精读

模块端口与内部寄存器见 [cartesian2polar.v:L9-L31](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L9-L31)：输入 16 位有符号 `i_x/i_y`，输出 12 位无符号 `o_rho/o_theta` 和脉冲 `o_en`。`ATTENUAION` 参数（注意作者拼写）可在幅值上再做右移衰减，`foc_top` 中保持默认 0。

主算法在 [cartesian2polar.v:L38-L91](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L38-L91) 这一个 `always` 块里。逐段看：

**① 空闲采样**（[L46-L54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L46-L54)）：

```verilog
if(cnt==5'd0) begin
    accb <= 0;  acca <= 0;
    signx <= (i_x < $signed(16'd0));
    signy <= (i_y < $signed(16'd0));
    absx  <= (i_x < $signed(16'd0)) ? -i_x : i_x;
    absy  <= (i_y < $signed(16'd0)) ? -i_y : i_y;
    if(i_en) cnt <= 5'd30;
end
```

记录两轴符号、取绝对值，把矢量暂存到「第一象限」。`i_en` 有效才启动。

**② 扳到第一扇区**（[L57-L66](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L57-L66)）：

```verilog
if(cnt>=5'd30) begin
    signxy <= absx < absy;
    if(absx < absy) begin
        absx <= absy;                 // 大的当 x
        smtb <= {absy, 12'h0};        // smtb = 大值 << 12
        smta <= {absx, 12'h0};        // smta = 小值 << 12
    end else begin
        smtb <= {absx, 12'h0};
        smta <= {absy, 12'h0};
    end
end
```

若 \(|x|<|y|\)（角度在 \([\pi/4,\pi/2]\)）就交换，并记 `signxy=1`。交换后 `absx` 恒为较大者，比值 \(r=\text{小}/\text{大}\le1\)。`smtb/smta` 是后续逐次逼近的「砝码」，初值都左移 12 位（即放大 \(2^{12}\) 倍），目的是给后续 25 次右移留足精度。

**③ 逐次逼近**（[L67-L73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L67-L73)）：

```verilog
end else if(cnt>5'd4) begin
    if( accb + smtb <= 28'h8000 ) begin   // 目标 32768
        accb <= accb + smtb;               // 分母累加器
        acca <= acca + smta[15:0];         // 分子累加器（同比例）
    end
    smtb <= smtb >> 1;                     // 砝码减半
    smta <= smta >> 1;
end
```

这是算法的心脏。`accb` 是「分母通道」，目标是逼近 `28'h8000`=32768；`acca` 是「分子通道」，与 `accb` 用**同一组选中位**累加，只是把 `smtb` 换成了同比例缩放的 `smta`。每拍砝码右移一位（权重减半）。

推导：设选中位的权重和为 \(S=\sum_{k\in\text{选中}}2^{12-k}\)，则

\[
\text{accb}\approx |x|_{\max}\cdot S\approx 32768,\qquad
\text{acca}\approx |y|_{\min}\cdot S = |y|_{\min}\cdot\frac{32768}{|x|_{\max}}=32768\cdot r
\]

也就是说 `acca` 把比值 \(r\) 编码进了 \([0,32768]\) 的定点数。由于 \(r\le1\)，`acca` 恰好不溢出 16 位。

> 为什么前若干拍什么也不加？因为初始 `smtb`=大值\(\ll12\) 远大于 32768，条件不满足；直到砝码右移到 \(\le32768\) 才开始累加。这等价于自动对齐了比值的小数点位置，对任意幅值都自适应。

**④ 查 ROM**（[L74-L76](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L74-L76)）：

```verilog
end else if(cnt==5'd4) begin
    a <= acca[15] ? 9'd424 : rom_a;
    theta <= (acca[15:3]>=13'd4090) ? 12'd512 : {3'b0,rom_theta};
end
```

`rom_a` 和 `rom_theta` 由另一个组合 `always` 块（[L93-L4191](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L93-L4191)，4096 项 `case`）以 `acca[14:3]` 为地址给出——本质上就是一张 \(\arctan\) 表和一张幅值修正表。`acca[15:3]>=4090`（即 \(r\) 极接近 1，角度接近 \(\pi/4\)）时把 `theta` 钳到 512；`acca[15]` 置位（\(r\ge1\) 边界）时 `a` 取满量程 424。可对照 ROM 末行 [L4190](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L4190)：`12'd4095:{rom_a,rom_theta}<={9'd424,9'd511};`——\(r=1\) 处 \(a=424\)、\(\theta=511\)，正好对应 \(\pi/4\)。

校验 \(a\) 的含义：\(\sqrt{1+r^2}\) 在 \(r=1\) 时为 \(\sqrt2\approx1.4142\)，\(1024\times(\sqrt2-1)\approx424.1\)，与 ROM 存的 424 吻合。同理首行 [L95](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L95) `12'd0:{rom_a,rom_theta}<={9'd0,9'd0};` 对应 \(r=0\)（角度 0，无修正）。

**⑤ 象限还原**（[L77-L84](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L77-L84)）：

```verilog
end else if(cnt==5'd3) begin
    if(signxy) theta <= 12'd1024 - theta;   // π/2 - θ  ：撤回交换
end else if(cnt==5'd2) begin
    amp <= ampatt_w;
    if(signx) theta <= 12'd2048 - theta;     // π - θ    ：x<0
end
```

- `signxy`：若当初交换过（原角度在 \([\pi/4,\pi/2]\)），算出的是 \(\pi/2-\alpha\)，故 \(\alpha=\pi/2-\text{theta}\)，即 `1024-theta`。
- `signx`：若原 \(x<0\)（左半平面），角度再反射为 \(\pi-\text{theta}\)，即 `2048-theta`。

**⑥ 幅值与输出**（[L33-L36](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L33-L36) 与 [L85-L89](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L85-L89)）：

```verilog
wire [23:0] mul    = {15'd0,a} * {8'd0,absx};          // a * absx
wire [15:0] amp_w  = {2'b0,mul[23:10]} + absx;         // absx*(a/1024) + absx
wire [15:0] ampatt_w = amp_w >> ATTENUAION;
...
o_en <= 1'b1;
o_rho   <= amp>16'd4095 ? 12'd4095 : amp[11:0];        // 饱和到 12 位
o_theta <= signy ? 12'd0-theta : theta;                // y<0 ：取负（即 4096-theta）
```

幅值 \(\rho=\text{absx}\cdot(1024+a)/1024=\text{absx}\cdot\sqrt{1+r^2}=\sqrt{|x|^2+|y|^2}\)（`absx` 是交换后的较大者），与符号无关。最后 `signy` 处理下半平面（\(y<0\) 时角度取负，等价于 `4096-theta` mod 4096）。`o_rho` 饱和到 4095，防止超出 12 位。

一次转换的总延迟约为 30 个时钟周期（`cnt` 从 30 递减到 1）。在 `foc_top` 里 `i_en` 恒为 1（见 4.2.3），所以模块一直在用最新的 \((V_d,V_q)\) 反复转换，远快于 2048 拍的控制周期。

#### 4.1.4 代码实践

**实践目标**：用 `tb_svpwm.v` 验证 `cartesian2polar` 的正确性——输入一个幅值恒定、角度扫描的矢量，确认输出 \(\rho\) 近似常数、\(\theta\) 近似输入角度，并搞清 `signxy` 交换的作用。

**操作步骤**：

1. 阅读 [SIM/tb_svpwm.v:L33-L52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L33-L52)。它借 `sincos` 模块生成 \(x=16384\cos\theta\)、\(y=16384\sin\theta\)，再各自除以 5 喂给 `cartesian2polar`：

   ```verilog
   .i_x ( x / 16'sd5 ),   // ±3277 的余弦波
   .i_y ( y / 16'sd5 ),   // ±3277 的正弦波
   .o_rho ( rho ),        // 应近似常数 3277
   .o_theta ( phi )       // 应近似 θ
   ```

2. 在 `SIM/` 目录运行（Windows 双击 `.bat`，Linux 用等价命令）：

   ```bash
   cd SIM
   iverilog -g2001 -o sim.out tb_svpwm.v ../RTL/foc/sincos.v ../RTL/foc/cartesian2polar.v ../RTL/foc/svpwm.v
   vvp -n sim.out
   gtkwave dump.vcd
   ```

   （脚本见 [tb_svpwm_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm_run_iverilog.bat)。）

3. 在 gtkwave 中把 `tb_svpwm/theta`（输入）、`tb_svpwm/phi`（输出角度）、`tb_svpwm/rho`（输出幅值）都设为 Signed Decimal → Analog → Step 显示。`theta` 的扫描循环见 [tb_svpwm.v:L71-L75](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L71-L75)。

**需要观察的现象**：

- `rho` 应是一条近似常数的直线，稳态值在 3277 附近（允许低位抖动，因为定点逐次逼近有量化误差）。
- `phi` 应跟随 `theta` 变化，二者曲线基本重合（注意 `phi` 是 `cartesian2polar` 转换完才更新，会比 `theta` 滞后约 30 拍）。
- 当 `theta` 跨过 \(\pi/4=1024\)、\(\pi/2=2048\) 等边界时，`phi` 仍应连续跟踪，说明象限还原正确。

**预期结果（推导）**：输入 \((3277\cos\theta,\ 3277\sin\theta)\)，幅值 \(\rho=\sqrt{(3277\cos\theta)^2+(3277\sin\theta)^2}=3277\)，与 \(\theta\) 无关；输出角度 \(\phi\approx\theta\)。若在示波器里看到 `rho` 在某些角度出现塌陷，多半是把信号设成了 Unsigned Decimal 导致负半周失真——按 u1-l4 的办法改回 Signed 即可。

> 若本地未装 iverilog/gtkwave，可标注「待本地验证」；但应先完成源码层面的纸面推导，确认 \(\rho=3277\) 与 \(\phi=\theta\) 在数学上成立。

**解释 `signxy` 交换的作用**（本实践的另一半任务）：

- `cartesian2polar` 的逐次逼近只能在比值 \(r\le1\)（即第一扇区 \([0,\pi/4]\)）内工作，否则 `acca` 会超过 32768、ROM 地址越界、角度无定义。
- 所以代码在 [L57-L66](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L57-L66) 先判断 \(|x|<|y|\)，若成立就把两轴交换，保证后续 `absx` 恒为较大者、\(r\le1\)；并用 `signxy` 记下「是否交换过」。
- 交换会把原角度 \(\alpha\in[\pi/4,\pi/2]\) 变成 \(\pi/2-\alpha\in[0,\pi/4]\)，所以查完 ROM 后在 [L78-L79](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L78-L79) 用 `theta=1024-theta` 撤回。这样整个 \([0,\pi/2]\) 范围都能高分辨率处理，再配合 `signx/signy` 覆盖全圆。

#### 4.1.5 小练习与答案

**练习 1**：若把 [L68](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v#L68) 的比较目标从 `28'h8000` 改成 `28'h4000`，`acca` 的含义会变成什么？`o_theta` 还正确吗？

**答案**：`acca` 会变成 \(\approx 16384\cdot r\)（目标减半，累加和减半）。由于 ROM 地址取 `acca[14:3]`（即 `acca/8`），地址也会减半，查出的 `rom_theta` 对应的是一半的比值，导致 `o_theta` 系统性偏小。所以不能随意改——目标、地址取位、ROM 内容三者是配套标定好的。

**练习 2**：为什么幅值修正系数 `a` 在 \(r=0\) 时为 0、在 \(r=1\) 时为 424？请用 \(\rho=\sqrt{x^2+y^2}\) 推导。

**答案**：\(\rho=|x|_{\max}\sqrt{1+r^2}\)，故修正因子 \(\sqrt{1+r^2}\)。代码用 \((1024+a)/1024\) 近似它，即 \(a\approx1024(\sqrt{1+r^2}-1)\)。\(r=0\Rightarrow a=0\)；\(r=1\Rightarrow a\approx1024(\sqrt2-1)\approx424\)。

**练习 3**：`cnt` 从 30 递减到 1 共 30 拍，但其中只有 `cnt==29..5` 这 25 拍用于逐次逼近。其余 5 拍（30、4、3、2、1）分别在干什么？

**答案**：`cnt==30` 做交换与砝码初始化；`cnt==4` 查 ROM 锁存 `a/theta`；`cnt==3` 做 `signxy` 反射；`cnt==2` 算幅值并做 `signx` 反射；`cnt==1` 做 `signy` 反射并置 `o_en` 输出。

### 4.2 foc_top：反 Park 变换与初始化标定

#### 4.2.1 概念说明

`cartesian2polar` 输出的是**转子极坐标** \((V_{r\rho},V_{r\theta})\)——因为它吃的是转子直角坐标 \(V_d/V_q\)，只是换了表示形式，参考系没变。但 SVPWM 要的是**定子极坐标** \((V_{s\rho},V_{s\theta})\)，因为三相 PWM 是相对于定子物理绕组发波的。这中间差的那一步，就是「反 Park 变换」：把矢量从转子坐标系旋转回定子坐标系。

回顾 u2-l4：正向 Park 把 αβ（定子）旋转 \(-\psi\) 到 dq（转子）。反 Park 是它的逆，旋转 \(+\psi\) 回去。在直角坐标下，这需要 \(V_\alpha=V_d\cos\psi-V_q\sin\psi\) 等四次三角乘法。**但作者把反 Park 放在极坐标下做**，于是：

- 幅值旋转不变：\(V_{s\rho}=V_{r\rho}\)，直接一根线连过去。
- 角度叠加：\(V_{s\theta}=V_{r\theta}+\psi\)，一次加法。

这就是 4.1.1 说的「反 Park 几乎免费」的落脚点。在 `foc_top.v` 里，这段反 Park 逻辑和一个「初始化标定」逻辑合并写在**同一个** `always` 块中——因为初始化阶段也要直接给 \((V_{s\rho},V_{s\theta})\) 赋值（发出最大幅值、角度 0 的矢量去拽转子），二者天然共享输出寄存器。

#### 4.2.2 核心流程

`foc_top.v` 中 [L218-L237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237) 的 `always` 块用 `init_cnt` 与 `INIT_CYCLES` 比较来区分两阶段：

1. **初始化阶段**（`init_cnt <= INIT_CYCLES`）：
   - 令 \(V_{s\rho}=4095\)（最大幅值）、\(V_{s\theta}=0\)（角度 0）→ 发出一个指向电角度 0 的最大电压矢量 → 转子被电磁力拽到 \(\psi=0\) 处对齐。
   - `init_cnt` 每拍 +1。当 `init_cnt==INIT_CYCLES` 时，锁存当前机械角度 `phi` 为初始机械角度 \(\Phi\)（即 u2-l2 里的标定），并置 `init_done=1`。
2. **运行阶段**（`init_cnt > INIT_CYCLES`）：
   - 反 Park：\(V_{s\rho}\leftarrow V_{r\rho}\)，\(V_{s\theta}\leftarrow V_{r\theta}+\psi\)。
   - `init_done` 此后恒为 1，它同时作为所有子模块的 `rstn`（见 u2-l1），让整条流水线在标定完成后同步解复位。

注意 `init_done` 是异步复位的来源：标定完成前 `init_done=0`，`cartesian2polar`、`clark_tr` 等子模块都处于复位态；标定完成那一拍 `init_done` 翻 1，全链路同时启动。这保证了 \(\Phi\) 先确定、反 Park 后工作，时序上不会错乱。

#### 4.2.3 源码精读

`cartesian2polar` 的例化见 [foc_top.v:L200-L209](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L200-L209)：

```verilog
cartesian2polar u_cartesian2polar (
    .rstn  ( init_done ),
    .clk   ( clk      ),
    .i_en  ( 1'b1     ),     // 恒使能：一转换完就立刻采下一组 Vd/Vq
    .i_x   ( vd       ),     // Vd
    .i_y   ( vq       ),     // Vq
    .o_en  (          ),     // 不接：foc_top 不需要脉冲，直接用 vr_rho/vr_theta
    .o_rho ( vr_rho   ),     // Vrρ
    .o_theta ( vr_theta )    // Vrθ
);
```

要点：`i_en` 恒为 1，所以模块「永远在转换」——每次 `cnt` 归零就抓最新的 \(V_d/V_q\) 重算，输出 \((V_{r\rho},V_{r\theta})\) 以约 30 拍的节拍刷新；`o_en` 悬空（`foc_top` 信任数据始终有效）。`rstn` 接 `init_done`，标定前保持复位。`ATTENUAION` 未覆盖，取默认 0。

反 Park 与初始化的 `always` 块见 [foc_top.v:L218-L237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237)：

```verilog
always @ (posedge clk or negedge rstn)
    if(~rstn) begin
        {vs_rho, vs_theta} <= 0;  init_cnt <= 0;  init_phi <= 0;  init_done <= 1'b0;
    end else begin
        if(init_cnt<=INIT_CYCLES) begin            // 初始化未完成
            vs_rho <= 12'd4095;                    //   Vsρ 取最大
            vs_theta <= 12'd0;                     //   Vsθ = 0
            init_cnt <= init_cnt + 1;
            if(init_cnt==INIT_CYCLES) begin        //   即将完成
                init_phi <= phi;                   //   锁存 Φ
                init_done <= 1'b1;                 //   解复位全链路
            end
        end else begin                             // 初始化完成 → 反Park
            vs_rho <= vr_rho;                      //   Vsρ = Vrρ （幅值旋转不变）
            vs_theta <= vr_theta + psi;            //   Vsθ = Vrθ + ψ （角度叠加）
        end
    end
```

逐行对照概念：

- [L226-L227](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L226-L227)：初始化期发出 \((4095,0)\) 的定子极坐标电压矢量——这正是 4.1.1 说的「极坐标让开环更直观」的体现，角度 0 直接写 0 即可。
- [L230](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L230)：在初始化的最后一拍锁存 \(\Phi\)。此时转子已被拽到 \(\psi=0\)，机械角度 `phi` 即为「电角度 0 对应的机械角度」。
- [L234-L235](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L234-L235)：反 Park 的全部——幅值直通、角度加 \(\psi\)。`psi` 即 [L50](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L50) 的电角度寄存器（由 u2-l2 的角度换算块产生）。`vr_theta + psi` 是 12 位无符号加法，自然 mod 4096 完成 \(2\pi\) 回绕。

> 为什么 `vs_theta` 用 `reg` 而非 `wire`？因为它在 `always` 块里被赋值（[L63-L64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L63-L64) 声明为 `reg`）。这承接 u1-l3 的结论：`reg`/`wire` 的选择取决于驱动方式（`always` vs `assign`），与综合后是触发器还是连线无关——不过这里它确实是触发器（带时钟赋值）。

#### 4.2.4 代码实践

**实践目标**：通过阅读和局部修改，确认反 Park 的两条等式成立，并理解初始化与反 Park 为何能共用 `vs_rho/vs_theta` 寄存器。

**操作步骤**：

1. **静态追踪**：在 [foc_top.v:L200-L237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L200-L237) 中标出数据通路：`vd/vq`（PI 输出）→ `cartesian2polar` → `vr_rho/vr_theta` → 反 Park `always` → `vs_rho/vs_theta` → `svpwm`（[L246-L256](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L246-L256)）。确认 `vs_rho/vs_theta` 只在这一个 `always` 块里被写。
2. **纸面验证反 Park**：设某时刻 \(V_d=1000,\ V_q=2000,\ \psi=1024\)（即 \(\pi/2\)）。先按定义算 \(V_{r\rho}=\sqrt{1000^2+2000^2}\approx2236\)、\(V_{r\theta}=\arctan(2000/1000)\approx1024\)（约 \(\pi/4\)，即 512）——注意此处仅做数量级直觉核对，不必精确。再按反 Park：\(V_{s\rho}\approx2236\)、\(V_{s\theta}=512+1024=1536\)。观察代码 [L234-L235](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L234-L235) 就是这两步。
3. **思考实验（不必真改源码）**：若把 [L235](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L235) 改成 `vs_theta <= vr_theta - psi`（减号），电机会怎样？
   - **预期结果**：反 Park 旋转方向反了，等效于给了一个角度为 \(-\psi\) 的偏置。电机表现为「正向电流却产生反向力矩」或无法稳定对齐、抖动甚至失步。这正是 `ANGLE_INV` 装反应付之外的另一处方向敏感点。

**需要观察的现象**：第 1 步应确认 `vs_rho/vs_theta` 是「单写者」；第 2 步应确认反 Park 在极坐标下确实只需「直通 + 加法」；第 3 步应意识到 `+psi` 这个符号与电机转向强相关。

> 待本地验证项：若本地有 `tb_svpwm` 仿真环境，可临时构造一个固定 \((V_d,V_q)\) 与扫描 \(\psi\) 的极简 testbench，观察 `vs_theta` 是否等于 `vr_theta+psi`。本项目未提供该 testbench，故标注待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cartesian2polar` 的 `i_en` 在 `foc_top` 里接 `1'b1`，而 `clark_tr`/`park_tr` 的 `i_en` 却接上一级的脉冲（`en_iabc`/`en_ialphabeta`）？

**答案**：`clark_tr`/`park_tr` 处理的是「事件型」数据（每个控制周期采一次电流，要用脉冲精确对齐节拍，避免算到旧数据）；而 `cartesian2polar` 处理的是 `vd/vq`——PI 的输出本身已经按控制周期更新，且模块内 `cnt==0` 时才会重新采样，所以让它「恒使能、转完就再转」即可，输出以约 30 拍自刷新，既不会错过 `vd/vq` 的更新，也不需要上游脉冲。

**练习 2**：反 Park 等式 \(V_{s\theta}=V_{r\theta}+\psi\) 中，\(\psi\) 是 12 位无符号、\(V_{r\theta}\) 也是 12 位无符号，加法溢出（>4095）时会发生什么？这对控制有影响吗？

**答案**：12 位加法自然截断高位，等价于 mod 4096，正好对应角度 mod \(2\pi\)——角度是周期量，回绕是完全正确的，对控制无影响。这也是角度信号全库用 12 位无符号的原因。

**练习 3**：初始化阶段直接令 `vs_rho=4095, vs_theta=0`，这相当于让 SVPWM 发出什么样的电压矢量？为什么这能把转子拽到 \(\psi=0\)？

**答案**：发出一个幅值最大、角度为 0（指向 A 相绕组方向，即定子电角度 0 方向）的电压矢量。它产生定子磁场指向 \(\psi=0\)，转子磁钢会与该磁场对齐，于是转子被强制转到电角度 0 处。此时读到的机械角度就是 \(\Phi\)。这也是极坐标输入给 SVPWM 带来的便利——「角度 0」直接写 0 即可，无需算 sin/cos。

## 5. 综合实践

把本讲两块知识串起来，完成一个「数据流标注 + 推理」任务：

1. 在 [foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) 中，从 `u_id_pi`/`u_iq_pi` 的 `o_value`（即 `vd`/`vq`）出发，一路画到 `u_svpwm` 的 `v_rho`/`v_theta` 输入，标出中间经过的每一个信号名和模块：
   - `vd/vq` → `u_cartesian2polar`（[L200](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L200)）→ `vr_rho/vr_theta` → 反 Park `always`（[L234-L235](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L234-L235)）→ `vs_rho/vs_theta` → `u_svpwm`（[L250-L251](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L250-L251)）。
2. 在这条链上标注每一段的「坐标系」：`vd/vq` 是转子直角坐标，`vr_*` 是转子极坐标，`vs_*` 是定子极坐标。指出「直角↔极坐标」「转子↔定子」这两次变换分别发生在哪一步。
3. 回答：整条链从 `vd/vq` 更新到 `vs_*` 更新，粗略经历多少个时钟周期？（提示：`cartesian2polar` 约 30 拍，反 Park 1 拍。）相对 2048 拍的控制周期，这是否构成瓶颈？
4. 进阶：如果改用「直角坐标输入的 SVPWM」，这条链需要删掉/新增什么？反 Park 那一步会变成什么运算？

**预期结论**：坐标系两次变换分别在 `cartesian2polar`（直角→极坐标，仍在转子系）和反 Park `always`（转子→定子，极坐标下做角度加法）；整链约 31 拍，远小于 2048，不是瓶颈；若改直角 SVPWM，可删掉 `cartesian2polar`，但反 Park 必须升级为 sin/cos 四次乘法，且初始化发「角度 0 矢量」也要重新算 \(V_\alpha,V_\beta\)——这正是作者选极坐标的核心理由。

## 6. 本讲小结

- `cartesian2polar` 把 \((V_d,V_q)\) 转成转子极坐标 \((V_{r\rho},V_{r\theta})\)：先取绝对值并交换到第一扇区（`signxy`），再用 `accb/acca` 逐次逼近编码比值 \(r\)，查 ROM 得 \(\theta=\arctan(r)\) 与幅值修正系数 \(a\)，最后按 `signxy/signx/signy` 三步反射回正确象限。
- 幅值 \(\rho=|x|_{\max}\cdot(1024+a)/1024=\sqrt{x^2+y^2}\)，修正系数 \(a\approx1024(\sqrt{1+r^2}-1)\)，在 \(r=0\) 时为 0、\(r=1\) 时为 424，与 ROM 首末两行精确吻合。
- 作者让 SVPWM 吃极坐标输入，换来反 Park 退化为「幅值直通 + 角度加 \(\psi\)」的一次加法，代价是多一个 `cartesian2polar`；同时初始化发「角度 0、幅值最大」的矢量变得直白。
- `foc_top` 用同一个 `always` 块复用 `vs_rho/vs_theta`：初始化期写 \((4095,0)\) 拽转子到 \(\psi=0\) 并锁存 \(\Phi\)；运行期写反 Park 的 \((V_{r\rho},\ V_{r\theta}+\psi)\)。
- `init_done` 既是初始化结束标志，又是所有子模块的 `rstn`，保证「先标定 \(\Phi\)、再启动反 Park」的时序。
- 一次 `cartesian2polar` 转换约 30 拍，`i_en` 恒为 1 使其自刷新，远快于 2048 拍控制周期，不构成瓶颈。

## 7. 下一步学习建议

下一讲 **u2-l7 SVPWM 调制器 svpwm.v** 将接住本讲输出的 \((V_{s\rho},V_{s\theta})\)，讲清七段式 SVPWM 如何把定子极坐标电压矢量变成三路中心对齐 PWM。建议阅读：

- [RTL/foc/svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v)：关注 2048 周期计数器 `cnt`、马鞍波 duty 的查表与乘法、以及 `v_amp`（`MAX_AMP`）如何缩放本讲输出的 \(\rho\)。
- 回顾本讲的 \((V_{s\rho},V_{s\theta})\) 含义：它是 SVPWM 的直接输入，理解它的极坐标语义是读懂下一讲的前提。
- 若对逐次逼近算法意犹未尽，可对比阅读 `sincos.v` 的状态机查表法——同是「用查表替代硬算」，思路与 `cartesian2polar` 的 ROM 互补。
