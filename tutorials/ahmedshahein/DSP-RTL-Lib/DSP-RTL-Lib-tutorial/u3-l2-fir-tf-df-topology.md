# 转置型与直接型 FIR 结构

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「同一个 FIR 卷积」为什么可以有两种截然不同的硬件实现：**转置型（Transposed Form, TF）** 与 **直接型（Direct Form, DF）**。
- 看懂 `filt_fir.v` 里 `generate ... if (gp_tf_df) ... else ...` 是如何用**一个参数**在两种拓扑之间二选一的。
- 指出两种结构里**寄存器分别插在哪里**：TF 把寄存器插在「乘加链之间」，DF 把寄存器插在「输入延迟线」上。
- 量化对比两者的**关键路径深度**与**寄存器位宽成本**，理解工程上「拿面积换速度」的取舍。
- 通过仿真亲手验证：两种拓扑在**样本对齐上是等价的**（都在第 \(n\) 个时钟给出 \(y[n]\)），差异只在「电路内部的组合路径长短」。

本讲承接 [u3-l1](u3-l1-fir-structure.md)（FIR 原理与 `filt_fir` 接口结构），把视线从「接口与系数」收窄到「**generate 块内部的数据流拓扑**」。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下内容（来自前置讲义）：

- **FIR 卷积**：\(y[n]=\sum_{k=0}^{N-1} h[k]\cdot x[n-k]\)。硬件上需要三样东西——保存历史样本的**延迟线**、计算 \(h[k]\cdot x[n-k]\) 的**乘法器**、把它们累加起来的**加法树**。([u3-l1](u3-l1-fir-structure.md))
- **dff 原语**：`dff` 是全库唯一的寄存器原子块，使能有效时它就是一个单位延迟 \(z^{-1}\)。([u2-l2](u2-l2-dff-primitive.md))
- **定点位宽增长**：两个 \(W\) 位补码数相乘最多增长到 \(2W\) 位；\(K\) 个数求和再增长 \(\lceil\log_2 K\rceil\) 位。([u2-l1](u2-l1-fixed-point-bitwidth.md))
- **命名前缀**：`gp_` 可覆盖参数、`c_` 派生常量、`r_` 寄存器、`w_` 组合连线。([u1-l4](u1-l4-coding-style-and-interface.md))

> 一个关键直觉：卷积公式只规定了「算什么」，**没有规定「寄存器插在哪里」**。把同一个公式写成电路时，寄存器可以放在输入侧、输出侧，也可以放在中间——这就是 TF 与 DF 的分歧根源。

## 3. 本讲源码地图

本讲只精读一个文件，但它是全库最典型的「参数化结构选择」范例：

| 文件 | 作用 |
| --- | --- |
| `.drl_src_code/filt_fir/rtl/filt_fir.v` | FIR 主体。一个 `generate` 块用 `gp_tf_df` 在 TF/DF 间二选一；第二个 `generate` 选择对应的输出抽头。 |
| `.drl_src_code/filt_fir/rtl/dff.v` | 两种结构都复用的寄存器原语，理解「寄存器插在哪」的物理载体。 |
| `.drl_src_code/filt_fir/octave/stimuli.m` | 黄金参考模型（GRM）。注意 `p_tf_df` 只流向 RTL，**不参与** `yy=filter(b,1,...)` 响应生成——这是「两种拓扑样本对齐」的关键证据。 |
| `.drl_src_code/filt_fir/sim/testbench/filt_fir_tb.sv` | 测试台。用同一份响应文件验证 TF 与 DF。 |

## 4. 核心概念与源码讲解

### 4.1 generate 结构选择：一个参数切换两种拓扑

#### 4.1.1 概念说明

`filt_fir` 不会同时综合出 TF 和 DF 两套电路。它用 `gp_tf_df` 这一个参数在编译期二选一：

- `gp_tf_df = 1` → **转置型（TF）**
- `gp_tf_df = 0` → **直接型（DF）**

