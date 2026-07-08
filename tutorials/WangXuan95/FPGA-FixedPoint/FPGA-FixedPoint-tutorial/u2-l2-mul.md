# fxp_mul：乘法与结果位宽推导

## 1. 本讲目标

学完本讲，你应当能够：

- 推导定点乘积的位宽公式：积的整数位宽 \(W_{RI}=W_{IIA}+W_{IIB}\)、小数位宽 \(W_{RF}=W_{IFA}+W_{IFB}\)，并能解释其数学来源。
- 看懂 `fxp_mul` 的「两段式」结构：`$signed(ina)*$signed(inb)` 先得到全精度积，再由唯一的 `fxp_zoom` 收敛到输出位宽。
- 理解为什么乘法模块里只有 1 个 `fxp_zoom`，而 `fxp_add` 需要 3 个——也就是为什么乘法「不需要输入对齐」。
- 说清楚 `ROUND` 参数在乘法里唯一的生效点：积小数位截断时的四舍五入。
- 判断一组 `(WIIA,WIFA,WIIB,WIFB,WOI,WOF)` 配置下乘积是否可能溢出输出范围。

## 2. 前置知识

本讲承接 [u1-l2（定点格式与参数命名）](./u1-l2-fixedpoint-format.md) 与 [u1-l3（fxp_zoom 位宽变换核心）](./u1-l3-fxp-zoom.md)，并和上一篇 [u2-l1（加减法）](./u2-l1-add-sub.md) 形成对照。你需要已经掌握：

- 定点数值 = 有符号补码码值 ÷ \(2^{W_F}\)，码值用 `$signed(code)` 取出。
- 全库统一参数：`WIIA/WIFA`、`WIIB/WIFB` 是两路输入的整数/小数位宽，`WOI/WOF` 是输出位宽，`ROUND` 控制截断时是否四舍五入。
- `fxp_zoom` 是全库的位宽搬运工：小数位 `WOF<WIF` 时砍低位并按 `ROUND` 舍入（含「正最大值防翻转」特判），整数位 `WOI<WII` 时检测上溢出/下溢出并饱和钳位。其内部机制已在 u1-l3 讲透，本讲只复用其结论。
- `fxp_add` 的三段式：两路输入先各自 `fxp_zoom` 对齐到公共格式，再做有符号加法，最后 `fxp_zoom` 还原输出位宽。

一个贯穿全讲的关键直觉：**加法要把两个数对齐到同一格式才能逐位相加；乘法天然不需要——任何两种定点格式相乘，积的格式都是确定的。** 这一条决定了 `fxp_mul` 比 `fxp_add` 少用两个 `fxp_zoom`。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在。本讲重点是其中的 `fxp_mul`（第 278–310 行），并对照 `fxp_add`（第 110–170 行）与 `fxp_zoom`（第 22–94 行）。 |
| [SIM/tb_add_sub_mul_div.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v) | 加减乘除共用的 testbench，其中例化了 `fxp_mul` 并打印 HW-result 与 SW-result 对比。 |
| [SIM/tb_add_sub_mul_div_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div_run_iverilog.bat) | iverilog 一键编译运行脚本（testbench 与 RTL 必须同时参与编译）。 |

## 4. 核心概念与源码讲解

### 4.1 乘积位宽推导：WRI=WIIA+WIIB、WRF=WIFA+WIFB

#### 4.1.1 概念说明

定点乘法有一个反直觉但极其重要的特性：**两个定点数相乘，乘积的位宽是完全确定的，不需要任何对齐操作**。这一点和人类手算十进制小数乘法一致——`(整数位 a + 小数位 a)` 位的数乘 `(整数位 b + 小数位 b)` 位的数，结果的整数位和小数位都可以提前算出来。

本模块要回答的核心问题就是：积的整数位宽 `WRI` 和小数位宽 `WRF` 各是多少？为什么？

