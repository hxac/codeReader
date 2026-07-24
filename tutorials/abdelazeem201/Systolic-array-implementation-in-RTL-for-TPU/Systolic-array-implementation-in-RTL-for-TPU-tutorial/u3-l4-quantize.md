# 后处理量化 quantize（饱和与定点截断）

## 1. 本讲目标

本讲精读 TPU 数据通路中紧跟在脉动阵列后面的小而关键的模块 `quantize`。读完本讲你应该能够：

- 说清 `quantize` 在整条数据通路中的位置，以及它把什么样的位宽变成什么样的位宽；
- 解释为什么把 21bit 的累加结果「截断」成 16bit 时，需要先用 `max_val / min_val` 做饱和（saturation / clamping），而不能直接砍高位；
- 看懂 `quantize.v` 里那条 `for` 循环如何把 8 个输出元素一次性并行量化、再打包成 128bit 总线；
- 自己手算几个输入样例，预测 `quantize` 的输出，并指出如果删掉饱和逻辑会在什么输入下得到错误结果。

本讲承接 u2-l3：上一讲我们看到 `systolic` 模块用 gather 逻辑把 8×8 累加矩阵的 cell 挑出来拼成 168bit 的 `mul_outcome`（即顶层的 `ori_data`）。本讲就讲这条 168bit 总线进入 `quantize` 之后发生了什么。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，定点数（fixed-point number）。** 普通整数把小数点默认放在最低位右边；定点数则是我们「人为约定」小数点在中间某处。本项目用 `Qm.n` 记法表示定点数，约定 **总位数 = m + n**，其中 `m` 是整数部分位数（**包含 1 位符号位**），`n` 是小数部分位数。例如一个 8 位有符号数写成 `Q4.4`，意味着 4 位整数（含符号）+ 4 位小数，它能表示的真实数值范围是：

\[
[-2^{3},\ 2^{3}) = [-8,\ 8)
\]

最小刻度（分辨率）是 \(2^{-4} = 1/16\)。定点数的好处是：加减乘和整数完全一样，硬件无需浮点单元，只要在「该截断/该对齐小数点」的地方人为处理位数即可。

**第二，饱和（saturation）。** 当一个值超出目标格式能表示的范围时，我们不让它「绕回去」（wrap-around），而是直接「钳位」到最近的边界。例如目标是有符号 16 位（范围 \([-32768, 32767]\)），那么 \(+50000\) 饱和成 \(+32767\)，\(-50000\) 饱和成 \(-32768\)。饱和让溢出变成「可预期的最大/最小值」，而不是符号翻转的灾难。

**第三，为什么脉动阵列后面非要有这么一步。** 阵列里每个 cell 在累加 8 个乘积时，为了避免溢出，特意多留了 5 位「保护位」（guard bits，详见 u1-l4 与 u2-l2）。这 5 位让中间结果有 21 位，范围远大于最终输出的 16 位。但最终写回外部 SRAM、以及后续神经网络层都只接受 16 位。于是必须有一个模块「安全地把 21 位收缩回 16 位」——这正是 `quantize` 的职责。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [rtl/quantize.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v) | 量化模块本体，把 21bit 饱和截断为 16bit | **全部精读** |
| [rtl/systolic.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v) | 上游脉动阵列，产出 `mul_outcome`（即 `ori_data`） | 看它如何用符号扩展造出 21bit、如何 gather 出 8 个结果 |
| [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) | 顶层，把 `systolic`、`quantize`、`write_out` 串起来 | 看 `quantize` 的例化与端口连接 |

数据流主线回顾（来自 u1-l3 / u2-l3）：

```
systolic (MAC + gather)  ──mul_outcome=168bit──>  ori_data
                                                       │
                                                  quantize
                                                       │  (本讲)
                                              quantized_data=128bit
                                                       │
                                                   write_out ──> a/b/c SRAM
```

`quantize` 是这条链上「算完之后、写回之前」的唯一一道后处理。

