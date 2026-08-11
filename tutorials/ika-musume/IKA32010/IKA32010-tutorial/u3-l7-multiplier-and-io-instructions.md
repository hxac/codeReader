# 乘法器与 I/O/数据存储类指令译码

## 1. 本讲目标

本讲是专家层指令译码的第三篇，专门拆解 `casez` 译码块里最后两组指令：

- **乘法器类指令**：`APAC` / `LT` / `LTA` / `LTD` / `MPY` / `MPYK` / `PAC` / `SPAC`
- **I/O 与数据存储类指令**：`DMOV` / `IN` / `OUT` / `TBLR` / `TBLW`

学完后你应当能够：

1. 说清 `LT/LTA/LTD/MPY/MPYK` 如何用 `reg_t_ld`、`mul_en`、`mul_op1_source_sel` 三个微码信号驱动 T/P 寄存器与乘法器。
2. 说清 `APAC/PAC/SPAC` 如何通过 `alu_pbsel = ALU_SOURCE_MUL` 把 P 寄存器切到 ALU 端口 B，再做加/装载/减。
3. 说清 `IN/OUT/TBLR/TBLW` 如何配合总线控制器（Bus Controller）的 `COMMAND_IN/COMMAND_OUT/DATA_READ/DATA_WRITE` 事务完成外设 I/O 与程序空间↔数据 RAM 的搬移。
4. 自己追踪一条 `LTD` 指令，解释它为何能在一个机器周期内同时完成「装载 T + 旧乘积累加 + 数据搬移」三件事。

## 2. 前置知识

本讲建立在前三讲之上，下面这些结论我们直接沿用，不再重复推导：

- **u3-l1（微码架构）**：译码块是一个组合 `always @(*)` 块，求值顺序固定为「默认值 → 复位态判断 → 中断预检查 → `casez` 覆盖」。默认值描述了最常见通路「读 RAM、加到 ACC 反馈但不写回、PC 自增、取下一条」，因此多数指令只需覆盖少数信号。多周期指令在前几个相位把 `ex_inst_cycle_rst = NO` 以推进计数器，并在中间相位写 `if_opcodereg_force_iack = NO` 以保证原子性。

- **u2-l8（乘法器与 T/P 寄存器）**：`IKA32010_multiplier` 子模块做有符号 16×16→32 乘法，内部 `op0_latch/op1_latch/result` 三级寄存，**乘积比操作数锁存晚一个机器周期**；`mul_en` 整周期有效、操作数稳定，故无需 `cyc_ncen` 选通；T 寄存器 `reg_t` 在顶层、`cyc_ncen` 拍从 `reg_wrbus` 载入；P 寄存器 `reg_p` 在顶层只是一根 wire，存储体是子模块里的 `result`。⚠️ 已知遗留点：`MPYK` 复用了 8 位立即数通路（`WRBUS_SOURCE_IMM` 只透传 `if_opcodereg[7:0]`），与官方手册「13 位有符号立即数」的描述存在出入，负立即数行为待仿真确认。

- **u2-l3（总线控制器）**：微码每周期给出 3 位 `busctrl_req` 与 1 位 `busctrl_addr_muxsel`，拼成 4 位 `busctrl_mode`。`busctrl_req` 编码六种事务：`STOP / OPCODE_READ / DATA_READ(TBLR) / DATA_WRITE(TBLW) / COMMAND_IN(IN) / COMMAND_OUT(OUT)`。读事务在相位 0~2 拉低选通、相位 3 抬起并把 `i_DIN` 锁进 `if_opcodereg`（取指）或 `busctrl_inlatch`（表读/IN）；写事务在相位 1 驱动 `o_DOUT`、相位 2 发出 `WE_n` 脉冲。三个选通 `o_MEN_n / o_DEN_n / o_WE_n` 互斥。

如果你对上述结论里的某个信号还不熟悉，建议先回去看对应讲义；本讲假设你已经认识 `reg_wrbus`、`alu_pbsel`、`busctrl_req`、`ex_inst_cycle` 这些名字。

> 术语速查：**机器周期（machine cycle）** = 4 个 `i_EMUCLK`，由 `cyclecntr` 0→1→2→3 计数；**相位（phase）** 指 `cyclecntr` 的某个值；主工作拍是 `cyc_ncen`（相位 3）。**事务（transaction）** 指一次完整的外部总线访问，由 4 个相位拼成。

## 3. 本讲源码地图

本讲只涉及两个源码文件，且全部聚焦在主文件的译码段：

| 文件 | 本讲用到的部分 | 作用 |
| --- | --- | --- |
| `src/IKA32010.sv` | L380~L401 乘法寄存器与子模块例化 | `reg_t` 装载、`mul_op1` 操作数选择、`reg_p` 输出 |
| `src/IKA32010.sv` | L459~L471 移位器 B（`shb_output`） | 把累加器经移位送上 `reg_wrbus`，表指令用它当程序地址 |
| `src/IKA32010.sv` | L488~L494 RAM 地址与例化 | `ram_addr` 生成、`ram_dmov`/`ram_wr` 控制端 |
| `src/IKA32010.sv` | L540~L594 微码默认值区 | 本讲所有指令「不覆盖即取默认值」的来源 |
| `src/IKA32010.sv` | L159~L253 总线控制器地址 MUX 与逐相位时序 | IN/OUT/表读写事务的电平展开 |
| `src/IKA32010.sv` | L1909~L1940 `IKA32010_ram` 子模块 | DMOV 搬移的硬件实现 |
| `src/IKA32010.sv` | L1985~L2016 `IKA32010_multiplier` 子模块 | 乘法器三级流水（u2-l8 已讲，本讲只引用） |
| `src/IKA32010.sv` | L1465~L1743 译码块里的两组 casez 分支 | 本讲的主角 |
| `src/IKA32010_mnemonics.sv` | L32~L34、L56~L57 | `MUL_OP1_SOURCE_*`、`ALU_SOURCE_*` 常量 |

