# 编写并编译一个 CoralNPU C++ 程序

## 1. 本讲目标

上一讲（[u2-l1](u2-l1-toolchain-linker-tcm.md)）我们搞清楚了「CoralNPU 裸机程序由谁组装、怎么点亮」——工具链、CRT、TCM 链接脚本。本讲把视角从「底层启动」上移到「写一个真正能跑的程序」：

- 掌握 CoralNPU 程序的**典型三段式结构**：输入缓冲、输出缓冲、计算主体。
- 理解 `__attribute__((section(".data")))` 这个看似不起眼的注解，是如何把变量「钉」进 DTCM、并让外部主机能找到它的。
- 学会用项目自定义的 `coralnpu_v2_binary` 规则，把一段 C++ 编译成可在内核上运行的 `.elf/.bin/.vmem` 三件套。
- 动手写一个对 8 个 `int32` 逐元素相乘的程序，编译后用 `readelf` 验证 `.data` 确实落在 DTCM 地址区间。

学完后，你应当能独立「写一个 CoralNPU 程序 → 编译 → 检查产物」，为下一讲（[u2-l3](u2-l3-run-on-verilator.md) 在 Verilator 上运行）准备好一个可以加载的 `.elf`。

## 2. 前置知识

在开始前，请确认你理解下面几个概念（不熟悉的话先回看 u2-l1）：

- **TCM（Tightly-Coupled Memory，紧耦合存储）**：CoralNPU 标量核自带的单周期 SRAM，分为 **ITCM**（放代码，默认 8KB，起址 `0x0`）和 **DTCM**（放数据，默认 32KB，起址 `0x00010000`）。它和 Cache 的区别是：地址固定、时序确定、不会被换出。
- **裸机程序（bare-metal）**：没有操作系统、没有 libc 启动代码（`-nostdlib`），程序直接从复位地址开始执行。CoralNPU 用项目自带的 CRT 提供 `_start`。
- **host 与 NPU 的关系**：CoralNPU 不是一个独立运行的主控 CPU，而是挂在某个主机 SoC 上的「加速器 IP」。典型用法是：主机把输入数据写进 CoralNPU 的 DTCM，启动它，等它算完停机，再读回 DTCM 里的结果。
- **GCC section 属性**：`__attribute__((section("名字")))` 是 GCC/Clang 的扩展，用来强制把一个变量（或函数）放进指定名字的段里，而不是编译器默认选择的段。
- **ELF 段（section）与地址**：`.text`、`.data`、`.bss` 是 ELF 里最常见的段。链接脚本（linker script）决定每个段最终落在哪个内存地址。

> 一个常见的疑问：既然 DTCM 也是内存，为什么变量不能「随便放」？因为 CoralNPU 的代码必须在 ITCM、数据必须在 DTCM，放错地方（比如代码进了 DTCM、或者变量进了根本不存在的地址）内核就会取不到指令或访存失败。链接脚本是「地址分配的宪法」，而本讲的 `section` 属性是「告诉宪法把这个变量分到 DTCM」。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [examples/hello_world_add_floats.cc](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/hello_world_add_floats.cc) | 一个最小的 CoralNPU 程序：两个 float 输入缓冲相加写回输出。本讲「程序结构」的主角。 |
| [examples/BUILD.bazel](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/BUILD.bazel) | 用 `coralnpu_v2_binary` 规则声明编译目标的示例 BUILD 文件。 |
| [doc/tutorials/writing_coralnpu_programs.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md) | 官方「如何写 CoralNPU 程序」教程，讲解结构、缓冲、并接上 cocotb 测试台。 |
| [rules/coralnpu_v2.bzl](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl) | 自定义 Bazel 规则 `coralnpu_v2_binary` 的实现：平台切换、链接脚本生成、产物三件套。 |
| [toolchain/coralnpu_tcm.ld.tpl](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/toolchain/coralnpu_tcm.ld.tpl) | TCM 链接脚本模板，决定 `.data` 落在 DTCM（本讲用于解释地址来源，详细讲解见 u2-l1）。 |
| [rules/linker.bzl](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/linker.bzl) | 把模板替换成最终 `.ld` 的规则，内含 DTCM 默认起址 `0x00010000`。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **程序结构**：输入/输出缓冲 + 计算主体，以及 host↔NPU↔DTCM 的数据往返。
2. **`section(".data")` 与 DTCM 布局**：为什么要把变量钉进 `.data`，它如何进入 DTCM。
3. **`coralnpu_v2_binary` 构建规则**：一行 BUILD 怎么变成可加载的 `.elf`。