#### 4.1.2 核心流程（数学推导）

设输入 `ina` 是 \(W_{IIA}+W_{IFA}\) 位有符号补码整数，码值为 \(c_a\)，它代表的真实值为：

\[
a=\frac{c_a}{2^{W_{IFA}}}
\]

同理 `inb` 的码值 \(c_b\)、真实值 \(b=c_b/2^{W_{IFB}}\)。

**第一步：码值相乘的位宽。** 两个有符号整数相乘，\(M\) 位 × \(N\) 位的结果恰好需要 \(M+N\) 位（这是 Verilog `$signed * $signed` 的保证，结果不会溢出容器）。所以：

\[
c_a \times c_b \;\text{是}\; (W_{IIA}+W_{IFA})+(W_{IIB}+W_{IFB}) \;\text{位有符号数}
\]

**第二步：积的小数位宽。** 这个乘积码值 \(c_a c_b\) 代表的真实值是：

\[
a \cdot b = \frac{c_a}{2^{W_{IFA}}}\cdot\frac{c_b}{2^{W_{IFB}}}=\frac{c_a c_b}{2^{W_{IFA}+W_{IFB}}}
\]

也就是说，把乘积码值解释为定点数时，它自带 \(W_{IFA}+W_{IFB}\) 个小数位。于是：

\[
W_{RF}=W_{IFA}+W_{IFB}
\]

**第三步：积的整数位宽。** 总位宽减去小数位宽即是整数位宽：

\[
W_{RI}=(W_{IIA}+W_{IFA}+W_{IIB}+W_{IFB})-(W_{IFA}+W_{IFB})=W_{IIA}+W_{IIB}
\]

**直觉版记忆法：**

- 幅值上，\(|a|\lesssim 2^{W_{IIA}-1}\)、\(|b|\lesssim 2^{W_{IIB}-1}\)，所以 \(|a\cdot b|\lesssim 2^{W_{IIA}+W_{IIB}-2}\)，整数部分需要 \(W_{IIA}+W_{IIB}\) 位（含 1 位符号）才装得下 → \(W_{RI}=W_{IIA}+W_{IIB}\)。
- 精度上，\(a\) 的分辨率为 \(2^{-W_{IFA}}\)、\(b\) 为 \(2^{-W_{IFB}}\)，乘积的分辨率是二者之积 \(2^{-(W_{IFA}+W_{IFB})}\)，所以小数位翻倍累加 → \(W_{RF}=W_{IFA}+W_{IFB}\)。

**关键推论：** 全精度积 `res` 的容器宽度 \(W_{RI}+W_{RF}\) 恰好等于两个输入宽度之和，刚好能装下 \(M\times N\) 的乘积——**所以 `fxp_mul` 内部的乘法永远精确、永远不会溢出它自己的容器**。溢出只可能发生在最后一步，当输出格式 `(WOI,WOF)` 比积的格式 `(WRI,WRF)` 更窄时。

#### 4.1.3 源码精读

位宽公式的落地只有两行 localparam：

[RTL/fixedpoint.v:293-294](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L293-L294) —— 定义积的整数位宽 `WRI=WIIA+WIIB` 与小数位宽 `WRF=WIFA+WIFB`，与上面推导完全一致。

对比 `fxp_add` 里的同名 localparam，能立刻看出加减与乘法的本质差异：

[RTL/fixedpoint.v:125-128](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L125-L128) —— `fxp_add` 取公共位宽 `WII=max(WIIA,WIIB)`、`WIF=max(WIFA,WIFB)`，中间结果 `WRI=WII+1`（多 1 位吸进位）。加法的中间位宽只是「输入位宽 +1」，因为和的幅值是两个输入幅值之「和」；而乘法的 `WRI=WIIA+WIIB` 远大于输入，因为积的幅值是两个输入幅值之「积」。

#### 4.1.4 代码实践

