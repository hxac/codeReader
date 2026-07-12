# 取指与译码数据结构

## 1. 本讲目标

上一讲（u3-l9）我们把 `exec_once` 当成一个黑盒，只关心它如何被 `execute` 循环反复调用、如何驱动 CPU 状态机。本讲要打开这个黑盒，拆解「执行一条指令」用到的核心数据结构与辅助函数。读完本讲你应当能够：

- 说清 `Decode` 结构体里每个字段的作用，理解它为什么是「一条指令的工作台」。
- 准确区分三种 PC——`pc`、`snpc`、`dnpc`——各自的语义与谁负责修改它们。
- 解释 `inst_fetch` 如何从内存取出指令字节并自动推进 PC。
- 描述 `invalid_inst` 在遇到非法指令时的诊断输出与 `NEMU_ABORT` 状态设置流程。

本讲只讲「取指」与「数据结构」，不涉及具体指令如何译码执行（那是 u3-l11 INSTPAT 的主题），也不涉及具体的内存读写实现（那是 U4 的主题）。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：真实 CPU 取一条指令要解决两件事——「去哪里取」和「取完后下一条在哪」。** 真实 CPU 里有个程序计数器（PC）指向当前指令。取完一条指令后，PC 通常自增一个指令长度，指向顺序上的下一条；但若当前指令是跳转/分支，PC 会被改成跳转目标。也就是说，「顺序下一条」和「实际要去的下一条」是两个不同的概念，只有在非跳转指令时它们才相等。NEMU 用 `snpc` 和 `dnpc` 两个变量分别表达这两件事，把它们彻底解耦。

**直觉二：模拟器每执行一条指令都需要一块临时空间来存放中间结果。** 比如取出来的指令编码、当前地址、译码出的操作数……这些信息只在执行这一条指令时有意义，下一条指令一来就被覆盖。NEMU 把这些临时信息打包成一个 `Decode` 结构体，像一块「工作台」，从 `exec_once` 一路传递到 ISA 相关的译码函数里。

此外请回忆 u3-l9 提到的几个事实：`exec_once(Decode *s, vaddr_t pc)` 是 ISA 无关骨架；它把具体工作委托给 `isa_exec_once(s)`；最后用 `cpu.pc = s->dnpc` 提交下一地址。本讲就是要把这三行的细节讲透。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `include/cpu/decode.h` | 定义 `Decode` 结构体，是本讲的核心数据结构。 |
| `include/cpu/ifetch.h` | 定义 `inst_fetch` 内联函数，负责取指并推进 PC。 |
| `src/engine/interpreter/hostcall.c` | 定义 `set_nemu_state` 与 `invalid_inst`，处理执行状态变更与非法指令诊断。 |
| `src/cpu/cpu-exec.c` | `exec_once` 在这里组装 `Decode`、调用 ISA、提交 PC，是数据流的「总装配车间」（u3-l9 已读，本讲复用其中行号）。 |
| `src/isa/riscv32/inst.c` | RISC-V 的 `isa_exec_once` 与 `decode_exec`，展示 `Decode` 在 ISA 层如何被消费。 |
| `include/cpu/cpu.h` | 定义 `NEMUTRAP` / `INV` 两个便捷宏。 |
| `src/isa/riscv32/include/isa-def.h` | 定义 `ISADecodeInfo`，决定 `Decode::isa` 字段的具体内容。 |

## 4. 核心概念与源码讲解

### 4.1 Decode 结构：一条指令的「工作台」

#### 4.1.1 概念说明

`Decode` 是 NEMU 在执行一条指令时使用的「上下文结构体」。你可以把它想象成 CPU 内部为「当前这条指令」临时腾出的一块工作台：上面摆着这条指令的地址、取出来的指令编码、运算中间结果，以及（可选的）一行用于追踪日志的文本。

它有几个重要特点：

