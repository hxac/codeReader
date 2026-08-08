# 跑起来：模拟器与第一个程序

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ZipCPU 仓库里**两套模拟器**分别是什么、有什么本质区别。
- 知道 `sim/verilator` 默认会构建出哪些可执行文件（`zipsys_tb` / `zipbones_tb` / `zipaxil_tb`），以及 `make test`、`make stest`、`make itest` 各做什么。
- 能用 `zip-gcc` 把 `sim/zipsw/hello.c` 编译链接成一个 ZipCPU 的 ELF 可执行文件。
- 跟踪「`printf` 的字符是怎么发出去的」和「`main` 返回后 CPU 是怎么停下来」这两条调用链。
- 把「编译 → 加载 → 运行 → 看到 SUCCESS」这条最小闭环跑通（或至少知道每一步该观察什么）。

本讲是入门单元的最后一讲。前面三讲让你认识了项目定位、目录结构和四种顶层封装；这一讲给你一套**能在命令行里亲眼看见 CPU 动起来**的方法。

## 2. 前置知识

在开始前，先用大白话建立几个概念：

- **RTL（Register Transfer Level，寄存器传输级）**：用 Verilog 这类硬件描述语言写成的「电路本身」。ZipCPU 的 `rtl/` 目录里就是 RTL。
- **模拟器（Simulator）**：一段普通程序，用来「假装自己是一块芯片」，让你的程序在还没烧进 FPGA 之前就能先跑起来、看结果。
- **ELF 文件**：Linux 下 `gcc` 默认产出的可执行文件格式。ZipCPU 的交叉工具链（`zip-gcc`）也产出 ELF，只不过里面的机器码是 ZipCPU 指令，而不是 x86/ARM 指令。
- **测试台（Testbench）**：在仿真里驱动被测电路的一段外壳程序——给它喂时钟、喂输入、收集输出。本讲的 `zipcpu_tb.cpp` 就是一个测试台。
- **交叉编译（Cross Compile）**：在你的 PC 上编译出**给别人（ZipCPU）**运行的程序。`zip-gcc` 就是那把「为 ZipCPU 造机器码」的编译器。

本讲建立在 [u1-l2（构建系统）](u1-l2-repo-layout-and-build.md) 和 [u1-l3（四种顶层封装）](u1-l3-rtl-top-wrappers.md) 之上：你需要知道 `make rtl` 会在 `rtl/obj_dir/` 生成 Verilator 模型（`Vzipsystem__ALL.a` 等），而本讲的模拟器正是去链接这些模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `sim/verilator/Makefile` | 构建 Verilator 模拟器的总入口，定义 `zipsys_tb`/`zipbones_tb`/`zipaxil_tb` 与 `test`/`stest`/`itest` 等目标 |
| `sim/verilator/zipcpu_tb.cpp` | 包裹 `zipsystem`（或 `zipbones`）RTL 的 C++ 测试台：模拟内存、加载 ELF、驱动时钟、判定成功/失败 |
| `sim/zipsw/hello.c` | 最简单的「Hello, World」C 源程序 |
| `sim/zipsw/Makefile` | 用 `zip-gcc` 把 `hello.c` 等编译成 ELF 的规则集 |
| `sim/zipsw/board.ld` | 链接脚本，决定程序各段被放到哪些内存地址 |
| `sim/zipsw/zlib/crt0.c` | C 运行时启动代码（`_start`）与停机代码（`_hw_shutdown`） |
| `sim/zipsw/zlib/syscalls.c` | C 库的底层接口：`_write_r`（输出）、`_exit`（退出）等 |
| `sim/cpp/zsim.cpp` | 另一套**独立的** C++ 指令级模拟器（ISS），不依赖 RTL |

## 4. 核心概念与源码讲解

### 4.1 两套模拟器：C++ ISS 与 Verilator RTL 模拟器

#### 4.1.1 概念说明

ZipCPU 提供了两套截然不同的模拟器，理解它们的差别是本讲最重要的一件事：

