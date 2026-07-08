# SVPWM 调制器 svpwm.v

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `svpwm.v` 如何用一个 11 位计数器 `cnt`（0~2047）定义一个 PWM 周期，并算出 PWM 频率 = `clk`/2048。
- 解释为什么所有“算占空比”的动作都挤在一个周期的最后 11 拍（`cnt` = 2037~2047）里完成，以及这套末段时序如何围绕 ROM 的 4 拍延迟 (`ROM_LATENCY`) 排布。
- 看懂三相马鞍波占空比 `pwma_duty/pwmb_duty/pwmc_duty` 是如何由“查表 (ROM) + 共享乘法器”分时得到，再映射成 `pwm_a/b/c` 的中心对齐 PWM。
- 推导 `pwm_a <= ~pwm_act || cnt<=pwma_lb || cnt>pwma_ub` 这种比较写法对应的占空比公式，并理解 `MAX_AMP` 为何会反过来限制采样窗口。

本讲只覆盖一个最小模块：**svpwm**。它承接 u2-l6 反 Park 变换输出的定子极坐标电压矢量 (Vsρ, Vsθ)，是整条 FOC 数据流的终点——把“想施加什么电压矢量”翻译成“三对 MOS 管在每个开关周期里导通多久”。

## 2. 前置知识

### 2.1 SVPWM 到底在干什么

电机有三相绕组，每相由一个半桥（上桥臂 + 下桥臂两个 MOS 管）驱动。我们最终能直接操纵的，只有每个半桥的输出端电平：上管导通（`pwm_x=1`）时该端点近似接到母线电压，下管导通（`pwm_x=0`）时近似接到地。

FOC 算出来的是“想施加的电压矢量” \((V_{s\rho}, V_{s\theta})\)（一个幅值 + 一个角度）。SVPWM（空间矢量脉宽调制）要解决的问题是：**在一个开关周期里，让三个半桥输出端电压的时间平均值，恰好合成出这个想要的电压矢量。**

直观理解：每个周期内，三相端点各自停留在“上管导通”状态的时间占比（占空比）不同，就能在三相线上产生不同的平均电压，三个平均电压的矢量合成就是我们想要的 \((V_{s\rho}, V_{s\theta})\)。随着 `Vsθ` 一个周期一个周期地旋转，三相占空比随之变化，端电压平均值就描出一个旋转的电压矢量，拖着电机的磁场转。

### 2.2 中心对齐 PWM 与“马鞍波”

如果直接把一个正弦波当成占空比给三相用，叫 SPWM，母线电压利用率不高。SVPWM 的常见实现会给三相同时注入一个“零序分量”，让三相占空比的波形从正弦变成**马鞍形（saddle）**——顶部和底部被削平。这种马鞍波的好处是：同样的母线电压能合成出更大的电压矢量，而且三相波形永远不超过 0%~100%。

本模块的做法很直接：把“第一象限内一段马鞍形曲线”预先采样存进一张 ROM 表，运行时按三相角度（互差 120°）去查表，乘上幅值，就得到三相马鞍波占空比。

### 2.3 极性约定

回顾 [RTL/foc/foc_top.v:33-35](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L33-L35) 的注释：`pwm_a/b/c = 1` 时**上桥臂**导通，`= 0` 时**下桥臂**导通；`pwm_en = 0` 时 6 个 MOS 管全部关断。本讲推导“占空比”时，指的都是**高电平（上桥臂导通）所占的时间比例**。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [RTL/foc/svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v) | 本讲主角，7 段式 SVPWM 调制器 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 例化 `svpwm` 的上层，提供 `(Vsρ, Vsθ)` 与 `MAX_AMP` |
| [SIM/tb_svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v) | 仿真：用 `sincos` 当信号源，验证马鞍波与占空比 |

## 4. 核心概念与源码讲解

### 4.1 cnt 计数器：一个 PWM 周期 = 2048 个时钟

