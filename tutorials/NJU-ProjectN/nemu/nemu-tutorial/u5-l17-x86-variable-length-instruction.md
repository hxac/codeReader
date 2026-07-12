# x86 变长指令实现对比

## 1. 本讲目标

本讲是 ISA 实现单元（U5）的收尾篇。在 u5-l16 里我们已经以 riscv32 为样板，走过了一条**定长**指令「取 4 字节 → 切字段 → 执行」的完整流程。本讲换成 x86，看 NEMU 如何用**同一套 INSTPAT 框架**去适配一种**变长**指令集。

学完后你应当能够：

- 说清 x86 一条指令由哪些字节拼成（prefix / opcode / ModR/M / SIB / disp / imm），以及它们为何「变长」。
- 读懂 `ModR_M`、`SIB` 两个联合体的位域布局，并能从一字节还原出 mod / reg / R_M、ss / index / base。
- 跟踪 `load_addr` → `decode_rm` 如何把 ModR/M 字节翻译成「寄存器号」或「内存地址」，并理解 `TYPE_G2E` / `TYPE_E2G` 等操作数类型如何把「地址/寄存器分流」做成数据。
- 解释 `0x66` 操作数大小前缀与 `0x0f` 两字节转义如何通过 `goto again` 与 `_2byte_esc` 嵌进主译码循环。
- 对比 riscv32「一次取 4 字节、按位切字段」与 x86「逐字节增量取指、按字节流推进」的译码复杂度差异。

## 2. 前置知识

在进入 x86 之前，先建立两个直觉。

**定长 vs 变长指令集。** RISC-V 是定长指令集：每条指令固定 32 位（4 字节），无论加法、跳转还是访存都一样长。译码时只要把 32 位按固定位置切成 opcode / rd / rs1 / rs2 / funct3 / funct7 / imm 即可，位置和长度都不变。x86 则是变长指令集：一条指令长度从 1 字节到 15 字节不等，长度由指令本身的内容决定——读到什么字节，才知道后面还有多少字节。这就是本讲的根本差异。

**x86 指令的通用格式。** 一条 x86 指令自左向右依次是：

| 字段 | 是否必选 | 作用 |
|------|----------|------|
| 前缀（prefix） | 可选 | 如 `0x66` 改操作数大小、`0x0f` 转义到两字节 opcode |
| opcode | 必选 | 1 字节（少数 2 字节）操作码 |
| ModR/M | 视指令而定 | 1 字节，指明两个操作数：一个寄存器 + 一个「寄存器或内存」 |
| SIB | 视 ModR/M 而定 | 1 字节，仅当 ModR/M 的 R/M=4 时出现，描述 base+index*scale 寻址 |
| 位移（disp） | 视 ModR/M 而定 | 0/1/4 字节，地址偏移量 |
| 立即数（imm） | 视指令而定 | 1/2/4 字节 |

注意这条链是**层层条件依赖**的：是否读 SIB 取决于 ModR/M 的内容；位移多长取决于 ModR/M 的 mod 字段；立即数多长取决于 opcode 和操作数大小。所以 x86 译码只能「读一字节、判断、再读一字节」，无法像 RISC-V 那样一次取完。

**承接关系。** 本讲默认你已读过 u5-l16（riscv32 指令实现）和 u3-l11（INSTPAT 模式匹配机制）。INSTPAT 宏本身的 `pattern_decode` / `key/mask/shift` / 计算跳转原理不再重复，这里只讲它在 x86 上的**接缝**与**落地方式**有何不同。

## 3. 本讲源码地图

本讲聚焦两个文件，并辅以几个支撑头文件：

| 文件 | 作用 |
|------|------|
| `src/isa/x86/inst.c` | x86 译码与执行的全部实现：ModR_M/SIB 解析、`load_addr`、`decode_rm`、操作数类型、`isa_exec_once` 主循环。本讲主战场。 |
| `include/cpu/decode.h` | `Decode` 结构体与 `INSTPAT` 宏定义，是 riscv 与 x86 共用的译码框架。 |
| `src/isa/x86/include/isa-def.h` | x86 的 `CPU_state` 与 `ISADecodeInfo`（含 16 字节指令缓冲 `inst[16]`）。 |
| `src/isa/x86/local-include/reg.h` | `reg_l/reg_w/reg_b` 三个宽度的寄存器访问宏。 |
| `include/cpu/ifetch.h` | `inst_fetch`：取指并推进 PC 的基础函数。 |
| `src/isa/x86/init.c` | 内置镜像 `img[]`，是一段现成的 x86 机器码，正好覆盖前缀、ModR/M、SIB、disp 各种情况，是本讲最好的实验素材。 |
| `src/isa/riscv32/inst.c` | 对照组：定长指令的 `isa_exec_once`。 |

## 4. 核心概念与源码讲解

### 4.1 变长取指与指令字节缓冲

#### 4.1.1 概念说明

riscv32 的 `isa_exec_once` 第一行就是 `s->isa.inst = inst_fetch(&s->snpc, 4);`——一把取 4 字节，存进一个 `uint32_t`。x86 做不到这一点，因为它在取第一个字节之前，根本不知道这条指令一共多长。所以 x86 的取指是**逐字节、增量式**的：先取 1 字节 opcode，译码过程中按需再取 ModR/M、SIB、disp、imm。

