# 微码架构总览：组合译码与默认值

## 1. 本讲目标

本讲是专家层（u3）的第一讲，从「读懂单条指令」跃迁到「读懂整台机器怎么被驱动」。

读完本讲，你应当能够：

- 说清楚 IKA32010 用**一个巨大的组合 `always @(*)` 块**当作「微码存储器」的设计思想。
- 解释「**先给所有控制信号赋默认值，再用 `casez(if_opcodereg)` 覆盖**」这种水平微码（horizontal microcode）风格为什么能让大多数指令只写几行。
- 默写出微码块顶部那张「默认值表」里都包含哪些类别的控制信号。
- 厘清微码块执行时**最先发生的三件事**：取默认值 → 复位态判断（`ex_state`）→ 中断预检查（`int_rq`）→ 才进入 `casez` 译码。
- 理解复位态 `ex_state==0` 与正常态的分支结构，以及 `casez` 末尾 `default: INVALID INSTRUCTION` 的兜底作用。

本讲**不**逐条讲解具体指令（那是 u3-l4 ~ u3-l7 的事），也**不**展开多周期指令的逐相位细节（那是 u3-l2 的事）。本讲只搭「骨架」。

## 2. 前置知识

本讲依赖你已经学完进阶层（u2）的数据通路讲义。下面几个概念会反复用到，先用一句话复习：

- **`if_opcodereg`（指令寄存器）**：保存「本机器周期正在译码的那条指令」的 16 位操作码。它每个机器周期换一次（见 u2-l2）。
- **`cyc_ncen`（相位 3 主工作拍）**：4 个 `i_EMUCLK` 构成一个机器周期，`cyc_ncen` 是其中的第 4 拍，几乎所有寄存器（PC、栈、ACC、AR、DP……）都在这一拍更新（见 u1-l4）。
- **`reg_wrbus`（内部写总线）**：16 位全局数据汇流，由 `register_wrbus_source_sel` 选 7 个数据源之一送上总线（见 u2-l1）。
- **组合逻辑块 `always @(*)`**：只要敏感信号（这里是 `if_opcodereg`、`ex_state`、`int_rq`、`ex_inst_cycle` 等）一变，块就重新求值，输出立即跟随，**不占用时钟拍**。

如果你还没建立「微码（microcode）」这个词的直觉，可以这样理解：一台处理器本质上是一堆数据通路（ALU、RAM、寄存器、总线），它们各自有很多「控制开关」（要不要写、选哪个源、做什么运算）。**微码就是「针对每一条指令，把这一拍所有开关应该拨到哪一档」的对照表。** 传统 CPU 把这张表存在一块 ROM 里（垂直微码）；而 IKA32010 没有用 ROM，它用一段**组合逻辑 + `casez`** 直接把这张表「算」出来——这就是本讲要讲的东西。

## 3. 本讲源码地图

本讲几乎全部围绕同一个文件：

| 文件 | 作用 |
| --- | --- |
| [src/IKA32010.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv) | 顶层模块。本讲的全部代码点都在其中第 **537~1755 行**那个超大 `always @(*)` 微码块里。 |
| [src/IKA32010_mnemonics.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv) | 常量字典。微码块里大量出现的 `PC_INCREASE`、`ALU_ADD`、`WRBUS_SOURCE_RAM`、`YES/NO` 等名字，全部定义在这里。 |

读源码时请把这两个文件并排打开：看到陌生大写名字就回 mnemonics 查，这能消除九成的阅读障碍。

## 4. 核心概念与源码讲解

### 4.1 微码块的整体定位：一个组合块就是一张微码表

#### 4.1.1 概念说明

IKA32010 没有写一个「取微指令地址 → 读 ROM → 输出控制信号」的传统微程序控制器。它的做法更直接：**写一个组合 `always @(*)` 块，输入是当前指令 `if_opcodereg`（以及少量状态），输出是这一拍要用到的全部控制信号。**

这种做法在硬件描述里常被称为「水平微码」风格：每个控制信号独占一根线（一个 `reg`），不同指令靠「覆盖其中少数几根线」来区分彼此。它的核心优点是**可读性极高**——你打开源码，每条指令对应 `casez` 里一个小段，一眼就能看清它拨动了哪些开关。

这个微码块的体量：从第 537 行的 `always @(*) begin` 一直到第 1755 行的 `end`，整整约 1219 行，是整个项目最庞大的一段逻辑，也是整颗 DSP 的「大脑」。

#### 4.1.2 核心流程

每个机器周期，这个组合块都会被重新求值一次，求值结果就是本拍的控制信号。它的内部执行顺序可以抽象成下面这段伪代码：

```text
always @(*) begin
    ① 默认值区：给「所有」控制信号赋一个最常见/最安全的初值；
    ② if (ex_state == 0)          // 复位态
           执行复位分支（停总线、PC 复位）；
       else begin                  // 正常态
    ③    中断预检查：依 int_rq 决定是否强制注入 IACK、是否跳 0x002、是否压栈；
    ④    casez (if_opcodereg)      // 按操作码覆盖默认值
               ... 各指令分支 ...
               default: INVALID INSTRUCTION;
         endcase
       end
end
```

