# 外部总线控制器 Bus Controller

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 IKA32010 的总线控制器（Bus Controller）由哪两部分组成，以及它为什么必须存在。
- 解释 `busctrl_req`（请求类型）与 `busctrl_mode`（模式寄存器）的关系，列出全部 6 种外部总线事务。
- 理解 `busctrl_mode[3]`（地址 MUX）如何让 `o_AOUT` 在「程序计数器 PC」与「外设口 PA」之间切换。
- 对每一种事务，画出 `cyclecntr` 在 0~3 四个相位上 `o_MEN_n`/`o_DEN_n`/`o_WE_n`/`o_DOUT_OE` 的电平时序表。
- 看懂读数据在相位 3 被锁存进 `if_opcodereg`（取指）或 `busctrl_inlatch`（表读/IN）的机制。

本讲只讲「总线控制器本身如何把一个模式翻译成 4 拍外部时序」，**不讲**微码针对每条指令如何选择模式——那是 u3 系列指令译码讲义的内容。本讲会在末尾点出二者的衔接关系。

## 2. 前置知识

本讲建立在你已经掌握以下概念之上（见 u1-l3、u1-l4、u2-l1、u2-l2）：

- **机器周期与四相位**：2 位计数器 `cyclecntr` 在 `0→1→2→3` 循环，4 个 `i_EMUCLK` 构成 1 个 DSP 机器周期；`cyclecntr` 的每个取值称为一个「相位」。
- **`cyc_ncen` 与 `cyc_pcen`**：`cyc_ncen = (cyclecntr==3) & i_CLKIN_PCEN` 是主力工作拍，几乎所有状态更新都发生在它的边沿。
- **端口命名约定**：`o_` 表示输出、`i_` 表示输入；`_n` 后缀表示低电平有效。
- **无片内 ROM**：IKA32010 不含程序 ROM，所有指令与外设数据都挂在一条外部总线上，必须由一组选通信号区分谁在占用总线。
- **`reg_wrbus` 内部写总线**：芯片内部数据搬运的主干道（见 u2-l1）。

补充两个本讲要用到的基础术语：

- **选通信号（strobe）**：一段在有效电平上持续若干相位的脉冲，外设/存储器靠它判断「现在主人要跟我说话」。
- **三态 / 输出使能（OE）**：FPGA 的双向引脚需要 `oe` 信号决定此刻是驱动输出还是释放为高阻 `Z`，避免与外部驱动器冲突。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/IKA32010.sv` | 顶层模块，含总线控制器 | 端口声明、`busctrl_mode`/`busctrl_req`、地址 MUX、四相位时序 always 块 |
| `src/IKA32010_mnemonics.sv` | 常量字典 | `BUSCTRL_ADDR_*`（地址选择）、`BUSCTRL_STOP/OPCODE_READ/...`（事务类型）常量 |
| `src/IKA32010_tb.v` | testbench | 外部如何用 `MEN_n`/`DEN_n` 选通 ROM 与外设、复用 `RDBUS` |

总线控制器的代码集中在 `src/IKA32010.sv` 中标注 `//////  Bus controller` 的区段，逻辑非常工整，本讲会逐段拆开。

## 4. 核心概念与源码讲解

### 4.1 总线控制器的整体设计：请求 + 相位定序器

#### 4.1.1 概念说明

IKA32010 没有片内 ROM，指令和大部分数据都来自外部。于是它对外暴露一组「总线控制信号」（`o_MEN_n`/`o_DEN_n`/`o_WE_n`/`o_AOUT`/`i_DIN`/`o_DOUT`/`o_DOUT_OE`），这些信号必须按 TMS32010 原始芯片的时序产生，否则外部 ROM 和外设无法正确配合。**总线控制器就是产生这套时序的硬件**。

理解总线控制器的关键是把它拆成两半：

1. **请求侧（request）**：微码在**每个机器周期**告诉总线控制器「下一个机器周期要做哪种事务」，这个请求是一个 3 位编码 `busctrl_req`，外加 1 位地址选择 `busctrl_addr_muxsel`。
2. **定序侧（sequencer）**：总线控制器拿到一个固定的模式后，**在机器周期的 4 个相位上**依次翻转 `o_MEN_n` 等输出，产生一次完整的外部总线访问时序。