记忆口诀：**乘法三件套** = `reg_t_ld`（装 T）、`mul_en`（开乘）、`mul_op1_source_sel`（选第二操作数）；**回送一把闸** = `alu_pbsel`；**I/O 两根线** = `busctrl_req`（做什么）+ `busctrl_addr_muxsel`（地址是 PC 还是 PA）。

## 4. 核心概念与源码讲解

### 4.1 乘法器装载与乘法启动：LT / LTA / LTD / MPY / MPYK

#### 4.1.1 概念说明

TMS32010 的乘法是一条独立的硬件流水线：你先往 **T 寄存器**里放一个操作数，再用一条乘法指令把 T 和另一个操作数相乘，乘积落在 **P 寄存器**里——而且因为子模块内部多了一级锁存，**P 比乘法指令晚一个周期才更新**（详见 u2-l8）。

这意味着「装 T」和「做乘法」必须拆成两条指令，否则 T 还没稳定就乘了。微码用三个信号控制这条流水线：

- `reg_t_ld`：让 T 寄存器在本拍 `cyc_ncen` 从 `reg_wrbus` 载入；
- `mul_en`：打开乘法器，让它锁存操作数并计算；
- `mul_op1_source_sel`：选择乘法器的第二操作数来自 RAM（`MPY`）还是指令字立即数（`MPYK`）。

`LTA` 和 `LTD` 是「组合指令」：它们在装载新 T 的同时，顺手把**上一周期**的旧 P 加到累加器（`LTA`），甚至再顺手做一次数据搬移（`LTD`）。这是为 FIR 滤波器抽头内循环量身定做的。

#### 4.1.2 核心流程

乘法通路的默认状态（来自微码默认值区）是「不装 T、不开乘、第二操作数默认取 RAM」：

```text
默认：reg_t_ld=NO, mul_en=NO, mul_op1_source_sel=MUL_OP1_SOURCE_RAM
```

五条指令各自覆盖的信号：

| 指令 | 操作码（`?` = 任意） | 覆盖的关键信号 | 语义 |
| --- | --- | --- | --- |
| `LT`  | `0110_1010_????_????` | `reg_t_ld=YES` | T ← RAM[addr] |
| `LTA` | `0110_1100_????_????` | `reg_t_ld=YES` + APAC 三信号 | T ← RAM[addr]，且 ACC += P |
| `LTD` | `0110_1011_????_????` | `reg_t_ld=YES` + APAC 三信号 + `ram_dmov=YES` | T ← RAM[addr]，ACC += P，且 RAM[addr+1] ← RAM[addr] |
| `MPY` | `0110_1101_????_????` | `mul_en=YES`, `mul_op1_source_sel=RAM` | P ← T × RAM[addr] |
| `MPYK`| `100?_????_????_????` | `mul_en=YES`, `mul_op1_source_sel=IMM`, `wrbus=IMM` | P ← T × 立即数 |

注意 `LT/LTA/LTD/MPY` 都带有间接寻址操作数（操作码低字节），因此它们的 casez 分支里都复制了一段**间接寻址副作用译码片段**（由 `if_opcodereg[7:5:4:3:0]` 驱动 AR 自增自减与 ARP 改写），这段在 u2-l4 已讲，本讲不展开。

#### 4.1.3 源码精读

先看默认值区给乘法器的「闲置态」：[src/IKA32010.sv:572-573](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L572-L573) 设 `reg_t_ld = NO; mul_en = NO; mul_op1_source_sel = MUL_OP1_SOURCE_RAM;`。所以不碰乘法器的指令（绝大多数）自动保持 T、P 不变。

T 寄存器的装载逻辑在 [src/IKA32010.sv:383-392](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L383-L392)：复位清零，否则在 `cyc_ncen` 拍当 `reg_t_ld` 为高时把 `reg_wrbus`（默认就是 RAM 读出的数据）载入 `reg_t`。

乘法器第二操作数的选择与符号扩展在 [src/IKA32010.sv:394-396](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L394-L396)：

```verilog
reg             mul_op1_source_sel;
wire [15:0] mul_op1 = mul_op1_source_sel ? reg_wrbus
                       : {{3{reg_wrbus[12]}}, reg_wrbus[12:0]}; //sign extended
```

- 选 `MUL_OP1_SOURCE_RAM`（=1）：`mul_op1 = reg_wrbus`，即 16 位 RAM 数据原样送入。
- 选 `MUL_OP1_SOURCE_IMM`（=0）：取 `reg_wrbus[12:0]` 符号扩展到 16 位。

这条「符号扩展」分支是为 `MPYK` 的 13 位有符号立即数设计的，**但**（承接 u2-l8 的遗留点）`MPYK` 走 `WRBUS_SOURCE_IMM`，而该源在 [src/IKA32010.sv:139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139) 只把 `if_opcodereg[7:0]` 零扩展送上总线（`{8'h00, if_opcodereg[7:0]}`），于是 `reg_wrbus[12]` 恒为 0，符号扩展退化为零扩展，`MPYK` 实际拿到的是 8 位无符号立即数。下文 4.1.4 会让你亲自核对这一点。

