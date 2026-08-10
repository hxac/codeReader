# 同一实体多架构模式

## 1. 本讲目标

第 1 单元我们建立了「同一 entity 配多套 architecture（实现）」这个全局印象，但只停留在概念层面。本讲我们要钻进源码，把这套库最核心、也最值得学习的设计模式彻底讲透。读完本讲，你应当能够：

- 解释**为什么**同一个端口契约（entity）要提供多套实现，以及这样设计换来了什么、付出了什么。
- 区分三套实现的命名与分工：`xilinx_behavioural_*`（封装 Xilinx `xpm`）、`intel_behavioural_*`（封装 Intel `altera_mf`）、`own_behavioural_*`（不依赖任何厂商的行为级实现）。
- 看懂 `library xpm;` / `library altera_mf;` 这些上下句子句**为什么写在某个 architecture 之前**，而不是堆在文件开头。
- 掌握在例化时用 `entity work.xxx(arch_name)` 选定具体架构的语法，知道一次例化只会「激活」其中一套实现。

> 承接：本讲假定你已读过 u1-l1，了解 entity/architecture、IP 核、CDC、FPGA 这些术语，并知道本库的目标是「可复用 + 跨厂商 + 统一接口」。本讲不再重复项目定位，而是把镜头对准实现这一目标的核心手法。

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。

**什么是 entity 与 architecture。** 在 VHDL 里，`entity` 描述一个模块「对外长什么样」——它有哪些端口（输入输出信号）、哪些 generic（可配置参数）。`architecture` 描述这个模块「内部怎么实现」——用哪些进程、例化哪些底层元件。一个 `entity` 可以配多份 `architecture`，就像同一个接口（契约）可以有多种实现。综合或仿真时，你选定其中**一份** architecture 来用。

**为什么 FPGA 代码要分厂商。** 不同 FPGA 厂商（Xilinx/AMD、Intel/Altera）有各自专用的底层原语（primitive）。例如 Xilinx 用 `xpm_fifo_sync`、`xpm_cdc_single`，Intel 用 `scfifo`、`dcfifo`、`altclklock`。这些原语由厂商综合工具最优化地映射到自家芯片的硬核资源（BRAM、PLL、专用时钟树）。直接用它们，性能最好、面积最小；代价是**绑死在一家厂商**。

**什么是「行为级实现」。** 用纯 VHDL（寄存器、进程、数组）写出功能等价、但不调用任何厂商原语的版本。它的优点是**与厂商无关**，任何支持 VHDL-2008 的仿真器都能直接跑，移植性最好；缺点是综合出来的电路不如厂商原语那样贴合某颗芯片的硬核资源。本库把它命名为 `own_behavioural_*`，作为「兜底」实现。

把这三点连起来，就得到本库的核心设计：**每个 IP 的对外接口（entity）只定义一次，内部却同时提供「Xilinx 专用 / Intel 专用 / 厂商无关行为级」三套实现，让你按目标芯片自由选用。** 这就是「同一实体多架构模式」。

## 3. 本讲源码地图

本讲精读三个文件，它们恰好覆盖了「三套齐全」「两套」「三套齐全」三种情况，足以说明这个模式的形态与边界。

| 文件 | 作用 | 本讲用它说明什么 |
| --- | --- | --- |
| [ip/memories/fifo/fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) | 同步 FIFO | **主样板**：一个 entity + 三套 architecture，最适合对比 |
| [ip/ff_synchroniser/ff_synchroniser.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd) | 单比特跨时钟域同步器 | **边界案例**：只有 xilinx + intel 两套，README 表格是「简化版」 |
| [ip/memories/fifo/fifo_async.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd) | 异步 FIFO | 三套齐全；其 `own_behavioural_*` 内部又例化了同步器，可观察「模式嵌套」 |

辅助引用（用来佐证例化语法）：