- **生命周期极短**：只在执行一条指令期间存在，下一条指令一来，同样的字段就被新值覆盖。
- **栈上复用**：它在 `execute()` 里被声明为局部变量，整个循环复用同一个实例，避免每条指令都 `malloc`/`free`，这是模拟器保持高吞吐的关键。
- **框架与 ISA 的接缝**：`Decode` 大部分字段是 ISA 无关的（地址、PC），但有一个 `isa` 字段是 ISA 专属的（指令编码），通过这个字段，通用框架代码与各 ISA 的译码代码被干净地缝合在一起。

#### 4.1.2 核心流程

`Decode` 的流转过程：

```
execute()  ──声明一个栈上 Decode s──►  exec_once(&s, cpu.pc)
                                            │
                                            ├─ s->pc = pc        记录当前地址
                                            ├─ s->snpc = pc      初始化顺序下一地址
                                            ├─ isa_exec_once(s)  委托 ISA（取指 + 译码执行）
                                            └─ cpu.pc = s->dnpc  提交动态下一地址
```

在 ISA 侧（以 riscv32 为例），`isa_exec_once` 把取到的指令编码写进 `s->isa.inst`，再调用 `decode_exec(s)` 完成译码与执行。整条链路上，`Decode *s` 是唯一被传递的「大参数」，所有中间状态都挂在它身上。

#### 4.1.3 源码精读

`Decode` 的定义只有寥寥几行：

[include/cpu/decode.h:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L21-L27) 定义了 `Decode` 结构体，包含 `pc`、`snpc`、`dnpc` 三个地址字段、ISA 专属的 `isa` 字段，以及仅在开启指令追踪时才存在的 `logbuf`。

逐字段说明：

- `vaddr_t pc`：当前正在执行的指令的地址。
- `vaddr_t snpc`：static next pc，顺序下一地址（假设不跳转时下一条指令在哪）。
- `vaddr_t dnpc`：dynamic next pc，动态下一地址（实际要去的下一条指令在哪）。
- `ISADecodeInfo isa`：ISA 专属译码信息。注意它的类型 `ISADecodeInfo` 是通过 `isa.h` 的宏拼接得到的当前 ISA 专属类型（见 u1-l4）。对 riscv32 而言，它只有一个字段 `uint32_t inst`，即取出的 32 位指令编码。
- `logbuf[128]`：仅当定义了 `CONFIG_ITRACE` 时才编译进来（`IFDEF` 宏的功劳），用来拼装「地址 + 指令字节 + 反汇编」这一行追踪日志。

`ISADecodeInfo` 的 ISA 专属定义可见 riscv32 头文件：

[src/isa/riscv32/include/isa-def.h:L27-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L27-L29) 把 `ISADecodeInfo` 定义成只含一个 `uint32_t inst` 的结构体，这就是 `s->isa.inst` 的来源。换一个 ISA（如 x86），这个结构体的字段就会不同，但 `Decode` 框架部分一行都不用改。

再看 `Decode` 是在哪里被声明和复用的：

[src/cpu/cpu-exec.c:L74-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83) 在 `execute()` 里声明了栈上的 `Decode s;`，循环中反复把它的地址传给 `exec_once`。注意它声明在循环之外——同一段栈空间被反复覆盖使用，没有堆分配开销。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `Decode` 的字段在每条指令执行时被填入新值。

**操作步骤**：

1. 打开 `src/cpu/cpu-exec.c`，在 `exec_once` 函数体开头（`cpu.pc = s->dnpc;` 这一行之后）临时加一行打印（示例代码，非项目原有代码）：

   ```c
   printf("[exec_once] pc=0x%x snpc=0x%x dnpc=0x%x\n", s->pc, s->snpc, s->dnpc);
   ```

2. 重新 `make` 编译，运行内置镜像：`make run`（或直接运行生成的 `build/riscv32-nemu-interpreter`，二进制名依你的 `menuconfig` 配置而定）。

**需要观察的现象**：屏幕上会逐条打印每条指令的三个 PC 值。

**预期结果**：内置镜像共有 4 条指令（见 4.2.3），你能看到 4 行打印，最后一行因 `ebreak` 触发 trap 后停止。验证完毕**记得删掉这行调试打印**再继续后续实验。

