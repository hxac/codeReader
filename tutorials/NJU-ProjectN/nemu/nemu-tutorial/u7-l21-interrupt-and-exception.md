# 中断与异常机制

## 1. 本讲目标

上一讲 u6-l20 把时钟中断链路铺到了 `timer_intr → dev_raise_intr → isa_query_intr`，并指出「最后一公里」——真正**响应**中断的 `isa_raise_intr` 与「在每条指令后查询」的接入点——都留待本讲。本讲就接通这最后一公里。

具体来说，学完本讲你应该能够：

- 说清「中断（异步）」与「异常（同步）」的区别，以及它们为何共用同一套 `isa_raise_intr` 接口；
- 解释 RISC-V 机器模式下「保存返回地址 `epc`、记录原因 `mcause`、跳到中断向量 `mtvec`、关中断」这套现场保存与转移流程；
- 实现 `isa_raise_intr(NO, epc)`：把 `epc` 存入 `mepc`、`NO` 存入 `mcause`、返回 `mtvec` 作为新 `pc`；
- 实现 `isa_query_intr()`：根据「挂起标志 + 中断使能」返回中断号或 `INTR_EMPTY`；
- 说清为什么中断查询必须放在「一条指令执行完」的边界（`isa_exec_once` 末尾），并正确接入。

本讲是 PA3（中断与分页）的核心之一。它消费 u6-l20 的挂起链路、u5-l16 的指令执行框架与 u4-l13 的访存接口，并为 u7-l22 分页下的异常（如缺页）奠定「异常如何被抛出」的基础。

## 2. 前置知识

### 2.1 同步异常 vs 异步中断

CPU 在运行中会被两类事件打断：

- **异常（exception，同步）**：由 CPU **正在执行的那条指令本身**引发，例如非法指令、地址未对齐、`ecall` 系统调用。它是「确定性的」——同一条指令重复执行必再触发。异常发生时，「现场」就是这条指令自己的地址。
- **中断（interrupt，异步）**：由 CPU **外部**的事件引发，例如时钟到点、键盘按下。它和当前正在执行哪条指令无关，是「随时可能到来」的。中断发生时，「现场」是「本该执行但还没执行的下一条指令」。

二者机制高度相似——都要「保存当前 PC、跳到一段处理程序、处理完返回」——所以 NEMU 用同一个接口 `isa_raise_intr(NO, epc)` 处理两者。它们的差别主要体现在 **`epc` 传什么**：

- 中断：`epc` = 被打断处**下一条**指令地址（被打断的指令已经执行完）；
- 异常：`epc` = **引发异常的那条**指令地址（通常需要重新执行或由处理程序跳过）。

这个差别由 `isa_raise_intr` 的**调用者**决定（见 4.4），`isa_raise_intr` 本身只负责「把传进来的 `epc` 存好」。

### 2.2 RISC-V 特权架构与 CSR

NEMU 的 riscv32 只实现机器模式（M-mode），不做保护（见 README「interrupt and exception: protection is not supported」）。M-mode 下有一组专用寄存器叫 **CSR（Control and Status Register，控制状态寄存器）**，它们不像 `gpr[0..31]` 那样由普通指令随意读写，而是由 `csrrw/csrrs/...` 等专用指令访问。本讲涉及的四个 CSR：

| CSR | 全称 | 作用 |
|-----|------|------|
| `mepc` | Machine Exception PC | 保存「返回地址」——中断/异常返回后应执行的指令地址 |
| `mcause` | Machine Cause | 保存「原因码」——最高位 1 表中断、0 表异常，低位是具体编号 |
| `mtvec` | Machine Trap Vector | 保存「中断向量」——trap 处理程序的入口地址 |
| `mstatus` | Machine Status | 保存全局状态，本讲关心其中的 **MIE**（机器中断使能）位 |

> 术语「trap」是 RISC-V 对「中断或异常发生后 CPU 转去处理」这一动作的统称。「中断向量（interrupt vector）」指 trap 处理程序的入口地址，在 RISC-V 里就存在 `mtvec` 里。

`mcause` 的编码（RV32，32 位）：

\[
\text{mcause} = \underbrace{\text{bit31}}_{\text{1=中断, 0=异常}} \;\Big|\; \underbrace{\text{bit30..0}}_{\text{原因编号}}
\]

本讲最常用的是**机器时钟中断**，其编号为 7，故 `mcause = 0x80000007`（最高位置 1 表示中断，低 7 表示机器时钟）。

| 事件 | mcause (RV32) | 说明 |
|------|---------------|------|
| 机器时钟中断 | `0x80000007` | bit31=1（中断）+ 编号 7 |
| 机器软件中断 | `0x80000003` | bit31=1 + 编号 3 |
| 机器外部中断 | `0x8000000B` | bit31=1 + 编号 11 |
| `ecall` from M-mode | `0x0000000B` | bit31=0（异常）+ 编号 11 |

注意「机器外部中断」和「ecall from M-mode」的低位都是 11，区分它们的是最高位——这就是为什么 `mcause` 要专门用一位区分中断/异常。

### 2.3 中断响应的时机——指令边界

硬件响应中断有一个铁律：**当前指令必须执行完，下一条还没取**。不会在一条指令执行到一半时插进去。这意味着 NEMU 必须在「一条客机指令执行完毕」的那个点上查询并响应中断——这正是 u6-l20 反复强调、却尚未接入的「检查点」。本讲 4.4 会把它接到 `isa_exec_once` 末尾。

### 2.4 前序术语回顾

