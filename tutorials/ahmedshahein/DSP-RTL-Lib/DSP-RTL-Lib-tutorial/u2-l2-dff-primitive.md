# dff.v — 基础寄存器原语

## 1. 本讲目标

本讲是「定点数与共享原语」单元的第二讲。学完后你应该能够：

- 一眼读懂 `dff.v` 的参数 `gp_data_width`、五个端口和它背后的时序约定。
- 解释 `always @(posedge i_clk or negedge i_rst_an)` 这个异步复位写法的工作原理与优先级。
- 理解 `dff` 在 DSP-RTL-Lib（下称 DRL）中的地位：它是全库所有时序单元（寄存器、延迟线、累加器、换向器）的**原子构建块**，相当于 DSP 信号流图里的单位延迟算子。
- 自己动手用 3 个 `dff` 原语搭出一个串行移位寄存器，并用 `iverilog` 编译通过。

本讲承接 [u1-l4 统一编码风格与接口约定](u1-l4-coding-style-and-interface.md)：那里确立了「异步低有效复位 + 同步高有效使能 + 上升沿」的三段式骨架和 `gp_/c_/r_/w_` 命名前缀。本讲不再重复这些约定的定义，而是把 `dff.v` 当成这个约定的**第一个完整范例**逐行拆透。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的两个基础概念。如果你已经熟悉数字电路，可以跳到第 3 节。

**什么是触发器（Flip-Flop）。** 在组合逻辑里，`assign y = a & b;` 的输出 `y` 会随着输入立即变化，没有记忆。而数字信号处理需要「记忆」——比如累加器要记住上一次的和，延迟线要记住历史样本。触发器就是一块带记忆的电路：它在时钟的某个边沿（本库统一用上升沿）把输入「锁存」进来，并在下一个边沿到来前一直保持这个值。DRL 里所有需要记忆的地方，最终都落在一个叫 `dff`（D Flip-Flop，D 触发器）的原语上。

**什么是「时序约定」。** 一个真实的触发器还要回答两个工程问题：刚上电时输出是什么？是不是每个时钟沿都要更新？DRL 给出全库统一的回答——刚上电（或复位）时输出清零；只有当「使能」信号有效时才在时钟沿采样新值，否则保持不变。这套约定在 [u1-l4](u1-l4-coding-style-and-interface.md) 已经定下，本讲看它如何落成具体代码。

**位宽参数化。** DSP 模块的数据位宽（8 位、12 位、16 位……）因应用而异。DRL 用一个参数 `gp_data_width` 让同一个 `dff` 既能存 8 位也能存 16 位，调用时再决定具体宽度。这正是 [u1-l3](u1-l3-toolchain-and-build-flow.md) 讲的「现场生成」思想在 RTL 层的体现。

## 3. 本讲源码地图

本讲主要精读一个文件，并用另外三个文件佐证「`dff` 被全库复用」这一结论。

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| `.drl_src_code/filt_cicd/rtl/dff.v` | 全库唯一的寄存器原语，逐行精读对象 | 4.1 / 4.2 节精读 |
| `.drl_src_code/filt_cicd/rtl/shift_register.v` | 用 `generate` 把多个 `dff` 串成延迟线 | 4.3 节：延迟线复用 |
| `.drl_src_code/filt_cicd/rtl/filt_cicd.v` | CIC 抽取滤波器，用 `dff` 当累加器和采样保持 | 4.3 节：累加 / 采样保持复用 |
| `.drl_src_code/filt_ppd/rtl/commutator.v` | 多相换向器，把环形计数器的位当 `dff` 的时钟 | 4.3 节：时钟门控捕获复用 |

> 说明：`dff.v` 在仓库里一共有 5 份拷贝（`filt_cicd / filt_cici / filt_fir / filt_ppd / filt_ppi` 各一份）。用 `diff` 逐对比对，5 份内容**完全一致**。DRL 采用「每个模块自带一份 `dff.v`」而非全局共享，是为了让任意单个模块都能被独立例化、独立仿真，符合 mix-and-match 的混搭理念。

## 4. 核心概念与源码讲解

### 4.1 dff 参数与端口

#### 4.1.1 概念说明

