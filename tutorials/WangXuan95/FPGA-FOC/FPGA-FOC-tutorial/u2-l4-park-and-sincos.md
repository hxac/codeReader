# Park 变换与 sincos 计算器

## 1. 本讲目标

本讲紧接 [u2-l3 Clark 变换](u2-l3-clark-transform.md)。Clark 把三相电流 `ia/ib/ic` 投影成了定子直角坐标系的 `iα/iβ`，但这仍是一个**随转子转动而旋转**的交流矢量，直接拿来控电流会很麻烦。Park 变换的任务，就是再把这个旋转矢量“旋”到**和转子一起转的坐标系**里，让它变成**直流**——这样 PI 控制器才能像调普通直流量一样去调它。

学完本讲你应当能够：

- 写出 Park 变换的公式 \(i_d=i_\alpha\cos\psi+i_\beta\sin\psi\)、\(i_q=i_\beta\cos\psi-i_\alpha\sin\psi\)，并解释它为何能把交流“解调”成直流。
- 读懂 `park_tr.v` 如何用 4 个乘法 + 2 个加减 + 一级取高位 `[31:16]` 实现这个变换，并说清楚这个取高位带来的定点缩放关系。
- 读懂 `sincos.v` 如何用 `IDLE→S1→…→S5` 状态机 + 一张余弦 ROM，分象限、分两次查表，算出 \(\sin\psi\) 与 \(\cos\psi\)。
- 说清楚全库的两条定点约定：角度 `0~4095` 对应 `0~2π`，三角函数 `-1~+1` 对应 `-16384~+16384`。

## 2. 前置知识

- **坐标系旋转（复习）**：定子坐标系 αβ 是**固定不动的**；转子坐标系 dq 是**贴在转子上、跟着转子一起转**的。两者之间差一个电角度 \(\psi\)。Park 变换就是把 αβ 里的矢量“反向旋转 \(\psi\)”搬到 dq 里。用复数表示最直观：

\[
i_d + j\,i_q = (i_\alpha + j\,i_\beta)\,e^{-j\psi}
\]

展开就得到上面的两个公式。关键是：如果电流矢量本身就在以 \(\theta\) 的角度旋转（\(i_\alpha+j\,i_\beta = A\,e^{j\theta}\)），而我们令 \(\psi=\theta\) 跟着它转，那么旋转就被抵消，\(i_d+i_q\) 变成一个**常数**——这就是“把交流变成直流”。

- **有符号定点乘法（复习）**：两个 16 位有符号数相乘，结果是 32 位有符号数。要从 32 位结果里取回 16 位，最省事的做法是直接取高 16 位 `result[31:16]`，它等价于“除以 65536 再四舍五入向下”。本讲会反复用到这一点。

- **单周期脉冲握手（复习）**：本库所有 FOC 子模块都用 `i_en`/`o_en` 一拍高电平脉冲表示“我的输入/输出这一拍有效”。数据沿流水线逐级下传，脉冲也跟着下传，保证数据与节拍对齐。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [RTL/foc/park_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v) | Park 变换器。输入 `iα/iβ` 与电角度 `ψ`，输出 `id/iq`。内部例化一个 `sincos`。 |
| [RTL/foc/sincos.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v) | sin/cos 计算器。输入角度 `0~4095`（对应 `0~2π`），输出 `sin/cos`（`-1~+1` 对应 `-16384~+16384`）。 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 把 `park_tr` 连进 FOC 通路：`ψ` 来自角度换算块，`iα/iβ` 来自 `clark_tr`，`id/iq` 送给两个 PI 控制器。 |
| [SIM/tb_clark_park_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v) | 仿真：用 `sincos` 当信号源合成三相正弦，验证 Clark 得正交 αβ、Park 得近似直流的 dq。 |

数据流位置（承接 [u2-l1](u2-l1-foc-top-overview.md) 的全景）：

```
... → clark_tr → (iα, iβ, en_ialphabeta) → park_tr → (id, iq, en_idq) → pi_controller×2 → ...
                                   ↑ ψ (电角度，来自 foc_top 角度换算块)
```

## 4. 核心概念与源码讲解

