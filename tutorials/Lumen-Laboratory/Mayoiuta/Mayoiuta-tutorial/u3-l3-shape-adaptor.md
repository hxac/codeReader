# 动态形状适配 Shape Adaptor

## 1. 本讲目标

本讲承接 u2-l2（卷积引擎），从「卷积怎么算」上溯到「卷积算完之后输出有多大」这一上游问题。

真实神经网络里，每一层卷积的输入尺寸、卷积核大小、步长（stride）、补零（padding）都可能不同；如果硬件把这些尺寸写死，换一个模型就得重新流片。`Shape_Adaptor` 就是用一小段电路，在每个时钟边沿**动态算出当前这层卷积的输出特征图尺寸**，并附带两个保护信号：是否需要补零、输出是否越界。

学完本讲，你应当能够：

- 用卷积输出尺寸公式 \(O=\lfloor (W+2P-K)/S\rfloor+1 \) 手算任意输入对应的输出边长。
- 解释 `pad_enable` 的触发条件，并指出它只是一种启发式判断、并非严格的补零需求判定。
- 说出 `MAX_DIM` 边界保护的设计意图，以及它由于非阻塞赋值而「滞后一拍」的隐患。
- 准确指出本模块最关键的「待确认」问题：`STRIDE`、`PAD` 两个标识符在源码里被反复使用却从未声明。
- 完成实践任务：给运行时 `configure` 任务增加 `dilation`（膨胀）参数，并据此改写输出尺寸公式。

## 2. 前置知识

阅读本讲前，建议你已经理解以下概念（u1、u2、u3 前序讲义已建立）：

- **卷积（convolution）**：用一个 \(K\times K\) 的小核作为滑动窗口扫过特征图，每个窗口位置做一次乘累加，得到一个输出点。详见 u2-l2。
- **特征图（feature map）尺寸 \(W\times H\)**：输入特征图的宽度与高度。
- **步长 stride（\(S\)）**：窗口每次横向/纵向移动的格数。\(S=1\) 逐格滑动，\(S=2\) 隔一格滑一次。
- **补零 padding（\(P\)）**：在特征图四周补几圈 0，用来控制输出尺寸（"same" 补零可让输出与输入等大）。
- **Verilog 时序逻辑**：`always @(posedge clk)` 与非阻塞赋值 `<=` 的语义——本讲的边界保护隐患正源于此。
- **Verilog `task`**：一段可被调用的子程序，本讲的 `configure` 用它来「运行时改参数」。

> 提醒：本仓库是**源码阅读型**项目，不提供仿真脚手架。本讲涉及的所有数值结论标注为「待本地验证」，需你自建 testbench 才能确认。

## 3. 本讲源码地图

本讲只涉及一个文件、一个模块：

| 文件 | 模块 | 作用 |
|---|---|---|
| `hardware/rtl/control/shape_adaptor.v` | `Shape_Adaptor` | 根据输入尺寸、卷积核、`STRIDE/PAD` 动态算出输出尺寸、检测是否需补零、做越界保护，并提供运行时 `configure` 任务 |

它在 NPU 数据通路中的位置：位于「控制」域（`control/`），是一个**控制/配置类**模块，本身不搬运或计算特征数据，而是把「这一层应该输出多大」这一元信息算给下游（如 u2-l2 的卷积引擎、u3-l2 的数据重排）使用。

## 4. 核心概念与源码讲解

> 本讲只有一个最小模块 `Shape_Adaptor`，但它的源码可清晰拆成三段逻辑：**输出尺寸计算**、**自动补零检测**、**边界保护 + 运行时配置**。我们先看整体，再逐段精读。

### 4.1 Shape_Adaptor

#### 4.1.1 概念说明

`Shape_Adaptor` 要解决的痛点是：**NPU 不能假设输入形状固定**。

- 一个能跑 ResNet 的 NPU，第一层可能是 \(224\times224\)，中间层可能是 \(56\times56\)，步长从 1 变到 2。
- 如果输出尺寸由软件每次算好再下发，会增加驱动与硬件的握手开销。
- 于是本模块把它做进硬件：把 `in_height/in_width/kernel_h/kernel_w` 当输入端口，**每个时钟边沿**自动算出 `out_height/out_width`，并同时给出 `pad_enable`（是否建议补零）。