**实践目标：** 用手算验证位宽公式，建立对 `WRI/WRF` 的肌肉记忆。

**操作步骤：**

1. 取一组配置 `WIIA=10, WIFA=11, WIIB=8, WIFB=12`（这是 `tb_add_sub_mul_div.v` 实际使用的配置）。
2. 手算：积的整数位宽 `WRI=10+8=18`，小数位宽 `WRF=11+12=23`，全精度积 `res` 共 `18+23=41` 位。
3. 再算一组默认配置 `WIIA=WIFA=WIIB=WIFB=8`：`WRI=16, WRF=16`，`res` 共 32 位。

**需要观察的现象：** 无论输入取什么值，`res` 的位宽只依赖配置参数、与具体数值无关——这正是「乘积位宽可提前确定」的体现。

**预期结果：** 第 2 步得 41 位、第 3 步得 32 位。可在仿真时用 `$display("res width = %d", WRI+WRF)`（需在模块内或用层次化路径引用 localparam）对照确认；若不便引用，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** `WIIA=4, WIFA=12, WIIB=6, WIFB=10`，求 `WRI`、`WRF` 与 `res` 总位宽。

> **答案：** `WRI=4+6=10`，`WRF=12+10=22`，`res` 总位宽 `10+22=32`（恰为两输入宽度 16 与 16 之和）。

**练习 2：** 为什么乘积位宽公式里小数位是「相加」而不是「取 max」？

> **答案：** 乘积的分辨率是两个输入分辨率之积 \(2^{-W_{IFA}}\cdot 2^{-W_{IFB}}=2^{-(W_{IFA}+W_{IFB})}\)，要把这么细的精度全部保留就需要 \(W_{IFA}+W_{IFB}\) 个小数位；而加法只要对齐到「较粗」的那个分辨率即可（取 max），因为和的精度不可能比两个加数更细。

---

### 4.2 fxp_mul 的两段式实现与 fxp_zoom 的复用

#### 4.2.1 概念说明

有了 4.1 的位宽公式，`fxp_mul` 的实现就异常简洁：**先用一行 `$signed` 乘法拿到全精度积，再用一个 `fxp_zoom` 把它收敛到输出位宽**。这就是「两段式」：全精度相乘 → 位宽收敛。它复用了 u1-l3 讲过的 `fxp_zoom` 来一次性完成「截断小数 + 舍入 + 溢出饱和」三件事。

#### 4.2.2 核心流程

```
ina (WIIA+WIFA位) ──┐
                    ├─ $signed(ina) * $signed(inb) ─→ res (WRI+WRF位, 全精度, 精确不溢出)
inb (WIIB+WIFB位) ──┘                                          │
                                                              ▼
                                       fxp_zoom: (WRI,WRF) → (WOI,WOF)
                                          · WOF<WRF: 砍小数位 + 按 ROUND 舍入
                                          · WOI<WRI: 检测上/下溢出 + 饱和
                                                              │
                                                              ▼
                                            out (WOI+WOF位), overflow
```

注意：**全模块只有 `res` 的乘法和一个 `fxp_zoom`，没有输入侧的 `fxp_zoom`**。这是乘法区别于加减法的最大结构特征。

#### 4.2.3 源码精读

整个 `fxp_mul` 模块只有约 30 行，核心是中间 3 处：

[RTL/fixedpoint.v:296](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L296) —— 第一段：`wire signed [WRI+WRF-1:0] res = $signed(ina) * $signed(inb);`。把两路输入直接当有符号补码相乘，得到 `WRI+WRF` 位的全精度积。`$signed` 不可或缺——若省略，Verilog 会按无符号相乘，负数会得到完全错误的结果。这里不需要先把 `ina/inb` 对齐到公共格式，因为乘法对缩放因子天然满足分配律。