---

### 4.1 CoralNPU 程序的典型结构

#### 4.1.1 概念说明

官方教程把 CoralNPU 程序的结构归纳为三部分（[writing_coralnpu_programs.md:30-38](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md#L30-L38)）：

1. **输入缓冲（input buffers）**：存放计算输入。本讲约定由主机在程序运行前把数据写进 CoralNPU 的 DTCM。
2. **输出缓冲（output buffers）**：存放计算结果。CoralNPU 把结果写进自己 DTCM 里的某个位置，主机在程序结束后读回。
3. **计算主体（computation）**：在 `main()` 里真正要做的运算。

这和我们平时写的「命令行 C++ 程序」有一个根本不同：**CoralNPU 程序不通过 `scanf`/`cin` 读输入、不通过 `printf` 打印输出**。它的 I/O 模型是「**共享 DTCM 内存**」——输入输出都是预先放在固定内存位置的全局缓冲区，主机通过 AXI 总线读写这些地址来「喂」输入和「取」结果。这也是为什么缓冲区要声明成**全局变量**：它们需要在程序启动前就存在、并且有固定可查的地址。

#### 4.1.2 核心流程

一次完整的「主机驱动 CoralNPU 计算」的数据流如下（接口细节由 cocotb 测试台提供，会在 [u2-l4](u2-l4-cocotb-testbench-intro.md) 详讲，这里只看宏观）：

```
[主机 Host]                     [CoralNPU 标量核]              [DTCM / ITCM]
    |                                |                             |
    |  1. write input1/input2 -----+------------------------------->| (写入 DTCM 缓冲)
    |  2. load_elf(program) -------+------------------------------->| (把 .text 装入 ITCM)
    |  3. execute_from(PC) ------->|  从 _start 进入 main()         |
    |                              |     for i: out = in1 + in2     | (读输入、写输出)
    |  4. wait_for_halted <--------|  return 0 → 内核 halt          |
    |  5. read output <-----------+-------------------------------| (读回 DTCM 缓冲)
```

关键点：

- 输入/输出缓冲的**地址由链接脚本分配**，写程序时不需要手填地址，cocotb 用 `lookup_symbol` 按符号名查出来（[writing_coralnpu_programs.md:59-61](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md#L59-L61)）。
- 内核在 `main` 返回后会停机（halt），主机靠 `wait_for_halted` 等待计算完成（[writing_coralnpu_programs.md:81](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md#L81)）。停机机制由上一讲提到的 CRT 控制（成功则 `mpause`，失败 `ebreak`）。

#### 4.1.3 源码精读

`hello_world_add_floats.cc` 是这套结构的「最小完整范例」：

[examples/hello_world_add_floats.cc:15-26](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/hello_world_add_floats.cc#L15-L26) —— 整个程序就这些：

```cpp
#include <string.h>

float input1[8] __attribute__((section(".data")));
float input2[8] __attribute__((section(".data")));
float output[8] __attribute__((section(".data")));

int main() {
  for (int i = 0; i < 8; i++) {
    output[i] = input1[i] + input2[i];
  }
  return 0;
}
```

逐行对照三段式结构：

- **输入缓冲**：`input1[8]` 和 `input2[8]`，各 8 个 `float`，对应「两个输入缓冲」。
- **输出缓冲**：`output[8]`，8 个 `float`，对应「一个输出缓冲」。
- **计算主体**：`main()` 里的 `for` 循环做逐元素加法 `output[i] = input1[i] + input2[i];`。

注意两点细节（对应真实源码，不粉饰）：

1. `#include <string.h>`（[L15](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/hello_world_add_floats.cc#L15)）实际上在本文件里**没有被使用**——程序里没有任何字符串操作。它是一个遗留包含，去掉也不影响编译。读源码时遇到这种「多余的 include」很正常，不要被它误导。
2. 这里的缓冲用 `float` 而官方教程骨架用 `uint32_t`（[writing_coralnpu_programs.md:47-49](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md#L47-L49)）。两者都合法——CoralNPU 支持 `rv32imf`，整数和单精度浮点都能算。`float` 加法正好也验证了上一讲提到的「CRT 会置 `mstatus.FS` 启用浮点」这一前提。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把「三段式结构」与「数据往返」对上号。

**步骤**：

1. 打开 [examples/hello_world_add_floats.cc](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/hello_world_add_floats.cc)，标出哪几行是输入缓冲、哪几行是输出缓冲、哪几行是计算主体。
2. 打开 [writing_coralnpu_programs.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md)，找到主机「写输入 → 启动 → 等停机 → 读输出」对应的 cocotb 调用：`write`、`execute_from`、`wait_for_halted`、`read`。
3. 画一张表：左列是「数据往返的 5 个阶段」，右列填入对应的 cocotb 接口名和它操作的内存（ITCM 还是 DTCM）。

**预期结果**：你能清晰地说明 `hello_world_add_floats` 的三个全局数组分别扮演输入/输出，以及主机何时写、何时读。cocotb 接口的精确签名留到 [u2-l4](u2-l4-cocotb-testbench-intro.md) 验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `main` 里的循环改成 `output[i] = input1[i] - input2[i];`，三段式结构的哪一部分发生了变化？

> **答案**：只有「计算主体」变了，输入/输出缓冲的声明完全不变。这正说明三段式的好处——换算法只改 `main` 体，缓冲布局不动。

**练习 2**：为什么输入/输出缓冲要声明成**全局变量**，而不是 `main` 里的局部变量？

> **答案**：局部变量分配在栈上，地址随调用栈变化、且生命周期只在函数内；而主机需要在程序运行**之前**写输入、在程序运行**之后**读输出，缓冲必须在整个程序生命周期内占据固定、可被外部按符号查到的地址。全局变量（配合链接脚本分配）正好满足这一点。

---

### 4.2 `__attribute__((section(".data")))` 与 DTCM 内存布局

#### 4.2.1 概念说明

每个缓冲后面的 `__attribute__((section(".data")))` 是本讲最关键的一行注解。官方教程一句话点明：它「定义缓冲被存放在 data 段」（[writing_coralnpu_programs.md:44](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md#L44)）。

为什么不能省？因为这三个缓冲**都没有初值**。在 C/C++ 里，没有初值的全局变量默认会被编译器放进 **`.bss`** 段（零初始化段），而不是 `.data`。问题在于：

- 在本项目的链接脚本里，`.bss` 被声明为 `NOLOAD`（不加载段，[coralnpu_tcm.ld.tpl:109-118](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/toolchain/coralnpu_tcm.ld.tpl#L109-L118)），它不会作为「带文件内容的加载段」出现在 ELF 的内存镜像里。
- 而 `load_elf` 的工作方式是「把所有**可加载段**搬进内存」。我们希望输入/输出缓冲成为内存镜像里**确定存在、可被主机按地址覆写**的区域。

显式写 `section(".data")` 就是**强制覆盖编译器的默认选择**：把这些未初始化的全局变量直接钉进 `.data` 段（一个 PROGBITS、可加载、有固定地址的段），从而保证它们进入 DTCM、并被 ELF 加载器识别。

> 小贴士：这个属性**只决定「进哪个段」**，至于那个段最终落在哪块内存，是链接脚本的职责。两步分工：GCC 负责「分段」，链接脚本负责「段→地址」。

#### 4.2.2 核心流程

一个带 `section(".data")` 的变量，从源码到 DTCM 地址的流程：

```
源码: float input1[8] __attribute__((section(".data")));
   │  GCC 看到显式 section 属性
   ▼
ELF 段: input1 被放进 .data 段（而非默认的 .bss）
   │  链接器读取 coralnpu_tcm.ld.tpl 生成的 .ld
   ▼
地址分配: .data 段被 "> DTCM" 规则放入 DTCM 区间
   │  DTCM 起址 = 0x00010000（由 linker.bzl 默认值决定）
   ▼
最终: input1 落在 0x0001xxxx，成为 ELF 内存镜像的一部分
```

DTCM 的地址区间由两个默认值决定（[rules/linker.bzl:24-30](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/linker.bzl#L24-L30)）：

- DTCM 起址 `0x00010000`（`dtcm_origin_default`）
- DTCM 长度 32KB（`dtcm_size_kbytes_default = 32`）

所以默认配置下，**DTCM 地址区间是 \[0x00010000, 0x00018000)**，任何 `.data` 变量都应落在这个范围内。

#### 4.2.3 源码精读

**第一步：链接脚本把 `.data` 放进 DTCM。**

[toolchain/coralnpu_tcm.ld.tpl:81-107](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/toolchain/coralnpu_tcm.ld.tpl#L81-L107) —— `.data` 段定义，结尾的 `> DTCM` 是把段放进 DTCM 内存区的关键：

```ld
.data : ALIGN(16) {
    __data_start__ = .;
    ...
    *(.sdata)
    *(.sdata.*)
    *(.data)        /* ← 我们 section(".data") 的变量汇集到这里 */
    *(.data.*)
    . = ALIGN(4);
    _ret = .;       /* ← main 返回值的存放位置 */
    . += 4;
    . = ALIGN(16);
    __data_end__ = .;
} > DTCM            /* ← 整个 .data 段进 DTCM */
```

两个要点：

1. `*(.data)` 这一通配会把所有「显式指定进 `.data` 段」的输入节（即我们三个缓冲对应的节）都收集起来，统一放进 DTCM。
2. 段内还预留了 `_ret`（[L101-103](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/toolchain/coralnpu_tcm.ld.tpl#L101-L103)）：4 字节，用来存 `main` 的返回值，注释说「可被系统里的另一个核（即主机）检查」。这正好呼应上一讲 CRT 的「主机观测内核状态」机制——程序算完后，主机不仅能读 DTCM 里的输出缓冲，还能读 `_ret` 判断成功与否。

**第二步：DTCM 起址从哪来。**

[rules/linker.bzl:24-30](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/linker.bzl#L24-L30) —— 模板替换逻辑：

```python
itcm_size_kbytes_default = 8
dtcm_size_kbytes_default = 32
dtcm_origin_default = "0x00010000"          # ← 默认 DTCM 起址
dtcm_origin_highmem = "0x00100000"
dtcm_origin = dtcm_origin_default
if ctx.attr.itcm_size_kbytes != ... or ctx.attr.dtcm_size_kbytes != ...:
    dtcm_origin = dtcm_origin_highmem        # 改了大小就换到高地址区
```

也就是说，只要用默认的 8KB/32KB，DTCM 起址就是 `0x00010000`；一旦你改了 `itcm_size_kbytes` 或 `dtcm_size_kbytes`，起址会跳到 `0x00100000`。这一点在后面实践里验证 `.data` 地址时要特别留意——**用默认参数构建，`.data` 才在 `0x0001xxxx`**。

#### 4.2.4 代码实践（验证型）

**目标**：用 `readelf` 亲眼确认 `hello_world_add_floats` 的 `.data` 段落在 DTCM \[0x00010000, 0x00018000) 区间内。

**步骤**：

1. 先按默认参数构建示例（构建规则的细节下一节讲，这里先用）：
   ```bash
   bazel build //examples:coralnpu_v2_hello_world_add_floats
   ```
2. 查看 ELF 的段表（`-S` 列出 section headers，`readelf` 解析任何架构的 ELF，不需要交叉工具链）：
   ```bash
   readelf -S bazel-bin/examples/coralnpu_v2_hello_world_add_floats.elf
   ```
3. 在输出里找到名为 `.data` 的那一行，看它的 **Address** 列。

**需要观察的现象**：

- `.data` 段的 Address 应当形如 `0001xxxx`（即落在 `0x00010000`~`0x00018000`），证明它确实进了 DTCM。
- 对照 `.text` 段的 Address 应当是 `00000000` 附近（进了 ITCM），`.bss` 也应在 DTCM 区间但标记为 NOLOAD。

**预期结果**：`.data` 的 Address 在 `0x00010000` 一带（程序里 `.data` 之前还有空的 `.tdata/.htif`，所以 `.data` 通常紧贴 DTCM 起址）。具体数值**待本地验证**（取决于工具链/版本生成的辅助符号体积），但只要落在 \[0x00010000, 0x00018000) 即说明布局正确。

> 提示：也可以用 `objdump -h bazel-bin/examples/coralnpu_v2_hello_world_add_floats.elf`，它同样打印每个段的起始地址（VMA），信息等价。

#### 4.2.5 小练习与答案

**练习 1**：把 `__attribute__((section(".data")))` 全部删掉，三个缓冲会进哪个段？还能被正确加载吗？

> **答案**：会进 `.bss`（因为是未初始化全局变量）。`.bss` 在本脚本里是 `NOLOAD`，不含文件内容；符号地址仍可在符号表查到（主机仍能按地址写输入），但缓冲不再是 ELF「带内容的加载段」的一部分。强制写 `.data` 是为了让它们成为确定的可加载区域，行为更可控、更利于仿真器/主机建立一致内存视图。

**练习 2**：如果把构建参数改成 `dtcm_size_kbytes = 64`，`.data` 的地址会怎么变？

> **答案**：根据 [linker.bzl:29-30](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/linker.bzl#L29-L30)，只要 `itcm/dtcm` 大小偏离默认（8/32），DTCM 起址就从 `0x00010000` 跳到 `0x00100000`。所以 `.data` 会出现在 `0x001xxxxx`，而不是 `0x0001xxxx`。这也是构建文件名会带 `_ITCM...DTCM64KB...` 后缀的原因（见 4.3）。

---

### 4.3 用 `coralnpu_v2_binary` 规则编译出 `.elf`

#### 4.3.1 概念说明

知道怎么写程序、知道变量怎么进 DTCM 之后，还差「怎么把它编译成内核能加载的镜像」。CoralNPU **不使用** Bazel 内置的 `cc_binary`，而是自定义了一条规则 `coralnpu_v2_binary`（[rules/coralnpu_v2.bzl](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl)），因为它需要替你做三件 `cc_binary` 不会自动做的事：

1. **切换到 RISC-V 裸机平台**：通过 Bazel 的 *platform transition*，把整条编译链切到 `//platforms:coralnpu_v2`，自动用上正确的 `-march=rv32imf...` 工具链。
2. **自动链接 CRT**：默认把 `//toolchain/crt`（上一讲的启动代码）挂进依赖，保证有 `_start`。
3. **按 TCM 大小生成链接脚本**：用模板 `coralnpu_tcm.ld.tpl` 现场生成 `.ld`，按你给的 ITCM/DTCM 大小分配地址。

最终一条规则产出**三件套**：`.elf`（带符号、可加载）、`.bin`（纯二进制）、`.vmem`（Verilog `$readmemh` 可读的十六进制镜像，仿真/FPGA 用）。

#### 4.3.2 核心流程

`coralnpu_v2_binary(name, srcs, ...)` 是一个 **Starlark 宏**（macro），它展开成几条底层规则，执行顺序如下：

```
coralnpu_v2_binary(                    # 宏入口（rules/coralnpu_v2.bzl:218）
  │  1. 决定 CRT 依赖（默认 //toolchain/crt）
  │  2. generate_linker_script(...)      # 用模板生成 .ld（含 ITCM/DTCM 大小）
  ▼
_coralnpu_v2_binary(                   # 真正的 rule（带平台 transition）
  │  transition → //platforms:coralnpu_v2   # 切到 RISC-V 裸机平台
  │  cc_common.compile(...)                # 编译 srcs
  │  cc_common.link(..., -T <生成的.ld>)    # 链接出 name.elf
  │  objcopy -O binary  elf → name.bin     # 抽取纯二进制
  │  srec_cat            bin → name.vmem   # 转成 Verilog vmem
  ▼
产物: name.elf / name.bin / name.vmem
（外加三个 filegroup: name.elf / name.bin / name.vmem 方便按需取用）
```

注意第 1 步生成链接脚本时，宏会比较你传入的 ITCM/DTCM/栈/堆参数与默认值；只要任一不同，生成的脚本文件名会带一个 `_ITCM8KB_DTCM32KB...` 风格的后缀（[coralnpu_v2.bzl:275-297](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L275-L297)），避免不同配置的脚本互相覆盖。

#### 4.3.3 源码精读

**用法：examples/BUILD.bazel。**

[examples/BUILD.bazel:17-22](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/BUILD.bazel#L17-L22) —— 声明 `hello_world` 目标只需这么多：

```python
load("//rules:coralnpu_v2.bzl", "coralnpu_v2_binary")

coralnpu_v2_binary(
    name = "coralnpu_v2_hello_world_add_floats",
    srcs = ["hello_world_add_floats.cc"],
)
```

只给了 `name` 和 `srcs`，其余全部走默认：8KB ITCM / 32KB DTCM、默认 CRT、生成 vmem。同文件里 `coralnpu_v2_rvv_add_intrinsic`（[L24-27](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/examples/BUILD.bazel#L24-L27)）是另一个用同样规则的目标，留待 [u10-l1](u10-l1-rvv-intrinsics.md) 的 RVV intrinsics 讲义使用。

**实现：rules/coralnpu_v2.bzl。**

平台切换是「自动切到 RISC-V」的核心——[coralnpu_v2.bzl:24-34](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L24-L34)：

```python
def _coralnpu_v2_transition_impl(_settings, attr):
    if attr.semihosting:
        return {"//command_line_option:platforms": CORALNPU_V2_SEMIHOSTING_PLATFORM}
    else:
        return {"//command_line_option:platforms": CORALNPU_V2_PLATFORM}
```

它把命令行的 `--platforms` 改写为 `//platforms:coralnpu_v2`（[L21](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L21)），从而让 `cc_common` 选中的工具链、`-march`、链接脚本全部是 CoralNPU 专用的。

编译 + 链接 + 抽 `.bin` 在实现函数里完成——[coralnpu_v2.bzl:99-138](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L99-L138)，关键三步：

```python
# 1) 编译源码
(_compilation_context, compilation_outputs) = cc_common.compile(...)
# 2) 链接：把生成的 .ld 作为 -T 传进去，产出 name.elf
linking_outputs = cc_common.link(
    name = "{}.elf".format(ctx.label.name),
    ...
    user_link_flags = ctx.attr.linkopts + ["-Wl,-T,{}".format(ctx.file.linker_script.path)],
    output_type = "executable",
)
# 3) objcopy 抽取纯二进制 name.bin
ctx.actions.run(..., executable = objcopy_tool,
    arguments = ["-O", "binary", linking_outputs.executable.path, out_bin.path], ...)
```

注意第 2 步的 `-Wl,-T,<脚本>`：这正是把 4.2 节那个「按 TCM 大小生成」的链接脚本喂给链接器，从而 `.data` 才能进 DTCM。

随后 `.vmem` 由 `srec_cat` 把 `.bin` 做 byte-swap、填充后转成十六进制（[coralnpu_v2.bzl:140-177](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L140-L177)），用于仿真器/ FPGA 的 `$readmemh` 加载。如果不需要，可传 `enable_vmem = False`。

宏的**默认参数**集中在签名里——[coralnpu_v2.bzl:218-232](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L218-L232)：

```python
def coralnpu_v2_binary(
        name, srcs, tags = [], semihosting = False,
        itcm_size_kbytes = 8, dtcm_size_kbytes = 32,   # ← 默认 8KB/32KB
        word_size = 32, linker_script = None,
        stack_size_bytes = 128, heap_size = "", heap_location = "DTCM",
        enable_vmem = True, crt = None, **kwargs):
```

而 CRT 的自动挂接——[coralnpu_v2.bzl:258-265](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L258-L265)：

```python
if crt != None:
    if crt: deps.append(crt)
elif semihosting:
    deps.append("//toolchain/crt:crt_semihosting")
else:
    deps.append("//toolchain/crt")          # ← 默认挂上一讲的 CRT
```

这解释了为什么 `hello_world_add_floats.cc` 里**看不到任何 `_start` 或启动代码**，却能正常从复位跑起来：CRT 由规则自动链接进来了（上一讲 u2-l1 的 `alwayslink` 机制）。

#### 4.3.4 代码实践（构建型）

**目标**：亲手构建 `hello_world`，确认三件套产物都在。

**步骤**：

1. 构建：
   ```bash
   bazel build //examples:coralnpu_v2_hello_world_add_floats
   ```
2. 列出产物：
   ```bash
   ls -la bazel-bin/examples/coralnpu_v2_hello_world_add_floats.*
   ```

**需要观察的现象**：应当看到 `.elf`、`.bin`、`.vmem` 三个文件。

**预期结果**：三个产物都生成。`.elf` 体积最大（含符号表/调试信息），`.bin` 是裸二进制，`.vmem` 是文本格式的十六进制。具体字节数**待本地验证**。

> 失败排查：如果构建报缺工具链或网络相关错误，通常是首次构建需下载 RISC-V 工具链（`toolchain_coralnpu_v2`），参考 [u1-l3](u1-l3-bazel-build-quickstart.md) 的 Quick Start 与 WORKSPACE 说明。

#### 4.3.5 小练习与答案

**练习 1**：`examples/BUILD.bazel` 里只写了 `name` 和 `srcs`，CRT 是怎么进来的？

> **答案**：宏 `coralnpu_v2_binary` 在 [L258-265](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/rules/coralnpu_v2.bzl#L258-L265) 检测到没有显式 `crt` 且 `semihosting=False`，就自动把 `//toolchain/crt` 追加进 `deps`。所以 BUILD 文件无需手写启动代码依赖。

**练习 2**：为什么 CoralNPU 要自定义 `coralnpu_v2_binary`，而不是直接用 `cc_binary`？

> **答案**：因为它必须额外做三件 `cc_binary` 不会做的事——(1) platform transition 切到 RISC-V 裸机平台、(2) 自动挂 CRT 提供 `_start`、(3) 按给定的 ITCM/DTCM 大小现场生成并传入链接脚本。这三步共同保证产物是「能进 ITCM/DTCM、能从复位启动」的裸机镜像，而不是主机上的普通可执行文件。

---

## 5. 综合实践

把本讲三块知识串起来：**写一个对 8 个 `int32` 逐元素相乘的程序，编译，并用 `readelf` 验证 `.data` 在 DTCM。**

### 5.1 实践目标

- 用三段式结构写一个新程序（输入→计算→输出）。
- 用 `coralnpu_v2_binary` 编译它。
- 用 `readelf` 证明 `.data` 落在 DTCM \[0x00010000, 0x00018000)，验证你对 4.2 节的理解。

### 5.2 操作步骤

**第 1 步：新建源文件** `examples/mul_int32.cc`（**示例代码**，参照 `hello_world_add_floats.cc` 改写，把 `float` 相加换成 `int32_t` 相乘）：

```cpp
// 示例代码：8 个 int32 逐元素相乘
#include <stdint.h>

int32_t input1[8] __attribute__((section(".data")));
int32_t input2[8] __attribute__((section(".data")));
int32_t output[8] __attribute__((section(".data")));

int main() {
  for (int i = 0; i < 8; i++) {
    output[i] = input1[i] * input2[i];
  }
  return 0;
}
```

**第 2 步：在 `examples/BUILD.bazel` 追加目标**（**示例代码**，加在已有两个目标之后）：

```python
coralnpu_v2_binary(
    name = "coralnpu_v2_mul_int32",
    srcs = ["mul_int32.cc"],
)
```

**第 3 步：编译**：

```bash
bazel build //examples:coralnpu_v2_mul_int32
```

**第 4 步：用 `readelf` 检查段地址**：

```bash
readelf -S bazel-bin/examples/coralnpu_v2_mul_int32.elf
```

### 5.3 需要观察的现象

- 在 `readelf -S` 输出里定位 `.data` 行。
- 确认其 **Address** 列在 `0001xxxx`，即 DTCM \[0x00010000, 0x00018000) 区间内。
- （可选）用 `readelf -s` 看符号表，确认 `input1`/`input2`/`output` 三个符号的地址也都在该区间，且彼此相邻（各占 32 字节）。

### 5.4 预期结果

- 构建成功，产出 `coralnpu_v2_mul_int32.elf/.bin/.vmem` 三件套。
- `.data` 段地址落在 DTCM 区间，证明 `section(".data")` + 默认链接脚本确实把变量放进了 DTCM。
- 段地址的精确数值**待本地验证**（取决于工具链生成的辅助符号）；只要在 \[0x00010000, 0x00018000) 内即视为通过。

### 5.5 进阶（可选）

- 在 BUILD 目标里加 `dtcm_size_kbytes = 64`，重新构建，再用 `readelf -S` 看 `.data` 地址是否如 4.2.5 练习 2 所述跳到了 `0x001xxxxx`。
- 思考：乘法 `*` 在本程序里由标量核的哪个执行单元完成？（提示：回顾 [u1-l1](u1-l1-project-overview.md) 提到的 MLU 单元，详细见 [u5-l2](u5-l2-mlu-dvu.md)。）

---

## 6. 本讲小结

- CoralNPU 程序是**三段式结构**：全局输入缓冲 + 全局输出缓冲 + `main()` 里的计算主体；I/O 不走 `printf`/`scanf`，而是主机与内核**共享 DTCM**。
- `__attribute__((section(".data")))` 的作用是**把未初始化的全局变量强制钉进 `.data` 段**（否则会进 `.bss`），使它们成为 ELF 里确定的可加载区域，便于主机/仿真器定位与覆写。
- `.data` 段由链接脚本 `coralnpu_tcm.ld.tpl` 的 `> DTCM` 规则放入 DTCM，默认起址 `0x00010000`（由 `linker.bzl` 决定），区间为 \[0x00010000, 0x00018000)。
- 编译用自定义规则 `coralnpu_v2_binary`：它通过 **platform transition** 切到 RISC-V 裸机平台、**自动链接 CRT**、**按 ITCM/DTCM 大小生成链接脚本**，产出 `.elf/.bin/.vmem` 三件套。
- BUILD 文件只需 `name` + `srcs`，其余（CRT、链接脚本、工具链）全部由规则默认值接管。
- 验证内存布局用 `readelf -S`（或 `objdump -h`）查看段地址，是连接「源码」与「实际地址」的常用手段。

## 7. 下一步学习建议

本讲你得到了一个能编译的 `.elf`，但它还没「跑起来」。接下来的学习路径：

- **[u2-l3 在 Verilator 仿真器上运行程序](u2-l3-run-on-verilator.md)**：把本讲产出的 `.elf` 用 `core_mini_axi_sim` 仿真器加载运行，观察 `main` 返回后内核 halt 的行为——这是「让程序真正执行」的第一步。
- **[u2-l4 cocotb 测试框架入门](u2-l4-cocotb-testbench-intro.md)**：用 `CoreMiniAxiInterface` 的 `load_elf`/`lookup_symbol`/`write`/`execute_from`/`wait_for_halted`/`read` 把本讲的「数据往返」流程在测试台里完整实现，给 `mul_int32` 喂输入、取输出、验证结果。
- 继续阅读 [doc/tutorials/writing_coralnpu_programs.md](https://github.com/google-coral/coralnpu/blob/77bc1ffe06dbf3b7bafc7eab167ead2b42668df9/doc/tutorials/writing_coralnpu_programs.md) 的「Creating the test bench」一节，它正是 u2-l4 的素材。
