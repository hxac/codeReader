# 主状态机 cpu_state：取指到执行的全流程

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 PicoRV32 主状态机 `cpu_state` 的**八个状态**（`fetch`/`ld_rs1`/`ld_rs2`/`exec`/`shift`/`stmem`/`ldmem`/`trap`）各自负责什么。
- 看懂每个状态在什么条件下跳到下一个状态，并能画出一条指令从取指到写回的**状态流转图**。
- 理解 `decoder_trigger`、`launch_next_insn` 这两个关键信号如何作为"发令枪"驱动状态机向前推进。
- 看懂复位（`resetn` 拉低）那一拍 CPU 是如何把 `reg_pc`、`reg_next_pc`、`irq_mask`、栈指针等初始化好并落入 `cpu_state_fetch` 的。

本讲是 u4-l1（译码器）的直接下集：译码器告诉我们"这是什么指令、立即数多少"，本讲回答"CPU 按什么顺序、在哪些状态里把这条指令执行完"。

## 2. 前置知识

在读本讲前，请确认你已经了解（对应前置讲义 u4-l1、u3-l2、u3-l1）：

- **一位译码信号 `instr_*` 与 `decoded_imm`**：译码器把 32 位指令字变成一堆一位的"是/否"信号（如 `instr_lw`、`instr_addi`、`instr_jal`）加上一个 32 位立即数。本讲的状态机就是消费这些信号来决定做什么、跳到哪。
- **`decoder_trigger`**：译码器在取指完成时拉高的触发信号（`decoder_trigger <= mem_do_rinst && mem_done`），表示"新指令已经译好，可以开始执行了"。
- **原生内存接口的握手三件套** `mem_valid`/`mem_ready`/`mem_wstrb`，以及"一次传输完成"的标志 `mem_done`（u1-l3、u3-l2）。
- **Verilog 时序基础**：`always @(posedge clk)` 里的非阻塞赋值 `<=` 是"本拍计算、下拍可见"；`case (cpu_state)` 这种写法是典型的状态机模板。
- **关键参数**：`ENABLE_IRQ`、`ENABLE_REGS_DUALPORT`、`CATCH_ILLINSN`、`CATCH_MISALIGN`、`STACKADDR`、`PROGADDR_RESET` 这些 parameter（u3-l1）会改变状态机的具体行为，本讲会逐一点到。

一个关键直觉：**PicoRV32 是一台"多周期"CPU**——它不像流水线 CPU 那样把取指/译码/执行/访存/写回压在五级流水线上同时进行，而是用**一个状态机串行地**走过这些阶段。好处是数据通路极简单、面积小、容易满足时序；代价是平均每条指令要花大约 4 个周期（CPI≈4）。这八个状态，就是这"大约 4 拍"的具体拆法。

## 3. 本讲源码地图

本讲全部内容集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | 整个 CPU 核。本讲关注从第 1170 行 `// Main State Machine` 注释开始、一直到第 1947 行附近的整段主状态机逻辑。 |

本讲涉及的关键代码点：

1. **状态编码 localparam**：八个 `cpu_state_*` 常量（第 1172–1179 行）。
2. **状态名调试输出** `dbg_ascii_state`：把当前状态映射成可读字符串（第 1186–1196 行）。
3. **`launch_next_insn`**：组合信号"是否在这一拍真正开始执行下一条指令"（第 1400 行）。
4. **主 `always @(posedge clk)` 块入口与每拍默认值**（第 1402–1456 行）。
5. **复位初始化** `if (!resetn)`（第 1457–1483 行）。
6. **八状态 `case (cpu_state)`**：`trap`（1487）、`fetch`（1491）、`ld_rs1`（1579）、`ld_rs2`（1759）、`exec`（1805）、`shift`（1829）、`stmem`（1854）、`ldmem`（1880）。
7. **对齐错误与非法指令陷入**（第 1922–1947 行）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应规格里的三个学习重点：

- **4.1 八状态 FSM**：状态编码、每个状态的一句话职责。
- **4.2 复位初始化与取指启动**：`resetn` 那一拍做了什么、`fetch` 状态如何与译码器握手把指令"发动"起来。
- **4.3 状态转移条件**：除 `fetch` 外的七个状态各自在什么条件下跳走，以及一条真实指令如何穿过这些状态。

### 4.1 八状态 FSM：状态编码与各状态职责

#### 4.1.1 概念说明

PicoRV32 用一个 8 位寄存器 `cpu_state` 来记录"CPU 现在在干嘛"。它有八个取值，每个取值正好占用 8 位中的某一位：

