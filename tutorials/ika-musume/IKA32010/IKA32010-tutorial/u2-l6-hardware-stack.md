# 硬件堆栈 IKA32010_stack

## 1. 本讲目标

本讲聚焦 IKA32010 内部那块「4 级硬件堆栈」子模块 `IKA32010_stack`。学完后你应当能够：

- 说清 4 级堆栈是用「移位寄存器」而不是「RAM + 栈指针」实现的，并解释 push/pop/hold 三种动作各自如何搬动数据；
- 读懂驱动堆栈的三个控制信号 `stk_push` / `stk_pop` / `stk_data_sel`，以及 `STACK_DATA_PC` / `STACK_DATA_ACC` 两个常量的含义；
- 理解堆栈宽度为什么是 12 位，以及在 CALL / RET / CALA / PUSH / POP / 中断响应中，堆栈分别被谁压入、弹出什么数据。

本讲只讲「栈这个硬件部件本身」，不展开指令译码的全貌（那是 u3-l1 之后的事），也不展开中断完整时序（留给 u3-l3）。

## 2. 前置知识

在进入正文前，先确认你已经建立下面几个概念（它们来自前置讲义）：

- **机器周期与节拍**：`cyclecntr` 在 `0→1→2→3` 循环，4 个 `i_EMUCLK` 构成 1 个 DSP 机器周期。其中 `cyc_ncen`（`cyclecntr==3`，相位 3）是核心的「工作拍」，PC、堆栈、ALU 写回等几乎所有状态更新都发生在 `cyc_ncen` 的上升沿。见 u1-l4。
- **程序计数器 `if_pc`**：12 位，对应 4K 字程序空间，在 `cyc_ncen` 边沿按 `if_pc_modesel` 更新。见 u2-l2。
- **内部写总线 `reg_wrbus`**：16 位共享数据汇流，有一个写入点（选源 MUX）和多个读取点。见 u2-l1。
- **水平微码风格**：顶层用一个组合 `always @(*)` 块，先给所有控制信号赋「默认值」，再用 `casez(if_opcodereg)` 按指令覆盖。见 u3-l1 会总览，本讲只需接受「默认值 + 按需覆盖」这一写法。

如果你还不太熟悉「栈（stack）」这个词本身：它是一种「后进先出」（LIFO, Last-In First-Out）的存储结构。想象一摞盘子，你只能往最上面放、从最上面取——最后放上去的盘子会被最先取走。CPU 用它来临时保存「回来以后还要用」的信息，最典型的就是「调用子程序前的返回地址」。

## 3. 本讲源码地图

本讲涉及两个源文件：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/IKA32010.sv` | 顶层模块 + 四个子模块 | 末尾的 `IKA32010_stack` 子模块、顶层的堆栈实例化、微码里驱动堆栈的信号 |
| `src/IKA32010_mnemonics.sv` | 常量字典 | `STACK_DATA_PC` / `STACK_DATA_ACC` 两个常量 |

需要精读的代码点（行号基于当前 HEAD `51bc1f0`）：

- `IKA32010_stack` 子模块本体：`src/IKA32010.sv:1943-1982`
- 顶层对堆栈的实例化：`src/IKA32010.sv:412-416`
- 微码里堆栈信号的默认值：`src/IKA32010.sv:579-581`
- `STACK_DATA_*` 常量：`src/IKA32010_mnemonics.sv:36-38`
- 各指令对堆栈的驱动：CALL `1422-1440`、CALA `1401-1420`、RET `1442-1461`、PUSH `713-732`、POP `690-711`、中断响应前置 `611-616`、IACK `624-636`

## 4. 核心概念与源码讲解

### 4.1 IKA32010_stack 子模块：移位式 4 级堆栈

#### 4.1.1 概念说明

`IKA32010_stack` 是一块 **4 级、每级 12 位** 的硬件堆栈。它要解决的问题是：当 CPU 调用子程序（CALL）或响应中断时，需要把「回来以后要继续执行的地址」存到一个安全的地方，等子程序返回（RET）时再取回来。

实现栈有两种常见思路：

1. **RAM + 栈指针（SP）**：拿一块内存当栈区，用一个「栈指针」寄存器记录栈顶位置。push 时 SP 移动并写入，pop 时读出并移动 SP。通用 CPU（如 x86、RISC-V）多用这种，栈深度可以是几千几万。
2. **移位寄存器栈（hardware stack）**：用固定数量的寄存器排成一行，push/pop 时让数据在寄存器之间整体移位。没有独立的栈指针——「栈顶」永远是同一个物理寄存器。

IKA32010 选择的是第 2 种，而且只做 **4 级**。这忠实复刻了原始 TMS32010 的硬件栈（TMS32010 的栈也是 4 级深度）。

为什么 DSP 核常用这种「又浅又是移位式」的栈？

- **快**：栈顶直接组合输出（`o_DOUT = stack[0]`），不需要先读 RAM 再等一拍，RET 取返回地址是「当拍可用」。
- **省控制**：没有 SP 寄存器要维护，也没有「栈空 / 栈满」判断逻辑——电路非常简单。
- **够用**：DSP 程序的子程序嵌套通常很浅，4 级足以应付绝大多数实时信号处理场景。

代价是：**深度被钉死在 4 级**，且**没有溢出 / 下溢检测**（后面 4.1.2 会细说）。

#### 4.1.2 核心流程

子模块对外只有三个动作，由 `{i_PUSH, i_POP}` 这个 2 位拼接值决定：

| `{PUSH,POP}` | 动作 | 数据怎么搬 |
| --- | --- | --- |
| `2'b10` | push（压栈） | 新数据进 `stack[0]`，其余整体「向下」挤 |
| `2'b01` | pop（出栈） | 数据整体「向上」提，`stack[0]` 输出后被覆盖 |
| 其它（`2'b00`/`2'b11`） | hold（保持） | 全部不变 |