这就带来一个新需求：**把一条指令的所有字节缓存下来**，供 itrace（指令追踪）或 iqueue（指令队列）在指令执行完后完整打印。riscv32 把指令存在 `uint32_t inst` 里就够了；x86 需要一个最多 16 字节的字节数组。

#### 4.1.2 核心流程

x86 的取指由 `x86_inst_fetch` 封装，它做两件事：

1. 调用通用的 `inst_fetch(&s->snpc, len)` 取 `len` 字节并把 `snpc` 前推 `len`。
2. 若开启了 `CONFIG_ITRACE` 或 `CONFIG_IQUEUE`，把刚取到的字节按小端逐字节拷进 `s->isa.inst[]` 缓冲，偏移量就是 `s->snpc - s->pc`（即「已经取了多少字节」）。

由于每次取指都把字节追加到缓冲里，等整条指令译码完成时，`inst[0..ilen-1]` 就正好是这条指令的完整机器码（`ilen = snpc - pc`）。缓冲大小为 16，对应 x86 单条指令最长 15 字节的规格，并用一个 `assert` 兜底。

#### 4.1.3 源码精读

先看缓冲的定义。x86 的 `ISADecodeInfo` 用 `uint8_t inst[16]` 取代了 riscv32 的 `uint32_t inst`，多出来的 `p_inst` 指针供 itrace/iqueue 模块使用：

[src/isa/x86/include/isa-def.h:43-46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L43-L46) —— x86 译码信息：16 字节指令缓冲 + 指针，是变长指令「边取边存」的基础。

再看取指封装本身：

[src/isa/x86/inst.c:43-58](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L43-L58) —— `x86_inst_fetch`：在 `inst_fetch` 之上叠加「按字节拷进 `s->isa.inst[]`」的逻辑，偏移取 `s->snpc - s->pc`，并用 `assert` 保证不超过 16 字节。

注意 `#if defined(CONFIG_ITRACE) || defined(CONFIG_IQUEUE)` 这层条件编译：若两个都没开，`x86_inst_fetch` 退化为直接返回 `inst_fetch`，零额外开销。这与 riscv32「总是把 4 字节存进 `inst`」不同——x86 的缓冲纯粹是为追踪服务的可选设施。

对比 riscv32 的取指，差异一目了然：

[src/isa/riscv32/inst.c:75-78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78) —— riscv32 `isa_exec_once`：一次性 `inst_fetch(&s->snpc, 4)`，固定 4 字节，取完即可整字切字段。

#### 4.1.4 代码实践

**实践目标**：直观看到 x86「逐字节取指」与 riscv32「一次取 4 字节」的区别。

**操作步骤**：

1. 用 `make menuconfig` 把 ISA 选成 `x86`，在 `[*] Debugging` 里打开 `ITRACE`（指令追踪）。
2. `make` 编译，产物为 `build/x86-nemu-interpreter`。
3. `make run`（或 `./build/x86-nemu-interpreter`）跑内置镜像，它会执行 `src/isa/x86/init.c` 里的 `img[]`，最后命中 `0xcc`（nemu_trap）退出。
4. 打开 `build/nemu-log.txt`，查看 itrace 打印的每条指令字节。
5. 切回 `riscv`，同样开 ITRACE 重编重跑，对比日志。

**需要观察的现象**：x86 的日志里每条指令字节数不同（1、2、5、6、10 字节都有）；riscv32 的日志里每条指令都是 4 字节。

**预期结果**：x86 一条 `mov $0x1234,%eax` 是 `b8 34 12 00 00`（5 字节），而 `int3` 是 `cc`（1 字节）；riscv32 无论 auipc 还是 ebreak 都是 4 字节。若日志未生成，检查 ITRACE 是否真的开启、`ARGS` 里 `--log` 是否指向了 `nemu-log.txt`。本步骤的日志路径与开关行为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 x86 的 `ISADecodeInfo` 用 `uint8_t inst[16]` 而不是 `uint32_t inst`？

**答案**：因为 x86 指令长度不固定（1~15 字节），无法用一个定宽整数装下；而 riscv32 指令恒为 4 字节，一个 `uint32_t` 就够。

**练习 2**：`x86_inst_fetch` 里 `assert(s->snpc - s->pc < sizeof(s->isa.inst))` 的意义是什么？

**答案**：`snpc - pc` 是当前已取的字节数，若达到或超过 16，说明这条指令异常长（超过 x86 规定的 15 字节上限，多半是译码逻辑把不该当指令的字节当成了指令），用断言尽早暴露问题。

### 4.2 ModR_M 与 SIB 字节解析

#### 4.2.1 概念说明

x86 大多数双操作数指令用一个 **ModR/M 字节**来同时指明两个操作数：

- `mod`（2 位）：寻址模式。`mod=3` 表示 R/M 字段是一个寄存器；`mod=0/1/2` 表示 R/M 字段是一个内存地址，区别在于位移 disp 的长度（0/1/4 字节）和是否有特殊规则。
- `reg`（3 位）：一个寄存器操作数（也可被复用为 opcode 扩展，见 4.4）。
- `R/M`（3 位）：另一个操作数，是寄存器还是内存由 `mod` 决定；若是内存，R_M 给出基址寄存器。