| 状态 localparam          | 二进制值     | 助记名  | 一句话职责 |
|--------------------------|-------------|---------|-----------|
| `cpu_state_trap`         | `8'b10000000` | trap   | 不可恢复的陷入死锁，拉高 `trap` 输出端口后停在原地。 |
| `cpu_state_fetch`        | `8'b01000000` | fetch  | 取下一条指令；上一条指令的写回（回写寄存器）也在这里完成。 |
| `cpu_state_ld_rs1`       | `8'b00100000` | ld_rs1 | 读第一源寄存器 rs1，并按指令类型决定下一站。 |
| `cpu_state_ld_rs2`       | `8'b00010000` | ld_rs2 | 读第二源寄存器 rs2（仅单端口寄存器堆才需要这个额外状态）。 |
| `cpu_state_exec`         | `8'b00001000` | exec   | 执行 ALU 运算 / 跳转 / 分支判定。 |
| `cpu_state_shift`        | `8'b00000100` | shift  | 迭代完成移位指令（sll/srl/sra 及其立即数版本）。 |
| `cpu_state_stmem`        | `8'b00000010` | stmem  | 执行 store 指令（sb/sh/sw）的内存写。 |
| `cpu_state_ldmem`        | `8'b00000001` | ldmem  | 执行 load 指令（lb/lh/lw/lbu/lhu）的内存读与符号扩展。 |

这种"每个状态占独立一位"的编码叫 **one-hot（一位热码）状态编码**。它的好处是：判断"当前是不是处于某个状态"只需读一位，综合后是一根简单的线，不需要解码器；状态转移逻辑也因此很扁平。代价是 8 个状态用了 8 位触发器（而紧凑二进制编码只要 3 位），但对 PicoRV32 这种状态数很少的设计而言几乎可忽略。

#### 4.1.2 核心流程

把八个状态按"一条指令的生命周期"排开，大致是这样一条主干（不同指令会走不同的支路）：

```
                 ┌───────────────────── resetn 上升沿 ─────────────────────┐
                 │  reg_pc <= PROGADDR_RESET; irq_mask <= ~0; ...          │
                 │  cpu_state <= cpu_state_fetch                            │
                 └──────────────────────────┬──────────────────────────────┘
                                            ▼
   ┌─────────────────────────── fetch ───────────────────────────┐
   │ 取指令(mem_do_rinst)；等 decoder_trigger；回写上一条结果     │
   └───┬───────────────────────────────────────────────────┬──────┘
       │ (decoder_trigger & 非跳转类)                        │ (jal: 算好目标就留在 fetch 继续)
       ▼                                                     ▼ (异常/非法且无处理)
   ld_rs1 ──读 rs1──┬─► ldmem   (load)                       trap（自锁）
                   ├─► stmem   (store, 双端口)
                   ├─► shift   (立即数移位/单端口移位)
                   ├─► exec    (ALUI/jalr/barrel-shift/双端口 R 型)
                   ├─► ld_rs2  (单端口 R 型/store/分支 → 先读 rs2)
                   └─► fetch   (lui/auipc/rdcycle/custom-irq 等无需 ALU 的)
                                   │
              ld_rs2 ──读 rs2──┬─► stmem
                              ├─► shift
                              └─► exec ──► fetch
   shift ──迭代移位──► fetch
   stmem ──写内存──► fetch
   ldmem ──读内存+符号扩展──► fetch
```

记住一个总规律：**几乎所有状态最终都回到 `fetch`**。`fetch` 既是"取下一条指令"的入口，也是"上条指令收尾（回写寄存器、更新 PC）"的出口。唯一不回到 `fetch` 的是 `trap`——它把自己锁死。

#### 4.1.3 源码精读

状态编码定义在这里，八个 localparam 清清楚楚：

[picorv32.v:1172-1179](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1172-L1179) — 定义八个状态常量，注意它们恰好是 8 位的 one-hot 编码。

状态寄存器本身只有一个 8 位 reg：

[picorv32.v:1181-1182](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1181-L1182) — `reg [7:0] cpu_state;` 与中断子状态机 `reg [1:0] irq_state;`。

紧跟着有一段纯调试用的组合逻辑，把当前状态翻译成可读字符串，仿真波形里可以直接看到 `"fetch"`、`"exec"` 这样的文字，非常方便：

[picorv32.v:1186-1196](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1186-L1196) — `dbg_ascii_state` 把 `cpu_state` 映射成 ASCII 字符串，仅供 trace/调试，不影响功能。

整段状态机的"主体"是一个 `always @(posedge clk)` 块，结构是：

```verilog
always @(posedge clk) begin
    // (1) 每拍先把一批信号清成默认值
    ...
    if (!resetn) begin
        // (2) 复位初始化
        ...
        cpu_state <= cpu_state_fetch;
    end else
    (* parallel_case, full_case *)
    case (cpu_state)
        cpu_state_trap:   ...
        cpu_state_fetch:  ...
        cpu_state_ld_rs1: ...
        ... (其余六个状态)
    endcase
    // (3) case 之外的全局检查：对齐错误、非法指令 → trap
end
```

[picorv32.v:1402-1414](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1402-L1414) — 块入口与每拍默认赋值：`trap<=0`、`reg_out<='bx`、各 `set_mem_do_*=0`、`alu_wait<=0` 等。每拍先复位成默认值，再由下面的 `case` 按当前状态覆写——这是状态机里避免锁存器（latch）的标准写法。