**待本地验证**：若你的配置不是 riscv32，二进制名与指令条数会不同；以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Decode` 要声明成栈上局部变量并在循环外复用，而不是每条指令 `malloc` 一个？

**参考答案**：NEMU 每秒要模拟上百万条指令，若每条指令都 `malloc`/`free` 一次，堆分配的开销会主导运行时间。栈上复用同一段内存，既无分配开销，又能让字段自然地被下一条指令覆盖，是高性能模拟器的常规手法。

**练习 2**：`Decode::isa` 字段的类型为什么不是固定的 `uint32_t`，而要用 `ISADecodeInfo`？

**参考答案**：不同 ISA 取出的「指令表示」不同——riscv32 是一个 32 位定长编码，x86 则可能需要记录变长指令的多个字节与前缀信息。用 ISA 专属的 `ISADecodeInfo` 类型，框架代码就能保持 ISA 无关，而把差异封装进这一个字段（参见 u1-l4 的 ISA 抽象层）。

---

### 4.2 三种 PC：pc / snpc / dnpc

#### 4.2.1 概念说明

这是本讲最关键的概念。`Decode` 里并存三个「PC」，初学者很容易混淆。它们的关系是：

| 字段 | 含义 | 谁来写 | 何时确定 |
| --- | --- | --- | --- |
| `pc` | 当前指令的地址 | `exec_once` 入口 | 执行前就已知 |
| `snpc` | 顺序下一地址（不跳转时的下一条） | `inst_fetch` 取指时推进 | 取指完成时 |
| `dnpc` | 动态下一地址（实际要去的地方） | 译码执行体（默认 = snpc） | 译码执行后 |

最关键的区分是 **`snpc` 与 `dnpc`**：

- `snpc` 回答的是「这条指令有多长」——它等于 `pc + 指令长度`。对于 RISC-V 定长指令就是 `pc + 4`；对于 x86 变长指令则取决于实际取了几个字节。
- `dnpc` 回答的是「下一条该执行哪条指令」——它默认等于 `snpc`（顺序执行），但跳转/分支指令会把它改成目标地址。

把这两件事分开有两个好处：第一，取指逻辑只关心指令长度，只动 `snpc`；执行逻辑只关心控制流，只动 `dnpc`，互不干扰。第二，判断「是否发生跳转」变得极其简单——只要 `dnpc != snpc`，就是跳转。

#### 4.2.2 核心流程

一条指令执行过程中三个 PC 的演变：

```
进入 exec_once(s, pc):
    s->pc   = pc            // 记下当前地址
    s->snpc = pc            // snpc 从 pc 起步

isa_exec_once(s):
    inst_fetch(&s->snpc, 4) // 取 4 字节，snpc 自动 += 4  →  snpc = pc + 4
    decode_exec(s):
        s->dnpc = s->snpc   // 默认：顺序执行，dnpc = snpc
        // 若是跳转指令，执行体改写：s->dnpc = 目标地址

回到 exec_once:
    cpu.pc = s->dnpc        // 提交：真正更新 CPU 的 PC