| 文件 | 作用 |
| --- | --- |
| [ip/memories/fifo/tb/tb_fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd) | 同步 FIFO 测试台，同一个测试台里同时例化了 xilinx 与 own 两套实现做对比 |
| [ip/communication/spi/spi_interface.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd) | SPI 顶层，内部用 `entity work.fifo_async(own_behavioural_async_fifo)` 选定了具体架构 |

## 4. 核心概念与源码讲解

### 4.1 entity + 多 architecture 模式

#### 4.1.1 概念说明

「同一实体多架构」指的是：**端口契约只写一次，实现却准备多份，每次使用时二选一（或多选一）。** 这是一种典型的「接口与实现分离」思想，和面向对象语言里「一个接口、多个实现类」是同构的。

它解决的核心工程问题是**可移植性与性能的矛盾**：

- 想要最优性能 → 用厂商专用原语（绑死厂商）。
- 想要可移植、能开箱仿真 → 用行为级实现（性能略逊）。
- 两者都要 → 把它们做成**同一份对外接口下的可替换实现**，让使用方按需挑选。

关键认知：**一次例化（instantiation）只会绑定其中一套 architecture。** 综合后落到芯片上的，永远只是你选中的那一份；其余几套就像「备选实现」，不参与本次综合（但仍会被编译器分析，见 4.3）。

#### 4.1.2 核心流程

一个 IP 文件的内部结构遵循下面的固定骨架：

```
library ieee;  use ieee....;          ← 标准 IEEE 库（所有架构都要用）
[ use work.xxx_pkg.all; ]             ← 本项目共享包（按需）

entity <name> is                      ← 端口契约：只写一次
    generic ( ... );
    port ( ... );
end entity;

library xpm;  use xpm.vcomponents.all;            ← Xilinx 库（只给下一套用）
architecture xilinx_behavioural_<name> of <name> is ... end architecture;

library altera_mf;  use altera_mf.altera_mf_components.all;  ← Intel 库（只给下一套用）
architecture intel_behavioural_<name> of <name> is ... end architecture;

architecture own_behavioural_<name> of <name> is ... end architecture;  ← 无厂商库
```

要点：

1. `entity` 在文件顶部声明一次，定义所有架构共用的端口与 generic。
2. 每套 `architecture` 顺序排列，各自的厂商库声明紧贴在它**前面**。
3. 使用方在例化时用 `architecture` 名字选定其中一套。

#### 4.1.3 源码精读

先看 `fifo_sync` 的 entity——整份契约只出现这一次，三套实现共用它：

[fifo_sync.vhd:7-25](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L7-L25) —— 定义 `fifo_sync` 的对外端口（时钟、复位、读写口、`full`/`empty`/`words_stored`）。注意 `write_data`/`read_data` 用的是**非约束** `std_ulogic_vector`，宽度留给例化方决定，三套架构都要遵守这同一份契约。

紧接着，文件里依次出现三套实现，名字遵循统一前缀：

- [fifo_sync.vhd:35](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L35) —— `architecture xilinx_behavioural_sync_fifo`
- [fifo_sync.vhd:107](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L107) —— `architecture intel_behavioural_sync_fifo`
- [fifo_sync.vhd:147](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L147) —— `architecture own_behavioural_sync_fifo`

三份 `architecture ... of fifo_sync` 共享同一个 entity 名，但内部实现截然不同。测试台正是利用这一点，**在同一个 tb 里同时例化两套做对比**：

[tb_fifo_sync.vhd:570](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L570) —— 例化名为 `DuT_xilinx`，选 Xilinx 实现。
[tb_fifo_sync.vhd:587](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L587) —— 例化名为 `DuT_own`，选自研行为级实现。

同一份激励可以同时跑两套实现并比对结果——这正是「同一接口、多实现」带来的直接红利：**回归测试天然成为跨实现的等价性检查。**

#### 4.1.4 代码实践

**目标：** 直观感受「一份契约，多份实现，例化时选其一」。

**步骤：**