[picorv32.v:1485-1489](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1485-L1489) — `case (cpu_state)` 的开头与 `cpu_state_trap` 分支：一旦进入 trap，每拍都把 `trap <= 1`，没有出口，CPU 停摆（只能靠外部 `resetn` 重新拉低来恢复）。

#### 4.1.4 代码实践

> **实践目标**：在仿真里"看见"这八个状态，建立"状态机真的在 8 个值之间跳"的直觉。

**操作步骤**：

1. 打开 [testbench_ez.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v)，确认它实例化的是裸 `picorv32`（不是 axi 版）。
2. 在 `picorv32.v` 第 1184 行 `dbg_ascii_state` 已经是 128 位字符串形式；如果你用的仿真器波形里看不到字符串，可以临时在 `case (cpu_state)` 入口附近（约第 1486 行后）加一行**示例代码**：
   ```verilog
   // 示例代码：仅用于观察，理解后请删除
   $display("[t=%0t] cpu_state=%s", $time, dbg_ascii_state);
   ```
3. 运行 `make test_ez`（u1-l3 已验证这是唯一不依赖 RISC-V 工具链的入口）。
4. 观察终端打印或波形。

**需要观察的现象**：你会看到 `cpu_state` 在 `fetch → ld_rs1 → exec → fetch …`、`fetch → ld_rs1 → ldmem → fetch …` 这样的序列之间循环，永远不会出现 `trap`（除非你故意塞一条非法指令）。

**预期结果**：能从输出里数出预置程序（u1-l3 的 6 条指令）每条分别经过了哪些状态——例如 `sw`/`lw` 会经过 `ldmem`/`stmem`，而 `addi` 只经过 `exec`。**待本地验证**：具体每条指令的拍数以你实际波形为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cpu_state` 用 8 位 one-hot 编码，而不是用 3 位二进制（`trap=3'd0, fetch=3'd1, …`）？

答案：one-hot 编码让"当前是不是某状态"的判断退化为读一位，省去解码器，状态转移条件逻辑更扁平、关键路径更短，利于提高 fmax。代价是 8 位触发器 vs 3 位，但状态数少时这点面积可忽略。对于追求 fmax 与简单数据通路的 PicoRV32，这是合理取舍。

**练习 2**：`cpu_state_trap` 有没有"出口"？怎么才能让 CPU 从 trap 里恢复？

答案：没有软件出口。`trap` 分支每拍只做 `trap <= 1`，不向任何其它状态转移。要恢复只能由外部把 `resetn` 拉低再释放，让复位分支（第 1457 行 `if (!resetn)`）把 `cpu_state` 重新写成 `cpu_state_fetch`。这就是 u3-l2 强调的"`trap` 是不可恢复的死锁，区别于可恢复的中断"。

---

### 4.2 复位初始化与取指启动

#### 4.2.1 概念说明

状态机的"起点"是复位。当 `resetn`（低有效同步复位，u3-l2）为 0 时，状态机不走 `case`，而是走 `if (!resetn)` 分支，一次性把所有需要确定初值的寄存器写好，并把 `cpu_state` 设成 `fetch`——这样 `resetn` 一释放，CPU 立刻从复位向量地址开始取指。

`fetch` 状态本身是整个状态机最复杂的一个，因为它身兼三职：

1. **取下一条指令**：通过 `mem_do_rinst` 请求一次取指传输，等 `mem_done`/`decoder_trigger`。
2. **回写上一条指令的结果**：把上一条指令算好的 `reg_out`（或分支链接地址）写回寄存器堆（`cpuregs_write`，见 u5-l1）。
3. **处理中断入口**：`irq_state` 子状态机在这里把 PC 切到 `PROGADDR_IRQ`。

本模块聚焦前两职与复位，中断入口留到 u6-l2 详讲。

#### 4.2.2 核心流程

复位那一拍的初始化清单（精简版）：

```
reg_pc        <= PROGADDR_RESET     // 程序计数器指向复位地址（默认 0）
reg_next_pc   <= PROGADDR_RESET     // "下一条 PC" 也指向复位地址
irq_mask      <= ~0                  // 复位后屏蔽所有中断（全 1）
irq_active    <= 0                   // 不在中断处理中
timer         <= 0                   // 定时器清零
cpu_state     <= cpu_state_fetch     // 落入 fetch
if (~STACKADDR):                     // 若设置了栈地址
    latched_store<=1; latched_rd<=2; reg_out<=STACKADDR  // 复位首动作：把 sp(x2) 设成 STACKADDR
