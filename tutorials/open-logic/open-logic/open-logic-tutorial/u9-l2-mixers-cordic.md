# 混频器与 CORDIC

## 1. 本讲目标

本讲进入 `fix` 区域的「信号处理」专题，讲解两组互相呼应的实体：

- **混频器（Mixer）**：`olo_fix_mix_r2c`（实到复）、`olo_fix_mix_c2r`（复到实），用于把信号与一个复数「本地振荡器（LO）」相乘，实现频率搬移（上/下变频）。
- **CORDIC**：`olo_fix_cordic_rot`（旋转模式：极坐标 → 直角坐标）、`olo_fix_cordic_vect`（向量模式：直角坐标 → 极坐标），只用「移位 + 加减」实现三角函数与幅相转换。

学完后你应当能够：

- 说清「下变频约定」为什么等价于乘以本地振荡器的共轭，并能据此写出 r2c 与 c2r 的输出公式。
- 读懂两个混频器如何复用 `olo_fix_mult` / `olo_fix_madd` 拼装，以及 Parallel 与 TDM 两种 I/Q 处理的差异。
- 理解 CORDIC 旋转/向量两种模式的迭代公式、收敛方向判据（看 z 还是看 y），以及 Open Logic 如何用「象限预映射 + 输出还原」把单象限核心扩展到整圆。
- 学会用 Open Logic 配套的 Python 位真模型做误差分析，并用 VUnit 协仿真做位真比对。

## 2. 前置知识

本讲默认你已经掌握 u8 系列与 u9-l1 的内容，尤其是：

- **定点三元组 \((S,I,F)\)** 与 `en_cl_fix` 的 `FixFormat_t` / `FixRound_t` / `FixSaturate_t`（u8-l1、u8-l2）。
- **字符串泛型模式**：接口用 `string` 传格式，实体内用 `cl_fix_format_from_string` 还原、用 `fixFmtWidthFromString` 推端口位宽（u8-l2）。
- **基本运算实体**：`olo_fix_mult`、`olo_fix_resize`、以及乘累加积木 `olo_fix_madd`（u8-l3、u9-l1）。
- **两进程法 / AXI-S 握手 / 同步高有效复位**（u1-l5、u2-l2）。

补充几个本讲用到、但前面讲得较少的概念：

- **复数信号与 I/Q**：一个复数采样写成 \(I + jQ\)，\(I\) 是同相分量（实部）、\(Q\) 是正交分量（虚部）。复数本地振荡器写成 \(e^{j\omega t}=\cos\omega t + j\sin\omega t\)，常把 \(\cos\) 部分叫 \(\text{MixI}\)、\(\sin\) 部分叫 \(\text{MixQ}\)。
- **CORDIC**（COordinate Rotation DIgital Computer）：一种只用加法、减法和二进制移位（乘以 \(2^{-i}\)）迭代逼近三角与双曲函数的算法，因不需要乘法器而广受 FPGA 青睐（CORDIC 的每步「乘 \(2^{-i}\)」在硬件里就是接线移位，不耗 DSP）。
- **归一化角度**：Open Logic 的 CORDIC 用「1.0 = 360° = \(2\pi\)」的归一化角度。例如 0.125 = 1/8 = 45°，正好等于 \(\arctan(1)/(2\pi)\)。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| `src/fix/vhdl/olo_fix_mix_r2c.vhd` | 实到复混频器：实信号 × 复数 LO → 复信号 |
| `src/fix/vhdl/olo_fix_mix_c2r.vhd` | 复到实混频器：复信号 × 复数 LO（共轭）→ 实信号，支持 Parallel / TDM |
| `src/fix/vhdl/olo_fix_cordic_rot.vhd` | CORDIC 旋转模式（极 → 直），PIPELINED / SERIAL 两种实现 |
| `src/fix/vhdl/olo_fix_cordic_vect.vhd` | CORDIC 向量模式（直 → 极），PIPELINED / SERIAL 两种实现 |
| `src/fix/python/olo_fix/olo_fix_mix_r2c.py` | r2c 的 Python 位真模型（复用 `olo_fix_cplx_mult` 的 MIX 模式） |
| `src/fix/python/olo_fix/olo_fix_cordic_vect.py` | 向量 CORDIC 的 Python 位真模型（用于误差分析与协仿真） |
| `test/fix/olo_fix_cordic_vect/olo_fix_cordic_vect_tb.vhd` | 向量 CORDIC 的 VUnit 协仿真测试台（stimuli/checker VC） |
| `test/fix/olo_fix_cordic_vect/cosim.py` | 生成协仿真激励 / 期望文件，并可在非协仿真模式下画误差图 |
| `sim/test_configs/olo_fix.py` | 按区域为每个实体注册不同 generic 组合的测试用例 |

---

## 4. 核心概念与源码讲解

### 4.1 实到复混频（olo_fix_mix_r2c）

#### 4.1.1 概念说明

混频的本质是「把信号搬移到另一个频率」。设想一个实数信号 \(r(t)\)（比如来自 ADC 的实采样），你想把它从高频搬到一个复数基带。最直接的做法是乘以一个复数本地振荡器：

