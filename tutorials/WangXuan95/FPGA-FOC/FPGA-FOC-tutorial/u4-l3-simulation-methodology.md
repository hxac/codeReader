# 仿真方法论与波形解读

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚**为什么本项目只仿真了 clark/park 和 cartesian2polar/svpwm 这几个子模块，却没有仿真整个 FOC 闭环**——根本原因是缺少电机的 Verilog 模型，并能把这条限制推广成一条通用的"开环切片"仿真策略。
- 把 `sincos` 模块当成一台**可控正弦信号源**来用，理解它在两个 testbench 里"被挪用"的原理，并能把同样的技巧迁移到给任意模块喂激励。
- 把"看波形"升级成"**用波形当判据**"：知道该在波形里读出哪些特征来证明 Clark/Park/SVPWM 的数学正确性（αβ 正交、dq 坍缩成直流、ρ 恒定、马鞍波、duty 决定占空比）。
- 掌握一套**可复用的 testbench 编写范式**：产生 `clk`/`rstn`、用循环递增角度做激励、`$dumpvars` 录信号、`$finish` 收尾，并能为一个全新的模块（如 `pi_controller`）独立写出 testbench。

本讲是专家层的"方法论"讲。它**不再重复工具链操作**（如何安装 iverilog、如何双击 `.bat`、如何把信号设成 Analog 显示——这些已在 [u1-l4](./u1-l4-iverilog-simulation.md) 讲透），而是上升一层：把 [u1-l4](./u1-l4-iverilog-simulation.md) 里"照着跑两个 testbench"的经验，提炼成"**为什么这样仿真、怎样读波形才算验证、怎样自己写 testbench**"的方法论，最终落地为给 `pi_controller` 写一个新 testbench 的综合实践。

## 2. 前置知识

本讲默认你已经掌握以下内容（若不熟请先补对应讲义）：

- **iverilog 三件套**：`iverilog` 编译 → `vvp` 运行 → `gtkwave` 看波形；`$dumpvars(1, <作用域>)` 决定哪些信号写进 `dump.vcd`；`signed` 信号要先设 `Signed Decimal` 再设 `Analog→Step` 才能正确显示正弦/马鞍波。详见 [u1-l4](./u1-l4-iverilog-simulation.md)。
- **FOC 数据流的几个阶段**：三相电流 \(abc\) →（Clark）→ 定子正交坐标 \(\alpha\beta\) →（Park）→ 转子坐标 \(dq\) →（PI）→ 电压 \(V_d/V_q\) →（cartesian2polar）→ 极坐标 \((\rho,\varphi)\) →（SVPWM）→ 三相 PWM。整条链路的源头是 [u2-l1](./u2-l1-foc-top-overview.md)。
- **定点约定**：角度一圈 \(2\pi\) 映射到 12 位无符号 `0~4095`；电流/电压是 16 位有符号；三角函数 \(-1\sim+1\) 映射到 \(-16384\sim+16384\)。详见 [u4-l1](./u4-l1-fixed-point-and-saturation.md)。
- **PI 控制器的流水线**：`pi_controller` 用 `i_en→en1→en2→en3→en4→o_en` 的 5 级节拍，输出 `o_value = value[31:16]`。详见 [u2-l5](./u2-l5-pi-controller.md)。

> 名词小贴士：**DUT**（Design Under Test）= 被测模块；**tb** = testbench；**plant** = 被控对象（这里是电机，仿真里没有它的模型）。

## 3. 本讲源码地图

本讲主要围绕 `SIM/` 目录的两个 testbench 展开，并把它们当作"方法论样本"来剖析；综合实践还会用到 `pi_controller` 与 `sincos`。

| 文件 | 作用 | 本讲用来讲什么 |
| --- | --- | --- |
| [SIM/tb_clark_park_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v) | 测 `clark_tr` + `park_tr` 的 testbench | "sincos 当信号源"与"波形验证"的样本一 |
| [SIM/tb_svpwm.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v) | 测 `cartesian2polar` + `svpwm` 的 testbench | "sincos 当信号源"与"波形验证"的样本二 |
| [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) | 项目说明，含"RTL 仿真"章节与 FAQ | "为何只仿真子模块"的权威出处 |
| [RTL/foc/pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v) | PI 控制器（综合实践的 DUT） | 综合实践：为它新写一个 testbench |
| [RTL/foc/sincos.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v) | 正弦/余弦计算器 | 当作"可控正弦信号源"反复借用 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，层层递进：先讲**仿真策略**（为什么这样切），再讲**信号源技巧**（用什么喂激励），再讲**波形即验证**（怎么读才算数），最后讲**写 testbench 的范式**（怎么迁移到新模块）。两个 testbench（`tb_clark_park_tr`、`tb_svpwm`）作为贯穿全讲的样本。

### 4.1 仿真策略：为什么本项目只仿真子模块

#### 4.1.1 概念说明

一个完整的 FOC 是一个**闭环系统**：角度传感器读角度 → 算法算出 PWM → PWM 驱动电机 → 电机的相电流又流回 ADC → 喂回算法。要仿真这个闭环，仿真器里必须有一个"**电机模型**"——给定 PWM，能算出转子角度和相电流随时间怎么变的 Verilog（或 Verilog-AMS/外部协同仿真）模块。

电机模型很难写：它涉及电磁方程、机械惯量、反电动势、采样保持器延迟……本项目作者没有这个模型，于是做出了一个工程上很常见的选择：**不仿真闭环，而是把闭环拆成一个个子模块，对每个子模块做开环仿真**。每个子模块的输入由 testbench 直接给定（而不是由"前一级 + 电机"产生），输出由人在波形里核对。

这就是"**开环切片仿真**"策略：