- **C++ 指令级模拟器（ISS，Instruction Set Simulator）**：位于 `sim/cpp/`，主程序是 `zsim`。它**完全不碰任何 Verilog/RTL**，而是用 C++ 重新实现了「取一条指令 → 译码 → 执行 → 更新 PC 与条件码」这个过程。它快、轻，但只是「语义级」的近似。
- **Verilator RTL 模拟器**：位于 `sim/verilator/`，主程序是 `zipsys_tb` / `zipbones_tb` / `zipaxil_tb`。它把 `rtl/` 里真正的 Verilog 代码（`zipsystem.v`、`zipbones.v`、`zipaxil.v`）用 Verilator 翻译成 C++ 模型，再用一个 C++ 测试台（`zipcpu_tb.cpp`）去驱动。它慢，但仿真的是**真正的硬件电路**，RTL 有 bug 它就能抓到。

一句话总结：**ISS 仿真「指令的语义」，Verilator 仿真「电路本身」**。前者用来快速跑程序、调试软件；后者用来验证硬件设计是否正确。

#### 4.1.2 核心流程

**C++ ISS（`zsim`）的工作方式**：

1. `main` 读取命令行传入的 ELF 文件名；
2. 用 `elfread` 把 ELF 的各个段加载到自己模拟的总线（`SIMBUS`）上；
3. 把入口地址写入虚拟 PC；
4. 进入主循环：`取指(bus->lx(pc)) → execute(insn) → 判停`，逐条解释指令。

**Verilator 模拟器（`zipsys_tb`）的工作方式**：

1. `main` 读取 ELF，把各段加载到测试台内部的一块内存模型（`MEMSIM`）里；
2. 通过**调试端口**（debug port）把 CPU 复位、设置 PC、再放它跑；
3. 每个 `tick()` 推进一个时钟节拍，测试台同时充当「挂在 Wishbone 总线上的一个从设备（RAM）」；
4. 循环判定 `test_success()` / `test_failure()`，命中就退出并打印结论。

两者的关键差别在于：ISS 自己造了一套设备（UART、RAM、ROM、SDRAM）；而 Verilator 测试台**只模拟了一块 RAM**，其余行为靠 CPU 内部和「仿真专用指令」来表达（见 4.4）。

#### 4.1.3 源码精读

C++ ISS 在 `main` 里挂接了自己的设备总线——可以看到它挂了一个 UART、一块 BlockRAM、一块 Flash、一块 SDRAM：

[sim/cpp/zsim.cpp:1040-1047](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1040-L1047) —— 这里 `bus->add(...)` 逐个登记设备：UART 在 `0x150`，SDRAM 在 `0x10000000`。这是 ISS **自带设备模型**的铁证。

而它的 UART 设备把「写数据口」直接变成往屏幕打印一个字符：

[sim/cpp/zsim.cpp:130-139](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L130-L139) —— `sw()` 在偏移 12 处调用 `putchar(vl & 0x0ff)`。所以 ISS 里的「串口输出」就是把字节打到你的终端上。

它的主循环就是朴素的「取指—执行」：