把 `stack[0..3]` 想象成一摞从上到下排列的盒子（`stack[0]` 在最顶），搬动规则用公式写出来：

压栈（push），输入数据 `D = i_DIN`：

\[
S'[0]=D,\quad S'[1]=S[0],\quad S'[2]=S[1],\quad S'[3]=S[2]
\]

即所有数据下移一格，原来最底的 `S[3]` 被「挤掉」丢失。这正是 LIFO 的压栈语义。

出栈（pop）：

\[
S'[0]=S[1],\quad S'[1]=S[2],\quad S'[2]=S[3],\quad S'[3]=S[3]
\]

即所有数据上移一格，原来的栈顶 `S[0]` 通过 `o_DOUT` 输出后被覆盖；最底的 `S[3]` 被「复制」到 `S'[2]` 和 `S'[3]` 两处——这相当于「栈底恒定保持原值」，是移位式栈处理「栈越来越空」的惯用手法。

由此带来两个重要特性：

- **没有「空 / 满」标志**：栈永远装着 4 个值（复位后全是 0）。对一个「空栈」做 pop，会读出 0；对一个「满栈」做 push，会丢掉最底层的值。CPU 自己负责不越界使用。
- **栈顶输出是组合的**：`o_DOUT = stack[0]` 不经过时钟，意味着 RET 指令在取返回地址的当拍就能拿到栈顶值，无需等待。

更新时机：所有 push/pop 都发生在 `posedge i_EMUCLK` 且 `i_CEN`（即 `cyc_ncen`）为高的边沿——与 PC、ALU 写回共享同一个主工作拍。复位 `i_RST_n` 拉低时，4 级全部清零。

#### 4.1.3 源码精读

子模块完整定义在文件末尾。先看端口与存储体：

[栈存储声明与组合输出 — src/IKA32010.sv:1955-1956](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1955-L1956)

```systemverilog
reg     [11:0]  stack[0:3];   // 4 个 12 位寄存器，stack[0] 是栈顶
assign  o_DOUT = stack[0];    // 栈顶组合输出，无时钟延迟
```

`stack[0:3]` 是 4 个独立的 12 位寄存器；`o_DOUT` 直接接 `stack[0]`，所以「读栈顶」不花任何周期。

再看核心的更新逻辑：

[push/pop/hold 三态移位 — src/IKA32010.sv:1958-1978](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1958-L1978)

```systemverilog
always @(posedge i_EMUCLK) begin
    if(!i_RST_n) begin
        stack[0] <= 12'h000;  stack[1] <= 12'h000;
        stack[2] <= 12'h000;  stack[3] <= 12'h000;   // 复位：全部清零
    end
    else begin
        if(i_CEN) begin
            case({i_PUSH, i_POP})
                2'b10: begin
                    stack[0]<=i_DIN; stack[1]<=stack[0]; stack[2]<=stack[1]; stack[3]<=stack[2]; //push 下移
                end
                2'b01: begin
                    stack[0]<=stack[1]; stack[1]<=stack[2]; stack[2]<=stack[3]; stack[3]<=stack[3]; //pop 上移
                end
                default: begin
                    stack[0]<=stack[0]; stack[1]<=stack[1]; stack[2]<=stack[2]; stack[3]<=stack[3]; //hold
                end
            endcase
        end
    end
end
```

逐行对照 4.1.2 的公式：