## 4. 核心概念与源码讲解

### 4.1 quantize 模块全貌：位置与「21bit → 16bit」任务

#### 4.1.1 概念说明

`quantize` 是一个**纯组合**模块（没有 `clk`、没有 `always@(posedge clk)`），它只做一件事：把上游送来的 168bit 打包总线 `ori_data`，按每个元素 21bit 切成 8 段，逐段饱和收缩成 16bit，再重新打包成 128bit 的 `quantized_data` 输出。

为什么是 168 进、128 出？因为 `ARRAY_SIZE = 8`：

- 输入 `ori_data` 位宽 = `ARRAY_SIZE * (DATA_WIDTH+DATA_WIDTH+5)` = \(8 \times 21 = 168\) bit，即 8 个 21bit 元素；
- 输出 `quantized_data` 位宽 = `ARRAY_SIZE * OUTPUT_DATA_WIDTH` = \(8 \times 16 = 128\) bit，即 8 个 16bit 元素。

定点语义上，这一步是把累加器的 **Q13.8**（21 位：13 位整数含符号 + 8 位小数）收缩为输出的 **Q8.8**（16 位：8 位整数含符号 + 8 位小数）。注意一个关键事实：**输入和输出都有 8 位小数**，所以这一步并不改变小数精度，只压缩整数范围——它实质是一个「带饱和的整数位宽收缩」，而不是传统意义上的重新量化（没有移动小数点）。

#### 4.1.2 核心流程

`quantize` 的执行过程可以用一行伪代码概括，对 8 个元素并行重复：

```
对 i = 0..7：
    slice21 = ori_data 中第 i 段 21bit            # 切片
    if slice21 >=  +32767 : out16 = +32767        # 正向饱和
    else if slice21 <= -32768 : out16 = -32768    # 负向饱和
    else                  : out16 = slice21 的低16bit  # 在范围内：直接取低位
    quantized_data 第 i 段 16bit = out16          # 打包
```

三件事：**切片 → 三分支判定 → 打包**。因为是 `always@*` 组合逻辑，综合后会生成 8 套并行的「比较器 + 多路选择器」，8 个元素在同一拍内全部量化完成，不占时钟周期。

#### 4.1.3 源码精读

先看模块的端口声明，这是整个模块的「契约」：

[rtl/quantize.v:L3-L12](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L3-L12) 声明了四个 `parameter` 与一进一出两个端口。注意输入 `ori_data` 和输出 `quantized_data` 都带 `signed`：

```verilog
input  signed [ARRAY_SIZE*(DATA_WIDTH+DATA_WIDTH+5)-1:0] ori_data,        // 168bit
output reg signed [ARRAY_SIZE*OUTPUT_DATA_WIDTH-1:0] quantized_data       // 128bit
```

`signed` 关键字对后续的 `>=` 比较至关重要——它保证比较按有符号数进行（否则负数会被当成大正数，饱和逻辑全错）。

再看它在顶层如何被例化与连接：

[rtl/tpu_top.v:L80-L92](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L80-L92) 例化 `quantize`。端口连接非常简单，只有一进一出：

```verilog
quantize #(.ARRAY_SIZE(ARRAY_SIZE), ...) quantize (
    .ori_data(ori_data),            // 来自 systolic 的 mul_outcome
    .quantized_data(quantized_data) // 送往 write_out
);
```

而 `ori_data` 这根线的源头在 [rtl/tpu_top.v:L116](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L116)：`.mul_outcome(ori_data)`——也就是说，`systolic` 的输出 `mul_outcome` 直接改名连成了 `quantize` 的输入。两根 wire 的声明见 [rtl/tpu_top.v:L47-L48](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L47-L48)。

> **源码阅读注意点（注释与代码不符）**：[rtl/quantize.v:L21](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L21) 的注释写着 `quantize the data from 32 bit(16: integer, 8: precision) to 16 bit`，提到「32bit」。但实际代码里 `ORI_WIDTH = 21`（见 4.1.3 末与 4.3.3），输入是 21bit。这条注释是**历史遗留、与代码不一致**。本项目一贯原则是「文档与代码矛盾时以源码为准」，所以请以 21bit 为准，别被注释误导。