1. 打开 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd)，确认 `entity fifo_sync` 在文件里**只出现一次**。
2. 在文件里搜索 `architecture `，数一下共有几份、各自叫什么名字（应当看到 3 份）。
3. 打开 [tb_fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd)，定位到第 570 行与第 587 行，确认两个 DUT 用的是同一个 entity、不同的 architecture。

**需要观察的现象：** 两个 DUT 共用同一组激励信号，输出波形应当功能等价（同样写满再读空、同样的 `full`/`empty` 时序）。

**预期结果：** 即便底层一个调 Xilinx 原语、一个用纯 RTL，对外行为一致。**待本地验证：** 实际仿真需配置 `use_xilinx_libs`（见 4.3 与 u1-l3）。

#### 4.1.5 小练习与答案

**练习 1：** 如果一个 entity 写了三套 architecture，综合后芯片上会有几份 FIFO 电路？

> **答案：** 只有一份——被你例化时选中的那一套。其余两套不参与本次综合（但仍参与编译分析）。

**练习 2：** 为什么把多套实现塞进「同一个 entity」、而不是做成三个独立 entity（如 `fifo_sync_xilinx`、`fifo_sync_intel`、`fifo_sync_own`）？

> **答案：** 因为它们**对外行为完全一致、端口契约相同**，理应共享一个名字。共用 entity 后，上层模块的端口连接代码不用改，只需换一个 architecture 名就能切换厂商；若做成三个 entity，每换一家厂商就要改上层连线，违背了「接口稳定」的初衷。

---

### 4.2 三种实现的命名与分工：xilinx / intel / own

#### 4.2.1 概念说明

本库用一套**命名约定**来标识每份实现所属的阵营，约定是 `<厂商>_behavioural_<功能>`：

| 前缀 | 含义 | 依赖的厂商库 | 例化的核心原语（以 FIFO 为例） |
| --- | --- | --- | --- |
| `xilinx_behavioural_` | Xilinx 专用实现 | `xpm`（及隐含的 UNISIM） | `xpm_fifo_sync` / `xpm_fifo_async` |
| `intel_behavioural_` | Intel/Altera 专用实现 | `altera_mf` | `scfifo` / `dcfifo` |
| `own_behavioural_` | 厂商无关行为级实现 | 无 | 自研 RTL + `dual_clock_dual_port_ram` |

> 术语：`xpm` = Xilinx Parameterized Macros，Xilinx 提供的一组可参数化宏；`altera_mf` = Altera Megafunction，Intel/Altera 的兆功能库。它们都是厂商自带的、可综合的 IP 原语。

注意名字里的 `behavioural`（行为级）是这三套共有的后缀风格，不代表「不可综合」——它们都可综合，只是实现的「抽象层级」不同。

#### 4.2.2 核心流程

三套实现的「分工」可以这样理解：

```
                ┌─────────────────────────────┐
   统一端口      │  entity fifo_sync            │   ← 契约
   (契约)   ──▶ │  full/empty/words_stored ... │
                └──────────────┬───────────────┘
                               │ 三选一（例化时决定）
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
 xilinx_behavioural_     intel_behavioural_      own_behavioural_
   调 xpm_fifo_sync         调 scfifo              自研 RTL 指针+RAM
   （性能最优/绑 Xilinx）   （性能最优/绑 Intel） （可移植/可开箱仿真）
```

厂商原语通常暴露**大量可选输出**（各种 almost-full、ECC 校验、复位忙标志等），而本库的统一端口只用到其中一小部分，于是厂商架构里会有一堆「接到了却不用」的信号——这就是 4.3 与综合实践里要看到的 `*_unconnected` 信号。行为级实现则完全由本库自己掌控，需要几个信号就生成几个，没有多余尾巴。

#### 4.2.3 源码精读

**Xilinx 实现**——封装 `xpm_fifo_sync`，并把用不到的厂商输出悬空：

[fifo_sync.vhd:55-101](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L55-L101) —— 例化 `xpm_fifo_sync`，generic 把本库的 `FIFO_DEPTH`、数据宽度映射给 Xilinx 原语。