这是 Verilog `generate` 机制的典型用法：`if (gp_tf_df)` 在**elaboration 阶段**（把参数代进去、展开电路的阶段）决定生成哪一段硬件。未选中那段**完全不存在**于最终网表里，不消耗任何资源。这也是为什么 TF 和 DF 可以用**同一个测试台、同一份黄金响应**来验证——它们实现的是同一个传递函数。

#### 4.1.2 核心流程

```text
读 gp_tf_df
   ├── 1  → 展开第一段 generate（g_fir_tf）：广播输入 + 乘加链间寄存器
   └── 0  → 展开第二段 generate（g_fir_df）：输入延迟线 + 组合加法链

第二个 generate 再按 gp_tf_df 选择输出抽头
   ├── 1  → o_data = w_add 的最低字（stage 0，TF 的最终和）
   └── 0  → o_data = w_add 的最高字（stage N-1，DF 的最终和）
```

#### 4.1.3 源码精读

参数声明里直接写明语义「1-> TF, 0-> DF」：

[.drl_src_code/filt_fir/rtl/filt_fir.v:6-13](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L6-L13) — 模块参数表，`gp_tf_df` 在第 10 行，注释 `1-> TF, 0-> DF`。

> 注意第 12 行的 `gp_oup_width` 默认表达式：`gp_inp_width+gp_coeff_width+$clog2(gp_coeff_length)`。这是个**派生默认值**，调用方可留空让它自动算，也可覆盖。两种拓扑共享同一个位宽推导（详见 [u2-l1](u2-l1-fixed-point-bitwidth.md)）。

结构选择的 generate 入口：

[.drl_src_code/filt_fir/rtl/filt_fir.v:34-38](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L34-L38) — `generate` 开始，`if (gp_tf_df)` 选择 TF 分支。

对应的 `else` 分支入口（DF）在第 85 行：

[.drl_src_code/filt_fir/rtl/filt_fir.v:85-87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L85-L87) — `else` 进入 DF 分支，声明输入延迟线 `r_dly_df`。

第二个 generate 决定从哪一「级」取输出：

[.drl_src_code/filt_fir/rtl/filt_fir.v:144-149](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L144-L149) — 输出抽头选择：TF 取 `w_add` 最低字，DF 取最高字。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「同一个 `.param` 只改一个值，电路就从 TF 变成 DF」。
2. **操作步骤**：
   - 复制 `.drl_param/filt_fir_1.param` 为两份，分别令 `gp_tf_df = 1` 与 `gp_tf_df = 0`。
   - 按 [u1-l3](u3-l1-fir-structure.md) 的流程，分别用 `./dsp_rtl_lib.sh` 触发「生成 RTL → Octave 生成激励/响应 → iverilog 仿真」。
3. **需要观察的现象**：两轮仿真都打印 `### INFO: Testcase PASSED with ... samples`。
4. **预期结果**：两份配置的 `PASSED` 样本数完全一致，因为它们用的是同一份黄金响应文件。
5. **待本地验证**：具体子命令名以你本机 `dsp_rtl_lib.sh` 的帮助为准；若环境无 iverilog/Octave，则改为下面的「源码阅读型实践」——直接对照第 38 行与第 85 行，确认 `if/else` 把两段互斥的 `generate` 体隔开。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `gp_tf_df` 设成 `2`，会发生什么？
**答案**：Verilog 里 `2` 在 `if (gp_tf_df)` 中为真，所以会被当作 TF 处理。它不是「第三种结构」，只是非 0 即真。规范用法只取 `0`/`1`。

**练习 2**：为什么 TF 和 DF 能共用同一个测试台和同一份 `response_tc_*.dat`？
**答案**：因为它们实现同一个传递函数 \(H(z)=\sum_k h[k]z^{-k}\)，在第 \(n\) 个时钟都给出 \(y[n]\)，样本对齐完全一致；GRM 算的是「正确答案」，与拓扑无关。

---

### 4.2 转置型（TF）：寄存器插在乘加链之间的乘加链

#### 4.2.1 概念说明

转置型的核心特征是：**所有乘法器读的都是同一个当前输入 `i_data`**，而把「保存历史」的职责从输入侧搬到了**累加通路**上——每两个乘加级之间插一级 `dff` 寄存器。

