# 累加器算术逻辑类指令译码

## 1. 本讲目标

本讲聚焦 IKA32010 微码中最大的一组指令——**累加器算术逻辑类指令**。学完后你应当能够：

- 看懂 `casez(if_opcodereg)` 中「ACCUMULATOR INSTRUCTIONS」一段的每一条分支，并解释它设置了哪些控制信号。
- 把一条抽象指令（如 `ADD`、`LAC`、`SACH`）翻译成一串微码控制信号（`alu_modesel`、`sha_amt`、`alu_paz`、`alu_pbdata`、`alu_acc_ld`、`ram_wr` 等），并据此推断数据如何在 RAM → 移位器 → ALU → 累加器 → 移位器 → RAM 之间流动。
- 区分三类指令的译码套路：算术（驱动 ALU 与移位器 A）、逻辑（只作用 port B 低 16 位）、存储（用移位器 B 与 `ram_wr`）。
- 理解 `SUBC` 为什么是一条「内嵌两周期」的特殊指令，以及它为何要求「下一条指令不能访问 ACC」。

本讲不重复 ALU 内部加法器、饱和、SUBC 除法的数学推导（那是 u2-l7 的内容），而是站在**微码（控制信号驱动）**的视角，讲清「指令字 → 控制信号 → 数据通路」这一层。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们来自前置讲义）：

- **水平微码风格**（u3-l1）：顶层那个大 `always @(*)` 块先给所有控制信号赋「默认值」，再用 `casez(if_opcodereg)` 的阻塞赋值 `=` 顺序覆盖。默认值描述的是「读 RAM、做加法但不写回、PC 自增」这条最常见通路。
- **ALU 的端口模型**（u2-l7）：ALU 有 port A（默认取累加器反馈 `alu_acc_output`）、port B（经 `alu_pbsel` 在移位器 A 与乘法器 P 之间选），以及一个加法器 + 多路选择统一实现 ADD/SUB/AND/OR/XOR/ABS/SUBC。`alu_paz`/`alu_pbz` 可把对应端口强制清零。
- **两个桶形移位器**（u2-l7）：移位器 A 在 ALU **输入侧**（对 16 位 RAM 数据符号扩展后左移 `sha_amt`），移位器 B 在 ALU **输出侧**（对 32 位累加器左移后选高/低字写回）。
- **`reg_wrbus` 汇流**（u2-l1）：16 位内部写总线，由 `register_wrbus_source_sel` 在 7 个数据源中选一个；默认 `WRBUS_SOURCE_RAM`，所以算术指令的操作数「自动」来自 RAM。
- **RAM 地址生成**（u2-l5）：`ram_addr` 由指令字 bit7 二选一——直接寻址 `{DP,位移}` 或间接寻址 `AR[ARP]`；带数据操作数的指令尾部都粘着同一段「间接寻址副作用译码片段」。

如果你对上面任何一项感到陌生，建议先回看对应讲义，再继续本讲。

## 3. 本讲源码地图

本讲只涉及两个文件，但聚焦的范围很集中：

| 文件 | 本讲用到的部分 | 作用 |
| --- | --- | --- |
| [src/IKA32010.sv:L771-L1084](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L771-L1084) | `casez` 中的 ACCUMULATOR INSTRUCTIONS 段 | 18 条累加器类指令的逐条译码 |
| [src/IKA32010.sv:L540-L593](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L540-L593) | 微码默认值块 | 理解每条指令「继承了什么默认值」 |
| [src/IKA32010.sv:L424-L471](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L424-L471) | 移位器 A / 移位器 B 的组合逻辑 | 算术指令的输入定标、存储指令的输出定标 |
| [src/IKA32010.sv:L1797-L1863](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1797-L1863) | ALU 子模块内 port A/B 生成与运算选择 | 解释控制信号如何变成实际运算 |
| [src/IKA32010.sv:L131-L144](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L131-L144) | `reg_wrbus` 选源 MUX | 理解操作数从哪儿来、写回数据到哪儿去 |
| src/IKA32010_mnemonics.sv | `ALU_*` / `ALU_PBDATA_*` / `ALU_SOURCE_*` 常量 | 控制信号的命名字典 |

> 提示：本讲提到的所有 `ALU_ADD`、`ALU_PBDATA_LOWWORD` 等常量都定义在 [src/IKA32010_mnemonics.sv:L40-L57](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L40-L57)。

## 4. 核心概念与源码讲解

### 4.1 累加器类指令译码总览与数据通路

#### 4.1.1 概念说明

TMS32010 把「与累加器（ACC）直接打交道」的指令归为一大类，IKA32010 忠实地在 `casez` 里用一段注释横幅 `ACCUMULATOR INSTRUCTIONS` 把它们圈起来。这一类指令的共同点是：**它们都通过配置 ALU 与移位器来完成一次 ACC 的读—算—写**，区别只在于「让 port A 等于什么、让 port B 等于什么、结果要不要写回 ACC、要不要顺带把 ACC 存进 RAM」。

理解这一类的钥匙，是先在脑子里画好下面这张「数据通路静态图」：

```
   RAM[ram_addr] ──o──> reg_wrbus ──> 移位器A(符号扩展+左移sha_amt) ──┐
                    │                                                   ▼
                    │                              ┌── port B ──> [ ALU ] ──> alu_output ──> ACC(reg)
   IMM/SHB/... ─────┘                              └── port A <── ACC(reg) 反馈
                                                                       │
   ACC(reg) ──> 移位器B(左移shb_amt, 选高/低字) ──> shb_output ──> reg_wrbus ──> RAM[ram_addr] (当 ram_wr=YES)
```

要点：

- `reg_wrbus` 既是「操作数进 ALU」的来路（默认选 RAM），也是「结果回 RAM」的去路（当选 `WRBUS_SOURCE_SHB`）。它在一拍内只能被一个源驱动，但「读口」和「写口」分别由不同消费者控制，所以同一拍里 RAM 数据可以经 `reg_wrbus` 进 ALU，同时 ALU 的旧 ACC 经移位器 B 写回 RAM——两者并不冲突（读的是 RAM、写的是 RAM 的另一拍/另一单元）。
- 算术/逻辑指令把 ACC 当「主角」：读 ACC 反馈到 port A，结果写回 ACC（`alu_acc_ld=YES`）。
- 存储指令（SACH/SACL）把 ACC 当「数据源」：ACC 经移位器 B 流向 RAM，而 ACC 本身不变（`alu_acc_ld` 保持默认 `NO`）。

#### 4.1.2 核心流程

一条累加器类指令在微码里被译码时，固定经历：

1. 进入 `casez(if_opcodereg)`，按操作码匹配到对应分支。
2. 用阻塞赋值 `=` 覆盖默认值中的少数几个信号（多数信号沿用默认值）。
3. 若指令带数据操作数（bit7 决定直接/间接寻址），尾部粘上同一段「间接寻址副作用片段」，处理 AR 自增/自减与 ARP 改写。
4. `ifdef IKA32010_DISASSEMBLY` 块调用对应 `disasm_typeN` 打印反汇编（这部分由 u3-l8 详述，本讲略）。
5. 组合输出的控制信号在同一机器周期内即时作用于 ALU/移位器/RAM，结果在 `cyc_ncen` 拍写回。

