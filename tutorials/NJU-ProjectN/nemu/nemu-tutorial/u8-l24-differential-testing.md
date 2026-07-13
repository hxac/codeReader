# 差分测试机制

## 1. 本讲目标

本讲讲解 NEMU 如何用「差分测试（Differential Testing）」来证明自己实现的指令是正确的。学完后你应当能够：

1. 说清楚 DUT 与 REF 的角色分工，以及 NEMU 为什么用 `dlopen` + 函数指针这种方式把两者粘起来。
2. 描述 `init_difftest` 的初始化同步过程，以及 `difftest_step` 每步比对寄存器的完整流程。
3. 解释 `difftest_skip_ref` 与 `difftest_skip_dut` 各自要解决的「不对齐」问题——尤其是为什么访问 MMIO 设备时必须 `difftest_skip_ref`，以及 QEMU 的「指令打包」如何用 `skip_dut_nr_inst` 追平。
4. 看懂 REF 侧需要导出哪些接口、`DIFFTEST_REG_SIZE` 如何与 `CPU_state` 的二进制布局对齐，并能动手在 menuconfig 里开启 DIFFTEST 以 spike 为 REF 编译运行。

## 2. 前置知识

在进入本讲前，你需要先具备以下认知（来自前置讲义）：

- **CPU 主循环与 `trace_and_difftest` 钩子（u3-l9）**：`cpu_exec → execute → exec_once` 三层调用链每执行一条客机指令后，会调用 `trace_and_difftest(&s, cpu.pc)`，它是「itrace 写日志 / 屏幕打印 / 差分比对」三合一的插入点。本讲的 `difftest_step` 就挂在这个钩子里。
- **RISC-V 指令实现与 `NEMUTRAP`（u5-l16）**：`ebreak` 被复用为 `nemu_trap`，返回码取自 `a0`，经 `NEMUTRAP` 宏置 `NEMU_END`。这是一条「NEMU 专属、REF 不会有同样语义」的指令，是差分测试必须跳过的典型场景之一。
- **执行状态机与 `set_nemu_state`（u7-l23）**：`set_nemu_state` 是所有停机路径的唯一闸口，写入 `state/halt_pc/halt_ret`。它在写状态前会先 `difftest_skip_ref()`，本讲会解释原因。
- **设备框架与 `find_mapid_by_addr`（u6-l18）**：MMIO 地址路由命中设备时会调用 `difftest_skip_ref()`，因为设备副作用无法在 REF 侧复现。

本讲用到但不再展开的术语：**DUT**（Design Under Test，被测设计，即 NEMU 自己）、**REF**（Reference，参考实现，一个被认为可信的模拟器，如 spike / QEMU / KVM）、**动态库**（Linux 下 `.so`，运行时加载）、**socket / GDB 协议**（QEMU/KVM REF 用网络端口通信）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/cpu/difftest/dut.c` | DUT 侧差分测试主体：`init_difftest`（加载 REF）、`difftest_step`（每步比对）、`difftest_skip_ref/skip_dut`、`checkregs`。 |
| `src/cpu/difftest/ref.c` | REF 侧的「占位实现」：5 个导出函数全部 `assert(0)`，是 REF 真实实现的接口模板。 |
| `include/cpu/difftest.h` | DUT 侧对外接口：函数指针声明、条件编译的空实现、`difftest_check_reg` 比对辅助函数。 |
| `include/difftest-def.h` | DUT 与 REF 共享的契约：`__EXPORT` 宏、`DIFFTEST_TO_DUT/REF` 方向枚举、各 ISA 的 `DIFFTEST_REG_SIZE`。 |
| `src/isa/riscv32/difftest/dut.c` | ISA 侧接缝：`isa_difftest_checkregs`（待实现）、`isa_difftest_attach`。 |
| `tools/spike-diff/difftest.cc` | REF 真实实现之一：以 spike 为引擎，直接导出 5 个 `__EXPORT` 函数。 |
| `tools/qemu-diff/src/diff-test.c` | REF 真实实现之二：fork QEMU 子进程，经 GDB 协议/socket 通信。 |
| `tools/difftest.mk` | 构建接线：如何把 REF 编译成 `.so`、如何把 `--diff=` 参数喂给 NEMU。 |
| `Kconfig` | 配置开关：`CONFIG_DIFFTEST` 及 REF 选择（spike/qemu/kvm）。 |

## 4. 核心概念与源码讲解

本讲按 5 个最小模块展开：先建立 DUT/REF 协作架构（4.1），再分别讲初始化加载（4.2）、每步比对（4.3）、两种跳过对齐（4.4），最后讲 REF 侧契约与构建接线（4.5）。

### 4.1 差分测试原理与 DUT/REF 协作架构

#### 4.1.1 概念说明

你在 PA 里一条条实现 RISC-V 指令，怎么知道每条都写对了？单测只能覆盖你想到的输入。差分测试换了一个思路：**找一个已经被广泛使用、可信的模拟器当「参考答案」（REF），让你写的 NEMU（DUT）和它跑同一份程序，每执行一条指令就比对一次两边的寄存器状态——只要有一次不一致，就说明 DUT 这条指令实现错了。** 这是一种「协同仿真（co-simulation）」思想，比手写测试用例强大得多，因为它能用整个程序当测试输入。

NEMU 没有把 REF 硬编码进自己，而是用了一个解耦设计：

- REF 被编译成一个**动态库**（`.so`），导出几个约定好名字的函数。
- DUT 在运行时用 `dlopen` 加载这个 `.so`，用 `dlsym` 取出函数地址，存进**函数指针**。
- 之后 DUT 调用这些函数指针来驱动 REF，就像调用普通函数一样。

这样做的好处是：DUT 的源码不依赖任何具体 REF，换 REF（spike / QEMU / KVM）只需换一个 `.so` 文件，重新编译都不用。代价是性能——每条指令都要让 REF 也走一步并比对，所以 Kconfig 里反复提醒「会显著降低性能」。

#### 4.1.2 核心流程

差分测试的完整生命周期分三阶段：

```text
启动期（init_difftest）
  命令行 --diff=REF_SO  ──►  dlopen(REF_SO)
                            ──►  dlsym 取 5 个函数指针
                            ──►  ref_difftest_init(port)        # REF 自身初始化
                            ──►  ref_difftest_memcpy(镜像)       # 把程序镜像同步给 REF
                            ──►  ref_difftest_regcpy(DUT→REF)   # 把寄存器初值同步给 REF