```

最后那行很巧妙：**设置栈指针不是一条真正的指令，而是"伪造"一次写回**。复位时让 `latched_store=1`、`latched_rd=2`（x2 即 sp）、`reg_out=STACKADDR`，于是 `fetch` 状态第一次运行时，回写逻辑会把 `STACKADDR` 写进 x2——等价于在程序最前面"免费"插了一条 `li sp, STACKADDR`。

`fetch` 状态运行时的主干节奏：

```
fetch 进入 → 默认 current_pc = reg_next_pc
         → 若上一条是 branch/store/irq，先把结果算进回写数据
         → reg_pc <= current_pc; reg_next_pc <= current_pc   (锁住本条 PC)
         → latched_rd <= decoded_rd      (记住本条要写哪个寄存器)
         → mem_do_rinst <= !decoder_trigger && !do_waitirq   (没译好就继续取)
         → 等 decoder_trigger 拉高：
              reg_next_pc <= current_pc + (compressed ? 2 : 4)   (算好顺序下一条)
              if (instr_jal): 留在 fetch，把 reg_next_pc 改成跳转目标
              else:           cpu_state <= cpu_state_ld_rs1
```

这里有个关键信号 `launch_next_insn`，它把"是不是真的在这一拍启动下一条指令"提炼成一个组合条件：

\[ \text{launch\_next\_insn} \;=\; \text{fetch} \;\land\; \text{decoder\_trigger} \;\land\; \text{(无 pending 且未屏蔽的中断)} \]

它为真，意味着译码已完成、且没有需要抢占的中断，CPU 这才放心地"开走"。它被用来清零一批调试寄存器（`dbg_rs1val` 等），也表示这一拍是新旧指令的真正分界。

#### 4.2.3 源码精读

复位初始化（注意 `cpu_state <= cpu_state_fetch` 在末尾）：

[picorv32.v:1457-1483](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1457-L1483) — `if (!resetn)` 分支：落实 `reg_pc`/`reg_next_pc`、`irq_mask<=~0`、`timer<=0`，并在 `~STACKADDR` 时伪造一次对 x2 的写回（设栈指针），最后 `cpu_state <= cpu_state_fetch`。

`decoder_trigger` 在每拍的开头被刷新（这是 u4-l1 提到的"取指完成即触发"）：

[picorv32.v:1446-1446](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1446) — `decoder_trigger <= mem_do_rinst && mem_done;`：取指请求且传输完成的那一拍，下一拍 `decoder_trigger` 就为 1。

`launch_next_insn` 的定义：

[picorv32.v:1400-1400](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1400) — 组合信号：处于 `fetch` 且 `decoder_trigger` 为真，且（关闭中断 / 处于中断延迟 / 正在中断 / 没有 pending 未屏蔽的中断）时为真。

`fetch` 状态主体（取指请求 + PC 锁定 + 回写调度）：

[picorv32.v:1491-1527](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1491-L1527) — `cpu_state_fetch`：`mem_do_rinst <= !decoder_trigger && !do_waitirq;`、`current_pc = reg_next_pc;`，按 `latched_branch`/`latched_store`/`irq_state` 算回写数据与目标 PC，最后 `reg_pc <= current_pc; reg_next_pc <= current_pc;`。

`fetch` 状态里"译码已完成、决定下一站"的关键分支：

[picorv32.v:1557-1576](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1557-L1576) — `if (decoder_trigger)`：算 `reg_next_pc <= current_pc + (compressed_instr ? 2 : 4)`；若是 `instr_jal` 则改写 `reg_next_pc` 为跳转目标并留在 `fetch`；否则 `cpu_state <= cpu_state_ld_rs1`。这正是"发令枪"打响、状态机开走的那一拍。

回写调度（在 `fetch` 里把上一条的结果送进寄存器堆）：

[picorv32.v:1309-1334](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1309-L1334) — 组合的 `cpuregs_write`/`cpuregs_wrdata`：仅当 `cpu_state==cpu_state_fetch` 时，按 `latched_branch`（写链接 PC）、`latched_store && !latched_branch`（写 ALU/访存结果）、`irq_state`（写中断返回信息）决定写什么。这就是"回写发生在 fetch"的来源。

#### 4.2.4 代码实践

> **实践目标**：在脑中（或纸上）把"复位释放后头几拍"串起来，验证 `reg_pc` 与栈指针的初值。

**操作步骤**：

1. 查 `PROGADDR_RESET` 与 `STACKADDR` 的默认值。在 [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) 的 parameter 区段（u3-l1 已定位）确认两者默认都是 `32'h 00000000`。
2. 推演 `resetn` 由 0 变 1 后的第一拍（即 `cpu_state` 首次等于 `fetch`）：
   - `reg_pc` 应为 `PROGADDR_RESET`；
   - 因 `~STACKADDR` 为假（默认 0），所以**不会**伪造 sp 写回——也就是说默认配置下复位后 sp 是未定义的，需要固件自己设（这就是 firmware/start.S 里要显式设 sp 的原因，见 u2-l1）。
3. 把 `STACKADDR` 改成一个非零值（例如在 testbench_ez 实例化时传 `.STACKADDR(32'h 00020000)`），重新推演：第一拍 `latched_store<=1; latched_rd<=2; reg_out<=0x20000;`，于是 `fetch` 第一次回写会把 `0x20000` 写进 x2。

