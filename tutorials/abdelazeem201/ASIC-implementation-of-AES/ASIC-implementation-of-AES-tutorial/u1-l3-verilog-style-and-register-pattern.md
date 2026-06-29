# Verilog 代码风格与寄存器模式

## 1. 本讲目标

前两讲我们认识了 AES 算法本身（u1-l1），也看清了仓库的目录结构和文件清单（u1-l2）。现在你知道「`rtl/aes.v` 是顶层 wrapper，`rtl/aes_core.v` 是核心状态机」——但打开这两个文件，里面满屏的 `reg`、`_new`、`_we`、`always @(posedge clk or negedge reset_n)` 究竟是什么意思？为什么一个寄存器要配三个信号？这正是本讲要解决的问题。

本工程并不是随手写出来的 Verilog，它遵循一套非常一致、贯穿所有模块的编码风格。掌握了这套风格，你在阅读 `aes_encipher_block.v`、`aes_key_mem.v` 等任何子模块时，都能用同一把「钥匙」快速看懂它们的时序和数据流向。

本讲学完后，你应当能够：

- 读懂 `always @(posedge clk or negedge reset_n)` 这一行到底描述了什么样的硬件电路，理解「上升沿触发 + 异步低有效复位」。
- 理解本工程最核心的寄存器写法——每个寄存器为什么常配 `_new`（下一个值）和 `_we`（写使能）两个伴随信号，以及「组合块算新值、时序块搬进寄存器」的分工。
- 区分两类 `always` 块：描述组合逻辑的 `always @*` 与描述时序逻辑（触发器）的 `always @(posedge clk ...)`，并知道为什么要给组合块里的信号「先写默认值」。

> 说明：本讲的实践任务最初提到「在 `aes.v` 中找 `block_reg` 与 `block_new`/`block_we`」。经核对源码，`aes.v` 里**只有 `block_reg` 和 `block_we`，并没有 `block_new`**（`block_new` 这个名字只出现在 `aes_encipher_block.v`、`aes_decipher_block.v` 里，含义不同）。本讲始终以源码实际内容为准：我们会把 `aes.v` 里「简化版」写法讲清楚，再到 `aes_core.v` 里看「完整三件套」的真正模样。

## 2. 前置知识

本讲默认你已经读过 u1-l1（AES 算法背景）和 u1-l2（目录结构），不再重复解释「AES 是对称分组密码」「`rtl/` 是源码目录」等内容。下面只补充几个 Verilog 语言层面最基础的概念，确保零基础读者也能跟上：

- **信号类型 `wire` 与 `reg`**：在 Verilog 里，`wire` 表示「连线」，只能用 `assign` 连续赋值，或者作为模块端口被别的信号驱动；`reg` 表示「会在过程块（`always` / `initial`）里被赋值的变量」。注意一个常见误区：`reg` **不一定**真的对应一个触发器（寄存器）——如果它被写在 `always @*`（组合逻辑）里，综合出来的是纯组合逻辑（导线/逻辑门），只有写在 `always @(posedge clk ...)` 里才会变成真正的触发器。
- **阻塞赋值 `=` 与非阻塞赋值 `<=`**：`=` 是「立刻生效」的阻塞赋值，用于组合逻辑；`<=` 是「在当前时钟沿结束时统一更新」的非阻塞赋值，用于时序逻辑（触发器）。本工程的约定非常严格：组合块里一律用 `=`，时序块里一律用 `<=`。
- **`posedge` / `negedge`**：分别表示信号的「上升沿」（从 0 跳到 1）和「下降沿」（从 1 跳到 0）。`posedge clk` 就是「时钟跳高的那一瞬间」。
- **复位（reset）**：把电路恢复到初始已知状态的操作。本工程用 `reset_n`（结尾的 `_n` 是 active-low 的惯用写法，意为「低电平有效」），即 `reset_n = 0` 时复位，`reset_n = 1` 时正常工作。
- **触发器（Flip-Flop，FF）**：时钟驱动的存储单元，每个时钟沿采样并保存一个值。本工程里绝大多数 `reg` 综合后都是 D 触发器。

> 一句话直觉：本工程把「决定下一个值」和「把下一个值搬进寄存器」这两件事**刻意拆成两段代码**。前者用组合逻辑算，后者用时序逻辑搬。理解了这个分工，就抓住了本讲一半的内容。

## 3. 本讲源码地图

本讲只盯住两个文件，它们足以展示本工程全部的编码风格：

