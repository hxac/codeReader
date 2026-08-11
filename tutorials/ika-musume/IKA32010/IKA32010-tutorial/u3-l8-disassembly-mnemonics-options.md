# 反汇编、助记符常量与编译选项

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `IKA32010_mnemonics.sv` 这份「常量字典」，知道它把哪些数字翻译成了什么符号、为什么这么做。
- 看懂 `IKA32010_disasm.sv` 里的七个反汇编函数 `disasm_type0` ~ `disasm_type6`，能判断任意一条指令由哪个函数打印、打印出的操作数长什么样。
- 理解 `IKA32010_DISASSEMBLY`、`IKA32010_DISASSEMBLY_SHOWID`、`IKA32010_DEVICE_ID` 三个编译期宏各自管什么、关掉会发生什么。
- 具备在仿真中为「未被覆盖的指令」或 `INVALID` 分支补一行 `$display` 调试输出的能力。

本讲是专家层（u3）的第八讲。它不教新的硬件通路，而是把前面所有讲义里反复出现的那些大写常量名和仿真日志，一次性讲清楚来源与机制。

## 2. 前置知识

在进入本讲前，请确认你已了解以下概念（它们在前序讲义中讲过）：

- **微码译码块**：`src/IKA32010.sv` 里有一个大约从 [L537](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537) 开始的超大 `always @(*)` 组合块，用「默认值 + `casez(if_opcodereg)` 覆盖」的方式产出全套控制信号。详见 u3-l1。
- **`if_opcodereg` 与取指流水**：当前周期译码的是上一周期取来的指令，而程序计数器 `if_pc` 在本周期已经指向下一条。详见 u2-l2。
- **多周期指令**：像 `TBLR`、`IN` 这类指令会在同一个 PC 上停留多个机器周期（`ex_inst_cycle`）。详见 u3-l2。
- **`$display` 与 `string`**：SystemVerilog 的仿真期打印函数和字符串类型，用于把执行轨迹输出到控制台。

下面两个名词本讲会反复用到，先解释清楚：

- **助记符（mnemonic）**：人类可读的指令缩写，如 `ADD`、`LAC`、`NOP`。
- **反汇编（disassembly）**：把内核正在执行的二进制操作码，反向翻译成「PC + 助记符 + 操作数」的文本行，打印到仿真控制台，方便你「看见」DSP 在干什么。

## 3. 本讲源码地图

本讲涉及的关键文件与各自分工：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| `src/IKA32010_mnemonics.sv` | 用 `localparam`/`parameter` 定义所有控制信号的符号常量 | 常量字典，本讲核心之一 |
| `src/IKA32010_disasm.sv` | 7 个反汇编函数 + 共享状态变量 | 反汇编逻辑，本讲核心之二 |
| `src/IKA32010.sv` | 顶层模块；定义编译宏、`include` 上述两文件、在微码块里调用反汇编函数 | 宏定义与调用点，本讲核心之三 |
| `README.md` | 项目说明，含「Compilation options」一节 | 宏开关的官方说明 |

三个文件的衔接关系（这点很关键）：

- `IKA32010_mnemonics.sv` 是**无条件**包含的（[src/IKA32010.sv:L41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L41)），因为内核逻辑本身就要用这些常量。
- `IKA32010_disasm.sv` 是**条件**包含的，只在 `IKA32010_DISASSEMBLY` 宏被定义时才 `include`（[src/IKA32010.sv:L36-L38](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L36-L38)）。关掉反汇编时，这份文件根本不参与编译。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **助记符常量字典**：`IKA32010_mnemonics.sv`
2. **六类（实为七个）反汇编函数**：`disasm_type0` ~ `disasm_type6`
3. **条件编译宏定义块**：`IKA32010_DISASSEMBLY` / `SHOWID` / `DEVICE_ID`

---

### 4.1 助记符常量字典：IKA32010_mnemonics.sv

#### 4.1.1 概念说明

回头看 u3-l1 讲过的那个微码块：它要给几十个控制信号赋值，每个信号都有自己的取值集合。比如程序计数器模式 `if_pc_modesel` 有 6 种取值，ALU 运算模式 `alu_modesel` 有 7 种取值。如果直接写数字：

```verilog
if_pc_modesel = 3'd1;   // 这是什么意思？ Hold？Increase？Load？
alu_modesel   = 3'd4;   // 又是什么？ AND？ADD？
```

读源码的人会完全懵掉。`IKA32010_mnemonics.sv` 就是来解决这个问题的：它把每一个「数字取值」都起一个**见名知意的符号名**，并用 `localparam`（局部常量）固定下来，成为整个项目共享的**常量字典**。于是上面两行可以写成：

