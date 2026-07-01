# RISC-V 工具链、CRT 与 TCM 链接脚本

## 1. 本讲目标

上一讲（u1-l3）我们用 Bazel 跑通了 CoralNPU 的构建系统，但你可能还有疑问：**CoralNPU 是一颗裸机（bare-metal）芯片，没有 Linux、没有 `printf`、甚至没有 `main` 之前的任何东西，那一条普通的 C/C++ 程序到底是怎么被「塞进」芯片并跑起来的？**

本讲就来回答这个问题。学完后你应该能够：

1. 说出 CoralNPU 专用 RISC-V 工具链的关键编译/链接参数（`-march`、`-mabi`、`-nostdlib`），以及它和「普通主机 GCC」的本质区别。
2. 看懂 `coralnpu_tcm.ld.tpl` 链接脚本，能判断 `.text` / `.data` / `.bss` 各自落在 ITCM 还是 DTCM，并算出每段的具体地址区间。
3. 用 `coralnpu_start.S` 画出从复位（reset）到调用 `main` 的完整 CRT（C Runtime，C 运行时）初始化序列。
4. 理解 `crt.S` 提供的「无 C 运行时依赖」辅助函数，以及 `coralnpu_gloss.cc` 为何要伪造一组系统调用。

本讲是 u2-l2「编写并编译一个 CoralNPU C++ 程序」和 u2-l3「在 Verilator 上运行」的**前置地基**——只有先搞懂「二进制长什么样、入口在哪」，你才能写出能在裸机上正确运行的程序。

---

## 2. 前置知识

在进入源码前，先建立四个关键直觉。如果你已经熟悉，可以跳到第 3 节。

### 2.1 裸机程序 vs 主机程序

你在 PC 上写 `int main()`，背后有一整套「宿主环境」帮你：操作系统负责把可执行文件加载进内存，**libc 的 `crt0`** 负责 `_start` → 清零 `.bss` → 调用 `main` → 退出。CoralNPU 没有操作系统，**这整套「`_start` 之前的事」必须由项目自己用汇编写好**，这就是 CRT（C RunTime）的职责。本讲的 `coralnpu_start.S` 和 `crt.S` 就是 CoralNPU 版的 `crt0`。

### 2.2 什么是链接脚本（Linker Script）

编译器把每个 `.cc` 编译成「目标文件」（`.o`），里面是一堆「段（section）」：`.text`（代码）、`.data`（有初值的全局变量）、`.bss`（初值为 0 的全局变量）、`.rodata`（只读常量）等。**链接器（linker）的工作，就是把这些零散的段拼成一个完整的镜像，并决定每一段最终落在哪个物理地址上。** 决定「哪段去哪」的说明书，就是**链接脚本**（`.ld` 文件）。

CoralNPU 的特殊之处在于：它的内存不是一整块 RAM，而是分成几块物理上不同的存储：

- **ITCM**（Instruction Tightly-Coupled Memory，指令紧耦合存储）：单周期访问的 SRAM，专门放**代码**。
- **DTCM**（Data TCM）：单周期访问的 SRAM，放**数据**。
- **EXTMEM / DDR**：可选的大容量外部/DDR 内存。

所以链接脚本必须把 `.text` 派进 ITCM、把 `.data` 派进 DTCM，否则程序根本无法正确执行。

### 2.3 什么是 TCM

TCM = Tightly-Coupled Memory，紧耦合存储。它和 Cache（高速缓存）的区别是：

| 属性 | TCM | Cache |
| --- | --- | --- |
| 是否确定可访问 | 是，地址固定、单周期 | 否，可能 miss、可能被替换 |
| 由谁管理 | 软件显式分配 | 硬件自动 |
| 适合放什么 | 实时性关键的代码/数据 | 一般数据 |

CoralNPU 默认 ITCM **8KB**、DTCM **32KB**（这和你在 u1-l1 学到的微架构参数一致）。

### 2.4 RISC-V 的几个编译参数

- `-march=...`：告诉编译器「目标 CPU 支持哪些指令扩展」，例如 `rv32imf` 表示 32 位 + 整数乘除（M）+ 单精度浮点（F）。
- `-mabi=...`：调用约定（ABI），`ilp32` 表示 32 位整型、寄存器传参。
- `-nostdlib`：**不要链接标准库**——因为裸机上没有标准库可链，启动代码由我们自己的 CRT 提供。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下。它们都属于 `toolchain/` 目录，是 CoralNPU「怎么把 C++ 变成可执行镜像」的全部秘密。

| 文件 | 作用 |
| --- | --- |
| [`toolchain/cc_toolchain_config.bzl`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/cc_toolchain_config.bzl) | 定义 CoralNPU 专用 C/C++ 工具链的编译/链接参数。**已升级为 multilib**：通过 `is_rv64`/`toolchain_prefix`/`gcc_version` 等可配置属性，同一套工具链既能产出 rv32 又能产出 rv64 代码。 |
| [`toolchain/crt/BUILD`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/BUILD) | 把 CRT 源文件打包成 `//toolchain/crt:crt` 这个 `cc_library`，会被所有程序自动链接。 |
| [`toolchain/coralnpu_tcm.ld.tpl`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl) | **链接脚本模板**，定义内存区划与各 section 的归属。`.tpl` 表示它是「模板」，构建时会被填充具体数值。 |
| [`rules/linker.bzl`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/linker.bzl) | Bazel 规则，把 `.tpl` 模板里的占位符替换成真实地址/大小，生成最终的 `.ld`。 |
| [`toolchain/crt/coralnpu_start.S`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S) | **程序入口 `_start`**，复位后第一条执行的指令，完成初始化并调用 `main`。 |
| [`toolchain/crt/crt.S`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/crt.S) | CRT 辅助函数（清零/拷贝内存段），在 C 运行时就绪前使用。 |
| [`toolchain/crt/coralnpu_gloss.cc`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_gloss.cc) | newlib 的「系统调用桩（syscall stubs）」，让 `printf`/`malloc` 之类在裸机上不至于链接失败。 |

