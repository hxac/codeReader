# CPU 执行主循环

## 1. 本讲目标

前面几讲我们走通了 NEMU 的启动链路（u1-l3），也实现了一个能驱动 CPU 的简单调试器 SDB（u2-l5）：在 SDB 里敲 `c` 或 `si N`，最终都会调用 `cpu_exec(n)`。但 `cpu_exec` 内部到底发生了什么？一条客机指令是怎么被「取出来、解释、再让 PC 前进」的？运行结束又是怎么判定成功还是失败的？

本讲我们就钻进 `src/cpu/cpu-exec.c`，把 NEMU 的 **CPU 执行主循环** 拆开看。学完本讲，你应当能够：

- 描述 `cpu_exec` 驱动的五种执行状态（`NEMU_RUNNING / STOP / END / ABORT / QUIT`）以及它们之间的转换条件。
- 画出 `cpu_exec → execute → exec_once` 这条三层调用链，并解释每一层的职责。
- 说清 `exec_once` 单步执行一条指令时，`pc / snpc / dnpc` 这三个 PC 是如何流转的，以及 `g_nr_guest_inst` 是在哪里累加的。
- 指出 **trace（指令追踪）** 与 **difftest（差分测试）** 在主循环中的精确插入点。
- 自己动手在主循环里加一个最简的「指令追踪打印（itrace）」，并能解释 `MAX_INST_TO_PRINT` 的作用。

本讲是 u3 单元（CPU 执行引擎与译码）的第一讲，只关注 **驱动与调度** 这一维度；至于「一条指令内部如何取指译码」「INSTPAT 模式匹配如何工作」，留给 u3-l10、u3-l11。

## 2. 前置知识

本讲假设你已经掌握以下概念（前置讲义已建立）：

- **NEMU 的整体结构**（u1-l1）：CPU 是一个子系统，源码在 `src/cpu/`。
- **启动链路**（u1-l3）：`main → init_monitor → engine_start`；在 native 模式下 `engine_start` 调 `sdb_mainloop`，在 AM 模式下直接 `cpu_exec(-1)` 跑到底。
- **SDB 命令框架**（u2-l5）：`c`（继续）与 `si N`（单步 N 条）最终都调用 `cpu_exec(n)`，其中 `c` 传入 `-1`。
- **宏体系**（u1-l2、u1-l4）：`IFDEF(CONFIG_xxx, ...)`、`MUXDEF(...)` 这类条件编译宏会在预处理期展开或消失；`FMT_WORD` 是当前 ISA 下格式化一个机器字（`word_t`）的 printf 格式串。

再补充三个本讲会反复用到、但前置讲义未展开的点：

- **状态机视角**：我们可以把 CPU 看成一个状态机——「取一条指令、更新寄存器/内存/PC」就是一次状态转移。`cpu_exec(n)` 就是「让这个状态机转移 n 步」。
- **三个 PC**（本讲会精读）：`pc` = 当前指令地址；`snpc`（static next pc）= 顺序意义下的下一条指令地址（按指令长度顺延）；`dnpc`（dynamic next pc）= 实际跳转后的下一条地址（遇到跳转/分支时与 `snpc` 不同）。
- **host 与 guest**：NEMU 是 host（宿主机，你正在用的电脑）上的一个程序，它模拟出来的机器叫 guest（客机）。`g_timer`、`g_nr_guest_inst` 这些带 `g_` 前缀的全局量是 host 侧的统计量。

## 3. 本讲源码地图

本讲主要围绕下面几个文件：

| 文件 | 作用 |
| --- | --- |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | **本讲主角**。包含 `cpu_exec`、`execute`、`exec_once`、`trace_and_difftest`、`statistic` 五个函数，是 CPU 执行主循环的全部实现。 |
| [include/cpu/cpu.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/cpu.h) | 暴露 `cpu_exec` 接口，并定义 `NEMUTRAP` / `INV` 两个用于结束/报错的宏。 |
| [include/utils.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h) | 定义五种状态的枚举、`NEMUState` 结构体与 `nemu_state` 全局变量。 |
| [src/utils/state.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/state.c) | `nemu_state` 的定义处（初值 `NEMU_STOP`）与 `is_exit_status_bad` 判定函数。 |
| [include/cpu/decode.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h) | `Decode` 结构体定义，`exec_once` 的核心参数。 |
| [src/device/device.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c) | `device_update()` 实现，主循环每步都会调用它来推进设备。 |
| [src/engine/interpreter/init.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/init.c) | `engine_start()`，是 `cpu_exec` 的上层调用者之一。 |

先在脑海里建立这张「调用链」全景图：

```
SDB 的 c / si 命令        AM 模式 engine_start()
        \                    /
         \                  /
          ──►  cpu_exec(n)          ← 第 1 层：状态机驱动 / 状态分发
                    │
                    ▼
               execute(n)           ← 第 2 层：循环 n 次
                    │  (循环体)
                    ▼
              exec_once(&s, cpu.pc)  ← 第 3 层：单步执行一条指令
                    │
                    ▼
        g_nr_guest_inst ++ ; trace_and_difftest ; device_update
```

下面按这五块逐个拆解。

## 4. 核心概念与源码讲解

### 4.1 cpu_exec：状态机驱动与状态分发

#### 4.1.1 概念说明

`cpu_exec(uint64_t n)` 是 CPU 执行引擎对外的唯一入口。它的语义很简单：**让客机 CPU 再执行 n 条指令**。但它不止「执行」，还承担两件额外的事：