[RTL/fixedpoint.v:298-308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L298-L308) —— 第二段：唯一的 `fxp_zoom` 例化 `res_zoom`，把全精度积从 `(WRI,WRF)` 搬到输出 `(WOI,WOF)`，并直接把模块参数 `ROUND` 透传进去。`$unsigned(res)` 只是把 `signed` 线网转成 `fxp_zoom` 入口所需的普通线网，比特内容不变。溢出信号 `overflow` 也由它独家输出。

对照 `fxp_add`，结构差异一目了然：

[RTL/fixedpoint.v:134-168](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L134-L168) —— `fxp_add` 例化了 **3 个** `fxp_zoom`：`ina_zoom`、`inb_zoom`（两路输入对齐到公共格式，且都用 `.ROUND(0)` 只对齐不舍入）、`res_zoom`（结果还原，用 `.ROUND(ROUND)`）。加减法必须有这两个输入侧 zoom，因为只有「同格式」的两个数才能逐位相加；而 `fxp_mul` 因为 4.1 的结论，省掉了它们，只剩 `res_zoom` 一个。

把对照整理成表：

| 维度 | `fxp_add` / `fxp_addsub` | `fxp_mul` |
| :--- | :--- | :--- |
| `fxp_zoom` 例化数 | 3（ina 对齐 + inb 对齐 + res 还原） | **1**（仅 res 收敛） |
| 中间整数位宽 `WRI` | `max(WIIA,WIIB)+1`（+1 吸进位） | `WIIA+WIIB`（幅值相乘） |
| 中间小数位宽 `WRF` | `max(WIFA,WIFB)` | `WIFA+WIFB`（精度相乘） |
| 输入是否需对齐 | 是（不同格式不能直接加） | **否**（积的格式可提前确定） |
| `ROUND` 生效点 | 仅 `res_zoom` | 仅 `res_zoom` |

最后一行是个有意思的相同点：尽管结构差异巨大，`ROUND` 在两类模块里都只在最后一个 `res_zoom` 生效——因为输入侧 zoom 永远用 `.ROUND(0)`。

#### 4.2.4 代码实践

**实践目标：** 通过阅读 testbench，确认 `fxp_mul` 的例化方式与打印套路，为下一节的动手实验做准备。

**操作步骤：**

1. 打开 [SIM/tb_add_sub_mul_div.v:62-75](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L62-L75)，看清 `fxp_mul_i` 的参数：`WIIA=10,WIFA=11,WIIB=8,WIFB=12,WOI=15,WOF=14,ROUND=1`。
2. 据此算出 `WRI=18, WRF=23`，输出 `WOI=15<18`、`WOF=14<23`——意味着这个配置下大数相乘**会**触发溢出，小数位**会**被截断舍入。
3. 看 testbench 如何把硬件结果还原为浮点对比：