四步的职责分工非常清晰：

1. **默认值区**——「无脑」给出最常见的行为（读 RAM、PC 递增、不写回……）。
2. **复位态分支**——芯片刚上电/复位未释放时，什么正经事都不做。
3. **中断预检查**——在译码之前先看有没有挂起的中断，若有就把下一条要执行的指令「偷换」成内部 IACK。
4. **`casez` 译码**——真正按操作码区分指令，每条指令只改写它需要的那几个信号。

注意：因为这是组合块，**①②③④ 是同一个周期内顺序求值、后者覆盖前者**——后面的赋值会冲掉前面同名信号的默认值。这正是「覆盖」一词的由来。

#### 4.1.3 源码精读

微码块的开头：[src/IKA32010.sv:536-549](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L536-L549)。

```verilog
//microcode
always @(*) begin
    //next bus transaction type
    busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;

    //next program counter operation
    if_pc_modesel = PC_INCREASE;

    //reset instruction cycle?
    ex_inst_cycle_rst = YES;

    //force next opcode nop?
    if_opcodereg_force_iack = YES; //flush!
    ...
```

要点解读：

- `always @(*)`：纯组合，敏感列表自动推导。
- 第一句注释 `//next bus transaction type` 揭示了微码块的输出本质——**给下一拍的总线事务定调**（默认是「取下一条指令」`OPCODE_READ`）。
- `if_opcodereg_force_iack = YES; //flush!`：默认把「下一条要取的指令」**冲刷成内部 IACK**。这是一种防御性默认值；下面 4.3 节会看到，正常态会用 `int_rq` 重新评估它。

#### 4.1.4 代码实践

**实践目标**：在源码里亲手确认微码块的边界与体量，建立直观感受。

**操作步骤**：

1. 打开 `src/IKA32010.sv`，定位第 537 行 `always @(*) begin`。
2. 向下滚动，找到与之配对的 `end`——它在第 1755 行（`endcase` 在 1753 行）。
3. 注意这段 `always` 之外、紧挨着它的三段「配套时序逻辑」：`ex_inst_cycle` 计数器（511-515 行）、复位延迟链 `rs_n_z/rs_n_zz`（518-525 行）、`ex_state` 状态寄存器（528-533 行）。微码块就是为它们「算下一拍控制信号」的组合前端。

**需要观察的现象**：你会看到从 537 到 1755 行之间几乎只有「默认值 + 一个巨型 `casez`」，没有任何时钟边沿、没有 `<=`（组合块里只能用 `=` 阻塞赋值）。

**预期结果**：确认这是一个**纯组合译码块**，体量约占全文件（约 2017 行）的六成，是名副其实的「机器大脑」。

#### 4.1.5 小练习与答案

**练习 1**：微码块用的是 `=` 还是 `<=`？为什么？
**答案**：全部用 `=`（阻塞赋值）。因为这是组合 `always @(*)` 块；`<=`（非阻塞）只用于时序块，在组合块里用 `<=` 会引入仿真与综合不一致。块内「后面覆盖前面」的语义正是靠 `=` 的顺序求值实现的。

**练习 2**：微码块的「输入」和「输出」分别是什么？
**答案**：输入主要是 `if_opcodereg`（当前指令）、`ex_state`（复位/正常）、`int_rq`（中断请求）、`ex_inst_cycle`（多周期指令的相位）；输出是一大堆控制信号（`busctrl_req`、`if_pc_modesel`、`alu_modesel`、`ram_wr`、`stk_push`……），它们再被各时序块在 `cyc_ncen` 拍采样。

---

### 4.2 默认值赋值区：零成本的最常见行为

#### 4.2.1 概念说明

这是整个微码设计最精妙的地方。在 `casez` 之前，微码块先给**每一个**控制信号赋一个初值。这些初值不是随便挑的，而是经过设计的——它们合起来描述的是「**最常见、最无害的那一种数据通路行为**」：

- 总线：去取下一条指令（`OPCODE_READ`）。
- PC：指向下一条（`PC_INCREASE`）。
- 数据来源：从片内 RAM 读（`WRBUS_SOURCE_RAM`）。
- ALU：做加法（`ALU_ADD`），但**不把结果写回累加器**（`alu_acc_ld = NO`）。
- 几乎所有「写」动作（写 RAM、压栈、改 ARP/DP、开乘法……）：**全部关闭**。

于是，`casez` 里那些「典型」的指令只需要改写少数几个信号就能成立。最极端的例子是 **NOP**——它在 `casez` 里只写了一句反汇编打印，**一个控制信号都没改**，因为默认值描述的恰好就是一个「什么都不做、PC 自然前进」的周期。