它一共承担三件事：

1. **算输出尺寸**：套用标准卷积输出公式。
2. **检测补零**：用一个简化条件给出 `pad_enable` 标志。
3. **越界保护**：当算出的输出超过 `MAX_DIM`（默认 4096），报警并把输出钳位到上限。

此外还提供一个 `configure` 任务，意图在运行时改写 `STRIDE/PAD`——但正是这个任务暴露了本模块最大的「待确认」缺陷（见 4.1.3）。

#### 4.1.2 核心流程

每个时钟上升沿（`posedge clk`），模块同步完成三步：

```text
clk ↑
 ├─ ① 算输出尺寸
 │     out_height <= (in_height + 2*PAD - kernel_h) / STRIDE + 1
 │     out_width  <= (in_width  + 2*PAD - kernel_w) / STRIDE + 1
 ├─ ② 算补零标志
 │     pad_enable <= (in_height 不能被 STRIDE 整除 或 in_width 不能被 STRIDE 整除) ? 1 : 0
 └─ ③ 越界保护
       若 out_height > MAX_DIM 或 out_width > MAX_DIM：
            打印错误，并把两者都钳位到 MAX_DIM
```

输出尺寸的核心是卷积公式：

\[
O = \left\lfloor \frac{W + 2P - K}{S} \right\rfloor + 1
\]

其中 \(W\) 为输入边长、\(K\) 为卷积核边长、\(P\) 为补零圈数、\(S\) 为步长。直觉上：分子 \(W+2P-K\) 是「补零后能被核覆盖的有效跨度」，除以 \(S\) 得到能放多少个窗口，再加 1 是因为第一个窗口放在第 0 格。

`configure` 任务则是独立于时钟的「配置入口」，被调用时把新 `STRIDE/PAD` 写入模块（设计意图如此，实际能否综合见 4.1.3）。

#### 4.1.3 源码精读