- `2'b10` 分支正是 push：`stack[0]` 收新输入 `i_DIN`，其余各自接收「上一拍的上一层」，最底 `stack[3]` 接收旧的 `stack[2]`，旧的 `stack[3]` 无人接收 → 丢失。
- `2'b01` 分支正是 pop：每层接收「下一层」的值，`stack[3]` 自己接收自己（保持），模拟「栈底不动」。
- `default` 是 hold：每层自保。注意 `{PUSH,POP}=2'b11`（同时压又弹）会落到这个 default，等价于 hold——源码里永远不会产生 `2'b11`，所以这是一个安全的兜底。

`i_CEN` 就是顶层接进来的 `cyc_ncen`（见 4.2.3 的实例化），所以「移位」只在相位 3 那个主工作拍发生，每机器周期最多移一次。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「复位清零」与「push 一次后栈内容如何分布」。

**操作步骤**（源码阅读型 + 仿真型，二选一）：

1. 打开 `src/IKA32010_tb.v`，确认 DUT 实例名为 `main`（第 30 行 `IKA32010 main (...)`）。
2. 因为 `stack[0:3]` 是子模块内部寄存器，没有端口引出，最不侵入的观察方式是在 testbench 里用**层次路径引用**（hierarchical reference）直接读它。在 testbench 任意 `initial` 块里加一段按周期打印的代码（示例代码，非项目原有）：

   ```systemverilog
   // 示例代码：在 IKA32010_tb.v 里追加，用于打印栈内容
   always @(posedge EMUCLK) begin
       if (main.u_stack.i_CEN)   // 只在每个机器周期的工作拍打印一次
           $display("t=%0t  stack[0..3]=%h %h %h %h",
                    $time,
                    main.u_stack.stack[0], main.u_stack.stack[1],
                    main.u_stack.stack[2], main.u_stack.stack[3]);
   end
   ```

3. 复位阶段（testbench 第 15-20 行的 `RS_n` 低电平窗口）应能看到 4 个值都变成 `000`。

**需要观察的现象**：复位有效期间 `stack[0..3]` 全为 `0`；复位释放、尚未执行任何 push 时它们仍保持 `0`。

**预期结果**：复位清零确认无误；这同时也说明「空栈的值是 0」，后续 pop 一个从未 push 过的栈会得到 0。

**说明**：仓库里的 testbench 用的是 Windows 绝对路径加载 ROM（第 63、82-83 行 `$readmemh("D:/...")`），且 ROM 文件不在仓库内，因此本实践在当前 Linux 环境下**待本地验证**——你需要自备一段最简程序 ROM（哪怕几条 NOP）才能跑通仿真。若暂无仿真器，也可只做纯源码阅读：对照 4.1.3 的三条 case 分支，手工模拟「连续 push 两个不同值」后 `stack[0..3]` 的内容。

#### 4.1.5 小练习与答案

**练习 1**：若栈当前内容是 `stack[0..3] = A, B, C, D`，再连续 push 两个值 `X`、`Y`，最终 `stack[0..3]` 是什么？`A` 和 `B` 去哪了？

**参考答案**：push `X` 后 → `X, A, B, C`（`D` 丢失）；再 push `Y` 后 → `Y, X, A, B`。`A` 还在 `stack[2]`，`B` 在 `stack[3]`，而最初的 `C`、`D` 已被挤出丢失。这说明「栈满后再压栈会从底部丢弃老数据」。

**练习 2**：栈当前 `stack[0..3] = P, Q, R, S`，连续 pop 两次后内容是什么？`S` 的值有没有变过？

**参考答案**：第一次 pop → `Q, R, S, S`；第二次 pop → `R, S, S, S`。`S` 自始至终在最底层「自我复制」，值不变——这正是 pop 分支 `stack[3]<=stack[3]` 的效果。

**练习 3**：子模块里有没有任何判断「栈空」「栈满」的逻辑？

**参考答案**：没有。`stack[0:3]` 永远持有 4 个值，既无空满标志位，也无 SP 寄存器。是否越界使用，完全由调用方（指令/程序）自己负责。

---

### 4.2 stk_push / stk_pop / stk_data_sel：堆栈的控制三件套与数据来源

#### 4.2.1 概念说明

子模块本身只是个「会移位的 4 格盒子」，它不知道何时该 push、何时该 pop、更不知道该把什么数据压进去。这三件事由顶层微码用三个信号告诉它：

- `stk_push`（1 位）：本拍是否压栈。
- `stk_pop`（1 位）：本拍是否出栈。
- `stk_data_sel`（1 位）：压栈时，数据来自 PC 还是累加器路径。这就是 `STACK_DATA_PC` / `STACK_DATA_ACC` 两个常量发挥作用的地方。

注意一个常被忽略的点：**`stk_data_sel` 只在 push 时有意义**。pop 操作的数据流方向是「栈顶 → 外部」，子模块不需要输入数据；你在源码里会看到 RET、POP 等弹栈指令也写了 `stk_data_sel = STACK_DATA_ACC`，那只是给一个无害的默认值，对 pop 行为没有任何影响。