| 文件 | 在本讲中的作用 |
|------|----------------|
| [`rtl/aes.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v) | 顶层 wrapper。这里有完整的「时序块 `reg_update`」与「组合块 `api`」，展示两种寄存器变体：`block_reg`/`block_we`（数据寄存器）与 `init_reg`/`init_new`（脉冲寄存器） |
| [`rtl/aes_core.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v) | 核心状态机。这里能看到最标准的 `reg`/`_new`/`_we`「三件套」，以及一个组合块如何同时为一个寄存器算出「下一个值」和「写使能」 |

阅读建议：把这两个文件并排打开，对照本讲引用的行号看。本讲引用的所有行号均基于 HEAD `585f265`。

## 4. 核心概念与源码讲解

### 4.1 时序逻辑与异步复位

#### 4.1.1 概念说明

硬件里的「时序逻辑」指的是**由时钟驱动的、带记忆的电路**——也就是触发器（寄存器）。它在每个时钟跳变沿「采样」输入，把值保存下来，直到下一个时钟沿才可能再变。这和组合逻辑（纯逻辑门，输入一变输出立刻变，没有记忆）是完全不同的两类电路。

本工程里，所有需要「记住状态」的寄存器都写在这样一个统一模板里：

```verilog
always @ (posedge clk or negedge reset_n)
  begin : reg_update
    if (!reset_n)
      begin
        // 复位：把所有寄存器置成初始值
      end
    else
      begin
        // 正常工作：把 _new 搬进 _reg
      end
  end
```

这短短几行包含了三个关键信息：

1. **`posedge clk`**：时钟上升沿触发——触发器在时钟从低跳高的那一刻动作。
2. **`or negedge reset_n`**：复位也是沿触发的，而且是 `reset_n` 的下降沿（即 `reset_n` 从 1 变 0 的瞬间）。这种把复位写进敏感列表的写法，综合出来的是**异步复位**——复位一旦有效，立刻生效，**不必等下一个时钟沿**。
3. **`if (!reset_n)`**：因为 `reset_n` 低有效，所以 `!reset_n` 为真（即 `reset_n == 0`）时执行复位分支。复位分支里给所有寄存器赋「安全初值」（通常全 0）。

为什么要异步复位？这是 ASIC/FPGA 设计里很常见的选择：它让整片电路能在任意时刻被强制拉回已知状态（上电、异常恢复时尤其重要），而不依赖时钟是否在跑。代价是复位释放时刻需要小心（避免「复位释放」也踩在时钟沿上导致亚稳态），这属于进阶话题，本讲只需理解它的写法和含义。

#### 4.1.2 核心流程

把上面模板拆开，一个时序寄存器在每个时钟周期经历的流程是：

```text
                reset_n == 0 ?
                 /        \
               是          否
               /            \
      立即异步复位          等待时钟上升沿
   (_reg <= 安全初值)            │
                          上升沿到来
                               │
                      执行 else 分支：
                      _reg <= _new（受 _we 控制）
```

要点：

- 复位是「最高优先级」，只要 `reset_n` 为 0，无论时钟在干什么，寄存器都被钉在初值。
- 复位释放（`reset_n` 变 1）后，每个上升沿寄存器才可能根据 `else` 分支更新。
- `<=` 非阻塞赋值保证「同一段里所有寄存器在本次时钟沿结束后同时更新」，避免互相依赖造成的竞争。

#### 4.1.3 源码精读