直观对比直接型（下一节）：直接型是「先把输入延迟 N 拍，再相乘相加」；转置型是「让每个乘法器都看当前输入，但让部分和在寄存器里『轮流接力』」。这就像一条流水线：产品（部分和）一站站往下传，每站都加上「当前输入 × 自己那一级的系数」。

转置型的工程价值在于：**关键路径被寄存器切碎**。每个时钟周期内，信号只需穿过「一个乘法器 + 一个加法器」就会落进下一级寄存器，于是这个滤波器能跑在很高的时钟频率上。

#### 4.2.2 核心流程

TF 的数据流（tap 编号 \(i=0\ldots N-1\)，注意「最终和」落在 \(i=0\)）：

```text
i_data ──┬──> × h[N-1] ──> (+) ──┐   ← stage N-1：只有乘，无前级反馈
         │                        │
         ├──> × h[N-2] ──> (+) <──┴── dff ──┐   ← stage N-2
         │                                    │
         ├──> × h[i]   ──> (+) <──────────────┴── dff ──┐   ← stage i
         │                                                │
         └──> × h[0]   ──> (+) <──────────────────────────┴── dff ──> o_data = stage 0
```

每一级 `stage i` 的加法器输入是「`i_data × h[i]`（组合）」加上「上一级 `stage i+1` 经 `dff` 寄存后的部分和」。用数学归纳可以证明，第 \(t\) 个时钟的输出正好是完整的卷积：

\[
o\_data[t]=w\_add_{0}[t]=\sum_{m=0}^{N-1} x[t-m]\cdot h[m]=y[t]
\]

也就是说，TF **从第 0 个时钟就给出正确的 \(y[0]\)**——因为寄存器复位为 0，恰好对应零初始条件 \(x[t<0]=0\)。这正是转置型「零样本延迟、高吞吐」的魅力。

#### 4.2.3 源码精读

TF 分支里，每个乘法器都引用 `$signed(i_data)`——「广播当前输入」的铁证：

[.drl_src_code/filt_fir/rtl/filt_fir.v:41-57](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L41-L57) — TF 乘法：第 47、51、56 行都是 `$signed(i_data) * c_coeff[...]`，所有 tap 共用 `i_data`。对称分支（`gp_symm`）只是改系数下标，乘法器输入仍是 `i_data`。

加法链：最后一级（`i==gp_coeff_length-1`）只有乘、不累加；其余级把「本级乘积」加上「前一级寄存器输出」：

[.drl_src_code/filt_fir/rtl/filt_fir.v:59-66](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L59-L66) — TF 加法。`w_add[i] = w_mul[i] + r_dly_tf[...]`，`r_dly_tf` 就是上一级的寄存器输出。

**寄存器插在乘加链之间**——这是本讲的题眼：

[.drl_src_code/filt_fir/rtl/filt_fir.v:68-79](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L68-L79) — `if (i>0)` 时例化一个 `dff`，输入接本级 `w_add[i]`，输出接 `r_dly_tf`。位宽是 `c_add_oup_width`（累加器全宽）。也就是说，每一级乘加的「和」都被寄存一拍，再喂给低一级的加法器。

TF 的延迟线宽度声明在第 40 行——注意它用的是 `c_add_oup_width`（宽），不是输入宽度：

[.drl_src_code/filt_fir/rtl/filt_fir.v:39-40](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L39-L40) — `r_dly_tf` 宽度 `(gp_coeff_length-1)*c_add_oup_width`，存放的是「部分和」而非「原始样本」。

#### 4.2.4 代码实践