```verilog
if_pc_modesel = PC_INCREASE;   // 一眼看出：PC 自增
alu_modesel   = ALU_ADD;       // 一眼看出：做加法
```

> 这是一种最朴素也最有效的可读性手段：**用符号常量代替魔法数字（magic number）**。它不消耗任何硬件资源——`localparam` 在综合时会被常量折叠掉，最终生成的电路和写 `3'd1` 一模一样。

#### 4.1.2 核心流程

这份文件只有 63 行，按「控制信号类别」分成 10 组。它的使用流程是单向的：

1. 内核作者在写微码时，**写入**端引用这些常量名来赋值控制信号。
2. 子模块（ALU/RAM/Stack 等）的 `case` 语句里，**读取**端也引用同样的常量名来分支。
3. 任何对取值编号的修改，只需改这一处定义，全项目自动生效。

也就是说，`mnemonics.sv` 是写入方（微码）和读取方（子模块）之间的**单一事实来源（single source of truth）**。

#### 4.1.3 源码精读

文件按类别分段，每段用注释开头。下面逐一说明（行号对应 `src/IKA32010_mnemonics.sv`）。

**① 程序计数器控制** — [L1-L9](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L1-L9)

```verilog
localparam  PC_HOLD             = 3'd0;
localparam  PC_INCREASE         = 3'd1;
localparam  PC_LOAD_IMMEDIATE   = 3'd2;
localparam  PC_LOAD_INTERRUPT   = 3'd3;
localparam  PC_LOAD_WRBUS       = 3'd4;
localparam  PC_RESET            = 3'd5;
localparam  DO_RESET            = 1'b0;
localparam  DO_INCREASE         = 1'b1;
```

这组对应 u2-l2 讲的 `if_pc_modesel` 的 6 种模式（HOLD/INCREASE/LOAD_IMMEDIATE/LOAD_INTERRUPT/LOAD_WRBUS/RESET）。注意 `PC_LOAD_INTERRUPT` 专供中断向量跳转（固定跳到 `0x002`），是 u3-l3 中断机制的落点。

**② 写总线数据源** — [L11-L18](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L11-L18)

```verilog
localparam  WRBUS_SOURCE_SHB     = 3'd0;  // 移位器 B（累加器经移位）
localparam  WRBUS_SOURCE_RAM     = 3'd1;  // 数据 RAM
localparam  WRBUS_SOURCE_AR      = 3'd2;  // 辅助寄存器
localparam  WRBUS_SOURCE_STACK   = 3'd3;  // 硬件堆栈
localparam  WRBUS_SOURCE_IMM     = 3'd4;  // 指令字立即数
localparam  WRBUS_SOURCE_FLAG    = 3'd5;  // 状态标志拼接
localparam  WRBUS_SOURCE_INLATCH = 3'd6;  // 外部输入锁存
```

这组对应 u2-l1 讲的选源 MUX `register_wrbus_source_sel` 的 7 个输入。默认值就是 `WRBUS_SOURCE_RAM`（[src/IKA32010.sv:L590](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L590)）。

**③ 地址输出选择 / ④ 总线事务类型** — [L20-L30](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L20-L30)

```verilog
//address output
localparam  BUSCTRL_ADDR_PC         = 1'd0;  // o_AOUT 输出 PC（程序空间）
localparam  BUSCTRL_ADDR_PERIPHERAL = 1'd1;  // o_AOUT 输出 PA（外设口）

//bus access types
localparam  BUSCTRL_STOP        = 3'd0;  // 空闲
localparam  OPCODE_READ         = 3'd1;  // 取指
localparam  DATA_READ           = 3'd2;  // 表读 TBLR
localparam  DATA_WRITE          = 3'd3;  // 表写 TBLW
localparam  COMMAND_IN          = 3'd4;  // IN 输入
localparam  COMMAND_OUT         = 3'd5;  // OUT 输出
```

这两组对应 u2-l3 讲的总线控制器：`BUSCTRL_ADDR_*` 是地址 MUX 选择，6 个 `*_READ/WRITE/IN/OUT` 是事务类型，拼成 4 位 `busctrl_mode`。

**⑤ 乘法器输入选择（注意：用了 `parameter`）** — [L32-L34](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L32-L34)

```verilog
parameter   MUL_OP1_SOURCE_IMM = 1'b0;
parameter   MUL_OP1_SOURCE_RAM = 1'b1;
```

