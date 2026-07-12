# RISC-V 指令实现

## 1. 本讲目标

本讲是 ISA 实现单元（U5）的第三篇，承接 u5-l14（ISA 抽象与 `CPU_state`）和 u5-l15（寄存器实现）。前两讲回答了「寄存器长什么样、怎么访问」；本讲要回答最关键的一步：**一条 RISC-V 指令，从内存里取出来之后，到底是怎么被识别、解码并执行写回的**。

NEMU 在 `src/isa/riscv32/inst.c` 里只给出了 4 条指令的完整实现——`auipc`、`lbu`、`sb`、`ebreak`——它们恰好构成内置自检镜像（u5-l15 见过的那段 `img[]`）所需的最小集合。这 4 条指令就是「样板」：看懂它们，你就掌握了用 `INSTPAT` 添加任意新指令的全部套路。

读完本讲，你应当能够：

- 说清 `isa_exec_once` 如何取 4 字节指令并把控制权交给 `decode_exec`，以及它为何只动 `snpc`、不碰 `dnpc`。
- 看懂 `decode_operand` 如何按 `TYPE_I/U/S` 三种类型，从 32 位指令字里切出 `rd/rs1/rs2` 与立即数 `imm`。
- 手算 `immI/immU/immS` 三个立即数宏的值，并解释 `SEXT`（符号扩展）为什么是 RISC-V 立即数处理的灵魂。
- 用 `INSTPAT` 写出一条完整指令的「模式串 + 执行体」，并知道 `INSTPAT_INST`/`INSTPAT_MATCH` 这两个 ISA 侧接缝的作用。
- 解释 `decode_exec` 末尾那句 `R(0) = 0` 在维护「x0 恒为零」不变量时的兜底意义。
- 说清 `ebreak` 如何被复用为 `nemu_trap`、返回值为何取自 `a0`（即 `R(10)`），以及它如何最终变成屏幕上的 `HIT GOOD TRAP` / `HIT BAD TRAP`。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。本讲默认你已读过 u3-l11（`INSTPAT` 模式匹配机制）、u5-l15（寄存器实现）和 u4-l13（vaddr 与 MMU 接口）。

- **定长指令**：RISC-V 标准 32 位指令集（RV32I）的每条指令都是固定 4 字节。这与 x86 的「1~15 字节变长」形成鲜明对比（x86 留到 u5-l17）。定长带来的好处是：取指只需固定读 4 字节、`ilen` 恒为 4、`snpc = pc + 4`，译码器不必「边取边决定还要不要继续取」。
- **指令字段（instruction fields）**：一条 32 位指令并非一串无意义的比特，而是按固定位置切分成若干字段。RISC-V 把 32 位从高位到低位切成 7 段：`funct7 | rs2 | rs1 | funct3 | rd | opcode`（不同类型略有增减）。`opcode`（低 7 位）决定指令的大类与格式，`funct3`/`funct7` 进一步区分同大类里的具体操作，`rd/rs1/rs2` 是寄存器编号字段。
- **指令格式（I/U/S/R/B/J）**：RISC-V 按立即数摆放方式把指令分成几种格式。本讲涉及三种：
  - **I-type**（立即数型）：`imm[11:0] | rs1 | funct3 | rd | opcode`，用于 `lbu`/`jalr` 等「寄存器 + 12 位立即数」的指令。
  - **U-type**（上位立即数型）：`imm[31:12] | rd | opcode`，用于 `auipc`/`lui`，立即数放在高 20 位。
  - **S-type**（存储型）：`imm[11:5] | rs2 | rs1 | funct3 | imm[4:0] | opcode`，用于 `sb`/`sw` 等 store 指令——它的立即数被拆成两段夹在 `rs2/rs1` 两侧。
- **符号扩展（sign extension, SEXT）**：立即数在指令里只占有限位宽（如 12 位），但它要参与 32 位运算。若把一个负数（最高位为 1）直接零扩展到 32 位，会变成一个大正数，运算就错了。符号扩展就是「用立即数的最高位填满高位」，把 12 位的 `-1` 还原成 32 位的 `0xFFFFFFFF`。这是本讲反复出现的核心操作。
- **三 PC 模型**：回顾 u3-l10，`Decode` 里有三种 PC——`pc`（当前指令地址）、`snpc`（顺序下一地址 = pc + 指令长度）、`dnpc`（动态下一地址，跳转指令改写它）。本讲会看到：取指只推进 `snpc`，执行体只改 `dnpc`，最后 `cpu.pc = dnpc` 提交。
- **`nemu_trap` 约定**：NEMU 没有真正的「操作系统调用退出」机制，于是约定用 `ebreak` 指令作为「程序结束」信号，退出码放在 `a0` 寄存器里。`a0 == 0` 表示成功（`HIT GOOD TRAP`），`a0 != 0` 表示失败（`HIT BAD TRAP`）。这套约定贯穿 AM 抽象机的所有测试程序。

## 3. 本讲源码地图

本讲以 riscv32 的 `inst.c` 为主战场，辅以寄存器访问宏与译码数据结构。

核心源码：

| 文件 | 作用 |
| --- | --- |
| [src/isa/riscv32/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c) | 指令实现全部内容：`isa_exec_once` 取指入口、`decode_exec` 译码执行、`decode_operand` 操作数解码、`immI/immU/immS` 立即数宏、4 条 `INSTPAT` 样板指令、`R(0)=0` 复位。 |
| [src/isa/riscv32/local-include/reg.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/local-include/reg.h) | `gpr(idx)` 访问宏与 `check_reg_idx` 越界检查（u5-l15 已讲）。本讲里 `R(i)` 就是它的别名。 |
| [include/cpu/decode.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h) | `Decode` 结构体与 `INSTPAT`/`INSTPAT_START`/`INSTPAT_END` 通用宏（u3-l11 已讲）。本讲只消费这些宏，不修改它们。 |

辅助契约与类型：

| 文件 | 作用 |
| --- | --- |
| [include/cpu/ifetch.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/ifetch.h) | `inst_fetch(pc, len)`：取指并推进 `*pc`。 |
| [include/cpu/cpu.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h) | `NEMUTRAP`/`INV` 两个宏，分别触发正常结束与非法指令。 |
| [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) | `ISADecodeInfo`（即 `Decode.isa`）只含一个 `uint32_t inst` 字段——这是定长 ISA 的标志。 |
| [include/macro.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h) | `BITS`（按位截取）、`SEXT`（符号扩展）、`BITMASK` 三个位操作宏。 |
| [src/isa/riscv32/init.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c) | 内置镜像 `img[]`——正是它驱动了本讲的 4 条样板指令。 |

调用方与落地：

