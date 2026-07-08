# ADC 采样时序 hold_detect.v

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么 FOC 的相电流采样不能"想采就采"，而必须卡在"三相下桥臂同时导通"的那段采样窗口里；
- 读懂 `hold_detect.v` 里 `latch1`/`latch2` 两级打拍如何抓住输入信号的上升沿，`cnt` 又如何做倒计时延时；
- 解释 `SAMPLE_DELAY` 参数的物理含义，以及它为何必须小于采样窗口长度；
- 把 `hold_detect` 放回 `foc_top.v` 的闭环里，讲清 `sn_adc → en_adc → 电流重构 → … → SVPWM → PWM → hold_detect` 这一整圈的节拍关系；
- 理解 `MAX_AMP` 为什么会反过来约束采样窗口长度，从而理解"最大力矩"与"可采样性"之间的工程折中。

## 2. 前置知识

- **下桥臂电阻采样**：本项目电机驱动板用的 MP6540 把采样电阻接在每个下桥臂 MOS 管的源极到地之间。只有下桥臂 MOS **导通**时，相电流才流过采样电阻、才能被 ADC 测到；下桥臂关断时采样电阻上没有电流，测到的是无意义值。这是本讲一切时序设计的物理出发点。
- **PWM 与桥臂的对应**：在 `foc_top` 的端口约定里，`pwm_x=1` 表示 x 相**上**桥臂导通，`pwm_x=0` 表示 x 相**下**桥臂导通，见 [RTL/foc/foc_top.v:L33-L35](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L33-L35) 的注释。所以"三相下桥臂同时导通"就等价于 `pwm_a=pwm_b=pwm_c=0`。
- **i_en/o_en 脉冲握手**：全库统一用单周期高电平脉冲表示"数据有效/节拍到来"（见 u2-l1）。本讲的 `sn_adc`（hold_detect 输出）和 `en_adc`（ADC 回送）也是这种脉冲。
- **SVPWM 周期 = 控制周期**：svpwm 用 11 位计数器 `cnt`（0~2047）定义一个 PWM 周期，共 2048 个 `clk`，所以 PWM 频率 = clk/2048（见 u2-l7）。一个控制周期 = 一个 PWM 周期 = 2048 个 `clk`，采样率、PID 更新率、占空比更新率三者同节拍。
- **定点约定**：`MAX_AMP` 是 9 位无符号（1~511），`SAMPLE_DELAY` 在 `foc_top` 里是 9 位、在 `hold_detect` 里被零扩展成 16 位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [RTL/foc/hold_detect.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v) | 本讲主角。检测输入 `in` 的上升沿并保持 `SAMPLE_DELAY` 个周期后，在 `out` 上产生一个时钟周期的高电平脉冲。 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 例化 `hold_detect`，把 `in` 接成 `~pwm_a & ~pwm_b & ~pwm_c`，把 `out` 引出到端口 `sn_adc`；并承接 `en_adc` 完成电流重构。 |
| [RTL/foc/svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v) | （u2-l7 已精读）本讲在分析"采样窗口长度受 `MAX_AMP` 影响"时需要回看它的占空比公式。 |

## 4. 核心概念与源码讲解

### 4.1 采样窗口：为什么相电流采样必须落在三相下桥臂同时导通期

#### 4.1.1 概念说明

FOC 电流环每个控制周期都要读一次三相电流 `ia/ib/ic`，但这一次读取并不是任意时刻都能做。原因在于上一节提到的"下桥臂电阻采样"：

- 采样电阻在下桥臂 MOS 的源极到地之间。**下桥臂不导通 = 该相电流不被采样电阻感知 = 测不到**。
- 想用一颗 ADC（如 AD7928）尽量"同时"读到三相电流，就得让 **三相的下桥臂在同一时间段里都导通**，这段三相 PWM 同时为低 (`pwm_a=pwm_b=pwm_c=0`) 的时间，就叫做**采样窗口**。

`hold_detect` 要解决的问题是：在每个 PWM 周期里，自动找到这段"三相同时为低"的窗口，等电流稳定后再给 ADC 发一个"可以采样了"的脉冲 (`sn_adc`)。所以它的输入就是"三相是否同时为低"这个逻辑表达式。

