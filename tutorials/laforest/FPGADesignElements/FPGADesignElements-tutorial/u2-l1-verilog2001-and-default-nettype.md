# 受限的 Verilog-2001 与 default_nettype

## 1. 本讲目标

本讲是「Verilog 编码规范」单元的第一篇。学完后你应当能够：

- 说清楚这本书为什么**只用 Verilog-2001 的可综合子集**，而刻意避开 SystemVerilog 和 Verilog-1995。
- 理解 `default_nettype none` 这一行的作用，知道为什么每个 `.v` 文件都要把它写在最前面。
- 掌握 `reg` 与 `wire` 的使用约定：哪些地方必须用 `wire`、哪些地方该用 `reg`，以及为什么所有寄存器都必须初始化。
- 能够照着 `Register.v` 的头部风格，亲手写出一个「合规」的最小模块。

本讲承接 u1-l2：你已经知道这个仓库「一模块一文件、参数默认值为 0、必须实例化才能用」，本讲就带你钻进单个 `.v` 文件，看清它的开头几行为何如此规矩。

## 2. 前置知识

在开始前，先用大白话解释几个本讲会用到的术语：

- **Verilog**：一种硬件描述语言（HDL）。你写的不是「一步一步执行」的程序，而是「电路长什么样」的描述，最终会被综合（synthesis）成真实的逻辑门和触发器。
- **综合（synthesis）/ 可综合（synthesizable）**：把 Verilog 代码翻译成 FPGA 上真实电路的过程。只有一部分 Verilog 写法能被综合；用来仿真的写法（如 `#10` 延时）不能上硬件。
- **CAD 工具**：综合、布局布线用的软件，例如 Xilinx Vivado、Intel Quartus、开源的 Yosys 等。不同工具对同一段代码的支持程度可能不一样。
- **线网（net）/ `wire`**：电路里的一根导线，组合逻辑的连线。
- **寄存器 / `reg`**：在 Verilog-2001 里，凡是在 `always` 块里被赋值的变量都声明成 `reg`，它最终多半对应一个触发器（flip-flop）。
- **X / Z 值**：Verilog 的四值逻辑里，`X` 表示「未知」，`Z` 表示「高阻」。仿真时它们会像传染病一样沿电路传播（X 传播），让结果难以解释。

> 一句话直觉：这本书的编码规范，本质上是**用一套受限、保守的写法，把 Verilog 里最容易踩坑的地方提前堵死**——让你把精力放在要设计的电路上，而不是语言的怪癖上。

## 3. 本讲源码地图

本讲只涉及两个关键文件：

| 文件 | 作用 |
| --- | --- |
| `verilog.html` | 全书的「Verilog 编码规范」正文。本讲的三个最小模块（语言版本、`default_nettype`、`reg/wire` 与初始化）都能在这里找到对应的章节。它由 `v2h.py` 从规范源文件渲染而来。 |
| `Register.v` | 一个参数化的同步寄存器模块。它是规范在真实代码里的「样板间」：文件头部的 `default_nettype none`、参数化端口、`initial` 初始化，正是本讲要拆解的写法。 |

另外，`Register.v` 末尾用到了「last assignment wins」复位惯用法，那是 u3-l2 的主题，本讲只在路过时点一句，不展开。

## 4. 核心概念与源码讲解

### 4.1 语言版本选型：为何是 Verilog-2001

#### 4.1.1 概念说明

Verilog 有几个主要版本：

- **Verilog-1995**：最早的标准版，功能较少。
- **Verilog-2001**：在 1995 的基础上补齐了很多实用功能（命名端口连接、向量位选、`generate` 块等），是被 CAD 工具支持得最广的版本。
- **SystemVerilog**：Verilog 的超集，加入了 interface、struct、enum、可参数化端口实例等大量现代特性。

这本书选定 **Verilog-2001，而且只用它的可综合子集**。这是一个刻意的「中庸」选择：

- 比 Verilog-1995 新：拥有让代码更短、更易读的关键功能，避免把代码写得很长很难懂。
- 比 SystemVerilog 旧：SystemVerilog 特性太多，**不同 CAD 工具支持得参差不齐**，写出来的代码可能在 A 工具能综合、在 B 工具不行。为了让同一份代码在所有主流工具里给出一致的综合结果，作者选择退回到更保守的 Verilog-2001。