`dff` 是 DRL 里**最小的、有记忆的**电路单元。你可以把它理解成一个「受控的、带清零的单拍延迟」：

- 它有 1 个参数：数据位宽 `gp_data_width`。
- 它有 3 个控制端口：复位 `i_rst_an`、使能 `i_ena`、时钟 `i_clk`。
- 它有 1 个数据输入 `i_data` 和 1 个数据输出 `o_data`，位宽都等于 `gp_data_width`。

从信号处理的角度，使能状态下的 `dff` 就是单位延迟算子 \(z^{-1}\)：输出等于「上一个时钟周期的输入」。

#### 4.1.2 核心流程

使能状态下，`dff` 的输入输出关系可以写成（\(n\) 表示第 \(n\) 个时钟周期）：

\[
\text{enabled:}\quad o[n] = i[n-1] \quad\Longleftrightarrow\quad O(z) = z^{-1}\,I(z)
\]

复位时，输出被强制清零，与输入无关：

\[
\text{reset:}\quad o[n] = 0
\]

端口与参数的语义如下表：

| 名称 | 方向/类别 | 含义 |
| --- | --- | --- |
| `gp_data_width` | 参数 | 输入输出位宽，默认 8，调用时可覆盖 |
| `i_rst_an` | 输入 | 异步、低有效复位（0 表示复位） |
| `i_ena` | 输入 | 同步、高有效使能（1 表示本拍采样） |
| `i_clk` | 输入 | 时钟，仅用上升沿 |
| `i_data` | 输入 | 数据输入，`gp_data_width` 位，有符号或无符号均可 |
| `o_data` | 输出 | 数据输出，`gp_data_width` 位，`i_data` 的延迟一拍版本 |

#### 4.1.3 源码精读

先看模块声明与端口列表（[dff.v:L6-L15](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L6-L15)）。这一段把「参数 + 五端口」的契约白纸黑字写死：

```verilog
module dff #(
  parameter gp_data_width = 8                  // Set input & output bit-width
)
(
  input  wire                     i_rst_an,    // Asynchronous active low reset
  input  wire                     i_ena,       // Synchronous active high enable
  input  wire                     i_clk,       // Rising-edge clock
  input  wire [gp_data_width-1:0] i_data,      // ... signed or unsigned
  output wire [gp_data_width-1:0] o_data       // ... signed or unsigned
);
```

注意两点。第一，端口顺序在**全库固定**为 `i_rst_an, i_ena, i_clk, i_data, o_data`——你后面看到的每一次 `dff` 例化都按这个顺序连线，这是 [u1-l4](u1-l4-coding-style-and-interface.md) 讲的「统一接口约定」的具体落地。第二，`i_data/o_data` 的注释写的是「signed or unsigned」——`dff` 本身不关心数据有没有符号，它只搬运比特；符号性由上层模块用 `$signed()` 决定（详见 [u2-l1](u2-l1-fixed-point-bitwidth.md)）。

接着看内部存储声明（[dff.v:L17](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L17)）：

```verilog
reg [gp_data_width-1:0] r_data;
```

真正「记住」数值的是这个 `reg` 型变量 `r_data`。按 [u1-l4](u1-l4-coding-style-and-interface.md) 的命名前缀，`r_` 表示「有记忆的寄存器值」。注意它**不是**输出端口本身——输出端口 `o_data` 是 `wire` 型，二者通过下面这行 `assign` 桥接（[dff.v:L27](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L27)）：

```verilog
assign o_data = r_data;
```

这正是 [u1-l4](u1-l4-coding-style-and-interface.md) 强调的一条规则：**输出端口永远声明为 `wire`，内部用 `reg` 存值，再用 `assign` 把 `reg` 连到输出 `wire` 上**。这样做的好处是「是否带记忆（`r_`/`w_`）」和「如何被驱动（`reg`/`wire` 关键字）」两件事解耦，全库风格一致。

#### 4.1.4 代码实践

**实践目标：** 验证 `gp_data_width` 能让同一个 `dff` 适配不同位宽。

**操作步骤：**