1. **实践目标**：用纸笔追踪 TF，确认它「第 0 拍就给出 \(y[0]\)」。
2. **操作步骤**：
   - 取 3 抽头 \(N=3\)，系数 \(h=[h_0,h_1,h_2]\)，输入序列 \(x[0],x[1],x[2]\)，设 \(x[t<0]=0\)。
   - 所有寄存器初值 = 0。逐拍填表（每拍先算组合的 `w_mul`、`w_add`，再在时钟沿把 `w_add` 打入寄存器）：

     | 拍 \(t\) | `i_data` | `w_add[2]` | 寄存器(原 `w_add[1]`) | `w_add[0]`=o_data | 期望 \(y[t]\) |
     |---|---|---|---|---|---|
     | 0 | \(x_0\) | \(x_0 h_2\) | 0 | \(x_0 h_0\) | \(x_0 h_0\) |
     | 1 | \(x_1\) | \(x_1 h_2\) | \(x_0 h_1\) | \(x_1 h_0 + x_0 h_1\) | 同左 |
     | 2 | \(x_2\) | \(x_2 h_2\) | \(x_1 h_1 + x_0 h_2\) | \(x_2 h_0 + x_1 h_1 + x_0 h_2\) | 同左 |

3. **需要观察的现象**：`o_data` 列与「期望 \(y[t]\)」列逐拍相等。
4. **预期结果**：从 \(t=0\) 起，TF 输出即与卷积公式吻合，无需「填充」若干拍。
5. **说明**：这是「源码阅读 + 手算」型实践；若要仿真，可在第 70 行的 `dff` 例化处临时加 `$display` 打印 `r_dly_tf` 验证。

#### 4.2.5 小练习与答案

**练习 1**：TF 用了多少个寄存器、每个多宽？以 `filt_fir_1.param`（\(N=17\)、输入 8 位、系数 8 位）为例。
**答案**：`c_add_oup_width = 8+8+$clog2(17) = 16+5 = 21` 位。寄存器数 \(=N-1=16\)，每个 21 位，共 \(16\times21=336\) 位。

**练习 2**：为什么 TF 的乘法器不需要读历史样本？
**答案**：因为「历史」被编码在累加通路里的部分和寄存器中。当前输入 `i_data` 与每个系数相乘后，加进「正流向输出」的部分和，历史样本的信息已经融在里面了。

---

### 4.3 直接型（DF）：输入延迟线 + 组合加法链

#### 4.3.1 概念说明

直接型是最「教科书」的实现：先把输入信号送进一条**移位寄存器延迟线**（保存 \(x[n], x[n-1], \ldots, x[n-(N-1)]\)），再让 \(N\) 个乘法器并行地从延迟线里各取一个样本与对应系数相乘，最后用一个**组合加法链**把所有乘积加起来。

它的特点恰好与 TF 互补：

- 寄存器**只在输入侧**，存的是**原始样本**（窄，输入位宽）。
- 加法**完全是组合逻辑**，没有寄存器打断——所以加法链是一条「行波链」，关键路径比 TF 长得多。

#### 4.3.2 核心流程

```text
i_data ──> [r_dly_df[0]=i_data] ──> dff ──> [r_dly_df[1]=x[n-1]] ──> dff ──> [r_dly_df[2]=x[n-2]] ──> ...
              │                                │                                │
              × h[0]                           × h[1]                           × h[2]
              └────────── w_add[0] ──>(+)──> w_add[1] ──>(+)──> w_add[2] ── ... ──> o_data
```

延迟线里 `r_dly_df[i][t] = x[t-i]`（第 0 槽是组合直连 `i_data`，其余靠 `dff` 移位）。于是：

\[
o\_data[t]=\sum_{i=0}^{N-1} r\_dly\_df[i][t]\cdot h[i]=\sum_{i=0}^{N-1} x[t-i]\cdot h[i]=y[t]
\]

DF 同样在第 \(t\) 个时钟给出 \(y[t]\)，与 TF 在样本上完全对齐。

#### 4.3.3 源码精读

DF 的延迟线——第 0 槽组合直连，其余 `dff` 移位：

[.drl_src_code/filt_fir/rtl/filt_fir.v:122-140](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L122-L140) — `i==0` 时 `r_dly_df[0] = i_data`（第 126 行，组合）；`i>0` 时例化 `dff` 做移位（第 130-138 行），位宽是 `gp_inp_width`（窄）。注意例化名仍叫 `FIR_TF_DFF`，属历史命名残留，但功能是 DF 延迟线。