#### 4.2.2 核心流程

三个信号都在顶层那个大的组合 `always @(*)` 微码块里产生，遵循「先默认值，后 casez 覆盖」：

1. **默认值**（每个机器周期开头先赋一次）：
   - `stk_data_sel = STACK_DATA_PC`
   - `stk_push = NO`、`stk_pop = NO`
   即「默认既不压也不弹；若要压，默认压 PC」。

2. **中断前置**：在进入 `casez` 之前，若检测到中断请求 `int_rq`，先把 `stk_push` 改成 `YES`、`stk_data_sel` 维持 `STACK_DATA_PC`——为中断压入返回地址。

3. **指令覆盖**：`casez(if_opcodereg)` 里，只有真正需要动栈的指令（CALL / CALA / RET / PUSH / POP）才会改写这三个信号；其余绝大多数指令沿用默认的「不压不弹」。

两个数据源常量定义在 mnemonics 文件：

[STACK_DATA 常量 — src/IKA32010_mnemonics.sv:36-38](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L36-L38)

```systemverilog
//stack data
localparam  STACK_DATA_ACC  = 1'b0;   // 压栈数据来自累加器路径（reg_wrbus[11:0]）
localparam  STACK_DATA_PC   = 1'b1;   // 压栈数据来自程序计数器 if_pc
```

#### 4.2.3 源码精读

先看微码块顶部给三个信号赋的默认值：

[堆栈信号默认值 — src/IKA32010.sv:579-581](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L579-L581)

```systemverilog
//stack
stk_data_sel = STACK_DATA_PC;
stk_pop = NO; stk_push = NO;
```

再看顶层的实例化，它把三个信号、数据源 MUX、输出都连到了子模块：

[堆栈实例化与输入数据选择 — src/IKA32010.sv:412-416](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L412-L416)

```systemverilog
IKA32010_stack u_stack (
    .i_EMUCLK(i_EMUCLK), .i_RST_n(i_RS_n), .i_CEN(cyc_ncen),
    .i_PUSH(stk_push), .i_POP(stk_pop),
    .i_DIN(stk_data_sel ? if_pc : reg_wrbus[11:0]), .o_DOUT(stk_output)
);
```

这一行是本讲最关键的一句话：

```systemverilog
.i_DIN(stk_data_sel ? if_pc : reg_wrbus[11:0])
```

- `stk_data_sel == STACK_DATA_PC`（1）→ 压入 `if_pc`（返回地址）。CALL / CALA / 中断走这条路。
- `stk_data_sel == STACK_DATA_ACC`（0）→ 压入 `reg_wrbus[11:0]`（写总线低 12 位）。PUSH 走这条路，此时 `reg_wrbus` 来自移位器 B（即累加器），见 4.3。

注意 `.i_CEN(cyc_ncen)`：再次确认堆栈只在相位 3 这个主工作拍移位。`.i_RST_n(i_RS_n)` 直接把顶层复位接到子模块复位。

栈顶输出 `stk_output`（12 位）会被 `reg_wrbus` 的选源 MUX 选用，见：

[reg_wrbus 选 STACK_DATA_STACK 时的拼接 — src/IKA32010.sv:138](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L138)

```systemverilog
WRBUS_SOURCE_STACK   : reg_wrbus = {4'h0, stk_output};
```

栈顶 12 位被零扩展到 16 位送上 `reg_wrbus`，供 RET、POP 取用（详见 4.3）。

#### 4.2.4 代码实践

**实践目标**：把源码里所有对 `stk_push` / `stk_pop` / `stk_data_sel` 的赋值整理成一张表，验证「`stk_data_sel` 只在 push 时才起作用」。

**操作步骤**（源码阅读型）：

1. 在 `src/IKA32010.sv` 中搜索 `stk_push`、`stk_pop`、`stk_data_sel`（共约 30 处）。
2. 为每条指令记录三信号取值。下表是一个起头示例（请你补全 CALL / CALA / 中断）：

   | 指令 / 场景 | 行号 | stk_push | stk_pop | stk_data_sel | 含义 |
   | --- | --- | --- | --- | --- | --- |
   | 默认值 | 580-581 | NO | NO | PC | 不动栈 |
   | 中断请求 `int_rq` | 615-616 | YES | NO | PC | 压入返回地址 |
   | IACK | 632 | NO | NO | ACC | 应答，不动栈 |
   | POP（cycle0） | 702 | NO | YES | ACC | 弹栈到 ACC |
   | PUSH（cycle0） | 723 | YES | NO | ACC | 压入累加器低 12 位 |
   | CALL（cycle0） | 1431 | YES | NO | PC | 压入返回地址 |
   | RET（cycle0） | 1452 | NO | YES | ACC | 弹栈到 PC |