1. 写一个最小的顶层 `dff_wrap`，例化一个 `dff`，故意把 `gp_data_width` 改成 4。
2. 写一个最小测试平台，复位后喂入 `4'b1010`，观察一拍后输出。

```verilog
// 示例代码：4 位版 dff 包装
module dff_wrap (input i_rst_an, i_ena, i_clk, input [3:0] i_data, output [3:0] o_data);
  dff #(.gp_data_width(4)) u_dff (
    .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk), .i_data(i_data), .o_data(o_data)
  );
endmodule
```

**需要观察的现象：** 复位期间 `o_data` 为 `0`；释放复位且 `i_ena=1` 后的第一个上升沿，`o_data` 变成上一拍的 `i_data`（即延迟一拍）。

**预期结果：** 在 `i_data=4'b1010` 且 `i_ena=1` 的情况下，`o_data` 在「下一个」上升沿才出现 `1010`，体现了 \(z^{-1}\) 关系。

**待本地验证：** 上述现象需在你本机的仿真器（如 `iverilog`）中确认；本讲不假定已经运行。

#### 4.1.5 小练习与答案

**练习 1：** 如果上层需要存一个 16 位的有符号数，`dff` 的例化代码要改哪里？为什么 `dff` 内部不需要关心「有符号」？

> **答案：** 把例化参数改为 `.gp_data_width(16)` 即可。`dff` 只做按位搬运和延迟，不参与算术运算，所以无需区分有符号/无符号；符号性由上层用 `$signed()` 在做加减乘时声明。

**练习 2：** 为什么 `o_data` 要声明成 `wire` 而不能直接声明成 `reg`？

> **答案：** 因为 `o_data` 由 `assign o_data = r_data;` 这种连续赋值驱动，连续赋值只能驱动 `wire`。真正带记忆的存储是内部的 `r_data`（`reg`），`assign` 只是把它的值「镜像」到输出端口上。

---

### 4.2 异步复位时序块

#### 4.2.1 概念说明

`dff` 的全部行为浓缩在一个 `always` 块里。这个块要同时实现三件事，且有严格的**优先级**：

1. **异步复位**：只要 `i_rst_an` 变 0，输出立刻清零，**不等时钟沿**。
2. **同步使能采样**：时钟上升沿到来时，若 `i_ena` 为 1，才把 `i_data` 锁进 `r_data`。
3. **保持**：既不复位、又不使能时，`r_data` 原值不动。

「异步」与「同步」的区别是本节重点：异步意味着复位动作**进入敏感列表**，一旦发生就立即生效；同步意味着使能只在时钟沿被检查，是「时钟驱动的」。

#### 4.2.2 核心流程

把 `dff` 在一个时钟沿的行为整理成优先级真值表：

| `i_rst_an` | `i_ena`（在上升沿） | 上升沿后 `r_data` |
| :---: | :---: | :--- |
| 0 | × | `0`（异步，无需时钟） |
| 1 | 1 | `i_data`（同步采样） |
| 1 | 0 | 保持不变 |

伪代码描述这个时序块：

```
敏感于: 时钟上升沿 或 复位下降沿      // 两个事件都唤醒本块
  if (复位有效):
      r_data ← 0                    // 复位优先级最高，且与时钟无关
  else if (使能有效):
      r_data ← i_data               // 同步采样
  // 否则什么都不写 → 隐式保持
```

关键细节：把 `negedge i_rst_an`（复位下降沿）写进 `always @(...)` 的敏感列表，是 Verilog 表达「异步复位」的标准手法——它告诉综合器「这个信号不需要等时钟就能改变状态」。

#### 4.2.3 源码精读

