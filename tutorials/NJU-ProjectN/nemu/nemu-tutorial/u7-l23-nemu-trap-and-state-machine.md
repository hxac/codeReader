# NEMU trap 与执行状态机

## 1. 本讲目标

真实计算机执行一段程序后，总得有个「停下来」的方式：要么程序正常结束，要么出错中止，要么用户手动打断。NEMU 作为一个教学全系统模拟器，必须把这套「停机语义」讲清楚，否则学生写的程序跑完之后无从判断对错。

本讲聚焦 NEMU 的**执行状态机**，学完后你应当能够：

- 理解 `nemu_trap` 这一「程序结束信号」的约定：为什么用一条 `ebreak` 指令充当 trap，返回值又放在哪个寄存器。
- 掌握 `NEMUState` 的五种状态（`NEMU_RUNNING/STOP/END/ABORT/QUIT`）以及它们之间的转换条件。
- 读懂 `NEMUTRAP` 与 `INV` 两个宏如何把「正常结束」与「非法指令」统一汇聚到 `set_nemu_state`。
- 区分屏幕上 `HIT GOOD TRAP`、`HIT BAD TRAP`、`ABORT` 三种输出的判定逻辑，并理解 `is_exit_status_bad` 如何把内部状态映射为进程返回码。

本讲是「程序如何结束」的收尾篇，把前面 CPU 主循环（u3-l9）与 RISC-V 指令实现（u5-l16）里留下的「`nemu_state`、`NEMUTRAP`、`invalid_inst`」一个个接通。

## 2. 前置知识

阅读本讲前，请确认你已了解以下概念（均在前序讲义中讲过）：

- **CPU 执行主循环**（u3-l9）：`cpu_exec(n) → execute(n) → exec_once` 三层调用链，以及 `execute` 循环里每步执行后会检查 `nemu_state.state != NEMU_RUNNING` 来决定是否 `break`。
- **INSTPAT 译码**（u3-l11）与 **RISC-V 指令实现**（u5-l16）：`decode_exec` 末尾的 `INSTPAT` 表，以及 `ebreak` 这条指令被复用为 `nemu_trap`、返回值约定在 `a0`（即 `R(10)`）。
- **Decode 与三种 PC**（u3-l10）：`s->pc`（当前地址）、`s->snpc`（顺序下一地址）、`s->dnpc`（动态下一地址），`ilen = snpc - pc`。
- **差分测试**（概念层面）：NEMU 作为 DUT，REF（如 spike/QEMU）作为参考实现，每步比对寄存器；差分测试失败也会终止运行。

几个本讲要用到的术语：

- **trap（陷阱）**：这里不是指中断/异常（那是 u7-l21 的内容），而是 NEMU 自定义的「程序结束信号」。客机程序执行到一条约定指令（riscv 下是 `ebreak`）即表示「我跑完了」，NEMU 据此停机。
- **halt_pc / halt_ret**：停机时的 PC 与返回码。`halt_ret` 来自 `a0`，`0` 表示成功，非 `0` 表示失败。
- **进程返回码**：NEMU 本身是个宿主机进程，`main` 最终 `return` 一个整数给操作系统，`0` 代表成功，非 `0` 代表失败。`is_exit_status_bad` 就是把 NEMU 内部状态翻译成这个返回码的函数。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `include/cpu/cpu.h` | 声明 `cpu_exec`、`set_nemu_state`、`invalid_inst`，定义 `NEMUTRAP`/`INV` 两个宏 |
| `src/engine/interpreter/hostcall.c` | 实现 `set_nemu_state`（状态写入闸口）与 `invalid_inst`（非法指令诊断） |
| `include/utils.h` | 定义五种状态的枚举与 `NEMUState` 结构体，声明全局 `nemu_state` |
| `src/utils/state.c` | 定义全局 `nemu_state` 的初值，实现 `is_exit_status_bad` |
| `src/cpu/cpu-exec.c` | CPU 主循环：`cpu_exec` 的状态守卫与收尾归类、`execute` 的循环退出条件、`statistic` 统计、`assert_fail_msg` 诊断转储 |
| `src/nemu-main.c` | `main` 在引擎结束后 `return is_exit_status_bad()` |
| `src/isa/riscv32/inst.c` | `ebreak` 与 `inv` 两条 INSTPAT，分别触发 `NEMUTRAP` 与 `INV` |
| `src/isa/riscv32/init.c` | 内置自检镜像 `img[]`，末尾以 `ebreak` 结束 |
| `include/debug.h` | `Assert`/`panic`/`TODO` 宏，调用 `assert_fail_msg` 后 `assert` 中止进程 |
| `src/device/device.c` | SDL 窗口关闭事件把状态置为 `NEMU_QUIT` |
| `src/cpu/difftest/dut.c` | 差分测试发现寄存器不一致时把状态置为 `NEMU_ABORT` |

## 4. 核心概念与源码讲解

### 4.1 NEMUTRAP / INV 宏——两种结束的统一入口

#### 4.1.1 概念说明

NEMU 里一条客机指令执行后想要「结束运行」，只有两条路径会主动停机：

1. **正常结束**：程序执行到约定的 trap 指令（riscv 下是 `ebreak`），表示「我跑完了，结果在 `a0`」。
2. **非法指令**：取到的指令没有任何一条 `INSTPAT` 能匹配，落到兜底的 `inv` 规则，表示「这条指令我不认识」。

这两条路径在 ISA 实现层（`inst.c`）看起来是两个不同的 `INSTPAT` 项，但它们都通过两个薄宏收敛到同一个状态写入函数：

- `NEMUTRAP(thispc, code)` → 正常结束，带走返回码 `code`。
- `INV(thispc)` → 非法指令，交给诊断函数处理。

把「停机动作」封装成宏的好处是：ISA 实现者只需在指令模式表里写 `NEMUTRAP(...)` 或 `INV(...)`，无需关心状态机细节；而状态机的真正逻辑集中在 `set_nemu_state` / `invalid_inst` 里，便于维护与统一诊断。

