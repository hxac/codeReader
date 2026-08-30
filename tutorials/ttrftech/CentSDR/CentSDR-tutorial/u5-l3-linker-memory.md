# 内存的版图：链接脚本与启动文件

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 CentSDR 在 STM32F303 上的完整内存版图：Flash 128KB、通用 SRAM 40KB、CCM 8KB 各自的地址范围、存放内容和相互关系。
2. 解释 `rules_code.ld` 如何通过「同名遮蔽」ChibiOS 官方链接片段，把 `ccmfunc.ld` 注入到段定义链中。
3. 读懂 `.ccmfunc` 段的 VMA/LMA 双地址机制，理解「代码存在 Flash、运行在 CCM」是如何做到的。
4. 说明 `crt2.c` 的 `__late_init()` 在启动的哪个阶段、为什么必须把代码从 Flash 搬进 CCM、`SYSCFG->RCR` 写保护起什么作用。
5. 独立使用 `arm-none-eabi-nm / objdump / size` 检查任意函数落在哪个内存区域，并把一个自选函数搬进 CCM。

本讲是全手册「进阶篇」的第三讲，视线从 C 代码下沉到构建产物的最底层——**谁决定每个字节住在哪个地址**。

## 2. 前置知识

### 2.1 从「编译完了」说起

在第 1 单元第 2 讲（构建与烧录）里我们已经知道：`make` 之后 `build/` 下会得到 `ch.elf`、`ch.bin`、`ch.hex` 三个产物。编译器（`arm-none-eabi-gcc`）把每个 `.c` 文件翻译成 `.o`（目标文件），此时函数和变量都还是「没有地址的符号」。**给每个符号分配最终地址的过程叫链接（link）**，而指挥链接器（`arm-none-eabi-ld`，实际经由 `gcc` 间接调用）分配地址的蓝图，就是**链接脚本（linker script）**。

### 2.2 两个容易混淆的地址：VMA 与 LMA

- **VMA（Virtual Memory Address，虚拟/运行地址）**：程序运行时，这个段「应该」出现在哪个地址。CPU 取指、访存用的都是 VMA。
- **LMA（Load Memory Address，加载地址）**：掉电后这个段的内容实际存放在哪里，也就是烧录到 Flash 里的位置。

对放在 Flash 里就地执行的代码，VMA = LMA。但对「存放在 Flash、运行时搬到 RAM」的段（比如初始化为零以外的全局变量 `.data`，以及本讲主角 `.ccmfunc`），两个地址不同：**LMA 在 Flash，VMA 在 RAM**。启动代码负责在 `main()` 之前把内容从 LMA 拷贝到 VMA。

### 2.3 链接脚本的三个核心语法

- `MEMORY { ... }`：声明芯片上有哪些内存区域（名字、起始地址 `org`、长度 `len`）。
- `SECTIONS { ... }`：声明输出段（如 `.text`、`.bss`）由哪些输入段（`.o` 里的 `.text.*` 等）汇聚而成，并指明放进哪个内存区域。
- `INCLUDE 文件名`：把另一个链接脚本文件原地展开进来，类似 C 的 `#include`。GNU ld 解析 `INCLUDE` 时按搜索路径查找文件，**当前目录优先**，其次才是 `-L` 传入的目录——这正是本讲 4.2 节「同名遮蔽」机制的依据。

### 2.4 与前两讲的衔接

- **u1-l2（构建与烧录）**：`text`（代码+常量）、`data`（有初值全局变量）、`bss`（零初始化全局变量）的划分，本讲会看到它们在链接脚本里的真身。
- **u1-l3**：`__early_init()` 是 ChibiOS 留给用户的复位早期钩子；本讲的 `__late_init()` 是同一族的另一个钩子，时机更晚（已经可以安全写 RAM）。
- **u4-l5（Flash 配置持久化）**：配置页固定占用 `0x0801f800` 起的最后一个 2KB Flash 页，且「配置页不被代码侵占只靠固件小于 126KB 的约定，链接脚本并无机制保留」。本讲将从链接器的角度证明这句话，并在综合实践中亲手验证。
- **u5-l2（SIMD 热路径）**：`dsp.c` 的 `cos_sin()`、`arm_biquad_cascade_df1_q15` 等热函数为什么要住进 CCM，本讲给出地址层面的答案。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| [STM32F303xB.ld](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld) | 86 | **内存版图总图**：声明 flash0/ram0/ram4 三块区域，用 `REGION_ALIAS` 给各段指路，最后 `INCLUDE rules.ld` |
| [rules_code.ld](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld) | 79 | **代码段规则**：ChibiOS 官方同名文件的修改副本，在 `.text` 之前插入 `INCLUDE ccmfunc.ld`（第 38 行，与官方版的唯一差异） |
| [ccmfunc.ld](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld) | 17 | **CCM 搬迁名单**：定义 `.ccmfunc` 输出段，用选择器挑出要住进 CCM 的函数和只读数据 |
| [crt2.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/crt2.c) | 25 | **启动搬运工**：`__late_init()` 把 `.ccmfunc` 从 Flash 拷到 CCM，并用 `SYSCFG->RCR` 加写保护 |
| [Makefile](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile) | 253 | **构建指挥**：`LDSCRIPT= STM32F303xB.ld` 指定链接脚本，定义 arm-none-eabi 工具链与 elf/bin/hex 产出规则 |

这五个文件构成一条链：Makefile 选中 `STM32F303xB.ld` → 它 `INCLUDE rules.ld` → 官方 `rules.ld` 再 `INCLUDE rules_code.ld`（被项目根目录的同名文件遮蔽）→ 项目版 `rules_code.ld` 又 `INCLUDE ccmfunc.ld`。而 `crt2.c` 编译成 `crt2.o` 后与这一切链接在一起，负责运行期的搬运。

另外两个背景事实（本讲会用到）：

- `.gitmodules` 声明子模块 `ChibiOS` 指向 <https://github.com/edy555/ChibiOS.git>，`git ls-files -s ChibiOS` 显示钉在提交 `fe0ba1049c...`。本仓库检出环境里没有该子模块的内容，涉及 ChibiOS 内部文件的描述均以该钉定提交的在线内容为准。
- 上游 ChibiOS 的构建规则文件 `rules.mk`（`Makefile` 第 235 行 include 的那个）在本讲写作时无法在线核对原文，涉及它的推断都会明确标注。

## 4. 核心概念与源码讲解

### 4.1 模块一：STM32F303xB.ld —— 内存版图的总平面图

#### 4.1.1 概念说明

一块 MCU 上通常有好几种存储器：Flash（掉电保留、就地执行代码）、通用 SRAM（随时可写、可被 DMA 访问）、以及 STM32F3 特有的 CCM（Core Coupled Memory，内核紧耦合内存）。链接脚本的第一项工作就是把这些物理资源登记造册，形成整块芯片的「地籍图」。

`STM32F303xB.ld` 只做三件事：登记内存区域、给每类段指定去向（`REGION_ALIAS`）、把细分的段规则外包给 `rules.ld`。它是纯粹的「规划文件」，不直接搬运任何字节。