> 术语：**采样窗口 (sampling window)** = 一个 PWM 周期内 `pwm_a=pwm_b=pwm_c=0` 持续的那段时间。**下桥臂 (lower arm)** = 每相半桥中接地的那个 MOS 管。

#### 4.1.2 核心流程

```text
每相 PWM 中心对齐：下桥臂导通段 (pwm=0) 集中在 cnt 中点附近
        ↓
三相 PWM 取与非 → in = ~pwm_a & ~pwm_b & ~pwm_c
        ↓
in=1 当且仅当三相同时为低（即位于采样窗口内）
        ↓
hold_detect 在 in 上升沿（窗口开始）装载延时计数器
        ↓
倒计时 SAMPLE_DELAY 拍（等电流稳定）→ sn_adc 产生 1 拍脉冲
        ↓
外部 ADC 收到 sn_adc，开始采样三相电流
```

为什么中心对齐 SVPWM 必然存在这样一段公共低电平？因为七段式 SVPWM 通过零序注入，让三相占空比同步抬升/下沉，使三相 PWM 的低电平段在 `cnt` 中点附近重合成一段公共区间。这段区间的长度就是采样窗口长度，它正是下一节 (4.3) 要量化的对象。

#### 4.1.3 源码精读

`foc_top.v` 里 `hold_detect` 的例化把"三相同时为低"直接写成了一条位运算表达式接进 `in`：

[RTL/foc/foc_top.v:L264-L271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L264-L271) — 例化 `hold_detect` 为 `u_adc_sn_ctrl`，其中 `.in(~pwm_a & ~pwm_b & ~pwm_c)` 把三相 PWM 的"同时为低"编码成 `in=1`，`.out(sn_adc)` 把脉冲引到 `foc_top` 的端口 `sn_adc` 上。

也就是说，`in` 是一个**组合逻辑信号**：

\[ \text{in} = \neg\,\text{pwm\_a} \;\wedge\; \neg\,\text{pwm\_b} \;\wedge\; \neg\,\text{pwm\_c} \]

只有在三相 PWM 同时为 0 的窗口里 `in` 才为 1。又因为 `hold_detect` 只在 `in=1` 期间才会倒计时并最终发脉冲（见 4.2），所以 `sn_adc` 脉冲**必然、也只能**落在三相 PWM 同时为低的窗口内（更精确地说，落在窗口开始后约 `SAMPLE_DELAY` 拍处，仍在窗口之内）。

注意例化里还有一处关键接线：`.rstn(init_done)`（[RTL/foc/foc_top.v:L267](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L267)）。也就是说，在初始化标定 Φ 期间 `init_done=0`，`hold_detect` 一直处于复位状态，不发任何 `sn_adc` 脉冲；直到标定完成、`init_done=1` 后才开始正常工作。这与 u2-l6 讲过的"`init_done` 既是标定完成标志、又是全链路 rstn"的设计一致。

#### 4.1.4 代码实践

1. **实践目标**：确认 `in` 与三相 PWM 的逻辑关系，理解 `sn_adc` 为何只在三相同时为低时出现。
2. **操作步骤**：
   - 打开 [RTL/foc/foc_top.v:L260-L271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L260-L271)，找到 `hold_detect` 的例化。
   - 把 `in` 的表达式 `~pwm_a & ~pwm_b & ~pwm_c` 列成一张真值表：只有 `(pwm_a,pwm_b,pwm_c)=(0,0,0)` 时 `in=1`，其余 7 种组合 `in=0`。
3. **需要观察的现象**：`in` 是三相 PWM 的"全低"指示，是一个组合信号，无寄存器延迟。
4. **预期结果**：因为 `hold_detect` 仅在 `in=1` 期间倒计时（见 4.2.3），所以 `sn_adc` 脉冲不可能出现在任一相 PWM 为高的时间段里——它被严格锁在采样窗口内。
5. 「待本地验证」：若你在 gtkwave 里仿真 `tb_svpwm`（见 u1-l4 / u2-l7），可把 `pwm_a/pwm_b/pwm_c` 和一个手算的 `~pwm_a & ~pwm_b & ~pwm_c` 信号一起显示，亲眼确认 `in=1` 的区间就是三相同时为低的区间。

#### 4.1.5 小练习与答案

