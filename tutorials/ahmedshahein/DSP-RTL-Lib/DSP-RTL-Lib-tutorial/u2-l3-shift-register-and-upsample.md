# shift_register 与 upsample 原语

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `shift_register` 如何用 `generate` 把多个 `dff` 级联成一条延迟线，并能解释 `o_shift_done` 标志的含义与生效条件。
- 说出 `upsample` 如何用一个计数器实现「零插值（zero-stuffing）」上采样，并能解释 `gp_phase` 相位偏移的作用。
- 在 `filt_cicd`、`filt_cici`、`filt_ppd/commutator` 中认出这两个原语被复用的位置，理解它们在多速率（抽取/插值）信号链中的角色。
- 自己动手给 `shift_register` 增加一个「并行输出」调试端口，并写一个最小测试平台观察各级波形。

本讲是单元 2（定点数与共享原语）的收尾篇。`dff`（见 u2-l2）是原子构建块，本讲的 `shift_register` 与 `upsample` 则是「把若干个 `dff` 组织成有特定行为的小电路」的第二个台阶——它们是后续 CIC 与多相滤波器反复调用的零件。

## 2. 前置知识

在开始前，请确认你已经理解下面这些来自前面讲义的概念：

- **延迟算子 \(z^{-1}\)**：一个上升沿触发的寄存器，把输入延迟一个时钟周期。u2-l2 已说明使能态下的 `dff` 就等价于 \(z^{-1}\)。
- **`dff` 的统一接口**：端口顺序固定为 `i_rst_an, i_ena, i_clk, i_data, o_data`，异步低有效复位、同步高有效使能、仅用上升沿。本讲的所有原语都按这个接口级联 `dff`。
- **命名前缀**：`gp_` 可覆盖参数、`c_` 派生常量、`r_` 寄存器、`w_` 组合连线（见 u1-l4）。
- **多速率基础直觉**：抽取（decimation）是「采样率变低」、插值（interpolation）是「采样率变高」。本讲不要求你懂 CIC/多相的完整理论，只要知道「上采样需要往样本之间补零」即可。

两个术语先在这里约定：

- **延迟线（delay line）/ 移位寄存器（shift register）**：一串首尾相接的寄存器，数据每拍整体向后挪一格。常用来「把信号延迟 N 拍」。
- **零插值（zero-stuffing / zero-insertion）**：在两个原始样本之间插入若干个 0，从而把采样率提高。例如对序列 \(x[n]\) 做 4 倍零插值，得到 \(x[0],0,0,0,x[1],0,0,0,\dots\)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [.drl_src_code/filt_cicd/rtl/shift_register.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v) | 本讲主角一：参数化延迟线，把 `gp_nr_stages` 个 `dff` 级联，输出末级，并产生 `o_shift_done` 标志。 |
| [.drl_src_code/filt_cici/rtl/upsample.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v) | 本讲主角二：零插值上采样器，每 `gp_nr_stages` 拍输出一个真实样本、其余拍补零，支持 `gp_phase` 相位偏移。 |
| [.drl_src_code/filt_cicd/rtl/dff.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) | 原子寄存器（u2-l2 已精读），两个原语都靠它级联。 |
| [.drl_src_code/filt_cicd/rtl/filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v) | CIC 抽取滤波器，在其「微分（comb）段」复用 `shift_register`（参数 `gp_nr_stages=gp_diff_delay`）。 |
| [.drl_src_code/filt_cici/rtl/filt_cici.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v) | CIC 插值滤波器，comb 段复用 `shift_register`，并实例化一个 `upsample` 实现零插值。 |
| [.drl_src_code/filt_ppd/rtl/commutator.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v) | 多相抽取器的换向器，用 `shift_register`（参数 `gp_nr_stages=gp_phase`）做相位对齐。 |

> 提示：`filt_cicd` 与 `filt_ppd` 各自目录下都有一份内容几乎相同的 `shift_register.v`（仅 `generate` 块标签名、always 块与 generate 块的先后顺序略有不同，逻辑一致）。本讲以 `filt_cicd` 版为精读对象。

## 4. 核心概念与源码讲解

### 4.1 shift_register：级联 dff 延迟线与「初始填充完成」标志