```

用一个数学化描述：设指令长度为 \(L\)，跳转目标为 \(T\)，则

\[
\text{snpc} = \text{pc} + L,\qquad
\text{dnpc} = \begin{cases}\text{snpc} & \text{顺序执行}\\ T & \text{跳转/分支}\end{cases}
\]

而非跳转指令的判据就是 \(\text{dnpc} = \text{snpc}\)。

#### 4.2.3 源码精读

先看框架侧 `exec_once` 如何安置这三个 PC：

[src/cpu/cpu-exec.c:L43-L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L43-L47) 这四行是整个数据流的骨架：先记录 `pc` 并让 `snpc` 从 `pc` 起步，然后委托 ISA 取指译码（ISA 内部会推进 `snpc`、设置 `dnpc`），最后把 `dnpc` 提交给 `cpu.pc`。

再看指令长度是如何「反推」出来的。在 `CONFIG_ITRACE` 开启时，`exec_once` 用 `snpc - pc` 计算刚执行指令的长度：

[src/cpu/cpu-exec.c:L48-L52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L48-L52) 这里 `ilen = s->snpc - s->pc` 正是利用了「snpc 被 inst_fetch 推进了指令长度」这一事实，无需 ISA 显式回报长度。这是 snpc/dnpc 解耦设计的直接受益点。

再看 ISA 侧 `decode_exec` 如何设置 `dnpc` 的默认值：

[src/isa/riscv32/inst.c:L50-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L50-L51) 进入 `decode_exec` 的第一件事就是把 `dnpc` 设为 `snpc`——这就是「默认顺序执行」的约定。后续若匹配到跳转指令，其执行体（INSTPAT_MATCH 的展开）会覆盖 `s->dnpc` 为目标地址；若没匹配到跳转指令，`dnpc` 就保持等于 `snpc`，CPU 顺序往下走。

为了让你有具体画面，看内置镜像的 4 条指令（其中没有跳转指令，所以每条都满足 `dnpc == snpc`）：

[src/isa/riscv32/init.c:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L27) 这是 `init_isa` 烧录的内置镜像，依次是 `auipc`、`sb`、`lbu`、`ebreak`，外加一个数据字 `0xdeadbeef`。前三条都不是跳转，因此它们的 `dnpc` 都等于 `snpc = pc + 4`。

> ⚠️ 注意：基础代码的 `inst.c` 里**尚未实现 `jal` 等跳转指令**（只有 `auipc/lbu/sb/ebreak/inv`），完整指令实现见 u5-l16。因此内置镜像本身不会出现 `dnpc != snpc` 的情况。

#### 4.2.4 代码实践

**实践目标**：观察跳转指令执行后 `snpc` 与 `dnpc` 的差异，画出 `pc → snpc → dnpc → cpu.pc` 的数据流。

**操作步骤**（两种方案任选其一）：

- **方案 A（不实现新指令，先看顺序执行）**：沿用 4.1.4 加的打印行，运行内置镜像。观察前三条指令是否都满足 `snpc == pc + 4` 且 `dnpc == snpc`。

- **方案 B（实现一条 jal，亲眼看到跳转）**：在 `src/isa/riscv32/inst.c` 的 `INSTPAT_END()` 之前**临时**加一行（示例代码，仅供观察）：

  ```c
  INSTPAT("??????? ????? ????? ??? ????? 11011 11", jal    , J, s->dnpc = s->pc + imm);
  ```

  并在 `decode_operand` 的 `enum` 与 `switch` 里补上 `TYPE_J` 分支（`immJ` 可参考 RV 手册的 J 型立即数解码，本步骤仅为观察 dnpc，可先用一个固定偏移 `s->dnpc = s->pc + 0x10;` 验证现象）。重新编译后构造一个会执行 `jal` 的程序，或直接用 SDB 的 `si` 单步。

**需要观察的现象**：执行 `jal` 那一条时，打印行应显示 `snpc = pc + 4`，但 `dnpc` 是另一个值（跳转目标），即 `dnpc != snpc`；而下一条指令的 `pc` 正好等于这条的 `dnpc`。

**预期结果**：数据流形如

```
pc=0x80000000 ──取指──► snpc=0x80000004 ──默认──► dnpc=snpc=0x80000004（非跳转）
pc=0x80000004 ──取指──► snpc=0x80000008 ──jal改写──► dnpc=0x80000010（跳转）
pc=0x80000010 ...        // 下一条 pc == 上一条的 dnpc
```

最后 `cpu.pc` 被 `exec_once` 的 `cpu.pc = s->dnpc` 提交，闭环成立。

**待本地验证**：方案 B 涉及 J 型立即数解码与构造测试程序，具体数值以你本地实现为准；若只想确认机制，方案 A 已足够。

#### 4.2.5 小练习与答案

**练习 1**：为什么判断「是否发生跳转」可以用 `dnpc != snpc`？

**参考答案**：因为 `dnpc` 的默认值就是 `snpc`（在 `decode_exec` 开头设置），只有跳转/分支指令的执行体才会显式把 `dnpc` 改成目标地址。所以二者相等意味着顺序执行，不等意味着发生了跳转。

**练习 2**：如果把 `exec_once` 里 `s->snpc = pc;` 这行删掉，会发生什么？

**参考答案**：`snpc` 会保留上一条指令执行后的旧值（因为 `Decode` 是栈上复用的），`inst_fetch(&s->snpc, 4)` 就会从错误的地址取指，导致取出错误指令、`ilen` 计算错误，CPU 行为完全错乱。这说明 `snpc` 必须在每条指令开头被重置为 `pc`。

---

### 4.3 inst_fetch：取指并推进 PC

#### 4.3.1 概念说明

`inst_fetch` 是一个极简但极为关键的内联函数：它从给定地址读取 `len` 个字节当作指令编码，同时把「PC 指针」向前推进 `len`。它把「读内存」和「推进 PC」这两步捆绑成一次调用，让调用方不必分别处理。

它的设计有两个要点：

- **接收的是指针 `vaddr_t *pc`**：这样它能在函数内部直接修改调用方的 PC 变量。在 NEMU 里，传入的几乎总是 `&s->snpc`，所以「推进 PC」实际就是「推进 `snpc`」。
- **长度 `len` 可变**：RISC-V 总是传 4（定长）；x86 变长指令会多次调用、每次传不同 `len`。这一份代码同时服务两种 ISA。
- **走「取指专用」内存接口 `vaddr_ifetch`**：而非数据读写的 `vaddr_read`。在当前 NEMU 里二者实现相同，但在有独立 I-cache 或区分执行侧/数据侧 MMU 的系统里，取指与读数据可能走不同路径，因此预留了独立接口。

#### 4.3.2 核心流程

```
inst_fetch(&s->snpc, 4):
    inst = vaddr_ifetch(*pc, len)   // 从地址 *pc 读 len 字节
    (*pc) += len                    // 把 snpc 向前推进 len
    return inst                     // 返回读到的指令编码
