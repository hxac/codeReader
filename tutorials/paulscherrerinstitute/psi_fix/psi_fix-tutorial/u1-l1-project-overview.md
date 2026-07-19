# 项目概览与定位

## 1. 本讲目标

本讲是整套 psi_fix 学习手册的第一篇，面向**完全没接触过本项目的读者**。读完本讲后，你应当能够：

- 用一句话说清 psi_fix 是什么、给谁用、解决什么问题。
- 理解 psi_fix 最核心的设计哲学——**「位真双模型」(bittrue dual model)**：每个 VHDL 组件都必须配套一个行为完全一致的 Python 模型。
- 说出 psi_fix 依赖的三个外部库 `en_cl_fix` / `psi_common` / `psi_tb`（以及仿真框架 `PsiSim`）各自的最低版本要求。
- 了解许可证（PSI HDL Library License = LGPL2.1 + FPGA 例外条款）与 `major.minor.bugfix` 版本号策略。

本讲不要求你懂 VHDL 或 FPGA 开发，只要跟着读下来即可；后续讲义才会进入具体源码。

## 2. 前置知识

为了让后面的内容好懂，先建立两个背景概念。这两个概念在本讲只要求「知道大意」，细节会在后续讲义展开。

### 2.1 什么是「定点 DSP」

FPGA（现场可编程门阵列）是一块可以用代码重新定义硬件电路的芯片。很多物理实验（比如 PSI——瑞士保罗谢尔研究所——的粒子加速器、探测器）需要 FPGA 实时处理模拟信号，做滤波、变频、求模等数字信号处理（DSP, Digital Signal Processing）。

在 FPGA 上做运算，和用 Python 写 `a + b` 不一样：你必须**提前规定每一个数用多少个二进制位表示、小数点放在哪里**。这种「位数和小数点位置固定」的数叫**定点数 (fixed-point number)**，与之相对的是浮点数 (float)。

> 一句话记忆：定点 DSP = 在 FPGA 上做「位数被严格限定」的数学运算。

psi_fix 就是一个把常见定点 DSP 算法（FIR 滤波器、CIC 滤波器、CORDIC、DDS……）实现成可复用 VHDL 组件的**库**。

### 2.2 什么是「位真」

假设你用 Python（浮点，精度几乎无限）算出一个滤波结果是 `1.2345`，但 FPGA 定点硬件只能表示到 `1.25`（因为位数有限）。这两者**不一致**是正常的——毕竟精度不同。

但「位真 (bittrue)」要求的是另一回事：**Python 模型必须主动把自己限制到和硬件完全相同的定点格式上，于是 Python 的输出每一位都和 VHDL 硬件的输出完全相同。** 这样 Python 模型就成了硬件的「黄金参考 (golden reference)」，只要两者一致，硬件就是对的。

> 一句话记忆：位真 = Python 模型不是「理想算法」，而是「硬件行为的精确软件镜像」。

理解了这两点，下面读源码就轻松了。

## 3. 本讲源码地图

本讲只读 4 个文档型文件（不涉及任何 VHDL/Python 代码细节），它们是了解项目全貌的入口：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md) | 项目主页：定位、维护者、许可证、依赖、运行仿真方法、定点格式速查表 |
| [doc/files/introduction.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md) | RTL 实现总说明：en_cl_fix 由来、工作副本结构、仿真与贡献规则、握手协议 |
| [Changelog.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md) | 版本变更记录：每个版本的新增功能、Bug 修复、依赖变更、不向后兼容改动 |
| [License.txt](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/License.txt) | PSI HDL 库许可证全文（LGPL2.1 + FPGA 比特流例外条款） |

> 提示：本讲引用的都是**真实存在**的文件，永久链接里的 commit 号 `821049e…` 就是当前仓库 HEAD，点击即可直达对应行。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 项目定位与位真理念**
- **4.2 依赖关系（en_cl_fix / psi_common / psi_tb）**
- **4.3 许可证与版本策略**

### 4.1 项目定位与位真理念

#### 4.1.1 概念说明

psi_fix 的官方自我介绍只有一句话，但信息量很大：

> This library contains **bittrue implementations in VHDL (for synthesis) and Python (for fast simulations)** of standard signal processing components.
>
> —— README 第 3 行

