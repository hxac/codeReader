# 乘法与除法单元

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `OPT_MPY` 这个综合期参数如何在 `mpyop` 内部用 `generate if` 选出 6 种不同的乘法实现（不做 / 单周期 / 双周期 / 三周期 / 四周期 FOIL / 逐位慢乘），并理解它们各自的面积与时钟周期取舍。
- 解释 `slowmpy` 在没有 DSP 资源时如何用「移位累加」逐位完成乘法，以及它的符号修正思路。
- 描述 `div` 除法单元基于「恢复余数法 / 移位相减」的状态机：`pre_sign` 取绝对值 → 32 个比特位的循环 → 必要时对结果取反，并估算一次 32 位除法大致需要多少个时钟周期。
- 理解乘除单元在流水线中的位置：它们都挂在第 4 级（执行级），运行期间会让 ALU、访存、FPU 都停下来等它，所以属于「多周期指令」。

本讲承接 u3-l4（ALU `cpuops`）。上一讲我们留下两个线索：乘法并不直接做在 ALU 里，而是经子模块 `mpyop` 多周期完成；除法则完全绕开 ALU，走独立的 `div` 模块。本讲就把这两个「执行级里的慢兄弟」拆开讲透。

## 2. 前置知识

在进入源码前，先用一段话把三个直觉建立起来。

**为什么乘除要单独成模块？** 一个 32 位加法器可以在一个时钟周期内算完，频率也压得下去；但一个 32×32 的硬件乘法器组合逻辑很深，直接塞进 ALU 的单周期数据通路会把主频拖垮。除法更极端——它本质上是一个「逐位试商」的迭代过程，根本不可能一个周期做完。所以 ZipCPU 的做法是：**把乘法和除法从 ALU 的关键路径里剥离出来，给它们各自一个可以占用多个时钟周期的执行单元**，用 `o_busy`/`o_valid` 这套握手协议告诉流水线「我还在算，先别往后走」。

**什么是「综合期参数」？** 这是贯穿整个 ZipCPU 的核心思想（u3-l1 已讲过）。`OPT_MPY`、`OPT_DIV` 这类参数不是运行时开关，而是 **综合（synthesis）时的剪刀**：`generate if (OPT_MPY == 1)` 这种结构在综合工具眼里就是「条件编译」，不满足条件的分支根本不会生成电路。所以同一份 `mpyop.v` 源码，根据参数不同会被裁剪成完全不同的一块硬件。

**两个关键算法名词：**
- **移位累加乘法（shift-and-add multiply）**：把乘数按二进制逐位扫描，某一位是 1 就把被乘数（左移到对应位权后）累加进结果。32 位的乘数要扫 32 次，所以慢但省逻辑。
- **恢复余数法 / 移位相减除法（restoring division）**：模拟手工长除法。把被除数逐位移入一个「部分余数」寄存器，每一位都试探「部分余数够不够减去除数」：够减则商位为 1 并真的减掉，不够则商位为 0。32 位商要循环 32 次。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/core/mpyop.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v) | 乘法单元。用一个 `generate if` 阶梯按 `OPT_MPY` 选出 6 种实现；最后一种会调用 `slowmpy`。 |
| [rtl/core/slowmpy.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v) | 逐位移位累加乘法器，参数化的位宽（默认 33 位），供无 DSP 时回退使用。 |
| [rtl/core/div.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v) | 整数除法单元，支持有符号/无符号，含除零检测与标志位输出。 |
| [rtl/core/cpuops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v) | ALU。内部实例化 `mpyop`，把乘法结果通过 case 语句汇入 ALU 输出；并在多周期乘法期间向流水线报告 `o_busy`。 |
| [rtl/core/zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v) | CPU 内核。在第 4 级实例化 `cpuops`（含乘法）与 `div`（除法），并用 `op_valid_div` / `div_busy` 控制停顿。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。`Multiply Operations` 与 `Divide Unit` 两节是权威说明；`OPT_MULTIPLY` 即 RTL 里的 `OPT_MPY`。 |
| [sim/verilator/div_tb.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp) | 除法单元的独立组件测试台，可用来实测除法周期数。 |

---

## 4. 核心概念与源码讲解

### 4.1 mpyop：多种乘法实现的综合期选择器

#### 4.1.1 概念说明

ZipCPU 有三条乘法指令：`MPY`（取 64 位积的低 32 位）、`MPYUHI`（无符号积的高 32 位）、`MPYSHI`（有符号积的高 32 位）。规范在 [spec.tex:907-913](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L907-L913) 说得很清楚。

但「怎么算这个乘积」取决于你的目标 FPGA 上有什么资源：