规范里还有一条更重要的原则：**它定义的是一种「受限的 Verilog」**——只用数量有限、经过挑选的写法（idioms）。这些限制的目的是减少 bug、提高可读性，并让综合结果在不同工具间保持一致，无论代码是人写的还是机器生成的（这本书的很多模块就是脚本生成的）。

#### 4.1.2 核心流程

这个选型如何落到每个文件上：

1. 每个模块文件只使用 Verilog-2001 可综合子集的写法。
2. 凡是只用于仿真/验证的写法（如延时、系统任务），一律视为规范范围之外。
3. 避免把 Verilog-1995 的老写法混进来（尽管向后兼容允许）。
4. 不使用 SystemVerilog 独有的特性（如 interface、struct、enum、端口定义里的 `localparam`）。

用伪代码概括这个取舍：

```
可选语言版本 = { Verilog-1995, Verilog-2001, SystemVerilog }
选择标准      = (CAD 工具支持广) 且 (功能够用) 且 (子集可稳定综合)
==> 命中：Verilog-2001 的可综合子集
```

#### 4.1.3 源码精读

规范正文专门有一节「Verilog Language Versions」讲这件事，三段分别对应「选 2001」「弃 1995」「避开 SystemVerilog」：

- [verilog.html:56-58](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L56-L58) 指定「使用 Verilog-2001，特别是其可综合子集，因为它在各 CAD 工具间支持最好」。

- [verilog.html:60-63](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L60-L63) 说明 Verilog-1995 缺命名端口连接、向量位选、generate 块等功能，会让代码又长又难懂，不要混用。

- [verilog.html:65-70](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L65-L70) 说明 SystemVerilog 特性太多、各工具支持不均，在受限可综合子集被普遍认可之前应避免（但很适合做仿真/验证）。

「受限形式」的总纲在 Scope 一节：

- [verilog.html:37-40](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L37-L40) 明确这套规范定义的是一种受限的 Verilog，目标是减 bug、增可读、跨工具一致，且对人写和机器生成的代码都成立。

而 `Register.v` 就是这套选型的一个标准样本：它用到的命名参数 `#(...)`、向量位选 `[WORD_WIDTH-1:0]` 都是 Verilog-2001 的标志性功能，整个文件没有任何 SystemVerilog 特性：

- [Register.v:41-52](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L41-L52) 用 ANSI 风格的参数与端口定义（`#(...)` 命名参数 + 行内端口类型声明），这正是 Verilog-2001 相对 1995 的关键改进。

#### 4.1.4 代码实践

**实践目标**：亲手识别 Verilog-2001 相对 1995 的改进点。

**操作步骤**：

1. 打开 [Register.v](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v)。
2. 找出三处「Verilog-1995 没有、Verilog-2001 才有」的写法。提示：命名参数块 `#(...)`、端口列表里直接写 `wire`/`reg` 类型和位宽、向量位选 `[WORD_WIDTH-1:0]`。
3. 想象用 Verilog-1995 重写：端口类型要在模块体里再声明一遍、参数要用古老的 `#(parameter X)` 风格逐一罗列，体会「代码会变长」。

**需要观察的现象 / 预期结果**：你会发现 Verilog-2001 的写法把「方向 + 类型 + 名字」写在端口列表一行里，省掉了在模块体里重复声明，这正是规范里强调的可读性收益。

> 「待本地验证」：如果你装了 Icarus Verilog 或 Verilator，可以用 `-g2001` 选项编译 `Register.v`（设好 `WORD_WIDTH` 参数的实例化测试台），确认它在该语言版本下能顺利通过。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者不直接用更现代的 SystemVerilog？

> **参考答案**：因为 SystemVerilog 特性太多，各 CAD 工具的支持参差不齐，同一份代码在不同工具上可能得到不一致的综合结果。规范追求「跨工具一致」，所以退回到支持最广的 Verilog-2001 可综合子集。SystemVerilog 被推荐留作仿真/验证用。

**练习 2**：规范说「避免把 Verilog-1995 的写法混进 Verilog-2001 代码，尽管向后兼容允许」。请举一个 1995 风格的写法并说明为何要避免。