#### 4.1.1 概念说明

SVPWM 是周期性的：每个开关周期做一次“查表→算占空比→比较输出”。本模块用一个自由运行的计数器 `cnt` 来定义这个周期。因为 `cnt` 是 11 位寄存器，它从 0 数到 2047 后自动回绕到 0，所以一个 PWM 周期正好是 2048 个 `clk`。

#### 4.1.2 核心流程

```
每个 posedge clk：
    cnt <= cnt + 1     （11 位自然回绕：2047 → 0）
PWM 周期 = 2048 个 clk
PWM 频率 = clk / 2048
例：clk = 36.864MHz → PWM ≈ 18kHz
```

这与 [RTL/foc/foc_top.v:21](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L21) 注释里的“控制频率 = 时钟频率 / 2048”完全一致——**采样率、PID 更新率、SVPWM 占空比更新率，三者被同一个 2048 计数器绑成同一个节拍**。

#### 4.1.3 源码精读

计数器本体非常简单（[RTL/foc/svpwm.v:44-48](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L44-L48)）：

```verilog
always @ (posedge clk or negedge rstn)
    if(~rstn)
        cnt <= 11'd0;
    else
        cnt <= cnt + 11'd1;
```

模块头注释也写明了频率关系（[RTL/foc/svpwm.v:10](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L10)）。

#### 4.1.4 代码实践

**目标**：验证“PWM 频率 = clk/2048”在仿真里成立。

**步骤**：
1. 打开 [SIM/tb_svpwm.v:18](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L18)，确认 `clk` 半周期 = 13563（单位 0.1ns，即 13.563ns 周期 → 36.864MHz）。
2. 在 SIM 目录运行 `tb_svpwm_run_iverilog.bat`（Linux 下等价命令见 u1-l4），生成 `dump.vcd`。
3. 用 gtkwave 打开，观察 `u_svpwm.cnt`：每经过 2048 个 `clk` 上升沿它回绕一次。
4. 把 `pwm_a` 设为二进制显示，测量相邻两次相同跳变沿之间相隔多少个 `clk` 周期。

**预期结果**：相邻周期 = 2048 个 `clk` = 2048 × 27.126ns ≈ 55.6µs，对应 ≈18kHz。

**说明**：本讲不假设你已运行；若未装 iverilog/gtkwave，结果标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习**：若把 `clk` 换成 100MHz，PWM 频率变成多少？电机啸叫通常出现在可听频段（<20kHz），这个频率是否合适？

**答案**：100MHz/2048 ≈ 48.8kHz。已高于人耳上限，不会引起可听啸叫；而且开关损耗会比 18kHz 更高。18kHz 处于可听边缘，是常见的折中。

---

### 4.2 末段时序：为什么所有计算都挤在 cnt=2037~2047

#### 4.2.1 概念说明

占空比每个 PWM 周期才更新一次。模块把“读取输入矢量 → 查表 → 乘法 → 形成占空比”这一整套运算，全部塞进每个周期的**最后 11 拍**（`cnt` = 2037~2047）里流水完成，算好的占空比刚好在 `cnt` 回绕到 0、新周期开始时生效。这样做的好处是：占空比在整整一个周期内保持稳定，比较器输出的是干净的中心对齐波形。

这套末段时序围绕一个关键参数 `ROM_LATENCY = 4`（[RTL/foc/svpwm.v:22](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L22)）排布：从把角度写进 `rom_x`，到 ROM 流水线吐出 `rom_y`，要经过 4 个时钟周期。所以输入必须在“需要用到结果的时刻”提前 4 拍锁存。

#### 4.2.2 核心流程（cnt=2037~2047 的取值序列）

下表把 [RTL/foc/svpwm.v:61-93](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L61-L93) 这段 if-else 链整理成时间线。记三相角度为 A/B/C，互差 120°（1365 ≈ 4096/3）：

