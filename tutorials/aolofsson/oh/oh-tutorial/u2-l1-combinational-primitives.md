# 组合逻辑原语：多路选择器与逻辑门

> 所属单元：u2 stdlib 基础原语 · 依赖前置讲义：[u1-l4 Verilog 2005 与 OH! 编码规范](u1-l4-coding-style.md)

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「组合逻辑（combinational logic）」是什么，以及它与时序逻辑的区别。
- 读懂 `stdlib` 中两类最基础的可综合原语：**逻辑门**（`oh_and2`/`oh_or2`/`oh_xor2`/`oh_inv`）与**多路选择器**（`oh_mux`/`oh_mux2/3/4`/`oh_mux8`）。
- 写出 OH! 风格的**参数化**组合逻辑：用 `parameter` 控制位宽，用 `{(N){sel}}` 做位扩展。
- 区分三组容易混淆的器件家族：`oh_mux*`（one-hot 选择器）、`oh_mx*`（按位掩码选择器）、`oh_mxi*`（反相输出的掩码选择器）。
- 按 Coding Guide 写出一个带 `default` 分支的 `casez` 风格选择器。

本讲覆盖两个最小模块：**参数化 mux** 与**逻辑门**。

## 2. 前置知识

### 2.1 组合逻辑 vs 时序逻辑

- **组合逻辑**：输出只取决于**当前这一刻**的输入，没有记忆、没有时钟。只要输入变，输出立刻（经过门延时）变。本讲的与门、选择器都是组合逻辑。
- **时序逻辑**：输出取决于时钟沿到来时锁存的**历史状态**，比如触发器（下一讲 u2-l2 讲）。判断方法：模块里出现 `always @(posedge clk)` 或真正的寄存器，就是时序逻辑。

本讲所有模块的输出都由 `assign` 一句话算出，没有时钟，是最纯粹的组合逻辑。

### 2.2 Verilog 的位运算与拼接

读懂本讲源码，需要先熟悉这几个 Verilog 2005 运算符（u1-l4 已锁定全库用这个标准）：

| 写法 | 含义 | 例子 |
| --- | --- | --- |
| `&` `|` `^` `~` | 按位 与/或/异或/取反 | `a & b` 逐位相与 |
| `{a, b}` | 拼接（concatenation） | `{2'b01, 2'b10}` = `4'b0110` |
| `{(N){x}}` | 把 `x` 复制 N 份再拼接 | `{(4){1'b1}}` = `4'b1111` |
| `in[hi:lo]` | 位切片 | `in[7:0]` 取低 8 位 |
| `in[base -: W]` | 从 base 起向下取 W 位 | `in[31 -: 8]` ≡ `in[31:24]` |

其中 `{(N){sel}}` 是本讲最关键的模式：它把 1 比特的 `sel` 复制成 N 比特，从而让 1 比特的选择信号能和 N 比特的数据做按位与。

### 2.3 one-hot 编码 vs 二进制编码

一个 4 选 1 选择器需要告诉它「选第几个」，有两种常见编码：

- **二进制（binary）**：用 2 根线，`00/01/10/11` 分别代表选 0/1/2/3。
- **one-hot（独热）**：用 4 根线，同一时刻只有 1 根为 1，`0001/0010/0100/1000` 分别代表选 0/1/2/3。

OH! 的 `oh_mux*` 家族**几乎全部采用 one-hot 选择**，这是理解它们实现写法的前提。读者要带着这个区别往下读。

### 2.4 参数化（承接 u1-l4）

OH! 用 `#(parameter ...)` 把位宽做成参数，同一个模块即可用于 1 位、8 位、32 位。本讲你会看到两种参数命名习惯（库内并不完全统一，这也是真实的源码现状）：

- `N`：常用于 `oh_mux*`（向量宽度）和 `oh_and2`（块宽度）。
- `DW`（data width）：常用于 `oh_mx*`、`oh_or2`、`oh_xor2`（数组/数据宽度）。