乘法器从延迟线取样本（而非广播当前输入）：

[.drl_src_code/filt_fir/rtl/filt_fir.v:88-108](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L88-L108) — DF 乘法：`$signed(r_dly_df[...]) * c_coeff[...]`，每个 tap 读不同延迟样本。与 TF 的 `$signed(i_data)` 形成鲜明对照。

组合加法链（行波结构）：

[.drl_src_code/filt_fir/rtl/filt_fir.v:110-120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L110-L120) — `i==0` 时 `w_add[0]=w_mul[0]`；其余 `w_add[i] = w_mul[i] + w_add[i-1]`。这是一条**没有寄存器打断**的串联链，最终和落在 `w_add[N-1]`。

DF 的延迟线宽度声明在第 87 行——用的是 `gp_inp_width`（窄）：

[.drl_src_code/filt_fir/rtl/filt_fir.v:85-87](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L85-L87) — `r_dly_df` 宽度 `gp_coeff_length*gp_inp_width`，存的是原始输入样本。

#### 4.3.4 代码实践

1. **实践目标**：确认 DF 的延迟线就是一条「输入移位寄存器」，并看清加法链是组合的。
2. **操作步骤**：
   - 读第 122-140 行：数一共有多少个 `dff`，每个存几位。
   - 读第 110-120 行：确认 `w_add` 链里**没有任何 `dff`**（纯 `assign`）。
3. **需要观察的现象**：延迟线寄存器位宽 = 输入位宽；加法链全组合。
4. **预期结果**：以 \(N=17\)、8 位输入为例，DF 寄存器数 \(=N-1=16\)，每个 8 位，共 128 位（远少于 TF 的 336 位）。
5. **说明**：这是源码阅读型实践。若想仿真验证，方法见 4.4.4。

#### 4.3.5 小练习与答案

**练习 1**：DF 的加法链为什么叫「行波（ripple）」结构？它对时序有什么影响？
**答案**：因为 `w_add[i]` 依赖 `w_add[i-1]`，逐级串联，信号要穿过 \(N-1\) 个加法器才到达输出，组合深度随抽头数线性增长。抽头越多，最高工作时钟越低。

**练习 2**：DF 第 0 槽 `r_dly_df[0]` 为什么是组合直连 `i_data`，而不是 `dff`？
**答案**：当前输入 \(x[n]\) 本就是「最新样本」，无需延迟；把它直接接进乘法器等价于读「0 拍延迟」。若硬加一级 `dff`，整体输出就会整体延后一拍，破坏样本对齐。

---

### 4.4 两种结构的工程对比：关键路径、寄存器成本与吞吐

#### 4.4.1 概念说明

TF 与 DF 实现同一个函数、同样的样本对齐、同样的吞吐（都是 1 样本/时钟）。它们的分歧纯粹是**电路结构**层面的「面积 vs 速度」取舍。理解这一节，就理解了为什么要用 `generate` 给设计者留这个选择。

#### 4.4.2 核心流程与定量对比

以 `filt_fir_1.param`（\(N=17\)、输入 8 位、系数 8 位，`c_add_oup_width=21`）为基准：

| 维度 | 直接型 DF (`gp_tf_df=0`) | 转置型 TF (`gp_tf_df=1`) |
| --- | --- | --- |
| 寄存器位置 | 输入侧延迟线 | 乘加链之间 |
| 寄存器内容 | 原始样本（窄） | 部分和（宽） |
| 单个寄存器位宽 | `gp_inp_width` = 8 | `c_add_oup_width` = 21 |
| 寄存器数量 | \(N-1\) = 16 | \(N-1\) = 16 |
| 寄存器总位数 | \(16\times8=128\) | \(16\times21=336\) |
| 关键路径 | \(t_{\text{mult}}+(N-1)\cdot t_{\text{add}}\) | \(t_{\text{mult}}+1\cdot t_{\text{add}}\) |
| 最高时钟频率 | 低（随抽头数下降） | 高（与抽头数几乎无关） |
| 样本延迟 | 0（第 \(n\) 拍给 \(y[n]\)） | 0（第 \(n\) 拍给 \(y[n]\)） |
| 吞吐 | 1 样本/时钟 | 1 样本/时钟 |