1. **状态守卫**：如果程序已经结束（`NEMU_END`）、异常中止（`NEMU_ABORT`）或用户退出（`NEMU_QUIT`），就拒绝再执行，直接返回——因为「一个已经停下来的程序不能再跑」。
2. **收尾归类**：执行完 n 步后，根据当前 `nemu_state.state` 判断这次停下来的「性质」，并打印对应的报告（`HIT GOOD TRAP` / `HIT BAD TRAP` / 中止）。

`n` 的类型是 `uint64_t`（无符号 64 位）。这点很关键：SDB 的 `c` 命令传入 `-1`，而 `-1` 转成 `uint64_t` 是 `0xFFFFFFFFFFFFFFFF`（一个极大的数），所以「执行 -1 条」等价于「一直执行，直到状态变化才停」。这就是 `c`（继续运行）的实现技巧。

五种执行状态定义在 [include/utils.h:L23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L23)：

| 状态 | 含义 | 谁会设置它 |
| --- | --- | --- |
| `NEMU_RUNNING` | 正在执行中（处于 `execute` 循环里） | `cpu_exec` 进入时设置 |
| `NEMU_STOP` | 暂停，等待下一条 SDB 命令 | `cpu_exec` 退出时由 `RUNNING` 降级；或监视点触发 |
| `NEMU_END` | 程序正常结束（执行到 `nemu_trap`/`ebreak`） | `NEMUTRAP` 宏 |
| `NEMU_ABORT` | 异常中止（如遇到非法指令） | `invalid_inst` |
| `NEMU_QUIT` | 用户主动退出（如关闭 SDL 窗口） | `device_update` 里的事件处理 |

承载这些状态的全局变量 `nemu_state` 是一个结构体（[include/utils.h:L25-L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L25-L31)），除了 `state` 还记录了 `halt_pc`（停在哪条指令）和 `halt_ret`（trap 返回值）。

#### 4.1.2 核心流程

`cpu_exec` 的执行流程（伪代码）：

```
函数 cpu_exec(n):
    g_print_step = (n < MAX_INST_TO_PRINT)     # 单步量小，才逐条打印到屏幕
    若 state ∈ {END, ABORT, QUIT}:
        打印 "Program execution has ended..."
        return                                  # 守卫：已停下的程序不再跑
    否则:
        state = RUNNING                         # 进入运行态

    timer_start = get_time()
    execute(n)                                  # ← 真正执行 n 条（见 4.2）
    g_timer += get_time() - timer_start         # 累计 host 耗时

    根据 state 收尾:
        RUNNING → 降级为 STOP                   # 正常跑完 n 步 / si 完成
        END / ABORT → 打印 GOOD/BAD TRAP，再调 statistic()
        QUIT → 调 statistic()
```

状态转换图：

```
                  cpu_exec 进入
        ┌─────────────────────────────┐
        │                             │
        ▼                             │
   ┌─────────┐  execute() 跑完 n 步   ┌─────────┐
   │ RUNNING │ ─────────────────────► │  STOP   │ ◄── 初始状态（state.c）
   └────┬────┘                        └─────────┘
        │ 遇到 nemu_trap                   ▲
        ├──────────────────────────────► │ END   │ ──► HIT GOOD/BAD TRAP
        │ 遇到非法指令                     │
        ├──────────────────────────────► │ ABORT │ ──► ABORT（红）
        │ 关闭 SDL 窗口                    │
        └──────────────────────────────► │ QUIT  │ ──► statistic
```

注意一个细节：`NEMU_STOP` 不在任何收尾 `case` 里，所以当 `execute` 因监视点或 `si` 跑完而把状态留在 `STOP` 时，`cpu_exec` 既不打印也不调 `statistic`，只是安静地返回 SDB——这正是「暂停」该有的样子。

#### 4.1.3 源码精读

入口函数完整实现（[src/cpu/cpu-exec.c:L99-L128](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L99-L128)）：

```c
/* Simulate how the CPU works. */
void cpu_exec(uint64_t n) {
  g_print_step = (n < MAX_INST_TO_PRINT);
  switch (nemu_state.state) {
    case NEMU_END: case NEMU_ABORT: case NEMU_QUIT:
      printf("Program execution has ended. To restart the program, exit NEMU and run again.\n");
      return;
    default: nemu_state.state = NEMU_RUNNING;
  }

  uint64_t timer_start = get_time();
  execute(n);
  uint64_t timer_end = get_time();
  g_timer += timer_end - timer_start;

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

逐段说明：

- 第 101 行 `g_print_step = (n < MAX_INST_TO_PRINT);`：根据本次要执行的步数决定是否「单步打印」。这个标志在 4.4 的 `trace_and_difftest` 里会被读取。`MAX_INST_TO_PRINT` 定义在第 26 行，值为 10。
- 第 102-107 行 **进入守卫**：只有 `END/ABORT/QUIT` 三个「终止态」会被拦下并 `return`；其余状态（`RUNNING/STOP`）都走 `default`，把状态置为 `RUNNING`。这意味着 `si`、`c`、甚至初次启动（状态为初始 `STOP`）都会从这里进入运行态。
- 第 109-114 行 **计时**：用 `get_time()`（host 时间，单位微秒）包住 `execute`，差值累加到 `g_timer`，供 `statistic` 计算模拟频率。
- 第 116-127 行 **收尾**：
  - `RUNNING` → 降级 `STOP`（`si N` 跑完、或没有任何终止条件命中就到了 n 步）。
  - `END/ABORT` → 用 `Log` 打印带颜色的结果。三元嵌套判断：`ABORT` 直接红色 `ABORT`；`END` 时再看 `halt_ret`，为 0 是绿色 `HIT GOOD TRAP`，非 0 是红色 `HIT BAD TRAP`。注意末尾注释 `// fall through`——`END/ABORT` 处理完后故意「漏」到 `QUIT` 的 `statistic()`，这样三种终止态都会打印统计。
  - `STOP` 不在 case 里 → 什么都不做，直接返回。