#### 4.1.2 核心流程

```
INSTPAT 表里的某条指令执行体
        │
        ├── NEMUTRAP(s->pc, R(10))   ──►  set_nemu_state(NEMU_END, pc, code)
        │                                      （正常结束，halt_ret = a0）
        │
        └── INV(s->pc)               ──►  invalid_inst(s->pc)
                                               ├── 打印 8 字节 opcode 诊断
                                               └── set_nemu_state(NEMU_ABORT, pc, -1)
                                                       （异常中止）
```

注意：两个宏最终都调用 `set_nemu_state`，差别只在传入的**状态值**（`NEMU_END` vs `NEMU_ABORT`）和**是否先做诊断输出**。

#### 4.1.3 源码精读

两个宏定义在 `include/cpu/cpu.h`：

[include/cpu/cpu.h:23-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L23-L27) —— 声明 `set_nemu_state`/`invalid_inst`，并把它们包装成 `NEMUTRAP`/`INV` 两个语义化宏。

```c
void set_nemu_state(int state, vaddr_t pc, int halt_ret);
void invalid_inst(vaddr_t thispc);

#define NEMUTRAP(thispc, code) set_nemu_state(NEMU_END, thispc, code)
#define INV(thispc) invalid_inst(thispc)
```

`NEMUTRAP` 直接展开为 `set_nemu_state(NEMU_END, ...)`；`INV` 展开为 `invalid_inst(...)`（诊断函数内部再去调 `set_nemu_state(NEMU_ABORT, ...)`）。

riscv32 的 INSTPAT 表里这两条规则紧挨着，位于所有真实指令之后、作为收尾：

[src/isa/riscv32/inst.c:66-67](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L66-L67) —— `ebreak` 触发 `NEMUTRAP`，返回码取自 `R(10)`（即 `a0`）；全 `?` 的 `inv` 兜底触发 `INV`。

```c
INSTPAT("0000000 00001 00000 000 00000 11100 11", ebreak , N, NEMUTRAP(s->pc, R(10))); // R(10) is $a0
INSTPAT("??????? ????? ????? ??? ????? ????? ??", inv    , N, INV(s->pc));
```

这是一个跨 ISA 的约定：每种 ISA 都挑一条「原本就该停下来」的指令复用为 `nemu_trap`，并从该 ISA 约定的返回值寄存器取返回码。例如 x86 用 `0xcc`（`int3`）作 trap、返回码取 `cpu.eax`；mips32 用 `sdbbp`、返回码取 `R(2)`（`$v0`）；loongarch32r 用 `break`、返回码取 `R(4)`（`$a0`）。机制完全一致，只是接缝不同。

#### 4.1.4 代码实践

1. **实践目标**：确认「正常结束」与「非法指令」确实走两个不同的 INSTPAT 项，并理解它们如何汇聚到 `set_nemu_state`。
2. **操作步骤**：
   - 打开 `src/isa/riscv32/inst.c`，定位 L66–L67 的 `ebreak` 与 `inv`。
   - 用编辑器全局搜索 `NEMUTRAP(` 与 `INV(`，观察它们在四种 ISA（riscv32/x86/mips32/loongarch32r）的 `inst.c` 里各自的调用点与返回码寄存器。
3. **需要观察的现象**：四种 ISA 都各有一处 `NEMUTRAP` 与一处 `INV`，且 `NEMUTRAP` 的第二个参数都是该 ISA 的「返回值寄存器」。
4. **预期结果**：你会看到 riscv 用 `R(10)`、x86 用 `cpu.eax`、mips 用 `R(2)`、loong 用 `R(4)`，印证「同一套 trap 机制、不同接缝」的设计。
5. 本实践为源码阅读型，无需运行，结论可直接从源码得出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `inv` 规则的模式串必须是全 `?`，且必须放在 INSTPAT 表的最后？

**答案**：`INSTPAT` 是自上而下逐条匹配的，全 `?` 表示「任意 32 位指令都匹配」，所以它能兜住所有未被前面规则识别的指令。若它不在最后，会提前吞掉后续规则；若不全 `?`，则某些非法指令漏网、行为未定义。

**练习 2**：如果某条已实现指令的模式写错了（比如 `addi` 的某位漏写），运行时会看到什么现象？

**答案**：该 `addi` 编码匹配不到对应规则，会落到 `inv`，被当作非法指令，触发 `INV(s->pc) → invalid_inst → NEMU_ABORT`，屏幕打印 `ABORT` 与 8 字节 opcode。这也是 `invalid_inst` 提示里「case 1: 未实现 / case 2: 实现错了」中 case 2 的典型场景。

### 4.2 set_nemu_state——状态写入的唯一闸口

#### 4.2.1 概念说明

`nemu_state` 是一个全局结构体，记录 NEMU 当前的执行状态。谁都能改它就乱套了，所以 NEMU 把「写状态」收敛到一个函数 `set_nemu_state(state, pc, halt_ret)`：任何想改变停机状态的代码都应当走这里，而不是直接 `nemu_state.state = ...`。

它做三件事：先通知差分测试「跳过接下来 REF 的比对」，再原子地写入三个字段（状态、停机 PC、返回码）。把 `difftest_skip_ref()` 放在这里，是因为停机动作（如 `ebreak`）是 NEMU 自定义约定，REF 不一定以同样方式对待，若不跳过会触发误报。

#### 4.2.2 核心流程

```
set_nemu_state(state, pc, halt_ret):
  1. difftest_skip_ref()        // 让 REF 跳过本次比对，避免 trap 指令误报
  2. nemu_state.state    = state
  3. nemu_state.halt_pc  = pc
  4. nemu_state.halt_ret = halt_ret
```

调用者只需关心「要切到哪个状态、停在哪、返回码多少」，副作用（差分跳过）由本函数统一注入。