[SIM/tb_add_sub_mul_div.v:116-122](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_add_sub_mul_div.v#L116-L122) —— 用 `($signed(omul)*1.0)/(1<<WOF)` 把乘法输出码值还原成浮点 HW-result，软件参考 SW-result 是 `(ina/2^WIFA)*(inb/2^WIFB)`，溢出时追加 `(o)` 标记。这套「`$signed*1.0/(1<<W)` 还原法」是全库 testbench 的万能钥匙。

**需要观察的现象：** 因为该配置 `WOI=15<WRI=18`，当 testbench 喂入两个大数（例如 `'h76de4b61 * 'hc9809a37`）时，乘积幅值远超 s15.14 范围，对应那行打印应出现 `(o)` 且 HW-result 被钳位到极值。

**预期结果：** 大数行的 HW-result 与 SW-result 相差悬殊并带 `(o)`；小数行的 HW-result 与 SW-result 误差在 1 LSB（即 \(2^{-14}\)）量级。具体哪些行溢出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `fxp_mul` 的乘法行必须写 `$signed(ina) * $signed(inb)`，而不能写 `ina * inb`？

> **答案：** `ina`/`inb` 声明为无符号 `wire [...]`，直接相乘 Verilog 会按无符号解释，负数（符号位为 1）会被当成大正数，乘积完全错误。`$signed` 把它们转回二进制补码有符号数，负数相乘才能得到正确符号与幅值。注意 `res` 本身声明为 `wire signed`，但其右值表达式的符号性由操作数决定，所以两个 `$signed()` 都不能省。

**练习 2：** `fxp_mul` 把 `ROUND` 透传给 `res_zoom`，但没有任何 `.ROUND(0)` 的输入侧 zoom。这与 `fxp_add` 一致吗？为什么乘法不需要输入侧 zoom？

> **答案：** 一致——两者都只有最后一个 zoom 用 `ROUND`。乘法不需要输入侧 zoom，是因为两个任意格式定点数相乘，积的格式 `(WIIA+WIIB, WIFA+WIFB)` 是确定的，可以直接相乘再收敛；而加法必须先把两路输入对齐到同一公共格式才能逐位相加，所以多出了两个 `.ROUND(0)` 的输入侧 zoom。

---

### 4.3 ROUND 舍入与溢出判定

#### 4.3.1 概念说明

`fxp_mul` 的两个输出语义——精度（`ROUND`）和正确性（`overflow`）——全部由唯一的 `res_zoom` 决定。本模块把这两个在 u1-l3 已建立的机制，专门落到「乘法场景」下：

- **ROUND 的唯一生效点**：积的小数位 `WRF=WIFA+WIFB` 截断到输出小数位 `WOF` 时是否四舍五入。由于两个输入小数位相加后通常远大于 `WOF`，所以乘法里 `ROUND` 几乎总是生效。
- **溢出判定**：当输出整数位 `WOI < WRI=WIIA+WIIB` 时，若积的真实幅值超出 \([-2^{WOI-1},\;2^{WOI-1}-2^{-WOF}]\)，`res_zoom` 置 `overflow=1` 并把 `out` 饱和到正最大或负最小。

#### 4.3.2 核心流程

ROUND 是否生效，先看有没有「小数位截断」：

\[
\begin{cases}
W_{OF} < W_{RF}=W_{IFA}+W_{IFB} & \Rightarrow \text{截断低位小数，ROUND 决定是否 }+1\text{ LSB（舍入）}\\
W_{OF} \ge W_{RF} & \Rightarrow \text{无小数截断，ROUND 无作用，结果精确}
\end{cases}
\]

溢出是否可能，先看整数位够不够：

\[
\begin{cases}
W_{OI} \ge W_{RI}=W_{IIA}+W_{IIB} & \Rightarrow \text{整数位永远装得下积，永不溢出（安全配置）}\\
W_{OI} < W_{RI} & \Rightarrow \text{大数相乘可能溢出，需检测并饱和}
\end{cases}
\]

> **注意：** \(W_{OI}<W_{RI}\) 只是「可能」溢出，并非「必然」溢出。例如两个绝对值小于 1 的小数相乘，积更小，即便整数位被砍也不会超限；只有当 \(|a\cdot b|\) 真正越过输出范围时才溢出。判定由 `res_zoom` 按真实数值完成，而非仅看位宽。

#### 4.3.3 源码精读

`fxp_mul` 本身不实现舍入与饱和，而是完整委托给 `fxp_zoom`。这里只点明两段 `fxp_zoom` 代码在乘法语境下的含义（内部机理见 u1-l3）：

[RTL/fixedpoint.v:41-54](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L41-L54) —— `fxp_zoom` 在 `WOF<WIF` 时截断低位小数：`ROUND=0` 直接砍，`ROUND=1` 看被砍掉的最高位决定是否 `+1`，并对「正最大值」特判以防 `+1` 引发符号翻转。在 `fxp_mul` 里，这里的 `WIF` 就是积的 `WRF=WIFA+WIFB`，所以这一段处理的是「积小数位 → 输出小数位」的精度损失。

[RTL/fixedpoint.v:65-90](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L65-L90) —— `fxp_zoom` 在 `WOI<WII` 时检测整数位溢出：正超限→上溢出饱和到正最大，负超限→下溢出饱和到负最小，并置 `overflow=1`。在 `fxp_mul` 里，这里的 `WII` 就是积的 `WRI=WIIA+WIIB`，所以这一段决定「积的整数部分是否塞得进输出」。

回到 `fxp_mul` 的例化，能清楚看到这两个机制都被激活：

[RTL/fixedpoint.v:298-308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L298-L308) —— `res_zoom` 的入参 `(WII=WRI, WIF=WRF)` 是全精度积格式，出参 `(WOI, WOF)` 是输出格式。只要 `WOI<WRI` 或 `WOF<WRF`（这在乘法里几乎是常态，因为积的位宽是两输入之和），上面两段 `fxp_zoom` 逻辑就会真正起作用。

#### 4.3.4 代码实践（核心动手实验）

**实践目标：** 构造一个「输出位宽明显窄于乘积真实范围」的 `fxp_mul`，亲手触发溢出饱和，并对比 `ROUND=1` 与 `ROUND=0` 的精度差异。

**配置选择：** 取 `WIIA=WIFA=WIIB=WIFB=8`（每路输入 s8.8，范围 \([-128,\,127.99609375]\)），输出 `WOI=WOF=8`（也是 s8.8）。则 `WRI=16, WRF=16`，全精度积是 s16.16；输出 s8.8 的整数位 `WOI=8 < WRI=16`、小数位 `WOF=8 < WRF=16`，正好同时触发「整数位砍半」的溢出检测与「小数位砍半」的舍入。

**操作步骤：**

1. 在 `SIM/` 下新建一个学习用 testbench（**示例代码**，请勿修改 `RTL/fixedpoint.v` 或已有 testbench），把 `fxp_mul` 例化为上表配置，并分别例化 `ROUND=1` 与 `ROUND=0` 两个实例做对照：

```verilog
// ============ 示例代码：学习用 testbench（读者自行新建文件） ============
`timescale 1ps/1ps
module tb_mul_study ();
localparam WIIA=8, WIFA=8, WIIB=8, WIFB=8, WOI=8, WOF=8;
reg  [WIIA+WIFA-1:0] ina = 0;
reg  [WIIB+WIFB-1:0] inb = 0;
wire [WOI +WOF -1:0] out_r1, out_r0;
wire                 ovf_r1, ovf_r0;

fxp_mul #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
          .WOI(WOI),.WOF(WOF),.ROUND(1)) mul_r1 (.ina(ina),.inb(inb),.out(out_r1),.overflow(ovf_r1));