这种风格叫「默认值 + 按需覆盖」，它把「写一条新指令」的成本压到了最低。

#### 4.2.2 核心流程

默认值区按功能把控制信号分成若干组。下表把第 540~598 行的默认值整理成一张「默认值表」（这也是本讲代码实践要你亲手做的事）：

| 功能组 | 控制信号 | 默认值 | 含义 |
| --- | --- | --- | --- |
| 外部总线 | `busctrl_req` | `OPCODE_READ` | 下一拍去取指 |
| 外部总线 | `busctrl_addr_muxsel` | `BUSCTRL_ADDR_PC` | 地址用 PC（程序空间） |
| 程序计数器 | `if_pc_modesel` | `PC_INCREASE` | PC 自增 |
| 指令周期 | `ex_inst_cycle_rst` | `YES` | 视为单周期指令（计数器归零） |
| 取指冲洗 | `if_opcodereg_force_iack` | `YES` | 默认把下条指令冲成 IACK |
| ALU 运算 | `alu_modesel` | `ALU_ADD` | 加法 |
| ALU 端口 | `alu_paz/alu_pbz` | `NO/NO` | 端口 A/B 不强制清零 |
| ALU 端口 B 切片 | `alu_pbdata` | `ALU_PBDATA_LONGWORD` | 取整字 |
| ALU 端口 B 来源 | `alu_pbsel` | `ALU_SOURCE_SHFT` | 来自移位器 A |
| 溢出标志 | `alu_v_set/alu_v_rst` | `NO/NO` | 不改 V |
| 累加器写回 | `alu_acc_ld` | `NO` | **不写回 ACC** |
| 饱和/中断模式 | `reg_ovm_set/rst`、`reg_intm_en/dis` | 全 `NO` | 不改 OVM、INTM |
| 辅助寄存器 | `reg_arp_set/rst`、`reg_ar_ld/inc/dec` | 全 `NO` | 不动 ARP/AR |
| 数据页指针 | `reg_dp_set/rst` | `NO/NO` | 不动 DP |
| 乘法器 | `reg_t_ld`、`mul_en` | `NO/NO` | 不装载 T、不开乘法 |
| 乘法器操作数 | `mul_op1_source_sel` | `MUL_OP1_SOURCE_RAM` | （乘法未开，备用） |
| RAM 写 | `ram_wr`、`ram_dmov` | `NO/NO` | 不写 RAM、不搬移 |
| 堆栈 | `stk_data_sel` | `STACK_DATA_PC` | 若压栈则压 PC |
| 堆栈 | `stk_pop/stk_push` | `NO/NO` | 不压不弹 |
| 移位器 A（输入侧） | `sha_amt`、`sha_ssup` | `5'd0`、`NO` | 不移位、保留符号扩展 |
| 移位器 B（输出侧） | `shb_amt`、`shb_mux` | `3'd0`、`LOW` | 不移位、取低字 |
| 写总线选源 | `register_wrbus_source_sel` | `WRBUS_SOURCE_RAM` | 总线默认读 RAM |
| 中断应答 | `int_ack` | `1'b0` | 不应答中断 |

> 提示：表中出现的常量名全部定义在 [src/IKA32010_mnemonics.sv:1-63](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L1-L63)。例如「微码通用布尔」`YES/NO/HIGH/LOW` 在第 59-63 行，PC 模式在第 1-9 行，总线事务类型在第 24-30 行，ALU 运算在第 40-47 行。

#### 4.2.3 源码精读

默认值区原文（按功能成行）：[src/IKA32010.sv:540-598](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L540-L598)。

```verilog
busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;  //总线：取指
if_pc_modesel = PC_INCREASE;                                       //PC：自增
ex_inst_cycle_rst = YES;                                           //单周期
if_opcodereg_force_iack = YES; //flush!                            //默认冲洗

alu_modesel = ALU_ADD; alu_paz = NO; alu_pbz = NO;
alu_pbdata = ALU_PBDATA_LONGWORD; alu_pbsel = ALU_SOURCE_SHFT;     //ALU：加法、不写回
alu_v_set = NO; alu_v_rst = NO;
alu_acc_ld = NO;

reg_ovm_set = NO; reg_ovm_rst = NO;                                //状态位：都不改
reg_intm_en = NO; reg_intm_dis = NO;
reg_arp_set = NO; reg_arp_rst = NO; reg_ar_ld = NO;
reg_ar_inc = NO; reg_ar_dec = NO;
reg_dp_set = NO; reg_dp_rst = NO;

reg_t_ld = NO; mul_en = NO; mul_op1_source_sel = MUL_OP1_SOURCE_RAM;  //乘法：不开
ram_wr = NO; ram_dmov = NO;                                        //RAM：不写

stk_data_sel = STACK_DATA_PC; stk_pop = NO; stk_push = NO;         //栈：不压不弹

sha_amt = 5'd0; sha_ssup = NO; shb_amt = 3'd0; shb_mux = LOW;      //移位器：不移
register_wrbus_source_sel = WRBUS_SOURCE_RAM;                      //总线源：RAM
int_ack = 1'b0;                                                    //不应答中断
```