这句话点明了 psi_fix 的三个定位关键词：

1. **standard signal processing components（标准信号处理组件）**：库存放的是**通用、可复用**的 DSP 组件（滤波器、CORDIC、DDS……），而不是某个具体实验的专用代码。
2. **VHDL (for synthesis)**：可综合的 VHDL，也就是最终真的会被「编译」进 FPGA 芯片的那份硬件描述。
3. **Python (for fast simulations) + bittrue**：每个 VHDL 组件都必须有一个**位真**的 Python 模型，用于快速仿真验证。Python 比 VHDL 仿真快得多，但行为必须和硬件逐位一致。

这就是 psi_fix（以及它所继承的 en_fix 思想）最核心的工程理念。

#### 4.1.2 核心流程

「位真双模型」带来的不是一份代码，而是**两份必须永远一致的代码**。整个库的运作流程可以画成下面这样：

```
        ┌─────────────────────────┐
        │  算法需求（滤波/CORDIC…）│
        └────────────┬────────────┘
                     │  同一份规格、同一套定点格式
        ┌────────────┴────────────┐
        ▼                         ▼
  ┌──────────┐              ┌──────────────┐
  │ Python   │  ← 位真镜像 → │   VHDL       │
  │ 位真模型  │   (逐位相同)  │ (可综合硬件)  │
  │ (快仿真)  │              │              │
  └─────┬────┘              └──────┬───────┘
        │                          │
        │  生成「黄金参考」文本刺激/期望输出      │
        ▼                          ▼
  ┌──────────────────────────────────────┐
  │ 自检测试台：把 VHDL 输出与 Python 输出  │
  │ 逐位比对，不一致就报 ###ERROR###        │
  └──────────────────────────────────────┘
```

关键点：**Python 模型不是「理想算法实现」，而是「硬件定点行为的软件镜像」。** 因此 Python 模型内部也要做舍入 (round)、饱和 (saturate)、位增长处理——这些本该是硬件才有的「笨拙」操作，Python 模型也一丝不苟地照做，这样两者才能逐位对齐。

#### 4.1.3 源码精读

**① README 对「位真模型是硬性要求」的强调**

库的「收录标准」一节把位真模型列为**入库门槛**——没有位真模型的组件根本不会被接受：

[README.md:22-31](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L22-L31) —— 这段说明 psi_fix 收录的是「不太应用相关、可被复用」的定点逻辑；并明确：「en_fix 的核心思想之一是每个 VHDL 组件都要有位真 Python 模型，因此**只有带位真模型的组件才会被收录**；否则建议另开一个『非位真定点代码』库。」同时还约定「每个 Package 或 Entity 用一个 `.vhd` 文件」。

**② introduction 对位真理念的再次确认**

[doc/files/introduction.md:9-10](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L9-L10) —— 本库目的是「为常见定点信号处理组件提供 HDL 实现**以及位真 Python 模型**，Python 模型还能被 MATLAB 调用」。

**③ 贡献规则中把「位真模型」和「自检测试台」都设为强制项**

[doc/files/introduction.md:76-100](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L76-L100) —— 贡献者必须遵守：代码风格（snake_case、端口 `_i`/`_o`/`_g` 后缀）、可配置性（参数做成 generic）、**强制提供位真 Python 模型**、**强制提供自检测试台**（测试台必须调用 Python 模型比对位真，出错时用 `###ERROR###:` 开头，因为回归脚本靠搜索这个字符串判定失败）。

#### 4.1.4 代码实践

> 这是一个**阅读型实践**（不需要装任何仿真器），目标是让你把「位真理念」从抽象口号变成自己的理解。

1. **实践目标**：用自己的话解释「为什么 psi_fix 强制要求 VHDL 组件配套位真 Python 模型」。
2. **操作步骤**：
   - 打开 [README.md:22-31](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L22-L31)，读「What belongs into this Library」。
   - 打开 [doc/files/introduction.md:86-94](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L86-L94)，读「Bit-true model」与「Self checking Test-benches」两条规则。
   - 用 3～5 句中文写下你的解释，要点应包含：(a) Python 仿真比 VHDL 快得多；(b) Python 模型限定位宽后可作为逐位黄金参考；(c) 自检测试台据此自动判错。