换句话说：**微码只负责「点菜」（给模式），总线控制器负责「上菜」（把模式展开成 4 拍时序）**。这种分工让指令译码（u3）可以保持简洁——译码只需挑一个 `busctrl_req` 常量，而不必手写每个相位上的电平。

> 与 u2-l1 的 `reg_wrbus` 对比：`reg_wrbus` 是**内部**数据总线（片内部件之间），而本讲的信号是**外部**总线（芯片与 ROM/外设之间）。两者并行存在，由总线控制器在需要时把外部数据搬进内部（读）或把内部数据送到外部（写）。

#### 4.1.2 核心流程

一次外部总线访问的整体流程：

```text
微码 always @(*) ──每个机器周期设置──> busctrl_req (3位) + busctrl_addr_muxsel (1位)
                                              │
                                              ▼ (组合 MUX 拼装)
                              busctrl_mode = {addr_muxsel, busctrl_req} (4位)
                                              │
              ┌───────────────────────────────┴───────────────────────────────┐
              ▼ (组合)                                                         ▼ (时序 always 块)
      case(busctrl_mode[3]) 选择地址                        case(busctrl_mode[2:0]) × case(cyclecntr)
      → o_AOUT = PC 或 PA                                  → 在相位 0/1/2/3 翻转
                                                           o_MEN_n/o_DEN_n/o_WE_n/o_DOUT_OE
                                                           并在相位 3 锁存读入数据
```

要点：

- `busctrl_mode` 在**一个机器周期内保持不变**，总线控制器就在这 4 个相位里把该模式完整演完。
- 到下一个机器周期，微码可以给出新的 `busctrl_req`，于是总线控制器换个模式再演 4 拍。多周期指令（如 TBLR、IN）正是靠「每个周期换一个模式」来串起多步外部访问的（见 4.2.4 实践）。

#### 4.1.3 源码精读

总线控制器相关的对外端口声明在：

[src/IKA32010.sv:14-22](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L14-L22) — 声明 `o_MEN_n`（外部指令读）、`o_DEN_n`（IN/数据读）、`o_WE_n`（OUT/写）、`o_AOUT`（地址）、`i_DIN`（数据入）、`o_DOUT`（数据出）、`o_DOUT_OE`（输出使能）。注意这些都声明为 `reg`（除 `o_AOUT` 是 `wire`），说明它们的值由时序逻辑锁存。

`cyclecntr` 与派生的相位脉冲：

[src/IKA32010.sv:49-61](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L49-L61) — 这是 4.4 节时序的「节拍器」。`cyclecntr` 在 `0~3` 循环；`cyc_ncen` 只在相位 3 为真，`cyc_pcen` 只在相位 1 为真。

#### 4.1.4 代码实践

**实践目标**：建立「请求侧 + 定序侧」的二分直觉。

**操作步骤**：

1. 打开 `src/IKA32010.sv`，跳到 `//////  Bus controller` 注释横幅（约 148 行）。
2. 向下找到 `busctrl_req` 的声明（约 167 行），阅读其上方注释列出的 6 种事务编号。
3. 继续向下找到 `always @(posedge i_EMUCLK)`（约 176 行），确认它内部是用 `case(busctrl_mode[2:0])` 外套 `case(cyclecntr)` 的「模式 × 相位」二维结构。

**需要观察的现象**：请求侧只是少数几行组合赋值；定序侧是一个长长的、但极其规整的 case 嵌套，每种模式占一段。

**预期结果**：你能用一句话指出「请求侧负责挑模式，定序侧负责把模式展开成 4 拍」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `o_MEN_n`/`o_DEN_n`/`o_WE_n` 被声明为 `reg`，而 `o_AOUT` 被声明为 `wire`？
  - **答案**：前三个是时序输出（在 `always @(posedge i_EMUCLK)` 块里用 `<=` 赋值），必须是 `reg`；`o_AOUT` 由组合 `assign o_AOUT = busctrl_addr` 驱动（`busctrl_addr` 才是 `reg`），所以顶层端口是 `wire`。