乘法器子模块例化在 [src/IKA32010.sv:398-401](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L398-L401)：`i_OP0` 永远是 `reg_t`，`i_OP1` 是上面的 `mul_op1`，乘积 `o_P` 即 `reg_p`。

现在看五条指令的 casez 分支。`LT` 在 [src/IKA32010.sv:1478-1493](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1478-L1493)，只设 `reg_t_ld = YES`，其余沿用默认——干净利落的「装 T」。

`MPY` 在 [src/IKA32010.sv:1534-1549](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1534-L1549)：`mul_en = YES; mul_op1_source_sel = MUL_OP1_SOURCE_RAM;`，第二操作数取 RAM 数据。注意它**没有** `reg_t_ld`，所以 T 保持上一次 `LT/LTA/LTD` 装入的值——这正是「先 LT 后 MPY」两周期配合的根因。

`MPYK` 在 [src/IKA32010.sv:1551-1559](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1551-L1559)：`register_wrbus_source_sel = WRBUS_SOURCE_IMM;`（把立即数送上总线），`mul_en = YES; mul_op1_source_sel = MUL_OP1_SOURCE_IMM;`。它的操作码模式 `100?_????_????_????` 用最高位做主匹配，13 位立即数占 `if_opcodereg[12:0]`。

`LTA` 在 [src/IKA32010.sv:1495-1512](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1495-L1512)，`LTD` 在 [src/IKA32010.sv:1514-1532](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1514-L1532)。两者都把 APAC 的三信号（`alu_pbsel=ALU_SOURCE_MUL; alu_modesel=ALU_ADD; alu_acc_ld=YES;`，见 4.2）和 `reg_t_ld=YES` 同时点亮；`LTD` 再多一句 `ram_dmov = YES`。它们的差异**只有这一行**，却对应了「装 T + 累加旧 P」与「装 T + 累加旧 P + 推进延迟线」两种语义——水平微码的威力就在这里：多干一件事往往只需多置一个比特。

#### 4.1.4 代码实践：核对 MPYK 的立即数位宽

1. **实践目标**：亲手验证「MPYK 实际使用 8 位立即数」这一遗留点，理解 `WRBUS_SOURCE_IMM` 与符号扩展的相互作用。
2. **操作步骤**（源码阅读型）：
   - 读 [src/IKA32010.sv:139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139)，确认 `WRBUS_SOURCE_IMM` 送上 `reg_wrbus` 的是 `{8'h00, if_opcodereg[7:0]}`。
   - 读 [src/IKA32010.sv:395](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L395)，确认 `IMM` 分支符号扩展的是 `reg_wrbus[12:0]`。
   - 假设执行 `MPYK -1`（手册含义：13 位有符号 −1，即 `if_opcodereg[12:0] = 13'b1_1111_1111_1111`）。