**练习 1**：如果电机驱动板改用"上桥臂电阻采样"（采样电阻在上桥臂），`in` 的表达式应改成什么？

**答案**：应改成 `in = pwm_a & pwm_b & pwm_c`，即三相上桥臂同时导通时才采样。本库用的是下桥臂采样，所以才是取反后的与。

**练习 2**：`in` 信号是寄存器输出还是组合逻辑？这会影响 `hold_detect` 内部的时序吗？

**答案**：`in` 是组合逻辑（`assign` 等价的例化端口表达式），不含寄存器。`hold_detect` 内部第一级 `latch1` 会把它打一拍，所以 `in` 的组合延迟会被 `latch1` 吸收，不会影响后续倒计时的节拍正确性，只要 `in` 在 `clk` 边沿前已稳定即可。

---

### 4.2 hold_detect 模块：上升沿检测 + SAMPLE_DELAY 保持倒计时

#### 4.2.1 概念说明

`hold_detect.v` 只有一件事：**当输入 `in` 出现上升沿（从 0 变 1，即进入采样窗口），并且能保持 `SAMPLE_DELAY` 个时钟周期不下落，就在 `out` 上产生一个周期的高电平脉冲**；如果 `in` 还没到 `SAMPLE_DELAY` 拍就掉回 0（窗口太短），则本周期不发脉冲。

> 关于源码注释的一句话说明：[RTL/foc/hold_detect.v:L6](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L6) 的文件头注释写作"检测 in 从高电平变为低电平"。但结合代码与 `foc_top` 的接法 (`in=~pwm_a&~pwm_b&~pwm_c`) 可知，代码实际检测的是 **`in` 的上升沿**（低→高），这恰对应三相 PWM 信号从高变低、进入采样窗口的时刻——注释里"高电平变为低电平"描述的是 PWM 的跳变方向，而 `in` 本身是低→高。读代码时以本节的逻辑分析为准。

需要理解三个关键点：

- **`SAMPLE_DELAY` 的物理含义**：三相下桥臂刚导通时，MOS 管从开始导通到电流稳定需要一小段时间（开通瞬态 + 电路分布参数）。若一导通就让 ADC 采样，采到的是还没稳定的瞬态电流，误差大。`SAMPLE_DELAY` 就是"等电流稳定"的延时拍数。
- **"保持"的要求**：因为 ADC 实际采样要发生在窗口之内，所以延时必须小于窗口长度；否则窗口已经结束（`in` 已掉回 0），`hold_detect` 会清零计数器、不发脉冲，本控制周期就没有电流采样。
- **每个窗口最多一个脉冲**：倒计时到 0 后 `cnt` 保持 0，即使 `in` 仍为高也不重发；要等下一次上升沿才重新装载。

#### 4.2.2 核心流程

`hold_detect` 内部用两条 `always` 块协作：

```text
【打拍移位】 latch1 <= in ;  latch2 <= latch1
        ↓
latch1 = in 延迟 1 拍 ;  latch2 = in 延迟 2 拍
        ↓
【判定】 用 latch1/latch2 的组合识别三种状态：
  (latch1=0)            → in 当前为低：cnt 清零
  (latch1=1, latch2=0)  → in 刚出现上升沿：cnt 装载 SAMPLE_DELAY
  (latch1=1, latch2=1)  → in 已持续为高：cnt 每拍 -1
        ↓
当 (latch1=1, latch2=1) 且 cnt==1 时：out 产生 1 拍脉冲
```

上升沿的判定逻辑是关键：`latch1=1` 说明"上一拍 `in` 已经是 1"，`latch2=0` 说明"上上拍 `in` 还是 0"——两者同时成立，就说明 `in` 恰好在上一拍发生了 0→1 跳变，这就是上升沿。`latch1=1` 且 `latch2=1` 则说明 `in` 已经连续为高至少两拍，进入"持续保持"阶段，开始倒计时。

#### 4.2.3 源码精读

先看模块端口与参数：[RTL/foc/hold_detect.v:L9-L16](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L9-L16) 定义了参数 `SAMPLE_DELAY`（默认 `16'd100`，但 `foc_top` 会用 `9'd120` 覆盖它）和端口 `rstn/clk/in/out`。注意 `out` 是 `reg`。