| cnt 触发沿 | 动作 | 含义 |
|-----------|------|------|
| 2037 = 2041−4 | `rom_x←v_theta`; `mul_i1←v_amp`; `mul_i2←v_rho` | 锁存 A 相角度进 ROM 流水线；启动第一次乘法 v_amp×v_rho |
| 2038 = 2042−4 | `rom_x←rom_x−1365` | B 相角度（−120°）进 ROM 流水线 |
| 2039 = 2043−4 | `rom_x←rom_x−1365`; `mul_i2←mul_o+8` | C 相角度（−240°）进 ROM；幅值系数 A = (v_amp·v_rho)>>9 + 8 就绪 |
| 2040, 2041 | （无动作） | ROM 流水线正在 4 拍延迟里“烹饪”三个角度 |
| 2042 | `mul_i1←rom_y`; `sya←rom_sy` | A 相 ROM 结果到，启动 A 相乘法 |
| 2043 | `mul_i1←rom_y`; `syb←rom_sy` | B 相 ROM 结果到，启动 B 相乘法 |
| 2044 | `mul_i1←rom_y`; `syc←rom_sy`; `ya←mul_o[11:3]` | C 相 ROM 结果到；捕获 A 相占空比偏移 `ya` |
| 2045 | `yb←mul_o[11:3]` | 捕获 B 相偏移 `yb` |
| 2046 | `pwma/b/c_duty←512±ya/yb/...` | 一次性算出三相占空比 |
| 2047 | `pwma/b/c_lb,ub←±duty`; `pwm_act←1` | 算出比较上下界，激活输出 |
| 回绕→0 | 新周期开始，比较器用新 `lb/ub` | 占空比正式生效 |

注意乘法器是**分时复用**的：同一个 `mul_i1*mul_i2` 硬件，在 2042/2043/2044 连续三拍分别乘 A/B/C 相的 `rom_y`，结果隔一拍被 `ya/yb/duty` 收走。ROM 也是分时的：三个相角在 2037/2038/2039 连续进流水线，4 拍后在 2041/2042/2043 依次出来。

#### 4.2.3 源码精读

提前锁存输入的关键代码（[RTL/foc/svpwm.v:61-69](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L61-L69)）：

```verilog
if(cnt==11'd2041-ROM_LATENCY) begin       // cnt==2037
    rom_x <= v_theta;      // 提前 4 拍锁存，ROM 才能在 cnt==2042 给出结果
    mul_i1 <= v_amp;
    mul_i2 <= v_rho;
end else if(cnt==11'd2042-ROM_LATENCY) begin   // cnt==2038
    rom_x <= rom_x - 12'd1365;   // −120°，B 相
end else if(cnt==11'd2043-ROM_LATENCY) begin   // cnt==2039
    rom_x <= rom_x - 12'd1365;   // 再 −120°，C 相
    mul_i2 <= mul_o + 12'd8;     // 幅值系数 A 就绪（+8 为四舍五入）
end
```

三相占空比一次性生成（[RTL/foc/svpwm.v:83-93](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L83-L93)）：

```verilog
end else if(cnt==11'd2046) begin
    pwma_duty <= sya ? 10'd512-{1'b0,ya} : 10'd512+{1'b0,ya};   // 512 ± ya
    pwmb_duty <= syb ? 10'd512-{1'b0,yb} : 10'd512+{1'b0,yb};
    pwmc_duty <= syc ? 10'd512-{1'b0,mul_o[11:3]} : 10'd512+{1'b0,mul_o[11:3]};
end else if(cnt==11'd2047) begin
    pwma_lb <= 11'd0 + {1'b0, pwma_duty};   // lb  = +duty
    pwma_ub <= 11'd0 - {1'b0, pwma_duty};   // ub  = 2048−duty（11 位回绕）
    ...
    pwm_act <= 1'b1;                        // 激活：下个周期起输出有效
end
```

#### 4.2.4 代码实践（对应主任务的第二问）