注意三个「带含义」的默认：

- `ex_inst_cycle_rst = YES`：默认把指令当成**单周期**。只有多周期指令在自己的分支里把它改成 `NO` 才会进入第 2、3 相位（详见 u3-l2）。
- `alu_acc_ld = NO`：ALU 默认算了个加法但**结果丢弃**。这意味着默认值其实已经把「RAM → 移位器 A → ALU 端口 B → 做加法」这条数据通路接通了，只差「写回 ACC」一个开关没拨。
- `register_wrbus_source_sel = WRBUS_SOURCE_RAM`：写总线默认读 RAM，于是算术指令的操作数「自动」从 RAM 来，无需声明。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手整理出「默认值表」，并用它解释为什么 ADD 与 LACK 两条指令的 `casez` 分支可以这么短。

**操作步骤**：

1. 打开 [src/IKA32010.sv:540-598](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L540-L598)，把每个被赋值的信号、它的默认值、对应的 mnemonics 常量含义填进一张表（可直接沿用上面 4.2.2 的表格作为参考答案）。
2. 跳转到 ADD 指令：[src/IKA32010.sv:786-802](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L786-L802)。

   ```verilog
   //ADD - Add to accumulator with shift
   16'b0000_????_????_????: begin
       alu_modesel = ALU_ADD;                 //其实和默认一样，可省
       alu_acc_ld = YES;                      //← 关键：唯一真正「新增」的动作
       sha_amt = {1'b0, if_opcodereg[11:8]};  //按指令字设置左移量
       if(if_opcodereg[7]) begin              //间接寻址副作用（ARP/AR）
           reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
           ...
       end
   end
   ```

   对照默认值表逐条核对：ADD 想要的「读 RAM、做加法、PC 自增、取下一条指令」**全都已经由默认值给出**。ADD 真正要做的只有两件新事——**允许写回累加器**（`alu_acc_ld = YES`）和**设置移位量**（`sha_amt`），外加按指令字处理间接寻址的 ARP/AR 副作用。连 `alu_modesel = ALU_ADD` 这一句都可以省（因为它和默认值相同），写出来只是出于可读性。

3. 再跳转到 LACK 指令：[src/IKA32010.sv:879-888](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L879-L888)。

   ```verilog
   //LACK - Load accumulator immediate
   16'b0111_1110_????_????: begin
       alu_modesel = ALU_ADD; alu_paz = YES; alu_pbdata = ALU_PBDATA_BYTE; //屏蔽 ACC 反馈、取立即数字节
       alu_acc_ld = YES;
       register_wrbus_source_sel = WRBUS_SOURCE_IMM;  //← 关键：操作数来自立即数而非 RAM
   end
   ```

   LACK 是「用立即数装载累加器」，它和默认值最大的不同是**数据来源**：默认是 `WRBUS_SOURCE_RAM`，LACK 必须改成 `WRBUS_SOURCE_IMM`（指令字里的立即数）。此外它用 `alu_paz = YES` 屏蔽累加器旧值（让结果是 0+立即数），用 `alu_pbdata = ALU_PBDATA_BYTE` 只取低字节。

**需要观察的现象 / 预期结果**：

- ADD 分支**没有**出现 `register_wrbus_source_sel`、`busctrl_req`、`if_pc_modesel`、`alu_pbsel` 等赋值——因为默认值已经正确。这证明「默认值表」承担了大部分工作。
- LACK 分支必须显式改写 `register_wrbus_source_sel`，因为它的数据来源与默认相反。
- 结论：**默认值描述的是「读 RAM、做加法、不写回、PC 前进、取下一条」这条最常见通路；每条指令只需拨动与该通路不同的那几个开关。** 这就是「默认值 + 覆盖」风格让代码短小且易读的根本原因。

> 若要进一步验证，可在仿真里把 `sha_amt` 的默认值从 `5'd0` 改成一个非零值（**仅作为本地实验，切勿提交**），观察 ADD（不设 `sha_amt` 时会用默认）之外、依赖「不移位」的指令是否会出错——这能直观感受默认值是如何被「借力」的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 NOP 在 `casez` 里几乎「什么都不写」也能正常工作？
**答案**：因为默认值区已经把所有「副作用」都关掉了（`alu_acc_ld=NO`、`ram_wr=NO`、`stk_push=NO`……），并且让 `if_pc_modesel=PC_INCREASE`、`busctrl_req=OPCODE_READ`，这恰好就是「什么都不做、PC 前进、取下一条指令」的 NOP 语义。NOP 因此只需写反汇编打印。

