# 项目定位与阅读方式

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完后，你应当能够：

- 说清楚 **FPGADesignElements** 这个项目到底是什么、为什么作者把它比作“硬件版的 libc（C 标准库）”。
- 学会两种阅读方式：在线阅读，以及 clone 到本地后用浏览器打开 `index.html` 进行导航。
- 看懂 `index.html` 这份“分类目录”是如何把上百个模块组织起来的，并能识别每个模块页面顶部的 **Source / License / Index** 三个链接分别指向什么。

这一篇不涉及任何 Verilog 语法细节，只解决一个问题：**这本书怎么读、怎么找东西**。把入口摸清楚，后续每一篇才好对照源码学习。

## 2. 前置知识

本讲面向零基础读者，只需要了解以下几个生活化的概念即可：

- **FPGA（现场可编程门阵列）**：一种可以通过代码“重新连线”的芯片。你用硬件描述语言（HDL，例如 Verilog）写下电路的行为，工具会把它变成真实的数字电路。
- **Verilog**：最常用的一种硬件描述语言。本书用的是较老但最通用的 **Verilog-2001** 版本，目的是让代码能在几乎所有工具链上跑起来。
- **模块（module）**：Verilog 里描述一块电路的基本单位，类似编程语言里的“函数/类”。本书里几乎每个 `.v` 文件就是一个模块。
- **libc（C 标准库）**：C 程序员随取随用的基础函数库（如 `printf`、`memcpy`）。作者用它作比喻，表示本书想做“FPGA 电路的基础零件库”。
- **HTML 页面**：浏览器打开的网页。本书把每个模块渲染成一个网页，注释当正文、代码当代码块。

如果你对 FPGA 完全陌生，只需记住一句话：**本书是一抽屉“现成的数字电路小零件”，你阅读它、挑选它、实例化它，就能拼出自己的 FPGA 设计。**

## 3. 本讲源码地图

本讲只看“书的入口”相关的几个文件，不进入任何具体电路模块：

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `README.md` | 仓库首页说明，交代项目是什么、怎么 clone、怎么用 | 给出项目一句话定位和本地获取方式 |
| `outline.html` | “大纲”页，介绍本书的定位与组织理念 | 给出“硬件版 libc”的官方比喻和分类思想 |
| `index.html` | “目录”页，把所有模块按分类列成可点击的清单 | 本讲的核心导航入口，分类目录的全部细节都在这里 |

另外，我们会顺带瞥一眼任意一个模块页（如 `Constant.html`）的**头部**，用来确认 Source/License/Index 三个链接的样子——这是练习任务的一部分。

> 提示：仓库里所有文件都平铺在**同一个目录**下（没有 `src/`、`docs/` 这样的子目录）。`.v` 是 Verilog 源码，`.html` 是由源码生成的网页，`.py` 是辅助工具脚本。这一点在下一讲会详细讲，本讲先有个印象即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**阅读入口**、**分类目录**。

### 4.1 项目定位

#### 4.1.1 概念说明

很多人第一次看到这个仓库会疑惑：它既不像一个“能跑起来的应用”，也不像一个“有 main 函数的程序”。原因很简单——**它根本不是一个程序，而是一本“在线书 + 零件库”**。

README 开篇一句话就点明了性质：