\[ r(t)\cdot(\cos\omega t + j\sin\omega t) = r(t)\cos\omega t + j\,r(t)\sin\omega t \]

这叫**上变频约定**（乘以 LO 本身）。但通信里更常用的是**下变频约定**——乘以 LO 的共轭 \((\cos\omega t - j\sin\omega t)\)：

\[ r(t)\cdot(\cos\omega t - j\sin\omega t) = r(t)\cos\omega t - j\,r(t)\sin\omega t \]

把它写成 Open Logic 的端口约定（实信号 = `In_SigReal`，LO 的 cos = `In_MixI`，sin = `In_MixQ`）：

> `Out_I = +In_SigReal × In_MixI`
> `Out_Q = −In_SigReal × In_MixQ`

这正是 `olo_fix_mix_r2c` 文件头注释里写死的约定（见源码精读）。**「实到复」的含义**：输入是一路实信号，输出是 I/Q 两路（即一个复数）。注意它**不做 TDM**——I 与 Q 的 LO 分量必须并行出现在 `In_MixI` / `In_MixQ` 上。

#### 4.1.2 核心流程

数据通路非常对称，是一条简单的流水线：

```text
In_SigReal, In_MixI  ──► [olo_fix_mult] ──► Mult_I ──► (直通寄存) ──┐
                                                       │
                                          i_resize_i ──┴──► Out_I

In_SigReal, In_MixQ  ──► [olo_fix_mult] ──► Mult_Q ──► cl_fix_neg ──┐
                                                       │
                                          i_resize_q ──┴──► Out_Q
```

要点：

1. 两个乘法器**并行**跑，分别算 I 路与 Q 路的全精度乘积（`MultFmt_c = cl_fix_mult_fmt(InFmt, MixFmt)`）。
2. I 路原样寄存一拍；Q 路在乘完之后做一次取负（`cl_fix_neg`），实现那个负号。
3. 两路最后各过一个 `olo_fix_resize`，把全精度格式收敛到 `OutFmt_g`，并在此处做用户指定的 `Round_g` / `Saturate_g`。

为什么 Q 路的取负要在「全精度乘积之后、舍入饱和之前」做？因为取负是精确操作（不引入误差），放在舍入前可以避免一次多余的量化，保证与 Python 位真模型逐位一致（注释里特别强调这点）。

#### 4.1.3 源码精读

实体声明与泛型：`InFmt_g`/`MixFmt_g`/`OutFmt_g` 都是字符串泛型，端口位宽由 `fixFmtWidthFromString` 在编译期推导（字符串泛型模式）。

