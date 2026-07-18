# RISC-V 压缩指令集支持

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `COMPRESSED_ISA` 这个参数（以及对应的 `-DCOMPRESSED_ISA` 编译宏）到底打开了什么、以什么方式打开。
- 用一条具体指令（如 `c.addi x2,x2,1`）追踪它如何在 `mem_rdata_q` 中被「部分展开」成等价的 32 位指令，并解释为什么不需要把整条 32 位编码都拼出来。
- 画出压缩指令下的取指流程：`next_pc[1]`、`mem_la_firstword`、`mem_la_secondword`、`prefetched_high_word` 如何协作处理「半字对齐取指」与「跨字 32 位指令」。
- 动手修改 `testbench_ez.v`，让一颗默认关闭压缩的核跑起一条真实压缩指令，并验证它的行为与 32 位版本完全一致。

## 2. 前置知识

本讲建立在 u4-l1（指令译码器）与 u5-l3（原生内存接口）之上，开始前请确认你熟悉下面几个概念：

- **RV32C 压缩指令集**：标准 RISC-V 指令固定 32 位（4 字节）。为了省代码空间，RISC-V 定义了一组 16 位（2 字节）的「压缩」指令，称为 C 扩展。一条 16 位指令是某条 32 位指令的简写：语义完全等价，只是编码更短、能表示的立即数/寄存器范围更小。程序里可以 16 位与 32 位指令混排。
- **一位热码（one-hot）译码**：u4-l1 讲过，PicoRV32 用 `instr_addi`、`instr_lw` 这样的「一位一指令」信号。本讲你会看到压缩指令最终也汇入同一套 `instr_*` 信号。
- **valid-ready 握手与字对齐取指**：u5-l3 讲过，原生接口用 `mem_valid`/`mem_ready` 一次传一个 **32 位字**。也就是说，即使要取的是 16 位指令，总线读回来的也是一整个 32 位字。本讲的核心难点正是「如何在只能按字读的总线上，正确取出任意半字位置的 16 位指令」。
- **冯·诺依曼结构**：PicoRV32 指令和数据共用同一条总线、同一块内存。

一句话直觉：**PicoRV32 内部其实只认 32 位指令。开启压缩后，它在取指环节多加了一层「翻译」，把取到的 16 位 C 指令当场改写成等价的 32 位形式，再喂给后面那套已经写好的 32 位译码器。** 这样后面的数据通路、ALU、状态机一行都不用改。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [picorv32.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v) | 全部压缩逻辑都集中在这一个文件里：参数声明、16→32 展开、取指对齐与预取、PC 增量与中断返回地址的「压缩位」处理。 |
| [testbench_ez.v](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v) | 唯一不依赖 RISC-V 工具链的最小测试台。本讲会改它两行，让默认关压缩的核跑起一条真实压缩指令。 |
| [Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile) | 里面藏着一个容易踩坑的细节：`COMPRESSED_ISA = C` 与 `$(subst ...)` 的关系。 |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | 对 `COMPRESSED_ISA`、q0 返回地址 LSB、large 配置的官方说明。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块，正好对应压缩支持的三个工程问题：

1. **开关**：用什么手段开启压缩、开了之后哪些电路被激活。
2. **展开**：16 位 C 指令如何被改写成等价 32 位、喂给 32 位译码器。
3. **对齐取指**：在只能按 32 位字读的总线上，怎么取到任意半字位置的指令，又不浪费总线带宽。

### 4.1 C 扩展开关：COMPRESSED_ISA

#### 4.1.1 概念说明

`COMPRESSED_ISA` 是一个 1 位的 Verilog `parameter`，默认为 0（关闭）。这继承了 u3-l1 讲过的「编译期开关」套路：综合时它是常量，配合 `if (COMPRESSED_ISA)`、`? :` 会被常量折叠掉，于是同一份 `picorv32.v` 既能综合出「不带压缩」的小核，也能综合出「带压缩」的大核。README 把它列为 [large 配置](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L729-L732)的组成部分之一。

它的声明在模块参数列表里：