3. 重点核对：凡是 `stk_pop=YES` 的行（POP、RET），`stk_data_sel` 都写的是 `STACK_DATA_ACC`。结合 4.2.1，说明这个值在这些指令里**不起作用**——pop 不需要输入数据。

**需要观察的现象**：所有「弹栈」指令的 `stk_data_sel` 取值与 pop 行为无关；只有「压栈」指令（CALL/CALA/中断/PUSH）的 `stk_data_sel` 才真正决定压进去的是 PC 还是 ACC。

**预期结果**：你整理出的表里，`stk_data_sel=STACK_DATA_PC` 只出现在 push 场景（CALL/CALA/中断）；`STACK_DATA_ACC` 出现在 PUSH（push 但压 ACC）以及所有 pop 场景（取值无关紧要）。

#### 4.2.5 小练习与答案

**练习 1**：默认值为什么把 `stk_data_sel` 设成 `STACK_DATA_PC` 而不是 `STACK_DATA_ACC`？

**参考答案**：因为堆栈最主要的用途是保存「返回地址」（CALL/CALA/中断都需要压 PC）。把默认值设成 PC，意味着只要微码把 `stk_push` 改成 YES、忘记改 `stk_data_sel`，压入的也是「最常用的 PC」，是一种安全的兜底（例如中断前置块 line 615-616 就只改 `stk_push`，复用了默认的 PC）。

**练习 2**：RET 里写了 `stk_data_sel = STACK_DATA_ACC`（line 1452），这一句如果删掉，RET 还能正常工作吗？

**参考答案**：能。因为 RET 是 pop（`stk_pop=YES`），`stk_data_sel` 只影响 push 的输入数据，对 pop 完全无效。删掉后 `stk_data_sel` 会沿用默认值 `STACK_DATA_PC`，但 RET 不压栈，所以无影响。源码里写上 `STACK_DATA_ACC` 只是「显式归零」的代码风格。

**练习 3**：实例化里 `.i_DIN(stk_data_sel ? if_pc : reg_wrbus[11:0])`，为什么取的是 `reg_wrbus[11:0]` 而不是整个 `reg_wrbus`？

**参考答案**：因为栈的存储体 `stack[0:3]` 每格只有 12 位（`reg [11:0]`），`i_DIN` 端口也是 12 位。累加器是 16 位，PUSH 时只能保存其低 12 位，高 4 位被截断——这是 12 位栈宽度带来的固有限制，4.3 会再强调。

---

### 4.3 堆栈在 CALL / RET / 中断 / PUSH / POP 中的协作

#### 4.3.1 概念说明

有了 4.1 的「会移位的盒子」和 4.2 的「三个控制旋钮」，现在把它们拼成完整的语义：每一条会动栈的指令，到底把什么数据从哪儿搬到哪儿。

记住两条对称的数据通路：

- **压栈方向（push）**：数据来自 `if_pc`（PC）或 `reg_wrbus[11:0]`（写总线），由 `stk_data_sel` 选择，进入 `stack[0]`。
- **出栈方向（pop）**：`stack[0]` 经 `stk_output` → `reg_wrbus`（选 `WRBUS_SOURCE_STACK`），再由各消费者取用（RET 送去更新 PC，POP 送去更新 ACC）。

#### 4.3.2 核心流程

把五类用法总结成下表（均为各自指令的 cycle0，即执行拍）：

| 指令 | push/pop | 数据源 → 目的 | 配套动作 |
| --- | --- | --- | --- |
| **CALL**（直接调用） | push | `if_pc`（返回地址）→ 栈 | PC 从指令字加载目标地址（`PC_LOAD_IMMEDIATE`） |
| **CALA**（间接调用，目标在 ACC） | push | `if_pc` → 栈 | PC 从 ACC（经 SHB/写总线）加载（`PC_LOAD_WRBUS`） |
| **RET**（子程序返回） | pop | 栈顶 → `if_pc` | `WRBUS_SOURCE_STACK` + `PC_LOAD_WRBUS` |
| **PUSH**（ACC 入栈） | push | 累加器（经移位器 B → 写总线）→ 栈 | `WRBUS_SOURCE_SHB` |
| **POP**（出栈到 ACC） | pop | 栈顶 → 累加器 | `WRBUS_SOURCE_STACK`，ALU 把栈顶装入 ACC |
| **中断响应** | push | `if_pc`（断点地址）→ 栈 | PC 跳到向量 `0x002`；随后内部 IACK 应答 |

注意 CALL / CALA 与 RET 是**严格对称**的：CALL 压入 `if_pc`，RET 弹出回 `if_pc`。因此「压进去什么值，回来时 PC 就是什么值」——无论那个返回地址的具体数值如何（受取指流水线时序影响），这一对称性都保证子程序能回到正确的位置。