[fifo_sync.vhd:40-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L40-L51) —— 一长串 `*_unconnected` 信号：`prog_full_unconnected`、`overflow_unconnected`、`almost_full_unconnected` …… 它们对应 `xpm_fifo_sync` 暴露、但本库端口用不到的输出，必须在 `port map` 里给个落点（接到一个不用的内部信号），否则端口映射不完整。

`words_stored` 由厂商的 `wr_data_count` 直接换算得到：

[fifo_sync.vhd:53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L53) —— `words_stored <= to_integer(unsigned(wr_data_count));`，把 Xilinx 提供的写侧计数转成本库统一的 `natural`。

**Intel 实现**——封装 `scfifo`：

[fifo_sync.vhd:110-139](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L110-L139) —— 例化 `scfifo`，generic 用 `lpm_numwords`/`lpm_width`/`lpm_widthu` 描述深度与位宽，`intended_device_family` 由 `INTEL_DEVICE_FAMILY` 这个 generic 传入。

注意 Intel 实现里**没有** `*_unconnected` 信号——`scfifo` 的端口集合较小，本库几乎全用上了。`read_data_valid` 还要自己补一拍寄存：

[fifo_sync.vhd:138](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L138) —— 因为 `scfifo` 不直接提供 valid，需要用 `read_enable` 打一拍来对齐。

**自研行为级实现**——完全不碰厂商原语，自己用指针 + RAM 搭：

[fifo_sync.vhd:231-240](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L231-L240) —— 例化的是本库自己的 `dual_clock_dual_port_ram`，不依赖任何厂商库；读写指针与填充水位都在 [fifo_sync.vhd:189-214](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L189-L214) 用普通进程实现。

**边界案例：`ff_synchroniser` 只有「两套」。** 它的源码里只能找到 xilinx 和 intel 两套：

[ff_synchroniser.vhd:43](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L43) —— `architecture xilinx_behavioural_ff_synchroniser`，封装 `xpm_cdc_single`。
[ff_synchroniser.vhd:66](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L66) —— `architecture intel_behavioural_ff_synchroniser`，用显式同步链 + `altera_attribute`。

这里没有第三份 `own_behavioural_*`。但 README 的 Technology Support 表把它的 Own/Behavioral 标成「Yes」——这是因为 Intel 那份实现的注释明确写了：它本身就能当厂商无关版本用，只是 `altera_attribute` 被忽略而已：

[ff_synchroniser.vhd:60-64](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L60-L64) —— 注释说明这份「technology specific synchroniser」同时可作 technology independent 使用。

> ⚠️ 教训：**以真实源码为准，而不是 README 表格。** README 的支持矩阵是简化呈现，遇到「到底有几套 architecture」「某套到底依赖什么」这类问题，要去 `.vhd` 文件里数 `architecture` 关键字。

#### 4.2.4 代码实践

**目标：** 量化「厂商原语端口多、行为级实现端口精」这一差异。

**步骤：**

1. 在 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) 里，分别统计三套 architecture 各自例化的元件、以及 `*_unconnected` 信号的数量。
2. 把结果填进综合实践（第 5 节）的对比表。

**需要观察的现象：** Xilinx 那套的 `*_unconnected` 信号最多（因为 `xpm_fifo_sync` 暴露的端口最丰富）；Intel 次之；自研行为级一套也没有。

**预期结果：** 直观体会「封装一个功能强大但端口庞杂的厂商原语」需要多少「悬空接线」的样板代码。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 Xilinx 实现里要把那些用不到的厂商输出接到 `*_unconnected` 信号，而不能直接省略？

> **答案：** VHDL 的元件例化要求端口映射完整；`xpm_fifo_sync` 这些是 out 端口，不接会在某些工具下报警告甚至报错。接到一个内部 signal（命名带 `_unconnected` 以表明意图）是标准做法，相当于「显式悬空」。