### 4.1 Park 变换与 park_tr.v

#### 4.1.1 概念说明

Clark 变换把三相电流“降维”成了两相 `iα/iβ`，但它们仍是**随时间变化的交流量**（转子一转，它们就呈正弦摆动）。直接对一个交流量做 PI 控制很别扭：误差一会儿正一会儿负，积分项无法稳定累积。

Park 变换解决的就是这个问题。它把 αβ 坐标系里的矢量**再旋转一个电角度 \(-\psi\)**，搬到一个**贴着转子、随转子一起转**的坐标系 dq 里。在这个坐标系里看，电流矢量不再旋转，变成了**常数（直流量）**。于是 PI 控制器就可以像调一个普通直流电压那样去调 `id`、`iq`。

数学上：

\[
\begin{aligned}
i_d &= i_\alpha\cos\psi + i_\beta\sin\psi \\
i_q &= i_\beta\cos\psi - i_\alpha\sin\psi
\end{aligned}
\]

这就是 `park_tr.v` 要实现的两条式子。它需要 4 个乘积项：`iα·cosψ`、`iα·sinψ`、`iβ·cosψ`、`iβ·sinψ`，然后两个加、两个减。注意 `iα·cosψ` 和 `iβ·sinψ` 这一项在 `id` 里是加，`iα·sinψ` 和 `iβ·cosψ` 在 `iq` 里组合——4 个乘积正好被两条输出复用，很经济。

#### 4.1.2 核心流程

`park_tr.v` 的数据通路分两级流水线，外加一个一直在线运行的 `sincos` 子模块：

```
        ┌───────────────┐
   ψ ─→ │   sincos      │── sin_psi, cos_psi  (一直在线，每 ~6 拍刷新一次)
        └───────────────┘            │
                                     ▼
  iα ─┬───────────────→ 乘: iα·cosψ ┐
      └───────────────→ 乘: iα·sinψ ┤  [级1寄存器: alpha_cos, alpha_sin, beta_cos, beta_sin]
  iβ ─┬───────────────→ 乘: iβ·cosψ ┤        ↑ i_en 在此拍锁存 (en_s1)
      └───────────────→ 乘: iβ·sinψ ┘
                                     │
          ide = alpha_cos + beta_sin (组合逻辑)   →  [级2] o_id <= ide[31:16]
          iqe = beta_cos  - alpha_sin (组合逻辑)  →  [级2] o_iq <= iqe[31:16]
                                                          ↑ en_s1 在此拍锁存为 o_en
```

执行过程（从 `i_en` 脉冲算起）：

1. **第 0 拍**：上游 `clark_tr` 拉高 `i_en`，同时 `iα/iβ` 与 `ψ` 已就绪。`sincos` 因 `i_en=1'b1` 一直在转，此刻 `sin_psi/cos_psi` 已是当前 `ψ` 对应的值。
2. **第 1 拍（级 1 锁存）**：在时钟上升沿把 4 个乘积和 `en_s1<=i_en` 一起写进寄存器。组合逻辑立刻算出 `ide`/`iqe`（32 位）。
3. **第 2 拍（级 2 锁存）**：`o_en<=en_s1` 拉高一个脉冲；同时 `o_id<=ide[31:16]`、`o_iq<=iqe[31:16]`。下游 PI 控制器看到 `en_idq` 脉冲，取走 `id/iq`。

所以 `park_tr` 自身是 **2 级流水线**（这与 [u2-l1](u2-l1-foc-top-overview.md) 给出的“电流重构 1 + Clark 3 + Park 2 = 6 拍”一致）。`sincos` 的延迟不算在这 2 拍里，因为它**持续预计算**着 `ψ` 的正余弦，`park_tr` 随用随取。

#### 4.1.3 源码精读

先看端口与内部信号。注意 `sin_psi/cos_psi` 是 16 位有符号，注释点明了定点约定：

[RTL/foc/park_tr.v:L9-L19](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L9-L19) — 模块端口与 `sin_psi/cos_psi` 的定点注释（`-1~+1` 映射到 `-16384~+16384`）。