中断稍微特殊：它不是某条用户指令，而是在译码块开头（`casez` 之前）由 `int_rq` 触发的强制动作——压入断点地址、跳到 `0x002`。随后 CPU 会执行一个「内部操作码」IACK（`0xF000`）来完成中断应答（清 `int_latched`）。完整的同步链与应答时序属于 u3-l3，本讲只关注它「也用到了栈、压的是 PC」。

#### 4.3.3 源码精读

**CALL —— 压入返回地址、跳转**：

[CALL cycle0 — src/IKA32010.sv:1424-1432](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1424-L1432)

```systemverilog
if(ex_inst_cycle == 2'd0) begin
    busctrl_req = DATA_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    if_pc_modesel = PC_LOAD_IMMEDIATE;       // 下一拍 PC←目标地址（指令字读回的 i_DIN）
    ...
    stk_push = YES; stk_data_sel = STACK_DATA_PC;   // 同时把当前 if_pc 压栈
end
```

CALL 是 2 字指令：cycle0 这拍从程序区读出目标地址（`DATA_READ`），既把 PC 装载到目标（`PC_LOAD_IMMEDIATE`），又把当前的 `if_pc` 压入栈。两件事发生在同一个 `cyc_ncen` 边沿：栈收到的是更新前的 `if_pc`，PC 同时跳走。

**RET —— 弹出栈顶回 PC**：

[RET cycle0 — src/IKA32010.sv:1444-1452](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1444-L1452)

```systemverilog
if(ex_inst_cycle == 2'd0) begin
    ...
    if_pc_modesel = PC_LOAD_WRBUS;                    // PC←写总线
    register_wrbus_source_sel = WRBUS_SOURCE_STACK;   // 写总线←栈顶
    ...
    stk_push = NO; stk_pop = YES; stk_data_sel = STACK_DATA_ACC;  // 弹栈
end
```

RET 建立了「栈顶 → `reg_wrbus` → `if_pc`」的通路：选源 MUX 取 `WRBUS_SOURCE_STACK`（4.2.3 已见 `{4'h0, stk_output}`），PC 模式选 `PC_LOAD_WRBUS`（`if_pc <= reg_wrbus[11:0]`），同时 `stk_pop=YES` 把栈顶弹掉。这就完成了与 CALL 的对称返回。

**CALA —— 与 CALL 同样压 PC，但目标来自累加器**：

[CALA cycle0 — src/IKA32010.sv:1403-1411](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1403-L1411)

```systemverilog
if(ex_inst_cycle == 2'd0) begin
    ...
    if_pc_modesel = PC_LOAD_WRBUS;                     // PC←写总线（写总线来自累加器）
    register_wrbus_source_sel = WRBUS_SOURCE_SHB; shb_mux = LOW;  // 写总线←移位器B（ACC 低字）
    ...
    stk_push = YES; stk_data_sel = STACK_DATA_PC;     // 同样压 if_pc
end
```

CALA 与 CALL 的**栈操作完全相同**（都压 `if_pc`），区别只在跳转目标来源：CALL 从程序区读目标，CALA 从累加器取目标。

**PUSH —— 压累加器低 12 位**：

[PUSH cycle0 — src/IKA32010.sv:715-723](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L715-L723)

```systemverilog
if(ex_inst_cycle == 2'd0) begin
    ...
    register_wrbus_source_sel = WRBUS_SOURCE_SHB;   // 写总线←移位器B（ACC）
    ...
    stk_push = YES; stk_data_sel = STACK_DATA_ACC;  // 压 reg_wrbus[11:0]（ACC 低 12 位）
end
```

这里 `stk_data_sel = STACK_DATA_ACC`，于是实例化的 MUX 选 `reg_wrbus[11:0]`，而 `reg_wrbus` 又选 `WRBUS_SOURCE_SHB`（移位器 B 输出，即累加器经移位后的值）。注意只保留低 12 位——累加器高 4 位会丢失，这是 12 位栈宽度的固有代价。

**POP —— 弹栈顶到累加器**：

[POP cycle0 — src/IKA32010.sv:692-702](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L692-L702)

```systemverilog
if(ex_inst_cycle == 2'd0) begin
    ...
    register_wrbus_source_sel = WRBUS_SOURCE_STACK;  // 写总线←栈顶
    alu_modesel = ALU_ADD; alu_paz = YES; alu_pbdata = ALU_PBDATA_LOWWORD; // ACC←端口B（低字）
    alu_acc_ld = YES;
    ...
    stk_push = NO; stk_pop = YES; stk_data_sel = STACK_DATA_ACC;  // 弹栈
end
```

