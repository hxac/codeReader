# 指令追踪与反汇编

## 1. 本讲目标

在前面的讲义里，我们已经把一条 RISC-V 指令「取指 → 译码 → 执行 → 写回」的全流程拆解清楚了（见 [u3-l9](u3-l9-cpu-exec-loop.md) 与 [u5-l16](u5-l16-riscv-instruction-implementation.md)）。但只要程序一跑起来，动辄几千万条指令飞驰而过，一旦出错，你只能面对一句 `HIT BAD TRAP` 或一段 `ABORT` 诊断，根本不知道是哪条指令、哪一步把状态带偏了。

本讲就要解决这个问题。NEMU 提供了一套**指令追踪（instruction trace，简称 itrace）+ 反汇编（disassembly）**机制：每执行一条指令，就把它的地址、原始字节、人可读的汇编助记符记录下来。学完本讲，你应当能够：

1. 理解 `exec_once` 中 `logbuf` 这一行的组装格式，尤其是**大小端字节序**为何对 RISC-V 要「倒着」打印字节。
2. 掌握 `init_disasm` 用 `dlopen` 在运行时**动态加载 capstone 反汇编库**的跨平台策略，以及为什么不用普通链接。
3. 看懂 `disassemble` 如何调用 capstone 产生助记符文本。
4. 分清指令日志的**两条输出路径**：屏幕（`si` 单步时）与文件（受 trace 窗口控制），以及 `ITRACE_COND` 这个编译期注入的开关。
5. 学会用 `log_enable` 的 **trace 窗口（TRACE_START / TRACE_END）** 在海量指令中「截取」关键片段，从 `nemu-log.txt` 中精确定位出错指令。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**反汇编（disassembly）是什么？**

CPU 内部只认 0/1 组成的机器码。例如 RISC-V 的 `ebreak` 指令是一串 32 位二进制 `00000000000100000000000001110011`，写成十六进制是 `0x00100073`。反汇编就是把这串「冷冰冰的数字」翻译成人能读的 `ebreak`。反汇编不是 NEMU 自己实现的——它调用了一个第三方库 **capstone**（一个被广泛使用的轻量级多架构反汇编引擎）。

**动态加载 `dlopen` 是什么？**

普通程序用某个库时，在编译链接阶段就把库「焊死」进可执行文件。而 `dlopen` 是另一条路：程序运行起来后，再用代码临时打开一个 `.so`（Linux）或 `.dylib`（macOS）文件，从中按符号名取出函数指针来调用。好处是解耦——库可以不存在、可以晚加载、可以热替换。NEMU 用这种方式加载 capstone，这样反汇编功能可以彻底成为「可选插件」。

**大小端（endianness）是什么？**

一个多字节数存进内存时，高字节放前面还是放后面？x86、RISC-V 默认都是**小端（little-endian）**：低位字节存放在低地址。但工具链（如 `objdump`）在显示一条 RISC-V 指令时，习惯**按大端阅读顺序**（高位字节在前）来展示，例如把 `0x00100073` 显示成 `00 10 00 73`。本讲你会看到 NEMU 为此特意把字节「倒着」打印。

**trace 窗口是什么？**

一个稍大的程序可能执行几十亿条指令。如果把每条指令的日志都写盘，文件会大到无法打开。所以 NEMU 给你一个「窗口」：只记录从第 `TRACE_START` 条到第 `TRACE_END` 条指令之间的日志。调试时把窗口卡在崩溃附近，就能用很小的日志文件抓住元凶。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/cpu/cpu-exec.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c) | CPU 执行主循环；`exec_once` 在此**组装 logbuf**，`trace_and_difftest` 在此**决定是否输出** |
| [src/utils/disasm.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/disasm.c) | capstone 反汇编封装：`init_disasm` 用 `dlopen` 加载库，`disassemble` 产生助记符 |
| [src/utils/log.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/log.c) | 日志文件初始化与 `log_enable` 的 **trace 窗口**判定 |
| [include/cpu/decode.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h) | `Decode` 结构体中 `logbuf[128]` 字段的定义 |
| [include/utils.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h) | `log_write` 宏：把日志写进文件（含窗口检查） |
| [Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig) | `TRACE / TRACE_START / TRACE_END / ITRACE / ITRACE_COND` 配置项 |
| [Makefile](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile) | 把 `ITRACE_COND` 字符串编译期注入为 C 表达式 |
| [src/utils/filelist.mk](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/filelist.mk) | 关闭 ITRACE 时把 `disasm.c` 加入黑名单、不构建 capstone |

先给一张本讲的**数据流向全景图**，后续每个模块都是对它的展开：

```
exec_once(取指/译码/执行)
   │
   ├─ 组装 s->logbuf :  "0x80000000: 73 00 10 00   ebreak"
   │                    └─ pc ──┘  └─ 字节 ──┘  └ 反汇编 ─┘
   │                                          ↑
   │                              disassemble() → capstone（运行时 dlopen）
   │
   └─ trace_and_difftest(每步一次)
         │
         ├─ 路径 A（文件）: if (ITRACE_COND) log_write(logbuf)
         │                       └→ log_enable() 判窗口 [START,END] → fprintf(log_fp)
         │                       └→ log_fp 由 init_log() 打开（-l 参数 / nemu-log.txt）
         │
         └─ 路径 B（屏幕）: if (g_print_step) puts(logbuf)
                                └→ g_print_step = (n < 10)，仅 si 单步时为真
```

---

## 4. 核心概念与源码讲解

### 4.1 `exec_once` 中 `logbuf` 的组装

