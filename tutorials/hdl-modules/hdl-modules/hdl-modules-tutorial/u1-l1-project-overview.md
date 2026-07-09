# hdl-modules 是什么：项目定位与设计哲学

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向「完全没接触过 hdl-modules、甚至刚入门 FPGA/VHDL」的读者。学完本讲后，你应该能够：

- 用一句话向别人解释 **hdl-modules 是什么、解决什么问题**。
- 说出项目的**许可证**（BSD 3-Clause）以及它对你商业项目的意义。
- 列出项目包含的 **14 个模块**，并大致知道每个模块负责哪一类工作。
- 理解贯穿全项目的三条**设计哲学**：可复用与可移植、面积/资源优化、用 generic（类属参数）开关功能以省资源。
- 知道项目代码的「单一信息源」在哪里——为什么 README 的文字其实是 Python 代码生成的。

本讲**不要求**你会写 VHDL，也不要求你装好工具链。我们只读文档与元信息文件，建立全局认知。后续讲义才会深入具体源码。

## 2. 前置知识

在开始前，先用大白话解释几个本讲会反复出现的术语：

- **FPGA（现场可编程门阵列）**：一种可以通过代码「重新连线」的芯片。你用硬件描述语言写代码，综合后烧进芯片，它就变成你设计的电路。
- **VHDL**：一种硬件描述语言（另一种常见的是 Verilog）。hdl-modules 全部用 VHDL-2008 标准编写。
- **构建块（building block）**：可被反复实例化、拼接成更大电路的小模块，比如一个 FIFO、一个寄存器文件、一个跨时钟域同步器。就像乐高积木。
- **IP 核（Intellectual Property core）**：可以复用的硬件功能模块，含义和「构建块」接近。
- **generic（类属参数）**：VHDL 里类似软件「构造参数」的机制。实例化一个模块时，你可以传 `width => 8`、`enable_last => true` 这样的参数，让**同一个模块**生成出不同形态的电路。
- **资源占用（resource utilization）**：FPGA 上的 LUT、触发器（FF）、块 RAM（BRAM）等是有限且昂贵的，所以「这个模块用了多少资源」是设计时永远要关心的问题。
- **CDC（Clock Domain Crossing，跨时钟域）**：当两个电路工作在不同时钟下，信号从一边传到另一边需要特殊处理，否则会出现亚稳态。resync 模块就是干这个的。
- **AXI / AXI-Stream / AXI-Lite**：ARM 提出的一套片上总线协议族。AXI 是完整版，AXI-Lite 是简化版（用于寄存器访问），AXI-Stream 是纯数据流（带握手，无地址）。hdl-modules 大量使用了这套协议。

理解这些就够了，本讲后面遇到新词会随文解释。

## 3. 本讲源码地图

本讲只涉及项目的「门面」文件，不碰具体 VHDL 逻辑。涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `readme.rst` | 仓库根目录的项目说明文件，GitHub 首页展示。包含项目定位、设计哲学与模块速览。 |
| `hdl_modules/about.py` | 项目元信息：标语（slogan）、仓库/网站 URL，以及生成 README 文本的函数。是 README 文字的「单一信息源」。 |
| `license.txt` | 许可证全文，确认为 BSD 3-Clause。 |
| `doc/sphinx/getting_started.rst` | 官方「快速上手」文档，说明克隆、依赖、源码集成与约束等使用方式。 |

> 小贴士：`rst` 是 reStructuredText 的缩写，一种类似 Markdown 的纯文本标记语言，Python/Sphinx 生态常用。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位与许可证**、**模块全景**、**设计哲学**。

### 4.1 项目定位与许可证

#### 4.1.1 概念说明

学一个开源项目，第一件事永远是搞清楚两件事：

1. **它是什么**——属于哪个领域、给谁用。
2. **它用什么许可证**——决定你能不能、以及怎么在商业产品里用它。

hdl-modules 定位非常明确：它是一组**可复用、高质量、经过同行评审（peer-reviewed）的 VHDL 构建块**。换句话说，它是 FPGA 工程师的「乐高零件库」，你不必每次都自己造 FIFO、造跨时钟域同步器，而是直接拿来拼。

许可证是 **BSD 3-Clause**，这是一个非常宽松的开源许可证：允许商业使用、修改、再分发，只要保留版权声明、不在衍生产品里用作者名做背书即可。对工程实践来说，这意味着你基本可以放心地把它用在公司项目里。

#### 4.1.2 核心流程：项目如何对外「自我介绍」