POP 复用了 ALU 来「装」数据：栈顶经 `reg_wrbus` 进入 ALU 的端口 B，`alu_paz=YES` 把端口 A 置零，于是 `ACC = 0 + 栈顶`，等价于「把栈顶装入累加器」，同时弹栈。

**中断响应 —— 在译码块开头强制压 PC**：

[中断前置 — src/IKA32010.sv:611-616](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L611-L616)

```systemverilog
else begin
    //interrupt check
    if_opcodereg_force_iack = (int_rq) ? YES : NO;
    if_pc_modesel           = (int_rq) ? PC_LOAD_INTERRUPT : PC_INCREASE;  // 跳到 0x002
    stk_push                = (int_rq) ? YES : NO;                         // 压断点地址
    stk_data_sel            = (int_rq) ? STACK_DATA_PC : STACK_DATA_ACC;
```

这段在 `casez` 之前，所以它对**任何**当前指令都生效：只要 `int_rq`（中断请求有效），就强制压入 `if_pc`（断点地址）、PC 跳到 `0x002`、并把下一拍的指令寄存器替换成内部 IACK。完整的中断同步链（`int_n_z/zz/zzz`）与 IACK 应答时序见 u3-l3。

#### 4.3.4 代码实践

**实践目标**：追踪 PUSH 与 POP 这对指令的完整数据通路，验证「压进去的值 = 弹出来的值（截断到 12 位）」。

**操作步骤**（源码阅读型）：

1. 假设累加器 ACC = `0xABCD`。
2. 沿 PUSH 的数据流走一遍：ACC → 移位器 B（`WRBUS_SOURCE_SHB`，移位量 0）→ `reg_wrbus = 0xABCD` → 实例化 MUX 取 `reg_wrbus[11:0]` = `0xBCD` → 压入 `stack[0]`。
3. 紧接着沿 POP 的数据流走：`stack[0] = 0xBCD` → `stk_output` → `reg_wrbus = {4'h0, 0xBCD} = 0x0BCD` → ALU 端口 B 低字 → `ACC = 0 + 0x0BCD = 0x0BCD`。
4. 对比 PUSH 前的 `0xABCD` 与 POP 后的 `0x0BCD`：高 4 位 `0xA` 被截断了。

**需要观察的现象**：PUSH+POP 一对操作后，累加器值并非完全还原——高 4 位丢失，低 12 位保留。

**预期结果**：`0xABCD` → PUSH → POP → `0x0BCD`。这从数据通路层面印证了「12 位栈宽度」对 PUSH/POP（保存累加器）这一用法的限制；而对 CALL/RET（保存返回地址）则没有影响，因为 PC 本身就是 12 位。

#### 4.3.5 小练习与答案

**练习 1**：CALL 和 CALA 在「栈操作」上有何异同？

**参考答案**：**完全相同**——两者都在 cycle0 把 `stk_push=YES`、`stk_data_sel=STACK_DATA_PC`，即都压入当前的 `if_pc`（返回地址）。区别仅在于跳转目标来源：CALL 的目标地址编码在指令字里（从程序区读，`PC_LOAD_IMMEDIATE`），CALA 的目标在累加器里（`PC_LOAD_WRBUS`，来自移位器 B）。

**练习 2**：为什么说「CALL 压什么、RET 就弹什么」？请用 RET 的源码说明。

**参考答案**：CALL 压入的是当时的 `if_pc`；RET 通过 `WRBUS_SOURCE_STACK` 把 `stack[0]`（即上次 CALL 压入的值）送上 `reg_wrbus`，再用 `PC_LOAD_WRBUS` 令 `if_pc <= reg_wrbus[11:0]`。压入和弹出经过的是同一条 12 位通路，数值严格对称，因此 RET 必然回到 CALL 所保存的那个地址。

**练习 3**：如果一段程序连续 CALL 嵌套 5 层（第 5 层还没返回时就调用第 6 层），栈会发生什么？

**参考答案**：栈只有 4 级。前 4 次 push 正常保存 4 个返回地址；第 5 次 push 时，按 push 公式 `stack[3]<=stack[2]`，最初压入的最底层返回地址会被挤出丢失。之后若逐层 RET，最外层的返回将回到一个错误（被覆盖的）地址。TMS32010 原版也是 4 级栈，故这是对历史芯片行为的忠实复刻，而非本实现的缺陷——程序必须自行避免超过 4 层的嵌套。

## 5. 综合实践

**任务**：追踪一次两层嵌套 CALL 过程中 `stack[0..3]` 的变化，画出每一步的栈状态。

**情景设定**（符号化地址，便于聚焦栈机制本身）：

- 主程序在地址 `M` 处执行 `CALL SUB_A`，调用子程序 A；返回地址记为 `R1`（CALL 压入的 `if_pc` 值）。
- 子程序 A 在地址 `A` 处执行 `CALL SUB_B`，调用子程序 B；返回地址记为 `R2`。
- 子程序 B 执行 `RET` 返回 A；A 再执行 `RET` 返回主程序。