含义相同，只是历史命名不一致。读源码时以端口实际位宽 `[N-1:0]` / `[DW-1:0]` 为准。

## 3. 本讲源码地图

| 文件 | 作用 | 关键点 |
| --- | --- | --- |
| `stdlib/rtl/oh_and2.v` | 2 输入与门 | 带 `SYN/TYPE` 的 soft/hard 双实现骨架 |
| `stdlib/rtl/oh_or2.v` | 2 输入或门 | 最简 `assign` 写法 |
| `stdlib/rtl/oh_xor2.v` | 2 输入异或门 | 最简 `assign` 写法 |
| `stdlib/rtl/oh_inv.v` | 取反（非门） | 单输入 |
| `stdlib/rtl/oh_mux.v` | **通用 N:1 one-hot 选择器** | 参数化 `M`（向量数）×`N`（位宽），`for` 循环实现 |
| `stdlib/rtl/oh_mux4.v` | 4:1 one-hot 选择器 | 固定 4 路，带仿真期 one-hot 断言 |
| `stdlib/rtl/oh_mux8.v` | 8:1 one-hot 选择器 | 与 `oh_mux4` 同模板，对比用 |
| `stdlib/rtl/oh_mx2.v` | 2 输入**按位掩码**选择器 | `s` 是位掩码，逐位独立选择 |
| `stdlib/rtl/oh_mxi2.v` | 反相输出的掩码选择器 | 与 `oh_mx2` 对比 |

## 4. 核心概念与源码讲解

### 4.1 逻辑门原语：oh_and2 / oh_or2 / oh_xor2 / oh_inv

#### 4.1.1 概念说明

逻辑门是最底层的组合逻辑构件。OH! 把常用的与/或/异或/非都封装成独立模块，每个都**参数化位宽**——比如 `oh_and2 #(.N(8))` 就是一个 8 位的两输入与门。这样做的好处是：上层设计（elink、gpio 等）只管例化 `oh_and2`，位宽交给参数决定，写法统一、便于替换为 ASIC 标准单元（见 u9-l1 的 soft/hard 双实现）。

#### 4.1.2 核心流程

对宽度为 \(W\) 的两输入按位运算，输出第 \(j\) 位只依赖两个输入的第 \(j\) 位：

\[
z_j = a_j \;\square\; b_j,\qquad \square \in \{\wedge,\vee,\oplus\},\quad 0\le j < W
\]

其中 \(\wedge\) 是与、\(\vee\) 是或、\(\oplus\) 是异或。Verilog 的 `&`/`|`/`^` 正好一一对应，所以核心实现就是一行 `assign`。

#### 4.1.3 源码精读

先看最简单的异或门 `oh_xor2`——全文件的核心只有一行：

```verilog
module oh_xor2 #(parameter DW = 1 ) // array width
   (
    input [DW-1:0]  a,
    input [DW-1:0]  b,
    output [DW-1:0] z
    );
   assign z =  a ^ b;          // 按位异或，DW 位
endmodule
```

