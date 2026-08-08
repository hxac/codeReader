# 软件工具链与 ABI

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ZipCPU 软件工具链由哪三大件（binutils / GCC / newlib）组成，它们各自用什么补丁文件加入 ZipCPU 后端，以及三者之间严格的构建依赖顺序。
- 读懂 `sw/Makefile` 用「nonce.txt 标记 + 三段式 Makefile 目标」驱动整个交叉工具链构建的机制。
- 复述 ZipCPU 的 ABI 关键约定：ELF 可执行格式与机器标识码、没有原生 `JSR` 指令这件事如何用「返回地址塞进 R0」绕过、前 5 个参数放 `R1–R5`、返回值放回 `R1`、`R13` 作栈指针、`R12` 作帧指针。
- 读懂 ZipCPU 的链接脚本规范：三类内存（flash / block RAM / SDRAM）、`_rom/_kram/_ram` 等引导符号、`.start` 与 `.boot` 段，以及 CRT0/Bootloader 如何据此把程序从 flash 搬到 RAM。
- 认识遗留汇编器 `zasm`，并理解它为何「退役但未删除」。

本讲承接 u2-l2（指令格式与编码）——那一讲讲的是「单条指令长什么样」，本讲讲的是「一堆指令如何被组装、链接成一个能在 ZipCPU 上跑的可执行程序，以及程序与 CPU 之间的二进制接口」。

## 2. 前置知识

在进入源码前，先建立几个关键直觉。

**交叉工具链（cross toolchain）是什么？**
普通 PC 上 `gcc` 生成的是给本机 x86/ARM 跑的机器码。但 ZipCPU 是 FPGA 里的软核，你的开发机并不是 ZipCPU。所以需要一套「在 x86 上运行、却生成 ZipCPU 机器码」的编译器，这叫**交叉编译器**，命令前缀为 `zip-`，例如 `zip-gcc`、`zip-as`、`zip-ld`。

**为什么用「补丁」而不是维护一份完整源码？**
GCC、binutils、newlib 都是庞大的上游项目。完整 fork 一份维护成本极高，且难以跟进上游修复。ZipCPU 的做法是：保留官方 tarball（如 `gcc-10.3.0.tar.xz`），用一个 `.patch` 文件描述「相对官方源码要改哪些地方来加入 ZipCPU 后端」。构建时先解压官方源码、再 `patch -p1` 打补丁。作者在 README 里坦言这个做法借鉴自 eco32 CPU，因为一份干净的补丁本身就是「后端要从哪里下手」的最佳文档。

**什么是 ABI？**
ABI（Application Binary Interface，应用二进制接口）是「编译好的机器码之间、以及机器码与 CPU 之间」的约定：参数用哪些寄存器传、返回值放哪、栈往哪个方向长、调用一个函数要遵守什么纪律。ISA（u2 单元）规定「CPU 能做什么」，ABI 规定「编译器该怎么用 CPU 才能让分别编译的模块拼到一起」。ABI 错了，函数调用的双方对不上号，程序就会崩。

**nonce 机制是什么？**
`nonce` 原意是「只用于这一次的随机数」。这里指一个空文件 `nonce.txt`，构建系统靠「这个文件存不存在」来判断某一步是否已经完成，从而决定要不要重跑。它是一种轻量的构建状态机。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `sw/README.md` | 说明工具链目录的用途、补丁思路、nonce 机制与安装位置 |
| `sw/Makefile` | 工具链构建的总调度，定义 binutils/gcc/newlib 三段目标与依赖 |
| `sw/gcc-script.sh` `sw/gas-script.sh` `sw/nlib-script.sh` | 三大件的 configure 脚本（解压+打补丁+配置） |
| `sw/gcc-zippatch.patch` `sw/gas-zippatch.patch` `sw/nlib-zippatch.patch` | 三大件的 ZipCPU 后端补丁 |
| `doc/src/spec.tex` 第 13 章（Tool Suite and ABI） | ABI 与链接脚本的权威规范 |
| `sim/zipsw/board.ld` | 仿真用链接脚本实例（单 RAM 区） |
| `bench/zipsim.ld` | 仿真用链接脚本实例（flash + sdram 两区） |
| `sw/zasm/README.md` `sw/zasm/zasm.y` | 遗留汇编器说明与其 yacc 语法/主程序 |

## 4. 核心概念与源码讲解