3. **需要观察的现象**：你会注意到「位真模型」和「自检测试台」在贡献规则里都被加粗成强制项——说明这是项目不可妥协的底线。
4. **预期结果**：你能说清「位真 = 硬件的精确软件镜像」，而不仅仅是一个「算法参考实现」。
5. 运行命令：本实践无需运行命令（纯阅读）。如果想在本地跑 Python 模型，依赖见 4.2 节实践。

#### 4.1.5 小练习与答案

**练习 1**：如果某个 VHDL 组件功能很完美，但作者懒得写 Python 模型，这个组件能被合并进 psi_fix 吗？为什么？

> **参考答案**：不能。README「What belongs into this Library」和 introduction 的贡献规则都把「位真 Python 模型」设为强制门槛；没有位真模型的组件应放到单独的「非位真定点代码」库，而非 psi_fix。

**练习 2**：Python 模型在内部是否也应该做 round/saturate（舍入/饱和）？还是只给出理想浮点结果就够了？

> **参考答案**：必须做 round/saturate。因为位真要求 Python 输出与 VHDL 输出**逐位相同**，所以 Python 模型必须主动把自己限制在和硬件一样的定点格式上（包括舍入与饱和），否则两者不可能逐位对齐。（具体实现见后续 `psi_fix_pkg` Python 包讲义。）

---

### 4.2 依赖关系（en_cl_fix / psi_common / psi_tb）

#### 4.2.1 概念说明

psi_fix 不是孤岛，它建立在 **PSI FPGA 库生态**之上。要正确使用或开发 psi_fix，必须连同它的依赖一起拿到本地。理解这些依赖的关系，是后续「把库跑起来」的前提。

依赖分两类：

- **库依赖（VHDL/TCL 代码）**：运行/综合 psi_fix 必须的兄弟仓库。
- **外部依赖（Python 环境）**：跑位真模型和回归测试必须的 Python 与第三方包。

#### 4.2.2 核心流程