这是全文件**唯一**一组用 `parameter` 而非 `localparam` 的常量。两者区别：`localparam` 不能从外部改写，而 `parameter` 可在模块实例化时被参数覆盖（`defparam` 或 `#(...)`）。这里为何单独用 `parameter`，源码与文档均未说明，**待确认**——可能是历史遗留，也可能是作者有意留作可配置。这是一个值得你留意的小细节。

**⑥ 栈数据来源 / ⑦ ALU 模式 / ⑧ ALU 端口 B 切片 / ⑨ ALU 端口 B 来源 / ⑩ 通用微码开关** — [L36-L63](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L36-L63)

```verilog
//stack data
localparam  STACK_DATA_ACC  = 1'b0;  // 压累加器
localparam  STACK_DATA_PC   = 1'b1;  // 压 PC（返回地址）

//ALU mode
localparam  ALU_AND = 3'd0; ... localparam  ALU_SUBC = 3'd6;  // AND/OR/XOR/ABS/ADD/SUB/SUBC

//ALU port B data part select
localparam  ALU_PBDATA_LONGWORD = 2'd0;  // 全 32 位
localparam  ALU_PBDATA_HIGHWORD = 2'd1;  // 高 16 位
localparam  ALU_PBDATA_LOWWORD  = 2'd2;  // 低 16 位
localparam  ALU_PBDATA_BYTE     = 2'd3;  // 低 8 位

//ALU port B source select
localparam  ALU_SOURCE_SHFT = 1'b0;  // 取移位器 A
localparam  ALU_SOURCE_MUL  = 1'b1;  // 取乘法器 P 寄存器

//Microcode
localparam  YES = 1'b1; localparam  NO  = 1'b0;
localparam  HIGH = 1'b1; localparam  LOW = 1'b0;
```

- `STACK_DATA_*` 对应 u2-l6 的 `stk_data_sel`；
- `ALU_*` 三组对应 u2-l7 的 ALU 端口配置；
- 最后的 `YES/NO/HIGH/LOW` 是「通用微码开关」——微码里到处是 `alu_acc_ld = YES`、`ram_wr = NO` 这种写法，比写 `1'b1`/`1'b0` 清楚得多。

> 把这 63 行当成「字典」备查即可，不必背。后面看微码遇到不认识的大写名字，回到这个文件按类别一查就知道含义与取值。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是建立「微码赋值 ↔ 常量字典」的对照手感。

1. **实践目标**：验证微码块里的符号赋值确实能在 `mnemonics.sv` 里查到对应数字。
2. **操作步骤**：
   - 打开 [src/IKA32010.sv:L625-L636](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L625-L636)（内部 IACK 分支）。
   - 你会看到 `busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC; if_pc_modesel = PC_INCREASE;`。
   - 回到 `mnemonics.sv`，查出这三个常量的值：`OPCODE_READ=3'd1`、`BUSCTRL_ADDR_PC=1'd0`、`PC_INCREASE=3'd1`。
3. **需要观察的现象**：把符号替换成数字后，IACK 分支等价于「下一次总线事务 = 取指（`3'd1`）、地址输出 = PC（`1'd0`）、PC = 自增（`3'd1`）」，语义恰好是「应答中断后恢复正常取指」。
4. **预期结果**：你会切身体会到——**符号常量不是装饰，而是微码可读性的根基**。

#### 4.1.5 小练习与答案

**练习 1**：为什么用 `localparam` 而不是直接写 `3'd1`？
**答案**：可读性与可维护性。`PC_INCREASE` 一眼表意，`3'd1` 则需翻手册；且若要重新编号，只改 `localparam` 一处即可，不会漏改。

**练习 2**：`MUL_OP1_SOURCE_IMM` 用了 `parameter`，其余用 `localparam`，二者在综合层面有何区别？
**答案**：综合结果无区别（都被常量折叠）。区别在可配置性：`parameter` 可在实例化时被上层覆盖，`localparam` 不可。本项目此处的差异未在文档中说明原因，**待确认**。

---

### 4.2 六类反汇编函数：disasm_type0 ~ disasm_type6

#### 4.2.1 概念说明

仿真时，你希望控制台能逐条打印 DSP 执行了什么，比如：

```
IKA32010_ikakawa:  PC=0x010 | NOP
IKA32010_ikakawa:  PC=0x011 | ADD  DAT0x05, 2
IKA32010_ikakawa:  PC=0x012 | LACK 0x3F
```

但 TMS32010 的指令格式**并不统一**：`NOP` 无操作数，`ADD` 带「地址 + 移位量」，`LACK` 带立即数，`B`（跳转）的目标地址在**下一个指令字**里，`IN/OUT` 还多一个 PA 端口号。要把 16 位操作码正确拆解成上述文本，一种格式对应一个打印函数最清晰。