[olo_fix_mix_r2c.vhd:38-63](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_r2c.vhd#L38-L63) — 实体声明。注意 `Round_g`/`Saturate_g` 有默认值，`MultRegs_g` 默认为 1；输入只有 `In_Valid` 一个握手信号，**没有 Ready**（每周期都能收）。

文件头的下变频约定就是公式出处：

[olo_fix_mix_r2c.vhd:10-13](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_r2c.vhd#L10-L13) — `Out_I = +In_SigReal x In_MixI`、`Out_Q = -In_SigReal x In_MixQ`。

两个并行乘法器完全对称，唯一区别是 B 输入接 `In_MixI` 还是 `In_MixQ`；二者都设 `Round_g=>Trunc`、`Saturate_g=>None`，即乘法段不量化、不饱和：

[olo_fix_mix_r2c.vhd:111-153](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_r2c.vhd#L111-L153) — I/Q 两路乘法器实例化。

Q 路的取负 + I 路的直通寄存在同一个进程里完成（I 路只是寄存一拍以便与 Q 路取负后的延迟对齐）：

[olo_fix_mix_r2c.vhd:156-169](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_r2c.vhd#L156-L169) — `Mult_I_Reg <= Mult_I;`（I 直通），`Neg_Q <= cl_fix_neg(Mult_Q, MultFmt_c, NegFmt_c);`（Q 取负）。

最后两路各接一个 `olo_fix_resize` 收敛到 `OutFmt_g`，且 `RoundReg_g`/`SatReg_g` 都强制为 `"YES"`（文档据此给出固定延迟 `MultRegs_g + 4` 拍）：

[olo_fix_mix_r2c.vhd:172-207](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_r2c.vhd#L172-L207) — I/Q 输出 resize。

#### 4.1.4 代码实践

**目标**：用 Python 位真模型验证「下变频约定」里那个负号确实作用在 Q 路。

**操作步骤**（无需 FPGA，纯 Python；先按 u8-l4 的方式把 `src/fix/python` 加入 `PYTHONPATH`，并确保 `3rdParty/en_cl_fix` 的 Python 包可用）：

1. 仿照位真模型 `olo_fix_mix_r2c.py` 的用法，构造一个 `olo_fix_mix_r2c` 对象，格式取测试台常用的 `InFmt='(1,8,8)'`、`MixFmt='(1,0,15)'`、`OutFmt='(1,9,8)'`。
2. 喂两组极端激励：
   - 组 A：`SigReal = 满量程`，`MixI = 满量程`，`MixQ = 0`。
   - 组 B：`SigReal = 满量程`，`MixI = 0`，`MixQ = 满量程`。
3. 打印 `Out_I`、`Out_Q`。

**预期结果**：

- 组 A：`Out_I ≈ +满量程`、`Out_Q ≈ 0`（只有 I 路有信号）。
- 组 B：`Out_I ≈ 0`、`Out_Q ≈ −满量程`（**Q 路取负**，所以是负的）。

如果在 B 组看到 `Out_Q` 为正，就说明约定的负号丢了。具体数值受定点舍入影响，**待本地验证**精确 LSB。

#### 4.1.5 小练习与答案

**练习 1**：r2c 用了几个乘法器？为什么 Q 路的取负不直接靠把 `In_MixQ` 取反后再相乘？
**答案**：2 个乘法器（I/Q 各一）。取负放在乘积之后是为了与 `olo_fix_cplx_mult` 的 MIX 模式逐位一致；若先取反 `In_MixQ` 再乘，舍入/饱和点会错位，无法保证位真。

**练习 2**：`olo_fix_mix_r2c` 的文档说延迟是 `MultRegs_g + 4`。数一数源码里的流水线寄存器，解释这 4 拍分别来自哪里。
**答案**：输入寄存 1 拍（`p_in_reg`）+ 乘法输出后的对齐/取负寄存 1 拍（`p_pipe`）+ resize 段的 round 寄存 1 拍 + sat 寄存 1 拍 = 4 拍（外加 `MultRegs_g` 在乘法器内部）。

---

### 4.2 复到实混频（olo_fix_mix_c2r）

#### 4.2.1 概念说明

`olo_fix_mix_c2r` 把一个**复数**信号 \((I+jQ)\) 与复数 LO 的共轭相乘，并**只取实部**，输出一路实信号。展开乘法：

\[ (I + jQ)(\cos\omega t - j\sin\omega t) = (I\cos\omega t + Q\sin\omega t) + j(\cdots) \]

实部就是 Open Logic 的约定：

> `Out_SigReal = +In_SigI × In_MixI + In_SigQ × In_MixQ`

所以 c2r 比 r2c 多了一次「乘累加」（两个乘积相加）。它还多了一个泛型 `IqHandling_g`，决定 I/Q 怎样进入实体：

- **`"Parallel"`**：I、Q 各自占独立端口（`In_SigI`/`In_SigQ`/`In_MixI`/`In_MixQ`），两个乘累加并行跑，资源多、延迟低。
- **`"TDM"`**：I 与 Q 在共享端口 `In_SigIQ`/`In_MixIQ` 上**先 I 后 Q**交替到达（一个乘法器分时复用），每对 I/Q 只产出一个实采样，输出速率是输入的一半。

TDM 模式还引入 `In_Last`：它在「Q 拍」上拉高时，把 I/Q 相位追踪器复位，保证下一个到达的采样被当成 I（重同步，和 `olo_fix_cplx_mult` 一致）。

#### 4.2.2 核心流程

**Parallel 架构**用两个 `olo_fix_madd` 串成一个累加链（结构对齐 `olo_fix_cplx_mult` 的 MULT4 / MIX 模式）：

```text
In_SigI, In_MixI ──► [olo_fix_madd (无累加)] ──► SigI*MixI ──┐
                                                              ├─ (+) ─► Real_Full ─► [resize] ─► Out_SigReal
In_SigQ, In_MixQ ──► [olo_fix_madd (Operation=Add, MaccIn=上面)] ─┘
```

第二个 `madd` 的 `MaccIn` 接第一个的输出、`Operation_g => "Add"`，正好实现「加上 Q 路」。回顾 u9-l1：`madd` 的功能是 `Out = MaccIn op (A×B)`，这里 op 取 Add。

**TDM 架构**只有一个乘法器，靠 `IsQ` 标志在 I 拍算 `SigI*MixI` 并**保持**，到 Q 拍算 `SigQ*MixQ` 并与之相加：

```text
I 拍: MultI_N0 = SigI*MixI  ──保持到──►  MultI_Hold_N1
Q 拍: MultI_N0 = SigQ*MixQ  ──►  AddI_N1 = MultI_Hold_N1 + MultI_N0  ──► [resize] ─► Out_SigReal
```

#### 4.2.3 源码精读

`IqHandling_g` 合法性由一个 elaborate 断言把关（`synthesis translate_off` 包起来，只在仿真/ elaboration 报错）：

[olo_fix_mix_c2r.vhd:87-91](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_c2r.vhd#L87-L91) — 断言 `IqHandling_g` 必须是 `"Parallel"` 或 `"TDM"`。

Parallel 架构：第一个 `madd`（I×I）不带累加、第二个（Q×Q）带 `Operation_g=>"Add"` 并把第一个的结果接到 `MaccIn`：

[olo_fix_mix_c2r.vhd:124-161](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_c2r.vhd#L124-L161) — `i_ii` 算 `SigI*MixI`，`i_qq` 用 `MaccIn=>II_Out_N1` 把 `SigQ*MixQ` 加上去，得到 `Real_Full_N2`。

TDM 架构里 `In_Last` 的重同步逻辑：每个有效采样翻转 `IsQ`，遇到 `In_Last='1'` 就把 `IsQ` 清回 0，使下一拍回到 I：

[olo_fix_mix_c2r.vhd:218-241](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_c2r.vhd#L218-L241) — `IsQ` 翻转与 `In_Last` 复位；`MultI_Hold_N1` 在 I 拍保持乘积，`AddI_N1` 在 Q 拍完成相加。

两个架构最终都过一个 `olo_fix_resize` 收敛到 `OutFmt_g`。Parallel 模式里 round/sat 寄存与否由编译期常量 `RoundReg_c`/`SatReg_c` 决定（Trunc/Warn/None 时不插额外寄存，省延迟）：

[olo_fix_mix_c2r.vhd:79-82](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_mix_c2r.vhd#L79-L82) — `RoundReg_c`/`SatReg_c` 的选择逻辑。

#### 4.2.4 代码实践

**目标**：跑通 c2r 在 Parallel 与 TDM 两种模式下的协仿真，确认两种架构数值位真一致。

**操作步骤**：

1. 打开 `sim/test_configs/olo_fix.py`，定位到 `### olo_fix_mix_c2r ###`（约 684 行起）。
2. 注意它用一个双重循环把 `IqHandling_g` 取 `'Parallel'` 和 `'TDM'` 各注册了一组用例（见下条引用）。
3. 在 `sim/` 目录运行 `run.py`，过滤出 c2r 的用例执行（具体命令取决于你的仿真器，**待本地验证**）：

   ```bash
   cd sim
   python run.py --ghdl "*olo_fix_mix_c2r*"
   ```

**预期结果**：Parallel 与 TDM 的所有用例都通过——它们吃相同的激励、比对相同的期望文件，说明两种架构对同一组 (I,Q,LO) 给出位真一致的结果。

**需要观察的现象**：TDM 模式下输出采样数是输入的一半（每对 I/Q 产出一个实采样）；Parallel 模式输出与输入一一对应。

[olo_fix.py:697-702](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py#L697-L702) — `IqHandling` 在 Parallel/TDM 间枚举、各自配多组 `MultRegs`/`Round`/`Sat`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Parallel 架构里第二个 `madd` 要把 `InAC_Valid` 接 `In_Valid_0`（延迟一拍的 valid）而不是直接接 `In_Valid`？
**答案**：因为 Q 路的输入 `In_SigQ_0`/`In_MixQ_0` 本身被寄存了一拍（见 `p_reg`），为了让累加两端的 valid 时序对齐，valid 也必须同步延迟一拍，否则 `SigI*MixI`（在 `II_Out_N1`）会和 `SigQ*MixQ` 错拍相加。

**练习 2**：TDM 模式下，如果连续给一对 I/Q 之后立刻又给一对，中间不发 `In_Last`，会发生什么？什么时候**必须**发 `In_Last`？
**答案**：正常运行——`IsQ` 自动在 I/Q 间翻转，无需每对都发 `In_Last`。只有当 I/Q 相位可能错乱时（例如上游重启、丢同步、或流的第一拍无法确定是 I 还是 Q），才必须在某个 Q 拍上拉 `In_Last` 把 `IsQ` 强制清零，使下一拍回到 I。

---

### 4.3 CORDIC 旋转模式（olo_fix_cordic_rot）

#### 4.3.1 概念说明

CORDIC 旋转模式把**极坐标**（幅度 `In_Mag`、角度 `In_Ang`）转成**直角坐标**（`Out_I`、`Out_Q`），即算

\[ (I, Q) = (\text{Mag}\cos\theta,\ \text{Mag}\sin\theta) \]

核心思想：把一个向量 \((\text{Mag}, 0)\) 一步一步旋转到目标角度 \(\theta\)。每一步旋转一个**固定**的角度 \(\arctan(2^{-i})\)，方向由「当前累计角度 \(z\) 与目标的差」的符号决定——目标是把 \(z\) 一步步逼近 0。

第 \(i\) 次迭代（\(i=0,1,\dots,N-1\)），设方向 \(d_i=\text{sign}(z_i)\)（旋转模式下看 \(z\)）：

\[
\begin{aligned}
x_{i+1} &= x_i - d_i\,y_i\,2^{-i} \\
y_{i+1} &= y_i + d_i\,x_i\,2^{-i} \\
z_{i+1} &= z_i - d_i\,\arctan(2^{-i})
\end{aligned}
\]

注意 \(2^{-i}\) 倍在硬件里就是「右移 \(i\) 位」，是接线、不是乘法器。迭代 \(N\) 次后：

\[
\begin{pmatrix} x_N \\ y_N \end{pmatrix}
= K_N\,\text{Mag}\begin{pmatrix} \cos\theta \\ \sin\theta \end{pmatrix},
\qquad
K_N=\prod_{i=0}^{N-1}\sqrt{1+2^{-2i}}\approx 1.647
\]

\(K_N\) 就是 **CORDIC 增益**——每步旋转都会让向量稍微变长，累计放大约 1.647 倍。Open Logic 提供 `GainCorrCoefFmt_g` 让你选择是否在实体内部乘以 \(1/K_N\) 补偿掉这个增益（默认补偿）。

两个泛型控制实现风格：

- `Mode_g = "PIPELINED"`：把 \(N\) 次迭代展开成 \(N\) 级流水线，**每周期可吃一个采样**，吞吐最高、资源最大。
- `Mode_g = "SERIAL"`：只用一级迭代硬件，**每 \(N\) 个周期吃一个采样**，资源最小。

#### 4.3.2 核心流程

CORDIC 核心只在一个有限角度范围内收敛，所以实体先把输入映射进核心范围、最后再把结果还原回整圆。旋转模式的做法是：

1. **取象限**：把输入角度的高 2 位存进 `Quad`（象限），低位的有效角度送进 \(z(0)\)。
2. **迭代**：按上面的三式流水线（或串行）跑 \(N\) 步，把 \(z\) 逼近 0、把 \((x,y)\) 旋到目标方向。
3. **象限还原**：对落在「相对」象限的结果，把 \((x,y)\) 同时取负，把整圆还原回来。
4. **增益补偿**（可选）：入口处乘 \(1/K_N\)，或出口不补偿。
5. **输出收敛**：两个 `olo_fix_resize` 把内部格式收敛到 `OutFmt_g`。

```text
In_Mag ──►(×1/K_N ?)──► x(0)=Mag, y(0)=0
In_Ang ──► z(0)=Ang, Quad=Ang[高2位]
            │
            ▼  迭代 N 次 (z→0, 看 sign(z))
         (x_N, y_N)
            │
            ▼  象限还原 (Quad 01/10 → 取负)
         (XQc, YQc)
            │
            ▼  resize ×2
         Out_I, Out_Q
```

#### 4.3.3 源码精读

实体声明。输入格式受限：`InMagFmt_g` 必须无符号、`InAngFmt_g` 必须是 `(0,0,x)`（归一化角度）；`Iterations_g` 范围 3..32；`IntXyFmt_g`/`IntAngFmt_g` 默认 `"AUTO"` 由实体自动选：

[olo_fix_cordic_rot.vhd:37-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_rot.vhd#L37-L48) — 实体泛型。

arctan 查找表：32 个常量 `arctan(2^{-i})/(2\pi)`，第一个是 0.125（=45°）。CORDIC 增益函数 `cordicGain` 对应 \(K_N=\prod\sqrt{1+2^{-2i}}\)：

[olo_fix_cordic_rot.vhd:88-120](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_rot.vhd#L88-L120) — `AngleTableReal_c` 与 `cordicGain`。注意 \(1/K_N\) 被量化进 `GcCoef_c` 用于增益补偿（L122）。

三步迭代函数。旋转模式看 `signed(zLast) > 0` 决定方向 \(d_i\)：\(x\) 用减、\(y\) 用加（\(d_i=+1\) 时），\(z\) 减去 atan；`-shift` 即右移 \(i\) 位：

[olo_fix_cordic_rot.vhd:127-185](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_rot.vhd#L127-L185) — `cordicStepx`/`cordicStepy`/`cordicStepz`。

PIPELINED 实现：初始化 `X(0)<=ProcMag; Y(0)<=(others=>'0'); Z(0)<=...ProcAng...`，再用一个 `for` 生成把 \(N\) 级全部展开、每级调用三个 step 函数：

[olo_fix_cordic_rot.vhd:235-270](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_rot.vhd#L235-L270) — 流水线主进程与 `for i in 0 to Iterations_g-1 loop` 展开循环。

象限还原：`Quad` 为 `"00"`/`"11"` 时原样输出，否则对 \(x,y\) 同时取负：

[olo_fix_cordic_rot.vhd:256-263](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_rot.vhd#L256-L263) — 象限校正。

增益补偿 vs 不补偿两套 `generate`：补偿时入口接一个 `olo_fix_mult` 乘 `GcCoef_c`，不补偿时直接 resize 输入：

[olo_fix_cordic_rot.vhd:367-441](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_rot.vhd#L367-L441) — `g_gain_comp` / `g_no_gain_comp`。

> 小贴士：CORDIC 的延迟随 `Mode_g`、`Iterations_g`、是否补偿、是否 round/sat 而变，官方明确说**不保证跨版本恒定**，建议用 `olo_base_latency_comp` 做延迟无关对接（见 doc）。

#### 4.3.4 代码实践

**目标**：用旋转 CORDIC 验证「45° 输入 → I=Q」与「0° 输入 → Q≈0」。

**操作步骤**（Python 位真模型，参考 `olo_fix_cordic_rot.py`；与向量模式的练习类似）：

1. 构造 `olo_fix_cordic_rot`，取 `InMagFmt='(0,1,15)'`、`InAngFmt='(0,0,16)'`、`OutFmt='(1,1,15)'`、`Iterations_g=16`、`GainCorrCoefFmt='(0,0,17)'`（开补偿）。
2. 取 `Mag = 1.0`（满量程），分别喂 `Ang = 0`（0°）、`Ang = 0.125`（45°）、`Ang = 0.25`（90°，注意接近边界）。
3. 打印 `Out_I`、`Out_Q`，并算 \(Q/I\) 是否等于 \(\tan\theta\)。

**预期结果**（开增益补偿，理想值）：

- `Ang=0`：`Out_I ≈ 1.0`、`Out_Q ≈ 0`。
- `Ang=0.125`：`Out_I ≈ Out_Q ≈ 0.707`（\(\cos 45°=\sin 45°\)）。
- `Ang=0.25`：接近 90° 边界，误差会变大（CORDIC 收敛范围有限）。

若关掉增益补偿（`GainCorrCoefFmt='NONE'`），所有幅度会偏大约 1.647 倍。精确 LSB **待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：旋转模式下，每步的方向 \(d_i\) 由谁决定？向量模式呢？
**答案**：旋转模式由 `signed(z) > 0` 决定（目标是把累计角度 \(z\) 逼近 0）；向量模式由 `signed(y) < 0` 决定（目标是把 \(y\) 逼近 0，即把向量旋到 x 轴上）。

**练习 2**：为什么 CORDIC 输出会自带一个约 1.647 的增益？
**答案**：每次「伪旋转」\((x,y)\to(x\mp y2^{-i},\ y\pm x2^{-i})\) 实际上把向量长度乘了 \(\sqrt{1+2^{-2i}}\)（不是严格的纯旋转），\(N\) 步累计就是 \(K_N=\prod\sqrt{1+2^{-2i}}\approx 1.647\)。要精确幅度就得乘 \(1/K_N\) 补偿。

---

### 4.4 CORDIC 向量模式（olo_fix_cordic_vect）

#### 4.4.1 概念说明

向量模式是旋转模式的「逆运算」：输入直角坐标 \((I, Q)\)，输出极坐标 \((\text{Mag}, \text{Ang})\)，即

\[ \text{Mag}=\sqrt{I^2+Q^2},\qquad \text{Ang}=\text{atan2}(Q, I) \]

这一次目标是把 \(y\) 一步步旋到 0，方向由 \(-\text{sign}(y_i)\) 决定（看 \(y\)，不看 \(z\)），同时把每次旋转的角度累加进 \(z\)（\(z\) 从 0 开始）：

\[
\begin{aligned}
x_{i+1} &= x_i - d_i\,y_i\,2^{-i} \\
y_{i+1} &= y_i + d_i\,x_i\,2^{-i} \\
z_{i+1} &= z_i - d_i\,\arctan(2^{-i}), \qquad d_i=-\text{sign}(y_i)
\end{aligned}
\]

迭代 \(N\) 次后：

\[
x_N \approx K_N\sqrt{I^2+Q^2},\qquad z_N \approx \text{atan2}(Q, I)
\]

CORDIC 核心只在第一象限（\(I,Q\ge 0\)）正确收敛，所以实体的关键工程技巧是**「象限预映射 + 输出还原」**：入口处对 \(I,Q\) 取绝对值（映射进第一象限）、并记下各自符号位组成 `Quad`；出口处根据 `Quad` 把第一象限内算出的角度还原成整圆的正确角度。

#### 4.4.2 核心流程

```text
In_I, In_Q
   │
   ▼  取绝对值映射进第一象限，记 Quad = sign(I) & sign(Q)
x(0)=|I|, y(0)=|Q|, z(0)=0, Quad=...
   │
   ▼  迭代 N 次 (y→0, 看 sign(y))
(x_N, z_N)        # x_N = K_N*|v|, z_N = atan(|Q|/|I|) ∈ [0, 45°...]
   │
   ├─► Mag = x_N  (可选 ×1/K_N 补偿)  ──► Out_Mag
   │
   └─► Ang = 按 Quad 还原:               ──► Out_Ang
         Quad 00:  Ang =  z              (第一象限)
         Quad 10:  Ang = 0.5 - z         (第二象限, +180°侧)
         Quad 11:  Ang = 0.5 + z         (第三象限)
         Quad 01:  Ang = 0.0 - z         (第四象限)
```

四象限角度还原里那个 `0.5` 就是归一化的 180°（因为 1.0 = 360°）。

#### 4.4.3 源码精读

实体声明。`InFmt_g` 必须有符号、`OutMagFmt_g` 必须无符号、`OutAngFmt_g` 必须是 `(0,0,x)`（归一化无符号角度）；同样支持 `"AUTO"` 内部格式与 PIPELINED/SERIAL：

[olo_fix_cordic_vect.vhd:37-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_vect.vhd#L37-L48) — 实体泛型。

与旋转模式共享同一张 arctan 表与同一个 `cordicGain` 函数：

[olo_fix_cordic_vect.vhd:96-128](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_vect.vhd#L96-L128) — `AngleTableReal_c` 与 `cordicGain`。

三步迭代函数。**关键区别**：方向判据是 `signed(yLast) < 0`（看 \(y\)），所以 \(y<0\) 时加、\(y\ge 0\) 时减，把 \(y\) 逼近 0；`cordicStepz` 同样根据 \(y\) 的符号加减 atan：

[olo_fix_cordic_vect.vhd:136-193](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_vect.vhd#L136-L193) — `cordicStepx`/`cordicStepy`/`cordicStepz`（看 \(y\)）。

入口映射进第一象限：对 `IReg`/`Qreg` 取绝对值赋给 `X(0)`/`Y(0)`，`Z(0)` 清零，`Quad(0)` 取两个符号位拼接：

[olo_fix_cordic_vect.vhd:250-256](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_vect.vhd#L250-L256) — `X(0)<=cl_fix_abs(IReg,...)`、`Quad(0)<=IReg'left & Qreg'left`。

四象限角度还原 `case`：用归一化常数 `0.5`（=180°）与 `0.0` 把第一象限角度 `Z` 还原成整圆：

[olo_fix_cordic_vect.vhd:272-281](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_vect.vhd#L272-L281) — `case Quad(Iterations_g)`，四个分支对应四个象限。

增益补偿：开补偿时用一个 `olo_fix_mult` 乘 `GcCoef_c` 出 `Out_Mag`，并用一个 `olo_base_delay` 给角度路径补 3 拍延迟对齐；不开补偿时 `Out_Mag` 走 resize、角度补 2 拍：

[olo_fix_cordic_vect.vhd:396-471](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/fix/vhdl/olo_fix_cordic_vect.vhd#L396-L471) — `g_no_gain_comp` / `g_gain_comp`。注意角度路径用 `olo_base_delay`（不可复位的数据延迟线，见 u5-l1）做延迟匹配。

#### 4.4.4 代码实践

**目标**：用向量 CORDIC 把一组 \((I,Q)\) 转成幅相，**比对 Python 位真模型**，并**观察幅度/相位误差随迭代次数 `Iterations_g` 的变化**。

本实践分两步：先用 Python 模型扫迭代次数看误差曲线，再用 VUnit 协仿真确认 VHDL 与模型逐位一致。

**步骤一：Python 误差扫描（无需 FPGA）**

参考 `test/fix/olo_fix_cordic_vect/cosim.py` 末尾的示例（它就是非协仿真模式下画误差图的入口）：

[cosim.py:93-106](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_cordic_vect/cosim.py#L93-L106) — 示例 generic 组合。

1. 把示例里的 `Iterations_g` 依次改成 `5`、`8`、`13`、`21`，分别调用 `cosim(generics=..., cosim_mode=False)`。
2. 观察 `olo_fix_plots` 画出的「Error Magnitude [LSB]」与「Error Angle [LSB]」子图（误差计算见下条引用）。

**预期结果**：随着 `Iterations_g` 增大，幅度与角度的误差（以 LSB 计）整体下降，并趋于一个由内部格式位宽决定的下限。粗略经验：迭代数与输出有效位数大致「一位对应一拍」（文档建议起点为「每位输出一次迭代」）。

[cosim.py:68-82](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_cordic_vect/cosim.py#L68-L82) — 误差图的数据准备（含角度做圆周 wrap 处理、`NONE` 补偿时把期望幅度乘回 CORDIC 增益）。

**步骤二：VUnit 协仿真位真比对（真硬件流程）**

协仿真的闭环是：仿真前 Python 先生成 `.fix` 激励/期望文件，TB 里的 stimuli VC 回放激励、checker VC 逐拍比对输出（详见 u8-l5）。测试台里甚至内置了延迟自检：

[olo_fix_cordic_vect_tb.vhd:144-161](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/fix/olo_fix_cordic_vect/olo_fix_cordic_vect_tb.vhd#L144-L161) — 延迟自检进程，按 `5/4 + GainLatency + Iterations_g` 算期望延迟并 `check_equal`。

在 `sim/` 下运行（命令与仿真器相关，**待本地验证**）：

```bash
cd sim
python run.py --ghdl "*olo_fix_cordic_vect*default-LowRes*"
python run.py --ghdl "*olo_fix_cordic_vect*default-General*"
```

**预期结果**：`default-LowRes`（`Iterations_g=5`，低分辨率）与 `default-General`（`Iterations_g=21`，高分辨率）两组用例都通过——VHDL 输出与 Python 模型逐位一致。这也间接验证了「误差随迭代次数变化」确实发生在数值层面（低分辨率用例误差更大，但仍在 checker 容忍范围内，因为它比对的本来就是同一个低分辨率模型）。

[olo_fix.py:365-373](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_fix.py#L365-L373) — `default-LowRes` 用例（`Iterations_g=5`、低内部格式）。

#### 4.4.5 小练习与答案

**练习 1**：向量模式入口为什么要对 \(I,Q\) 取绝对值？`Quad` 记的是什么？
**答案**：CORDIC 核心只在第一象限正确收敛，取绝对值把任意 \((I,Q)\) 映射进第一象限统一处理。`Quad` 记录原始 \(I\) 与 \(Q\) 的符号位（`I'left & Q'left`），出口处据此把第一象限角度还原成正确的整圆角度。

**练习 2**：还原角度时，第二象限（`Quad="10"`）用 `0.5 - z`、第三象限（`"11"`）用 `0.5 + z`。结合归一化角度约定（1.0=360°），解释这两个式子。
**答案**：`0.5` = 180°。第二象限真角度在 90°~180°，而第一象限内算出的 \(z\) 是相对 0°~90° 的，需要用 180° 减去它（镜像）；第三象限真角度在 180°~270°，用 180° 加上第一象限的 \(z\)。第四象限（`"01"`）真角度在 270°~360°，即归一化的 \(-90°\sim0°\)，所以用 `0.0 - z`（负角度，靠环绕表示）。

---

## 5. 综合实践

把本讲的四个实体串成一条「数字下变频 + 解调」的迷你链路（纯源码阅读 + 模型推演，不要求一次跑通硬件）：

**场景**：实数射频采样 \(r(t)\) 进入，先下变频到复基带，再做幅相检测。

1. **下变频**：用 `olo_fix_mix_r2c` 把实信号 \(r\) 与复数 LO 相乘，得到复基带 \((I, Q)\)。写出你期望的 `Out_I`/`Out_Q` 公式，并说明为什么是下变频约定（乘共轭）。
2. **进一步变频 / 信道选择**（可选）：如果还要在复基带内再搬一次频，用 `olo_fix_mix_c2r` 把复信号与第二个 LO 相乘取实部；对比它 Parallel 与 TDM 两种用法的资源/吞吐取舍。
3. **幅相检测**：把复基带 \((I,Q)\) 送进 `olo_fix_cordic_vect`，得到瞬时幅度与相位。先用 Python 位真模型（`olo_fix_cordic_vect.py`）扫 `Iterations_g`，选定一个让幅度/角度误差（LSB）满足你需求的迭代数。
4. **反向核对**：把得到的 \((\text{Mag}, \text{Ang})\) 喂给 `olo_fix_cordic_rot`，看能否还原出近似的 \((I, Q)\)（注意两侧的增益补偿要一致：一个开补偿、另一个也得开补偿，否则幅度会偏 1.647 倍）。

**验收要点**：

- 能写出 r2c、c2r 的下变频公式并解释共轭来源。
- 能说出 CORDIC 旋转看 `z`、向量看 `y`，并解释四象限还原里的 `0.5`。
- 能用 Python 模型画出「误差 vs 迭代次数」曲线，据此选迭代数。

> 提示：完整硬件链路的延迟会随 CORDIC 的 `Mode_g`/`Iterations_g` 变化，建议在关键节点用 `olo_base_latency_comp`（u5-l1）做延迟对齐，而不是硬记拍数。

## 6. 本讲小结

- **混频 = 乘复数 LO**：下变频约定等价于乘 LO 的共轭。`mix_r2c` 算 `Out_I=+r·MixI`、`Out_Q=−r·MixQ`（2 个乘法器，Q 路乘后取负）；`mix_c2r` 算 `Out=SigI·MixI+SigQ·MixQ`（两个 `madd` 累加），并支持 Parallel（并行双乘）与 TDM（单乘分时复用 + `In_Last` 重同步）。
- **CORDIC 用移位 + 加减逼近三角函数**：每步乘 \(2^{-i}\) 是接线移位，不耗乘法器；固有增益 \(K_N\approx1.647\) 可由 `GainCorrCoefFmt_g` 选择是否在实体内补偿。
- **旋转 vs 向量的唯一区别是收敛判据**：旋转模式看 `sign(z)`、把 \((\text{Mag},0)\) 旋到角度 \(\theta\)（极→直）；向量模式看 `sign(y)`、把向量旋到 x 轴并累加角度（直→极）。
- **整圆扩展靠象限处理**：rot 用角度高 2 位选 `Quad`、对相对象限取负还原；vect 入口取绝对值映射进第一象限、出口用 `0.5±z` 还原四象限角度。
- **两种实现风格**：`PIPELINED` 展开 \(N\) 级、每周期一采样（高吞吐）；`SERIAL` 复用一级、每 \(N\) 周期一采样（省资源）。
- **位真验证闭环**：每个实体都有 Python 位真模型 + VUnit 协仿真 TB（stimuli/checker VC），且 TB 内置延迟自检；CORDIC 的精度调优应以 Python 模型的误差曲线为依据。

## 7. 下一步学习建议

- **继续 DSP 专题**：下一讲 u9-l3 讲 CIC 抽取滤波器，u9-l4 讲 FIR 滤波器与系数存储——它们常与混频器串联构成完整的数字下变频（DDC）链路。
- **深挖复数乘法根**：本讲的混频器都复用了 `olo_fix_cplx_mult` 的 MULT4/MIX/TDM 结构（u9-l1）。想彻底读懂混频器的位真来源，建议回头精读 `olo_fix_cplx_mult.vhd`。
- **延迟与流控**：CORDIC 不带输出反压且延迟可变，若要把它接入反压链路，复习 `olo_base_latency_comp`（u5-l1）与 `olo_base_flowctrl_handler`（u5-l4）。
- **协仿真体系**：本讲实践大量依赖 Python 位真模型与 `.fix` 文件协仿真，完整原理见 u8-l5；想自定义激励可参照 `test/fix/olo_fix_cordic_vect/cosim.py` 的写法。