- **`nemu_state.state` 五种状态**（`NEMU_RUNNING/STOP/END/ABORT/QUIT`，见 u3-l9、[include/utils.h:L23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L23)）：`timer_intr` 用 `NEMU_RUNNING` 守卫，保证只在 CPU 真正运行时才请求中断（见 u6-l20）。
- **三种 PC**（`pc`/`snpc`/`dnpc`，见 u3-l10）：`pc` 是当前指令地址，`snpc` 是顺序下一地址，`dnpc` 是动态下一地址。`exec_once` 末尾 `cpu.pc = s->dnpc` 提交 PC。本讲中，中断查询命中后会把 `s->dnpc` 改写为中断向量地址，从而「跳走」。
- **`isa_exec_once` / `decode_exec`**（见 u5-l16）：前者是「执行一条 ISA 指令」的入口，取指后调后者译码执行；本讲的中断检查点就加在二者之间之后。
- **`dev_raise_intr`**（见 u6-l20）：设备侧「拉起中断挂起」的函数，目前是空实现，本讲综合实践会接通它与 `isa_query_intr`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/isa/riscv32/system/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c) | `isa_raise_intr`（TODO，目前返回 0）、`isa_query_intr`（目前恒返回 `INTR_EMPTY`）——本讲主战场 |
| [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) | `isa_raise_intr` / `isa_query_intr` / `INTR_EMPTY` 的接口声明 |
| [src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) | `CPU_state` 定义——**当前只有 `gpr` + `pc`，需你自行添加 CSR 字段** |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | `execute` 主循环、`exec_once` 单步骨架（ISA 无关部分），中断检查的「宿主」 |
| [src/isa/riscv32/inst.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c) | `isa_exec_once`——中断检查点应加在此处末尾 |
| [src/device/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c) | `dev_raise_intr`（空实现）——设置挂起标志的地方（承接 u6-l20） |
| [src/device/timer.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c) | `timer_intr`——时钟信号到 `dev_raise_intr` 的桥（u6-l20 已讲） |
| [include/cpu/cpu.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h) | `NEMUTRAP`/`INV` 宏、`set_nemu_state`——对比「trap 退出」与「中断响应」的区别 |
| [src/cpu/difftest/dut.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c) | `ref_difftest_raise_intr`——差分测试下通知 REF 同步响应中断（详见 u8-l24） |

## 4. 核心概念与源码讲解

本讲按「先讲现场与向量是什么，再讲怎么保存与跳转，再讲怎么查询，最后讲在哪查询」的顺序，拆成四个最小模块：

1. **epc 与中断向量（CSR 概念）**：`mepc`/`mcause`/`mtvec`/`mstatus` 是什么、为何需要它们、它们目前在 NEMU 里还**不存在**；
2. **isa_raise_intr**：把 `epc` 存入 `mepc`、`NO` 存入 `mcause`、关中断、返回 `mtvec`；
3. **isa_query_intr 与 INTR_EMPTY**：根据挂起标志与中断使能返回中断号或哨兵；
4. **exec_once 中的中断检查点**：把查询接到每条指令执行完的边界。

### 4.1 epc 与中断向量——中断现场与 CSR

#### 4.1.1 概念说明

想象 CPU 正在跑用户程序，突然时钟中断来了。CPU 要做两件事：

1. **记住「我从哪儿被叫走」**，这样处理完才能回来继续。这个「回来的地址」就是 `epc`，存进 `mepc`。
2. **知道「我该去哪儿处理」**，即一段预先写好的处理程序入口，叫中断向量，存进 `mtvec`。

光有这两个还不够。回来之后 CPU 怎么知道「刚才到底是什么事把我叫走的」？所以还要把原因记下来——存进 `mcause`。另外，处理程序执行期间通常不希望再被中断打断（否则容易栈混乱），所以进中断时要**关中断**——这由 `mstatus` 的 MIE 位控制。

这套「保存现场 + 跳转」的状态，在 RISC-V 里就是上面四个 CSR。它们的访问不像通用寄存器那样用 `add`/`lw`，而是用 `csrrw`/`csrrs`/`csrw`/`csrr` 等**特权指令**——这些指令本身也是 PA 要实现的（属于 CSR 指令集），本讲不展开实现，只关注 `isa_raise_intr` 如何**写**它们。

#### 4.1.2 核心流程

一个 trap 被响应时，CSR 的变化（RISC-V 特权规范）：

```
进入 trap（isa_raise_intr 负责）:
  mepc    ← epc              # 保存返回地址
  mcause  ← NO               # 记录原因
  mstatus.MPIE ← mstatus.MIE # 保存「进 trap 前的中断使能」
  mstatus.MIE  ← 0           # 关中断（trap 处理期间屏蔽中断）
  pc      ← mtvec            # 跳到中断向量

返回（mret 指令负责，不属于本讲）:
  mstatus.MIE  ← mstatus.MPIE # 恢复进 trap 前的中断使能
  mstatus.MPIE ← 1
  pc      ← mepc             # 回到被打断处
```

`mtvec` 的值是**客机程序自己**通过 `csrw mtvec, ...` 写进去的（在 AM 的 CTE 初始化、或 Nanos 启动时）。NEMU 不设默认 `mtvec`——它只负责在响应 trap 时读取这个 CSR。这也是为什么「中断能正确跳转」要求客机程序已经把 `mtvec` 设好。

#### 4.1.3 源码精读

先看一个**关键事实**：当前 riscv32 的 `CPU_state` 里**根本没有 CSR 字段**。

```c
typedef struct {
  word_t gpr[MUXDEF(CONFIG_RVE, 16, 32)];
  vaddr_t pc;
} MUXDEF(CONFIG_RV64, riscv64_CPU_state, riscv32_CPU_state);
```

[src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24)：结构体只有通用寄存器 `gpr` 和 `pc`。也就是说，`mepc`/`mcause`/`mtvec`/`mstatus` 现在无处可存。所以实现中断机制的第一步，是**给 `CPU_state` 加上 CSR 字段**。这属于本讲的「前置改造」，下面是一份**示例代码**（非项目原有，需你自行添加到 isa-def.h）：