整个时序块只有 7 行（[dff.v:L19-L25](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v#L19-L25)）：

```verilog
always @(posedge i_clk or negedge i_rst_an)
  begin: p_dff
    if (!i_rst_an)
      r_data <= 'd0;
    else if (i_ena)
      r_data <= i_data;
  end
```

逐元素拆解：

- **敏感列表 `posedge i_clk or negedge i_rst_an`**：时钟用上升沿，复位用下降沿。两者并列在列表里，意味着「时钟上升沿」或「复位下降沿」任意一个发生都会触发本块。这就是「异步复位」的语法表达——复位不必与时钟同步。
- **块名 `begin: p_dff`**：`p_` 前缀是 [u1-l4](u1-l4-coding-style-and-interface.md) 约定的「过程块标签」。给块命名便于仿真器定位，也是全库统一风格。
- **`if (!i_rst_an)` 在最外层**：保证复位优先级最高。`i_rst_an` 低有效，所以取反 `!` 表示「复位生效时」。
- **`r_data <= 'd0;`**：复位值是 0。`'d0` 是 Verilog-2001 的写法，表示十进制 0，会被自动零扩展到 `gp_data_width` 位宽。
- **`else if (i_ena)`**：复位无效时，再看使能。使能为 1 才采样 `i_data`。
- **没有 `else`**：当复位无效、使能也为 0 时，代码里没有任何赋值语句。在 `always` 时序块里，不给 `r_data` 赋值就意味着**保持原值**——这正是「保持」语义的来源，无需额外写 `r_data <= r_data;`。
- **非阻塞赋值 `<=`**：时序块必须用非阻塞赋值，保证多个 `dff` 在同一时钟沿更新时行为可预测。这是 RTL 编码的基本功。

> 小结：这 7 行就是 DRL 全库**所有时序逻辑的模板**。你在 `shift_register`、`filt_cicd`、`commutator` 里看到的每一个「记住上一次值」的需求，本质上都是在反复使用这个模板——要么直接例化 `dff`，要么照抄这个 `always` 结构。

#### 4.2.4 代码实践

**实践目标：** 用一个小测试平台，肉眼区分「异步复位」和「同步使能」两种行为。

**操作步骤：**

1. 例化一个 `dff`（位宽随意，比如 8）。
2. 让时钟持续翻转。先拉低 `i_rst_an` 一段时间，观察输出；再释放复位，但让 `i_ena=0`，观察输出；最后置 `i_ena=1`，喂入数据。
3. 重点观察：在 `i_rst_an` 拉低的**那个瞬间**（而不是下一个时钟沿），输出是否已经变成 0。

**需要观察的现象：**

- 复位拉低瞬间（即使当时没有时钟沿），`o_data` 立即归零 → 体现**异步**。
- 复位释放后但 `i_ena=0` 期间，喂入任何 `i_data` 输出都不变 → 体现**同步使能**。
- `i_ena=1` 后，输出在**下一个上升沿**才跟随 `i_data` → 体现**延迟一拍**。

**预期结果：** 复位归零发生在复位信号的下降沿时刻，而非时钟沿；使能采样发生在时钟上升沿。两者的时间点不同，正是「异步 vs 同步」的可观察差异。

**待本地验证：** 具体波形需在本地仿真器中确认。

#### 4.2.5 小练习与答案

**练习 1：** 如果把敏感列表里的 `or negedge i_rst_an` 删掉，只留 `always @(posedge i_clk)`，`dff` 的复位行为会变成什么？

> **答案：** 复位将变成**同步**的——只有当时钟上升沿到来、且 `i_rst_an` 仍为 0 时，`r_data` 才会清零。复位不再瞬时生效，而是要等到下一个时钟沿。这正是「异步」与「同步」复位的本质差别由敏感列表决定。

**练习 2：** 为什么 `r_data <= i_data;` 用的是非阻塞赋值 `<=` 而不是阻塞赋值 `=`？

> **答案：** 时序逻辑（`always @(posedge clk)`）里必须用非阻塞赋值，它保证本块右值在块末尾统一更新，从而当多个 `dff` 串联（如移位寄存器）时，每个触发器读到的是「更新前」的邻居值，行为与真实硬件一致。若用阻塞赋值 `=`，仿真顺序会改变结果。

---

### 4.3 作为全库原子构建块

#### 4.3.1 概念说明

前两节我们把 `dff` 当成一个独立的触发器来读。但 `dff` 在 DRL 里真正的价值在于：**它是被反复复用的原子积木**。所有的延迟线、累加器、采样保持器、换向器，剥到最后都是若干个 `dff` 的不同连线方式。

回顾 4.1.2，使能状态下的 `dff` 就是 \(z^{-1}\)。而 DSP 信号流图本身就是由 \(z^{-1}\)（单位延迟）、加法器、乘法器拼出来的。所以下面你会看到：同一个 `dff` 原语，仅仅因为 `i_clk` 和 `i_ena` 接的东西不同，就扮演了完全不同的 DSP 角色。

#### 4.3.2 核心流程

下表汇总了 `dff` 在全库的几种典型复用方式。注意「时钟接什么」和「使能接什么」两列——这正是同一个原语变形为不同功能的关键。

| 复用场景 | 出现位置 | `i_clk` 接什么 | `i_ena` 接什么 | DSP 角色 |
| --- | --- | --- | --- | --- |
| 串联延迟线 | `shift_register.v` | 主时钟 `i_clk` | 主使能 `i_ena` | 多级串联的 \(z^{-1}\) |
| 累加器反馈 | `filt_cicd.v` 积分级 | 主时钟 `i_clk` | 主使能 `i_ena` | 累加和的保持寄存器 |
| 抽取采样保持 | `filt_cicd.v` 下采样级 | 主时钟 `i_clk` | 相位脉冲 `w_sclk` | 按相位捕获并保持 |
| 换向器相位捕获 | `commutator.v` 捕获级 | 环形计数器某位 `r_ring_cnt[x]` | 主使能 `i_ena` | 用计数器位当「时钟」逐相捕获 |
| 寄存输出打包 | `commutator.v` 输出级 | 完成脉冲 `r_done` | 主使能 `i_ena` | 在完成时刻把并行数据打包输出 |

读这张表的方法：**固定 `i_clk=i_clk, i_ena=i_ena` 时，`dff` 就是最普通的「每拍延迟一拍」**；一旦把 `i_clk` 或 `i_ena` 换成某个脉冲信号，`dff` 就变成了「只在特定时刻才更新」的采样保持器。这就是同一个原语产生千变万化功能的全部秘密。

#### 4.3.3 源码精读

**复用方式一：串联延迟线。** `shift_register.v` 用 `generate for` 循环把 `gp_nr_stages` 个 `dff` 串起来（[shift_register.v:L28-L56](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L28-L56)）。第 0 级把外部 `i_data` 接进来（[shift_register.v:L33-L41](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L33-L41)）：

```verilog
dff #(.gp_data_width(gp_data_width)) REG_COMMUTATOR_INP_DATA (
  .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
  .i_data(i_data),
  .o_data(w_data[(i+1)*gp_data_width-1 -: gp_data_width])
);
```

后续每一级把上一级的 `o_data` 当作自己的 `i_data`（[shift_register.v:L45-L53](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L45-L53)）。于是 N 级串联就是 N 个 \(z^{-1}\) 首尾相接，即 \(z^{-N}\)。所有 `dff` 的 `i_clk` 和 `i_ena` 都接主信号——这是最朴素的「移位寄存器」。本讲的综合实践（第 5 节）就是要你手写一个它的简化版。

**复用方式二：累加器反馈寄存器。** 在 `filt_cicd.v` 的积分器级里，`dff` 的 `i_data` 接的是「当前输入 + 自身上一拍输出」的和，从而构成累加器（[filt_cicd.v:L71-L79](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L71-L79)）：

```verilog
assign w_int_add[...] = $signed(w_data) + $signed(r_int_dly[...]);
dff #(.gp_data_width(gp_oup_width)) CIC_INT_DLY (
  .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
  .i_data(w_int_add[...]), .o_data(r_int_dly[...])
);
```

这里 `dff` 的角色是「记住累加和」，把它反馈到加法器输入端。同一个 `dff`，端口接法不变，但因为外围有加法器反馈，它就成了累加器。`i_clk`、`i_ena` 仍接主信号——所以积分器每个使能拍都在累加。

**复用方式三：抽取采样保持。** 紧接着的「下采样」级只改了一个端口：把 `i_ena` 从 `i_ena` 换成了相位脉冲 `w_sclk`（[filt_cicd.v:L100-L109](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L100-L109)）：

```verilog
assign w_sclk = r_count[gp_phase];
dff #(.gp_data_width(gp_oup_width)) cicd_downsample (
  .i_rst_an(i_rst_an), .i_ena(w_sclk), .i_clk(i_clk),   // 注意 i_ena 接 w_sclk
  .i_data(w_int_add[gp_order*gp_oup_width-1 -: gp_oup_width]),
  .o_data(r_comb_inp)
);
```

`w_sclk` 来自环形计数器的某一位，每 N 拍才为 1。于是这个 `dff` 只在「正确的相位」那一拍采样，其余拍保持——这正是抽取器「按相位挑样本」的需求。**同一个 `dff`，仅仅改了 `i_ena` 接什么，就从「每拍延迟」变成了「按相位采样保持」。**

**复用方式四：换向器相位捕获（最 exotic）。** 在多相换向器 `commutator.v` 里，`i_clk` 被直接接到了环形计数器的某一位 `r_ring_cnt[x-1]` 上（[commutator.v:L85-L96](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L85-L96)）：

```verilog
for (x=gp_decimation_factor; x>0; x=x-1) begin: g_reg_comm_inp
  dff #(.gp_data_width(gp_idata_width)) REG_COMMUTATOR_INP_DATA (
    .i_rst_an(i_rst_an), .i_ena(i_ena),
    .i_clk(r_ring_cnt[x-1]),    // 用环形计数器某一位当「时钟」
    .i_data(d_data),
    .o_data(w_data[x*gp_idata_width-1 -: gp_idata_width])
  );
end
```

环形计数器一次只有一位为 1，且每一位轮流变 1。把它当 `i_clk`，意味着每个 `dff` 各自在「属于自己的那一拍」捕获输入，从而把串行输入数据按相位分拣到并行输出字的不同切片里。紧接着的输出级再把 `i_clk` 接到完成脉冲 `r_done` 上，把并行数据一次性打包输出（[commutator.v:L98-L112](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L98-L112)）。

> 读到这里你应该建立这样一个心智模型：**`dff` 是一个「条件触发的存储单元」，触发条件由 `i_clk` 和 `i_ena` 两个端口的外部连线决定**。改这两个端口的接法，就能让同一块积木变成延迟线、累加器、采样器或并行分拣器。这就是 DRL 用一个 30 行的原语撑起整套 DSP 模块库的设计哲学。

#### 4.3.4 代码实践

**实践目标：** 通过阅读源码，把 `dff` 的不同复用方式与 4.3.2 的表对上号（源码阅读型实践）。

**操作步骤：**

1. 打开 `filt_cicd.v`，找到积分器段（`L65-L95`）和下采样段（`L97-L109`）。
2. 对每一处 `dff` 例化，抄下它的 `.i_clk(...)` 和 `.i_ena(...)` 各接了什么信号。
3. 打开 `commutator.v`，找到捕获段（`L85-L96`）和输出段（`L98-L112`），做同样的事。

**需要观察的现象：** 你会发现四处 `dff` 例化的差别**只**在 `i_clk`/`i_ena` 的连线上，模块本体一字未改。

**预期结果：** 填出一张与 4.3.2 表格一致的「端口接线表」，从而确认「同一原语、不同连线 = 不同功能」。

**待本地验证：** 此为阅读型实践，无需运行；若想进一步确认，可在第 5 节的综合实践里仿照 4.3.3 的方式改接 `i_ena`，观察行为变化。

#### 4.3.5 小练习与答案

**练习 1：** `shift_register.v` 里的 N 个 `dff` 都把 `i_clk` 和 `i_ena` 接成主信号，整体相当于 DSP 里的什么算子？

> **答案：** 相当于 \(z^{-N}\)（N 级单位延迟串联）。因为每一级是 \(z^{-1}\)，N 级首尾相接就是 \(z^{-N}\)，即输入样本要经过 N 拍才出现在末端输出。

**练习 2：** 为什么 `commutator.v` 敢把一个普通寄存器位 `r_ring_cnt[x-1]` 当作 `i_clk` 来用？这种用法对 `dff` 本身有什么前提要求？

> **答案：** 因为 `dff` 的 `always` 块对 `i_clk` 的要求只是「上升沿触发」，并不限定 `i_clk` 必须是周期性时钟。`r_ring_cnt[x-1]` 从 0 变 1 时也会产生上升沿，于是触发该 `dff` 采样。前提是 `dff` 内部没有假定 `i_clk` 是理想周期时钟（确实没有），所以可以这么「挪用」。这是把同一个原语玩出花来的典型技巧。

---

## 5. 综合实践：用 dff 原语搭建 3 级串行移位寄存器

本实践的目的是把第 4 节的三个知识点（端口契约、时序块、作为构建块）串起来：你不许用现成的 `shift_register`，只能用 `dff` 原语手搓一个 3 级延迟线，并用 `iverilog` 编译通过。

### 5.1 实践目标

- 复用 `dff` 的统一端口顺序，正确级联 3 个实例。
- 观察一个输入样本「逐级移动」3 拍后才到达末级的过程，亲手验证 \(z^{-3}\)。
- 用 `iverilog` 编译并仿真，确认能跑通。

### 5.2 顶层模块（示例代码）

把前一级的 `o_data` 接到下一级的 `i_data`，三级首尾相接，并额外把每一级的输出都引出来方便观察：

```verilog
// 示例代码：用 dff 原语搭建的 3 级串行移位寄存器
module shift3 #(
  parameter gp_data_width = 8
) (
  input  wire                     i_rst_an,
  input  wire                     i_ena,
  input  wire                     i_clk,
  input  wire [gp_data_width-1:0] i_data,
  output wire [gp_data_width-1:0] o_d1,   // 第 1 级输出
  output wire [gp_data_width-1:0] o_d2,   // 第 2 级输出
  output wire [gp_data_width-1:0] o_d3    // 第 3 级输出（末级）
);
  // 级 1：外部输入 -> d1
  dff #(.gp_data_width(gp_data_width)) u_d1 (
    .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
    .i_data(i_data), .o_data(o_d1));

  // 级 2：d1 -> d2
  dff #(.gp_data_width(gp_data_width)) u_d2 (
    .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
    .i_data(o_d1), .o_data(o_d2));

  // 级 3：d2 -> d3
  dff #(.gp_data_width(gp_data_width)) u_d3 (
    .i_rst_an(i_rst_an), .i_ena(i_ena), .i_clk(i_clk),
    .i_data(o_d2), .o_data(o_d3));
endmodule
```

注意：`o_d1/o_d2` 虽然是 `output wire`，但它们同时也作为下一级 `dff` 的 `i_data` 输入——一根 `wire` 既可以被某个实例驱动、又可以扇出给其它实例当输入，这是合法且常见的连法。

### 5.3 最小测试平台（示例代码）

为了让结果可观察，写一个时钟发生器 + 一段受控的复位与喂值时序，并在每个上升沿后打印三级输出：

```verilog
// 示例代码：3 级移位寄存器的最小测试平台
`timescale 1ns/1ps
module tb_shift3;
  reg         clk;
  reg         rst_an;
  reg         ena;
  reg  [7:0]  data;
  wire [7:0]  d1, d2, d3;

  shift3 #(.gp_data_width(8)) dut (
    .i_rst_an(rst_an), .i_ena(ena), .i_clk(clk),
    .i_data(data), .o_d1(d1), .o_d2(d2), .o_d3(d3));

  always #5 clk = ~clk;                  // 100 MHz，周期 10ns

  initial begin
    clk = 0; rst_an = 0; ena = 1; data = 8'hA1;  // 复位期间先备好第 1 个值
    #20 rst_an = 1;                              // 释放异步复位
    #10 data = 8'hB2;                            // 喂第 2 个值
    #10 data = 8'hC3;                            // 喂第 3 个值
    #10 data = 8'h00;                            // 喂 0，观察移位
    #30 $finish;
  end

  // 每个上升沿之后 1ns 打印（确保读到的是更新后的值）
  always @(posedge clk) #1 $display(
    "t=%0t  i_data=%h  d1=%h d2=%h d3=%h", $time, data, d1, d2, d3);