#### 4.1.1 概念说明

`logbuf` 是一条指令的「身份证 + 体检报告」，它把三个信息拼成一行可读文本：

1. **pc**：这条指令在客机内存中的地址。
2. **原始字节**：这条指令的机器码字节序列（十六进制）。
3. **反汇编文本**：capstone 翻译出的助记符，如 `addi a0, a0, 1`。

这一行被写进 `Decode` 结构体的 `logbuf[128]` 缓冲区——一个 128 字节的栈上字符数组，每条指令复用同一个缓冲区（详见 [u3-l10](u3-l10-fetch-and-decode-struct.md) 对 `Decode` 栈上复用的讲解）。整个组装过程只在 `CONFIG_ITRACE` 打开时才编译进来。

#### 4.1.2 核心流程

`exec_once` 在把指令执行完、`cpu.pc = s->dnpc` 提交之后，进入 logbuf 组装阶段，步骤是：

1. 写入 pc：用 `FMT_WORD` 格式化当前地址，后跟冒号。
2. 算出指令长度：`ilen = s->snpc - s->pc`（RISC-V 恒为 4，x86 为变长 1~15）。
3. 把 `s->isa.inst` 当作字节数组，**按 ISA 决定的顺序**逐字节写入十六进制。
4. 用空格把字节区填充到固定列宽（对齐反汇编列）。
5. 调用 `disassemble()` 把助记符写进缓冲区剩余部分。

#### 4.1.3 源码精读

先看缓冲区字段定义——只在开 ITRACE 时才存在，省内存：