```

调用方拿到返回值后，通常立刻把它存进 `s->isa.inst`，供后续译码使用。

#### 4.3.3 源码精读

`inst_fetch` 的完整定义：

[include/cpu/ifetch.h:L20-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/ifetch.h#L20-L24) 这五行就是全部：调用 `vaddr_ifetch` 读内存、把传入的 PC 指针加 `len`、返回指令。注意它是 `static inline`，会直接内联进 `isa_exec_once`，没有函数调用开销。

再看它在 RISC-V 里如何被使用：

[src/isa/riscv32/inst.c:L75-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78) `isa_exec_once` 把 `&s->snpc` 传给 `inst_fetch`，取回的 32 位编码写入 `s->isa.inst`，随后交给 `decode_exec` 译码执行。这一行同时完成了两件事：把指令字节存进 `Decode`，并把 `snpc` 推进 4。

`vaddr_ifetch` 的声明可见内存接口头：

[include/memory/vaddr.h:L21-L23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/vaddr.h#L21-L23) 这里并列声明了取指 `vaddr_ifetch`、读数据 `vaddr_read`、写数据 `vaddr_write` 三个接口。当前实现里 `vaddr_ifetch` 与 `vaddr_read` 都直接转发到物理内存（见 U4），但接口分开为将来分页/MMU 区分留下了扩展点。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码理解 `inst_fetch` 对 `snpc` 的副作用，验证「指令长度 = snpc 的推进量」。

**操作步骤**：

1. 阅读上面三段源码，确认 `inst_fetch(&s->snpc, 4)` 调用后 `s->snpc` 增加了 4。
2. 回到 `src/cpu/cpu-exec.c:L51` 的 `ilen = s->snpc - s->pc`，理解为何这个减法能正确得到 RISC-V 指令长度 4。
3. 思考题：若把 `inst.c` 里 `inst_fetch(&s->snpc, 4)` 的第二个参数改成 `2`，会发生什么？（**不要真的改源码**，只做分析。）

**需要观察的现象**：这是源码阅读型实践，重点在理解指针参数 `&s->snpc` 的副作用。

**预期结果**：你能口述出——「`inst_fetch` 通过指针参数修改了 `s->snpc`，使其增加 `len`；这正是 `ilen = snpc - pc` 得以工作的前提」。

**待本地验证**：第 3 步的思考题结论——`snpc` 只增加 2，`ilen` 变成 2，`cpu.pc` 会指向错误地址，且取到的 32 位指令只有低 16 位有效，译码将全面错乱。这只是分析，不建议真的修改。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inst_fetch` 的第一个参数是 `vaddr_t *pc`（指针），而不是 `vaddr_t pc`（值）？