- **练习 2**：如果总线控制器不存在、让微码直接驱动每个相位的电平，会带来什么问题？
  - **答案**：每条指令的译码都得手写 4 拍时序，代码会膨胀且容易出错；用「请求 + 定序」分层后，指令译码只需挑一个 3 位常量，时序细节被复用。

---

### 4.2 busctrl_req 与 busctrl_mode：六种事务请求

#### 4.2.1 概念说明

`busctrl_req` 是一个 3 位信号，编码「下一个机器周期要执行哪种外部总线事务」。源码注释和常量字典共同定义了 6 种取值：

| `busctrl_req` | 常量 | 含义 | 对外主要动作 |
| --- | --- | --- | --- |
| `3'd0` | `BUSCTRL_STOP` | 空闲，无事务 | 全部选通无效 |
| `3'd1` | `OPCODE_READ` | 取指令 | 拉低 `o_MEN_n`，读 ROM 到指令寄存器 |
| `3'd2` | `DATA_READ` | 表读 TBLR | 拉低 `o_MEN_n`，读 ROM 到 `busctrl_inlatch` |
| `3'd3` | `DATA_WRITE` | 表写 TBLW | 拉低 `o_WE_n`，把 RAM 数据写进 ROM 空间 |
| `3'd4` | `COMMAND_IN` | IN 输入 | 拉低 `o_DEN_n`，从外设读 |
| `3'd5` | `COMMAND_OUT` | OUT 输出 | 拉低 `o_WE_n`，写向外设 |

这些常量定义在：

[src/IKA32010_mnemonics.sv:24-30](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L24-L30) — 把抽象名字（`OPCODE_READ` 等）与 3 位编码绑定，让微码可读。

注意三类「读」的微妙区别：

- `OPCODE_READ` 与 `DATA_READ` 都用 `o_MEN_n`（memory enable），地址都来自 PC，都是从**程序 ROM** 读。区别只在于读到后送给谁：前者送指令寄存器，后者送 `busctrl_inlatch`。
- `COMMAND_IN` 用 `o_DEN_n`（data/port enable），地址来自 PA，从**外设端口**读。

两类「写」(`DATA_WRITE`/`COMMAND_OUT`) 的时序形状完全相同（都用 `o_WE_n` + `o_DOUT_OE`），区别在于：写入的数据来源（`ram_output` vs `reg_wrbus`）和地址来源（PC vs PA）。

#### 4.2.2 核心流程

请求到模式的拼装是纯组合逻辑：

```text
busctrl_mode[2:0] = busctrl_req;          // 复制请求类型
busctrl_mode[3]   = busctrl_addr_muxsel;  // 复制地址选择位
```

`busctrl_mode` 实际上是 `{addr_muxsel, req}`——一个 4 位打包：低 3 位是事务类型，第 4 位是地址 MUX 选择。下游（地址 MUX 与时序块）分别读 `busctrl_mode[3]` 和 `busctrl_mode[2:0]`。

为何要打包成 `busctrl_mode` 而不是让下游直接读 `busctrl_req`？因为 `busctrl_req` 与 `busctrl_addr_muxsel` 是组合信号，而时序 always 块在边沿采样它们；中间多一层 `busctrl_mode`（虽仍是组合）便于阅读，也让「模式」作为一个整体概念出现在波形里。

#### 4.2.3 源码精读

请求信号与模式寄存器的声明、组合拼装：

[src/IKA32010.sv:153-174](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L153-L174) — 注意 167-169 行的注释明确列出了 6 种事务编号，这就是本节那张表的原始出处。`busctrl_mode` 由两个 always 块协同：地址 MUX 块（下节讲）写 `busctrl_addr`，本段组合块写 `busctrl_mode`。

地址选择常量：