**需要观察的现象**：`reg_pc` 在复位释放后立刻等于复位地址；栈指针是否被自动设置，完全取决于 `STACKADDR` 参数是否非零。

**预期结果**：理解"为什么默认 `STACKADDR=0` 时固件必须自己设 sp"，以及"为什么 PicoSoC 集成时会传一个非零 `STACKADDR` 来省掉这一步"。**待本地验证**：若你在 testbench_ez 里改了参数，可加打印核对 x2 的值。

#### 4.2.5 小练习与答案

**练习 1**：`fetch` 状态里 `mem_do_rinst <= !decoder_trigger && !do_waitirq;` 这个条件为什么要在 `decoder_trigger` 为真时把取指请求关掉？

答案：`decoder_trigger` 为真表示"当前指令已译码完成、状态机马上要离开 fetch 去执行它"。此时若继续请求取指，会和即将在 `ld_rs1`/`exec` 等状态发起的访存冲突。所以在 `decoder_trigger` 为真的那一拍关掉 `mem_do_rinst`，把总线控制权交给后续状态。对 `instr_jal` 这类留在 `fetch` 的指令，代码会在 `decoder_trigger` 分支里另行把 `mem_do_rinst<=1` 重新打开，以取下一条指令。

**练习 2**：为什么把栈指针的初始化做成"伪造一次写回"，而不是写一段专门的硬件逻辑？

答案：因为回写通路（`cpuregs_write` + `latched_rd` + `reg_out` → 寄存器堆）本来就是 `fetch` 状态现成的机制。复用它能零成本地在"第一条真正的指令之前"插入一次等价于 `li sp, STACKADDR` 的写回，不需要额外的数据通路或状态。这是 PicoRV32 用最少硬件实现目标的典型手法。

---

### 4.3 状态转移条件：各状态如何决定下一站

#### 4.3.1 概念说明

`fetch` 把"发令枪"打响后（`decoder_trigger` 为真、`cpu_state <= ld_rs1`），接下来的每个状态都面对同一个问题：**根据当前指令的 `instr_*` 信号，我应该把状态机送去哪里？** 本模块逐个讲清 `ld_rs1`、`ld_rs2`、`exec`、`shift`、`stmem`、`ldmem` 六个状态的转移条件，以及它们如何最终回到 `fetch`。

理解这些转移的关键是记住"指令按需要的资源分道扬镳"：

- **不需要 rs2、也不需要 ALU 计算地址的**（`lui`/`auipc`/`rdcycle`/自定义 IRQ 指令）：在 `ld_rs1` 读个 rs1（甚至不读）就回 `fetch`。
- **只需要 rs1 + 立即数的 ALUI/移位/跳转**（`addi`/`slli`/`jalr`）：`ld_rs1` 直接送 `exec` 或 `shift`。
- **需要 rs1 + rs2 的**（R 型、分支、store）：双端口寄存器堆在 `ld_rs1` 同时读出 rs2 并就地分流；单端口则要额外进 `ld_rs2` 读 rs2。
- **访存类**（`lb…lw`/`sb…sw`）：算好地址后送 `ldmem`/`stmem`。

#### 4.3.2 核心流程

下表汇总六个状态"在什么条件下、跳到哪个状态"（以 `ENABLE_REGS_DUALPORT=1` 为主路径）：

| 当前状态 | 主要分支条件 | 下一状态 |
|---------|-------------|---------|
| `ld_rs1` | `is_lb_lh_lw_lbu_lhu`（load） | `ldmem` |
| `ld_rs1` | `is_sb_sh_sw`（store，双端口） | `stmem` |
| `ld_rs1` | `is_slli_srli_srai && !BARREL_SHIFTER` | `shift` |
| `ld_rs1` | `is_jalr_addi_slti_...` 或 barrel-shift 立即数 | `exec` |
| `ld_rs1` | `is_lui_auipc_jal` | `exec`（算完即回 fetch） |
| `ld_rs1` | R 型/store/分支（默认分支）双端口 | `stmem`/`shift`/`exec` |
| `ld_rs1` | 同上但**单端口**寄存器堆 | `ld_rs2` |
| `ld_rs1` | `rdcycle`/`getq`/`setq`/`retirq`/`maskirq`/`timer` | `fetch`（直接出结果） |
| `ld_rs1` | 非法指令 `instr_trap` + `WITH_PCPI` | PCPI 握手 → `fetch` 或 `trap` |
| `ld_rs2` | `is_sb_sh_sw` | `stmem` |
| `ld_rs2` | `is_sll_srl_sra && !BARREL_SHIFTER` | `shift` |
| `ld_rs2` | 默认（R 型/分支） | `exec` |
| `exec` | 分支 `is_beq...` 且 `mem_done` | `fetch` |
| `exec` | 非分支（ALU/jalr） | `fetch` |
| `shift` | `reg_sh==0`（移位完成） | `fetch` |
| `shift` | `reg_sh>0` | `shift`（自循环） |
| `stmem` | 写传输完成 | `fetch` |
| `ldmem` | 读传输完成 | `fetch` |