**练习 2：** `ff_synchroniser` 没有 `own_behavioural_*`，那它算不算「厂商无关」可用？

> **答案：** 仍可用——用它的 `intel_behavioural_ff_synchroniser`，因为该实现的核心是一段普通同步链进程，`altera_attribute` 在非 Intel 工具下会被忽略。这就是 README 把它标成「Yes」的实际含义。但要严格「与厂商完全无关」，需自行确认 `altera_attribute` 在你的工具链下确被忽略。

---

### 4.3 厂商库的声明位置：library xpm / altera_mf 写在 architecture 之前

#### 4.3.1 概念说明

VHDL 的 `library` 与 `use` 是**上下句子句（context clause）**。它们的位置决定了「可见范围」。本库的一个关键风格是：**厂商库声明不堆在文件顶部，而是紧贴在「需要它的那一份 architecture」之前。**

这样做有两个好处：

1. **可读性：** 读者一眼就能看出「这一套实现依赖哪个厂商库」，依赖关系局部化、就近可见。
2. **职责隔离：** `own_behavioural_*` 之前**没有任何厂商库声明**，这正是「厂商无关」在源码层面的直接体现——它不 `use` 任何 `xpm`/`altera_mf`。

#### 4.3.2 核心流程

上下文子句的作用域遵循「自上而下、对后续设计单元生效」的规则：

```
library ieee; use ieee.std_logic_1164.all;   ← 对文件里【所有】后续单元生效
entity fifo_sync is ... end entity;          ← entity 用到 ieee

library xpm; use xpm.vcomponents.all;        ← 只对【紧随其后】的 architecture 生效
architecture xilinx_behavioural_sync_fifo ... ← 用到 xpm（xpm_fifo_sync）

library altera_mf; use altera_mf.altera_mf_components.all;  ← 只对【紧随其后】的 architecture 生效
architecture intel_behavioural_sync_fifo ...  ← 用到 altera_mf（scfifo）

architecture own_behavioural_sync_fifo ...    ← 【前面没有任何厂商库】，故厂商无关
```

注意一个细节：`library xpm;` 出现在 `entity fifo_sync` 之后（entity 本身只用 `ieee`），所以 entity 的声明不依赖厂商库；厂商库只服务于各自的 architecture。

> 关于「编译 vs. 选用」的一个提醒：因为三套 architecture 共用一个文件，分析器在编译该文件时仍会分析全部 architecture，因此工具链通常需要 `xpm` 与 `altera_mf` 库都「在位」才能完成整文件编译（这也是 `vhdl_ls.toml` 把它们声明为第三方库、CI 用 `nvc --install` 预装它们的原因，见 u1-l4）。但「使用」层面，你只需例化 `own_behavioural_*` 即可得到一份厂商无关的行为。两者不矛盾——前者是「分析全文件」的要求，后者是「选用哪套实现」的自由。

#### 4.3.3 源码精读

看 `fifo_sync.vhd` 里厂商库声明的精确落点：

[fifo_sync.vhd:27-28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L27-L28) —— `library xpm; use xpm.vcomponents.all;`，位于 entity 结束（L25）之后、Xilinx architecture（L35）之前。
[fifo_sync.vhd:104-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L104-L105) —— `library altera_mf; use altera_mf.altera_mf_components.all;`，位于 Xilinx architecture 结束（L102）之后、Intel architecture（L107）之前。
[fifo_sync.vhd:142-147](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L142-L147) —— `own_behavioural_sync_fifo` 之前只有注释、没有任何厂商库声明。

`ff_synchroniser.vhd` 的写法完全一致，可以互为佐证：