运行期（每条指令后）
  exec_once 走完一条 DUT 指令
    └─► trace_and_difftest
          └─► difftest_step(pc, npc)
                ├─ 正常: ref_difftest_exec(1) → 取 REF 寄存器 → checkregs 比对
                ├─ skip_ref: 把 DUT 寄存器拷给 REF，REF 不执行
                └─ skip_dut: 等 DUT 追上 REF.pc
失败时
  checkregs 不一致 → NEMU_ABORT → 打印寄存器 → is_exit_status_bad 返回 1
```

#### 4.1.3 源码精读

差分测试的入口是命令行参数 `--diff`。`monitor.c` 解析它并存到 `diff_so_file`，最后在初始化链里交给 `init_difftest`：

[src/monitor/monitor.c:L71-L99](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L71-L99) —— `parse_args` 用 `getopt_long` 解析 `-d/--diff=REF_SO`（第 75 行表项、第 86 行 `case 'd'`），存入静态变量 `diff_so_file`。

[src/monitor/monitor.c:L125-L126](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L125-L126) —— `init_monitor` 在「加载镜像」之后调用 `init_difftest(diff_so_file, img_size, difftest_port)`。注意它排在 `load_img` 之后：必须先有镜像才能同步给 REF。

DUT 侧的「函数指针表」定义在 `dut.c` 顶部，4 个全局函数指针初始为 `NULL`：

[src/cpu/difftest/dut.c:L24-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L24-L27) —— `ref_difftest_memcpy / regcpy / exec / raise_intr` 四个函数指针，将在 `init_difftest` 里被 `dlsym` 填上。

整份 `dut.c` 的差分逻辑被 `#ifdef CONFIG_DIFFTEST` 包裹（[src/cpu/difftest/dut.c:L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L29)）。未开启时，`init_difftest` 是一个空函数（[src/cpu/difftest/dut.c:L131](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L131)），其余接口在头文件里被替换成 `static inline` 空实现——这就是为什么关掉 DIFFTEST 时差分代码零开销。

#### 4.1.4 代码实践

**实践目标**：建立「命令行参数 → REF 加载 → 每步比对」的全局数据流印象，不动源码。

**操作步骤**：

1. 打开 `src/monitor/monitor.c`，从 `parse_args` 的 `case 'd'` 开始，跟踪 `diff_so_file` 这个变量如何流到 `init_difftest`。
2. 打开 `src/cpu/cpu-exec.c` 第 35–41 行 `trace_and_difftest`，确认 `difftest_step(_this->pc, dnpc)`（第 40 行）挂在 `exec_once` 之后。
3. 回到 `execute` 循环（第 74–83 行），确认调用顺序是 `exec_once → g_nr_guest_inst++ → trace_and_difftest → 状态检查`。

**需要观察的现象**：`difftest_step` 的两个参数 `pc` 与 `npc` 分别对应「本条指令地址」与「提交后的下一条地址」。在 `execute` 里 `trace_and_difftest(&s, cpu.pc)` 传入的第二个实参是 `cpu.pc`，而此时 `cpu.pc` 已在第 47 行被更新为 `s->dnpc`。

**预期结果**：画出 `--diff=REF_SO` → `diff_so_file` → `init_difftest` → `dlopen` → 函数指针 → `difftest_step` 的调用链。结论：DUT 把 REF 当成一个「外挂的可信计算器」，每步问它「你走完这步寄存器长啥样」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 NEMU 不直接 `#include` spike 的头文件、把 REF 编译进自己的二进制，而要走 `dlopen` 动态库？

**参考答案**：为了解耦。其一，REF 可能是 C++（spike）而 NEMU 是 C，链接麻烦；其二，换 REF（spike/qemu/kvm）只需换 `.so`，DUT 无需重编；其三，REF 可以独立维护、独立编译，甚至用 socket 通信（qemu-diff）而非进程内调用，统一藏在函数指针后面。

**练习 2**：`init_difftest` 排在 `load_img` 之后、`init_sdb` 之前，能否调换 `init_difftest` 与 `init_sdb` 的顺序？

**参考答案**：可以。二者无数据依赖：`init_difftest` 只依赖镜像与寄存器（`init_isa`/`load_img` 已完成），`init_sdb` 初始化正则与监视点池。它们都依赖 `init_mem`/`init_isa`，但彼此独立，故可调换。

### 4.2 init_difftest：dlopen 加载 REF 与初始同步

#### 4.2.1 概念说明

`init_difftest` 是差分测试的「点火」函数，做三件事：

1. **加载**：`dlopen` 打开 REF 的 `.so`，`dlsym` 取出 5 个约定名字的函数地址，填进 4.1 提到的函数指针（外加一个本地取的 `difftest_init`）。
2. **REF 自身初始化**：调用 `ref_difftest_init(port)`，让 REF 准备好（spike 建模拟器实例；qemu-diff fork QEMU 子进程并连 GDB）。
3. **初始状态同步**：把 DUT 的镜像内存和寄存器初值拷给 REF，让两边从**完全相同的起点**出发。这一步至关重要——差分测试比的是「同一条指令执行后的状态」，若起点不同则步步不同。