```c
/* 示例代码：在 isa-def.h 的 CPU_state 中添加 CSR 字段（PA3 待实现） */
typedef struct {
  word_t gpr[MUXDEF(CONFIG_RVE, 16, 32)];
  vaddr_t pc;
  /* CRs -- 机器模式控制状态寄存器 */
  word_t mepc;       // Machine Exception PC
  word_t mcause;     // Machine Cause
  word_t mtvec;      // Machine Trap Vector
  word_t mstatus;    // Machine Status（关心其中的 MIE 位）
} MUXDEF(CONFIG_RV64, riscv64_CPU_state, riscv32_CPU_state);
```

> 提示：`mstatus` 的 MIE 位是第 3 位（按 RISC-V 规范，`MSTATUS_MIE = 1 << 3`）。最小实现里你也可以用一个独立的 `bool intr_enable` 代替完整的 `mstatus`，但若后续要对接 AM/差分测试，按规范字段实现更稳妥。具体字段组织请按你的 PA 要求来。

四个 CSR 的语义对照（便于实现时对照）：

| CSR | 由谁写 | 何时写 | 本讲用途 |
|-----|--------|--------|----------|
| `mepc` | `isa_raise_intr` | 进 trap | 存 `epc`（返回地址） |
| `mcause` | `isa_raise_intr` | 进 trap | 存 `NO`（原因码） |
| `mtvec` | 客机程序（`csrw`） | 启动初始化 | `isa_raise_intr` 读取它作为新 `pc` |
| `mstatus` | `isa_raise_intr` / `mret` | 进/出 trap | 保存/恢复/开关 MIE |

#### 4.1.4 代码实践

**实践目标**：确认 CSR 字段缺失，并规划好要加哪些字段。

**操作步骤**（源码阅读 + 设计型实践）：

1. 打开 [src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24)，确认当前 `CPU_state` 只有 `gpr` 与 `pc`。
2. 对照 2.2 的 CSR 表，列出本讲至少需要哪几个字段（`mepc`/`mcause`/`mtvec`/`mstatus`），并想好它们在结构体里的位置与类型（都用 `word_t`）。
3. 思考：为什么把 CSR 放进 `CPU_state` 而不是另起一个全局变量？（提示：差分测试 `difftest_regcpy` 会整体搬运 `CPU_state`，放进去才能让 REF 同步看到 CSR——见 u5-l14、u8-l24。）

**预期结果**：能说清每个 CSR 的写入时机与读取方，并确认「CSR 字段需自行添加」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mepc` 存的是「返回地址」而不是「当前指令地址」？二者什么时候相同、什么时候不同？

**参考答案**：`mepc` 的语义是「trap 返回后应执行的指令地址」。对**中断**（异步），被打断的指令已执行完，返回时应执行其下一条，故 `mepc` = 下一条指令地址（与「当前指令地址」不同）。对**异常**（同步，如 `ecall`），引发异常的指令本身没「完成」，返回时常需重新执行或由处理程序跳过，故 `mepc` = 引发异常的指令地址（与「当前指令地址」相同）。所以「二者是否相同」取决于这次 trap 是中断还是异常。

**练习 2**：`mtvec` 由客机程序写入，NEMU 只读。如果客机程序忘记设 `mtvec`，会发生什么？

**参考答案**：`mtvec` 初值为 0（`CPU_state cpu = {};` 零初始化），`isa_raise_intr` 会返回 0 作为新 `pc`，于是 trap 后 CPU 跳到地址 0 继续取指，通常触发非法指令或越界，最终 `NEMU_ABORT`。这复刻了真实硬件「未设中断向量就响应中断」的灾难性后果。

---

### 4.2 isa_raise_intr——保存现场并跳转中断向量

#### 4.2.1 概念说明

`isa_raise_intr` 是「响应一个 trap」的核心函数。它的职责用一句话概括：**保存现场（`mepc`/`mcause`/`mstatus`），然后返回中断向量（`mtvec`）作为新的 `pc`**。

注意它的签名：

```c
vaddr_t isa_raise_intr(word_t NO, vaddr_t epc);
```

- 入参 `NO`：trap 的「编号」。在 RISC-V 里它直接就是 `mcause` 的值（如时钟中断 `0x80000007`）。这个抽象对 x86 则是「IDT 下标」（见 [src/isa/x86/system/intr.c:L19-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/system/intr.c#L19-L25) 的注释「use `NO` to index the IDT」）——`NO` 的具体含义是 ISA 相关的，接口本身不规定。
- 入参 `epc`：返回地址，由调用者决定（中断传 `snpc`、异常传 `pc`，见 2.1）。
- **返回值**：新 `pc`，即中断向量 `mtvec`。调用者会把它写进 `s->dnpc`，让 `exec_once` 末尾的 `cpu.pc = s->dnpc` 完成「跳走」。

「用返回值传新 `pc`」而不是直接改 `cpu.pc`，是为了和 NEMU 的 PC 流转模型一致——所有 PC 变更都经由 `dnpc` 提交（见 u3-l10），保持单一提交点。

#### 4.2.2 核心流程

```
isa_raise_intr(NO, epc):
  cpu.csr.mepc    = epc          # 1. 保存返回地址
  cpu.csr.mcause  = NO           # 2. 记录原因
  # 3. 保存并关闭中断（MIE → MPIE，MIE ← 0）
  if (NO 的最高位为 1，即中断):  # 仅对中断清 MIE（异常可不关，按 PA 要求）
      保存 mstatus.MIE 到 mstatus.MPIE
      mstatus.MIE = 0
  return cpu.csr.mtvec           # 4. 返回中断向量作为新 pc