3. **需要观察的现象**：由于 `WRBUS_SOURCE_IMM` 只取 `[7:0] = 8'b1111_1111 = 255`，而 `[12:8]` 被填 0，于是 `reg_wrbus[12]=0`，符号扩展后 `mul_op1 = 16'd255` 而非 `16'shFFFF`（−1）。
4. **预期结果**：`MPYK` 把立即数当 8 位**无符号**处理，乘积 = T × 255。若想验证，可在 testbench 里放一条 `MPYK` 并用反汇编（`disasm_type3` 在 [src/IKA32010_disasm.sv:121](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_disasm.sv#L121) 用 `signed'(opcodereg[12:0])` 打印成 13 位有符号十进制）对照观察——**反汇编打印的数值与乘法器实际使用的数值会不一致**，这正是问题所在。
5. **结论**：标注「待本地验证负立即数乘积」。这是一个值得报告给上游的候选问题。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LT` 和 `MPY` 必须是两条独立指令，而不能合并成「读 RAM、装 T、立刻乘」一条？

> **答案**：乘法器子模块内部有 `op0_latch → result` 的两级锁存，乘积比操作数锁存晚一个周期（u2-l8）。若同一周期既装 T 又开乘，`i_OP0`（= `reg_t`）要到本拍 `cyc_ncen` 才更新，而 `mul_en` 在本周期就开始锁存，两者错位会乘到旧 T。拆成两条指令可保证 `MPY` 周期内 `reg_t` 已稳定。

**练习 2**：`LTA` 与 `LTD` 的 casez 分支只差一行 `ram_dmov = YES`。这一行如何同时改变 RAM 的写地址与写数据？

> **答案**：见 [src/IKA32010.sv:1919-1923](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1919-L1923)：`i_DMOV=1` 时 `ram_we` 强制为 1（强制写）、写地址变成同页 +1（`{i_ADDR[7], i_ADDR[6:0]+1}`）、写数据取读端口输出 `ram_dout`，即完成 `RAM[addr+1] ← RAM[addr]`。

### 4.2 乘积回送累加器：APAC / PAC / SPAC

#### 4.2.1 概念说明

乘积落在 P 寄存器后，要进累加器（ACC）才能参与后续运算。TMS32010 提供三条「把 P 经 ALU 送进 ACC」的指令，区别只是 ALU 对 P 做什么：

- `APAC`（Add P to ACCumulator）：ACC ← ACC + P
- `PAC`（Load ACC from P）：ACC ← P（丢弃原 ACC）
- `SPAC`（Subtract P from ACC）：ACC ← ACC − P

它们的关键开关是 **`alu_pbsel`**：默认它选移位器 A 的输出（`ALU_SOURCE_SHFT`），这三条指令把它改写成 `ALU_SOURCE_MUL`，把 ALU 端口 B 的来源切换到乘法器的 P 寄存器。

#### 4.2.2 核心流程

ALU 端口 B 的来源由 `alu_pbsel` 在移位器 A 与乘法器 P 之间二选一，见 [src/IKA32010.sv:451](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L451) 的实例化：`.i_ALU_PB(alu_pbsel ? reg_p : sha_output)`。

三条指令对 ALU 控制信号的覆盖：

| 指令 | 操作码 | `alu_pbsel` | `alu_modesel` | `alu_paz`（屏蔽 ACC 反馈） | 语义 |
| --- | --- | --- | --- | --- | --- |
| `APAC` | `0111_1111_1000_1111` | `MUL` | `ADD` | NO（默认） | ACC + P |
| `PAC`  | `0111_1111_1000_1110` | `MUL` | `ADD` | **YES** | 0 + P = P |
| `SPAC` | `0111_1111_1001_0000` | `MUL` | **`SUB`** | NO | ACC − P |

`PAC` 的巧思：它仍然用 `ALU_ADD`，但用 `alu_paz = YES` 把端口 A（ACC 反馈）强制清零，于是 `0 + P = P`，等价于「装载」。`SPAC` 则把模式切到 `ALU_SUB`，减法在 ALU 内部由「取反端口 B + 进位 1」实现（u2-l7）。

注意这三条**没有数据操作数**（操作码是全定点），所以不需要间接寻址片段，casez 分支极短。

#### 4.2.3 源码精读

默认值里 `alu_pbsel = ALU_SOURCE_SHFT`（[src/IKA32010.sv:552](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L552)），即默认 ALU 吃移位器 A 的数据，不碰乘法器。

`APAC` 在 [src/IKA32010.sv:1468-1476](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1468-L1476)：三行 `alu_pbsel = ALU_SOURCE_MUL; alu_modesel = ALU_ADD; alu_acc_ld = YES;` 就完事——端口 B 取 P，加到端口 A（ACC 反馈），结果写回 ACC。

`PAC` 在 [src/IKA32010.sv:1561-1569](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1561-L1569)，多了一句 `alu_paz = YES; //block acc feedback`。

`SPAC` 在 [src/IKA32010.sv:1571-1579](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1571-L1579)：把 `alu_modesel` 改成 `ALU_SUB`。

对比 `APAC`（1468）和 `LTA`/`LTD` 里的 APAC 部分（1497-1498 / 1516-1517），你会发现**完全相同的三行**——`LTA/LTD` 就是「LT + 内联 APAC」。这就是 4.1 里说「多干一件事只需多置一个比特」的另一面：组合指令是把简单指令的控制信号**抄一份**到同一分支。

#### 4.2.4 代码实践：把 APAC 改读成 PAC

1. **实践目标**：通过对比微码差异，直观感受 `alu_paz` 一个比特如何把「加」变成「装载」。
2. **操作步骤**（源码阅读型）：
   - 并排打开 `APAC`（[L1468-L1476](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1468-L1476)）与 `PAC`（[L1561-L1569](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1561-L1569)）。
   - 列出两者控制信号的差异表。
   - 假设此时 `ACC = 100`，`P = 7`，分别手算 `APAC` 和 `PAC` 执行后的 ACC。
3. **需要观察的现象**：两者唯一区别是 `alu_paz`。
4. **预期结果**：`APAC` → ACC = 107；`PAC` → ACC = 7（ACC 反馈被屏蔽，端口 A = 0）。
5. 若有仿真环境，可在一段小程序里先 `MPY` 再分别用 `APAC`/`PAC`，观察累加器波形印证；否则记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`SPAC` 用了 `ALU_SUB`，但 `APAC` 和 `PAC` 都用 `ALU_ADD`。为什么 `PAC` 不需要一个专门的「装载」ALU 模式？

> **答案**：装载 P 等价于「0 + P」。用 `alu_paz = YES` 把端口 A（ACC 反馈）清零，`ADD` 就退化为 `0 + P = P`，无需新增 ALU 模式，复用现有加法器即可。

**练习 2**：`LTD` 里的「APAC 部分」加到 ACC 的 P 是**本次**乘积还是**上一次**乘积？为什么这对 FIR 内循环至关重要？

> **答案**：是**上一次**的 P。因为 P 寄存器在乘法器子模块里多一级锁存，比 `MPY` 指令晚一个周期（u2-l8）。FIR 内循环里，`LTD` 装入的是**新** T（供下一拍 `MPY` 用），累加的是**旧** P（上一拍的乘积），两者正好错开一拍，形成无缝流水。

### 4.3 并口 I/O 指令：IN / OUT（两周期总线事务）

#### 4.3.1 概念说明

TMS32010 没有专门的 I/O 地址空间，而是借用程序总线：用 `o_AOUT` 的低 3 位作为外设口地址 **PA0~PA7**（由指令字 `[10:8]` 给出），用 `o_DEN_n` 选通外设。两条指令：

- `IN`：从 PA 口读 16 位数据，写入数据 RAM。
- `OUT`：把数据 RAM 的内容写到 PA 口。

它们都是**两周期指令**：第一周期做实际 I/O 事务并把 PC 冻住、拒绝中断；第二周期把数据落盘（IN）或恢复取指（OUT）。两周期的根因是「总线被 I/O 事务占用，没法同时取指」，必须多花一拍去取下一条指令。

#### 4.3.2 核心流程

两条指令共用 `if(ex_inst_cycle==2'd0) … else if(==2'd1) …` 两相位模板：

**IN**（[src/IKA32010.sv:1604-1632](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1604-L1632)）：

| 相位 | `busctrl_req` | `addr_muxsel` | `if_pc_modesel` | 做什么 |
| --- | --- | --- | --- | --- |
| cycle0 | `COMMAND_IN` | `PERIPHERAL`（地址=PA） | `PC_HOLD` | `DEN_n` 拉低读外设，相位 3 把 `i_DIN` 锁进 `busctrl_inlatch`；`ex_inst_cycle_rst=NO`、`force_iack=NO` 推进且禁止中断 |
| cycle1 | `OPCODE_READ` | `PC` | （默认 `INCREASE`） | `reg_wrbus = WRBUS_SOURCE_INLATCH`、`ram_wr=YES`：把刚读到的外设数据写进 RAM[addr]；恢复取指 |

**OUT**（[src/IKA32010.sv:1634-1661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1634-L1661)）：

| 相位 | `busctrl_req` | `addr_muxsel` | 关键数据通路 | 做什么 |
| --- | --- | --- | --- | --- |
| cycle0 | `COMMAND_OUT` | `PERIPHERAL`（地址=PA） | `reg_wrbus = RAM` | 总线控制器在相位 1 把 `reg_wrbus`（=RAM 数据）送上 `o_DOUT`，相位 2 发 `WE_n` 脉冲；`PC_HOLD`、禁止中断 |
| cycle1 | `OPCODE_READ` | `PC` | — | 恢复取指（OUT 不写 RAM，故 cycle1 不设 `ram_wr`） |

#### 4.3.3 源码精读

外设地址怎么来？看地址 MUX [src/IKA32010.sv:159-163](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L159-L163)：`busctrl_mode[3]=1` 时 `o_AOUT = {9'b0, if_opcodereg[10:8]}`，即 PA 口号。`busctrl_mode[3]` 就是 `busctrl_addr_muxsel`（[src/IKA32010.sv:172-173](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L172-L173)）。

`COMMAND_IN` 的逐相位时序在 [src/IKA32010.sv:233-241](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L233-L241)：相位 0~2 `DEN_n=0` 选通外设，相位 3 抬起并把 `i_DIN` 锁进 `busctrl_inlatch`。注意它和「指令读」都用 `i_DIN`，但选通不同（`DEN_n` vs `MEN_n`），所以外设数据与指令 ROM 共享 `i_DIN` 总线、靠选通互斥（u2-l3）。

`COMMAND_OUT` 的逐相位时序在 [src/IKA32010.sv:244-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L244-L252)：相位 1 `o_DOUT_OE=1; o_DOUT <= reg_wrbus;`（驱动数据），相位 2 `WE_n=0`（写脉冲）。这里 `o_DOUT` 直接取 `reg_wrbus`，而 OUT 的 cycle0 设了 `register_wrbus_source_sel = WRBUS_SOURCE_RAM`（[src/IKA32010.sv:1639](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1639)），于是送出去的就是 RAM 数据。

> ⚠️ **读代码留意点**：[src/IKA32010.sv:1639](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1639) 写成 `register_wrbus_source_sel <= WRBUS_SOURCE_RAM;`，在**组合** `always @(*)` 块里用了非阻塞赋值 `<=`。这与周围 `busctrl_req = …` 的阻塞赋值风格不一致，是个值得留意的代码风格点（仿真与综合行为建议本地确认）。本讲不改动它，仅提示你阅读时注意。

IN 的 cycle1 把 `busctrl_inlatch` 写进 RAM：`register_wrbus_source_sel = WRBUS_SOURCE_INLATCH; ram_wr = YES;`（[src/IKA32010.sv:1617-1618](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1617-L1618)），`WRBUS_SOURCE_INLATCH` 的 MUX 分支见 [src/IKA32010.sv:141](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L141)。两周期指令的「自终止」靠 cycle0 设 `ex_inst_cycle_rst = NO`、cycle1 沿用默认 `YES` 自动归零（u3-l2）。

#### 4.3.4 代码实践：列 IN 与 OUT 的逐相位电平表

1. **实践目标**：把微码的「模式选择」与总线控制器的「电平展开」两层对应起来。
2. **操作步骤**：
   - 对 `IN`：从 [src/IKA32010.sv:1606-1618](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1606-L1618) 读出 cycle0/cycle1 的 `busctrl_req`，再查 `COMMAND_IN`（[L233-L241](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L233-L241)）和 `OPCODE_READ`（[L201-L208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L201-L208)）的相位电平。
   - 对 `OUT`：同理查 `COMMAND_OUT`（[L244-L252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L244-L252)）。
   - 为两条指令各列一张「cycle × cyclecntr(0~3) → `MEN_n/DEN_n/WE_n/DOUT_OE`」表。
3. **需要观察的现象**：IN 的 cycle0 期间 `DEN_n` 在相位 0~2 为低、相位 3 为高；OUT 的 cycle0 期间 `DOUT_OE` 在相位 1~2 为高、`WE_n` 在相位 2 为低。
4. **预期结果**：两张表应清晰显示「读外设」与「写外设」的电平差异，且 `MEN_n` 在整个 I/O 期间恒为高（不碰程序 ROM）。
5. 无法本地仿真则记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 IN 的数据要先用 cycle0 锁进 `busctrl_inlatch`，cycle1 再写 RAM，而不是 cycle0 直接写 RAM？

> **答案**：cycle0 的总线被 `COMMAND_IN` 占用（`DEN_n` 拉低读外设），`i_DIN` 直到相位 3 才稳定可用，而 RAM 写地址 `ram_addr` 由指令操作数决定、数据要来自 `reg_wrbus`。把外设数据中转锁进 `busctrl_inlatch`，下一周期再把 `INLATCH` 经 `reg_wrbus` 写入 RAM，时序上更干净，也避免了「同一周期既要读外设又要写 RAM」的端口冲突。

**练习 2**：IN 和 OUT 的 cycle0 都设了 `if_pc_modesel = PC_HOLD` 和 `if_opcodereg_force_iack = NO`，分别有什么作用？

> **答案**：`PC_HOLD` 让 PC 冻住，使 cycle1 取到的下一条指令地址正确（不会因为 cycle0 没取指而错位）；`force_iack = NO` 让指令寄存器在 cycle0（非取指事务）保持不变，使同一 `casez` 分支在 cycle1 继续命中，同时附带「拒绝中断插入」的原子性效果（u3-l1/u3-l2）。

### 4.4 表读写指令：TBLR / TBLW（三相位 + 栈借用）

#### 4.4.1 概念说明

「表指令」用来在**程序空间**与**数据 RAM** 之间搬数据：程序 ROM 里常存放滤波器系数表、正弦表等常数，`TBLR` 把它们读进 RAM，`TBLW` 反向写回。

难点在于：程序空间用 **PC** 寻址，而指令本身也要靠 PC 取指。当 `TBLR/TBLW` 要用 ACC 指定的地址去访问程序空间时，PC 就被「征用」了，没法同时取下一条指令。源码的解法很巧妙——**借用 4 级硬件栈保存返回地址**，事后再弹回 PC。因此这两条是**三周期指令**：

- `TBLR`（Table Read）：`RAM[ram_addr] ← 程序空间[ACC[11:0]]`
- `TBLW`（Table Write）：`程序空间[ACC[11:0]] ← RAM[ram_addr]`

#### 4.4.2 核心流程

`TBLR` 三相位（[src/IKA32010.sv:1663-1702](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1663-L1702)）：

| 相位 | `busctrl_req` | `reg_wrbus` 来源 | `if_pc_modesel` | 栈 | 作用 |
| --- | --- | --- | --- | --- | --- |
| cycle0 | `STOP` | `SHB`（=ACC） | `PC_LOAD_WRBUS` | push PC | 准备：PC 即将 ← ACC，先把返回地址压栈 |
| cycle1 | `DATA_READ` | `STACK`（=返回地址） | `PC_LOAD_WRBUS` | pop | 执行：在 PC=ACC 处读程序空间 → `busctrl_inlatch`；同时 PC ← 返回地址 |
| cycle2 | `OPCODE_READ` | `INLATCH` | （默认） | — | 落盘：把读到的常数写进 RAM[ram_addr]，恢复取指 |

`TBLW` 三相位（[src/IKA32010.sv:1704-1743](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1704-L1743)）：

| 相位 | `busctrl_req` | 关键动作 |
| --- | --- | --- |
| cycle0 | `DATA_READ` | `PC ← ACC`（`SHB→wrbus`），push 返回地址 |
| cycle1 | `DATA_WRITE` | 在 PC=ACC 处把 `RAM[ram_addr]`（`ram_output`）写进程序空间；pop 恢复 PC |
| cycle2 | `OPCODE_READ` | 恢复取指 |

> 关键技巧：`PC_LOAD_WRBUS`（[src/IKA32010_mnemonics.sv:6](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L6)）让 PC 从 `reg_wrbus` 取值。配合 `WRBUS_SOURCE_SHB`，`reg_wrbus` 就是移位器 B 输出的累加器（[src/IKA32010.sv:463-470](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L463-L470) 默认 `shb_mux=LOW`、`shb_amt=0` 时取 ACC 低 16 位），于是「PC ← ACC[11:0]」一行搞定；配合 `WRBUS_SOURCE_STACK`，`reg_wrbus` 又变成栈顶返回地址，于是「PC ← 返回地址」也一行搞定。**同一个 `PC_LOAD_WRBUS` 模式，靠改 `reg_wrbus` 的来源服务于两种用途**——这是理解表指令的钥匙。

#### 4.4.3 源码精读

移位器 B（输出侧）在 [src/IKA32010.sv:459-471](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L459-L471)：`shb_amt=0` 时 `shb_intermediate = alu_acc_output`，`shb_mux=LOW`（默认）时 `shb_output = shb_intermediate[15:0]`。所以 `WRBUS_SOURCE_SHB` 在表指令里就是把 ACC 低 16 位送上总线，再由 `PC_LOAD_WRBUS` 截取低 12 位装入 PC。

`TBLR` cycle1 的 `DATA_READ` 时序见 [src/IKA32010.sv:211-219](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L211-L219)：相位 3 把 `i_DIN` 锁进 `busctrl_inlatch`——读到的常数暂存于此。cycle2 再用 `WRBUS_SOURCE_INLATCH` + `ram_wr=YES` 把它写进 RAM。

`TBLW` cycle1 的 `DATA_WRITE` 时序见 [src/IKA32010.sv:222-230](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L222-L230)：相位 1 `o_DOUT <= ram_output`（直接取 RAM 读端口数据，不经 `reg_wrbus`），相位 2 `WE_n=0` 写脉冲。所以 `TBLW` 写进程序空间的数据是 `RAM[ram_addr]`。

> ⚠️ **需在仿真中验证的细节**：`TBLW` 的 cycle0 发起的是 `DATA_READ`（读程序空间），而 cycle2 又设了 `ram_wr=YES` 配合 `WRBUS_SOURCE_INLATCH`（把 cycle0 读到的值写回 RAM[ram_addr]）。这与 `TBLR` 的 cycle2「写回 RAM」结构相同，但 `TBLW` 的 cycle0 读取地址是 PC 尚未更新前的旧值。其净效果（是否会在搬运后改写源 RAM 单元）建议在仿真中用波形确认。本讲只如实描述译码，不断言其完整副作用。

#### 4.4.4 代码实践：TBLR 三相位信号追踪表

1. **实践目标**：把 `TBLR` 三个相位的「总线事务 + PC + 栈 + RAM 写」四路信号填进一张表，验证 `RAM[ram_addr] ← 程序[ACC]` 的数据流。
2. **操作步骤**：
   - 读 [src/IKA32010.sv:1664-1697](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1664-L1697)，对每个相位记录：`busctrl_req`、`register_wrbus_source_sel`、`if_pc_modesel`、`stk_push/stk_pop`、`ram_wr`、`ex_inst_cycle_rst`。
   - 假设执行前 `ACC=0x0050`（程序地址）、栈空、`ram_addr=0x10`、`程序[0x050]=0x1234`。
   - 逐相位推演 PC、栈顶、`busctrl_inlatch`、`RAM[0x10]` 的变化。
3. **需要观察的现象**：cycle0 后 PC 变为 0x050、栈顶存原返回地址；cycle1 后 `busctrl_inlatch=0x1234`、PC 恢复；cycle2 后 `RAM[0x10]=0x1234`。
4. **预期结果**：`RAM[0x10]` 最终等于 `程序[0x050]`，且 PC、栈都恢复到指令前的状态。
5. 记为「待本地验证」若无仿真器；重点是能把四个相位的信号值填对。

#### 4.4.5 小练习与答案

**练习 1**：`TBLR` 为什么需要 3 个周期，而 `IN` 只要 2 个？

> **答案**：`IN` 的外设地址（PA）由指令字 `[10:8]` 直接给出，不占用 PC，cycle0 读完外设、cycle1 写 RAM 即可。`TBLR` 的源地址在 ACC 里、要借 PC 去寻址程序空间，而 PC 本身又是取指所必需，于是要先花 cycle0 把返回地址压栈并把 PC 指向 ACC，多出一个「准备拍」。

**练习 2**：`TBLR` cycle1 同时做了「读程序空间」和「pop 栈恢复 PC」。这两件事在数据通路上会不会冲突？

> **答案**：不会。读程序空间用的是外部总线（`i_DIN` → `busctrl_inlatch`），栈恢复 PC 用的是内部 `reg_wrbus`（`WRBUS_SOURCE_STACK` → `PC_LOAD_WRBUS`），两者走不同通路。总线控制器此时面向外部（`DATA_READ`），栈与 PC 是内部操作，可并行。

## 5. 综合实践：追踪一条 LTD 指令

`LTD` 是本讲最有代表性的指令——它在**一个机器周期**内同时完成 `LT`（装载 T）、`APAC`（旧 P 加到 ACC）与 `DMOV`（RAM 数据搬移）三件事。请完整追踪它，把「乘加流水线」与「延迟线推进」串起来。

**背景**：FIR 滤波器的一个抽头内循环（卷积）需要反复执行「取一个数据、乘系数、累加、数据移位」。TMS32010 用 `LTD + MPY` 两条指令的流水来高效完成：

```
; 经典 FIR 抽头循环（示意，非项目原始代码）
;   设 AR0 指向数据缓冲，AR1 指向系数表
LT   *-        ; T = 数据[n]      （首次装载）
MPY  *-        ; P = T * 系数[n]   ; AR 自减
LTD  *+, AR1   ; ACC += P(旧); T = 数据[n-1]; 数据[n]→数据[n+1]; 切到 AR1
MPY  *-, AR0   ; P = T * 系数[n-1]
...            ; 重复 LTD/MPY 即可完成整段卷积
```

> 上面是教学示意程序，不是仓库里的真实代码，标注为「示例代码」。

**实践目标**：解释为什么 `LTD` 能单周期完成三件事，并指出哪一件用的是「新」数据、哪一件用的是「旧」数据。

**操作步骤**：

1. 打开 `LTD` 的 casez 分支 [src/IKA32010.sv:1514-1532](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1514-L1532)，把它设置的信号分成三组：
   - LT 组：`reg_t_ld = YES`
   - APAC 组：`alu_pbsel = ALU_SOURCE_MUL; alu_modesel = ALU_ADD; alu_acc_ld = YES;`
   - DMOV 组：`ram_dmov = YES`
2. 对照默认值（[src/IKA32010.sv:590](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L590) `register_wrbus_source_sel = WRBUS_SOURCE_RAM` 未被覆盖），确认 `reg_wrbus` 本周期 = RAM 读出值 = `RAM[ram_addr]`。
3. 分别回答三件事的数据来源与时机：
   - **装 T**：`reg_t` 在 `cyc_ncen` 拍载入 `reg_wrbus`（[L389-L390](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L389-L390)），即新 T = 当前 `RAM[ram_addr]`。这个新 T 要等**下一拍** `MPY` 才被乘。
   - **累加 P**：`alu_pbsel=MUL` 让端口 B 取 `reg_p`。但 `reg_p` 是乘法器子模块上一周期锁存的结果（u2-l8 的「晚一拍」），所以累加的是**上一次** `MPY` 的乘积——旧 P。
   - **数据搬移**：`ram_dmov=YES` 让 RAM 子模块在 `i_DMOV=1` 时强制写、写地址同页 +1、写数据取读端口 `ram_dout`（[L1919-L1923](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1919-L1923)），即 `RAM[ram_addr+1] ← RAM[ram_addr]`——推进延迟线。
4. 画出这一拍的数据流向图（文字版即可）：

   ```
   RAM[addr] ──(读端口, reg_wrbus)─┬─→ reg_t          (新 T, 供下拍 MPY)
                                   ├─→ ALU.portB? 否, ALU.portB 取 reg_p
                                   └─→ RAM 写口 din(经 DMOV) → RAM[addr+1]  (延迟线)
   reg_p(上一拍 MPY 的乘积) ──→ ALU.portB ──+──→ ACC  (旧 P 累加)
   ACC(旧) ─────────────────────→ ALU.portA ──┘
   ```

**需要观察的现象**：三件事在同一个 `cyc_ncen` 边沿生效，但用的是**同一份** `RAM[ram_addr]` 读值的两个不同去向（装 T 与搬移），加上一个**延迟一拍**的 `reg_p`（累加）。

**预期结果**：你能清楚说出「新 T + 旧 P + 延迟线推进」三者并行不悖的原因——
- 装新 T 与搬移都用「当前」RAM 读值，互不干扰（一个进寄存器，一个进高地址单元）；
- 累加用的是「上一拍」的 P，与正在装入的新 T 在时间上错开，正是乘法器一级锁存带来的天然流水；
- 三者由同一组微码信号在同一周期点亮，故只需一条指令、一个机器周期。

若本地有仿真器，可写一段「`LT`/`MPY`/`LTD`/`MPY`」序列，在 `LTD` 那一拍观察 `reg_t`、`reg_p`、ACC、`RAM[addr+1]` 四个波形同时变化，验证上述分析；否则标注「待本地验证」。

**延伸思考**：如果 `LTD` 把 `reg_t_ld` 去掉（即只做 APAC + DMOV），FIR 循环还能正常工作吗？

> 不能。去掉装 T 后，下一拍的 `MPY` 会用到**更早**的 T（没有更新），导致乘的是错误的系数-数据配对。这正说明 `LTD` 把 `reg_t_ld` 也合并进来是必要的。

## 6. 本讲小结

- 乘法通路由三个微码信号驱动：`reg_t_ld`（装 T）、`mul_en`（开乘）、`mul_op1_source_sel`（第二操作数选 RAM 还是立即数）；`LT` 与 `MPY` 必须分属不同指令，以保证 T 在乘法周期内稳定。
- `LTA` / `LTD` 是「内联 APAC」的组合指令：在装新 T 的同时累加**上一拍**的旧 P，`LTD` 再加一个 `ram_dmov` 推进 FIR 延迟线——水平微码让「多干一件事」只需多置一个比特。
- `APAC` / `PAC` / `SPAC` 靠 `alu_pbsel = ALU_SOURCE_MUL` 把 P 切到 ALU 端口 B；`PAC` 用 `alu_paz` 屏蔽 ACC 反馈实现「0+P=装载」，`SPAC` 用 `ALU_SUB` 做减法，三者复用同一个加法器。
- `IN` / `OUT` 是两周期指令：cycle0 做 `COMMAND_IN/OUT` 外设事务并冻住 PC、禁止中断，cycle1 落盘或恢复取指；外设地址取自指令字 `[10:8]`（PA 口）。
- `TBLR` / `TBLW` 是三周期指令，靠**借用硬件栈**保存返回地址、用 `PC_LOAD_WRBUS` 配合 `WRBUS_SOURCE_SHB/STACK` 在「PC←ACC」与「PC←返回地址」之间切换，完成程序空间与数据 RAM 的互搬。
- 已发现两个值得上游关注的点：`MPYK` 因复用 8 位 `WRBUS_SOURCE_IMM` 通路，立即数被当 8 位无符号处理（与手册 13 位有符号不符）；`TBLW` 的 cycle0 `DATA_READ` 与 cycle2 `ram_wr=INLATCH` 的副作用需仿真确认。两者均标注「待本地验证」。

## 7. 下一步学习建议

至此，`casez` 译码块里的六大指令组（控制/辅助寄存器、累加器、分支子程序、乘法器、I/O 数据存储）已全部讲完。建议：

1. **横向对照**：回头读 u3-l4 / u3-l5 / u3-l6，把所有指令组的「默认值 + 覆盖」风格统一起来看，体会水平微码的一致性。特别注意「间接寻址副作用片段」在 `LT/LTA/LTD/MPY/DMOV/IN/OUT/TBLR/TBLW` 里反复出现的复用模式。
2. **反汇编与调试**：进入 u3-l8（反汇编、助记符常量与编译选项），学习 `disasm_type1~6` 如何把本讲的指令打印成可读助记符，并用 `IKA32010_DISASSEMBLY` 宏在仿真中观察本讲指令的真实执行轨迹。
3. **系统集成**：进入 u3-l9（FPGA 综合与集成），把本讲的 `IN/OUT` 外设接口和乘法器的 DSP 块映射放到一个真实顶层里，写一段用 `OUT` 驱动外设、用 `MPY/LTD` 做一次小卷积的程序并上板验证。
4. **动手验证遗留点**：本讲标出的 `MPYK` 立即数位宽与 `TBLW` 副作用两个「待本地验证」点，非常适合作为上板/仿真练习——若确认是 bug，可对照 git 历史 `0532c08 fix multiplier bug` 提交修复。