#### 4.1.4 代码实践

**实践目标**：确认 `quantize` 在通路中的「夹层」位置与位宽变换。

**操作步骤**：
1. 打开 [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v)。
2. 找到 `wire signed [ARRAY_SIZE*ORI_WIDTH-1:0] ori_data;`（L47）和 `wire signed [ARRAY_SIZE*OUTPUT_DATA_WIDTH-1:0] quantized_data;`（L48）。
3. 用计算器把 `ARRAY_SIZE=8`、`ORI_WIDTH=21`、`OUTPUT_DATA_WIDTH=16` 代入，算出两根线的位宽。

**需要观察的现象 / 预期结果**：`ori_data` = 168bit，`quantized_data` = 128bit；`quantize` 是它们之间唯一的转换环节；它既不接 `clk` 也不在 `always@(posedge)` 里，是纯组合。

#### 4.1.5 小练习与答案

**Q1**：如果把 `OUTPUT_DATA_WIDTH` 从 16 改成 8（且相应改 `max_val/min_val`），`quantize` 输出总线变成多宽？每段几位？

**答**：输出总线 = `ARRAY_SIZE * OUTPUT_DATA_WIDTH` = \(8 \times 8 = 64\)bit，每段 8bit。这也是为什么位宽全用参数表达——改一个参数，总线宽度自动跟随。

**Q2**：`quantize` 模块里有 `clk` 端口吗？为什么这很重要？

**答**：没有。它是纯组合逻辑，意味着量化「零延迟」地发生在数据流过之时，不增加流水线级数，也不会引入额外的时钟周期开销。

---

### 4.2 饱和边界 max_val / min_val 与三分支判断

#### 4.2.1 概念说明

「量化」的核心难点不在切片，而在**溢出处理**。21bit 有符号数的范围是 \([-2^{20},\ 2^{20})\)，而 16bit 有符号数只能表示 \([-32768,\ 32767]\)。一个 21bit 值可能远超 16bit 的表示范围。

如果我们天真地「直接砍掉高 5 位、保留低 16 位」，就会遭遇**二进制补码的回绕（wrap-around）**：超出上界的正数会变成负数，低于下界的负数会变成正数，符号彻底翻转。这在神经网络里是致命的——本来一个很大的正激活值会突然变成负数。

`quantize` 的解法是**饱和（saturation）**：用两个边界常量 `max_val = 32767`（16bit 正上限）和 `min_val = -32768`（16bit 负下限）把超界值钳位。这两个常量正是「16 位有符号数能表示的极值」。

#### 4.2.2 核心流程

判定逻辑是一个三分支决策树（按优先级，先判正向、再判负向、最后才取低位）：

```
slice21 = ori_data 的第 i 段（21bit 有符号）
├─ slice21 >= +32767  ──>  out16 = +32767   （正饱和）
├─ slice21 <= -32768  ──>  out16 = -32768   （负饱和）
└─ 否则（在范围内）    ──>  out16 = slice21[15:0]  （取低 16 位）
```

为什么「在范围内」时取低 16 位就正确？这是本模块最精妙的一点，留到 4.3.1 展开。这里先记住结论：**只要 21bit 值确实落在 \([-32768, 32767]\) 内，它的高 5 位必然全是符号扩展位，丢掉它们不影响数值；一旦它越界，高 5 位就不再是纯符号扩展，这时必须靠饱和兜底。** 饱和存在的意义，正是检测「符号扩展假设何时失效」。

边界处的衔接是无缝的：当 `slice21` 恰好等于 \(+32767\) 时，正向分支输出 32767；而若走第三分支取低 16 位，结果也是 32767——两者一致。负向边界 \(-32768\) 同理。所以三分支在边界上不会跳变。