> **参考答案**：例如在模块体里再次声明端口类型（而不是在端口列表里写 `input wire ...`），或用位置顺序连接端口。这些老写法会让代码冗长、易错（位置连错端口不会报错），抵消了 2001 带来的可读性。

---

### 4.2 default_nettype：强制声明每一根线

#### 4.2.1 概念说明

Verilog 有一个容易让初学者栽跟头的默认行为：**如果你用了一个没声明过的标识符，编译器会默认它是一根 1 位的 `wire`**，而不会报错。

这看起来很「贴心」，实际是隐患。考虑你拼错了一个信号名：

```verilog
wire data_in;
assign data_iiin = 1'b0;   // 拼错了，编译器默默造了一根新的 1 位 data_iiin
```

代码能编译通过，但 `data_in` 根本没被驱动，综合后会出现「悬空」或意外宽度， bug 非常难找。

`default_nettype` 编译指令就是用来管这件事的。写成

```verilog
`default_nettype none
```

就关掉了「隐式线网」，**任何未显式声明的标识符都会变成编译错误**。这样拼错名字、漏声明端口立刻就会被工具抓住。

#### 4.2.2 核心流程

`default_nettype` 是一条编译器指令（以反引号 `` ` `` 开头），作用范围是「从它出现的位置开始，一直到文件末尾或下一条 `default_nettype`」。规范规定的用法：

1. **每个 `.v` 文件的最开头**（在 `module` 之前）写 `` `default_nettype none ``。
2. 如果不得不引入**第三方/厂商代码**（它们往往假定有默认线网类型，比如某些 IP 模型），就用一对指令把它包起来，局部恢复默认、用完再关掉：

   ```verilog
   `default_nettype wire
       ... 厂商代码 ...
   `default_nettype none
   ```

用伪代码概括：

```
文件开头:
    `default_nettype none      // 之后所有信号必须显式声明
    module ...                 // 端口列表里的 input/output 仍要写清类型
        wire  foo;             // OK：显式声明
        // bar              // 报错：未声明
    endmodule
```

#### 4.2.3 源码精读

- [verilog.html:97-100](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L97-L100) 规定：在每个文件开头、模块定义之前写 `` `default_nettype none ``，使任何未定义变量成为错误；否则未定义变量会变成 1 位 wire，在综合时引发隐蔽 bug。

- [verilog.html:102-112](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L102-L112) 给出「包裹不可控厂商代码」的模板：前后分别用 `` `default_nettype wire `` 和 `` `default_nettype none `` 夹住。

`Register.v` 的第一行（注释之后）就是这条指令，所有模块无一例外：

- [Register.v:39](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L39) `` `default_nettype none `` 紧贴在 `module` 之前，是这个文件的第一条可执行指令。

正因为开了 `none`，`Register.v` 端口列表里每个信号都老老实实写了 `wire` 或 `reg`（见 [Register.v:47-51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L47-L51)），没有任何一个靠「默认」混过去。

#### 4.2.4 代码实践

**实践目标**：直观感受 `default_nettype none` 如何把拼写错误变成编译错误。

**操作步骤**：

1. 复制下面这段「示例代码」（非项目原有代码）到一个临时文件 `play.v`：

   ```verilog
   // 示例代码
   `default_nettype none
   module play
   (
       input  wire [7:0] data_in,
       output wire [7:0] data_out
   );
       assign data_out = data_inn;   // 故意拼错：data_inn
   endmodule
   ```

2. 先用 Icarus Verilog 编译：`iverilog -o play.vvp play.v`（或用 Verilator `verilator --lint-only play.v`）。
3. 观察报错信息，确认它指向未声明的 `data_inn`。
4. 把第一行 `` `default_nettype none `` 删掉，重新编译。

**需要观察的现象 / 预期结果**：有 `none` 时，编译器报「`data_inn` is not declared」之类的错误；删掉后，编译可能通过（`data_inn` 被当成 1 位 wire），反而把 bug 藏了起来——这正是规范要避免的。

> 「待本地验证」：不同工具对隐式线网的告警级别不同，Verilator 即便没有 `none` 通常也会给 MULTIDRIVE/UNOPTFLAT 之类提示，但 `none` 能保证所有工具一致报错。

#### 4.2.5 小练习与答案