`ref_difftest_*` 这组函数指针就是 DUT/REF 的「接口契约」：DUT 只认这 4 个指针（外加 `difftest_init`），不关心 REF 内部是 spike 还是 QEMU。这正是「接口先行、实现后补」的工程化思路。

#### 4.2.2 核心流程

```text
init_difftest(ref_so_file, img_size, port):
  handle = dlopen(ref_so_file, RTLD_LAZY)          # 加载动态库
  ref_difftest_memcpy   = dlsym(handle, "difftest_memcpy")
  ref_difftest_regcpy   = dlsym(handle, "difftest_regcpy")
  ref_difftest_exec     = dlsym(handle, "difftest_exec")
  ref_difftest_raise_intr = dlsym(handle, "difftest_raise_intr")
  ref_difftest_init     = dlsym(handle, "difftest_init")
  ref_difftest_init(port)                            # REF 初始化
  ref_difftest_memcpy(RESET_VECTOR, guest_to_host(RESET_VECTOR),
                      img_size, DIFFTEST_TO_REF)     # 镜像 → REF
  ref_difftest_regcpy(&cpu, DIFFTEST_TO_REF)         # 寄存器 → REF
```

#### 4.2.3 源码精读

[src/cpu/difftest/dut.c:L62-L92](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L62-L92) —— `init_difftest` 全貌：`dlopen` 后连续 5 次 `dlsym`，每个都跟一个 `assert` 确保符号存在（REF 漏导出任何一个都会在这里崩）；随后 `ref_difftest_init(port)`、`ref_difftest_memcpy` 同步镜像、`ref_difftest_regcpy` 同步寄存器。

注意第 90 行 `ref_difftest_memcpy(RESET_VECTOR, guest_to_host(RESET_VECTOR), img_size, DIFFTEST_TO_REF)`：源地址用 `guest_to_host(RESET_VECTOR)` 把客机物理地址转成宿主机指针（u4-l12 讲过的平移），直接把 DUT 内存里的镜像字节流灌给 REF。方向参数 `DIFFTEST_TO_REF` 在 `difftest-def.h` 里定义：

