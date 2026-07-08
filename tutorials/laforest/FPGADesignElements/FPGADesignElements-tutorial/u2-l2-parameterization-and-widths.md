# 参数化与位宽处理

## 1. 本讲目标

学完本讲你应该能够：

- 用 `parameter` 定义「可由使用者设置」的端口位宽，用 `localparam` 定义模块内部常量，并说清两者的差别与各自能出现的合法位置。
- 理解「所有参数默认值为 0」这条约束如何逼着使用者在实例化时显式设置参数，从而把「位宽用错」变成吵闹的编译失败。
- 用拼接 `{}` 与复制 `{N{...}}` 构造与参数化位宽严格匹配的常量，消除位宽告警、避免 X 传播。
- 用 `clog2` 函数计算「索引 N 个项目所需的最少二进制位数」，并知道何时要多加一位。

## 2. 前置知识

本讲承接 u2-l1。你已经知道每个 `.v` 文件开头有 `` `default_nettype none ``，只用 `reg`/`wire` 两种类型，且所有寄存器都必须初始化为不含 X/Z 的值。本讲继续往文件里走，回答两个新问题：

1. 一个模块的端口位宽，怎么写成「能被调用方填入」的形式？
2. 当位宽是一个变量时，怎么给它写出一个「位宽完全匹配」的常量初值？

需要先接受几个基础概念：

- **参数化（parameterization）**：把代码里写死的数字（比如 32 位）换成可以被调用方填入的「占位」，让同一个模块能复用到不同位宽。
- **实例化（instantiation）**：把一个模块放进另一个模块里使用，并在此时给它填参数、连信号。
- **elaboration（精化）**：综合之前的一步。工具在这一步把参数代进去、展开 generate、算出每根线的真实位宽。本讲的「参数默认 0 → 位宽变成非法的 `[-1:0]`」就是在这一步失败。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `verilog.html` | 全书 Verilog 编码规范正文。本讲引用 Parameterization / Bit Widths / Parameters 等节。 |
| `Register.v` | 最小的参数化模块样板：用 `parameter WORD_WIDTH` 定义端口位宽。 |
| `clog2_function.vh` | 可被各模块 `` `include `` 的「向上取整 log2」函数，用来计算索引位宽。 |
| `Counter_Binary.v`、`Pipeline_Stall_Smoother.v` | 真实工程里把「复制构造常量 + clog2」合用的范例。 |

## 4. 核心概念与源码讲解

### 4.1 参数与 localparam

#### 4.1.1 概念说明

Verilog 有两种「命名常量」机制，必须分清：

- **`parameter`（模块参数）**：在模块声明头部的 `#(...)` 里定义，**可以在实例化时被调用方改写**。它是端口位宽的唯一来源——因为端口的位宽必须在定义端口时就知道，而 `localparam` 此时还没法定义。
- **`localparam`（局部参数）**：只能在模块体内部定义，**不能在实例化时改写**，专门用来持有「由其他参数算出来的内部常量」。

一句话区分：**能被用户改的用 `parameter`，算出来给内部用的用 `localparam`。**

还有一条本书的硬规矩（承接 u1-l2/u2-l1）：**所有 `parameter` 的默认值必须是 `0` 或空串**。这不是疏忽，而是一道安全栅栏——如果你实例化时忘了设 `WORD_WIDTH`，它就是 0，端口位宽 `[WORD_WIDTH-1:0]` 就退化成 `[-1:0]`，这是非法位宽，elaboration 直接失败。模块「吵闹地失败」，而不是静默地用一个错位宽跑起来。

#### 4.1.2 核心流程

定义一个参数化模块的标准步骤：

1. 在 `#(...)` 里用 `parameter` 声明用户可设的位宽，默认 `0`。
2. 端口的 `[WIDTH-1:0]` 引用这些 parameter。
3. 进入模块体后，用 `localparam` 定义「从 parameter 算出来的」内部常量。
4. 如果某个端口位宽是「全局常量 / 由其他参数算出、不应让用户直接设」，只能**再用一个 `parameter`** 持有它（因为 `localparam` 在端口定义里不合法——那是 SystemVerilog 才有的能力），并加注释「不要在实例化时设置」。