**目标**：解释“为何在 `cnt = 2041 − ROM_LATENCY` 提前锁存 `v_theta/v_amp`”。

**步骤**：
1. 阅读 ROM 流水线四级寄存器 `x1 → x2/y3 → rom_y`（[RTL/foc/svpwm.v:120-148](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L120-L148)），数清楚从 `rom_x` 写入到 `rom_y` 有效要经过几个时钟沿。
2. 确认 `rom_y`（A 相）首次被使用的时刻是 `cnt==2042`（[RTL/foc/svpwm.v:70](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L70)）。
3. 反推：要在 `cnt==2042` 拿到 `rom_y`，必须提前 4 拍（`2042 − 4 = 2038` 边沿后 `rom_y` 才有效，即 2037 边沿写入 `rom_x`）。

**预期结果**：ROM 流水线级数 = 4（`rom_x→x1`、`x1→x2`、`x2→y3/rom_y` 的组合逻辑跨 4 个寄存器沿），与 `ROM_LATENCY=4` 严格对应。若把锁存推迟到 `cnt==2038`，则 `cnt==2042` 时 `rom_y` 还差一拍，A 相占空比会错位一个周期。**这就是“提前 `ROM_LATENCY` 拍锁存”的原因**。

> 待本地验证：可在仿真里把 `ROM_LATENCY` 临时改成 3 或 5（仅用于观察，勿提交），观察 `pwm_a` 是否发生相位错位。

#### 4.2.5 小练习与答案

**练习 1**：为什么三相角度用“减 1365”而不是“加 1365”？

**答案**：1365 ≈ 4096/3 对应 120°。加或减都只是相序方向的选择；这里用减，配合 `ANGLE_INV` 等上层方向约定，保证输出相序与电机绕组接线一致。换电机相序时可在上层处理，不必改本表。

**练习 2**：`mul_i2 <= mul_o + 12'd8` 里的 `+8` 是什么？

**答案**：`mul_o = (v_amp·v_rho)>>9`，丢掉了低 9 位小数。`+8`（即 `+0.5<<9` 的整数部分一半偏移）是在做**四舍五入**，把截断误差从最大 1 LSB 降到最大 0.5 LSB。

---

### 4.3 ROM 查表：从角度到每相调制值

#### 4.3.1 概念说明

`rom_y`（9 位，0~511）是某相在当前角度下的“调制值”，`rom_sy` 是它的符号。要查全圆 0~360°，但 ROM 只存了第一象限（0~90°）的曲线，其余象限靠折叠 + 符号反射还原。这与 u2-l4 里 `sincos.v` 的“只存第一象限、靠反射覆盖全圆”是同一种思想。

ROM 表 [RTL/foc/svpwm.v:150-1176](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L150-L1176) 的曲线**不是纯正弦**：它在第一象限内先升后降（约在 30° 处达到峰值 511，两端较低）。正是这种形状，使得三相查表结果在乘上幅值、中心对齐到 512 之后，自然呈现出 SVPWM 标志性的**马鞍波**，而无需额外的零序注入逻辑。

#### 4.3.2 核心流程（象限折叠）

```
rom_x (12bit, 0~4095 = 0~360°)
   │  取绝对值（关于 0°/180° 对称）：x1 = min(rom_x, 4096−rom_x)
   ▼
x1  (0~2048)
   │  关于 90° 折叠到第一象限：x2 = (x1<=1024)? x1 : 2048−x1
   ▼
x2  (0~1024 = 0~90°) ──查表──▶ y3  (0~511)
   │                              │
   │  s2/s3 记录原象限符号        │
   ▼                              ▼
rom_sy (符号)                 rom_y = y3 (幅值)
```

#### 4.3.3 源码精读

取绝对值与折叠（[RTL/foc/svpwm.v:120-135](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L120-L135)）：