在 [`rtl/aes.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L140-L181) 顶层 wrapper 里，所有寄存器的更新都集中在一个名为 `reg_update` 的时序块中（[aes.v:140-181](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L140-L181)）：

```verilog
always @ (posedge clk or negedge reset_n)
  begin : reg_update
    integer i;
    if (!reset_n)
      begin
        for (i = 0 ; i < 4 ; i = i + 1)
          block_reg[i] <= 32'h0;
        ...
        init_reg   <= 1'b0;
        ...
      end
    else
      begin
        ...
      end
```

这一段就是 4.1.1 模板的真实落地：敏感列表同时含 `posedge clk` 和 `negedge reset_n`，`if (!reset_n)` 分支把 `block_reg`、`key_reg`、`init_reg` 等全部清零（[aes.v:144-160](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L144-L160)）。

`rtl/aes_core.v` 用了完全相同的模板，并且在注释里把规则写得很明确（[aes_core.v:149-156](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L149-L156)）：

```verilog
// reg_update
//
// Update functionality for all registers in the core.
// All registers are positive edge triggered with asynchronous
// active low reset. All registers have write enable.
```

这段注释的中文意思正是：**所有寄存器都是「上升沿触发 + 异步低有效复位」，且都带写使能**。这三句话概括了本工程的全部寄存器风格，也是本讲接下来三节的纲领。对应的 `else` 分支见 [aes_core.v:164-174](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L164-L174)。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是让你亲手在代码里确认「复位」分支的存在。

1. **实践目标**：确认 `aes.v` 与 `aes_core.v` 都遵循「异步低有效复位」模板，并能指出复位时各寄存器被置成什么值。
2. **操作步骤**：
   - 打开 [`rtl/aes.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L140-L181)，定位第 140 行的 `always @ (posedge clk or negedge reset_n)`。
   - 在第 144-160 行的复位分支里，找出 `block_reg`、`key_reg`、`init_reg`、`result_reg`、`ready_reg` 各自被置成什么初值。
   - 再打开 [`rtl/aes_core.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L156-L175)（第 156 行起），看它的复位分支把 `aes_core_ctrl_reg` 置成哪个状态常量。
3. **需要观察的现象**：注意 `aes_core.v` 里 `ready_reg` 的复位初值是 `1'b1`（[aes_core.v:161](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L161)），而不是 `1'b0`——也就是说复位后核立刻就是「就绪」状态。
4. **预期结果**：你能填出下面这张小表：

   | 寄存器 | 复位初值 | 出处 |
   |--------|----------|------|
   | `block_reg` | `0` | aes.v 复位分支 |
   | `init_reg` | `0` | aes.v 复位分支 |
   | `aes_core_ctrl_reg` | `CTRL_IDLE`（即 `2'h0`） | aes_core.v 复位分支 |
   | `ready_reg`（core 内） | `1` | aes_core.v 复位分支 |

5. 如果无法确定运行结果，明确写「待本地验证」——本实践是阅读型，不涉及运行；但要真正看到复位效果，需结合 u1-l5 的仿真，在波形里把 `reset_n` 拉低再观察上述寄存器。

#### 4.1.5 小练习与答案

**练习 1**：如果把敏感列表里的 `or negedge reset_n` 删掉，只留 `always @(posedge clk)`，并把复位写成 `if (!reset_n)`，综合出来的还是异步复位吗？

> **答案**：不是。删掉 `negedge reset_n` 后，复位信号不再在敏感列表里，`if (!reset_n)` 只会在「时钟上升沿到来时」被检查到——这变成**同步复位**（复位要等到时钟沿才生效）。本工程用的是异步复位，所以必须把 `negedge reset_n` 放进敏感列表。

**练习 2**：为什么时序块里必须用非阻塞赋值 `<=` 而不是阻塞赋值 `=`？

> **答案**：时序块里多个寄存器应当「在同一时钟沿结束后同时更新」，彼此读到的都是「沿之前」的旧值。`<=`（非阻塞）正是这个语义；若用 `=`（阻塞），排在后面的语句会读到排在前面的语句刚刚改过的新值，造成难以预料的竞争和仿真/综合不一致。本工程所有时序块一律用 `<=`。

### 4.2 reg/_new/_we 寄存器写使能模式

#### 4.2.1 概念说明

这是本工程**最重要、最统一**的编码约定。先说直觉：

> 一个寄存器 `xxx_reg` 想「在满足条件时更新成某个新值」。本工程不把「算新值」和「搬进寄存器」混写在一起，而是拆成两段：
>
> - 用一个**组合逻辑块**算出两样东西：下一个值 `xxx_new`，以及「这一拍要不要更新」的写使能 `xxx_we`。
> - 用一个**时序逻辑块**在时钟沿上做一件简单的事：`if (xxx_we) xxx_reg <= xxx_new;`

这样做的好处是「关注点分离」：算新值的逻辑（往往很复杂，比如状态机下一状态怎么走）放在组合块里，可以被综合工具充分优化；时序块只负责机械地搬运，干净、规整、不易出错。而且只要看到一对 `xxx_new`/`xxx_we`，你就立刻知道「这个寄存器是被这套机制更新的」。

这里要先澄清一个容易混淆的点（也是本讲实践任务需要纠正的地方）：在本工程里，「`reg` + 伴随信号」其实有**三种变体**，分别用在三种场景：

| 变体 | 形态 | 何时用 | 典型例子 |
|------|------|--------|----------|
| 完整三件套 | `xxx_reg` + `xxx_new` + `xxx_we` | 下一值需要「算出来」**且**「按条件更新」 | `aes_core.v` 的 `aes_core_ctrl_reg` |
| 数据寄存器 | `xxx_reg` + `xxx_we`（无 `_new`） | 下一值直接就是某个输入（如 `write_data`），只需「按条件更新」 | `aes.v` 的 `block_reg` |
| 脉冲寄存器 | `xxx_reg` + `xxx_new`（无 `_we`） | 每拍都更新，但下一值可能是 0（一拍脉冲） | `aes.v` 的 `init_reg` |

下面三小节分别对照源码看这三种变体。其中「完整三件套」是后面所有子模块（`aes_encipher_block.v`、`aes_key_mem.v` 等）的标准写法，务必吃透。

#### 4.2.2 核心流程

三种变体共享同一个「两段式」结构，区别只在伴随信号有几根：

```text
变体A 完整三件套 (aes_core.v 状态机)
   组合块: 算出 aes_core_ctrl_new 和 aes_core_ctrl_we
            └─ 默认 _new=IDLE, _we=0；满足转移条件才 _new=下一状态, _we=1
                        │
                        ▼
   时序块: if (aes_core_ctrl_we) aes_core_ctrl_reg <= aes_core_ctrl_new;

变体B 数据寄存器 (aes.v 的 block_reg)
   组合块: 只算出 block_we（命中地址范围时为 1，新值就是 write_data，无需单独 _new）
                        │
                        ▼
   时序块: if (block_we) block_reg[地址] <= write_data;

变体C 脉冲寄存器 (aes.v 的 init_reg)
   组合块: 算出 init_new（命中 CTRL.init 位时为 1，否则为 0）
                        │
                        ▼
   时序块: init_reg <= init_new;   // 每拍都搬，无 _we
```

注意三种变体里「决定更新」的判断都发生在组合块，时序块只做机械搬运——这个分工始终一致。

#### 4.2.3 源码精读

**变体 A：完整三件套（在 `aes_core.v`）**

先看寄存器声明，每个状态/状态位寄存器都规规矩矩地配了三根（[aes_core.v:41-51](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L41-L51)）：

```verilog
reg [1 : 0] aes_core_ctrl_reg;
reg [1 : 0] aes_core_ctrl_new;
reg         aes_core_ctrl_we;

reg         result_valid_reg;
reg         result_valid_new;
reg         result_valid_we;

reg         ready_reg;
reg         ready_new;
reg         ready_we;
```

`_new` 和 `_we` 由组合块 `aes_core_ctrl`（4.3 节详讲）算出，比如默认值设在开头（[aes_core.v:241-242](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L241-L242)），状态转移时再覆盖（例如进入加密的 `CTRL_NEXT` 分支里 [aes_core.v:264-265](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L264-L265) 把 `_new` 设为 `CTRL_NEXT`、`_we` 设为 `1`）。

最后时序块把它们搬进寄存器（[aes_core.v:164-174](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L164-L174)）：

```verilog
if (aes_core_ctrl_we)
  aes_core_ctrl_reg <= aes_core_ctrl_new;
```

这就是「组合块算 `_new`/`_we`，时序块 `if (_we) _reg <= _new`」的标准写法。

**变体 B：数据寄存器（在 `aes.v` 的 `block_reg`）**

声明只有两根，没有 `block_new`（[aes.v:70-71](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L70-L71)）：

```verilog
reg [31 : 0] block_reg [0 : 3];
reg          block_we;
```

为什么不需要 `block_new`？因为新值就是主机送来的 `write_data`，没什么可「算」的。组合块 `api` 只需决定「要不要写」（[aes.v:214-215](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L214-L215)）：

```verilog
if ((address >= ADDR_BLOCK0) && (address <= ADDR_BLOCK3))
  block_we = 1'b1;
```

时序块在写使能有效时把 `write_data` 写进对应字（[aes.v:178-179](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L178-L179)）：

```verilog
if (block_we)
  block_reg[address[1 : 0]] <= write_data;
```

`key_reg`/`key_we`（[aes.v:73-74](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L73-L74)、[aes.v:175-176](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L175-L176)）和 `encdec_reg`/`keylen_reg` 共享的 `config_we`（[aes.v:66-68](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L66-L68)、[aes.v:169-173](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L169-L173)）都属于这一变体。

**变体 C：脉冲寄存器（在 `aes.v` 的 `init_reg`）**

声明是 `init_reg` 配 `init_new`，没有 `init_we`（[aes.v:60-61](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L60-L61)）：

```verilog
reg init_reg;
reg init_new;
```

组合块把 `init_new` 设成 `write_data` 的第 0 位（仅当主机写 `ADDR_CTRL` 时为 1，否则默认 0，见 [aes.v:204](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L204)）；时序块**每拍都无条件**把它搬进 `init_reg`（[aes.v:166](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L166)）：

```verilog
init_reg <= init_new;
```

效果是：主机写一次 `CTRL.init`，`init_reg` 拉高**一拍**（作为启动脉冲送给 `aes_core`），下一拍由于 `init_new` 默认回 0，`init_reg` 自动落下。`next_reg`/`next_new`（[aes.v:63-64](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L63-L64)）同理。

#### 4.2.4 代码实践

这是本讲的主实践。目标是用一张数据流小图，把「时钟上升沿 + 写使能有效 → 把新值写入寄存器」这条链路画清楚。**注意：`aes.v` 里没有 `block_new`，新值就是 `write_data`。**

1. **实践目标**：为变体 B（`block_reg`/`block_we`）画一张数据流图；再到 `aes_core.v` 为变体 A（完整三件套）画一张对照图，体会两者的异同。
2. **操作步骤**：
   - 打开 [`rtl/aes.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L178-L179)，在第 178-179 行找到 `block_reg` 的更新语句；再翻到 `api` 块第 214-215 行找到 `block_we` 被置 1 的条件。
   - 在纸上画出下面这张「变体 B」数据流图（用你自己的话标注每一步发生在哪一行）：

     ```text
     主机写: cs=1, we=1, address 落在 BLOCK 区间, write_data=明文某字
                          │
                          ▼
        ┌──── 组合块 api (always @*, aes.v:189-236) ────┐
        │  进入时先默认 block_we = 0            (L195)   │
        │  命中地址范围 → block_we = 1          (L214-215)│
        └────────────────────────────────────────────────┘
                          │ (block_we, write_data 都是组合输出)
                          ▼
        ┌──── 时序块 reg_update (posedge clk, aes.v:140) ────┐
        │  时钟上升沿到来, reset_n==1                         │
        │  if (block_we)                                     │
        │      block_reg[address[1:0]] <= write_data  (L178-179)│
        └────────────────────────────────────────────────────┘
                          │
                          ▼
        下一拍 block_reg 已更新 → 经 core_block (aes.v:105) 送入 aes_core
     ```

   - 接着打开 [`rtl/aes_core.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L172-L173)，为 `aes_core_ctrl_reg` 画一张「变体 A」对照图：组合块算出 `aes_core_ctrl_new`/`aes_core_ctrl_we`（默认 L241-242，转移时 L264-265），时序块 L172-173 搬运。
3. **需要观察的现象**：两张图里，「决定更新条件」都发生在组合块，时序块都只有一行 `if (_we) _reg <= ...`。区别在于变体 A 多一根「算出来的新值」`_new`，变体 B 的新值直接复用 `write_data`。
4. **预期结果**：你能口头复述——「`block_we` 是组合块根据地址算出的写使能；时钟沿到来且 `block_we=1` 时，`write_data` 被写进 `block_reg` 的对应字」。
5. 若想真正看到这一拍写入，需在仿真里（u1-l5）触发一次写 `ADDR_BLOCK0`，在波形里观察 `block_we` 拉高与 `block_reg[0]` 在下一沿变化的对应关系——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`init_reg` 为什么没有 `_we`？如果不写 `_we`，每拍都 `init_reg <= init_new`，会不会「一直保持高电平」？

> **答案**：不会。`init_new` 在组合块里默认是 0（[aes.v:191](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L191)），只有主机写 `ADDR_CTRL` 且对应位为 1 的那一拍才是 1。所以 `init_reg` 每拍都跟随 `init_new`：写的那一拍变 1，下一拍立刻回 0，正好形成一个单拍脉冲送给 `aes_core`。这正是「脉冲寄存器」的用途，因此不需要 `_we`。

**练习 2**：`encdec_reg` 和 `keylen_reg` 共用同一个 `config_we`（[aes.v:169-173](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L169-L173)）。这样做合理吗？为什么可以共用？

> **答案**：合理。`encdec`（加密/解密选择）和 `keylen`（密钥长度选择）都属于「配置」，二者都只在主机写 `ADDR_CONFIG` 时更新（[aes.v:208-209](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L208-L209)）。它们更新条件完全相同，所以共用一个 `config_we`；新值分别取 `write_data` 的不同位（`CTRL_ENCDEC_BIT` 与 `CTRL_KEYLEN_BIT`）。共用写使能既省逻辑也更清晰地表达了「它们是一组配置」。

**练习 3**：在完整三件套里，如果把时序块写成 `aes_core_ctrl_reg <= aes_core_ctrl_new;`（**去掉** `if (aes_core_ctrl_we)`），会发生什么？

> **答案**：状态机会出问题。`aes_core_ctrl_new` 的默认值是 `CTRL_IDLE`（[aes_core.v:241](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L241)），只在转移条件成立时才覆盖成别的状态。如果没有 `_we` 门控，那么每当状态机「不想转移」时，`_new` 仍是 `CTRL_IDLE`，下一拍会被无条件写进去——状态被强行拉回 `IDLE`，无法停留在 `CTRL_INIT`/`CTRL_NEXT`。`_we` 的作用正是「不想动时保持原值」，这是状态机能正常工作的关键。

### 4.3 组合逻辑 always @*

#### 4.3.1 概念说明

第二类 `always` 块是**组合逻辑块**，标志是敏感列表写成 `always @*`（或 `always @(*)`，等价）。`@*` 的意思是「这块里用到的所有输入信号，任何一个变了就重新求值」——综合出来的是纯组合逻辑（逻辑门 + 导线），**没有触发器、没有记忆**，输入一变输出立刻变。

在本工程里，组合块承担「算新值/算写使能/做多路选择」的任务，和 4.1 节的时序块（搬进寄存器）形成明确分工。你已经在 4.2 节看到了 `aes.v` 的 `api` 块、`aes_core.v` 的 `aes_core_ctrl` 块——它们都是组合块。

写组合块有一条**铁律**：在块的最开头，给所有被赋值的信号先写一遍「默认值」。原因是——如果某个信号在某些条件分支里没被赋值，综合工具会认为「它需要保持上次的值」，于是偷偷给你生成一个锁存器（latch），这通常是 bug。先写默认值能彻底避免这个陷阱。本工程每个组合块都严格遵守这条规矩。

另外，组合块里一律用**阻塞赋值 `=`**（与时序块的 `<=` 相反），表示「算完一个再算下一个」，符合组合逻辑的直觉。

#### 4.3.2 核心流程

一个标准的本工程组合块长这样：

```text
always @*
  begin : 块名
    // ① 先给所有输出写默认值（防 latch）
    signal_a = 默认值;
    signal_b = 默认值;
    // ② 再根据条件覆盖
    if (条件)
      signal_a = 某值;
    case (状态)
      ...
    endcase
  end
```

三个识别要点：

- 敏感列表是 `@*`（组合）。
- 开头先清零/写默认值。
- 全程用 `=` 阻塞赋值。

#### 4.3.3 源码精读

**例子 1：`aes.v` 的 `api` 块——命令译码（组合）**

这是顶层把「主机总线访问」翻译成各种 `_new`/`_we` 的组合块（[aes.v:189-236](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L189-L236)）。注意开头那一坨「全部置默认值」（[aes.v:191-196](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L191-L196)），正是 4.3.1 说的防 latch 写法：

```verilog
always @*
  begin : api
    init_new      = 1'b0;
    next_new      = 1'b0;
    config_we     = 1'b0;
    key_we        = 1'b0;
    block_we      = 1'b0;
    tmp_read_data = 32'h0;
    if (cs)
      ...
```

之后才根据 `cs/we/address` 覆盖这些默认值（例如写 `ADDR_CTRL` 时设 `init_new`/`next_new`，见 [aes.v:202-206](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L202-L206)；读操作用 `case` 选出 `tmp_read_data`，见 [aes.v:220-230](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes.v#L220-L230)）。这些 `_new`/`_we` 随后被 4.1 节的时序块 `reg_update` 消费。

**例子 2：`aes_core.v` 的多路选择块（组合）**

`aes_core.v` 里有三个独立的组合块，分别负责不同选择：`sbox_mux`（[aes_core.v:184-194](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L184-L194)）决定把共享的 S-box 分给谁，`encdec_mux`（[aes_core.v:203-224](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L203-L224)）决定走加密还是解密通路。以 `encdec_mux` 为例，它也是先写默认值再分支：

```verilog
always @*
  begin : encdec_mux
    enc_next = 1'b0;
    dec_next = 1'b0;
    if (encdec)
      begin
        enc_next = next; ...
      end
    else
      begin
        dec_next = next; ...
      end
  end
```

**例子 3：`aes_core.v` 的 `aes_core_ctrl` 块——状态机下一状态（组合）**

这是最复杂的组合块（[aes_core.v:234-303](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L234-L303)），它根据「当前状态 + 输入」算出 `aes_core_ctrl_new`/`aes_core_ctrl_we`、`ready_new`/`ready_we` 等。开头同样先把这一组信号全部置默认值（[aes_core.v:236-242](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L236-L242)）。这块是 4.2 节「完整三件套」里 `_new`/`_we` 的真正生产者。

#### 4.3.4 代码实践

1. **实践目标**：确认「组合块开头写默认值」这条规则，并理解它如何避免锁存器。
2. **操作步骤**：
   - 打开 [`rtl/aes_core.v`](https://github.com/abdelazeem201/ASIC-implementation-of-AES/blob/585f265f91338bea074cebb8c31a3eb602e7cdae/rtl/aes_core.v#L203-L224) 的 `encdec_mux` 块（第 203-224 行）。
   - 设想：如果删掉第 205-206 行的 `enc_next = 1'b0; dec_next = 1'b0;`，那么当 `encdec` 为某值时，另一个信号在 `if/else` 里都没被赋值——会发生什么？
3. **需要观察的现象**：被删默认值的信号在某些分支里「没有赋值」，综合器会推断它需要「保持原值」。
4. **预期结果**：综合工具会为该信号生成一个**锁存器（latch）**，这通常是非预期的（latch 容易引入毛刺、难以静态时序分析）。保留默认值后，该信号在每个分支都有确定值，综合出纯组合逻辑。结论：**组合块开头给所有输出写默认值是本工程防 latch 的标准手段**。
5. 本实践为阅读/推理型；若要用工具确认是否产生 latch，可在 u1-l5 的仿真或综合工具的告警里观察——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：怎么一眼区分 `aes_core.v` 里的某个 `always` 块是组合逻辑还是时序逻辑？

> **答案**：看敏感列表。`always @ (posedge clk or negedge reset_n)` 是时序逻辑（触发器）；`always @*` 是组合逻辑。此外，组合块用阻塞赋值 `=`，时序块用非阻塞赋值 `<=`，这也是辅助判断依据。`aes_core.v` 里 `reg_update`（L156）是时序，`sbox_mux`（L184）、`encdec_mux`（L203）、`aes_core_ctrl`（L234）都是组合。

**练习 2**：`aes_core.v` 的 `aes_core_ctrl` 块既是「状态机逻辑」却写成组合块，状态到底存在哪里？

> **答案**：状态本身存在触发器 `aes_core_ctrl_reg` 里（4.2 节变体 A）。组合块 `aes_core_ctrl` 只负责「读当前状态 `aes_core_ctrl_reg` + 输入，算出下一状态 `aes_core_ctrl_new` 和写使能 `aes_core_ctrl_we`」；真正改状态的是时序块 `reg_update` 里的 `if (aes_core_ctrl_we) aes_core_ctrl_reg <= aes_core_ctrl_new;`。这种「组合块算下一状态、时序块搬状态」正是「两段式状态机」写法，本工程所有 FSM 都这么写。

## 5. 综合实践

把本讲三个模块（时序逻辑、`reg/_new/_we` 模式、组合逻辑）串起来，做一次**端到端追踪**：主机如何通过一次总线写，让 `aes_core` 的状态机从 `IDLE` 进入 `INIT`。

任务：阅读下面这条「主机写 `ADDR_CTRL` 且 `init` 位置 1」的链路，在纸上把它走通，并标注每一步发生在哪个文件的哪一段：

```text
① 主机发起写: cs=1, we=1, address=ADDR_CTRL(0x08), write_data[0]=1 (init 位)
        │
        ▼
② 组合块 api (aes.v:189-236, always @*)
   默认 init_new=0 (L191)
   命中 address==ADDR_CTRL → init_new = write_data[CTRL_INIT_BIT] = 1  (L204)
        │ (init_new 立刻变成 1, 这是组合输出, 无延时)
        ▼
③ 时序块 reg_update (aes.v:140, posedge clk)
   init_reg <= init_new   (L166)   // 脉冲寄存器, 无 _we, 每拍都搬
        │ (时钟上升沿后, init_reg 变成 1)
        ▼
④ 经 assign core_init = init_reg (aes.v:107) 送到 aes_core 的 init 端口
        │
        ▼
⑤ aes_core 的组合块 aes_core_ctrl (aes_core.v:234-303, always @*)
   当前状态 aes_core_ctrl_reg == CTRL_IDLE, 且 init==1
   → aes_core_ctrl_new = CTRL_INIT, aes_core_ctrl_we = 1   (L254-255)
   → ready_new = 0, ready_we = 1                            (L250-251)
        │
        ▼
⑥ aes_core 时序块 reg_update (aes_core.v:156, posedge clk)
   if (aes_core_ctrl_we) aes_core_ctrl_reg <= aes_core_ctrl_new  (L172-173)
        │ (下一个时钟沿后, 状态真正变成 CTRL_INIT)
        ▼
⑦ 状态机进入 INIT, 开始驱动 key_mem 做密钥扩展 (后续 u2-l3 详讲)
```

观察要点（请边读边确认）：

- 整条链路里，**「算值」全是组合块**（②⑤），**「搬值」全是时序块**（③⑥），完美对应本讲的三模块。
- 从主机写入到 `aes_core` 状态真正改变，跨越了**两个时钟沿**（③ 和 ⑥各一个），这解释了为什么 `ready` 信号会有节拍延迟——主机发起 `init` 后不能立刻认为「开始扩展了」，要等握手（u1-l4、u3-l1 会展开）。
- `init_reg` 是一拍脉冲：⑥执行时 `init` 输入很可能已经回落为 0，但状态机靠自己的 `CTRL_INIT` 状态「记住」了正在扩展，不再依赖 `init` 信号——这正是状态机相对组合逻辑的优势。

预期产出：你能不看讲义，对着源码把这 7 步指给同学看。如果某一步对不上行号，回到对应小节复核。波形验证留到 u1-l5「运行仿真与阅读波形」。

## 6. 本讲小结

- 本工程所有寄存器都写在 `always @(posedge clk or negedge reset_n)` 里，采用**上升沿触发 + 异步低有效复位**（`reset_n` 低有效），复位分支把所有寄存器置已知初值（如 `aes_core_ctrl_reg` 置 `CTRL_IDLE`、core 的 `ready_reg` 置 `1`）。
- 核心编码约定是「**两段式**」：组合块算出下一个值 `_new` 和写使能 `_we`，时序块在时钟沿上 `if (_we) _reg <= _new` 机械搬运——这是阅读所有子模块的通用钥匙。
- 本工程的 `reg` 伴随信号有三种变体：完整三件套 `reg/_new/_we`（如 `aes_core_ctrl`）、数据寄存器 `reg/_we`（如 `block_reg`，新值即 `write_data`，**无 `_new`**）、脉冲寄存器 `reg/_new`（如 `init_reg`，每拍都搬，形成单拍脉冲）。
- 组合逻辑用 `always @*` + 阻塞赋值 `=`，且**块开头先给所有输出写默认值**以防生成锁存器；`aes.v` 的 `api` 块、`aes_core.v` 的三个 mux/ctrl 块都是范例。
- 时序块一律用非阻塞赋值 `<=`，组合块一律用阻塞赋值 `=`，两者绝不混用。
- 状态机采用「组合块算下一状态 + 时序块搬状态」的标准两段式写法，状态本身存在 `_reg` 触发器里。

## 7. 下一步学习建议

本讲建立的是「读 Verilog 的通用语法/风格基础」。下一步建议按顺序：

1. **u1-l4「顶层接口与寄存器地址映射」**：本讲的 `api` 块已经在做命令译码，u1-l4 会把 `aes.v` 的端口（`cs/we/address/write_data/read_data`）和 `0x00~0x33` 的地址映射（NAME/CTRL/STATUS/CONFIG/KEY/BLOCK/RESULT）系统讲一遍，是理解「主机怎么驱动这个核」的关键。
2. **u1-l5「运行仿真与阅读波形」**：本讲多次提到「波形验证待本地」，u1-l5 会用 `tb_aes.v` 教你真正跑起来，亲眼看到 `reset_n`、`block_we`、`ready` 在时钟沿上的变化。
3. 进入进阶篇（单元二）后，你会频繁遇到这里的 `reg/_new/_we` 三件套和两段式状态机——届时可回看本讲 4.2 节作为速查表。建议优先读 `u2-l1 aes_core 顶层控制与状态机`，它正是本讲 `aes_core_ctrl` 块的完整展开。