[picorv32.v:72](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L72) —— `parameter [0:0] COMPRESSED_ISA = 0;`，CPU 压缩指令集的总开关。

#### 4.1.2 核心流程：宏 vs 参数，一个关键陷阱

读者最先会问：既然是参数，那 Makefile 里的 `COMPRESSED_ISA = C` 是怎么回事？这其实是**两层东西**，混在一起最容易踩坑：

- `Makefile` 第 19 行定义 `COMPRESSED_ISA = C`（注意这是一个 **make 变量**，字符串 `"C"`）。
- 各编译规则（如 [Makefile:69-71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L71) 的 `testbench_ez.vvp`）里有 `$(subst C,-DCOMPRESSED_ISA,$(COMPRESSED_ISA))`。这句把字符串里的 `C` 替换成 `-DCOMPRESSED_ISA`，于是命令行上多了一个 **Verilog 宏定义** `-DCOMPRESSED_ISA`。

> 也就是说，`make test_ez` 实际上是用 `-DCOMPRESSED_ISA` 去编译的。**但是**——这是关键——`-DCOMPRESSED_ISA` 只定义了一个名为 `COMPRESSED_ISA` 的文本宏，**它不会自动把模块参数也置成 1**。参数是否为 1，取决于实例化处有没有写 `\.COMPRESSED_ISA(1)` 或源码里有没有 `\`ifdef COMPRESSED_ISA` 去改参数。

证据就在 `testbench.v`：它确实写了对应的 ifdef 守卫来把宏翻译成参数。