```verilog
always @ (posedge clk)
    if(rom_x >= 12'd2048)  x1 <= 12'd0 - rom_x;   // 关于 180° 折叠
    else                   x1 <= rom_x;

always @ (posedge clk) begin
    z2 <= x1 == 12'd1024;                         // 恰为 90° 时输出为 0
    if(x1 <= 12'd1024) begin x2 <= x1[9:0];      s2 <= 1'b0; end  // 第一象限
    else                begin x2 <= 10'd0-x1[9:0]; s2 <= 1'b1; end // 折叠 + 记符号
end
```

符号打拍与查表输出（[RTL/foc/svpwm.v:142-148](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L142-L148)）：

```verilog
always @ (posedge clk) begin
    rom_sy <= s3;
    if(z3)  rom_y <= 9'd0;     // 90° 整点强制 0，避免查表索引越界
    else    rom_y <= y3;
end
```

ROM 表本体是一个 1024 项的 `case`（节选头部，[RTL/foc/svpwm.v:150-152](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L150-L152)）：

```verilog
always @ (posedge clk)
case(x2)
10'd0:   y3<=9'd442;
10'd1:   y3<=9'd443;
...
10'd370: y3<=9'd511;   // 峰值区
...
10'd1023:y3<=9'd1;
endcase
```

#### 4.3.4 代码实践

**目标**：在仿真里直接看到 `rom_y` 随角度变化的马鞍形。

**步骤**：运行 `tb_svpwm` 后，在 gtkwave 里把 `u_svpwm.rom_y`（先设为 Signed Decimal，再 Analog→Step）和 `u_svpwm.rom_x` 一起显示；`tb_svpwm.v` 里 `theta` 每隔 2048 个 `clk` 递增（[SIM/tb_svpwm.v:71-74](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L71-L74)），`v_theta` 跟着 `phi`（即 `cartesian2polar` 输出）旋转一周。

**预期结果**：`rom_y` 在一个电角度周期内出现一次先升后降的包络；三相 `rom_y`（A/B/C 因角度差 120° 而错相）合成的占空比呈马鞍形。

> 待本地验证。

#### 4.3.5 小练习与答案

**练习**：ROM 表为什么只存 0~1023 共 1024 项，却能覆盖 0~360° 全圆？

**答案**：因为 `rom_x` 先被折叠到 0~2048（关于 180° 对称），再折叠到 0~1024（关于 90° 对称），最终索引 `x2` 落在 0~1023；原角度所在的象限由 `s2/s3/rom_sy` 单独记录，用于决定占空比是 `512+ya` 还是 `512−ya`。所以 1024 项 + 一个符号位 = 全圆覆盖。

---

### 4.4 共享乘法器：从调制值到占空比偏移 ya/yb

#### 4.4.1 概念说明

`rom_y` 只是“单位幅值下的调制值”，真实占空比偏移还要乘上本次电压矢量的幅值。本模块**只用一个乘法器**，靠 4.2 节的分时调度，在三个时钟里依次算出 A/B/C 三相的偏移 `ya/yb/yc`。

幅值系数分两步算（也复用同一个乘法器）：
1. 先算 `v_amp × v_rho`，右移 9 位再加 8（四舍五入），得到幅值系数 `A`。
2. 再算 `rom_y × A`，右移 12 位得到 9 位偏移 `ya`（`ya = mul_o[11:3]`）。

其中 `v_amp = MAX_AMP = 384`（来自 [RTL/foc/foc_top.v:16](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L16)），是 SVPWM 允许的最大调制幅值。

#### 4.4.2 核心流程

```
v_amp(384) × v_rho(Vsρ) ──>>9──▶ A = mul_o + 8        （cnt==2039 完成）
rom_y × A            ──>>12──▶ ya/yb/yc = mul_o[11:3]  （cnt==2044/2045/2046 依次）
```

#### 4.4.3 源码精读

乘法器是组合 `wire`，结果寄存在 `mul_o`（[RTL/foc/svpwm.v:28-42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L28-L42)）：