[README.md:L5-L8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/README.md#L5-L8) —— 这三行说明本书是“一本自包含的在线书（self-contained online book），内含一套 FPGA 设计元件库及相关编码/设计指南”。

其中几个关键词要拆开理解：

- **self-contained（自包含）**：不依赖外部服务，clone 下来就能离线看。
- **library of FPGA design elements**：一套 FPGA“设计元件”库——也就是一堆可以复用的电路小模块。
- **related coding/design guides**：除了电路模块，还附带“怎么写 Verilog”“怎么做系统设计”的规范文档。

而最能体现作者意图的，是 `outline.html` 里那个著名的比喻：

[outline.html:L15-L18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L15-L18) —— 把本书当作“C 标准库（libc）及其文档的硬件对应物（a hardware analog to the C Standard Library）”。

这个比喻非常重要，理解了它就理解了全书的精神：

| 角度 | C 标准库 (libc) | 本书 (FPGADesignElements) |
| --- | --- | --- |
| 提供什么 | `printf`、`malloc` 等基础函数 | Register、Multiplexer、FIFO 等基础电路 |
| 使用方式 | `#include` 后调用函数 | 实例化模块、连上线 |
| 附带什么 | man 手册 / 文档 | 每个模块一页讲解（由注释渲染而成） |
| 目标 | 不必每次重写基础功能 | 不必每次重画基础电路 |

一句话定位：**这是一本“FPGA 的 libc”——基础电路零件库 + 配套文档**。

#### 4.1.2 核心流程

全书在概念上是这样组织起来的（从抽象到具体）：

```text
一本在线书
   │
   ├── 规范文档（concept 章）：怎么写 Verilog、怎么做系统设计、握手规则、CDC 理论
   │
   └── 设计元件库（零件）：上百个可复用电路模块
            │
            ├── 按类别分组（Boolean Logic、Synchronous Logic、Integer Arithmetic……）
            │
            └── 每个模块 = 一个 .v 源文件 + 一页由注释渲染的 HTML 讲解
```

`outline.html` 用一段话描述了这种“由浅入深、互相复用”的组织方式：

[outline.html:L20-L26](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L20-L26) —— 设计元件被分门别类，从最基础的（有些纯粹是为教学）逐步加复杂，直到“大量复用其他元件”搭出的完整接口与专用引擎。

这解释了本书的一个核心写作手法：**复杂模块是用简单模块拼出来的**。例如后面的计数器会用加法器＋寄存器拼，CDC FIFO 会用格雷码指针＋同步器拼。所以阅读顺序很重要——这也是本套学习手册按依赖关系排序的原因。

> 关于“参数默认值为 0”的提醒：README 和 outline 都用粗体 **IMPORTANT** 强调，模块**默认不能直接综合**，必须实例化时显式设置参数。这是有意为之的安全设计。这个约束属于下一讲（u1-l2）的内容，本讲只要知道“零件要按需配置后才好用”即可。

#### 4.1.3 源码精读

把上面引用的两段关键文本对齐来看：

- README 的定位句：[README.md:L5-L8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/README.md#L5-L8) —— “self-contained online book … library of FPGA design elements”。
- libc 比喻：[outline.html:L15-L18](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L15-L18) —— “Think of it as a hardware analog to the C Standard Library”。
- 分类与由浅入深：[outline.html:L20-L26](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L20-L26)。

注意 README 还提到“可以用作 CAD 工具里的库”：

[README.md:L16-L17](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/README.md#L16-L17) —— 所有文件都在同一个目录下，把所有 Verilog 文件一次性导入你的 CAD 工具，就能当成库来用。

这说明本书有“双重身份”：既是**读物**（HTML 页面讲解），也是**工程依赖**（`.v` 文件直接拿去综合）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：用自己的话写出项目的一句话定位。
2. **操作步骤**：
   - 打开 [README.md:L1-L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/README.md#L1-L14) 与 [outline.html:L15-L26](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L15-L26)。
   - 找出三处线索：①“online book”、②“library of design elements”、③“hardware analog to libc”。
3. **观察现象**：注意 README 偏“怎么获取/怎么用”，outline 偏“这是什么/为什么这样组织”。
4. **预期结果**：你能写出类似“这是一本自包含的在线书，提供 FPGA 基础电路零件库与编码规范，相当于硬件版的 libc”这样一句话。
5. 本步骤无需运行任何命令，纯阅读即可。

#### 4.1.5 小练习与答案

**练习 1**：作者为什么选择用 libc 来比喻本书，而不是用“一个框架”或“一个应用”？

> **参考答案**：因为本书提供的是**可复用的基础零件**（函数/电路模块），使用者把它纳入自己的工程、按需调用，而不是一个开箱即用、自身运行的整体应用。libc 这个比喻准确传达了“基础库 + 文档”的定位。

**练习 2**：README 说本书是 “self-contained”，结合仓库结构，这个特性的实际好处是什么？

> **参考答案**：所有 `.v`/`.html`/`.py` 都在同一个目录、不依赖外部服务，因此可以离线 clone、离线阅读，并能直接把全部 Verilog 文件作为库一次性导入 CAD 工具。

---

### 4.2 阅读入口

#### 4.2.1 概念说明

知道“它是什么”之后，下一个问题是“怎么读”。本书提供两条等价的阅读路径：

1. **在线阅读**：直接访问作者部署的网站。
2. **本地阅读**：`git clone` 后用浏览器打开本地的 `index.html`。

两条路看到的页面完全一样，因为网页文件就在仓库里。README 给出了这两种方式：

[README.md:L5-L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/README.md#L5-L14) —— 第 8 行给出在线地址 `https://fpgacpu.ca/fpga/`；第 10–14 行给出 clone 命令和“用浏览器打开 index.html”的本地方式。

为什么推荐本地阅读？因为：

- 可以离线、随时翻阅；
- 可以对照 `.v` 源码一起看（网页和源码同目录）；
- 后续做练习、改参数、跑综合时，本地副本就是你的工作目录。

#### 4.2.2 核心流程

本地阅读的标准流程是“三步走”：

```text
1. git clone https://github.com/laforest/FPGADesignElements.git
        ↓ 得到本地目录（所有 .v/.html/.py 平铺在一起）
2. 用浏览器打开 ./index.html
        ↓ 进入“分类目录”主页
3. 在目录里点任意模块名
        ↓ 进入该模块的讲解页（顶部有 Source/License/Index 三个链接）
```

这里有一个非常关键的机制，决定了“为什么这本书不会过时”：**网页是由源码的注释直接生成的**。`outline.html` 这样描述：

[outline.html:L41-L44](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L41-L44) —— 每个模块网页都是**直接由 Verilog 源码生成**的；注释构成正文，代码放进带边框的预格式化文本块里。

也就是说，作者写模块时，**注释就是用 Markdown 写的讲解**，再用一个叫 `v2h.py` 的小工具把 `.v` 转成 `.html`。源码一改，重新生成页面即可，**网页永远不会和代码脱节**。这一点会在后续 u18-l1（工具链）里深入讲，本讲先记住结论。

#### 4.2.3 源码精读

clone 与打开方式的官方描述在两处都出现过，可以互相印证：

- README 版：[README.md:L10-L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/README.md#L10-L14) —— 给出 `git clone ...` 命令并指明“access index.html from your favourite browser”。
- outline 版：[outline.html:L46-L52](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L46-L52) —— 同样的 clone 命令，并补充了“所有文件在同一目录、可作库导入”和 license 说明。

网页由源码生成的描述见 [outline.html:L41-L44](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/outline.html#L41-L44)。

#### 4.2.4 代码实践（动手型）

1. **实践目标**：在本地把这本书跑起来，确认能正常浏览。
2. **操作步骤**：
   ```bash
   git clone https://github.com/laforest/FPGADesignElements.git
   cd FPGADesignElements
   ```
   然后用浏览器打开仓库根目录里的 `index.html`（双击文件，或在地址栏输入其 `file://` 路径）。
3. **观察现象**：页面顶部出现书名“FPGA Design Elements”和作者名；正文是一组带标题的列表（分类目录）。
4. **预期结果**：看到和 `https://fpgacpu.ca/fpga/` 一致的目录页，且离线可正常跳转。
5. 如果在公司内网/受限环境无法 clone，**待本地验证**：可改用在线站点浏览，但后续涉及改参数的练习仍需本地副本。

#### 4.2.5 小练习与答案

**练习 1**：在线站点和本地 clone 看到的内容为什么是一样的？

> **参考答案**：因为网页（`.html`）和样式（`style.css`）都直接存放在仓库里，在线站点本质上就是把这些文件挂到了一个 Web 服务器上。两边是同一份文件。

**练习 2**：作者说“网页不会随代码演进而过时（the web page doesn't get stale）”，靠的是什么机制？

> **参考答案**：靠“注释即 Markdown、源码即页面源”的生成方式——`.html` 是由 `v2h.py` 从 `.v` 源码（含注释）直接生成的，因此代码一旦更新、重新生成页面，讲解就自动同步。

---

### 4.3 分类目录

#### 4.3.1 概念说明

`index.html` 是本书的“总目录”。它用一连串 `<h2>` 小标题把内容切成若干**顶层分类（category）**，每个分类下面是一个项目符号列表，列出该分类的模块（有的模块还有更细的子分类）。

理解目录的切分逻辑，能帮你在上百个模块里快速定位。顶层分类大致可以分成三类：

- **元信息类**：`Introduction`、`References`、`Tools`——不是电路模块，而是书的导览、规范文档和辅助工具。
- **电路零件类**（本书主体）：`Useful Functions`、`Simulation and Test Bench`、`Boolean Logic`、`Synchronous Logic`、`Integer Arithmetic`、`Pulse Logic`、`Elastic Pipelines`、`Arbitration and Synchronization`、`Clock Domain Crossing (CDC)`、`Interfaces`。
- **规划/待写类**：`Analog Signal Handling`、`Hashing and Pattern Matching`、`Communications`——这些分类下大多是**尚未实现**的条目（纯文字、没有链接），表示作者规划中但还没写的内容。
- **杂项**：`Miscellaneous Bits`——FSM 实现方法、开放设计问题等杂文。

#### 4.3.2 核心流程

在 `index.html` 里找一个模块的典型路径：

```text
打开 index.html
   │
   ├── 找到所属分类的 <h2>（如 "Boolean Logic"）
   │
   ├── 在该分类的列表里找到模块名（如 "Multiplexer (One-Hot)"）
   │
   └── 点击链接 → 跳转到 模块名.html 的讲解页
            │
            └── 页面顶部三个链接：
                  Source  → 模块名.v   （看 Verilog 源码）
                  License → legal.html （看许可协议）
                  Index   → index.html （回到目录）
```

每个模块页头部的这三个链接是本讲练习的重点。以最简单的 `Constant` 模块为例：

[Constant.html:L12-L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Constant.html#L12-L14) —— 三行分别是 Source（指向 `./Constant.v`）、License（指向 `./legal.html`）、Index（指向 `./index.html`）。

也就是说，**Source 链接把“讲解页”和“源码文件”一一绑定**：看讲解时点 Source 就能立刻看到生成它的 `.v` 源码；这正是“注释即文档”理念在导航上的体现。

#### 4.3.3 源码精读

下面把 `index.html` 里所有顶层分类的标题行集中列出（行号即各 `<h2>` 所在行），方便你建立全局地图：

| 行号 | 分类 | 性质 |
| --- | --- | --- |
| [index.html:L24](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L24) | Introduction | 导览（Outline、License） |
| [index.html:L31](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L31) | References | 规范文档（Verilog/System/Handshake 标准、链接、书单） |
| [index.html:L43](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L43) | Tools | 辅助工具（v2h.py、verilinter、生成器等） |
| [index.html:L83](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L83) | Useful Functions | 可复用函数（abs/max/min/clog2…） |
| [index.html:L97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L97) | Simulation and Test Bench | 仿真与测试（Simulation Clock、Harness） |
| [index.html:L105](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L105) | Boolean Logic | 布尔逻辑（Constant、MUX、地址译码…） |
| [index.html:L156](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L156) | Synchronous Logic | 时序逻辑（Register、Pipeline、RAM…） |
| [index.html:L192](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L192) | Integer Arithmetic | 整数算术（加减、计数器、乘除…） |
| [index.html:L257](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L257) | Pulse Logic | 脉冲逻辑（Pulse Latch/Generator…） |
| [index.html:L267](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L267) | Elastic Pipelines | 弹性流水线（Skid Buffer、Fork/Join/Merge…） |
| [index.html:L296](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L296) | Arbitration and Synchronization | 仲裁与同步（Arbiter、Muller C…） |
| [index.html:L307](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L307) | Clock Domain Crossing (CDC) | 跨时钟域（CDC 理论与同步器…） |
| [index.html:L323](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L323) | Interfaces | 接口（IDELAYCTRL、反串行化…） |
| [index.html:L340](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L340) | Analog Signal Handling | 模拟信号处理（多为待写） |
| [index.html:L351](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L351) | Hashing and Pattern Matching | 哈希与模式匹配（待写） |
| [index.html:L359](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L359) | Communications | 通信（多为待写） |
| [index.html:L374](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L374) | Miscellaneous Bits | 杂项（FSM、开放问题…） |

其中 `References` 分类尤其值得关注，它列出了贯穿全书的几篇“概念章”：

[index.html:L31-L39](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L31-L39) —— Verilog 编码标准、系统设计标准、ready/valid 握手规则、有用链接、参考书单。本套学习手册的入门层（u2/u3/u4 单元）正是围绕前三篇规范展开的。

而模块页头部的三个链接，可以再拿 `Register` 模块互相印证（结构与 `Constant` 完全一致）：

[Register.html:L12-L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Register.html#L12-L14) —— Source 指向 `./Register.v`，License 指向 `./legal.html`，Index 指向 `./index.html`。

#### 4.3.4 代码实践（动手型）

1. **实践目标**：熟悉分类目录，并能解释模块页三链接的含义。
2. **操作步骤**：
   - 在本地打开 `index.html`。
   - 任选 3 个**已实现**的电路分类（带蓝色可点链接的，例如 Boolean Logic、Synchronous Logic、Elastic Pipelines）。
   - 在每个分类下挑 3 个模块，点进去看一眼。
3. **观察现象**：每个模块页顶部都有且仅有 Source / License / Index 三个带边框的链接。
4. **预期结果**：你能回答——
   - **Source**：跳转到生成本页的那个 `.v` Verilog 源码文件（“讲解↔源码”一一对应）。
   - **License**：跳转到 `legal.html`，查看许可与免责声明（本书可自由使用）。
   - **Index**：跳转回 `index.html` 总目录，方便继续浏览。
5. 如果某些分类下条目点不动（如 Communications 里的纯文字项），那是**尚未实现**的规划条目，属于正常现象，**待本地验证**后你应能区分“已有模块”与“待写占位”。

#### 4.3.5 小练习与答案

**练习 1**：我想找“弹性流水线里的 Skid Buffer”，应该去哪个分类？它对应的源码文件名是什么？

> **参考答案**：去 **Elastic Pipelines** 分类（[index.html:L267](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L267)）。列表里的 “Pipeline Skid Buffer” 链接到 `Pipeline_Skid_Buffer.html`，点其顶部 Source 链接可得到源码文件 `Pipeline_Skid_Buffer.v`。

**练习 2**：为什么有些分类（如 Communications）下的条目点不动？

> **参考答案**：这些是作者**规划中但尚未实现**的模块，目前只写了文字设想、没有对应的 `.v`/`.html`，所以没有超链接。它们表示本书“仍在持续演进”。

**练习 3**：模块页顶部的 Index 链接和浏览器“后退”按钮，作用一样吗？

> **参考答案**：结果通常是同一个 `index.html`，但机制不同。Index 链接是页面里写死的、固定指向 `./index.html` 的超链接（[Constant.html:L14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/Constant.html#L14)）；而后退按钮回到的是浏览器历史里的上一页，未必是目录。

## 5. 综合实践

把本讲三个模块串起来的小任务——**做一份属于你自己的“地图笔记”**：

1. **目标**：证明你已经能独立导航这本书，并理解“讲解页↔源码↔目录”三者的一一对应关系。
2. **步骤**：
   - 按本讲 4.2.4 的方法把仓库 clone 到本地并用浏览器打开 `index.html`。
   - 列出全部顶层分类（参考 4.3.3 的表格核对，不要漏掉元信息类与待写类）。
   - 从中挑出 **3 个已实现的电路分类**，每个分类下选 **3 个代表性模块**，按下表填写：

     | 分类 | 模块名 | 模块讲解页 | 对应源码（点 Source 得到） |
     | --- | --- | --- | --- |
     | 例如 Boolean Logic | Multiplexer (One-Hot) | `Multiplexer_One_Hot.html` | `Multiplexer_One_Hot.v` |
     | … | … | … | … |

   - 对其中任意一个模块页，截图或抄录顶部 **Source / License / Index** 三个链接，各用一句话说明它们分别指向哪里、为什么这样设计。
3. **现象与预期**：你会发现“模块讲解页文件名”与“源码文件名”只差一个后缀（`.html` ↔ `.v`），命名完全一致——这正是“一模块一文件、注释即文档”约定的体现。
4. **产出**：一份 markdown 或纸笔笔记，作为后续每一讲的随身索引。后续讲义提到某模块时，你都能用“分类 → 模块 → Source”三步快速定位源码。

## 6. 本讲小结

- 本书 **FPGADesignElements** 是一本“自包含的在线书 + FPGA 基础电路零件库”，作者把它比作**硬件版的 libc**。
- 阅读方式有两条等价路径：**在线 `https://fpgacpu.ca/fpga/`** 与 **本地 clone 后打开 `index.html`**；后者还方便对照源码与做练习。
- 网页由 Verilog 源码经 `v2h.py` 生成，**注释即 Markdown 正文、代码即代码块**，因此页面永远不会与代码脱节。
- `index.html` 把内容组织成约 17 个顶层分类，分为**元信息 / 电路零件 / 规划待写 / 杂项**四大类；主体是电路零件。
- 每个模块页顶部都有 **Source（→`.v` 源码）/ License（→`legal.html`）/ Index（→目录）** 三个链接，体现“讲解页↔源码”一一对应。
- 目录里没有超链接的条目是**尚未实现**的规划项，本书仍在持续演进。

## 7. 下一步学习建议

本讲只解决了“怎么读、怎么找”。下一步建议：

- 进入 **u1-l2（仓库结构与开箱即用约束）**：搞清楚扁平目录、`.v`/`.html`/`.py` 的职责、一模块一文件命名约定，以及那个重要的“参数默认值为 0”约束——理解它之后你才能正确实例化任何模块。
- 在那之前，可以先随手点开几个模块页（如 `Register.html`、`Constant.html`）的 Source 链接，**先看注释、再看代码**，提前感受“注释即文档”的写作风格，为下一讲铺好直觉。
- 后续的 u2/u3 单元会带你读 `References` 里的 Verilog 编码标准，那才是真正动笔写 Verilog 的开始。