#### 4.1.1 概念说明

很多 DSP 运算都需要「把信号延迟固定拍数」。最朴素的办法是把若干个 \(z^{-1}\)（即 `dff`）串起来：

\[
\text{out}[n] = \text{in}[n-N]
\]

其中 \(N\) 就是级联的级数。`shift_register` 把这件事参数化了：你给它 `gp_nr_stages`，它就生成相应级数的 `dff` 串联，把**末级**作为 `o_data` 输出。

但它多做了一个细节：刚复位时，整条延迟线里全是 0，还「没装满」真实数据。于是它额外维护一个计数器，数够 `gp_nr_stages` 拍后拉高 `o_shift_done`，告诉上层「延迟线已填满，输出现在有效了」。这个标志在多相换向器里被当作「可以开始输出」的使能信号。

#### 4.1.2 核心流程

整体由两部分组成：

1. **数据通路**：一条由 `gp_nr_stages` 个 `dff` 首尾相接构成的链。
   - 第 0 级吃外部输入 `i_data`。
   - 第 \(i\) 级吃第 \(i-1\) 级的输出。
   - 末级输出接到 `o_data`。

2. **标志通路**：一个计数器 `r_cnt` 从 0 数到 `gp_nr_stages`；一旦到达，就把 `r_shift_done` 锁存为 1 并保持，经 `o_shift_done` 送出。

用伪代码描述：

```
数据通路：
  stage[0] <= i_data            // 第 0 级 dff
  for i in 1..N-1:
      stage[i] <= stage[i-1]    // 后续各级 dff
  o_data = stage[N-1]           // 只暴露末级

标志通路：
  if 复位: r_cnt=0, r_shift_done=0
  elif i_ena:
      if r_cnt < N: r_cnt++
      if (r_cnt==N 或 r_shift_done): r_shift_done=1
  o_shift_done = (r_cnt==N) ? 1 : r_shift_done
```

注意：`shift_register` 的输出**只暴露末级** `stage[N-1]`，中间各级数据在模块内部被「藏」在打包线 `w_data` 里。这正是本讲代码实践要改造的地方。

#### 4.1.3 源码精读

模块头：两个参数 + 五个端口，端口顺序与 `dff` 完全一致（便于级联），多了一个 `o_shift_done` 标志输出。

[.drl_src_code/filt_cicd/rtl/shift_register.v:L6-L16](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L6-L16) —— 这里声明了 `gp_data_width`（位宽）与 `gp_nr_stages`（级数），以及统一的 `i_rst_an/i_ena/i_clk/i_data/o_data`，再加 `o_shift_done`。

计数器位宽用一个派生常量给出：

[.drl_src_code/filt_cicd/rtl/shift_register.v:L19](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L19) —— `localparam c_cnt_width = $clog2(gp_nr_stages);`。⚠️ 注意这里**没有 +1**（与下面 `upsample` 的写法不同），这一点会在 4.1.5 讨论。

数据通路用 `generate-for` 把 `gp_nr_stages` 个 `dff` 级联起来：

[.drl_src_code/filt_cicd/rtl/shift_register.v:L27-L56](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L27-L56) —— 第 0 级（`i==0`）吃 `i_data`，后续级吃上一级输出。各级输出都被拼接到一条打包线 `w_data` 上（按 `[(i+1)*W-1 -: W]` 分段）。这正是「把多个 dff 当作一条延迟线」的标准写法，体现 u2-l2 所说的「改接 `i_clk`/`i_ena` 即可让 dff 变形」。

标志通路是一个熟悉的「异步复位 + 同步使能」三段式 always 块：

[.drl_src_code/filt_cicd/rtl/shift_register.v:L58-L73](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L58-L73) —— 复位清零优先；使能时 `r_cnt` 自增（到 `gp_nr_stages` 封顶），并在条件满足时把 `r_shift_done` 锁存为 1。

最后用三句 `assign` 收尾：