> 提示：`.S`（大写 S）和 `.s`（小写 s）不同。大写 `.S` 会经过 C 预处理器，所以里面可以写 `#include`、宏和注释；本讲的两个汇编文件都是 `.S`。

---

## 4. 核心概念与源码讲解

### 4.1 RISC-V 工具链：编译目标与 CRT 的自动挂载

#### 4.1.1 概念说明

CoralNPU 的标量核是一颗 32 位 RISC-V 处理器。要把 PC 上的 C++ 编译成它能懂的机器码，需要一套「交叉工具链（cross toolchain）」——编译器跑在 x86 主机上，但产出的二进制是给 RISC-V 跑的。CoralNPU 用 Bazel 的 `cc_toolchain` 机制定义了这样一套专用工具链，并为它设定了一组默认的关键参数。

> **本讲对应的工具链版本说明**：自 commit `a8281b0d`（`toolchain: Upgrade and switch to multilib`）起，这套工具链已升级为 **multilib（多架构共用）**：一份预编译的 GCC/Clang（基于 `riscv64-unknown-elf` 前缀，GCC 16.1.0 / Clang 20）能同时产出 rv32 与 rv64 代码，由 `-march`/`-mabi` 在编译期选择对应的多库变体。工具链配置因此从「写死」改为「可配置」——增加了 `is_rv64`、`toolchain_prefix`、`gcc_version`、`clang_version` 四个属性。但**默认行为（`is_rv64=False`）仍然是产出 rv32imf 代码**，所以本讲后面引用的 `-march=rv32imf...`/`-mabi=ilp32` 在默认配置下依旧成立。理解这些参数，你才能理解后续 CRT 为什么必须那样写。

#### 4.1.2 核心流程

工具链决定三件事，这三件事直接塑造了本讲后面所有源码：

1. **指令集**：由 `is_rv64` 开关决定 `-march`/`-mabi`。默认 `is_rv64=False`，于是用 `rv32imf...`/`ilp32`；若置 `True` 则切到 `rv64imf...`/`lp64`。默认配置下 CoralNPU 支持 32 位整数、单精度浮点、向量（zve）、CSR、`zifencei`（指令缓存刷新）、`zbb`（基本位操作）。
2. **是否带标准库**：用 `-nostdlib` 关掉。于是没有 libc 提供的 `_start`，CRT 必须自己提供。注意升级后这组架构标志同时作用于**编译与链接**动作，确保两阶段一致。
3. **谁来当 `_start`**：通过「自动挂载 CRT」解决——每个 CoralNPU 程序都会被默认链接进 `//toolchain/crt:crt`，而它里面就有 `_start`。

#### 4.1.3 源码精读

先看工具链的架构参数——这是理解「为什么 CRT 要自己写」的根。升级为 multilib 后，`-march`/`-mabi` 不再是写死的字符串，而是由 `is_rv64` 开关在配置期算出来的：