两个需要单独点名的细节：

- **`exec` 的分支判定**：分支指令（`beq`/`bne`/...）要等 `mem_done`（取下一条指令的预取完成）才回 `fetch`；若分支成立，还会把 `decoder_trigger<=0; set_mem_do_rinst=1;`，强制重新取指（因为顺序预取的那条作废了）。
- **`ldmem`/`stmem` 的"伪触发"**：它们完成时同时拉高 `decoder_trigger` 与 `decoder_pseudo_trigger`。后者（u4-l1 讲过）表示"只刷新译码缓存、不重新译码"，因为当前指令早就译好了，这里只是借触发之名把写回信息对齐到 `fetch`。

#### 4.3.3 源码精读

`ld_rs1` 是整个状态机里分支最多的状态，按指令类型分流。先看 load/store/移位/ALUI 这几条主路径：

[picorv32.v:1696-1723](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1696-L1723) — `ld_rs1` 中的 load（→`ldmem`）、立即数移位（→`shift`）、`jalr`/ALUI/barrel-shift（→`exec`）三条分支。注意 load 在这里只读了 rs1（地址基址），不读 rs2。

`ld_rs1` 的默认分支（R 型/store/分支），以及单/双端口寄存器堆的分叉：

[picorv32.v:1724-1755](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1724-L1755) — 默认分支：双端口时读出 rs2 并就地分流（`stmem`/`shift`/`exec`）；单端口（`else`）时只读 rs1，把读 rs2 的工作推迟到 `ld_rs2`。这就是 u3-l1 提到"单端口寄存器堆需要额外的 `ld_rs2` 状态、CPI 多 1"的根源。

`ld_rs2` 状态（仅单端口走得到），读完 rs2 后分流，逻辑与 `ld_rs1` 默认分支的后半段几乎一致：

[picorv32.v:1759-1803](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1759-L1803) — `ld_rs2`：读 rs2，按 store/移位/其它分别送 `stmem`/`shift`/`exec`。

`exec` 状态，分支 vs 非分支两条出路：

[picorv32.v:1805-1827](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1805-L1827) — `exec`：分支指令取 `alu_out_0`（比较结果）作为 `latched_branch`/`latched_store`，等 `mem_done` 回 `fetch`；非分支则 `latched_store<=1; latched_stalu<=1;` 直接回 `fetch`（`latched_stalu` 表示回写值来自 ALU 而非访存）。

`shift` 状态的自循环迭代：

[picorv32.v:1829-1852](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1829-L1852) — `shift`：`reg_sh==0` 时输出结果并回 `fetch`；否则按 `TWO_STAGE_SHIFT` 先移 4 位（`reg_sh-=4`）再移 1 位（`reg_sh-=1`）地迭代。这就是 README 里"移位 4–14 拍"的来源（u5-l2 会详讲）。

`stmem` 与 `ldmem`，注意它们末尾的"伪触发"：

[picorv32.v:1854-1878](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1854-L1878) — `stmem`：按 `sb/sh/sw` 设 `mem_wordsize`，算地址 `reg_op1 <= reg_op1 + decoded_imm`，`set_mem_do_wdata=1` 发起写；完成后 `decoder_trigger<=1; decoder_pseudo_trigger<=1;` 回 `fetch`。

[picorv32.v:1880-1912](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1880-L1912) — `ldmem`：按 `lb/lh/lw/lbu/lhu` 设 `mem_wordsize` 与符号扩展标志 `latched_is_lu/lh/lb`，算地址，`set_mem_do_rdata=1` 发起读；完成后按 `latched_is_*` 把 `mem_rdata_word` 符号扩展进 `reg_out`，同样"伪触发"回 `fetch`。

最后是 `case` 之外的两类全局检查——对齐错误与非法指令，它们能把状态机强行送进 `trap`：

[picorv32.v:1922-1947](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1922-L1947) — `CATCH_MISALIGN` 下，load/store 地址未对齐（字访问 `addr[1:0]!=0`、半字 `addr[0]!=0`）或取指 PC 未对齐时，触发 `irq_buserror`（若中断可用且未屏蔽）或直接 `cpu_state <= cpu_state_trap`；`!CATCH_ILLINSN` 时遇到 `ecall/ebreak` 也进 `trap`。

#### 4.3.4 代码实践（重点：追踪一条 `lw` 指令）

> **实践目标**：追踪 `lw x5, 8(x6)` 在八个状态间的真实流转，绘制状态图。
>
> 注意：规格里给出的参考路径是 `fetch→ld_rs1→ld_rs2→exec→ldmem→fetch`，但**对照真实源码，`lw` 并不经过 `ld_rs2` 和 `exec`**——因为 `lw` 只用 rs1（作地址基址），不需要 rs2；而地址加法 `reg_op1 + decoded_imm` 是在 `ldmem` 状态里完成的，不经过 `exec`。本实践就以源码为准，画出 `lw` 的真实路径，并解释它为什么"跳过"那两个状态。

