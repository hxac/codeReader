# 启动流程——从 main 到引擎运行

## 1. 本讲目标

通过本讲，你将追踪 NEMU 从一条命令行输入，到最终进入指令执行主循环的**完整启动链路**。具体来说，学完后你应该能够：

1. 画出 `main()` → `init_monitor()` → `engine_start()` 的调用顺序，并说出每一步的职责。
2. 说清 `load_img()` 如何把一段程序字节流载入到 `RESET_VECTOR` 处，以及它和 `init_isa()` 里那段「内置镜像」谁覆盖谁。
3. 区分 **native 模式**（带交互式调试器 SDB）与 **AM 模式**（直接跑到底）在启动阶段的差异。

理解启动流程的意义在于：它是后续所有模块（SDB、CPU、内存、设备、ISA）的「汇合点」——每一个子系统的 `init_xxx()` 都在启动时被串起来。看懂这条链，你就拿到了 NEMU 全局结构的骨架。

---

## 2. 前置知识

在阅读本讲前，请确认你已理解（来自 u1-l1、u1-l2）：

- **配置项 `CONFIG_*`**：menuconfig 产生的开关，最终变成 C 宏（写进 `autoconf.h`）和 Makefile 变量（写进 `auto.conf`）。本讲会反复出现 `CONFIG_TARGET_AM`、`CONFIG_DEVICE`、`CONFIG_ITRACE` 这样的开关。
- **条件编译宏**：`IFDEF(CONFIG_X, foo)` 表示「若定义了 `CONFIG_X` 则展开为 `foo()`，否则什么都不做」。这是 NEMU 用一套源码适配多种形态的关键。
- **客机（guest）与宿主机（host）**：NEMU 模拟的是「客机」，而 NEMU 自己运行在真实的「宿主机」上。`guest_to_host()` 就是把客机物理地址翻译成宿主机进程里的虚拟地址。
- **镜像（image）**：一段可以被 CPU 直接执行的原始字节流（机器码 + 数据），相当于一台真实电脑开机时 ROM/Flash 里烧录的内容。

> 术语提示：本讲里的「引擎（engine）」指 NEMU 的执行核心。NEMU 的引擎只有一种实现——解释器（interpreter），但代码框架把它抽象成 `engine_start()`，为将来可能换上 JIT 等其他引擎预留了切换点。

---

## 3. 本讲源码地图

本讲涉及的文件都处在「启动链路」的不同位置：

| 文件 | 作用 |
| --- | --- |
| `src/nemu-main.c` | 程序入口 `main()`，决定走 native 还是 AM 路径，最后交给引擎。 |
| `src/monitor/monitor.c` | 监视器（monitor）的实现：参数解析、内存/设备/ISA/镜像/difftest/SDB 初始化，以及欢迎信息。 |
| `src/engine/interpreter/init.c` | 引擎入口 `engine_start()`：native 模式进 SDB 交互循环，AM 模式直接执行到底。 |
| `src/isa/riscv32/init.c` | `init_isa()` 的 riscv32 实现：烧录内置镜像、设置初始 PC（`restart()`）。 |
| `include/memory/paddr.h` | `RESET_VECTOR` 等地址常量的定义。 |
| `src/utils/state.c` | 全局状态 `nemu_state` 与退出判定 `is_exit_status_bad()`。 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：入口 `main`、初始化链 `init_monitor`、镜像加载 `load_img`、引擎分发 `engine_start`。

### 4.1 程序入口 main：两条启动路径

#### 4.1.1 概念说明

任何 C 程序都从 `main()` 开始，NEMU 也不例外。但 NEMU 有两种截然不同的「运行身份」：

- **native 模式**：NEMU 作为一个普通的 Linux 命令行程序运行，带一个交互式简单调试器 SDB，用户可以单步、查看寄存器。这是你做 PA 作业时最常用的形态。
- **AM 模式**：NEMU 被编译成 AM（Abstract Machine）抽象机的一个目标，没有交互界面，开机就直接把镜像跑到结束。这种形态用于把 NEMU 当作底层参考实现。