为什么文件名带 `xB`？意法半导体的 STM32F303 命名里容量后缀 `B` 对应 128KB Flash 的型号，脚本里的 `len = 128k` 与之一致；`Makefile` 第 108 行被注释掉的官方脚本 `STM32F303xC.ld` 则对应更大容量的型号。

#### 4.1.2 核心流程

```text
STM32F303xB.ld 的执行逻辑（伪代码）

1. MEMORY：登记可用内存
   flash0 = [0x08000000, 0x08000000+128k)   ← 向量表+代码+常量+.data/.ccmfunc 的存储镜像
   ram0   = [0x20000000, 0x20000000+40k)    ← 栈 + .data + .bss + 堆
   ram4   = [0x10000000, 0x10000000+8k)     ← CCM，只给 .ccmfunc 用
   （flash1-7、ram1-3/5-7 长度为 0，占位表示"不存在"）

2. REGION_ALIAS：起"通用别名"
   代码类段（VECTORS/XTORS/TEXT/RODATA/VARIOUS） → flash0
   数据类段（DATA/BSS/HEAP/两种栈）            → ram0
   数据类段的存储镜像（*_LMA）                  → flash0

3. INCLUDE rules.ld：把段的具体拼装规则展开进来
```

整理成地址表（本讲最重要的一张图，后面反复引用）：

| 区域 | 起始地址 | 长度 | 运行时存放内容 |
|---|---|---|---|
| `flash0` | `0x08000000` | 128KB | 向量表、全部代码、常量、`.data`/`.ccmfunc` 的初始镜像；**最后 2KB（`0x0801f800` 起）按约定留给配置页** |
| `ram0` | `0x20000000` | 40KB | 主栈（中断）、过程栈（main 线程）、`.data`、`.bss`、堆 |
| `ram4`（CCM） | `0x10000000` | 8KB | `.ccmfunc`：DSP 热点代码 + arctan 表 |

#### 4.1.3 源码精读

**登记三块内存**。[STM32F303xB.ld:L20-L38](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L20-L38) 声明了 16 个区域槽位，真正有容量的是三个：

- [STM32F303xB.ld:L22](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L22)：`flash0 : org = 0x08000000, len = 128k` —— Flash 从 `0x08000000` 起 128KB。
- [STM32F303xB.ld:L30](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L30)：`ram0 : org = 0x20000000, len = 40k` —— 通用 SRAM。
- [STM32F303xB.ld:L34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L34)：`ram4 : org = 0x10000000, len = 8k` —— **CCM**。STM32F3 的 CCM 挂在 Cortex-M4 内核私有总线上，是 8KB 的 0 等待 SRAM。

其余槽位（`flash1-7`、`ram1-3`、`ram5-7`）长度全为 0：ChibiOS 的段规则模板要引用 `ram0`~`ram7` 八个名字，没有对应硬件就填零占位，模板不用改。

**给段指路**。接下来的 `REGION_ALIAS` 把「段的用途」翻译成「住在哪块内存」：

- [STM32F303xB.ld:L44-L61](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L44-L61)：向量表、构造/析构表、代码、只读数据、杂项段全部指向 `flash0`，各自的 `_LMA` 别名也是 `flash0`（存 Flash、在 Flash 运行，VMA = LMA）。
- [STM32F303xB.ld:L64](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L64)：`RAM_INIT_FLASH_LMA` → `flash0`。这个别名专门表示「各类 RAM 段的初始化镜像存在 Flash 里」，`.ccmfunc` 的 `AT >` 用的就是它（见 4.3 节）。
- [STM32F303xB.ld:L68-L82](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L68-L82)：主栈、过程栈、`.data`、`.bss`、堆全部指向 `ram0`；其中 [L75-L76](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L75-L76) `DATA_RAM → ram0`、`DATA_RAM_LMA → flash0`，正是「有初值全局变量：值存 Flash、运行在 SRAM」这句老话的脚本写法。

注意一个细节：**`ram4` 没有出现在任何 `REGION_ALIAS` 里**。ChibiOS 官方的 `rules_data.ld` 虽然也为 `ram4` 准备了 `.ram4_init`/`.ram4` 段（在钉定提交的官方文件中可见），但那些段需要源码用 `__attribute__((section(...)))` 主动申报才会非空；CentSDR 的源码没有申报，所以 `ram4` 实际上专供 `.ccmfunc` 独占。

**外包段规则**。[STM32F303xB.ld:L85](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L85) 只有一行 `INCLUDE rules.ld` —— 规划与拼装就此分家，下一模块接着讲。

#### 4.1.4 代码实践：用 size 验证 Flash 版图与配置页的边界

**实践目标**：回答综合实践的第一问——「`0x0801f800` 的配置页为什么不会与固件代码冲突？」的定量版本。

**操作步骤**：

1. 完整构建一次：`make`（需要 `arm-none-eabi-` 工具链与已检出的 ChibiOS 子模块）。
2. 查看 Flash 总占用：`arm-none-eabi-size build/ch.elf`。
3. 逐段核对：`arm-none-eabi-size -A build/ch.elf`。

**需要观察的现象**：

- Berkeley 格式（第 2 步）的 `text` 列不只包含指令，而是**所有需要占用 Flash 的东西**：向量表、`.text`、`.rodata`、`.data` 的初始镜像、`.ccmfunc` 的初始镜像。`data` 列是运行时占 SRAM 的有初值变量，`bss` 列是零初始化变量。
- 设 `text = T` 字节。只要

  \[ T < 0x1\mathrm{f}800 = 126\,\mathrm{KB} \]

  链接器自 `0x08000000` 向上分配的所有内容就都落在 `0x0801f800` 之前，最后一个 2KB 页安然无恙。

**预期结果**：`text` 明显小于 126KB，配置页安全。**这正是 u4-l5 所说「约定」的定量含义：链接脚本没有任何一行提到 `0x0801f800`，也没有 `LENGTH = 126k` 这样的截短——保护完全依赖固件体量。** 一旦未来功能膨胀使 `text` 逼近 126KB，链接器不会报任何错，配置页会被无声覆盖，唯一症状是保存的配置在下次烧录后损坏。

（本讲义写作环境无工具链，以上数值**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：把 `flash0` 的 `len` 从 `128k` 改成 `126k`，能否从机制上保护配置页？

**答案**：能，而且比「约定」可靠。`LENGTH` 截短后，任何使段越过 `0x0801f800` 的改动都会让链接器直接报 `section ... will not fit in region flash0` 并失败，把隐患挡在构建期。代价是 `.data`/`.ccmfunc` 的 LMA 空间也一并受限（它们同样存放在 flash0）。原作没有这样做，推测是希望保留完整的 128KB 视图、以体量约定控制——这是一种工程取舍。

**练习 2**：`ram0` 的 40KB 里，栈、`.data`、`.bss`、堆的先后顺序由谁决定？