`park_tr` 直接例化一个 `sincos`，把 `ψ` 喂给它，并**让它的 `i_en` 恒为 `1'b1`**：

[RTL/foc/park_tr.v:L27-L35](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L27-L35) — 例化 `sincos`，`i_en` 恒为 `1'b1`（持续计算），`o_en` 悬空不接（`park_tr` 用自己的 `i_en` 节拍，不依赖 `sincos` 的握手）。

> 为什么 `i_en=1'b1`？因为 `sincos` 是状态机，一次计算要 6 拍；而 FOC 控制周期是 2048 拍。让它一直空转预计算，`park_tr` 需要时直接拿最新结果即可，省掉“要用了才启动”的等待。代价只是几个寄存器的动态功耗。

级 1 的乘法与锁存——4 个乘积并行算出、一起打拍：

[RTL/foc/park_tr.v:L37-L46](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L37-L46) — 第 1 级：`en_s1<=i_en`，并把 `iα·cosψ`、`iα·sinψ`、`iβ·cosψ`、`iβ·sinψ` 四个 32 位乘积写入寄存器。

组合逻辑做加减，得到 32 位的 `ide`/`iqe`：

[RTL/foc/park_tr.v:L24-L25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L24-L25) — `ide = alpha_cos + beta_sin`（即 \(i_\alpha\cos\psi+i_\beta\sin\psi=i_d\)），`iqe = beta_cos - alpha_sin`（即 \(i_\beta\cos\psi-i_\alpha\sin\psi=i_q\)）。

级 2 取高位并发出 `o_en` 脉冲——这是定点缩放的关键：

[RTL/foc/park_tr.v:L48-L57](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L48-L57) — 第 2 级：`o_id <= ide[31:16]`、`o_iq <= iqe[31:16]`，即把 32 位乘加结果右移 16 位取回 16 位输出。

**定点缩放关系（本讲重点之一）。** `sincos` 输出的 `sin_psi = 2^{14}\sin\psi`（因为 `±1` 被映射成 `±16384=±2^{14}`，详见 4.2）。于是：

\[
\text{ide} = i_\alpha\cdot(2^{14}\cos\psi) + i_\beta\cdot(2^{14}\sin\psi) = 2^{14}\,(i_\alpha\cos\psi+i_\beta\sin\psi)
\]

而 `o_id = ide[31:16] = ide / 2^{16}`，所以：

\[
o_{id} = \frac{2^{14}(i_\alpha\cos\psi+i_\beta\sin\psi)}{2^{16}} = \frac{i_\alpha\cos\psi+i_\beta\sin\psi}{4} = \frac{i_d}{4}
\]

也就是说，`park_tr` 输出的 `id/iq` 比理论值**整体小了 4 倍**。这并非 bug，而是工程取舍：

- 取 `ide[31:16]` 只需一条“取高半字”的连线，综合后零成本；若要精确还原 \(i_d\) 得取 `ide[29:14]` 之类的非边界切片，反而啰嗦。
- 这个 `1/4` 是一个**常数增益**，会被下游 PI 控制器的 `Kp/Ki` 整定掉（FOC 是线性系统，常数增益等价于改一下 PI 系数）。这正是全库反复出现的“系数不必精确、误差交 PID”思想。

**溢出校核**：`iα/iβ` 是 16 位有符号（范围 ±32767），`sin/cos` 是 ±16384。单个乘积最大约 `32767×16384 ≈ 5.4×10^8`，`ide` 是两个这样的乘积之和，最大约 `1.08×10^9`，远小于 32 位有符号上限 `≈2.15×10^9`，故 `[31:16]` 取高位安全无溢出。

最后看 `foc_top.v` 里如何把它接进通路：

[RTL/foc/foc_top.v:L140-L150](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L140-L150) — `park_tr` 的例化：`ψ` 接 `psi`（来自角度换算块），`i_en` 接 `en_ialphabeta`（Clark 的输出有效脉冲），`o_en` 产生 `en_idq`，`id/iq` 送往下文两个 `pi_controller`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 Park 变换把旋转的正弦“坍缩”成直流，并用波形验证 `o_id = ide[31:16]` 的定点缩放关系。