### 4.1 工具链补丁机制与构建脚本

#### 4.1.1 概念说明

ZipCPU 不重写编译器，而是给三大上游组件各打一个补丁：

- **binutils**（`gas-zippatch.patch`）：加入汇编器 `zip-as`、链接器 `zip-ld`、归档器 `zip-ar`、反汇编等。这是最底层，没有它 `zip-as` 不存在，GCC 也无从编译。
- **GCC**（`gcc-zippatch.patch`）：加入 ZipCPU 后端，产出 C 编译器 `zip-gcc`。这是工具链的核心。
- **newlib**（`nlib-zippatch.patch`）：给嵌入式环境提供 C 标准库（`printf`、`malloc`、字符串函数等）。

这三个补丁的关系是**串行依赖**：GCC 构建时需要 `zip-as` 已经在 `PATH` 里，而 GCC 的完整库构建又依赖 newlib。所以构建顺序固定为「binutils → GCC host 部分 → newlib → GCC 库部分」。

#### 4.1.2 核心流程

每个组件的构建都遵循同一个三段式流程，由对应的 `*-script.sh` 驱动：

```text
1. 解压官方 tarball 并重命名加 -zip 后缀
   tar -xf gcc-10.3.0.tar.xz --transform s,gcc-10.3.0,gcc-10.3.0-zip,
2. 进入目录，打补丁
   cd gcc-10.3.0-zip && patch -p1 <../gcc-zippatch.patch
3. 在独立的 build-xxx 目录里 configure（out-of-tree 构建）
   ../gcc-10.3.0-zip/configure --target=zip --prefix=.../cross-tools ...
```

关键细节：

- **目标三元组就是 `zip`**。这决定了产出命令前缀 `zip-` 与库安装目录 `cross-tools/zip/`。
- **不装进系统目录**，而是装到仓库内的 `install/cross-tools`，避免污染宿主机、也免去管理员权限。
- **out-of-tree 构建**：源码目录（`gcc-10.3.0-zip/`）只读，编译产物全部进 `build-gcc/`，便于 `make clean`。

#### 4.1.3 源码精读