```

第 3 步「关中断」是防止 trap 处理程序里又被同一个中断反复打断。最小实现里，如果你不打算支持嵌套中断，也可以暂时省略 MIE 处理，但要注意：若 `isa_query_intr` 不检查 MIE，时钟中断会每条指令都重新命中，CPU 永远在中断向量里打转出不来。所以「关中断」与 4.3 的「查询时检查 MIE」是配对的——要么都做，要么用别的方式（如查询后清挂起标志）避免风暴。

#### 4.2.3 源码精读

当前实现是空桩：

```c
word_t isa_raise_intr(word_t NO, vaddr_t epc) {
  /* TODO: Trigger an interrupt/exception with ``NO''.
   * Then return the address of the interrupt/exception vector.
   */

  return 0;
}
```

[src/isa/riscv32/system/intr.c:L18-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L18-L24)：注释点明两件事——「用 `NO` 触发一个中断/异常」「返回中断/异常向量的地址」。当前 `return 0` 意味着「什么都没保存，pc 跳到 0」——显然不可用，需你实现。

接口声明在 isa.h：

```c
// interrupt/exception
vaddr_t isa_raise_intr(word_t NO, vaddr_t epc);
#define INTR_EMPTY ((word_t)-1)
word_t isa_query_intr();
```

[include/isa.h:L49-L52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L49-L52)：注意 `isa_raise_intr` 返回 `vaddr_t`（即 `word_t`，见 [include/common.h:L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L42)），与 `mtvec`/`pc` 同宽，故可直接返回 `mtvec`。三行声明把「响应（raise）」「查询（query）」「空哨兵（INTR_EMPTY）」放在一起，构成完整的中断接口契约。

一份最小实现（**示例代码**，非项目原有）：

```c
/* 示例代码：isa_raise_intr 最小实现（需先在 CPU_state 加 CSR 字段） */
word_t isa_raise_intr(word_t NO, vaddr_t epc) {
  cpu.csr.mepc   = epc;     // 保存返回地址
  cpu.csr.mcause = NO;      // 记录原因码

  // 关中断：MIE → MPIE，MIE ← 0（仅对中断；最高位为 1 表示中断）
  if (NO >> (sizeof(word_t) * 8 - 1)) {
    // 把 mstatus.MIE 存到 MPIE，再清 MIE（按你定义的位布局实现）
    int mie = (cpu.csr.mstatus >> 3) & 1;
    cpu.csr.mstatus = (cpu.csr.mstatus & ~(1 << 7)) | (mie << 7); // MPIE <- mie
    cpu.csr.mstatus &= ~(1 << 3);                                  // MIE  <- 0
  }

  return cpu.csr.mtvec;     // 返回中断向量，作为新 pc
}
```

> 位布局说明：RISC-V 规范里 `mstatus.MIE` = bit 3、`mstatus.MPIE` = bit 7。上面用移位读写是为了强调「位操作」；如果你在 `CPU_state` 里用位域或单独的 `bool intr_enable` 字段，代码会更直观。具体按你的 PA 约定。

**差分测试的同步问题**（与 u8-l24 相关，这里提一句）：开启 `CONFIG_DIFFTEST` 时，NEMU（DUT）响应了一个 trap，必须让 REF（spike/qemu）也响应**同一个** trap，否则两边寄存器立刻对不上。机制是 `ref_difftest_raise_intr(NO)`——一个经 `dlsym` 从 REF 动态库加载的函数指针，声明在 [src/cpu/difftest/dut.c:L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L27)、加载于 [src/cpu/difftest/dut.c:L78-L79](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L78-L79)；REF 侧的对应导出是 [src/cpu/difftest/ref.c:L33-L35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/ref.c#L33-L35) 的 `difftest_raise_intr`（目前 `assert(0)`，待 REF 侧实现）。实践中通常在 `isa_raise_intr` 末尾 `IFDEF(CONFIG_DIFFTEST, ref_difftest_raise_intr(NO);)`。细节留待 u8-l24。

#### 4.2.4 代码实践

**实践目标**：把 `isa_raise_intr` 从「返回 0」改成「保存现场并返回 `mtvec`」。

**操作步骤**：

1. 先按 4.1 在 [src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) 的 `CPU_state` 里加上 `mepc`/`mcause`/`mtvec`/`mstatus` 字段。
2. 在 [src/isa/riscv32/system/intr.c:L18-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L18-L24) 把 `isa_raise_intr` 实现成「`mepc=epc`、`mcause=NO`、（关 MIE）、`return mtvec`」。
3. （验证手段，待本地验证）写一个最小客机程序：`csrw mtvec, handler` 设好向量，然后开中断、使能时钟中断、`wfi` 等中断；中断向量里先把 `a0` 置一个魔数再 `mret`。跑起来后用 SDB `info r` 看 `a0` 是否被改成魔数，从而确认 `pc` 确实跳到了 `mtvec`。若暂时写不出完整程序，至少在 `isa_raise_intr` 里加一行 `Log("raise_intr NO=0x%x epc=0x%x mtvec=0x%x", NO, epc, cpu.csr.mtvec);` 观察是否被调用。

**需要观察的现象**：`isa_raise_intr` 被调用时，`mepc` 应等于传入的 `epc`，返回值应等于 `mtvec`。

**预期结果**：`pc` 在中断命中后跳到 `mtvec` 指向的处理程序入口。完整可运行还需 4.3、4.4 与客机侧 `mret`/`csrw` 指令的实现。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`isa_raise_intr` 用「返回值」传递新 `pc`，而不是直接 `cpu.pc = mtvec`。这样设计的好处是什么？

**参考答案**：保持 PC 流转的单一提交点。NEMU 里所有 PC 变更都经由 `s->dnpc`，最后由 `exec_once` 的 `cpu.pc = s->dnpc` 统一提交（见 u3-l10）。`isa_raise_intr` 返回 `mtvec`、由调用者写入 `s->dnpc`，既复用了这条提交通路，又让「跳转」和普通指令的 PC 变更走同一套逻辑，便于追踪（itrace、difftest 也只需盯住 `dnpc`）。

**练习 2**：为什么 `NO` 在 RISC-V 里可以直接写入 `mcause`，而在 x86 里却要「用它索引 IDT」？

**参考答案**：两 ISA 对「中断号」的语义不同。RISC-V 的 `mcause` 直接编码「中断/异常位 + 原因编号」，`NO` 本身就是这个编码值，故可直接存。x86 的中断号是一个 0–255 的向量下标，真正要跳去的处理程序地址存在 IDT（中断描述符表）里、由这个下标索引得到，故 `NO` 是「索引」而非「地址」。`isa_raise_intr` 接口只规定「用 `NO` 触发 trap 并返回向量」，把 `NO` 的具体解释留给各 ISA。

---

### 4.3 isa_query_intr 与 INTR_EMPTY——查询挂起中断

#### 4.3.1 概念说明

`isa_raise_intr` 解决了「怎么响应」，但响应的前提是「知道有中断」。`isa_query_intr` 就负责这件事：**查询当前是否有挂起的中断**，有则返回其中断号（交给 `isa_raise_intr` 去响应），没有则返回一个特殊哨兵 `INTR_EMPTY`。

回顾 u6-l20 的链路：`SIGVTALRM → timer_intr → dev_raise_intr` 应当设置一个「时钟中断挂起」标志，`isa_query_intr` 查这个标志。但当前 `dev_raise_intr` 是空函数、`isa_query_intr` 恒返回 `INTR_EMPTY`，所以链路是断的。本节接通「挂起标志 ↔ 查询」这一段。

`INTR_EMPTY` 是「没有中断」的哨兵，定义在 isa.h：

```c
#define INTR_EMPTY ((word_t)-1)
```

[include/isa.h:L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h#L51)：`(word_t)-1` 是全 1（riscv32 下 `0xFFFFFFFF`）。选全 1 是因为合法的 `mcause` 值里，中断类最高位为 1 但低位是有意义的小编号，全 1 不会与任何真实中断号冲突——是一个安全的「空」值。用 `0` 不行，因为 `0` 是合法异常号（指令地址未对齐）。

#### 4.3.2 核心流程

```
dev_raise_intr():                 # 设备侧（u6-l20），置挂起标志
  timer_intr_pending = true