```verilog
reg  [ 8:0] mul_i1;
reg  [11:0] mul_i2;
wire [20:0] mul_y = mul_i1 * mul_i2;   // 9bit × 12bit = 21bit
reg  [11:0] mul_o;
always @ (posedge clk or negedge rstn)
    if(~rstn) mul_o <= 12'd0;
    else      mul_o <= mul_y[20:9];     // 取高 12 位 = >>9
```

偏移捕获（[RTL/foc/svpwm.v:79-81](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L79-L81)）：

```verilog
ya <= mul_o[11:3];   // A 相：取 mul_o 的高 9 位（再 >>3）
...
yb <= mul_o[11:3];   // B 相
```

> 关键定点结论：`ya` 的最大值正好被 `MAX_AMP=384` 封顶（当 `v_rho` 与 `rom_y` 同时最大时 `ya≈384`）。所以占空比摆幅被限制在 \(512 \pm 384\)，详见 4.5。

#### 4.4.4 代码实践

**目标**：验证 `ya` 的最大值确实被 `MAX_AMP` 封顶。

**步骤**：
1. 在 `tb_svpwm.v` 中 `v_amp` 已固定为 `9'd384`（[SIM/tb_svpwm.v:57](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L57)），`v_rho` 来自 `cartesian2polar`，振幅约 3277（[SIM/tb_svpwm.v:50](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L50)）。
2. 仿真后把 `u_svpwm.ya` 设 Analog 显示，观察其峰值。

**预期结果**：`ya` 峰值 ≈ 384（而非更大），印证幅值被 `MAX_AMP` 限制。

> 待本地验证。

#### 4.4.5 小练习与答案

**练习**：为什么 `ya` 取 `mul_o[11:3]` 而不是整个 `mul_o`？

**答案**：`mul_o` 是 12 位（已 >>9），再取 `[11:3]` 等于再 >>3，总共相对原始 21 位乘积右移 12 位。这是为了把“9 位 rom_y × 12 位幅值系数”的结果重新标度回 9 位（0~511），正好能和 512 做加减生成 10 位占空比。多出来的位数被舍弃，误差由 FOC 的闭环 PI 吸收。

---

### 4.5 中心对齐 PWM 比较器：duty 如何变成 pwm_a

#### 4.5.1 概念说明

算出 `pwma_duty` 后，还要把它变成实际的引脚电平 `pwm_a`。本模块用**中心对齐**方式：在一个周期内，`pwm_a` 的高电平分布在两端、低电平集中在中间（或反之），这样三相的低电平脉冲都集中在周期中部，彼此重叠——这正是 u2-l8 `hold_detect` 能找到“三相下桥臂同时导通”采样窗口的原因。

#### 4.5.2 核心流程与数学

在 `cnt==2047` 拍算出比较上下界（11 位运算，注意 `pwma_ub` 是 `0−duty` 的回绕结果）：

\[
\text{pwma\_lb} = \text{pwma\_duty}, \qquad
\text{pwma\_ub} = 2048 - \text{pwma\_duty}
\]

输出比较（[RTL/foc/svpwm.v:105](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L105)）：

```verilog
pwm_a <= ~pwm_act || cnt<=pwma_lb || cnt>pwma_ub;
```

即 `pwm_a = 0`（下桥臂导通）仅当 `pwm_act` 有效 **且** `lb < cnt ≤ ub`。展开后，**下桥臂导通（低电平）的计数值**为：

\[
W_{\text{low}} = \text{pwma\_ub} - \text{pwma\_lb} = 2048 - 2\cdot\text{pwma\_duty}
\]

上桥臂导通（高电平）的计数值：

\[
W_{\text{high}} = 2048 - W_{\text{low}} = 2\cdot\text{pwma\_duty}
\]

所以高电平占空比：

\[
D_{\text{high}} = \frac{W_{\text{high}}}{2048} = \frac{\text{pwma\_duty}}{1024}
\]