| 维度 | 闭环仿真（本项目做不到） | 开环切片仿真（本项目采用） |
| --- | --- | --- |
| 激励来源 | 前一级模块 + 电机模型反馈 | testbench 用信号源直接给定 |
| 能验证什么 | 整个系统的稳定性、跟随曲线 | 单个模块的数学正确性 |
| 需要电机模型 | 是 | 否 |
| 适合谁 | 系统级验证 | 模块级开发与回归测试 |

这条策略的直接后果是：**凡是没有被切片出来配 testbench 的模块，就没有 RTL 级仿真覆盖**。本项目的 `pi_controller`、`hold_detect`、`i2c_register_read`、`adc_ad7928`、`uart_monitor` 以及整个 `foc_top` 闭环，目前都没有 testbench——它们靠作者在线路板上实测验证（见 README 的串口跟随曲线）。本讲的综合实践，正是要补上 `pi_controller` 这一块。

#### 4.1.2 核心流程

判断"某个模块能不能被切片出来单独仿真"，看两点：

1. **它的输入能不能用 testbench 直接合成？** 数学变换类模块（clark、park、sincos、cartesian2polar、svpwm）的输入就是数值（电流、角度、电压），testbench 用一个递增的角度 + 一个正弦源就能合成，所以都能切片。
2. **它的输出能不能用波形肉眼判定对错？** clark 该出正交波、park 该出直流、cartesian2polar 该出恒定 ρ、svpwm 该出马鞍波——都有明确的"应然形态"，适合波形验证。

反之，PI 控制器的"对错"依赖于闭环里电机是否被驱动到目标——单看一段 `o_value` 波形，很难说它"对不对"，只能看它"方向对不对、动不动"。这正是它原本没配 testbench 的原因之一，也是本讲综合实践要诚实面对的限制。

切片仿真的工作流：

```
   挑一个子模块做 DUT
          │
          ▼
   testbench 用信号源合成激励 ──► DUT ──► 输出信号
          │                              │
          ▼                              ▼
   $dumpvars 录进 dump.vcd        人在 gtkwave 里核对"应然形态"
```

#### 4.1.3 源码精读

README 的"RTL 仿真"开篇就点明了这条策略：

[README.md:466](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L466) —— "因为我并没有电机的 Verilog 模型，没法对整个 FOC 算法进行仿真，所以只对 FOC 中的部分模块进行了仿真。"

那么"部分模块"到底是哪几个？答案就藏在两个 `.bat` 编译命令的文件列表里——`iverilog` 后面跟了哪些 `.v`，就说明这次仿真覆盖了哪些模块（testbench 自身除外）：