`IKA32010_disasm.sv` 就是这么做的：它定义了 **7 个函数** `disasm_type0` ~ `disasm_type6`（标题里说「六类」是沿用了项目主题描述的习惯说法，实际编号 0 到 6 共 7 个），每个函数负责一种指令格式的反汇编。微码块的每个 `casez` 分支，在译码的同时调用对应函数，把执行轨迹打出来。

#### 4.2.2 核心流程

每个反汇编函数都遵循同一个七步骨架：

```
1. 清空输出字符串 disasm = ""
2. （若开了 SHOWID）前缀设备 ID "IKA32010_<ID>: "
3. 格式化 PC 头部 " PC=0x%h |"，注意用的是 {pc-1}
4. 追加助记符
5. 按本函数负责的指令格式，追加操作数
6. 追加换行 "\n"
7. 去重打印：if(pc_z != pc) $display(disasm);  pc_z = pc;
```

其中有两个机制贯穿所有函数，必须先讲清楚：

**(A) PC 减 1 的流水线偏移**。所有函数格式化 PC 时都用 `{pc-1}[11:0]`（例如 [disasm_type0:L15](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L15)）。原因承接 u2-l2：本周期译码的是**上一周期**取来的指令，而此时 `if_pc` 已经自增指向下一条了。所以「这条指令真正所在的地址」是 `if_pc - 1`。打印 `pc-1` 才能让日志里的 PC 与源程序地址对得上。

**(B) 用 `pc_z` 去重**。所有函数结尾都是 `if(pc_z != pc) $display(disasm); pc_z = pc;`。因为反汇编函数被放在**组合 `always @(*)` 块**里（[src/IKA32010.sv:L537](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537)），该块在敏感信号变化时会**多次重新求值**；多周期指令更会在同一个 PC 上停留好几个周期。若不去重，同一条指令会被打印几十遍，淹没日志。`pc_z` 记住「上次打印的 PC」，只在 PC 变化时打印一次。

#### 4.2.3 源码精读

先看文件顶部 4 个**共享状态变量**——它们在所有函数间共用（[src/IKA32010_disasm.sv:L1-L5](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L1-L5)）：

```verilog
int     pc_z;          // 上次打印的 PC，用于去重
int     rst_cyc = 0;   // 声明了，但全工程从未被读写（待确认是否遗留）
int     tbl_cyc = 0;   // 表类指令（TBLR/TBLW）的多周期计数
string  disasm, num_data;  // 累积输出行 / 临时片段
```

> 注意 `rst_cyc` 虽然声明了，但在 `disasm.sv` 与 `IKA32010.sv` 中都**没有任何引用**（已用全文搜索确认），属于疑似遗留代码。这是阅读真实工程时常见的情况——**不要假设每个声明的变量都在用**。

下面按函数逐一讲解。每个函数标注「被哪些指令调用」（数据来自 [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) 微码块的实际调用点）。

**disasm_type0 —— 无操作数指令**（[L7-L21](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L7-L21)）

参数仅 `(mnemonic, pc)`，只打印「PC | 助记符」。被调用：`NOP`、`DINT`、`EINT`、`ROVM`、`SOVM`、`ABS`、`ZAC`、`MAR(NOP)`、以及 `default` 分支的 `INVALID INSTRUCTION`。例如 NOP 的调用见 [src/IKA32010.sv:L639-L643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639-L643)。

**disasm_type1 —— 累加器算术类（带移位量）**（[L23-L56](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L23-L56)）

参数 `(mnemonic, opcodereg, pc, shb)`。它的核心是按指令字 `bit7` 区分直接/间接寻址：

- 直接（`bit7==0`）：打印 `DAT0x%h, %d`——地址取 `[6:0]`，移位量取 `[11:8]`（4 位，0~15）。
- 间接（`bit7==1`）：打印 `*` / `*-` / `*+`（由 `[5:4]` 选，见 [L41-L43](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L41-L43)），再跟移位量与可选的下一 ARP（`[0]`）。

`shb` 参数控制移位量字段宽度：`shb==0` 取 `[11:8]`（4 位），`shb==1` 取 `[10:8]`（3 位）。被调用：`ADD`、`LAC`、`SUB`、`APAC`、`PAC`、`SPAC`（均 `shb=0`），以及 `SACH`（`shb=1`，因为 SACH 的移位只有 0/1/4 三档，3 位够用）。

**disasm_type2 —— 数据存储类（无移位量，可带 AR 前缀与表类去重）**（[L58-L100](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L58-L100)）