**参考答案**：因为它需要同时完成「读内存」和「推进 PC」两件事。传指针才能在函数内部修改调用方（如 `s->snpc`）的值；若传值，调用方还要再写一行 `snpc += len`，既啰嗦又容易漏写。

**练习 2**：NEMU 为什么要单独留一个 `vaddr_ifetch`，而不直接复用 `vaddr_read`？

**参考答案**：取指和读数据在语义上是两件事——将来引入分页/MMU 时，执行侧（取指）与数据侧（读写）可能使用不同的页表项属性或不同的 TLB，某些架构（如 x86 的 NX 位）甚至对取指有独立检查。现在接口分开，是为未来的系统机制扩展预留接缝（详见 U4、U7）。

---

### 4.4 invalid_inst：非法指令的诊断与中止

#### 4.4.1 概念说明

当译码器遇到一条无法识别的指令时，NEMU 必须停下来并告诉用户「这条指令有问题」。这就是 `invalid_inst` 的职责。它本质上对应真实 CPU 的「非法指令异常」(illegal instruction exception)，但在教学模拟器里，它的处理方式更直接：打印详尽的诊断信息后，把状态置为 `NEMU_ABORT`，让主循环退出。

触发它的途径是译码表里那条「兜底」规则——所有合法指令模式都没匹配上时，最后一条全 `?` 的 `inv` 模式必然命中，它调用 `INV(s->pc)` 宏，展开就是 `invalid_inst(s->pc)`。

诊断信息会列举两种可能原因，这正是 NEMU 教学设计的贴心之处：

1. **这条指令根本没实现**（你的 PA 还没写到它）。
2. **某条指令实现错了**，导致本不该执行到这里时执行到了非法字节。

`invalid_inst` 还有一个容易忽略的细节：它在诊断时用 `inst_fetch` 再取了 8 个字节用于打印，但**取的是局部副本**，不会污染 `Decode` 里的 `snpc`。

#### 4.4.2 核心流程

```
译码全 ? 兜底命中  →  INV(s->pc)  →  invalid_inst(s->pc):
    ├─ 用局部 pc 再 inst_fetch 两次共 8 字节（不动 s->snpc）
    ├─ 打印 8 字节的十六进制（逐字节 + 两个 32 位字）
    ├─ 打印「两种可能原因」提示 + isa_logo（红色高亮）
    └─ set_nemu_state(NEMU_ABORT, thispc, -1)
                          │
                          ├─ difftest_skip_ref()   跳过下一次差分比对
                          ├─ nemu_state.state = NEMU_ABORT
                          ├─ nemu_state.halt_pc = thispc
                          └─ nemu_state.halt_ret = -1
返回后，execute() 检测到 state != NEMU_RUNNING 而 break，
cpu_exec 随后打印红色 "ABORT" 字样并输出 statistic。
```

#### 4.4.3 源码精读

先看两个便捷宏，它们是 ISA 代码与状态函数之间的桥梁：

[include/cpu/cpu.h:L26-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L26-L27) `NEMUTRAP` 与 `INV` 是两个语义化宏：`NEMUTRAP` 表示「教学约定的程序结束」（如 ebreak），`INV` 表示「遇到非法指令」。它们让 inst.c 里的代码读起来像 `NEMUTRAP(s->pc, R(10))`、`INV(s->pc)`，意图一目了然。

`INV` 在 riscv32 译码表里被兜底规则使用：

[src/isa/riscv32/inst.c:L66-L67](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L66-L67) 倒数第二条 INSTPAT 是全 `?` 模式，会匹配任何 32 位编码，执行体是 `INV(s->pc)`。这就是「所有未实现指令最终都走到 invalid_inst」的原因。（`INSTPAT` 机制本身是下一讲 u3-l11 的主题，此处只需理解它是一条「兜底匹配」。）