[.drl_src_code/filt_cicd/rtl/shift_register.v:L75-L77](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v#L75-L77) —— `w_shift_done = (r_cnt==gp_nr_stages)?1:r_shift_done`；`o_shift_done` 直接送出；**`o_data` 只取 `w_data` 的最高一段（末级）**，中间级不外露。

#### 4.1.4 代码实践

**实践目标**：给 `shift_register` 增加一个并行输出端口 `o_data_all`，一次性暴露所有中间级数据，用于调试延迟链填充过程；再写一个最小测试平台，在 `gp_nr_stages=4` 时观察各级波形。

**操作步骤**：

1. 复制一份原语做实验（不要改动仓库源码）：

```bash
cp .drl_src_code/filt_cicd/rtl/shift_register.v /tmp/shift_register_dbg.v
cp .drl_src_code/filt_cicd/rtl/dff.v            /tmp/dff.v
```

2. 把模块改名 `shift_register_dbg`，新增端口 `o_data_all`，并把内部打包线 `w_data` 直接接出。关键改动（示例代码，仅展示新增/修改行）：

```verilog
// 示例代码：基于 shift_register.v 修改
module shift_register_dbg #(
  parameter gp_data_width = 8,
  parameter gp_nr_stages  = 4
) (
  input  wire                     i_rst_an,
  input  wire                     i_ena,
  input  wire                     i_clk,
  input  wire [gp_data_width-1:0] i_data,
  output wire [gp_data_width-1:0] o_data,                              // 末级（最深延迟）
  output wire [gp_nr_stages*gp_data_width-1:0] o_data_all,             // 新增：所有级并行
  output wire                     o_shift_done
);
  // ... generate 级联 dff 的数据通路保持不变 ...
  wire [gp_nr_stages*gp_data_width-1:0] w_data;

  assign o_data     = w_data[gp_nr_stages*gp_data_width-1 -: gp_data_width];
  assign o_data_all = w_data;   // 新增一行：把所有中间级一起暴露
endmodule
```

3. 编写最小测试平台（示例代码）：

```verilog
`timescale 1ns/1ps
module tb_shift_register_dbg;
  reg        clk = 1'b0, rst_an = 1'b0, ena = 1'b0;
  reg  [7:0] i_data = 8'd0;
  wire [7:0] o_data;
  wire [31:0] o_data_all;       // gp_nr_stages(4) * gp_data_width(8) = 32
  wire       o_shift_done;

  shift_register_dbg #(.gp_data_width(8), .gp_nr_stages(4)) DUT (
    .i_rst_an(rst_an), .i_ena(ena), .i_clk(clk), .i_data(i_data),
    .o_data(o_data), .o_data_all(o_data_all), .o_shift_done(o_shift_done));

  always #5 clk = ~clk;          // 10ns 周期

  initial begin
    #12 rst_an = 1'b1; ena = 1'b1;
    // 依次送入 1,2,3,4,5,6，每拍一个
    i_data=8'd1; #10 i_data=8'd2; #10 i_data=8'd3; #10 i_data=8'd4;
    #10 i_data=8'd5; #10 i_data=8'd6; #10;
    $display("o_data_all = %h", o_data_all);
    $finish;
  end
endmodule
```

4. 用 iverilog 编译并运行（命令本身依赖你本地是否装了 iverilog）：

```bash
cd /tmp && iverilog -o sim.vvp shift_register_dbg.v dff.v tb_shift_register_dbg.v && vvp sim.vvp
```

**需要观察的现象**：

- `o_data_all` 的 4 个字节分别对应 4 级 `dff`。按位拼接顺序，`o_data_all[7:0]` 是第 0 级（最新输入），`o_data_all[31:24]` 是第 3 级（最旧、即 `o_data`）。
- 随着每拍送入新值，你能看到数据像「传送带」一样逐级向后挪。

**预期结果（手工追踪，待本地验证）**：在送完 `1,2,3,4,5,6` 后，延迟线被填满，各级内容为：

| 字段 | `o_data_all[31:24]`（第3级/末级） | `[23:16]`（第2级） | `[15:8]`（第1级） | `[7:0]`（第0级） |
| --- | --- | --- | --- | --- |
| 值 | 3 | 4 | 5 | 6 |

所以 `$display` 应打印 `o_data_all = 03040506`，且 `o_data = 8'd3`。请本地运行确认。

> 同时请留意 `o_shift_done`：在本实践参数（`gp_nr_stages=4`）下，它大概率**不会**拉高。原因见 4.1.5 的练习 2。