伪代码（示例代码）：

```verilog
module Foo
#(
    parameter INPUT_WIDTH = 0,                       // 用户必设
    parameter INPUT_COUNT = 0,                       // 用户必设
    // 计算型 parameter：勿在实例化时设（localparam 在此处不合法）
    parameter TOTAL_WIDTH = INPUT_WIDTH * INPUT_COUNT
)
(
    input wire [TOTAL_WIDTH-1:0] this_input
);
    ...
endmodule
```

#### 4.1.3 源码精读

`Register.v` 是最小的参数化样板，两个参数都默认 `0`：

[Register.v:L42-L52](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L42-L52) —— 声明 `WORD_WIDTH=0` 与 `RESET_VALUE=0`，端口 `data_in`/`data_out` 都用 `[WORD_WIDTH-1:0]`。实例化时不设 `WORD_WIDTH`，端口就变成 `[-1:0]`，这就是「参数默认 0」栅栏的具体落点。

规范里把这条规矩写得很清楚——「所有 parameter 默认 0 或空串，否则 elaboration 失败」「用 parameter 定义端口位宽，因为 localparam 太晚」：

[verilog.html:L268-L283](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L268-L283)

而「localparam 不能用在端口定义里，所以计算型位宽只能再用一个 parameter 持有」的规矩，规范用一个 `module Foo` 例子演示，`TOTAL_WIDTH = INPUT_WIDTH * INPUT_COUNT` 带注释「不要在实例化时设」：

[verilog.html:L316-L343](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L316-L343)

规范还指出 `localparam` 同样可以带位宽（如 `localparam [WORD_WIDTH-1:0] WORD_CONSTANT = ...`），并明确 `reg` 输出端口不能在声明处初始化、必须紧跟一个 `initial` 块：

[verilog.html:L170-L197](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L170-L197)

`Register.v` 正是这样做的——`data_out` 是 `output reg`，所以用 `initial` 初始化：

[Register.v:L54-L56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L54-L56) —— `initial begin data_out = RESET_VALUE; end`。

#### 4.1.4 代码实践

实践目标：亲手确认「参数默认 0」栅栏。

1. 打开 `Register.v`，找到 `parameter WORD_WIDTH = 0`。
2. 假设你在顶层实例化 `Register`，但**忘记**传 `WORD_WIDTH`（示例代码）：

   ```verilog
   Register my_reg(
       .clock(clock), .clock_enable(1'b1), .clear(1'b0),
       .data_in(data_in), .data_out(data_out)
   );
   ```
3. 在脑中跑一遍 elaboration：`WORD_WIDTH` 是 0，`data_in`/`data_out` 位宽 = `[0-1:0]` = `[-1:0]`。

需要观察的现象：综合/lint 工具报「端口位宽非法（负宽度 / 零宽度）」错误。

预期结果：elaboration 失败，设计无法生成。这正是栅栏起作用——逼你回去补上 `.WORD_WIDTH(8)`。

> 待本地验证：如果你手头有 Vivado / Yosys / Verilator，把上面那段不设参数的实例化跑一次 lint，确认报错信息。

#### 4.1.5 小练习与答案

**练习 1**：为什么端口的位宽必须用 `parameter`，而不能用 `localparam`？

**答案**：端口位宽在端口定义阶段就要确定，而 `localparam` 只能在模块体里定义，那时端口已经定义完了——太晚。只有 SystemVerilog 才允许 `localparam` 出现在端口定义里。

**练习 2**：`parameter X = 0` 与 `localparam X = 0` 在「能否被实例化改写」上有什么区别？

**答案**：`parameter` 可以在实例化时被改写（如 `.X(8)`），`localparam` 不能。所以「可由用户调的量」用 `parameter`，「内部算出的常量」用 `localparam`。

---

### 4.2 位宽匹配与拼接/复制

#### 4.2.1 概念说明

参数化带来一个新麻烦：当位宽是变量（比如 `WORD_WIDTH`）时，你怎么写出一个「正好 `WORD_WIDTH` 位」的常量？