[ff_synchroniser.vhd:35-36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/ff_synchroniser/ff_synchroniser.vhd#L35-L36) —— Xilinx 库声明紧贴 `xilinx_behavioural_ff_synchroniser`。

#### 4.3.4 代码实践

**目标：** 验证「厂商库声明的可见范围止于对应 architecture」。

**步骤：**

1. 在 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd) 里，确认 `library xpm;` 出现在第 27 行、而 entity 在第 7–25 行——即 entity 声明段并未引用 `xpm`。
2. 思考：如果把 `library xpm; use xpm.vcomponents.all;` 移到文件最顶部（`library ieee;` 旁边），功能会坏吗？风格上又有什么损失？

**需要观察的现象：** 移到顶部后功能不会坏（可见范围只会变大），但读者将无法从「architecture 上方有没有厂商库声明」一眼判断每套实现的厂商依赖——局部性丧失。

**预期结果：** 理解为什么本库坚持「厂商库声明就近写」是一种刻意的工程风格，而非随性排版。

#### 4.3.5 小练习与答案

**练习 1：** `own_behavioural_sync_fifo` 之前为什么刻意不放 `library xpm;`？

> **答案：** 因为它不依赖 `xpm`。厂商库声明的不存在，本身就是「这套实现厂商无关」的源码级证据。一旦加上，反而误导读者以为它依赖 Xilinx。

**练习 2：** 假设你的环境里完全没有 `altera_mf` 库。你只想用 Xilinx 实现，文件能编译通过吗？

> **答案：** 严格地说，因为三套 architecture 同处一个文件，分析器会分析 Intel 那套，从而需要 `altera_mf` 在位。实际工程里要么预装所有厂商库（CI、`vhdl_ls.toml` 的做法），要么接受「整文件编译需全库在位」这一前提，再用例化选用你想要的那一套。**待本地验证：** 不同仿真器对「分析未使用 architecture」时缺失库的处理可能不同。

---

### 4.4 例化时如何选定 architecture：entity work.xxx(arch_name)

#### 4.4.1 概念说明

光定义多套 architecture 还不够，使用方必须能**指定**用哪一套。VHDL-2008 的直接例化（direct instantiation）语法支持在 entity 名后用括号写出 architecture 名：

```vhdl
<实例名>: entity work.<entity名>(<architecture名>)
    [ generic map ( ... ) ]
    port map ( ... );
```

括号里的 architecture 名就是「选择开关」。**不写括号**时，工具会按默认规则选用（通常是最后编译的那份，或配置指定的那份），行为不可预期——所以本库在需要明确选型时**总是写全** `(...)`。

#### 4.4.2 核心流程

```
    使用方代码
        │
        ├─ 写 entity work.fifo_sync(xilinx_behavioural_sync_fifo)  → 选 Xilinx 原语实现
        ├─ 写 entity work.fifo_sync(intel_behavioural_sync_fifo)   → 选 Intel 原语实现
        └─ 写 entity work.fifo_sync(own_behavioural_sync_fifo)     → 选厂商无关实现
```

切换厂商实现 = 改括号里一个名字，端口连线一行不动。

#### 4.4.3 源码精读

仓库里**真实存在**的选定语法，遍布设计源码与测试台：

[spi_interface.vhd:231](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L231) —— SPI 顶层内部例化异步 FIFO 时，显式选定 `own_behavioural_async_fifo`：
```vhdl
fifo_async_inst: entity work.fifo_async(own_behavioural_async_fifo)
```

[tb_fifo_sync.vhd:570](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L570) 与 [tb_fifo_sync.vhd:587](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L587) —— 同一测试台里分别选定 Xilinx 与 own 两套做对比。

**一个值得注意的嵌套细节：** `fifo_async.vhd` 的 `own_behavioural_async_fifo`（号称厂商无关的那套）在内部例化同步器时，却**硬编码**选定了 Xilinx 架构：

[fifo_async.vhd:258](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258) 与 [fifo_async.vhd:271](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L271) —— 写死 `ff_synchroniser_vector(xilinx_behavioural_ff_synchroniser_vector)`。