项目的自我介绍有一个有趣的工程细节——它不是把项目简介写死在 README 里，而是用 **Python 代码作为单一信息源**，再生成出不同场合的 README：

1. `hdl_modules/about.py` 里集中保存标语、URL，并提供一个 `get_readme_rst()` 函数。
2. 这个函数根据参数（`include_extra_for_github` / `include_extra_for_website`）生成「GitHub 版」和「网站版」两份略有不同的 README 文本。
3. 仓库根目录的 `readme.rst` 文件，其内容正是「GitHub 版」的输出。

这样做的好处是：标语和项目描述只写在一处，GitHub 首页和官网不会出现说法不一致的情况。

#### 4.1.3 源码精读

先看 README 开头的项目一句话定位：

[readme.rst:29-31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L29-L31) — 这三行是整段话的核心：hdl-modules 是「reusable, high-quality, peer-reviewed VHDL building blocks」（可复用、高质量、同行评审的 VHDL 构建块），并以 BSD 3-Clause 开源。

再看 Python 侧的「单一信息源」。URL 常量集中在这里：

[hdl_modules/about.py:11-12](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/about.py#L11-L12) — 把仓库地址与网站地址定义成常量，方便全文统一引用。

标语由一个函数返回，并被 Python 包的 docstring 复用：

[hdl_modules/about.py:15-22](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/about.py#L15-L22) — `get_short_slogan()` 返回那句「A collection of reusable, high-quality, peer-reviewed VHDL building blocks」。注释特意提醒：这句标语要与 README、网站上保持一致——这正是把它放在代码里的原因。

README 生成函数的文档注释解释了「为什么要在两个地方重复」：

[hdl_modules/about.py:28-41](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/about.py#L28-L41) — 说明 GitHub 不支持 README 里的 RST 文件包含指令（`include`），所以不得不「在两处重复」，于是用这个函数集中生成、避免手工同步出错。

最后看许可证。第一行是版权声明，随后是标准 BSD 3-Clause 三条款：

[license.txt:1](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/license.txt#L1) — 版权归 Lukas Vik 所有。

[license.txt:3-11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/license.txt#L3-L11) — 标准 BSD 3-Clause 条款：保留版权声明、二进制分发需在文档中复制声明、不得用作者名为衍生产品背书，以及免责条款。

#### 4.1.4 代码实践

**实践目标**：亲手验证「标语在代码里、且 README 与之同源」这一说法，并确认许可证。

**操作步骤**：

1. 克隆仓库并进入目录（如果你还没克隆）：

   ```bash
   git clone https://github.com/hdl-modules/hdl-modules.git
   cd hdl-modules
   ```

2. 用 Python 直接调用 `about.py` 里的函数，打印标语：

   ```bash
   python -c "from hdl_modules.about import get_short_slogan; print(get_short_slogan())"
   ```

3. 查看 `license.txt` 的头部：

   ```bash
   head -n 3 license.txt
   ```

**需要观察的现象**：

- 第 2 步应输出：`A collection of reusable, high-quality, peer-reviewed VHDL building blocks`。
- 这个字符串与 `readme.rst` 第 29 行的措辞完全一致（仅大小写不同），印证「单一信息源」。
- 第 3 步应看到 `Copyright (c) Lukas Vik ...` 以及 `Redistribution and use ...` 的字样，这是 BSD 3-Clause 的标志。

**预期结果**：标语输出正确；`license.txt` 顶部三行包含版权与 BSD 3-Clause 的第一句。若你的环境没装 Python，可以直接用编辑器打开 `about.py` 第 22 行与 `readme.rst` 第 29 行人工比对，结论一致。

> 若无法运行上述命令，标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：BSD 3-Clause 是否允许你把 hdl-modules 用在商业闭源产品里？需要满足什么义务？

> **答案**：允许。义务是：在源码与二进制分发中保留版权声明、本条件列表与免责声明；不得用版权人或贡献者的名为衍生产品做背书。详见 [license.txt:3-11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/license.txt#L3-L11)。

**练习 2**：为什么项目要把标语写进 `about.py`，而不是直接写在 README 里？

> **答案**：为了让 GitHub 首页、官方网站、Python 包文档等「多处」共用同一句话，避免手工维护导致说法不一致。代码是单一信息源，README 是生成产物之一。

---

### 4.2 模块全景：14 个构建块分别做什么

#### 4.2.1 概念说明

一个 IP 库好不好用，很大程度上取决于它「覆盖了哪些常见需求」。hdl-modules 的模块划分非常贴合真实 FPGA 工程的痛点：握手、跨时钟域、FIFO、总线、寄存器、数学运算、专用外设，以及仿真验证用的总线功能模型（BFM）。

注意一个容易被忽略的细节：仓库 `modules/` 目录下实际有 **14 个模块**，而 README 的「at a glance（速览）」清单只重点列出了其中 **12 个**（没有单独列出 `axi_stream` 与 `ring_buffer`）。所以读 README 时要明白：那是一份精选清单，不是完整清单。完整清单以 `modules/` 目录为准。

#### 4.2.2 核心流程：从需求到模块

遇到下面这些典型需求时，你可以这样把需求「映射」到对应模块：

```text
「两个时钟域之间要传信号」      → resync（CDC）
「数据流要缓冲一下」            → fifo / hard_fifo / axi_stream
「要挂个寄存器给 CPU 读写」     → register_file + axi_lite
「要把数据高效写进 DDR」        → dma_axi_write_simple
「要产生一个正弦波」            → sine_generator
「要在仿真里驱动 AXI 总线」     → bfm
「需要点杂项小工具」            → common / math / lfsr
```

#### 4.2.3 源码精读

README 的模块速览清单在这里（节选首尾）：

[readme.rst:49-86](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L49-L86) — 逐条列出 axi、axi_lite、bfm、common、dma_axi_write_simple、fifo、hard_fifo、lfsr、math、register_file、resync、sine_generator 共 12 个模块的一句话说明。

为了给你一个**完整**的全景，下表把 README 速览里的 12 个，加上实际存在但未在速览里列出的 2 个（axi_stream、ring_buffer），共 **14 个模块**整理在一起（后两个的描述依据其源码文件用途）：

| 模块 | 一句话职责 |
|------|-----------|
| `axi` | AXI3/AXI4 总线的交叉栏（crossbar）、FIFO、CDC 等。 |
| `axi_lite` | AXI-Lite 总线的交叉栏、FIFO、CDC 等（用于寄存器访问）。 |
| `axi_stream` | AXI-Stream 数据流的类型包与 FIFO（接口更贴近标准 AXI-Stream）。 |
| `bfm` | 仿真用的总线功能模型（AXI/AXI-Lite/AXI-Stream 的 master/slave）。 |
| `common` | 杂项但常用的小工具（握手、属性、类型、去抖动等）。 |
| `dma_axi_write_simple` | 把 AXI-Stream 数据高效写入 DDR 的 DMA，附带完整 C++ 驱动。 |
| `fifo` | 同步/异步 FIFO，采用类 AXI-Stream 的握手接口。 |
| `hard_fifo` | 对 Xilinx 硬 FIFO 原语的封装，提供更干净的握手接口。 |
| `lfsr` | 最大长度线性反馈移位寄存器，用于伪随机数生成。 |
| `math` | 常用数学运算的硬件实现（饱和、舍入截断、除法等）。 |
| `register_file` | 通用寄存器文件，以及仿真用的寄存器操作支持包。 |
| `resync` | 各类信号与总线的跨时钟域（CDC）同步，附带正确约束。 |
| `ring_buffer` | 环形缓冲写入（环形地址管理的存储结构）。 |
| `sine_generator` | 专业级正弦波形发生器（即 DDS / NCO）。 |

> 这张表你不用背。它只是让你建立「这个库大概能干什么」的直觉。后续每一单元都会单独精讲其中一组。

#### 4.2.4 代码实践

**实践目标**：动手确认「README 速览清单」与「实际模块目录」的差异。

**操作步骤**：

1. 列出 `modules/` 下的全部子目录：

   ```bash
   ls modules
   ```

2. 把上一步看到的目录数与 `readme.rst` 第 49–86 行列出的模块数做对比。

3. 找出「README 没列、但目录里存在」的模块，打开它们的 readme 看看是做什么的：

   ```bash
   cat modules/axi_stream/readme.rst
   cat modules/ring_buffer/readme.rst
   ```

**需要观察的现象**：

- `modules/` 下应有 **14** 个目录。
- README 速览只列了 **12** 个。
- 多出来的两个是 `axi_stream` 与 `ring_buffer`。

**预期结果**：确认 14 与 12 的差值正好是 axi_stream 和 ring_buffer。这样你以后看 README 速览时，心里就会清楚它是「精选」而非「全集」。若无法运行命令，可在 GitHub 仓库页面直接浏览 `modules/` 目录得到相同结论（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：要把一个工作在 50MHz 的计数器值安全地送给 100MHz 的时钟域，应该用哪个模块？为什么不能直接拉一根线？

> **答案**：用 `resync` 模块。多比特信号直接拉线会因各比特到达时刻不同而出现「中间错误值」；单比特信号直接拉线则有亚稳态风险。resync 提供了专用的同步器（如 `resync_counter` 用格雷码同步多比特计数）。

**练习 2**：`fifo`、`axi_stream`、`hard_fifo` 三个模块都和 FIFO 有关，它们的分工是什么？

> **答案**：`fifo` 是用通用 RAM 推断实现的同步/异步 FIFO；`axi_stream` 是接口贴合标准 AXI-Stream 协议的 FIFO/类型包；`hard_fifo` 是对 Xilinx 硬 FIFO 原语（如 FIFO36E2）的封装，换取更好的时序/资源，但可移植性受限于 Xilinx 器件。

---

### 4.3 设计哲学：可复用、面积优化、用 generic 裁剪功能

#### 4.3.1 概念说明

读懂一个库的「设计哲学」，比记住它的 API 更重要——因为哲学决定了后续每一个模块为什么那样写。hdl-modules 反复强调三件事：

1. **可复用与可移植**：代码要有干净直观的接口，能在不同工程、不同器件间搬来搬去。
2. **资源/面积优先优化**：FPGA 资源宝贵，模块要尽可能高效；尤其是 FIFO 这种「到处都在用」的模块，会被刻意做面积优化。
3. **用 generic 开关功能**：一个模块用 generic 决定「要不要启用某个特性」，不用的特性不消耗资源。这是「同一份代码、按需生成不同电路」的核心手段。

此外，项目把**质量**放在第一位：所有代码都经过同行评审、有单元测试覆盖、并在真实 FPGA 设计中被验证过。

#### 4.3.2 核心流程：generic 如何「省资源」

用一个直觉性的例子说明 generic 的意义。假设有一个 FIFO，它**可能**需要支持「包模式（packet mode）」——即只在收到 `last` 标记后整包才可读。如果你不写包逻辑，就要多维护一套指针与状态机，浪费 LUT/FF。

hdl-modules 的做法是给 FIFO 一个 generic，例如概念上类似：

```vhdl
-- 示例代码（仅示意 generic 的作用，并非项目原文逐字复制）
enable_packet_mode : boolean := false
```

综合工具看到一个 `if enable_packet_mode generate ... end generate;`（VHDL 的条件生成），就会：

- 当 `enable_packet_mode = false` 时，**整段包逻辑被删除**，不消耗任何资源。
- 当 `enable_packet_mode = true` 时，才把那部分电路「生成」出来。

于是同一份源码，既能给「只要普通缓冲」的场景省资源，又能给「需要包模式」的场景提供完整功能。资源占用可以近似理解为：

\[
\text{资源占用} = \text{基础电路} + \sum_{i \in \text{启用的特性}} \text{特性 } i \text{ 的代价}
\]

未启用的特性代价为 0。这就是「用 generic 裁剪功能以省面积」的本质。

#### 4.3.3 源码精读

README 把这三条哲学讲得很清楚。先看「可复用 + 面积优化 + generic」这一段：

[readme.rst:35-39](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L35-L39) — 说明代码追求可复用、可移植、接口干净；并明确「Using generics to enable/disable different features and modes means that resources can be saved when not all features are used.」（用 generic 开关功能，可以在不全用时省资源）。

紧接着点名为面积优化的典型：

[readme.rst:40-42](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L40-L42) — 特别指出 FIFO 被「刻意做面积优化」，因为 FPGA 工程里 FIFO 用得极其频繁，省一点乘以使用次数就是大量资源。

最后是「质量高于一切」的表态：

[readme.rst:44-47](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L44-L47) — 一切都经过同行评审、有良好单元测试覆盖、在真实设计中验证过；代码以可读性与可维护性为先。

这套哲学在工程组织上也有体现——`getting_started.rst` 说明源码分目录归档，可综合代码与仿真代码严格分开：

[doc/sphinx/getting_started.rst:51-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/getting_started.rst#L51-L64) — 每个模块的 `src` 放可综合源码（同时进仿真与构建工程），`test` 放测试台（仅仿真），`sim` 放 BFM（仅仿真），库名与模块名一致，所有文件按 VHDL-2008 处理。这种整齐的约定正是「可复用、可移植」哲学在工程结构上的落地。

#### 4.3.4 代码实践

**实践目标**：在真实模块上感受「generic 开关功能」的存在（本讲只做源码阅读，不跑综合）。

**操作步骤**：

1. 打开 FIFO 实体的接口定义文件（后续 u4-l1 会精读，本讲只看接口）：

   ```bash
   sed -n '1,80p' modules/fifo/src/fifo.vhd
   ```

2. 在文件顶部的 entity 声明里，找出形如 `enable_last`、`enable_packet_mode` 之类的 **boolean generic**。

3. 对每个这样的 generic，思考：如果它设为 `false`，实体内部对应的 `... generate` 块会被综合工具删除吗？删除后省下了什么资源？

**需要观察的现象**：

- `fifo.vhd` 的 generic 区会出现多个 `enable_xxx : boolean`（或类似命名）开关。
- 每个 `enable_xxx` 在架构体里通常对应一个 `if enable_xxx generate ... end generate;`。

**预期结果**：你能指出至少 2 个功能开关 generic，并说出它们各自控制哪一类逻辑（例如包模式、输出寄存器等）。本练习只需阅读源码、不要求跑工具；具体的资源数值对比留到第 4 单元（FIFO）与第 8 单元（资源回归）。若你暂时读不懂 VHDL 语法，标注「待后续单元确认」即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FIFO 在这个项目里被「刻意做面积优化」，而不是「先求正确、不管资源」？

> **答案**：因为 FIFO 在 FPGA 设计里使用频率极高（几乎每个工程都用到多个）。单个 FIFO 省下的资源，乘以使用数量，总量非常可观。README 在 [readme.rst:40-42](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L40-L42) 明确给出了这个理由。

**练习 2**：用一句话概括「generic 裁剪功能」对资源占用的影响。

> **答案**：综合时，generic 为假的功能对应的 `generate` 块会被删除，未启用的特性不占任何 LUT/FF/RAM，因此同一份源码可以按需生成「刚好够用」的电路。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个小任务：

> **任务：为 hdl-modules 写一张「项目名片」**

1. **定位**：用一句话写下 hdl-modules 是什么（参考 [readme.rst:29-31](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L29-L31)）。
2. **许可证**：写下许可证类型与一项关键义务（参考 [license.txt:3-11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/license.txt#L3-L11)）。
3. **模块全景**：从 4.2.3 的 14 模块表里挑出 3 个你最可能在近期工程里用到的模块，各写一句「它帮我解决什么问题」。
4. **哲学**：用自己的话写一条「设计哲学」并指出它在 README 的哪几行（参考 [readme.rst:35-47](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst#L35-L47)）。
5. **验证**：运行 4.1.4 的 Python 命令，确认标语输出，把它贴在名片顶部。

完成后，你应该拥有一份不超过半页纸、但能向同事快速介绍清楚项目的「名片」。这张名片也是你后续深入学习各模块时的「索引页」。

## 6. 本讲小结

- hdl-modules 是一组**可复用、高质量、同行评审**的 VHDL-2008 构建块，面向 FPGA 工程师，用宽松的 **BSD 3-Clause** 开源。
- 仓库实际包含 **14 个模块**；README 的速览清单是精选的 12 个，完整清单以 `modules/` 目录为准。
- 三条设计哲学：**可复用/可移植**、**面积优先优化**（FIFO 是典型）、**用 generic 开关功能以省资源**。
- 项目把**质量**放在首位：同行评审 + 单元测试 + 真实设计验证。
- 标语与 README 文本以 `hdl_modules/about.py` 为**单一信息源**，README 是其生成产物之一。
- 源码工程上，每个模块按 `src`/`test`/`sim` 严格分目录，库名与模块名一致，全部按 VHDL-2008 处理。

## 7. 下一步学习建议

本讲建立了全局认知，但还没有真正进入仓库结构、工具链与具体源码。建议下一步：

1. **学下一讲 u1-l2《仓库布局与单个模块的目录约定》**：弄清顶层目录的职责分工，以及单个模块内 `src`/`test`/`sim`/`scoped_constraints` 各放什么。这是后续读懂任何模块的前提。
2. **接着学 u1-l3《工具链与依赖：如何仿真与构建》**：了解 tsfpga、VUnit、hdl-registers 依赖与 Python 入口脚本，把环境跑起来。
3. **再学 u1-l4《Python 入口与 tsfpga Module 模式》**：理解 `get_hdl_modules()` 与每个 `module_*.py` 的统一模式。
4. 在阅读后续讲义前，建议先在本地 `git clone` 仓库，并打开本讲引用过的 `readme.rst`、`about.py`、`license.txt` 三份文件，对照本文行号亲自浏览一遍，加深印象。