[include/cpu/decode.h:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/cpu/decode.h#L21-L27) —— `Decode` 结构体，第 26 行用 `IFDEF(CONFIG_ITRACE, char logbuf[128]);` 条件性地加入 128 字节日志缓冲。

`FMT_WORD` 的定义在 common.h，它随 ISA 位宽自适应（riscv32 下是 `"0x%08" PRIx32`，即补零到 8 位十六进制）：

[include/common.h:L40](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L40) —— `FMT_WORD` 宏，决定 pc 与寄存器的打印宽度。

接下来是核心组装逻辑：

[src/cpu/cpu-exec.c:L48-L72](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L48-L72) —— 整段 `#ifdef CONFIG_ITRACE` 块。逐段看：

- 第 50 行写入 pc：`p += snprintf(p, sizeof(s->logbuf), FMT_WORD ":", s->pc);`，`p` 是个不断前移的写入游标。
- 第 51 行算指令长度：`int ilen = s->snpc - s->pc;`（`snpc` 是顺序下一地址，见 u3-l10）。
- 第 53 行把 `s->isa.inst` 强转为字节指针：`uint8_t *inst = (uint8_t *)&s->isa.inst;`。对 riscv，`isa.inst` 是 `uint32_t`；对 x86，它是 `uint8_t inst[16]` 字节数组。强转后统一按字节访问。

**最关键的字节序处理**在这一段：

[src/cpu/cpu-exec.c:L54-L60](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L54-L60) —— `#ifdef CONFIG_ISA_x86` 走正序 `for (i = 0; i < ilen; i++)`，其余 ISA（riscv/mips/loongarch）走**逆序** `for (i = ilen - 1; i >= 0; i--)`，每字节用 `" %02x"` 打印。

为什么 RISC-V 要倒着打？宿主机是小端，`uint32_t inst` 的字节在内存里是「低字节在前」。`objdump` 等工具显示 RISC-V 指令时，习惯把整个 32 位字按**高位在前**（大端阅读序）展示。所以要从 `inst[3]`（最高字节）打到 `inst[0]`（最低字节）。

以 `ebreak`（编码 `0x00100073`）为例，内存布局与打印结果：

```
inst 内存（小端宿主）:  inst[0]=0x73  inst[1]=0x00  inst[2]=0x10  inst[3]=0x00
逆序打印 (i=3→0):        00           10            00            73
读成 32 位字:           0x00 10 00 73  == 0x00100073  ✓ 就是 ebreak
```

而 x86 是变长指令流，工具链按**字节流原序**（低地址在前）显示，所以正序打印 `inst[0], inst[1], ...`。这正是本讲的「大小端字节序处理」要点：**同一个 `uint8_t*` 视图，RISC-V 倒序、x86 正序，目的是与各自 ISA 工具链的显示习惯一致**。

**列对齐**逻辑：

[src/cpu/cpu-exec.c:L61-L66](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L61-L66) —— `ilen_max = MUXDEF(CONFIG_ISA_x86, 8, 4)`：x86 字节区预留 8 字节宽，其余 ISA 预留 4 字节宽；`space_len = (ilen_max - ilen) * 3 + 1` 计算需要补的空格数，让反汇编列始终从同一列开始。每字节占 3 个字符（一个空格 + 两位十六进制），`+1` 是字节区与助记符之间的分隔空格。

最后调用反汇编：

[src/cpu/cpu-exec.c:L68-L70](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L68-L70) —— `disassemble(p, 剩余空间, 基址地址, 字节指针, ilen)`。注意传入的反汇编「基址地址」用 `MUXDEF(CONFIG_ISA_x86, s->snpc, s->pc)`：非 x86 传当前 `pc`，x86 传 `snpc`。该地址会被 capstone 记为这条指令的地址（用于它在内部计算相对跳转目标等），属于实现细节，本讲把焦点放在助记符输出上。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 logbuf 的格式与 RISC-V 的逆序字节打印。

**操作步骤**：

1. 确认 menuconfig 中 `Testing and Debugging → Enable tracer (TRACE)` 与 `Enable instruction tracer (ITRACE)` 都开启（默认即开）。
2. 编译运行内置镜像并把日志写进文件：`make run` 后，或在 batch 模式下 `make` 后执行 `./build/riscv32-nemu-interpreter -b -l nemu-log.txt`（`-l` 指定日志文件，见 [src/monitor/monitor.c:L85](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L85) 的 `-l` 选项解析）。
3. 打开 `nemu-log.txt`，找到一条 `ebreak` 行（内置镜像末尾就是它）。

**需要观察的现象**：一行形如

```
0x80000000: 00 00 00 00   ...
...
0x.........: 73 00 10 00   ebreak
```

注意 `ebreak` 那行的字节是 `73 00 10 00`（最低字节 `0x73` 在**最右**），印证「逆序打印」。

**预期结果**：把 `73 00 10 00` 按「高位在前」拼回，得到 `0x00100073`，正是 `ebreak` 的标准编码，验证字节序理解无误。若你的镜像首地址不是 `0x80000000`，pc 列会不同，但字节序规律一致。

> 若环境无法运行 NEMU，可改为「源码阅读型实践」：在 `cpu-exec.c` 第 53~60 行处手动模拟 `inst[0..3]` 的值，推导一条 `auipc` 指令（如 `0x00000097`）会被打印成 `97 00 00 00`，再与 capstone 给出的 `auipc ra, 0x0` 对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 x86 用正序 `for (i=0; i<ilen; i++)` 而 RISC-V 用逆序？能否统一成正序？

**参考答案**：x86 是变长指令流，工具链按字节流原序（低地址在前）展示机器码，所以正序；RISC-V 是 32 位定长指令，工具链习惯把整字按大端阅读序（高位字节在前）展示，宿主机小端存储下必须逆序才能还原这个阅读序。若 RISC-V 也正序，`ebreak` 会显示成 `73 00 10 00`（最低字节在前），与 `objdump` 不一致，可读性变差。

**练习 2**：`ilen_max` 对 x86 是 8、对 RISC-V 是 4。如果一条 x86 指令长达 9 字节，第 62~63 行的 `space_len` 会怎样？

**参考答案**：`space_len = 8 - 9 = -1`，第 63 行 `if (space_len < 0) space_len = 0;` 把它钳为 0，于是这条超长指令的字节区会「越界」挤压反汇编列，不再对齐——这是被接受的小代价，因为 x86 指令最长 15 字节而显示列只预留 8 字节宽。

---

### 4.2 `init_disasm`：用 `dlopen` 动态加载 capstone

#### 4.2.1 概念说明

capstone 是 NEMU 唯一依赖的「重型」第三方库，但它并不随 NEMU 源码一起发布——而是在你首次开启 ITRACE 构建时，由 `tools/capstone/Makefile` 从 GitHub 克隆 5.0.6 版本并编译出动态库（见 [tools/capstone/Makefile:L17-L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/capstone/Makefile#L17-L31)）。

NEMU 没有用 `#include` + 链接的常规方式使用它，而是用 `dlopen` 在运行时加载。这样做有三个好处：

1. **彻底可选**：不开 ITRACE 时，`disasm.c` 被 filelist 黑名单排除（见 4.5），capstone 根本不会被克隆和编译，开发体验干净。
2. **避免污染系统环境**：capstone 的 `.so`/`.dylib` 放在 `tools/capstone/repo/` 下，不需要安装到系统库路径、不需要配置 `rpath`。
3. **跨平台统一接口**：用同一套 `dlopen/dlsym` 代码适配 Linux 与 macOS 的库命名差异。

#### 4.2.2 核心流程

`init_disasm` 在 NEMU 启动时（`init_monitor` 末尾）被调用一次，流程为：

1. 按平台拼出正确的库文件名（`.so.5` 或 `.5.dylib`）。
2. `dlopen` 打开库，拿到句柄。
3. `dlsym` 按名字取出要用的 capstone 函数（`cs_open`、`cs_disasm`、`cs_free`，x86 还多取 `cs_option`），存进静态函数指针。
4. 根据当前 ISA 用 `MUXDEF` 级联选出 `arch` 与 `mode`，调 `cs_open_dl` 初始化一个 capstone 句柄 `handle`。
5. 若是 x86，额外把语法设为 AT&T。

#### 4.2.3 源码精读

**跨平台库名后缀**：

[src/utils/disasm.c:L20-L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/disasm.c#L20-L26) —— `CS_LIB_SUFFIX` 在 macOS（`__APPLE__`）下为 `"5.dylib"`，Linux（`__linux__`）下为 `"so.5"`，其余平台直接 `#error`。这正是本讲实践任务要分析的对象。

> 为什么是 `5`？因为 capstone 主版本号是 5（克隆的是 `5.0.6` tag）。Linux 动态库惯例是 `libfoo.so.<主版本>`，macOS 是 `libfoo.<主版本>.dylib`。两边命名规则不同，故用宏分流。

**函数指针声明**——这些就是要从 capstone 里「借」出来的函数：

[src/utils/disasm.c:L28-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/disasm.c#L28-L32) —— `cs_disasm_dl`、`cs_free_dl` 是静态函数指针；`handle` 是 capstone 的反汇编句柄。注意函数签名直接抄自 capstone 头文件，这样既能类型安全，又不需要在链接期依赖 capstone 符号。

**`init_disasm` 主体**：

[src/utils/disasm.c:L34-L68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/disasm.c#L34-L68) —— 分段看：

- 第 36 行 `dlopen("tools/capstone/repo/libcapstone." CS_LIB_SUFFIX, RTLD_LAZY)`：打开库，`RTLD_LAZY` 表示符号延迟解析。第 37 行 `assert(dl_handle)` 确保成功——失败通常是没先构建 capstone。
- 第 40 行 `cs_open_dl = dlsym(dl_handle, "cs_open");`：按字符串名取函数地址，存进局部函数指针；第 43、46 行同理取 `cs_disasm`、`cs_free`。每个 `dlsym` 后都 `assert` 非空。
- 第 49~52 行用 `MUXDEF` 级联选 `arch`：`CONFIG_ISA_x86 → CS_ARCH_X86`，`mips32 → CS_ARCH_MIPS`，`riscv → CS_ARCH_RISCV`，`loongarch32r → CS_ARCH_LOONGARCH`。
- 第 53~56 行同理选 `mode`：riscv32 是 `CS_MODE_RISCV32 | CS_MODE_RISCVC`（`RISCVC` 表示启用压缩指令解码），riscv64 是 `CS_MODE_RISCV64 | CS_MODE_RISCVC`，x86 是 `CS_MODE_32`。
- 第 57 行 `cs_open_dl(arch, mode, &handle)` 初始化句柄。
- 第 60~67 行：仅 x86 额外调 `cs_option_dl(handle, CS_OPT_SYNTAX, CS_OPT_SYNTAX_ATT)` 切到 AT&T 语法（操作数十序为「源, 目的」，寄存器带 `%` 前缀）。

**这套设计的精髓**：NEMU 源码里完全没有出现对 `cs_disasm` 等符号的**直接链接依赖**，全部走函数指针。所以即便系统里没装 capstone，只要不开 ITRACE，NEMU 照样能编译链接运行。

#### 4.2.4 代码实践

**实践目标**：理解 `dlopen` 的「软依赖」特性。

**操作步骤**：

1. 在 `src/utils/disasm.c` 第 36 行 `dlopen(...)` 后，临时加一行 `printf("loaded capstone from: tools/capstone/repo/libcapstone.%s\n", CS_LIB_SUFFIX);`（**示例代码**，仅供观察，验证后请删除）。
2. 开启 ITRACE 重新 `make`，运行内置镜像。
3. 观察启动日志里打印的库路径与后缀。

**需要观察的现象**：在 Linux 上后缀是 `so.5`，路径指向 `tools/capstone/repo/libcapstone.so.5`；若把这份代码原样拿到 macOS 上构建，后缀会自动变成 `5.dylib`。

**预期结果**：你将直观看到「NEMU 进程在运行时才把 capstone 库加载进来」，且库文件确实存在于 `tools/capstone/repo/` 下（由 `tools/capstone/Makefile` 在首次构建时克隆并编译产生）。**验证完务必删除那行 printf，本讲禁止修改源码留存。**

> 待本地验证：如果你手动 `mv tools/capstone/repo/libcapstone.so.5 /tmp/` 后再运行，预期会在 `assert(dl_handle)` 处崩溃，从而确认这是运行时软依赖而非编译期硬链接。

#### 4.2.5 小练习与答案

**练习 1**：`disasm.c` 里只 `dlsym` 了 `cs_open`、`cs_disasm`、`cs_free`（外加 x86 的 `cs_option`）。但 capstone 还有很多 API，为什么 NEMU 只取这几个？

**参考答案**：NEMU 的需求极简——开一个句柄（`cs_open`）、反汇编一段字节（`cs_disasm`）、释放结果（`cs_free`）。它不需要 capstone 的细节 API（如 `cs_insn` 的 `detail`、`cs_reg_read` 等），所以只取最小够用的三个。这也体现了 dlopen 的优势：用多少取多少，函数指针表很精简。

**练习 2**：`#include <capstone/capstone.h>` 仍在文件第 17 行。既然是动态加载，为什么还要 include 头文件？

**参考答案**：头文件提供的是**类型定义**（`csh`、`cs_insn`、`cs_arch`、`cs_mode`、`cs_err` 等）和**常量**（`CS_ARCH_X86`、`CS_ERR_OK`、`RTLD_LAZY` 来自 `<dlfcn.h>`），这些在编译期就需要。`dlopen/dlsym` 解决的是**符号链接**问题（运行期绑定函数地址），与头文件提供的类型声明是正交的两件事。`-I tools/capstone/repo/include`（见 [src/utils/filelist.mk:L20](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/filelist.mk#L20)）就是把这份头文件路径告诉编译器。

---

### 4.3 `disassemble`：调用 capstone 产生助记符

#### 4.3.1 概念说明

`disassemble` 是 NEMU 给自己留的极薄封装：接收「字节缓冲 + 长度 + 基址」，调用 capstone 的 `cs_disasm` 反汇编，把结果（助记符 + 操作数）格式化进调用方给的字符串。它在 `exec_once` 组装 logbuf 时被调用（见 4.1）。

#### 4.3.2 核心流程

1. 调 `cs_disasm_dl(handle, code, nbyte, pc, 0, &insn)` 反汇编 `nbyte` 字节。
2. `assert(count == 1)`：预期恰好产生一条指令。
3. 写入助记符 `insn->mnemonic`；若操作数字符串 `insn->op_str` 非空，追加一个制表符和操作数。
4. `cs_free_dl` 释放 capstone 分配的 `cs_insn`。

#### 4.3.3 源码精读

[src/utils/disasm.c:L70-L79](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/disasm.c#L70-L79) —— `disassemble` 函数。几个要点：

- 第 72 行 `cs_disasm_dl(handle, code, nbyte, pc, 0, &insn)`：参数依次是句柄、字节指针、字节数、反汇编基址、最大反汇编条数（`0` 表示尽可能多）、输出指令数组。这里只喂入**一条指令**的字节（`ilen`），故预期只产 1 条。
- 第 73 行 `assert(count == 1)`：如果字节数与一条指令不匹配（例如给了 3 字节却期望 RISC-V 的 4 字节指令），capstone 会返回 0，这里断言失败。这其实是 NEMU 的一种自检——只要 `ilen` 正确就不会触发。
- 第 74~77 行格式化：先写 `mnemonic`（如 `addi`），再判断 `op_str[0] != '\0'` 决定是否追加操作数（如 `\ta0, a0, 1`）。`ret` 记录已写入长度，用于计算剩余空间 `size - ret`，防止越界。
- 第 78 行 `cs_free_dl(insn, count)`：capstone 内部用 `malloc` 分配了 `cs_insn`，必须由 `cs_free` 回收，否则每条指令都泄漏。

最终这一行 `"<mnemonic>\t<op_str>"` 文本就被 `exec_once` 拼到 logbuf 的字节区后面，形成完整的一行追踪记录。

#### 4.3.4 代码实践

**实践目标**：理解「喂入一条指令的字节，恰好产出一条助记符」的契约。

**操作步骤**：

1. 阅读本函数与 4.1 中 `exec_once` 对它的调用，确认传入的 `ilen` 就是当前指令的实际长度。
2. 在 `nemu-log.txt` 中任选一行，把它的字节区手工「喂」给系统自带的反汇编工具对照：例如对一条 RISC-V 指令，用 `riscv32-unknown-elf-objdump -d` 反汇编同一段字节（若工具链可用）。

**需要观察的现象**：capstone 给出的助记符与 `objdump` 给出的应当一致（操作数格式可能略有差异，但助记符和语义相同）。

**预期结果**：例如字节 `97 00 00 00`（即 `0x00000097`），capstone 应输出 `auipc ra, 0x0`（或 `auipc x1, 0x0`），与 objdump 一致。**待本地验证**：若手头无 RISC-V 工具链，可只做源码侧的对照阅读。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `disassemble` 里要 `assert(count == 1)`，而不是 `assert(count >= 1)`？

**参考答案**：因为调用方（`exec_once`）每次只喂入**一条指令**对应的字节（长度恰为 `ilen`）。capstone 在这段字节里最多也只能译出一条指令。若 `count != 1`（为 0 说明译不出，>1 几乎不可能），都说明 `ilen` 算错或字节流异常，是 bug，应当尽早暴露而非静默继续。

**练习 2**：`insn->op_str[0] != '\0'` 这个判断处理的是哪类指令？

**参考答案**：处理**没有操作数**的指令，如 x86 的 `ret`、`cli`，或 RISC-V 的 `ebreak`、`nop`（部分表示）。这类指令 `op_str` 为空字符串，若不判断就会多写一个孤零零的 `\t`，破坏对齐。判断后只输出助记符，保持日志整洁。

---

### 4.4 `ITRACE_COND` 与指令日志的两条输出路径

#### 4.4.1 概念说明

logbuf 组装好之后，谁来「消费」它？答案在 `trace_and_difftest`——每执行一条指令调用一次的钩子函数（详见 u3-l9）。它把指令日志导向**两条相互独立的路径**：

- **路径 A：写进日志文件**，由编译期表达式 `ITRACE_COND` 控制。
- **路径 B：打印到屏幕**，由运行期变量 `g_print_step` 控制（仅 `si` 单步时为真）。

理解这两条路径的区别，是掌握「为什么 `c` 全速运行时屏幕不刷屏、但日志文件却在增长」的关键。

#### 4.4.2 核心流程

`trace_and_difftest` 每步执行：

1. 若 `ITRACE_COND` 为真：`log_write("%s\n", _this->logbuf)`（写文件，内部还受 trace 窗口约束，见 4.5）。
2. 若 `g_print_step` 为真：`puts(_this->logbuf)`（写屏幕）。
3. 若开了差分测试：`difftest_step`（本讲不展开，见 u8-l24）。

其中 `g_print_step` 在 `cpu_exec` 入口被赋值为 `(n < MAX_INST_TO_PRINT)`，`MAX_INST_TO_PRINT` 是 10。

#### 4.4.3 源码精读

**两条输出路径**：

[src/cpu/cpu-exec.c:L35-L41](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L35-L41) —— `trace_and_difftest` 函数。

- 第 36~38 行：`#ifdef CONFIG_ITRACE_COND` 包裹路径 A。`if (ITRACE_COND) { log_write("%s\n", _this->logbuf); }`。`ITRACE_COND` 是一个**编译期注入的表达式**（见下文 Makefile），默认为 `true`，但你可以在 menuconfig 里改成任意 C 表达式（如 `g_nr_guest_inst > 1000`）来精细控制「哪些指令才记日志」。
- 第 39 行：路径 B，`if (g_print_step) { IFDEF(CONFIG_ITRACE, puts(_this->logbuf)); }`。`puts` 直接写 stdout。

`MAX_INST_TO_PRINT` 与 `g_print_step` 的定义：

[src/cpu/cpu-exec.c:L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L26) —— `#define MAX_INST_TO_PRINT 10`，注释明确：仅当执行指令数小于此值时才把汇编输出到屏幕，专供 `si` 命令使用，可按需修改。

[src/cpu/cpu-exec.c:L101](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L101) —— `g_print_step = (n < MAX_INST_TO_PRINT);`。`c` 命令传 `n = (uint64_t)-1`（一个极大的数），故 `g_print_step` 为假，屏幕安静；`si 5` 传 `n = 5 < 10`，为真，屏幕逐条打印。

**`ITRACE_COND` 是怎么从 menuconfig 字符串变成 C 表达式的**：

[Makefile:L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Makefile#L51) —— `CFLAGS_TRACE += -DITRACE_COND=$(if $(CONFIG_ITRACE_COND),$(call remove_quote,$(CONFIG_ITRACE_COND)),true)`。`CONFIG_ITRACE_COND` 是 menuconfig 里的字符串值（默认 `"true"`），`remove_quote` 去掉引号后，通过 `-DITRACE_COND=...` 直接作为宏定义注入。于是源码里的 `ITRACE_COND` 会被替换成你填的表达式。这是一处很巧的「配置即代码」——把调试条件做成编译期常量，运行期零开销。

**`log_write` 宏**——路径 A 的真正落点：

[include/utils.h:L59-L68](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/utils.h#L59-L68) —— `log_write` 用 `IFDEF(CONFIG_TARGET_NATIVE_ELF, ...)` 包裹，内部还检查 `log_enable()`（trace 窗口，见 4.5）和 `log_fp != NULL`，都满足才 `fprintf(log_fp, ...)` 并 `fflush`。所以路径 A 实际有三道闸门：`ITRACE_COND`、`log_enable()`、`log_fp`。

> 也就是说：**屏幕路径（B）只受 `g_print_step` 控制，与 trace 窗口无关**；而**文件路径（A）同时受 `ITRACE_COND` 与 trace 窗口约束**。这就是为什么 `si` 单步时哪怕窗口外也能在屏幕看到指令，但日志文件只记录窗口内的指令。

#### 4.4.4 代码实践

**实践目标**：亲手验证两条路径的独立性。

**操作步骤**：

1. menuconfig 里把 `ITRACE_COND` 设为字符串 `g_nr_guest_inst < 3`（表示只追踪前 3 条指令），保存退出会自动重新生成 autoconf.h。
2. `make`，然后 `./build/riscv32-nemu-interpreter -b -l nemu-log.txt`（batch 模式 + 日志文件）。
3. 分别看屏幕与 `nemu-log.txt`。

**需要观察的现象**：

- 屏幕：因为 batch 模式直接 `c` 全速跑，`g_print_step` 为假，**屏幕几乎看不到逐条指令**（只有最终 TRAP 信息）。
- 日志文件：受 `ITRACE_COND`（`g_nr_guest_inst < 3`）约束，**只记录前 3 条指令**（注意还会叠加 trace 窗口 `log_enable()`，见 4.5）。

**预期结果**：你会清楚看到「屏幕安静、日志文件只有寥寥几行」，从而理解两条路径相互独立、各自受不同条件控制。**验证后请把 `ITRACE_COND` 改回 `true`。**

#### 4.4.5 小练习与答案

**练习 1**：为什么 `c` 全速运行时屏幕不刷屏，而 `si` 会逐条打印？

**参考答案**：`c` 传 `n = -1`（极大），`g_print_step = (n < 10)` 为假，路径 B（`puts`）被关闭；`si` 传小 `n`，`g_print_step` 为真，路径 B 开启。这是为了避免全速运行时上亿条指令把终端冲爆。

**练习 2**：路径 A 为什么还要再套一层 `log_enable()` 检查，而不是只要 `ITRACE_COND` 为真就写？

**参考答案**：`ITRACE_COND` 是**编译期**注入的、面向「程序整个生命周期」的条件（通常固定或基于全局计数），而 `log_enable()` 是**运行期**的 trace 窗口判定（基于 `g_nr_guest_inst` 是否落在 `[START, END]`）。两者职责不同：前者决定「要不要这个特性级别的过滤」，后者决定「此刻这条指令在不在关注区间」。叠加使用才能既灵活又精细地控制日志量。

---

### 4.5 `log_enable` 与 trace 窗口（TRACE_START / TRACE_END）

#### 4.5.1 概念说明

前面多次提到 trace 窗口。它就是路径 A（`log_write` 写文件）的第二道闸门：只有当全局已执行指令计数 `g_nr_guest_inst` 落在 `[TRACE_START, TRACE_END]` 区间内，`log_enable()` 才返回真，日志才真正落盘。

这是一个「在海量指令里截取片段」的机制。想象一个跑了几十亿指令才崩溃的程序——你不可能记录全部，但你多半知道「崩溃发生在很靠后的某段」。把窗口卡在那一段，就能用可控大小的日志文件抓住现场。

#### 4.5.2 核心流程

1. `execute` 每执行一条指令后 `g_nr_guest_inst++`（客机指令计数的**唯一累加点**，见 u3-l9）。
2. `log_write` 内部调 `log_enable()`，它判断 `g_nr_guest_inst >= CONFIG_TRACE_START && g_nr_guest_inst <= CONFIG_TRACE_END`。
3. 为真才 `fprintf(log_fp, ...)`。

`init_log` 负责 `log_fp` 的打开：若命令行给了 `-l FILE`，就打开该文件（覆盖写）；否则 `log_fp = stdout`。

#### 4.5.3 源码精读

**`log_enable` 的窗口判定**：

[src/utils/log.c:L33-L36](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/log.c#L33-L36) —— `return MUXDEF(CONFIG_TRACE, (g_nr_guest_inst >= CONFIG_TRACE_START) && (g_nr_guest_inst <= CONFIG_TRACE_END), false);`。关 TRACE 时恒返回 `false`（整段日志写文件路径失效）；开 TRACE 时返回计数是否落在窗口内。`CONFIG_TRACE_START`/`END` 是 Kconfig 的 `int`，默认 `0` 与 `10000`。

**`init_log` 打开日志文件**：

[src/utils/log.c:L23-L31](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/log.c#L23-L31) —— 默认 `log_fp = stdout`；若 `log_file != NULL`（命令行传了 `-l`），则 `fopen(log_file, "w")` 覆盖打开并指向它。`Assert(fp, ...)` 确保打开成功。

**三个 Kconfig 项的依赖关系**：

[Kconfig:L130-L152](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L130-L152) —— 配置项簇：

- `TRACE`（L130）：总开关，默认 `y`。
- `TRACE_START`（L134）、`TRACE_END`（L139）：`depends on TRACE`，类型 `int`，默认 `0` 与 `10000`，注释写明单位是「指令数」。
- `ITRACE`（L144）：`depends on TRACE && TARGET_NATIVE_ELF && ENGINE_INTERPRETER`——即只在 Native ELF 目标、解释器引擎下可用，AM 目标或 JIT 引擎下不可用。默认 `y`。
- `ITRACE_COND`（L149）：`depends on ITRACE`，类型 `string`，默认 `"true"`。

注意整段 `log.c` 用 `#ifndef CONFIG_TARGET_AM` 包裹（[L20, L37](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/log.c#L20-L37)）：AM 目标下没有日志文件这一套（AM 自带 `ioe_init` 等机制，见 u6-l20 对 AM 模式的说明），所以 `init_log`/`log_enable` 在 AM 下根本不编译。

**关闭 ITRACE 时的「连根拔起」**：

[src/utils/filelist.mk:L16-L24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/filelist.mk#L16-L24) —— `ifeq ($(CONFIG_ITRACE)$(CONFIG_IQUEUE),)` 判断 ITRACE 与 IQUEUE 都关；是则把 `src/utils/disasm.c` 加入 `SRCS-BLACKLIST-y`（不编译），否则定义 `LIBCAPSTONE` 依赖、加 `-I tools/capstone/repo/include`、并让 `disasm.c` 依赖 capstone 库目标（触发 `tools/capstone` 的克隆与编译）。这就是「不开 ITRACE 就完全不碰 capstone」的实现。

#### 4.5.4 代码实践

**实践目标**：用 trace 窗口精确定位内置镜像末尾的 `ebreak`（即触发 `HIT GOOD TRAP` 的那条指令）。

**操作步骤**：

1. menuconfig 里确认 `TRACE = y`、`ITRACE = y`。内置镜像只有寥寥几条指令，故默认 `TRACE_START=0 / TRACE_END=10000` 已能覆盖全部，无需改窗口。
2. `make`，运行 `./build/riscv32-nemu-interpreter -b -l nemu-log.txt`（`-b` batch 模式跑完，`-l` 写日志）。
3. 在 `nemu-log.txt` 末尾查找 `ebreak`。

**需要观察的现象**：日志文件末尾几行是内置镜像的指令序列，最后一条是 `... 73 00 10 00   ebreak`，它正是触发 `HIT GOOD TRAP` 的指令（`ebreak` 复用为 `nemu_trap`，返回码取自 `a0`，见 u5-l16）。

**预期结果**：你能从日志里读出完整的执行轨迹，并指认末尾的 `ebreak` 是停机指令。对真实出错的程序，方法相同——把 `TRACE_START/END` 卡在崩溃前后，再从日志里逐条比对状态。

> 若要演示「窗口截断」，可把 `TRACE_END` 改成 `2`，重新编译运行，观察 `nemu-log.txt` 只剩前 2 条指令——这模拟了「海量指令中只截取开头片段」的场景。

#### 4.5.5 小练习与答案

**练习 1**：`g_nr_guest_inst` 在哪里自增？为什么 `log_enable` 能用它做窗口判定？

**参考答案**：在 `execute` 循环里 `exec_once` 之后、`trace_and_difftest` 之前 `g_nr_guest_inst ++`（[cpu-exec.c:L78](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/cpu-exec.c#L78)，这是全局唯一累加点，见 u3-l9）。它是「已执行客机指令数」的全局真值，因此 `log_enable` 用它与 `[START, END]` 比较就能精确表达「第几条到第几条才记录」。

**练习 2**：如果把 `TRACE_END` 设得比 `TRACE_START` 还小（如 `START=100, END=50`），会发生什么？

**参考答案**：`log_enable()` 返回 `(g_nr_guest_inst >= 100) && (g_nr_guest_inst <= 50)`，对任何计数都恒为假（空区间），于是日志文件除了 `Log()` 打印的零散信息外，**没有任何指令追踪行**。这是合法但无意义的配置，NEMU 不会报错，只是日志为空——提醒我们设窗口时要确保 `START <= END`。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次「端到端的指令追踪调试」。

**任务背景**：内置镜像本身会 `HIT GOOD TRAP`，没有「错」。我们人为制造一个需要排查的场景——通过 trace 窗口与日志，**完整还原镜像的执行轨迹并定位停机指令**，同时验证 capstone 的跨平台加载策略。

**步骤**：

1. **配置**：`make menuconfig` 确认 `Testing and Debugging → Enable tracer (TRACE)` 与 `Enable instruction tracer (ITRACE)` 均为 `y`；记录当前 `TRACE_START / TRACE_END` 的值（默认 `0 / 10000`）。
2. **构建与运行**：`make`，然后 `./build/riscv32-nemu-interpreter -b -l nemu-log.txt`。这里：
   - `-b` 进入 batch 模式，等价于自动执行 `c`（全速跑到停）。
   - `-l nemu-log.txt` 把日志写到该文件（对应 [src/monitor/monitor.c:L85](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L85) 的 `-l` 解析与 `init_log` 的 `fopen`）。
3. **观察日志**：打开 `nemu-log.txt`，验证每行格式为 `pc: 字节 反汇编`（对应 4.1）。注意 RISC-V 字节是「高位在前」的逆序打印。
4. **定位停机指令**：在日志末尾找到 `ebreak`（对应 4.5）。它就是触发 `HIT GOOD TRAP` 的指令。把它前面的几条指令串起来读，对照 [u5-l16](u5-l16-riscv-instruction-implementation.md) 讲的内置镜像（`auipc → sb → lbu → ebreak`），确认执行轨迹合理。
5. **验证窗口**：把 `TRACE_END` 改成一个很小的值（如 `2`），重新编译运行，确认日志只剩前 2 条指令（对应 4.5 的窗口截断）。改回 `10000`。
6. **分析跨平台加载**：阅读 [src/utils/disasm.c:L20-L26](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/disasm.c#L20-L26) 与 [tools/capstone/Makefile:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/tools/capstone/Makefile#L21-L27)。回答：为什么 `CS_LIB_SUFFIX` 在 macOS 是 `5.dylib` 而在 Linux 是 `so.5`？两处（disasm.c 的宏、capstone Makefile 的 `suffix`）是否一致？为什么不直接 `#include` + 链接 capstone？

**预期产出**：

- 一份能解读的 `nemu-log.txt`，能指出停机指令及其字节编码。
- 一段对 `CS_LIB_SUFFIX` 跨平台分流的文字解释：capstone 5.x 在 Linux 产出 `libcapstone.so.5`，在 macOS 产出 `libcapstone.5.dylib`，命名惯例不同故需分流；`dlopen` 让 NEMU 对 capstone 形成「运行时软依赖」，从而能彻底可选（关 ITRACE 时不克隆、不编译、不链接），且不污染系统库路径。disasm.c 的宏与 capstone Makefile 的 `suffix` 选择逻辑一致，互相对应。

> 待本地验证：若运行环境无 capstone 或无法克隆，步骤 1~5 可降级为「源码阅读型实践」——在 `cpu-exec.c`、`disasm.c`、`log.c` 之间手工走一遍数据流，画出一条指令从执行到落盘的完整调用链。

---

## 6. 本讲小结

- **logbuf 是指令追踪的核心数据结构**：`exec_once` 把 `pc : 字节 : 反汇编` 拼进 `Decode.logbuf[128]`，其中 RISC-V 字节**逆序**打印、x86 **正序**打印，目的是与各自工具链的显示习惯（大小端阅读序）一致。
- **反汇编靠 capstone**：`disassemble` 是极薄封装，喂入一条指令的字节、`assert(count==1)` 后输出 `助记符\t操作数`。
- **capstone 用 `dlopen` 动态加载**：`init_disasm` 在运行时打开 `libcapstone.<后缀>`，用 `dlsym` 取函数指针调用，从而形成「运行时软依赖」——关 ITRACE 时 `disasm.c` 被 filelist 黑名单排除，capstone 完全不参与构建。
- **跨平台靠 `CS_LIB_SUFFIX`**：macOS `5.dylib`、Linux `so.5`，与 `tools/capstone/Makefile` 的 `suffix` 逻辑一致，对应 capstone 5.x 在两平台的库命名差异。
- **日志有两条独立路径**：屏幕路径受 `g_print_step`（`n < MAX_INST_TO_PRINT`，仅 `si` 时为真）控制；文件路径受编译期 `ITRACE_COND`（Makefile `-D` 注入）+ 运行期 `log_enable()` trace 窗口 + `log_fp` 三重控制。
- **trace 窗口控制日志量**：`log_enable` 判断 `g_nr_guest_inst` 是否落在 `[TRACE_START, TRACE_END]`，让你在海量指令中截取关键片段，从 `nemu-log.txt` 精确定位出错指令。

---

## 7. 下一步学习建议

本讲把「单条指令如何被记录与反汇编」讲透了。接下来建议：

1. **学差分测试（u8-l24）**：trace 是「自我记录」，difftest 是「与可信模型对照」。两者常配合使用——日志告诉你「哪条指令开始偏了」，difftest 告诉你「寄存器在哪一步不一致」。阅读 [src/cpu/difftest/](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/cpu/difftest) 下的 DUT/REF 实现。
2. **扩展 trace 家族**：本仓库还有 `CONFIG_IQUEUE`（指令队列，与 ITRACE 共同决定是否构建 capstone，见 [src/utils/filelist.mk:L16](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/utils/filelist.mk#L16)）、内存追踪 mtrace、函数调用追踪 ftrace 等同类机制。建议对照 Kconfig 中 `Testing and Debugging` 菜单，思考它们能否复用本讲的「编译期条件 + 运行期窗口」双闸门模式。
3. **阅读宏工程化（u8-l26）**：本讲反复出现的 `MUXDEF`、`IFDEF`、`IFDEF(CONFIG_ITRACE, char logbuf[128])` 都来自 `include/macro.h`。理解这些条件编译宏后，你会更清楚地看到 NEMU 如何用一套源码适配多 ISA/多目标。

> 提示：调试 PA 时，本讲的 trace 窗口与 `nemu-log.txt` 是你最常用的排错工具之一。养成「先开 ITRACE + 设窗口、再看日志、再定位」的习惯，能极大提升排错效率。