参数 `(mnemonic, opcodereg, pc, aux, tbl)`。这是用得最多的函数：

- `aux==1`：在助记符后前缀 `AR%b,`（取 `[8]`），用于 `LAR`/`SAR` 这类**显式指定目标辅助寄存器**的指令。
- 寻址部分同 type1，但**没有移位量**：直接打印 `DAT0x%h`，间接打印 `*`/`*-`/`*+` 加可选 ARP。
- `tbl==1`：启用 `tbl_cyc` 计数器（[L94-L96](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L94-L96)），保证 `TBLR`/`TBLW` 这类 3 周期指令只打印一次。

被调用：`LST`、`SSR`、`ADDH`、`ADDS`、`AND`、`OR`、`SACL`、`SUBC`、`SUBH`、`SUBS`、`XOR`、`ZALH`、`ZALS`、`MAR(LARP)`、`LDP`、`LT`、`LTA`、`LTD`、`MPY`、`DMOV`（均 `aux=0,tbl=0`），`LAR`/`SAR`（`aux=1`），`TBLR`/`TBLW`（`tbl=1`）。

**disasm_type3 —— 立即数类**（[L102-L126](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L102-L126)）

参数 `(mnemonic, opcodereg, pc, aux, mul)`：

- `aux==1`：前缀 `AR%b,`（`LARK` 用）。
- `mul==0`：以 8 位十六进制打印立即数 `[7:0]`（`LACK`/`LARK`，见 [L120](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L120)）。
- `mul==1`：以**有符号 13 位十进制**打印 `[12:0]`（`MPYK`，见 [L121](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L121)，用了 `signed'(...)`）。

> 这里反汇编**假设 `MPYK` 的立即数是 13 位有符号数**，这与 u2-l8/u3-l7 指出的「源码实际按 8 位无符号处理」存在出入。即反汇编打印的数字，与内核真正参与运算的数字，可能不一致——这是阅读时要注意的「文档/工具与实现不一致」的典型案例。

**disasm_type4 —— 分支与子程序类（两字指令，跨两周期打印）**（[L128-L158](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L128-L158)）

参数 `(mnemonic, pc, cycle, branch, ret)`。这类指令是「两字指令」：第一字是操作码，第二字是 12 位目标地址，需两个周期取全。函数因此按 `cycle`（即 `ex_inst_cycle`）分两拍打印：

- `cycle==0`：只打印「PC | 助记符」头部（[L135-L143](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L135-L143)）。
- `cycle==1`：追加目标地址 `0x%h`（取 `branch[11:0]`）再打印（[L144-L151](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L144-L151)）。`ret==1`（即 `RET`）时无目标地址，直接换行打印。

`branch` 参数是目标地址字：直接跳转（`B`/`CALL`/条件分支）传 `busctrl_inlatch`（从程序空间读来的第二字），间接跳转 `CALA` 传 `alu_acc_output[15:0]`（来自累加器，见 [src/IKA32010.sv:L1418](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1418)）。被调用：`POP`、`PUSH`、`B`、`BANZ`、`BGEZ`/`BGZ`/`BIOZ`/`BLEZ`/`BLZ`/`BNZ`/`BV`/`BZ`、`CALA`、`CALL`、`RET`。

**disasm_type5 —— 单位操作数**（[L160-L177](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L160-L177)）

参数 `(mnemonic, opcodereg, pc)`，打印 `[0]` 的 1 位二进制（`0x%b`）。实际只被 `LDPK` 调用（[src/IKA32010.sv:L1156](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1156)）。注意：`LARP` 的语义被并入了 `MAR(LARP)`，用 `disasm_type2` 打印，所以 type5 基本就是为 `LDPK` 服务。

**disasm_type6 —— I/O 类（带 PA 端口）**（[L179-L211](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L179-L211)）

参数 `(mnemonic, opcodereg, pc)`。与 type2 类似，但多打印一个 PA 端口号（取 `[10:8]`，最多 8 个口，见 [L193](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L193) 与 [L200](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L200)）。只被 `IN`、`OUT` 调用。

#### 4.2.4 代码实践

这是一个**源码阅读 + 字符串预测型实践**。

1. **实践目标**：给定一条 `LACK 0x3F` 指令（操作码 `0x7F3F`，PC 假设为 `0x011`），手工推演它会被哪个函数、打印成什么样。
2. **操作步骤**：
   - 在微码块中找到 `LACK` 分支 [src/IKA32010.sv:L886](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L886)，确认它调用 `disasm_type3("LACK", if_opcodereg, if_pc, 0, 0)`（`aux=0, mul=0`）。
   - 进入 `disasm_type3` [L103-L126](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L103-L126) 走一遍：`aux=0` 不前缀 AR；`mul=0` 走 [L120](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L120)，以 `0x%h` 打印 `opcodereg[7:0] = 0x3F`。
   - PC 头部用 `{pc-1}`，故打印 `PC=0x010`（而非 `0x011`）。