**这就是主任务要找的对应关系：`pwma_duty` 越大，高电平占空比越大**（上桥臂导通越久）。`pwma_duty` 从 1 到 1023，占空比从 ~0.1% 到 ~99.9%；而 512 对应 50%。

再结合 4.4：`pwma_duty = 512 \pm ya`，`ya ∈ [0, 384]`，所以：

\[
D_{\text{high}} \in \left[\frac{512-384}{1024},\ \frac{512+384}{1024}\right] = [12.5\%,\ 87.5\%]
\`

也就是说，`MAX_AMP=384` 把三相占空比强行限制在 12.5%~87.5% 之间，**永远不会出现 0% 或 100%**。这保证每个半桥、每个周期都有最少约 12.5% 的下桥臂导通时间——三相下桥臂导通区间在周期中部必然存在重叠，给 ADC 留出采样窗口。这正是 [RTL/foc/foc_top.v:16](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L16) 注释所说“该值也不能太大，以保证 3 个下桥臂有足够的持续导通时间来供 ADC 进行采样”的数学来源，也是本模块与下一讲 `hold_detect` 的接口约定。

#### 4.5.3 源码精读

比较上下界与激活（[RTL/foc/svpwm.v:86-93](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L86-L93)）：

```verilog
end else if(cnt==11'd2047) begin
    pwma_lb <= 11'd0 + {1'b0, pwma_duty};   //  +duty
    pwma_ub <= 11'd0 - {1'b0, pwma_duty};   //  11 位回绕 = 2048−duty
    ... (b/c 相同理)
    pwm_act <= 1'b1;
end
```

三相输出比较（[RTL/foc/svpwm.v:97-108](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L97-L108)）：

```verilog
always @ (posedge clk or negedge rstn)
    if(~rstn) begin
        pwm_en <= 1'b0; pwm_a <= 1'b1; pwm_b <= 1'b1; pwm_c <= 1'b1;
    end else begin
        pwm_en <= pwm_act;
        pwm_a <= ~pwm_act || cnt<=pwma_lb || cnt>pwma_ub;
        pwm_b <= ~pwm_act || cnt<=pwmb_lb || cnt>pwmb_ub;
        pwm_c <= ~pwm_act || cnt<=pwmc_lb || cnt>pwmc_ub;
    end