isa_query_intr():                 # CPU 侧，每条指令后调用
  if (timer_intr_pending && MIE 使能):
      timer_intr_pending = false  # 查询即「取走」，避免重复响应
      return IRQ_TIMER            # = 0x80000007（机器时钟中断的 mcause）
  return INTR_EMPTY               # 无中断或被屏蔽
```

两个关键设计：

1. **查询即取走**：`isa_query_intr` 返回中断号的同时清掉挂起标志，保证一次中断只被响应一次。否则下一条指令又会查到同一个挂起、再次响应，CPU 陷入死循环。
2. **检查 MIE**：中断应只在「全局中断使能（`mstatus.MIE`）打开」时才投递。这是避免「trap 处理程序里又被时钟中断反复打断」的第二道防线（第一道是 4.2 进 trap 时关 MIE）。若你 4.2 没做关 MIE，这里就必须查 MIE，否则时钟中断会风暴。

#### 4.3.3 源码精读

当前实现：

```c
word_t isa_query_intr() {
  return INTR_EMPTY;
}
```

[src/isa/riscv32/system/intr.c:L26-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L26-L28)：恒返回 `INTR_EMPTY`——「永远没有中断」。配合空的 `dev_raise_intr`（[src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19)），整条时钟中断链路当前完全不工作。

一个关键事实（u6-l20 已指出，这里再强调）：**当前仓库里没有任何代码调用 `isa_query_intr`**。它被声明、被定义（为桩），但没有执行路径会调它。这是因为「在每条指令后查询」这个接入点也是 TODO，4.4 会接上。

`dev_raise_intr` 与挂起标志的放置有个跨层问题：`dev_raise_intr` 当前定义在设备侧 [src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19)，而挂起标志与 `isa_query_intr` 属于 ISA 侧。两种常见组织方式：

- **方式 A**：把挂起标志放 ISA 侧 `intr.c`，`dev_raise_intr` 也挪到 ISA 侧实现（删掉设备侧那份，避免重复定义）；
- **方式 B**：设备侧 `dev_raise_intr` 调一个 ISA 侧函数（如 `isa_set_irq_pending()`）。

`timer_intr` 通过 `extern void dev_raise_intr();` 调用它（[src/device/timer.c:L34-L35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/timer.c#L34-L35)），所以只要你保留同名函数，无论放哪都能衔接。具体按你的 PA 要求。

一份最小实现（**示例代码**，非项目原有；假设挂起标志放 ISA 侧）：

```c
/* 示例代码：isa_query_intr 与 dev_raise_intr 的最小实现 */
#include <isa.h>

#define IRQ_TIMER (((word_t)1 << 31) | 7)   // 机器时钟中断的 mcause = 0x80000007

static bool timer_intr_pending = false;

void dev_raise_intr() {            // 覆盖 src/device/intr.c 里的空实现
  timer_intr_pending = true;
}