[toolchain/cc_toolchain_config.bzl:164-178](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/cc_toolchain_config.bzl#L164-L178) — 第 164-165 行先用三元表达式算出 `arch`/`abi`（`is_rv64=True` → `rv64imf...`/`lp64`，默认 `False` → `rv32imf...`/`ilp32`）；第 166-178 行把它们连同 `-mcmodel=medany`、`-nostdlib` 组成架构标志集。注意第 167 行的 `actions = all_compile_actions + all_link_actions`——这组标志**同时附加给编译和链接动作**（升级前只给编译），确保 multilib 选库时两阶段的 `-march/-mabi` 完全一致。

逐字解读（按默认 `is_rv64=False` 展开）：

- `rv32imf...` —— 32 位 RISC-V，带 M（乘除）、F（浮点）、Zve32f（向量扩展子集）、Zicsr（CSR 指令）、Zifencei（`fence.i` 指令缓存同步）、Zbb（位操作）。这正好对应 u1-l1 里讲的「标量核 + 浮点 + 向量」能力。
- `ilp32` —— 所有整型用 32 位、用寄存器传参；注意**没有 `f` 后缀**，意味着浮点参数也是用整数寄存器传递（soft-float ABI 的变种，由 CRT 中后续把 FS 置位来配合）。
- `medany` —— 代码模型允许放在任意地址（相对 `medlow` 只能放低 2GB），适合 TCM 这种地址不固定在 0 附近的情况。
- `-nostdlib` —— **关键**：不链接标准 C 库的启动文件，把 `_start` 的定义权完全交给本项目自己的 CRT。

再看这四个可配置属性的定义，它们是 multilib 化的「旋钮」：

[toolchain/cc_toolchain_config.bzl:417-421](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/cc_toolchain_config.bzl#L417-L421) — 新增属性：`toolchain_prefix`（默认 `"riscv64-unknown-elf"`，multilib GCC 的统一前缀）、`gcc_version`（默认 `"16.1.0"`）、`clang_version`（默认 `"20"`）、`is_rv64`（默认 `False`）。同时第 386-387 行据此算出 `target_cpu`（`riscv64`/`riscv32`）与 `abi`，传给 `cc_common.create_cc_toolchain_config_info`。

> **multilib 的小陷阱**：默认 `toolchain_prefix = "riscv64-unknown-elf"` 看上去像 64 位工具链，但 `is_rv64 = False` 时实际编译用的是 `-march=rv32imf...`/`-mabi=ilp32`，multilib 机制会自动挑选 rv32 的库变体。也就是说「前缀是 riscv64，产物是 rv32」并不矛盾——这正是 multilib 的意义：一套编译器，按 `-march/-mabi` 路由到不同子目录的库。include 路径也相应改为按 `prefix`/`ver` 拼接（见第 142-157 行），并新增了一条 Clang 自带头文件路径 `lib/clang/<clang_ver>/include`（第 154 行）。

再看 CRT 是怎么被「自动挂载」到每个程序的。在 Bazel 的 `coralnpu_v2_binary` 规则里，只要你不指定自定义 CRT，就会默认把 `//toolchain/crt` 加进依赖：

[rules/coralnpu_v2.bzl:258-265](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/coralnpu_v2.bzl#L258-L265) — 二进制规则的 CRT 选择逻辑：未指定 `crt` 时，默认链接 `//toolchain/crt`；若 `semihosting=True` 则改用 `//toolchain/crt:crt_semihosting`。

而这个 `//toolchain/crt` 把所有 CRT 源文件（含我们后面要精读的两个 `.S`）编译进同一个库，并用 `alwayslink = True` 保证即使没被显式引用也会被链接进来：

[toolchain/crt/BUILD:17-31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/BUILD#L17-L31) — `cc_library` 名为 `crt`，源文件包括 `crt.S`、`coralnpu_start.S`、`coralnpu_gloss.cc` 等；`alwayslink = True` 确保启动代码一定进入最终镜像；`-DSKIP_HTIF_SYMBOLS` 关闭半主机（semihosting）相关符号。

> 小贴士：`alwayslink = True` 是 Bazel/链接器的概念。普通库只有「被引用的符号」才会被链接；但 `_start` 在程序里从没人显式「调用」，它是被链接脚本 `ENTRY(_start)` 标记为入口的。`alwayslink` 强制整库参与链接，这样 `_start` 才不会因为「没人引用」而被丢掉。

#### 4.1.4 代码实践

**实践目标**：确认你机器上实际使用的工具链参数与版本，把「书本结论」变成「亲眼所见」。

操作步骤：

1. 执行 `bazel build //examples:coralnpu_v2_hello_world_add_floats`（若尚未配置环境，可在 bazel 输出的命令行里找到实际的 `clang`/`riscv64-unknown-elf-gcc` 调用）。
2. 用 `bazel aquery 'mnemonic("CppCompile", deps(//examples:coralnpu_v2_hello_world_add_floats))'` 查看实际编译命令，确认其中包含 `-march=rv32imf_zve32f_zicsr_zifencei_zbb` 与 `-mabi=ilp32`。
3. 在工具链 include 路径里能看到 `riscv64-unknown-elf/include/c++/16.1.0`（注意前缀是 `riscv64`，但配合上面的 `-march=rv32imf` 正是 multilib 的体现），印证 GCC 16.1.0 / Clang 20 版本。
4. 在 `WORKSPACE` 里找到 `http_archive` 段（约第 162-178 行），确认这次升级把预编译工具链归档由 `toolchain_kelvin_v2`（`.tar.gz`）更名为 `toolchain_coralnpu_v2`（`.tar.xz`，`@toolchain_coralnpu_v2` 仓库），这就是上述 multilib 工具链的下载来源。

需要观察的现象：实际编译命令里确实出现上述 `-march`/`-mabi`/`-nostdlib`，且没有 `-lcrt0` 之类的标准启动库。

预期结果：参数与本讲引用的源码完全一致。若环境无法运行 bazel，则**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CoralNPU 工具链要用 `-nostdlib`？如果去掉会怎样？
**参考答案**：因为裸机环境没有操作系统提供的标准库启动代码（`crt0`）和系统调用。去掉 `-nostdlib` 后，链接器会尝试拉入宿主 libc 的 `_start`，它依赖 `write`/`brk` 等系统调用，在 CoralNPU 上不存在，导致链接失败或运行即崩溃。本项目用 `coralnpu_start.S` 自带的 `_start` 替代。

**练习 2**：`-mabi=ilp32` 中没有 `f`，但 CoralNPU 明明有浮点单元，这矛盾吗？
**参考答案**：不矛盾。`-mabi` 描述的是**参数传递约定**（这里用整数寄存器传参），而浮点运算能力由 `-march` 中的 `f` 扩展提供。CRT 在启动时会把 `mstatus` 的 FS 位置为 Dirty 来真正启用浮点单元（见 4.3 节）。

---

### 4.2 TCM 链接脚本模板：决定每一段的归宿

#### 4.2.1 概念说明

这是本讲最核心的一个文件。`coralnpu_tcm.ld.tpl` 是一个**链接脚本模板**（`.tpl` = template）。它本身不能直接用，因为里面的内存大小还是占位符（如 `@@ITCM_LENGTH@@`）。构建时，`rules/linker.bzl` 会把这些占位符替换成真实数字（ITCM=8、DTCM=32 等），产出最终的 `coralnpu_tcm.ld`。

这个文件回答了一个根本问题：**CoralNPU 程序的内存长什么样？** 答案是一张「内存区划 + 段落分配表」。

#### 4.2.2 核心流程

链接脚本的工作可以拆成两步：

**第一步：声明内存区域（MEMORY）**——告诉链接器「我有哪些存储，各自从哪开始、多大」。

默认参数下（ITCM=8KB、DTCM=32KB），最终的内存布局是：

| 区域 | 起始地址 | 结束地址 | 大小 | 用途 |
| --- | --- | --- | --- | --- |
| ITCM | `0x00000000` | `0x00001FFF` | 8 KB | 代码 + 只读数据 |
| DTCM | `0x00010000` | `0x00017FFF` | 32 KB | 全局变量 + 栈 + 堆 |
| EXTMEM | `0x20000000` | — | 4 MB | 可选外部内存 |
| DDR | `0x80000000` | — | 2 GB | 可选 DDR |

地址计算用十六进制很直观。例如 DTCM 起始 `0x00010000` = 64KB，长度 32KB = `0x8000`，所以结束于：

\[
\text{DTCM}_{\text{end}} = 0x00010000 + 0x8000 = 0x00018000
\]

即区间 `[0x10000, 0x18000)`，亦即文档里写的 `0x10000 - 0x17FFF`。

**第二步：把 section 分配进区域（SECTIONS）**——核心规则是：

- `.text`（代码）/ `.rodata`（只读）/ `.init.array`（构造函数表）→ **ITCM**（只读可执行 `rx`）。
- `.data`（有初值数据）/ `.bss`（零初值数据）/ `.tdata`/`.tbss`（线程局部存储）/ 栈 / 堆 → **DTCM**（可读可写 `rw`）。
- `.extdata`/`.ddr_data` 等自定义段 → **EXTMEM / DDR**。

#### 4.2.3 源码精读

先看 MEMORY 声明，这是整张地图的「图例」：

[toolchain/coralnpu_tcm.ld.tpl:5-10](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L5-L10) — 声明四个内存区域。`ITCM(rx)` 表示只读可执行，`ORIGIN = 0x00000000`；`DTCM(rw)` 的 `ORIGIN` 与大小都是占位符 `@@DTCM_ORIGIN@@` / `@@DTCM_LENGTH@@`；EXTMEM 起始 `0x20000000`，DDR 起始 `0x80000000`。

注意 `ITCM` 的 `ORIGIN = 0x00000000` 是写死的——这非常关键，它意味着**复位后 PC 从地址 0 取指**，所以程序入口 `_start` 必须放在 ITCM 的最开头。

接着看 ITCM 里的段落（代码区）：

[toolchain/coralnpu_tcm.ld.tpl:20-56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L20-L56) — `.text` 段最先把 `*(._init)` 放进来，紧接着 `.init.array`（C++ 全局对象构造函数指针表）和 `.rodata`（字符串常量等）也落在 ITCM。

这里有个精妙的设计：`.text` 的第一条 `*(._init)`，专门接收名为 `._init` 的段。回头你会看到，`_start` 函数正是用 `.section ._init` 声明自己的——这样就**保证 `_start` 一定排在 ITCM 最前面、也就是地址 0**，复位即执行。

再看 DTCM 里的数据区，这里有本讲最值得讲的一处技巧——「全局指针 relaxation」：

[toolchain/coralnpu_tcm.ld.tpl:81-107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L81-L107) — `.data` 段定义在 DTCM。其中 `_global_pointer = . + 0x800` 和 `__global_pointer$ = . + 0x800` 设定「全局指针锚点」，让 `gp` 寄存器能以更短的指令访问 `[gp-2048, gp+2047]` 范围内的小数据；末尾还预留了一个 `_ret` 4 字节槽位，用于存放 `main` 的返回值，供系统里其他核/主机查看。

`_ret` 这个细节会在 4.3 节再次出现：CRT 在调用 `main` 前会往 `_ret` 写一个「哨兵值」`0x0badd00d`，调用后再写入真正的返回值。这样外部观察者只要读到 `0x0badd00d`，就知道「`main` 还没正常返回」，是个很实用的调试手段。

继续看 `.bss`、`.noinit`：

[toolchain/coralnpu_tcm.ld.tpl:109-123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L109-L123) — `.bss`（零初值全局变量）放 DTCM；`.noinit (NOLOAD)` 表示「不生成加载镜像」，启动时**不**被清零，适合放需要跨复位保留的数据。

最后看堆和栈：

[toolchain/coralnpu_tcm.ld.tpl:158-172](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/coralnpu_tcm.ld.tpl#L158-L172) — `.heap` 放在 `@@HEAP_LOCATION@@`（默认 DTCM），`.stack` 固定放 DTCM，大小为 `STACK_SIZE`，并以 `__stack_end__` 作为栈顶（栈是向下生长的）。

这里 `@@HEAP_SIZE_SPEC@@` 和 `@@STACK_START_SPEC@@` 又是占位符，它们的真实内容由 `linker.bzl` 根据堆位置决定——下面马上看。

现在看模板是怎么被「填空」的。这是占位符替换的核心逻辑：

[rules/linker.bzl:22-56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/rules/linker.bzl#L22-L56) — 设定默认 ITCM=8KB、DTCM=32KB、`dtcm_origin_default = "0x00010000"`；当 ITCM/DTCM 不等于默认值时改用 highmem 起址 `0x00100000`；默认栈 128 字节；若堆在 DTCM 且未指定大小，则采用「剩余空间」逻辑 `. = ORIGIN(DTCM) + LENGTH(DTCM) - STACK_SIZE`，让堆吃满 DTCM 尾部、栈紧贴其后。

这段逻辑揭示了一个关键事实：**默认 DTCM 起址是 `0x00010000`，和 `doc/integration_guide.md` 的内存映射表完全吻合**（见下方验证）。同时，「堆吃满剩余空间」意味着你 `malloc` 能用的空间 = `DTCM 总大小 - .data - .bss - 栈`。

#### 4.2.4 代码实践

**实践目标**：亲手把模板占位符填上，得到真实的 `.ld`，并与官方内存映射表互相验证。

操作步骤：

1. 打开 [`doc/integration_guide.md`](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md) 的「CoralNPU Memory Map」一节（约第 156-164 行），找到 ITCM/DTCM 的范围表。
2. 手工把模板填空：把 `@@ITCM_LENGTH@@` 填 `8`、`@@DTCM_ORIGIN@@` 填 `0x00010000`、`@@DTCM_LENGTH@@` 填 `32`，写出 MEMORY 块的最终内容。
3. 标注 `.text` / `.rodata` → ITCM（`0x00000000` 起），`.data` / `.bss` / `.stack` → DTCM（`0x00010000` 起）。

需要观察的现象：你填出来的 ITCM `[0x0000, 0x1FFF]`、DTCM `[0x10000, 0x17FFF]` 应当与 integration_guide 表格**逐字节一致**。

预期结果（自行填写的对照表）：

| Section | 落在区域 | 起始地址 |
| --- | --- | --- |
| `.text` / `._init` / `.init.array` / `.rodata` | ITCM | `0x00000000` |
| `.data`（含 `_ret`）/ `.bss` / `.tdata` / `.stack` | DTCM | `0x00010000` |
| `.extdata` | EXTMEM | `0x20000000` |
| `.ddr_data` | DDR | `0x80000000` |

若想进一步在构建产物上验证，可对最终 `.elf` 运行 `riscv32-unknown-elf-readelf -l <elf>` 或 `objdump -h <elf>`，观察各段的 `VMA`（虚拟地址）落在上述区间。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.bss` 用 `(NOLOAD)` 而 `.data` 不用？
**参考答案**：`.bss` 存的是初值为 0 的变量，镜像里不需要保存一堆 0——只需记住「这段地址范围在启动时被清零即可」，所以用 `NOLOAD` 不占镜像体积。`.data` 有非零初值，必须把初值烧进镜像，由启动代码（或加载器）拷贝到 DTCM，所以不能 `NOLOAD`。

**练习 2**：如果某个程序的数据非常大，32KB 的 DTCM 装不下，开发者该怎么办？
**参考答案**：利用链接脚本预留的自定义段，把大块数据显式放进 EXTMEM 或 DDR——例如用 `__attribute__((section(".ddr_data")))` 把大数组放进 DDR；或通过 `coralnpu_v2_binary` 规则的 `heap_location` 参数把堆搬到 DDR。

---

### 4.3 CRT 启动序列：从复位到 `main`

#### 4.3.1 概念说明

`coralnpu_start.S` 是整个 CoralNPU 软件的「第 0 行代码」。复位瞬间，PC=0（ITCM 起址），CPU 取到的第一条指令就是这个文件里的 `_start`。它要在「什么都没有」的荒原上，把 C/C++ 程序运行所需的一切前提条件准备就绪：栈、全局指针、清零的 `.bss`、构造函数、浮点使能、异常向量，最后才调用 `main`。

理解这段代码，等于理解了「一个裸机程序是如何被点亮的」。

#### 4.3.2 核心流程

`_start` 的执行序列可以画成一条清晰的流水线：

```
复位 (PC=0)
   │
   ▼
1. 复位指令计数器 minstret（供协同仿真校验）
   │
   ▼
2. 设置 sp（栈顶）、gp（全局指针）；清零所有通用寄存器
   │
   ▼
3. crt_section_clear：把 .bss 清零
   │
   ▼
4. 遍历 .init.array：运行 C++ 全局对象的构造函数
   │
   ▼
5. 设置 mtvec（异常向量）= coralnpu_exception_handler
   │
   ▼
6. 置 mstatus 的 FS/VS 位 = Dirty（启用浮点与向量）
   │
   ▼
7. 往 _ret 写哨兵 0x0badd00d
   │
   ▼
8. 调用 main(argc=0, argv=0)
   │
   ▼
9. 保存返回值 → __cxa_finalize → 跑 .fini_array 析构函数
   │
   ▼
10. 把返回值写回 _ret；返回 0 则执行 mpause（成功停机），非 0 则 ebreak（失败断点）
```

#### 4.3.3 源码精读

**入口与复位计数器**。`_start` 被放进 `._init` 段（链接脚本里它排在 ITCM 最前，于是地址 = 0 = 复位 PC）：

[toolchain/crt/coralnpu_start.S:26-35](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S#L26-L35) — `_start` 用 `.section ._init` 声明，`.global`/`.weak` 使其可被覆盖；开头先把 `minstret`/`minstreth`（已退休指令计数器）清零并读回，用于和参考模型做协同仿真比对。

**栈与全局指针**。这里有一对容易迷惑的伪指令 `.option norelax` / `.option relax`：

[toolchain/crt/coralnpu_start.S:39-42](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S#L39-L42) — `la sp, __stack_end__` 设栈顶；`la gp, _global_pointer` 设全局指针。`norelax` 阻止汇编器把 `la gp`「优化」成相对 `gp` 自身的短指令——因为此刻 `gp` 还没被赋值，那种优化会用到一个垃圾值。

> 名词解释：**linker relaxation（链接器松弛）**是 RISC-V 的一项优化，把 `la gp, sym`（两条指令）在能证明 `sym` 离 `gp` 很近时，改写成 `addi gp, gp, offset`（一条指令）。但在「`gp` 尚未初始化」的启动阶段绝不能这么做，所以临时关掉。这正是 4.2 节链接脚本特意定义 `_global_pointer` 锚点的用意——两者配合，让后续程序里访问小数据更省指令。

**清零 `.bss`**。复用 `crt.S` 里的辅助函数：

[toolchain/crt/coralnpu_start.S:71-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S#L71-L74) — 把 `__bss_start__` / `__bss_end__`（链接脚本导出的符号）放进 `a0`/`a1`，调用 `crt_section_clear` 把 `.bss` 区间清零。这是 C 程序「全局变量初值为 0」的物理保障。

**运行构造函数 + 设置异常向量 + 启用浮点**：

[toolchain/crt/coralnpu_start.S:76-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S#L76-L96) — 遍历 `[__init_array_start__, __init_array_end__)` 逐个调用构造函数指针（C++ 全局对象由此被构造）；把 `mtvec` 指向默认的 `coralnpu_exception_handler`；再用 `mstatus` 的 `0x6600` 位把 FS（`mstatus.FS`，bit 13-14）和 VS（`mstatus.VS`，bit 9-10）都置为 `Dirty`（即 `0b11`），正式打开浮点与向量单元。

> 名词解释：`mstatus` 是 RISC-V 机器模式状态寄存器。`FS`/`VS` 字段必须设成非 `Off`，浮点/向量指令才会真正生效，否则会触发非法指令异常。`0x6000` 是 FS=`Dirty`，`0x0600` 是 VS=`Dirty`，合起来就是 `0x6600`。

**哨兵与调用 `main`**：

[toolchain/crt/coralnpu_start.S:98-114](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S#L98-L114) — 先往 `_ret` 写哨兵 `0x0badd00d`；随后以 `argc=0`、`argv=0` 调用 `main`，并用 `ra = main` 让 `main` 返回后能继续往下执行；返回值暂存到 `s2`。

**退出序列（析构 + 停机）**：

[toolchain/crt/coralnpu_start.S:116-146](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_start.S#L116-L146) — 调用 `__cxa_finalize`，再逆序遍历 `.fini_array` 跑析构函数；把 `main` 的返回值写回 `_ret`；若返回值为 0（成功），读回 `minstret` 后执行自定义 `mpause` 指令（`.word 0x08000073`）让核优雅停机；若非 0（失败），执行 `ebreak` 触发断点，便于调试器或仿真器捕获。

这里的 `.word 0x08000073` 是 CoralNPU 自定义的 `mpause` 指令的原始编码——它不在标准 RISC-V 里，是本项目用来告诉硬件/仿真器「我跑完了，可以停了」的信号。u2-l3 讲仿真器时你会再次遇到「检测 halt」这个机制。

#### 4.3.4 代码实践

**实践目标**：在纸上完整复现「复位 → main」的指令序列，建立对启动流程的肌肉记忆。

操作步骤：

1. 对照上面的「核心流程」十步，在 `coralnpu_start.S` 里为每一步找到对应的指令行号。
2. 回答三个问题：(a) 复位后 `sp` 指向哪个符号？(b) `.bss` 是由哪条 `call` 清零的？(c) 浮点单元是在哪一步被启用的？
3. 进阶：假设你写了一个 C++ 程序，含一个带构造函数的全局对象 `Foo g_foo;`，追踪它的构造函数是在 `_start` 的哪一步、通过哪个数组被调用的。

需要观察的现象：你能把每一步都精确映射到具体行号，并且说清「为什么这一步必须在下一步之前」（例如：必须先清零 `.bss` 才能让构造函数安全运行）。

预期结果：
- (a) `sp` 指向 `__stack_end__`（DTCM 末尾附近）。
- (b) 由 `call crt_section_clear` 完成（`coralnpu_start.S:74`）。
- (c) 浮点在「置 `mstatus` 的 `0x6600`」一步启用（`coralnpu_start.S:95-96`）。
- 进阶：`g_foo` 的构造函数指针被链接器收集进 `.init.array`，由 `_start` 的 `init_array_loop`（`coralnpu_start.S:80-84`）逐个调用。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_start` 要先把哨兵 `0x0badd00d` 写进 `_ret`，而不是直接调用 `main`？
**参考答案**：这是一种防御性调试手段。如果程序在 `main` 返回前就崩了（比如触发了 `ebreak`），外部观察者读到的 `_ret` 仍是 `0x0badd00d`，一眼就能判断「`main` 没正常返回」。只有 `main` 正常返回后，`_ret` 才会被覆盖成真实返回值。

**练习 2**：`main` 返回 0 和返回非 0，CRT 的行为有何不同？为什么这样设计？
**参考答案**：返回 0 视为成功，执行 `mpause` 优雅停机（仿真器据此判定「程序正常结束」）；返回非 0 视为失败，执行 `ebreak` 进入断点，方便调试。这样测试框架只需观察核是 `mpause` 还是 `ebreak`，就能区分通过/失败。

---

### 4.4 CRT 辅助库 `crt.S` 与系统调用桩 `gloss`

#### 4.4.1 概念说明

`_start` 用到了 `crt_section_clear`，但它来自另一个文件 `crt.S`。这个文件提供「在 C 运行时就绪之前」可用的纯汇编工具函数。为什么单独拎出来？因为清零 `.bss` 时，C 运行时（栈、全局指针、堆）可能还没准备好，不能用 C 函数——只能用不依赖任何运行时的「裸」汇编。

而 `coralnpu_gloss.cc` 解决的是另一个问题：很多库代码（包括 newlib 的 `printf`、`malloc`）会调用一组「系统调用」（`_write`、`_sbrk` 等）。在主机上这些由 OS 实现；裸机上没有 OS，但链接器仍会找这些符号。`gloss`（glue code 的戏称）提供一组「桩」实现，让链接通过、让这些函数在裸机上「有地方可去」。

#### 4.4.2 核心流程

`crt.S` 的两个函数遵循 RISC-V 调用约定（`a0`-`a7` 传参），但**刻意不依赖栈/全局指针/线程指针**，这样它们能在 CRT 早期安全使用：

- `crt_section_clear(a0=start, a1=end)`：把 `[start, end)` 区间按字（4 字节）清零，要求 4 字节对齐。
- `crt_section_copy(a0=dst_start, a1=dst_end, a2=src)`：把 `src` 的内容按字拷贝到 `[dst_start, dst_end)`，要求三者都对齐且不重叠。

`gloss` 则提供：`_write`（缓冲到行缓冲区，本讲版本不真正输出）、`_sbrk`（基于 `__heap_start__`/`__heap_end__` 实现 `malloc` 的堆）、`_exit`/`_kill`/`_getpid`（都用 `ebreak` 兜底）等。

#### 4.4.3 源码精读

先看 `crt_section_clear` 的实现，注意它如何用「注释里的契约」保证安全：

[toolchain/crt/crt.S:31-78](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/crt.S#L31-L78) — `.section .crt, "ax", @progbits` 把它放进可分配的代码段；`crt_section_clear` 先检查 `start < end`、再检查 4 字节对齐，对齐错误就 `ebreak`；通过后用 `sw zero` 循环逐字清零。

注意 `.section .crt, "ax"` 里的 `"ax"` 标志——`a`（allocatable，分配地址）、`x`（executable，可执行）。注释说得很明白：没有 `ax`，链接器不会给这个段分配 ROM 空间，函数就会被丢弃。这是裸机链接里很容易踩的坑。

`crt_section_copy` 同理，多了一处精巧的重叠检查：

[toolchain/crt/crt.S:101-152](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/crt.S#L101-L152) — 检查 `dst`/`end`/`src` 三者对齐，再用 `(start - src)` 与 `(end - start)` 比较来禁止「源与目标破坏性重叠」，最后正向逐字拷贝。

> 历史彩蛋：`crt.S` 文件头注释提到，传统工具链里有个 `crt0.o` 会被链进每个可执行文件做类似的事，CoralNPU 沿用了这个命名习惯。

再看 `gloss` 的 `_sbrk`——这是 `malloc` 能在裸机工作的关键：

[toolchain/crt/coralnpu_gloss.cc:124-137](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_gloss.cc#L124-L137) — `_sbrk` 用一个静态指针 `_heap_ptr` 从 `__heap_start__` 开始单调增长，越界（超过 `__heap_end__`）则返回 `ENOMEM`。这两个符号正是 4.2 节链接脚本里 `.heap` 段导出的边界。

> **为何此文件本次有改动**：升级到 GCC 16.1.0 后，标准头对 `size_t` 的暴露更严格，文件顶部新增了 `#include <cstddef>` 与 `using std::size_t;`（第 22-23 行），确保下方 `operator new(size_t)` 等声明能找到 `size_t`。逻辑本身未变，仅整体下移了 2 行。

这就把整条链串起来了：**链接脚本定义 `.heap` → 导出 `__heap_start__`/`__heap_end__` → `gloss._sbrk` 用它们实现堆 → `malloc`/C++ `new` 据此工作**。

`gloss` 末尾还把 C++ 的 `operator new`/`delete` 转发到 `malloc`/`free`：

[toolchain/crt/coralnpu_gloss.cc:139-143](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/toolchain/crt/coralnpu_gloss.cc#L139-L143) — 重载全局 `operator new`/`delete` 调用 `malloc`/`free`（并提供了带 `size_t` 对齐参数的 sized-delete 重载），让裸机上的 `new` 也能用（堆来自 `_sbrk`）。

#### 4.4.4 代码实践

**实践目标**：追踪一次 `malloc` 在 CoralNPU 上的完整内存来源，验证「堆来自 DTCM」。

操作步骤（源码阅读型实践）：

1. 假设程序里写了 `int* p = (int*)malloc(64);`。
2. 追踪调用链：`malloc` → `_sbrk`（`coralnpu_gloss.cc:124`）→ 读写 `_heap_ptr`。
3. 找到 `_heap_ptr` 的初值来源：`&__heap_start__`，这是链接脚本 `.heap` 段（`coralnpu_tcm.ld.tpl:158`）的起始。
4. 回到 `linker.bzl`，确认默认 `heap_location = "DTCM"`，所以 `__heap_start__` 落在 DTCM。
5. 估算可用堆大小：`堆上限 = ORIGIN(DTCM) + LENGTH(DTCM) - STACK_SIZE - 已用 .data/.bss`。

需要观察的现象：你能完整画出 `malloc(64)` → `_sbrk` → `__heap_start__`(DTCM) 的数据流，并解释「为什么 malloc 的内存是单周期可访问的 DTCM 而不是慢速 DDR」。

预期结果：默认配置下，`malloc` 返回的地址位于 `[0x00010000, 0x00017FFF]`（DTCM）内，紧贴 `.data`/`.bss` 之后、栈之前。

> 进阶思考：若把 `coralnpu_v2_binary` 的 `heap_location` 设为 `"DDR"`，则 `__heap_start__` 会跳到 `0x80000000`，`malloc` 的内存就变成 DDR——容量大但访问慢。这是 CoralNPU 程序员可以主动权衡的「性能 vs 容量」开关。

#### 4.4.5 小练习与答案

**练习 1**：`crt_section_clear` 为什么要求 4 字节对齐？如果 `.bss` 不是 4 字节对齐会怎样？
**参考答案**：因为它用 `sw`（store word，4 字节）逐字写，若起止不对齐 4 字节，`sw` 会触发地址未对齐异常。链接脚本里 `.bss` 用了 `ALIGN(16)`，且函数内部还有对齐检查（不对齐就 `ebreak`），双重保险。

**练习 2**：`_write` 这个系统调用桩并没有真正把字符发到串口，那它存在的意义是什么？
**参考答案**：让链接通过。很多库（newlib 的 `printf` 等）会引用 `_write`，如果找不到该符号就会链接失败。`gloss` 提供一个「不报错但也不真输出」的桩（本版本只是缓冲到行缓冲区），保证程序能编出来；真正想要日志输出的场景，可以改用 semihosting 版的 `coralnpu_htif_gloss.cc`（对应 `crt_semihosting`）。

---

## 5. 综合实践：画一张「CoralNPU 程序的诞生与运行」全景图

把本讲三个最小模块串起来，完成一个综合任务：**为一条简单的 `int main() { return 0; }` 程序，画出从源码到 CPU 取指执行的完整链路，并标注每一步由哪个文件负责。**

要求产出一张表/图，至少包含以下阶段，每阶段写出「负责文件」+「关键符号/行号」：

1. **编译**：源码 → `.o`。负责：`cc_toolchain_config.bzl`（由 `is_rv64` 算出 `-march`/`-mabi`/`-nostdlib`，第 164-178 行）。
2. **链接脚本生成**：模板 → `.ld`。负责：`coralnpu_tcm.ld.tpl` + `linker.bzl`（占位符替换，第 48-56 行）。
3. **段落归位**：决定 `.text`/`.data`/`.bss` 的地址。负责：`coralnpu_tcm.ld.tpl`（`.text`→ITCM 第 22-27 行，`.data`→DTCM 第 81-107 行）。
4. **CRT 挂载**：把 `_start` 链进镜像。负责：`crt/BUILD`（`alwayslink=True`，第 17-31 行）+ `coralnpu_v2.bzl`（第 264-265 行）。
5. **复位执行**：PC=0 取到 `_start`。负责：`coralnpu_start.S`（`._init` 段，第 26-31 行）。
6. **运行时就绪**：设栈/清 `.bss`/构造/启浮点。负责：`coralnpu_start.S`（第 39-96 行）+ `crt.S`（`crt_section_clear`，第 50 行）。
7. **进入 `main`**：调用用户代码。负责：`coralnpu_start.S`（`jalr ra, ra`，第 112 行）。
8. **退出停机**：返回值写 `_ret`，成功 `mpause`/失败 `ebreak`。负责：`coralnpu_start.S`（第 131-146 行）。

**验收标准**：你能对着这张图，回答任意一个「这一步如果不做会怎样」的反事实问题，例如：

- 如果 `linker.bzl` 把 DTCM 起址填错（比如填成 `0x00000000` 与 ITCM 重叠），会发生什么？（答：`.data` 会覆盖 `.text`，程序取到的是数据而非指令，行为完全不可预测。）
- 如果 `coralnpu_start.S` 漏掉清零 `.bss`，会发生什么？（答：全局变量初值是上电时的随机 SRAM 内容，C 程序假设的「初值为 0」不成立，逻辑错误。）
- 如果忘了置 `mstatus.FS`，会发生什么？（答：第一条浮点指令触发非法指令异常，跳到 `coralnpu_exception_handler` 的 `ebreak`。）

完成后，你就真正理解了「一个 CoralNPU 二进制是如何被组装起来、又是如何被点亮的」——这是后续编写程序（u2-l2）和在仿真器上运行（u2-l3）的共同基础。

---

## 6. 本讲小结

- CoralNPU 用专用 RISC-V 工具链编译，关键参数是 `-march=rv32imf_zve32f_zicsr_zifencei_zbb -mabi=ilp32 -nostdlib`，因此必须自己提供 `_start`。
- `coralnpu_tcm.ld.tpl` 是链接脚本模板：把 `.text`/`.rodata` 放进 ITCM（`0x00000000` 起），把 `.data`/`.bss`/堆/栈放进 DTCM（默认 `0x00010000` 起）；构建时由 `linker.bzl` 填充占位符。
- 默认内存布局（ITCM 8KB @ `0x0`、DTCM 32KB @ `0x10000`）与 `integration_guide.md` 的内存映射表完全一致。
- `coralnpu_start.S` 的 `_start` 是复位入口（靠 `._init` 段排在地址 0），完成设栈/清 `.bss`/跑构造函数/启浮点/调 `main` 的全套 CRT 初始化。
- `crt.S` 提供「无运行时依赖」的 `crt_section_clear`/`crt_section_copy`；`coralnpu_gloss.cc` 提供 newlib 系统调用桩，其中 `_sbrk` 把 `malloc` 接到链接脚本定义的 `.heap`（默认在 DTCM）。
- CRT 通过 `//toolchain/crt:crt`（`alwayslink=True`）被每个程序自动链接，`_ret` 哨兵与 `mpause`/`ebreak` 区分成功/失败停机。

---

## 7. 下一步学习建议

本讲解决了「二进制长什么样、入口在哪」。接下来：

- **u2-l2 编写并编译一个 CoralNPU C++ 程序**：动手写第一个程序，亲手使用 `__attribute__((section(".data")))` 把变量放进 DTCM，并用 `coralnpu_v2_binary` 编译，验证本讲的链接脚本结论。
- **u2-l3 在 Verilator 仿真器上运行程序**：把编译出的 `.elf` 加载进 `core_mini_axi_sim`，观察 `mpause`/`ebreak` 停机行为——你会看到本讲的退出序列如何被仿真器捕获。

进阶方向（后续单元）：

- 想深入了解「ITCM/DTCM 的硬件实现」，参考 u6-l2（TCM 与 SRAM）。
- 想了解「外部主机如何把代码灌进 ITCM 并启动 CoralNPU」，参考 `doc/integration_guide.md` 的「Booting CoralNPU」一节与 u3-l5（CSR 与启动控制），它和本讲的 `_start` 共同构成完整的「加载—启动—执行」闭环。