**练习 1**：`` `default_nettype none `` 应该写在文件的什么位置？为什么？

> **参考答案**：写在每个文件的最开头、`module` 定义之前。因为它是编译器指令，作用域从出现处延续到文件末尾；写在模块前面才能覆盖整个文件的所有信号声明。

**练习 2**：如果你的模块里必须实例化一段厂商提供的、假定有默认线网类型的 IP 模型，该怎么处理？

> **参考答案**：用 `` `default_nettype wire `` ... 厂商代码 ... `` `default_nettype none `` 把它夹起来，局部恢复默认线网、用完立刻关掉，避免影响文件其余部分。

---

### 4.3 reg / wire 与寄存器初始化

#### 4.3.1 概念说明

Verilog-2001 的类型不多，这本书的规定更简单：**只用 `reg` 和 `wire` 两种类型**。判断规则也很干脆：

- 凡是**模块输入端口**、**把若干模块端口连在一起的连线**、以及**三态 I/O**，必须用 `wire`。
- 除此之外，**所有信号默认用 `reg`**（因为它们多半最终是触发器）。

落实到端口上还有一层更细的「语义提示」：

- 所有 `input` 一定是 `wire`。
- `output` 如果是 `wire`，说明它来自某个**子模块实例**——在高度模块化的设计里，这应是常态。
- `output` 如果是 `reg`，说明它来自本模块内的**局部逻辑**——这是一种「这里有特殊情况」的提示。

与之配套的还有**逻辑值与初始化**的规定：

- 只用 `0, 1, X, Z` 四个值，并且**尽量不用 X 和 Z**。不要给寄存器或线网赋 X：X 会沿电路传播（X 传播），让测试很难做，而且 Verilator 这类 2 态仿真器根本不支持 X。
- **所有寄存器都必须初始化**为不含 X/Z 的值，可以在声明处初始化，也可以用 `initial` 块。FPGA 的配置比特流本身就包含所有寄存器的初值，相当于「免费的上电复位」。

为什么寄存器输出端口要用 `initial` 块？因为**端口不能像普通变量那样在声明处初始化**，只能在 `initial` 块里给它赋初值。

#### 4.3.2 核心流程

写一个模块端口时，按下面这个决策树选 `wire` 还是 `reg`：

```
端口方向?
├─ input        => wire（必然）
├─ inout        => wire（仅用于三态 I/O）
└─ output
    ├─ 由子模块实例驱动?  => wire（常态）
    └─ 由本模块 always 块驱动? => reg（特殊情况）+ 必须用 initial 初始化
```

寄存器初始化的两种合法写法：

```verilog
// (1) 声明处初始化（普通内部 reg 可用）
reg [W-1:0] foo = WORD_ZERO;

// (2) initial 块（寄存器输出端口只能用这种）
initial begin
    foo = WORD_ZERO;
end
```

其中 `WORD_ZERO` 这种「与参数位宽匹配的全零常量」要靠拼接/复制构造（这是 u2-l2 的主题），本讲只需记住：初值里不能有 X 或 Z。

#### 4.3.3 源码精读

**reg/wire 的总规定**：

- [verilog.html:118-121](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L118-L121) 规定只用 `reg` 和 `wire`；除「模块输入端口、连接端口的连线、三态 I/O」必须用 `wire` 外，其余信号都用 `reg`。

**逻辑值的规定**：

- [verilog.html:123-129](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L123-L129) 只用 `0,1,X,Z`，尽量避开 X 和 Z；不要给 reg/wire 赋 X（X 传播会让测试困难，且 2 态仿真器不支持）；Z 只用于三态 I/O。

**必须初始化**：

- [verilog.html:131-135](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L131-L135) 所有寄存器都必须用不含 X/Z 的值初始化（声明处或 `initial` 块），并提醒复位与初始化的某些组合在部分 FPGA 家族上可能不兼容。

**端口的「方向+类型+名字」与 reg 输出端口的 initial**：

- [verilog.html:285-309](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L285-L309) 说明每个端口都要写全方向、类型、名字；并解释 `output wire`（来自子模块，常态）与 `output reg`（来自局部逻辑，特殊情况）的语义差别。

- [verilog.html:311-314](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L311-L314) 规定：只要有 `reg` 类型的输出端口，就要在端口定义之后立刻用 `initial` 块把它初始化为启动值（因为端口不能在定义处初始化）。

- [verilog.html:316-343](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilog.html#L316-L343) 给出完整的样板模块：开头 `` `default_nettype none ``、参数化端口、`output reg another_output`、紧跟一个 `initial begin another_output = 1'b0; end`——这就是本讲实践任务要模仿的范式。