#### 4.2.3 源码精读

[src/engine/interpreter/hostcall.c:21-26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/hostcall.c#L21-L26) —— `set_nemu_state` 的全部实现，先 `difftest_skip_ref()` 再写三字段。

```c
void set_nemu_state(int state, vaddr_t pc, int halt_ret) {
  difftest_skip_ref();
  nemu_state.state = state;
  nemu_state.halt_pc = pc;
  nemu_state.halt_ret = halt_ret;
}
```

`difftest_skip_ref` 在未开启差分测试时是个空函数，零开销：

[include/cpu/difftest.h:30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/difftest.h#L30) —— `CONFIG_DIFFTEST` 关闭时的空桩，使 `set_nemu_state` 在无差分测试时不受影响。

```c
static inline void difftest_skip_ref() {}
```

值得注意：并非所有 `NEMU_ABORT` 都经 `set_nemu_state`。差分测试发现寄存器不一致时，`checkregs` 直接写 `nemu_state`（见 4.4 节），这是「绕过闸口」的一个例外——因为那时已经不需要 `difftest_skip_ref`（差分本身就在报错），且需要立即 `isa_reg_display()` 打印现场。

#### 4.2.4 代码实践

1. **实践目标**：理解 `set_nemu_state` 是状态写入的统一入口，并验证 `difftest_skip_ref` 在不同配置下的形态。
2. **操作步骤**：
   - 阅读 `src/engine/interpreter/hostcall.c:21-26`。
   - 在 `src/cpu/difftest/dut.c` 中找到 `difftest_skip_ref` 的真实实现（约 L36），对比 `include/cpu/difftest.h:30` 的空桩，理解 `#ifdef CONFIG_DIFFTEST` 如何在两份实现间切换。
3. **需要观察的现象**：开启 `CONFIG_DIFFTEST` 时 `difftest_skip_ref` 会置 `is_skip_ref = true`；关闭时是空函数。
4. **预期结果**：确认 `set_nemu_state` 的「差分跳过」副作用只在开启差分测试时生效，普通运行无额外开销。
5. 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set_nemu_state` 要把 `difftest_skip_ref()` 放在写状态**之前**，而不是之后？

**答案**：`difftest_skip_ref` 设置的是「下一步比对时跳过 REF」的标志。trap 指令（`ebreak`）本身在 REF 侧的行为与 NEMU 不同，必须在本条指令的比对发生之前就标记跳过；若放后面，本步比对已经误报。放在写状态之前也保证了一旦进入停机流程，差分逻辑先被妥善处理。

**练习 2**：如果想让「用户按 Ctrl-C 退出」也走 `set_nemu_state`，应该传什么状态？

**答案**：传 `NEMU_QUIT`（用户主动退出）。不过当前 NEMU 的 `q` 命令并未这么做（见 4.5 节），这正是 PA1 让学生修复的「退出状态」问题之一。

### 4.3 invalid_inst 诊断——非法指令的第一现场

#### 4.3.1 概念说明

当指令匹配到 `inv`，NEMU 不会默默崩溃，而是尽可能多地把现场信息打印出来，帮助学生判断到底是「指令没实现」还是「实现错了」。这就是 `invalid_inst(thispc)` 的职责：它是非法指令的「第一现场报告」。

它做四件事：从出错 PC 处再取 8 字节、按字节与按字两种格式打印、给出两种可能原因的提示、最后把状态置为 `NEMU_ABORT`。其中 `__attribute__((noinline))` 是有意的——保证这个函数不会被内联，栈回溯时能清晰看到调用栈。

#### 4.3.2 核心流程

```
invalid_inst(thispc):
  1. 用 inst_fetch 从 thispc 连续取 2 个 4 字节字（temp[0], temp[1]）
  2. 把 temp 当 uint8_t* 打印前 8 字节十六进制
  3. 再按两个 32 位字打印
  4. 打印提示：
       - case 1: 该 PC 的指令未实现
       - case 2: 某处实现有误
     并附 isa_logo（ASCII 教学提示）
  5. set_nemu_state(NEMU_ABORT, thispc, -1)
```

注意第 1 步用了一个**本地副本** `vaddr_t pc = thispc`，再用 `inst_fetch(&pc, 4)` 推进——因为 `inst_fetch` 会修改传入的 PC 指针（推进 `snpc`），不能直接动 `thispc` 这个参数。

#### 4.3.3 源码精读

[src/engine/interpreter/hostcall.c:28-51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/hostcall.c#L28-L51) —— `invalid_inst` 全文：取 8 字节、双格式打印、两案例提示、置 `NEMU_ABORT`。

```c
__attribute__((noinline))
void invalid_inst(vaddr_t thispc) {
  uint32_t temp[2];
  vaddr_t pc = thispc;
  temp[0] = inst_fetch(&pc, 4);
  temp[1] = inst_fetch(&pc, 4);

  uint8_t *p = (uint8_t *)temp;
  printf("invalid opcode(PC = " FMT_WORD "):\n"
      "\t%02x %02x %02x %02x %02x %02x %02x %02x ...\n"
      "\t%08x %08x...\n",
      thispc, p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], temp[0], temp[1]);
  // ... 两案例提示与 isa_logo ...
  set_nemu_state(NEMU_ABORT, thispc, -1);
}
```

几个要点：

- `FMT_WORD` 是按 ISA 位宽自适应的地址格式串（riscv32 下为 `"0x%08" PRIx32`），定义在 [include/common.h:40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L40)。
- 打印用了「字节视图」与「字视图」两种格式：前者方便对照反汇编的字节序，后者方便对照手册里的 32 位编码。
- 末尾 `set_nemu_state(NEMU_ABORT, thispc, -1)`：返回码传 `-1`（无意义，因为 `NEMU_ABORT` 分支不关心 `halt_ret`，见 4.5 节）。

`invalid_inst` 与另一个诊断函数 `assert_fail_msg` 互补：前者面向「非法指令」，后者面向「`Assert`/`panic` 断言失败」。

[src/cpu/cpu-exec.c:94-97](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L94-L97) —— `assert_fail_msg` 在断言失败时打印寄存器与统计，是崩溃排错的第一现场。

```c
void assert_fail_msg() {
  isa_reg_display();
  statistic();
}
```

它被 `Assert`/`panic`/`TODO` 宏调用：

[include/debug.h:27-41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/debug.h#L27-L41) —— `Assert` 宏在条件失败时调 `assert_fail_msg()` 再 `assert(cond)` 中止进程；`panic` 与 `TODO` 都建立在 `Assert` 之上。

```c
#define Assert(cond, format, ...) \
  do { \
    if (!(cond)) { \
      /* ... 打印红色错误信息 ... */ \
      extern void assert_fail_msg(); \
      assert_fail_msg(); \
      assert(cond); \
    } \
  } while (0)
#define panic(format, ...) Assert(0, format, ## __VA_ARGS__)
#define TODO() panic("please implement me")
```

注意 `Assert` 路径最后调的是标准库 `assert(cond)`，它会直接 `abort()` 进程（触发 `SIGABRT`），**不经过 `cpu_exec` 的收尾 switch**——所以断言失败时你不会看到 `HIT BAD TRAP`，而是看到 `assert_fail_msg` 的寄存器转储后进程被信号终止。

#### 4.3.4 代码实践

1. **实践目标**：亲手触发一次 `invalid_inst`，观察它的诊断输出格式。
2. **操作步骤**：
   - 打开 `src/isa/riscv32/init.c`，找到内置镜像 `img[]`（L21–L27）。
   - 把**第一条**指令 `0x00000297`（`auipc t0,0`）临时改成 `0x00000000`。`0x00000000` 的 opcode 字段（低 7 位）为 `0000000`，不匹配 `auipc`/`lbu`/`sb`/`ebreak` 中的任何一个，会落到 `inv`。
   - 重新编译并运行内置镜像（不带 `-i` 参数）。
3. **需要观察的现象**：屏幕应打印 `invalid opcode(PC = 0x80000000):`，下面跟 8 字节十六进制与两个 32 位字，再跟两案例提示与红色 `isa_logo`，最后 `nemu: ABORT at pc = 0x80000000`。
4. **预期结果**：因为第一条指令即非法，`halt_pc` 应为 `RESET_VECTOR`（`0x80000000`），8 字节里前 4 字节为 `00 00 00 00`（你改的 `0x00000000`），后 4 字节为 `23 88 02 00`（原第二条 `sb` 的小端表示）。实际字节序输出待本地验证。
5. 验证后记得把 `img[0]` 改回 `0x00000297`。

#### 4.3.5 小练习与答案

**练习 1**：`invalid_inst` 为什么要取 **8** 字节而不是 4 字节？

**答案**：riscv32 是定长 4 字节指令，取 4 字节够看当前指令；但取 8 字节能同时看到「下一条指令」的字节，便于在反汇编结果里定位上下文、判断是不是因为上一条跳转指令算错地址才跳到了非法位置。这也是它打印 `...` 省略号表示「后面还有」的原因。

**练习 2**：`invalid_inst` 里为什么先 `vaddr_t pc = thispc;` 再 `inst_fetch(&pc, ...)`，而不是直接 `inst_fetch(&thispc, ...)`？

**答案**：`inst_fetch` 的第一个参数是 `vaddr_t *`，它会修改该指针指向的值（推进 PC）。`thispc` 是函数参数（也是 `invalid_inst` 要报告的出错地址），若被推进就丢失了「出错 PC」信息。先用副本 `pc` 接住推进，`thispc` 保持不变，后续 `printf` 与 `set_nemu_state(..., thispc, ...)` 才能用对地址。

### 4.4 NEMUState 五种状态与状态机

#### 4.4.1 概念说明

NEMU 的整个执行生命周期被建模成一个有限状态机，状态变量是全局 `nemu_state.state`，取值有五种。理解这五种状态的含义与转换条件，是读懂 `cpu_exec` 的关键。

五种状态（枚举值即为编号）：

| 状态 | 值 | 含义 |
| --- | --- | --- |
| `NEMU_RUNNING` | 0 | CPU 正在执行指令 |
| `NEMU_STOP` | 1 | 暂停（单步结束、监视点命中、初始态） |
| `NEMU_END` | 2 | 程序正常结束（trap，`halt_ret` 区分好坏） |
| `NEMU_ABORT` | 3 | 异常中止（非法指令、差分失败） |
| `NEMU_QUIT` | 4 | 用户退出（SDL 关窗） |

承载它们的结构体 `NEMUState` 除了 `state`，还记录停机时的 `halt_pc` 与 `halt_ret`，供收尾时打印与判定。

#### 4.4.2 核心流程

状态转换图（箭头标注触发条件）：

```
                       init
                        │
                        ▼
                   NEMU_STOP ◄─────────────────────────────┐
                        │                                    │
            cpu_exec 进入, default 分支                       │
                        │                                    │
                        ▼                                    │
                   NEMU_RUNNING                               │
                  /     |      \                              │
       execute 跑完  | ebreak   inv/difftest   SDL_QUIT       │
       未触发停机   | NEMUTRAP  NEMU_ABORT     (关窗)         │
                |     |          |              |            │
                ▼     ▼          ▼              ▼            │
          NEMU_STOP NEMU_END  NEMU_ABORT    NEMU_QUIT        │
                |     |          |              |            │
                |     |  (cpu_exec 收尾 switch 统一处理)      │
                |     |          |              |            │
                └─────┴──────────┴──────────────┴────────────┘
                     （下次 cpu_exec 被 guard 拦截或
                       主循环退出后 is_exit_status_bad 判定）
```

要点：

- **进入 `cpu_exec` 时**，若已是终止态（`END/ABORT/QUIT`），直接打印「已结束」并返回；否则切到 `NEMU_RUNNING`。
- **`execute` 循环每步后**检查 `state != NEMU_RUNNING` 即 `break`，把控制权交回 `cpu_exec`。
- **`cpu_exec` 收尾 switch**：`NEMU_RUNNING`（没触发停机，如 `si N` 跑完）回落为 `NEMU_STOP`；`NEMU_END`/`NEMU_ABORT` 打印 `HIT GOOD/BAD TRAP` 或 `ABORT` 并 `fall through` 到 `statistic()`；`NEMU_QUIT` 也调 `statistic()`。

#### 4.4.3 源码精读

状态枚举与结构体定义在 `include/utils.h`：

[include/utils.h:23-31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L23-L31) —— 五种状态枚举、`NEMUState` 结构体、全局 `nemu_state` 声明。

```c
enum { NEMU_RUNNING, NEMU_STOP, NEMU_END, NEMU_ABORT, NEMU_QUIT };

typedef struct {
  int state;
  vaddr_t halt_pc;
  uint32_t halt_ret;
} NEMUState;

extern NEMUState nemu_state;
```

全局变量初值在 `src/utils/state.c`：

[src/utils/state.c:18](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/state.c#L18) —— `nemu_state` 初值为 `NEMU_STOP`（上电后未运行）。

```c
NEMUState nemu_state = { .state = NEMU_STOP };
```

`execute` 循环的退出条件在 `src/cpu/cpu-exec.c`：

[src/cpu/cpu-exec.c:74-83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83) —— 每步执行后检查 `nemu_state.state != NEMU_RUNNING` 即 `break`。

```c
static void execute(uint64_t n) {
  Decode s;
  for (;n > 0; n --) {
    exec_once(&s, cpu.pc);
    g_nr_guest_inst ++;
    trace_and_difftest(&s, cpu.pc);
    if (nemu_state.state != NEMU_RUNNING) break;
    IFDEF(CONFIG_DEVICE, device_update());
  }
}
```

`cpu_exec` 的入口守卫与收尾归类：

[src/cpu/cpu-exec.c:100-128](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L100-L128) —— 入口拦截已终止态、收尾 switch 打印 `HIT GOOD/BAD TRAP`/`ABORT` 并 `statistic()`。

```c
void cpu_exec(uint64_t n) {
  g_print_step = (n < MAX_INST_TO_PRINT);
  switch (nemu_state.state) {
    case NEMU_END: case NEMU_ABORT: case NEMU_QUIT:
      printf("Program execution has ended. To restart the program, exit NEMU and run again.\n");
      return;
    default: nemu_state.state = NEMU_RUNNING;
  }
  // ... execute(n) ...
  switch (nemu_state.state) {
    case NEMU_RUNNING: nemu_state.state = NEMU_STOP; break;
    case NEMU_END: case NEMU_ABORT:
      Log("nemu: %s at pc = " FMT_WORD,
          (nemu_state.state == NEMU_ABORT ? ANSI_FMT("ABORT", ANSI_FG_RED) :
           (nemu_state.halt_ret == 0 ? ANSI_FMT("HIT GOOD TRAP", ANSI_FG_GREEN) :
            ANSI_FMT("HIT BAD TRAP", ANSI_FG_RED))),
          nemu_state.halt_pc);
      // fall through
    case NEMU_QUIT: statistic();
  }
}
```

`NEMU_QUIT` 的来源是 SDL 窗口关闭事件：

[src/device/device.c:48-52](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L48-L52) —— `SDL_QUIT` 事件把状态置为 `NEMU_QUIT`。

```c
while (SDL_PollEvent(&event)) {
  switch (event.type) {
    case SDL_QUIT:
      nemu_state.state = NEMU_QUIT;
      break;
```

`NEMU_ABORT` 的另一来源是差分测试失配（注意它绕过了 `set_nemu_state`，直接写字段并立即打印寄存器）：

[src/cpu/difftest/dut.c:94-100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L94-L100) —— `checkregs` 发现寄存器不一致时直接置 `NEMU_ABORT` 并 `isa_reg_display()`。

```c
static void checkregs(CPU_state *ref, vaddr_t pc) {
  if (!isa_difftest_checkregs(ref, pc)) {
    nemu_state.state = NEMU_ABORT;
    nemu_state.halt_pc = pc;
    isa_reg_display();
  }
}
```

#### 4.4.4 代码实践

1. **实践目标**：把状态机的五条转换边在源码里一一对应出来。
2. **操作步骤**：
   - 在 `src/cpu/cpu-exec.c` 中标出：入口守卫（L102–L107）、`execute` 退出条件（L80）、收尾 switch（L116–L127）。
   - 在 `src/device/device.c:51` 标出 `NEMU_QUIT` 来源；在 `src/cpu/difftest/dut.c:96` 标出 `NEMU_ABORT` 的差分来源；在 `src/engine/interpreter/hostcall.c:50` 标出 `NEMU_ABORT` 的非法指令来源；在 `include/cpu/cpu.h:26` 标出 `NEMU_END` 来源。
3. **需要观察的现象**：`NEMU_END` 只有一个来源（`NEMUTRAP` 宏）；`NEMU_ABORT` 有两个来源（`invalid_inst` 与 `checkregs`）；`NEMU_QUIT` 目前只有 SDL 关窗一个来源。
4. **预期结果**：画出一张「状态 ← 触发点」对照表，确认每种终止态的来源都已覆盖。
5. 本实践为源码阅读型，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`cpu_exec` 入口的守卫 switch 里，`default` 分支把状态置为 `NEMU_RUNNING`。若当前是 `NEMU_STOP`，会发生什么？若当前是 `NEMU_END` 呢？

**答案**：`NEMU_STOP` 不在 `case NEMU_END/ABORT/QUIT` 列表里，走 `default` 被置为 `NEMU_RUNNING`，随后正常 `execute`——这就是 `si`/`c` 能从暂停态恢复执行的原因。`NEMU_END` 命中 case，打印「程序已结束」并直接 `return`，不会重新运行——要再跑必须重启 NEMU。

**练习 2**：为什么 `execute` 循环里检查到 `state != NEMU_RUNNING` 后只是 `break`，而不是直接 `return`？

**答案**：`break` 只跳出 `for` 循环，控制权回到 `cpu_exec`，让收尾 switch 有机会打印 `HIT GOOD/BAD TRAP`/`ABORT` 并调 `statistic()`。若直接 `return`，这些收尾诊断就全被跳过了。

### 4.5 is_exit_status_bad——从状态到进程返回码

#### 4.5.1 概念说明

NEMU 本身是宿主机上的一个进程，`main` 最后要 `return` 一个整数给操作系统。PA 的自动测试脚本就是靠这个返回码判断程序对错：`0` 为通过，非 `0` 为失败。

但 NEMU 内部用的是 `NEMUState` 的五种状态，不是简单的 0/1。`is_exit_status_bad()` 就是翻译层：它读 `nemu_state`，返回 `0`（成功）或 `1`（失败）。函数名读作「退出状态是否糟糕」，返回 `true`（即 `1`）意味着糟糕（失败）。

#### 4.5.2 核心流程

判定规则只有一行：

```
good = (state == NEMU_END && halt_ret == 0)   // 正常结束且返回码为 0
    || (state == NEMU_QUIT)                    // 用户主动退出
return !good                                   // 取反：好→0，坏→1
```

含义：

- `NEMU_END` 且 `halt_ret == 0`：程序正常 trap 且 `a0==0` → 成功，返回 `0`。
- `NEMU_END` 但 `halt_ret != 0`：程序 trap 了但 `a0` 非 0（比如 AM 测试返回失败码）→ 失败，返回 `1`（即 `HIT BAD TRAP`）。
- `NEMU_QUIT`：用户关窗退出 → 视为成功，返回 `0`。
- 其他（`NEMU_STOP`、`NEMU_ABORT`）：失败，返回 `1`。

#### 4.5.3 源码精读

[src/utils/state.c:20-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/state.c#L20-L24) —— `is_exit_status_bad` 的全部实现，一行 `good` 表达式加取反。

```c
int is_exit_status_bad() {
  int good = (nemu_state.state == NEMU_END && nemu_state.halt_ret == 0) ||
    (nemu_state.state == NEMU_QUIT);
  return !good;
}
```

`main` 在引擎结束后调用它并返回：

[src/nemu-main.c:23-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/nemu-main.c#L23-L35) —— `main` 末尾 `return is_exit_status_bad()`，把内部状态映射为进程返回码。

```c
int main(int argc, char *argv[]) {
  /* Initialize the monitor. */
  init_monitor(argc, argv);   // 或 am_init_monitor()
  /* Start engine. */
  engine_start();
  return is_exit_status_bad();
}
```

这里有一个**重要的教学陷阱**：`q` 命令（`cmd_q`）只是 `return -1` 退出 SDB 主循环，并**没有**把状态置为 `NEMU_QUIT`。

[src/monitor/sdb/sdb.c:51-53](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L51-L53) —— `cmd_q` 仅返回 `-1`，不写 `nemu_state`。

```c
static int cmd_q(char *args) {
  return -1;
}
```

于是，如果你只跑了 `si 1` 就 `q`，`nemu_state.state` 仍是 `NEMU_STOP`（`cpu_exec` 收尾时由 `NEMU_RUNNING` 回落而来），`is_exit_status_bad` 返回 `1`——也就是说，**默认构建下 `q` 退出后进程返回码是 1（失败）**。这正是 PA1 让学生修复的「实现正确的退出状态」任务：在 `cmd_q` 里加一句 `nemu_state.state = NEMU_QUIT;`，让 `q` 走 `NEMU_QUIT` 的「成功」分支。理解了本讲的状态机，这个修复就是顺理成章的。

#### 4.5.4 代码实践

1. **实践目标**：验证 `is_exit_status_bad` 在不同终止态下的返回值，并复现 `q` 退出码问题。
2. **操作步骤**：
   - 运行内置镜像（`c` 跑到 `ebreak`），再输入 `q` 退出，随后在 shell 里 `echo $?` 查看返回码。此时 `state == NEMU_END && halt_ret == 0`。
   - 仅输入 `si 1` 再 `q` 退出，`echo $?`。此时 `state == NEMU_STOP`。
   - （可选修复）在 `cmd_q` 里加 `nemu_state.state = NEMU_QUIT;`，重编后重复上一步，观察返回码变化。
3. **需要观察的现象**：第一种应返回 `0`；第二种应返回 `1`；修复后第二种也返回 `0`。
4. **预期结果**：印证判定规则——`NEMU_END+halt_ret==0` 与 `NEMU_QUIT` 返回 `0`，`NEMU_STOP` 返回 `1`。实际返回码待本地验证。
5. 若你尚未实现足够指令导致内置镜像跑不到 `ebreak`，可只做源码推理部分。

#### 4.5.5 小练习与答案

**练习 1**：差分测试失败时（`checkregs` 置 `NEMU_ABORT`），`is_exit_status_bad` 返回什么？为什么 `halt_ret` 此时不重要？

**答案**：返回 `1`（失败）。因为 `good` 的两个条件分别是 `NEMU_END && halt_ret==0` 与 `NEMU_QUIT`，`NEMU_ABORT` 两条都不满足，`good=false`，取反得 `1`。`halt_ret` 只在 `NEMU_END` 分支里被检查，`NEMU_ABORT` 根本不看它（`invalid_inst` 传的 `-1` 也因此无意义）。

**练习 2**：为什么 `NEMU_QUIT`（用户关窗）被判定为「成功」退出？

**答案**：关窗是用户主动行为，不代表程序本身出错。把 `NEMU_QUIT` 归为 `good` 可避免「用户手动结束时被误报失败」，让返回码只反映「客机程序是否正确跑完」。这也正是 `cmd_q` 应当走 `NEMU_QUIT` 分支的设计动机。

### 4.6 statistic——收尾统计

#### 4.6.1 概念说明

无论以哪种方式结束（`END`/`ABORT`/`QUIT`），NEMU 都会在 `cpu_exec` 收尾时调用 `statistic()` 打印性能统计：宿主机耗时、客机指令总数、模拟频率（每秒模拟多少条指令）。这些数字在 PA 调试时很有用——比如模拟频率骤降往往意味着某条指令实现里做了昂贵的事。

统计依靠两个全局计数器：`g_nr_guest_inst`（累计执行的客机指令数，`execute` 循环里唯一累加点）与 `g_timer`（累计宿主机耗时，`cpu_exec` 用 `get_time()` 前后差值累加）。

#### 4.6.2 核心流程

```
cpu_exec:
  timer_start = get_time()
  execute(n)           // 每步 g_nr_guest_inst ++
  timer_end   = get_time()
  g_timer += timer_end - timer_start

  收尾 switch:
    NEMU_RUNNING -> NEMU_STOP
    NEMU_END / NEMU_ABORT -> 打印 HIT GOOD/BAD TRAP 或 ABORT，fall through
    NEMU_QUIT -> statistic()   // 打印 g_timer / g_nr_guest_inst / 频率
```

注意 `statistic()` 只在这三个终止分支被调用；`NEMU_RUNNING → NEMU_STOP`（如 `si N` 跑完）不调，避免每次单步都刷屏统计。

#### 4.6.3 源码精读

计数器与 `statistic` 定义在 `src/cpu/cpu-exec.c`：

[src/cpu/cpu-exec.c:28-31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L28-L31) —— 三个全局计数器：客机指令数、宿主机耗时、单步打印开关。

```c
CPU_state cpu = {};
uint64_t g_nr_guest_inst = 0;
static uint64_t g_timer = 0; // unit: us
static bool g_print_step = false;
```

[src/cpu/cpu-exec.c:85-92](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L85-L92) —— `statistic` 打印耗时、指令数与模拟频率。

```c
static void statistic() {
  IFNDEF(CONFIG_TARGET_AM, setlocale(LC_NUMERIC, ""));
#define NUMBERIC_FMT MUXDEF(CONFIG_TARGET_AM, "%", "%'") PRIu64
  Log("host time spent = " NUMBERIC_FMT " us", g_timer);
  Log("total guest instructions = " NUMBERIC_FMT, g_nr_guest_inst);
  if (g_timer > 0) Log("simulation frequency = " NUMBERIC_FMT " inst/s", g_nr_guest_inst * 1000000 / g_timer);
  else Log("Finish running in less than 1 us and can not calculate the simulation frequency");
}
```

`g_nr_guest_inst` 的唯一累加点在 `execute` 循环：

[src/cpu/cpu-exec.c:77-79](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L77-L79) —— 每执行一条指令计数加一。

```c
exec_once(&s, cpu.pc);
g_nr_guest_inst ++;
trace_and_difftest(&s, cpu.pc);
```

`g_timer` 的累加在 `cpu_exec` 主体：

[src/cpu/cpu-exec.c:109-114](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L109-L114) —— `execute` 前后取墙钟差值累加到 `g_timer`。

```c
uint64_t timer_start = get_time();
execute(n);
uint64_t timer_end = get_time();
g_timer += timer_end - timer_start;
```

`NUMBERIC_FMT` 里的 `%'` 是本地化的千分位分隔符（依赖 `setlocale(LC_NUMERIC, "")`），让大数字易读；AM 模式下退化为普通 `%`。

#### 4.6.4 代码实践

1. **实践目标**：观察 `statistic` 输出，并理解频率的计算口径。
2. **操作步骤**：
   - 运行内置镜像 `c` 到 `ebreak`，再 `q`，查看末尾三行 `host time spent` / `total guest instructions` / `simulation frequency`。
   - 把 `g_timer` 与 `g_nr_guest_inst` 代入公式 `频率 = 指令数 * 1000000 / g_timer`，手算验证。
3. **需要观察的现象**：内置镜像仅 4 条指令，耗时极短，可能出现「Finish running in less than 1 us」提示（`g_timer == 0` 时走 else 分支）。
4. **预期结果**：若 `g_timer > 0`，频率应为一个很大的数（NEMU 简单指令每秒可模拟上千万条）；若 `g_timer == 0`，打印「无法计算频率」。实际数值待本地验证。
5. 想看到非零 `g_timer`，可跑一个循环次数较多的程序（如 AM 下的 `am-kernels`）。

#### 4.6.5 小练习与答案

**练习 1**：`g_nr_guest_inst` 为什么必须在 `execute` 循环里、而不是 `exec_once` 内部累加？

**答案**：`exec_once` 是 ISA 无关骨架里被 `execute` 调用的单步函数，把计数放在 `execute` 循环（[cpu-exec.c:78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L78)）能保证「每驱动一次客机指令就加一」，与具体 ISA 无关，且在 `trace_and_difftest` 之前完成，便于差分测试与统计解耦。

**练习 2**：`statistic()` 只在 `NEMU_END/ABORT/QUIT` 分支被调用，而不在 `NEMU_RUNNING→NEMU_STOP` 分支。为什么？

**答案**：`NEMU_RUNNING→NEMU_STOP` 对应 `si N` 单步跑完、监视点命中暂停等中间态，程序并未结束。此时打印统计既无意义又会刷屏干扰调试；只在真正终止时统计才反映一次完整运行的开销。

## 5. 综合实践

把本讲五个最小模块串起来，完成下面这个「追踪一条 trap 的完整生命」的任务。

**任务**：从一个 `ebreak` 指令出发，一路追到进程返回码，并在三种终止场景下分别观察输出。

**步骤**：

1. **GOOD TRAP 路径追踪**（理论 + 实跑）：
   - 沿调用链读源码：`ebreak` INSTPAT（[src/isa/riscv32/inst.c:66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/inst.c#L66)）→ `NEMUTRAP(s->pc, R(10))` 宏（[include/cpu/cpu.h:26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h#L26)）→ `set_nemu_state(NEMU_END, ...)`（[src/engine/interpreter/hostcall.c:21-26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/hostcall.c#L21-L26)）→ `execute` 检测到 `state != NEMU_RUNNING` 而 `break`（[src/cpu/cpu-exec.c:80](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L80)）→ 收尾 switch 打印 `HIT GOOD TRAP`（[src/cpu/cpu-exec.c:119-126](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L119-L126)）→ `main` 返回 `is_exit_status_bad()`（[src/nemu-main.c:34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/nemu-main.c#L34) + [src/utils/state.c:20-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/state.c#L20-L24)）。
   - 运行内置镜像（`src/isa/riscv32/init.c` 的 `img[]`，`lbu a0,16(t0)` 读到 `sb` 刚写入的 `0`，故 `a0==0`），`c` 后应见绿色 `HIT GOOD TRAP`，`q` 退出后 `echo $?` 应为 `0`。

2. **构造 BAD TRAP**：让 `a0` 在 `ebreak` 时非 0。最简做法（仅用已匹配的 `auipc/lbu/sb`）是把 `img[1]`（`0x00028823`，即 `sb zero,16(t0)`）临时换成 `0x00000297`（再来一条 `auipc t0,0`，对 `t0` 无副作用）。这样偏移 16 处的字节保持 `0xdeadbeef` 的低字节 `0xef` 不被清零，`lbu a0,16(t0)` 读到 `0xef`，`ebreak` 时 `a0 != 0`。重编运行，预期红色 `HIT BAD TRAP`，`echo $?` 为 `1`。验证后改回。

3. **构造 invalid_inst（ABORT）**：按 4.3.4 节把 `img[0]` 改成 `0x00000000`，重编运行，预期 `invalid opcode(...)` 诊断 + 红色 `ABORT`，`echo $?` 为 `1`。验证后改回。

4. **填表**：把三种场景的 `nemu_state.state`、`halt_ret`、屏幕输出、进程返回码填入下表（「待本地验证」处实跑确认）。

| 场景 | state | halt_ret | 屏幕输出 | 返回码 |
| --- | --- | --- | --- | --- |
| 内置镜像 GOOD | `NEMU_END` | `0` | `HIT GOOD TRAP` | `0` |
| 改 img 后 BAD | `NEMU_END` | `0xef` | `HIT BAD TRAP` | `1` |
| 非法指令 | `NEMU_ABORT` | `-1`（不参与判定） | `ABORT` + 诊断 | `1` |

> 说明：以上运行结果基于源码逻辑推断，实际输出请以本地实跑为准（待本地验证）。若你尚未实现足够指令导致内置镜像无法跑到 `ebreak`，可先完成理论追踪部分，等 PA 推进到能跑通内置镜像后再实跑验证。

## 6. 本讲小结

- NEMU 用 `NEMUTRAP(thispc, code)` 与 `INV(thispc)` 两个宏把「正常结束」与「非法指令」汇聚到 `set_nemu_state`，前者切 `NEMU_END`、后者经 `invalid_inst` 切 `NEMU_ABORT`。
- `set_nemu_state` 是状态写入的统一闸口，先 `difftest_skip_ref()` 再写 `state/halt_pc/halt_ret`；唯一的例外是差分测试 `checkregs` 直接置 `NEMU_ABORT`。
- `invalid_inst` 是非法指令的第一现场：取 8 字节双格式打印、给两案例提示、置 `NEMU_ABORT`；`assert_fail_msg` 则是 `Assert`/`panic` 断言失败时的寄存器转储。
- 执行状态机有五态 `RUNNING/STOP/END/ABORT/QUIT`：`cpu_exec` 入口拦截已终止态、`execute` 每步检查 `!=RUNNING` 即 `break`、收尾 switch 打印 `HIT GOOD/BAD TRAP`/`ABORT` 并 `statistic()`。
- `is_exit_status_bad` 把状态翻译成进程返回码：`NEMU_END && halt_ret==0` 或 `NEMU_QUIT` 返回 `0`（成功），其余返回 `1`（失败）；`q` 命令未置 `NEMU_QUIT` 是 PA1 待修的退出状态问题。
- `statistic` 依靠 `g_nr_guest_inst`（`execute` 里唯一累加点）与 `g_timer`（`cpu_exec` 前后墙钟差）打印耗时、指令数与模拟频率，只在终止分支调用。

## 7. 下一步学习建议

本讲把「程序如何结束」讲透了，接下来建议：

- **差分测试的内部细节**：本讲多次提到 `difftest_skip_ref` 与 `checkregs`，其完整的 DUT/REF 动态库协作机制在 u8-l24（差分测试机制）详述，可继续阅读 `src/cpu/difftest/dut.c` 与 `ref.c`。
- **指令追踪与反汇编**：`HIT BAD TRAP` 之后如何定位出错指令？答案在 u8-l25（指令追踪与反汇编），看 `exec_once` 里 `logbuf` 如何组装、`disasm.c` 如何用 capstone 反汇编、`log.c` 的 trace 窗口如何控制日志量。
- **真正的中断/异常**：本讲的「trap」是 NEMU 自定义的结束信号，与 CPU 的中断/异常是两回事。若想了解 `mepc`/`mcause`/`mtvec` 那套机器模式中断，回到 u7-l21（中断与异常机制）。
- **动手方向**：尝试在 `cmd_q` 里补上 `nemu_state.state = NEMU_QUIT;` 修复退出状态，并思考为何 `NEMU_QUIT` 被归为「成功」——这是把本讲状态机知识立刻变现的最小练习。