[sim/cpp/zsim.cpp:1072-1098](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/cpp/zsim.cpp#L1072-L1098) —— `insn = bus->lx(zipm->pc())` 取指，`zipm->execute(insn)` 执行，命中 `halted()` 就打印 `CPU HALT` 退出。

再看 Verilator 测试台：它只认一块 RAM，RAM 的基址写死在常量里：

[sim/verilator/zipcpu_tb.cpp:205-208](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L205-L208) —— `LGRAMLEN = 28`，`RAMBASE = 1<<28 = 0x10000000`。也就是说，ELF 必须被链接/加载到 `0x10000000` 才能被这个测试台接受。

当 CPU 访问的地址不落在 RAM 范围时，测试台会回一个总线错误并标记「炸弹」：

[sim/verilator/zipcpu_tb.cpp:1320-1337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1320-L1337) —— 注意 `m_bomb = (m_tickcount > 20)`：一旦程序访问了不存在的地址，测试台会在几拍之后判定失败。这解释了为什么「程序访问的外设地址必须和测试台模型对得上」。

#### 4.1.4 代码实践

1. **实践目标**：用源码对照的方式，亲手确认两套模拟器的设备差异。
2. **操作步骤**：
   - 打开 `sim/cpp/zsim.cpp` 第 1040–1047 行，数一下 ISS 登记了几个设备、各自的地址。
   - 打开 `sim/verilator/zipcpu_tb.cpp` 第 205–208 行与 1320–1337 行，确认 Verilator 测试台**只有一块 RAM**，且访问错误地址会触发 `m_bomb`。
3. **需要观察的现象**：ISS 自带 UART 等设备；Verilator 测试台没有 UART 设备模型。
4. **预期结果**：你应当得出结论——在 Verilator 模拟器里，「程序往物理 UART 写字符」未必能直接看到输出，而要看程序用的是哪种输出机制（见 4.4）。
5. 本实践为源码阅读型，**无需运行**。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 ISS「快但不够真」，Verilator 模拟器「真但慢」？
> **答案**：ISS 用 C++ 直接计算指令结果，省去了时钟、流水线、总线握手等细节，所以快；但也因此无法暴露 RTL 的时序与电路 bug。Verilator 模拟的是真正的 Verilog 电路，每个时钟节拍都要算，所以慢，但能抓到硬件设计错误。

**练习 2**：ISS 的 UART 写数据口（偏移 12）对应的 C 函数是什么？
> **答案**：`UARTDEV::sw()`，它在偏移 12 处调用 `putchar(vl & 0x0ff)` 把字节打到终端。

---

### 4.2 sim/verilator 的构建目标与测试台主流程

#### 4.2.1 概念说明

`sim/verilator/Makefile` 是本讲的核心入口。它的默认 `all` 目标会同时构建多个可执行文件，对应 [u1-l3](u1-l3-rtl-top-wrappers.md) 讲过的三种顶层封装：

- `zipsys_tb`：包裹 **Zipsystem**（带片内外设）的测试台。
- `zipbones_tb`：包裹 **Zipbones**（精简 Wishbone）的测试台。
- `zipaxil_tb`：包裹 **Zipaxil**（AXI4-Lite）的测试台。
- 此外还有组件级测试台 `div_tb`（除法器）、`mpy_tb`（乘法）、`pfcache_tb`（指令缓存）和工具 `mkhex`、`pdump`。

一个关键细节：`zipsys_tb` 和 `zipbones_tb` **共享同一份源码** `zipcpu_tb.cpp`，靠编译期宏 `ZIPBONES` 来切换。这正呼应了 u1-l3「同一个 `zipcore`、不同外壳」的思想。

#### 4.2.2 核心流程

构建与运行的关系如下：

```
rtl/obj_dir/Vzipsystem__ALL.a   (make rtl 产物，Verilator 模型)
                │
                ▼
   zipsys_tb = zipcpu_tb.cpp + 模型 + ncurses + libelf   (本目录 make 产物)
                │
                ▼
   ./zipsys_tb [-a|-s] <elf>            (加载 ELF 运行)
```

测试台 `main()` 有三种运行模式（靠命令行开关选择）：

- `-a`（autorun）：自动跑到结束，无需交互。
- `-s`（autostep）：用「单步调试端口」一条一条地推进 CPU。
- 不加开关：进入 ncurses 全屏交互界面，可以手动单步、复位、改寄存器。

三种模式都遵循同一条「加载 → 复位 → 设 PC → 放行 → 判停」的主线。

#### 4.2.3 源码精读

默认构建目标列出了全部产物：

[sim/verilator/Makefile:122](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L122) —— `all:` 一次性构建 `mkhex pdump div_tb mpy_tb pfcache_tb zipsys_tb zipbones_tb zipaxil_tb`。

`zipbones_tb` 与 `zipsys_tb` 同源、只差一个宏：

[sim/verilator/Makefile:186-188](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L186-L188) —— 编译 `zipbones_tb.o` 时多加了 `-DZIPBONES`。于是在 `zipcpu_tb.cpp` 里，`#ifdef ZIPBONES` 就会选择 `Vzipbones` 模型、否则选择 `Vzipsystem`。

测试台里 `autorun` 模式的主循环，把「复位 → 设 PC → 放行 → 跑到成功或失败」讲得非常清楚：

[sim/verilator/zipcpu_tb.cpp:2280-2304](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2280-L2304) —— 先 `CMD_HALT|CMD_RESET|CMD_CATCH` 复位并暂停，再把入口地址写入 `CPU_sPC`，等复位释放后写 `CMD_GO` 放 CPU 跑，然后循环 `tick()` 直到 `test_success()` 或 `test_failure()`。

加载 ELF 时，测试台会断言段地址落在 RAM 范围内：

[sim/verilator/zipcpu_tb.cpp:2251-2267](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2251-L2267) —— `elfread(...)` 读出各段，`assert(secp->m_start >= RAMBASE)` 等三处断言保证程序确实被放在 `0x10000000` 起的 RAM 里，然后 `tb->m_mem.load(...)` 装入。

最后，退出结论由这几行决定（`HALT` = 成功，`BUSY`/炸弹 = 失败）：

[sim/verilator/zipcpu_tb.cpp:2526-2536](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L2526-L2536) —— `m_bomb` 真 → `TEST BOMBED`；`test_success()` 真 → `SUCCESS!`；`test_failure()` 真 → `TEST FAILED!`；否则 `User quit`。

#### 4.2.4 代码实践

1. **实践目标**：构建 Verilator 模拟器并跑一次仓库自带的 CPU 测试，亲眼看到 `SUCCESS!`。
2. **操作步骤**（依赖：先按 `INSTALL.md` 装好 Verilator，并已完成 `make rtl`）：
   ```bash
   # 1) 先在 bench/asm 里造出默认测试程序 simtest（Makefile 的 test 目标依赖它）
   make -C bench/asm test

   # 2) 构建并运行模拟器全套测试（zipsys/zipbones/zipaxil 的 atest+stest）
   make -C sim/verilator test

   # 只想最快验证一次：
   make -C sim/verilator stest     # = ./zipsys_tb -s ../../bench/asm/simtest
   ```
3. **需要观察的现象**：程序末尾会打印 `SUCCESS!`（或失败时 `TEST BOMBED` / `TEST FAILED!`），并附带 `Clocks used`、`Instructions Issued`、`Instructions / Clock` 等统计。
4. **预期结果**：在 RTL 与工具链都正常时，`simtest` 应以 `SUCCESS!` 结束——这正是 Makefile 注释里说的「HALT 表示成功，BUSY 表示失败」。
5. 若你的环境尚未装好 Verilator 或工具链，命令会失败——这属正常，标注「待本地验证」，先理解流程即可。

#### 4.2.5 小练习与答案

**练习 1**：`zipsys_tb` 和 `zipbones_tb` 为什么能用同一个 `zipcpu_tb.cpp`？
> **答案**：因为源码里用 `#ifdef ZIPBONES ... #else ... #endif` 区分；编译 `zipbones_tb` 时加 `-DZIPBONES`，于是选中 `Vzipbones` 模型，否则选中 `Vzipsystem`。

**练习 2**：`make stest`、`make itest`、`make atest` 三者有什么不同？
> **答案**：分别用 `-s`（单步推进）、无开关（ncurses 交互）、`-a`（全自动）三种方式运行**同一个**测试程序 `bench/asm/simtest`。`make test` 会把 zipsys/zipbones/zipaxil 三种外壳的 atest+stest 都跑一遍。

---

### 4.3 sim/zipsw 的 hello 程序：从源码到 ELF

#### 4.3.1 概念说明

`sim/zipsw/` 是「跑在 ZipCPU 上的示例软件」集合。`hello.c` 是其中最简单的一个——就是 `printf("Hello, World!\n")`。但它能编译成功，背后依赖三件事：

1. **交叉工具链** `zip-gcc` / `zip-as` / `zip-ld`（来自 `sw/`，见 [u1-l2](u1-l2-repo-layout-and-build.md)）。
2. **链接脚本** `board.ld`：决定代码段、数据段、栈各放在哪个地址。
3. **C 运行时库胶水** `zlib/`（`crt0.c` 的 `_start`、`syscalls.c` 的系统调用桩），被打包成 `libziptb.a`。

和 PC 上 `gcc hello.c` 不同的是：这里没有操作系统，`printf` 最终会走到我们自己写的 `_write_r`，而 `main` 返回后也不会「回到 shell」，而是去停机（见 4.4）。

#### 4.3.2 核心流程

`make hello` 的编译链接流程：

```
hello.c ──zip-gcc -O3 -c──► obj-zip/hello.o
                                   │
                                   ▼  zip-gcc -T board.ld ... -lc -lziptb -lgcc
                                 hello   (ZipCPU ELF 可执行文件)
```

运行时（被模拟器加载后）的启动流程：

```
复位 → _start(crt0.s 汇编) → 清寄存器/设栈/清cache → main() → 返回 → exit() → 停机
```

#### 4.3.3 源码精读

`hello.c` 本身朴素到只有一行有效代码：

[sim/zipsw/hello.c:22-24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/hello.c#L22-L24) —— `int main(...){ printf("Hello, World!\n"); }`。注意它**没有显式 `return`**，按 C 标准等价于 `return 0`，这一点对后面的「成功停机」很关键。

`Makefile` 用 `zip-gcc` 编译并链接：

[sim/zipsw/Makefile:123-124](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/Makefile#L123-L124) —— `hello` 目标先把 `hello.c` 编成 `.o`，再用 `-T board.ld ... -lc -lziptb -lgcc` 链接成 ELF。这里 `-lziptb` 就是 `zlib/` 里那些胶水代码。

链接脚本把整段程序放进一块叫 `bkram` 的内存：

[sim/zipsw/board.ld:50](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L50) —— `bkram(wx) : ORIGIN = 0x04000000, LENGTH = 0x04000000`。链接脚本里写的 ORIGIN 决定了 ELF 各段的物理地址。

> ⚠️ **注意一个易踩的坑**：`board.ld`/`board.h` 是与具体「板子配置」绑定的文件。仓库里这份把 RAM 放在 `0x04000000`；而 Verilator 测试台的 `RAMBASE` 是 `0x10000000`（见 4.1.3）。要让 `hello` 在 `zipsys_tb` 里跑起来，链接地址必须和测试台的 RAM 范围对齐，否则 4.2.3 里的断言 `secp->m_start >= RAMBASE` 会失败。实际工程中会按目标重新生成 `board.ld`/`board.h`。

`_start` 启动代码（在 `crt0.c` 里以内嵌汇编写成）展示了 ZipCPU 上电后的标准动作：

[sim/zipsw/zlib/crt0.c:165-228](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/zlib/crt0.c#L165-L228) —— 它清零所有寄存器、把栈指针设为 `_top_of_stack`、清缓存，然后 `JSR main`。`main` 返回后落到 `_graceful_kernel_exit`，调用 `exit`，再到 `_hw_shutdown`。

#### 4.3.4 代码实践

1. **实践目标**：亲手把 `hello.c` 编译成 ZipCPU 的 ELF，并检查它的段地址。
2. **操作步骤**（依赖：`zip-gcc` 已在 `PATH` 中）：
   ```bash
   cd sim/zipsw
   make hello                 # 产出 ./hello（ZipCPU ELF）
   zip-readelf -a hello | head -40     # 查看各段地址
   zip-objdump -d hello | head -60     # 看 _start / main 的反汇编
   ```
3. **需要观察的现象**：`readelf` 里能看到一段被放在链接脚本指定的 ORIGIN 处的 `LOAD` 段；`objdump -d` 能看到 ZipCPU 指令（如 `LDI`、`JSR`、`BRA`）。
4. **预期结果**：`./hello` 生成成功，反汇编里能看到 `_start` 与 `main`。具体段地址取决于 `board.ld`，若地址不是 `0x10000000`，先记录下来——这正是 4.4 实践里要面对的对齐问题。
5. 若 `zip-gcc` 尚未安装，命令会报 `command not found`，属正常，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`make hello` 链接时 `-lc -lziptb -lgcc` 三个库分别提供什么？
> **答案**：`-lc` 是 newlib 的 C 标准库（`printf` 等）；`-lziptb` 是 `zlib/` 里为 ZipCPU 写的胶水（`_start`、`_write_r`、`_exit` 等）；`-lgcc` 是 GCC 自带的辅助运行时（如除法、软浮点 helper）。

**练习 2**：为什么 `hello.c` 里 `main` 没写 `return` 也能正常停机？
> **答案**：C 标准规定 `main` 不写 `return` 等价于 `return 0`；返回值 0 经由 `crt0.c` 的 `_graceful_kernel_exit → exit → _hw_shutdown` 触发「成功停机」路径。

---

### 4.4 程序如何输出、如何结束：printf→UART、exit→NEXIT/HALT→SUCCESS

#### 4.4.1 概念说明

这一节回答两个初学者最容易困惑的问题：

1. **`printf` 的字符到底怎么「出去」？** 在没有操作系统的裸机环境里，`printf` 会一路调到 C 库底层的 `_write_r`，再由我们自己写的 `_outbyte` 把每个字节写到一个 UART 外设的数据寄存器。
2. **`main` 返回后，CPU 怎么「知道」该停下来？** ZipCPU 有一条「仿真专用指令」（在汇编里写作 `NEXIT`/`NSOUT`/`NDUMP` 等，编码成特殊的 SIM 指令）。测试台或 ISS 识别到这条指令，就知道「程序请求退出」；若仿真器不认它，程序会继续落到一条 `HALT` 指令，把 CPU 永久停下。

测试台最终判定成功，依据的就是这两个信号之一。

#### 4.4.2 核心流程

**输出链**：

```
printf("Hello")  →  newlib 内部  →  _write_r(fd=1, ...)  →  _outbytes / _outbyte  →  UARTTX = c
```

**结束链**：

```
main() 返回 → exit(0) → _exit(0) → _hw_shutdown → NEXIT R1 (仿真退出指令)
                                              ↘ 若仿真器不识别 → _kernel_is_dead: HALT
测试台：execsim() 看到 SIM-Exit → m_exit=true → test_success() → 打印 SUCCESS!
        或：CPU 命中 HALT → 进入 supervisor+sleep → test_success() → SUCCESS!
```

#### 4.4.3 源码精读

输出链的尽头：`_outbyte` 把字节写到 UART 的发送寄存器 `UARTTX`（即 `_uart->u_tx`）：

[sim/zipsw/zlib/syscalls.c:57-71](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/zlib/syscalls.c#L57-L71) —— 注意 `#define TXBUSY 0`（永远不忙），所以它不等待，直接 `UARTTX = c`。`_uart` 的地址定义在 `board.h`：

[sim/zipsw/board.h:130](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L130) —— `_uart` 被放在 `0x02000000`。这又一次说明：`hello.c` 走的是「真实 UART 外设」的输出路径，能否在某个模拟器里看到字符，取决于该模拟器是否在 `0x02000000` 提供了 UART 模型。

`_write_r` 是 newlib 与我们 `_outbyte` 之间的桥梁：

[sim/zipsw/zlib/syscalls.c:344-355](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/zlib/syscalls.c#L344-L355) —— 当 `fd` 是 `STDOUT_FILENO`/`STDERR_FILENO` 时，调用 `_outbytes(nbytes, buf)`。

结束链的尽头：`_exit` 收尾后调用 `_hw_shutdown`：

[sim/zipsw/zlib/syscalls.c:384-404](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/zlib/syscalls.c#L384-L404) —— 它先补发两个空格「冲」一下串口管线，再调用 `_hw_shutdown(rcode)`。

而 `_hw_shutdown` 是一段汇编，先尝试仿真退出指令，失败则 `HALT`：

[sim/zipsw/zlib/crt0.c:223-228](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/zlib/crt0.c#L223-L228) —— `NEXIT R1` 是「仿真退出」；`_kernel_is_dead:` 下的 `HALT` 是兜底的「硬停机」。

测试台一侧，仿真退出指令的译码在 `execsim()` 里：

[sim/verilator/zipcpu_tb.cpp:1986-1993](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1986-L1993) —— 当即时数匹配 `SIM Exit(0)` 时，置 `m_exit=true; m_rcode=0`。

而 `test_success()` 正是靠这两个标志（或 CPU 进入 halt/sleep）来判定成功：

[sim/verilator/zipcpu_tb.cpp:1633-1640](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L1633-L1640) —— `return ((m_exit)&&(m_rcode == 0)) || ((!r_gie)&&(r_sleep));`。前半句对应「程序主动 SIM-Exit(0)」，后半句对应「CPU 执行 `HALT` 后停在监管态睡眠」。两条路殊途同归，都打印 `SUCCESS!`。

> 💡 因此，「HALT 表示成功」在源码层面有两层含义：程序正常 `NEXIT` 退出，或落到 `HALT` 指令；测试台都视作成功。失败则是 `BUSY` 指令或 4.1.3 里的「访问非法地址导致 `m_bomb`」。

#### 4.4.4 代码实践

1. **实践目标**：在源码里完整走一遍「`printf` 出字符」与「`main` 返回后停机」两条链，并尝试在模拟器里加载 `hello`。
2. **操作步骤**：
   - 顺着本节给出的链接，从 `printf` → `_write_r` → `_outbyte` → `UARTTX` 走一遍输出链；
   - 再从 `_graceful_kernel_exit` → `exit` → `_hw_shutdown` → `NEXIT/HALT` → `execsim` → `test_success` 走一遍结束链；
   - 尝试在 Verilator 模拟器里加载 `hello`（自动模式）：
     ```bash
     cd sim/verilator && make zipsys_tb        # 先确保测试台已构建
     ./zipsys_tb -a ../zipsw/hello
     ```
3. **需要观察的现象**：程序末尾的结论行——`SUCCESS!`、`TEST BOMBED` 或 `TEST FAILED!`，以及 `Clocks used` / `Instructions Issued` 统计。
4. **预期结果**：
   - 如果 `hello` 的链接地址（`board.ld`）与测试台 RAM（`RAMBASE=0x10000000`）一致，且测试台提供匹配的输出模型，则可看到 `SUCCESS!`；能否在终端直接看到 `Hello, World!` 字样，取决于 UART 输出是否被该模拟器捕获——**待本地验证**。
   - 如果地址不一致或 UART 写触发总线错误，则会看到 `TEST BOMBED`（对应 4.1.3 的 `m_bomb`）。此时回到 `board.ld` 检查 ORIGIN 即可定位原因。
5. 不要假设上面命令一定打印 `Hello`——按本节分析，它是否可见强依赖板子配置；如实记录你观察到的结论即可。

#### 4.4.5 小练习与答案

**练习 1**：`test_success()` 在源码里有哪两种「成功」判定？
> **答案**：① `(m_exit)&&(m_rcode==0)`——程序执行了 `SIM Exit(0)`（即 `NEXIT` 带返回码 0）；② `(!r_gie)&&(r_sleep)`——CPU 执行 `HALT` 后进入监管态且睡眠。

**练习 2**：为什么 `_exit` 在调用 `_hw_shutdown` 前要 `_outbyte(' ')` 两次？
> **答案**：因为 UART 发送管线里可能还有未发完的字符（尤其最后一个换行），先发两个空格可以「推」一下管线，避免程序在换行还没发完时就停机。

---

## 5. 综合实践

把本讲内容串成一个最小闭环任务：**「编译一个 ZipCPU 程序，加载到 Verilator 模拟器，亲眼确认它成功停机。」**

建议步骤：

1. **准备工具链与模型**：确认 `zip-gcc` 在 `PATH`；在仓库根目录执行 `make rtl` 生成 `rtl/obj_dir/` 下的 Verilator 模型。
2. **构建模拟器并跑官方测试**（这是最稳妥的「能跑」证据）：
   ```bash
   make -C bench/asm test
   make -C sim/verilator stest      # 期望末尾 SUCCESS!
   ```
   记下你看到的 `SUCCESS!`/`TEST BOMBED`、以及 `Instructions / Clock` 数值。
3. **编译 hello**：
   ```bash
   make -C sim/zipsw hello
   zip-readelf -a sim/zipsw/hello | grep LOAD     # 记录段地址
   ```
4. **尝试加载 hello 到测试台**：
   ```bash
   cd sim/verilator && ./zipsys_tb -a ../zipsw/hello
   ```
5. **如实记录结果**：
   - 末尾是 `SUCCESS!` 还是 `TEST BOMBED`？
   - 段地址（第 3 步）与测试台 `RAMBASE=0x10000000` 是否一致？若不一致，结合 4.1.3 解释你观察到的现象。
   - 终端是否出现 `Hello, World!`？为什么（结合 4.4 的 UART 输出链分析）？
6. **（进阶）换用 ISS**：在 `sim/cpp` 下 `make` 得到 `zsim`，再 `./zsim ../zipsw/hello`，对比两套模拟器的行为差异，写一两句话总结。

> 本任务的关键不是「必须看到 Hello」，而是让你把 **编译链接、地址映射、输出与停机机制、两套模拟器的差异** 这几件事在一次实操里串起来，并能解释自己看到的每一个现象。

## 6. 本讲小结

- ZipCPU 有**两套模拟器**：`sim/cpp` 的 C++ ISS（语义级、自带 UART/RAM/ROM/SDRAM 设备模型）和 `sim/verilator` 的 Verilator 模拟器（仿真真实 RTL，测试台只模型了一块 RAM）。
- `sim/verilator/Makefile` 默认构建 `zipsys_tb`/`zipbones_tb`/`zipaxil_tb`（同一份 `zipcpu_tb.cpp`，靠 `-DZIPBONES` 等宏切换外壳），并提供 `test`/`stest`/`itest` 等运行目标。
- 测试台把 ELF 加载到 `RAMBASE = 0x10000000` 的 RAM 里，靠调试端口复位、设 PC、放行，循环 `tick()` 直到成功或失败；**`HALT`（或 SIM-Exit(0)）= 成功，`BUSY`/总线错误 = 失败**。
- `sim/zipsw/hello.c` 经 `zip-gcc -T board.ld -lziptb` 编译链接成 ELF；`zlib/`（`crt0.c` 的 `_start`、`syscalls.c` 的 `_write_r`/`_exit`）是裸机下连接 C 库与硬件的胶水。
- 输出链 `printf → _write_r → _outbyte → UARTTX`；结束链 `main 返回 → exit → _hw_shutdown → NEXIT/HALT → test_success() → SUCCESS!`。
- 一个易踩的坑：`board.ld`/`board.h` 的地址必须与目标模拟器的设备模型对齐，否则会触发断言失败或 `TEST BOMBED`。

## 7. 下一步学习建议

- **想真正读懂每条指令**：进入第 2 单元，从 [u2-l1（ISA 概览：寄存器组与状态寄存器）](u2-l1-isa-overview-regs-status.md) 开始，对照 `doc/src/spec.tex`。之后你会更深刻地理解本讲里 `crt0.s` 汇编（`LDI`、`MOV ... uPC`、`HALT` 等）每一条在做什么。
- **想了解测试台更底层的判定**：阅读 `sim/verilator/zipcpu_tb.cpp` 中 `wb_read`/`wb_write` 如何驱动调试端口（这会自然引出第 5 单元的「调试接口」主题）。
- **想用更接近硬件的方式自测组件**：看 `div_tb.cpp`、`mpy_tb.cpp`、`pfcache_tb.cpp` 这些「组件级测试台」，它们是第 5 单元「Verilator 测试框架」的素材。
- **想跑懂官方自测**：在本地装好 Verilator 与工具链后，完整跑一遍 `make -C sim/verilator test`，把本讲的「SUCCESS 流程」彻底变成你机器上的事实。