**Register.v 的对应实现**：

- [Register.v:47-51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L47-L51) 所有 `input` 都是 `wire`，唯一的 `output data_out` 是 `reg`（因为它在 `always @(posedge clock)` 里被赋值，属于「本模块局部逻辑」这个特殊情况），位宽用参数 `WORD_WIDTH` 表达。

- [Register.v:54-56](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.v#L54-L56) 因为 `data_out` 是 `reg` 输出端口，无法在声明处初始化，所以这里用 `initial` 块把它置为 `RESET_VALUE`（一个不含 X/Z 的参数化常量），正符合上面的规范条款。

#### 4.3.4 代码实践

**实践目标**：照着 `Register.v` 的头部风格，亲手写出一个完全合规的最小模块。

**操作步骤**：

1. 新建一个文件 `my_flag.v`，写一个 1 位的「标志寄存器」：每个时钟上升沿，若 `set` 为高则输出 1，否则保持。
2. 严格按下面这个「示例代码」骨架填写（注释里点出了每条规范对应的小模块）：

   ```verilog
   // 示例代码（模仿 Register.v 头部风格）
   `default_nettype none               // (4.2) 强制声明每个信号

   module my_flag
   #(
       parameter WORD_WIDTH  = 0,       // (4.1) Verilog-2001 命名参数；默认 0 = 必须实例化设置
       parameter RESET_VALUE = 0
   )
   (
       input  wire                    clock,    // (4.3) input 必为 wire
       input  wire                    set,
       output reg   [WORD_WIDTH-1:0]  flag      // (4.3) output 为 reg = 本模块局部逻辑
   );

       initial begin                  // (4.3) reg 输出端口必须用 initial 初始化
           flag = RESET_VALUE;
       end

       always @(posedge clock) begin
           if (set == 1'b1) begin
               flag <= {WORD_WIDTH{1'b1}};      // 用复制构造全 1，避免位宽不匹配
           end
       end

   endmodule
   ```

3. 实例化它（设 `WORD_WIDTH = 1`）做一个小测试台，复位后给 `set` 一个脉冲，观察 `flag` 是否被锁存为 1。

**需要观察的现象 / 预期结果**：

- 删掉 `initial` 块后，仿真开始时 `flag` 是 `X`，`set` 之前无法确定其值——直观体现「寄存器必须初始化」。
- 把 `output reg` 改成不写类型，某些工具可能推断成 `wire` 而报错（因为 `always` 块里给它赋了值）——体现「端口必须写全方向+类型+名字」。
- 故意把 `set` 拼成 `sett`，因为开了 `default_nettype none`，编译器立刻报错——体现 4.2 的价值。

> 「待本地验证」：上述行为依赖具体仿真器（Icarus/Verilator/Vivado），未初始化的 `reg` 在 4 态仿真器里显示为 `X`，在 2 态仿真器（Verilator 默认）里显示为 `0`，建议两种都试一次体会差别。

#### 4.3.5 小练习与答案

**练习 1**：为什么规范的样板里，`output reg` 之后要立刻跟一个 `initial` 块？不能用 `output reg foo = 1'b0;` 吗？

> **参考答案**：因为**端口不能在声明处初始化**（这是 Verilog 的语法限制）。普通内部 `reg` 可以 `reg foo = ...;`，但端口不行，所以 `reg` 输出端口必须在 `initial` 块里赋初值。

**练习 2**：规范强烈不建议给寄存器赋 `X`。请给出两条理由。

> **参考答案**：（1）X 会沿电路传播（X 传播），让仿真结果难以解释、测试难以进行；（2）Verilator 等 2 态仿真器不支持 X，赋 X 会得到与 4 态仿真器不一致的行为。规范要求用「兜底的默认逻辑表达式」代替显式赋 X。

**练习 3**：规范说「`output wire` 表示来自子模块实例，是常态；`output reg` 表示来自本模块局部逻辑，是特殊情况」。请结合本书「极度模块化」的设计哲学解释这句话。

> **参考答案**：因为本书主张把逻辑尽量拆进小子模块，再拼装起来。所以一个模块的输出，理想情况下是「把某个子模块实例的输出直接引出去」，自然是 `wire`。只有当一个模块自己还做了一点点局部逻辑（像 `Register` 自己锁存数据）时，输出才是 `reg`——所以 `reg` 输出是对读者的一个「这里有特殊处理」的提示。

## 5. 综合实践

把本讲的三个最小模块串起来自检。请完成下面这个「三连违反」练习：

**任务**：从你在 4.3.4 写出的 `my_flag.v` 出发，依次制造并修复三个错误，每一步都对照本讲的一个最小模块。

| 步骤 | 故意制造的违反 | 对应最小模块 | 预期工具反应 | 修复方式 |
| --- | --- | --- | --- | --- |
| 1 | 删掉文件第一行的 `` `default_nettype none ``，并把 `set` 拼成 `sett` | 4.2 default_nettype | 有 `none` 时编译报错；无 `none` 时可能静默通过，留下悬空线网 | 加回指令并改正拼写 |
| 2 | 把端口 `output reg [WORD_WIDTH-1:0] flag` 的类型删掉（只剩 `output ... flag`） | 4.3 reg/wire | 不同工具可能推断出不同类型，仿真/综合结果不一致 | 写全 `output reg` |
| 3 | 删掉 `initial begin ... end` 块 | 4.3 初始化 | 上电瞬间 `flag` 为 X（4 态仿真器），可能造成 X 传播 | 用 `initial` 把 `flag` 初始化为 `RESET_VALUE` |

完成后再做一件**正向**的事：用一条一句话注释，标注 `my_flag` 用到的某个 Verilog-2001 特性（如命名参数 `#(...)`），说明它为何比 Verilog-1995 写法更简洁（对应 4.1）。

> 提示：这个练习不需要你跑通完整综合，重点是养成「看到一行代码就能联想到它对应的规范条款」的习惯。全部「待本地验证」具体报错文案。

## 6. 本讲小结

- 本书**只用 Verilog-2001 的可综合子集**：比 1995 新（有命名参数、位选、generate），比 SystemVerilog 旧（避开各工具支持不均的现代特性），目的是跨工具一致、减 bug、对人写和机器生成的代码都成立。
- `` `default_nettype none `` 写在每个文件最开头，**把「未声明标识符」从静默的 1 位 wire 变成编译错误**，是堵住拼写错误的 cheap insurance；遇到假定默认线网的厂商代码，用 `` `default_nettype wire``/`` `default_nettype none `` 夹起来。
- **只用 `reg` 和 `wire`**：输入端口必然是 `wire`；`output wire` 表示来自子模块（常态），`output reg` 表示来自本模块局部逻辑（特殊情况）。
- **逻辑值只用 0/1，尽量不用 X/Z**；不给 reg/wire 赋 X，以免 X 传播并破坏 2 态仿真。
- **所有寄存器都必须初始化**为不含 X/Z 的值；`reg` 输出端口因为不能在声明处初始化，必须在紧跟其后的 `initial` 块里赋初值——`Register.v` 就是这套写法的标准样板。

## 7. 下一步学习建议

本讲建立了「单个文件开头几行的规矩」。下一讲 **u2-l2（参数化与位宽处理）** 会顺着 `Register.v` 继续往下，讲清三件你已经在本讲撞见但还没展开的事：

- 为什么所有参数默认值是 `0`，以及这带来的「必须实例化」后果（u1-l2 已点过，这里讲透位宽层面发生了什么）。
- `localparam` 与参数化位宽、拼接/复制（你在 4.3.4 里见到的 `{WORD_WIDTH{1'b1}}`）。
- `clog2` 函数为何要单独放进 `.vh` 文件里 `include`。

再往后，**u3-l1（赋值风格与三元运算符）** 会讲 `Register.v` 里那个 `always @(posedge clock)` 块的阻塞/非阻塞赋值规则，**u3-l2（复位哲学与 Register 模块）** 则会专门拆解本讲路过的「last assignment wins」复位惯用法。建议你先把 `Register.v` 完整读一遍，带着问题进入下一讲。