3. **需要观察的现象**：若开了 `SHOWID`，最终输出应为 `IKA32010_ikakawa:  PC=0x010 | LACK 0x3f`。
4. **预期结果**：你手工推演的字符串，应与仿真控制台实际打印一致；若不一致，先核对 `if_pc` 在 `LACK` 译码时是否确实已自增、以及 `aux/mul` 实参是否传对。实际仿真核对属于「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PC 要打印 `{pc-1}` 而不是 `pc`？
**答案**：因为取指是「一周期流水」——本周期译码的是上周期取来的指令，此时 `if_pc` 已自增。`pc-1` 才是该指令在程序中的真实地址。

**练习 2**：去掉 `if(pc_z != pc)` 这个判断，只保留 `$display(disasm)`，会发生什么？
**答案**：反汇编函数位于组合 `always @(*)` 块中，每次敏感信号变化都会重新求值；多周期指令更会在同一 PC 停留数周期。去掉去重后，同一条指令会被重复打印数十次，日志被淹没。

**练习 3**：`TBLR` 调用 type2 时传了 `tbl=1`，而 `ADDH` 传 `tbl=0`，为什么？
**答案**：`TBLR` 是 3 周期指令，会在同一 PC 上调用 type2 三次；`tbl=1` 启用 `tbl_cyc` 计数器，保证只打印一次。`ADDH` 是单周期指令，只调用一次，无需此机制。

---

### 4.3 条件编译宏定义块：IKA32010_DISASSEMBLY / SHOWID / DEVICE_ID

#### 4.3.1 概念说明

反汇编是**仿真期调试用的**，真正综合到 FPGA 时毫无用处，反而会因为大量 `$display` 拖慢仿真、引入不必要的代码依赖。所以项目用 Verilog 的**条件编译宏**（`` `ifdef ``）把整套反汇编包成一个「可一键开关」的功能。

三个宏集中在 [src/IKA32010.sv:L33-L38](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L33-L38)：

```verilog
`define IKA32010_DISASSEMBLY
`define IKA32010_DISASSEMBLY_SHOWID
`define IKA32010_DEVICE_ID "ikakawa"
`ifdef IKA32010_DISASSEMBLY
`include "IKA32010_disasm.sv"
`endif
```

`README.md` 的「Compilation options」一节（[L52-L55](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/README.md#L52-L55)）给出了这三个宏的官方说明。

#### 4.3.2 核心流程

三个宏是「层级开关」关系：

```
IKA32010_DISASSEMBLY        ← 总开关：决定反汇编是否存在
   └─ IKA32010_DISASSEMBLY_SHOWID  ← 子开关：决定每行是否带设备 ID 前缀
        └─ IKA32010_DEVICE_ID      ← 配置项：ID 字符串本身
```

总开关 `IKA32010_DISASSEMBLY` 控制两件事：

1. **是否 `include` 反汇编文件**：见 [L36-L38](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L36-L38)。关掉时 `IKA32010_disasm.sv` 整个不参与编译，里面的 `disasm`/`pc_z`/函数统统不存在。
2. **是否执行调用点**：微码块里**每一个** `disasm_typeN(...)` 调用和 `RESET`/`IRQ RECEIVED` 的 `$display`，都被 `` `ifdef IKA32010_DISASSEMBLY `` 包裹（例如 [L596-L598](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L596-L598)、[L605-L608](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L605-L608)、[L633-L635](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L633-L635)）。这两层包裹**必须同时存在**：否则关掉总开关后，函数没定义却仍被调用，编译会报「unknown identifier」。

子开关 `IKA32010_DISASSEMBLY_SHOWID` 只在每个函数**内部**生效（例如 [disasm_type0:L12-L14](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L12-L14)）：定义时在行首拼上 `IKA32010_<DEVICE_ID>: `。它的价值在多 DSP 系统——当一片 FPGA 里挂了多个 IKA32010 实例（或还有别的 CPU），给每个实例设不同 `DEVICE_ID`，日志里就能一眼区分是谁打印的。`RESET` 与 `IRQ RECEIVED` 这两条特殊消息直接用了 `` `IKA32010_DEVICE_ID ``（[L606](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L606)、[L634](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L634)），与函数内的前缀保持一致风格。

#### 4.3.3 源码精读

**宏定义与条件包含** — [src/IKA32010.sv:L33-L41](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L33-L41)

```verilog
`define IKA32010_DISASSEMBLY
`define IKA32010_DISASSEMBLY_SHOWID
`define IKA32010_DEVICE_ID "ikakawa"
`ifdef IKA32010_DISASSEMBLY
`include "IKA32010_disasm.sv"
`endif