#### 4.2.3 源码精读

边界常量定义在模块顶部：

[rtl/quantize.v:L14](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L14)：

```verilog
localparam max_val = 32767, min_val = -32768;
```

这正好是 16 位有符号整数的两个极值（\(2^{15}-1\) 与 \(-2^{15}\)）。它们用 `localparam` 而非 `parameter`，说明是模块内部派生、不对外暴露的常量。

三分支判断本身在循环体里：

[rtl/quantize.v:L25-L27](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L25-L27)：

```verilog
if      (ori_shifted_data >= max_val) quantized_data[i*OUTPUT_DATA_WIDTH +: OUTPUT_DATA_WIDTH] = max_val;
else if (ori_shifted_data <= min_val) quantized_data[i*OUTPUT_DATA_WIDTH +: OUTPUT_DATA_WIDTH] = min_val;
else                                  quantized_data[i*OUTPUT_DATA_WIDTH +: OUTPUT_DATA_WIDTH] = ori_shifted_data[OUTPUT_DATA_WIDTH-1:0];
```

三个关键点：

1. `ori_shifted_data` 在 [rtl/quantize.v:L17](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L17) 声明为 `reg signed [ORI_WIDTH-1:0]`（21bit 有符号），所以 `>=` / `<=` 都是有符号比较——负数能被正确识别为「小于正数」。
2. 两个比较分别用 `>= max_val` 和 `<= min_val`，覆盖了「正向溢出」与「负向溢出」两端。
3. 第三分支用 `ori_shifted_data[OUTPUT_DATA_WIDTH-1:0]`，即取**最低 16 位**（不是高 16 位！）。这和「丢掉高 5 位保护位」完全吻合。

#### 4.2.4 代码实践（本讲核心实践任务）

**实践目标**：亲手验证三分支饱和逻辑，并体会「若删掉饱和、直接截取低位」会在什么输入下出错。

我们用 `ori_shifted_data` 的**原始 21bit 有符号整数值**来手算（定点的小数部分对判定没有影响，比较只看整数大小）。下面三个样例分别命中三条分支。记 \(\text{raw}\) 为 21bit 有符号整数原值，\(\text{out}\) 为输出的 16bit 有符号整数。

| 样例 | raw（21bit 有符号） | 命中分支 | out（正确/饱和） | 若**去掉饱和**直接取低 16 位 |
|------|---------------------|----------|------------------|------------------------------|
| A：在范围内 | \(+1000\)（= 0x003E8） | 第三分支 | \(+1000\)（0x03E8） | \(+1000\)（0x03E8）✅ 一致 |
| B：远大于 max | \(+50000\)（= 0x0C350） | 正饱和 | \(+32767\)（0x7FFF） | \(50000 - 65536 = -15536\)（0xC350）❌ 符号翻转 |
| C：远小于 min | \(-50000\)（补码 0x1F3CB0） | 负饱和 | \(-32768\)（0x8000） | \(-50000 + 65536 = +15536\)（0x3CB0）❌ 符号翻转 |

**手算要点**：

- 样例 A：\(1000 \le 32767\) 且 \(1000 \ge -32768\)，走第三分支，取低 16 位 = 1000。
- 样例 B：\(50000 \ge 32767\) 成立，正饱和到 32767。若不饱和，50000 的低 16 位是 0xC350，作为 16 位有符号数 = \(50000 - 65536 = -15536\)——一个本该是「很大的正数」变成了负数，这就是补码回绕。
- 样例 C：\(-50000 \le -32768\) 成立，负饱和到 \(-32768\)。若不饱和，\(-50000\) 的低 16 位 = \(65536 - 50000 = 15536 =\) 0x3CB0，作为 16 位有符号数 = +15536——本该是「很小的负数」变成了正数。

**需要观察的现象 / 预期结果**：只有样例 A 这种「落在 \([-32768, 32767]\) 内」的输入，截取低位才与饱和结果一致；样例 B、C 这种越界输入，删掉饱和必然得到符号翻转的错误结果。**结论：饱和逻辑在「正/负溢出」这两类输入下不可或缺。**