参考 [stdlib/rtl/oh_xor2.v:7-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_xor2.v#L7-L16)，这段代码定义了端口宽度由 `DW` 决定的两输入异或门。`oh_or2` 几乎一模一样，只是把 `^` 换成 `|`，见 [stdlib/rtl/oh_or2.v:14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_or2.v#L14)；单输入的取反 `oh_inv` 用 `~a`，见 [stdlib/rtl/oh_inv.v:13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_inv.v#L13)。

再看 `oh_and2`，它比另外两个多了一套 soft/hard 双实现骨架：

```verilog
module oh_and2  #(parameter N  = 1,
                  parameter SYN  = "TRUE",
                  parameter TYPE = "DEFAULT")
   ( input [N-1:0] a, input [N-1:0] b, output [N-1:0] z );

   generate
      if(SYN == "TRUE")  begin
         assign z = a & b;          // soft：可综合 RTL
      end
      else begin
         // ... 在 else 分支里再次例化 oh_and2（占位，见下文说明）
      end
   endgenerate
endmodule
```

参考 [stdlib/rtl/oh_and2.v:8-33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v#L8-L33)。要点有三：

1. **soft 分支**（`SYN=="TRUE"`）就是 `assign z = a & b;`——和 `oh_xor2` 一样朴素。
2. **`SYN/TYPE` 参数**：这是 OH! 切换 soft（可综合 RTL）与 hard（ASIC 硬核）的统一开关，机制详见 u1-l4、u9-l1。`build.sh` 默认 `-DCFG_ASIC=0`，全部走 soft。
3. **hard 分支是占位**：注意 [stdlib/rtl/oh_and2.v:22-31](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v#L22-L31) 的 `else` 分支例化的还是 `oh_and2` 自身（理论上应换成 `asic_and2`）。由于默认与绝大多数调用都让 `SYN=="TRUE"`，这个分支不会被展开，所以不影响仿真与综合——它印证了 u1-l1 提到的「stdlib 的 hard 实现多为占位/施工区」，读源码时遇到 `//TODO`、自例化等不必奇怪。

> ⚠️ 真实源码现状：stdlib 的逻辑门有的带 `SYN/TYPE`（如 `oh_and2` 用参数 `N`），有的不带（如 `oh_xor2`/`oh_or2` 用参数 `DW`）。这种命名不统一是库的历史现状，读源码以端口位宽为准。

#### 4.1.4 代码实践

**目标**：直观感受「参数化位宽」对同一个门的作用。

**步骤**：

1. 打开 [stdlib/rtl/oh_xor2.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_xor2.v)，确认 `DW` 默认为 1。
2. 在纸上（或编辑器里）手写两个例化：
   - `oh_xor2 #(.DW(1)) g1 (.a(1'b1), .b(1'b0), .z(z1));`
   - `oh_xor2 #(.DW(8)) g8 (.a(8'hFF), .b(8'h0F), .z(z8));`
3. 推算输出。

**预期结果**：

- `z1 = 1'b1`（1 位异或：1 ^ 0 = 1）。
- `z8 = 8'hF0`（逐位异或：`1111_1111 ^ 0000_1111 = 1111_0000`）。

**现象观察**：同一个模块只改一个参数，就从 1 位门变成 8 位门，这正是参数化的威力。如果你已按 u1-l3 装好 iverilog，可以把这两句包进一个小 testbench 用 `iverilog -g2005` 编译验证；若尚未装好，标注**待本地验证**即可。

#### 4.1.5 小练习与答案

**练习 1**：用 `oh_or2` 和 `oh_inv` 组合出一个 2 输入或非门（NOR）的等价行为，输出 `z = ~(a | b)`。

**参考答案**：把 `oh_or2` 的输出接到 `oh_inv` 的输入：

```verilog
wire [DW-1:0] or_out;
oh_or2  #(.DW(DW)) u_or  (.a(a), .b(b), .z(or_out));
oh_inv  #(.DW(DW)) u_inv (.a(or_out), .z(z));   // z = ~(a|b)
```

（库中其实有现成的 `oh_nor2`，这里只是练习组合。）

**练习 2**：`oh_and2` 的 `SYN` 是什么类型？为什么 `if(SYN == "TRUE")` 能在 `generate` 里用作综合期分支？

**参考答案**：`SYN` 是字符串 `parameter`，默认 `"TRUE"`。`generate if` 是** elaboration 期**（综合/编译期）的条件，工具会在展开电路时只保留命中的一路，因此字符串比较等价于一个编译期开关，不会生成真正的比较器硬件。

---

### 4.2 多路选择器 oh_mux 族：one-hot 选择

#### 4.2.1 概念说明

多路选择器（multiplexer，简称 mux）从多路输入中挑一路送到输出。OH! 的 `oh_mux` 家族用 **one-hot 选择信号**：`oh_mux4` 有 4 个独立的 `sel0..sel3` 端口，哪一个为 1 就选哪一路（`oh_mux2/3/8` 同理）。还有一个**通用版** `oh_mux`，用 `M` 表示向量个数、`N` 表示每个向量的位宽，可表示任意 `M:1` 选择器。

为什么用 one-hot 而不是二进制？因为 one-hot 选择器可以写成「逐路与再或」，没有译码器、没有优先级，电路规整、延时均匀，适合数据通路；缺点是选择信号线多（M 根）。在 OH! 这种「外设寄存器写选通」场景里（一个周期只会拉高一个 `_write`），one-hot 天然契合。

#### 4.2.2 核心流程

one-hot 选择器的本质是对所有「被选中的那一一路」做按位或。对 `M` 路、每路 `N` 位的输入，输出为：

\[
out[n] = \bigvee_{i=0}^{M-1}\bigl(sel_i \wedge in_i[n]\bigr),\qquad 0\le n < N
\]

只要 `sel` 是合法 one-hot（恰好一位为 1），上式就退化为 `out = in_k`（k 是那一位）。实现上，把 1 比特的 `sel_i` 用 `{(N){sel_i}}` 扩展成 N 位，再和 `in_i` 按位与，最后全部或起来——这就是 `oh_mux4` 的写法。

通用版 `oh_mux` 把这个过程写成一个 `for` 循环，循环次数由参数 `M` 决定。

#### 4.2.3 源码精读

**先看固定 4:1 版 `oh_mux4`**，它最能体现 one-hot AND-OR 模式：

```verilog
module oh_mux4 #(parameter N = 1 )
   ( input sel3, input sel2, input sel1, input sel0,
     input [N-1:0] in3, input [N-1:0] in2,
     input [N-1:0] in1, input [N-1:0] in0,
     output [N-1:0] out );

   assign out[N-1:0] = ({(N){sel0}} & in0[N-1:0] |
                        {(N){sel1}} & in1[N-1:0] |
                        {(N){sel2}} & in2[N-1:0] |
                        {(N){sel3}} & in3[N-1:0]);
   ...
```

参考 [stdlib/rtl/oh_mux4.v:21-24](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux4.v#L21-L24)。逐句读：`{(N){sel0}}` 把 1 位的 `sel0` 复制成 N 位（例如 N=8、sel0=1 → `8'b11111111`），再和 `in0` 按位与；四路或起来送给 `out`。`oh_mux2`/`oh_mux3`/`oh_mux8` 是同一模板的 2/3/8 路版，例如 [stdlib/rtl/oh_mux2.v:17-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux2.v#L17-L18)、[stdlib/rtl/oh_mux8.v:29-36](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux8.v#L29-L36)。

`oh_mux4` 还藏了一段**只在仿真期生效**的 one-hot 断言：

```verilog
`ifdef TARGET_SIM
   wire error;
   assign error = (sel0 | sel1 | sel2 | sel3) &
                  ~(sel0 ^ sel1 ^ sel2 ^ sel3);
   always @ (posedge error)
     #1 if(error)
       $display ("ERROR at in oh_mux4 %m at ",$time);
`endif
```

参考 [stdlib/rtl/oh_mux4.v:26-35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux4.v#L26-L35)。`TARGET_SIM` 宏由 `build.sh` 的 `-DTARGET_SIM=1` 定义（见 u1-l3），综合时这段代码不存在。其含义：如果有任意一个 `sel` 为 1，但 `sel0^sel1^sel2^sel3`（异或的奇偶）不为 1，就说明同时有多个 `sel` 为 1（违反 one-hot），于是 `error` 跳变并打印错误。这是 OH! 用仿真断言保护协议约束的典型手法。

**再看通用 N:1 版 `oh_mux`**——它把固定写法换成 `for` 循环：

```verilog
module oh_mux
  #(parameter N   = 32,   // vector width（每路位宽）
    parameter M   = 2,    // number of vectors（路数）
    parameter SYN  = "TRUE", parameter TYPE = "DEFAULT")
   ( input [M-1:0] sel,
     input [M*N-1:0] in,  // 拼接输入 {...,in1,in0}
     output [N-1:0] out );

   generate
      if(SYN == "TRUE") begin
         reg [N-1:0] mux; integer i;
         always @* begin
            mux[N-1:0] = 'b0;
            for(i=0;i<M;i=i+1)
              mux[N-1:0] = mux[N-1:0] | {(N){sel[i]}} & in[((i+1)*N-1)-:N];
         end
         assign out[N-1:0] = mux[N-1:0];
      end
      else begin
         //TODO: implement   ← hard 分支同样未实现
         ...
      end
   endgenerate
endmodule
```

参考 [stdlib/rtl/oh_mux.v:8-41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux.v#L8-L41)。三个细节：

1. **拼接输入**：`in` 是 `M*N` 位的大向量，第 `i` 路用 `in[((i+1)*N-1)-:N]` 切出来，等价于 `in[(i+1)*N-1 : i*N]`。这是处理「任意路数」的标准切片技巧。
2. **`always @*` + `for`**：循环体里把每一路 `(sel[i] 扩展) & in_i` 累加或进 `mux`，与 `oh_mux4` 的四行展开等价，只是参数化了。`always @*` 是组合逻辑的阻塞赋值块（这里用 `=`，组合逻辑允许）。
3. **hard 分支是 `//TODO`**：又一次印证 stdlib 的 hard 实现多为占位。

> 💡 **一个真实的历史 bug（教学点）**：当前 HEAD（`7edfcb5`）的提交信息是 *「Fixing bug from 3 years ago!」*，它只改了 `oh_mux.v` 一行——把 [stdlib/rtl/oh_mux.v:30](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux.v#L30) 从旧的 `assign out[N-1:0] = mux[N-1];`（只取了第 `N-1` 位，再零扩展到 N 位）修正为 `assign out[N-1:0] = mux[N-1:0];`（完整范围）。旧代码在 `N>1` 时会让输出的每一位都等于 `mux` 的最高位，从而悄悄出错 3 年。教训：**位切片的范围一定要写全**，`mux[N-1]`（一位）和 `mux[N-1:0]`（N 位）天差地别。你现在看到的是修复后的版本。

**真实下游用法**：`oh_mux4` 在 gpio 里被用来在「写 / 清 / 置 / 翻转」四种 GPIO_OUT 写法之间做选择，正是 one-hot（一次只拉高一个 `_write`）的典型场景，见 [gpio/hdl/gpio.v:139-145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L139-L145)；elink 的配置回读也用它做多源选择，见 [elink/hdl/erx_cfg.v:255-261](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_cfg.v#L255-L261)。

> ⚠️ 待本地验证：注意这两处例化写成 `oh_mux4 #(.DW(N))` / `#(.DW(32))`，而 `oh_mux4` 的参数名其实是 `N`（不是 `DW`）。这是一个跨文件的命名不一致，不同仿真器对「按名覆盖不存在的参数」处理不同（有的告警并忽略）。是否实际影响行为，建议你用 iverilog 本地编一次确认。

#### 4.2.4 代码实践

**目标**：亲手验证 one-hot AND-OR 的等价性，并理解「非法 one-hot」时的输出。

**步骤**：

1. 想象一个 `oh_mux4 #(.N(8))`，输入 `in0=8'h11, in1=8'h22, in2=8'h33, in3=8'h44`。
2. 分别设以下选择信号，用公式 `out = (sel0?in0:0) | (sel1?in1:0) | (sel2?in2:0) | (sel3?in3:0)` 手算 `out`：
   - (a) `sel0=1, 其余=0`
   - (b) `sel2=1, 其余=0`
   - (c) `sel1=1, sel3=1, 其余=0`（非法 one-hot）
   - (d) 全 0

**预期结果**：

- (a) `out = 8'h11`（选 in0）。
- (b) `out = 8'h33`（选 in2）。
- (c) `out = 8'h22 | 8'h44 = 8'h66`——**两路同时选中会按位或**，这正是 `oh_mux4` 仿真断言要拦截的非法情形。
- (d) `out = 0`。

**现象观察**：(c) 说明 one-hot mux 在多 sel 同时有效时输出不是「任选一路」而是「按位或」，因此协议上必须保证一次只拉一个 sel。如果你本地有 iverilog，把上述四组放进 testbench，编译时加 `-DTARGET_SIM=1`，观察 (c) 是否打印 `ERROR ... oh_mux4`。

#### 4.2.5 小练习与答案

**练习 1**：`oh_mux`（通用版）的参数 `M` 和 `N` 分别控制什么？若要做一个「16 路、每路 4 位」的选择器，怎么例化？

**参考答案**：`M` 是向量个数（路数），`N` 是每个向量的位宽。例化为 `oh_mux #(.N(4), .M(16)) u (.sel(sel[15:0]), .in(in[63:0]), .out(out[3:0]));`，其中 `in` 共 `M*N=64` 位。

**练习 2**：为什么 `oh_mux4` 的仿真断言写成 `~(sel0 ^ sel1 ^ sel2 ^ sel3)` 而不是直接数 1 的个数？

**参考答案**：四个 1 比特异或的结果等于「1 的个数的奇偶」：1 的个数为奇数时异或为 1，为偶数（含 0）时为 0。合法 one-hot 恰好有 1 个 1（奇数），所以「有 sel 为 1（任一为 1）且异或为 0」就代表「1 的个数是偶数 ≥2」，即非法。这是一种省去「数 1」电路的奇偶判别技巧。

---

### 4.3 oh_mx 与 oh_mxi 族：按位掩码选择与反相输出

#### 4.3.1 概念说明

`oh_mx*` 是**另一类**选择器，名字像 mux 但行为完全不同。它的选择信号 `s` 不是「选第几路」的 one-hot，而是一个**和数据等宽的位掩码（mask）**，每一位独立地决定输出对应位取哪个输入。

典型用途：按字节使能的合并写。比如要把 `d1` 的若干字节合并进 `d0`，掩码 `s` 的每一位对应一个字节是否用 `d1`。这正是它被命名为 `mx`（mask-mux）而非 `mux` 的原因。

`oh_mxi*`（i = inverting）是 `oh_mx*` 的**反相输出**版本，输出多一个取反。

> 📌 厘清三个家族（这是本讲的重点区分）：
> - `oh_mux*`：**one-hot** 选择，`sel` 是 M 根线选「第几路」，输出整路数据。
> - `oh_mx*`：**按位掩码**选择，`s` 是 DW 位掩码，每一位独立选 2 个输入之一。
> - `oh_mxi*`：同 `oh_mx*`，但输出取反。

（大纲里把 `oh_mx*` 笼统称作「带反相输入版本」并不准确；反相输出的是 `oh_mxi*`。以源码为准。）

#### 4.3.2 核心流程

对 2 输入的 `oh_mx2`，输出第 \(j\) 位为：

\[
z_j = \bigl(\neg s_j \wedge d_{0,j}\bigr)\vee\bigl(s_j \wedge d_{1,j}\bigr)
\]

即 `s[j]=0` 时取 `d0[j]`，`s[j]=1` 时取 `d1[j]`，**逐位独立**。这与 `oh_mux2`「整路选择」截然不同。`oh_mxi2` 只是再多一层整体取反：\( z_j = \neg\,(\text{上式}) \)。

#### 4.3.3 源码精读

`oh_mx2` 全文核心仍是一行 `assign`：

```verilog
module oh_mx2 #(parameter DW = 1 )
   ( input [DW-1:0] d0, input [DW-1:0] d1,
     input [DW-1:0] s,  output [DW-1:0] z );
   assign z = (d0 & ~s) | (d1 & s);
endmodule
```

参考 [stdlib/rtl/oh_mx2.v:8-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mx2.v#L8-L18)。注意 `s` 的位宽是 `DW`（和数据相同），不是 1 位的「选哪一路」。`d0 & ~s` 保留 `d0` 中掩码为 0 的位；`d1 & s` 保留 `d1` 中掩码为 1 的位；两者或起来即合并结果。

`oh_mxi2` 只差一个最外层 `~`：

```verilog
   assign z = ~((d0 & ~s) | (d1 & s));
```

参考 [stdlib/rtl/oh_mxi2.v:16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mxi2.v#L16)。3 输入的 `oh_mxi3` 把它推广为多一个 `d2` 与对应掩码位，见 [stdlib/rtl/oh_mxi3.v:16-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mxi3.v#L16-L22)。

#### 4.3.4 代码实践

**目标**：用 `oh_mx2` 实现「按掩码合并两个字节」。

**步骤**：

1. 设 `d0 = 8'hAA`，`d1 = 8'h0F`，掩码 `s = 8'b0000_1111`（低 4 位用 d1，高 4 位用 d0）。
2. 手算 `z = (d0 & ~s) | (d1 & s)`。

**预期结果**：

- `d0 & ~s = 8'hAA & 8'hF0 = 8'hA0`
- `d1 & s  = 8'h0F & 8'h0F = 8'h0F`
- `z = 8'hA0 | 8'h0F = 8'hAF`

**现象观察**：输出的高 4 位来自 `d0`、低 4 位来自 `d1`，每一位由掩码独立决定。把它和 `oh_mux2`（整路二选一，输出要么全是 d0 要么全是 d1）对比，区别一目了然。本地可用 iverilog 验证；未装则标**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `oh_mx2` 的 `s` 设成全 1，输出是什么？设成全 0 呢？

**参考答案**：`s=全1` → `z = (d0 & 0) | (d1 & 1) = d1`；`s=全0` → `z = (d0 & 1) | (d1 & 0) = d0`。即掩码全 1/全 0 时退化为「整路选 d1/d0」。

**练习 2**：如何用 `oh_mx2` 实现「如果 `en=1` 则输出 `new_val`，否则输出 `old_val`」（逐位）？

**参考答案**：把单比特 `en` 扩展成 DW 位掩码作为 `s`：`wire [DW-1:0] mask = {(DW){en}};`，然后 `oh_mx2 #(.DW(DW)) u (.d0(old_val), .d1(new_val), .s(mask), .z(z));`。当 `en=1`，掩码全 1，输出 `new_val`；`en=0` 输出 `old_val`。

---

## 5. 综合实践：写一个参数化 8:1 选择器

本任务贯穿本讲：既练**参数化**，又练 Coding Guide 要求的 `case + default`，并和库里已有的 one-hot `oh_mux8` 对照。

### 5.1 实践目标

实现一个**二进制选择**的 8:1 选择器 `my_mux8`：用 3 位 `sel`（`000`~`111`）从 8 路输入中选一路，位宽 `N` 可参数化。要求带 `default` 分支，符合 u1-l4 的 Coding Guide。

> 注意区别：库里的 [oh_mux8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux8.v) 是 **one-hot**（8 根 sel 线）。这里要你写的是 **3 位二进制** 版，正好体会两种编码的取舍。

### 5.2 操作步骤

1. 在 `oh-tutorial/` 之外（比如临时目录）新建 `my_mux8.v`，仿照 `oh_mux4` 的文件头注释风格（`//###...` 注释块 + 功能说明 + License 注释）。
2. 端口定义：`input [2:0] sel`，`input [N-1:0] in0..in7`，`output [N-1:0] out`，参数 `#(parameter N = 1)`。
3. 用 `casez`（`docs/verilog_reference.md` 第 537–587 行介绍了 case/casez/casex 的差别）实现选择，务必带 `default`。
4. （可选）用两级 4:1 思路实现另一种版本：第一级用两个 `oh_mux4` 在 `in0..in3` 和 `in4..in7` 里各选一路，第二级再用一个 `oh_mux4`/`oh_mx2` 按 `sel[2]` 二选一。比较两种写法的可读性。

### 5.3 参考实现（示例代码，非项目原有文件）

```verilog
// 示例代码：二进制 3 位 sel 的 8:1 选择器
module my_mux8 #(parameter N = 1) (
    input  [2:0]   sel,
    input  [N-1:0] in0, in1, in2, in3, in4, in5, in6, in7,
    output reg [N-1:0] out
);
    always @* begin
        casez (sel)
            3'b000:  out = in0;
            3'b001:  out = in1;
            3'b010:  out = in2;
            3'b011:  out = in3;
            3'b100:  out = in4;
            3'b101:  out = in5;
            3'b110:  out = in6;
            3'b111:  out = in7;
            default: out = {N{1'b0}};   // Coding Guide 要求的 default
        endcase
    end
endmodule
```

### 5.4 需要观察的现象与预期结果

- 遍历 `sel` 从 `000` 到 `111`，`out` 应依次等于 `in0..in7`。
- `case` 用的是二进制精确匹配，这里其实用普通 `case` 即可；`casez` 的价值在含 `z`（don't-care）位时才体现。本练习若改写成「`sel[2]=1` 时一律选 in7」之类含 don't-care 的分支，才能看出 `casez` 与 `case` 的区别。
- `out` 声明为 `reg`（因为在 `always` 里赋值），但整个模块仍是**组合逻辑**（`always @*`，无时钟）——这是 Verilog 初学者常困惑的点：`reg` 不等于时序逻辑。

### 5.5 验证方式

若有 iverilog（见 u1-l3）：写一个简单 testbench 遍历 `sel`，用 `iverilog -g2005 -DTARGET_SIM=1` 编译并运行，用 `$display` 打印 `out` 核对。若环境未就绪，至少完成「手写 + 纸面推演」，并标注**待本地验证**。

## 6. 本讲小结

- 组合逻辑没有时钟、输出只取决于当前输入；本讲的门与选择器都是 `assign`/`always @*` 一句话算出的组合逻辑。
- 逻辑门 `oh_and2`/`oh_or2`/`oh_xor2`/`oh_inv` 是参数化（`N` 或 `DW`）的按位运算；其中 `oh_and2` 带 `SYN/TYPE` 双实现骨架，但 hard 分支多为占位。
- `oh_mux*` 家族是 **one-hot** 选择器，核心写法是 `{(N){sel}} & in` 的 AND-OR；`oh_mux` 通用版用 `for` 循环 + `in[((i+1)*N-1)-:N]` 切片实现任意 `M:1`。
- `oh_mux4` 内置 `TARGET_SIM` 仿真断言来拦截非法 one-hot（多个 sel 同时有效会按位或）。
- `oh_mx*` 是**按位掩码**选择器（`s` 等宽掩码、逐位独立），`oh_mxi*` 是其反相输出版——不要和 one-hot 的 `oh_mux*` 混淆。
- HEAD 提交 `7edfcb5` 刚修复了 `oh_mux.v` 一个 3 年的位切片 bug（`mux[N-1]` → `mux[N-1:0]`），提醒我们位切片范围务必写全。

## 7. 下一步学习建议

- 下一讲 **u2-l2 时序原语：触发器家族** 将进入带时钟的器件（`oh_dffq`/`oh_dffrq` 等），与本讲的组合逻辑互补，建议紧接着读。
- 想巩固本讲，可以先翻 [stdlib/rtl/oh_mux8.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux8.v) 和 `oh_mux12.v`，验证它们是否都遵循同一 AND-OR 模板。
- 想看 one-hot mux 的真实用途，可先扫一眼 [gpio/hdl/gpio.v:139-145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio.v#L139-L145)（GPIO_OUT 的写/清/置/翻转选择），这会在 u6-l2 GPIO 模块全解析里详细展开。