//include mnemonic list
`include "IKA32010_mnemonics.sv"
```

注意对比：`mnemonics.sv` 在 `ifdef` **之外**，是无条件包含；`disasm.sv` 在 `ifdef` **之内**，是条件包含。这正呼应了它们的角色——常量字典是内核必需品，反汇编是可选调试件。

**调用点的双层保护** — 以 NOP 为例，[src/IKA32010.sv:L639-L643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639-L643)

```verilog
//NOP
16'b0111_1111_1000_0000: begin
    `ifdef IKA32010_DISASSEMBLY
        disasm_type0("NOP", if_pc);
    `endif
end
```

`disasm_type0` 这个函数名只在 `IKA32010_disasm.sv` 里定义，而该文件又只在 `IKA32010_DISASSEMBLY` 定义时才被 `include`。调用点的 `ifdef` 与文件包含的 `ifdef` 是**同一个宏**，二者绑死，确保「要么都在、要么都不在」。

#### 4.3.4 代码实践

这是一个**配置切换 + 行为预测型实践**（**待本地验证**，因为本讲不改源码、不跑仿真）。

1. **实践目标**：体会三个宏各自关掉后的编译/运行表现。
2. **操作步骤**（读者自行在本地副本上操作）：
   - **实验 A**：注释掉 `` `define IKA32010_DISASSEMBLY `` 这一行，重新编译仿真。预测：因为 `disasm.sv` 不再被 `include`，所有 `disasm_typeN` 调用点也都被 `ifdef` 屏蔽，编译应通过，但仿真控制台**完全没有任何指令轨迹**，只有 DUT 自身的端口波形。
   - **实验 B**：保留总开关，但注释掉 `` `define IKA32010_DISASSEMBLY_SHOWID ``。预测：指令轨迹仍在，但每行**不再有** `IKA32010_ikakawa: ` 前缀，直接以 `PC=0x... |` 开头。
   - **实验 C**：把 `` `define IKA32010_DEVICE_ID "ikakawa" `` 改成 `` "dsp0" ``。预测：前缀变成 `IKA32010_dsp0: `，`RESET` 与 `IRQ RECEIVED` 消息也相应变成 `IKA32010_dsp0: RESET`。
3. **需要观察的现象**：三次实验的控制台输出差异，应分别体现「总开关／子开关／配置项」三个层级。
4. **预期结果**：见上预测；具体仿真输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果只注释掉 `` `include "IKA32010_disasm.sv" `` 那一行，但保留所有调用点的 `ifdef` 和宏定义，会怎样？
**答案**：宏 `IKA32010_DISASSEMBLY` 仍被定义，所以调用点的 `disasm_typeN(...)` 会被编译器看到，但函数体已不在工程中 → 编译报错「unknown function」。这说明「文件包含」与「调用点门控」必须用同一个宏同步控制。

**练习 2**：`IKA32010_DISASSEMBLY` 与 `IKA32010_DISASSEMBLY_SHOWID` 的区别是什么？
**答案**：前者是总开关，决定反汇编功能是否存在（影响编译）；后者是显示开关，只在总开关开启的前提下，决定每行是否带设备 ID 前缀（不影响功能是否存在）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成本讲的核心实践任务：**为 `INVALID` 分支补写 `$display`，让非法操作码也能在仿真中被定位**。

### 背景

当前 `default`（非法指令）分支只调用了 `disasm_type0("INVALID INSTRUCTION", if_pc)`（[src/IKA32010.sv:L1745-L1752](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1745-L1752)）。它打印了出错的 PC，却**没打印那个非法的操作码本身**。调试时你往往想知道「到底是哪个 16 位数没被识别」，所以补一行打印原始 `if_opcodereg` 很有价值。

### 步骤

> 说明：以下代码片段由读者自行加入本地源码副本，属于实践操作；本讲按规则不修改源码。