> 说明：`R1`、`R2` 的具体数值取决于取指流水线时序（即 `if_pc` 在 push 边沿的瞬时值，详见 u2-l2）。本实践聚焦「栈内容的移位变化」，因此用符号 `R1/R2` 代表即可；若在仿真中跟踪，请用 4.1.4 的层次路径打印观察真实数值。

**操作步骤**：

1. 假设复位后栈为空（4 格全 0）：`[0,1,2,3] = 0, 0, 0, 0`。
2. 逐步应用 4.1.2 的 push/pop 公式填写下表。

**请补全的状态序列**（答案见下方）：

| 步骤 | 事件 | 动作 | stack[0] | stack[1] | stack[2] | stack[3] |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 复位后 | — | 0 | 0 | 0 | 0 |
| 1 | `CALL SUB_A` | push R1 | ? | ? | ? | ? |
| 2 | `CALL SUB_B` | push R2 | ? | ? | ? | ? |
| 3 | （B 中）`RET` | pop | ? | ? | ? | ? |
| 4 | （A 中）`RET` | pop | ? | ? | ? | ? |

**参考答案**：

| 步骤 | 事件 | stack[0] | stack[1] | stack[2] | stack[3] | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 复位后 | 0 | 0 | 0 | 0 | 全 0 |
| 1 | push R1 | R1 | 0 | 0 | 0 | 返回地址 R1 在栈顶 |
| 2 | push R2 | R2 | R1 | 0 | 0 | 第二个返回地址压在 R1 之上 |
| 3 | pop（B 返回） | R1 | 0 | 0 | 0 | 弹出 R2 → 回到 A；栈顶恢复为 R1 |
| 4 | pop（A 返回） | 0 | 0 | 0 | 0 | 弹出 R1 → 回到主程序；栈回到全 0 |

可以看到：栈像一个「会伸缩的弹簧」，CALL 时压下去、RET 时弹回来，LIFO 顺序保证了「最后调用的子程序最先返回」，两轮 RET 之后栈恰好回到初始的全 0 状态。

**进阶（选做）**：把情景改成「三层嵌套 CALL」（再增加 `CALL SUB_C`），预测每步 `stack[0..3]`，并解释为什么第 4 步 push 时最底层的值会被挤掉（对照 4.1.5 练习 1）。

## 6. 本讲小结

- IKA32010 的栈是 **4 级 × 12 位的移位寄存器栈**（`stack[0:3]`），栈顶 `stack[0]` 组合输出，没有栈指针、也没有空满标志——忠实复刻 TMS32010 的硬件栈。
- push 让数据「下移」（旧 `stack[3]` 丢失），pop 让数据「上移」（`stack[3]` 自我复制保持），hold 不变；动作选择由 `{stk_push, stk_pop}` 决定。
- 三个控制信号 `stk_push` / `stk_pop` / `stk_data_sel` 由微码产生，默认「不压不弹、若压则压 PC」；`stk_data_sel` 只在 push 时有意义（`STACK_DATA_PC` 压 `if_pc`，`STACK_DATA_ACC` 压 `reg_wrbus[11:0]`）。
- 栈宽度 12 位 = PC 宽度，因此保存返回地址（CALL/CALA/RET/中断）毫无损失；但 PUSH/POP 累加器时会截断高 4 位。
- CALL 与 RET 是严格对称的：CALL 压 `if_pc`，RET 经 `WRBUS_SOURCE_STACK` + `PC_LOAD_WRBUS` 弹回 `if_pc`；中断响应在译码块开头同样压入断点地址并跳到 `0x002`（完整中断时序见 u3-l3）。
- 栈深度硬性限制为 4 级，超过会从底部丢失返回地址——程序须自行避免超过 4 层的子程序嵌套。

## 7. 下一步学习建议

- **u3-l1（微码架构总览）**：本讲反复提到的「默认值 + casez 覆盖」水平微码风格，在那里会有系统讲解，能帮你把堆栈三信号放进整个控制信号集合中理解。
- **u3-l3（中断机制）**：本讲只点了中断「压断点地址、跳 0x002」这一下，完整的 `int_n_z/zz/zzz` 同步链、`int_latched`、`int_rq`、内部 IACK 应答时序都在这一讲。
- **u3-l6（分支与子程序类指令译码）**：CALL/CALA/RET 的两相位执行、`ex_inst_cycle` 如何在 cycle0/cycle1 之间切换，以及条件分支如何配合标志位，会在那里完整剖析。
- 继续阅读源码时，建议带着本讲的「栈状态表」随手对照：每看到一处 `stk_push=YES`，就问一句「压的是 PC 还是 ACC？为什么？」