第一条 `always` 是两级移位寄存器：

[RTL/foc/hold_detect.v:L21-L25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L21-L25) — 复位时 `{latch1,latch2}<=2'b11`（这一点在 4.2.4 解释）；否则 `{latch1,latch2}<={in,latch1}`，即 `latch1<=in`、`latch2<=latch1`，构成对 `in` 的两级打拍。

第二条 `always` 是核心状态机：

[RTL/foc/hold_detect.v:L27-L44](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L27-L44) — 默认 `out<=0`；然后：

- `if(latch1)`（`in` 上一拍为高）：
  - `if(latch2)`（`in` 已连续为高）：若 `cnt!=0` 则 `cnt<=cnt-1`；并且 `out <= (cnt==1)`（[L37](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L37)），即倒计时数到 1 的那一拍发脉冲；
  - `else`（`latch2=0`，即刚检测到上升沿）：`cnt <= SAMPLE_DELAY`（[L39](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L39)），装载延时值；
- `else`（`in` 上一拍为低）：`cnt <= 0`（[L42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/hold_detect.v#L42)），清零计数器。

注意一个细节：在"持续保持"分支里，当 `cnt==0` 时**不给 `cnt` 赋值**（既不减也不清零），所以 `cnt` 会"粘"在 0 上，保证一个窗口内只发一次脉冲，必须等 `in` 掉回 0、再次上升沿才会重新装载。

**一次成功采样的时序追踪**（取 `SAMPLE_DELAY=3` 便于观察，稳态 `in=0` 时 `latch1=latch2=cnt=0`）：

| 周期 (in 取值) | latch1(旧) | latch2(旧) | cnt(旧) | cnt(新) | out(新) | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| A: in=1 | 0 | 0 | 0 | 0 | 0 | in 刚拉高，但 latch1 还是 0 |
| B: in=1 | 1 | 0 | 0 | 3 | 0 | 检测到上升沿 (latch1=1,latch2=0)，装载 cnt=3 |
| C: in=1 | 1 | 1 | 3 | 2 | 0 | 持续高，倒计时 3→2 |
| D: in=1 | 1 | 1 | 2 | 1 | 0 | 倒计时 2→1 |
| E: in=1 | 1 | 1 | 1 | 0 | **1** | cnt==1，`out` 产生 1 拍脉冲 |
| F: in=1 | 1 | 1 | 0 | 0 | 0 | cnt 保持 0，不再重发 |

可见从 `in` 上升沿（周期 A）到 `out` 脉冲（周期 E）相隔 `SAMPLE_DELAY+1` 拍，其中 `+1` 是上升沿检测本身的 1 拍流水线延迟。

**窗口太短时的时序追踪**（`in` 只保持了 3 拍就掉回 0）：

| 周期 (in 取值) | latch1(旧) | latch2(旧) | cnt(旧) | cnt(新) | out(新) | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| A: in=1 | 0 | 0 | 0 | 0 | 0 | in 拉高 |
| B: in=1 | 1 | 0 | 0 | 3 | 0 | 装载 cnt=3 |
| C: in=1 | 1 | 1 | 3 | 2 | 0 | 3→2 |
| D: in=0 | 1 | 1 | 2 | 1 | 0 | in 掉回 0，但旧 latch1 仍=1，cnt 继续到 1 |
| E: in=0 | 0 | 1 | 1 | 0 | 0 | 旧 latch1=0 → cnt 清零，**不发脉冲** |

这就是"保持"要求的体现：窗口短于 `SAMPLE_DELAY`（再加检测延迟）时，本周期没有 `sn_adc` 脉冲，也就没有电流采样。

#### 4.2.4 代码实践

1. **实践目标**：手工追踪一次 `hold_detect` 的脉冲产生过程，并理解复位初值 `2'b11` 的用意。
2. **操作步骤**：
   - 取 `SAMPLE_DELAY=3`，按 4.2.3 的两张表在纸上重画一遍 `latch1/latch2/cnt/out` 的波形，确认脉冲发生在 `cnt==1` 那一拍。
   - 思考：为什么复位时 `{latch1,latch2}<=2'b11` 而不是 `2'b00`？
3. **需要观察的现象**：若复位初值是 `2'b00`，且 `init_done` 拉高的那一刻 `in` 恰好已经是 1（极少见但理论存在），逻辑会把第一拍当成上升沿立刻装载 `cnt`；而初值设为 `2'b11` 时，复位释放后必须观察到一次真正的 0→1 跳变才会装载，避免了上电瞬间的假触发。
4. **预期结果**：复位初值 `2'b11` 让"上升沿"必须是真实 observed 的 0→1 跳变，保证 `init_done` 刚拉高时不会误发 `sn_adc`。
5. 「待本地验证」：可写一个最小 testbench（只例化 `hold_detect`，用 `initial` 产生 `clk/rstn` 和一段 `in` 脉冲），用 iverilog 跑出 `dump.vcd`，在 gtkwave 里对照上表验证 `out` 恰好在 `cnt==1` 拍拉高一次。

#### 4.2.5 小练习与答案

**练习 1**：把 `SAMPLE_DELAY` 改成 0，`out` 会有什么行为？

**答案**：装载 `cnt=0` 后，在"持续高"分支里 `cnt!=0` 不成立，`cnt` 不减；`out <= (cnt==1)` 即 `(0==1)` 恒为 0。所以 `SAMPLE_DELAY=0` 时**永远不会发脉冲**——这说明该参数实际上要求 ≥1，也印证了"必须等电流稳定至少 1 拍"的物理意图。

**练习 2**：一个采样窗口里 `out` 最多发几次脉冲？为什么？

**答案**：最多 1 次。倒计时到 0 后 `cnt` "粘"在 0（`cnt==0` 时既不清零也不重装），`out <= (0==1)=0`，必须等 `in` 掉回 0 再出现下一次上升沿才会重新装载 `cnt`。这保证一个 PWM 周期（一个采样窗口）最多触发一次 ADC 采样，与"采样率 = 控制频率"的节拍一致。

**练习 3**：`hold_detect` 的 `SAMPLE_DELAY` 参数是 16 位，而 `foc_top` 传进来的 `SAMPLE_DELAY` 是 9 位（`9'd120`），这会出错吗？

**答案**：不会。Verilog 参数在例化传递时会做位宽调整，9 位无符号值 `120` 会被零扩展成 16 位的 `16'd120`，数值不变。`hold_detect` 用 16 位 `cnt` 只是留了更大的延时余量（最大可设 65535），而 `foc_top` 的 9 位上限（511）对实际应用已经足够。

---

### 4.3 窗口长度与 MAX_AMP 的折中 + 闭环节拍

#### 4.3.1 概念说明

采样窗口不是免费午餐——它的长度被 SVPWM 的最大振幅 `MAX_AMP` 反向约束：

- `MAX_AMP` 越大，电机能达到的最大力矩越大（电压矢量幅值更大）；
- 但 `MAX_AMP` 越大，三相 PWM 的占空比摆幅越大，"三相同时为低"的那段公共时间就越短，采样窗口越短；
- 当 `MAX_AMP=511`（最大值）时，采样窗口长度退化为 0，根本无法采样。

所以 `MAX_AMP` 是"最大力矩"与"可采样性"之间的折中旋钮。这也是为什么 FAQ 里强调：默认 `MAX_AMP=9'd384` 是个够用的折中值，再大就可能采不到电流。

同时，本节还要把 `hold_detect` 放回闭环：`sn_adc` 只是"命令 ADC 开始"，ADC 真正把结果送回来是 `en_adc` 脉冲，`foc_top` 在 `en_adc` 时才做电流重构。整条链路有一个硬约束：

\[ T_{\text{SAMPLE\_DELAY}} \;+\; T_{\text{sn\_adc} \to \text{en\_adc}} \;<\; T_{\text{window}} \]

即"hold_detect 的延时 + ADC 完成三通道采样的时间差"必须小于采样窗口长度，否则 ADC 还没采完，窗口就关了。

#### 4.3.2 核心流程

```text
svpwm 占空比摆幅 ∝ MAX_AMP
        ↓
max_duty = 512 + MAX_AMP   （电压矢量幅值最大时，最坏情况）
        ↓
采样窗口长度 = 2048 - 2 × max_duty = 1024 - 2 × MAX_AMP   (单位：clk 周期)
        ↓
MAX_AMP=511 → 窗口≈0 ；MAX_AMP=384 → 窗口≈256 拍(最坏情况, ≈7µs)
        ↓
约束：SAMPLE_DELAY + (sn_adc→en_adc 拍数) < 窗口长度
        ↓
闭环：sn_adc → ADC 采样 → en_adc → 电流重构(ia/ib/ic) → clark → park → PI
      → cartesian2polar → 反park → svpwm → PWM → hold_detect → sn_adc …
```

#### 4.3.3 源码精读

先看窗口长度怎么由占空比决定。svpwm 的中心对齐比较式为：

[RTL/foc/svpwm.v:L105-L107](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L105-L107) — `pwm_a <= ~pwm_act || cnt<=pwma_lb || cnt>pwma_ub;` 即在活动区里，`pwm_a=0`（下桥臂导通）当且仅当 `pwma_duty < cnt <= 2048-pwma_duty`。其中边界由 [RTL/foc/svpwm.v:L87-L92](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L87-L92) 给出：`pwma_lb = +pwma_duty`、`pwma_ub = -pwma_duty`（即 `2048-pwma_duty`）。

所以 A 相下桥臂导通段长度为 \(2048 - 2\cdot\text{pwma\_duty}\) 个 `clk`。三相同时为低（采样窗口）要求 `cnt` 同时落在三相的下桥臂导通段内，即：

\[ T_{\text{window}} = 2048 - 2\cdot \max(\text{pwma\_duty},\text{pwmb\_duty},\text{pwmc\_duty}) \]

而占空比由 [RTL/foc/svpwm.v:L83-L85](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L83-L85) 给出：`pwma_duty = 512 ± ya`，其中 `ya` 的幅度正比于 `v_amp = MAX_AMP`（[RTL/foc/svpwm.v:L63](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/svpwm.v#L63) 把 `v_amp` 送入乘法器）。在电压矢量幅值最大的最坏情况下，`ya` 的峰值达到 `MAX_AMP`，于是：

\[ \max(\text{duty}) = 512 + \text{MAX\_AMP} \quad\Rightarrow\quad T_{\text{window,min}} = 2048 - 2(512+\text{MAX\_AMP}) = 1024 - 2\cdot\text{MAX\_AMP} \]

代入两个锚点验证：

- `MAX_AMP=511`：\(1024 - 1022 = 2\) 拍 ≈ 0，与 FAQ"窗口长度就是 0"吻合；
- `MAX_AMP=384`：\(1024 - 768 = 256\) 拍，按 `clk=36.864MHz`（1 拍≈27.1ns）约 **7µs**（这是最坏情况下的下限；FAQ 给出的"十几 µs"是典型运行值，因为稳态下电压矢量幅值 `v_rho` 通常小于最大值，窗口会更宽）。

> 上述公式给出的是**最坏情况**（电压矢量幅值最大时）的窗口下限。设计时必须用这个下限来校验约束，因为必须保证**每一个**控制周期都能采到电流。

再看 `foc_top` 如何承接 `en_adc` 完成电流重构，串起闭环：

[RTL/foc/foc_top.v:L99-L109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L99-L109) — 当 `en_adc` 出现脉冲时（[L103](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L103) `en_iabc<=en_adc`），用 `adc_a/adc_b/adc_c` 重构出 `ia/ib/ic`（[L105-L107](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L105-L107)，即 u2-l2 讲过的 `Ia=ADCb+ADCc-2·ADCa` 等），随后经 Clark→Park→PI→cartesian2polar→反Park→svpwm→PWM，下一周期的 PWM 又被 `hold_detect` 监视，形成闭环。

`sn_adc` 与 `en_adc` 的端口定义见 [RTL/foc/foc_top.v:L28-L29](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L28-L29)：`sn_adc` 是输出（命令 ADC 开始），`en_adc` 是输入（ADC 转换完成、结果有效）。FAQ 对这个闭环约束有明确表述，见 [README.md:L567-L569](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L567-L569)：`MAX_AMP` 越大窗口越短，`MAX_AMP=511` 时窗口为 0，`MAX_AMP=384` 时窗口约十几 µs；且用户必须自己算好 `hold_detect.v 的延时 + ADC 采样三个通道的时间差 < 采样窗口长度`。

#### 4.3.4 代码实践

1. **实践目标**：把 `MAX_AMP`、采样窗口、`SAMPLE_DELAY` 三者的数值关系算清楚，理解为什么不能把 `MAX_AMP` 调到 511。
2. **操作步骤**：
   - 用公式 \(T_{\text{window,min}} = 1024 - 2\cdot\text{MAX\_AMP}\) 计算几个值：`MAX_AMP=384 → 256 拍`、`MAX_AMP=448 → 128 拍`、`MAX_AMP=511 → 2 拍`。
   - 估算"sn_adc→en_adc"的时间：AD7928 用 SPI 串行采 3 个通道，`spi_sck=clk/2`，每通道约 16 个 SCK 周期再加切换开销，合计大约一百多拍（精确值可仿真 `adc_ad7928.v` 得到，见 u3-l2）。
   - 加上 `SAMPLE_DELAY=120` 拍，得到总占用约 \(120 + T_{\text{adc}}\) 拍，与窗口下限比较。
3. **需要观察的现象**：`MAX_AMP` 越大，窗口下限越小；当窗口下限小于 `SAMPLE_DELAY + T_adc` 时，部分控制周期会采不到电流，电流环失效。
4. **预期结果**：默认 `MAX_AMP=384`（窗口下限 256 拍）与 `SAMPLE_DELAY=120` 留出了刚好够用的余量；若把 `MAX_AMP` 调到接近 511，窗口几乎归零，`hold_detect` 因 `in` 保持不住而发不出 `sn_adc`，电机失控。
5. 「待本地验证」：在 `fpga_top.v` 里把 `foc_top` 的 `MAX_AMP` 参数（见 [RTL/foc/foc_top.v:L16](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L16)）逐步调大并上板观察 UART 打印的 `id/iq`，应能看到电流跟随变差直至发散——这是窗口不足导致采样缺失的直接现象。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MAX_AMP` 增大会让采样窗口变短？用一句话和公式回答。

**答案**：`MAX_AMP` 增大 → 三相占空比摆幅 `ya` 增大 → `max_duty=512+MAX_AMP` 增大 → 窗口 \(T=2048-2\cdot\max\_duty=1024-2\cdot\text{MAX\_AMP}\) 减小。

**练习 2**：若电机的 `MAX_AMP` 必须设到 480（为了更大力矩），`SAMPLE_DELAY=120` 还合适吗？该怎么调整？

**答案**：`MAX_AMP=480` 时窗口下限 \(1024-960=64\) 拍，而 `SAMPLE_DELAY=120` 已经超过窗口，必定采不到。应把 `SAMPLE_DELAY` 调小（例如 30~40 拍），同时确认 ADC 的 `sn_adc→en_adc` 时间也小于剩余窗口。如果两者加起来仍超过 64 拍，就只能换更快的 ADC（并行 ADC）或降低 `MAX_AMP`。

**练习 3**：`hold_detect` 的 `rstn` 接的是 `init_done` 而不是 `foc_top` 的 `rstn`，这意味着什么？

**答案**：意味着在初始化标定 Φ 的那 0.45 秒里（`init_done=0`），`hold_detect` 一直复位，不发 `sn_adc`，也就不采电流——这正是想要的，因为标定阶段还没开始闭环控制。标定一结束 `init_done=1`，`hold_detect` 立即开始监视 PWM 并触发采样，与全链路同时启动。

---

## 5. 综合实践

**任务**：追踪 `foc_top.v` 里 `hold_detect` 的输入 `in = ~pwm_a & ~pwm_b & ~pwm_c`，完整说明 `sn_adc` 脉冲为何只会在三相 PWM 同时为低的窗口内出现，以及该窗口长度受 `MAX_AMP` 影响的原因。

**建议步骤**：

1. **画接线图**：在 [RTL/foc/foc_top.v:L264-L271](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L264-L271) 处，画出 `pwm_a/pwm_b/pwm_c → (与非) → in → hold_detect → sn_adc → (外部 ADC) → en_adc + adc_a/b/c → 电流重构` 这条链路，标出每一段是组合逻辑还是寄存器、是脉冲还是电平。
2. **证明"只在窗口内"**：用 4.1.3 的真值表说明 `in=1` 当且仅当三相同时为低；再用 4.2.3 的"窗口太短则不发脉冲"追踪说明，`sn_adc` 不仅出现在窗口内，而且要求窗口足够长。
3. **量化窗口**：用 4.3.3 的公式 \(T_{\text{window,min}}=1024-2\cdot\text{MAX\_AMP}\) 计算 `MAX_AMP=384` 和 `MAX_AMP=511` 两种情况，对照 FAQ（[README.md:L567-L569](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L567-L569)）验证结论。
4. **写一个最小 testbench**（可选，源码阅读型）：只例化 `hold_detect`，给定一段长度可变的 `in` 脉冲，用 iverilog 仿真，观察 `in` 宽度分别为 `SAMPLE_DELAY+1`、`SAMPLE_DELAY+2`、`SAMPLE_DELAY` 时 `out` 是否发脉冲，验证 4.2 的时序分析。命令形如（在 `SIM/` 目录）：
   ```bash
   iverilog -g2001 -o sim.vvp ../RTL/foc/hold_detect.v tb_hold_detect.v
   vvp -n sim.vvp
   gtkwave dump.vcd
   ```
5. **预期成果**：一张接线图 + 一份窗口长度计算表 + 一段说明文字，讲清"`sn_adc` 受 `in` 约束、`in` 受 PWM 约束、PWM 占空比受 `MAX_AMP` 约束"这条因果链。若未实际运行仿真，相关结论标注「待本地验证」。

## 6. 本讲小结

- 相电流采样必须落在"三相下桥臂同时导通"的采样窗口里，因为采样电阻在下桥臂，下桥臂不导通就测不到电流；`foc_top` 用 `in = ~pwm_a & ~pwm_b & ~pwm_c` 把这一条件编码成 `in=1`。
- `hold_detect` 用 `latch1/latch2` 两级打拍检测 `in` 的上升沿（`latch1=1,latch2=0`），装载 `cnt=SAMPLE_DELAY`，在持续保持（`latch1=1,latch2=1`）期间逐拍倒计时，数到 1 时发 1 拍 `sn_adc` 脉冲。
- `SAMPLE_DELAY` 是"等 MOS 管电流稳定"的延时；若窗口短于该延时，`in` 提前掉回 0，`cnt` 被清零，本周期不发脉冲——这是采样窗口长度对系统的硬约束。
- 采样窗口长度 \(T_{\text{window,min}}=1024-2\cdot\text{MAX\_AMP}\) 个 `clk` 周期：`MAX_AMP` 越大，力矩越大但窗口越短；`MAX_AMP=511` 时窗口≈0，无法采样。
- 闭环节拍：`sn_adc`（命令采样）→ 外部 ADC → `en_adc`（结果有效）→ 电流重构 `ia/ib/ic` → Clark → Park → PI → cartesian2polar → 反Park → svpwm → PWM → `hold_detect` → 下一周期 `sn_adc`；约束为 `SAMPLE_DELAY + T_{sn_adc→en_adc} < T_window`。
- `hold_detect` 的 `rstn=init_done`，保证标定期间不采样、标定结束后与全链路同步启动。

## 7. 下一步学习建议

- 下一讲进入第 3 单元，先读 [u3-l2 SPI ADC 读取 adc_ad7928.v](./u3-l2-spi-adc-read.md)：本讲反复提到的"`sn_adc → en_adc` 时间差"正是由 `adc_ad7928.v` 决定的，读完它就能把闭环约束里的 `T_{sn_adc→en_adc}` 量化出来。
- 若想验证本讲的窗口公式，可回头重跑 [u2-l7 SVPWM 仿真](./u2-l7-svpwm.md)：在 `tb_svpwm` 的波形里直接量 `pwm_a/pwm_b/pwm_c` 三相同时为低的时长，对照 \(1024-2\cdot\text{MAX\_AMP}\)。
- 进阶读者可阅读 [u4-l2 参数整定与跨平台移植](./u4-l2-parameter-tuning-and-porting.md)，系统理解 `MAX_AMP`、`SAMPLE_DELAY` 等 5 个参数的取值范围与调参策略，以及更换 ADC/传感器时如何复用 `foc_top` 的脉冲握手接口。