> 说明：以上为根据源码逻辑的手算预测，未在仿真器中实际运行；若要验证可在 testbench 里构造对应 21bit 值观察输出（属于「源码阅读型 + 手算型」实践）。

#### 4.2.5 小练习与答案

**Q1**：边界值 `ori_shifted_data == 32767` 会命中哪条分支？输出是多少？如果改成走第三分支输出又是多少？两者矛盾吗？

**答**：命中正向分支（`>= max_val`），输出 32767。若走第三分支取低 16 位，0x7FFF = 32767，也是 32767。两者一致，不矛盾——说明作者写成 `>=` 还是 `>` 在此边界上结果相同，是无缝衔接。

**Q2**：为什么两个比较必须用**有符号**比较？若 `ori_shifted_data` 误声明为无符号，样例 C（\(-50000\)）会怎样？

**答**：若无符号，\(-50000\) 的 21bit 模式（0x1F3CB0 = 2047152）会被当成大正数 2047152，于是命中正向饱和输出 +32767，完全错误。`signed` 是饱和逻辑正确的基石。

---

### 4.3 for 循环逐元素量化、切片打包与定点语义

#### 4.3.1 概念说明

上一节解释了「单个元素怎么量化」。本节回答两个问题：(1) 8 个元素是怎么被一并处理的？(2) 为什么「取低 16 位」对范围内的数是精确的、没有精度损失？

**并行处理**：`quantize` 用一条 `for (i=0; i<ARRAY_SIZE; i=i+1)` 循环把 8 个元素各量化一次。因为整个块是 `always@*` 组合逻辑，循环在综合时**完全展开**成 8 套并行硬件，8 个元素同一拍全部完成——这正是 TPU 高吞吐的一个缩影。

**为什么取低位无损**：回到定点语义。输入是 Q13.8（21bit），输出是 Q8.8（16bit），**两者都有 8 位小数**。那 5 位「多出来的」位全在**整数方向**（高位），是阵列累加时为了防溢出而加的保护位（见 [rtl/systolic.v:L99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L99) 的符号扩展 `{5{mul_result[15]}}`）。因此：

- 小数点没有移动，小数精度（\(2^{-8}\)）全程不变；
- 只要数值没越界，高 5 位就只是符号位的重复（符号扩展），丢掉它们数值不变；
- 这一步**不损失任何小数分辨率**，只压缩整数动态范围，越界部分由饱和承担。

所以严格地说，这个 `quantize` 做的是「**饱和式整数位宽收缩**」而非「重新量化」——它没有重新标定小数点。

> **源码阅读注意点（名字与行为不符）**：变量名 `ori_shifted_data` 里的 `shifted` 容易让人以为做了移位（`<<`/`>>`）。但看 [rtl/quantize.v:L24](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L24)，它只是 `ori_data[i*ORI_WIDTH +: ORI_WIDTH]`——一个**位切片（part-select）**，没有任何移位操作。名字是误导性的历史遗留，读源码时请以操作为准。

#### 4.3.2 核心流程

逐元素量化的打包/解包流程：

```
ori_data  (168bit = 8 段 × 21bit，段号即行号 i)
   │
   ├─ for i = 0..7:
   │     ori_shifted_data = ori_data[i*21 +: 21]      # 取第 i 段 21bit
   │     （4.2 的三分支判定）
   │     quantized_data[i*16 +: 16] = out16            # 写入第 i 段 16bit
   │
   └─ quantized_data (128bit = 8 段 × 16bit)
```

`+:` 是 Verilog 的变基部分选择（indexed part-select）：`base +: width` 表示「从 `base` 位起、向上取 `width` 位」。所以 `ori_data[i*ORI_WIDTH +: ORI_WIDTH]` = 从第 `i*21` 位起取 21 位 = 第 `i` 个元素。这种 `段号*位宽` 的切片方式与上游 `systolic` 的 gather 块完全对称（详见 u2-l3），保证「`systolic` 拼出第几段，`quantize` 就量化第几段，`write_out` 就写第几段」三者下标一致。