#### 4.1.5 小练习与答案

**练习 1**：为什么第 0 级 `dff` 要单独写一个 `if (i==0)` 分支，而不能和后续级用同一段代码？

<details>
<summary>参考答案</summary>

因为第 0 级的输入是**外部端口** `i_data`，而后续第 \(i\) 级的输入是**上一级的输出** `w_data[i*W-1 -: W]`。两者来源不同，所以必须分支处理。这也正是「打包线 `w_data`」的作用：它把每一级的输出都固定地存放在一个可寻址的位段里，方便下一级用统一的下标表达式去取。
</details>

**练习 2**（进阶）：`shift_register` 的计数器位宽是 `c_cnt_width = $clog2(gp_nr_stages)`，但判断条件是 `r_cnt == gp_nr_stages`。当 `gp_nr_stages=4` 时，`o_shift_done` 会不会拉高？为什么？对照 `upsample` 的写法，差异在哪里？

<details>
<summary>参考答案（待本地仿真最终确认）</summary>

不会拉高。`$clog2(4)=2`，所以 `r_cnt` 只有 2 位，能表示的最大值是 3。计数器在 `0→1→2→3→0` 之间循环，永远到不了 4，因此 `r_cnt == gp_nr_stages(4)` 永假，`w_shift_done` 始终等于初值 0，`o_shift_done` 也就一直是 0。

根因是要「比较等于 N」就需要能把 N 本身表示出来，位宽应是 `$clog2(gp_nr_stages)+1`。`upsample` 正是这样写的（见 4.2.3 的 [upsample.v:L20](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L20)），而 `shift_register` 漏了这个 +1。在 CIC 抽取器里这个标志被悬空（`.o_shift_done()`），所以不影响功能；但在多相换向器里它被接成了使能信号，使用前应留意参数取值。
</details>

---

### 4.2 upsample：零插值上采样器与相位偏移

#### 4.2.1 概念说明

CIC 插值器（`filt_cici`）需要把低速率信号「升采样」到高速率。最直接的方式是**零插值**：在每两个原始样本之间插入 \(L-1\) 个 0（\(L\) 为插值因子），得到 \(L\) 倍采样率。`upsample` 就是做这件事的参数化原语。

它额外提供一个 `gp_phase` 参数：在某些多速率结构里，真实样本应当出现在 \(L\) 个输出槽位中的某一个特定相位上，而不是固定在第 0 槽。`gp_phase` 让真实样本在输出流中「延后」若干拍，从而对齐到期望相位。

#### 4.2.2 核心流程

`upsample` 用一个「模 \(L\) 计数器」决定当前拍是「输出真实样本」还是「输出 0」：

```
每 L 拍为一个周期：
  当 r_cnt == 0：w_load = 1，选择 i_data（真实样本）
  其余拍：      w_load = 0，选择零插入链送来的 0
若 gp_phase != 0：在输出前再加 gp_phase 级 dff，把样本延后到目标相位
o_data = 经过相位偏移后的数据
```

于是输出序列形如（\(L=4, gp\_phase=0\)）：

\[
x[0],\,0,\,0,\,0,\,x[1],\,0,\,0,\,0,\,\dots
\]

#### 4.2.3 源码精读

模块头比 `shift_register` 多一个 `gp_phase` 参数：

[.drl_src_code/filt_cici/rtl/upsample.v:L6-L17](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L6-L17) —— `gp_data_width`、`gp_nr_stages`（即插值因子 \(L\)）、`gp_phase`。

两个关键派生常量，注意与 `shift_register` 的对比：

[.drl_src_code/filt_cici/rtl/upsample.v:L20-L21](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L20-L21) —— `c_cnt_width = $clog2(gp_nr_stages)+1`（**这里加了 +1**），以及 `c_phase_offset = (gp_phase==0)?1:gp_phase`（相位为 0 时退化为 1 级）。

数据选择器：由 `w_load` 决定送真实样本还是 0：

[.drl_src_code/filt_cici/rtl/upsample.v:L33](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L33) —— `assign w_upsample_data = (w_load) ? i_data : w_zero_insertion[gp_data_width-1:0];`

**相位偏移通路**（第一个 generate 块）：