#### 4.1.3 源码精读

先看注释横幅与整段的起点：

[src/IKA32010.sv:L771-L783](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L771-L783) —— 这段是 ACCUMULATOR INSTRUCTIONS 的开头，第一条是 `ABS`：

```verilog
//  ACCUMULATOR INSTRUCTIONS
//
//ABS - Absolute value of accumulator
16'b0111_1111_1000_1000: begin
    alu_modesel = ALU_ABS; alu_pbz = YES; //disable port B
    alu_acc_ld = YES;
    ...
end
```

可以看到，`ABS` 只动了 3 个信号：`alu_modesel`、`alu_pbz`、`alu_acc_ld`。其余如 `alu_paz`（NO）、`sha_amt`（0）、`register_wrbus_source_sel`（RAM）全部继承默认值。这就是「默认值 + 覆盖」的威力——一条指令往往只需写两三行。

为了方便后续讲解，先把这一段 18 条指令的操作码与「改动信号」整理成速查表：

| 指令 | 操作码（二进制前 8 位为固定前缀） | `alu_modesel` | 关键改动信号 | 含义 |
| --- | --- | --- | --- | --- |
| ABS | `0111_1111_1000_1000` | `ALU_ABS` | `alu_pbz=YES` | ACC ← \|ACC\| |
| ADD | `0000_????_????_????` | `ALU_ADD` | `sha_amt=[11:8]`,`alu_acc_ld` | ACC ← ACC + (RAM≪s) |
| ADDH | `0110_0000_????_????` | `ALU_ADD` | `sha_amt=16`,`alu_acc_ld` | ACC ← ACC + (RAM≪16) |
| ADDS | `0110_0001_????_????` | `ALU_ADD` | `sha_ssup=YES`,`alu_acc_ld` | ACC ← ACC + 无符号 RAM |
| AND | `0111_1001_????_????` | `ALU_AND` | `alu_pbdata=LOW`,`alu_acc_ld` | ACC ← ACC & {0,RAM} |
| LAC | `0010_????_????_????` | `ALU_ADD` | `alu_paz=YES`,`sha_amt=[11:8]`,`alu_acc_ld` | ACC ← RAM≪s |
| LACK | `0111_1110_????_????` | `ALU_ADD` | `alu_paz=YES`,`pbdata=BYTE`,`wrbus=IMM`,`alu_acc_ld` | ACC ← imm8（零扩展） |
| OR | `0111_1010_????_????` | `ALU_OR` | `alu_pbdata=LOW`,`alu_acc_ld` | ACC ← ACC \| {0,RAM} |
| SACH | `0101_1???_????_????` | （默认 ADD，不写回） | `wrbus=SHB`,`ram_wr=YES`,`shb_amt=[10:8]`,`shb_mux=HIGH` | RAM ← (ACC≪s)[31:16] |
| SACL | `0101_0???_????_????` | （默认 ADD，不写回） | `wrbus=SHB`,`ram_wr=YES`,`shb_mux=LOW` | RAM ← ACC[15:0] |
| SUB | `0001_????_????_????` | `ALU_SUB` | `sha_amt=[11:8]`,`alu_acc_ld` | ACC ← ACC − (RAM≪s) |
| SUBC | `0110_0100_????_????` | `ALU_SUBC` | `sha_amt=15`（ACC 下一拍才写回） | 条件减法，用于除法 |
| SUBH | `0110_0010_????_????` | `ALU_SUB` | `sha_amt=16`,`alu_acc_ld` | ACC ← ACC − (RAM≪16) |
| SUBS | `0110_0011_????_????` | `ALU_SUB` | `sha_ssup=YES`,`alu_acc_ld` | ACC ← ACC − 无符号 RAM |
| XOR | `0111_1000_????_????` | `ALU_XOR` | `alu_pbdata=LOW`,`alu_acc_ld` | ACC ← ACC ^ {0,RAM} |
| ZAC | `0111_1111_1000_1001` | `ALU_ADD` | `alu_paz=YES`,`alu_pbz=YES`,`alu_acc_ld` | ACC ← 0 |
| ZALH | `0110_0101_????_????` | `ALU_ADD` | `alu_paz=YES`,`pbdata=HIGH`,`sha_amt=16`,`alu_acc_ld` | ACC 高半 ← RAM，低半 ← 0 |
| ZALS | `0110_0110_????_????` | `ALU_ADD` | `alu_paz=YES`,`pbdata=LOW`,`alu_acc_ld` | ACC ← {0,RAM}（无符号低字） |

（表中 `s` 为移位量；`pbdata` 取 `LOW/HIGH/BYTE/LONGWORD`；`wrbus` 即 `register_wrbus_source_sel`。）

再回头看它们共同继承的默认值（节选自微码顶部）：

[src/IKA32010.sv:L551-L590](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L551-L590) —— 默认值：ALU 做加法、port A/B 不清零、port B 取移位器、ACC 不写回、`sha_amt=0`、`shb_amt=0`、`ram_wr=NO`、`register_wrbus_source_sel=WRBUS_SOURCE_RAM`。

```verilog
//ALU operation
alu_modesel = ALU_ADD; alu_paz = NO; alu_pbz = NO;
alu_pbdata = ALU_PBDATA_LONGWORD; alu_pbsel = ALU_SOURCE_SHFT;
//ACC load
alu_acc_ld = NO;
//RAM write
ram_wr = NO; ram_dmov = NO;
//shifter enable
sha_amt = 5'd0; sha_ssup = NO;
shb_amt = 3'd0; shb_mux = LOW;
//read source
register_wrbus_source_sel = WRBUS_SOURCE_RAM;
```

这 8 行默认值就是「读 RAM、符号扩展、不左移、加到 ACC 反馈上、但不写回 ACC」的完整描述。理解了它，你就理解了为什么 `ADD` 几乎什么都不用写也能工作——它只是把默认的 `alu_acc_ld=NO` 翻成 `YES`。

#### 4.1.4 代码实践

**实践目标**：在脑子里跑通「默认值 + 覆盖」的译码机制，建立「指令字 → 控制信号」的直觉。

**操作步骤**：

1. 打开 [src/IKA32010.sv:L540-L593](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L540-L593)，把默认值表抄到一张纸上。
2. 打开 `ABS` 分支 [src/IKA32010.sv:L776-L783](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L776-L783)，逐行列出它覆盖了哪几个信号。
3. 仿照 4.1.3 的速查表，为 `ZAC`（[src/IKA32010.sv:L1040-L1047](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1040-L1047)）手动推导它的完整控制信号集（含所有继承自默认的信号）。