word_t isa_query_intr() {
  bool mie = (cpu.csr.mstatus >> 3) & 1;   // mstatus.MIE
  if (timer_intr_pending && mie) {
    timer_intr_pending = false;            // 查询即取走
    return IRQ_TIMER;
  }
  return INTR_EMPTY;
}
```

> 若你 4.2 没有关 MIE、也没在 `mstatus` 里维护 MIE 位，`isa_query_intr` 可退化为「只看挂起标志」。但务必保证「查询即取走」，否则同一中断会被每条指令重复响应。

#### 4.3.4 代码实践

**实践目标**：接通「挂起标志 ↔ 查询」，让 `isa_query_intr` 能在挂起时返回 `IRQ_TIMER`。

**操作步骤**：

1. 决定挂起标志的归属（方式 A 或 B，见 4.3.3），声明一个 `static bool timer_intr_pending`。
2. 实现 `dev_raise_intr`：置标志为真。注意处理与 [src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19) 原空实现的重复定义——二选一，不要两处都留函数体。
3. 实现 [src/isa/riscv32/system/intr.c:L26-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L26-L28) 的 `isa_query_intr`：挂起且 MIE 开 → 返回 `IRQ_TIMER` 并清标志，否则返回 `INTR_EMPTY`。
4. （验证手段，待本地验证）在 `isa_query_intr` 返回非 `INTR_EMPTY` 的分支加 `Log("query_intr hit IRQ_TIMER");`，跑一个会触发时钟信号的程序（或干脆 `c` 跑一段时间），观察日志是否出现该行。

**需要观察的现象**：CPU 全速运行约 16.6 ms（虚拟 CPU 时间）后，`isa_query_intr` 应命中一次并返回 `IRQ_TIMER`。

**预期结果**：日志中周期性出现命中记录（频率约 `TIMER_HZ=60` 次/秒 CPU 时间）。**待本地验证**——能否真正「响应」还需 4.4 接入查询点与 4.2 的 `isa_raise_intr`。

#### 4.3.5 小练习与答案

**练习 1**：`isa_query_intr` 在返回中断号时清掉挂起标志。如果不清会怎样？

**参考答案**：挂起标志一直为真，下一条指令执行完后 `isa_query_intr` 又返回同一个中断号，CPU 再次进入 `isa_raise_intr`、跳到 `mtvec`……于是 CPU 永远在「执行一条 handler 指令 → 又被同一个时钟中断打断 → 跳回 handler 开头」里死循环，永远退不出中断。清标志保证「一次挂起只响应一次」。

**练习 2**：`INTR_EMPTY` 定义为 `(word_t)-1`。在 riscv64（`word_t` 为 64 位）下它是多少？会和 64 位的 `mcause` 冲突吗？

**参考答案**：64 位下 `(word_t)-1` = `0xFFFFFFFFFFFFFFFF`。RISC-V 64 的 `mcause` 中断类是 bit 63 置 1 加低位编号，全 1 的低位 `0x7FFFFFF...` 不是任何合法编号，故仍不会冲突。这正是用「全 1」而非某个具体编号当哨兵的好处——它在任何位宽下都安全。

---

### 4.4 exec_once 中的中断检查点

#### 4.4.1 概念说明

4.2、4.3 给出了「响应」与「查询」的实现，但还差最关键一步：**谁来调用 `isa_query_intr`？在什么时候调？**

答案来自 2.3 的铁律——中断在「指令边界」响应。在 NEMU 里，「执行一条指令」的统一入口是 `isa_exec_once`（见 u5-l16），它取指后调 `decode_exec` 译码执行。所以最自然的检查点就是 **`isa_exec_once` 末尾、`decode_exec` 之后**：此时当前指令已完整执行、PC 已推进到 `snpc`，正是「下一条还没取」的边界。

这个位置是 ISA 相关的（在 `src/isa/riscv32/inst.c` 里），而非框架侧的 `exec_once`——因为「是否查询中断、查什么」与 ISA 强相关（x86 还要查 IF 标志、可屏蔽/不可屏蔽中断等）。框架侧的 `exec_once`（[src/cpu/cpu-exec.c:L43-L72](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L43-L72)）保持 ISA 无关，只负责 PC 流转与 itrace 组装。

#### 4.4.2 核心流程

```
isa_exec_once(s):                 # src/isa/riscv32/inst.c
  s->isa.inst = inst_fetch(&s->snpc, 4)   # 取指，推进 snpc
  decode_exec(s)                           # 译码并执行（更新 dnpc）
  # —— 中断检查点（待接入）——
  word_t intr_no = isa_query_intr()        # 有挂起中断吗？
  if (intr_no != INTR_EMPTY):
      s->dnpc = isa_raise_intr(intr_no, s->snpc)   # 响应：跳到 mtvec
  return 0