**模块声明与端口**（[shape_adaptor.v:1-13](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/control/shape_adaptor.v#L1-L13)）：

```verilog
module Shape_Adaptor #(
    parameter MAX_DIM = 4096
)(
    input  wire [15:0] in_height, in_width,   // 输入特征图高/宽
    input  wire [15:0] kernel_h,  kernel_w,   // 卷积核高/宽
    output reg  [15:0] out_height, out_width, // 输出特征图高/宽（寄存输出）
    output reg         pad_enable             // 是否建议补零
    ...
);
```

这段定义了唯一的参数 `MAX_DIM = 4096`（输出边长上限），以及 4 个 16 位输入和 3 个输出。**注意：端口与参数列表里没有 `STRIDE`、也没有 `PAD`**——这一点是后文所有「待确认」问题的根源，请先记下。

**① 输出尺寸计算**（[shape_adaptor.v:17-19](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/control/shape_adaptor.v#L17-L19)）：这两行就是公式 \(O=\lfloor (W+2P-K)/S\rfloor+1\) 的直接翻译，高、宽各算一次。`/` 在 Verilog 中对整数做截断除法（向零取整），正好对应公式里的下取整 \(\lfloor\cdot\rfloor\)。代入一组数验证（待本地验证）：

- \(W=32, K=3, P=1, S=1\)：\((32+2-3)/1+1 = 31+1 = 32\)（"same" 补零，输出与输入等大）。
- \(W=32, K=3, P=0, S=1\)：\((32-3)/1+1 = 30\)（"valid"，不补零，输出缩小）。
- \(W=32, K=3, P=1, S=2\)：\((32+2-3)/2+1 = 15+1 = 16\)（步长 2，输出减半）。

**② 自动补零检测**（[shape_adaptor.v:21-23](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/control/shape_adaptor.v#L21-L23)）：

```verilog
pad_enable <= ((in_height % STRIDE != 0) ||
               (in_width  % STRIDE != 0)) ? 1'b1 : 1'b0;
```

它判断的是「输入边长能否被步长整除」。例如 \(S=2\) 时：`in_height=32` → 32%2=0 → `pad_enable=0`；`in_height=33` → 33%2=1 → `pad_enable=1`。这里有两点要提醒：

- **运算符优先级**：`%` 优先级高于 `!=`，所以 `in_height % STRIDE != 0` 等价于 `(in_height % STRIDE) != 0`，写法本身没问题。
- **语义是启发式**：真正「是否需要补零」取决于 \((W+2P-K)\) 能否被 \(S\) 整除、以及核是否还在图内，而不是 \(W\) 能否被 \(S\) 整除。所以 `pad_enable` 只是一个粗略的「这块特征图对当前步长是否齐整」的提示，**不能等同于严格的补零需求**——这是本模块的一处设计简化。

**③ 边界保护**（[shape_adaptor.v:25-30](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/control/shape_adaptor.v#L25-L30)）：

```verilog
if (out_height > MAX_DIM || out_width > MAX_DIM) begin
    $display("Error: Output dimension exceeds maximum limit!");
    out_height <= MAX_DIM;
    out_width  <= MAX_DIM;
end
```

意图很清楚：输出太大就钳到 4096。但这里藏着一个**非阻塞赋值的时序陷阱**——

- 第 18-19 行用 `<=` 给 `out_height/out_width` 赋了新值，但非阻塞赋值要到本时钟沿结束时才生效。
- 第 26 行紧接着读 `out_height > MAX_DIM`，读到的还是**上一拍的旧值**，不是本拍刚算出来的新值。
- 结果：边界判断**滞后一拍**。当本拍算出越界值时，本拍不会钳位（因为判断用的是旧值），要到下一拍才可能反应。这是真实的时序 bug，待本地验证其影响。

另外，第 27 行的 `$display` 是**仿真专用语句，不可综合**，上硅/综合时需移除或换成中断/状态寄存器。

**④ 运行时 configure 任务**（[shape_adaptor.v:33-40](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/control/shape_adaptor.v#L33-L40)）：

```verilog
task configure(
    input [15:0] new_stride,
    input [15:0] new_pad
);
    STRIDE = new_stride;
    PAD    = new_pad;
endtask
```

设计意图是提供一个「运行时改步长和补零」的入口，避免重新综合。但这段把本模块最严重的问题彻底暴露——

> 🔴 **头号「待确认」问题：`STRIDE` 与 `PAD` 从未声明。**

- 第 18、19、22、23 行的算式、以及第 38、39 行的赋值，都在用 `STRIDE` 和 `PAD`。
- 但翻遍第 1-13 行的参数列表与端口列表，**既没有 `parameter STRIDE/PAD`，也没有 `input/reg STRIDE/PAD`**。
- 在 Verilog 中，未声明的标识符会被当成**隐式 1 位线网（implicit wire）**，于是 `2*PAD`、`/STRIDE`、`%STRIDE` 全部基于 1 位值参与运算，公式不可能按预期工作；第 38-39 行给一个 1 位线网赋 16 位值也会截断。
- 作为对照，u2-l2 的 `conv_engine.v` 第 3-4 行明确写了 `parameter STRIDE = 1, parameter PAD = 1`（见 [conv_engine.v:3-4](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/conv_engine.v#L3-L4)）。`Shape_Adaptor` 漏掉了同样的声明。

还有一个**语义矛盾**：即便补上 `parameter STRIDE/PAD`，`parameter` 是**编译期常量**，运行时不能被 `task` 赋值（第 38-39 行非法）。要让 `configure` 真正可运行，`STRIDE/PAD` 应声明为 `reg [15:0]`（运行时可变），而独立的 `task` 调用通常也不可综合——更可综合的写法是把配置做成一个带 `valid` 的同步写端口。因此「运行时配置」目前停留在**示意层面，待确认**。

此外还有一处小遗漏：端口里有 `rst_n`，但整个 `always` 块**没有复位分支**（没有 `if (!rst_n) ...`），上电时各 `reg` 输出处于不确定态，待确认是否需要补齐复位逻辑。

#### 4.1.4 代码实践

**实践目标**：给 `configure` 任务增加 `dilation`（膨胀/空洞）参数，并据此改写输出尺寸公式，同时保留 `MAX_DIM` 边界保护；顺带修掉头号「待确认」问题（声明缺失的参数）。

**背景：什么是 dilation？** 膨胀卷积让卷积核的各权重之间留出间隔，从而在不增加参数量和计算量的前提下扩大感受野。膨胀率为 \(d\) 时，核内相邻权重的间距为 \(d-1\) 个位置，等效核边长变为：

\[
K_{\text{eff}} = d\,(K-1) + 1
\]

把 \(K_{\text{eff}}\) 代回标准公式，得到带膨胀的输出尺寸：

\[
O = \left\lfloor \frac{W + 2P - K_{\text{eff}}}{S} \right\rfloor + 1
= \left\lfloor \frac{W + 2P - \bigl(d(K-1)+1\bigr)}{S} \right\rfloor + 1
\]

**操作步骤**（本仓库为源码阅读型项目，下列改动请在你自己的副本/testbench 上进行，**不要修改仓库源码**）：

1. 在模块声明处补齐缺失的参数声明，并把需要运行时可变的改为 `reg`（示例代码）：

   ```verilog
   // 示例代码（非项目原有代码）：声明此前缺失的 STRIDE/PAD，并新增 DILATION
   parameter MAX_DIM = 4096;
   reg [15:0] STRIDE = 1;     // 运行时可配置，故用 reg
   reg [15:0] PAD    = 0;
   reg [15:0] DILATION = 1;   // 新增：膨胀率，默认 1（即普通卷积）
   ```

2. 改写输出尺寸算式，把 `kernel_h/kernel_w` 换成等效核尺寸（示例代码）：

   ```verilog
   // 示例代码（非项目原有代码）：带 dilation 的输出尺寸
   // 等效核尺寸 DILATION*(kernel-1)+1
   out_height <= (in_height + 2*PAD - (DILATION*(kernel_h - 16'd1) + 16'd1)) / STRIDE + 16'd1;
   out_width  <= (in_width  + 2*PAD - (DILATION*(kernel_w - 16'd1) + 16'd1)) / STRIDE + 16'd1;
   ```

3. 扩展 `configure` 任务接口，多接一个 `new_dilation`（示例代码）：

   ```verilog
   // 示例代码（非项目原有代码）：configure 增加 dilation 参数
   task configure(
       input [15:0] new_stride,
       input [15:0] new_pad,
       input [15:0] new_dilation
   );
       STRIDE   = new_stride;
       PAD      = new_pad;
       DILATION = new_dilation;
   endtask
   ```

4. 边界保护（第 25-30 行）**保持不变**——它对 `MAX_DIM` 的钳位逻辑与是否有 dilation 无关，这正是把它独立成一段的好处。

**需要观察的现象**：

- 用 \(W=32, K=3, P=1, S=1, d=2\) 手算：\(K_{\text{eff}}=2\times2+1=5\)，\(O=(32+2-5)/1+1=30\)。
- 用 \(W=32, K=3, P=0, S=1, d=1\)（退化为普通卷积）：\(O=(32-3)/1+1=30\)，应与未改公式前的 "valid" 结果一致，说明 `DILATION=1` 时新公式向后兼容。

**预期结果**：`DILATION=1` 时输出尺寸与原公式完全一致；`DILATION>1` 时输出随等效核变大而相应缩小。由于仓库无仿真环境，上述数值**待本地验证**——你需自建 testbench、把 `STRIDE/PAD/DILATION` 声明为 `reg` 后驱动 `configure`，并打印 `out_height/out_width` 对照手算值。

#### 4.1.5 小练习与答案

**练习 1**：输入 \(56\times56\)，核 \(3\times3\)，\(P=1, S=2\)，求输出尺寸。

> **答**：\(O=(56+2-3)/2+1 = 55/2+1 = 27+1 = 28\)。输出 \(28\times28\)（待本地验证）。

**练习 2**：若把 `MAX_DIM` 从 4096 改成 16，输入 \(32\times32\)、核 3、\(P=1, S=1\)，`out_height` 最终会被钳位吗？

> **答**：算出来的 `out_height = 32`，大于 `MAX_DIM = 16`，**本拍**因非阻塞赋值读旧值未必立刻钳位，但**下一拍**会进入钳位分支，最终稳定在 16。这正好体现了 4.1.3 指出的「滞后一拍」隐患（待本地验证）。

**练习 3**：为什么不能把 `STRIDE/PAD` 声明成 `parameter` 又同时用 `task` 给它们赋值？

> **答**：`parameter` 是编译期（elaboration-time）常量，综合后就是固定连线，运行时不可修改；而 `task configure(...)` 的语义是「在电路运行起来之后改写它们」，两者矛盾，综合器会报错。要支持运行时配置，应把它们声明为 `reg [15:0]`，并把配置做成带握手（如 `cfg_valid`）的同步写端口，而不是独立的 `task` 调用。

## 5. 综合实践

把本讲的「公式 + 补零检测 + 边界保护 + 运行时配置」四件事串起来，完成下面这个小任务：

**场景**：你正在为一层卷积配置 `Shape_Adaptor`。输入特征图 \(64\times64\)，卷积核 \(5\times5\)，要求输出为 \(32\times32\)。

1. **求参数**：先固定 \(S=2\)，反推需要多大的 \(P\)。提示：解方程 \((64+2P-5)/2+1 = 32\)，得 \(64+2P-5 = 62\)，\(2P=3\)。说明在整数 stride/padding 下无法精确得到 32，需向上取整 \(P=2\)，算出实际输出 \((64+4-5)/2+1 = 63/2+1 = 31+1 = 32\)，恰好命中。
2. **判断 pad_enable**：\(64 \% 2 = 0\)，故 `pad_enable = 0`（输入对步长齐整）。请思考：即便 `pad_enable=0`，我们仍然设了 \(P=2\)——这是否印证了 4.1.3 中「`pad_enable` 是启发式、不等于真实补零需求」的结论？
3. **配置模块**：写出对应的 `configure` 调用（示例代码），传入 `new_stride=2, new_pad=2`。
4. **加 dilation**：若改用 \(d=2\) 的膨胀卷积，\(K_{\text{eff}}=2\times4+1=9\)，在同样 \(P=2, S=2\) 下重算输出 \((64+4-9)/2+1 = 59/2+1 = 29+1 = 30\)，输出降为 \(30\times30\)。记录这一变化。
5. **边界检查**：确认上述所有输出都远小于 `MAX_DIM=4096`，不会触发钳位。

**交付物**：一张表，列出「(S, P, d) → (K_eff, out_size, pad_enable, 是否触发 MAX_DIM)」的对照；并写一段话说明本模块当前不能直接仿真运行的原因（`STRIDE/PAD` 未声明、`task`/`$display` 不可综合、复位缺失）。

## 6. 本讲小结

- `Shape_Adaptor` 用一个时钟同步的 `always` 块，把卷积输出尺寸公式 \(O=\lfloor(W+2P-K)/S\rfloor+1\) 做进硬件，让 NPU 能适配任意形状的卷积层。
- `pad_enable` 判断的是「输入边长能否被步长整除」，只是启发式提示，**不等于**真实的补零需求。
- `MAX_DIM`（默认 4096）提供越界钳位保护，但由于非阻塞赋值，判断**滞后一拍**；`$display` 不可综合。
- 头号「待确认」问题：`STRIDE`、`PAD` 在源码中被使用和赋值却**从未声明**，对比 `conv_engine.v:3-4` 可知应补上声明。
- 运行时 `configure` 任务与 `parameter` 常量语义冲突，且 `task` 通常不可综合，「运行时配置」目前仅为示意，待确认。
- 实践中给模块增加了 `dilation` 参数，把输出公式升级为 \(O=\lfloor(W+2P-d(K-1)-1)/S\rfloor+1\)，并验证 `d=1` 时向后兼容。

## 7. 下一步学习建议

- **横向对比**：回头读 u2-l2 的 `conv_engine.v`（[conv_engine.v:3-4](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/core/conv_engine.v#L3-L4)），看它如何声明并（未）使用 `STRIDE/PAD` 参数，理解「形状信息到底由谁供给卷积引擎」这一上下游关系。
- **向上看架构**：进入 u4-l3（全系统数据通路与集成），把 `Shape_Adaptor` 放回 SoC→存储→重排→计算→写回的整条链路中，理解它作为「控制/配置」模块的位置。
- **补一个 testbench**：本讲所有数值都标注「待本地验证」。建议为本模块写一个最小的 `tb_shape_adaptor`，先把 `STRIDE/PAD` 声明为 `reg`、用 `configure` 驱动，再逐组核对 `out_height/out_width/pad_enable`，亲手验证公式与边界保护的时序行为。
- **思考扩展**：真实 NPU 还要处理 dilation、group convolution、动态输入分辨率。本讲的 dilation 实践是迈向这些扩展的第一步，可作为二次开发的切入点。