- 有完整的 32×32 DSP 乘法块 → 一个周期就能算完。
- 只有更小的 DSP 块 → 拆成几个小乘法拼起来，多花几个周期。
- 完全没有 DSP → 用逻辑门做逐位移位累加，要几十个周期。

`mpyop` 的全部意义就是：**用一个模块 + 一个参数 `OPT_MPY`，把上面所有情况都覆盖掉**，对外暴露同一套端口（`i_a`、`i_b`、`i_op`、`o_result`、`o_busy`、`o_valid`、`o_hi`），调用方（`cpuops`）完全不用关心内部选了哪种实现。

#### 4.1.2 核心流程

`mpyop` 顶层端口与 `OPT_MPY` 参数定义在 [mpyop.v:43-75](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L43-L75)。参数注释把 6 种取值的含义列得明明白白：

```
OPT_MPY 取值    含义
   0        不支持乘法
   1        单周期乘法，时序与 ADD 相同
   2        两周期乘法
   3        三周期乘法，标准 Xilinx DSP 时序
   4        三周期乘法，Xilinx Spartan DSP 时序
   其它     低逻辑慢速乘法（走 slowmpy）
```

> 说明：源码里 `OPT_MPY==4` 的分支注释写的是「The four clock option, polynomial multiplication」，用 FOIL（首项-外项-内项-末项）二项式展开做 4 拍乘法，而 `OPT_MPY==3` 注释写的是「three clock」。spec.tex 的 [Tbl. opt-multiply, spec.tex:924-954](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L924-L954) 与之一致。两处对「3/4」的命名略有出入（参数注释把 4 也叫 three clock），以 spec 的表格为准：3 拍＝输入输出都寄存的 DSP 工作horse；4 拍＝FOIL 分拆给只能做 16×16 的 DSP。

整个模块是一个巨大的 `generate` 块，结构是一串 `if/else if` 阶梯：

```
generate
  if (OPT_MPY == 0)        : MPYNONE   // 不生成乘法硬件
  else if (OPT_MPY == 1)   : MPY1CK    // 单周期，纯组合 *
  else if (OPT_MPY == 2)   : MPY2CK    // 寄存输入，2 拍
  else if (OPT_MPY == 3)   : MPY3CK    // 寄存输入+输出，3 拍
  else if (OPT_MPY == 4)   : MPY4CK    // FOIL 分拆，4 拍
  else                     : MPYSLOW   // 实例化 slowmpy，逐位慢乘
endgenerate
```

这个阶梯收尾在 [mpyop.v:429](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L429) 那一串 `end end end end end`——每一层 `if/else` 对应一个 `end`，读源码时把它当成「6 选 1 的开关」即可。

`i_op` 两位控制乘法的三种形态，定义见 [mpyop.v:64-68](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L64-L68)：

| `i_op` | 含义 | 对应指令 |
|--------|------|----------|
| `2'b00` | 32×32，取低 32 位 | `MPY` |
| `2'b10` | 32×32 无符号，取高 32 位 | `MPYUHI` |
| `2'b11` | 32×32 有符号，取高 32 位 | `MPYSHI` |

其中 `i_op[1]` 表示「取高半（hi）还是低半」，`i_op[0]` 表示「是否有符号」。有符号时需要把操作数符号扩展到 64 位再乘。

#### 4.1.3 源码精读

**单周期乘法 `MPY1CK`** —— [mpyop.v:107-129](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L107-L129)：

```verilog
assign w_mpy_a_input = {{(32){(i_a[31])&(i_op[0])}},i_a[31:0]};
assign w_mpy_b_input = {{(32){(i_b[31])&(i_op[0])}},i_b[31:0]};
assign o_result = (OPT_LOWPOWER && !i_stb) ? 0 : (w_mpy_a_input * w_mpy_b_input);
assign o_busy  = 1'b0;
assign o_valid = i_stb;
```

这就是一个纯组合的 `*`：当 `i_op[0]=1`（有符号）时把符号位复制 32 份做符号扩展，否则高位补 0。`o_busy` 恒为 0、`o_valid` 直接等于 `i_stb`——也就是说**乘法当拍就算完，对流水线没有任何停顿**，时序和一条 ADD 一样。代价是这条组合路径很长，能否用取决于综合后的时序能否收敛（需要 FPGA 有 32×32 的 DSP 块）。

**三周期乘法 `MPY3CK`** —— [mpyop.v:179-267](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L179-L267) 演示了「多周期 + 握手」的典型写法。关键是用一个两位的 `mpypipe` 移位寄存器当流水线标尺：

```verilog
mpypipe <= { mpypipe[0], i_stb };        // 每拍把 i_stb 推进去
...
assign o_busy  = mpypipe[0];             // 还在算
assign o_valid = mpypipe[1];             // 结果出来了
```