`lw x5, 8(x6)` 的编码：opcode=`0000011`（LOAD），`funct3=010`（LW），`rs1=x6`，`rd=x5`，`imm=8`。

**操作步骤**：

1. 在第 1696 行的 `ld_rs1` 分支条件 `is_lb_lh_lw_lbu_lhu && !instr_trap` 处确认：译码器会把这条指令点亮成 `instr_lw=1`，从而命中这一分支。
2. 逐拍填写下表（假设双端口寄存器堆、单周期内存、`CATCH_MISALIGN=1`）：

| 拍 | `cpu_state` | 本拍做的事 | 触发转移的条件 | 下一状态 |
|----|------------|-----------|---------------|---------|
| T0 | `fetch` | 取到 `lw` 指令字；`mem_done`→下拍 `decoder_trigger=1`；锁 `reg_pc`/`latched_rd<=5` | `decoder_trigger` 拉高，非 `jal` | `ld_rs1` |
| T1 | `ld_rs1` | 读 rs1=x6 的值进 `reg_op1`（地址基址） | 命中 `is_lb_lh_lw_lbu_lhu` 分支 | `ldmem` |
| T2 | `ldmem` | 设 `mem_wordsize<=0`（字）；`latched_is_lu<=1`；算地址 `reg_op1 <= reg_op1+8`；`set_mem_do_rdata=1` 发起读 | 读尚未完成（`!mem_done`） | `ldmem` |
| T3 | `ldmem` | 读传输完成（`mem_done`）；`reg_out <= mem_rdata_word`（字，无符号扩展） | `!mem_do_prefetch && mem_done` | `fetch` |
| T4 | `fetch` | 回写：`cpuregs_write=1`（因 `latched_store=1`、`latched_rd=5`），把 `reg_out` 写进 x5；同时取下一条指令 | — | （下一条指令的 `ld_rs1`） |

3. 画出状态图（就是上表的压缩版）：
   ```
   fetch ──decoder_trigger──► ld_rs1 ──is_lw──► ldmem ──(发起读)──► ldmem ──mem_done──► fetch
   ```
4. **对比验证**：再追一条 R 型 `add x5, x6, x7`（双端口）。它命中 `ld_rs1` 的默认分支（第 1724 行），在那里同时读出 rs2 并就地送 `exec`（第 1750 行），`exec` 里 `latched_stalu<=1` 后回 `fetch`。所以 `add` 的路径是 `fetch→ld_rs1→exec→fetch`（3 拍，与 README 的 "ALU reg+reg CPI=3" 吻合）。**单端口**时 `add` 才会多走一个 `ld_rs2`（CPI=4）。

**需要观察的现象**：`lw` 的路径长度（约 5 拍）与 README 表格"memory load CPI=5"一致；它确实不经过 `ld_rs2`/`exec`。`add` 的路径（3 拍）与"ALU reg+reg CPI=3"一致。