[include/difftest-def.h:L23-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/difftest-def.h#L23-L24) —— `__EXPORT`（`visibility("default")`，确保符号导出给 DUT `dlsym`）与 `enum { DIFFTEST_TO_DUT, DIFFTEST_TO_REF }` 方向枚举（值为 0/1）。

`regcpy` 的方向是双向的：`DIFFTEST_TO_REF` 把 DUT 寄存器写给 REF（初始化、skip_ref 时用），`DIFFTEST_TO_DUT` 把 REF 寄存器读回 DUT 侧的临时变量做比对（`difftest_step` 用）。单个函数靠 `direction` 参数复用两个方向。

#### 4.2.4 代码实践

**实践目标**：理解「先同步镜像、再同步寄存器」的顺序为何不可颠倒。

**操作步骤**：

1. 阅读第 90、91 行，确认顺序是 `memcpy(镜像)` 在前、`regcpy(寄存器)` 在后。
2. 设想颠倒顺序：先 `regcpy(&cpu, DIFFTEST_TO_REF)` 把 `cpu.pc = RESET_VECTOR` 写给 REF，再 `memcpy` 灌镜像。
3. 思考：spike 的 `difftest_init`（[tools/spike-diff/difftest.cc:L102-L124](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/spike-diff/difftest.cc#L102-L124)）内部已经 `new sim_t(...)` 建了一个空内存的模拟器。

**需要观察的现象**：REF 的 `difftest_init` 通常会把自己的内存清零、寄存器置初值。若先 `regcpy` 设好 `pc` 再让 `memcpy` 写镜像，问题不大；但若 REF 的 `difftest_init` 在内部重置了寄存器/内存，则后调用的那一步会覆盖先调用的那一步。

**预期结果**：当前顺序「init → memcpy → regcpy」保证镜像先就位、最后用 DUT 寄存器覆盖 REF 的任何初值，确保两边 `pc` 与 `gpr` 完全一致。结论：**最后一步必须是 `regcpy`**，否则 REF 内部初始化可能把寄存器冲掉。

#### 4.2.5 小练习与答案

**练习 1**：`dlsym` 之后为什么每个都跟一句 `assert(...)`？

**参考答案**：`dlsym` 找不到符号时返回 `NULL` 且不报错（仅在 `dlerror` 里有信息）。若不检查，后续通过 `NULL` 函数指针调用会段错误，排错困难。`assert` 把「REF 漏导出某符号」这个错误提前到加载期并明确指出。

**练习 2**：`dlopen` 用了 `RTLD_LAZY` 而非 `RTLD_NOW`，有什么影响？

**参考答案**：`RTLD_LAZY` 推迟符号解析——动态库里的外部符号在首次被调用时才解析，而非加载时全部解析。对 REF 而言加载更快、且能容忍库内有未被调用的未解析符号；代价是符号错误可能到运行时才暴露。这里取它是因为 REF（尤其 spike C++ 库）符号量大、且只用到导出的几个函数。

### 4.3 difftest_step：每步寄存器比对

#### 4.3.1 概念说明

`difftest_step(pc, npc)` 是差分测试的心脏，由 `trace_and_difftest` 在每条 DUT 指令执行后调用一次。它的核心职责是：**让 REF 也走一步，取回 REF 的寄存器，与 DUT 比对，不一致就让 NEMU 进入 `NEMU_ABORT`。**

实际比对分两层：

- 框架层 `checkregs`：拿到 REF 寄存器后调 ISA 侧的 `isa_difftest_checkregs`，若返回 `false` 就置 `NEMU_ABORT`、记录 `halt_pc`、调 `isa_reg_display()` 打印寄存器（这是排错第一现场）。
- ISA 层 `isa_difftest_checkregs`：逐个比较 `gpr` 与 `pc`，通常借助头文件里的 `difftest_check_reg` 辅助函数打印「哪个寄存器、right 值、wrong 值、diff 位」。

`difftest_step` 还要处理两种「不走正常比对」的分支（skip_ref / skip_dut），留到 4.4 讲。本节先聚焦正常路径。

#### 4.3.2 核心流程

```text
difftest_step(pc, npc):
  if (skip_dut_nr_inst > 0):  ... 追赶 REF, 见 4.4
  if (is_skip_ref):           ... 跳过 REF, 见 4.4

  # 正常路径
  ref_difftest_exec(1)                  # REF 单步执行一条
  ref_difftest_regcpy(&ref_r, DIFFTEST_TO_DUT)   # 取回 REF 寄存器到 ref_r
  checkregs(&ref_r, pc)                 # 与 DUT(cpu) 比对

checkregs(ref, pc):
  if (!isa_difftest_checkregs(ref, pc)):
    nemu_state.state = NEMU_ABORT
    nemu_state.halt_pc = pc
    isa_reg_display()                   # 打印 DUT 寄存器供对照
```

#### 4.3.3 源码精读

[src/cpu/difftest/dut.c:L102-L129](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L102-L129) —— `difftest_step` 全貌。第 125–128 行是正常路径三连：`ref_difftest_exec(1)` 让 REF 走一步、`ref_difftest_regcpy(&ref_r, DIFFTEST_TO_DUT)` 把 REF 寄存器读进局部变量 `ref_r`、`checkregs(&ref_r, pc)` 比对。

[src/cpu/difftest/dut.c:L94-L100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L94-L100) —— `checkregs`：委托 ISA 侧 `isa_difftest_checkregs`，失败则置 `NEMU_ABORT`、记 `halt_pc`、调 `isa_reg_display()`（u5-l15 讲过，打印全部寄存器）。

[src/isa/riscv32/difftest/dut.c:L20-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/difftest/dut.c#L20-L25) —— riscv32 的 `isa_difftest_checkregs` 当前是**待实现**的 stub，直接 `return false`。这意味着：一旦开启 DIFFTEST，第一次 `difftest_step` 就会因 `false` 而 ABORT。所以**学生必须先实现这个函数**，差分测试才能跑起来。

比对辅助函数在头文件里，便于 ISA 侧逐个寄存器比较并打印差异：

[include/cpu/difftest.h:L43-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/difftest.h#L43-L51) —— `difftest_check_reg(name, pc, ref, dut)`：若 `ref != dut`，用 `Log` 打印「寄存器名、pc、right(ref)、wrong(dut)、diff(异或)」并返回 `false`，否则返回 `true`。`diff = ref ^ dut` 直接给出哪些 bit 不同，定位 bug 很方便。

#### 4.3.4 代码实践

**实践目标**：实现 riscv32 的 `isa_difftest_checkregs`，让差分测试真正可比对。

**操作步骤**：

1. 打开 `src/isa/riscv32/difftest/dut.c`，把 `return false` 改为逐个比较 `ref_r->gpr[i]` 与 `cpu.gpr[i]`（i 从 0 到 31）以及 `ref_r->pc` 与 `cpu.pc`。
2. 用 `difftest_check_reg` 辅助函数（[include/cpu/difftest.h:L43-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/difftest.h#L43-L51)）打印差异，全部相等才返回 `true`。`regs[]` 名表（u5-l15）可提供寄存器名作为第一个参数。
3. 编译后以 spike 为 REF 运行（见 4.5 或综合实践），观察是否出现「xxx is different after executing instruction at pc = ...」。

**需要观察的现象**：若某条指令实现有 bug，会看到类似 `a0 is different after executing instruction at pc = 0x80000008, right = 0x..., wrong = 0x..., diff = 0x...` 的日志，随后 NEMU ABORT 并打印 `isa_reg_display` 的全部寄存器。

**预期结果**：实现正确且指令无 bug 时，程序一路跑到 `HIT GOOD TRAP`；若有 bug，则精确定位到出错指令的 pc 与出错寄存器。本实践涉及修改源码（PA 作业性质允许），属「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`checkregs` 失败时为什么调用 `isa_reg_display()` 而不是 `isa_difftest_checkregs` 自己打印就够了？

**参考答案**：`isa_difftest_checkregs` 只打印「哪个寄存器不同」的差异行；`isa_reg_display()` 则把 DUT 全部 32 个寄存器与 pc 成片打印出来，便于和 REF 的寄存器做整体对照、快速发现连带错误（一个寄存器错往往牵连多条指令的写回）。两者互补。

**练习 2**：`difftest_step` 比对时用的是 DUT 的 `cpu` 全局变量，而 REF 寄存器存在局部变量 `ref_r` 里。为什么 REF 不也用一个全局变量？

**参考答案**：REF 的状态完全由 REF 自己管理（spike 的 `sim_t` 对象、qemu 的子进程），DUT 只能通过 `regcpy` 接口「快照」式地取回它的寄存器。用一个局部变量 `ref_r` 承接这次快照，既避免引入跨函数的全局状态，也明确表达「这是本步 REF 的瞬时寄存器值」。

### 4.4 difftest_skip_ref / difftest_skip_dut：对齐不可复现行为与指令打包

#### 4.4.1 概念说明

理想情况下「DUT 走一步、REF 走一步、比寄存器」就能覆盖所有指令。但现实有两类指令会破坏这种一对一：

**第一类：DUT 执行了 REF 无法或不应复现的行为——用 `difftest_skip_ref()` 跳过 REF。**

典型场景：

- **MMIO 设备访问**：DUT 读写设备寄存器会有真实副作用（串口输出、RTC 取时间），REF 侧没有这个设备、或副作用不可复现。若仍让 REF 走一步并比对，必然不一致。解决办法：这一步不让 REF 执行，而是把 DUT 的寄存器直接拷给 REF（`regcpy(DUT→REF)`），让 REF「假装也走过了」、状态与 DUT 保持一致。
- **`nemu_trap`（ebreak）与 `invalid_inst`**：`ebreak` 在 NEMU 里被复用为 trap（u5-l16），REF 的语义完全不同；非法指令在 NEMU 里触发 `NEMU_ABORT`。这些是 NEMU 专属语义，REF 不该跟着走，故 `set_nemu_state` 在停机前先 `difftest_skip_ref()`。

**第二类：REF 一步走的指令数 ≠ DUT 一步走的指令数——用 `difftest_skip_dut(nr_ref, nr_dut)` 追平。**

QEMU 是翻译型模拟器，有时「让它单步一次」会翻译并执行一整段（多条客机指令），称为**指令打包（instruction packing）**。这时 DUT 走 1 条、REF 走了 N 条，pc 对不上。语义约定是：先让 REF 立刻走 `nr_ref` 步，然后允许 DUT 在接下来的 `nr_dut` 步内「追上」REF 的 pc；追上之前不比对，追上后恢复比对。

#### 4.4.2 核心流程

```text
# 跳过 REF（不可复现）
difftest_skip_ref():
  is_skip_ref = true
  skip_dut_nr_inst = 0      # 同时清掉追赶计数

difftest_step 检测到 is_skip_ref:
  ref_difftest_regcpy(&cpu, DIFFTEST_TO_REF)   # DUT 寄存器覆盖 REF
  is_skip_ref = false
  return                                       # 不 exec、不比对

# 跳过 DUT 比对（指令打包）
difftest_skip_dut(nr_ref, nr_dut):
  skip_dut_nr_inst += nr_dut
  while (nr_ref-- > 0): ref_difftest_exec(1)    # 立刻让 REF 走 nr_ref 步

difftest_step 检测到 skip_dut_nr_inst > 0:
  ref_difftest_regcpy(&ref_r, DIFFTEST_TO_DUT)  # 看 REF 现在 pc 在哪
  if (ref_r.pc == npc):                         # DUT 追上了
    skip_dut_nr_inst = 0; checkregs(&ref_r, npc); return
  skip_dut_nr_inst --
  if (skip_dut_nr_inst == 0): panic("can not catch up ...")  # 没追上，报错
```

#### 4.4.3 源码精读

[src/cpu/difftest/dut.c:L36-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L36-L46) —— `difftest_skip_ref`：置 `is_skip_ref = true`，并把 `skip_dut_nr_inst` 清零（注释解释：若被跳过的指令恰好处于 QEMU 打包段中，干脆结束追赶以尽力保持一致）。注释里坦承这不完美——若打包段已写内存而后续指令又读该内存，会出现假阴性（漏报），但这种情况罕见。

[src/cpu/difftest/dut.c:L54-L60](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L54-L60) —— `difftest_skip_dut`：累加 `skip_dut_nr_inst += nr_dut`，并立刻 `while (nr_ref--) ref_difftest_exec(1)` 让 REF 先走 `nr_ref` 步。

[src/cpu/difftest/dut.c:L102-L129](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L102-L129) —— `difftest_step` 的两个跳过分支：第 105–116 行处理 `skip_dut_nr_inst > 0`（追赶 REF.pc），第 118–123 行处理 `is_skip_ref`（把 DUT 寄存器拷给 REF、跳过 exec 与比对）。

**谁调用 `difftest_skip_ref`？** 两个关键调用点：

[include/device/map.h:L37-L46](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/map.h#L37-L46) —— `find_mapid_by_addr` 在 MMIO 地址命中设备时（第 41 行）立即 `difftest_skip_ref()`：因为这次访存会触发设备回调（副作用），REF 无法复现，必须跳过。

[src/engine/interpreter/hostcall.c:L21-L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/hostcall.c#L21-L26) —— `set_nemu_state`（trap/abort 的唯一闸口，u7-l23）在第 22 行先 `difftest_skip_ref()`：因为 `NEMUTRAP`（ebreak）与 `invalid_inst` 都是 NEMU 专属语义，REF 不该跟着停。

#### 4.4.4 代码实践

**实践目标**：理解为什么 MMIO 访问与 `set_nemu_state` 必须 `difftest_skip_ref`。

**操作步骤**：

1. 在 `include/device/map.h` 第 41 行 `difftest_skip_ref()` 处，设想把它注释掉会发生什么。
2. 跟踪一次串口输出：DUT 执行 `sb` 写串口寄存器 → `paddr_write` → `mmio_write` → `find_mapid_by_addr` 命中 → `map_write` 调 `serial_putc` 输出到 stderr（u6-l19）。
3. 思考：若没有 `difftest_skip_ref`，`difftest_step` 会让 REF 也执行这条 `sb`，REF 侧没有串口设备（或行为不同），REF 的寄存器/内存与 DUT 可能不一致。

**需要观察的现象**：当前实现下，命中 MMIO 后 `is_skip_ref = true`，`difftest_step` 把 DUT 寄存器拷给 REF、跳过比对，两边状态保持一致；若去掉则会因为设备副作用在很早的某条 `sb`/`lbu` 处误报不一致。

**预期结果**：结论——**`difftest_skip_ref` 是「DUT 侧的特权指令/设备副作用」逃生口**，没有它差分测试会被设备访问干扰而频频误报。本实践为源码阅读型，不修改源码即能完成推理。

#### 4.4.5 小练习与答案

**练习 1**：`difftest_skip_ref` 里为什么要把 `skip_dut_nr_inst` 清零？

**参考答案**：被跳过的指令可能正落在 QEMU 打包段里。既然这一步 DUT 与 REF 都不比对（直接把 DUT 状态拷给 REF），继续之前的「追赶」已无意义且可能错位，干脆清零结束追赶，以「DUT 寄存器覆盖 REF」作为新的对齐基准。

**练习 2**：`difftest_skip_dut` 的追赶循环里，若 `nr_dut` 步内 DUT 的 pc 始终没等于 `ref_r.pc`，会发生什么？

**参考答案**：每步 `skip_dut_nr_inst--`，当它减到 0 仍未追上时，第 113–114 行 `panic("can not catch up with ref.pc = ...")` 直接终止运行。这表示「DUT 在预期步数内没追上 REF」，通常是 `nr_ref/nr_dut` 估计不当或 DUT 实现有误。

**练习 3**：`difftest_skip_dut` 的注释说「Let REF run `nr_ref` instructions first. We expect that DUT will catch up with REF within `nr_dut` instructions.」请解释「追上」的判定标准是什么。

**参考答案**：判定标准是「REF 当前的 pc == DUT 提交后的 npc」（第 107 行 `ref_r.pc == npc`）。即 DUT 走到这一步时，它的下一条指令地址正好落在 REF 已经执行到的位置，说明两边在指令流上重新对齐，可恢复逐条比对。

### 4.5 REF 侧接口、DIFFTEST_REG_SIZE 与构建接线

#### 4.5.1 概念说明

DUT 通过 4 个函数指针驱动 REF，REF 侧就必须导出对应符号——这是双方契约的另一端。REF 需要导出 5 个 `__EXPORT` 函数：

| 函数 | 作用 |
| --- | --- |
| `difftest_init(port)` | REF 自身初始化（spike 建 `sim_t`；qemu fork 子进程连 GDB）。 |
| `difftest_memcpy(addr, buf, n, direction)` | 在 REF 内存与 `buf` 间拷贝 `n` 字节（初始化镜像用）。 |
| `difftest_regcpy(dut, direction)` | 在 REF 寄存器与 `dut` 指向的 `CPU_state` 间拷贝。 |
| `difftest_exec(n)` | 让 REF 执行 `n` 条指令。 |
| `difftest_raise_intr(NO)` | 让 REF 也响应一次中断（u7-l21 提到 DUT 侧 `isa_raise_intr` 后需 `ref_difftest_raise_intr` 同步）。 |

`regcpy` 靠 `memcpy` 在 DUT 的 `CPU_state` 与 REF 的寄存器结构间直接搬字节，因此**两边的寄存器结构二进制布局必须完全一致**——字段顺序、宽度、数量都要对齐。这个「应搬多少字节」由 `DIFFTEST_REG_SIZE` 约定，是 DUT/REF 的另一条共享契约，定义在 `difftest-def.h`。

NEMU 自带一个 REF「占位实现」`src/cpu/difftest/ref.c`：5 个函数全部 `assert(0)`。它的作用是**声明接口模板**——当你想用「另一个 NEMU 构建」当 REF 时（`CONFIG_DIFFTEST_REF_NEMU`），你会基于这个模板填实现。真正的外部 REF（spike/qemu/kvm）在 `tools/` 下各自实现。

构建上，REF 被编成带 `-so` 后缀的共享库（如 `build/riscv32-spike-so`），运行时由 `--diff=` 传入。这一接线由 `tools/difftest.mk` 与 `scripts/native.mk` 完成，开关在 `Kconfig`。

#### 4.5.2 核心流程

```text
配置期 (menuconfig)
  CONFIG_DIFFTEST=y  ──►  choice: spike / qemu / kvm
  └─► DIFFTEST_REF_PATH = tools/spike-diff (举例)
      DIFFTEST_REF_NAME = spike

构建期 (make)
  difftest.mk:
    DIFF_REF_SO = build/$(GUEST_ISA)-$(REF_NAME)-so   # 如 riscv32-spike-so
    若非 NEMU-REF: make -C tools/spike-diff SHARE=1 ENGINE=interpreter
  native.mk:
    ARGS += --diff=$(DIFF_REF_SO)                     # 自动带上 --diff
    run: 依赖 $(BINARY) 与 $(DIFF_REF_SO)

运行期
  NEMU 收到 --diff=.../riscv32-spike-so
    └─► init_difftest ─► dlopen 加载该 .so
```

#### 4.5.3 源码精读

**`DIFFTEST_REG_SIZE` 与方向枚举**：

[include/difftest-def.h:L23-L38](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/difftest-def.h#L23-L38) —— 按各 ISA 计算寄存器区大小。以 riscv（第 30–33 行）为例：`RISCV_GPR_TYPE` 随 `CONFIG_RV64` 在 `uint64_t/uint32_t` 间选、`RISCV_GPR_NUM` 随 `CONFIG_RVE` 在 16/32 间选，`DIFFTEST_REG_SIZE = sizeof(GPR_TYPE) * (GPR_NUM + 1)`（`+1` 是 pc）。riscv32 即 `4 * (32+1) = 132` 字节，恰好等于 `CPU_state` 的 `gpr[32] + pc`（见 [src/isa/riscv32/include/isa-def.h:L21-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L21-L24)）。x86 为 9 个 32 位（8 GPR + pc），mips32 为 38 个（含 status/lo/hi/badvaddr/cause/pc）。这种「布局由 ISA 定义、大小由宏计算、二者必须吻合」的设计，使同一份 `regcpy` 适配多 ISA。

**REF 占位实现（接口模板）**：

[src/cpu/difftest/ref.c:L21-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/ref.c#L21-L42) —— 5 个 `__EXPORT` 函数全部 `assert(0)`，仅 `difftest_init` 做了 `init_mem()` + `init_isa()`。这是「用 NEMU 当 REF」时的起点模板。

**真实 REF 之一：spike（直接库调用）**：

[tools/spike-diff/difftest.cc:L39-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/spike-diff/difftest.cc#L39-L42) —— `diff_context_t` 结构 `{ gpr[32], pc }`，与 NEMU 的 `CPU_state` 布局一致，正是 `regcpy` 能直接 `memcpy` 的前提。

[tools/spike-diff/difftest.cc:L82-L100](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/spike-diff/difftest.cc#L82-L100) —— `difftest_memcpy` 调 spike 的 `mmu->store`、`difftest_regcpy` 调 `diff_set_regs/diff_get_regs`、`difftest_exec` 调 `sim_t::step`。spike 是进程内 C++ 对象，调用直接而快。

**真实 REF 之二：qemu（fork + socket/GDB）**：

[tools/qemu-diff/src/diff-test.c:L49-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/qemu-diff/src/diff-test.c#L49-L51) —— `difftest_exec` 用 `while (n--) gdb_si()` 通过 GDB 协议让 QEMU 单步。

[tools/qemu-diff/src/diff-test.c:L53-L94](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/qemu-diff/src/diff-test.c#L53-L94) —— `difftest_init` 用 `fork()` 起一个 QEMU 子进程（第 79 行 `execlp(ISA_QEMU_BIN, ...)` 带 `-gdb tcp::port`），父进程用 `gdb_connect_qemu(port)` 经 socket 连上。这解释了为什么 `init_difftest` 要传 `port` 参数。注意第 96–99 行 `difftest_raise_intr` 在 qemu-diff 里 `assert(0)`（不支持），而 spike 版（[tools/spike-diff/difftest.cc:L126-L129](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/spike-diff/difftest.cc#L126-L129)）有真实实现——选不同 REF 能力不同。

**构建接线**：

[Kconfig:L155-L193](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L155-L193) —— `config DIFFTEST` 依赖 `TARGET_NATIVE_ELF`（u1-l2 讲过，AM/共享库模式不可用）；`choice` 按 ISA 给默认 REF（riscv→spike、x86→kvm、其余→qemu）；`DIFFTEST_REF_PATH/NAME` 把选择映射到 `tools/xxx-diff` 目录与名字。

[tools/difftest.mk:L16-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/difftest.mk#L16-L28) —— `DIFF_REF_SO = build/$(GUEST_ISA)-$(REF_NAME)-so`（如 `riscv32-spike-so`）；`ifndef CONFIG_DIFFTEST_REF_NEMU` 时用 `make -C $(DIFF_REF_PATH) SHARE=1 ENGINE=interpreter` 编译外部 REF；`ARGS_DIFF = --diff=$(DIFF_REF_SO)` 把运行参数准备好。

[scripts/native.mk:L19-L34](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/scripts/native.mk#L19-L34) —— 第 19 行 `include difftest.mk`；第 28 行 `ARGS += $(ARGS_DIFF)`（开启 DIFFTEST 后自动带 `--diff=`）；第 34 行 `run-env: $(BINARY) $(DIFF_REF_SO)` 把 REF `.so` 设为 `run` 的依赖，`make run` 会先编 REF 再跑 NEMU。

#### 4.5.4 代码实践

**实践目标**：亲手开启 DIFFTEST、选 spike 为 REF、编译并运行，看清 REF `.so` 的产物与运行输出。

**操作步骤**：

1. `make menuconfig`，进入 *Testing and Debugging*，开启 `Enable differential testing`（`CONFIG_DIFFTEST`），确认 *Reference design* 为 `Spike`（riscv 默认）。
2. 保存退出，`make` 编译 NEMU 主二进制；再 `make run`，观察它是否会先编译 `tools/spike-diff` 产出 `build/riscv32-spike-so`（由 `run-env` 依赖触发）。
3. 运行后留意启动日志里的 `Differential testing: ON` 与 `The result of every instruction will be compared with .../riscv32-spike-so`（来自 [src/cpu/difftest/dut.c:L84-L87](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L84-L87)）。

**需要观察的现象**：编译期生成 `build/riscv32-nemu-interpreter`（DUT）与 `build/riscv32-spike-so`（REF）两个产物；运行期性能明显下降（每步都要让 spike 走一步并比对）；若 `isa_difftest_checkregs` 未实现（仍 `return false`），会立即 ABORT。

**预期结果**：在已正确实现 `isa_difftest_checkregs` 与基本指令的前提下，内置镜像能跑到 `HIT GOOD TRAP`；若指令有 bug，会精确报出出错 pc 与寄存器差异。能否成功取决于本地是否已安装 spike 依赖，若 `tools/spike-diff` 编译失败则为「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：riscv32 下 `DIFFTEST_REG_SIZE` 是多少？为什么必须等于 `sizeof(CPU_state)`？

**参考答案**：`4 * (32 + 1) = 132` 字节。`difftest_regcpy` 用 `memcpy` 按 `DIFFTEST_REG_SIZE` 搬字节，若它不等于 `sizeof(CPU_state)`，要么搬多了读到结构体外内存、要么搬少了丢字段，比对就失真。它等于 `gpr[32]`（32×4）加 `pc`（4）。

**练习 2**：`CONFIG_RVE` 同时影响 `CPU_state` 和 `DIFFTEST_REG_SIZE`，体现了什么设计原则？

**参考答案**：同一处宏（`MUXDEF(CONFIG_RVE, 16, 32)`）既作用于 [src/isa/riscv32/include/isa-def.h:L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/include/isa-def.h#L22) 的 `gpr` 数组大小、又作用于 [include/difftest-def.h:L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/difftest-def.h#L32) 的 `RISCV_GPR_NUM`，确保 `CPU_state` 与 `DIFFTEST_REG_SIZE` 同步缩放。这是「单一真相源（single source of truth）」原则——配置改动一次，结构体与差分契约自动一致。

**练习 3**：spike 版 `difftest_raise_intr` 有实现，qemu 版 `assert(0)`。这对差分测试中断意味着什么？

**参考答案**：用 spike 作 REF 时，DUT 的 `isa_raise_intr` 可通过 `ref_difftest_raise_intr` 同步给 REF、继续比对中断后的状态；用 qemu 作 REF 时一旦发生中断就会 `assert(0)` 崩溃，说明 qemu-diff 不支持中断差分。选 REF 时要考虑它对中断、内存、特权级等的支持范围。

## 5. 综合实践

把本讲内容串起来，完成下面这个贯穿性任务：

**任务**：阅读 `difftest_step` 与 `checkregs`，解释 `skip_dut_nr_inst` 如何处理 QEMU 指令打包；再在 menuconfig 开启 DIFFTEST 以 spike 为 REF 编译运行，分析输出。

**步骤**：

1. **读代码回答问题**：
   - 打开 [src/cpu/difftest/dut.c:L102-L129](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L102-L129) 与 [src/cpu/difftest/dut.c:L54-L60](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest/dut.c#L54-L60)。
   - 用自己的话写一段说明：当 QEMU 一次单步执行了多条客机指令时，`difftest_skip_dut(nr_ref, nr_dut)` 先立刻让 REF 走 `nr_ref` 步（第 57–59 行），并设 `skip_dut_nr_inst = nr_dut`；随后 DUT 每走一步，`difftest_step` 检查 REF 的 pc 是否已等于 DUT 的 `npc`（第 107 行），相等则追上、恢复 `checkregs` 比对，否则 `skip_dut_nr_inst--` 继续不比对；若减到 0 仍没追上则 `panic`。这就是「先放 REF 跑、再让 DUT 追」的对齐策略。
2. **实际编译运行**（需本地有 spike 依赖）：
   - `make menuconfig` 开启 `CONFIG_DIFFTEST`、确认 REF 选 `Spike`。
   - 先实现 [src/isa/riscv32/difftest/dut.c:L20-L22](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/difftest/dut.c#L20-L22) 的 `isa_difftest_checkregs`（用 `difftest_check_reg` 逐个比 `gpr` 与 `pc`），否则会立刻 ABORT。
   - `make run`，观察启动日志 `Differential testing: ON`、性能下降、以及（若指令实现有 bug）出错寄存器差异行。
3. **分析输出**：若跑到 `HIT GOOD TRAP`，说明你的指令实现与 spike 在每一步都一致——这是「机器永远是对的」给你的背书；若 ABORT，根据打印的 `pc`、`right/wrong/diff` 定位到具体指令，结合 u8-l25 的 itrace 在 `nemu-log.txt` 里找到出错指令的追踪记录。

**预期结果**：能清晰复述 `skip_dut_nr_inst` 的追赶机制；能在本地（若依赖就绪）看到差分测试实际运行并解读其输出。环境不满足时，步骤 1 的源码分析仍可独立完成，步骤 2–3 标注「待本地验证」。

## 6. 本讲小结

- 差分测试让 NEMU（DUT）与一个可信模拟器（REF）同跑一份程序，每条指令后比对寄存器，不一致即定位 bug；NEMU 用 `dlopen` + 函数指针把 REF 当「外挂可信计算器」，解耦且可换 REF。
- `init_difftest` 负责 `dlopen` 加载 REF `.so`、`dlsym` 取 5 个函数指针，再把镜像与寄存器同步给 REF，确保两边同起点；顺序是 init → memcpy 镜像 → regcpy 寄存器。
- `difftest_step` 是每步比对核心：正常路径 `ref_difftest_exec(1)` → 取 REF 寄存器 → `checkregs` → ISA 侧 `isa_difftest_checkregs`（riscv32 当前是待实现 stub）。
- `difftest_skip_ref` 处理「REF 无法复现的行为」（MMIO 设备副作用、`nemu_trap`、`invalid_inst`），做法是把 DUT 寄存器拷给 REF、跳过 exec 与比对；MMIO 在 `find_mapid_by_addr`、停机在 `set_nemu_state` 两处调用它。
- `difftest_skip_dut` 处理 QEMU「指令打包」（一步走多条），先让 REF 走 `nr_ref` 步，再让 DUT 在 `nr_dut` 步内靠 `ref.pc == npc` 追上，追不上则 `panic`。
- REF 侧需导出 5 个 `__EXPORT` 函数；`DIFFTEST_REG_SIZE` 必须与 `CPU_state` 二进制布局一致（riscv32 为 132 字节）；构建由 `difftest.mk` 产出 `build/<isa>-<ref>-so`，`native.mk` 自动带 `--diff=`，开关在 `Kconfig`。

## 7. 下一步学习建议

- **u8-l25 指令追踪与反汇编**：差分测试报错后，下一步是用 itrace 在 `nemu-log.txt` 里定位出错指令。两讲配合构成「差分测试发现错误 → itrace 定位错误」的完整调试闭环。
- **u7-l21 中断与异常机制**：本讲提到 `ref_difftest_raise_intr` 用于同步中断。学完 u7-l21 后可回头理解：DUT 的 `isa_raise_intr` 之后为何要调 `ref_difftest_raise_intr(NO)` 让 REF 也走一次中断响应，否则两边在中断返回后的状态会失配。
- **延伸阅读**：对照阅读 `tools/spike-diff/difftest.cc` 与 `tools/qemu-diff/src/diff-test.c`，体会「进程内库调用」与「fork + socket/GDB 协议」两种 REF 实现的取舍；进一步可研究 `difftest_attach/difftest_detach`（[include/cpu/difftest.h:L27-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/difftest.h#L27-L28)）这两个已声明但留空的接口，思考它们在「运行中动态挂接/摘除 REF」场景下的用途。