#### 4.3.3 源码精读

整个量化主体就一个组合块加一条循环：

[rtl/quantize.v:L22-L29](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L22-L29)：

```verilog
always@* begin
    for(i=0; i<ARRAY_SIZE; i=i+1) begin
        ori_shifted_data = ori_data[i*ORI_WIDTH +: ORI_WIDTH];           // 切片：取第 i 段 21bit
        if(...)     ... = max_val;                                        // 三分支（见 4.2.3）
        ...
    end
end
```

四个要点：

- `ORI_WIDTH` 定义在 [rtl/quantize.v:L15](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L15) 为 `DATA_WIDTH+DATA_WIDTH+5 = 21`，与顶层 `tpu_top` 的 `ORI_WIDTH`、`systolic` 的 `OUTCOME_WIDTH`（[rtl/systolic.v:L28](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L28)）三处同值——同一条数据通路的「位宽宪法」。
- `ori_shifted_data` 在循环里被反复赋值再使用，这在组合块里合法：每次迭代先把切片装进来，紧接着用它做判定，是典型的「临时变量」用法。
- 循环变量 `i` 在 [rtl/quantize.v:L19](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/quantize.v#L19) 声明为 `integer`。
- 这 5 个保护位的「源头」在上游：[rtl/systolic.v:L99](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L99) 用 `{ {5{mul_result[15]}} , mul_result }` 把 16bit 乘积符号扩展成 21bit；`quantize` 正是这些保护位的「下游收尾人」。

#### 4.3.4 代码实践

**实践目标**：跟踪一个具体元素从 `mul_outcome` 到 `quantized_data` 的位变换，确认「段号一致、位宽收缩」。

**操作步骤**：
1. 假设当前 `matrix_index` 使 gather 选出的第 `i = 3` 段（21bit）来自 `matrix_mul_2D[3][...]`（参见 u2-l3 的 gather 逻辑 [rtl/systolic.v:L137-L142](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L137-L142)）。
2. 该 21bit 值被放进 `mul_outcome[3*21 +: 21]`（即第 63..83 位）。
3. 在 `quantize` 里，`ori_shifted_data = ori_data[3*21 +: 21]` 把这一段切出来——下标完全对应。
4. 经三分支后，结果写入 `quantized_data[3*16 +: 16]`（即第 48..63 位）。

**需要观察的现象 / 预期结果**：`i` 在 systolic 的 gather、quantize 的切片、write_out 的写回里用的是**同一套「段号 = 行号」**编号，因此一个元素在三类模块间不会错位。位宽则从 21bit 收缩为 16bit。

**待本地验证**：可在 testbench 里给 `ori_data` 第 3 段人为注入一个已知 21bit 值（如 4.2.4 的样例 A），观察 `quantized_data` 第 3 段（第 48..63 位）是否等于预期 16bit 值。

#### 4.3.5 小练习与答案

**Q1**：`ori_data[i*ORI_WIDTH +: ORI_WIDTH]` 中，`i=5` 时取的是哪几位？这段对应输出的哪 16 位？

**答**：取 `ori_data` 的第 \(5 \times 21 = 105\) 位起、共 21 位（第 105..125 位）；对应输出 `quantized_data` 的第 \(5 \times 16 = 80\) 位起、共 16 位（第 80..95 位）。段号都是 5，一一对应。

**Q2**：把 `ARRAY_SIZE` 改成 16（假设其它配套也改对），这条 `for` 循环会量化多少个元素？输出总线多宽？

**答**：循环量化 16 个元素，输出总线 = \(16 \times 16 = 256\)bit。循环次数随 `ARRAY_SIZE` 自动伸缩——这正是参数化的价值。

**Q3**：这一步量化有没有损失小数精度？为什么？

**答**：没有。输入 Q13.8 与输出 Q8.8 都有 8 位小数，小数点未移动；丢掉的 5 位全在整数（高位）方向，对范围内数值只是去掉冗余的符号扩展位，小数分辨率 \(2^{-8}\) 全程不变。

## 5. 综合实践

把本讲三块知识（位置与位宽任务、饱和三分支、循环并行打包）串成一个端到端的小任务。

**任务**：为 `quantize` 设计一张「输入—分支—输出」对照表，并指出它与上游 `systolic`、下游 `write_out` 的接口契约。

**操作步骤**：

1. **画位宽链**：从 [rtl/systolic.v:L23](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L23) 的 `mul_outcome`（168bit）→ [rtl/tpu_top.v:L47-L48](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v#L47-L48) 的 `ori_data`/`quantized_data` → `quantize` 收缩为 128bit → 送 `write_out`。在图上标出每段位宽与段数。
2. **填饱和表**：用 4.2.4 的三个样例（raw = +1000 / +50000 / −50000），分别写出命中分支、正确饱和输出、以及「无饱和」的错误输出，亲手验证补码回绕。
3. **解释契约**：写一段话说明为什么 `systolic` 的 gather 段号、`quantize` 的 `for` 段号、`write_out` 的写回段号必须用同一套编号（提示：否则一个结果会被量化错位、写错地址）。
4. **（可选，待本地验证）**：在 `Pre-Synthesis_Simulation/test_tpu.v` 里临时把某段 `ori_data` 钳到已知值，跑仿真观察 `quantized_data` 对应段，验证你的手算。

**预期成果**：一张位宽链图 + 一张饱和对照表 + 一段段号一致性说明。完成后你应能向别人讲清「TPU 算完一个 8 元素结果行后，如何被安全地塞进 16bit 输出 SRAM」。

## 6. 本讲小结

- `quantize` 是 `systolic` 与 `write_out` 之间唯一的纯组合后处理，把 168bit（8×21）饱和收缩为 128bit（8×16）。
- 边界常量 `max_val = 32767`、`min_val = -32768` 正是 16 位有符号整数的两极，用 `localparam` 内部定义。
- 三分支判定（`>= max_val` 正饱和 / `<= min_val` 负饱和 / 否则取低 16 位）靠 `signed` 比较正确识别负数；越界输入若直接截取低位会发生补码回绕、符号翻转。
- 「取低 16 位」对范围内数值无损，是因为输入 Q13.8 与输出 Q8.8 共享 8 位小数，多出的 5 位全是上游 [rtl/systolic.v:L99](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L99) 符号扩展出的整数保护位。
- `for` 循环在 `always@*` 里综合成 8 路并行硬件，一拍内量化完全部 8 个元素；段号与上下游完全对齐。
- 源码里有两处「名/注不副实」：L21 注释写「32bit」实为 21bit；变量名 `ori_shifted_data` 实为切片而非移位——读源码时一律以操作为准。

## 7. 下一步学习建议

- 至此 u3 单元（控制器 / 地址 / 写回 / 量化）的数据通路后处理已讲完。建议回头把 u3-l1（控制器何时拉高 `sram_write_enable`）与本讲连起来看：**`quantize` 的输出只有在 `cycle_num >= 9` 之后才会被 `write_out` 真正写进 SRAM**，理解「算出—量化—择机写回」的三段时序。
- 接下来进入 **u4 单元（端到端仿真与验证闭环）**：u4-l1 讲 testbench 的 `data2sram` 如何把测试矩阵喂进输入 SRAM，u4-l2 讲 `golden` 参考如何与输出 SRAM 逐地址比对——你会在那里看到 `quantize` 产出的 16bit 值如何被检验正确性。
- 想加深定点理解，可重读 u1-l4 的「Q4.4 → Q8.8 → Q13.8 → Q8.8」数值旅程，把本讲的饱和收缩放回整条定点链中理解。