**练习 2**：默认值里 `alu_modesel = ALU_ADD`，但 `alu_acc_ld = NO`。这两者合起来是什么意思？
**答案**：ALU 仍然会把「RAM 数据（经移位）+ 累加器旧值」算成一个加法结果，但因为 `alu_acc_ld=NO`，这个结果**不会写回累加器**，等于被丢弃。换句话说默认值「假装在加」，但只有显式 `alu_acc_ld=YES` 的指令才真正改变累加器。

**练习 3**：哪几个默认值是「防御性」的，专门为了让未实现/异常情况安全退化？
**答案**：`if_opcodereg_force_iack = YES`（默认冲洗下条指令，复位时尤其重要）、`ex_inst_cycle_rst = YES`（默认当单周期，避免误入多周期相位）、以及 `casez` 末尾的 `default: INVALID INSTRUCTION`（见 4.4 节）。

---

### 4.3 复位态与中断检查的前置处理

#### 4.3.1 概念说明

`casez` 译码之前，微码块要先处理两件「全局性」的事，它们与具体是哪条指令无关：

1. **复位态判断**：芯片刚复位时（`ex_state==0`），不执行任何指令，只做复位动作。
2. **中断预检查**：每条指令译码前，先看有没有被使能的中断挂起（`int_rq`）；若有，就把「即将执行的指令」偷换成内部 IACK，并让 PC 跳到中断向量 `0x002`。

这两步之所以放在 `casez` 之前，是因为它们的优先级**高于**普通指令译码——复位要压住一切，中断要在指令执行前抢占。理解了这个「前置层」，你才能理解为什么 `casez` 里第一条分支是内部指令 IACK 而不是某条真实指令。

#### 4.3.2 核心流程

**复位检测**用一条两级同步链 + 上升沿检测实现：

```text
rs_n_z  <= i_RS_n          // 第 1 级同步（在 cyc_ncen 拍）
rs_n_zz <= rs_n_z          // 第 2 级
检测 i_RS_n 的上升沿：(~rs_n_zz & rs_n_z) == 1
```

检测到上升沿（复位刚释放）后，`ex_state` 从 0 切到 1：

```text
if(ex_state == 0 && 上升沿)  ex_state <= 1;
```

**微码块里的复位分支**：当 `ex_state==0` 时：

```text
busctrl_req = BUSCTRL_STOP;   // 停掉外部总线
if_pc_modesel = PC_RESET;     // PC 保持复位
//（只打印 RESET，不译码任何指令）
```

**中断预检查**（仅在 `ex_state==1` 正常态进行）：信号 `int_rq = int_latched & ~reg_intm`（见 [src/IKA32010.sv:354](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L354)），即「有挂起中断 且 中断未被屏蔽（`reg_intm==0`）」。当 `int_rq` 为真：

```text
if_opcodereg_force_iack = YES;                       // 下条指令强制变 IACK(0xF000)
if_pc_modesel           = PC_LOAD_INTERRUPT;         // PC 跳到 0x002
stk_push                = YES; stk_data_sel = STACK_DATA_PC;  // 压入返回地址
```

随后 `casez` 命中的就是 IACK 分支（4.4 节），完成中断应答。

#### 4.3.3 源码精读

先看复位/状态相关的三段**时序逻辑**（它们给微码块提供 `ex_state` 输入）：

- 指令周期计数器：[src/IKA32010.sv:510-515](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L510-L515)。
- 复位延迟链 `rs_n_z/rs_n_zz`：[src/IKA32010.sv:517-525](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L517-L525)。
- `ex_state` 状态机及其注释（`0: reset low / 1: normal operation`）：[src/IKA32010.sv:502-533](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L502-L533)。

  ```verilog
  reg ex_state;
  /*
      0: reset low
      1: normal operation
  */
  ...
  if(ex_state == 1'b0) if((~rs_n_zz & rs_n_z) == 1'b1) ex_state <= 1'b1;
  ```

再看微码块里的**复位分支**与**中断预检查**：[src/IKA32010.sv:600-616](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L600-L616)。