PSI 的 FPGA 库采用「**相对路径互相引用**」的组织方式：几个仓库必须按固定目录名**并排 (side-by-side)** 摆放，仓库之间用相对路径互相 `use`/引用。仓库 [psi_fpga_all](https://github.com/paulscherrerinstitute/psi_fpga_all) 把所有 FPGA 相关仓库以 submodule 形式放进了正确的目录结构，可以一键拉取全部。

四个核心依赖及其角色：

| 依赖 | 角色 | 最低版本 |
|------|------|----------|
| **psi_fix** | 本仓库：定点 DSP 组件 + 位真 Python 模型 | （自身）|
| **en_cl_fix** | 定点运算基础包（Enclustra 原版，PSI fork） | `>= 1.2.0` |
| **psi_common** | PSI 通用 VHDL 组件（FIFO、RAM、时钟域穿越等基础设施） | `>= 2.15.0` |
| **psi_tb** | 测试台辅助包（文本文件比对、握手检查等） | `>= 2.7.0` |
| **PsiSim** (TCL) | TCL 仿真回归框架 | `>= 2.1.0` |

> 历史小知识：psi_fix 早期自带一套定点运算包；从 2.0.0 起改为复用 Enclustra 的 `en_cl_fix`，原来的 `psi_fix` 包变成一层**薄包装**，并提供双向转换函数，所以 `psi_fix` 与 `en_cl_fix` 两种风格的组件可以混用（见 [doc/files/introduction.md:14-17](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L14-L17)）。

#### 4.2.3 源码精读

**① 库依赖与版本要求**

[README.md:47-62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L47-L62) —— 这段是 README 里**会被脚本自动解析**的依赖声明区（注释里写明 `DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies`）。里面列出了 `PsiSim >= 2.1.0`、`psi_common >= 2.15.0`、`psi_tb >= 2.7.0`、`en_cl_fix >= 1.2.0`，并要求固定目录名摆放，或使用 `psi_fpga_all` 一键仓库。

**② 依赖的自动检出脚本**

[README.md:64-70](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L64-L70) —— 依赖可以用 `scripts/dependencies.py` 自动检出（`python dependencies.py -help` 查看用法），但需要先安装 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 包。

**③ 外部 Python 依赖**

[README.md:72-76](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L72-L76) —— 运行位真模型需要 **Python 3.x**，并安装 **SciPy**（`pip install scipy`）和 **NumPy**（`pip install numpy`）。

[doc/files/introduction.md:29-34](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L29-L34) —— 这里更明确：需要 **Python 3.5 及以上**，命令行必须能用 `python3` 调起（Linux 默认即可；Windows 需复制一份 `python.exe` 改名为 `python3.exe` 并加入 PATH），同样要求 SciPy 与 numpy。

**④ 历史版本的依赖变更轨迹**

Changelog 记录了依赖随版本演进的过程，便于排查「旧代码跑不起来」的问题：

- [Changelog.md:16-20](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L16-L20) —— 3.2.0 版把 `en_cl_fix` 提升到 `>= 1.2.0`，并新增了对 Python 「>53 位宽定点」的支持。
- [Changelog.md:23-29](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L23-L29) —— 3.1.0 版把 `psi_common` 提升到 `>= 2.15.0`。

#### 4.2.4 代码实践

1. **实践目标**：列出在你自己的机器上跑 psi_fix 位真模型所需的完整 Python 依赖清单，并验证版本。
2. **操作步骤**：
   - 读 [README.md:72-76](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L72-L76) 与 [doc/files/introduction.md:29-34](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L29-L34)。
   - 确认命令行 `python3 --version` 输出 ≥ 3.5。
   - 执行 `python3 -m pip install scipy numpy`（或确认已安装：`python3 -c "import scipy, numpy; print(scipy.__version__, numpy.__version__)"`）。
3. **需要观察的现象**：`import scipy, numpy` 不报错，且能打印出两个版本号。
4. **预期结果**：SciPy 与 NumPy 都成功导入；`python3` 命令可用。
5. 如果你**没有本地 Python 环境**或无法确认结果，请明确写「**待本地验证**」，不要假装命令已成功运行。

#### 4.2.5 小练习与答案

**练习 1**：你想跑 psi_fix 的回归仿真，但发现 `psi_common` 版本是 2.10.0。这会出问题吗？

> **参考答案**：会。README 的依赖声明要求 `psi_common >= 2.15.0`（当前 HEAD）。2.10.0 不满足最低版本，可能缺少 psi_fix 用到的某些组件或接口，应升级到 2.15.0 或以上（也可用 `scripts/dependencies.py` 自动拉取正确版本）。

**练习 2**：`en_cl_fix` 是谁提供的？psi_fix 为什么不自己维护一套定点运算包？

> **参考答案**：`en_cl_fix` 由 Enclustra GmbH 提供（PSI 在 GitHub 上做了 fork）。psi_fix 从 2.0.0 起决定复用 Enclustra 的成熟实现，而不是重复造轮子；原来的 `psi_fix` 包退化为一层包装，并提供双向转换函数，使两个「世界」的组件可混用（见 introduction.md 第 14–17 行）。

---

### 4.3 许可证与版本策略

#### 4.3.1 概念说明

开源/共享硬件库有两个现实问题需要先讲清楚，否则读者不敢在生产中使用：

1. **法律层面**：我能把 psi_fix 用在自己的 FPGA 产品里吗？需要公开我自己的源码吗？
2. **工程层面**：psi_fix 的版本号代表什么？升级一个版本会不会破坏我的旧代码？

本模块分别回答这两个问题。

#### 4.3.2 核心流程

**许可证模型**：PSI HDL Library License = **LGPL2.1 + 一条针对 FPGA 的例外条款**。

- 基础是 GNU LGPL2.1：库本身开源，使用者修改库源码后需公开修改部分。
- **例外条款**（关键）：允许你**链接、综合、生成比特流**并把**二进制/硬件产物**（明确包含 FPGA bitstream、flash 镜像）按你自己的条款分发——也就是说，**用 psi_fix 综合出的 FPGA 比特流不受 LGPL 的开源约束**，这对商业产品极其重要。

**版本号模型**：`major.minor.bugfix` 三段式（语义化版本的简化版）。

| 改动类型 | 升哪一段 | 含义 |
|----------|----------|------|
| 不向后兼容的接口变更 | `major` | 旧代码可能要改才能用 |
| 新增功能（向后兼容） | `minor` | 旧代码一般照常工作 |
| 仅修 Bug、无功能变化 | `bugfix` | 行为基本不变，更稳了 |

#### 4.3.3 源码精读

**① 许可证声明**

[README.md:13-14](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L13-L14) —— README 一句话点明：本库采用 PSI HDL Library License，即 **LGPL（详见 LGPL2_1.txt）+ 若干澄清条款**，以厘清 LGPL 在固件开发场景下的适用性。

[License.txt:1-21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/License.txt#L1-L21) —— 许可证全文。其中：
- 第 8–13 行声明基础条款是 GNU LGPL（版本 2 或更高）。
- 第 15–21 行的 **EXCEPTION NOTICE（例外通知）** 是关键：第 19 行明确「binary 形式包含 FPGA bitstream、flash 镜像等设备配置文件」，允许你按自己的条款分发这些硬件产物；但例外**不包括**能还原库源码的数据。

**② 版本号策略**

[README.md:38-43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L38-L43) —— 「Tagging Policy」一节定义了三段式版本号：不向后兼容 → 升 `major`；新增功能 → 升 `minor`；仅修 Bug → 升 `bugfix`。

**③ 版本策略的真实案例**

Changelog 里有非常好的实例，帮你直观理解三段式：

- [Changelog.md:10-14](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L10-L14) —— **4.0.0** 升了 `major`，原因是「把组件接口从 camelCase 重构为 snake_case」——这是**不向后兼容**的改动，所以触发大版本号。
- [Changelog.md:16-20](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L16-L20) —— **3.2.0** 升了 `minor`，因为只是「新增了对 Python >53 位宽定点的支持」，向后兼容。
- [Changelog.md:5-7](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L5-L7) 与 [Changelog.md:1-3](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L1-L3) —— **4.0.1 / 4.0.2** 只升 `bugfix`，因为只是修 Bug（FIR 复位处理、`cordic_vect` 的 `rdy_i`→`rdy_o` 重命名修正）。

#### 4.3.4 代码实践

1. **实践目标**：判断一次真实的版本升级是否会影响你已有的设计。
2. **操作步骤**：
   - 假设你现在的工程锁在 psi_fix **3.2.0**，你想升到 **4.0.2**。
   - 打开 [Changelog.md:1-20](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L1-L20)。
   - 找出从 3.2.0 → 4.0.2 之间所有「Changes (not reverse compatible)」条目。
   - 列出你需要在工程里改什么（提示：4.0.0 的 snake_case 接口重构会直接影响你例化组件时写的端口名）。
3. **需要观察的现象**：`major` 升级（4.0.0）下明确标注了「not reverse compatible」，而 `bugfix` 升级（4.0.1/4.0.2）没有这类标注。
4. **预期结果**：你得出的结论应是——从 3.2.0 升到 4.0.x **不是**无缝升级，必须按 Changelog 改接口命名；而从 4.0.1 升到 4.0.2 通常很安全。
5. 运行命令：本实践为阅读型，无需运行命令。

#### 4.3.5 小练习与答案

**练习 1**：你用 psi_fix 综合出了一块 FPGA 比特流，想卖到商业产品里，但不公开自己的工程源码。这违反许可证吗？

> **参考答案**：不违反。License.txt 第 19 行的例外条款明确允许你按自己的条款分发 binary 形式（含 FPGA bitstream、flash 镜像）。例外只约束「能还原库源码的数据」。当然，如果你**修改了 psi_fix 库源码本身**，LGPL 仍要求你公开对库的修改。

**练习 2**：作者提交了一个改动：把某组件的端口 `dataIn_i` 重命名为 `dat_i`。这应该升 `major`、`minor` 还是 `bugfix`？

> **参考答案**：升 `major`。端口重命名属于「不向后兼容」的接口变更——已有例化该组件的代码会编译失败。Changelog 4.0.0 把 camelCase 全量改为 snake_case 就是同类改动，触发了大版本号升级（见 Changelog.md 第 10–14 行）。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**「项目调研报告」**小任务（全程只读文档，不需要装工具）：

**背景**：你刚加入一个团队，leader 让你评估「能否引入 psi_fix 来做一块 FPGA 数据采集板的实时滤波」。

**请产出一份 300 字左右的中文调研笔记，必须包含**：

1. **定位**：用你自己的话说 psi_fix 是什么、适不适合做实时滤波（提示：参考 4.1）。
2. **依赖**：要拉取哪些仓库、各自最低版本是多少、Python 环境要装什么（提示：参考 4.2，列出 `psi_common >= 2.15.0`、`psi_tb >= 2.7.0`、`en_cl_fix >= 1.2.0`、`PsiSim >= 2.1.0`、Python 3.5+、SciPy、NumPy）。
3. **风险**：引入它要承担的法律义务是什么、版本升级要注意什么（提示：参考 4.3，提及 LGPL2.1 + FPGA 比特流例外、`major.minor.bugfix` 策略、4.0.0 接口重构的不兼容风险）。

**参考要点（写完后对照）**：

- 定位应点出「位真双模型」是 psi_fix 的核心卖点——既有可综合 VHDL，又有逐位一致的 Python 模型可快速仿真。
- 依赖应说明几个仓库需**并排摆放**（或用 `psi_fpga_all` 一键拉取），因为它们用相对路径互相引用。
- 风险应说明：商业比特流可闭源分发（例外条款），但若改了库源码需公开修改；升级要看 Changelog 里是否有 `not reverse compatible` 标记。

> 如果你暂时没有本地环境验证某些细节（例如 `python3` 是否可用），请在笔记里标注「**待本地验证**」，保持诚实，不要编造运行结果。

## 6. 本讲小结

- **psi_fix 是 PSI 提供的定点 DSP VHDL 库**，每个组件都同时有「可综合 VHDL」和「位真 Python 模型」两份实现（[README.md:3](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L3)）。
- **位真理念是硬性要求**：没有位真 Python 模型的组件不会被收录，自检测试台必须比对 Python 与 VHDL 输出的位真性（[README.md:22-31](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L22-L31)、[doc/files/introduction.md:86-94](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md#L86-L94)）。
- **依赖四件套**：`en_cl_fix`（定点运算，`>= 1.2.0`）、`psi_common`（通用组件，`>= 2.15.0`）、`psi_tb`（测试台，`>= 2.7.0`）、`PsiSim`（回归框架，`>= 2.1.0`），需按固定目录并排摆放（[README.md:47-62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L47-L62)）。
- **外部依赖**：Python 3.5+，并安装 SciPy 与 NumPy（[README.md:72-76](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L72-L76)）。
- **许可证**：PSI HDL Library License = LGPL2.1 + FPGA 比特流例外，商业比特流可闭源分发（[License.txt:15-21](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/License.txt#L15-L21)）。
- **版本号**：`major.minor.bugfix` 三段式，`major` = 不向后兼容（如 4.0.0 的 snake_case 重构），`minor` = 新功能，`bugfix` = 仅修 Bug（[README.md:38-43](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L38-L43)、[Changelog.md:1-20](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/Changelog.md#L1-L20)）。

## 7. 下一步学习建议

本讲只读了「项目介绍类」文档，还没碰任何代码。下一讲建议按顺序推进：

1. **u1-l2 目录结构与源码组织**：搞清 `hdl/`、`model/`、`testbench/`、`sim/`、`scripts/`、`doc/` 各顶层目录的职责，以及「一个组件 = 一个 `.vhd` + 一个 Python 模型 + 一个测试台 + 一份文档」的对应关系。
2. 之后是 **u1-l3 仿真与回归测试框架**（`sim/config.tcl` / `run.tcl`），这是把库真正「跑起来」的关键。
3. 再之后是 **u1-l4 定点数格式与握手约定**（`[s,i,f]` 格式、位增长、AXI-S 握手），为进入 `psi_fix_pkg` 源码做铺垫。

如果你想立刻对「位真模型」有体感，也可以先扫一眼 [model/psi_fix_pkg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_pkg.py)——它是 VHDL 定点包的 Python 镜像，本讲提到的「Python 也做 round/saturate」就实现在那里。该文件的精读会在单元 2 展开。