**需要观察的现象**：你会发现 `ZAC`（清零累加器）只覆盖了 `alu_paz`、`alu_pbz`、`alu_acc_ld` 三个信号——把两个端口都强制清零，再做加法并写回，结果自然就是 0。它甚至没有用到一个「真正的清零运算」，而是复用了「加法 + 两个操作数都是 0」。

**预期结果**：`ZAC` 的完整信号集应为 `alu_modesel=ALU_ADD`、`alu_paz=YES`、`alu_pbz=YES`、`alu_pbdata=ALU_PBDATA_LONGWORD`、`alu_pbsel=ALU_SOURCE_SHFT`、`sha_amt=0`、`alu_acc_ld=YES`、`ram_wr=NO`、`register_wrbus_source_sel=WRBUS_SOURCE_RAM`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ABS` 要设 `alu_pbz=YES`（禁用 port B），而不是 `alu_paz=YES`？

**参考答案**：`ABS` 取的是累加器自身的绝对值，运算只发生在 port A（ACC 反馈）上。ALU 实现 `ABS` 的方式是「若 ACC 为负，则把 port A 取反并加 1（补码）」（见 u2-l7 与 [src/IKA32010.sv:L1814-L1816](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1814-L1816)）。若不禁用 port B，RAM 数据会作为加数混进来，破坏绝对值运算；所以必须用 `alu_pbz=YES` 把 port B 置零，而不是动 port A。

**练习 2**：速查表里 `ADD` 和 `LAC` 的 `alu_modesel` 都是 `ALU_ADD`，二者最本质的区别是哪一个信号？

**参考答案**：是 `alu_paz`。`ADD` 保持默认 `alu_paz=NO`，port A 取 ACC 反馈，于是 ACC + RAM；`LAC` 设 `alu_paz=YES`，port A 被强制清零，于是 0 + RAM = 直接装载。两者共用同一个加法器，只靠「是否屏蔽 ACC 反馈」来区分「加」与「装」。

---

### 4.2 算术加载与加减指令：驱动 ALU 与移位器 A

#### 4.2.1 概念说明

本模块覆盖这一类里数量最多的一簇指令：`LAC / ADD / SUB / ADDH / ADDS / SUBH / SUBS / ZALH / ZALS / ZAC / ABS`。它们的共同译码套路是：

- 用 `alu_modesel` 选择 `ALU_ADD`（装载、加、清零）或 `ALU_SUB`（减）；
- 用 `alu_paz` 决定 port A 是「ACC 反馈」还是「0」（从而区分加/减 vs 装载/清零）；
- 用 `sha_amt` 给移位器 A 设定**输入侧**的左移量；
- 用 `sha_ssup` 抑制符号扩展（实现「无符号」语义的 ADDS/SUBS/ZALS）；
- 用 `alu_acc_ld=YES` 把结果写回 ACC。

「移位」是定点 DSP 的命脉——乘以 \(2^n\) 就是左移 n 位。TMS32010 允许在「取操作数进 ALU」这一步顺手左移 0～15 位，从而把 `RAM ≪ s` 作为一个原子操作数。移位量 `s` 直接编码在指令字里。

#### 4.2.2 核心流程

以 `ADD dma, 4`（把 RAM 单元左移 4 位后加到 ACC）为例，控制信号的作用顺序是：

1. `register_wrbus_source_sel = WRBUS_SOURCE_RAM`（默认）→ `reg_wrbus = ram_output`。
2. 移位器 A（[src/IKA32010.sv:L428-L431](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L428-L431)）：先按 `sha_ssup` 决定是否符号扩展到 32 位，再左移 `sha_amt`。
3. `alu_pbsel = ALU_SOURCE_SHFT`（默认）→ port B = 移位器 A 输出。
4. `alu_paz = NO`（默认）→ port A = ACC 反馈。
5. `alu_modesel = ALU_ADD` → `alu_output = port_a + port_b`（含 OVM 饱和）。
6. `alu_acc_ld = YES` → 在 `cyc_ncen` 拍把 `alu_output` 写回 ACC。

对装载类（`LAC`），只把第 4 步的 `alu_paz` 翻成 `YES`，port A 变 0，加法退化为「直接装载」。

「无符号」变体（`ADDS/SUBS`）在第 2 步把 `sha_ssup=YES`，使 16 位 RAM 数据**零扩展**而非符号扩展到 32 位：

\[ \text{val}_{32} = \text{sha\_ssup}\ ?\ \{16'h0000, \text{RAM}\}\ :\ \{16\{\text{RAM}[15]\}, \text{RAM}\} \]

#### 4.2.3 源码精读

先看 `ADD` 与 `LAC` 这对「加 vs 装」的对照：

[src/IKA32010.sv:L785-L802](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L785-L802)（ADD）：

```verilog
//ADD - Add to accumulator with shift
16'b0000_????_????_????: begin
    alu_modesel = ALU_ADD; //load from port B
    alu_acc_ld = YES;
    sha_amt = {1'b0, if_opcodereg[11:8]};
    if(if_opcodereg[7]) begin        // 间接寻址副作用片段
        reg_ar_inc = if_opcodereg[5]; reg_ar_dec = if_opcodereg[4];
        if(!if_opcodereg[3]) begin
            if(if_opcodereg[0]) reg_arp_set = YES;
            else                reg_arp_rst = YES;
        end
    end
    ...
end
```

注意三件事：

- 移位量取自指令字 `[11:8]`，4 位，范围 0～15，所以 `{1'b0, if_opcodereg[11:8]}` 拼成 5 位的 `sha_amt`。
- `alu_modesel = ALU_ADD` 其实与默认值相同，写出来是为了可读性（注释里也强调了「从 port B 取数」）。
- 尾部那段 `if(if_opcodereg[7])` 就是 u2-l4 讲过的「间接寻址副作用译码片段」——只要指令带数据操作数，这段就几乎逐字复制一遍。

再看 `LAC`，只比 `ADD` 多一个 `alu_paz = YES`：

[src/IKA32010.sv:L860-L877](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L860-L877)（LAC）：

```verilog
//LAC - Load accumulator with shift
16'b0010_????_????_????: begin
    alu_modesel = ALU_ADD; alu_paz = YES; //block acc feedback
    alu_acc_ld = YES;
    sha_amt = {1'b0, if_opcodereg[11:8]};
    ...
end
```

`alu_paz=YES` 把 port A 清零，ACC 反馈被切断，加法变成 `0 + (RAM≪s)`。

接着看「高半字」与「无符号」两个变体，体会它们如何用同样的加法器+不同移位/扩展实现不同语义：

[src/IKA32010.sv:L804-L821](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L804-L821)（ADDH）——把 RAM 左移 16 位再加，相当于加到 ACC 的高 16 位：

```verilog
//ADDH - Add to high-order accumulator bits
16'b0110_0000_????_????: begin
    alu_modesel = ALU_ADD; alu_pbdata = ALU_PBDATA_LONGWORD; //load from port B, low bits masked
    alu_acc_ld = YES;
    sha_amt = 5'd16;
    ...
end
```

[src/IKA32010.sv:L823-L840](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L823-L840)（ADDS）——用 `sha_ssup=YES` 抑制符号扩展，实现「加无符号 16 位数」：

```verilog
//ADDS - Add to accumulator with no sign extension
16'b0110_0001_????_????: begin
    alu_modesel = ALU_ADD; alu_pbdata = ALU_PBDATA_LONGWORD;
    alu_acc_ld = YES;
    sha_ssup = YES;
    ...
end
```

减法家族 `SUB/SUBH/SUBS` 与加法家族完全对称，只是把 `ALU_ADD` 换成 `ALU_SUB`。ALU 内部对 `ALU_SUB` 的处理是「port B 取反 + cin=1」（补码加法），见 [src/IKA32010.sv:L1805-L1825](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1805-L1825)：

```verilog
if(i_ALU_MODESEL == ALU_SUB || i_ALU_MODESEL == ALU_SUBC) adder_cin = 1'b1; //make 2's complement
...
if(i_ALU_MODESEL == ALU_SUB || i_ALU_MODESEL == ALU_SUBC) port_b = ~i_ALU_PB;
```

最后看「清零并装载」组合 `ZALH / ZALS`——它们用 `alu_paz=YES` 屏蔽 ACC，再用 `alu_pbdata` 选 port B 的高半字或低半字，实现「清 ACC 的同时把 RAM 装到指定半字」：

[src/IKA32010.sv:L1049-L1066](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1049-L1066)（ZALH）：`alu_paz=YES`、`alu_pbdata=ALU_PBDATA_HIGHWORD`、`sha_amt=16`、`alu_acc_ld=YES`——结果为 `{RAM, 16'h0000}`，即「清低半、装高半」。

[src/IKA32010.sv:L1068-L1084](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1068-L1084)（ZALS）：`alu_paz=YES`、`alu_pbdata=ALU_PBDATA_LOWWORD`、`alu_acc_ld=YES`——结果为 `{16'h0000, RAM}`，即「清高半、装低半」。

> `alu_pbdata` 如何在 ALU 内部切片 port B，见 [src/IKA32010.sv:L1827-L1832](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1827-L1832)：`LONGWORD` 全保留、`HIGHWORD` 低 16 位清零、`LOWWORD` 高 16 位清零、`BYTE` 只留低 8 位。

#### 4.2.4 代码实践

**实践目标**：把 `ADD dma, 0`、`LAC dma, 0`、`SUB dma, 0` 三条指令「翻译」成同一张控制信号表，亲眼看「加/装/减」三者只差一两个信号。

**操作步骤**：

1. 在 [src/IKA32010.sv:L785-L802](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L785-L802)（ADD）、[src/IKA32010.sv:L860-L877](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L860-L877)（LAC）、[src/IKA32010.sv:L946-L963](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L946-L963)（SUB）里分别读出三条指令对 `alu_modesel`、`alu_paz`、`sha_amt`、`alu_acc_ld` 的设置（其余信号记为「默认」）。
2. 画一张 3 行 × 4 列的对照表。
3. 在注释里写出每条指令的 C 语言语义（设 `ACC` 为 32 位有符号、`RAM` 为 16 位）。

**需要观察的现象**：三行里 `alu_acc_ld` 全是 `YES`，`sha_amt` 全是 `{1'b0, if_opcodereg[11:8]}`；只有 `alu_modesel`（ADD vs SUB）和 `alu_paz`（LAC=YES，其余 NO）不同。

**预期结果**：
- `ADD`：`ACC = ACC + sign_ext(RAM) << s`
- `LAC`：`ACC = sign_ext(RAM) << s`
- `SUB`：`ACC = ACC - sign_ext(RAM) << s`

> 待本地验证：若你手头有仿真环境，可在 testbench 里把上述三条指令各放一条、给 RAM 单元写入 `0xFFFF`，观察 `reg_ovm=0` 时 `ADD`（符号扩展为 −1）与 `ADDS`（零扩展为 65535）得到截然不同的 ACC。

#### 4.2.5 小练习与答案

**练习 1**：`ADDH` 用 `sha_amt=5'd16` 实现「加到高半字」。如果不用移位器，而是改用 `alu_pbdata=ALU_PBDATA_HIGHWORD`（像 `ZALH` 那样），能达成同样效果吗？为什么源码选择了移位而不是切片？

**参考答案**：不能等价。`ADDH` 要做的是 `ACC + (RAM ≪ 16)`，是**加法**，必须保留 ACC 原值（`alu_paz=NO`），所以 port A 是 ACC 反馈、port B 必须是被左移到高位的 RAM。若改用 `alu_pbdata=HIGHWORD`，port B 的高 16 位会是 RAM、低 16 位是 0，但要达成「RAM 在高半字」还需要 RAM 本身先被放到高 16 位——切片只是把 port B 的某些位置零，并不能把 16 位数据搬进高 16 位。所以源码用 `sha_amt=16` 让移位器 A 把 RAM 整体左移到高位，这是唯一的正确做法。

**练习 2**：`ZALH` 同时设了 `alu_paz=YES` 和 `sha_amt=16`。既然 `alu_paz` 已经把 port A 清零，为什么还要左移 16？

**参考答案**：`alu_paz` 清的是 port A（ACC 反馈），与 port B 的移位无关。`ZALH` 需要 port B = `RAM ≪ 16`（把 RAM 放进高 16 位、低 16 位为 0），这个左移发生在移位器 A 上，由 `sha_amt=16` 控制。两件事作用于不同端口，缺一不可：`alu_paz=YES` 保证 ACC 不参与（实现「清」），`sha_amt=16` 保证 RAM 进入高半字（实现「装高」）。

---

### 4.3 逻辑类指令 AND/OR/XOR 与 port B 低字掩码（含 LACK）

#### 4.3.1 概念说明

逻辑类指令 `AND / OR / XOR` 与算术类有一个关键差别：**它们只作用在 port B 的低 16 位**。这是因为 TMS32010 的逻辑运算定义在「16 位数据 ↔ 32 位累加器」之间——RAM 数据是 16 位的，与 32 位 ACC 做按位逻辑时，高 16 位的处理方式由 ALU 硬件固定：`{16'h0000, port_b[15:0]}`，即 port B 高 16 位恒为 0。

因此这三条指令不约而同地设 `alu_pbdata = ALU_PBDATA_LOWWORD`，把 port B 截成 `{16'h0000, RAM[15:0]}`。这条「低字掩码」是逻辑类的统一签名。

立即数加载 `LACK` 在结构上更像逻辑类的特例：它把 port B 截成低 8 位（`ALU_PBDATA_BYTE`），并把数据源从 RAM 切换成立即数（`WRBUS_SOURCE_IMM`）。

#### 4.3.2 核心流程

以 `AND dma` 为例：

1. `register_wrbus_source_sel = WRBUS_SOURCE_RAM`（默认）→ `reg_wrbus = RAM`。
2. 移位器 A：`sha_amt=0`（默认）、`sha_ssup=NO`（默认）→ `sha_output = 符号扩展的 RAM`。但逻辑运算只取低 16 位，所以高 16 位的符号扩展值会被 ALU 丢弃。
3. `alu_pbdata = ALU_PBDATA_LOWWORD` → port B = `{16'h0000, RAM[15:0]}`。
4. `alu_paz = NO`（默认）→ port A = ACC 反馈。
5. `alu_modesel = ALU_AND` → `alu_output = ACC & {16'h0000, RAM}`。

效果是：ACC 的高 16 位与 0 相「与」被清零，低 16 位与 RAM 相「与」。这正是 TMS32010 手册定义的行为。

`LACK` 的流程多一步——切换数据源：

1. `register_wrbus_source_sel = WRBUS_SOURCE_IMM` → `reg_wrbus = {8'h00, if_opcodereg[7:0]}`（见 [src/IKA32010.sv:L139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139)），即低 8 位是立即数、高 8 位是 0。
2. `alu_paz = YES` → port A = 0。
3. `alu_pbdata = ALU_PBDATA_BYTE` → port B = `{24'h0, imm[7:0]}`。
4. `alu_modesel = ALU_ADD` → `ACC = 0 + imm`，立即数被零扩展后装入 ACC。

#### 4.3.3 源码精读

[src/IKA32010.sv:L842-L858](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L842-L858)（AND）：

```verilog
//AND - AND with accumulator
16'b0111_1001_????_????: begin
    alu_modesel = ALU_AND; alu_pbdata = ALU_PBDATA_LOWWORD; //load from port B
    alu_acc_ld = YES;
    ...
end
```

`OR`（[src/IKA32010.sv:L890-L906](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L890-L906)）、`XOR`（[src/IKA32010.sv:L1022-L1037](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1022-L1037)）结构完全相同，只把 `ALU_AND` 换成 `ALU_OR` / `ALU_XOR`。

ALU 内部对三种逻辑运算的处理见 [src/IKA32010.sv:L1853-L1855](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1853-L1855)，可以清楚看到 port B 被强制取低 16 位、高 16 位补 0：

```verilog
ALU_AND : alu_output = port_a & {16'h0000, port_b[15:0]};
ALU_OR  : alu_output = port_a | {16'h0000, port_b[15:0]};
ALU_XOR : alu_output = port_a ^ {16'h0000, port_b[15:0]};
```

> 注意：虽然指令里写了 `alu_pbdata = ALU_PBDATA_LOWWORD`，但 ALU 的逻辑分支**又显式地**只取 `port_b[15:0]`。两者一致地保证了「只作用低 16 位」。这是硬件上的双重保险。

再看 `LACK`：

[src/IKA32010.sv:L879-L888](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L879-L888)（LACK）：

```verilog
//LACK - Load accumulator immediate
16'b0111_1110_????_????: begin
    alu_modesel = ALU_ADD; alu_paz = YES; alu_pbdata = ALU_PBDATA_BYTE; //block acc feedback
    alu_acc_ld = YES;
    register_wrbus_source_sel = WRBUS_SOURCE_IMM;
    ...
end
```

这里把 `ALU_ADD` 当成「装载」来用（配合 `alu_paz=YES`），并用 `ALU_PBDATA_BYTE` 把 port B 截成 8 位立即数。`WRBUS_SOURCE_IMM` 让 `reg_wrbus` 取指令字低 8 位（[src/IKA32010.sv:L139](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L139)），从而完成「零扩展 8 位立即数装入 ACC」。

> 待确认：`LACK` 的立即数是否为「有符号」？从源码看，`WRBUS_SOURCE_IMM` 把指令字 `[7:0]` 放进 16 位总线的低 8 位、高 8 位补 0，移位器 A 的 `sha_ssup` 保持默认 NO 会做符号扩展，但 `[15]` 位是 0（因为高 8 位是 0），符号扩展等价于零扩展；再经 `ALU_PBDATA_BYTE` 截取低 8 位、高 24 位补 0。因此 `LACK` 实际是**零扩展**装载 8 位立即数，范围 0～255。请对照 TMS32010 手册确认这一点。

#### 4.3.4 代码实践

**实践目标**：亲手验证「逻辑指令清掉 ACC 高 16 位」这一行为，并理解它对程序的影响。

**操作步骤**：

1. 阅读 [src/IKA32010.sv:L1853-L1855](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1853-L1855)，确认 `AND/OR/XOR` 都把 port B 的高 16 位填 0。
2. 假设当前 `ACC = 0x1234_5678`，RAM 单元 = `0xF0F0`。手动计算执行 `AND`、`OR`、`XOR` 后的 ACC 值。
3. 思考：如果你需要「只改 ACC 低 16 位、保留高 16 位」，能不能直接用 `OR`？

**需要观察的现象**：三条指令的结果高 16 位都是 0（因为与 0 运算）——也就是说，TMS32010 的逻辑指令**会破坏 ACC 的高 16 位**。

**预期结果**：
- `AND`：`0x1234_5678 & 0x0000_F0F0 = 0x0000_5070`
- `OR`：`0x1234_5678 | 0x0000_F0F0 = 0x0000_F6F8`
- `XOR`：`0x1234_5678 ^ 0x0000_F0F0 = 0x0000_A688`

三者的 ACC 高 16 位都被清零。因此「只改低 16 位、保留高 16 位」**不能**用单条 `OR` 实现，必须配合 `SACH`/`LAC` 等指令保存/恢复高半字。

> 待本地验证：可在仿真里把 ACC 预置为 `0x12345678`，对某 RAM 单元写 `0xF0F0` 后执行 `AND`，检查 ACC 是否变为 `0x00005070`。

#### 4.3.5 小练习与答案

**练习 1**：`LACK` 用 `ALU_PBDATA_BYTE` 截取低 8 位，但 `WRBUS_SOURCE_IMM` 已经把立即数放在 `reg_wrbus` 的低 8 位了。既然如此，为什么不直接用 `ALU_PBDATA_LONGWORD`（默认值）省掉这一句？

**参考答案**：若用默认的 `ALU_PBDATA_LONGWORD`，port B 会保留 `reg_wrbus` 的全部 16 位。虽然 `WRBUS_SOURCE_IMM` 的高 8 位是 0，经移位器 A 符号扩展后仍是 0，看上去结果一样；但 `ALU_PBDATA_BYTE` 是一道**显式的语义声明**——它告诉读者「这条指令的立即数只有 8 位」。更重要的是，它是一道硬件保险：即便将来有人修改 `WRBUS_SOURCE_IMM` 的拼法（比如让高 8 位带上别的位），`ALU_PBDATA_BYTE` 也能保证只有低 8 位进入运算。所以这一句既是文档也是防护。

**练习 2**：要把 ACC 的低 16 位清零、高 16 位保留，用本讲学过的指令组合怎么实现？

**参考答案**：TMS32010 没有直接的「按位逻辑只影响低字」指令（逻辑指令都会清高 16 位）。一种可行组合是：先用 `SACH dma` 把 ACC 高 16 位存到 RAM，再用 `ZALS dma2` 把一个 0 单元装到 ACC 低半字（此时高半字也被清零），最后再用 `ZALH dma` 把刚才存的高半字装回。更实际的做法是程序层面避免这种需求，或借助 `LAC` 重新装载。这个练习说明：逻辑指令「清高 16 位」的特性是一种必须时刻记住的副作用。

---

### 4.4 存储类指令 SACH/SACL：移位器 B 与 RAM 写控制

#### 4.4.1 概念说明

`SACH`（Store Accumulator High）和 `SACL`（Store Accumulator Low）的方向与前面所有指令相反：它们**不修改 ACC，而是把 ACC 的内容写进 RAM**。因此这两条指令的译码签名也与算术/逻辑指令截然不同：

- `alu_acc_ld` 保持默认 `NO`——ACC 不被写回，保持原值；
- `register_wrbus_source_sel = WRBUS_SOURCE_SHB`——把「写总线」的数据源切到移位器 B（累加器输出侧移位器）；
- `ram_wr = YES`——允许 RAM 写口把 `reg_wrbus` 写入 `ram_addr`；
- 用 `shb_amt` / `shb_mux` 控制移位器 B「左移多少、取高位还是低位」。

这一组的本质是：「ACC → 移位器 B → reg_wrbus → RAM」这条反向通路。

#### 4.4.2 核心流程

移位器 B 的硬件实现见 [src/IKA32010.sv:L458-L471](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L458-L471)：

```verilog
assign shb_output = shb_mux ? shb_intermediate[31:16] : shb_intermediate[15:0];
always @(*) begin
    case(shb_amt)
        3'd0:    shb_intermediate = alu_acc_output;
        3'd1:    shb_intermediate = alu_acc_output << 1;
        3'd4:    shb_intermediate = alu_acc_output << 4;
        default: shb_intermediate = alu_acc_output;
    endcase
end
```

两个控制旋钮：

- `shb_amt`：只实现了 0、1、4 三档左移（其余取值视为不移位）。这对应 TMS32010 手册里 SACH 允许的 `≪0 / ≪1 / ≪4` 三种移位。
- `shb_mux`：`HIGH`（1）取 32 位结果的 `[31:16]`（高半字），`LOW`（0）取 `[15:0]`（低半字）。

于是：

- `SACL`：`shb_mux=LOW`、`shb_amt=0`（默认）→ `shb_output = ACC[15:0]`，写 16 位低字进 RAM。
- `SACH n`：`shb_mux=HIGH`、`shb_amt = 指令字[10:8]`（取值 0/1/4 有效）→ `shb_output = (ACC ≪ n)[31:16]`，写移位后的高半字进 RAM。

数据最终经 `reg_wrbus` 落入 RAM，写地址 `ram_addr` 同样由指令字 bit7 决定直接/间接寻址（见 [src/IKA32010.sv:L488](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L488)）。

#### 4.4.3 源码精读

[src/IKA32010.sv:L908-L925](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L908-L925)（SACH）：

```verilog
//SACH - Store high-order accumulator bits with shift
16'b0101_1???_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_SHB;
    ram_wr = YES;
    shb_amt = if_opcodereg[10:8]; shb_mux = HIGH;
    ...
end
```

[src/IKA32010.sv:L927-L944](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L927-L944)（SACL）：

```verilog
//SACL - Store low-order accumulator bits
16'b0101_0???_????_????: begin
    register_wrbus_source_sel = WRBUS_SOURCE_SHB;
    ram_wr = YES;
    shb_mux = LOW;
    ...
end
```

注意 `SACL` 没有写 `shb_amt`，所以它继承默认值 `3'd0`（不移位），只取低半字。

把这两条与 `WRBUS_SOURCE_SHB` 在 MUX 里的分支对照看（[src/IKA32010.sv:L135](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L135)）：`WRBUS_SOURCE_SHB : reg_wrbus = shb_output;`——移位器 B 的 16 位输出直接送上写总线，再被 RAM 写口采走（`i_DIN(reg_wrbus)`，见 [src/IKA32010.sv:L493](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L493)）。这就是「ACC → RAM」的完整闭环。

#### 4.4.4 代码实践

**实践目标**：手动模拟 `SACH` 在不同 `shb_amt` 下的输出，理解为什么只实现了 0/1/4 三档。

**操作步骤**：

1. 设 `ACC = 0x1234_5678`。
2. 分别对 `shb_amt = 0`、`1`、`4` 计算 `shb_intermediate`，再取 `[31:16]` 得到 `shb_output`。
3. 假设 `shb_amt = 2`（未实现档位），根据 [src/IKA32010.sv:L465-L470](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L465-L470) 的 `default` 分支推断结果。

**需要观察的现象**：`shb_amt=2` 会落入 `default`，结果与 `shb_amt=0` 相同（不移位）。这意味着硬件**不会报错**，而是悄悄退化成不移位。

**预期结果**（`SACH`，取 `[31:16]`）：
- `shb_amt=0`：`shb_intermediate = 0x12345678` → `shb_output = 0x1234`
- `shb_amt=1`：`shb_intermediate = 0x2468ACF0` → `shb_output = 0x2468`
- `shb_amt=4`：`shb_intermediate = 0x04567800`（`0x12345678 ≪ 4`，32 位溢出）→ `shb_output = 0x0456`（**待本地验证**溢出位宽与具体值）
- `shb_amt=2`：落入 default → `shb_output = 0x1234`（同 `shb_amt=0`）

> 待本地验证：`shb_amt=4` 时 `0x12345678 ≪ 4` 的精确 32 位结果。注意 Verilog 的 `<<` 是逻辑左移，超出 32 位的位会丢弃。

#### 4.4.5 小练习与答案

**练习 1**：`SACH` 和 `SACL` 都没有设 `alu_acc_ld=YES`。如果误把它们改成 `alu_acc_ld=YES`，会发生什么？

**参考答案**：`alu_acc_ld=YES` 会让 ACC 在 `cyc_ncen` 拍被 `alu_output` 覆盖。此时 `alu_modesel` 是默认的 `ALU_ADD`、port A 是 ACC 反馈、port B 是 RAM（默认源）经移位器 A（不移位）——于是 ACC 会被改成 `ACC + RAM`。这违背了「存储指令不应改 ACC」的语义，是个典型 bug。源码正确地保留了 `alu_acc_ld=NO`（默认）。

**练习 2**：为什么 `SACH` 用移位器 B（输出侧），而 `LAC` 用移位器 A（输入侧）？两者的移位方向在数学上是否一致？

**参考答案**：移位器 A 服务于「数据进 ALU 之前」的定标——16 位 RAM 数据先符号扩展到 32 位再左移，是**输入**定标；移位器 B 服务于「ACC 出去到 RAM 之前」的定标——32 位 ACC 先左移再截取 16 位，是**输出**定标。`LAC` 要把 16 位数据装进 32 位 ACC 并左移，必须用输入侧；`SACH` 要把 32 位 ACC 左移后取高 16 位存出去，必须用输出侧。两者都是「左移」，数学方向一致，但作用对象（输入 vs 输出）和位宽（16→32 vs 32→16）不同。

---

### 4.5 SUBC：内嵌两周期的条件减法（除法）

#### 4.5.1 概念说明

`SUBC` 是这一类里最特殊的一条。它用于实现**除法**：重复执行 `SUBC` 16 次即可完成一次 32 位 ÷ 16 位的恢复式除法。它的特殊性体现在两点：

1. 它是「条件减法」——根据上一步结果是否非负，决定是「减+左移」还是「只左移」，从而恢复余数。
2. 它在微码层是**单周期**指令（`ex_inst_cycle_rst` 保持默认 `YES`，序列器不延长），但 ALU 内部用 `prev_subc` 寄存器把运算**自动延伸到下一个机器周期**才写回 ACC。正因如此，源码用注释明确警告：**下一条指令不能访问 ACC**。

> 注意：这里的「两周期」与 u3-l2 讲的 `TBLR/IN` 用 `ex_inst_cycle` 拆相位不同——`SUBC` 不走序列器的多周期机制，而是 ALU 子模块**自带的**跨周期行为。这是本讲容易混淆的一点。

#### 4.5.2 核心流程

`SUBC` 的除法原理（恢复式除法的一位迭代）：

设被除数在 ACC（32 位）、除数在 RAM（16 位），先左移 15 位对齐。每一次 `SUBC` 执行：

1. 用除数试减：`ACC − (RAM ≪ 15)`。
2. 若结果非负（`alu_output[31]==0`），说明「够减」，下一位商为 1——余数保留为 `结果 ≪ 1 | 1`。
3. 若结果为负（`alu_output[31]==1`），说明「不够减」，下一位商为 0——恢复余数为 `原ACC ≪ 1`。

「够减则保留并补 1、不够减则恢复并补 0」正是恢复式除法的位迭代。重复 16 次后，ACC 的低 16 位是商、高 16 位是余数。

#### 4.5.3 源码精读

微码侧，`SUBC` 分支非常简洁——只设 `alu_modesel` 和 `sha_amt`，**不设 `alu_acc_ld`**：

[src/IKA32010.sv:L965-L982](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L965-L982)（SUBC）：

```verilog
//SUBC - Conditional subtract (for divide)
16'b0110_0100_????_????: begin
    //!!! next instruction cannot use the ACC !!!
    alu_modesel = ALU_SUBC;
    sha_amt = 5'd15;
    //ACC will be loaded next cycle
    ...
end
```

关键在于 ALU 子模块内部的跨周期逻辑。先看 ACC 写回控制：

[src/IKA32010.sv:L1874-L1881](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1874-L1881)：

```verilog
//accumulator control
wire alu_acc_ld = prev_subc | i_ALU_ACC_LD;
always @(posedge i_EMUCLK) begin
    if(!i_RST_n) o_ALU_ACC_OUTPUT <= 32'h0000_0000;
    else begin
        if(i_CEN) if(alu_acc_ld) o_ALU_ACC_OUTPUT <= alu_output;
    end
end
```

注意 `alu_acc_ld` 被局部 `wire` 重定义为 `prev_subc | i_ALU_ACC_LD`——也就是说，**即使微码没有设 `i_ALU_ACC_LD`，只要上一拍是 SUBC（`prev_subc=1`），ACC 也会被写回**。

再看 port A/B 与运算结果如何随 `prev_subc` 切换：

[src/IKA32010.sv:L1800-L1836](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1800-L1836)（节选）：

```verilog
//port A
if(prev_subc) port_a = subc_divided ? 32'h0000_0000 : i_ALU_PA << 1;
...
//port B
if(prev_subc) port_b = subc_divided ? prev_adder << 1 : 32'h0000_0000;
...
//ALU operation select
if(prev_subc) alu_output = port_a + port_b + adder_cin;
```

以及 `subc_divided` / `prev_subc` 的更新：

[src/IKA32010.sv:L1866-L1872](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1866-L1872)：

```verilog
always @(posedge i_EMUCLK) if(i_CEN) begin
    if(i_ALU_MODESEL == ALU_SUBC && alu_output[31] == 1'b0) subc_divided <= 1'b1;
    else subc_divided <= 1'b0;
    prev_subc <= i_ALU_MODESEL == ALU_SUBC;
    prev_adder <= alu_output;
end
```

把这段逻辑翻译成两个周期：

- **周期 0（SUBC 拍）**：`prev_subc=0`，走正常 `ALU_SUBC` 分支，port A=ACC、port B=`~(RAM≪15)`、cin=1，做试减 `ACC − (RAM≪15)`。结果存进 `prev_adder`，`alu_output[31]` 决定 `subc_divided`。**ACC 不写回**（因为 `i_ALU_ACC_LD=NO` 且 `prev_subc=0`）。
- **周期 1（下一指令的第一拍）**：`prev_subc=1`，port A/port B 改由 `subc_divided` 分支决定，`alu_output = port_a + port_b + cin`，并且 `alu_acc_ld=1` → **此时才把最终结果写回 ACC**。

正因为「真正写回 ACC」发生在**下一条指令的周期**，所以下一条指令如果读 ACC，读到的是旧值——这就是注释警告的根因。

#### 4.5.4 代码实践

**实践目标**：通过阅读源码，复现 `SUBC` 两个周期里 `port_a`/`port_b`/`alu_output`/`subc_divided` 的取值，理解恢复式除法的一位迭代。

**操作步骤**：

1. 设一个简单情形：`ACC = 0x0001_0000`（即 65536），`RAM = 0x0002`（即 2）。注意真实除法要把除数左移 15 位对齐，这里只是定性跟踪。
2. 在 [src/IKA32010.sv:L1800-L1872](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1800-L1872) 里逐行推导周期 0 的 `alu_output` 与 `alu_output[31]`，判断 `subc_divided` 的值。
3. 推导周期 1 的 `port_a`/`port_b`，得出最终写回 ACC 的值。
4. 解释为什么「下一条指令不能访问 ACC」。

**需要观察的现象**：周期 0 ACC 不变，周期 1 ACC 才被改写。如果下一条指令（如 `SACL`）在周期 1 的同一拍读 ACC 旧值，就会得到错误结果。

**预期结果**：周期 0 的试减结果决定 `subc_divided`；周期 1 据 `subc_divided` 选择「左移+补商位」或「仅左移恢复」。最终 ACC 写回发生在周期 1。**具体数值待本地验证**——建议在仿真里给 `SUBC` 单步、用波形观察 `subc_divided`、`prev_subc`、`prev_adder`、`o_ALU_ACC_OUTPUT` 四个信号。

> 待本地验证：完整的 16 次 `SUBC` 迭代除法。手册要求 `OVM=0`（否则饱和逻辑会干扰），且 `SUBC` 后必须跟一条不访问 ACC 的指令（如 `NOP`）。

#### 4.5.5 小练习与答案

**练习 1**：`SUBC` 在微码里是单周期（序列器不延长），但实际占用了两个周期的 ACC。序列器为什么不直接用 `ex_inst_cycle` 把它拆成两相位（像 `IN` 那样）？

**参考答案**：这是作者的设计取舍。`SUBC` 的第二周期里，指令寄存器已经装载了下一条真实指令，序列器也在译码它——如果用 `ex_inst_cycle` 强行把 SUBC 占住两拍，就会延迟下一条指令的取指与执行，破坏「每周期一条」的流水节奏。作者选择让 ALU 子模块「悄悄」在下一拍完成写回，代价是下一条指令不能访问 ACC。这是一种「用约束换吞吐」的折中：除法本就慢且罕见，让程序员承担「SUBC 后插 NOP」的义务，比拖慢整条流水更划算。

**练习 2**：`subc_divided` 在周期 0 根据 `alu_output[31]==0` 置位。这个「结果非负」的判断，对应恢复式除法里的哪一步决策？

**参考答案**：对应「够减 vs 不够减」的判断。`alu_output[31]==0` 表示试减结果非负，即「够减」——此时下一位商为 1，余数保留为试减结果（在周期 1 由 `subc_divided=1` 分支：`port_b = prev_adder ≪ 1`、`port_a = 0`，并因 `subc_divided` 配合加法补上商位 1）。`alu_output[31]==1` 表示「不够减」，下一位商为 0，余数恢复为原 ACC 左移（由 `subc_divided=0` 分支：`port_a = i_ALU_PA ≪ 1`、`port_b = 0`）。这正是恢复式除法「够减保留、不够减恢复」的核心。

---

## 5. 综合实践

把本讲的三类指令串起来，完成一个完整的「RAM → ACC → RAM」往返任务。

**任务背景**：假设 RAM 的某个数据页里，`RAM[0x00]` 存着一个 16 位定点数 `x`。我们希望计算 `y = (x ≪ 2) + 0x0005`，并把 `y` 的低 16 位存回 `RAM[0x01]`，高 16 位（应为 0）存回 `RAM[0x02]` 作为校验。

**要求**：

1. 用本讲学过的指令（`LAC / ADD / LACK / SACL / SACH`）写出对应的指令序列（汇编层面，不必写真二进制）。
2. 为序列里**每一条**指令填写一张控制信号表，包含：`alu_modesel`、`alu_paz`、`alu_pbdata`、`alu_pbsel`、`sha_amt`、`sha_ssup`、`alu_acc_ld`、`register_wrbus_source_sel`、`shb_amt`、`shb_mux`、`ram_wr`（标注「默认」或具体值）。
3. 用一张数据流图说明：哪条指令让数据从 RAM 流进 ACC？哪条让数据从 ACC 流回 RAM？
4. 指出这个序列里有没有「逻辑指令清掉 ACC 高 16 位」的隐患；如果有，如何规避。

**参考思路**（请先自己写再对照）：

- `LAC 0x00, 2`：ACC ← `x ≪ 2`（`alu_paz=YES`、`sha_amt=2`、`alu_acc_ld=YES`、源 RAM）。
- `LACK 5`：注意 `LACK` 会清掉 ACC 高位吗？不会——它用 `ALU_ADD`+`alu_paz=YES`，把 ACC 反馈屏蔽后装载立即数，**会覆盖整个 ACC**。所以这里要先加立即数再装载，或调整顺序。更稳妥的写法是先 `LACK 5` 再 `ADD 0x00, 2`。
- `ADD 0x00, 2`：ACC ← ACC + `x ≪ 2`（`alu_paz=NO`、`sha_amt=2`、`alu_acc_ld=YES`）。
- `SACL 0x01`：RAM[0x01] ← ACC[15:0]（`wrbus=SHB`、`shb_mux=LOW`、`ram_wr=YES`）。
- `SACH 0x02, 0`：RAM[0x02] ← ACC[31:16]（`wrbus=SHB`、`shb_mux=HIGH`、`shb_amt=0`、`ram_wr=YES`）。

> 这条综合实践帮你把「算术装载（移位器 A）→ 算术加（port A 反馈）→ 存储低字（移位器 B LOW）→ 存储高字（移位器 B HIGH）」四件事串成一条完整的数据回路，正好覆盖本讲全部三个最小模块。

## 6. 本讲小结

- 累加器类指令共 18 条，全部位于 `casez` 的 `ACCUMULATOR INSTRUCTIONS` 段（[src/IKA32010.sv:L771-L1084](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L771-L1084)），共享「默认值 + 少量覆盖」的译码风格。
- 算术类（`ADD/SUB/LAC/...`）通过 `alu_modesel`（ADD/SUB）、`alu_paz`（屏蔽 ACC 反馈实现「装载」）、`sha_amt`（输入侧左移 0~15）、`sha_ssup`（无符号扩展）这四个旋钮排列组合出全部语义。
- 逻辑类（`AND/OR/XOR`）统一设 `alu_pbdata=ALU_PBDATA_LOWWORD`，ALU 硬件又显式只取 port B 低 16 位——因此逻辑指令**会清掉 ACC 高 16 位**，这是必须记住的副作用。
- 存储类（`SACH/SACL`）走反向通路 `ACC → 移位器 B → reg_wrbus → RAM`，标志是 `alu_acc_ld` 保持 `NO`、`register_wrbus_source_sel=WRBUS_SOURCE_SHB`、`ram_wr=YES`；移位器 B 只实现 0/1/4 三档左移。
- `LACK` 用 `WRBUS_SOURCE_IMM` + `ALU_PBDATA_BYTE` + `alu_paz=YES` 把 8 位立即数零扩展装进 ACC。
- `SUBC` 是「微码单周期、ALU 内部两周期」的特殊指令，靠 `prev_subc`/`subc_divided`/`prev_adder` 在下一拍才写回 ACC，实现恢复式除法的一位迭代，因此要求「下一条指令不能访问 ACC」。

## 7. 下一步学习建议

本讲把累加器类指令的译码讲透了，接下来建议：

- **u3-l6 分支与子程序类指令**：分支类指令（`B/BANZ/BGEZ/...`）会用到本讲建立的标志位（Z/N/V）概念，并引入两相位译码与栈操作，是自然的下一站。
- **u3-l7 乘法器与 I/O/数据存储类指令**：那里会讲 `APAC/PAC/SPAC` 如何把 P 寄存器经 ALU 端口 B（`alu_pbsel=ALU_SOURCE_MUL`）加到 ACC——本讲出现的 `alu_pbsel` 信号在那里才真正发挥作用。
- **回看 u2-l7**：如果你对 `SUBC` 的除法数学、OVM 饱和值、加法器拆分求溢出仍有疑惑，建议重读 u2-l7 的 ALU 子模块精读部分，那里有完整的数学推导。
- **动手验证**：本讲多处标注「待本地验证」，建议用 u1-l5 学到的 testbench 方法，写一段最小程序（`LAC`+`ADD`+`SACL`）在仿真里观察 ACC 与 RAM 波形，把本讲的「控制信号表」逐一印证成真实波形。