**答案**：由 `rules_stacks.ld` / `rules_data.ld`（ChibiOS 官方片段，经 `rules.ld` 串联进来）中 `SECTIONS` 的书写顺序和各段的 `> RAM` 指派决定；`STM32F303xB.ld` 只决定「这些段都去 `ram0`」。堆在官方 `rules_data.ld` 中被放到区域的剩余尾部（`__heap_base__` 到 `ORIGIN+LENGTH`），所以 `.bss` 越大堆越小。

**练习 3**：为什么 `ram4`（CCM）不拿来放 `.bss` 或堆？

**答案**：CCM 只挂在 CPU 私有总线上，**DMA 控制器访问不到**。CentSDR 的音频采样靠 I2S DMA 搬运、SPI DMA 刷屏，缓冲区若放进 CCM，DMA 会静默失败。所以 CCM 只适合放「CPU 独享」的内容——这正是它被用来放热点代码与 arctan 只读表的原因（详见 4.3.1）。

### 4.2 模块二：rules_code.ld —— 同名遮蔽与段规则链

#### 4.2.1 概念说明

`STM32F303xB.ld` 末尾的 `INCLUDE rules.ld` 引用的是 ChibiOS 的官方片段（位于子模块 `os/common/startup/ARMCMx/compilers/GCC/ld/` 下）。在钉定提交 `fe0ba1049c` 中，官方 `rules.ld` 的全部内容只有三行 `INCLUDE`（见 [官方文件](https://github.com/edy555/ChibiOS/blob/fe0ba1049c38346ceb2a396fa560848ef8323dd1/os/common/startup/ARMCMx/compilers/GCC/ld/rules.ld)）：

```text
INCLUDE rules_stacks.ld    ← 栈规则
INCLUDE rules_code.ld      ← 代码段规则   ← 本模块的主角
INCLUDE rules_data.ld      ← 数据段规则
```

关键在于：GNU ld 查找 `INCLUDE` 的文件时**先搜索当前目录，再搜索 `-L` 给出的目录**。`make` 从项目根目录发起链接（`LDSCRIPT= STM32F303xB.ld` 也是相对路径，见 4.5 节），而项目根目录恰好放着一份与官方**同名**的 `rules_code.ld`——它是官方文件的逐字副本，唯一改动是在 `xtors` 与 `.text` 之间插入了 `INCLUDE ccmfunc.ld`。于是官方 `rules.ld` 的 `INCLUDE rules_code.ld` 实际展开的是**项目自己的改版**，`ccmfunc.ld` 就这样被悄悄夹带进了段定义链。

这个手法叫「同名遮蔽（shadowing）」：不改上游一行代码，靠搜索顺序覆盖上游行为。理解了它，就理解了 CentSDR 代码搬迁机制的全部秘密。

> 说明：`Makefile` 第 235 行 include 的 `rules.mk` 负责实际的链接命令与 `-L` 搜索路径，该文件在写作时无法在线核对原文，以上搜索顺序依据 GNU ld 手册对 `INCLUDE` 的规定；遮蔽是否真实发生，可用下面 4.2.4 的改名实验直接验证（结论不受 `rules.mk` 细节影响——只要根目录文件被先找到）。

#### 4.2.2 核心流程

项目版 `rules_code.ld` 展开后的段装配顺序（在 Flash 中自低地址向高地址排布）：

```text
SECTIONS
{
  ① vectors   ← KEEP(*(.vectors))        中断向量表，必须位于 0x08000000（映射到 0x00000000 的 boot 空间）
  ② xtors     ← .init_array/.fini_array  C++ 风格构造/析构表（ChibiOS 内核也用它）
  ③ .ccmfunc  ← INCLUDE ccmfunc.ld       ★ 本项目插入：DSP 热点代码，LMA 在这里，VMA 在 CCM
  ④ .text     ← *(.text) *(.text.*) ...  其余全部代码
  ⑤ .rodata   ← *(.rodata) ...           常量（含各查表）
  ⑥ .ARM.extab / .ARM.exidx / .eh_frame* ← 异常展开表（C++/栈回溯用）
}
（随后官方 rules_data.ld 继续装配 .data/.bss/堆，VMA 在 ram0）
```

`ENTRY(Reset_Handler)` 告诉链接器「程序入口是复位向量对应的处理函数」，决定 ELF 的入口点元数据。

段顺序在本机制里是**功能性**的：`.ccmfunc` 写在 `.text` 之前，它的选择器才有优先权把匹配的输入段先挑走；若写在 `.text` 之后，`*(.text.*)` 早已把那些函数吞进 Flash 的 `.text`，CCM 选择器将扑空。

#### 4.2.3 源码精读

**入口与向量表**。[rules_code.ld:L17](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L17) `ENTRY(Reset_Handler)` 声明入口；[rules_code.ld:L21-L24](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L21-L24) 把 `.vectors` 输入段用 `KEEP(...)` 放进输出段 `vectors` 并指向 `VECTORS_FLASH`（即 flash0，16 字节对齐）。`KEEP` 防止链接器在「没人引用向量表」的错觉下把它当作垃圾清除——没有它，`--gc-sections` 会删掉向量表，板子直接变砖。

**构造/析构表**。[rules_code.ld:L26-L36](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L26-L36) 定义 `xtors` 段，记录 `__init_array_start/end` 边界。ChibiOS 虽是 C 项目，但其内核初始化也走这套通用机制。

**唯一的私货**。[rules_code.ld:L38](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L38)：

```text
INCLUDE ccmfunc.ld
```

与钉定提交的官方 `rules_code.ld` 逐字对比，整份 79 行文件**只有这一行是新增的**（外加第 40 行缩进的空白差异）。git 历史也印证：提交 `ac7fb97`（"place dsp functions onto ccm ram using linker script"）一次性加入了 `rules_code.ld`、`ccmfunc.ld`、`crt2.c` 三件套。

**其余代码归 Flash**。[rules_code.ld:L40-L47](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L40-L47)：`.text` 输出段收集所有 `.text`、`.text.*`、`.glue_7t/.glue_7`（ARM/Thumb 互跳胶水代码）、`.gcc*`，放进 `TEXT_FLASH`。[rules_code.ld:L49-L57](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L49-L57)：`.rodata` 收集全部常量，记录 `__rodata_base__/__rodata_end__` 边界。[rules_code.ld:L59-L78](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/rules_code.ld#L59-L78)：ARM 异常展开相关段收尾。

#### 4.2.4 代码实践：改名实验，亲手证实「遮蔽」存在

**实践目标**：证明根目录的 `rules_code.ld` 真的参与了链接（而不是官方版被使用）。

**操作步骤**：

1. 确保当前能完整 `make` 成功。
2. 把根目录的 `rules_code.ld` 临时改名：`mv rules_code.ld rules_code.ld.bak`。
3. 再次 `make`。
4. 观察链接阶段的报错，然后 `mv rules_code.ld.bak rules_code.ld` 恢复。

**需要观察的现象**：链接器报出类似 `undefined reference to '__ccmfunc_init_text__'` / `'__ccmfunc_init__'` / `'__ccmfunc_end__'` 的错误。

**预期结果与原理**：这三个符号只在 `crt2.c` 里被 `extern` 引用（[crt2.c:L4](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/crt2.c#L4)），唯一给它们赋值的定义在 `ccmfunc.ld`（第 4、5、15 行，见 4.3.3）。改名后 ld 转而找到 ChibiOS 官方版 `rules_code.ld`（不含 `INCLUDE ccmfunc.ld`），`.ccmfunc` 段不存在，三符号无定义，链接必然失败——失败本身就是「根目录文件先前被优先采用」的铁证。（**待本地验证**。）

#### 4.2.5 小练习与答案

**练习 1**：如果把根目录 `ccmfunc.ld` 改名（保持 `rules_code.ld` 不动），会发生什么？

**答案**：`rules_code.ld` 第 38 行的 `INCLUDE ccmfunc.ld` 找不到可展开的文件，ld 直接报「cannot open linker script file ccmfunc.ld」之类的错误，链接失败。与练习正文中的实验不同，这次连官方备用文件都没有——`ccmfunc.ld` 这个名字是项目自创的。

**练习 2**：为什么不直接修改 ChibiOS 子模块里的官方 `rules_code.ld`？

**答案**：子模块内容属于上游仓库的钉定提交，改动会让本地子模块变脏、升级上游时冲突或丢失。把改版副本放在自己仓库根目录，利用搜索顺序覆盖，上游可以随时自由升级——这是嵌入式项目定制 vendor 构建体系的常见惯例，代价是「魔法感」强：不看文件名和搜索规则，很难发现这份 79 行文件是活的。

**练习 3**：`.ccmfunc` 若被挪到 `.text` **之后**再定义，为什么必然失效？

**答案**：链接器按 `SECTIONS` 的书写顺序消耗输入段。`.text` 的通配 `*(.text.*)` 会先把所有函数段吞走，轮到 `.ccmfunc` 时选择器（如 `*dsp.o(.text.*)`）已无段可匹配，`.ccmfunc` 变成空段，`__ccmfunc_init__ == __ccmfunc_end__`，`crt2.c` 的拷贝循环一次都不执行，DSP 代码实际仍在 Flash 运行——程序不报错但优化目标落空，属于典型的「静默失效」。

### 4.3 模块三：ccmfunc.ld —— 把代码搬进 CCM 的搬迁名单

#### 4.3.1 概念说明

CCM（Core Coupled Memory）是 Cortex-M4 内核私有的 8KB SRAM（`0x10000000` 起）。它的两个物理特性决定了它的用途：

1. **0 等待、不经总线矩阵**：CPU 从 CCM 取指不会与任何外设 DMA 争用总线。CentSDR 的 DSP 线程要持续做滤波/FFT，同时 I2S DMA 在搬采样、SPI 在刷屏——把热函数放进 CCM，等于给 CPU 修了一条专用快车道。u5-l2 分析过的 SIMD 热路径（`cos_sin`、`arm_biquad_cascade_df1_q15`、CFFT 等）正是首批住户。
2. **DMA 不可见**：反过来说，任何要被 DMA 碰的缓冲区绝不能放这里。

`ccmfunc.ld` 全部 17 行只定义一个输出段 `.ccmfunc`，本质是一份「搬迁名单」：左边是搬迁对象（输入段选择器），右下角是落户地址（`> ram4`，即 CCM），外加一行「家当暂存处」（`AT > RAM_INIT_FLASH_LMA`，即 Flash）。

为什么搬代码能提升性能而不用改任何 C 代码？因为函数的调用在机器码层面就是「跳到某个地址」。链接器把 `cos_sin` 的地址从 `0x08xxxxxx`（Flash）改成 `0x1000xxxx`（CCM）后，所有调用点的跳转目标自动更新——源码零改动，这正是「在链接期做优化」的优雅之处。

#### 4.3.2 核心流程

```text
.ccmfunc 段的双地址人生

烧录后（掉电状态）            启动中（__late_init）          运行时
┌──────────────┐   逐字拷贝   ┌──────────────┐
│ Flash @ LMA   │ ──────────► │ CCM  @ VMA    │  CPU 从 0x1000xxxx 取指
│ __ccmfunc_    │  (crt2.c)   │ __ccmfunc_    │  0 等待、无总线竞争
│ init_text__   │             │ init__        │
└──────────────┘             └──────┬───────┘
                                    │ 之后 SYSCFG->RCR=0x00ff
                                    ▼ 写保护，防止意外改写
```

三个边界符号由链接器自动算出：`__ccmfunc_init_text__` = Flash 侧起点（LMA），`__ccmfunc_init__`/`__ccmfunc_end__` = CCM 侧起点/终点（VMA）。拷贝循环只需要「从哪拷、拷到哪、拷多少」三要素，全部齐备。

选择器语法速查：

| 写法 | 含义 |
|---|---|
| `*dsp.o(.text.*)` | 任何名为 `dsp.o` 的输入文件中的全部 `.text.*` 输入段（即 dsp.c 的所有函数） |
| `*display.o(.text.draw_waveform)` | 只挑 `display.o` 里名为 `.text.draw_waveform` 的那一个函数段 |
| `*dsp.o(.rodata.arctantbl)` | 只挑 `dsp.o` 里名为 `arctantbl` 的只读数据段 |

能按单函数挑选，前提是编译时开了「按函数/数据分节」（`-ffunction-sections -fdata-sections` 一类选项）——`display.o` 能被精确到 `draw_waveform` 一个函数，证明本构建确实产生了 `.text.<函数名>` 形式的输入段。

#### 4.3.3 源码精读

完整的段定义在 [ccmfunc.ld:L1-L17](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L1-L17)，逐块拆开：

**段头与三个边界符号**（[ccmfunc.ld:L1-L5](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L1-L5)）：

```text
.ccmfunc : ALIGN(4)
{
    . = ALIGN(4);
    __ccmfunc_init_text__ = LOADADDR(.ccmfunc);
    __ccmfunc_init__ = .;
```

- `.ccmfunc : ALIGN(4)`：新建输出段，输入段按 4 字节对齐。
- `__ccmfunc_init_text__ = LOADADDR(.ccmfunc)`：`LOADADDR()` 是链接脚本内建函数，返回段的 **LMA**（Flash 侧地址）。这是「双地址」在脚本里的显式表达。
- `__ccmfunc_init__ = .`：位置计数器 `.` 当前值即段起始 **VMA**（CCM 侧，从 `0x10000000` 附近开始）。

**搬迁名单**（[ccmfunc.ld:L6-L14](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L6-L14)）：

```text
	*dsp.o(.text.*)
	*dsp.o(.rodata.arctantbl)
	*arm_biquad_cascade_df1_q15.o(.text.*)
	*arm_cfft_radix4_q31.o(.text.*)
	*arm_bitreversal.o(.text.*)
	*display.o(.text.draw_waveform)
	*display.o(.text.draw_waterfall)
	*display.o(.text.draw_spectrogram)
	*display.o(.text.disp_fetch_samples)
```

三个来源：①`dsp.c` 全部函数——核心 DSP 热路径（`cos_sin` 定义于 [dsp.c:L267-L268](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L267-L268)，`static uint32_t cos_sin(uint16_t phase)`，属 `.text.cos_sin`，被 `*dsp.o(.text.*)` 命中）；②CMSIS-DSP 三个关键库文件（IIR 滤波、CFFT、位反转）；③`display.c` 的四个渲染热函数（波形/瀑布/频谱图/采样抓取，定义如 [display.c:L898-L899](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L898-L899) 的 `draw_waveform`）。此外还有查表：`arctantbl` 是 [dsp.c:L493](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L493) 定义的 `const int16_t arctantbl[256+2]`（约 516 字节，CORDIC 角度查表），git 历史显示它是后续提交 `1e6e7c6`（"place arctan table into CCM SRAM"）追加的——只读表放 CCM 同样收益：查表不占总线。

注意对 `display.c` 与 CMSIS 库用的是**逐函数挑选**而非 `*display.o(.text.*)`：8KB 的 CCM 装不下整个 `display.o`，只挑每帧必执行的渲染内圈。`dsp.o` 则整体搬入，说明它体量小且几乎全是热路径。

**段尾与双地址声明**（[ccmfunc.ld:L15-L17](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L15-L17)）：

```text
	__ccmfunc_end__ = .;
	. = ALIGN(4);
    } > ram4 AT > RAM_INIT_FLASH_LMA
```

`> ram4` 设定 VMA 区域（CCM），`AT > RAM_INIT_FLASH_LMA` 设定 LMA 区域（该别名在 [STM32F303xB.ld:L64](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L64) 中指向 flash0）。这一行是整个机制的题眼：**「住在 CCM，家当存在 Flash」**。结尾的 `ALIGN(4)` 保证段长是 4 的倍数——`crt2.c` 按 32 位字拷贝（见 4.4.3），不补齐会漏抄尾巴。

#### 4.3.4 代码实践：用 nm 参观 CCM 住户

**实践目标**：亲眼看到热函数的地址落在 `0x1000xxxx`（CCM 区间）。

**操作步骤**：

1. `make` 构建成功后，列出符号表并过滤：
   ```bash
   arm-none-eabi-nm build/ch.elf | grep -E ' (cos_sin|draw_waveform|draw_waterfall|draw_spectrogram|disp_fetch_samples|arctantbl)$'
   ```
2. 再看一个反例（仍在 Flash 的函数）：
   ```bash
   arm-none-eabi-nm build/ch.elf | grep -E ' (set_modulation|si5351_setupPLL|main)$'
   ```
3. 汇总 `.ccmfunc` 段的双地址：
   ```bash
   arm-none-eabi-objdump -h build/ch.elf
   ```

**需要观察的现象与预期结果**：

- 第一组符号地址应以 `1000` 开头（形如 `1000xxxx t cos_sin`、`1000xxxx T draw_waveform`、`1000xxxx R arctantbl`）。小写 `t` 表示 static 函数，大写 `T/R` 表示全局符号——`cos_sin` 是 static，若被 `-O2` 完全内联则可能查不到，此时以 `draw_waveform`/`arctantbl` 为准。
- 第二组反例地址应以 `0800` 开头（Flash）。
- `objdump -h` 输出中 `.ccmfunc` 一行的 `Vma` 列是 `0x1000xxxx`、`Lma` 列是 `0x08xxxxxx`，两列不同——双地址机制最直观的一帧照片。

（**待本地验证**：本讲义写作环境无工具链。）

#### 4.3.5 小练习与答案

**练习 1**：`*dsp.o(.text.*)` 与 `*display.o(.text.draw_waveform)` 的粒度为什么不一样？

**答案**：容量预算决定粒度。`dsp.o` 几乎全是每次采样都要执行的热路径且体量小，整体搬入划算；`display.o` 还包含菜单、字体、初始化等冷代码，整体搬入会撑爆 8KB，所以只点名叫那四个渲染函数。CCM 分配是「寸土寸金」的资源调度。

**练习 2**：把 `.ccmfunc` 的 `AT >` 改掉（比如不写 `AT`），会发生什么？

**答案**：没有 `AT >` 时 LMA 默认等于 VMA，即链接器认为这段代码「已经」在 `0x10000000` 处，Flash 里不再保存它的镜像。烧录后 CCM 实际是随机内容，`__late_init` 也没有 Flash 源可拷（`LOADADDR` 返回 VMA，拷贝等于自己拷自己），一进 DSP 线程就执行乱码、HardFault。`AT >` 是「代码在 ROM、运行在 RAM」布局的必要条件。

**练习 3**：若往名单里加太多函数，导致总量超过 8KB，什么时候报错、报什么错？

**答案**：链接期报错（不是运行期），形如 `section .ccmfunc will not fit in region ram4`。这是链接脚本机制优于「运行期踩内存」的地方：区域容量是硬约束，超了立刻失败。

### 4.4 模块四：crt2.c —— 启动阶段的搬运工与写保护

#### 4.4.1 概念说明

链接脚本只是「图纸」：它让 `ch.elf` 里记录了 `.ccmfunc` 的双地址，并让 `ch.bin` 里包含 Flash 侧的代码镜像。但复位那一刻，CCM 里只有上次的残渣或随机值——**必须有人在 CPU 开始跑 DSP 代码之前，把镜像从 Flash 搬过去**。

ChibiOS 的标准启动代码（`crt0.s`，位于子模块中）只认识 `.data`、`.ram0_init`~`.ram7_init` 这些官方段，不知道 `.ccmfunc` 为何物。ChibiOS 留了两个用户钩子解决这类需求：u1-l3 见过的 `__early_init`（极早期）和本讲的 `__late_init`（较晚期：RAM 已初始化、可以安全写）。`crt2.c` 就是靠实现 `__late_init`，把自己的搬运逻辑挂进标准启动流程——不用碰子模块里的汇编启动文件，这和 4.2 的「遮蔽」是同一种「不改上游」的思路。

另一个巧思是 `SYSCFG->RCR`：STM32F3 提供对 CCM 的按页（1KB×8 页）硬件写保护。代码搬进去之后，它逻辑上是「ROM」——开启写保护后，哪怕程序跑飞写了野指针，也很难破坏这份「RAM 里的固件」，相当于给 CCM 上了锁。

#### 4.4.2 核心流程

```text
上电复位
  │
  ├─ 向量表取出 Reset_Handler（crt0.s，ChibiOS 官方）
  ├─ __early_init()            ← 钩子（本仓库未自定义）
  ├─ 时钟、基本 RAM 就绪
  ├─ 官方启动代码搬运 .data 等官方段
  ├─ __late_init()             ★ crt2.c 的战场：
  │     ① SYSCFG->RCR = 0x0000   解除 CCM 写保护
  │     ② for (p = CCM 起; p < CCM 终; ) *p++ = *tp++   从 Flash 逐字拷入
  │     ③ SYSCFG->RCR = 0x00ff   8 页全部写保护
  ├─ C++ 风格构造数组（init_array）、内核初始化 chSysInit
  └─ main()                     ← 此时 CCM 里的代码已可正常调用
```

0x00ff 的含义：RCR 每 1 位管 1KB 页，8KB 共 8 位；`0x00ff` 即 8 页全保护，与 [STM32F303xB.ld:L34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/STM32F303xB.ld#L34) 声明的 `len = 8k` 严丝合缝。

#### 4.4.3 源码精读

全部 25 行，结构一目了然。

**符号接口**（[crt2.c:L4](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/crt2.c#L4)）：

```c
extern uint32_t __ccmfunc_init_text__, __ccmfunc_init__, __ccmfunc_end__;
```

声明链接脚本生成的三个边界符号。C 侧的「变量」其实是链接器算出的地址常量——链接脚本与 C 代码之间最典型的握手方式。取名后在 `Makefile` 的源文件清单末尾把 `crt2.c` 编入（[Makefile:L121-L134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L121-L134)，与 `dsp.c`、`main.c`、`flash.c` 并列）。

**钩子本体**（[crt2.c:L9-L25](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/crt2.c#L9-L25)）：

```c
void __late_init(void) {
  // CCM RAM protection register
  SYSCFG->RCR = 0x0000;                    // ① 解锁

  /* Copying CCM initialization code. */
  uint32_t *tp = &__ccmfunc_init_text__;   // ② 源指针：Flash 侧（LMA）
  uint32_t *p = &__ccmfunc_init__;         //    目的指针：CCM 侧（VMA）

  while (p < &__ccmfunc_end__) {
    *p = *tp;
    p++;
    tp++;
  }

  // CCM RAM protection register
  SYSCFG->RCR = 0x00ff;                    // ③ 8 页全部上锁
}
```

逐行品读：

- ① 写保护寄存器先清零。STM32F3 复位后 RCR 默认就是 0，这行是**防御性**的——显式声明意图，不依赖默认值。
- ② 拷贝循环用 `uint32_t*` 逐字（4 字节）搬运。敢用字拷贝的前提是两端都 4 字节对齐且总长为 4 的倍数——这正是 [ccmfunc.ld:L3](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L3) 与 [ccmfunc.ld:L16](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L16) 两处 `ALIGN(4)` 存在的理由。链接脚本与 C 代码在这里互为契约。
- 循环体量即 `.ccmfunc` 段长（不超过 8KB，对 168MHz 的 F3 来说是微秒级的一次性开销）。
- ③ `0x00ff` 上锁。此后 CCM 对 CPU 取指是只读的（执行不受影响），数据写入被硬件拦下。

调用时机由 ChibiOS 官方 `crt0.s` 决定（子模块未在本环境检出，无法给出精确行号，**待确认**；可在 `git submodule update --init` 后于 `ChibiOS/os/common/startup/ARMCMx/compilers/GCC/crt0_v7m.s` 中搜索 `__late_init` 的调用点验证「发生在 main 之前」）。

#### 4.4.4 代码实践：数一数搬了多少字节，验证锁真的上了

**实践目标**：把不可见的启动搬运变成可观测的数据。

**操作步骤**（源码阅读 + 本地实验两种方式任选，推荐先做 A 再做 B）：

A. **阅读推算**：`arm-none-eabi-objdump -h build/ch.elf` 找到 `.ccmfunc` 行，记下十六进制段大小。

B. **插桩观测**（改的是我们自己实验用的副本，观测完还原）：

1. 在拷贝循环里加计数：把 `crt2.c` 的 while 循环临时改为
   ```c
   volatile uint32_t words = 0;
   while (p < &__ccmfunc_end__) { *p = *tp; p++; tp++; words++; }
   ```
   （`words` 仅用于观测，属示例代码。）
2. 在 `SYSCFG->RCR = 0x00ff;` 之后临时加一段把 `words` 与 `SYSCFG->RCR` 的回读值发到 shell/串口的代码（工程已有 shell 组件，见 [Makefile:L105](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L105)）；或者更简单地，用调试器在函数尾下断点查看。
3. `make flash` 后观察输出。
4. **还原 `crt2.c`**，重新 `make`。

**需要观察的现象**：

- `words × 4` 应与 A 步读到的 `.ccmfunc` 段大小一致，且小于 8192。
- 回读的 `SYSCFG->RCR` 应为 `0x000000ff`。

**预期结果**：两件事都对上，即证明「搬运量 = 链接期分配量」以及「写保护已生效」。（**待本地验证**。）

#### 4.4.5 小练习与答案

**练习 1**：为什么这段代码放在 `__late_init` 而不是 `__early_init`？

**答案**：`__early_init` 处于极早期，连 RAM 时序/时钟体系都未必就绪，往 CCM 大块写数据并操作 SYSCFG 寄存器风险高；`__late_init` 时机上 RAM 已可用、又在构造数组和 `main` 之前，正好满足「任何 CCM 代码被调用之前完成搬运」的约束，且约束最宽松（不需要更早）。

**练习 2**：如果不小心把 `SYSCFG->RCR = 0x00ff;` 删掉，程序会出错吗？

**答案**：不会立刻出错——代码已经在 CCM 里，取指照常。失去的只是「防跑飞改写」这层保险：任何野指针落在 `0x10000000~0x10001fff` 都会直接改写正在执行的代码，故障从「被硬件拦截」退化为「诡异崩溃」。这是一个纯加固措施，删掉属于降低健壮性而非引入功能错误。

**练习 3**：`*p = *tp` 若改成 `*tp = *p` 会怎样？

**答案**：方向反了，变成用 CCM 的随机残渣覆盖 Flash 里的代码镜像（Flash 写并非一条商店指令就能完成，实际后果取决于 Flash 接口行为，多半是触发总线错误或写入被忽略）。即便「成功」，启动后 CCM 依旧是随机内容，DSP 一跑就崩。方向感是搬运代码的第一要义：**源是 LMA（Flash），目的是 VMA（CCM）**。

### 4.5 模块五：Makefile —— LDSCRIPT 与工具链如何串成产物

#### 4.5.1 概念说明

链接脚本自己不会运行，需要构建系统把它交给链接器，并把链接结果转换成烧录器认识的格式。`Makefile` 扮演这个「总调度」：选脚本、列源文件、定义交叉工具链、生成 elf/bin/hex 三件套、提供烧录与发布目标。本模块把 4.1~4.4 的静态机制接回 u1-l2 讲过的构建流程，看一份 C 源码最终如何变成 Flash 里的字节。

三个产物的分工（呼应 u1-l2）：

| 产物 | 生成方式 | 用途 |
|---|---|---|
| `build/ch.elf` | 链接器直接输出 | 完整的可执行镜像 + 符号表 + 调试信息（`-ggdb`），调试与一切分析的母本 |
| `build/ch.bin` | `objcopy -O binary` 从 elf 提取 | 纯二进制，从最低 LMA 开始平铺，stlink 烧录用 |
| `build/ch.hex` | `objcopy -O ihex` 从 elf 提取 | Intel HEX，带显式地址记录，兼容部分烧录器/OTA |

#### 4.5.2 核心流程

```text
make
 ├─ ① 编译：arm-none-eabi-gcc -mcpu=cortex-m4 -mthumb -O2 ... 每个 .c → build/obj/.../*.o
 │      （含 crt2.c；ChibiOS 的 crt0.s 汇编启动文件由 startup_stm32f3xx.mk 提供）
 ├─ ② 链接：arm-none-eabi-gcc ... -T STM32F303xB.ld *.o ... → build/ch.elf
 │      展开 STM32F303xB.ld → rules.ld → (根目录) rules_code.ld → ccmfunc.ld
 │      产出符号 __ccmfunc_*，且 crt2.o 的 extern 与之匹配 → 链接成功
 ├─ ③ objcopy：ch.elf → ch.bin / ch.hex   （bin 按 LMA 平铺，含 .ccmfunc 的 Flash 镜像）
 ├─ ④ size：打印 text/data/bss 汇总
 ├─ make flash：arm-none-eabi-gdb -x flash-stutil.gdb 把 ch.elf 烧进芯片
 └─ make release：把 ch.bin/ch.hex/ch.elf 打成 zip
```

#### 4.5.3 源码精读

**选定链接脚本**（[Makefile:L107-L109](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L107-L109)）：

```make
# Define linker script file here
#LDSCRIPT= $(STARTUPLD)/STM32F303xC.ld
LDSCRIPT= STM32F303xB.ld
```

被注释的一行是 ChibiOS 演示工程的默认写法：`$(STARTUPLD)` 是由 [Makefile:L92](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L92) include 的 `startup_stm32f3xx.mk` 所定义的启动链接脚本目录（ChibiOS 子模块内）。生效的一行改用**不带路径**的 `STM32F303xB.ld`——相对当前目录解析，即项目根目录里那份带 CCM 规划的自制脚本（git 历史 `3fe066e` "use linker script" 引入）。这一个等号，就是把整条 `rules_code.ld → ccmfunc.ld` 链条接进构建的开关。

**自研源文件清单**（[Makefile:L121-L134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L121-L134)）：`CSRC` 先汇集各 `.mk` 提供的 ChibiOS 内核/HAL/板级源与 CMSIS-DSP（[Makefile:L111-L117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L111-L117)，正是 `ccmfunc.ld` 点名的三个库文件所在），再逐一列入项目源；末行 `dsp.c main.c flash.c crt2.c` 中 `crt2.c` 压轴——没有这一行，`__late_init` 不存在，`.ccmfunc` 永远不会被搬运。

**交叉工具链一览**（[Makefile:L176-L193](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L176-L193)）：

- [L179](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L179) `TRGT = arm-none-eabi-`：所有工具的统一前缀。
- [L185](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L185) `LD = $(TRGT)gcc`：链接经由 gcc 驱动（自动补运行时库等）。
- [L187](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L187) / [L190](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L190) / [L191](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L191)：`CP=objcopy`、`OD=objdump`、`SZ=size`——本讲所有分析命令的出处。
- [L192-L193](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L192-L193)：`HEX = $(CP) -O ihex`、`BIN = $(CP) -O binary`，即上表 elf→hex/bin 的转换规则。

**官方构建规则接入**（[Makefile:L234-L235](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L234-L235)）：`RULESPATH` 指向 ChibiOS 的 GCC 构建规则目录并 `include rules.mk`。编译/链接/objcopy 的具体命令模板都来自它（该文件未能在写作时在线核对原文，链接命令如何携带 `-T $(LDSCRIPT)` 及 `-L` 搜索路径的细节以本地文件为准）。

**烧录与发布**（[Makefile:L243-L247](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L243-L247)、[Makefile:L249-L253](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L249-L253)）：`make flash` 先依赖 `all` 完整构建，再用 `arm-none-eabi-gdb -x flash-stutil.gdb` 经 st-link 烧录；`make release` 把 `build/ch.bin`、`build/ch.hex`、`build/ch.elf`（[Makefile:L250](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L250)）打进以日期命名的 zip。

#### 4.5.4 代码实践：从 make 到三件套的全流程巡查

**实践目标**：把本讲的所有静态知识在构建产物里对号入座。

**操作步骤**：

1. `make`（首次需 `git submodule update --init` 检出 ChibiOS）。
2. `ls -l build/` 确认 `ch.elf`、`ch.bin`、`ch.hex` 都已生成，记录三者大小。
3. `arm-none-eabi-size build/ch.elf` 记录 text/data/bss。
4. `arm-none-eabi-objdump -h build/ch.elf | grep -E 'ccmfunc|\.text|\.rodata|\.data|\.bss'` 记录各段 Vma/Lma。
5. 对比 `ch.bin` 与 `ch.elf` 的文件大小。

**需要观察的现象与预期结果**：

- 步骤 4：只有「运行在 RAM」的段（`.ccmfunc`、`.data`）Vma ≠ Lma，纯 Flash 段两者相同。
- 步骤 5：`ch.elf` 远大于 `ch.bin`——差值主要是 `-ggdb` 调试信息与符号表（它们不占 Flash，烧录时 gdb/st-util 只写加载镜像）。
- 步骤 3 的 text 数值应满足 4.1.4 的配置页不等式。

（**待本地验证**。）

#### 4.5.5 小练习与答案

**练习 1**：把 `LDSCRIPT` 改回被注释的 `$(STARTUPLD)/STM32F303xC.ld` 会发生什么？

**答案**：官方 `STM32F303xC.ld` 也在其目录内 `INCLUDE rules.ld`，但那份 `rules.ld` 解析 `INCLUDE rules_code.ld` 时同样会先命中根目录的改版……不过官方 303xC 脚本按更大容量芯片规划内存区域，与实际 128KB/8KB CCM 的硬件不符，区域声明与真实芯片错位本身就是错误配置；即便链接侥幸通过，`crt2.c` 照样会把「代码」拷进按错误容量规划的区域，埋下难以追踪的故障。结论：`LDSCRIPT` 必须与真实芯片和自制段规划严格配套。

**练习 2**：为什么同时发布 bin 和 hex 两种格式？

**答案**：bin 是「从最低加载地址开始的裸字节流」，小而简单，但隐含「基地址」这个外部约定；hex 每行自带地址与校验，显式记录每段应写入的地址，兼容性更好（不同烧录器、分段写入场景）。发布两种是为覆盖不同用户的烧录工具链（[Makefile:L250](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L250) 同时打包两者）。

**练习 3**：`ch.bin` 里包含 `.ccmfunc` 的代码吗？在哪个位置？

**答案**：包含。`objcopy -O binary` 按 LMA 平铺输出，`.ccmfunc` 的 LMA 在 flash0 内（位于 `xtors` 与 `.text` 之间），所以 bin 中相应偏移处就是那些 DSP 函数的机器码——它们「暂存」在 Flash，等 `__late_init` 接走。这也再次说明 4.1.4 中 text 列要计入 `.ccmfunc` 镜像的原因。

## 5. 综合实践

**任务：把 `set_modulation` 搬进 CCM，并用工具链完成全套验证**（对应本讲实践任务的两问）。

### 第 1 关：回答「0x0801f800 配置页为什么不会与固件代码冲突？」

先写下你的答案，再对照：链接器对 flash0 的分配永远从 `0x08000000` 向上生长，段的顺序是 vectors → xtors → `.ccmfunc` 镜像 → `.text` → `.rodata` → …（4.2.2）。`0x0801f800` 是 128KB Flash 的最后一个 2KB 页：

\[ 0x08000000 + 0x20000 - 0x800 = 0x0801\mathrm{f}800 \]

链接脚本里没有任何机制提到或保留这一页——**不冲突的唯一保证是「所有需要存放在 Flash 的内容（含 `.data` 与 `.ccmfunc` 的镜像）总长小于 126KB」这一体量约定**（u4-l5 的结论在链接器视角下的重述）。用 4.1.4 的 `arm-none-eabi-size` 即可定量核验；若想把它变成硬保证，可按练习 4.1.5-1 把 `flash0` 截短为 126k。

### 第 2 关：观察「先遣部队」——cos_sin 已经在 CCM 里

规格书建议「把 cos_sin 放入 CCM」，但读完 `ccmfunc.ld` 你会发现选择器 `*dsp.o(.text.*)` 已经把整个 `dsp.c` 搬进去了。先验证这一点：

```bash
arm-none-eabi-nm build/ch.elf | grep cos_sin
# 预期：1000xxxx t cos_sin     ← 0x1000 开头即 CCM（若被内联查不到，改查 draw_waveform / arctantbl）
```

所以真正的练习是给**新住户**办入住。

### 第 3 关：给 `set_modulation` 办理 CCM 入住

1. **记录基线**：
   ```bash
   arm-none-eabi-nm build/ch.elf | grep set_modulation
   # 预期：0800xxxx T set_modulation（定义于 main.c:L179，尚未入选，住在 Flash）
   ```
2. **改名单**：编辑 `ccmfunc.ld`，在 [ccmfunc.ld:L14](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L14) 之后补一行：
   ```text
   	*main.o(.text.set_modulation)
   ```
   说明：搬迁名单物理上位于 `ccmfunc.ld`（经 `STM32F303xB.ld → rules.ld → rules_code.ld` 的 INCLUDE 链生效），改动它就是改动这套链接描述——不必也不应在 `STM32F303xB.ld` 里另起炉灶定义段。
3. **重新构建**：`make`（若改动 `.ld` 未触发重链，执行 `make clean && make`）。
4. **验证地址**：
   ```bash
   arm-none-eabi-nm build/ch.elf | grep set_modulation
   # 预期：1000xxxx T set_modulation     ← 地址从 0x08 段跳到 0x10 段，搬迁成功
   arm-none-eabi-objdump -h build/ch.elf | grep ccmfunc
   # 预期：.ccmfunc 的 Size 增大（约增加该函数的机器码长度），Vma 仍在 0x1000xxxx
   arm-none-eabi-size build/ch.elf
   # 观察：text 变化很小（镜像换了位置但总量近似）；ram4 占用 = .ccmfunc 新长度，须 < 8KB
   ```
5. **功能回归**：`make flash` 烧录，用 shell 的调制模式切换命令反复切换 AM/LSB/USB 等，确认行为正常——调用点无需任何修改，链接器已自动把所有跳转重定向到 CCM 新址。
6. **（可选）反向实验**：把 `*main.o(.text.*)`（整个 main.o）加进名单，预期链接器报 `ram4` 容量不足——亲眼看一次区域溢出保护如何工作。实验后还原 `ccmfunc.ld`。

**预期结果**：第 4 步两个地址证据齐全、第 5 步功能正常，即完整走通了「链接期代码搬迁」闭环。（**待本地验证**：本讲义写作环境无工具链与硬件。）

## 6. 本讲小结

- **版图**：`STM32F303xB.ld` 用 `MEMORY` 登记 Flash 128KB（`0x08000000`）、通用 SRAM 40KB（`0x20000000`）、CCM 8KB（`0x10000000`），用 `REGION_ALIAS` 决定各段去向；配置页 `0x0801f800` 的安全只靠「固件 Flash 占用 < 126KB」的约定。
- **遮蔽**：ChibiOS 官方 `rules.ld` 会 `INCLUDE rules_code.ld`，GNU ld 的搜索顺序使项目根目录的同名改版生效——它与官方版的唯一差异是在 `.text` 之前插入 `INCLUDE ccmfunc.ld`，整段链路一行上游代码都不用改。
- **双地址**：`.ccmfunc` 段 `> ram4 AT > RAM_INIT_FLASH_LMA`，VMA 在 CCM、LMA 在 Flash；`LOADADDR()` 算出三个边界符号，选择器按文件/段（甚至单函数）点名搬迁对象。
- **搬运与上锁**：`crt2.c` 的 `__late_init()` 在 `main` 之前逐字把镜像从 Flash 拷入 CCM，前后用 `SYSCFG->RCR`（`0x0000` → `0x00ff`）解锁/上锁 8 页写保护。
- **产物链**：`LDSCRIPT= STM32F303xB.ld` 经 `arm-none-eabi-gcc -T` 进入链接，`objcopy -O binary/ihex` 产出 `ch.bin/ch.hex`，`nm/objdump/size` 是验证内存布局的三件兵器。
- **边界意识**：CCM 是 CPU 专属、DMA 不可达，容量 8KB 由链接器硬约束；「哪些代码值得进 CCM」要用 `nm` 的地址证据说话，而不是凭感觉。

## 7. 下一步学习建议

- **补全启动链路**：`git submodule update --init` 检出 ChibiOS 后，通读 `os/common/startup/ARMCMx/compilers/GCC/crt0_v7m.s`，找到 `__early_init`/`__late_init` 的调用点与 `.data` 搬运代码，验证本讲 4.4.2 流程图中每个箭头，并顺带核对 `rules_stacks.ld` 中主栈/过程栈的位置安排。
- **精读 map 文件**：在链接命令中已有 `-Map` 输出（由 `rules.mk` 模板提供）的前提下，打开 `build/ch.map`，按地址顺序浏览每个输出段的确切起止与每个符号的落点——map 是链接脚本的「竣工图」，也是排查「段去哪了」的第一现场。
- **与 u5-l2 呼应**：回到 `dsp.c` 的 SIMD 热路径，结合本讲的地址知识思考：如果 8KB CCM 只能再容纳一个函数，你会选谁？用调用频次 × 每次指令数的直觉做一次「CCM 排位赛」，然后用 `nm` 检查现状与你的判断差多远。
- **警惕下一个坑**：当固件功能继续增长，用 4.1.4 的 size 检查持续盯住 `text < 126KB` 这条红线；也可以练习把 `flash0` 截短为 126k，把这个「约定」升级为链接期硬约束，体会构建期防错与运行期踩坑的代价差异。