```

复位时 `pwm_a/b/c=1`（默认上桥臂导通），但 `pwm_en=0` 关断总使能；直到第一个周期结束 `pwm_act=1`，`pwm_en` 才置 1，此后比较结果生效。

#### 4.5.4 代码实践（对应主任务的第一问）

**目标**：在仿真里验证“`pwma_duty` 越大，`pwm_a` 高电平占空比越大”。

**步骤**：
1. 运行 `tb_svpwm`，用 gtkwave 同时显示 `u_svpwm.pwma_duty`（Analog）和 `pwm_a`（二进制）。
2. 选两个不同时刻：一个 `pwma_duty` 较大、一个较小。
3. 对每个时刻，在完整的一个 2048 周期内数 `pwm_a=1` 的 `clk` 周期数，除以 2048。

**预期结果**：测得的高电平占空比 ≈ `pwma_duty/1024`。例如 `pwma_duty=512` 时占空比≈50%，`=800` 时≈78%。这直接印证公式 \(D_{\text{high}} = \text{pwma\_duty}/1024\)。

**若无法运行**：「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：把 `pwm_a <= ~pwm_act || cnt<=pwma_lb || cnt>pwma_ub` 改写成“低电平区间”的条件，应是什么？

**答案**：`pwm_a = 0` 当且仅当 `pwm_act && cnt>pwma_lb && cnt<=pwma_ub`，即 `cnt ∈ (lb, ub]` 的中间区段。这正是“中心对齐”：低电平脉冲被 `lb/ub` 夹在周期中部。

**练习 2**：若把 `MAX_AMP` 从 384 调到 500，会同时改变哪两件事？

**答案**：① 最大占空比摆幅变大（可达 \([1.2\%, 98.8\%]\)），电机能输出更大扭矩；② 三相下桥臂的最小保证导通时间变短，采样窗口变窄，可能突破 `SAMPLE_DELAY + ADC 转换时间` 的约束，导致电流采样失真。这是“最大力矩”与“可采样性”的折中（参见 [RTL/foc/foc_top.v:16](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L16) 与下一讲 hold_detect）。

---

## 5. 综合实践

**任务**：把本讲的三条主线——周期定义、末段时序、中心对齐比较——串成一张完整的“单周期时序图”。

1. 在一张横轴为 `cnt`（0~2047）的纸上，标出 2037~2047 这 11 拍里每拍发生的事件（参考 4.2.2 的表），并在 `cnt=0` 处标注“新周期占空比生效”。
2. 在同一张图的下半部分，画出一个周期内 `pwm_a/b/c` 三条中心对齐波形：假设 `pwma_duty=700、pwmb_duty=400、pwmc_duty=550`，分别画出它们的高电平区间（两端）与低电平区间（中部），标出 `lb/ub` 的位置。
3. 在图上用阴影标出“三相同时为低”的重叠窗口，说明它就是 `hold_detect`（u2-l8）要检测、并在此后延时 `SAMPLE_DELAY` 触发 ADC 采样的位置。
4. 写一段话回答：为什么占空比更新频率、电流采样频率、PID 更新频率必须三者相等？提示——它们都被同一个 `cnt`（0~2047）的回绕所同步（[RTL/foc/foc_top.v:21](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L21)）。

> 这是一个纯源码阅读 + 画图型实践，无需上电；若要数值验证，可在 `tb_svpwm` 仿真中读取上述三个 duty 值对应的实际波形。

## 6. 本讲小结

- `svpwm.v` 用 11 位 `cnt`（0~2047）定义 PWM 周期，PWM 频率 = `clk/2048`（36.864MHz 下约 18kHz），与采样率、PID 更新率同节拍。
- 所有占空比计算挤在每个周期的最后 11 拍（`cnt=2037~2047`）流水完成，围绕 ROM 的 4 拍延迟 `ROM_LATENCY` 排布；输入矢量必须在 `cnt=2041−ROM_LATENCY=2037` 提前锁存。
- 三相马鞍波占空比来自“第一象限 ROM 查表（角度互差 120°，靠减 1365 实现）× 共享乘法器分时复用”，最后写成 `pwma_duty = 512 ± ya`。
- 中心对齐比较 `pwm_a <= ~pwm_act || cnt<=lb || cnt>ub` 给出占空比公式 \(D_{\text{high}} = \text{pwma\_duty}/1024\)，`pwma_duty` 越大占空比越大。
- `MAX_AMP=384` 把占空比限制在 12.5%~87.5%，既决定最大力矩，又保证三相下桥臂有足够的重叠导通时间供 ADC 采样——这是与下一讲 `hold_detect` 的关键接口。

## 7. 下一步学习建议

- **下一讲 u2-l8** 将精读 [RTL/foc/hold_detect.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v)，看它如何检测本模块输出的 `pwm_a & pwm_b & pwm_c` 同时为低的窗口，并在延时 `SAMPLE_DELAY` 后产生 `sn_adc` 脉冲。建议先回头弄清本讲的“重叠窗口”是如何由 `MAX_AMP` 与中心对齐共同决定的。
- 若对定点标度感兴趣，可在 u4-l1 看到对 Clark/Park/PI/SVPWM 各模块输入输出位宽的系统整理，本讲的 `>>9`、`>>12`、`[11:3]` 正是其中一环。
- 想加深理解，可尝试：把 ROM 表导出（`case` 里的 1024 个值）画成曲线，验证它确实呈现马鞍形而非正弦形；并思考若改成纯正弦表，三相占空比会失去什么性质。