当 `R/M == 4`（即 `R_ESP`）且 `mod != 3` 时，光靠 ModR/M 不够描述寻址——需要一个 **SIB（Scale-Index-Base）字节**来表达 `base + index*scale` 形式的地址。SIB 的三个字段是：

- `scale`（2 位，代码里叫 `ss`）：比例因子，取 1/2/4/8。
- `index`（3 位）：变址寄存器号；`index==4`（`R_ESP`）表示「没有变址」。
- `base`（3 位）：基址寄存器号。

NEMU 用 C 位域联合体 `ModR_M`、`SIB` 来解析这两个字节，把一个 `uint8_t` 同时看成「整体值」和「字段拆分」两种视图。

#### 4.2.2 核心流程

ModR/M 的解析流程：

1. 取 1 字节，存进 `ModR_M.val`。
2. 读 `m->mod`：若为 3，R/M 是寄存器；否则 R/M 是内存，进入 `load_addr`。
3. 读 `m->reg`：得到 reg 操作数寄存器号。
4. 读 `m->R_M`：得到另一个操作数的寄存器号或基址寄存器号。

SIB 只在 `load_addr` 内部、`R_M==4` 时取一字节解析。位域联合体的好处是：取字节后无需手写移位掩码，`m->mod`、`sib.base` 等字段直接可读。

注意位域布局受「小端 + 低位在前」约束。`ModR_M` 把 `R_M` 放在最低 3 位、`reg` 在中间 3 位、`mod` 在最高 2 位，正好对应 x86 字节里「低 3 位 = R/M，中 3 位 = reg，高 2 位 = mod」的编码。

#### 4.2.3 源码精读

[src/isa/x86/inst.c:21-32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L21-L32) —— `ModR_M` 联合体：同一字节既可作 `val` 整体取，也可按 `R_M/reg/mod` 或 `dont_care/opcode` 两种位域视图取。

[src/isa/x86/inst.c:34-41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L34-L41) —— `SIB` 联合体：`base/index/ss` 三字段，描述 `base + index*2^ss` 寻址。

这里有个 C 语言细节值得提醒：位域的内存布局是**实现定义**的，NEMU 依赖 GCC 在小端机器上「位域从低位分配」的约定，所以这套代码换到大端机或某些编译器可能失效。教学模拟器假定小端宿主机，这与 u4-l12 里 `host_read` 隐含的小端假设一脉相承。

#### 4.2.4 代码实践

**实践目标**：用内置镜像里的真实字节，练手 ModR/M 与 SIB 的手工解析。

**操作步骤**：看 `src/isa/x86/init.c` 的 `img[]` 第二条带 ModR/M 的指令：