```

注意传给 `isa_raise_intr` 的 `epc` 是 **`s->snpc`**（顺序下一地址），符合 2.1 的中断语义：被打断的指令（`s->pc`）已执行完，返回时应执行其下一条（`s->snpc`）。把返回值写进 `s->dnpc`，框架侧 `exec_once` 末尾的 `cpu.pc = s->dnpc`（[src/cpu/cpu-exec.c:L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L47)）会自然完成「跳到 `mtvec`」。

与之对照，框架侧主循环每步还做两件与中断间接相关的事：

```c
static void execute(uint64_t n) {
  Decode s;
  for (;n > 0; n --) {
    exec_once(&s, cpu.pc);          // 内部含上面的中断检查点
    g_nr_guest_inst ++;
    trace_and_difftest(&s, cpu.pc);
    if (nemu_state.state != NEMU_RUNNING) break;   // 中断只是改 pc，不停机
    IFDEF(CONFIG_DEVICE, device_update());          // u6-l20：屏幕/键盘轮询
  }
}
```

[src/cpu/cpu-exec.c:L74-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83)：中断响应不会改变 `nemu_state.state`（它不是 `NEMU_END/ABORT`），所以循环不会 `break`，CPU 只是「跳到 handler 继续跑」。这与 `ebreak` 触发的 `NEMUTRAP`（置 `NEMU_END`，见 [include/cpu/cpu.h:L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L26)）截然不同——后者是「程序结束」，前者是「换个地方继续执行」。

#### 4.4.3 源码精读

当前 `isa_exec_once` 没有任何中断查询：

```c
int isa_exec_once(Decode *s) {
  s->isa.inst = inst_fetch(&s->snpc, 4);
  return decode_exec(s);
}
```

[src/isa/riscv32/inst.c:L75-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78)：取指 → `decode_exec` → 返回。`decode_exec` 内部会执行指令并设置 `s->dnpc`（见 [src/isa/riscv32/inst.c:L50-L73](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L50-L73)，开头 `s->dnpc = s->snpc;` 是「默认顺序执行」）。要接入中断，在 `decode_exec` 之后加查询。

接入示例（**示例代码**，非项目原有）：

```c
/* 示例代码：在 isa_exec_once 末尾接入中断查询 */
int isa_exec_once(Decode *s) {
  s->isa.inst = inst_fetch(&s->snpc, 4);
  decode_exec(s);

  word_t intr_no = isa_query_intr();
  if (intr_no != INTR_EMPTY) {
    s->dnpc = isa_raise_intr(intr_no, s->snpc);   // epc = snpc（中断语义）
  }
  return 0;
}
```

为何不放在框架侧 `exec_once`？因为 `isa_query_intr`/`isa_raise_intr` 本就是 ISA 接口，且不同 ISA 的「中断检查时机与条件」不同（x86 要看 `EFLAGS.IF`、区分可屏蔽/不可屏蔽）。把它留在 ISA 侧的 `isa_exec_once`，框架侧 `exec_once`（[src/cpu/cpu-exec.c:L43-L72](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L43-L72)）保持 ISA 无关——这是 NEMU「框架/ISA 接缝」划分的一贯风格（见 u3-l11、u5-l14）。

**与异常的对照**：异常（如 `ecall`、非法指令）不是在 `isa_exec_once` 末尾查询的，而是在**指令执行过程中**由指令自己主动调用 `isa_raise_intr`。例如 `ecall` 会在它的 `INSTPAT` 执行体里调 `isa_raise_intr(ECALL_M, s->pc)`——此时 `epc = s->pc`（引发异常的指令本身）。这说明同一套 `isa_raise_intr` 接口服务于两类 trap，差别只在「谁调、传什么 `epc`」：

| trap 类型 | 谁调用 isa_raise_intr | epc 传什么 |
|-----------|----------------------|-----------|
| 中断（异步） | `isa_exec_once` 末尾的查询点 | `s->snpc`（下一条） |
| 异常（同步） | 指令执行体（如 `ecall` 的 INSTPAT） | `s->pc`（本条） |

#### 4.4.4 代码实践

**实践目标**：把中断查询点接入 `isa_exec_once`，跑通「时钟中断 → 响应 → 返回」的闭环。

**操作步骤**：

1. 在 [src/isa/riscv32/inst.c:L75-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78) 的 `isa_exec_once` 里，`decode_exec(s)` 之后插入 4.4.3 的查询代码。
2. 确认 4.1（CSR 字段）、4.2（`isa_raise_intr`）、4.3（`isa_query_intr` + `dev_raise_intr`）均已实现。
3. 准备一个能验证闭环的客机程序（**待本地验证**）：它需要
   - 用 `csrw mtvec, handler` 设中断向量（需你已实现 `csrw` 指令）；
   - 用 `csrw mie`/`csrs mstatus` 打开时钟中断使能与全局 MIE；
   - 在 `handler` 里读 `mepc`、做点可见副作用（如往串口写一个字符或自增一个计数器）、再 `mret` 返回。
4. 开 ITRACE（见 u8-l25）运行，从 `nemu-log.txt` 里观察：`pc` 在某条指令后突然跳到 `mtvec`，若干条 handler 指令后又跳回 `mepc`。

**需要观察的现象**：ITRACE 日志里出现「顺序执行 → 跳到 `mtvec` → 执行 handler → 跳回 `mepc` 继续」的轨迹，且周期性出现（约每 16.6 ms CPU 时间一次）。

**预期结果**：时钟中断被正确响应并返回，程序不卡死、不 ABORT。**待本地验证**——能否跑通强依赖于你已实现 `csrw`/`mret`/`mie` 等 CSR 指令与客机程序本身。

#### 4.4.5 小练习与答案

**练习 1**：为什么中断查询放在 `isa_exec_once` 末尾（ISA 侧），而不是框架侧的 `exec_once` 或 `execute` 循环里？

**参考答案**：两个理由。其一，时机——中断必须在「一条指令执行完、下一条没取」的边界响应，`isa_exec_once` 正是「执行一条 ISA 指令」的入口，`decode_exec` 之后恰是这个边界。其二，ISA 相关性——「查不查、查什么、什么条件下投递」与 ISA 强相关（x86 要看 `EFLAGS.IF` 等），属 ISA 职责；框架侧 `exec_once`/`execute` 须保持 ISA 无关。放在 ISA 侧符合 NEMU 的接缝划分。

**练习 2**：中断命中后 `s->dnpc` 被改成 `mtvec`，但 `nemu_state.state` 没变。`execute` 循环会因此停吗？

**参考答案**：不会。`execute` 的 `break` 条件是 `nemu_state.state != NEMU_RUNNING`（[src/cpu/cpu-exec.c:L80](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L80)）。中断响应只改 `pc`（经 `dnpc`），不动 `nemu_state`，故状态仍是 `NEMU_RUNNING`，循环继续——CPU 只是「跳到 handler 接着跑」。这与 `ebreak`/`NEMUTRAP`（置 `NEMU_END`，会触发 `break` 并打印 `HIT ... TRAP`）性质完全不同：中断是「换地方继续」，trap 退出是「程序结束」。

---

## 5. 综合实践

**任务**：接通 u6-l20 遗留的完整时钟中断链路 `timer_intr → dev_raise_intr → isa_query_intr → isa_raise_intr`，并让一次时钟中断能被正确响应、处理后返回。这是本讲四个最小模块的串联通路。

### 第一步：补齐 CSR 基础设施

在 [src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24) 的 `CPU_state` 中加入 `mepc`/`mcause`/`mtvec`/`mstatus` 字段（参考 4.1.3 的示例）。若你已实现 `csrr`/`csrw` 指令，确认它们读写的就是这几个字段。

### 第二步：实现「挂起 → 查询」

1. 实现 `dev_raise_intr`（当前空实现于 [src/device/intr.c:L18-L19](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/intr.c#L18-L19)）：置一个时钟中断挂起标志（参考 4.3.3，注意避免与设备侧重复定义）。
2. 实现 [src/isa/riscv32/system/intr.c:L26-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L26-L28) 的 `isa_query_intr`：挂起且 MIE 开 → 返回 `IRQ_TIMER`（`0x80000007`）并清标志，否则返回 `INTR_EMPTY`。

### 第三步：实现「响应」

实现 [src/isa/riscv32/system/intr.c:L18-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c#L18-L24) 的 `isa_raise_intr`：`mepc ← epc`、`mcause ← NO`、（保存并关 MIE）、`return mtvec`（参考 4.2.3）。

### 第四步：接入查询点

在 [src/isa/riscv32/inst.c:L75-L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L75-L78) 的 `isa_exec_once` 末尾、`decode_exec(s)` 之后插入查询：`isa_query_intr()` 非 `INTR_EMPTY` 时 `s->dnpc = isa_raise_intr(intr_no, s->snpc);`（参考 4.4.3）。

### 第五步：验证闭环

1. 开 ITRACE（见 u8-l25）并设置合适的 `TRACE_START/END` 窗口。
2. 跑一个「设 `mtvec`、开 MIE/`mie.MTIE`、`wfi` 或忙等」的客机程序（在 AM 上跑需要时钟的程序亦可）。
3. 从 `nemu-log.txt` 中确认：`pc` 周期性跳到 `mtvec`，handler 执行若干条后 `mret` 跳回 `mepc`，主程序继续。
4. 若暂无完整客机程序，退而求其次：在 `dev_raise_intr`、`isa_query_intr` 命中分支、`isa_raise_intr` 三处各加一行 `Log(...)`，`c` 跑一段时间后确认三者按 `dev_raise_intr → isa_query_intr(命中) → isa_raise_intr` 的顺序周期性出现。

### 思考题

- 若把 `isa_raise_intr` 里「关 MIE」那步删掉、同时 `isa_query_intr` 也不检查 MIE，会发生什么？（答：每条 handler 指令执行完又查到同一个挂起——但 4.3 的「查询即取走」已经清了标志，所以不会再命中；真正的风险在于**新**的时钟中断在 handler 执行期间到来并打断 handler，可能导致 handler 还没 `mret` 就被重入。是否允许嵌套中断取决于你的设计。）
- `epc` 传 `s->snpc` 而非 `s->pc`，对中断返回语义意味着什么？（答：返回后执行被打断指令的**下一条**，因为被打断的指令已经完整执行过。）

## 6. 本讲小结

- 中断（异步）与异常（同步）共用 `isa_raise_intr(NO, epc)` 接口；差别在调用者传的 `epc`——中断传 `s->snpc`（下一条），异常传 `s->pc`（本条）。
- RISC-V 机器模式下，响应 trap 要保存四个 CSR：`mepc`（返回地址）、`mcause`（原因码，最高位区分中断/异常）、`mtvec`（中断向量，由客机程序 `csrw` 设定）、`mstatus`（其 MIE 位控制全局中断使能）。**这些 CSR 字段当前不在 `CPU_state` 里，需自行添加**。
- `isa_raise_intr(NO, epc)` 的实现：`mepc ← epc`、`mcause ← NO`、保存并关 MIE、返回 `mtvec` 作为新 `pc`（经 `s->dnpc` 提交，保持单一 PC 提交点）。
- `isa_query_intr()` 查询挂起中断：挂起标志 + MIE 使能时返回 `IRQ_TIMER`（`0x80000007`）并**清标志**（查询即取走），否则返回 `INTR_EMPTY = (word_t)-1`（全 1，安全哨兵，不与任何合法 `mcause` 冲突）。
- 中断检查点接在 `isa_exec_once` 末尾、`decode_exec` 之后——这是「一条指令执行完、下一条没取」的边界；放在 ISA 侧而非框架侧 `exec_once`，因为中断查询条件是 ISA 相关的。
- 中断响应只改 `pc`、不动 `nemu_state.state`，故 `execute` 循环不会 `break`，CPU「跳到 handler 继续跑」——这与 `ebreak` 触发 `NEMUTRAP`（置 `NEMU_END`、停机）性质截然不同。差分测试下还需 `ref_difftest_raise_intr(NO)` 通知 REF 同步（详见 u8-l24）。

## 7. 下一步学习建议

- **u7-l22 分页与 MMU 地址翻译**：分页启用后，缺页等异常也要经 `isa_raise_intr` 抛出——本讲是它的前置。同时 `isa_mmu_check`/`isa_mmu_translate` 与本讲的 `isa_query_intr`/`isa_raise_intr` 一样，都是「接口先行、实现后补」的 ISA 抽象，可对照体会 NEMU 的设计哲学。
- **u8-l24 差分测试机制**：本讲提到的 `ref_difftest_raise_intr` / `difftest_raise_intr` 是中断在 DUT/REF 间同步的关键，细节在该讲详述。实现中断后开 `CONFIG_DIFFTEST` 与 spike 对拍，是发现「`mepc`/`mcause`/MIE 处理错误」的最有效手段。
- **CSR 指令的实现**：本讲只讲 `isa_raise_intr` 如何**写** CSR，但 `csrw`/`csrr`/`mret` 等指令的实现（在 `inst.c` 里用 `INSTPAT` 新增）是跑通闭环的前提，建议在综合实践前先把它们补齐——尤其是 `mret`，它要恢复 MIE 并把 `pc` 设为 `mepc`，是 `isa_raise_intr` 的逆操作。
- **建议继续阅读的源码**：[src/isa/riscv32/system/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/system/intr.c)、[src/isa/riscv32/include/isa-def.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h) 与 [src/isa/x86/system/intr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/x86/system/intr.c)（对比 x86 用 `NO` 索引 IDT 的不同语义），并结合 RISC-V 特权规范对照 `mepc`/`mcause`/`mtvec`/`mstatus` 的位定义。