这意味着：选用 `own_behavioural_async_fifo` 时，其叶子节点（指针同步器）其实仍然走的是 Xilinx 实现，因而并不「100% 厂商无关」。这是读源码时应当留意的一个真实细节——**「自研行为级」是分层的，顶层自研不等于所有叶子都自研**。移植到 Intel 平台时，这里需要手动改成对应架构（或改用 `ff_synchroniser_vector` 的 intel 实现）。

#### 4.4.4 代码实践

**目标：** 写一段合法的例化代码，选定自研行为级 FIFO，并理解参数如何传递。

**步骤：** 参照 [tb_fifo_sync.vhd:587](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/tb/tb_fifo_sync.vhd#L587) 的真实写法，写下面这段示例（**示例代码**，非项目原有文件）：

```vhdl
-- 示例代码：例化一个 8 位宽、深度 16 的厂商无关同步 FIFO
signal clk, rst_n, wr_en, rd_en : std_ulogic;
signal wr_data, rd_data         : std_ulogic_vector(7 downto 0);
signal rd_valid, full, empty    : std_ulogic;
signal stored                   : natural;

my_fifo_inst: entity work.fifo_sync(own_behavioural_sync_fifo)
    generic map (
        FIFO_DEPTH                       => 16,
        UNDER_AND_OVERFLOW_ASSERTIONS    => true
    )
    port map (
        sys_clk          => clk,
        sys_rst_n        => rst_n,
        write_enable     => wr_en,
        write_data       => wr_data,
        read_enable      => rd_en,
        read_data        => rd_data,
        read_data_valid  => rd_valid,
        full             => full,
        empty            => empty,
        words_stored     => stored
    );
```

**需要观察的现象：**

1. 把括号里的 `own_behavioural_sync_fifo` 改成 `xilinx_behavioural_sync_fifo`，端口连线**一行都不用动**——只换了一个名字，就换了底层实现。
2. `write_data`/`read_data` 是非约束端口，宽度由外部信号（这里是 8 位）决定，三套架构都自动适配。

**预期结果：** 编译能通过即说明语法与端口契约理解正确。**待本地验证：** 若选 Xilinx 架构，需在 `test_runner.py` 里设 `use_xilinx_libs=True`（原因见 u1-l3 / 第 5 节）。

#### 4.4.5 小练习与答案

**练习 1：** 例化时如果**不写**括号里的 architecture 名，会发生什么？

> **答案：** 工具按默认规则选用一份 architecture（通常是最后分析的那份，或由配置指定），结果不可预期。本库要求在需要明确选型时写全 `(...)`，避免歧义。

**练习 2：** 选用 `own_behavioural_async_fifo` 是否就一定不依赖任何厂商库？结合 4.4.3 的事实回答。

> **答案：** 不一定。顶层 `own_behavioural_async_fifo` 自身不调厂商原语，但它内部例化的 `ff_synchroniser_vector` 当前被硬编码为 `xilinx_behavioural_ff_synchroniser_vector`，所以叶子层仍依赖 `xpm`。移植时需同步修改这一处选定。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「三套 architecture 对比表」，并动手写一段选定自研实现的例化代码。

### 任务 1：对比表（基于 fifo_sync.vhd 真实源码）

阅读 [fifo_sync.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd)，把下表填完整（参考答案见后）：

| 维度 | `xilinx_behavioural_sync_fifo` | `intel_behavioural_sync_fifo` | `own_behavioural_sync_fifo` |
| --- | --- | --- | --- |
| 例化的核心元件 | ？ | ？ | ？ |
| 该元件来自哪个库 | ？ | ？ | ？ |
| `*_unconnected` 信号数量 | ？ | ？ | ？ |
| `words_stored` 由谁提供 | ？ | ？ | ？ |
| 是否需要厂商仿真库才能跑 | ？ | ？ | ？ |

**参考答案：**

| 维度 | `xilinx_behavioural_sync_fifo` | `intel_behavioural_sync_fifo` | `own_behavioural_sync_fifo` |
| --- | --- | --- | --- |
| 例化的核心元件 | `xpm_fifo_sync`（[L55](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L55)） | `scfifo`（[L110](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L110)） | `dual_clock_dual_port_ram`（[L231](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L231)） |
| 该元件来自哪个库 | `xpm`（[L27-28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L27-L28)） | `altera_mf`（[L104-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L104-L105)） | 本库自研（无厂商库） |
| `*_unconnected` 信号数量 | 12 个（[L40-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L40-L51)） | 0 个（端口几乎全用上） | 0 个 |
| `words_stored` 由谁提供 | `wr_data_count` 换算（[L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L53)） | `usedw` 换算（[L139](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L139)） | 自维护 `fifo_fill_level`（[L216](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_sync.vhd#L216)） |
| 是否需要厂商仿真库才能跑 | 是（`use_xilinx_libs=True`） | 是（Intel 库） | 否（可开箱仿真） |

### 任务 2：写一段选定自研实现的例化代码

直接采用 4.4.4 的示例代码，把 `fifo_sync(own_behavioural_sync_fifo)` 例化到一个你自己的顶层里。完成后，做一个小实验：把括号里的架构名在 `own_behavioural_sync_fifo` ↔ `xilinx_behavioural_sync_fifo` 之间切换，确认**端口连线无需改动**，从而亲手验证「同一接口、可替换实现」的核心价值。

### 任务 3（进阶，源码阅读型）：追踪「自研实现并不彻底」

阅读 [fifo_async.vhd:258](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L258) 与 [fifo_async.vhd:271](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L271)，回答：如果要把整个 `own_behavioural_async_fifo` 做成「真正厂商无关」，需要修改哪两行？把架构名改成什么？（提示：`ff_synchroniser_vector` 的 intel 架构，或一个不依赖 `xpm` 的版本。）

## 6. 本讲小结

- 「同一实体多架构」= **端口契约只写一次（entity），实现准备多份（architecture）**，用「接口与实现分离」换取可移植性与性能的兼顾。
- 三套实现按 `<厂商>_behavioural_<功能>` 命名：`xilinx_behavioural_*` 调 `xpm`、`intel_behavioural_*` 调 `altera_mf`、`own_behavioural_*` 不依赖任何厂商库。
- 厂商库声明 `library xpm;`/`library altera_mf;` **紧贴各自 architecture 之前**，是刻意的工程风格：依赖局部化、可读性强，且 `own_behavioural_*` 前没有厂商库本身就是「厂商无关」的源码证据。
- 用 `entity work.xxx(arch_name)` 在例化时选定具体架构；括号里的名字就是「厂商切换开关」，端口连线无需改动。
- **以真实源码为准**：`ff_synchroniser` 实际只有 xilinx + intel 两套（README 表格是简化呈现）；`own_behavioural_async_fifo` 的叶子同步器被硬编码为 Xilinx 架构——「自研」是分层的，顶层自研 ≠ 全树自研。
- 厂商原语端口庞杂，导致 Xilinx 实现里有一长串 `*_unconnected` 悬空信号；行为级实现则干净利落、按需生成。

## 7. 下一步学习建议

本讲建立的是「模式」的认知。接下来按兴趣选择：

- **想看厂商库在仿真/CI 里如何被装配** → 读 u2-l2「厂商仿真库与库声明」，理解 `use_xilinx_libs` 解 `glbl.GSR` 报错的原理，以及 `vhdl_ls.toml` 与 CI 如何提供这些库。
- **想看「选定实现」如何与时序属性配合** → 读 u2-l3「综合属性、防优化与时钟门控策略」，理解 `preserve`/`dont_touch`/`altera_attribute` 如何保护关键寄存器不被优化。
- **想横向验证这个模式在其他 IP 上的形态** → 直接打开 `ip/pll/pll.vhd`（全库唯一**没有** `own_behavioural_*` 的 IP，所以它在 CI 里被排除）、`ip/memories/ram/` 下的 RAM，数一数各自有几套 architecture、各自依赖哪个厂商库。