| 文件 | 作用 |
| --- | --- |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | `exec_once` 调 `isa_exec_once`，并在 `cpu_exec` 里把 `NEMU_END` 状态翻译成 `HIT GOOD/BAD TRAP` 文案。 |
| [src/memory/vaddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c) | `vaddr_read`/`vaddr_write`（即本讲里的 `Mr`/`Mw`），当前直接转发 `paddr_*`（u4-l13）。 |

## 4. 核心概念与源码讲解

### 4.1 isa_exec_once：取指与执行入口

#### 4.1.1 概念说明

`isa_exec_once` 是 ISA 层暴露给框架的「执行一条指令」接口（u5-l14 里 `isa.h` 声明的统一接口之一）。框架侧的 `exec_once`（cpu-exec.c）只负责搭好 `Decode` 工作台、设好 `pc`/`snpc`、最后提交 `cpu.pc = dnpc`；至于「这条指令到底是什么、怎么执行」，它一概不知，全部委托给 `isa_exec_once`。

对 RISC-V 来说，这个入口要做的第一件事就是**取指**：从内存里读出 4 字节、塞进 `Decode.isa.inst`，然后交给 `decode_exec` 去识别和执行。注意这里有个关键的设计纪律：取指只动 `snpc`（顺序 PC），绝不碰 `dnpc`（动态 PC）——`dnpc` 留给执行体在「真的发生跳转」时才改写。

#### 4.1.2 核心流程

```
exec_once(s, pc)                 # 框架侧（cpu-exec.c）
  s->pc = pc; s->snpc = pc;
  isa_exec_once(s)               # ← 本讲入口
  │
  ├─ s->isa.inst = inst_fetch(&s->snpc, 4)
  │       │
  │       ├─ inst = vaddr_ifetch(*pc, 4)   # 从内存读 4 字节
  │       └─ *pc += 4                      # 只推进 snpc
  │
  └─ decode_exec(s)              # 识别 + 执行（见 4.4）
  cpu.pc = s->dnpc               # 框架侧提交
```

要点：

- 固定取 4 字节——这是 RV32I 定长指令的直接体现。x86 的 `isa_exec_once` 不会这样写，它要逐字节试探（u5-l17）。
- `inst_fetch` 接收的是 `&s->snpc`（指针），所以在内部把 `snpc` 加了 4。返回值是 `uint32_t`，意味着 4 个字节被按宿主机小端约定拼成一个 32 位整数。
- 取指用 `vaddr_ifetch`（而非 `vaddr_read`），是为将来分页时区分「取指」与「读数据」的权限埋的接缝（u4-l13 讲过 `MEM_TYPE_IFETCH`）。

#### 4.1.3 源码精读

`isa_exec_once` 只有两行，但每一行都对应上面流程图里的关键一步。见 [src/isa/riscv32/inst.c:75-78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78)：先取指填入 `s->isa.inst`，再调用 `decode_exec`。