[testbench.v:168-170](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench.v#L168-L170) —— 当宏 `COMPRESSED_ISA` 存在时，才把实例参数 `.COMPRESSED_ISA(1)` 传进去。

而 `testbench_ez.v` **没有**任何 `\`ifdef` 守卫：

[testbench_ez.v:47-48](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L47-L48) —— `picorv32 #() uut (...)`，参数覆盖列表是空的。

**结论**：`make test_ez` 虽然带着 `-DCOMPRESSED_ISA` 编译，但 `testbench_ez.v` 从不消费这个宏，所以实例化出来的核 `COMPRESSED_ISA` 取默认值 0，**并不支持压缩**。这正是本讲实践任务要亲手改掉的地方。

开启压缩后，下列电路被「激活」（在源码里都由 `COMPRESSED_ISA ?` 或 `if (COMPRESSED_ISA)` 门控）：

- 取指阶段的半字对齐与预取逻辑（4.3 节）。
- 取指完成后的 16→32 位展开大 `case`（4.2 节）。
- 译码器里的 `compressed_instr` 标志与直接解码路径（4.2 节）。
- PC 增量从「恒 +4」变成「按 `compressed_instr` 选 +2 或 +4」。
- 对齐检查从 `|reg_pc[1:0]` 放宽为 `reg_pc[0]`（4.3 节末）。

#### 4.1.3 代码实践：肉眼确认开关

1. 实践目标：确认「`-D` 宏 ≠ 参数为 1」这件事。
2. 操作步骤：阅读 [picorv32.v:72](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L72) 的参数声明、[Makefile:19](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L19) 与 [Makefile:69-71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L71)、[testbench_ez.v:47-48](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L47-L48)。
3. 需要观察的现象：`testbench_ez.v` 实例化参数为空，没有任何 `\`ifdef COMPRESSED_ISA`。
4. 预期结论：`make test_ez` 编译出的核 `COMPRESSED_ISA=0`，若硬塞一条 16 位指令进去，核会把它当成 32 位指令去译码，几乎必然触发非法指令陷入。
5. 待本地验证：可在 `testbench_ez.v` 的 `picorv32 #()` 里临时写 `.COMPRESSED_ISA(1)`，对比是否改变行为。

### 4.2 压缩到 32 位展开：mem_rdata_q 的「部分改写」

#### 4.2.1 概念说明

PicoRV32 不打算为压缩指令再写一套独立的执行单元。它的做法是：**在取指与译码之间，把 16 位 C 指令「翻译」成一条语义相同的 32 位指令，再交给已有的 32 位译码器**。这个翻译不是在执行期动态做的，而是在取指完成的那一拍，用一段组合/时序逻辑把取回字的若干位段「改写」到位。

承担「改写后结果」的寄存器就是 `mem_rdata_q`：

[picorv32.v:354](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L354) —— `reg [31:0] mem_rdata_q;`，保存「展开后」的指令字。

关键直觉：**这个展开是「部分」的**。它只改写那些「32 位译码器需要、但 16 位编码里字段位置不一样」的位段（主要是 funct3、funct7、立即数）；至于寄存器号 `rd/rs1/rs2`、以及「这是哪一类运算」（ALU 立即数型？load？store？分支？），则由译码器**直接从 16 位原始编码里读出来**，根本不经过 `mem_rdata_q`。两者配合，省下了把整条 32 位编码全部拼齐的硬件。

#### 4.2.2 核心流程：两段协同的解码

`mem_rdata_q` 的赋值在两个地方：

[picorv32.v:432-433](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L432-L433) —— 每次取指成交（`mem_xfer`）时，先把取回字（经对齐处理后的 `mem_rdata_latched`）整体搬进 `mem_rdata_q`。

然后是一段长长的 `case`，只在压缩模式且取指完成时触发，逐条把 C 指令的 funct3/funct7/立即数「覆盖」到 `mem_rdata_q` 上：

[picorv32.v:436-543](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L436-L543) —— 按 C 扩展的三个 quadrant（`mem_rdata_latched[1:0]` 为 00/01/10）和 funct3 分发，对每条 C 指令改写对应字段。例如 [picorv32.v:456-459](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L456-L459) 处理 `C.ADDI`：把 funct3 置 `000`、把立即数从 16 位编码里抽出来符号扩展后放进 `mem_rdata_q[31:20]`。

与此同时，译码器在另一段逻辑里**直接**从 16 位原始字读出寄存器号和「指令类别」：

[picorv32.v:892-897](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L892-L897) —— 取指完成时，若 `mem_rdata_latched[1:0] != 2'b11`（最低两位不是 `11` 即判定为 16 位压缩指令，因为所有 32 位指令的低两位都是 `11`），置 `compressed_instr=1`，并把 `decoded_rd/rs1/rs2` 清零准备重填。

[picorv32.v:902-1034](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L902-L1034) —— 按 quadrant/funct3 直接解码：设置 `is_alu_reg_imm`、`is_lb_lh_lw_lbu_lhu`、`is_sb_sh_sw`、`is_beq_...` 等类别信号，并填好 `decoded_rd/rs1/rs2`。注意这些 `is_*` 会**覆盖**掉 [picorv32.v:874-878](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L874-L878) 按 32 位 opcode 算出来的初值——因为 16 位指令的「opcode 位」根本不是标准 32 位 opcode。

最后，u4-l1 讲过的第二级译码（由 `decoder_trigger` 触发）读这个**已改写**的 `mem_rdata_q`，靠 funct3/funct7 选出最终的 `instr_*`。例如：

[picorv32.v:1057](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1057) —— `instr_addi <= is_alu_reg_imm && mem_rdata_q[14:12]==3'b000;`。对 `C.ADDI` 来说，`is_alu_reg_imm` 由压缩解码直接置 1，`mem_rdata_q[14:12]` 已被改写成 `000`，于是 `instr_addi` 被正确点亮。

用一段伪代码概括这三步协作：

```
取指成交:
    mem_rdata_q <= 取回字          // 整体搬入
    if 压缩:
        按 C 编码改写 mem_rdata_q 的 funct3/funct7/imm   // 部分展开
        直接从 16 位字读出 is_* 类别与 rd/rs1/rs2        // 不经过 mem_rdata_q
译码(decoder_trigger):
    用 is_* + 已改写的 mem_rdata_q[funct3/funct7] 选出 instr_*   // 复用 32 位译码器
```

#### 4.2.3 源码精读：用 c.addi x2,x2,1 走一遍

把 [`addi x2,x2,1`](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L67)（32 位编码 `0x00110113`）换成压缩版 `c.addi x2,x2,1`，编码为 `0x0105`（字段拆分见下表，C 扩展的 `funct3` 在 bits[15:13]、rd/rs1 在 bits[11:7]、imm 在 bits[12] 与 bits[6:2]、最低两位 `01` 标识 quadrant 1）。

| 字段 | 位数 | 值 | 含义 |
| --- | --- | --- | --- |
| op | [1:0] | `01` | quadrant 1 |
| imm[4:0] | [6:2] | `00001` | 立即数低位 = 1 |
| rd/rs1 | [11:7] | `00010` | x2 |
| imm[5] | [12] | `0` | 立即数符号位 |
| funct3 | [15:13] | `000` | C.ADDI |

拼起来：bits[15:0] = `000_0_00010_00001_01` = `0x0105`。这样改写后：

- [picorv32.v:893-894](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L893-L894)：`mem_rdata_latched[1:0]=01 != 2'b11` → `compressed_instr=1`。
- [picorv32.v:924-928](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L924-L928)（C.ADDI 分支）：`is_alu_reg_imm<=1`，`decoded_rd<=2`，`decoded_rs1<=2`（bits[11:7]=00010）。
- [picorv32.v:456-459](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L456-L459)：`mem_rdata_q[14:12]<=000`，`mem_rdata_q[31:20] <= $signed({bit12=0, bits[6:2]=00001}) = 1`。
- [picorv32.v:1057](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1057)：`is_alu_reg_imm=1` 且 funct3 已是 `000` → `instr_addi=1`。

最终执行的就是 `addi x2, x2, 1`，与 32 位版完全等价。注意 `mem_rdata_q[6:0]`（opcode 位）此时仍是压缩编码的残留值，并不是标准 ADDI 的 opcode `0x13`——但没关系，译码器从不靠 `mem_rdata_q[6:0]` 判定 ALU 立即数类指令，它靠 `is_alu_reg_imm`。

一个小细节：`c.ebreak`（`0x9002`）是少数在第二级译码里被**直接**特殊识别的压缩指令，见 [picorv32.v:1086-1087](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1086-L1087)，它不走上面的展开 `case`。

#### 4.2.4 代码实践：用工具链核对编码

1. 实践目标：验证 `c.addi x2,x2,1` 的 16 位编码确实是 `0x0105`，并对照源码展开表。
2. 操作步骤（需要 u2-l1 安装的 rv32ic 工具链）：
   ```bash
   echo 'c.addi x2,x2,1' | riscv32-unknown-elf-gcc -c -x assembler-with-cpp -march=rv32ic -mabi=ilp32 -o - -o /tmp/t.o
   riscv32-unknown-elf-objdump -d /tmp/t.o
   ```
3. 需要观察的现象：objdump 反汇编里这条指令显示为 `0105 c.addi a1,1`（x2 即 a1，工具链常用 ABI 名）。
4. 预期结果：编码低 16 位 = `0x0105`，与上表手工推导一致。
5. 待本地验证：若无工具链，可保留手工推导结果，等到第 5 节综合实践里直接在仿真中验证其行为。

### 4.3 压缩取指与对齐：next_pc[1] 与 mem_la_firstword

#### 4.3.1 概念说明

压缩支持最棘手的不是译码，而是**取指**。原因：总线一次只能读一个 32 位字（4 字节，地址低两位为 00），但程序计数器 PC 现在可以指向任意 2 字节边界——也就是某个字的低半字（PC[1]=0）或高半字（PC[1]=1）。更麻烦的是，当 PC 落在高半字、而那里的指令又恰好是 32 位指令时，这条指令会**跨越两个字**（高半字在本字、低半字在下一个字），必须读两次总线。

PicoRV32 用三个机制解决：

1. **`mem_la_firstword`**：识别「PC 在高半字」的取指，把本字的高半字挪到指令低位。
2. **`mem_la_secondword` + `mem_16bit_buffer`**：处理跨字的 32 位指令，把两块半字拼起来。
3. **`prefetched_high_word`**：预取优化——读一个字时若低半字是 2 字节压缩指令，就把同字的高半字顺手存起来，下一次取指（PC+2，正好落在这个高半字）就直接复用，省掉一次总线读。

#### 4.3.2 核心流程

先看 `next_pc` 与「首字」判定。`next_pc` 是下一拍要取的地址：

[picorv32.v:1213](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1213) —— `assign next_pc = latched_store && latched_branch ? reg_out & ~1 : reg_next_pc;`（分支时用目标地址，否则顺序推进）。

它的 bit 1 决定了目标指令在字的哪一半：

[picorv32.v:362](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L362) —— `wire mem_la_firstword = COMPRESSED_ISA && (mem_do_prefetch || mem_do_rinst) && next_pc[1] && !mem_la_secondword;`。当取指且 `next_pc[1]=1`（目标在高半字）时拉高，意思是「我要的高半字在当前字的高位」。

`mem_la_addr` 永远把地址按字对齐（低两位补 0），靠 `+ mem_la_firstword_xfer` 在必要时跳到下一个字：

[picorv32.v:382](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L382) —— 取指地址 = `{next_pc[31:2] + mem_la_firstword_xfer, 2'b00}`。

读回字后，`mem_rdata_latched` 做「半字重排」，把目标半字摆到低位：

[picorv32.v:386-388](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L386-L388) —— 三种压缩场景：用预取高字（`{16'bx, mem_16bit_buffer}`）、跨字第二字（`{本字低半字, 缓存的第一块}`）、PC 在高半字（`{16'bx, 本字高半字}`）；非压缩时原样输出。

跨字 32 位指令的处理在内存状态机里：

[picorv32.v:600-619](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L600-L619) —— 读到一个字后：若处于 `mem_la_read` 且压缩模式，说明还要读第二个字（`mem_la_secondword<=1`），并把第一块存进 `mem_16bit_buffer`；否则在压缩模式下检查低半字是不是 2 字节指令（`~&mem_rdata[1:0]`，即低两位非 `11`），是的话把高半字预存起来。

[picorv32.v:612](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L612) —— `prefetched_high_word <= 1;`，标记「高半字已缓存，下次可复用」。

下次取指若命中这块缓存，就**完全不发起总线读**：

[picorv32.v:372-373](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L372-L373) —— `mem_la_use_prefetched_high_word` 命中时，`mem_xfer` 直接视为成交，[picorv32.v:584](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L584) 里 `mem_valid <= !mem_la_use_prefetched_high_word` 把总线请求也压下去。

预取的前提是「顺序执行」。一旦发生分支、中断或复位，缓存的高半字就不再有效，必须作废：

[picorv32.v:1295-1301](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1295-L1301) —— `clear_prefetched_high_word` 在 `latched_branch || irq_state || !resetn` 时被置起（仅压缩模式），下一拍清掉 `prefetched_high_word`。

PC 增量也按是否压缩走 +2/+4：

[picorv32.v:1552](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1552) 与 [picorv32.v:1560](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1560) —— `reg_next_pc <= current_pc + (compressed_instr ? 2 : 4);`。

最后是对齐检查的放宽。非压缩时 PC 必须四字节对齐（`|reg_pc[1:0]` 非零即未对齐）；压缩时允许 2 字节对齐，只检查最低位：

[picorv32.v:1938](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1938) —— `CATCH_MISALIGN && ... (COMPRESSED_ISA ? reg_pc[0] : |reg_pc[1:0])`。相应地，若关掉 `CATCH_MISALIGN`，复位后会强制把 PC 的对齐位清零，见 [picorv32.v:1965-1972](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1965-L1972)。

一个值得记住的副产物：因为返回地址要能正确回到「下一条指令」，而中断可能打断在 2 字节或 4 字节指令上，PicoRV32 把这个「是不是压缩」的标志**塞进返回地址的最低位**：

[picorv32.v:1325](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1325) —— 中断保存返回地址时 `cpuregs_wrdata = reg_next_pc | latched_compr;`，最低位即压缩标志。

这与 u6-l2 讲过的「q0 的 LSB 是压缩标志」是同一件事，README 也有明确说明：[README.md:516-518](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L516-L518)。

#### 4.3.3 小练习与答案

**练习 1**：假如关掉 `prefetched_high_word` 这套预取（假设把它恒置 0），压缩程序的 CPI 会怎样变化？功能会出错吗？

> 参考答案：功能不会出错——预取只是省一次总线读的优化；关掉后，每次取高半字都要重新发起一次 32 位字读，总线事务变多，CPI 上升（代码体积越小、压缩指令越密集，影响越明显）。

**练习 2**：为什么 `mem_la_firstword` 里要有 `!mem_la_secondword` 这个条件？

> 参考答案：`mem_la_secondword=1` 表示当前正在读跨字 32 位指令的「第二个字」，此时目标指令的低半字来自新读的字、高半字已在 `mem_16bit_buffer` 里，不再属于「PC 在高半字的首字」情形，所以要把 `mem_la_firstword` 拉低，避免重排逻辑选错分支。

**练习 3**：分支跳转后，`prefetched_high_word` 为什么必须清零？

> 参考答案：预取的前提是「下一条指令顺序地落在本字高半字」。分支/中断会跳到任意地址，缓存的高半字不再是被取的指令，若不清零就会把陈旧数据当成指令执行。

## 5. 综合实践：让 testbench_ez 跑一条真实压缩指令

这是一个把本讲三块内容（开关、展开、对齐预取）串起来的动手任务。目标：**只改 `testbench_ez.v` 两行，让默认关压缩的核跑起一条压缩指令，并证明它的行为与原 32 位版本逐字节相同。**

### 实践目标

把 [`testbench_ez.v:67`](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L67) 那条 32 位 `addi x2,x2,1`（`0x00110113`）替换成等价的压缩指令，观察：

1. 取指成交后 `mem_rdata_q` 如何被部分展开；
2. 程序对外可见的行为（往 `0x3fc` 写的计数序列）与原来是否一致；
3. 高半字被预取后，第二次取指是否省掉了一次总线读（即少一行 `ifetch` 打印）。

### 操作步骤

**第 1 步——打开压缩开关。** 把 [testbench_ez.v:47](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L47) 的空参数列表改成显式开启压缩：

```verilog
picorv32 #(.COMPRESSED_ISA(1)) uut (
```

**第 2 步——塞进一条压缩指令，且不破坏后续地址对齐。** 直接把一条 2 字节指令替换原本 4 字节的 `addi`，会让后面所有指令地址前移 2 字节、`j` 的跳转偏移也失效。解决办法是**用一个 2 字节压缩指令 + 一个 2 字节 `c.nop`（`0x0001`）凑成 4 字节**，正好占满原来的一个字，后续指令地址不变。

把 [testbench_ez.v:67](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/testbench_ez.v#L67) 改为：

```verilog
memory[3] = 32'h 00010105; // c.addi x2,x2,1 (0x0105) | c.nop (0x0001)
```

小端字节序下，低半字 `0x0105` 落在地址 12（`c.addi x2,x2,1`），高半字 `0x0001` 落在地址 14（`c.nop`），合起来正好占据原 `memory[3]` 这个字。地址 16 的 `sw`、地址 20 的 `j` 都不动，跳转偏移自然也不用改。

**第 3 步——编译运行。**

```bash
make test_ez
```

> 说明：`make test_ez` 本就带 `-DCOMPRESSED_ISA` 编译（见 [Makefile:69-71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L71)），但因为我们在实例上硬写了 `.COMPRESSED_ISA(1)`，所以即便那个宏被忽略也无妨。

### 需要观察的现象与预期结果

1. **行为等价**：`write 0x000003fc:` 后面应依次出现 `0x00000000`、`0x00000001`、`0x00000002`、…… 与未改之前的 32 位版本**完全相同**。这证明 `c.addi x2,x2,1` 经过 4.2 节的展开后，行为与 `addi x2,x2,1` 一致。
2. **取指打印的变化**：在地址 `0x0000000c` 会看到一次 `ifetch ...: 0x00010105`（读出整个字）；但**地址 `0x0000000e`（`c.nop`）不会有独立的 `ifetch` 行**——因为它的高半字在上一笔读时已被预存进 `mem_16bit_buffer`（[picorv32.v:612](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L612)），本笔取指靠 `mem_la_use_prefetched_high_word` 直接成交、不发起总线读（[picorv32.v:584](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L584)）。这正是 4.3 节预取优化的可观测证据。
3. **`next_pc[1]` 与 `mem_la_firstword` 的体现**：CPU 在执行完地址 12 的 `c.addi` 后，`compressed_instr=1` 使 `reg_next_pc = 12 + 2 = 14`（[picorv32.v:1560](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L1560)），于是下一次取指 `next_pc[1]=1`，`mem_la_firstword` 拉高（[picorv32.v:362](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L362)）——又因为该高半字已被预取，命中 `mem_la_use_prefetched_high_word`，于是免总线读。

> 待本地验证：上述打印行与「地址 0x0e 无 ifetch」的结论，请在本地 Icarus Verilog 上运行确认；不同 iverilog 版本的 `$display` 时序细节可能略有差异，但「`0x3fc` 处计数序列不变」这一点是稳定的判据。

### 进阶（可选）

把 `memory[3]` 改成一条**真正的 32 位指令放在高半字**的布局（例如让地址 12 是某个 2 字节压缩指令、地址 14 起是一条 32 位指令），观察 `mem_la_secondword` 如何发起第二次总线读、`mem_16bit_buffer` 如何把两块半字拼成完整 32 位指令（对照 [picorv32.v:600-619](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L600-L619) 与 [picorv32.v:386-388](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L386-L388)）。这一步需要重新计算后续地址与跳转偏移，建议借助 rv32ic 工具链汇编后用 `objdump` 确认编码，再填回 `memory[]`。

## 6. 本讲小结

- `COMPRESSED_ISA` 是 1 位编译期参数；Makefile 里的 `-DCOMPRESSED_ISA` 只是文本宏，**不会自动把参数置 1**——`testbench_ez.v` 正因为没有 `\`ifdef` 守卫，所以默认跑的是不带压缩的核。
- 压缩支持的本质是「取指环节加一层翻译」：在取指完成的那拍把 16 位 C 指令**部分展开**进 `mem_rdata_q`（只改 funct3/funct7/立即数），寄存器号与指令类别则由译码器**直接**从 16 位字读出，最终复用整套 32 位 `instr_*` 译码器与数据通路。
- 取指对齐靠三个机制：`mem_la_firstword` 处理「PC 在高半字」、`mem_la_secondword`+`mem_16bit_buffer` 处理跨字 32 位指令、`prefetched_high_word` 用预取省掉相邻半字的重复总线读；分支/中断/复位会清掉预取缓存。
- PC 增量按 `compressed_instr` 选 +2/+4；对齐检查从四字节放宽到两字节；中断返回地址把「是否压缩」的标志塞进最低位（q0 LSB）。
- 两行改动（`.COMPRESSED_ISA(1)` + `memory[3]=0x00010105`）即可让 `testbench_ez` 跑起真实压缩指令，且对外行为与 32 位版本逐字节一致。

## 7. 下一步学习建议

- **回到完整 SoC**：本讲只动了 CPU 核。下一讲 u8-l1（PicoSoC）会展示 `picorv32` 如何与 SRAM、SPI flash、UART 集成在一颗完整片上系统里，那时你会看到压缩指令在真实「从 SPI flash 取指执行」场景下对启动镜像体积的影响。
- **总线侧的体现**：压缩指令经本讲的 `mem_la_*` 接口流出后，在 AXI4-Lite/Wishbone 上的表现可结合 u7-l1 一起读，理解 `mem_instr`/`arprot` 等信号在压缩取指时是否仍正确。
- **延伸阅读源码**：若想彻底吃透展开表，可逐条对照 RISC-V C 扩展手册，把 [picorv32.v:436-543](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L436-L543) 的每个 `case` 分支与 [picorv32.v:902-1034](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L902-L1034) 的直接解码一一配对，体会「哪些字段需要改写、哪些字段原位复用」的设计取舍。