**操作步骤**：

1. 进入 `SIM/` 目录，运行 `tb_clark_park_tr_run_iverilog.bat`（Linux 下手动执行等价命令，参见 [u1-l4](u1-l4-iverilog-simulation.md)）：

   ```
   iverilog -g2001 -o sim.out tb_clark_park_tr.v ../RTL/foc/sincos.v ../RTL/foc/clark_tr.v ../RTL/foc/park_tr.v
   vvp -n sim.out
   ```

2. 用 gtkwave 打开 `dump.vcd`。把 `theta`、`ialpha`、`ibeta`、`id`、`iq` 都设为 **Signed Decimal → Analog (Step)**。
3. 选中 `u_park_tr` 实例，把内部信号 `alpha_cos`、`beta_sin`、`ide`、`en_s1` 也加进来（`ide` 是 32 位，看 Signed Decimal；`o_id` 看 Signed Decimal/Analog）。
4. 在 `id`/`iq` 已经“坍缩”成近似直流的稳态区，读出同一时刻的 `ialpha`、`ibeta`、`sin_psi`、`cos_psi`、`ide`、`o_id`。

**需要观察的现象**：

- `ialpha`、`ibeta` 仍是正弦波（且相位差约 \(\pi/2\)，这是 Clark 的功劳）。
- `id`、`iq` 不再是正弦，而是**两条近似水平的直线**（只在很小的范围里抖动，抖动来自 `sincos` 的查表量化与移位近似的残余）。