`inst_fetch` 的实现见 [include/cpu/ifetch.h:20-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/ifetch.h#L20-L24)：它把「读内存」与「推进 PC」合二为一——这是 u3-l10 强调的「取指专用接口」。

`Decode.isa` 字段的类型在 [src/isa/riscv32/include/isa-def.h:27-29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L27-L29) 定义，riscv32 下就是 `struct { uint32_t inst; }`。对比 x86 的 `inst[16] + p_inst`，单字段 vs 字节数组的差异，正是定长与变长译码分叉的起点。

#### 4.1.4 代码实践

- **实践目标**：验证「RISC-V 每条指令推进 PC 恰好 4」并理清 `pc/snpc/dnpc` 在取指阶段的数据流。
- **操作步骤**：
  1. 在 `src/isa/riscv32/inst.c` 的 `isa_exec_once` 里，临时加一行打印（**示例代码**，仅供观察，验证后删除）：
     ```c
     int isa_exec_once(Decode *s) {
       s->isa.inst = inst_fetch(&s->snpc, 4);
       printf("[fetch] pc=0x%08x snpc=0x%08x inst=0x%08x\n", s->pc, s->snpc, s->isa.inst);
       return decode_exec(s);
     }
     ```
  2. 重新 `make` 编译，运行内置镜像（`make run` 或 `./build/riscv32-nemu-interpreter`，进入 SDB 后若 `c` 命令尚未实现可先用 `si 4` 单步 4 条）。
- **需要观察的现象**：每条指令打印一行，`snpc` 比 `pc` 大 4；`inst` 依次是 `0x00000297`、`0x00028823`、`0x0102c503`、`0x00100073`（即内置 `img[]` 的前 4 个字，见 4.6）。
- **预期结果**：4 条指令后 `pc` 从 `0x80000000` 推进到 `0x8000000c`，随后 `ebreak` 触发结束。实际运行结果**待本地验证**（取决于 SDB 是否已具备 `si`/`c`，参见 u2-l5、u3-l9）。

#### 4.1.5 小练习与答案

1. **练习**：如果把 `inst_fetch(&s->snpc, 4)` 改成 `inst_fetch(&s->dnpc, 4)`，会发生什么？
   **答案**：`snpc` 不再被推进，`ilen = snpc - pc` 恒为 0；同时 `dnpc` 被取指逻辑改成了 `pc+4`，覆盖了执行体本该写入的跳转目标——任何跳转指令都会失效。这正说明取指必须只动 `snpc`。
2. **练习**：为什么 `isa_exec_once` 用 `vaddr_ifetch` 而不是 `vaddr_read`？
   **答案**：当前两者实现相同（都转发 `paddr_read`，见 [src/memory/vaddr.c:19-29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29)），但接口分开是为分页实现后按访存种类做权限区分（取指/读/写对应 `MEM_TYPE_IFETCH/READ/WRITE`，u4-l13）。

### 4.2 decode_operand：操作数解码与寄存器字段

#### 4.2.1 概念说明

指令取回来之后是 32 位裸比特，执行体却想用「`src1`、`src2`、`imm`、`rd`」这种语义化的变量来写逻辑。`decode_operand` 就是这两者之间的翻译官：它按指令类型，从固定比特位置切出寄存器编号和立即数，填到执行体要用的变量里。

这个设计的好处是**执行体与字段布局解耦**：写 `lbu` 的人只关心「`src1 + imm` 是地址、读 1 字节到 `rd`」，不必记住 `rs1` 在 19:15、`imm` 在 31:20。所有位切片的脏活都集中在 `decode_operand` 和三个立即数宏里。

#### 4.2.2 核心流程

`decode_operand` 的参数是「输出参数」指针（`rd/src1/src2/imm`），返回值通过指针写回。它先无脑切出三个寄存器字段，再按 `type` 决定填哪些：

```
decode_operand(s, &rd, &src1, &src2, &imm, type)
  i = s->isa.inst
  rs1 = BITS(i, 19, 15)          # 永远切出来备用
  rs2 = BITS(i, 24, 20)
  *rd = BITS(i, 11, 7)           # rd 总是填（所有格式都有 rd 字段）
  switch (type):
    TYPE_I: src1 = R(rs1);            imm = immI();     # 如 lbu/jalr
    TYPE_U:                             imm = immU();     # 如 auipc/lui
    TYPE_S: src1 = R(rs1); src2 = R(rs2); imm = immS();  # 如 sb/sw
    TYPE_N: （什么都不填）                                 # 如 ebreak/inv
```

要点：

- `rs1/rs2` 总是被切出，但只有对应类型才用 `src1R()`/`src2R()` 把它读成寄存器值。`TYPE_U` 既不读 `rs1` 也不读 `rs2`，所以 `src1/src2` 保持 `INSTPAT_MATCH` 初始化的 0。
- `*rd` 总是填上编号，但「是否写回」由执行体决定（见 4.5 的 `R(0)` 问题）。
- `type` 是个裸 token（`I`/`U`/`S`/`N`），由 `INSTPAT_MATCH` 用 `concat(TYPE_, type)` 拼成 `TYPE_I` 等枚举值。

#### 4.2.3 源码精读

寄存器字段切片与类型分发见 [src/isa/riscv32/inst.c:36-48](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L36-L48)：

```c
static void decode_operand(Decode *s, int *rd, word_t *src1, word_t *src2, word_t *imm, int type) {
  uint32_t i = s->isa.inst;
  int rs1 = BITS(i, 19, 15);
  int rs2 = BITS(i, 24, 20);
  *rd     = BITS(i, 11, 7);
  switch (type) {
    case TYPE_I: src1R();          immI(); break;
    case TYPE_U:                   immU(); break;
    case TYPE_S: src1R(); src2R(); immS(); break;
    case TYPE_N: break;
    default: panic("unsupported type = %d", type);
  }
}
```

`src1R/src2R` 与类型枚举见 [src/isa/riscv32/inst.c:25-31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L25-L31)：`src1R()` 展开为 `*src1 = R(rs1)`，而 `R(i)` 就是 u5-l15 讲过的 `gpr(i)`，带 `check_reg_idx` 越界检查。

位切片用的 `BITS` 定义在 [include/macro.h:86-87](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L86-L87)：`BITS(x, hi, lo) = ((x) >> lo) & BITMASK(hi-lo+1)`，等价于 Verilog 里的 `x[hi:lo]`——先右移去掉低位、再用掩码截出指定宽度。

#### 4.2.4 代码实践

- **实践目标**：手工从一条真实指令字里切出 `rd/rs1/rs2/imm`，验证 `decode_operand` 的字段布局。
- **操作步骤**：
  1. 取内置镜像第 2 条指令 `0x00028823`（注释为 `sb zero,16(t0)`）。把它写成 32 位二进制：`0000 0000 0000 0010 1000 1000 0010 0011`。
  2. 按 S-type 字段切分（从高位到低位）：`imm[11:5] | rs2 | rs1 | funct3 | imm[4:0] | opcode`。
  3. 对应比特：`imm[11:5]=0000000`、`rs2=00000`、`rs1=00101`、`funct3=000`、`imm[4:0]=10000`、`opcode=0100011`。
  4. 解读：`rs1=5=t0`、`rs2=0=zero`、`imm = (0000000<<5)|10000 = 16`、`funct3=000=sb`，与注释 `sb zero,16(t0)` 完全一致。
- **需要观察的现象**：手算结果与 `init.c` 里注释逐字对应。
- **预期结果**：`rs1=5`、`rs2=0`、`imm=16`，验证 `decode_operand` 在 `TYPE_S` 下会读 `R(5)` 作 `src1`、`R(0)` 作 `src2`、`imm=16`。这是纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

1. **练习**：对 `0x0102c503`（`lbu a0,16(t0)`）做同样的字段切片，指出它的 `type`。
   **答案**：`opcode=0000011`（LOAD），属 I-type。`imm[11:0]=000000010000=16`、`rs1=00101=5=t0`、`funct3=100=lbu`、`rd=01010=10=a0`。故 `decode_operand` 走 `TYPE_I` 分支：`src1=R(5)`、`imm=16`、`rd=10`。
2. **练习**：为什么 `*rd = BITS(i, 11, 7)` 写在 `switch` 之前，而 `src1/src2` 的读取写在 `switch` 内部？
   **答案**：所有 RISC-V 格式都有 `rd` 字段且位置固定（11:7），所以无条件填；但 `rs1/rs2` 是否需要、立即数如何拼接，随类型而变，故按 `type` 分支处理。把公共字段提前、差异字段入 switch，是减少重复的常见写法。

### 4.3 immI / immU / immS：三种立即数格式与 SEXT

#### 4.3.1 概念说明

立即数（immediate）是直接编码在指令里的常数，不必从寄存器取。RISC-V 为了让不同指令共用同一套译码逻辑，故意把立即数摆在「尽量对齐」的位置，但不同格式仍有差异：I 型立即数在 31:20 连续 12 位；U 型立即数在 31:12 连续 20 位（使用时左移 12）；S 型立即数被拆成 31:25 和 11:7 两段（中间夹着 `rs2` 字段）。

`immI/immU/immS` 三个宏各自负责把对应格式的比特拼出正确数值。这里最关键的操作是**符号扩展**：立即数在指令里是 12 或 20 位的有符号数，要参与 32 位运算就必须用最高位（符号位）填充高位。NEMU 用一个巧妙的 `SEXT` 宏完成这件事。

#### 4.3.2 核心流程

三个立即数宏的拼接逻辑：

```
immI(): imm = SEXT( BITS(i,31,20), 12 )
        # 取 31:20 共 12 位，符号扩展到 64 位
immU(): imm = SEXT( BITS(i,31,12), 20 ) << 12
        # 取 31:12 共 20 位，符号扩展后左移 12 位（低 12 位补 0）
immS(): imm = ( SEXT(BITS(i,31,25), 7) << 5 ) | BITS(i,11,7)
        # 高 7 位 31:25 符号扩展后左移 5，拼上低 5 位 11:7
```

`SEXT(x, len)` 的原理（见 [include/macro.h:88](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L88)）：借助 C 位域（bit-field）声明一个 `int64_t n : len` 的有符号位域，把 `x` 赋给它——C 标准规定位域赋值会截断到 `len` 位并按有符号解释，于是高位自动填符号位；再转成 `uint64_t` 拿出来。等价数学表达：

\[
\text{SEXT}(x, len) = \begin{cases} x & \text{若 } x \text{ 的第 } len-1 \text{ 位为 } 0 \\ x \,|\, (\text{全 1} \ll len) & \text{若为 } 1 \end{cases}
\]

要点：

- I 型：12 位立即数，最高位（bit 31）是符号位。负立即数（如 `lw a0,-4(t0)`）的 bit 31 为 1，`SEXT` 后高位全填 1，得到 32 位负数。
- U 型：20 位立即数在高位，使用时左移 12 位（低 12 位恒 0）。`auipc` 用它构造「高 20 位 + 低 12 位零」的地址偏移。
- S 型：立即数被拆两段，先拼后扩展。注意是「高 7 位先 SEXT 再左移 5，再或上低 5 位」——低 5 位是无符号的，直接或进去即可。

#### 4.3.3 源码精读

三个立即数宏定义在 [src/isa/riscv32/inst.c:32-34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L32-L34)：

```c
#define immI() do { *imm = SEXT(BITS(i, 31, 20), 12); } while(0)
#define immU() do { *imm = SEXT(BITS(i, 31, 12), 20) << 12; } while(0)
#define immS() do { *imm = (SEXT(BITS(i, 31, 25), 7) << 5) | BITS(i, 11, 7); } while(0)
```

`SEXT` 与 `BITS`/`BITMASK` 一起定义在 [include/macro.h:86-88](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L86-L88)。`do { ... } while(0)` 包装让宏能像一条语句那样安全地在 `switch` 里使用（u2-l6 提到过这个惯用法）。

> 提示：`SEXT` 用了 GCC 的语句表达式 `({ ... })` 与位域，是 GCC 扩展，并非标准 C——这也是 NEMU 依赖 GCC/Clang 而非任意 C 编译器的原因之一。

#### 4.3.4 代码实践

- **实践目标**：亲手算一个**负**立即数，体会 `SEXT` 的必要性。
- **操作步骤**：
  1. 考虑 `lw a0, -4(t0)` 的编码。其 12 位立即数为 `-4`，即 `0xFFFFFFFC` 的低 12 位 `0xFFC`，二进制 `1111 1111 1100`。
  2. 不做符号扩展：`BITS(i,31,20) = 0xFFC = 4092`，当成地址偏移就是 `t0 + 4092`——完全错。
  3. 做 `SEXT(..., 12)`：因为 bit 11（立即数最高位）为 1，高位全填 1，得 `0xFFFFFFFC = -4`，地址偏移才是正确的 `t0 - 4`。
- **需要观察的现象**：同一个 12 位比特 `0xFFC`，零扩展得 `4092`、符号扩展得 `-4`。
- **预期结果**：理解「凡立即数最高位为 1 必须符号扩展，否则地址/分支目标会偏到一个巨大的正值」。这是阅读型实践；若想运行验证，可在实现 `lw` 后构造一条负偏移 load，用 `info r` 看 `a0` 与预期一致（**待本地验证**）。

#### 4.3.5 小练习与答案

1. **练习**：`auipc t0, 0` 的 `immU` 是多少？为什么 `immU` 要左移 12？
   **答案**：`BITS(i,31,12)=0`，`SEXT(0,20)=0`，左移 12 仍为 0，故 `imm=0`。左移 12 是因为 U 型立即数表示的是「高 20 位」，低 12 位由其他指令（如 `addi`）补充——`lui`/`auipc` 只负责高 20 位，低 12 位恒 0。
2. **练习**：`immS` 里为什么低 5 位 `BITS(i,11,7)` 不需要 `SEXT`？
   **答案**：符号位是立即数的最高位（bit 31），已经在「高 7 位」那段的 `SEXT(...,7)` 里处理过。低 5 位只是立即数的低位部分，是无符号的，直接或到已扩展的高位之后即可，再做 `SEXT` 反而会引入新的（错误的）符号填充。

### 4.4 INSTPAT 实例：auipc / lbu / sb 的执行体

#### 4.4.1 概念说明

u3-l11 已经讲清了 `INSTPAT` 的模式匹配机制：`pattern_decode` 在编译期把 `"01??"` 串编译成 `key/mask/shift`，运行时用 `(inst>>shift)&mask==key` 一次比较完成匹配，命中就执行 `INSTPAT_MATCH` 体并 `goto` 跳出。本讲要看的，是这套机制在 riscv32 上「落地的样子」——也就是 `inst.c` 里那几条真实的 `INSTPAT(...)`。

这里有两个 ISA 侧接缝（u3-l11 提过）：`INSTPAT_INST(s)` 告诉框架「怎么从 `Decode` 里读出原始指令字」，`INSTPAT_MATCH(s, name, type, body)` 告诉框架「匹配命中后做什么」。对 riscv32，前者是 `((s)->isa.inst)`（一个 32 位字），后者是「调 `decode_operand` 解码操作数 + 执行 `body`」。这两个宏由各 ISA 自己定义在 `inst.c` 里，使 `decode.h` 的 `INSTPAT` 框架完全 ISA 无关。

#### 4.4.2 核心流程

`decode_exec` 的整体结构：

```
decode_exec(s):
  s->dnpc = s->snpc               # 默认：下一指令 = 顺序下一地址

  # 两个 ISA 侧接缝
  #define INSTPAT_INST(s)   ((s)->isa.inst)
  #define INSTPAT_MATCH(s, name, type, body):
  #     int rd=0; word_t src1=0, src2=0, imm=0;
  #     decode_operand(s, &rd, &src1, &src2, &imm, TYPE_ ## type);
  #     body ;

  INSTPAT_START()
    INSTPAT("...00101 11", auipc, U, R(rd) = s->pc + imm);   # PC 相对寻址
    INSTPAT("...00000 11", lbu  , I, R(rd) = Mr(src1+imm,1)); # 无符号读字节
    INSTPAT("...01000 11", sb   , S, Mw(src1+imm,1, src2));   # 写低字节
    INSTPAT("...11100 11", ebreak, N, NEMUTRAP(s->pc, R(10)));# 见 4.6
    INSTPAT("全 ?",       inv   , N, INV(s->pc));              # 兜底
  INSTPAT_END()

  R(0) = 0                         # 见 4.5
  return 0
```

要点：

- 第一行 `s->dnpc = s->snpc` 是「默认顺序执行」。只有跳转/分支指令才在 `body` 里改写 `dnpc`，否则 `dnpc` 保持 `snpc`，提交后 `cpu.pc = pc + 4`。
- `INSTPAT_MATCH` 先把 `rd/src1/src2/imm` 初始化为 0，再按 `type` 调 `decode_operand` 填值，最后跑 `body`。所以 `body` 里直接用这四个变量即可。
- `R(rd)` 是写回寄存器；`Mr(addr,len)`/`Mw(addr,len,data)` 是读写内存（`Mr`=`vaddr_read`，`Mw`=`vaddr_write`）。
- `inv` 的模式串全是 `?`，匹配任意 32 位值，必须放最后，作为「未识别指令」的兜底。

#### 4.4.3 源码精读

`decode_exec` 全貌与两个接缝宏见 [src/isa/riscv32/inst.c:50-73](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L50-L73)：

```c
static int decode_exec(Decode *s) {
  s->dnpc = s->snpc;

#define INSTPAT_INST(s) ((s)->isa.inst)
#define INSTPAT_MATCH(s, name, type, ... ) { \
  int rd = 0; \
  word_t src1 = 0, src2 = 0, imm = 0; \
  decode_operand(s, &rd, &src1, &src2, &imm, concat(TYPE_, type)); \
  __VA_ARGS__ ; \
}

  INSTPAT_START();
  INSTPAT("??????? ????? ????? ??? ????? 00101 11", auipc  , U, R(rd) = s->pc + imm);
  INSTPAT("??????? ????? ????? 100 ????? 00000 11", lbu    , I, R(rd) = Mr(src1 + imm, 1));
  INSTPAT("??????? ????? ????? 000 ????? 01000 11", sb     , S, Mw(src1 + imm, 1, src2));
  INSTPAT("0000000 00001 00000 000 00000 11100 11", ebreak , N, NEMUTRAP(s->pc, R(10)));
  INSTPAT("??????? ????? ????? ??? ????? ????? ??", inv    , N, INV(s->pc));
  INSTPAT_END();

  R(0) = 0;
  return 0;
}
```

逐条解读：

- `auipc`（U 型）：`R(rd) = s->pc + imm`。注意用的是 `s->pc`（本指令地址），不是 `snpc`——这是 PC 相对寻址的本质。模式串末 7 位 `0010111` 是 `auipc` 的 opcode。
- `lbu`（I 型）：`R(rd) = Mr(src1 + imm, 1)`。从 `src1+imm` 读 1 字节，**零扩展**写入 `rd`。零扩展是「免费的」：`vaddr_read` 返回 `word_t`，读 1 字节时只有低 8 位有效、高位自然为 0（u4-l12 讲过 `host_read` 按 `len` 解引用）。
- `sb`（S 型）：`Mw(src1 + imm, 1, src2)`。把 `src2` 的低 8 位写到 `src1+imm`。截断由 `host_write` 的 `*(uint8_t*)` 赋值完成。
- `ebreak`（N 型）：见 4.6。
- `inv`（N 型）：`INV(s->pc)` → `invalid_inst`，打印诊断后 `set_nemu_state(NEMU_ABORT, ...)`。

`R`/`Mr`/`Mw` 三个别名宏见 [src/isa/riscv32/inst.c:21-23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L21-L23)，`R(i)` 即 u5-l15 的 `gpr(i)`。通用 `INSTPAT` 宏见 [include/cpu/decode.h:90-97](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L90-L97)，`INSTPAT_START`/`INSTPAT_END` 见 [include/cpu/decode.h:99-100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L99-L100)（用 GCC「标号作为值」`&&label` 与「计算跳转」`goto *ptr` 实现匹配即跳出块尾，详见 u3-l11）。

#### 4.4.4 代码实践

- **实践目标**：用 `INSTPAT` 新增一条最简单的指令 `addi`（I 型，`rd = src1 + imm`），跑通整个「加模式串→加执行体」流程。
- **操作步骤**：
  1. 在 `inst.c` 的 `INSTPAT_START()`/`INSTPAT_END()` 之间、`inv` 之前，加入一行（**示例代码**）：
     ```c
     INSTPAT("??????? ????? ????? 000 ????? 00100 11", addi   , I, R(rd) = src1 + imm);
     ```
     （opcode `0010011` 是 ADDI/算术立即数大类，funct3 `000` 选中 `addi`。）
  2. 重新 `make` 编译。
  3. 临时把 `init.c` 的 `img[]` 改成下面两条指令的自检程序（**示例代码**，验证后还原）：
     ```c
     static const uint32_t img [] = {
       0x00100513,  // addi a0, x0, 1   → a0 = 1
       0x00050513,  // addi a0, a0, 0   → a0 保持 1（验证读 src1）
       0x00100073,  // ebreak           → trap，a0=1 → 预期 HIT BAD TRAP
     };
     ```
  4. 运行 `make run`。
- **需要观察的现象**：程序在 `ebreak` 结束，因为 `a0=1`（非 0）应输出 `HIT BAD TRAP`。
- **预期结果**：看到 `HIT BAD TRAP` 即证明 `addi` 正确读到了 `src1`（`a0`）和 `imm`（1）。若看到 `ABORT`/非法指令，说明 `addi` 模式串写错或没加在 `inv` 之前。实际运行结果**待本地验证**。

#### 4.4.5 小练习与答案

1. **练习**：`auipc` 的执行体为什么是 `s->pc + imm` 而不是 `s->snpc + imm`？
   **答案**：`auipc`（Add Upper Immediate to PC）的定义是「把立即数加到**本指令**的地址上」。`s->pc` 是当前指令地址，`s->snpc` 是 `pc+4`（下一条地址）。用 `snpc` 会差 4，得到的地址就错了。这也体现了「取指只推进 `snpc`、`pc` 始终是当前指令地址」的设计好处。
2. **练习**：`lbu` 的执行体没有显式做零扩展，为什么读出来的就是无符号字节？
   **答案**：`Mr(addr, 1)` 返回 `word_t`，底层 `host_read` 读 1 字节时按 `uint8_t` 解引用再赋给 `word_t`，C 的整数提升会自动把高 24 位填 0。所以「零扩展」由内存子系统天然完成，`lbu` 不必再处理。（对比：若实现 `lb`——有符号加载——则需要 `SEXT(Mr(...,1), 8)`。）

### 4.5 R(0) = 0：零寄存器不变量的强制复位

#### 4.5.1 概念说明

RISC-V 规定 `x0` 是「硬连线零」：读取永远是 0、写入被丢弃。但 NEMU 的实现里，`R(rd) = ...` 这条写回语句并不区分 `rd` 是不是 0——如果某条指令的 `rd` 字段恰好是 0（例如 `addi x0, x1, 2`），执行体照样会往 `cpu.gpr[0]` 写入一个非零值，破坏「x0 恒零」的不变量。

NEMU 采取了一个简单到近乎粗暴的兜底策略：在 `decode_exec` 末尾无条件执行 `R(0) = 0`，把 `x0` 复位回 0。这样无论执行体往 `x0` 写了什么，一条指令结束时 `x0` 又变回 0。代价是每条指令多一次写，换来的是所有指令的执行体都不必特判 `rd != 0`——用一致性换简洁性。

#### 4.5.2 核心流程

```
decode_exec(s):
  ... 某条指令的 body 执行 R(rd) = 某值 ...
        │
        ├─ 若 rd != 0：正常写回，无副作用
        └─ 若 rd == 0：cpu.gpr[0] 被写脏（暂时的）
  R(0) = 0      # 兜底复位：无论上面写了什么，x0 强制归零
  return 0
```

要点：

- 复位发生在「每条指令执行之后」，所以从下一条指令的视角看，`x0` 始终是 0——不变量得以维持。
- `R(0)` 即 `gpr(0)`，会过 `check_reg_idx`（u5-l15）；`idx=0` 永远合法。
- 这个策略与 `restart()` 里 `cpu.gpr[0] = 0`（u5-l15）呼应：上电时置 0，运行中每步再强制保持 0。

#### 4.5.3 源码精读

`decode_exec` 末尾的复位见 [src/isa/riscv32/inst.c:70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L70)，就一行：`R(0) = 0; // reset $zero to 0`。`R` 宏定义在 [src/isa/riscv32/inst.c:21](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L21)（`#define R(i) gpr(i)`），`gpr` 见 [src/isa/riscv32/local-include/reg.h:26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/local-include/reg.h#L26)。

注意内置镜像的 4 条指令里，`rd` 分别是 `t0`(auipc)、无(sb)、`a0`(lbu)、无(ebreak)——没有一条写 `x0`，所以这行对内置自检而言并不触发，但它在你新增 `add`/`addi` 后会变得关键（综合实践里 `add a0,x0,x0` 读 `x0` 是 0，正依赖这个不变量）。

#### 4.5.4 代码实践

- **实践目标**：直观感受「`R(0)=0` 兜底」的必要性。
- **操作步骤**：
  1. 在已实现 `addi`（见 4.4.4）的基础上，构造一条往 `x0` 写非零值的指令（**示例代码**）：
     ```c
     static const uint32_t img [] = {
       0x00100093,  // addi x0, x0, 1   → 试图把 x0 设成 1
       0x00000513,  // addi a0, x0, 0   → 把 x0 读到 a0
       0x00100073,  // ebreak           → trap，a0 应为 0 → HIT GOOD TRAP
     };
     ```
  2. 运行，记录 `a0`（即 trap 返回值）。
  3. 然后把 `inst.c` 里 `R(0) = 0;` 那一行**临时注释掉**，重新编译运行，再看 `a0`。
- **需要观察的现象**：保留 `R(0)=0` 时 `a0=0`（GOOD TRAP）；注释掉后 `a0=1`（BAD TRAP）——因为 `addi x0,x0,1` 把 `x0` 写成了 1，`addi a0,x0,0` 读到的就是 1。
- **预期结果**：两相对比，说明 `R(0)=0` 是维持 x0 恒零的关键。验证后**务必还原** `R(0)=0`。实际运行结果**待本地验证**。

#### 4.5.5 小练习与答案

1. **练习**：如果不在每条指令后 `R(0)=0`，还有别的实现方式吗？
   **答案**：可以在 `INSTPAT_MATCH` 的写回路径上特判，例如把 `R(rd) = ...` 改成 `if (rd) R(rd) = ...`，或在 `gpr` 宏里对 `idx==0` 的写做拦截。这些方案更「精确」但要在每个写回点加判断；NEMU 选择「先随便写、最后统一复位」的笨办法，换取执行体的简洁。
2. **练习**：为什么 `restart()` 里也要 `cpu.gpr[0] = 0`？有了每步 `R(0)=0` 不够吗？
   **答案**：`R(0)=0` 只在「执行了一条指令之后」生效。上电后、第一条指令执行前，`cpu.gpr[0]` 可能是未初始化的脏值（`CPU_state cpu = {};` 虽零初始化，但显式置 0 是防御式编程）。`restart()` 保证从第 0 条指令开始 `x0` 就是 0。

### 4.6 ebreak 与 NEMUTRAP：trap 约定与返回值

#### 4.6.1 概念说明

模拟器里运行的「客机程序」需要一个机制告诉模拟器「我跑完了，结果是成功还是失败」。真实硬件靠操作系统调用（如 `exit`），但 NEMU 在 PA 阶段没有操作系统，于是约定：**执行 `ebreak` 指令即表示程序结束**，返回值放在 `a0` 寄存器里。这个约定叫 `nemu_trap`。

- `a0 == 0` → 程序成功 → 屏幕打印 `HIT GOOD TRAP`。
- `a0 != 0` → 程序失败 → 屏幕打印 `HIT BAD TRAP`。

`ebreak` 本是 RISC-V 的断点指令（用于调试器），NEMU 「挪用」它作 trap 信号，是因为在 PA 早期阶段它不会被用到、编码固定、好识别。这就是内置镜像 `img[]` 末尾那条 `0x00100073`（`ebreak`）的真正用意——它不是断点，而是「自检结束、报告 a0」。

#### 4.6.2 核心流程

```
ebreak 命中 INSTPAT
  → NEMUTRAP(s->pc, R(10))
       │  s->pc  = ebreak 的地址（halt_pc）
       │  R(10)  = a0 的值（halt_ret）
       └─ set_nemu_state(NEMU_END, thispc, code)
              ├─ nemu_state.state   = NEMU_END
              ├─ nemu_state.halt_pc = thispc
              └─ nemu_state.halt_ret= code

execute() 循环发现 nemu_state.state != NEMU_RUNNING → break
cpu_exec() 收尾 switch：
  case NEMU_END:
    if (halt_ret == 0)  → "HIT GOOD TRAP"
    else                → "HIT BAD TRAP"
```

要点：

- `R(10)` 即 `a0`（regs 表里 `regs[10]="a0"`，见 u5-l15）。
- `NEMUTRAP` 第一个参数是 `s->pc`（ebreak 自己的地址），用作 `halt_pc`——这是「程序停在哪」的现场信息。
- 状态置为 `NEMU_END` 后，`execute` 循环的下一次状态检查会 `break`，回到 `cpu_exec` 走收尾分支。注意 `exec_once` 里 `cpu.pc = s->dnpc` 仍会执行（`dnpc` 此时等于 `snpc`=pc+4），但执行已停止，无影响。

#### 4.6.3 源码精读

`ebreak` 的 `INSTPAT` 项见 [src/isa/riscv32/inst.c:66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L66)：模式串 `0000000 00001 00000 000 00000 11100 11` 是 `ebreak`（`0x00100073`）的完整编码——`imm=1`、`rs1=0`、`funct3=0`、`rd=0`、`opcode=1110011`（SYSTEM）。注释 `// R(10) is $a0` 点明了返回值取自 `a0`。

`NEMUTRAP`/`INV` 两个宏定义在 [include/cpu/cpu.h:26-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L26-L27)：

```c
#define NEMUTRAP(thispc, code) set_nemu_state(NEMU_END, thispc, code)
#define INV(thispc) invalid_inst(thispc)
```

`set_nemu_state` 设置的 `nemu_state` 在 `cpu_exec` 收尾时被翻译成文案，见 [src/cpu/cpu-exec.c:119-125](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L119-L125)：`halt_ret == 0` 打 `HIT GOOD TRAP`（绿色），否则 `HIT BAD TRAP`（红色）；`NEMU_ABORT` 则打 `ABORT`。这套状态机的完整脉络见 u3-l9 与 u7-l23。

内置镜像 `img[]` 见 [src/isa/riscv32/init.c:21-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L27)，注释把每条指令的意图写得很清楚：

```c
static const uint32_t img [] = {
  0x00000297,  // auipc t0,0
  0x00028823,  // sb  zero,16(t0)
  0x0102c503,  // lbu a0,16(t0)
  0x00100073,  // ebreak (used as nemu_trap)
  0xdeadbeef,  // some data
};
```

它是一个自检程序：`auipc` 算出基址 `t0`，`sb` 把 0 写到 `t0+16`（覆盖 `0xdeadbeef` 的最低字节），`lbu` 再从 `t0+16` 读回这个字节到 `a0`，最后 `ebreak` 结束。因为写入和读出的都是 0，`a0=0`，于是输出 `HIT GOOD TRAP`——这条链路同时验证了 `auipc`/`sb`/`lbu` 三条指令与内存读写路径的正确性。

#### 4.6.4 代码实践

- **实践目标**：跑通内置镜像，观察 `HIT GOOD TRAP`；再改一个字节让它变成 `HIT BAD TRAP`，理解返回值约定。
- **操作步骤**：
  1. 确认 `auipc`/`lbu`/`sb`/`ebreak` 四条指令已实现（仓库里默认就有），`make` 编译后 `make run` 运行内置镜像。
  2. 在 SDB 里执行 `c`（或 `si 4` 单步到 `ebreak`）。
  3. 观察结束时的输出。
  4. 把 `init.c` 的 `img[]` 第 3 条 `0x0102c503`（`lbu a0,16(t0)`）临时改成 `0x0002c503`（**示例代码**：`lbu a0,0(t0)`，即偏移从 16 改成 0），重新编译运行。
- **需要观察的现象**：第 3 步应看到 `HIT GOOD TRAP`（`a0=0`，因为 `sb` 刚把 `t0+16` 写成 0，`lbu` 读回 0）。第 4 步改成读 `t0+0`——那是 `auipc` 指令自身的最低字节 `0x97`，`a0=0x97 != 0`，应看到 `HIT BAD TRAP`。
- **预期结果**：两步对比，直观印证「`a0` 即 trap 返回值、0 为成功」的约定。第 4 步的改动是**示例代码**，验证后请还原。实际运行结果**待本地验证**（依赖 SDB 的 `c`/`si` 已就绪，见 u2-l5、u3-l9）。

#### 4.6.5 小练习与答案

1. **练习**：`ebreak` 的 `INSTPAT` 模式串是完整 32 位全确定的，而不是用 `?` 通配。为什么？
   **答案**：`ebreak` 的编码 `0x00100073` 每一位都固定（它属于 SYSTEM 大类，但 `imm` 字段必须是 1 才是 `ebreak`，`imm=0` 是 `ecall`）。用完整模式串能精确匹配 `ebreak` 而不误伤同 opcode 的其他指令。若用通配，会把 `ecall` 等也当成 trap，语义就错了。
2. **练习**：`NEMUTRAP(s->pc, R(10))` 里为什么传 `s->pc` 而不是 `s->dnpc`？
   **答案**：`halt_pc` 表示「程序停在哪条指令」，自然是 `ebreak` 自己的地址 `s->pc`。`s->dnpc` 此时是 `pc+4`（ebreak 之后），传它会让「停止现场」错位一条指令，不利于排错。

## 5. 综合实践

把本讲所有模块串起来，完成 PA 的标志性任务：**为 riscv32 实现 `add`/`lw`/`sw`/`jal`/`jalr`/`beq` 六条基本指令，让一个简单 RISC-V 程序跑通并 `HIT GOOD TRAP`**。

### 步骤一：扩展操作数类型与立即数格式

仓库里的 `enum` 只有 `TYPE_I/U/S/N`（见 [src/isa/riscv32/inst.c:25-28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L25-L28)）。六条新指令里：`lw` 复用 I 型、`sw` 复用 S 型、`jalr` 复用 I 型；但 `add`（R 型）、`beq`（B 型）、`jal`（J 型）需要新类型与立即数宏。在 `enum` 与 `decode_operand` 里新增（**示例代码**）：

```c
enum { TYPE_I, TYPE_U, TYPE_S, TYPE_R, TYPE_B, TYPE_J, TYPE_N };

#define src1R()     do { *src1 = R(rs1); } while (0)
#define src2R()     do { *src2 = R(rs2); } while (0)
#define immI() do { *imm = SEXT(BITS(i, 31, 20), 12); } while(0)
#define immU() do { *imm = SEXT(BITS(i, 31, 12), 20) << 12; } while(0)
#define immS() do { *imm = (SEXT(BITS(i, 31, 25), 7) << 5) | BITS(i, 11, 7); } while(0)
// 新增：
#define immB() do { *imm = SEXT((BITS(i,31,31)<<12)|(BITS(i,7,7)<<11)|(BITS(i,30,25)<<5)|(BITS(i,11,8)<<1), 13); } while(0)
#define immJ() do { *imm = SEXT((BITS(i,31,31)<<20)|(BITS(i,19,12)<<12)|(BITS(i,20,20)<<11)|(BITS(i,30,21)<<1), 21); } while(0)
```

并在 `decode_operand` 的 `switch` 里加分支（**示例代码**）：

```c
case TYPE_R: src1R(); src2R();             break;
case TYPE_B: src1R(); src2R(); immB();     break;
case TYPE_J:                    immJ();     break;
```

> 说明：B 型立即数是 13 位（imm[12:1]，最低位恒 0，因为分支目标总是偶地址）；J 型立即数是 21 位（imm[20:1]，同理）。两者的比特在指令里都被打乱重排，需按 RISC-V 手册的位映射拼回。

### 步骤二：在 INSTPAT 表里加入六条指令

在 `inv` 之前插入（**示例代码**，模式串从高位到低位 7 字段：`funct7/funct3 段 | rs2 | rs1 | funct3 | rd | opcode`）：

```c
INSTPAT("0000000 ????? ????? 000 ????? 01100 11", add  , R, R(rd) = src1 + src2);
INSTPAT("??????? ????? ????? 010 ????? 00000 11", lw   , I, R(rd) = Mr(src1 + imm, 4));
INSTPAT("??????? ????? ????? 010 ????? 01000 11", sw   , S, Mw(src1 + imm, 4, src2));
INSTPAT("??????? ????? ????? ??? ????? 11011 11", jal  , J, R(rd) = s->snpc; s->dnpc = s->pc + imm);
INSTPAT("??????? ????? ????? 000 ????? 11001 11", jalr , I, R(rd) = s->snpc; s->dnpc = (src1 + imm) & ~1);
INSTPAT("??????? ????? ????? 000 ????? 11000 11", beq  , B, if (src1 == src2) s->dnpc = s->pc + imm);
```

逐条要点：

- `add`（R 型）：`src1 + src2` 写回 `rd`，无立即数。
- `lw`（I 型）：读 4 字节到 `rd`。riscv32 下 `word_t` 是 `uint32_t`，`Mr(...,4)` 直接返回 32 位，无需扩展；若将来适配 RV64，`lw` 需 `SEXT(Mr(...,4), 32)`。
- `sw`（S 型）：把 `src2` 的 4 字节写到 `src1+imm`。
- `jal`（J 型）：**写回返回地址** `R(rd) = s->snpc`（= pc+4，下一条指令地址），**并改写 `dnpc`** 跳到 `s->pc + imm`。这是第一条「动 `dnpc`」的指令——它让 `cpu.pc` 不再顺序推进。
- `jalr`（I 型）：返回地址同上，跳转目标为 `(src1 + imm) & ~1`（按 RISC-V 规定把最低位清零）。
- `beq`（B 型）：**条件改写 `dnpc`**——仅当 `src1 == src2` 才跳到 `s->pc + imm`，否则 `dnpc` 保持 `decode_exec` 开头设的 `s->snpc`（顺序执行）。

注意 `jal`/`jalr` 用 `s->snpc` 作为返回地址而非 `s->pc + 4`：对定长 RISC-V 两者等价，但用 `snpc` 语义更清晰（「顺序下一地址」），也与 u3-l10 的三 PC 模型一致。

### 步骤三：最小冒烟测试

先把 `add` 单独验证。临时把 `init.c` 的 `img[]` 换成（**示例代码**，验证后还原）：

```c
static const uint32_t img [] = {
  0x00000533,  // add a0, x0, x0   → a0 = 0
  0x00100073,  // ebreak           → HIT GOOD TRAP
};
```

`make run` 后预期看到 `HIT GOOD TRAP`——这同时验证了 `add`（`0+0=0`）与 `R(0)=0`（读 `x0` 为 0）。**待本地验证**。

### 步骤四：控制流与访存测试

用 RISC-V 工具链（如 `riscv32-unknown-elf-gcc`）编译一个稍大的程序，或加载 AM 提供的测试用例（如 `am-kernels` 里的 cpu-tests）。一个能同时覆盖 `lw/sw/jal/jalr/beq` 的最小场景是「循环求和」：用 `beq` 做循环退出判断、`jal`/`jalr` 做函数调用与返回、`lw/sw` 维护栈上的局部变量。

- **操作**：编译得到二进制后，按 u1-l3 的方式用 `make run` 或 `-b` 加载运行。
- **观察**：程序正常结束应输出 `HIT GOOD TRAP`；若中途 `HIT BAD TRAP` 或 `ABORT`，开启 itrace（u8-l25）定位出错指令。
- **预期结果**：跑通则证明六条指令的译码、立即数符号扩展、控制流改写 `dnpc` 全部正确。实际运行**待本地验证**（依赖工具链与测试程序可用性）。

### 步骤五：分析 SEXT 的作用

回顾步骤一/二，回答：

- `beq` 的偏移若为负（向前跳，构成循环），`immB` 不做 `SEXT` 会怎样？
- `lw a0, -4(sp)` 若不做 `SEXT`，读到的地址是什么？

**结论**：所有带立即数的指令，只要立即数最高位为 1（负数），就必须符号扩展，否则地址/分支目标会变成一个巨大的正值，程序立即崩溃。`SEXT` 是 RISC-V 立即数处理的灵魂。

## 6. 本讲小结

- `isa_exec_once` 是 ISA 侧的「执行一条指令」入口：固定取 4 字节填入 `s->isa.inst`、只推进 `snpc`，再调 `decode_exec`。定长取指是 RV32I 区别于 x86 的根本特征。
- `decode_operand` 是 32 位比特与语义变量之间的翻译官：按 `TYPE_I/U/S` 从固定字段切出 `rd/rs1/rs2/imm`，让执行体只关心语义、不关心位布局。
- `immI/immU/immS` 三个立即数宏分别处理 I/U/S 三种格式；`SEXT` 借 C 位域完成符号扩展，是负立即数正确性的关键。
- `INSTPAT` 在 riscv32 上落地为 `INSTPAT_INST`（读 `isa.inst`）+ `INSTPAT_MATCH`（解码操作数 + 跑执行体）两个 ISA 侧接缝；`auipc`/`lbu`/`sb` 是三条样板，分别示范了 PC 相对寻址、无符号读字节、写字节。
- `decode_exec` 末尾的 `R(0) = 0` 用「每步强制复位」维持 x0 恒零的不变量，换取执行体不必特判 `rd==0` 的简洁性。
- `ebreak` 被复用为 `nemu_trap`：`NEMUTRAP(s->pc, R(10))` 把 `a0` 作为返回码传给 `set_nemu_state(NEMU_END,...)`，最终在 `cpu_exec` 里映射成 `HIT GOOD TRAP`（a0=0）或 `HIT BAD TRAP`（a0≠0）。内置镜像 `img[]` 正是靠这条约定完成自检。

## 7. 下一步学习建议

- **横向对比变长指令**：下一讲 u5-l17 会切到 x86，看 `isa_exec_once` 如何变成「逐字节取指 + ModR/M 解码」，体会定长（RISC-V）与变长（x86）ISA 在译码复杂度上的天壤之别。届时你会更感激 RISC-V 把立即数摆放得如此规整。
- **补全指令集**：本讲综合实践只实现 6 条。PA 的后续阶段要求实现足够多的指令以跑通 AM 测试程序（`am-kernels`）。建议按 RV32I 手册逐条加：算术（`sub`/`sll`/`srl`/`slt`）、逻辑（`and`/`or`/`xor`）、分支（`bne`/`blt`/`bge`）、加载存储（`lb`/`lh`/`lw`/`lbu`/`lhu`/`sh`/`sw`）。每加一条都用差分测试（u8-l24）验证。
- **接续系统机制**：指令实现够用后，U7 单元会进入中断异常（u7-l21）与分页（u7-l22）。届时 `ebreak` 这条 trap 约定会演化为更完整的中断/异常状态机，`isa_mmu_check`（当前恒返回 `MMU_DIRECT`，见 [src/isa/riscv32/include/isa-def.h:31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L31)）会真正开始翻译虚拟地址。
- **推荐继续精读**：`src/isa/riscv32/inst.c`（本讲主文件，反复读）、`include/cpu/decode.h`（INSTPAT 框架，配合 u3-l11）、以及 AM 仓库里的测试程序源码（看真实程序如何用 `ebreak` + `a0` 报告结果）。