关键路径的差距是核心：

\[
\text{DF 路径}=t_{\text{mult}}+(N-1)\,t_{\text{add}},\qquad
\text{TF 路径}=t_{\text{mult}}+t_{\text{add}}
\]

TF 用「更宽、更多位的寄存器」（336 vs 128 位）换来了「更短的关键路径」。当抽头数 \(N\) 很大（如 `stimuli.m` 里默认 \(N=64\)）时，DF 的行波加法链会显著拖慢时钟，TF 的优势就更明显。

#### 4.4.3 源码精读

把两种结构的「寄存器插点」并排看，差异一目了然：

- TF 寄存器在乘加链之间（位宽 `c_add_oup_width`）：[filt_fir.v:68-79](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L68-L79)
- DF 寄存器在输入延迟线（位宽 `gp_inp_width`）：[filt_fir.v:122-140](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L122-L140)

GRM 只算一次答案、不区分拓扑的铁证——`p_tf_df` 进了 `defines`，却没进 `yy=filter(...)`：

[.drl_src_code/filt_fir/octave/stimuli.m:114-128](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/octave/stimuli.m#L114-L128) — `p_tf_df` 只写入 `defines` 结构（第 117 行），而 `yy=filter(b,1,octave_data)`（第 125 行）完全不引用它。所以同一份 `response_tc_*.dat` 既验证 TF 也验证 DF。

#### 4.4.4 代码实践（本讲主实践）

> 本任务对应规格里的实践要求：在同一参数下分别令 `gp_tf_df=1` 和 `0`，对比两种拓扑，并解释为何 TF 的寄存器在乘加链之间。

1. **实践目标**：用仿真验证「TF 与 DF 样本对齐、输出逐比特相同」，并理解真正的差异在关键路径而非延迟。
2. **操作步骤**：
   1. 复制 `.drl_param/filt_fir_1.param` 为 `tf.param`（`gp_tf_df=1`）与 `df.param`（`gp_tf_df=0`），其余参数（输入/系数位宽、抽头数、对称性）保持一致。
   2. 分别走构建流水线（参考 [u1-l3](u3-l1-fir-tf-df-topology.md)）：Octave 生成激励/响应/defines → iverilog 编译仿真。
   3. 记录两轮每个 testcase 的 `PASSED with ... samples`，以及首个有效样本出现的时刻。
   4. （可选）用 `-s` 或导出 VCD，在波形上比较两种拓扑首个非零输出相对首个输入的时钟差。
3. **需要观察的现象**：
   - 两轮的样本数一致、均 `PASSED`；
   - **首个有效样本出现的时钟完全相同**（都在首个使能拍给出 \(y[0]\)）——即「首个有效样本延迟差」= 0。
4. **预期结果 / 结论**：实验会推翻「两种拓扑存在样本延迟差」的直觉。真正的差异是：
   - **DF** 的加法链是组合行波（[L110-120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L110-L120)），关键路径 \(t_{\text{mult}}+(N-1)t_{\text{add}}\) 随抽头数变长，最高时钟较低；
   - **TF** 把寄存器插在乘加链之间（[L68-79](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L68-L79)），把长加法链切成「一乘一加」的小段，关键路径缩短到 \(t_{\text{mult}}+t_{\text{add}}\)，代价是寄存器更宽（21 位 vs 8 位）。
   
   所以「TF 的寄存器在乘加链之间」是为了**打断组合加法链、提升可工作频率**，而不是为了引入样本延迟。
5. **待本地验证**：若无 iverilog/Octave 环境，改用 4.2.4 的手算追踪 + 本节源码对照，同样可得出「样本对齐一致、差异在关键路径」的结论。

#### 4.4.5 小练习与答案

**练习 1**：若一个 FIR 抽头数 \(N=64\)、目标是高采样率，你会选 TF 还是 DF？为什么？
**答案**：选 TF。DF 的行波加法链有 63 级串联，关键路径过长；TF 关键路径只有「一乘一加」，更适合高时钟。代价是 64 级宽位寄存器（部分和位宽）。

**练习 2**：DF 的加法链（[L110-120](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_fir/rtl/filt_fir.v#L110-L120)）是行波结构。若不改成 TF，还有什么办法缩短它的关键路径？
**答案**：把行波链改成「平衡二叉加法树」，深度从 \(N-1\) 降到 \(\lceil\log_2 N\rceil\)；或在加法树里插入流水寄存器。但前者仍属组合逻辑、后者会引入样本延迟，需权衡。

## 5. 综合实践

把本讲的知识串起来，完成一次「拓扑审计」：

1. 打开 `.drl_src_code/filt_fir/rtl/filt_fir.v`，定位 `generate`（第 34 行）与输出选择 `generate`（第 144 行）。
2. 画两张数据流图：一张 TF（广播输入 + 链间寄存器），一张 DF（输入延迟线 + 组合加法链）。在图上标出每个 `dff` 的位置与位宽。
3. 以 \(N=17\)、8/8 位为基准，在图旁标注：TF 寄存器总位数（336）、DF 寄存器总位数（128）、两者关键路径表达式。
4. 写一段话回答：为什么 `stimuli.m` 里 `p_tf_df` 不参与黄金响应的计算？这说明了 TF 与 DF 什么关系？
5. （进阶）查阅 `filt_mac`（[u3-l4](u3-l4-filt-mac.md) 将详述）：它用「单乘法器分时复用」实现 FIR，是第三种取舍（牺牲吞吐换面积）。把它与 TF/DF 并列，写一张「面积-速度」三象限对比表。

通过这一题，你应能向别人解释清楚：「同一个卷积，三种电路；TF 拿宽寄存器换高频，DF 用窄寄存器但频率受限，MAC 用时间换乘法器。」

## 6. 本讲小结

- `filt_fir` 用 **`generate + if(gp_tf_df)`** 在编译期二选一：`1`→转置型 TF，`0`→直接型 DF；未选中的那段不进网表。
- **TF**：所有乘法器读同一个当前输入 `i_data`，寄存器插在**乘加链之间**，存的是宽位「部分和」；关键路径 = 一乘一加，利于高频。
- **DF**：输入先进**移位寄存器延迟线**，乘法器各取一个延迟样本，加法是**组合行波链**；寄存器窄（输入位宽），但关键路径随抽头数线性增长。
- 两者**样本对齐完全一致**：都在第 \(n\) 个时钟给出 \(y[n]\)，零样本延迟、同吞吐。证据是 GRM 的 `p_tf_df` 不参与响应计算，同一份黄金文件验证两种拓扑。
- 取舍本质：TF 用「更宽更多位的寄存器」换「更短的关键路径」，即面积换速度。
- `gp_symm` 只影响系数下标镜像，不改变 TF/DF 的拓扑结构（对称优化的细节留给 [u3-l3](u3-l3-fir-symmetric-coefficients.md)）。

## 7. 下一步学习建议

- **下一讲 [u3-l3 对称系数优化](u3-l3-fir-symmetric-coefficients.md)**：深入 `gp_symm` 分支，看线性相位 FIR 如何用「预加 \(x[n]+x[N-1-n]\)」把乘法器减半——它建立在你看懂 TF/DF 数据流的基础上。
- **再下一讲 [u3-l4 filt_mac](u3-l4-filt-mac.md)**：第三种 FIR 取舍——单个乘法器 + 累加器分时复用，对比 TF/DF 的并行结构，理解「面积-吞吐」的完整谱系。
- **横向联系**：本讲的「延迟线 + 乘加」思想会在 [u4 CIC](u4-l1-cicd-decimation.md) 与 [u5 多相滤波器](u5-l1-ppd-top-level.md) 中再次出现，只是延迟对象变成「积分器/梳齿」或「多相支路」。建议学完本讲后，回头比较三种模块对「寄存器插点」的不同选择。