`halt_ret == 0` 的判定配合 [src/utils/state.c:L20-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/state.c#L20-L24) 的 `is_exit_status_bad` 一起理解：只有 `END && halt_ret==0` 或 `QUIT` 才算「成功」，进程返回 0（`EXIT_SUCCESS`）。

#### 4.1.4 代码实践

这是一个 **源码阅读 + 观察型实践**，目标是亲眼看到状态分发：

1. **实践目标**：理解进入守卫与收尾分支分别何时触发。
2. **操作步骤**：
   - 阅读 [src/cpu/cpu-exec.c:L99-L128](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L99-L128)，对照上面的状态转换图，逐个 `case` 标注「谁会把我设置成这个状态」。
   - 编译并运行内置镜像（具体命令见你本地的 `make run` 或 `./build/riscv32-nemu`，**待本地验证**），在 SDB 里连续执行两次 `c`：第一次应跑到 `HIT GOOD TRAP`，第二次应看到 `Program execution has ended. To restart the program, exit NEMU and run again.`。
3. **需要观察的现象**：第一次 `c` 正常结束并打印统计；第二次 `c` 因为状态已是 `NEMU_END` 被守卫拦下。
4. **预期结果**：第二次 `c` 不再执行任何指令，直接返回，`g_nr_guest_inst` 不再增长。
5. 运行结果：**待本地验证**（取决于你本机是否已实现足够指令让内置镜像跑到 trap）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SDB 的 `c` 命令调用 `cpu_exec(-1)`，而不是某个很大的具体数字？传 `-1` 安全吗？

> **答案**：因为 `n` 是 `uint64_t`，`-1` 会被解释成 `2^64-1`（极大值），`execute` 的循环会一直跑下去，直到 `nemu_state.state` 变化（trap、中止、退出或监视点）才 `break`。这比写死一个大数字更优雅且不会溢出，所以安全。调用点见 [src/monitor/sdb/sdb.c:L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/sdb/sdb.c#L46)。

**练习 2**：如果一个程序已经 `HIT GOOD TRAP`（状态 `NEMU_END`），用户又敲了 `si 5`，会发生什么？为什么这样设计？

> **答案**：`cpu_exec` 的进入守卫会命中 `NEMU_END`，打印「Program execution has ended...」并立即 `return`，不会执行任何指令。这样设计是为了避免在一个已经终止的状态机上继续转移状态——程序理应退出后重新启动，而不是从 trap 之后继续跑。

---

### 4.2 execute：核心执行循环

#### 4.2.1 概念说明

`execute(uint64_t n)` 是真正「跑指令」的循环。它把「执行 n 条」拆成一个重复 n 次的循环体，每次循环体做四件事：

1. **单步执行一条**：调用 `exec_once(&s, cpu.pc)`。
2. **计数**：`g_nr_guest_inst ++`，累计客机执行了多少条指令。
3. **追踪与差分测试**：调用 `trace_and_difftest`，把这一步的信息送给 itrace/difftest（详见 4.4）。
4. **检查是否该停**：若 `nemu_state.state != NEMU_RUNNING` 就 `break`（trap、监视点、退出等都会改状态）。
5. **推进设备**：若开启了设备（`CONFIG_DEVICE`），调用 `device_update()`。

这四到五步的 **顺序** 不能随意调换：必须先执行、再计数、再追踪、再判断状态、最后才推进设备——因为设备里可能产生中断/退出事件，只应在一条指令「完整提交」之后再处理。

#### 4.2.2 核心流程

```
函数 execute(n):
    定义本函数局部的解码信息结构 s
    循环 n 次（每次 n --）:
        exec_once(&s, cpu.pc)          # 取指-译码-执行这一条
        g_nr_guest_inst ++              # 客机指令计数 +1
        trace_and_difftest(&s, cpu.pc)  # itrace 写日志 + difftest 比对
        若 state != RUNNING: break      # 遇到任何「停下来」的条件就跳出
        device_update()                 # （开启设备时）推进设备/中断
```

#### 4.2.3 源码精读

完整实现（[src/cpu/cpu-exec.c:L74-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83)）：

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

要点：

- 第 75 行 `Decode s;` 是一个 **栈上局部变量**，每条指令复用同一个 `s` 来承载解码中间信息（`pc/snpc/dnpc/isa` 等）。它的结构见 [include/cpu/decode.h:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L21-L27)。
- 第 76 行 `for (; n > 0; n --)`：当 `n = -1`（即 `2^64-1`）时，这个循环本会跑天文数字次，但实际上第 80 行的 `break` 会在程序终止时提前结束它。
- 第 78 行 `g_nr_guest_inst ++`：**客机指令计数的唯一累加点**。整个 NEMU 用它来统计执行了多少条指令，也作为 trace 窗口（u8-l25）的时间轴。
- 第 80 行是「停下来」的总开关：任何让状态离开 `RUNNING` 的地方（`NEMUTRAP` 设 `END`、`invalid_inst` 设 `ABORT`、监视点设 `STOP`、SDL 关窗设 `QUIT`）都会让循环在这里 `break`。
- 第 81 行 `IFDEF(CONFIG_DEVICE, device_update())`：`device_update` 是 **节流** 的——它内部会比较距上次调用的时间差，不足 `1000000 / TIMER_HZ` 微秒就直接 return（见 [src/device/device.c:L36-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/device.c#L36-L42)）。所以「每步都调」并不会真的每步都重绘屏幕。

#### 4.2.4 代码实践

**源码阅读型实践**：跟踪一次 `si 3` 的执行轨迹。

1. **实践目标**：验证循环体的执行顺序。
2. **操作步骤**：
   - 在 [src/cpu/cpu-exec.c:L74-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83) 的循环体里，依次标出「单步 / 计数 / 追踪 / 状态检查 / 设备」五步。
   - 思考：如果 `exec_once` 内部触发了 `NEMUTRAP`（把状态设成 `END`），第 78 行的 `g_nr_guest_inst ++` 还会执行吗？`trace_and_difftest` 呢？
3. **需要观察的现象**（阅读推理，不必运行）：`g_nr_guest_inst` 会 +1（trap 那条也算一条），`trace_and_difftest` 也会执行（trap 指令也会被记录），随后第 80 行检测到 `state != RUNNING` 而 `break`。
4. **预期结果**：trap 指令本身被计入统计并被追踪，但 `device_update` 不会再被调用（因为在它之前就 `break` 了）。
5. 运行结果：**待本地验证**（可通过在 `statistic` 打印的总指令数与你的 itrace 记录条数对比来印证）。

#### 4.2.5 小练习与答案

**练习 1**：`execute` 里的 `Decode s;` 为什么定义在函数内、循环外，而不是每条指令 `malloc` 一个？

> **答案**：性能与简洁。`s` 只用来暂存「当前这条指令」的解码中间结果，下一条会整体覆盖，没必要动态分配。把它放在栈上、循环外复用，零分配开销，是模拟器主循环里的常见写法。

**练习 2**：为什么 `device_update()` 放在状态检查 `break` 之后，而不是之前？

> **答案**：因为一条指令的执行结果必须先「完整提交」（包括确认它没有让程序终止），之后才适合让设备（屏幕、键盘、时钟）推进。如果某条指令触发了 trap，我们不应该再为它处理设备事件，所以先 `break` 跳过 `device_update`。

---

### 4.3 exec_once：单步执行的最小单元

#### 4.3.1 概念说明

`exec_once(Decode *s, vaddr_t pc)` 是「执行一条指令」的最小单元，也是 ISA 相关与 ISA 无关代码的 **交界处**：它本身是 ISA 无关的骨架，但通过调用 `isa_exec_once(s)` 把真正的「取指-译码-执行」交给当前 ISA 的实现（如 `src/isa/riscv32/inst.c`）。

它做三件事：

1. 把当前 `pc` 记入 `s->pc`，并把 `s->snpc` 初始化为 `pc`（顺序下一条的「起点」，等 ISA 代码按指令长度推进它）。
2. 调 `isa_exec_once(s)`——这一步会取指、译码、执行，并设置 `s->dnpc`（真正的下一条 PC）。
3. 把 `s->dnpc` 提交回架构状态 `cpu.pc`——这一步是「指令生效」的瞬间。

之后（在 `CONFIG_ITRACE` 开启时）还会把这条指令格式化成 `s->logbuf`（`pc: 字节码 反汇编`），供追踪使用。这一段是 NEMU 自带的「官方 itrace」，本讲的综合实践会让你写一个简化版，届时可以和它对照。

#### 4.3.2 核心流程

```
函数 exec_once(s, pc):
    s->pc   = pc              # 当前指令地址
    s->snpc = pc              # 静态下一 PC 的初值（ISA 代码会 += ilen）
    isa_exec_once(s)          # ← ISA 相关：取指、译码、执行，写出 s->dnpc、s->isa.inst
    cpu.pc = s->dnpc          # 提交：架构 PC 更新为动态下一 PC

    # （开启 ITRACE 时）组装 logbuf：
    ilen = s->snpc - s->pc        # 指令长度
    往 logbuf 写 "pc:"，再写指令字节，再写反汇编
```

三个 PC 的关系（核心要点）：

- `pc`：本条指令的地址。
- `snpc`：进入 `isa_exec_once` 前等于 `pc`；ISA 的取指代码每取若干字节就 `s->snpc += n`，所以执行完后 `snpc - pc` 恰好是指令长度 `ilen`。它代表「如果顺序执行，下一条在哪」。
- `dnpc`：动态下一 PC。对于非跳转指令，ISA 代码令 `dnpc = snpc`；对于跳转/分支指令，`dnpc` 是跳转目标。最后 `cpu.pc = dnpc` 让真正的 PC 跟随它。

#### 4.3.3 源码精读

骨架部分（[src/cpu/cpu-exec.c:L43-L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L43-L47)）只有四行：

```c
static void exec_once(Decode *s, vaddr_t pc) {
  s->pc = pc;
  s->snpc = pc;
  isa_exec_once(s);
  cpu.pc = s->dnpc;
```

紧接着是 `CONFIG_ITRACE` 保护下的 `logbuf` 组装（[src/cpu/cpu-exec.c:L48-L71](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L48-L71)），它的逻辑值得细看，因为综合实践要仿写它：

```c
#ifdef CONFIG_ITRACE
  char *p = s->logbuf;
  p += snprintf(p, sizeof(s->logbuf), FMT_WORD ":", s->pc);
  int ilen = s->snpc - s->pc;
  int i;
  uint8_t *inst = (uint8_t *)&s->isa.inst;
#ifdef CONFIG_ISA_x86
  for (i = 0; i < ilen; i ++) {
#else
  for (i = ilen - 1; i >= 0; i --) {
#endif
    p += snprintf(p, 4, " %02x", inst[i]);
  }
  ...
  void disassemble(char *str, int size, uint64_t pc, uint8_t *code, int nbyte);
  disassemble(p, s->logbuf + sizeof(s->logbuf) - p,
      MUXDEF(CONFIG_ISA_x86, s->snpc, s->pc), (uint8_t *)&s->isa.inst, ilen);
#endif
```

要点：

- `int ilen = s->snpc - s->pc;`：用「静态下一 PC 减当前 PC」算出指令长度。这就是 `snpc` 的核心用途——它既是「顺序下一 PC」，也顺便记录了「这条指令有多长」。
- `uint8_t *inst = (uint8_t *)&s->isa.inst;`：取指令机器码的字节指针。对 riscv32，`s->isa.inst` 就是一个 `uint32_t`（见 [src/isa/riscv32/include/isa-def.h:L27-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L27-L29)）。
- 字节序差异：x86 从低字节往高字节打印（`i = 0 → ilen`），RISC-V 反过来（`i = ilen-1 → 0`）。这是因为显示时希望和「指令编码阅读顺序」对齐——RISC-V 是定长小端编码，逆序打印后看起来像大端，便于和手册里的编码位域对照。
- 最后 `disassemble(...)` 调用 capstone（通过 dlopen 动态加载，详见 u8-l25）生成汇编文本，追加到 `logbuf` 末尾。

#### 4.3.4 代码实践

**源码阅读型实践**：跟踪三个 PC 的数据流。

1. **实践目标**：彻底分清 `pc / snpc / dnpc / cpu.pc` 四者。
2. **操作步骤**：
   - 在 [src/cpu/cpu-exec.c:L43-L47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L43-L47) 标出 `s->pc`、`s->snpc`、`s->dnpc`、`cpu.pc` 四个写入点。
   - 想象一条「顺序指令」（如 `addi`）和一条「跳转指令」（如 `jal`）分别执行后，这四个值的关系。顺序指令：`dnpc = snpc = pc + 4`；跳转指令：`dnpc = 跳转目标`，但 `snpc` 仍等于 `pc + 4`。
3. **需要观察的现象**（阅读推理）：`ilen = snpc - pc` 对两种指令都等于 4（riscv32 定长），但 `cpu.pc` 在跳转指令后等于 `dnpc` 而非 `snpc`。
4. **预期结果**：你能用一句话说出「`snpc` 决定指令长度，`dnpc` 决定真正去向」。
5. 运行结果：**待本地验证**（待 u5-l16 实现 `jal` 后可在 itrace 里看到跳转前后的 PC 不连续）。

#### 4.3.5 小练习与答案

**练习 1**：`exec_once` 为什么把 `s->snpc = pc;` 而不是 `s->snpc = pc + 4;` 作为初值？

> **答案**：因为 NEMU 要支持变长 ISA（x86），指令长度不能写死。把 `snpc` 初值设为 `pc`，让 ISA 的取指代码在每取一段字节后自己 `snpc += n`，最后 `snpc - pc` 自然就是这条指令的实际长度。这是「ISA 无关骨架 + ISA 相关实现」分层的好处。

**练习 2**：`cpu.pc = s->dnpc;` 这一行如果删掉会怎样？

> **答案**：架构 PC 永远不会前进，下一条指令仍从原 `pc` 取，程序会无限循环执行同一条指令。这行是「指令提交」的关键——只有把解码出的动态下一 PC 写回 `cpu.pc`，一条指令才算真正生效。

---

### 4.4 trace_and_difftest：追踪与差分测试插入点

#### 4.4.1 概念说明

`trace_and_difftest(Decode *_this, vaddr_t dnpc)` 是一个「三合一」的钩子函数，在每条指令执行完之后（`execute` 循环里）被调用一次，集中处理三件 **正交** 的事：

1. **itrace 写日志**：当 `ITRACE_COND` 为真时，把这条指令的 `logbuf` 写进日志文件 `nemu-log.txt`。
2. **单步打印**：当 `g_print_step` 为真（即本次 `cpu_exec` 的步数 `< MAX_INST_TO_PRINT`）时，把 `logbuf` 打印到屏幕。
3. **差分测试**：当开启 `CONFIG_DIFFTEST` 时，调用 `difftest_step(pc, dnpc)`，把 NEMU（DUT）这一步后的寄存器与参考实现（REF，如 spike/qemu）比对。

这三件事互不依赖，靠各自的 `CONFIG_*` 与运行期标志独立开关，是 NEMU 「在一处插入点同时服务调试、追踪、验证」的典型设计。

#### 4.4.2 核心流程

```
函数 trace_and_difftest(_this, dnpc):
    若 ITRACE_COND 成立:                 # 默认 "true"（Kconfig）
        log_write("%s\n", _this->logbuf)  # 写入日志文件（受 trace 窗口节流）
    若 g_print_step 成立:                 # 本次 cpu_exec 的 n < 10
        puts(_this->logbuf)               # 打印到屏幕
    若 CONFIG_DIFFTEST 开启:
        difftest_step(_this->pc, dnpc)    # 与 REF 比对寄存器
```

#### 4.4.3 源码精读

实现非常短（[src/cpu/cpu-exec.c:L35-L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L35-L41)）：

```c
static void trace_and_difftest(Decode *_this, vaddr_t dnpc) {
#ifdef CONFIG_ITRACE_COND
  if (ITRACE_COND) { log_write("%s\n", _this->logbuf); }
#endif
  if (g_print_step) { IFDEF(CONFIG_ITRACE, puts(_this->logbuf)); }
  IFDEF(CONFIG_DIFFTEST, difftest_step(_this->pc, dnpc));
}
```

三个插入点逐个看：

- 第 36-38 行 **itrace 写日志**：`ITRACE_COND` 是一个来自 Kconfig 的字符串宏，默认值是 `"true"`（[Kconfig:L149-L152](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L149-L152)），展开后即 `if (true)`，所以默认每条指令都会触发。`log_write` 内部还会受 trace 窗口 `log_enable()` 节流（只写 `[TRACE_START, TRACE_END]` 区间内的指令，详见 u8-l25），避免日志爆炸。
- 第 39 行 **单步打印**：`g_print_step` 由 `cpu_exec` 设置为 `(n < MAX_INST_TO_PRINT)`。所以 `si 5` 会在屏幕上逐条打印 5 条指令的 `logbuf`，而 `c`（n 极大）不会打印——这是为了避免 `continue` 时刷屏。
- 第 40 行 **差分测试**：`difftest_step` 会把 NEMU 的寄存器与 REF 比对，不一致就报错。这是 NEMU 找指令实现 bug 的利器，详见 u8-l24。

注意第 36 行的 `#ifdef CONFIG_ITRACE_COND`：它检查的是「是否配置了这个条件字符串」，而第 37 行的 `if (ITRACE_COND)` 检查的是「运行期这个字符串表达式是否为真」。一个是编译期开关，一个是运行期判断，两者配合。

#### 4.4.4 代码实践

**观察型实践**：对比 `si` 与 `c` 的屏幕输出差异，体会 `g_print_step` 的作用。

1. **实践目标**：亲眼看到 `MAX_INST_TO_PRINT` 如何控制屏幕打印。
2. **操作步骤**：
   - 确认 `CONFIG_ITRACE` 已开启（默认开，见 [Kconfig:L144-L147](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L144-L147)）。
   - 在 SDB 里执行 `si 5`，观察屏幕是否逐条打印 5 行 `pc: 字节 反汇编`。
   - 再执行 `c`，观察屏幕是否 **不再** 逐条打印（而是直接跑到 trap）。
3. **需要观察的现象**：`si 5` 有逐条打印，`c` 没有。
4. **预期结果**：因为 `5 < 10` 成立 → `g_print_step = true`；而 `c` 的 n 是 `2^64-1`，`n < 10` 为假 → `g_print_step = false`。
5. 运行结果：**待本地验证**（需要已实现 `si` 命令与若干基础指令，见 u2-l5、u5-l16）。

#### 4.4.5 小练习与答案

**练习 1**：为什么把 itrace 写日志、单步打印、difftest 三件事放在同一个函数、同一个调用点？

> **答案**：因为它们都需要「每条指令执行完之后」这个时机，且彼此正交（互不依赖）。集中到一个函数既避免了在 `execute` 循环里堆砌三段条件编译代码，也让「在主循环的哪个位置插入追踪/验证」这件事一目了然——这正是题目所说「掌握 trace 与 difftest 的插入点」。

**练习 2**：`log_write` 写日志已经受 `log_enable()` 的 trace 窗口节流，那为什么还要再用 `g_print_step` 单独控制屏幕打印？

> **答案**：两者目标不同。trace 窗口（`TRACE_START/END`）控制的是 **写进日志文件** 的指令区间，用于事后排查；而 `g_print_step` 控制的是 **实时打印到屏幕**，主要用于 `si` 单步时即时观察。`c` 跑几千万条指令时，即使不写日志，也不能逐条往屏幕打印，否则会严重拖慢并刷屏，所以需要单独的 `g_print_step` 闸门。

---

### 4.5 statistic：运行统计与退出报告

#### 4.5.1 概念说明

`statistic()` 负责在程序结束时打印一份「性能报告」，包含三项：

1. **host time spent**：NEMU 作为 host 进程，跑这批指令花了多少微秒（`g_timer`）。
2. **total guest instructions**：客机执行了多少条指令（`g_nr_guest_inst`）。
3. **simulation frequency**：模拟频率 = 客机指令数 / host 耗时（条/秒），衡量 NEMU 的执行速度。

它会在三种终止态下被调用：`NEMU_END`、`NEMU_ABORT`（通过 `cpu_exec` 收尾的 fall through）、`NEMU_QUIT`；也会在断言失败时由 `assert_fail_msg` 调用，帮你看到「出错时已经跑了多少条指令」。

#### 4.5.2 核心流程

```
函数 statistic():
    setlocale(LC_NUMERIC, "")        # （非 AM 模式）启用千位分隔符显示
    Log("host time spent = %d us", g_timer)
    Log("total guest instructions = %d", g_nr_guest_inst)
    若 g_timer > 0:
        Log("simulation frequency = %d inst/s", g_nr_guest_inst * 1000000 / g_timer)
    否则:
        Log("Finish running in less than 1 us ...")
```

模拟频率公式（用独立公式表达）：

\[
\text{frequency} = \frac{\text{g\_nr\_guest\_inst}}{\text{g\_timer} / 10^6}
= \frac{\text{g\_nr\_guest\_inst} \times 10^6}{\text{g\_timer}}
\]

分母 `g_timer` 单位是微秒（us），乘 `10^6` 换算成「条/秒」。代码里写成 `g_nr_guest_inst * 1000000 / g_timer` 正是这个意思。

#### 4.5.3 源码精读

实现（[src/cpu/cpu-exec.c:L85-L92](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L85-L92)）：

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

要点：

- 第 86 行 `IFNDEF(CONFIG_TARGET_AM, setlocale(LC_NUMERIC, ""));`：在 native 模式下设置本地化，让 `%'` 格式串给数字加千位分隔符（如 `1,234,567`）。AM 模式不带这功能，所以用 `IFNDEF` 排除。
- 第 87 行 `NUMBERIC_FMT`：一个条件编译的格式串——native 模式是 `%'PRIu64`（带分隔符），AM 模式是 `%PRIu64`（不带）。这是 u8-l26 宏体系的典型用法。
- 第 88-90 行：分别打印 host 耗时、客机指令总数、模拟频率。`g_timer` 与 `g_nr_guest_inst` 都是 [src/cpu/cpu-exec.c:L28-L30](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L28-L30) 定义的全局量。
- 第 91 行的 `else` 分支处理「程序极快结束（不足 1us）」的边界，避免除零。

`statistic` 还会被 `assert_fail_msg` 调用（[src/cpu/cpu-exec.c:L94-L97](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L94-L97)），后者先打印所有寄存器、再打印统计——这正是你在 PA 里遇到 `ABORT` 时看到的那一大段输出。

#### 4.5.4 代码实践

**观察型实践**：读取一份真实的统计输出并反推性能。

1. **实践目标**：理解三项统计的含义与相互关系。
2. **操作步骤**：
   - 跑通内置镜像到 `HIT GOOD TRAP`（**待本地验证**），复制末尾 `statistic` 打印的三行。
   - 用计算器验证 `simulation frequency ≈ total guest instructions × 1000000 ÷ host time spent`。
3. **需要观察的现象**：三者数值满足上面的等式；模拟频率通常在百万条/秒量级（NEMU 是教学用解释器，不追求性能）。
4. **预期结果**：手算与程序输出一致（允许整数除法带来的小误差）。
5. 运行结果：**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`g_timer` 是在哪里累加的？为什么不在 `execute` 循环里每步累加？

> **答案**：在 [cpu_exec:L113-L114](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L113-L114) 用 `get_time()` 前后差值累加。不每步累加是因为 `get_time()` 本身是系统调用，有开销；放在 `execute` 外面包一次，既能测得整批指令的 host 耗时，又不污染主循环的性能。

**练习 2**：为什么 `g_timer == 0` 时要单独处理？

> **答案**：因为模拟频率公式 `g_nr_guest_inst * 1000000 / g_timer` 分母为 0 会触发整数除零（未定义行为/异常）。当程序极快结束（不足 1 微秒，`g_timer` 取整为 0）时，改打印提示信息，避免除零。

---

## 5. 综合实践

**实践目标**：在 CPU 执行主循环里自己实现一个最简的「指令追踪打印（itrace）」，亲眼观察内置镜像的前几条指令，并解释 `MAX_INST_TO_PRINT` 的作用。这是把本讲五个模块串起来的综合任务——你会同时用到 `execute`（4.2）、`exec_once`（4.3）的 `Decode` 结构、`g_print_step` 与 `MAX_INST_TO_PRINT`（4.1、4.4）。

> 说明：NEMU 在 `exec_once` 里已经有一套「官方 itrace」（组装 `logbuf`），但它要等 u8-l25 才细讲。本实践让你 **先自己写一个最简版**，建立直觉，再回头对照官方实现。

### 操作步骤

1. **确认默认 ISA 与配置**。本实践以默认的 riscv32 为例。`s.isa.inst` 对 riscv32 是一个 `uint32_t`（[src/isa/riscv32/include/isa-def.h:L27-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L27-L29)）。`FMT_WORD` 在 32 位下展开为 `"0x%08" PRIx32`（[include/common.h:L40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L40)）。

2. **在 `execute` 循环里、`exec_once` 之后插入一行简单打印**。打开 [src/cpu/cpu-exec.c:L74-L83](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L74-L83)，在 `exec_once(&s, cpu.pc);` 这一行 **之后** 加入（示例代码，需自行验证 ISA 可移植性）：

   ```c
   /* 示例代码：最简 itrace，打印 pc 与指令机器码（riscv32 视角） */
   printf("itrace: pc = " FMT_WORD ", inst = %08x, ilen = %d\n",
          s.pc, s.isa.inst, (int)(s.snpc - s.pc));
   ```

   说明：
   - `s.pc` 是本条指令地址，`s.isa.inst` 是 32 位机器码，`s.snpc - s.pc` 是指令长度。
   - 如果你想做得 **ISA 无关**（兼容 x86 的变长指令），可改成按字节打印，模仿官方 `logbuf` 的写法：

     ```c
     /* 示例代码：按字节打印，ISA 无关 */
     int ilen = s.snpc - s.pc;
     uint8_t *b = (uint8_t *)&s.isa.inst;
     printf("itrace: pc = " FMT_WORD " |", s.pc);
     for (int i = 0; i < ilen; i++) printf(" %02x", b[i]);
     printf("\n");
     ```

3. **重新编译运行**。用你本地的构建命令（如 `make` 后运行 `./build/riscv32-nemu`，**待本地验证**）。进入 SDB 后执行：

   ```text
   (nemu) si 10
   ```

4. **观察前 10 条指令**。屏幕上应出现 10 行你刚加的 `itrace:` 输出（同时还会出现官方的单步打印 `logbuf`，因为 `10` 不小于 `MAX_INST_TO_PRINT=10`？注意这里是 `<` 而非 `<=`，`si 10` 时 `g_print_step = (10 < 10) = false`，所以官方不会打印，只有你加的这行会打印）。

### 需要观察的现象

- 每条指令的 `pc` 是否递增 4（riscv32 定长指令）？前几条通常是内置镜像的初始化代码（**具体字节待本地验证**）。
- `ilen` 是否都等于 4？
- 跳转指令执行后，下一条的 `pc` 是否不等于上一条 `pc + 4`？（这需要先实现 `jal` 等，属 u5-l16 范畴；若尚未实现，则暂时只看到顺序执行。）

### 预期结果

- `si 10` 打印 10 行 `itrace:`。
- 执行 `c`（继续运行到 trap）时，你的 `printf` 会 **疯狂刷屏**，因为它不像官方 `logbuf` 那样受 `g_print_step` 或 trace 窗口节流。这正是下一个分析点的引子。

### 分析 `MAX_INST_TO_PRINT` 的作用

回答下面三个问题（参考 [src/cpu/cpu-exec.c:L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L26) 与 [L101](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L101)）：

1. **它定义在哪里、值是多少？** 定义在 [cpu-exec.c:L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L26)，值为 `10`。
2. **它控制什么？** 它通过 `g_print_step = (n < MAX_INST_TO_PRINT)` 决定是否在 `trace_and_difftest` 里 `puts(logbuf)` 把每条指令打印到 **屏幕**。注意是比较 `<`，所以 `si 1`~`si 9` 会打印，`si 10` 及以上不打印。
3. **为什么需要它？** 注释（[L21-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L21-L25)）说得很清楚：用 `si` 单步时，你希望即时看到每条指令的汇编；但 `c` 会跑几千万条，逐条打印会刷屏并严重拖慢。所以用一个小阈值把「单步打印」限制在少量指令的场景。

把你上面加的 `printf` 和官方 `puts(logbuf)` 对比：官方版本 **受 `g_print_step` 节流**，而你的版本不受——这就是为什么你的版本在 `c` 时会刷屏。要修复，你可以把打印也用 `if (g_print_step)` 包起来，或者干脆复用官方的 `logbuf`（这正是 NEMU 设计者已经做好了的，详见 u8-l25）。

> 完成后记得 **回退你的修改**（删掉那行 `printf`），以免影响后续 PA 实验。

## 6. 本讲小结

- `cpu_exec(n)` 是 CPU 引擎的唯一入口，由 SDB 的 `c`/`si` 或 AM 模式的 `engine_start` 调用；`n` 是 `uint64_t`，`-1` 表示「跑到停」。
- 五种执行状态 `RUNNING/STOP/END/ABORT/QUIT` 构成一个状态机：`cpu_exec` 进入时设 `RUNNING`，收尾时按状态决定降级为 `STOP`、打印 `HIT GOOD/BAD TRAP` 还是直接 `statistic`；已终止态会被进入守卫拦下。
- 执行链路是三层：`cpu_exec`（状态分发）→ `execute`（循环 n 次）→ `exec_once`（单步一条）。
- `exec_once` 通过 `isa_exec_once(s)` 把取指译码执行交给 ISA 实现，并用 `pc/snpc/dnpc` 三个 PC 分别记录当前地址、顺序下一地址、动态下一地址，最后 `cpu.pc = dnpc` 提交。
- `g_nr_guest_inst` 在 `execute` 循环里每步 +1，是客机指令总数和 trace 时间轴的唯一来源。
- `trace_and_difftest` 是「每步一次」的三合一钩子：itrace 写日志、`g_print_step` 控制屏幕打印、`difftest_step` 与参考实现比对——这就是 trace 与 difftest 的插入点。
- `MAX_INST_TO_PRINT`（=10）通过 `g_print_step = (n < 10)` 控制 `si` 时的屏幕逐条打印，避免 `c` 刷屏。

## 7. 下一步学习建议

本讲只讲了 **驱动与调度**，把一条指令当成了黑盒（`isa_exec_once` 内部做了什么我们没展开）。接下来的三讲会层层打开这个黑盒：

- **u3-l10 取指与译码数据结构**：精读 `Decode` 结构、`inst_fetch` 如何取指并推进 `snpc`、`hostcall.c` 里 `set_nemu_state` 与 `invalid_inst` 的错误处理。学完你会彻底理解 `snpc` 是怎么被推进的、`INV` 宏如何触发 `NEMU_ABORT`。
- **u3-l11 INSTPAT 模式匹配译码机制**：NEMU 的招牌设计——把 `INSTPAT("????? ????? .....")` 这样的模式串编译成 `key/mask/shift`，运行时用位掩码匹配指令。
- **u5-l16 RISC-V 指令实现**：进入 `isa_exec_once` 内部，看一条 `addi`/`jal` 是怎么用 INSTPAT 实现的，以及 `ebreak` 如何约定成 `nemu_trap`（与本讲的 `NEMUTRAP` → `NEMU_END` 闭环）。

阅读建议：在进 u3-l10 之前，先用本讲综合实践里的 itrace 跑一次 `si 10`，把内置镜像前 10 条指令的字节码抄下来；等学完 u3-l11 和 u5-l16 后再回来，你应该能逐条手工译出这些字节码对应的汇编——那会是非常有成就感的闭环验证。