**预期结果（为何会坍缩）**：testbench 里三相电流是用 `sincos` 合成的、角频率由 `theta` 驱动的正弦，所以 `iα+j·iβ` 是一个以 `theta` 为角度旋转的矢量 \(A e^{j\theta}\)。而 `park_tr` 的 `psi = theta + 512`（即 \(\theta + \pi/4\)，见 [SIM/tb_clark_park_tr.v:L84](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/SIM/tb_clark_park_tr.v#L84)），也就是 `ψ` **跟着 `theta` 一起转**，只差一个常数偏移 \(\delta=\pi/4\)。于是：

\[
i_d + j\,i_q = A\,e^{j\theta}\cdot e^{-j\psi} = A\,e^{j(\theta-\psi)} = A\,e^{-j\delta} = A(\cos\delta - j\sin\delta)
\]

旋转被抵消，\(i_d,i_q\) 变成两个**常数**；偏移 \(\delta\) 只是把这个常数矢量再转一个固定角度，决定的是 `id` 与 `iq` 的**比例**，而不是让它俩重新变成正弦。

**验证定点缩放**：对同一时刻的读数，检查：

\[
4 \times o_{id} \;\approx\; i_\alpha\cos\psi + i_\beta\sin\psi \;=\; \text{ide} / 2^{14}
\]

或者更直接——`o_id` 的 16 位正好就是 `ide` 的高 16 位，在波形里比对 `o_id`（Signed）与 `ide[31:16]` 应严格相等（除了符号位扩展的视角差异）。

> 若无法本地运行，以上数值关系标注为「待本地验证」。但“`psi` 跟随 `theta` 时 `id/iq` 坍缩为直流”这一结论由上述推导保证，不依赖运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 testbench 里的 `psi = theta + 12'd512` 改成 `psi = theta`（偏移为 0），`id` 和 `iq` 会变成什么样？哪个会是 0？

**答案**：`id+iq = A·e^{-j·0} = A`，即 `id` 变成一个正的常数（等于矢量幅值，再乘上 `1/4` 缩放），`iq` 变为 0。因为旋转矢量与坐标系完全同步，电流矢量在 q 轴上没有投影。

**练习 2**：`park_tr` 为什么不直接 `o_id <= ide[29:14]`（这样能精确还原 \(i_d\)，不带 `1/4` 增益）？

**答案**：取 `ide[31:16]` 是 32 位结果的高半字，综合后就是一组连线，零成本；取 `[29:14]` 是非边界切片，需要额外的选通逻辑。而 `1/4` 这个常数增益会被 PI 的 `Kp/Ki` 整定吸收，对线性 FOC 无害，所以作者选了最省资源的写法。

**练习 3**：`park_tr` 把 `sincos` 的 `o_en` 悬空不接，仅用 `i_en` 做节拍。这样安全吗？什么前提下安全？

**答案**：安全的前提是 `sincos` 持续在线（`i_en=1'b1`）且其刷新周期（6 拍）远小于 FOC 控制周期（2048 拍）。这样 `park_tr` 每次 `i_en` 到来时，`sin_psi/cos_psi` 必然已是最近一次刷新过的有效值；多出的几拍延迟相对 2048 拍可忽略，且 FOC 对此不敏感。

### 4.2 sincos 计算器

#### 4.2.1 概念说明

`park_tr` 需要 \(\sin\psi\) 和 \(\cos\psi\)，但 FPGA 里没有“正弦函数”这种东西。`sincos.v` 就是一个用硬件实现的“正余弦查表器”。

它解决两个问题：

1. **怎么存？** 直接存一整圈 `0~2π` 的 sin 表太费资源。由于三角函数有对称性，只需要存**第一象限** `0~π/2` 的值，其它象限靠“折叠 + 加符号”推导出来。本库存的是**余弦**表（理由见 4.2.3）。
2. **怎么算两个函数？** sin 和 cos 都要。作者没有用两张 ROM，而是**用一张 ROM、分两次查表**——先查 cos、再查 sin，由状态机把两次访问错开。

定点约定（务必记住，全库统一）：

- 输入角度 `i_theta`：`0~4095` 一圈，对应 `0~2π`。所以 `1024` 对应 `π/2`，`2048` 对应 `π`，`3072` 对应 `3π/2`。
- 输出 `o_sin/o_cos`：16 位有符号，`-1~+1` 映射到 `-16384~+16384`。注意是 `16384=2^{14}`，不是 `32768=2^{15}`，留了一位符号位在 16 位里。

#### 4.2.2 核心流程

`sincos` 是一个 6 状态的有限状态机，每来一次 `i_en`（在本库实际接 `1'b1`，所以它一直在循环）走一遍：

```
IDLE ──(i_en)──> S1 ──> S2 ──> S3 ──> S4 ──> S5 ──> IDLE
                  │      │      │      │
                  │      │      │      └─ 读 rom_y 得 sin，加符号 → o_sin；o_cos<=cos_tmp；o_en<=1
                  │      │      └──── 读 rom_y 得 cos，加符号 → cos_tmp
                  │      └────────── 写 sin 的查表索引 rom_x，设 sin 的符号标志 sin_z/sin_s
                  └─────────────── 写 cos 的查表索引 rom_x，设 cos 的符号标志 cos_z/cos_s
```

ROM 本身是**寄存器式**的（`always@(posedge clk) case(rom_x) rom_y<=...`），有 1 拍延迟：在 `S1` 写入 cos 的索引，`S3` 才能读到 cos 的结果；在 `S2` 写入 sin 的索引，`S4` 才能读到 sin 的结果。状态机正好把这两次“写索引—读结果”错开，**复用同一张 ROM** 完成两次查表。

角度折叠思路：

- 先算 `theta_a = i_theta - 1024`，即把角度**减去 \(\pi/2\)**。这是为了把 sin 也变成“查余弦表”的问题——因为 \(\sin\theta = \cos(\theta-\pi/2)\)，于是 sin 和 cos 可以共用同一张余弦 ROM，只是查的角度不同。
- `theta_b` 是把角度**折叠到 `[0, π]`** 的中间量；再判断它是否 `>1024`（即落在第二象限 `(π/2, π]`），若是则镜像回第一象限并记下“cos 取负”的符号。
- 四个标志位：`cos_z`/`sin_z` 表示该值**正好为 0**（落在 \(\pi/2\) 边界），`cos_s`/`sin_s` 表示该值**取负**。查完表用它们恢复符号。

从 `i_en`（IDLE 采样）到 `o_en`（S4 置 1）约 **4 拍**；一次完整计算循环（含回到 IDLE）**6 拍**。

#### 4.2.3 源码精读

先看端口与定点注释：

[RTL/foc/sincos.v:L11-L18](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L11-L18) — 端口：`i_theta` 一圈映射到 `0~4095`；`o_sin/o_cos` 把 `-1~+1` 映射到 `-16384~+16384`。

状态定义与关键寄存器：

[RTL/foc/sincos.v:L21-L34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L21-L34) — 6 个状态 `IDLE/S1…S5`；`theta_a/theta_b` 是折叠用的中间角度；`cos_z,cos_s,sin_z,sin_s` 是四个符号/零标志；`rom_x/rom_y` 是 ROM 的地址与数据。

**IDLE：采样角度，开始折叠。**

[RTL/foc/sincos.v:L47-L54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L47-L54) — `theta_a <= i_theta - 1024`（减 \(\pi/2\)，为查 sin 备用）；若 `i_theta>2048`（角度在 `(π,2π)`）则 `theta_b = -i_theta`（镜像回 `[0,π]`），否则 `theta_b = i_theta`。

**S1：写 cos 的查表索引与符号。** 注意 `if(theta_b>1024)` 用的是**本拍开始时**的 `theta_b`（非阻塞赋值，读到的是旧值），同时把 `theta_b` 更新为折叠后的新值供 S2 用：

[RTL/foc/sincos.v:L55-L70](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L55-L70) — 若 `theta_b>1024`（第二象限）：`rom_x = -theta_b[9:0]`（镜像到第一象限），`cos_s=1`（cos 取负）；否则 `rom_x = theta_b[9:0]`，`cos_z=(theta_b==1024)`（恰为 \(\pi/2\) 时 cos=0）。

**S2：写 sin 的查表索引与符号**（用 S1 更新后的 `theta_b`，即基于 `theta_a=i_theta-π/2` 折叠的结果）：

[RTL/foc/sincos.v:L71-L82](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L71-L82) — 逻辑同 S1，但写入 `sin_z/sin_s`。此时 ROM 已完成对 S1 写入的 cos 索引的查表，`rom_y` 即 cos 的幅值。

**S3：读 cos 结果，加符号。**

[RTL/foc/sincos.v:L83-L91](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L83-L91) — `cos_z` 则 `cos_tmp=0`；`cos_s` 则 `cos_tmp=-rom_y`；否则 `cos_tmp=+rom_y`。`{1'b0,rom_y}` 把 15 位无符号幅值零扩展成 16 位正数，再用 `$signed` 转有符号后取负。

**S4：读 sin 结果，加符号，输出。**

[RTL/foc/sincos.v:L92-L102](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L92-L102) — 同样按 `sin_z/sin_s` 恢复符号写入 `o_sin`；`o_cos<=cos_tmp`；`o_en<=1` 发出有效脉冲。

**ROM：一张第一象限的余弦表。** 关键看首尾两端：

[RTL/foc/sincos.v:L110-L114](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L110-L114) — `rom_x=0` 时 `rom_y=16384`，即 \(\cos(0)=1\)。

[RTL/foc/sincos.v:L1130-L1133](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L1130-L1133) — `rom_x=1023` 时 `rom_y=25`，即 \(\cos(\pi/2)\approx 0\)（量化到 1024 点后的残余）。

可见 `rom_x` 范围 `0~1023` 覆盖第一象限 `[0, π/2)`，步长 \(\pi/2048\)；`rom_y` 是 15 位无符号幅值（第一象限内 cos 恒非负，故无需符号位）。整张表共 1024 项，是本库中最长的单个 `case`。

**用四个基准角度验证象限逻辑**（均经手工追踪状态机得到，读者可对照源码复算）：

| `i_theta` | 角度 | `theta_a` | cos 路径 (rom_x, cos_z, cos_s) | sin 路径 (rom_x, sin_z, sin_s) | `o_cos` | `o_sin` |
|---|---|---|---|---|---|---|
| `0`    | 0      | 3072 (−1024) | (0, 0, 0) | (0, 1, 0) | +16384 | 0 |
| `1024` | π/2    | 0            | (0, 1, 0) | (0, 0, 0) | 0      | +16384 |
| `2048` | π      | 1024         | (0, 0, 1) | (0, 1, 0) | −16384 | 0 |
| `3072` | 3π/2   | 2048         | (0, 1, 0) | (0, 0, 1) | 0      | −16384 |

四个角度的输出与 \((\sin,\cos)\) 的理论值完全吻合，验证了“折叠 + 符号标志 + 余弦 ROM”的正确性。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `sincos` 的象限处理与定点映射，确认 `0~4095 ↔ 0~2π`、`-1~1 ↔ -16384~16384` 两条约定。

**操作步骤**：

1. 复制 `SIM/tb_clark_park_tr.v` 为一个新文件 `SIM/tb_sincos.v`（示例文件名，本讲义不要求提交），只例化一个 `sincos`：

   ```verilog
   // 示例代码：最小 sincos 测试台
   module tb_sincos();
       reg clk=1'b1, rstn=1'b0;
       always #(13563) clk=~clk;            // 36.864MHz
       reg i_en; reg [11:0] i_theta;
       wire o_en; wire signed [15:0] o_sin, o_cos;
       sincos dut (.rstn(rstn), .clk(clk), .i_en(i_en), .i_theta(i_theta),
                   .o_en(o_en), .o_sin(o_sin), .o_cos(o_cos));
       initial $dumpvars(1, tb_sincos);
       integer i;
       initial begin
           repeat(4) @(posedge clk); rstn<=1'b1;
           for(i=0;i<4096;i=i+64) begin      // 每隔 64 步采样一圈
               @(posedge clk); i_en<=1'b1; i_theta<=i;
               @(posedge clk); i_en<=1'b0;
               repeat(8) @(posedge clk);     // 等 6 拍计算完成
           end
           $finish;
       end
   endmodule
   ```

2. 编译运行：`iverilog -g2001 -o sim.out tb_sincos.v ../RTL/foc/sincos.v && vvp -n sim.out`。
3. gtkwave 打开 `dump.vcd`，把 `i_theta`、`o_sin`、`o_cos` 设为 Signed Decimal + Analog。

**需要观察的现象**：`o_sin` 与 `o_cos` 应是两条相位差 \(\pi/2\) 的正弦曲线，幅值在 `±16384` 之间。

**预期结果**：在 `i_theta = 0/1024/2048/3072` 四个点附近，应读出上表那四组 `(o_sin, o_cos)` 值（`0/16384`、`16384/0`、`0/−16384`、`−16384/0`），从而同时验证了定点映射与四象限符号。

> 上述最小测试台为「示例代码」，运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sincos` 只存一张**余弦**表，却连 sin 也能算出来？

**答案**：利用 \(\sin\theta=\cos(\theta-\pi/2)\)。代码里 `theta_a = i_theta - 1024` 正是减去 \(\pi/2\)，于是查 sin 就变成“用 `theta_a` 去查同一张余弦表”，只需把两次查表在状态机里错开即可，省掉第二张 ROM。

**练习 2**：`rom_y` 是 15 位无符号，而 `o_sin/o_cos` 是 16 位有符号。代码里 `-$signed({1'b0,rom_y})` 这一步在做什么？

**答案**：`{1'b0,rom_y}` 在 15 位幅值前面补一个 0，拼成 16 位，相当于零扩展成一个正的 16 位数；`$signed` 把它当作有符号数看待；最前面的负号在需要取负（`sin_s`/`cos_s` 为 1）时把它变成负数。这样 15 位无符号幅值就被正确地映射成 16 位有符号的正/负值。

**练习 3**：`sincos` 一次计算要 6 拍，而 `park_tr` 让它 `i_en=1'b1` 一直转。如果 FOC 控制周期是 2048 拍，这种“持续空转”会不会导致 `park_tr` 拿到过期的 sin/cos？

**答案**：会有至多约 6 拍的延迟（`ψ` 已变、`sincos` 还没刷新完），但相对 2048 拍的控制周期，这点延迟在电角度上几乎不可察觉，且 FOC 控制环对此有容错（PI 会修正）。换来的是“随用随取、无需启动等待”，是合算的取舍。

## 5. 综合实践

把本讲的两块知识（Park 的“解调”数学 + sincos 的定点约定）串起来做一个实验：

**任务**：在 `SIM/tb_clark_park_tr.v` 中，把 `park_tr` 的 `psi` 偏移从 `theta + 12'd512`（\(\pi/4\)）依次改成：

- (a) `theta`（偏移 \(\delta=0\)）；
- (b) `theta + 12'd1024`（偏移 \(\delta=\pi/2\)）。

每次改完重新仿真，在 gtkwave 里读出稳态时 `id`、`iq` 的直流电平。

**要求**：

1. 先**预测**：根据 \(i_d+j\,i_q = A\,e^{-j\delta}\)，写出两种情况下 `id`、`iq` 的预期比例（例如 \(\delta=0\) 时 `iq≈0`、`id>0`；\(\delta=\pi/2\) 时 `id≈0`、`iq` 取某个值）。
2. 再**比对**波形读数，看是否符合预测。若 `id`/`iq` 的幅值与预测差了约 4 倍，那正是 4.1.3 推导的 `o_id=i_d/4` 缩放——据此反推 αβ 矢量的真实幅值 \(A\)。
3. **思考**：为什么无论 \(\delta\) 取多少，\(\sqrt{i_d^2+i_q^2}\)（再乘 4 修正缩放）都应近似等于 `iα/iβ` 矢量的幅值？这验证了 Park 变换的什么性质？

**预期结论**：Park 是一个**保幅旋转**——它只改变矢量在 dq 两轴上的投影分配，不改变矢量大小。所以稳态 `id`、`iq` 虽随 \(\delta\) 不同而此消彼长，但其合成幅值恒定；偏移 \(\delta\) 唯一的作用是决定这个常数矢量在 dq 平面里的朝向。这正是 FOC 能把“控扭矩”简化成“控一个直流 `iq`”的数学根基。

## 6. 本讲小结

- Park 变换用 \(i_d=i_\alpha\cos\psi+i_\beta\sin\psi\)、\(i_q=i_\beta\cos\psi-i_\alpha\sin\psi\) 把旋转的 αβ 矢量“旋”到转子坐标系，使其变成直流，便于 PI 控制。
- `park_tr.v` 是 2 级流水线：级 1 并行算 4 个乘积并锁存，级 2 加减后取 `ide[31:16]`/`iqe[31:16]` 输出；`i_en`→`en_s1`→`o_en` 脉冲逐级下传。
- 由于 `sincos` 用 `2^{14}` 缩放、而 `park_tr` 取高 16 位（除以 `2^{16}`），输出 `id/iq` 带一个 `1/4` 的常数增益——该增益被 PI 的 `Kp/Ki` 吸收，是“系数不必精确”思想的又一次体现。
- `sincos.v` 用 `IDLE→S1→…→S5` 状态机 + 一张第一象限**余弦** ROM，靠 `\sin\theta=\cos(\theta-\pi/2)` 让 sin 与 cos 共用同一张表，两次查表在时间上错开复用。
- 全库定点约定：角度 `0~4095 ↔ 0~2π`，三角函数 `-1~1 ↔ -16384~16384`（`2^{14}` 缩放，留一位符号位）。
- 当 `ψ` 跟随电流矢量的旋转角 `theta` 时，Park 把交流“解调”成直流；常数偏移只改变 `id/iq` 的比例，不破坏直流性。

## 7. 下一步学习建议

下一讲 [u2-l5 PI 控制器 pi_controller.v](u2-l5-pi-controller.md) 将接收本讲输出的 `id/iq`，把它们与目标 `id_aim/iq_aim` 比较并算出 `Vd/Vq`。建议带着两个问题去读：

1. `pi_controller` 也是流水线 + `i_en/o_en` 握手，它的节拍如何接在 `en_idq` 之后？
2. 本讲留下的那个 `1/4` 增益，最终是如何被 `Kp/Ki` 的整定“吃掉”的？

若想加深对 `sincos` 的理解，可以尝试用 Excel 或脚本把 ROM 的 1024 项与标准 \(\cos\) 曲线对比，观察量化误差的数量级（应与 4.2.3 表中 `rom_x=1023` 处的残余 25 量级一致）。