再看 `invalid_inst` 本体：

[src/engine/interpreter/hostcall.c:L28-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/hostcall.c#L28-L51) 整个诊断函数。注意几个细节：①`__attribute__((noinline))` 禁止内联，避免这段含 `printf` 的冗长代码被塞进热点译码路径；②它声明了**局部变量** `vaddr_t pc = thispc;`，用这个副本去 `inst_fetch`，所以不会改动 `Decode` 的 `snpc`；③它连续取两个 4 字节字 `temp[0]`、`temp[1]`，既能按字节打印（`p[0..7]`）也能按字打印；④结尾调用 `set_nemu_state(NEMU_ABORT, thispc, -1)`。

最后看 `set_nemu_state`：

[src/engine/interpreter/hostcall.c:L21-L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/hostcall.c#L21-L26) 它先调用 `difftest_skip_ref()`——因为 trap/abort 不是一条「正常指令」，参考模型（REF）不应该拿这一步与自己比对；然后设置 `nemu_state` 的三个字段：状态、停机 PC、返回码。`NEMU_ABORT` 会在 `cpu_exec` 的收尾 switch 里被映射成红色的 `ABORT` 输出（见 u3-l9 的 `cpu_exec` 与 u7-l23 的状态机）。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次 `invalid_inst`，观察它的完整诊断输出。

**操作步骤**（最稳妥的方式：让一条本来合法的指令「变得非法」）：

1. 打开 `src/isa/riscv32/inst.c`，**临时注释掉**第一条 `INSTPAT("??????? ????? ????? ??? ????? 00101 11", auipc ...)`（在行首加 `//`）。
2. 重新 `make` 编译。
3. 运行内置镜像：`make run`。因为内置镜像第一条指令就是 `auipc`（见 `init.c`），注释掉后它无法匹配任何已实现模式，只能命中兜底 `inv` → 触发 `invalid_inst`。
4. 观察终端输出。
5. **实验结束后务必取消注释、恢复源码**，再重新编译。

**需要观察的现象**：终端会打印类似下面的内容（具体字节依地址而定）：

```
invalid opcode(PC = 0x80000000):
        97 02 00 00 ef be ad de ...
        00000297 deadbeef...
There are two cases which will trigger this unexpected exception:
1. The instruction at PC = 0x80000000 is not implemented.
2. Something is implemented incorrectly.
Find this PC(0x80000000) in the disassembling result to distinguish which case it is.

If it is the first case, see
... (isa_logo) ...
If it is the second case, remember:
* The machine is always right!
* Every line of untested code is always wrong!
```

随后是红色的 `ABORT` 字样与 `statistic` 统计。

**预期结果**：第一条指令 `auipc`（编码 `0x00000297`，小端字节 `97 02 00 00`）的字节被准确打印；进程以 `NEMU_ABORT` 终止。

**待本地验证**：若你用的不是 riscv32，注释哪条 INSTPAT、内置镜像首条指令是什么都会不同；以你本地的 `init.c` 与输出为准。

#### 4.4.5 小练习与答案

**练习 1**：`invalid_inst` 里为什么要 `__attribute__((noinline))`？

**参考答案**：`invalid_inst` 含大量 `printf` 和 8 字节取指，代码体积大。若被内联进 `inv` 的 INSTPAT 展开处，会让每条指令的译码路径都背上这段冗长代码，污染 icache、增大二进制体积。标 `noinline` 强制它成为一个真正的函数调用，只在真正触发时才付出代价。

**练习 2**：`invalid_inst` 诊断时为什么用局部变量 `pc = thispc` 去取 8 字节，而不是直接用 `s->snpc`？

**参考答案**：因为它只想「读出来打印」，不想改动译码状态。若直接对 `&s->snpc` 调用 `inst_fetch`，会把 `snpc` 推进 8，破坏 `Decode` 的正确性（虽然马上就要 abort 了，但保持局部副本的写法更干净、更安全，也不会被差分测试等机制误读）。

---

## 5. 综合实践

把本讲四个模块串起来：给 `exec_once` 加一个「迷你 itrace」，亲手看到一条指令从取指到提交 PC 的完整生命周期，并在最后触发一次非法指令诊断。

**任务**：

1. 在 `src/cpu/cpu-exec.c` 的 `exec_once` 末尾（`cpu.pc = s->dnpc;` 之后、`#ifdef CONFIG_ITRACE` 块之前），加入如下**临时**调试代码（示例代码）：

   ```c
   int __ilen = s->snpc - s->pc;
   const char *__jump = (s->dnpc != s->snpc) ? "JUMP" : "seq ";
   printf("[trace] pc=0x%08x len=%d %s -> next=0x%08x\n",
          s->pc, __ilen, __jump, s->dnpc);
   ```

2. 重新编译并运行内置镜像，收集全部 `[trace]` 行。

3. 用收集到的数据，**手画数据流图**：对每条指令标注 `pc →（取 len 字节）→ snpc →（默认/跳转）→ dnpc → cpu.pc`。验证：①前三条非跳转指令的 `next` 等于 `pc + 4` 且标记为 `seq`；②`ebreak` 那条之后程序因 trap 停止。

4. 接着按 4.4.4 的方法**临时注释掉 `auipc` 的 INSTPAT**，重新编译运行，观察 `invalid_inst` 打印的字节是否与你 `[trace]` 里第一条指令的 `len`、地址一致，体会「取指长度」与「诊断字节」的关联。

5. **清理**：删除所有临时调试代码、取消所有注释，确认 `git diff` 只剩你无意的空白变动（或干净如初），重新编译确认内置镜像仍能 `HIT GOOD TRAP`。

**验收标准**：你能不查源码地讲清楚——一条指令执行时 `pc` 谁写、`snpc` 谁推进、`dnpc` 谁决定、最后怎么落到 `cpu.pc`；并能解释 `invalid_inst` 打印出的那 8 个字节分别对应内存里的什么。

## 6. 本讲小结

- `Decode` 是执行单条指令的「工作台」，栈上声明、循环复用；其中 `isa` 字段是 ISA 专属接缝（riscv32 下只含 `uint32_t inst`）。
- 三种 PC 分工明确：`pc` 是当前地址、`snpc` 是顺序下一地址（= `pc + 指令长度`）、`dnpc` 是动态下一地址（默认等于 `snpc`，跳转指令改写为目标）。
- 取指与控制流被彻底解耦：`inst_fetch` 只推进 `snpc`，执行体只改写 `dnpc`；`dnpc != snpc` 即判跳转，`ilen = snpc - pc` 反推指令长度。
- `inst_fetch(&s->snpc, len)` 把「读内存 + 推进 PC」合二为一，通过指针参数就地修改 `snpc`，并走取指专用接口 `vaddr_ifetch`。
- 非法指令由兜底 `inv` 规则触发 `INV(s->pc) → invalid_inst`，后者打印 8 字节诊断、列举两种原因，再经 `set_nemu_state(NEMU_ABORT, …)` 终止运行。
- `set_nemu_state` 还会调用 `difftest_skip_ref()`，确保这一异常步不参与差分比对。

## 7. 下一步学习建议

本讲把「取指」和「数据结构」讲透了，但刻意回避了译码表本身如何工作——`INSTPAT("??????? …", auipc, U, …)` 那种用 `0/1/?` 模式串匹配指令编码的机制，正是下一讲 **u3-l11 INSTPAT 模式匹配译码机制** 的主题，那里会拆解 `pattern_decode` 如何把模式串编译成 `key/mask/shift`、运行时如何用位掩码比对。

如果你对 `Decode` 被消费的下游更感兴趣，可以提前翻看 **u5-l16 RISC-V 指令实现**，看 `decode_operand` 如何从 `s->isa.inst` 里切出 I/U/S 型立即数与寄存器号，把本讲的「工作台」真正用起来。而 `invalid_inst` 最终落到的 `NEMU_ABORT` 状态如何被 `cpu_exec` 转化为进程返回码，则在 **u7-l23 NEMU trap 与执行状态机** 中系统讲解。