[.drl_src_code/filt_cici/rtl/upsample.v:L35-L71](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L35-L71) —— 当 `gp_phase==0` 时，`w_phase_offset` 直接等于 `w_upsample_data`（无延迟）；否则用 `gp_phase` 个 `dff` 把样本延后相应拍数，实现相位对齐。

**零插入通路**（第二个 generate 块）：

[.drl_src_code/filt_cici/rtl/upsample.v:L73-L93](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L73-L93) —— 用 `gp_nr_stages-1` 段构成一条延迟链，最末端恒接 `'d0`，于是这条链源源不断地把 0 向前传送；当 `w_load=0` 时，`w_upsample_data` 取这条链送来的 0。

计数器与标志（与 `shift_register` 同款的异步复位 + 同步使能骨架）：

[.drl_src_code/filt_cici/rtl/upsample.v:L95-L112](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L95-L112) —— 复位时 `r_cnt` 预置为 `gp_nr_stages`（注意与 `shift_register` 复位为 0 不同）；使能时模 \(L\) 计数，到达 `gp_nr_stages-1` 后回零。

最后的输出选择：

[.drl_src_code/filt_cici/rtl/upsample.v:L114-L117](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L114-L117) —— `w_load = (r_cnt==0)`；`o_data` 取相位偏移链的末段。

#### 4.2.4 代码实践

**实践目标**：手工追踪 `upsample`（`gp_nr_stages=4, gp_phase=0`）一个完整周期，验证它是「1 个真实样本 + 3 个 0」的零插值。

**操作步骤**：