`mpypipe[0]` 在请求后的第 1 拍置位（告诉外界 busy），`mpypipe[1]` 在第 2 拍置位（结果有效）。第 1 拍寄存输入，第 2 拍算乘法并寄存结果，第 3 拍结果可被取走——正好 3 拍。spec 称这是「most FPGA DSP implementations」的工作horse（[spec.tex:934-939](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L934-L939)）。

注意它对 Verilator 和真实综合做了区分（`ifdef VERILATOR`，[mpyop.v:222-254](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L222-L254)）：仿真时同时算一份有符号、一份无符号结果，按 `r_sgn` 选；真实综合时让综合器从同一个 DSP 推断出所需的有/无符号乘法。这是常见的「仿真求直观、综合求资源」的写法。

**FOIL 四周期乘法 `MPY4CK`** —— [mpyop.v:269-395](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L269-L395)。把 32 位数拆成两段 16 位，用二项式展开：

\[
A\cdot B = (2^{16}a_h+a_l)(2^{16}b_h+b_l) = 2^{32}(a_h b_h) + 2^{16}(a_h b_l + a_l b_h) + (a_l b_l)
\]

源码注释把这套「F-O-I-L」命名用得很形象：`pp_f`（first，\(a_h b_h\)）、`pp_oi`（outer+inner，\(a_h b_l+a_l b_h\)）、`pp_l`（last，\(a_l b_l\)）。第 2 拍算这 4 个 16×16 部分积（[mpyop.v:362-377](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L362-L377)），第 3 拍把它们加起来拼成 64 位积（[mpyop.v:381-391](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L381-L391)）。它面向「只有 16×16 DSP、拼不出 32×32」的器件。有符号时用翻转符号位的偏置技巧处理（`{(~i_a[31]),i_a[30:0]}`，[mpyop.v:300-307](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L300-L307)）。

**慢乘回退 `MPYSLOW`** —— [mpyop.v:396-429](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L396-L429)。当 `OPT_MPY` 取 0/1/2/3/4 以外的值（spec 说是「5+」，测试台用 36）时，实例化 `slowmpy`，位宽 `NA=33`（额外一位用来容纳符号修正）：

```verilog
slowmpy #(.LGNA(6), .NA(33)) slowmpyi(
    i_clk, i_reset, i_stb,
    { (i_op[0])&(i_a[31]), i_a },        // 33 位输入：符号位 + 32 位
    { (i_op[0])&(i_b[31]), i_b }, 1'b0, o_busy,
        o_valid, full_result, unused_aux);
```

这套实现完全不依赖 DSP，靠纯逻辑逐位累加，下一节展开。

**外层调用方 `cpuops`** —— 乘法并不直接被 `zipcore` 实例化，而是包在 ALU `cpuops` 里。[cpuops.v:162-174](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L162-L174) 实例化 `mpyop`，并用一个判定信号决定何时启动它：

```verilog
assign this_is_a_multiply_op = (i_stb)&&((i_op[3:1]==3'h5)||(i_op[3:0]==4'hc));
```

即 ALU 操作码 `0xa/0xb/0xc`（MPYUHI/MPYSHI/MPY）才驱动 `i_stb`。乘法结果通过 case 语句汇入 ALU 输出（[cpuops.v:196-198](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L196-L198)），并在多周期实现里由 `o_busy`/`o_valid` 报告状态（[cpuops.v:206-240](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L206-L240)）——`OPT_MPY>1` 时 `o_valid` 会被压到 `mpydone` 才拉高，从而让流水线多等几拍。

#### 4.1.4 代码实践

**实践目标**：对比 `mpyop` 不同实现的端口一致性与周期差异，直观感受「同一端口、不同周期」。

**操作步骤**：