`main()` 的职责非常薄：根据是否定义了 `CONFIG_TARGET_AM`，选择对应的初始化入口，然后统一交给 `engine_start()`。

#### 4.1.2 核心流程

```text
程序启动
   │
   ├── 定义了 CONFIG_TARGET_AM ?
   │       是 → am_init_monitor()        （AM 路径，参数硬编码）
   │       否 → init_monitor(argc, argv) （native 路径，解析命令行）
   │
   ├── engine_start()                    （进入执行引擎）
   │
   └── return is_exit_status_bad()       （把退出状态映射成进程返回码）
```

注意最后一步：`main` 的返回值不是固定的 0，而是 `is_exit_status_bad()` 的结果。这是 NEMU 判断「本次运行是否成功」的地方（见模块 4.4 末尾）。

#### 4.1.3 源码精读

整个入口非常短：

[src/nemu-main.c:23-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/nemu-main.c#L23-L35) —— NEMU 的 `main` 函数，用 `#ifdef CONFIG_TARGET_AM` 在两条初始化路径间二选一，随后统一调用 `engine_start()`，并以 `is_exit_status_bad()` 作为进程返回值。

```c
int main(int argc, char *argv[]) {
  /* Initialize the monitor. */
#ifdef CONFIG_TARGET_AM
  am_init_monitor();
#else
  init_monitor(argc, argv);
#endif

  /* Start engine. */
  engine_start();

  return is_exit_status_bad();
}
```

`am_init_monitor()` 和 `init_monitor()` 都定义在 `monitor.c` 里，用一个 `#else` 分隔成两套实现（详见模块 4.2 和 4.4）。

#### 4.1.4 代码实践

1. **目标**：确认你本地的 NEMU 默认编译成哪条路径。
2. **操作步骤**：
   - 打开 `include/generated/autoconf.h`（需要先 `make menuconfig` 并编译过一次才会生成）。
   - 用搜索功能查找 `CONFIG_TARGET_AM`。
3. **需要观察的现象**：
   - 若 `autoconf.h` 里**没有** `#define CONFIG_TARGET_AM`，说明当前是 native 模式，`main` 会调用 `init_monitor(argc, argv)`。
   - 若有该宏，则是 AM 模式。
4. **预期结果**：默认配置（Native ELF 目标）下，`CONFIG_TARGET_AM` 不存在，走 native 路径。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `am_init_monitor()` 不接受 `argc/argv` 参数，而 `init_monitor()` 需要？

> **答案**：AM 模式下 NEMU 不是独立命令行程序，没有「用户传参」的概念——镜像地址、日志路径等都来自 AM 构建系统；而 native 模式需要解析用户在终端敲入的 `-b`、`-l`、`-d` 等选项，所以要把 `argc/argv` 传进去。

**练习 2**：如果删掉 `main` 里的 `engine_start()`，程序会发生什么？

> **答案**：初始化完成后直接执行 `return is_exit_status_bad()`。由于此时 `nemu_state.state` 还是初始的 `NEMU_STOP`（见 `src/utils/state.c:18`），`is_exit_status_bad()` 会判定为「非正常结束」，进程返回非 0 退出码，但**不会执行任何一条客机指令**。

---

### 4.2 init_monitor 初始化链

#### 4.2.1 概念说明

`init_monitor()` 是 native 模式的初始化总指挥。它把所有子系统的 `init_xxx()` 按固定顺序串起来。理解这条链，相当于拿到了 NEMU 的「装配清单」——以后你阅读任何一个模块（内存、设备、ISA……），都能在脑子里定位它「是在启动的哪一步被唤醒的」。

这条链有一个很重要的设计：**初始化顺序不能随便换**。例如必须先 `init_mem()`（建好物理内存）才能 `init_isa()`（把内置镜像 `memcpy` 进内存）；必须先 `load_img()`（载入真正要跑的程序）才能 `init_difftest()`（把这份镜像同步给参考实现）。

#### 4.2.2 核心流程

native 模式 `init_monitor()` 的 11 个步骤，严格按下列顺序执行：

```text
1. parse_args(argc, argv)          解析命令行 → 填充 log_file / img_file / diff_so_file 等静态变量
2. init_rand()                     设置随机种子（影响 MEM_RANDOM 等行为）
3. init_log(log_file)              打开日志文件（若指定）
4. init_mem()                      建立物理内存 pmem
5. IFDEF(CONFIG_DEVICE, init_device())   装配设备（仅当开启了设备）
6. init_isa()                       烧录内置镜像 + 设置初始 pc（依赖内存已就绪）
7. load_img() → img_size           载入用户指定的镜像（会覆盖内置镜像）；无则返回 4096
8. init_difftest(diff_so_file, img_size, difftest_port)   差分测试初始化
9. init_sdb()                       初始化简单调试器命令表/词法分析
10. IFDEF(CONFIG_ITRACE, init_disasm())   初始化反汇编（仅当开启了指令追踪）
11. welcome()                       打印欢迎信息（⚠ 当前这里有一个 assert(0)）
```

> 关键细节：第 6 步 `init_isa()` 已经把一段「内置镜像」写进了 `RESET_VECTOR`；第 7 步 `load_img()` 若拿到了真实的镜像文件，会用 `fread` **覆盖**掉它。所以「内置镜像」是一种兜底程序——当你不提供任何镜像时，NEMU 仍有一段可执行的最小程序（最后一条是 `ebreak`，作为 NEMU trap）。

#### 4.2.3 源码精读

`init_monitor()` 本体：每一行注释都对应一个子系统初始化：

[src/monitor/monitor.c:101-135](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L101-L135) —— native 模式的初始化总链：依次完成参数解析、随机种子、日志、内存、设备、ISA、镜像、差分测试、SDB、反汇编、欢迎信息。

```c
void init_monitor(int argc, char *argv[]) {
  parse_args(argc, argv);
  init_rand();
  init_log(log_file);
  init_mem();
  IFDEF(CONFIG_DEVICE, init_device());
  init_isa();
  long img_size = load_img();
  init_difftest(diff_so_file, img_size, difftest_port);
  init_sdb();
  IFDEF(CONFIG_ITRACE, init_disasm());
  welcome();
}
```

参数解析 `parse_args()` 用的是标准 `getopt_long`，定义了 5 个选项：

[src/monitor/monitor.c:71-99](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L71-L99) —— `parse_args` 用 `getopt_long` 解析 `-b/-l/-d/-p/-h` 以及一个位置参数（镜像文件名）。`case 1` 表示「不带 `-` 的位置参数」即镜像文件。

```c
static int parse_args(int argc, char *argv[]) {
  const struct option table[] = {
    {"batch", no_argument, NULL, 'b'},
    {"log",   required_argument, NULL, 'l'},
    {"diff",  required_argument, NULL, 'd'},
    {"port",  required_argument, NULL, 'p'},
    {"help",  no_argument, NULL, 'h'},
    {0, 0, NULL, 0},
  };
  int o;
  while ((o = getopt_long(argc, argv, "-bhl:d:p:", table, NULL)) != -1) {
    switch (o) {
      case 'b': sdb_set_batch_mode(); break;
      case 'p': sscanf(optarg, "%d", &difftest_port); break;
      case 'l': log_file = optarg; break;
      case 'd': diff_so_file = optarg; break;
      case 1: img_file = optarg; return 0;   // 位置参数 = 镜像文件
      default: /* 打印 usage 并 exit(0) */
    }
  }
  return 0;
}
```

注意 `getopt_long` 第三个参数字符串 `"-bhl:d:p:"` 开头的 `-`：它让任何「不以 `-` 开头的参数」（即位置参数）被当作返回值 `1` 处理，于是镜像文件名被捕获到 `img_file`。

最后一个步骤 `welcome()` 目前包含一个**故意为之**的 `assert(0)`：

[src/monitor/monitor.c:27-37](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L27-L37) —— 欢迎信息函数。最后一行 `assert(0)` 在 debug 构建下永远失败，会立即中止程序；这是 PA 给新手的「第一道门」：你必须删掉它、重新编译，NEMU 才能真正跑起来。

```c
static void welcome() {
  Log("Trace: %s", MUXDEF(CONFIG_TRACE, ...ON..., ...OFF...));
  Log("Build time: %s, %s", __TIME__, __DATE__);
  printf("Welcome to %s-NEMU!\n", ANSI_FMT(str(__GUEST_ISA__), ...));
  printf("For help, type \"help\"\n");
  Log("Exercise: Please remove me in the source code and compile NEMU again.");
  assert(0);   // ← 就在这里
}
```

> 这个 `assert(0)` 是教学设计：它逼着每一位学生在继续之前，亲手完成一次「改源码 → 重新编译 → 运行」的完整闭环，确保编译环境是通的。本讲的代码实践（第 5 节）就是完成它。

#### 4.2.4 代码实践

1. **目标**：在不真正启动 NEMU 的情况下，画出 `init_monitor` 各步骤的依赖关系。
2. **操作步骤**：
   - 在 `monitor.c` 里找到 4 个 `static` 变量：`log_file`、`diff_so_file`、`img_file`、`difftest_port`（[monitor.c:44-47](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L44-L47)）。
   - 追踪 `parse_args` 的每个 `case` 分别改了哪个变量，以及这些变量又被第几步用到。
3. **需要观察的现象**：`img_file` 同时被 `parse_args`（写入）和 `load_img`（读取）使用；`diff_so_file` 被 `parse_args` 写入、`init_difftest` 读取。
4. **预期结果**：你会得到一张「写者 → 读者」的数据流图，证明参数解析必须排在所有用得到它的初始化之前。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `init_mem()` 和 `init_isa()` 调换顺序，会发生什么？

> **答案**：`init_isa()` 内部会执行 `memcpy(guest_to_host(RESET_VECTOR), img, ...)`（见 4.3.3），而 `guest_to_host` 依赖物理内存 `pmem` 已经分配。若先 `init_isa()`，此时 `pmem` 尚未建立，会把内置镜像写进非法地址，导致段错误或未定义行为。

**练习 2**：`welcome()` 里那行 `Log("Trace: %s", MUXDEF(CONFIG_TRACE, ...))` 是怎么根据配置显示 ON/OFF 的？

> **答案**：`MUXDEF(CONFIG_TRACE, A, B)` 是宏：若定义了 `CONFIG_TRACE` 就展开成 `A`（绿色的 `"ON"`），否则展开成 `B`（红色的 `"OFF"`）。所以这行在编译期就决定了显示内容，运行时没有 if 判断。

---

### 4.3 镜像加载：load_img 与 RESET_VECTOR

#### 4.3.1 概念说明

启动的核心动作之一，是把「要执行的程序」放进内存里 CPU 能取到的位置。这个位置叫 **RESET_VECTOR（复位向量）**——就像真实 CPU 上电后第一条指令所在的地址。

NEMU 有两层镜像：

1. **内置镜像**（`init_isa()` 里那段 `img[]`）：一段 5 条指令的极简程序，最后一条是 `ebreak`（被 NEMU 当作 trap，程序结束信号）。
2. **用户镜像**（`load_img()` 从文件读）：你真正想跑的程序，如一个 AM 应用或裸机程序。

两者关系是「**先内后外、后者覆盖**」：`init_isa()` 先把内置镜像放好并设置好初始 PC；`load_img()` 若拿到了文件，再用 `fread` 把内容覆盖到同一个 `RESET_VECTOR` 起始的位置。

#### 4.3.2 核心流程

`RESET_VECTOR` 是一个由三个配置项算出来的地址：

\[ \texttt{RESET\_VECTOR} = \texttt{PMEM\_LEFT} + \texttt{CONFIG\_PC\_RESET\_OFFSET} = \texttt{CONFIG\_MBASE} + \texttt{CONFIG\_PC\_RESET\_OFFSET} \]

其中 `CONFIG_MBASE` 是物理内存的起始地址（客机视角），`CONFIG_PC_RESET_OFFSET` 是 PC 相对内存起始的偏移。对 riscv32 默认配置，二者通常都是 0，故 `RESET_VECTOR = 0x80000000`（具体值取决于 Kconfig，此处为典型值，**待本地确认**）。

```text
init_isa():
   memcpy(guest_to_host(RESET_VECTOR), img, sizeof(img))   写入内置镜像
   restart(): cpu.pc = RESET_VECTOR; cpu.gpr[0] = 0        设置初始 PC

load_img():
   if (img_file == NULL) return 4096;                       无文件 → 保留内置镜像
   fread(guest_to_host(RESET_VECTOR), size, 1, fp)          有文件 → 覆盖
   return size
```

#### 4.3.3 源码精读

地址常量定义在 `paddr.h`：

[include/memory/paddr.h:21-26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h#L21-L26) —— `PMEM_LEFT/PMEM_RIGHT/RESET_VECTOR` 三个地址常量，以及 `guest_to_host` 的声明。`guest_to_host(paddr)` 把「客机物理地址」转成「宿主机进程里的可读写指针」。

```c
#define PMEM_LEFT  ((paddr_t)CONFIG_MBASE)
#define PMEM_RIGHT ((paddr_t)CONFIG_MBASE + CONFIG_MSIZE - 1)
#define RESET_VECTOR (PMEM_LEFT + CONFIG_PC_RESET_OFFSET)

uint8_t* guest_to_host(paddr_t paddr);
```

内置镜像与 `restart()`（以 riscv32 为例）：

[src/isa/riscv32/init.c:21-43](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L43) —— 内置镜像 `img[]` 共 5 个字（auipc / sb / lbu / ebreak / 数据），`restart()` 把 PC 设到 `RESET_VECTOR` 并把通用寄存器 0 清零；`init_isa()` 先写镜像再调 `restart()`。

```c
static const uint32_t img[] = {
  0x00000297,  // auipc t0,0
  0x00028823,  // sb  zero,16(t0)
  0x0102c503,  // lbu a0,16(t0)
  0x00100073,  // ebreak (used as nemu_trap)
  0xdeadbeef,  // some data
};

static void restart() {
  cpu.pc = RESET_VECTOR;     // 初始 PC
  cpu.gpr[0] = 0;            // x0 恒为 0
}

void init_isa() {
  memcpy(guest_to_host(RESET_VECTOR), img, sizeof(img));   // 烧录内置镜像
  restart();                                                // 初始化系统状态
}
```

> 内置镜像的逻辑很巧妙：`auipc` 算出当前 PC，`sb` 把 0 写到 `PC+16` 处（正好覆盖 `0xdeadbeef` 那个字），`lbu` 再把它读进 `a0`，最后 `ebreak` 触发 trap 并把 `a0` 当作返回值。这段小程序用于验证「内存读写 + 寄存器 + trap」最小通路是否打通。

native 模式的 `load_img()`：

[src/monitor/monitor.c:49-69](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L49-L69) —— `load_img`：无文件时返回 4096（沿用内置镜像的字节大小占位）；有文件时用 `fseek/ftell` 测出大小，再 `fread` 一次性读入到 `guest_to_host(RESET_VECTOR)`。

```c
static long load_img() {
  if (img_file == NULL) {
    Log("No image is given. Use the default build-in image.");
    return 4096; // built-in image size
  }
  FILE *fp = fopen(img_file, "rb");
  Assert(fp, "Can not open '%s'", img_file);
  fseek(fp, 0, SEEK_END);
  long size = ftell(fp);
  fseek(fp, 0, SEEK_SET);
  int ret = fread(guest_to_host(RESET_VECTOR), size, 1, fp);
  assert(ret == 1);
  fclose(fp);
  return size;
}
```

注意 `return 4096` 这个「魔数」：当用户不提供镜像时，返回值 4096 会被传给 `init_difftest()`，告诉差分测试「内置镜像占多大」。它和上面 `img[]` 实际只有 20 字节并不矛盾——4096 只是给 difftest 同步的一个保守上限。

#### 4.3.4 代码实践

1. **目标**：观察「内置镜像」被覆盖的过程。
2. **操作步骤**：
   - 准备一个最小的镜像文件（如果你已有 AM 编译出的 bin，可直接用；否则可暂用任意小文件作为占位，仅观察日志）。
   - 运行 `./build/riscv32-nemu-interpreter 你的镜像.bin`（路径与二进制名以你本地 `CONFIG_GUEST_ISA` / `CONFIG_ENGINE` 为准，**待本地确认**）。
3. **需要观察的现象**：
   - 不带镜像参数时，日志里出现 `No image is given. Use the default build-in image.`
   - 带镜像参数时，日志里出现 `The image is <文件名>, size = <大小>`。
4. **预期结果**：两种情况下 NEMU 都应成功进入后续阶段（在删除 `assert(0)` 之后），证明镜像加载通路正常。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `restart()` 里要把 `cpu.gpr[0] = 0` 单独写一遍？x86 也会这么做吗？

> **答案**：RISC-V 的 `x0` 寄存器硬连线为常量 0，软件写入无效。NEMU 用普通数组模拟寄存器，没法做到「硬件级只读」，所以每次重启都要手动把它清零，防止上一轮运行残留了非 0 值。x86 没有这样的恒 0 寄存器，对应的 `init_isa()` 里不需要这一步（可对照 `src/isa/x86/init.c`）。

**练习 2**：`load_img` 用 `fseek(fp,0,SEEK_END); ftell(fp);` 来量文件大小，这种写法在什么情况下会出错？

> **答案**：`ftell` 对普通二进制文件返回当前文件位置的字节偏移，在 Linux 上量大小没问题；但对非常规文件（如管道、设备节点、超过 `long` 范围的超大文件）会返回 -1 或不可靠的值。NEMU 假设镜像是普通磁盘文件，所以做了简化。

---

### 4.4 engine_start 分发：native 与 AM 的最终分野

#### 4.4.1 概念说明

初始化全部完成后，`main()` 调用 `engine_start()`。这是启动链的终点，也是「执行阶段」的起点。和 `init_monitor` 一样，它也用 `CONFIG_TARGET_AM` 分成两条路：

- **native 模式**：调用 `sdb_mainloop()`，进入简单调试器 SDB 的命令循环——打印提示符、读命令、分发执行（如 `c` 连续运行、`si` 单步、`q` 退出）。CPU 的实际执行由 SDB 命令间接触发。
- **AM 模式**：直接调用 `cpu_exec(-1)`，参数 `-1` 表示「一直跑到 trap/退出为止」，没有任何交互。

也就是说，两种模式的「初始化」差异不大（都建内存、载镜像、设 PC），真正的区别在于**谁驱动 CPU 执行**：native 是人在终端一条条命令地驱动，AM 是一口气跑到底。

#### 4.4.2 核心流程

```text
engine_start()
   ├── 定义了 CONFIG_TARGET_AM ?
   │     是 → cpu_exec(-1)      一口气跑到 trap（无交互）
   │     否 → sdb_mainloop()    SDB 命令循环（交互式）
   └── （当 SDB 收到 q 或 trap 触发，循环返回）

随后回到 main:
   return is_exit_status_bad()
```

退出判定 `is_exit_status_bad()` 把全局状态 `nemu_state` 映射成进程返回码：

\[ \text{good} = (\text{state} == \text{NEMU\_END} \land \text{halt\_ret} == 0) \;\lor\; (\text{state} == \text{NEMU\_QUIT}) \]

只有 `good` 为真时进程返回 0（成功），其余都返回非 0。

#### 4.4.3 源码精读

`engine_start()` 极简：

[src/engine/interpreter/init.c:20-27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/engine/interpreter/init.c#L20-L27) —— 引擎入口。native 模式交给 SDB 主循环 `sdb_mainloop()`；AM 模式直接 `cpu_exec(-1)` 跑到底。

```c
void engine_start() {
#ifdef CONFIG_TARGET_AM
  cpu_exec(-1);
#else
  /* Receive commands from user. */
  sdb_mainloop();
#endif
}
```

退出状态判定在 `state.c`：

[src/utils/state.c:18-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/state.c#L18-L24) —— 全局状态变量初始为 `NEMU_STOP`；`is_exit_status_bad()` 当「正常结束且返回 0」或「用户主动退出」时返回 0（成功），否则返回非 0。

```c
NEMUState nemu_state = { .state = NEMU_STOP };

int is_exit_status_bad() {
  int good = (nemu_state.state == NEMU_END && nemu_state.halt_ret == 0) ||
             (nemu_state.state == NEMU_QUIT);
  return !good;
}
```

作为对比，AM 模式的初始化入口 `am_init_monitor()` 也定义在 `monitor.c` 的 `#else` 分支里：

[src/monitor/monitor.c:145-152](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L145-L152) —— AM 模式的 `am_init_monitor()`：省去了参数解析、日志、difftest、SDB、disasm，只做 rand → mem → isa → 内置镜像 → 设备 → welcome。

```c
void am_init_monitor() {
  init_rand();
  init_mem();
  init_isa();
  load_img();
  IFDEF(CONFIG_DEVICE, init_device());
  welcome();
}
```

> 对照 4.2 的 native 版本，你能清楚看到 AM 版本的「瘦身」：没有命令行、没有日志文件、没有 difftest、没有 SDB、没有反汇编——因为 AM 模式下 NEMU 不与人交互，只是被 AM 当作一台「会跑程序的裸机」。

#### 4.4.4 代码实践

1. **目标**：观察 native 模式下 `engine_start` 如何进入交互式 SDB。
2. **操作步骤**：在删除 `welcome()` 里的 `assert(0)` 并重新编译后（见第 5 节综合实践），运行 `make run`（或直接执行二进制）。
3. **需要观察的现象**：屏幕出现 `Welcome to riscv32-NEMU!`（ISA 名视你的配置而定）和 `(nemu)` 提示符，等待你输入命令。
4. **预期结果**：输入 `c`（continue）会运行内置镜像，很快因 `ebreak` 触发 trap 而停止；输入 `q` 退出后，shell 提示符里能看到进程返回码（成功为 0）。**完整运行结果待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：`cpu_exec(-1)` 里的 `-1` 是什么含义？为什么 AM 模式用它？

> **答案**：`cpu_exec(n)` 表示「执行 n 条指令」。`n = -1` 在位级上是一个极大的无符号数（`-1` 的补码是全 1），相当于「执行几乎无限多条」，于是会一直跑到 trap 或退出条件触发才停。AM 模式不需要单步交互，所以用这种方式「跑到底」。

**练习 2**：用户在 SDB 里敲 `q` 退出后，`nemu_state.state` 会变成什么？`is_exit_status_bad()` 会返回什么？

> **答案**：`q` 命令会把状态设为 `NEMU_QUIT`。代入 `is_exit_status_bad()`，`good` 因 `(state == NEMU_QUIT)` 而为真，返回 `!good = 0`，即进程成功退出（返回码 0）。

---

## 5. 综合实践

本实践贯穿全讲：完成 PA 约定的「第一道门」——删除 `welcome()` 里的 `assert(0)`，让 NEMU 真正跑起来，并借此走完一遍启动链。

### 实践目标

亲手打通「修改源码 → 重新编译 → 运行 → 观察输出与退出码」的完整闭环，同时验证你对 `init_monitor` 11 步初始化顺序、`load_img` 镜像加载、`engine_start` 进入 SDB 的理解。

### 操作步骤

1. **打开** `src/monitor/monitor.c`，定位到 `welcome()` 函数末尾的 `assert(0);`（约第 36 行）。
2. **删除** 这一行 `assert(0);`（保留或删除其上方那条 `Log("Exercise: ...")` 都可以，但建议一起删，保持整洁）。
3. **重新编译**：

   ```bash
   make
   ```

   产物为 `build/<ISA>-nemu-interpreter`（具体名取决于你的 menuconfig，**待本地确认**）。

4. **运行内置镜像**（不带任何镜像参数）：

   ```bash
   make run
   # 或直接： ./build/riscv32-nemu-interpreter
   ```

5. 进入 SDB 后，输入 `c` 让它跑内置镜像，再输入 `q` 退出。也可加 `-b` 直接进 batch 模式：

   ```bash
   ./build/riscv32-nemu-interpreter -b
   ```

### 需要观察的现象

- 启动时依次打印：Trace 状态（ON/OFF）、Build time、`Welcome to <isa>-NEMU!`。删除 `assert(0)` 后**不再**立即 abort。
- 由于没有传镜像文件，日志里应出现 `No image is given. Use the default build-in image.`
- 输入 `c` 后，内置镜像执行到 `ebreak` 触发 trap，程序停止。

### 预期结果

- NEMU 不再因 `assert(0)` 中止，能正常进入 SDB 提示符或 batch 运行。
- 内置镜像跑完后命中 trap，根据 `a0` 的值（这里为 0）判定为 `HIT GOOD TRAP`，进程返回码为 0。
- 若你刻意让 trap 返回值非 0（这是后续 PA 阶段的事），则会得到 `HIT BAD TRAP`，返回码非 0。

> ⚠ 完整的 trap 输出格式、`HIT GOOD/BAD TRAP` 字样在 NEMU 完成更多 PA 阶段后才会完整呈现。当前阶段只要 NEMU 不再 `assert` 失败、能进入执行循环即为成功。**具体运行输出待本地验证。**

---

## 6. 本讲小结

- NEMU 从 `main()` 出发，用 `#ifdef CONFIG_TARGET_AM` 在 **native**（`init_monitor`）与 **AM**（`am_init_monitor`）两条启动路径间二选一，最后统一调用 `engine_start()`。
- native 模式的 `init_monitor()` 是一条 11 步的初始化链：`parse_args → init_rand → init_log → init_mem → init_device → init_isa → load_img → init_difftest → init_sdb → init_disasm → welcome`，**顺序由依赖关系决定，不能随意调换**。
- 镜像加载是「**先内后外**」：`init_isa()` 先烧录内置镜像并设置 `cpu.pc = RESET_VECTOR`，`load_img()` 再用文件内容覆盖；不提供镜像时沿用内置镜像。
- `RESET_VECTOR = CONFIG_MBASE + CONFIG_PC_RESET_OFFSET`，是 CPU 上电后取第一条指令的地址。
- `engine_start()` 是启动与执行的交接点：native 走 `sdb_mainloop()`（交互），AM 走 `cpu_exec(-1)`（跑到底）。
- `main` 的返回值由 `is_exit_status_bad()` 决定，把 `nemu_state` 映射成进程退出码（正常结束且返回 0、或用户主动退出 → 0）。

---

## 7. 下一步学习建议

现在你已经看懂 NEMU 的启动骨架，接下来有两个自然方向：

1. **深入 SDB**：`engine_start()` 在 native 模式调用的 `sdb_mainloop()` 是下一单元的主角。建议先读 `src/monitor/sdb/sdb.c` 的命令表 `cmd_table` 与主循环，这是 **u2-l5（SDB 命令框架）** 的内容。
2. **深入执行引擎**：若你对 `cpu_exec(-1)` 背后的状态机驱动更感兴趣，可以直接看 `src/cpu/cpu-exec.c`——它定义了 `cpu_exec` 如何循环调用 `exec_once` 单步执行，对应 **u3-l9（CPU 执行主循环）**。

建议按手册顺序先学 **u2（监视器与 SDB）**，因为 SDB 是你在后续 PA 中调试自实现 CPU 的核心工具，越早熟练越好。