1. **定位**：打开 [src/IKA32010.sv:L1745-L1752](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1745-L1752) 的 `default:` 分支。
2. **在 `disasm_type0(...)` 之后，补一条 `$display`**（示例代码）：

   ```verilog
   //INVALID INSTRUCTION
   default: begin
       busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;

       `ifdef IKA32010_DISASSEMBLY
           disasm_type0("INVALID INSTRUCTION", if_pc);
           // 示例代码：额外打印触发 INVALID 的原始操作码
           $display("IKA32010_", `IKA32010_DEVICE_ID,
                    ": >>> unknown opcode = 0x%h (bin %b)", if_opcodereg, if_opcodereg);
       `endif
   end
   ```

   注意这条 `$display` 必须放在 `` `ifdef IKA32010_DISASSEMBLY `` 之内，否则关掉反汇编时它仍会打印（虽不致编译错误，但违背「调试输出随总开关一起关」的设计约定）。

3. **构造测试激励**：在你的 testbench 的程序 ROM 里，故意放一个未被任何 `casez` 分支匹配的 16 位数（例如 `0x0000`，它不是任何合法指令）。可用 `$readmemh` 把它「烧」进 ROM（方法见 u1-l5）。
4. **运行仿真**，观察控制台。

### 需要观察的现象

当 PC 推进到那条非法指令时，控制台应出现类似：

```
IKA32010_ikakawa:  PC=0x020 | INVALID INSTRUCTION
IKA32010_ikakawa: >>> unknown opcode = 0x0000 (bin 0000000000000000)
```

### 预期结果

- 你现在能同时看到「在哪里（PC）」和「是什么（操作码）」出错。
- 进一步可对照 `docs/` 里的 opcode table，判断这个数是手册里有、但源码未实现的指令，还是根本就非法。
- 实际仿真输出「待本地验证」。

### 进阶（可选）

把同样的思路用到**尚未被反汇编函数覆盖的格式**上：若你为项目新增了一条指令的微码分支，却没有合适的 `disasm_typeN` 能打印它的操作数格式，可以仿照 `disasm_type0` 的骨架，新写一个 `disasm_type7`，并在你的新分支里调用它。记住三件事：① 函数定义放在 `IKA32010_disasm.sv`；② 调用点用 `` `ifdef IKA32010_DISASSEMBLY `` 包裹；③ 复用 `pc_z` 去重与 `{pc-1}` 偏移。

---

## 6. 本讲小结

- `IKA32010_mnemonics.sv` 是全项目共享的**常量字典**，用 `localparam` 把控制信号的取值（PC 模式、写总线源、总线事务、ALU 模式等 10 组）翻译成见名知意的符号，是微码可读性的根基；唯一一组乘法器常量用了 `parameter`，原因**待确认**。
- `IKA32010_disasm.sv` 提供 **7 个反汇编函数** `disasm_type0` ~ `disasm_type6`，按指令格式分工：无操作数 / 算术带移位 / 数据存储 / 立即数 / 两字分支 / 单位操作数 / I-O。每个函数都遵循「拼字符串 + 去重打印」的同一骨架。
- 两个贯穿机制：**`{pc-1}` 偏移**修正取指流水带来的 PC 错位；**`pc_z` 去重**防止组合块多次求值淹没日志（`TBLR/TBLW` 额外用 `tbl_cyc`）。
- 三个编译宏是层级开关：`IKA32010_DISASSEMBLY`（总开关，同时控制文件包含与调用点）、`IKA32010_DISASSEMBLY_SHOWID`（是否带前缀）、`IKA32010_DEVICE_ID`（前缀字符串，用于多 DSP 调试）。
- 「文件条件包含」与「调用点 `ifdef`」用**同一个宏**绑死，保证开关一致性；`rst_cyc` 是声明却未使用的疑似遗留变量。
- 注意反汇编的 `MPYK` 按 13 位有符号数打印，与内核实际按 8 位无符号处理存在出入——**工具/文档与实现不一致**是阅读真实工程要警惕的。

## 7. 下一步学习建议

- **下一讲 u3-l9（FPGA 综合、外设接口与系统集成）**：本讲的编译宏（尤其 `IKA32010_DISASSEMBLY` 在综合时应关闭）和 `DEVICE_ID`（多 DSP 系统）会再次出现。学完 u3-l9，你将能把 IKA32010 真正实例化进一个带程序 ROM 与外设的完整系统。
- **若你想更深入**：回到 `docs/` 的 TMS32010 User's Guide，对照反汇编输出与官方指令语义，逐条核对哪些指令的反汇编操作数格式与手册完全一致、哪些（如 `MPYK`）有出入，这是一份高质量的源码贡献切入点。
- **若你对微码本身更感兴趣**：重新精读 u3-l1 的「默认值 + `casez` 覆盖」框架，并尝试关掉 `IKA32010_DISASSEMBLY` 后再读一遍微码块——你会更清楚地看到「内核逻辑」与「调试装饰」的边界。