```verilog
if(ex_state == 1'b0) begin
    busctrl_req = BUSCTRL_STOP; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    if_pc_modesel = PC_RESET;
    `ifdef IKA32010_DISASSEMBLY
    disasm = {"IKA32010_", `IKA32010_DEVICE_ID, ": RESET\n"};
    $display(disasm);
    `endif
end
else begin
    //interrupt check
    if_opcodereg_force_iack = (int_rq) ? YES : NO;   // ← 覆盖了默认的 YES
    if_pc_modesel           = (int_rq) ? PC_LOAD_INTERRUPT : PC_INCREASE;
    stk_push                = (int_rq) ? YES : NO;
    stk_data_sel            = (int_rq) ? STACK_DATA_PC : STACK_DATA_ACC;
```

注意这里有一个**默认值被覆盖**的典型例子：4.2 节里默认 `if_opcodereg_force_iack = YES`（冲洗），但正常态这一行用 `(int_rq) ? YES : NO` 把它重新评估了——没有中断时变回 `NO`，让真正取来的指令正常进入译码。这也回答了「为什么默认要写成 YES」：它是给复位态（`ex_state==0` 分支不执行 613 行）留的安全值，确保复位期间指令寄存器被持续冲刷成无害的 IACK。

#### 4.3.4 代码实践

**实践目标**：在仿真里观察「复位释放 → `ex_state` 切换 → 微码从复位分支跳到正常分支」的完整过程。

**操作步骤**：

1. 打开 testbench [src/IKA32010_tb.v:15-24](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L15-L24)，看清复位激励时序：`#30 RS_n<=0; #100 RS_n<=1;`（先拉低 100 单位再释放）。
2. 在仿真器（Icarus / ModelSim / Verilator 等）里把 `ex_state`、`rs_n_z`、`rs_n_zz`、`if_pc`、`if_pc_modesel` 加入波形，并开启 `IKA32010_DISASSEMBLY`（源码默认已 `define`，见 [src/IKA32010.sv:33](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L33)）。
3. 运行仿真，注意控制台会持续打印 `RESET` 直到复位释放。

**需要观察的现象**：

- 复位期间 `ex_state==0`、`if_pc==0`、`if_pc_modesel==PC_RESET`、`busctrl_req==BUSCTRL_STOP`，控制台不断打印 `RESET`。
- `RS_n` 上升沿之后的某个 `cyc_ncen` 拍，`(~rs_n_zz & rs_n_z)` 出现一个周期的高电平，`ex_state` 随之由 0 变 1。
- 此后 `RESET` 打印停止，开始出现真正的指令反汇编行（取决于 ROM 内容）。

**预期结果**：你将清楚地看到「复位分支」与「正常分支」的分界点正是 `ex_state` 的 0→1 翻转，它由两级同步链检测到的 `i_RS_n` 上升沿触发。

> 说明：testbench 第 63/82-83 行用绝对路径 `D:/PROCESSOR/...` 加载 ROM 文件（`$readmemh`）。若你本地没有这些 ROM，可暂时把这两处 `initial` 注释掉、改用一个手写的极小指令数组，仅用于观察复位时序。**待本地验证**（取决于你是否具备 ROM 文件与仿真器）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ex_state` 的 0→1 转移要用 `(~rs_n_zz & rs_n_z)` 而不是直接判断 `i_RS_n`？
**答案**：为了在 `cyc_ncen` 时钟域里做**同步化与边沿检测**。直接判断异步的 `i_RS_n` 会引入亚稳态，且无法识别「上升沿」。两级寄存 `rs_n_z/rs_n_zz` 后，`(~rs_n_zz & rs_n_z)` 正好在一拍内为高，表示「上拍还是 0、本拍已是 1」的上升沿。

**练习 2**：复位态下 `busctrl_req = BUSCTRL_STOP`，这和默认值 `OPCODE_READ` 有什么不同？为什么复位时必须停总线？
**答案**：`OPCODE_READ` 会驱动外部总线去取指（拉低 `o_MEN_n` 等），而 `BUSCTRL_STOP` 让所有选通信号无效（见 u2-l3 的空闲时序）。复位期间 PC 还没就绪、外部 ROM 可能也未稳定，此时不应驱动总线，所以必须停。

**练习 3**：中断预检查里为什么要把 `stk_data_sel` 设成 `STACK_DATA_PC`？
**答案**：中断响应要把「返回地址」（即当前 PC）压栈，所以压栈数据来源必须是 PC（`STACK_DATA_PC`）。注意默认值其实也已经是 `STACK_DATA_PC`，这里显式写出是为了在「无中断时不压栈」的语境下把语义讲清楚（无中断时 `stk_push=NO`，`stk_data_sel` 取值无意义）。

---

### 4.4 casez(if_opcodereg) 译码框架

#### 4.4.1 概念说明

经过默认值、复位判断、中断预检查之后，微码块进入真正的指令译码：一个 `casez(if_opcodereg)`。`casez` 是 SystemVerilog 的通配分支语句——分支标号里的 `?` 表示「该位不关心」，于是可以用一个 16 位模式匹配一整族操作码（例如 `16'b0000_????_????_????` 匹配所有高 4 位为 0000 的指令）。

整个 `casez` 按指令类别分成若干组，每组用注释横幅隔开，俨然一份目录：

| 横幅注释（行号） | 指令类别 | 代表指令 |
| --- | --- | --- |
| `CONTROL INSTRUCTIONS`（621） | 控制类 | NOP、DINT、EINT、POP、PUSH、SSR、IACK |
| `ACCUMULATOR INSTRUCTIONS`（772） | 累加器算逻 | ABS、ADD、ADDH、ADDS、AND、LAC、LACK、OR、SACH、SACL、SUB、SUBC、XOR、ZAC… |
| `AUXILLARY REGISTER AND DATA POINTER`（1089） | 辅助寄存器/数据页 | LAR、LARK、MAR、LARP、LDP、LDPK、SAR |
| `BRANCH INSTRUCTIONS`（1190） | 分支/子程序 | B、BANZ、BGEZ…、CALL、CALA、RET |
| `MULTIPLIER INSTRUCTION`（1465） | 乘法器 | LT、LTA、LTD、MPY、MPYK、APAC、PAC、SPAC |
| `I/O AND DATA MEMORY INSTRUCTION`（1584） | I/O 与表操作 | DMOV、IN、OUT、TBLR、TBLW |
| `default`（1745） | 非法指令 | 打印 `INVALID INSTRUCTION` |

> 这些横幅是后续 u3-l4 ~ u3-l7 各讲的天然入口，本讲只看框架，不展开每组细节。

#### 4.4.2 核心流程

`casez` 的求值规则：从上到下匹配**第一个**与 `if_opcodereg` 相符的模式（`?` 位任意），执行该分支。分支体内用阻塞赋值 `=` 覆盖默认值——同一条指令可以只改几个信号，其余沿用 4.2 节的默认值。匹配不到任何模式则走 `default`（非法指令）。

一个要点：`casez` 的**第一个分支**不是某条真实指令，而是内部指令 **IACK**（`16'hF000`）。它不是从程序 ROM 取来的，而是当中断发生时由 `if_opcodereg_force_iack` 强制注入的（见 4.3 节）。把它放在最前面、用一个固定全等模式 `16'b1111_0000_0000_0000` 匹配，是为了让「被偷换进来的 IACK」能被立刻识别并完成中断应答。`0xF000` 这个值专门避开所有真实指令的操作码空间，是个安全的「非法但有意」编码。

#### 4.4.3 源码精读

`casez` 开头与第一条 IACK 分支：[src/IKA32010.sv:618-636](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L618-L636)。

```verilog
casez(if_opcodereg)
    //internal special instruction IACK
    16'b1111_0000_0000_0000: begin
        busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
        if_pc_modesel = PC_INCREASE;
        if_opcodereg_force_iack = NO;              //取回真正的下一条指令
        int_ack = (int_rq) ? YES : NO;             //应答中断（清 int_latched）
        stk_push = NO; stk_data_sel = STACK_DATA_ACC;
        ...
    end
```

NOP 分支（演示「几乎不写」）：[src/IKA32010.sv:639-643](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L639-L643)。

```verilog
//NOP
16'b0111_1111_1000_0000: begin
    `ifdef IKA32010_DISASSEMBLY
        disasm_type0("NOP", if_pc);
    `endif
end
```

可以看到 NOP 分支**只写了一行反汇编**，没有任何控制信号赋值——完全依赖默认值。

`casez` 末尾的兜底分支与结束：[src/IKA32010.sv:1745-1755](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1745-L1755)。

```verilog
//INVALID INSTRUCTION
default: begin
    busctrl_req = OPCODE_READ; busctrl_addr_muxsel = BUSCTRL_ADDR_PC;
    `ifdef IKA32010_DISASSEMBLY
        disasm_type0("INVALID INSTRUCTION", if_pc);
    `endif
end
endcase
```

`default` 分支的作用：遇到未定义的操作码时安全退化——继续取下一条指令、PC 自然前进（沿用默认 `PC_INCREASE`），并打印 `INVALID INSTRUCTION` 提示。这保证了非法指令不会让机器卡死。

#### 4.4.4 代码实践

**实践目标**：通过「让一条指令从 `default` 兜底」来验证你对译码框架的理解。

**操作步骤**：

1. 在 testbench 里把一段从未被 `casez` 覆盖的操作码（例如 `16'hFF00`，确认它不在任何分支模式里）写入指令 ROM 的起始地址。若不方便改 ROM，可在 [src/IKA32010.sv:180](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L180) 附近临时把复位初值 `if_opcodereg <= 16'h7F80;` 改成一个非法值如 `16'hFF00`（**仅本地实验，勿提交**），让复位后第一条就是非法指令。
2. 开启 `IKA32010_DISASSEMBLY` 宏，运行仿真。

**需要观察的现象**：控制台打印出 `INVALID INSTRUCTION`，且 PC 仍按 `PC_INCREASE` 前进、总线仍为 `OPCODE_READ`，机器没有卡住。

**预期结果**：证明 `default` 分支确实兜住了所有未定义操作码，机器安全退化。恢复你做的临时修改。

> 若想进一步练习「覆盖默认值」，可在 IACK 分支（625 行）后仿照 ADD 的写法，新增一行 `alu_acc_ld = YES;`，然后思考：这会让中断响应额外发生什么？（答：会把一个无意义的加法结果写回累加器，破坏现场——从而体会「为什么 IACK 分支刻意不写 `alu_acc_ld`」。）**仅作思考实验，不要提交。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 IACK 用 `16'hF000` 而不是占用某个真实指令的操作码？
**答案**：IACK 是「芯片内部」的指令，不从 ROM 取，而是由 `if_opcodereg_force_iack` 强制注入。`0xF000` 位于真实指令的操作码空间之外，确保它永远不会和某条真实指令的 `casez` 模式冲突，可以独占第一个分支。

**练习 2**：`casez` 里分支的顺序重要吗？
**答案**：重要。`casez`（与 `casex`）按从上到下的顺序匹配**第一个**命中模式，且 `?` 通配会放宽匹配。如果把一个较宽的模式（很多 `?`）写在前面，会把后面的较窄模式「吃掉」。源码里把 IACK 的固定全等模式放在最前、把带 `?` 的族群模式放在后面，正是为了顺序正确。这也是综合工具偶尔会对 `casez/casex` 报 priority 警告的原因。

**练习 3**：如果 `casez` 没有 `default` 分支，遇到非法操作码会怎样？
**答案**：组合 `casez` 若无 `default`，未命中时所有控制信号保留 4.2 节的默认值——恰好也是「PC 前进、取下一条、不写回」的安全行为，所以机器多半仍能继续。但有了 `default`，额外打印 `INVALID INSTRUCTION` 能在调试时立刻暴露问题，是有意的可观测性设计。

## 5. 综合实践

把本讲的三块知识（默认值表、复位/中断前置、`casez` 框架）串起来，做一次「**给微码块做一次手术**」的练习：

1. **读懂一条新指令的译码**：自选一条本讲没细讲的指令（例如 [src/IKA32010.sv:776-783](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L776-L783) 的 ABS，或 1089 行起的某条辅助寄存器指令），列出它的 `casez` 分支**实际改写**了哪些控制信号，其余信号分别沿用 4.2 节默认值表里的哪一行。
2. **画出该指令的数据流**：依据「改写信号 + 默认信号」拼出完整数据通路（例如 ABS 是「累加器反馈 → ALU(ABS) → 写回 ACC」，端口 B 被默认 + 分支里的 `alu_pbz=YES` 双重屏蔽）。
3. **回答两个问题**：
   - 这条指令有没有触发任何前置处理（复位态？中断）？为什么通常不会？
   - 如果这条指令的操作码模式被误删，它会落到 `default` 还是命中另一条指令的宽模式？
4. **（可选，仿真型）** 用 testbench 喂入这条指令，开启 `IKA32010_DISASSEMBLY`，核对打印出的助记符与你选的指令一致。

通过这次手术，你会真切体会到：「读懂一条指令 = 默认值表 + 该分支的少数覆盖」。

## 6. 本讲小结

- IKA32010 用**一个约 1219 行的组合 `always @(*)` 块**（[src/IKA32010.sv:537-1755](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537-L1755)）充当整台机器的微码表，输入操作码，输出全套控制信号。
- 微码块的求值顺序固定为四步：**默认值 → 复位态判断 → 中断预检查 → `casez` 译码**，后者用 `=` 覆盖前者。
- 「**默认值 + `casez` 覆盖**」是水平微码风格的核心：默认值描述了「读 RAM、做加法、不写回、PC 前进、取下一条」这条最常见通路，因此 NOP 几乎零赋值、ADD 只需拨动「写回 + 移位量」两三个开关。
- 默认值表覆盖了总线、PC、ALU、标志、ARP/AR、DP、乘法器、RAM、栈、移位器、写总线选源、中断应答等所有控制信号，所有常量定义在 `IKA32010_mnemonics.sv`。
- 复位态 `ex_state==0` 由两级同步链检测 `i_RS_n` 上升沿后切换到 1；复位期间停总线、PC 复位、不译码。
- 中断预检查用 `int_rq = int_latched & ~reg_intm` 抢占译码：有中断时强制注入内部 IACK（`0xF000`）、PC 跳 `0x002`、压栈返回地址。
- `casez` 按六大指令类别人工分组（控制/累加器/辅助寄存器/分支/乘法/I-O），第一条分支是内部 IACK，末尾 `default` 兜底非法指令。

## 7. 下一步学习建议

本讲只搭了「微码骨架」，接下来按依赖关系深入：

- **u3-l2 多周期指令时序与状态机**：本讲反复提到的 `ex_inst_cycle`、`ex_inst_cycle_rst`、`rs_n_z/zz`、`ex_state` 到底怎么配合，把单条指令拆成 cycle 0/1/2 多个相位执行。这是理解 POP/PUSH/B/CALL/RET/TBLR 等多周期指令的钥匙。
- **u3-l3 中断机制**：本讲只讲了中断的「预检查」与 IACK 应答，完整的中断同步链（`int_n_z/zz/zzz`）、`int_latched` 锁存、`reg_intm`（DINT/EINT）在该讲展开。
- **u3-l4 ~ u3-l7**：按指令类别逐组剖析 `casez` 各分支——这正是本讲留下的「目录」的细化。
- 建议同步重读 [src/IKA32010_mnemonics.sv](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv) 全文，把每个常量与 4.2 节默认值表一一对应，作为后续阅读各指令分支的「随身字典」。