[src/isa/x86/init.c:20-30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/init.c#L20-L30) —— 内置镜像，每行注释即反汇编结果。

以 `0x89, 0x01`（`movl %eax,(%ecx)`）为例：

1. `0x89` 是 opcode（mov G2E，宽度 4）。
2. `0x01 = 0b00_000_001` → `mod=0, reg=0(eax), R_M=1(ecx)`。
3. `mod=0`、`R_M=1`（不是 4/5）→ 无 SIB、无 disp，地址就是 `reg_l(ecx)`。
4. 所以语义是 `*(uint32_t*)ecx = eax`，与注释 `movl %eax,(%ecx)` 吻合。

**需要观察的现象**：你能在不运行的情况下，从两个字节还原出「源是 eax、目的是内存 [ecx]、无位移」。

**预期结果**：手工解析与注释一致。再挑战 `0x66,0xc7,0x84,0x99,...`（带 SIB 的那条）：`0x84=0b10_000_100` → `mod=2,R_M=4`，故取 SIB `0x99=0b10_011_001` → `ss=2(scale4),index=3(ebx),base=1(ecx)`，`mod=2` → disp32。结果应是 `-0x2000(%ecx,%ebx,4)`，与注释一致。

#### 4.2.5 小练习与答案

**练习 1**：ModR/M 字节 `0xC0`（`0b11_000_000`）表示什么？

**答案**：`mod=3`，意味着 R/M 字段是一个寄存器而非内存；`reg=0`、`R_M=0`，两个操作数都是 0 号寄存器（如 eax）。这类指令不访存，也不需要 SIB/disp。

**练习 2**：SIB 的 `index==4` 为何被特判为「无变址」？

**答案**：因为 `R_M==4` 本身已被用来表示「需要 SIB」，若 SIB 的 index 也用 4 号寄存器就会和「ESP 不能作变址」的编码冲突；x86 规定 SIB 中 `index==4` 表示没有变址寄存器，从而腾出编码空间。本讲源码里对应 `if (sib.index != R_ESP) { index_reg = sib.index; }`。

### 4.3 load_addr 与 decode_rm：地址计算与寄存器/内存分流

#### 4.3.1 概念说明

ModR/M 字节只给出寻址的「骨架」（mod + R_M），真正算出内存地址还要处理 SIB、位移、特殊规则。`load_addr` 专门干这件事：把 ModR/M（以及可能的 SIB、disp）算成一个线性地址。

但很多指令只关心「这个操作数到底是寄存器还是内存」——若是寄存器，直接读写寄存器号；若是内存，才算地址并访存。`decode_rm` 把这层判断封成统一入口：它取一字节 ModR/M，若 `mod==3` 就把 R_M 当寄存器号返回，否则调用 `load_addr` 算地址并标记「这是内存」。

这两者合在一起，实现了 x86 译码里最关键的一步：**把 ModR/M 字节流翻译成「寄存器号」或「内存地址」二选一的操作数**。

#### 4.3.2 核心流程

`load_addr` 计算地址的流程（前提 `mod != 3`）：

1. 决定 base 与 index：
   - 若 `R_M == R_ESP`：取 SIB 字节，`base = sib.base`，`scale = sib.ss`，若 `sib.index != R_ESP` 则 `index = sib.index`，否则无 index。
   - 否则：`base = R_M`，无 SIB、无 index。
2. 决定 disp 长度：
   - `mod==0`：默认 disp=0；但若 `base == R_EBP`（且无 SIB 时 R_M==5），则 base 失效、disp 改为 4 字节（这是 x86「mod=0 + R_M=5 表示 disp32 无基址」的特殊规则）。
   - `mod==1`：disp 1 字节（带符号）。
   - `mod==2`：disp 4 字节。
3. 若有 disp，按长度取并符号扩展（1 字节时转 `int8_t`）。
4. 地址 = disp + (base 有效 ? reg_l(base) : 0) + (index 有效 ? reg_l(index) << scale : 0)。

写成公式：

\[
\text{addr} = \text{disp} + (\text{base}\neq -1\;?\;\text{reg\_l}(\text{base}) : 0) + (\text{index}\neq -1\;?\;\text{reg\_l}(\text{index})\ll \text{scale} : 0)
\]

`decode_rm` 的流程很简单：取 ModR/M → 填 `reg` → 若 `mod==3` 把 R_M 写进 `rm_reg`（标记寄存器），否则 `load_addr` 算 `rm_addr` 并把 `rm_reg` 置 `-1`（标记内存）。

#### 4.3.3 源码精读

[src/isa/x86/inst.c:78-110](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L78-L110) —— `load_addr`：依次处理 SIB、disp 长度、符号扩展，最后把 base/index/disp 汇总成 `*rm_addr`。注意 `assert(m->mod != 3)`：调用方负责保证只在内存模式下进入。

[src/isa/x86/inst.c:112-118](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L112-L118) —— `decode_rm`：取 ModR/M，按 `mod==3` 分流到「寄存器」或「`load_addr` 算内存地址」，用 `rm_reg=-1` 作为「这是内存」的哨兵。

「寄存器或内存」二选一的语义被一组小宏固化下来，让执行体不必关心操作数到底落在哪：

[src/isa/x86/inst.c:120-130](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L120-L130) —— `Rr/Rw`（寄存器读写）、`Mr/Mw`（内存读写）、`RMr`（读 r/m 操作数：寄存器或内存）、`RMw`（写 r/m 操作数）。`RMw` 用 `rd != -1` 判断写寄存器还是写内存。

`reg_read`/`reg_write` 按宽度（4/1/2）分派到 `reg_l/reg_b/reg_w`：

[src/isa/x86/inst.c:60-76](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L60-L76) —— 宽度分派。`reg_b` 的索引 `cpu.gpr[idx & 0x3]._8[idx >> 2]` 体现了 x86 字节寄存器的奇怪编码：0~3 是 AL/CL/DL/BL（低字节），4~7 是 AH/CH/DH/BH（高字节），都挤在前 4 个寄存器里。

#### 4.3.4 代码实践

**实践目标**：跟踪一条带 SIB + disp32 的访存指令，验证 `load_addr` 的地址计算。

**操作步骤**：

1. 在 `load_addr` 末尾 `*rm_addr = addr;` 前临时加一行 `printf("load_addr: base=%d index=%d scale=%d disp=0x%x addr=0x%x\n", base_reg, index_reg, scale, disp, addr);`（这是**示例代码**，仅为观察，验证后请删掉，勿提交）。
2. 以 x86 编译并 `make run`。
3. 观察针对 `0x66,0xc7,0x84,0x99,0x00,0xe0,0xff,0xff,...`（`movw $0x1,-0x2000(%ecx,%ebx,4)`）那行打印的值。

**需要观察的现象**：`base=1(ecx) index=3(ebx) scale=2(即 4) disp=0xffffe000(-0x2000)`，最终 `addr = ecx + ebx*4 - 0x2000`。

**预期结果**：打印的 `addr` 满足 `addr == reg_l(1) + (reg_l(3) << 2) + (int32_t)0xffffe000`。若 `disp` 打印成大正数（如 `0xffffe000`），那是 `sword_t` 按 `%x` 打印的结果，其算术值仍是 -8192。该 printf 输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`decode_rm` 用 `rm_reg = -1` 表示「内存操作数」，为什么不用一个单独的 bool？

**答案**：因为寄存器号本身是非负的（0~7），用 `-1` 这个不可能取到的值当哨兵，能把「是寄存器还是内存」和「寄存器号是多少」合并进一个变量，下游 `RMr`/`RMw` 只需 `reg != -1` 一次判断即可分流，省一个状态位。

**练习 2**：`mod==0` 且 `R_M==5`（`R_EBP`）时，为何 `base_reg` 被置 -1、disp 变成 4 字节？

**答案**：这是 x86 的编码约定——`mod=0 + R_M=5` 专门用来表示「只有 32 位位移、无基址」的绝对地址寻址，否则 `R_M=5` 本应指 ebp。代码用 `if (base_reg == R_EBP) { base_reg = -1; }`（此时 `disp_size` 保持默认 4）实现了这条特殊规则。

### 4.4 操作数大小前缀（0x66）与两字节转义（0x0f）

#### 4.4.1 概念说明

x86 用**前缀字节**在不新增 opcode 的前提下改变一条指令的行为。本讲关注两种最常见的前缀：

- **操作数大小前缀 `0x66`**：把默认 32 位操作数改成 16 位（或反之）。例如 `0xc7` 默认是 `mov r/m32, imm32`，前面加 `0x66` 就变成 `mov r/m16, imm16`。这意味着同一个 opcode 因前缀不同而走不同宽度。
- **两字节转义 `0x0f`**：x86 单字节 opcode 不够用，`0x0f` 表示「真正的 opcode 在下一字节」。例如 `0x0f 0xb6` 是 `movzx`。NEMU 把这两类前缀都做成了主译码循环里的「特殊 opcode」。

#### 4.4.2 核心流程

`isa_exec_once` 用一个 `again:` 标签 + `goto again` 来处理前缀：

1. 进入函数，`is_operand_size_16 = false`。
2. `again:` 取 1 字节 opcode。
3. 若是 `0x66`（`data_size` 模式）：把 `is_operand_size_16` 置 true，`goto again`——回到第 2 步继续取下一个字节。这样 `0x66` 后面跟的 opcode 译码时就知道要用 16 位。
4. 若是 `0x0f`（`2byte_esc` 模式）：调用 `_2byte_esc(s, is_operand_size_16)`，它再取 1 字节作为「第二字节 opcode」并在自己的 INSTPAT 块里译码。
5. 否则是普通单字节 opcode，走主 INSTPAT 块。

`is_operand_size_16` 如何影响宽度？在 `INSTPAT_MATCH` 里，`w = width == 0 ? (is_operand_size_16 ? 2 : 4) : width`。即：若指令模式声明 `width=0`（「用默认宽度」），则 `w` 由 `is_operand_size_16` 在 4 与 2 间二选一；若声明了具体宽度（如 `1`），则前缀不影响（字节操作不受 0x66 影响）。

#### 4.4.3 源码精读

[src/isa/x86/inst.c:186-220](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L186-L220) —— `isa_exec_once` 主循环：`again` 标签、`0x0f` 转义、`0x66` 前缀 `goto again`、各 mov 变体、`0xcc` nemu_trap、`inv` 兜底。

聚焦前缀与转义这两行：

[src/isa/x86/inst.c:195-197](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L195-L197) —— `0x0f` 调 `_2byte_esc`，`0x66` 置标志后 `goto again`。注意 `goto again` 会重新取下一字节 opcode，所以「`0x66` + opcode」被拆成两次循环迭代处理。

`_2byte_esc` 本身是主循环的「迷你版」，有自己独立的 INSTPAT 块（当前只放了 `inv` 兜底，留作扩展）：

[src/isa/x86/inst.c:179-184](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L179-L184) —— `_2byte_esc`：取第二字节 opcode，在自己的 INSTPAT 块里译码。它接收 `is_operand_size_16` 以保证前缀在两字节指令里仍生效。

宽度由前缀决定的逻辑藏在 `INSTPAT_MATCH`：

[src/isa/x86/inst.c:150-157](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L150-L157) —— `INSTPAT_MATCH`：`w = width==0 ? (is_operand_size_16?2:4) : width`，把「默认宽度」与「前缀」耦合在一起。`s->dnpc = s->snpc` 把顺序下一条 PC 设为已取字节的末尾（跳转指令再改写 dnpc）。

内置镜像正好提供了 `0x66` 前缀的真实例子：

[src/isa/x86/init.c:24-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/init.c#L24-L27) —— `0x66,0xc7,...` 即 `movw $0x1,0x4(%ecx)`：`0x66` 把 `0xc7`（默认 mov r/m32,imm32）降为 16 位，立即数随之只取 2 字节 `0x01,0x00`。

#### 4.4.4 代码实践

**实践目标**：验证 `0x66` 前缀确实把 32 位 mov 降为 16 位。

**操作步骤**：

1. 在 `INSTPAT_MATCH` 的 `int w = ...` 行后临时加 `printf("opcode=%02x w=%d\n", opcode, w);`（**示例代码**，验证后删除）。
2. 以 x86 编译并 `make run`。
3. 对照 `img[]`，定位 `0xc7`（不带前缀，应为 `w=4`）与 `0x66,0xc7`（带前缀，应为 `w=2`）两次打印。

**需要观察的现象**：同一 opcode `0xc7`，无前缀时打印 `w=4`，前面有 `0x66` 时打印 `w=2`。

**预期结果**：证实 `is_operand_size_16` 经 `goto again` 跨循环迭代传递，并被 `INSTPAT_MATCH` 折算成 `w=2`。输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`0x66` 前缀为什么用 `goto again` 而不是直接在当前迭代里继续取字节？

**答案**：因为 `0x66` 后面跟的还是一个普通 opcode，复用 `again:` 的取指与 INSTPAT 分发逻辑最简洁；`goto again` 等价于「丢掉这一字节、把标志置上、重新走一遍取 opcode」，避免重复写一套译码。同时 `is_operand_size_16` 是函数局部变量，跨迭代保持有效。

**练习 2**：为什么字节宽度的指令（如 `0x88` mov r/m8, r8，`width=1`）不受 `0x66` 影响？

**答案**：`INSTPAT_MATCH` 里 `w = width==0 ? ... : width`，当模式声明 `width=1` 时直接用 1，不看 `is_operand_size_16`。这符合 x86 语义：`0x66` 只在「默认操作数大小」（16/32）之间切换，不影响固定 8 位的字节操作。

### 4.5 INSTPAT 在 x86 上的落地与操作数类型

#### 4.5.1 概念说明

INSTPAT 机制本身（`pattern_decode` 把 `"1000 1000"` 编译成 key/mask/shift，运行时 `(inst>>shift)&mask==key` 比对）是 ISA 无关的，由 u3-l11 详述。本讲只看 x86 的**两个接缝**与**操作数类型**如何落地。

接缝一：`INSTPAT_INST(s)`——「拿什么去和模式串比」。riscv32 里它是 `(s)->isa.inst`（整条 32 位指令字）；x86 里它是 `opcode`——只比当前取到的那个 opcode 字节，因为 x86 是逐字节译码，每个 INSTPAT 项匹配的是 1 字节 opcode。

接缝二：`INSTPAT_MATCH`——「匹配后做什么」。riscv32 里它切 32 位字的字段并执行；x86 里它先按操作数类型 `TYPE_*` 调 `decode_operand` 把字节流解析成 `rd/src1/addr/rs/imm`，再执行体。

操作数类型是 x86 译码的「数据驱动」精华：把「这条指令的两个操作数分别从哪取」编码成一个枚举值，`decode_operand` 用 switch 把枚举翻译成对 `decode_rm`/`imm` 等的调用。于是新增一条指令往往只是「选一个 TYPE + 写执行体」，而不必每次重写取指逻辑。

#### 4.5.2 核心流程

x86 一条指令的译码执行流程：

1. `isa_exec_once` 取 1 字节 opcode（含前缀的 `goto again` / `0x0f` 转义）。
2. 进入 INSTPAT 块，逐项用 `(opcode>>shift)&mask==key` 比对模式串。
3. 命中某项 → `INSTPAT_MATCH` 展开：
   - 计算 `w`（默认宽度受 `0x66` 影响）。
   - `decode_operand(s, opcode, ..., TYPE)` 按 TYPE 解析操作数。例如 `TYPE_G2E` 调 `decode_rm` 把 E（r/m）放进 `rd/addr`、把 G（reg）放进 `rs` 并读入 `src1`；`TYPE_I2r` 直接从 opcode 低 3 位取寄存器号、再取立即数。
   - `s->dnpc = s->snpc`（默认顺序执行）。
   - 执行体（如 `RMw(src1)` 把 src1 写进 E 操作数）。
   - `goto` 跳到 INSTPAT 块尾，结束本条指令。
4. 全不命中 → `inv` 兜底 → `INV(s->pc)` → `invalid_inst` 打印诊断并 `NEMU_ABORT`。

#### 4.5.3 源码精读

x86 的两个接缝定义：

[src/isa/x86/inst.c:149-157](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L149-L157) —— `INSTPAT_INST(s)=opcode`（只比 1 字节 opcode）、`INSTPAT_MATCH` 调 `decode_operand` 后执行体。对比 riscv32 的 `INSTPAT_INST(s)=((s)->isa.inst)`（整字），差异正是定长/变长的分野。

操作数类型枚举（节选）：

[src/isa/x86/inst.c:132-147](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L132-L147) —— `TYPE_G2E`（Eb<-Gb / Ev<-Gv）、`TYPE_E2G`（Gb<-Eb / Gv<-Ev）、`TYPE_I2r`（XX<-Ib / eXX<-Iv）、`TYPE_I2E`（Eb<-Ib / Ev<-Iv）、`TYPE_O2a`/`TYPE_a2O`（AL/eAX 与绝对地址互传）等。命名约定 `X2Y` 表示「从 X 传到 Y」，`E`=r/m 操作数、`G`=reg 字段、`I`=立即数、`a`=累加器、`O`=绝对地址偏移。

`decode_operand` 用 switch 把 TYPE 翻译成具体取数动作：

[src/isa/x86/inst.c:159-171](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L159-L171) —— `decode_operand`：`TYPE_G2E` 调 `decode_rm(s, rd_, addr, rs, w)` 后 `src1r(*rs)`（E 作目的 r/m、G 作源 reg）；`TYPE_E2G` 反过来（G 作目的、E 作源）；`TYPE_I2r` 从 `opcode&0x7` 取寄存器号并取立即数。

把这些串起来看主循环里的 mov 家族：

[src/isa/x86/inst.c:200-214](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L200-L214) —— 一组 mov 变体：`0x88/0x89`（G2E，reg→r/m）、`0x8a/0xb`（E2G，r/m→reg）、`0xb0?/0xb1?`（I2r，立即数→reg）、`0xc6/0xc7`（I2E，立即数→r/m）。每行只是「opcode 模式 + TYPE + 宽度 + 极短执行体」，复杂度被 `decode_operand` 与 `RMw/RMr` 宏吸收。

注意 INSTPAT 块的写法——x86 与 riscv32 都调用 `INSTPAT_START()` / `INSTPAT_END()` 但**不传 name 参数**：

[include/cpu/decode.h:99-100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L99-L100) —— `INSTPAT_START(name)`/`INSTPAT_END(name)` 用 `name` 区分同函数内多个块的跳转标号；调用时传空，`concat(__instpat_end_, )` 展开为 `__instpat_end_`。x86 的 `isa_exec_once` 与 `_2byte_esc` 分属不同函数，各有一个空名块，标号互不冲突。

兜底与陷阱：

[src/isa/x86/inst.c:215-216](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L215-L216) —— `0xcc` 复用为 `nemu_trap`（与 riscv32 的 `ebreak` 同思路，返回值取自 `eax`），`"???? ????"` 是 `inv` 兜底，必须放最后。

`INV` 与 `NEMUTRAP` 都是 `set_nemu_state` 的薄封装：

[include/cpu/cpu.h:26-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L26-L27) —— `NEMUTRAP` 置 `NEMU_END`（正常结束），`INV` 调 `invalid_inst` 走 `NEMU_ABORT`（异常中止）。两者的状态机语义见 u7-l23。

#### 4.5.4 代码实践

**实践目标**：用 INSTPAT 新增一条 x86 指令，走通「加模式项 + 选 TYPE + 写执行体」的全流程。

**操作步骤**：为 x86 实现 `movzx Gv, Eb`（opcode `0x0f 0xb6`，把 8 位源零扩展到 32/16 位目的寄存器）。它属于两字节指令，应加进 `_2byte_esc`。由于现有 `decode_operand` 没有 ready 的类型，用 `TYPE_N`（不解码）并在执行体里手动调 `decode_rm`。在 `_2byte_esc` 的 `inv` 之前插入（**示例代码**）：

```c
// 示例代码：movzx Gv, Eb  (0x0f 0xb6)
INSTPAT("1011 0110", movzx, N, 0,
  decode_rm(s, &rs, &addr, &rd, w);   // rd=reg(目的G), rs=r/m(源E)
  Rw(rd, w, RMr(rs, 1));              // 源按1字节读(自动零扩展), 目的按w写
);
```

说明：`rs/addr/rd` 是 `INSTPAT_MATCH` 已声明的局部变量；`decode_rm(s, &rs, &addr, &rd, w)` 把 reg 字段放进 `rd`（目的）、r/m 放进 `rs`（源）；`RMr(rs, 1)` 读源为 1 字节（`reg_read` 返回 `word_t`，高位为 0，即零扩展），`Rw(rd, w, ...)` 写目的。

**需要观察的现象**：构造一段测试机器码 `0x0f,0xb6,0xc0`（`movzx eax, al`），单步执行后 `eax` 应等于原 `al` 的值且高 24 位为 0。可用 SDB 的 `si 1` 后 `info r`（需先实现 `isa_reg_display`，见 u5-l15）或 `p $eax` 观察。

**预期结果**：若 `al=0xff` 而 `eax` 原为 `0x123456ff`，执行 `movzx eax,al` 后 `eax=0x000000ff`。由于 `info r`/`p` 依赖你在 PA1 的实现，本步骤**待本地验证**；若尚未实现，可改为在执行体里 `printf` 打印 `rd` 与读写值来观察。

#### 4.5.5 小练习与答案

**练习 1**：x86 的 `INSTPAT_INST(s)` 为何是 `opcode` 而不是整条指令？

**答案**：因为 x86 指令变长且逐字节译码，INSTPAT 块在主循环里匹配的只是「当前这一字节的 opcode」；ModR/M、SIB、disp、imm 是匹配成功后才在 `decode_operand`/`decode_rm` 里按需取的，不属于 opcode 模式匹配的范畴。riscv32 没有这个问题，所以用整条 `inst` 一次性匹配。

**练习 2**：`TYPE_G2E` 与 `TYPE_E2G` 在 `decode_rm` 的参数顺序上有何区别？

**答案**：`TYPE_G2E` 调 `decode_rm(s, rd_, addr, rs, w)`——r/m（E）放进 `rd_`/`addr` 作目的，reg（G）放进 `rs` 作源；`TYPE_E2G` 调 `decode_rm(s, rs, addr, rd_, w)`——r/m（E）放进 `rs` 作源，reg（G）放进 `rd_` 作目的。同一个 `decode_rm`，靠参数顺序区分方向，是 x86 译码复用代码的精妙之处。

**练习 3**：若新加的 INSTPAT 项放在 `"???? ????" inv` 之后，会发生什么？

**答案**：永远不会被命中。因为 `"???? ????"` 匹配任意 1 字节 opcode，且 INSTPAT 是自上而下首个命中即 `goto` 跳出，所以兜底项之后的所有模式都不可达。新指令必须放在 `inv` 之前——这与 u3-l11 的结论一致。

## 5. 综合实践

把本讲内容串起来，完成一个「定长 vs 变长」的对比小任务。

**任务**：在同一台机器上分别用 riscv32 与 x86 跑通内置镜像，量化两者的取指差异，并为 x86 增补一条指令。

**步骤**：

1. **riscv32 侧**：`make menuconfig` 选 `riscv`，开 ITRACE，`make && make run`。从 `build/nemu-log.txt` 抽取若干条 itrace 行，记录每条指令的字节长度。
2. **x86 侧**：切到 `x86`，开 ITRACE，`make && make run`。同样抽取 itrace 行。
3. **对比**：填一张表，统计两边「最长/最短指令字节」「是否需要 ModR/M/SIB/disp」「取指次数」。
4. **读源码**：对照 [src/isa/riscv32/inst.c:75-78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78)（一次取 4 字节）与 [src/isa/x86/inst.c:186-220](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/inst.c#L186-L220)（取 1 字节 opcode 后按需追加），写出两者取指调用次数的差异。
5. **增补指令**：按 4.5.4 的示例，在 `_2byte_esc` 里实现 `movzx Gv, Eb`（`0x0f 0xb6`），并顺手加 `movsx Gv, Eb`（`0x0f 0xbe`，把 `RMr(rs,1)` 换成 `SEXT(RMr(rs,1), 8)`，`SEXT` 见 [include/macro.h:88](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L88)）。
6. **验证**：构造最小测试字节序列（如 `0x0f,0xb6,0xc0`），单步执行并查看寄存器，确认零扩展/符号扩展正确。

**预期产出**：

- 一张对比表，能看出 riscv32 指令恒 4 字节、取指 1 次；x86 指令 1~10+ 字节、取指 1~5 次。
- 一段总结：定长 ISA 译码是「按位切字段」，逻辑线性、易实现；变长 ISA 译码是「按字节流推进 + 层层条件分支」，复杂度随前缀/ModR/M/SIB/disp 的组合爆炸，但表达力更强、代码密度更高。
- movzx/movsx 两条新指令可在 x86 上单步验证通过（若寄存器查看命令未实现，则以执行体内 `printf` 输出为证）。

> 提示：本实践涉及修改 `src/isa/x86/inst.c`。本讲义要求「不修改源码」仅指生成讲义过程中不得改动；作为学习者的练习，你按 PA 约定修改自己的仓库是正常且预期的。所有运行结果在未实际执行前均**待本地验证**。

## 6. 本讲小结

- x86 是**变长**指令集，一条指令由 prefix/opcode/ModR/M/SIB/disp/imm 串接而成，长度 1~15 字节，是否出现各字段层层条件依赖，只能逐字节增量取指。
- `x86_inst_fetch` 在通用 `inst_fetch` 之上把字节追加进 `s->isa.inst[16]` 缓冲，供 itrace/iqueue 完整记录一条指令；`assert` 保证不超过 16 字节上限。
- `ModR_M`/`SIB` 用 C 位域联合体把一字节同时看成整体值与字段拆分，对应 mod/reg/R_M 与 ss/index/base；布局依赖小端宿主机约定。
- `load_addr` 把 ModR/M(+SIB+disp) 算成线性地址，`decode_rm` 用 `mod==3` 把操作数分流为「寄存器号」或「内存地址」，并用 `-1` 作内存哨兵，配合 `RMr/RMw` 宏让执行体与操作数落点解耦。
- `0x66` 操作数大小前缀经 `goto again` 跨迭代置 `is_operand_size_16`，在 `INSTPAT_MATCH` 里把默认宽度从 4 折为 2；`0x0f` 两字节转义调 `_2byte_esc` 取第二字节 opcode，在自己的 INSTPAT 块里译码。
- INSTPAT 在 x86 的接缝是 `INSTPAT_INST(s)=opcode`（只比 1 字节 opcode）与 `INSTPAT_MATCH` 调 `decode_operand`；操作数类型 `TYPE_G2E/E2G/I2r/I2E/...` 把「操作数从哪取」做成数据，新增指令只需「选 TYPE + 写执行体」。
- 与 riscv32 对比：定长 = 一次取 4 字节 + 按位切字段，逻辑线性；变长 = 逐字节取 + 条件分支组合，复杂度高但代码密度高。两者共用同一套 INSTPAT 框架，差异全在 ISA 接缝。

## 7. 下一步学习建议

- **进入系统机制**：x86/riscv32 的 `isa_mmu_check` 目前都恒返回 `MMU_DIRECT`（见 [src/isa/x86/include/isa-def.h:52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/include/isa-def.h#L52)）。下一阶段 U7 会讲分页 MMU 地址翻译（u7-l22）与中断异常（u7-l21），届时 vaddr 层的 `isa_mmu_check`/`isa_mmu_translate` 与 `isa_raise_intr`/`isa_query_intr` 才真正发力，建议先回顾 u4-l13 的 MMU 接口铺垫。
- **补全 x86 指令**：本讲只实现了 mov 家族与 movzx/movsx。可参考真实 i386 手册，按 `TYPE_*` 体系扩充算术、逻辑、栈、跳转类指令，体会「数据驱动的操作数类型」如何让新增指令规模化。
- **追踪与差分测试**：实现更多指令后，建议结合 u8-l24（差分测试）以 QEMU/spike 为 REF 验证正确性，并用 u8-l25 的 itrace + capstone 反汇编定位出错指令——这正是 `x86_inst_fetch` 填充 `inst[16]` 缓冲的用武之地。
- **对照阅读 mips32/loongarch32r**：NEMU 还内置了这两种 ISA（见 Kconfig），它们的定长译码与 riscv32 同构，可作对照练习，进一步理解「框架不变、ISA 接缝切换」的设计。