fxp_mul #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
          .WOI(WOI),.WOF(WOF),.ROUND(0)) mul_r0 (.ina(ina),.inb(inb),.out(out_r0),.overflow(ovf_r0));

task show;
    input [15:0] _ina; input [15:0] _inb;
begin
    #10000 ina=_ina; inb=_inb; #10000
    $display("a=%f b=%f | true=%f | R1=%f (ovf=%b) | R0=%f (ovf=%b)",
        ($signed(ina)*1.0)/(1<<WIFA), ($signed(inb)*1.0)/(1<<WIFB),
        (($signed(ina)*1.0)/(1<<WIFA))*(($signed(inb)*1.0)/(1<<WIFB)),
        ($signed(out_r1)*1.0)/(1<<WOF), ovf_r1,
        ($signed(out_r0)*1.0)/(1<<WOF), ovf_r0);
end
endtask

initial begin
    show('h6400, 'h6400);   // 100.0 * 100.0 = 10000.0  → 必然溢出
    show('h020B, 'h030C);   // (2+11/256)*(3+12/256) ≈ 6.2248 → 在范围内, 观察 ROUND 差异
    show('h0100, 'h0100);   // 1.0 * 1.0 = 1.0 → 精确, 不溢出
    $finish;
end
endmodule
```

2. 用与官方脚本相同的命令编译运行（testbench 与 RTL 同时参与编译）：

```bash
iverilog -g2001 -o sim.out tb_mul_study.v ../RTL/fixedpoint.v && vvp -n sim.out
```

**需要观察的现象：**

- 第一组 `100.0*100.0=10000.0`：积的真实值远超 s8.8 的上限 \(127.99609375\)。预期 `ovf_r1=ovf_r0=1`，且两个实例的 `out` 都被饱和到正最大值 `0x7FFF`（即 \(127.99609375\)），HW-result 不等于 10000。
- 第二组 \((2+\tfrac{11}{256})(3+\tfrac{12}{256})\approx 6.2248\)：积在范围内不溢出（`ovf=0`），但积的小数位 s16.16 截断到 s8.8 时存在舍入。该乘积码值的低字节为 `0x84`（即被砍掉的最高位为 1），因此 `ROUND=1` 会 `+1 LSB`、`ROUND=0` 直接截断：预期 `R1≈6.2265625`、`R0≈6.22265625`，真值约 `6.2248`，`R1` 误差更小。
- 第三组 `1.0*1.0=1.0`：积精确可表示，`R1==R0==1.0`，`ovf=0`，证明无截断时 `ROUND` 无作用。

**预期结果：** 第一组两实例均 `overflow=1` 且输出钳位到 `0x7FFF`；第二组 `R1` 与 `R0` 相差 1 LSB 且 `R1` 更接近真值；第三组两者完全一致。若本地 iverilog 行为与此不符，以实际仿真输出为准（关键现象是「大数溢出饱和」「ROUND 影响末位」两点）。

#### 4.3.5 小练习与答案

**练习 1：** 配置 `WIIA=WIFA=WIIB=WIFB=WOI=WOF=8`。判断：是否存在不溢出的输入？给出一个不溢出、但 `ROUND=1` 与 `ROUND=0` 输出不同的例子。

> **答案：** 存在大量不溢出的输入（任何 \(|a\cdot b|\le 127.99609375\) 的组合）。例如 4.3.4 中的 \((2+\tfrac{11}{256})(3+\tfrac{12}{256})\approx 6.2248\) 在范围内不溢出，但其 s16.16 表示的低字节为 `0x84`，被砍最高位为 1，故 `ROUND=1` 得 `6.2265625`、`ROUND=0` 得 `6.22265625`，两者不同且 `ROUND=1` 更接近真值。

**练习 2：** 若把输出改为 `WOI=16, WOF=16`（其余仍为 8），`overflow` 还会发生吗？为什么？

> **答案：** 不会。因为此时 `WOI=16 ≥ WRI=WIIA+WIIB=16`，整数位永远装得下积的整数部分；同时 `WOF=16 ≥ WRF=16`，小数位也不截断。`res_zoom` 既不触发整数溢出检测，也不触发小数舍入，输出等于全精度积，`overflow` 恒为 0、`ROUND` 也无作用。这正是「安全配置」\(W_{OI}\ge W_{IIA}+W_{IIB}\) 的体现。

**练习 3：** 为什么即便 `WOI < WIIA+WIIB`，也并非所有输入都溢出？

> **答案：** 溢出与否取决于积的**真实数值**是否越过输出范围 \([-2^{WOI-1},\,2^{WOI-1}-2^{-WOF}]\)，而非仅看位宽是否够。两个绝对值小于 1 的小数相乘，积的幅值比输入还小，即使整数位被砍也不会超限。`res_zoom` 是按 `ini` 的实际符号位与高位判定上/下溢出的，所以是「逐输入数值判定」。

## 5. 综合实践

把本讲三条主线——**位宽推导、ROUND 舍入、溢出饱和**——串成一个自检任务：

1. **选定一个非平凡配置**，例如 `WIIA=5, WIFA=11, WIIB=7, WIFB=9, WOI=6, WOF=10, ROUND=1`。
2. **先手算再验证**：算出 `WRI=12, WRF=20`，全精度积 32 位；输出 `WOI=6<12` 可能溢出，`WOF=10<20` 会舍入。
3. **写一个最小 testbench**（参考 4.3.4 的示例代码结构），同时例化 `ROUND=1` 与 `ROUND=0`，喂入三类输入：
   - 一组必然溢出的大数（如各自接近 s5.11 / s7.9 的正最大值），确认 `overflow=1` 且 `out` 钳位到正最大；
   - 一组在范围内、但积小数位截断会触发舍入的数，确认 `R1≠R0` 且 `R1` 误差更小；
   - 一组积可精确表示的数，确认 `R1==R0` 且 `overflow=0`。
4. **用 `$signed(out)*1.0/(1<<WOF)` 还原输出**，与软件参考 `((ina*1.0)/(1<<WIFA))*((inb*1.0)/(1<<WIFB))` 逐组对比，统计每组误差是否在 1 LSB（\(2^{-WOF}\)）以内。
5. **运行**：`iverilog -g2001 -o sim.out <你的tb>.v ../RTL/fixedpoint.v && vvp -n sim.out`。

完成标志：你能不查源码说出「为什么 `fxp_mul` 只有一个 `fxp_zoom`」，并能对任意一组配置预判「会不会溢出、ROUND 会不会生效」。

## 6. 本讲小结

- 定点乘积位宽可提前确定：整数位宽 \(W_{RI}=W_{IIA}+W_{IIB}\)、小数位宽 \(W_{RF}=W_{IFA}+W_{IFB}\)，源于「幅值相乘、精度相乘」。
- `fxp_mul` 是极简的两段式：`$signed(ina)*$signed(inb)` 拿全精度积，再由唯一的 `fxp_zoom` 收敛到 `(WOI,WOF)`；全精度积本身精确且不溢出容器。
- 与 `fxp_add` 的 3 个 `fxp_zoom` 相比，`fxp_mul` 只有 1 个——因为乘法不需要把输入对齐到公共格式，积的格式天然确定。
- `ROUND` 在乘法里唯一生效于 `res_zoom`：控制积小数位截断时的四舍五入；当 `WOF≥WRF` 时无截断、`ROUND` 无作用。
- 溢出只发生在 `WOI<WRI` 且积的真实幅值越界时，由 `res_zoom` 按数值（而非仅位宽）检测并饱和钳位；`WOI≥WIIA+WIIB` 是「永不溢出」的安全配置。
- 全精度乘法行里两个 `$signed()` 缺一不可，否则负数被当无符号大正数，结果完全错误。

## 7. 下一步学习建议

- 本讲的 `fxp_mul` 是**组合逻辑（单周期）**实现。下一篇 [u3-l1（流水线设计模式与 fxp_mul_pipe）](./u3-l1-mul-pipe.md) 会把它改造成 2 级流水线：把乘法寄存一拍、把 `fxp_zoom` 结果再寄存一拍，引入 `rstn/clk` 与固定延迟。建议先读 [RTL/fixedpoint.v:326-380](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L326-L380) 的 `fxp_mul_pipe`，体会「功能等价、只多了延迟」的流水线改造套路。
- 若想继续横向对比，可跳到 [u2-l3（fxp_div 除法）](./u2-l3-div.md)：除法的位宽推导（`WRI/WRF` 取 max 而非相加）与乘法形成鲜明对照，能加深对「为什么只有乘积位宽是相加」的理解。
- 阅读建议：把本讲的 `fxp_mul` 与 `fxp_add`（[RTL/fixedpoint.v:110-170](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L110-L170)）并排打开对照，是理解全库「`fxp_zoom` 复用模式」最有效的方式。