直觉写法 `WORD_WIDTH'b0` **在 Verilog-2001 里是非法的**——`parameter` 与 `localparam` 都不能用作字面量的位宽说明符，只有 `` `define `` 宏和字面整数可以。规范原话：「module parameters and localparams are not valid width specifiers for literal values」。

解决办法是用**拼接 `{}` 和复制 `{N{...}}`**：

- `{WORD_WIDTH{1'b0}}` 复制 `WORD_WIDTH` 个 `1'b0`，得到一个全 0、位宽正好是 `WORD_WIDTH` 的常量。
- `{{WORD_WIDTH-1{1'b0}},1'b1}` = 前面 `WORD_WIDTH-1` 个 0 拼上一个 1，即「位宽匹配的 1」。
- `~{WORD_WIDTH{1'b0}}` = 位宽匹配的全 1（也即 −1）。

与之并列的另一条硬规矩是**位宽必须严格匹配**：赋值时左右位宽不一致，即使 Verilog 会自动做零扩展/符号扩展，也会在 CAD 工具里产生「无意义告警」，淹没真正重要的告警。所以统一用复制构造出严格匹配的常量，让任何位宽告警都变得「有意义」。

#### 4.2.2 核心流程

构造位宽匹配常量的固定套路（示例代码）：

```verilog
localparam WORD_WIDTH     = 72;                         // 或来自 parameter
localparam WORD_ZERO      = {WORD_WIDTH{1'b0}};         // 全 0
localparam WORD_ONE       = {{WORD_WIDTH-1{1'b0}},1'b1};// 全 0 末位 1
localparam WORD_MINUS_ONE = ~WORD_ZERO;                 // 全 1

reg [WORD_WIDTH-1:0] foo = WORD_ZERO;                   // 严格位宽匹配赋值
```

三条要点：

1. **永远不要**写 `reg [WORD_WIDTH-1:0] foo = WORD_WIDTH'b0;`（非法）。
2. 少写 `'b0` / `'d42` 这种不指定位宽的字面量去赋给宽变量——超过 32/64 位时，各工具的隐式扩展行为不一致，可能补 0 也可能补 X。
3. 赋值时左右位宽对齐；需要把一个窄值扩展到宽位时，用拼接显式做零扩展或符号扩展（示例代码）：

   ```verilog
   // 零扩展：把 M 位的 val 放进 N 位（N > M），高位补 (N-M) 个 0
   wide = {{(N-M){1'b0}}, val};
   // 符号扩展：高位补 (N-M) 个符号位 val[M-1]
   wide = {{(N-M){val[M-1]}}, val};
   ```

#### 4.2.3 源码精读

规范给出拼接/复制构造位宽匹配常量的标准范例，并明确 `WORD_WIDTH'b0` 非法、`'b0` 在宽位下扩展行为不可靠：

[verilog.html:L155-L197](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L155-L197)

规范的 Bit Widths 一节强调「严格位宽匹配」：

[verilog.html:L199-L234](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L199-L234)

真实模块里的用法——`Counter_Binary.v` 用复制构造全 0 常量，随后给 `next_count` 做位宽严格匹配的初值：

[Counter_Binary.v:L47](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Counter_Binary.v#L47) —— `localparam WORD_ZERO = {WORD_WIDTH{1'b0}};`

更完整的范例是 `Pipeline_Stall_Smoother.v`，一次构造出「全 0」与「全 0 末位 1」两个位宽匹配常量：

[Pipeline_Stall_Smoother.v:L89-L91](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L89-L91) —— `BUFFER_COUNT_ONE = {{BUFFER_COUNT_WIDTH-1{1'b0}},1'b1}`、`BUFFER_COUNT_ZERO = {BUFFER_COUNT_WIDTH{1'b0}}`。

> 对照 `Register.v` 的 `parameter RESET_VALUE = 0`（不带位宽）：它依赖 `0` 的隐式扩展，对「全 0」没问题；但本节的复制写法更显式、位宽严格匹配，未来要把初值改成非零模式时只需改一个 `localparam`。

#### 4.2.4 代码实践

实践目标：用一个参数化寄存器构造位宽匹配的 `RESET_VALUE` 常量。

1. 设你有一个参数化寄存器，端口为 `output reg [WORD_WIDTH-1:0] data_out`，希望上电初值为全 0。
2. 在模块体里写（示例代码）：

   ```verilog
   localparam RESET_VALUE = {WORD_WIDTH{1'b0}};
   initial begin
       data_out = RESET_VALUE;
   end
   ```
3. 用 lint 工具检查：复制构造版本不应出现任何「位宽不匹配」告警。

需要观察的现象：与「直接 `data_out = 0`」相比，复制构造版本位宽严格相等。

预期结果：`data_out` 初值为全 0，且无位宽告警。

#### 4.2.5 小练习与答案

**练习 1**：写出一个位宽为 `WORD_WIDTH`、值为「全 1」的 `localparam`。

**答案**：`localparam WORD_ALL_ONES = {WORD_WIDTH{1'b1}};`（等价地 `~{WORD_WIDTH{1'b0}}`）。

**练习 2**：为什么本书避免 `reg [W-1:0] foo = 'b0;`？

**答案**：`'b0` 是不指定位宽的字面量，超过 32/64 位时各 CAD 工具的隐式扩展（补 0 还是补 X）不一致，可能引入隐蔽 bug。用 `{W{1'b0}}` 复制则位宽确定、行为一致。

---

### 4.3 clog2 函数

#### 4.3.1 概念说明

参数化模块经常遇到一个问题：「我有 N 个项目，需要几位二进制来给它们编号？」例如：

- 16 个寄存器，地址要几位？→ 4 位（编址 0..15）。
- 一个深度为 `DEPTH` 的 FIFO，存放「当前数据量」的计数器又要几位？

答案是「向上取整的 log2」，记作：

\[ \mathrm{clog2}(N) = \lceil \log_2 N \rceil \]

SystemVerilog 内建了 `$clog2()`，但本书只用 Verilog-2001（承接 u2-l1），所以自带了一个等价函数 `clog2`，放在 `clog2_function.vh` 里，谁需要谁 `` `include ``。

一个易错点：`clog2(N)` 给的是「编址 N 个项目（编号 0..N−1）所需的位数」。但如果你要存的是一个**计数器，其值可以达到 N 本身**（而不是 N−1），那就得多一位，写成 `clog2(N) + 1`。下一节的源码会看到这个 `+1`。

#### 4.3.2 核心流程

`clog2` 的算法本质是「不断右移直到归零，数一共移了几次」：

\[ \text{位数} = \text{把 } (N-1) \text{ 右移到 0 所需的移位次数} \]

为什么是 \(N-1\)？因为 N 个项目的最大编号是 \(N-1\)，我们数的是「表示 \(N-1\) 需要几位」。例如 \(N=16\)，\(N-1=15=\mathtt{1111}\)，右移 4 次归零，所以 4 位；\(N=17\)，\(N-1=16=\mathtt{10000}\)，右移 5 次归零，所以 5 位。

参考值（与函数注释一致）：

- \(\mathrm{clog2}(15) = 4\)
- \(\mathrm{clog2}(16) = 4\)
- \(\mathrm{clog2}(17) = 5\)

使用流程：

1. 在模块体开头 `` `include "clog2_function.vh" ``。
2. 用 `localparam ADDR_WIDTH = clog2(NUM_ITEMS);` 把结果固化成常量。
3. 该 `localparam` 之后可作内部寄存器位宽、genvar 上界等。

#### 4.3.3 源码精读

函数本体——外层包了 `` `ifndef CLOG2_FUNCTION `` / `` `define `` / `` `endif ``，保证被多个文件重复 `` `include `` 时只定义一次（幂等）：

[clog2_function.vh:L36-L45](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/clog2_function.vh#L36-L45) —— `temp = value - 1`，循环里 `temp = temp >> 1` 并 `clog2 = clog2 + 1`，直到 `temp` 归零。

函数头部的用法说明与示例值（也说明了典型用途：「模块收到一个项目数参数，需要造一个内部索引寄存器」）：

[clog2_function.vh:L8-L26](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/clog2_function.vh#L8-L26)

真实工程用法——`Pipeline_Stall_Smoother.v` 同时展示 `clog2` 与那个微妙的 `+1`：

[Pipeline_Stall_Smoother.v:L78-L91](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Pipeline_Stall_Smoother.v#L78-L91) —— 先 `` `include "clog2_function.vh" ``，再 `localparam BUFFER_COUNT_WIDTH = clog2(FIFO_DEPTH) + 1`。注释（L85-L87）解释了 `+1`：因为「存放数据量的计数」要能表示 `FIFO_DEPTH` 这个值本身（0..FIFO_DEPTH），而不是只编址 0..FIFO_DEPTH−1，所以比纯索引多一位。注意 L81 的 `max(MAX_STALL_CYCLES, 2)` 是一个兜底，保证深度不小于 2（否则 `clog2` 可能返回 0，撞上 Verilog-2001 的「零位宽非法」问题）。

#### 4.3.4 代码实践

实践目标：用 `clog2` 计算「索引 N 个项目所需位数」。

1. 用函数逻辑手算几个值：
   - \(N=8\)：\(N-1=7=\mathtt{111}\)，右移 3 次归零 → \(\mathrm{clog2}(8)=3\)（3 位编址 0..7）。
   - \(N=256\)：→ \(\mathrm{clog2}(256)=8\)。
   - \(N=300\)：\(N-1=299\)，介于 \(2^8=256\) 与 \(2^9=512\) 之间 → 需要 9 位，\(\mathrm{clog2}(300)=9\)。
2. 想象一个深度 `DEPTH=4` 的 FIFO，要存「当前数据量」（值可达 4），则计数器位宽 = `clog2(4) + 1 = 2 + 1 = 3` 位。

需要观察的现象：纯索引（编址 0..N−1）用 `clog2(N)`；要表示到 N 本身用 `clog2(N)+1`。

预期结果：8 项需 3 位地址；256 项需 8 位；深度 4 的 FIFO 计数器需 3 位。

> 待本地验证：在任意支持 Verilog 的仿真器里写一个 `initial $display(clog2(300));`（先 `` `include `` 函数），确认打印 9。

#### 4.3.5 小练习与答案

**练习 1**：一个 RAM 有 1000 个表项，地址端口需要几位？

**答案**：`clog2(1000)`。\(N-1=999\)，而 \(2^9=512 < 999 < 1024=2^{10}\)，所以需要 10 位。

**练习 2**：为什么 `Pipeline_Stall_Smoother` 里写 `clog2(FIFO_DEPTH) + 1` 而不是 `clog2(FIFO_DEPTH)`？

**答案**：因为它要存的是「数据量计数」，该计数的最大值是 `FIFO_DEPTH` 本身（满），不是 `FIFO_DEPTH-1`，所以比纯索引多一位。

---

## 5. 综合实践

把三个最小模块串起来，写一个参数化的「回绕计数器」：给定 `TABLE_DEPTH`，内部从 0 数到 `TABLE_DEPTH-1`，回绕时拉高 `wrap` 一个周期。

示例代码（非项目原有文件）：

```verilog
`default_nettype none

module Wrap_Counter
#(
    parameter TABLE_DEPTH = 0          // 表项数，必须 >= 2（见讨论）
)
(
    input  wire    clock,
    input  wire    clear,
    output wire    wrap                // 每回绕一次拉高一个周期
);

    `include "clog2_function.vh"

    // (1) clog2：算出编址 0..TABLE_DEPTH-1 所需位数（内部 localparam，非端口位宽）
    localparam ADDR_WIDTH = clog2(TABLE_DEPTH);

    // (2) 复制构造：位宽严格匹配的全 0 常量
    localparam ADDR_ZERO  = {ADDR_WIDTH{1'b0}};

    // TABLE_DEPTH-1 由 clog2 的定义保证放得进 ADDR_WIDTH 位
    localparam [ADDR_WIDTH-1:0] ADDR_MAX = TABLE_DEPTH - 1;

    reg  [ADDR_WIDTH-1:0] address;
    reg                   wrap_internal;

    // (3) reg 用 initial 初始化（承接 u2-l1）
    initial begin
        address       = ADDR_ZERO;
        wrap_internal = 1'b0;
    end

    always @(posedge clock) begin
        if (clear == 1'b1) begin
            address       <= ADDR_ZERO;
            wrap_internal <= 1'b0;
        end
        else if (address == ADDR_MAX) begin
            address       <= ADDR_ZERO;
            wrap_internal <= 1'b1;
        end
        else begin
            address       <= address + 1'b1;   // 见讨论
            wrap_internal <= 1'b0;
        end
    end

    assign wrap = wrap_internal;

endmodule

`default_nettype wire
```

任务步骤与讨论点：

1. **为什么 `ADDR_WIDTH` 是 localparam 却没用在端口上？** 因为 `localparam` 不能作端口位宽（见 4.1）。本例端口 `clock/clear/wrap` 恰好都是 1 位，回避了这个问题。如果你的端口本身需要 `ADDR_WIDTH` 位宽，就得把它提升成 `parameter`（并由调用方在实例化时算好填入）。
2. **`TABLE_DEPTH` 为什么必须 ≥ 2？** 若 `=1`，`clog2(1)=0`，`ADDR_WIDTH=0` 会触发 Verilog-2001 的「零位宽非法」问题。真实模块用 `max(DEPTH, 2)` 兜底——见 `Pipeline_Stall_Smoother.v` 的 `FIFO_DEPTH`。
3. **`address + 1'b1` 的位宽瑕疵：** `1'b1` 是 1 位，与 `ADDR_WIDTH` 位的 `address` 相加会有隐式扩展，严格说会产生位宽告警。本书 `Counter_Binary.v` 通过实例化 `Adder_Subtractor` 子模块来严格匹配位宽；本练习为聚焦主题，接受这个 `+1`。
4. 检查你的实现里每个赋值的左右位宽是否都严格匹配（`ADDR_ZERO`、`ADDR_MAX` 都用复制/带位宽 localparam 构造）。

预期结果：得到一个可综合的参数化回绕计数器，`TABLE_DEPTH=16` 时 `address` 为 4 位、`TABLE_DEPTH=300` 时为 9 位，lint 无位宽告警。

> 待本地验证：用 Verilator 或 Vivado 综合 `TABLE_DEPTH` 分别为 16 与 300 的两个实例，确认 `address` 位宽与回绕行为。

## 6. 本讲小结

- 用 `parameter` 定义可被实例化改写的量（尤其是端口位宽），用 `localparam` 定义体内算出的内部常量；两者位置受限：`localparam` 不能出现在端口定义里。
- 「所有 `parameter` 默认 0」是一道安全栅栏：忘设参数会让位宽退化成非法的 `[-1:0]`，在 elaboration 阶段吵闹地失败。
- Verilog-2001 不允许用 `parameter`/`localparam` 作字面量位宽说明符，所以 `WORD_WIDTH'b0` 非法；改用复制 `{N{1'b0}}` 构造位宽严格匹配的常量。
- 位宽必须严格匹配：不匹配即使能隐式扩展，也会产生无意义告警、淹没真问题。
- `clog2(N) = ⌈log₂ N⌉` 给出编址 N 个项目所需的位数；要表示到 N 本身则需 `clog2(N)+1`；注意 `N<2` 时会撞上零位宽问题。
- `clog2` 函数放在 `clog2_function.vh` 里，用 `` `ifndef `` 包成幂等，供需要的模块 `` `include ``。

## 7. 下一步学习建议

本讲建立的「参数化 + 复制构造常量 + clog2」三件套，会在后续几乎每一篇讲义里反复出现：

- u3（赋值、三元、复位）会接着讲阻塞/非阻塞赋值、`last-assignment-wins` 复位惯用法——本讲的 `Register.v` 已经用到了它，届时会展开。
- u6（Register 家族）、u8（Counter_Binary）会把 `clog2` 与 `{WORD_WIDTH{1'b0}}` 用到具体的寄存器和计数器上，建议对照阅读 `Counter_Binary.v` 看一个完整实例。
- u7（RAM）会遇到「零位宽 padding」问题更复杂的版本，可回看 `verilog.html` 的「Avoiding Zero-Width Padding」一节。

继续阅读建议：`Counter_Binary.v`（看 clog2 + 复制常量在真实计数器里的合用）、`Pipeline_Stall_Smoother.v`（看 `+1` 与 `max(...,2)` 兜底）。