1. 阅读 [upsample.v:L95-L117](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/upsample.v#L95-L117)，列出每个使能拍 `r_cnt`、`w_load` 与 `o_data` 的取值。
2. 假设 `i_data` 在整个追踪期间恒为常数 `D`（模拟 CIC 插值器里慢时钟域样本在一组快时钟拍内保持稳定）。

**预期结果（手工追踪，待本地验证）**：复位后 `r_cnt=4`，之后使能拍依次为：

| 拍 | r_cnt（本拍取值） | w_load | o_data |
| --- | --- | --- | --- |
| 1 | 4 → 0 | 0 | 0（零插入链，启动瞬态） |
| 2 | 0 → 1 | 1 | D（真实样本） |
| 3 | 1 → 2 | 0 | 0 |
| 4 | 2 → 3 | 0 | 0 |
| 5 | 3 → 0 | 0 | 0 |
| 6 | 0 → 1 | 1 | D |

可以看到「每 4 拍恰好 1 拍 `w_load=1`」，正是 4 倍零插值。请用 iverilog 实例化 `upsample` 并打印 `o_data` 波形确认。

#### 4.2.5 小练习与答案

**练习 1**：`upsample` 的零插入链用 `generate` 例化了 `gp_nr_stages-1` 段，且最末端 `assign ... = 'd0`。为什么是 \(L-1\) 段而不是 \(L\) 段？

<details>
<summary>参考答案</summary>

因为「真实样本」这一拍由 `i_data` 直接提供（经 `w_load` 选择），不需要额外寄存器；只有其余 \(L-1\) 个「补零」拍需要一条延迟链来承载并向前传递 0。链的最末端恒为 0，正好是这个零的「源头」，前 \(L-2\) 段 `dff` 负责把它逐拍搬运到 `w_zero_insertion[gp_data_width-1:0]` 供 `w_load=0` 时选用。所以总共 \(L-1\) 段、其中 1 段是常量 0、其余 \(L-2\) 段是 `dff`。
</details>

**练习 2**：`gp_phase` 从 0 改成 2 后，输出序列会怎样变化？

<details>
<summary>参考答案</summary>

真实样本会被**额外延后 2 拍**出现。`c_phase_offset` 由 1 变为 2，相位偏移通路会例化 2 级 `dff`，把 `w_upsample_data` 延迟 2 拍再输出。结果是同一个样本在输出流中的位置向后移了 2 个槽位，相当于把零插值的「相位」对齐到了第 2 槽。这正是多相/插值结构中做相位对齐的常用手法。
</details>

---

### 4.3 两个原语如何在多速率模块中被复用

#### 4.3.1 概念说明

`shift_register` 与 `upsample` 不是孤立的练习件，而是 CIC 与多相滤波器的「标准零件」。本节把它们放回真实调用点，让你看清「同一个原语，换个参数就换了角色」的复用哲学——这正是 u1-l1 讲到的 mix-and-match 理念的落地。

#### 4.3.2 核心流程

三处典型复用：

1. **CIC 抽取器 `filt_cicd` 的微分段**：用 `shift_register`（`gp_nr_stages=gp_diff_delay`）实现梳齿延迟 \(z^{-D}\)。因为运行在下采样后的慢相位上，例化时把 `.i_ena` 接到环形计数器的某一位 `r_count[gp_phase]`，从而只在被选中的相位上移位。
2. **CIC 插值器 `filt_cici`**：微分段同样用 `shift_register`；而插值核心则用一个 `upsample`（`gp_nr_stages=gp_interpolation_factor`）做零插值，并把它接在**快时钟 `i_fclk`** 上。
3. **多相换向器 `commutator`**：当 `gp_phase != 0` 时，用一个 `shift_register`（`gp_nr_stages=gp_phase`）把输入延后若干拍，做相位对齐；其 `o_shift_done` 还被接成了内部使能 `w_ena`。

#### 4.3.3 源码精读

CIC 抽取器微分段对 `shift_register` 的调用（注意 `.i_ena(r_count[gp_phase])` 这个相位门控）：

[.drl_src_code/filt_cicd/rtl/filt_cicd.v:L119-L129](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L119-L129) —— 这里 `gp_nr_stages` 取 `gp_diff_delay`（梳齿延迟，常为 1），`o_shift_done` 悬空不用。

CIC 插值器对 `upsample` 的调用（注意时钟是快时钟 `i_fclk`）：

[.drl_src_code/filt_cici/rtl/filt_cici.v:L86-L97](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L86-L97) —— `.i_clk(i_fclk)` 把零插值放在快时钟域；`gp_nr_stages` 取 `gp_interpolation_factor`，`gp_phase` 透传。其前置微分段也用了 `shift_register`，见 [filt_cici.v:L50-L60](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cici/rtl/filt_cici.v#L50-L60)。

多相换向器用 `shift_register` 做相位对齐，并把 `o_shift_done` 接成使能：

[.drl_src_code/filt_ppd/rtl/commutator.v:L71-L81](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L71-L81) —— `gp_nr_stages` 取 `gp_phase`（仅当 `gp_phase != 0` 才例化），`o_shift_done` 接到 `w_ena`。逆时针（CCW）分支有一份对称写法，见 [commutator.v:L155-L165](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L155-L165)。

#### 4.3.4 代码实践

**实践目标**：通过「源码阅读」把三处调用点的参数与接线对应起来，体会复用模式。

**操作步骤**：

1. 打开上面三段源码，填写下表（待本地确认）：

| 调用点 | 原语 | `gp_nr_stages` 取值 | 时钟/使能接法 | `o_shift_done` 接法 |
| --- | --- | --- | --- | --- |
| `filt_cicd` 微分段 | `shift_register` | `gp_diff_delay` | `i_clk` / `r_count[gp_phase]` | 悬空 |
| `filt_cici` 微分段 | `shift_register` | ? | ? | ? |
| `filt_cici` 插值核心 | `upsample` | `gp_interpolation_factor` | `i_fclk` / `i_ena` | ? |
| `filt_ppd/commutator` 相位对齐 | `shift_register` | `gp_phase` | `i_clk` / `i_ena` | `w_ena` |

2. 思考：为什么 `filt_cicd` 的 `shift_register` 要把使能接到 `r_count[gp_phase]`，而 `filt_cici` 的 `upsample` 却用全局 `i_ena`？

**预期结果**：`filt_cicd` 运行在「先积分、后下采样、再微分」的单一时钟域，微分段必须只在被下采样选中的相位上移位，所以用环形计数器的某一位做门控使能；`filt_cici` 的 `upsample` 本身就靠内部模 \(L\) 计数器决定哪拍取真实样本，因此只需稳定的全局使能，由它自己完成零插值节拍。

#### 4.3.5 小练习与答案

**练习**：对比 `filt_cicd` 与 `filt_cici` 中 `shift_register` 的实例化，两者在「数据流位置」和「时钟」上有什么根本区别？

<details>
<summary>参考答案</summary>

- **位置**：在抽取器 `filt_cicd` 中，`shift_register` 处于**微分段（comb）**，紧跟在下采样之后；在插值器 `filt_cici` 中，`shift_register` 同样处于微分段，但位于上采样**之前**（微分→上采样→积分）。两者的共同点是：梳状微分器都用 `shift_register` 实现 \(z^{-D}\)。
- **时钟**：`filt_cicd` 全程单时钟 `i_clk`（慢速率有效，靠 `r_count[gp_phase]` 门控）；`filt_cici` 的微分段跑在慢时钟 `i_clk`，而随后的 `upsample` 与积分段跑在快时钟 `i_fclk`。这正对应 u4 讲义将讲到的「插值器后半段必须在快时钟上运行」。
</details>

## 5. 综合实践

把本讲三个最小模块串起来做一个小任务：**用「`dff` + `shift_register` + `upsample`」拼一个可视化的小演示**。

1. 实例化一个 `shift_register_dbg`（你自己在 4.1.4 改造的、带 `o_data_all` 的版本），`gp_nr_stages=4`，输入接一个计数器（每拍 `i_data` 自增 1）。
2. 再实例化一个 `upsample`，`gp_nr_stages=4, gp_phase=0`，把 `shift_register` 的 `o_data` 作为 `upsample` 的 `i_data`，时钟接到一个 4 倍频的快时钟。
3. 用 `$display` 或 dump VCD，观察：延迟线如何逐拍填充（看 `o_data_all`），以及 `upsample` 如何把延迟线输出「展开」成「1 个真实样本 + 3 个 0」。
4. 把 `upsample` 的 `gp_phase` 改成 2，重新观察真实样本在输出流中的位置变化。

**验收标准**：

- 能画出 `shift_register_dbg` 的 `o_data_all` 4 个字节随时间演化的表格。
- 能解释 `upsample` 输出中真实样本出现的拍位置，以及 `gp_phase` 改变后它如何移动。

这个任务把「延迟线 → 零插值」这条最短的多速率数据流走通，为 u4（CIC）和 u5（多相）打好直觉。

## 6. 本讲小结

- `shift_register` = `gp_nr_stages` 个 `dff` 的 `generate` 级联，输出末级，并提供 `o_shift_done`「延迟线已填满」标志。
- `shift_register` 只暴露末级；中间级藏在打包线 `w_data` 里，调试时可加 `o_data_all` 并行端口（见 4.1.4）。
- `upsample` 用一个模 \(L\) 计数器实现零插值：每 \(L\) 拍取 1 次真实样本、其余补 0；`gp_phase` 用额外 `dff` 把样本延后到目标相位。
- 两者的计数器位宽写法不同：`shift_register` 用 `$clog2(gp_nr_stages)`，`upsample` 用 `$clog2(gp_nr_stages)+1`；这会影响 `o_shift_done` 能否在 2 的幂级数下生效（见 4.1.5 练习 2）。
- 复用模式：CIC 抽取/插值的微分段用 `shift_register` 做 \(z^{-D}\)；CIC 插值核心用 `upsample` 做零插值（跑快时钟）；多相换向器用 `shift_register` 做相位对齐，并把 `o_shift_done` 当使能。

## 7. 下一步学习建议

- **横向**：若想再看一份几乎相同的实现，对比 [.drl_src_code/filt_ppd/rtl/shift_register.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/shift_register.v) 与本讲的 `filt_cicd` 版，体会「同一原语多目录复制」的工程取舍。
- **向前进 u3**：进入 FIR 滤波器。FIR 的抽头-延迟线本质上就是一条 `shift_register`，你会在这里第一次看到原语被大规模复用。
- **向前进 u4**：进入 CIC 滤波器，本讲提到的「微分段 `shift_register`」与「`upsample` 跑快时钟」将完整出现在 `filt_cicd` / `filt_cici` 的数据流里。
- **向前进 u5**：进入多相滤波器，本讲的 `commutator` 相位对齐将展开成换向器的完整设计。