1. 打开 [mpyop.v:43-75](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L43-L75)，记下模块对外的 6 个端口：`i_clk/i_reset/i_stb`、`i_op[1:0]`、`i_a/i_b`、`o_valid/o_busy/o_result/o_hi`。
2. 分别跳到 `MPY1CK`（[mpyop.v:107-129](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L107-L129)）与 `MPY3CK`（[mpyop.v:179-267](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/mpyop.v#L179-L267)），核对它们的端口赋值都落到同一组 `o_*` 上——你会看到无论选哪条分支，对外端口名完全相同，只是 `o_busy`/`o_valid` 的时序不同。
3. 填写下表（从源码注释和 `mpypipe` 位数推断）：

| `OPT_MPY` | 实现分支 | `o_busy` 拉高几拍 | 从 `i_stb` 到 `o_valid` 几拍 |
|-----------|----------|-------------------|------------------------------|
| 1 | MPY1CK | 0 | 1 |
| 2 | MPY2CK | 0（`mpypipe` 延迟 1） | 2 |
| 3 | MPY3CK | 1 | 3 |
| 4 | MPY4CK | 2（`|mpypipe[1:0]`） | 4 |

**需要观察的现象**：单周期分支的 `o_valid = i_stb`（当拍有效，零停顿）；多周期分支用 `mpypipe` 移位寄存器把 `o_valid` 逐拍后移。

**预期结果**：端口完全一致、周期数随 `OPT_MPY` 递增。这说明上层 `cpuops` 可以在不知情的情况下换实现——这正是 `generate` + 统一端口握手的好处。

> 待本地验证：上表周期数为依据源码 `mpypipe` 位数与注释推断；若要实测，可参考 `sim/verilator` 下 `OPT_MPY` 不同取值的组件测试台（`VMPY_TB` 宏相关，见 [cpuops.v:242-278](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L242-L278)），用 `m_tickcount` 数从 `i_stb` 到 `o_valid` 的拍数。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OPT_MPY==1` 的单周期乘法 `o_busy` 恒为 0，而 `OPT_MPY==3` 的 `o_busy` 要拉高 1 拍？

**参考答案**：单周期乘法当拍就算完、当拍就 `o_valid`，对流水线没有任何额外占用，所以不需要 busy。三周期乘法第 2、3 拍还没出结果，必须用 `o_busy` 告诉流水线「别往后送下一条依赖结果的指令」，否则会读到错误的旧值。

**练习 2**：`i_op[0]`（符号位）在 `MPY1CK` 里是如何影响计算的？

**参考答案**：它参与符号扩展 `{{(32){(i_a[31])&(i_op[0])}}, i_a[31:0]}`——`i_op[0]=1`（有符号）时把 `i_a[31]` 复制 32 份当高位，做算术扩展；`i_op[0]=0`（无符号）时高位补 0。低 32 位结果（`MPY`）对有/无符号相同，所以 `i_op[0]` 只在取高 32 位时（`MPYUHI/MPYSHI`）才真正有区别。

---

### 4.2 slowmpy：无 DSP 时的逐位乘法软件回退

#### 4.2.1 概念说明

当目标器件没有 DSP 乘法块（例如某些小型 FPGA 或 ASIC 流程），`mpyop` 会回退到 `slowmpy`。文件头一句话点明它的定位（[slowmpy.v:7-9](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L7-L9)）：

> 「designed for low logic and slow data signals. It takes one clock per bit plus two more to complete the multiply.」

即 **每比特一拍，再加 2 拍收尾**。默认 `NA=33` 位，所以约 35 拍算完一次乘法。它用很少的逻辑门换取很慢的速度，是有/无 DSP 之间的最后退路。

`slowmpy` 是参数化的（位宽 `NA`、`NB` 可配），所以它不止服务 ZipCPU 的 32 位乘法，也能被别处复用。`mpyop` 调用时传 `NA=33`（多出的一位用于容纳有符号时的修正项）。

#### 4.2.2 核心流程

算法是经典的「移位累加二进制乘法」，作者注明思路取自维基百科（[slowmpy.v:11-12](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L11-L12)）。伪代码：

```
乘数 b 的每一位从低到高扫描：
    如果该位是 1，把被乘数 a（移到当前位权）累加进部分积 partial
    partial 整体右移一位（为下一位腾位置）
处理完所有位后，对有符号结果做一次修正（鲍斯/补码修正）
```

握手状态机很简单，只有三态：**空闲 → 忙（计数 NA 拍）→ done 一拍**。

- `count` 从 `NA-1` 递减到 0，每拍减 1；
- `count==0` 时 `pre_done` 成立，下一拍 `almost_done`，再下一拍 `o_done` 拉高一拍并清 `o_busy`。

所以总周期数 \(\approx NA + 2\)。

#### 4.2.3 源码精读

**端口与参数** —— [slowmpy.v:44-68](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L44-L68)。注意 `OPT_SIGNED` 参数（默认 1）：有符号版本对累加的首尾两步做了额外修正。

**握手 FSM** —— [slowmpy.v:93-121](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L93-L121)：

```verilog
end else if (!o_busy) begin           // 空闲：接收新请求
    o_done <= 0;
    o_busy <= i_stb;
end else if (almost_done) begin       // 快算完：拉一拍 done
    o_done <= 1;
    o_busy <= 0;
end else
    o_done <= 0;
```

这是 ZipCPU 内部非常典型的「请求-忙-有效」三件套协议，和 `div`、`memops` 等模块风格一致。

**逐位累加核心** —— [slowmpy.v:123-155](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L123-L155)。关键两行：

```verilog
assign pwire = (p_b[0] ? p_a : 0);   // 当前位为1则取被乘数，否则取0
...
p_b <= (p_b >> 1);                    // 乘数每拍右移一位，露出下一位
partial[NB-2:0] <= partial[NB-1:1];   // 部分积每拍右移一位
```

`p_b[0]` 是当前扫描到的乘数位；`partial` 是双向移位的累加器。无符号时直接 `partial + pwire`（[slowmpy.v:150-152](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L150-L152)）；有符号时第一步和最后一步改用「取反加一」式的补码修正（[slowmpy.v:144-149](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L144-L149)），最终在 `o_p` 上再加一个补偿常数（[slowmpy.v:159-168](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L159-L168)）。这正是补码乘法的标准处理：把负数当成「先按无符号算，再修正」。

**形式化属性** —— 文件后半段（[slowmpy.v:179-273](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L179-L273)，`ifdef FORMAL`）用断言检查「结果的符号位等于两个输入符号位的异或」等性质。这部分不是功能逻辑，而是给 SymbiYosys 用的证明约束（u5-l2 会专题讲解）。

#### 4.2.4 代码实践

**实践目标**：读懂 `slowmpy` 的周期构成，并对比它与硬件乘法 `MPY1CK` 的端口/周期差异。

**操作步骤**：

1. 读 [slowmpy.v:72-91](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L72-L91)，找到 `count`（位宽 `LGNA=6` 位）和 `pre_done`/`almost_done` 的关系：`pre_done = (count==0)`，`almost_done` 在 `pre_done` 成立的下一拍置位。
2. 追踪 `count` 的初值与递减：[slowmpy.v:127-154](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/slowmpy.v#L127-L154)。空闲接收请求时 `count <= NA-1 = 32`，此后每忙一拍 `count <= count - 1`。
3. 列端口对照表：

| 维度 | `mpyop` (MPY1CK) | `slowmpy` |
|------|------------------|-----------|
| 触发 | `i_stb` | `i_stb` |
| 输入 | `i_a, i_b (32位), i_op[1:0]` | `i_a, i_b (NA位), OPT_SIGNED 参数` |
| 输出有效 | `o_valid` | `o_done` |
| 占用周期 | 1 | \(\approx NA+2 = 35\) |
| 依赖 DSP | 是（需 32×32 块） | 否（纯逻辑） |

**需要观察的现象**：`slowmpy` 把「有符号/无符号」做成编译期参数 `OPT_SIGNED`，而 `mpyop` 用运行期输入 `i_op[0]` 区分——因为 `slowmpy` 一次只算一种符号，符号修正逻辑是写死的。

**预期结果**：`slowmpy` 用约 35 拍换取「零 DSP 依赖」，是面积最小、速度最慢的乘法方案。

> 待本地验证：35 拍是依据「每比特一拍 + 2」与 `NA=33` 推得；精确计数可给 `slowmpy` 包一层测试台，在 `i_stb` 后用 `while(!o_done) tick()` 数拍。

#### 4.2.5 小练习与答案

**练习 1**：`count` 为什么从 `NA-1` 而不是 `NA` 开始递减？

**参考答案**：`count==0` 是结束判据（`pre_done`）。从 `NA-1` 减到 0 共经历 `NA` 个值（`NA-1, NA-2, …, 0`），对应 `NA` 拍逐位处理——正好每位一拍。若从 `NA` 起算会多出一拍空转。

**练习 2**：为什么 `mpyop` 给 `slowmpy` 传 `NA=33` 而不是 32？

**参考答案**：有符号乘法的补码修正会在中间结果上引入一位额外的进位/符号位，33 位宽度才能无损容纳这个修正项，保证最终取低 64 位结果正确。

---

### 4.3 div：恢复余数法除法单元

#### 4.3.1 概念说明

除法是 ZipCPU 里最「重」的指令：它**完全不经过 ALU**，而是 `zipcore` 在第 4 级（执行级）单独实例化的 `div` 模块。规范在 [spec.tex:956-976](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L956-L976) 专门用一节描述它，并强调：

> 「While the divide is running, the ALU, any memory loads, and the floating point unit (if installed) will all be idle.」（[spec.tex:966-969](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L966-L969)）

也就是说，除法一旦启动，整个执行级都停下来等它——这决定了除法是名副其实的多周期指令，流水线必须停顿。

除法有两条件：`DIVS`（有符号）和 `DIVU`（无符号）（[spec.tex:714-715](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L714-L715)）。除数为零时会触发「除零异常」，置 CC 的第 11 位（[spec.tex:593-601](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L593-L601)）。

#### 4.3.2 核心流程

`div` 的算法是「移位相减」恢复余数法，文件头有一段很完整的人话注释（[div.v:10-67](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L10-L67)）。整体分四阶段：

```
① 请求 (i_wr)        : 锁存被除数/除数，判定是否除零，拉高 o_busy
② pre_sign (可选,1拍) : 若有符号且存在负操作数，取绝对值，并记住结果符号
③ 逐位循环 (32拍)    : 每拍把部分余数左移一位、试减除数，
                        够减→商位=1并扣除，不够→商位=0
④ 取反 (可选,1拍)    : 若结果应为负，把商取反
→ o_valid 拉高一拍, o_busy 清零
```

握手协议与 `mpyop`/`slowmpy` 一致，且文件头特别强调一条铁律（[div.v:63-67](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L63-L67)）：`o_busy` 与 `o_valid` 绝不能同一拍同时为真；`o_busy` 一旦在 `o_valid` 之后就不能再为真。这是 ZipCPU 内部协议，调用方依赖它来判断「结果何时可用」。

#### 4.3.3 源码精读

**端口** —— [div.v:99-113](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L99-L113)：`i_wr` 启动、`i_signed` 选符号、`i_numerator/i_denominator` 是被除数/除数；输出 `o_busy/o_valid/o_err/o_quotient/o_flags[3:0]`。注意 `o_flags` 输出 Z/N/C 四个标志位（除法也会更新条件码）。

**试减的核心算式** —— [div.v:123-124](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L123-L124)：

```verilog
wire [(BW):0] diff;
assign diff = r_dividend[2*BW-2:BW-1] - r_divisor;
```

`diff` 取部分余数的高位减去除数。`diff[BW]`（最高位）是借位位：为 1 表示不够减（结果为负），为 0 表示够减。这就是「试商」的依据。

**两个 busy 寄存器的精妙之处** —— `div` 用了 `r_busy` 和 `o_busy` 两个忙标志，差别在 [div.v:117-119](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L117-L119) 与 [div.v:149-167](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L149-L167) 的注释里说得很清楚：

- `r_busy` 在最后一个比特位就清零；
- `o_busy` 可能比 `r_busy` 多撑一拍——那一拍用来对结果取反（signed 负结果时）。

所以 `o_busy = (signed && 需取反) ? r_busy + 1拍 : r_busy`。

**逐位循环计数** —— `r_bit` 是 5 位计数器（`LGBW=5`），见 [div.v:219-233](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L219-L233)；`last_bit` 标志循环将至，见 [div.v:235-251](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L235-L251)。注释解释了一个易错点：我们想在 `r_bit==-1` 时结束，但 `r_bit` 是无符号的，于是改成「在 `r_bit==-2`（即 `BW-2=30`）时预设 `last_bit`」，这样 `r_bit` 走到 31 时 `last_bit` 正好为真。

**被除数与商的移位** —— [div.v:286-307](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L286-L307)（被除数）与 [div.v:348-360](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L348-L360)（商）。每拍：

```verilog
r_dividend <= { r_dividend[2*BW-3:0], 1'b0 };   // 左移一位
if (!diff[BW])                                   // 够减
    r_dividend[2*BW-2:BW] <= diff[(BW-2):0];     // 扣除
...
o_quotient <= { o_quotient[BW-2:0], 1'b0 };      // 商左移
if (!diff[BW]) o_quotient[0] <= 1'b1;            // 够减则商位=1
```

这就是手工长除法的硬件翻版：被除数逐位「喂」进高位余数区，够减就扣并把 1 推进商。

**符号处理** —— `pre_sign`（[div.v:253-264](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L253-L264)）在启动后插一拍取绝对值；`r_sign`（[div.v:323-343](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L323-L343)）记住「结果应为正还是负」（两操作数符号异或）；最后若 `r_sign` 为真，`o_quotient <= -o_quotient`（[div.v:356-357](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L356-L357)）。策略是「先按无符号算绝对值商，最后再定符号」，比直接做有符号除法简单得多。

**除零检测** —— `zero_divisor`（[div.v:169-174](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L169-L174)）在请求当拍记录除数是否为 0；若是，`o_err` 拉高（[div.v:204-217](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L204-L217)），并提前结束。

**内核中的实例化** —— [zipcore.v:1527-1563](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1527-L1563)。当 `OPT_DIV!=0` 时实例化 `div`（形式化验证时换成 `abs_div` 抽象模型）：

```verilog
`DIVIDE_MODULE #(.OPT_LOWPOWER(OPT_LOWPOWER)) thedivide(
    .i_clk(i_clk), .i_reset(i_reset || clear_pipeline),
    .i_wr(div_ce), .i_signed(op_opn[0]),
    .i_numerator(op_Av), .i_denominator(op_Bv),
    .o_busy(div_busy), .o_valid(div_valid),
    .o_err(div_error), .o_quotient(div_result), .o_flags(div_flags));
```

启动信号 `div_ce` 由译码级的 `dcd_DIV` 经 `op_valid_div` 门控产生（[zipcore.v:1073](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1073)）；`div_busy` 则汇入流水线停顿逻辑。`OPT_DIV==0` 时整个 `div` 不生成，除法指令会被判为非法（形式化断言见 [zipcore.v:5014-5015](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L5014-L5015)）。

#### 4.3.4 代码实践

**实践目标**：通过源码估算一次 32 位除法的时钟周期数，并用组件测试台实测验证。

**操作步骤**：

1. **估算**。从源码拆出每段的拍数：
   - 请求锁存：1 拍（`i_wr` 当拍，`r_busy`/`o_busy` 拉高，被除数/除数就位）；
   - 逐位循环：32 拍（`r_bit` 从 0 走到 31，每位一拍，见 [div.v:226-233](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L226-L233) 与 [div.v:243-251](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L243-L251)）；
   - `pre_sign`：有符号且含负操作数时 +1 拍（[div.v:259-264](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L259-L264)）；
   - 结果取反：有符号且结果为负时 +1 拍（[div.v:356-357](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L356-L357)，`o_busy` 多撑一拍）。
   
   于是：**无符号除法约 33 拍（1 + 32）；有符号除法约 33–35 拍**，取决于是否需要 `pre_sign` 与取反。

2. **实测**。打开除法测试台 [div_tb.cpp](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp)。它的驱动函数 `div()` 正是「请求→等 busy→数 tick→验结果」的循环（[div_tb.cpp:140-200](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/div_tb.cpp#L140-L200)）：

   ```cpp
   m_core->i_wr = 1; ... tick();          // 请求
   m_core->i_wr = 0;
   while(!m_core->o_valid) {              // 一直 tick 到 valid
       ...
       tick();
   }
   ```

   在 `sim/verilator` 下编译并运行 `div_tb`（具体目标名以该目录 Makefile 为准，参考 u1-l4/u5-l3）。

3. 想直接数拍，可在 `div_tb.cpp` 的 `while(!m_core->o_valid)` 循环里加一个计数器（示例代码，非项目原有）：

   ```cpp
   int cycles = 0;                       // 示例代码
   while(!m_core->o_valid) { ++cycles; tick(); }
   printf("div took %d ticks\n", cycles);
   ```

**需要观察的现象**：无符号除法的 `cycles` 应落在 32 附近（while 循环从 `i_wr` 拉高后的那一拍开始数到 `o_valid`）；有符号且结果为负时会多 1–2。

**预期结果**：估算与实测一致，落在 32–35 拍区间，印证「除法是 ~30 多周期的多周期指令」。

> 待本地验证：具体拍数依赖 `BW=32` 与综合/仿真环境；上表为依据源码状态机推断，请以本地 `div_tb` 实测为准。若环境未装好 Verilator，可先做纯源码估算部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `div` 用 `r_busy` 和 `o_busy` 两个忙标志，而不直接用一个？

**参考答案**：逐位循环在最后一个比特位就完成了 `o_quotient` 的绝对值计算，此时 `r_busy` 清零；但若有符号且结果应为负，还需要额外一拍做 `o_quotient <= -o_quotient`。`o_busy` 比 `r_busy` 多撑这一拍，正是为了覆盖取反阶段。用单标志无法表达「算完了但还要再修一下」的中间态。

**练习 2**：除数为零时，`div` 是怎么处理的？会一直 busy 吗？

**参考答案**：请求当拍 `zero_divisor <= (i_denominator==0)` 记录下来（[div.v:171-174](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L171-L174)）。进入 `r_busy` 后，`(r_busy)&&(zero_divisor)` 会立刻清 `r_busy`/`o_busy`、拉高 `o_err` 和 `o_valid`（[div.v:191-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L191-L192)、[div.v:213-214](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/div.v#L213-L214)）。所以除零不会卡死，而是很快返回一个带错误标志的结果，CPU 据此触发除零异常（置 CC 第 11 位）。

**练习 3**：估算 \(-6 \div 2\) 需要几拍，并说明比 \(6 \div 2\) 多在哪里。

**参考答案**：\(-6 \div 2 = -3\)，有符号且被除数为负 → 需要 `pre_sign` 拍取绝对值（+1），结果为负 → 需要取反拍（+1），逐位循环仍 32 拍，请求 1 拍，合计约 35 拍。而 \(6 \div 2 = 3\)（无符号或两正数有符号）没有 `pre_sign`、没有取反，约 33 拍。多出的 2 拍全花在符号处理上。

---

## 5. 综合实践

**任务**：给 ZipCPU 选一组乘除配置，并预测它们对一段循环程序性能的影响。

背景：假设你要把 ZipCPU 用在一个资源紧张的小型 FPGA 上做数字信号处理，程序里有一段形如下面的点积循环：

```c
// 示例代码（伪 C，仅说明语义）
for (int i = 0; i < N; i++) {
    acc += a[i] * b[i];     // 乘法
    acc /= scale;            // 除法
}
```

请完成：

1. **选型**：阅读 [spec.tex:924-954](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L924-L954) 的乘法实现表与本讲对 `div` 的周期估算，从下面三套配置中选一套并说明理由：
   - 配置 A：`OPT_MPY=3`（3 拍 DSP 乘法）、`OPT_DIV=1`
   - 配置 B：`OPT_MPY=36`（slowmpy 慢乘）、`OPT_DIV=1`
   - 配置 C：`OPT_MPY=0`（无乘法）、`OPT_DIV=0`（无除法）
2. **定量估算**：设 \(N=100\)。按你所选配置，估算这段循环里乘法和除法各自的总耗时（以时钟周期计），并指出谁是瓶颈。提示：单次乘法周期 ≈ `OPT_MPY` 对应的拍数（3 / 35 / 非法）；单次除法 ≈ 33–35 拍或非法。
3. **代码追踪**：在 [zipcore.v:1506-1563](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v#L1506-L1563) 确认：乘法结果通过 `cpuops`（`doalu`）回流，而除法结果是 `div_result` 独立一路；两者最终都在写回级与 ALU/内存结果竞争（u3-l7 详述）。画出这条「乘 vs 除」数据回流路径的草图。
4. **反思**：配置 C 里乘除都非法，那这段 C 程序还能跑吗？结合 [spec.tex:918-919](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L918-L919)（`OPT_MULTIPLY=0` 会触发 ILLEGAL 指令异常）说明软件层面要怎么补救（提示：用库函数/软件实现的乘除子程序，即「软除法」「软乘法」）。

**预期产出**：一份选型决定 + 一张周期估算表 + 一张数据回流草图 + 一段对「无乘除硬件时如何退化为软件实现」的说明。

> 待本地验证：若想在模拟器里实测不同 `OPT_MPY` 下的乘法周期，可参照 `cpuops.v` 的 `VMPY_TB` 测试钩子（[cpuops.v:242-278](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/cpuops.v#L242-L278)）和 `div_tb`，比较 `OPT_MPY=3` 与 `OPT_MPY=36` 下同一段循环的实际 tick 数。

## 6. 本讲小结

- ZipCPU 把乘法和除法从 ALU 的单周期关键路径里剥离出来，各做一个带 `o_busy`/`o_valid` 握手的多周期执行单元，挂在流水线第 4 级（执行级）。
- `mpyop` 用一个 `generate if` 阶梯按综合期参数 `OPT_MPY` 选 6 种实现：0＝不做（非法指令）、1＝单周期、2/3/4＝多周期 DSP、其它＝回退到 `slowmpy`。spec 里的 `OPT_MULTIPLY` 就是 RTL 的 `OPT_MPY`。
- `slowmpy` 是逐位移位累加乘法器，约 \(\text{NA}+2\) 拍（默认 33 位 → 约 35 拍），完全不依赖 DSP，是面积最小、速度最慢的退路。
- `div` 用恢复余数法 / 移位相减实现除法：请求 1 拍 + 逐位循环 32 拍 +（有符号时）取绝对值与取反各至多 1 拍，无符号约 33 拍、有符号约 33–35 拍；除零会快速返回 `o_err`。
- 乘法结果经 `cpuops` 回流到 ALU 输出，除法结果 (`div_result`) 是执行级的独立一路；两者运行时都会让 ALU/访存/FPU 停下来等待，所以都属于「多周期指令」，是流水线停顿的重要来源（承接 u3-l7）。
- 这三个模块都用同一套「请求 (`i_stb`/`i_wr`) → 忙 (`o_busy`) → 有效 (`o_valid`/`o_done`)」握手协议，且都带 `ifdef FORMAL` 的形式化属性段（为 u5-l2 铺路）。

## 7. 下一步学习建议

- **u3-l6（访存模块族）**：继续看执行级里的另一个多周期单元——`memops`/`pipemem`/`dcache`，它们的握手协议与本讲的乘除单元如出一辙，可以对照阅读。
- **u3-l7（流水线冒险与停顿）**：本讲多次提到「除法运行时流水线停顿」。下一讲会从 `zipcore` 的 `master_stall`/`div_busy` 控制逻辑讲清楚停顿到底是怎么产生的、写回级如何裁决 ALU/乘/除/内存四路结果。
- **u5-l2（形式化验证）**：`slowmpy.v` 和 `div.v` 末尾的 `ifdef FORMAL` 段是 SymbiYosys 证明的入口，到进阶层会专门讲怎么跑、怎么读这些断言。
- **延伸阅读**：spec.tex 的 [Multiply Operations](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L905-L955) 与 [Divide Unit](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L956-L976) 两节是本讲涉及的 ISA 权威说明，建议对照源码再读一遍。