[SIM/tb_clark_park_tr_run_iverilog.bat:2](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr_run_iverilog.bat#L2) 与 [SIM/tb_svpwm_run_iverilog.bat:2](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm_run_iverilog.bat#L2)：

```verilog
iverilog -g2001 -o sim.out tb_clark_park_tr.v ../RTL/foc/sincos.v ../RTL/foc/clark_tr.v ../RTL/foc/park_tr.v
iverilog -g2001 -o sim.out tb_svpwm.v       ../RTL/foc/sincos.v ../RTL/foc/cartesian2polar.v ../RTL/foc/svpwm.v
```

把这两行整理成一张**仿真覆盖矩阵**，就能一眼看出全库哪些被仿真、哪些没被仿真：

| RTL 模块 | 是否被仿真 | 由哪个 testbench 覆盖 | 角色 |
| --- | --- | --- | --- |
| `sincos` | ✅ | 两个 tb 都用到 | 信号源（兼被 `park_tr` 调用） |
| `clark_tr` | ✅ | tb_clark_park_tr | DUT |
| `park_tr` | ✅ | tb_clark_park_tr | DUT |
| `cartesian2polar` | ✅ | tb_svpwm | DUT |
| `svpwm` | ✅ | tb_svpwm | DUT |
| `pi_controller` | ❌ | —— | 综合实践将补 |
| `hold_detect` | ❌ | —— | 未仿真 |
| `foc_top`（整环） | ❌ | —— | 缺电机模型 |
| `i2c_register_read` / `adc_ad7928` / `uart_monitor` | ❌ | —— | 外设，靠实测 |

> 这张表是本讲最重要的"地图"：它告诉你项目的仿真边界在哪里。注意 `sincos` 出现在两个 tb 里，但**都不是为了测它**——它是被借去当信号源的（详见 4.2）。

#### 4.1.4 代码实践

**实践目标**：自己动手把上面的"仿真覆盖矩阵"验证一遍，建立对项目仿真边界的准确认识。

**操作步骤**：

1. 打开 `SIM/` 目录，列出其中所有文件，确认只有两个 testbench。
2. 打开两个 `.bat`，把 `iverilog` 行里的 `.v` 文件名抄下来，与 `RTL/foc/` 目录下的 `.v` 文件逐一比对。
3. 用 `Grep` 在 `SIM/` 下搜索每个 RTL 模块名（如 `pi_controller`、`hold_detect`），确认它们确实没有出现在任何 testbench 里。

**需要观察的现象**：

- `pi_controller`、`hold_detect` 在 `SIM/` 下搜不到任何引用。
- `clark_tr`、`park_tr`、`cartesian2polar`、`svpwm`、`sincos` 各自至少在一个 testbench 里被例化。

**预期结果**：你得到的覆盖情况与 [4.1.3](#413-源码精读) 的矩阵一致。若搜到 `pi_controller` 出现在某 tb 里，说明项目已更新（本讲以当前 HEAD 为准，结果**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：假如你手头有一个电机的 Verilog 模型 `motor_model.v`（输入 PWM、输出角度和三相电流），你能仿真整个 FOC 闭环吗？还需要补什么？

**参考答案**：原则上可以。把 `foc_top` 的 PWM 输出接到 `motor_model` 的输入，再把 `motor_model` 输出的角度/电流接到 `foc_top` 对应的输入（替代真实的 AS5600/AD7928），就构成闭环 testbench。难点不在连线上，而在电机模型本身的准确度（电感、反电动势、采样延迟等参数），以及仿真时长——闭环要跑很多个控制周期才能看出跟随曲线，仿真会非常慢。

**练习 2**：为什么作者优先给 clark/park/svpwm 配 testbench，而不是优先给 `pi_controller` 配？

**参考答案**：因为前三者的"对错"有明确的、不依赖闭环的判据（正交、坍缩成直流、马鞍波），波形一眼能判；而 PI 的"对错"（能不能把电流稳到目标）本质上是闭环性能问题，开环单看 `o_value` 只能看方向和动态，难以判"对错"，所以优先级低。

---

### 4.2 把 sincos 当作可控正弦信号源

#### 4.2.1 概念说明

要给一个变换模块喂"正弦波"激励，testbench 需要一个正弦波发生器。在 C/Python 里我们会直接写 `sin(x)`，但在 Verilog-2001 里没有现成的 `sin` 函数。本项目里现成的、能算正弦的模块就是 [RTL/foc/sincos.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v)——它在真实设计里是**被 `park_tr` 调用**来算 \(\sin\psi/\cos\psi\) 的（见 [u2-l4](./u2-l4-park-and-sincos.md)），但在 testbench 里被**挪用**成了一台信号源。

这是一个值得记住的技巧：**项目里任何"能把角度变成正弦/余弦"的模块，都可以在 testbench 里当信号源用**，免得自己写查表或 CORDIC。`sincos` 的接口正适合当信号源：

[RTL/foc/sincos.v:11-18](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L11-L18) —— 输入角度，输出 ±16384 振幅的正弦/余弦：

```verilog
module sincos(
    input  wire               rstn,
    input  wire               clk,
    input  wire               i_en,
    input  wire        [11:0] i_theta,      // 0~4095 映射 0~2π
    output reg                o_en,
    output reg  signed [15:0] o_sin, o_cos  // -1~+1 映射 -16384~+16384
);
```

只要让 `i_theta` 递增，`o_sin`/`o_cos` 就是正弦波/余弦波；给 `i_theta` 加不同的常数偏置，就能得到不同初相位的正弦波。两个 testbench 都靠这一招。

#### 4.2.2 核心流程

用 `sincos` 当信号源的统一套路：

1. 在 testbench 里声明一个递增的 `reg [11:0] theta`（主循环里每个节拍 `theta <= theta + 步长`）。
2. 例化 `sincos`，把 `theta`（或 `theta + 相位偏置`）接到 `i_theta`，把 `o_sin`/`o_cos` 接到 DUT 的输入（必要时做 `/常数` 缩放振幅）。
3. `sincos` 的 `i_en` 可以恒为 1（持续转换，如 tb_svpwm），也可以用脉冲（如 tb_clark_park_tr 借 `en_theta`）。

信号流：

```
   reg theta (递增)
        │
        ▼ (+ 可选相位偏置)
     sincos ──► o_sin / o_cos ──► (可选 /缩放) ──► DUT 的输入
```

#### 4.2.3 源码精读

两个 testbench 把 `sincos` 用出了**两种不同的信号源花样**，把它们对照看，就能掌握这一招的全部变化。

**花样一：用三个 sincos 合成三相正弦波**（tb_clark_park_tr）

[SIM/tb_clark_park_tr.v:36-65](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L36-L65) —— 同一个 `theta`，加上三个不同的相位偏置，得到三相：

```verilog
sincos u1_sincos ( .i_theta ( theta + PI_M2_D3 ), .o_sin ( ia ) );  // θ + (2/3)π
sincos u2_sincos ( .i_theta ( theta + PI_D3    ), .o_sin ( ib ) );  // θ + (1/3)π
sincos u3_sincos ( .i_theta ( theta            ), .o_sin ( ic ) );  // θ
```

三个偏置 `\((2/3)\pi, (1/3)\pi, 0\)` 让 `ia/ib/ic` 的初相位依次错开，合成一组标准三相电流。源码第 36 行注释特别强调"这里只是借用 sincos 当信号源"，提醒读者别误以为真实设计里 `sincos` 喂给 `clark_tr`。

**花样二：用一个 sincos 同时取 sin 和 cos，得到正交 (x, y)**（tb_svpwm）

[SIM/tb_svpwm.v:33-41](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L33-L41) —— 这次 `i_en` 恒为 1，且同时取了 `o_sin` 和 `o_cos`：

```verilog
sincos u_sincos (
    .i_en    ( 1'b1  ),
    .i_theta ( theta ),
    .o_sin   ( y     ),   // y = sinθ
    .o_cos   ( x     )    // x = cosθ
);
```

\((x,y)=(\cos\theta,\sin\theta)\) 是一对正交信号，正好当成直角坐标输入喂给 `cartesian2polar`。注意这里 `i_en=1'b1` 持续使能，`sincos` 内部状态机会不断循环转换，`o_sin/o_cos` 持续刷新——比 tb_clark_park_tr 的脉冲用法更"直给"。

**两种花样的共同点与缩放技巧**：两个 tb 都对 `sincos` 输出做了整数除法来缩放振幅——tb_clark_park_tr 用 `/ 16'sd2` 把 ±16384 缩到 ±8192（见 [SIM/tb_clark_park_tr.v:72-74](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L72-L74)），tb_svpwm 用 `/ 16'sd5` 缩到 ±3277（见 [SIM/tb_svpwm.v:47-48](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L47-L48)）。这是因为 `sincos` 的振幅固定 ±16384，而各 DUT 规定的输入范围不同，需要在例化端口处用除法缩放。本讲综合实践给 `pi_controller` 喂正弦时，也会用同样的 `/常数` 缩放（见 [第 5 节](#5-综合实践)）。

> 名词小贴士：端口连接表达式里写 `/ 16'sd2` 是合法的——例化时端口的实参可以是任意常量表达式，综合/仿真器会算出一个除法电路或直接求值。仿真里它就是一次整数除法。

#### 4.2.4 代码实践

**实践目标**：通过修改相位偏置，亲手用 `sincos` 造出"超前/滞后"的正弦波，直观理解相位控制。

**操作步骤**：

1. 复制 `SIM/tb_clark_park_tr.v` 为 `SIM/tb_my.v`（自己练习用，不入库）。
2. 把其中一个 `sincos` 实例的 `i_theta` 从 `theta + PI_D3` 改成 `theta + 12'd1024`（即加 \(90°\)）。
3. 编译运行（仿照 `.bat`，把 `tb_my.v` 换进去），用 gtkwave 把改动的这路 `o_sin` 与原来 `ic` 这路（`theta`，初相 0）都设成 Analog 显示。

**需要观察的现象**：

- 改动后的正弦波比 `ic`（初相 0）**超前 \(90°\)**（即 `ic` 到峰值时它已过峰值往下走，或在 `ic` 过零向上时它已到峰值）。

**预期结果**：相位差 \(90°\)（\(\pi/2\)）清晰可见。这验证了"`i_theta` 加常数 = 改初相位"这一信号源技巧。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`sincos` 在真实设计里被 `park_tr` 调用，在 testbench 里被当信号源。这两种用法在**接口**上有没有区别？

**参考答案**：接口上没有区别——都是把角度接到 `i_theta`、从 `o_sin/o_cos` 取结果。区别只在**谁驱动 `i_en`、`i_theta`**：真实设计里是 `park_tr` 用电角度 ψ 驱动；testbench 里是 testbench 用递增的 θ 驱动。模块本身不知道、也不关心调用方是谁，这正是把它当信号源可行的原因。

**练习 2**：为什么 tb_svpwm 里 `sincos` 的 `i_en` 接 `1'b1`，而 tb_clark_park_tr 里接的是脉冲 `en_theta`？

**参考答案**：tb_svpwm 的主循环每个 PWM 周期（2048 拍）才推进一次 θ，期间希望 `(x,y)` 保持稳定可用，所以持续使能让 `sincos` 不断刷新即可；tb_clark_park_tr 则是每个小节拍推进 θ 并希望和 `clark_tr`/`park_tr` 的流水线节拍对齐，所以用脉冲 `en_theta` 精确控制"何时启动一次转换"。两种接法都成立，选哪种取决于 DUT 对节拍同步的要求。

---

### 4.3 波形即验证：从波形读出变换的数学正确性

#### 4.3.1 概念说明

仿真的目的不是"跑完生成 vcd"，而是**用波形当判据**，确认 DUT 的输出符合数学预期。本节给出一份"波形验证清单"：对着两个 testbench 的波形，该看哪些特征、每个特征证明了什么数学结论。掌握这份清单后，你看到任何一段 FOC 波形，都能反向推断它对不对。

"波形即验证"的核心思想是：**每个变换都有其"应然形态"（expected shape）**，把实际波形与应然形态对照，就是一次验证。

| 变换 | 输入形态 | 应然输出形态 | 证明了什么 |
| --- | --- | --- | --- |
| Clark | 三相正弦（相位差 \(2\pi/3\)） | 一对正交正弦（相位差 \(\pi/2\)） | 投影正确、正交性 |
| Park | 旋转的 \(\alpha\beta\) | 近似直流（常数） | 旋转角与信号同步、解调成功 |
| cartesian2polar | 旋转的 \((x,y)\) | 恒定 ρ + 线性增长的 φ | 幅值正确、角度正确 |
| SVPWM | 极坐标 \((\rho,\varphi)\) | 三路马鞍波 duty | 七段式调制正确 |
| duty → PWM | 马鞍波 duty | duty 越大、PWM 高电平越宽 | 占空比映射正确 |

#### 4.3.2 核心流程

做波形验证的标准四步：

1. **设对显示**：所有 `signed` 信号先 `Signed Decimal` 再 `Analog→Step`（否则负半周变形，结论全错）。
2. **对齐输入**：先确认激励本身符合预期（如 `ia/ib/ic` 确实是三相正弦），再去看输出。输入错了，输出再好看也白搭。
3. **核对应然形态**：按下文清单逐条对照。
4. **解释偏差**：若波形不完美（如 Park 输出不是严格直流、有轻微纹波），要能解释偏差来源（定点近似、采样离散化），判断它是否在可接受范围内。

#### 4.3.3 源码精读

下面把四个关键特征逐一对应到 testbench 的实际信号和代码注释上。

**特征一：αβ 正交（Clark 验证）**

[SIM/tb_clark_park_tr.v:75-77](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L75-L77) 的注释写明了应然形态：

```verilog
.o_ialpha ( ialpha ),  // Iα ，应该为初相位为 (4/3)*π 的正弦波
.o_ibeta  ( ibeta  )   // Iβ ，相位应该比 Iα 滞后 (1/2)*π ，也就是与 Iα 正交
```

验证方法：把 `ialpha` 和 `ibeta` 设成 Analog，找一个 `ialpha` 过零点的时刻，看此刻 `ibeta` 是否在峰值（或谷值）。若是，则相位差恰为 \(\pi/2\)，正交性成立。

**特征二：dq 坍缩成直流（Park 验证）**

[SIM/tb_clark_park_tr.v:89-90](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L89-L90)：

```verilog
.o_id ( id ),  // Id ，应该变为一个定值
.o_iq ( iq )   // Iq ，应该变为一个定值
```

**为什么 Park 能把正弦"坍缩"成直流？** 关键在于 testbench 给 Park 的旋转角 `psi` 与 \(\alpha\beta\) 矢量的旋转角是**同步增长**的：

[SIM/tb_clark_park_tr.v:84](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L84) —— `psi = theta + 12'd512`：

```verilog
.psi ( theta + 12'd512 ),  // input : θ + (1/4)*π
```

\(\alpha\beta\) 是由 `theta` 合成的，矢量角随 `theta` 线性增长；`psi` 也随 `theta` 线性增长。两者增长速率相同，故 \(\psi - \text{矢量角} = \text{常数}\)。Park 是一次保幅旋转，旋转角与信号同步时，旋转的交流量就被"解调"成直流量。固定偏置 `512`（\(\pi/4\)）只决定这个直流被分到 `id` 还是 `iq` 多少，不影响"坍缩"本身。

> 验证方法：把 `id`/`iq` 设成 Analog，应看到两条**近似水平线**（带轻微纹波）。纹波来自 [u2-l3](./u2-l3-clark-transform.md)/[u2-l4](./u2-l4-park-and-sincos.md) 讲过的定点近似（√3 用移位逼近、乘积取高位），属于可接受误差，正是 [u4-l1](./u4-l1-fixed-point-and-saturation.md) 所说"系数误差交 PID 吸收"的体现。

**特征三：ρ 恒定、φ 跟随 θ（cartesian2polar 验证）**

[SIM/tb_svpwm.v:47-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L47-L52)：

```verilog
.i_x    ( x / 16'sd5 ),   // ±3277 的余弦波
.i_y    ( y / 16'sd5 ),   // ±3277 的正弦波
.o_rho  ( rho ),          // ρ，应该一直等于或近似 3277
.o_theta( phi )           // φ，应该是一个接近 θ 的角度值
```

\((x,y)=(\cos\theta,\sin\theta)/5\) 到原点距离恒为 1（缩放后恒为 3277），故 \(\rho=\sqrt{x^2+y^2}\approx 3277\) 恒定；角度 \(\varphi=\arctan2(y,x)\approx\theta\) 线性增长。验证：`rho` 应近似水平线 3277，`phi` 应与 `theta` 形状一致（锯齿状递增、到 4095 回绕）。

**特征四：马鞍波 duty 决定 PWM 占空比（SVPWM 验证）**

[SIM/tb_svpwm.v:54-64](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L54-L64) 例化 `svpwm`，内部产生 `pwma_duty/pwmb_duty/pwmc_duty` 三路马鞍波。注意这三个信号**不在顶层**，所以 testbench 专门 dump 了 `u_svpwm` 实例（见 [SIM/tb_svpwm.v:13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L13)）。

README 明确给出了最后的判据：

[README.md:510](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L510) —— "pwma_duty、pwmb_duty、pwmc_duty 分别决定了 pwm_a, pwm_b, pwm_c 的占空比"。

验证方法：放大到单个 PWM 周期，量 `pwm_a` 高电平的宽度，它与 `pwma_duty` 的数值成正比——duty 越大，高电平越宽。具体的占空比公式 \(D_{\text{high}}=\text{pwma\_duty}/1024\) 推导见 [u2-l7](./u2-l7-svpwm.md)。

#### 4.3.4 代码实践

**实践目标**：对着 tb_svpwm 的波形，一次性走完"特征三 + 特征四"两条验证，体会"波形即验证"的完整流程。

**操作步骤**：

1. 跑 tb_svpwm 生成 `dump.vcd`，gtkwave 打开（操作见 [u1-l4 第 4.1.4 节](./u1-l4-iverilog-simulation.md#414-代码实践)）。
2. 把顶层 `theta, x, y, rho, phi` 和 `u_svpwm` 内部的 `pwma_duty, pwmb_duty, pwmc_duty` 以及 `pwm_a, pwm_b, pwm_c` 都加进来；signed 的设成 Analog，PWM 数字信号保持 0/1。
3. 先看 `rho`：确认它近似一条 3277 的水平线。
4. 再看 `pwma_duty` 等三路：确认是马鞍波、三相之间有相位差。
5. 放大到一个 PWM 周期，量 `pwm_a` 高电平宽度，与 `pwma_duty` 当前值对照。

**需要观察的现象**：

- `rho` 稳在 3277 附近（有小纹波）。
- `pwma_duty/pwmb_duty/pwmc_duty` 为马鞍波，互差 120°。
- `pwm_a` 高电平宽度随 `pwma_duty` 增大而增大。

**预期结果**：以上三条同时满足，则 cartesian2polar 与 svpwm 的数学正确性同时得到验证（对应 README"图5/图6"）。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：Park 验证时，`id/iq` 不是严格的水平直线，而是带纹波的"近似直流"。请解释纹波的两个主要来源。

**参考答案**：① Clark 里 √3 用 9 项移位之和逼近（见 [u2-l3](./u2-l3-clark-transform.md)），有约 0.0068% 的误差，使 \(\alpha\beta\) 本身不是完美正弦；② Park 的 16×16 乘积统一取高 16 位 `[31:16]`（见 [u2-l4](./u2-l4-park-and-sincos.md)），带来量化误差。两者都是定点近似的代价，可被 PID 吸收（见 [u4-l1](./u4-l1-fixed-point-and-saturation.md)）。

**练习 2**：如果 Park 的 `psi` 写成 `theta + 12'd512` 时 dq 能坍缩成直流，那如果把 `psi` 改成 `2*theta`（转速翻倍），dq 还会是直流吗？为什么？

**参考答案**：不会。坍缩的前提是 `psi` 与 \(\alpha\beta\) 矢量角**同步增长**（速率相同，差为常数）。`2*theta` 的增长速率是矢量角的两倍，差不再是常数，Park 会把信号"过度旋转"，dq 重新变成交流（且频率翻倍）。这个反例能帮你确认"同步"才是坍缩的本质。

---

### 4.4 为新模块写 testbench 的范式

#### 4.4.1 概念说明

前两节讲了"为什么仿真"和"用什么喂、怎么看"。本节把两个 testbench 里重复出现的结构抽象成一套**写 testbench 的范式**，让你能给任意新模块套用。这套范式由五个固定要素组成，缺一不可：

| 要素 | 作用 | 本项目里的写法 |
| --- | --- | --- |
| ① 时钟 `clk` | 驱动所有时序逻辑 | `always #(13563) clk = ~clk;` |
| ② 复位 `rstn` | 上电先复位再工作 | `initial begin repeat(4) @(posedge clk); rstn<=1'b1; end` |
| ③ dump 开关 | 决定录哪些信号 | `initial $dumpvars(1, <作用域>);` |
| ④ 激励主循环 | 给 DUT 喂随时间变化的输入 | `for(...) begin ... @(posedge clk); ... end` |
| ⑤ 结束 | 让仿真停下来 | `$finish;` |

掌握这五要素后，写一个新 testbench 就是"填空"：换 DUT、换激励、换想 dump 的信号。

#### 4.4.2 核心流程

写 testbench 的通用骨架（伪代码）：

```
module tb_xxx;
    ① 时钟/复位声明
    ③ initial $dumpvars(1, tb_xxx);   // 想看子模块内部就再加一句
    ② DUT 的输入 reg 声明 + 输出 wire 声明
       例化 DUT
    ④ initial begin
            while(~rstn) @(posedge clk);   // 等复位释放
            for(...) begin                  // 激励主循环
                给输入赋值;
                @(posedge clk);             // 推进节拍
            end
    ⑤      $finish;
       end
endmodule
```

这个骨架和两个现有 testbench 的主循环几乎一字不差（见 4.4.3），说明它是可复用的。

#### 4.4.3 源码精读

把两个 testbench 的主循环并排看，五要素一目了然。

[SIM/tb_clark_park_tr.v:96-106](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L96-L106)：

```verilog
initial begin
    while(~rstn) @ (posedge clk);        // ② 等复位释放
    for (i=0; i<1000; i=i+1) @ (posedge clk) begin   // ④ 激励主循环
        en_theta <= 1'b1;                 //    喂使能脉冲
        theta    <= theta + 12'd10;       //    递增角度
        @ (posedge clk);
        en_theta <= 1'b0;
        repeat (9) @ (posedge clk);       //    空等 9 拍控制节拍
    end
    $finish;                              // ⑤ 结束
end
```

[SIM/tb_svpwm.v:69-77](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L69-L77) 结构相同，只是激励换成"每 2048 拍推进一个 PWM 周期"：

```verilog
initial begin
    while(~rstn) @ (posedge clk);        // ②
    for(i=0; i<200; i=i+1) begin          // ④
        theta <= 25 * i;
        repeat(2048) @ (posedge clk);     //    等满一个 PWM 周期
        $display("%d/200", i);
    end
    $finish;                              // ⑤
end
```

要素①②③则在两个文件开头完全一致（详见 [u1-l4 第 4.2.3/4.3.3 节](./u1-l4-iverilog-simulation.md)），这里只点出关键点：

- 时钟半周期 `13563` 对应 36.864 MHz（与真实主时钟一致），见 [SIM/tb_clark_park_tr.v:17](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L17)。
- 复位 `repeat(4) @(posedge clk)` 模拟"上电复位几拍后释放"，见 [SIM/tb_clark_park_tr.v:18](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L18)。
- 想看 DUT 内部信号就再加一句 `initial $dumpvars(1, u_xxx);`，见 [SIM/tb_svpwm.v:13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_svpwm.v#L13)。

**迁移到新模块时的"DUT 适配"**：骨架五要素不变，只需根据新模块的接口调整两点：一是端口连接（哪些是 reg 激励、哪些是 wire 观察），二是**激励节拍要匹配 DUT 的流水线深度**。这点对 `pi_controller` 尤其重要——它是 5 级流水线（`i_en→…→o_en`，共 5 拍），见 [RTL/foc/pi_controller.v:62-75](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L62-L75)，所以喂 `i_en` 脉冲的间隔必须大于 5 拍，否则前一次计算还没走完流水线就被下一次打断。综合实践里我们取间隔 10 拍，留足余量。

#### 4.4.4 代码实践

**实践目标**：用五要素骨架，给一个最简单的模块——`sincos` 本身——套一个微型 testbench，跑通"填空"流程。

**操作步骤**：

1. 新建 `SIM/tb_sincos.v`（自己练习用），按骨架填空：声明 `clk/rstn`、`i_en=1'b1`、递增的 `theta`，例化 `sincos`，主循环让 `theta` 递增 1000 次，`$dumpvars(1, tb_sincos)`，最后 `$finish`。
2. 编译运行：`iverilog -g2001 -o sim.out tb_sincos.v ../RTL/foc/sincos.v && vvp -n sim.out`。
3. gtkwave 打开，把 `theta, o_sin, o_cos` 设成 Analog。

**需要观察的现象**：

- `o_sin` 是正弦波，`o_cos` 是余弦波，两者正交（相位差 \(\pi/2\)）。

**预期结果**：正交的 sin/cos 波形出现，说明骨架填空成功、`sincos` 作为 DUT 工作正常。**待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：骨架里 `while(~rstn) @(posedge clk);` 这一行如果删掉，会发生什么？

**参考答案**：主循环会在复位还没释放时就开始喂激励。此时 DUT 内部还在复位状态（寄存器为初值），前几个节拍的激励会被"吞掉"或与复位值混在一起，导致波形开头一段不正确。虽然后续复位释放后能恢复，但开头的激励丢失会让波形解读变麻烦。所以这一行的作用是"等复位稳定后再开始喂激励"。

**练习 2**：为什么写 testbench 时，给 DUT 的输入通常声明成 `reg`，而输出声明成 `wire`？

**参考答案**：因为 testbench 要在 `initial/always` 块里**主动驱动**输入（赋值），能被过程语句驱动的必须是 `reg`；而输出是 DUT 驱动、testbench 只**观察**的，必须用 `wire` 连续驱动。这与 [u1-l3](./u1-l3-fpga-top.md) 讲过的"`reg`/`wire` 取决于驱动方式"是同一条规则。

---

## 5. 综合实践

把本讲四节串起来：用 4.1 的"开环切片"策略、4.2 的"sincos 当信号源"技巧、4.3 的"波形即验证"判据、4.4 的"五要素骨架"，给全库唯一缺 testbench 的核心模块——`pi_controller`——写一个 testbench。

**任务**：为 `pi_controller.v` 写 testbench `tb_pi_controller.v`。给定一组正弦形的 `i_real`（用 `sincos` 产生）和恒定的 `i_aim`，观察 `o_value` 是否朝"使误差趋零"的方向响应，并用 gtkwave 的 Analog 显示验证 PI 的动态响应。

### 5.1 设计思路

先回顾 DUT 的接口和流水线（详见 [u2-l5](./u2-l5-pi-controller.md)）：

[RTL/foc/pi_controller.v:9-19](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L9-L19)：

```verilog
module pi_controller (
    input  wire               rstn, clk, i_en,
    input  wire        [30:0] i_Kp, i_Ki,
    input  wire signed [15:0] i_aim, i_real,
    output reg                o_en,
    output wire signed [15:0] o_value    // = value[31:16]
);
```

激励设计：

- `i_aim` 恒定 `+200`（目标电流）。
- `i_real` 用 `sincos` 产生正弦，再 `/82` 把振幅从 ±16384 缩到 ±200（套用 4.2 的缩放技巧）。
- `i_Kp = 32768`、`i_Ki = 1024`。注意 `o_value = value[31:16]`（见 [RTL/foc/pi_controller.v:25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L25)），相当于把 Kp/Ki 再除以 \(2^{16}\)，所以 `Kp=32768` 的实际比例增益约 0.5，`Ki=1024` 的实际积分增益约 0.0156——量级合理，便于观察。
- `i_en` 每 10 拍给一个脉冲（大于 5 级流水线深度，见 4.4.3）。

### 5.2 完整 testbench 代码

下面是可直接编译运行的完整 testbench（**示例代码**，不是项目原有文件，请自行另存为 `SIM/tb_pi_controller.v`）：

```verilog
//--------------------------------------------------------------------------------------------------------
// Module  : tb_pi_controller
// Type    : simulation, top   (示例代码，供练习使用)
// Standard: Verilog 2001 (IEEE1364-2001)
// Function: testbench for pi_controller.v
//           用 sincos 产生正弦形的 i_real，恒定 i_aim，观察 o_value 是否朝"使误差趋零"的方向响应。
//--------------------------------------------------------------------------------------------------------
module tb_pi_controller();

initial $dumpvars(1, tb_pi_controller);          // ③ dump 顶层

// ① 时钟
reg rstn = 1'b0;
reg clk  = 1'b1;
always #(13563) clk = ~clk;                       // 36.864MHz
initial begin repeat(4) @(posedge clk); rstn<=1'b1; end   // ② 复位

// DUT 输入 (reg) / 输出 (wire)
reg                 i_en  = 1'b0;
reg  signed [15:0]  i_aim = 16'sd200;             // 目标电流恒定 +200
reg  [30:0]         i_Kp  = 31'd32768;            // 比例增益
reg  [30:0]         i_Ki  = 31'd1024;             // 积分增益
wire signed [15:0]  i_real;                       // 实际电流 = 正弦 ±200
wire                o_en;
wire signed [15:0]  o_value;

// 用 sincos 当正弦信号源 (真实设计中 i_real 来自 park_tr，不是 sincos)
reg  [11:0] theta = 0;
wire signed [15:0] sin_raw;
assign i_real = sin_raw / 16'sd82;                // ±16384 / 82 ≈ ±200

sincos u_sincos (
    .rstn    ( rstn    ),
    .clk     ( clk     ),
    .i_en    ( 1'b1    ),
    .i_theta ( theta   ),
    .o_en    (         ),
    .o_sin   ( sin_raw ),
    .o_cos   (         )
);

pi_controller u_pi (
    .rstn   ( rstn   ),
    .clk    ( clk    ),
    .i_en   ( i_en   ),
    .i_Kp   ( i_Kp   ),
    .i_Ki   ( i_Ki   ),
    .i_aim  ( i_aim  ),
    .i_real ( i_real ),
    .o_en   ( o_en   ),
    .o_value( o_value)
);

// ④ 激励主循环
integer i;
initial begin
    while(~rstn) @(posedge clk);
    for (i=0; i<2000; i=i+1) begin
        @(posedge clk);
        theta <= theta + 12'd10;                  // i_real 缓慢正弦摆动
        i_en  <= 1'b1;                            // 单拍使能脉冲
        @(posedge clk);
        i_en  <= 1'b0;
        repeat(9) @(posedge clk);                 // 每 10 拍喂一次，> 5 级流水线
    end
    $finish;                                      // ⑤ 结束
end

endmodule
```

### 5.3 编译运行

在 `SIM/` 目录（或自建练习目录）执行（Linux 等价命令；Windows 可仿照现有 `.bat`）：

```bash
cd SIM
rm -f sim.out dump.vcd
iverilog -g2001 -o sim.out tb_pi_controller.v ../RTL/foc/sincos.v ../RTL/foc/pi_controller.v
vvp -n sim.out
gtkwave dump.vcd &
```

> 注意：编译时要把 `tb_pi_controller.v`、`sincos.v`、`pi_controller.v` 三个文件一起喂给 iverilog（testbench 例化 `pi_controller`，而 `pi_controller` 本身不例化 `sincos`——这里 `sincos` 是 testbench 直接当信号源用的，所以也要参与编译）。

### 5.4 波形解读（验证清单）

把 `i_aim, i_real, o_value, o_en` 加进波形区，`signed` 的设成 `Signed Decimal → Analog → Step`，按下表核对：

| 观察对象 | 应然形态 | 证明了什么 |
| --- | --- | --- |
| `i_real` | ±200 正弦 | 信号源工作正常 |
| `o_value` 的**符号** | 始终 ≥0（因为 `i_aim=200` 恒大于 `i_real`，误差恒正） | PI 方向正确，朝"增大 i_real 以减小误差"的方向输出 |
| `o_value` 的**大小** | `i_real` 越接近 +200（误差越小）`o_value` 越小；`i_real≈-200`（误差最大）`o_value` 越大 | 比例项 \(K_p\cdot e\) 起作用 |
| `o_value` 的**基线漂移** | 随时间整体上扬（误差持续为正，积分项 \(K_i\cdot\Sigma e\) 累积） | 积分项起作用 |
| `o_en` | 每 10 拍一个脉冲，且滞后 `i_en` 恰好 5 拍 | 5 级流水线节拍正确 |

### 5.5 诚实的限制

必须指出（这正是 4.1 方法论的体现）：**这个 testbench 是开环的，没有电机模型**，`i_real` 是 testbench 强行注入的正弦，不会因为 `o_value` 变大而真的被"拉向 `i_aim`"。所以我们**看不到真正的闭环收敛**（`i_real` 不会趋近 200），只能验证：

1. `o_value` 的**方向**与误差一致（朝减小误差的方向）；
2. 比例项、积分项各自的动态特征；
3. 流水线节拍正确。

要看真正的"目标值突变后实际值跟随"的闭环曲线，只能像 README 那样在真实电机上用串口测（见 [README.md:405-417](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L405-L417) 的跟随数据）。本实践的价值在于：**它是开环切片仿真能验证 PI 的上限**——方向、动态、节拍。这恰好和 4.1 的结论闭环呼应。

> 若 `o_value` 一开始就饱和在 ±32767 不动，说明 `i_Kp`/`i_Ki` 给得太大或 `i_real` 振幅太大，把它们调小再试。具体数值**待本地验证**，本实践不假装已替你运行。

## 6. 本讲小结

- 本项目**只仿真了子模块、没仿真整个 FOC 闭环**，根本原因是缺电机的 Verilog 模型；这推广成一条"**开环切片仿真**"策略——把闭环拆成输入可合成、输出可判定的子模块分别测（`clark_tr`/`park_tr`/`cartesian2polar`/`svpwm` 已覆盖，`pi_controller`/`hold_detect` 等未覆盖）。
- `sincos` 模块在 testbench 里被**挪用成可控正弦信号源**：递增 `i_theta` 得正弦波，加相位偏置得不同初相，`/常数` 缩放振幅；tb_clark_park_tr 用它合成三相，tb_svpwm 用它取正交 `(x,y)`。
- **波形即验证**：每个变换都有应然形态——Clark 出正交 αβ、Park 把同步旋转的 αβ 坍缩成直流 dq、cartesian2polar 出恒定 ρ、SVPWM 出马鞍波 duty 且 duty 越大 PWM 占空比越大。偏差（如 dq 的纹波）来自定点近似，可被 PID 吸收。
- 写 testbench 的**五要素范式**：时钟 `clk`、复位 `rstn`、`$dumpvars`、激励主循环、`$finish`；两个现有 testbench 的主循环结构几乎相同，迁移到新模块只需换 DUT 与激励，并让激励节拍匹配 DUT 的流水线深度。
- 综合实践给 `pi_controller` 写了 testbench，验证了 PI 的**方向、动态、节拍**；但由于没有电机模型，看不到真正的闭环收敛——这正是开环切片仿真的上限。

## 7. 下一步学习建议

- 想补全仿真覆盖，可仿照综合实践，为 `hold_detect.v` 写 testbench：给它喂一段模拟"三相 PWM 同时为低"的 `in` 信号，观察 `sn_adc` 脉冲是否在 `SAMPLE_DELAY` 拍后出现（接口与原理见 [u2-l8](./u2-l8-hold-detect.md)）。
- 想真正做闭环仿真，需要引入电机模型，可参考 [u4-l4 二次开发与系统扩展](./u4-l4-extension-and-development.md) 里关于 MCU+FPGA 协同与外环扩展的讨论。
- 回顾定点与饱和细节，见 [u4-l1 定点数运算与饱和保护](./u4-l1-fixed-point-and-saturation.md)——它解释了本讲看到的"dq 纹波"和"系数误差交 PID"的数学根据。
- 若要重新熟悉工具链操作（安装 iverilog、`.bat` 详解、Analog 显示设置），回到 [u1-l4 用 iverilog 跑仿真并看波形](./u1-l4-iverilog-simulation.md)。