endmodule
```

### 5.4 编译与运行命令

把 `dff.v`、上面的 `shift3` 顶层和测试台存为三个文件后执行：

```bash
iverilog -g2012 -o shift3.vvp dff.v shift3.v tb_shift3.v
vvp shift3.vvp
```

> 说明：`-g2012` 启用 SystemVerilog 语法超集（DRL 的测试台普遍用 SV），对本实践的纯 Verilog 代码完全兼容。若你的环境只有旧版 iverilog，改用 `-g2001` 也可（`dff`/`shift3` 只用到 Verilog-2001 特性）。

### 5.5 预期结果（根据 \(z^{-1}\) 语义推导）

按时序逐拍分析（复位在 `t=20` 释放；上升沿发生在 `t=5,15,25,35,…`）：

| 上升沿时刻 | `i_data` | `d1` | `d2` | `d3` | 说明 |
| :---: | :---: | :---: | :---: | :---: | :--- |
| t=5, 15 | — | 00 | 00 | 00 | 复位有效，输出保持 0 |
| t=25 | A1 | **A1** | 00 | 00 | A1 首次被采样进 d1 |
| t=35 | B2 | **B2** | **A1** | 00 | A1 移到 d2 |
| t=45 | C3 | **C3** | **B2** | **A1** | A1 移到 d3（历经 3 拍） |
| t=55 | 00 | **00** | **C3** | **B2** | 数据继续逐级下移 |
| t=65 | 00 | 00 | 00 | **C3** | C3 也到达 d3 |

关键现象：在 `t=0` 就准备好的 `A1`，先在 `t=25` 进入 `d1`，再花 2 拍依次到 `d2`、`d3`，于 `t=45` 出现在末级——正好 3 个使能拍，对应 \(z^{-3}\)。

**待本地验证：** 上述各寄存器值是根据 `dff` 的 \(z^{-1}\) 语义解析推导的；你本机 `vvp` 的逐行文本输出格式（时间戳单位、十六进制宽度）可能略有差异，且需要确认 `iverilog` 是否已安装。请以本地实际运行为准。

### 5.6 进阶（可选）

仿照 4.3.3 的「采样保持」复用方式，把 `shift3` 的 `i_ena` 从常驻 `1` 改成「每 2 拍才为 1」的脉冲（例如用一个翻转寄存器生成），观察数据移动速度变成原来的一半——这就是把 `dff` 从「延迟线」改造成「降速采样器」的最小实验。

## 6. 本讲小结

- `dff` 是 DRL 全库**唯一**的寄存器原语，5 份拷贝完全一致；端口顺序固定为 `i_rst_an, i_ena, i_clk, i_data, o_data`。
- 它用一个参数 `gp_data_width` 适配任意位宽，数据符号性由上层决定，自身只搬运比特。
- 时序块 `always @(posedge i_clk or negedge i_rst_an)` 用「复位进敏感列表」实现**异步低有效复位**，优先级最高；使能采样在时钟沿同步进行，无 `else` 即「保持」。
- 内部用 `reg r_data` 存值，再用 `assign o_data = r_data;` 桥接到 `wire` 输出端口——这是全库「记忆性与驱动方式解耦」的范本。
- 使能态下的 `dff` 等价于单位延迟 \(z^{-1}\)；改接 `i_clk`/`i_ena` 就能让它变形为延迟线、累加器、采样保持器或换向捕获器，这就是「一个原语撑起整套库」的设计哲学。

## 7. 下一步学习建议

- 下一讲 [u2-l3 shift_register 与 upsample 原语](u2-l3-shift-register-and-upsample.md) 会把本讲的 `dff` 串成本节提到的 `shift_register`，并引入另一个原语 `upsample`，讲清两者在 CIC 与多相模块里的复用。建议先做完第 5 节的综合实践再读，体会会更深。
- 想看 `dff` 在真实模块里的累加用法，可直接翻 [filt_cicd.v 的积分器段](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L65-L95)，为 [u4 CIC 滤波器](u4-l1-cicd-decimation.md) 单元预热。
- 如果对位宽自动推导还不太熟，回头补 [u2-l1 Verilog 定点数与位宽推导](u2-l1-fixed-point-bitwidth.md)，理解 `$clog2` 如何让 `gp_oup_width` 等派生参数随 `gp_data_width` 自动算出。