GCC 脚本里的「打补丁」这一步在 [sw/gcc-script.sh:55-62](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/gcc-script.sh#L55-L62)：检测补丁文件存在后进入源码目录执行 `patch -p1`，否则报错退出。这说明补丁文件是构建的硬依赖——丢了它就直接失败。

紧接着脚本把目标设为 `zip`，见 [sw/gcc-script.sh:78](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/gcc-script.sh#L78)（`CLFS_TARGET="zip"`）。随后在 [sw/gcc-script.sh:87-91](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/gcc-script.sh#L87-L91) 有一段关键断言：如果 `which zip-as` 找不到汇编器就直接退出。**这正是「GCC 依赖 binutils 先装好」的硬约束**——GCC 编译期要用 `zip-as` 把测试小程序汇编出来探测后端能力。

最终 configure 的参数见 [sw/gcc-script.sh:112-119](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/gcc-script.sh#L112-L119)：`--disable-multilib`（不要多套库变体）、`--disable-threads --disable-tls`（裸机嵌入式无线程）、`--with-newlib`（用 newlib 而非 glibc）。

补丁本身「长什么样」？以 `gcc-zippatch.patch` 为例，它新增了一整套后端文件，集中在 `gcc/config/zip/` 目录下：`zip.h`（目标宏定义，含寄存器与 ABI）、`zip.c`（后端主要 C 逻辑）、`zip.md`/`zip-di.md`/`zip-float.md`/`zip-peephole.md`（机器描述）、`genzipops.c`（生成器）、`zip-protos.h` 等；并修改 `config.sub`、`config.gcc` 让构建系统认得 `zip` 这个目标。

> 关于 nonce：[sw/README.md:12-23](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/README.md#L12-L23) 解释了 `nonce.txt` 如何标记「补丁已打」「已 configure」等阶段，出错时删掉对应 `nonce.txt` 即可从那一步重来。

#### 4.1.4 代码实践

**实践目标**：搞清三个补丁文件分别对应哪三个上游组件，并验证「GCC 依赖 binutils」。

**操作步骤**（源码阅读型，无需真的构建）：

1. 在 `sw/` 目录看三个 `*-zippatch.patch` 与三个 `*-script.sh` 的一一对应关系。
2. 打开 [sw/gcc-script.sh:87-91](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/gcc-script.sh#L87-L91)，确认 GCC 配置前必须能找到 `zip-as`。

**预期结果**：`gas-zippatch.patch → binutils`、`gcc-zippatch.patch → GCC`、`nlib-zippatch.patch → newlib`；且 GCC 无法先于 binutils 构建。若你在本地真跑构建，结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者选择「官方 tarball + 补丁」而不是维护一份完整 fork？
**答案**：跟进上游修复成本低；补丁本身是「后端改动清单」的文档；合并上游新版本时只需重新打补丁、解决冲突，而不必比对整个源码树。README 明确说这是借鉴 eco32 的做法。

**练习 2**：`--with-newlib` 这个 configure 参数如果换成 `--with-glibc` 会怎样？
**答案**：ZipCPU 是裸机软核，没有 Linux 内核、没有动态链接、没有 `syscall` 约定，glibc 这类依赖 POSIX/内核的库根本无法运行。newlib 才是面向裸机的轻量 C 库，能配合 ZipCPU 的 `_write_r`/`_exit` 等 stub 工作。

### 4.2 sw/Makefile 的构建目标与依赖顺序

#### 4.2.1 概念说明

`sw/Makefile` 是工具链构建的「总调度」。它不直接编译，而是把工作转发给各组件的 `build-xxx` 目录（用 `$(SUBMAKE) --directory=...`）。它的两个核心设计是：**nonce 驱动的阶段化**（每完成一个阶段 touch 一个 `nonce.txt`，下一阶段依赖它）和**显式的依赖链**（GCC 依赖 binutils 装好，newlib 依赖 GCC host 装好）。

#### 4.2.2 核心流程

版本与安装前缀定义在 [sw/Makefile:80-92](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L80-L92)：

- `BINUTILSD = binutils-2.27`、`GCCD = gcc-10.3.0`、`NLIBD = newlib-4.1.0`
- `INSTALLD = $(pwd)/install`（装到仓库内，不污染系统）
- `PATH` 追加 `install/cross-tools/bin`（让后续阶段能找到 `zip-as` 等）

默认目标的依赖链见 [sw/Makefile:73-77](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L73-L77)：

```text
install → basic-install → gas-install + gcc-install + newlib-install
```

三大件的构建顺序由 nonce 依赖自然强制：

```text
1. binutils:    打补丁 → configure → make → make install
                （产出 zip-as/zip-ld，PATH 就绪）
2. gcc-host:    打补丁 → 生成 zip-ops.md → configure → make all-host → install-host
                （产出 zip-gcc 本体，不含依赖 newlib 的库）
3. newlib:      打补丁 → configure → make → install
                （产出 libc/libm，依赖第 2 步的 zip-cc）
4. gcc(完整):   依赖 newlib-install → make（编译依赖 newlib 的 libgcc 等库）
```

#### 4.2.3 源码精读

**binutils 段**——打补丁与 nonce 在 [sw/Makefile:103-108](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L103-L108)：解压 `binutils-2.27.tar.bz2`、`patch -p1 <../gas-zippatch.patch`、`touch nonce.txt`。注意第 125 行 `binutils-install` 还会把仿真链接脚本 `bench/zipsim.ld` 拷进 `ldscripts` 目录，方便 `zip-ld` 默认找到。

**GCC 段**有一个独特步骤——**代码生成器**。GCC 后端的机器描述 `zip-ops.md` 不是手写的，而是由 `genzipops.c` 编译运行后生成。见 [sw/Makefile:188-192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L188-L192)：先用宿主机 `gcc` 把 `genzipops.c` 编成可执行 `genzipops`，再跑它产出 `zip-ops.md`。这个 `.md` 是后续 configure 的依赖。

GCC 被刻意拆成两半，注释见 [sw/Makefile:199-208](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L199-L208)：「host 部分（`all-host`）不依赖已编好的编译器，先编；依赖编译器的库部分（newlib 相关）放后面」。`zip-cc` 只是 `zip-gcc` 的符号链接，见 [sw/Makefile:223-224](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L223-L224)。

**newlib 段**依赖 GCC host 已装好（[sw/Makefile:266-270](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L266-L270) 打补丁，[sw/Makefile:277-281](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L277-L281) 构建）。最终完整 `gcc` 目标在 [sw/Makefile:233-238](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L233-L238)，它同时依赖 binutils 与 newlib 都已安装——这把「三大件串行」的约束写进了 Make 依赖图。

> 顺带一提：`zasm` 的构建目标在 [sw/Makefile:301-305](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L301-L305) 已被整段注释掉，故 `make` 不再构建遗留汇编器（见 4.5 节）。

#### 4.2.4 代码实践

**实践目标**：用一张依赖图说明「构建一个能用的 `zip-gcc` 到底依赖哪几个补丁文件」。

**操作步骤**：

1. 在 [sw/Makefile:182-186](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L182-L186) 找到 GCC 的补丁依赖 `gcc-zippatch.patch`。
2. 沿依赖回溯：`build-gcc/nonce.txt` 依赖 `build-gas/install-nonce.txt`（binutils 已装），而 binutils 又依赖 `gas-zippatch.patch`（[sw/Makefile:103](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L103)）。
3. 完整 `gcc` 目标（含库）还依赖 `build-nlib/install-nonce.txt`，即 `nlib-zippatch.patch`（[sw/Makefile:267](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L267)）。

**预期结果**：仅编出 `zip-gcc` 编译器本体依赖 `gcc-zippatch.patch` 一个补丁；但要得到「能编译并链接出可执行程序的完整工具链」，需要 `gas-zippatch.patch`（提供 `zip-as`/`zip-ld`）与 `nlib-zippatch.patch`（提供 C 库）共同就位——三个补丁缺一不可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GCC 要先编 `all-host`、装好之后才回头编「库部分」？
**答案**：库部分（如 libgcc 的某些运行时例程）需要用**已经编好的 `zip-gcc`** 来编译，构成「用自己编自己」的自举。但自举前必须有一个能用的编译器，所以先把不依赖目标编译器的 host 部分编出来并安装，再用它编依赖它的库。

**练习 2**：`zip-ops.md` 为什么不直接提交进仓库而要现场生成？
**答案**：它是 `genzipops.c` 按规则批量生成的产物，手写易错、改一处要同步多处。用生成器保证 `.md` 与生成逻辑一致，符合「生成物不进版本库」的工程惯例。

### 4.3 ABI：可执行格式、调用约定与栈帧

#### 4.3.1 概念说明

ABI 章节定义了「编译后的 ZipCPU 程序」与「ZipCPU 硬件 + loader」之间的契约，集中在 [doc/src/spec.tex:2362](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2362) 起的「Tool Suite and Application Binary Interface」一章。它回答四个问题：程序以什么格式存盘？函数之间怎么传参/传返回值？栈怎么用？跳转与重定位怎么做？

#### 4.3.2 核心流程

**可执行格式**：ZipCPU 用 ELF（Executable and Linkable Format），机器标识码为 `16'hdad1`（非官方注册号，未来可能变），目前**只有静态链接**、没有动态加载器（[doc/src/spec.tex:2379-2389](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2379-L2389)）。

**调用约定**（最核心）：

| 寄存器 | ABI 角色 |
| --- | --- |
| `R0` | 链接寄存器 LR：`JSR` 把返回地址存入 R0；`RET = MOV R0,PC` |
| `R1–R5` | 函数前 5 个参数；**返回值也放回 R1** |
| 第 6 个及以后 | 压栈传递 |
| `R12` | 帧指针 FP（可选） |
| `R13` | 栈指针 SP，栈向低地址生长 |
| `R14` | CC 状态寄存器 |
| `R15` | PC |

「没有原生 JSR」是 ZipCPU 的一大特色——u2 已知它只有 29 条指令，省掉了 `JSR`/`CALL`。汇编器把 `JSR addr` 展开成 `MOV #(PC),R0`（把下一条指令地址存进 R0）加一条跳转（[doc/src/spec.tex:2440-2454](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2440-L2454)）。对应的返回指令 `RET` 派生为 `MOV R0,PC`（[doc/src/spec.tex:1466-1469](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L1466-L1469)）。

**栈与栈帧**：进入函数时，编译器一次性「从 SP 减去帧大小」，然后把本函数用到的 `R5–R12` 连同 `R0` 存到栈帧的固定偏移；若用帧指针则把 `SP+偏移` MOV 进 `R12`。这个 MOV 的立即数只有 14 位有符号，故单个栈帧上限为 \(2^{12}-1 \) 字节（[doc/src/spec.tex:2391-2416](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2391-L2416)）。返回时把 FP 直接还给 SP、恢复寄存器、把 SP 加回原值、`MOV R0,PC` 返回。

**重定位**：链接器要填的两类 32 位地址——装入寄存器用 `BREV + LDILO` 两条指令承载 32 位值；长跳转用 `LW (PC),PC` 后跟一个待填的 32 位地址。条件长跳转则用 `LW.x 4(PC),PC` + `ADD 4,PC` + 地址（[doc/src/spec.tex:2417-2438](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2417-L2438)）。这正是 u2-l4 讲过的「条件 LDI 派生为 BREV+LDILO」在链接期的落点。

#### 4.3.3 源码精读

「前 5 个参数进 R1–R5、其余进栈」的规范原文在 [doc/src/spec.tex:2452-2454](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2452-L2454)。规范正文没有逐字写「返回值在 R1」，但 GCC 后端补丁把这写死成了宏：`FUNCTION_VALUE_REGNO_P(REGNO) ((REGNO)==zip_R1)` 与 `FUNCTION_VALUE(...) gen_rtx_REG(..., zip_R1)`，即整数返回值用 R1（见 `sw/gcc-zippatch.patch` 中 `gcc/config/zip/zip.h` 的 `FUNCTION_VALUE` 宏定义）。R1 同时是第一个入参寄存器和返回寄存器——调用方在 `JSR` 之后直接从 R1 取返回值。

ABI 还定义了一组**内建函数（built-ins）**，让 C 程序员能直接触达特殊指令：`zip_halt()`（`OR $SLEEP,CC`）、`zip_rtu()`（`OR $GIE,CC`，进入用户态）、`zip_syscall()`（`CLR CC`，触发 trap 回监管态）、`zip_cc()`/`zip_ucc()`（读当前/用户 CC）、`zip_save_context`/`zip_restore_context`（一次性存/取 16 个用户寄存器）等（[doc/src/spec.tex:2456-2506](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2456-L2506)）。这些内建把 u2-l5 的「双寄存器组中断模型」暴露给了 C 层。

#### 4.3.4 代码实践

**实践目标**：手算一个简单函数调用的寄存器使用。

**操作步骤**：

1. 设函数 `int add(int a, int b, int c)`，调用 `add(10, 20, 30)`。
2. 按 [doc/src/spec.tex:2452-2454](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2452-L2454) 的约定，确定 `a/b/c` 分别在哪个寄存器。
3. 确定返回值（假设为 60）落在哪个寄存器。

**预期结果**：`R1=10`、`R2=20`、`R3=30`（均在 R1–R5 内，不压栈）；返回值 60 放在 `R1`，调用方在 `JSR` 之后读 R1。

#### 4.3.5 小练习与答案

**练习 1**：如果函数有 7 个参数，第 7 个怎么传？
**答案**：前 5 个进 `R1–R5`，第 6、第 7 个压栈。调用方按调用约定把它们写进栈，被调方从栈上读取。

**练习 2**：为什么「栈帧上限 \(2^{12}-1\) 字节」？这个限制来自哪？
**答案**：来自设置帧指针的那条 `MOV SP+offset,FP`，其偏移是 14 位有符号立即数（MOV 的 Operand B），且要为寄存器保存区留余量，故单个帧不能超过约 4095 字节。超大局部数组会被编译器改用 `SP+大偏移` 的其它寻址或拆分处理。

**练习 3**：`JSR` 后面紧跟 `BRA`（先调用再跳走）为什么规范说难以优化合并？
**答案**：因为 `JSR` 已展开成「`MOV #(PC),R0` + 跳转」，紧跟的 `BRA` 又是一条跳转，两条跳转无法折叠成一条；且返回地址 R0 此时已无意义（函数不会再返回到 BRA 之后）。详见 [doc/src/spec.tex:2449-2450](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2449-L2450)。

### 4.4 链接脚本、启动流程与 CRT0/Bootloader

#### 4.4.1 概念说明

链接脚本（linker script，`*.ld`）告诉 `zip-ld`：程序里各个段（`.text`/`.data`/`.bss`...）该放到哪些内存地址上。ZipCPU 对内存布局**不做任何假设**——一片 FPGA 板可能有 flash（非易失）、block RAM（最快）、SDRAM（最大）三种存储，组合千差万别，所以内存布局是「板级特定」的，每块板子配自己的链接脚本。

#### 4.4.2 核心流程

一个 ZipCPU 链接脚本要做四件事：

```text
1. 声明内存区：  flash/blkram/sdram，各带 ORIGIN、LENGTH、权限 (r/wx)
2. 定义引导符号：_rom / _kram / _ram  ——  指向三类存储起点
3. 摆放段并定义搬运边界：
     .start  → 必须在 RESET_ADDRESS，只放最初始启动代码
     .boot   → bootloader，必须留 flash（它负责把别的段搬进 RAM）
     .text/.rodata/.data → 主程序
     _ram_image_start/_ram_image_end  ——  要从 flash 拷进 RAM 的范围
     _bss_image_end                   ——  BSS 清零的终点
4. 定义栈与堆：   _top_of_stack（SP 初值）、_top_of_heap（堆起点）
```

启动时（见 spec「Starting a ZipCPU program」）：CPU 从 `RESET_ADDRESS` 取第一条指令，那是 `.start` 段里的 `_start`（即 CRT0）。CRT0 设好 SP，调用 Bootloader；Bootloader 按 `_kram_*`/`_ram_image_*` 把代码数据从 flash 搬到 RAM、把 BSS 清零，最后跳到 `entry()`（即 C 的 `main`）。

#### 4.4.3 源码精读

**内存声明与三类存储符号**见 [doc/src/spec.tex:2526-2565](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2526-L2565)：`_rom` 是 flash 起点（程序启动前所在），`_kram` 是高速 block RAM 起点（可空），`_ram` 是普通 RAM 起点；若 `_rom` 与 `_ram` 都有定义，CRT0 会把软件从 `_rom` 拷到 `_ram`。

**`.start` 与 `.boot` 段**见 [doc/src/spec.tex:2566-2592](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2566-L2592)：`.start` 只能放最初始的启动代码并置于 `RESET_ADDRESS`；`.boot` 放 bootloader，因为它「负责把东西从 flash 搬进 RAM」，自己不能也被搬走，必须留在 flash。

**Bootloader 需要的搬运边界符号**见 [doc/src/spec.tex:2593-2642](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2593-L2642)：`_kram_start/_kram_end`（flash→block RAM 的范围）、`_ram_image_start/_ram_image_end`（flash→SDRAM 的范围）、`_bss_image_end`（BSS 清零终点）。**栈与堆符号**见 [doc/src/spec.tex:2643-2662](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2643-L2662)：`_top_of_stack`（SP 初值，通常是 RAM 末尾）与 `_top_of_heap`（`.bss` 之后的空闲起点）。

**真实链接脚本实例**——`sim/zipsw/board.ld`（仿真板，只有一块 RAM）。它声明单一区 `bkram(wx): ORIGIN=0x04000000, LENGTH=0x04000000`（[sim/zipsw/board.ld:50](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L50)），把 `_kram=0`、`_rom=0`（无 flash，程序直接载入 RAM，无需搬运）、`_ram=ORIGIN(bkram)`、`_top_of_stack=ORIGIN+LENGTH`（[sim/zipsw/board.ld:57-62](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L57-L62)）。SECTIONS 把 `.start/.boot/.text*/.data` 全放进 `bkram`，并标出 `_ram_image_end`/`_bss_image_end`/`_top_of_heap`（[sim/zipsw/board.ld:66-86](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L66-L86)）。

对比 `bench/zipsim.ld`，它声明了 **flash + sdram 两区**（[bench/zipsim.ld:44-45](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/zipsim.ld#L44-L45)）：`.start` 进 flash、主程序 `.text/.data` 用 `> sdram AT> flash`（运行在 sdram、镜像存 flash，启动时搬运），是「flash 启动 + RAM 运行」的典型布局。

**CRT0 与 Bootloader**：CRT0 即 `_start`，负责设 SP、调 Bootloader、再调 `entry()`（[doc/src/spec.tex:2700-2710](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2700-L2710)）。Bootloader 按「flash→block RAM、flash→SDRAM、BSS 清零」三步搬运，可用 DMA 加速（[doc/src/spec.tex:2718-2728](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2718-L2728)）。

#### 4.4.4 代码实践

**实践目标**：对照规范读懂仿真链接脚本 `board.ld`，画出程序布局。

**操作步骤**：

1. 打开 [sim/zipsw/board.ld:50](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L50)，记下 RAM 起止地址。
2. 读 [sim/zipsw/board.ld:57-62](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L57-L62)，确认 `_top_of_stack` 在 RAM 顶端、`_rom=0` 表示无搬运。
3. 对比 [bench/zipsim.ld:55-69](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/zipsim.ld#L55-L69)，看 `.start` 为何必须进 flash。

**预期结果**：`board.ld` 下程序整体载入 `0x04000000` 起、SP 初值在 `0x08000000`（RAM 顶端）、无 bootloader 搬运；`zipsim.ld` 下 `.start` 在 flash、主程序镜像也在 flash 但运行时搬到 sdram。这种差异正是「板级特定」的体现。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `.start` 段只能放 `_start` 这一个符号？
**答案**：因为 `.start` 必须位于 `RESET_ADDRESS`，而 RESET_ADDRESS 是 CPU 复位后取的第一条指令地址。把多个无关符号塞进来会破坏「第一条指令就是 `_start`」的保证。详见 [doc/src/spec.tex:2708-2710](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2708-L2710)。

**练习 2**：`_top_of_heap` 应该等于什么？
**答案**：等于 `.bss` 段结束之后、第一块未使用内存的地址（即 `_bss_image_end` 附近对齐后的位置），作为 `malloc` 的起点。见 [doc/src/spec.tex:2654-2659](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2654-L2659)。

### 4.5 遗留汇编器 zasm

#### 4.5.1 概念说明

`sw/zasm/` 是 ZipCPU **最早的自研汇编器**，用 flex/bison（`zasm.l`/`zasm.y`）手写。自从把 ZipCPU 后端并入 binutils、改用 GNU 汇编器 `zip-as` 之后，`zasm` 就退役了。但它没有从仓库删除，原因有二：`zasm.y` 本身就是「汇编器如何看待 ZipCPU 指令」的参考实现；更重要的是其中的 `zopcodes.cpp`/`zopcodes.h` 仍被**反汇编器/调试器**复用来打印指令（u2-l2、u2-l4 已多次引用）。

#### 4.5.2 核心流程

`zasm` 是一个经典的「词法分析 → 语法分析 → 代码生成」汇编器。`zasm.l` 做词法（识别助记符、寄存器名、数字、标号），`zasm.y` 做语法（Yacc 文法，描述指令的合法形式并生成机器码）。值得一提的是 `zasm.y` **既是语法文件也是主程序**——`int main` 直接写在其中，负责解析命令行参数（`-o` 输出、`-I` 包含路径、`-d` 反汇编等）、驱动词法语法分析、写出 ELF。

#### 4.5.3 源码精读

退役说明在 [sw/zasm/README.md:1-9](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/README.md#L1-L9)：明言「这是原始汇编器的残骸，已被 GNU 汇编器取代；除 `zopcodes` 外的源码都不再使用，而 `zopcodes` 仍在调试器里支持反汇编」。

主程序入口在 [sw/zasm/zasm.y:496](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zasm.y#L496)（`int main(int argc, char **argv)`），随后是命令行参数解析（`-o`/`-E`/`-I`/`-d`/`-h`，见 [sw/zasm/zasm.y:521-546](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/zasm.y#L521-L546)）。这与现代 `zip-as` 的命令行风格一脉相承，便于脚本兼容。

构建层面，`zasm` 已被排除在工具链之外——`sw/Makefile` 里 `zasm` 的目标被注释掉（[sw/Makefile:301-305](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L301-L305)），`clean` 目标里对 `zasm` 的清理也注释掉了（[sw/Makefile:317](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L317)）。

#### 4.5.4 代码实践

**实践目标**：理解「退役模块为何仍保留」。

**操作步骤**：

1. 读 [sw/zasm/README.md:1-9](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/zasm/README.md#L1-L9) 找出它仍被保留的唯一原因。
2. 回顾 u2-l2 用过的 `zopcodes.h` 里的 `ZIP_REGFIELD/ZIP_IMMFIELD` 宏——它们就来自这个「退役」目录。

**预期结果**：`zasm` 编译器本身不再使用，但 `zopcodes.*` 因被调试器/反汇编器复用而保留。这是「文档即代码」的另一面：旧实现是 ISA 的活参考。

#### 4.5.5 小练习与答案

**练习 1**：既然有了 GNU `zip-as`，为什么调试器还偏要用 `zasm` 里的 `zopcodes` 来反汇编？
**答案**：`zopcodes.cpp` 是一份独立的、与 GCC/binutils 解耦的指令表，自带「掩码+匹配值+字段描述符」，调试器（`zipdbg`）直接链接它即可打印指令，不必拖入整个 binutils。它是反汇编的轻量数据源。

## 5. 综合实践

把工具链、ABI、链接脚本串起来，完成下面这个「从 C 源码到可执行程序」的追踪任务。

**任务**：假设你已编出 `zip-gcc`，现要编译一个调用 `int add(int,int)` 的最小 C 程序并在仿真器里运行。请完成以下分析与配置：

1. **编译命令**：写出用 `zip-gcc`、指定 `sim/zipsw/board.ld` 为链接脚本、链接 `ziptb`/`zlib` 胶水库的命令（参考 u1-l4 的 `zip-gcc -T board.ld -lziptb` 模式）。
2. **ABI 验证**：在 [doc/src/spec.tex:2452-2454](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2452-L2454) 确认 `add` 的两个参数走 `R1`、`R2`，返回值走 `R1`；用 `zip-objdump -d`（来自 binutils 段）反汇编 `add`，核对 `JSR` 是否被展开成 `MOV #(PC),R0` + 跳转、返回处是否为 `MOV R0,PC`。
3. **链接脚本核对**：对照 [sim/zipsw/board.ld:50-62](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.ld#L50-L62) 说明该程序的 `.start`（`_start`）落在 `0x04000000`、SP 初值为 `0x08000000`、且因 `_rom=0` 而无需 bootloader 搬运。
4. **构建依赖回顾**：对照 [sw/Makefile:233-238](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L233-L238) 说明，要得到这一步用的 `zip-gcc`，`gas-zippatch.patch`、`gcc-zippatch.patch`、`nlib-zippatch.patch` 三个补丁缺一不可。

**验收标准**：能说出「C 源码 → 汇编（参数/返回值在 R1/R2）→ 链接（段布局按 board.ld）→ 加载到仿真器 RAM 跑」整条链上每一步对应的工具与规范依据。若在本地实跑，命令输出与现象待本地验证。

## 6. 本讲小结

- ZipCPU 工具链由 binutils、GCC、newlib 三大件组成，各用 `gas-zippatch.patch`/`gcc-zippatch.patch`/`nlib-zippatch.patch` 一个补丁加入后端；构建顺序是 binutils → GCC host → newlib → GCC 库。
- `sw/Makefile` 用 nonce.txt 标记阶段、用 Make 依赖强制串行；`zip-ops.md` 由 `genzipops.c` 现场生成；工具链装到仓库内 `install/cross-tools`，命令前缀 `zip-`。
- 可执行格式为 ELF，机器码 `0xdad1`，目前仅静态链接。
- ABI 约定：前 5 个参数走 `R1–R5`、其余压栈、返回值回 `R1`；`R0` 是链接寄存器，`R13` 是 SP（向低生长）、`R12` 是 FP；没有原生 `JSR`，由汇编器展开为「存返回地址到 R0 + 跳转」，`RET` 派生为 `MOV R0,PC`。
- 链接脚本板级特定：声明 flash/block RAM/SDRAM、定义 `_rom/_kram/_ram` 与 `_ram_image_*`/`_bss_image_end`/`_top_of_stack`/`_top_of_heap` 等引导符号；`.start` 段只放 `_start`、`.boot` 段留 bootloader；CRT0 设栈并调 Bootloader 把程序搬进 RAM。
- 遗留汇编器 `zasm` 已退役，但其 `zopcodes.*` 因被调试器反汇编复用而保留。

## 7. 下一步学习建议

- 学 **u5-l6 构建参数与集成选项**：把本讲的「软件构建」与硬件侧的 `OPT_*` 综合期裁剪对齐，理解软硬件协同的可配置性。
- 学 **u5-l7 把 ZipCPU 集成进自定义 SoC**：本讲的链接脚本（地址映射、`_start`、栈顶）是自定义 SoC 软件侧的直接落点，配合 `addrdecode`/`wbxbar` 完成软硬一体的最小系统。
- 继续阅读：`doc/src/spec.tex` 第 13 章剩余小节（Built-ins、Loading ZipCPU Programs），以及 `sw/gcc-zippatch.patch` 里 `gcc/config/zip/zip.h` 的寄存器与 ABI 宏定义，作为本讲 ABI 部分的「源代码级」佐证。