**预期结果**：得到两张状态图——`lw: fetch→ld_rs1→ldmem→fetch`，`add: fetch→ld_rs1→exec→fetch`（双端口）。**待本地验证**：用 4.1.4 的 `$display` 在 `make test_ez` 或 `make test_vcd` 波形里核对实际经过的状态序列与拍数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lw` 不经过 `exec` 状态？地址 `rs1+imm` 是在哪里算的？

答案：`lw` 的地址加法 `reg_op1 <= reg_op1 + decoded_imm` 直接写在 `ldmem` 状态里（第 1897 行），不需要 ALU 的全套比较/逻辑运算，因此没必要绕道 `exec`。`exec` 是为需要 ALU 比较/运算结果的指令（ALUI、R 型、分支、`jalr`）准备的。把简单加法就地做掉，能省一个状态、少一拍。

**练习 2**：单端口寄存器堆（`ENABLE_REGS_DUALPORT=0`）时，`add x5,x6,x7` 的状态路径和拍数会怎样变化？为什么？

答案：路径变成 `fetch→ld_rs1→ld_rs2→exec→fetch`，多了一个 `ld_rs2` 状态，CPI 从 3 变成 4。原因是单端口寄存器堆一拍只能读一个寄存器：`ld_rs1` 先读 rs1（并据此分流到 `ld_rs2`，见第 1754 行的 `else` 分支），`ld_rs2` 再读 rs2，最后才进 `exec`。双端口堆一拍能同时读 rs1 和 rs2，所以能把这两步合并进 `ld_rs1`。这正是 README"CPI (SP)"列比"CPI"列多 1 的原因。

**练习 3**：`ldmem`/`stmem` 完成时为什么要把 `decoder_pseudo_trigger` 也拉高？去掉会怎样？

答案：因为这两条指令在**执行末尾**才需要把"已译好的写回信息"对齐到 `fetch`，但当前指令早在取指阶段就译过码了，不能再用（可能已变化的）`mem_rdata_q` 重新译码。`decoder_pseudo_trigger` 就是"借触发之名行缓存之实、但不重新译码"的保护信号（u4-l1 已详述）。去掉它，第二级译码会再跑一次，可能覆盖正在提交的 `latched_*` 信息，导致写回错误。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个"**手画状态流转图 + 拍级时序表**"的任务，覆盖多种指令类型，让你能闭着眼睛说出任意 RV32I 指令的状态路径。

1. 选 5 条指令，要求覆盖状态机的不同支路：`addi`（ALUI→exec）、`add`（R 型→exec）、`beq`（分支→exec，分 taken/not-taken 两种）、`lw`（→ldmem）、`sw`（→stmem）。
2. 对每条指令，按 4.3.4 的表格形式，逐拍列出：`cpu_state`、本拍做的事、触发转移的条件、下一状态。
3. 把它们画成一张总状态图，把 `fetch` 画在中间，八个状态作为节点，转移条件标在箭头上。重点标出：
   - `fetch` 是唯一的"回写点"（`cpuregs_write` 只在 `fetch` 为真）；
   - `trap` 是唯一的"死胡同"；
   - 单/双端口寄存器堆在 `ld_rs1` 默认分支的分叉。
4. 用 README 的 CPI 表（[README.md 第 344–354 行](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L344-L354)）核对：你推出来的路径长度是否与表中的 CPI 一致（如 `add` 双端口=3、`lw`=5、`beq` taken=5）。若不一致，回到源码对应行号检查你漏掉了哪个状态（常见的漏点是 `shift` 的自循环、`ldmem`/`stmem` 要等 `mem_done` 的那一拍）。

完成本任务后，你应当能"看一眼指令就知道它会在八个状态里怎么走、大约花几拍"——这是阅读下一单元数据通路（寄存器堆/ALU/移位/内存接口）的必要前提，因为那些模块都是被这个状态机按拍驱动的。

## 6. 本讲小结

- PicoRV32 用一个 8 位 one-hot 状态机 `cpu_state` 串行地走完一条指令的生命周期，八个状态：`trap`/`fetch`/`ld_rs1`/`ld_rs2`/`exec`/`shift`/`stmem`/`ldmem`。
- 复位（`resetn` 低）那一拍一次性初始化 `reg_pc<=PROGADDR_RESET`、`irq_mask<=~0`、`timer<=0` 等，并把 `cpu_state<=fetch`；若 `STACKADDR` 非零，还会"伪造"一次对 x2 的写回来设栈指针。
- `fetch` 身兼三职：取下一条指令（`mem_do_rinst`，等 `decoder_trigger`）、回写上一条结果（`cpuregs_write` 仅在 `fetch` 为真）、处理中断入口；`launch_next_insn = fetch && decoder_trigger && 无抢占中断` 是新旧指令的真正分界。
- 除 `trap` 外，所有状态最终都回到 `fetch`；`ld_rs1` 是分流的"道岔"，按指令类型把指令送去 `ldmem`/`stmem`/`shift`/`exec`/`ld_rs2`/`fetch`。
- 单端口寄存器堆（`ENABLE_REGS_DUALPORT=0`）会让 R 型/store/分支多走一个 `ld_rs2` 状态，CPI 比 1 拍——这就是 README 两列 CPI 差 1 的根源。
- `ldmem`/`stmem` 完成时同时拉高 `decoder_trigger` 与 `decoder_pseudo_trigger`，后者保护"只对齐写回信息、不重新译码"；对齐错误与非法指令由 `case` 外的全局检查送进 `trap`。

## 7. 下一步学习建议

本讲讲清了"状态机按什么节奏走"，但还没讲"每个状态里用到的数据通路长什么样"。下一单元（u5）会从状态机下沉到数据通路：

- **u5-l1 寄存器堆与 ALU**：讲 `cpuregs`/`picorv32_regs` 的读写端口、单/双端口差异如何影响 `ld_rs2` 状态（本讲已埋伏笔），以及 `alu_out` 组合逻辑如何实现加减/比较/逻辑。
- **u5-l2 移位运算**：展开本讲 `shift` 状态里 `TWO_STAGE_SHIFT` 两级移位与 `BARREL_SHIFTER` 的细节，解释"移位 4–14 拍"的来历。
- **u5-l3 原生内存接口**：展开本讲 `ldmem`/`stmem` 里的 `mem_do_rdata`/`mem_do_wdata`/`mem_wordsize` 如何驱动 `mem_state` 状态机完成一次总线传输。

建议同步阅读：

- [picorv32.v 第 1170 行起的 `// Main State Machine` 区段](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1170)，对照本讲边读边在脑中跑状态机。
- 想加深"多周期 vs 流水线"直觉的读者，可对比任意一款五级流水线 RISC-V 核的状态/级数划分，体会 PicoRV32 为"小面积、高 fmax"做的取舍（README 第 333 行那句"optimized for size and fmax, not performance"是最好的注脚）。