[src/IKA32010_mnemonics.sv:20-22](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_mnemonics.sv#L20-L22) — `BUSCTRL_ADDR_PC = 1'd0`（地址用 PC）、`BUSCTRL_ADDR_PERIPHERAL = 1'd1`（地址用 PA）。

微码的默认请求（每个机器周期未覆盖时生效）：

[src/IKA32010.sv:537-540](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L537-L540) — 微码块顶部把 `busctrl_req` 默认设为 `OPCODE_READ`、`busctrl_addr_muxsel` 默认设为 `BUSCTRL_ADDR_PC`。这意味着「绝大多数周期，总线控制器都在用 PC 地址从 ROM 取下一条指令」。只有 IN/OUT/TBLR/TBLW 等少数指令才会在某些周期覆盖它。复位态（`ex_state==0`）则强制为 `BUSCTRL_STOP`（见 601 行）。

#### 4.2.4 代码实践

**实践目标**：体会「多周期指令 = 每个周期换一个 `busctrl_req`」。

**操作步骤**：

1. 阅读 OUT 指令译码 [src/IKA32010.sv:1634-1661](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1634-L1661)。
2. 注意它在 `ex_inst_cycle==0` 时设 `busctrl_req = COMMAND_OUT`（向外设输出），在 `ex_inst_cycle==1` 时设 `busctrl_req = OPCODE_READ`（恢复取下一条指令）。
3. 再对比 IN 指令译码 [src/IKA32010.sv:1604-1632](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1604-L1632)，确认它第 0 周期是 `COMMAND_IN`、第 1 周期是 `OPCODE_READ`。

**需要观察的现象**：同一条指令在不同 `ex_inst_cycle` 上请求了不同的事务类型。

**预期结果**：你能解释「OUT 指令的第 1 个机器周期把数据写到外设，第 2 个机器周期恢复正常取指」——这就是多周期指令串起多步外部访问的方式。

#### 4.2.5 小练习与答案

- **练习 1**：`OPCODE_READ` 和 `DATA_READ` 都拉低 `o_MEN_n`、都用 PC 地址，外部 ROM 如何区分？
  - **答案**：外部 ROM **不区分**——它只是响应 `o_MEN_n` 把 `o_AOUT` 处的字送上 `i_DIN`。区别在芯片内部：`OPCODE_READ` 把 `i_DIN` 锁进 `if_opcodereg`（取指），`DATA_READ` 锁进 `busctrl_inlatch`（供后续写回 RAM）。是「同一个外部动作，两个内部消费者」。
- **练习 2**：为什么微码默认值是 `OPCODE_READ` 而不是 `BUSCTRL_STOP`？
  - **答案**：IKA32010 是流水取指——大多数周期都要预取下一条指令。默认 `OPCODE_READ` 让「什么都不特别指定」自动等于「继续取指」，指令译码只须在需要别的访问时显式覆盖即可。

---

### 4.3 地址多路选择：o_AOUT 在 PC 与 PA 之间切换

#### 4.3.1 概念说明

`o_AOUT` 是 12 位地址线，但它有「双重身份」：

- **取指 / 表读 / 表写时**：输出程序计数器 `if_pc`，用于寻址 4K 字（`0x000~0xFFF`）的程序 ROM。
- **IN / OUT 时**：输出外设端口地址 PA，用于选择挂在数据总线上的某个外设。

到底用哪一个，由 `busctrl_mode[3]`（即 `busctrl_addr_muxsel`）决定。这是一个典型的「地址多路选择器（Address MUX）」：同一组地址引脚，根据当前事务类型接到不同的地址源。

> 这也是 u1-l3 提到的「`o_AOUT` 双重身份」的具体实现位置。

#### 4.3.2 核心流程

地址 MUX 是纯组合逻辑，用一个 `case` 二选一：

```text
case (busctrl_mode[3])
    1'b0:  busctrl_addr = if_pc;                              // 寻址程序空间
    1'b1:  busctrl_addr = {9'b0, if_opcodereg[10:8]};         // 选择外设口 PA
endcase
o_AOUT = busctrl_addr;
```

关键点：

- 外设模式下，地址的高 9 位恒为 0，只有最低 3 位来自指令字的 `[10:8]`，因此外设地址空间只有 8 个口（源码注释写作 `PA0 1 2`，即示意 PA0、PA1、PA2 等若干端口）。
- 这 3 位端口编号直接取自**当前指令字** `if_opcodereg[10:8]`——也就是说，IN/OUT 指令在编码里就带好了端口编号，译码时无需额外查找。

#### 4.3.3 源码精读

地址 MUX 与地址输出：

[src/IKA32010.sv:155-164](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L155-L164) — 注意 161 行注释 `program counter + data offset`，说明 PC 模式下地址就是 `if_pc`（表读/表写时 `if_pc` 已被指令改造为指向目标表格地址，见 TBLR/TBLW 译码）。162 行 `{9'b0, if_opcodereg[10:8]}` 即外设口选择。

IN/OUT 译码中如何设置地址选择位：

[src/IKA32010.sv:1605-1607](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1605-L1607)（IN）与 [src/IKA32010.sv:1635-1637](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1635-L1637)（OUT）—— 这两条指令在第 0 周期都把 `busctrl_addr_muxsel` 设为 `BUSCTRL_ADDR_PERIPHERAL`，于是那一周期 `o_AOUT` 输出的是 PA 而非 PC。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 OUT 指令期间 `o_AOUT` 的变化。

**操作步骤**：

1. 假设执行 `OUT PA2, ...`（指令字 `[10:8]=3'd2`），`if_pc` 当前为某个值如 `0x123`。
2. 对照 OUT 译码（1635-1660 行）与地址 MUX（159-164 行）：
   - 第 0 周期：`busctrl_addr_muxsel = BUSCTRL_ADDR_PERIPHERAL` → `o_AOUT = {9'b0, 3'd2} = 0x002`。
   - 第 1 周期：`busctrl_addr_muxsel = BUSCTRL_ADDR_PC` → `o_AOUT = if_pc`（恢复为程序地址）。
3. 在 testbench 的 `o_AOUT`（即 `ADDR`）上加波形探针，对照上述推断。

**需要观察的现象**：同一根 `o_AOUT`，在 OUT 的两个机器周期里先显示 `0x002`（外设口），再显示程序地址。

**预期结果**：波形上能看到 `o_AOUT` 在两个周期之间在「外设口编号」与「PC」之间跳变，验证了地址 MUX 的切换。**待本地验证**：实际数值取决于你喂入的程序与 PC 推进。

#### 4.3.5 小练习与答案

- **练习 1**：外设地址为什么只用了 `o_AOUT` 的低 3 位？
  - **答案**：因为外设口编号直接来自指令字 `if_opcodereg[10:8]`（3 位，最多 8 个口），高 9 位补 0。TMS32010 的 IN/OUT 本就只支持少量端口，3 位足够。
- **练习 2**：TBLR 指令（表读）需要从「ACC 指向的 ROM 地址」读数据。它用的是 PC 模式还是 PA 模式？
  - **答案**：PC 模式（`BUSCTRL_ADDR_PC`）。TBLR 在执行前会把 `if_pc` 改写为 ACC 低 12 位（`PC_LOAD_WRBUS`，见 1668 行），于是「PC 模式 + 被改造过的 if_pc」恰好指向 ACC 想读的 ROM 地址。它并不走外设口。

---

### 4.4 四相位时序：六种事务的电平表与数据锁存

#### 4.4.1 概念说明

本节是总线控制器的「心脏」：给定一个模式，如何在 `cyclecntr` 的 0~3 四个相位上产生外部信号。这套时序对应原始 TMS32010 的外部总线协议——每个机器周期里，先给出地址与读/写方向，中段保持数据，末段回收选通。

三个选通信号的含义：

- `o_MEN_n`（Memory Enable，低有效）：拉低表示「我要读程序 ROM」。
- `o_DEN_n`（Data Enable，低有效）：拉低表示「我要从外设/数据口读」（IN）。
- `o_WE_n`（Write Enable，低有效）：拉低表示「我要写」（OUT / 表写）。
- `o_DOUT_OE`（高有效）：拉高表示 `o_DOUT` 引脚正在驱动数据输出（写事务的中段）。

设计准则：**任意时刻最多一个读选通或一个写选通有效**，避免 `i_DIN` 与 `o_DOUT` 在外部总线上打架。testbench 正是靠 `MEN_n` 与 `DEN_n` 互斥地把不同存储器挂上 `RDBUS`（见 4.4.4）。

#### 4.4.2 核心流程

时序 always 块的结构是 `case(busctrl_mode[2:0])` 外套 `case(cyclecntr)`——一个「6 模式 × 4 相位」的矩阵。把源码精简成下表（1 = 高电平/无效，0 = 低电平/有效；`OE` 即 `o_DOUT_OE`；「锁存」列说明相位 3 的副作用）：

| 模式 | 相位 0 | 相位 1 | 相位 2 | 相位 3 |
| --- | --- | --- | --- | --- |
| `STOP` | MEN=1,DEN=1,WE=1,OE=0 | 同左 | 同左 | 同左 |
| `OPCODE_READ` | MEN=**0**,OE=0 | MEN=**0**,OE=0 | MEN=**0**,OE=0 | MEN=1 → 锁存 `if_opcodereg←i_DIN` |
| `DATA_READ` | MEN=**0**,OE=0 | MEN=**0**,OE=0 | MEN=**0**,OE=0 | MEN=1 → 锁存 `busctrl_inlatch←i_DIN` |
| `DATA_WRITE` | 全无效 | OE=**1**, `o_DOUT←ram_output` | WE=**0**,OE=**1** | 全无效 |
| `COMMAND_IN` | DEN=**0**,OE=0 | DEN=**0**,OE=0 | DEN=**0**,OE=0 | DEN=1 → 锁存 `busctrl_inlatch←i_DIN` |
| `COMMAND_OUT` | 全无效 | OE=**1**, `o_DOUT←reg_wrbus` | WE=**0**,OE=**1** | 全无效 |

规律很清晰：

- **读类事务**（`OPCODE_READ`/`DATA_READ`/`COMMAND_IN`）：选通在相位 0~2 保持有效、相位 3 抬起，并在**相位 3 的边沿**把 `i_DIN` 锁存进目标寄存器。相位 3 正是 `cyc_ncen`，与「全芯片在相位 3 更新状态」的节拍一致。
- **写类事务**（`DATA_WRITE`/`COMMAND_OUT`）：相位 0 空闲，相位 1 把数据装上 `o_DOUT` 并打开 OE，相位 2 拉低 `WE_n` 发出写脉冲，相位 3 收回。数据来源不同（`ram_output` vs `reg_wrbus`）。

读路径的数据到达时刻可用一个简单的时序量描述：从相位 0 拉低 `MEN_n` 到相位 3 锁存，外部 ROM 有约 3 个 `EMUCLK` 周期的寻址窗口。若机器周期为 \(T_{mach}\)，则寻址窗口约 \( \tfrac{3}{4} T_{mach} \)。

#### 4.4.3 源码精读

时序 always 块整体框架与复位、取指锁存：

[src/IKA32010.sv:176-188](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L176-L188) — 复位时 `busctrl_inlatch←0`、`if_opcodereg←0x7F80`（NOP）；相位 3 时若 `if_opcodereg_force_iack` 则注入内部 IACK 码 `0xF000`（中断用，见 u3-l3），否则在 `OPCODE_READ` 模式下把 `i_DIN` 锁进指令寄存器。注意整个块受 `i_CLKIN_PCEN` 门控，与 u1-l4 一致。

六种事务各自的相位矩阵：

- `STOP`：[src/IKA32010.sv:191-198](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L191-L198)（全部无效）
- `OPCODE_READ`：[src/IKA32010.sv:201-208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L201-L208)
- `DATA_READ`：[src/IKA32010.sv:211-219](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L211-L219)（注意 217 行相位 3 锁存 `busctrl_inlatch`）
- `DATA_WRITE`：[src/IKA32010.sv:222-230](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L222-L230)（226 行相位 1 装载 `ram_output`）
- `COMMAND_IN`：[src/IKA32010.sv:233-241](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L233-L241)（239 行相位 3 锁存 `busctrl_inlatch`）
- `COMMAND_OUT`：[src/IKA32010.sv:244-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L244-L252)（248 行相位 1 装载 `reg_wrbus`）

外部如何复用总线（testbench 侧）：

[src/IKA32010_tb.v:72](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L72) 与 [src/IKA32010_tb.v:80](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010_tb.v#L80) — 两行 `assign RDBUS = (X_n) ? 'Z : 数据` 分别用 `DEN_n` 和 `MEN_n` 选通两个不同的存储器（外设 RAM 与程序 ROM）挂上同一根 `RDBUS`。因为 `MEN_n` 与 `DEN_n` 从不同时为低，二者不会驱动冲突——这就是「互斥选通」在系统级的体现，也是 u1-l5 提到的总线复用的具体实现。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手把「指令读」与「OUT 输出」两种事务在 4 个相位上的电平整理成时序表，把源码读懂为表格。

**操作步骤**：

1. 打开 [src/IKA32010.sv:201-208](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L201-L208)（`OPCODE_READ`），逐相位读出 `o_MEN_n`/`o_DEN_n`/`o_WE_n`/`o_DOUT_OE` 的取值，填进下表 1。
2. 打开 [src/IKA32010.sv:244-252](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L244-L252)（`COMMAND_OUT`），同样填进下表 2。
3. 在 testbench 中给 `cyclecntr`、`o_MEN_n`、`o_DEN_n`、`o_WE_n`、`o_DOUT_OE` 加波形探针，跑一段包含取指与 OUT 的程序，对照两张表。

**表 1：指令读 OPCODE_READ（参考答案）**

| `cyclecntr` | `o_MEN_n` | `o_DEN_n` | `o_WE_n` | `o_DOUT_OE` | 相位 3 副作用 |
| --- | --- | --- | --- | --- | --- |
| 0 | **0** | 1 | 1 | 0 | — |
| 1 | **0** | 1 | 1 | 0 | — |
| 2 | **0** | 1 | 1 | 0 | — |
| 3 | 1 | 1 | 1 | 0 | `if_opcodereg ← i_DIN` |

**表 2：OUT 输出 COMMAND_OUT（参考答案）**

| `cyclecntr` | `o_MEN_n` | `o_DEN_n` | `o_WE_n` | `o_DOUT_OE` | 备注 |
| --- | --- | --- | --- | --- | --- |
| 0 | 1 | 1 | 1 | 0 | 空闲 |
| 1 | 1 | 1 | 1 | **1** | `o_DOUT ← reg_wrbus` |
| 2 | 1 | 1 | **0** | **1** | 写脉冲 |
| 3 | 1 | 1 | 1 | 0 | 收回 |

**需要观察的现象**：指令读时 `o_MEN_n` 在相位 0~2 为低、相位 3 抬起；OUT 时 `o_WE_n` 仅在相位 2 为低，且 `o_DOUT_OE` 在相位 1~2 为高。

**预期结果**：波形与上表一致。若你在仿真里看到了与表不同的电平，多半是因为那一周期并非该模式（例如 OUT 的第 2 个机器周期已切回 `OPCODE_READ`）——可对照指令译码里的 `ex_inst_cycle` 分支确认。**待本地验证**：取决于你运行的程序。

#### 4.4.5 小练习与答案

- **练习 1**：为什么写事务（`DATA_WRITE`/`COMMAND_OUT`）在相位 1 就打开 `o_DOUT_OE`，而把 `o_WE_n` 留到相位 2 才拉低？
  - **答案**：先驱动数据（`o_DOUT` 稳定）再发写脉冲，保证接收端在 `WE_n` 有效的边沿采样到的是已稳定的数据，避免毛刺写入。这是经典的「数据先就绪、选通后到达」的时序惯例。
- **练习 2**：`DATA_WRITE`（表写）与 `COMMAND_OUT`（OUT）的选通时序完全相同，外部如何区分是把数据写进 ROM 还是写向外设？
  - **答案**：靠**地址**区分。`DATA_WRITE` 用 `BUSCTRL_ADDR_PC`（地址是程序空间的 `if_pc`），`COMMAND_OUT` 用 `BUSCTRL_ADDR_PERIPHERAL`（地址是 PA）。外部解码逻辑根据 `o_AOUT` 的取值范围把写操作路由到 ROM 还是外设。两者的 `WE_n` 形状相同，但落在不同地址上。
- **练习 3**：相位 3 抬起读选通（`MEN_n`/`DEN_n` 回到 1）的同时锁存数据，这个顺序安全吗？
  - **答案**：安全。选通是寄存输出，在相位 3 的 `EMUCLK` 边沿更新；同一边沿上 `i_DIN` 已在整个相位 2 期间被外部 ROM 稳定驱动，故锁存到的是有效数据。选通抬起与锁存在同一拍完成，外部下一拍即可释放总线。

---

## 5. 综合实践

把本讲的三块知识串起来：**模式选择 → 地址 MUX → 四相位时序 → 数据锁存**。

**任务**：用一张大图描述「TBLR 表读指令」第 1 个机器周期（`ex_inst_cycle==1`，执行 `DATA_READ`）里，总线控制器的全部行为。

**操作步骤**：

1. 阅读 TBLR 译码 [src/IKA32010.sv:1675-1684](https://github.com/ika-musume/IKA32010/blob/51bc1f05a2a08a61c8815a9643d08a42e99779c6/src/IKA32010.sv#L1675-L1684)，确认该周期 `busctrl_req = DATA_READ`、`busctrl_addr_muxsel = BUSCTRL_ADDR_PC`、`register_wrbus_source_sel = WRBUS_SOURCE_STACK`、`if_pc_modesel = PC_LOAD_WRBUS`。
2. 据此推导本周期：
   - `busctrl_mode = {1'b0, 3'd2} = 4'b0010`（PC 地址 + DATA_READ）。
   - 地址 MUX：`o_AOUT = if_pc`（此时 `if_pc` 已被改造为 ACC 指向的 ROM 表地址）。
   - 时序：相位 0~2 `o_MEN_n=0`，相位 3 锁存 `busctrl_inlatch ← i_DIN`（从 ROM 读到的表数据）。
3. 画出这个周期的 4 相位时序表，并标注：相位 3 之后，`busctrl_inlatch` 里的数据会在下一周期（`ex_inst_cycle==2`）经 `WRBUS_SOURCE_INLATCH` 写进片内 RAM。

**预期结果**：你能用一句话讲清「TBLR 的这一拍，总线控制器用 PC 地址读 ROM、把数据锁进 `busctrl_inlatch`，为下一拍写回 RAM 做准备」——这正是总线控制器与内部写总线（u2-l1）、RAM（u2-l5）协作的缩影。

**待本地验证**：可在 testbench 中对 `busctrl_mode`、`o_AOUT`、`o_MEN_n`、`busctrl_inlatch` 加探针，跑一段含 TBLR 的程序核对。

## 6. 本讲小结

- 总线控制器分为**请求侧**（`busctrl_req` + `busctrl_addr_muxsel`，由微码每周期设置）与**定序侧**（把模式展开成 4 相位外部时序）两层。
- `busctrl_req`（3 位）编码 6 种事务：`STOP`/`OPCODE_READ`/`DATA_READ`/`DATA_WRITE`/`COMMAND_IN`/`COMMAND_OUT`，常量定义在 `IKA32010_mnemonics.sv`。
- `busctrl_mode` 是 `{addr_muxsel, req}` 的 4 位打包：`[3]` 选地址源、`[2:0]` 选事务类型。
- 地址 MUX 让 `o_AOUT` 在 `if_pc`（程序空间）与 `{9'b0, if_opcodereg[10:8]}`（外设口 PA）之间二选一。
- 读事务在相位 0~2 保持选通低、相位 3 抬起并锁存数据（取指→`if_opcodereg`，表读/IN→`busctrl_inlatch`）；写事务在相位 1 驱动 `o_DOUT`、相位 2 发 `WE_n` 脉冲。
- 三个选通 `MEN_n`/`DEN_n`/`WE_n` 在系统级保证「同一时刻只有一个数据源驱动总线」，testbench 正是靠它们的互斥来复用 `RDBUS`。

## 7. 下一步学习建议

- **u3-l1（微码架构总览）**：本讲一直说「微码每周期设置 `busctrl_req`」，下一阶段就去拆开那个巨大的 `casez(if_opcodereg)` 微码块，看清默认值与覆盖机制。
- **u3-l2（多周期指令时序与状态机）**：本讲提到了 `ex_inst_cycle`，那是多周期指令的相位计数器，建议接着学它如何与总线控制器配合完成 TBLR/IN/OUT 这类跨周期事务。
- **u2-l5（数据 RAM 与寻址）**：本讲的 `busctrl_inlatch`（读入数据）最终多被写回片内 RAM，可结合 RAM 讲义看清「外部总线 → `busctrl_inlatch` → `reg_wrbus` → RAM」的完整数据通路。
- **延伸阅读**：对照 `docs/` 下的 TMS32010 用户手册外部总线时序图（MEN/DEN/WE 部分），核对本讲整理的电平表与官方时序是否一致。
