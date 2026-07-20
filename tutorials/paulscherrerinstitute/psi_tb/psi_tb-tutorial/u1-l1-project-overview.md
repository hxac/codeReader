# psi_tb 项目总览与定位

## 1. 本讲目标

本讲是整个学习手册的第一篇，目标是让你在**不写任何代码**的情况下，先搞清楚三个问题：

1. psi_tb 到底是一个**什么样的库**？它解决什么问题、不解决什么问题？
2. 这个库**由谁维护**、依据**什么许可证**发布？我能不能在公司项目里用它？
3. 它**从 V1.00 一路演进到 3.0.0**，每个大版本大致发生了什么变化？

学完本讲，你应该能用自己的话向同事解释「psi_tb 是干嘛的」，并能看懂 README、Changelog、License 这三份项目根目录下最基础的文档。**本讲不要求你有任何 VHDL 基础**——所有概念都会从零讲起。

---

## 2. 前置知识

本讲是纯「项目认知」层面，唯一的预备概念是下面这两个词，我们先用大白话解释清楚：

- **VHDL**：一种硬件描述语言（Hardware Description Language），用来写 FPGA / ASIC 的逻辑。你暂时把它当成「给芯片写代码的语言」即可。
- **仿真（Simulation）**：在把代码烧进真实芯片之前，先用软件（如 ModelSim、GHDL）「跑一遍」这段硬件代码，看它的行为对不对。这一步叫仿真。

理解了上面两个词，就能理解本讲最关键的一对概念：

| 概念 | 中文 | 作用 | 是否要能变成真实电路 |
| --- | --- | --- | --- |
| **RTL / synthesizable code** | 可综合代码 | 描述最终要烧进芯片的逻辑 | **必须**能被综合成真实电路 |
| **Testbench** | 测试平台 | 给 RTL 喂输入、检查输出是否正确 | **不用**，它只在仿真软件里跑 |

> 直觉类比：RTL 是「被考试的学生」，testbench 是「出题 + 判卷的老师」。老师本身不需要变成芯片，它只要能在仿真软件里把学生考一遍就行。

psi_tb 正是一个**专门给「老师」（testbench）用的工具箱**。记住这一点，后面所有内容都围绕它展开。

---

## 3. 本讲源码地图

本讲只涉及项目根目录下三份**说明性文档**（不是 VHDL 代码），它们是了解 psi_tb 的入口：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `README.md` | 项目主页说明：维护者、作者、许可证、库的归属边界、依赖、版本号策略 | 「What belongs / does not belong」两节、维护者与许可证、依赖 |
| `Changelog.md` | 版本变更日志，记录每个版本修了什么 bug、加了什么功能 | V1.00 → 3.0.0 的演进脉络 |
| `License.txt` | PSI HDL Library License 全文（LGPL + 固件例外条款） | 为什么这个许可证对 FPGA 工程友好 |

作为对照，整个仓库的目录布局如下（**本讲只需有个印象，细节留给下一讲 u1-l2**）：

```
psi_tb/
├── README.md            # 本讲重点①
├── Changelog.md         # 本讲重点②
├── License.txt          # 本讲重点③
├── LGPL2_1.txt          # License 所基于的 LGPL 全文
├── hdl/                 # 7 个 VHDL package（库的真正实现）
│   ├── psi_tb_txt_util.vhd
│   ├── psi_tb_compare_pkg.vhd
│   ├── psi_tb_activity_pkg.vhd
│   ├── psi_tb_axi_pkg.vhd
│   ├── psi_tb_axi_conv_pkg.vhd
│   ├── psi_tb_textfile_pkg.vhd
│   └── psi_tb_i2c_pkg.vhd
├── testbench/           # 示例 testbench（I2C）
├── sim/                 # PsiSim 仿真脚本（config/run/runGhdl/ci.do）
├── scripts/             # CI 脚本（ciFlow.py、dependencies.py）
├── doc/                 # 文档（pdf/docx）
└── sigasi/              # Sigasi IDE 工程映射文件
```

记住一句话：**本讲读文档，后续讲义才会进 `hdl/` 读真正的 VHDL 代码。**

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- 4.1 psi_tb 的定位：仅供 testbench 的工具库
- 4.2 归属边界：What belongs / What does not belong（psi_tb 解决的「三类问题」就在这里）
- 4.3 维护者、作者与 PSI HDL Library License
- 4.4 版本演进：从 V1.00 到 3.0.0

---

### 4.1 psi_tb 的定位：仅供 testbench 的工具库

#### 4.1.1 概念说明

psi_tb 是瑞士保罗谢勒研究所（Paul Scherrer Institute，简称 PSI）开源的一套 **VHDL testbench 工具库**。它的核心定位可以用 README 里一句话概括——这段代码**只服务于 testbench，因此不需要可综合**。

为什么「不需要可综合」反而是一种**优势**？因为可综合 VHDL 受到很多限制（比如不能随便用 `wait`、不能动态分配、打印能力很弱），而 testbench 跑在仿真器里，几乎可以用 VHDL 语言的全部能力。psi_tb 正是利用了这种自由，提供了大量「写 testbench 时反复要用、但又不想每次自己重写」的工具：打印、数值比较、时钟同步等待、总线握手模型……

这层定位会贯穿整本手册——**你看到 psi_tb 里任何「奇怪」的写法（无限循环、`wait`、动态字符串拼接），先想到「这是 testbench 专用，不用综合」就对了。**

#### 4.1.2 核心流程

把 psi_tb 放进一个典型 FPGA 工程的工作流，它的位置是这样的：

```
  ┌─────────────────┐     喂输入      ┌──────────────────────┐
  │  你的 RTL 代码   │ ◄────────────── │   你的 testbench     │
  │ （可综合，进芯片）│ ──────────────► │ （只仿真，不进芯片）  │
  └─────────────────┘     查输出       └──────────┬───────────┘
                                                  │ 调用
                                                  ▼
                                       ┌──────────────────────┐
                                       │   psi_tb 工具库       │
                                       │ （打印/比较/激励/BFM） │
                                       └──────────────────────┘
```

也就是说，psi_tb 不是被测对象（DUT），而是「**帮 testbench 干活的一组现成过程和函数**」。

#### 4.1.3 源码精读

README 开头直接点明了库的用途与「仅供 testbench」的定位：

- [README.md:L16-L18](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L16-L18)：标题 `## What belongs into this Library` 紧接着的两句——「This library contains VHDL code that is useful for testbenches. The code is meant for testbenches only, so it does not have to be synthesizable.」**这是全篇最重要的一句话**，它定义了 psi_tb 的全部边界。

- README 还建议「一个 package / entity 对应一个 `.vhd` 文件」，这也是为什么 `hdl/` 目录下每个文件都正好是一个独立的 package（见第 3 节目录树）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你把「定位」内化为自己的话。

1. **实践目标**：能不看讲义，向别人解释 psi_tb 为什么「不需要可综合」。
2. **操作步骤**：
   - 打开 [README.md:L16-L18](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L16-L18)。
   - 找到 `...does not have to be synthesizable.` 这一句。
   - 用你自己的话，在一张纸上写下：「如果允许不可综合，testbench 能做哪些 RTL 不能做的事？」（提示：无限等待、任意字符串拼接、文件读写……）
3. **需要观察的现象**：你会意识到 testbench 的「自由度」远高于 RTL，这正是 psi_tb 能提供丰富工具的前提。
4. **预期结果**：你能写出至少 2 条「只有 testbench 才能用、RTL 不能用」的语言特性。
5. 说明：本步不运行任何仿真，属于纯阅读理解。

#### 4.1.5 小练习与答案

**练习 1**：psi_tb 里的代码能不能被综合成真实电路？为什么？
**参考答案**：不要求能综合。因为 psi_tb 明确声明「code is meant for testbenches only, so it does not have to be synthesizable」，它只在仿真器里运行，不需要变成芯片电路。

**练习 2**：如果一个功能「既能在 testbench 用、也能在 RTL 用」，按照 psi_tb 的定位，它应该放进 psi_tb 吗？
**参考答案**：不合适。psi_tb 只收「testbench 专用」的代码；能综合、能进 RTL 的通用代码更适合放进 `psi_common` 这类综合库（见 4.3 与第 7 节）。

---

### 4.2 归属边界：What belongs / What does not belong

> 本节直接对应实践任务里的「**psi_tb 解决的三类问题**」——这三类问题就是 README 在 `What belongs` 里列出的三条。

#### 4.2.1 概念说明

一个开源库要长期可维护，必须划清「我收什么、不收什么」。psi_tb 用两节列表把这个边界写得很死：

- **What belongs**（属于本库的）：testbench 才需要的三类工具。
- **What does not belong**（不属于本库的）：项目相关代码、属于别的库的代码、**可综合代码**。

这种「边界声明」是阅读任何成熟库的第一步——它告诉你「遇到某类需求时，该来这里找，还是去别处找」。

#### 4.2.2 核心流程：psi_tb 解决的三类问题

README 在 `What belongs` 里给出了三条示例，这正好就是 **psi_tb 解决的三类问题**，也是整本手册后续单元的骨架：

```
┌─────────────────────────────────┬─────────────────────────────────────────┐
│  类别（README 原文）             │  通俗解释 + 对应后续讲义                  │
├─────────────────────────────────┼─────────────────────────────────────────┤
│ 1. Bus-Functional-Models        │ 总线功能模型（BFM）：替你驱动/检查 AXI、   │
│    （原文误写为 Modelsim）       │ I2C 等总线接口 → 单元 u5（AXI）、u7（I2C）│
│ 2. Functions for checking values│ 数值检查函数：自动比较「期望值 vs 实际值」 │
│                                 │ 并打印可读错误 → 单元 u3（compare）        │
│ 3. Functionality for automated  │ 自动激励生成：如随机发生器、时钟同步等待、 │
│    stimuli generation           │ 选通脉冲 → 单元 u4（activity）            │
└─────────────────────────────────┴─────────────────────────────────────────┘
```

> 说明：README 原文 `Bus-Functional-Modelsim` 是 `Bus-Functional-Models` 的笔误（多打了一个 `sim`），它指的就是业内常说的 **BFM（Bus Functional Model）**。读源码时要留意这种小笔误，不要被字面意思带偏。

与之对应，`What does not belong` 列了三条「不收」的，构成一道简单的归类判定流程：

```
一段新代码 ──► 是项目专用的？ ──是──► 不收（放项目自己的库）
              │
              否
              ├──► 更适合别的库？ ──是──► 不收（如可综合代码放 psi_common）
              │
              否
              ├──► 是为综合（进芯片）写的？ ──是──► 不收
              │
              否
              └──► 收进 psi_tb
```

#### 4.2.3 源码精读

- [README.md:L16-L25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L16-L25)：`What belongs into this Library` 整节。其中 L22–L25 列出了三类「应该收」的例子（即上面表格的三类问题）。

- [README.md:L27-L31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L27-L31)：`What does not belong into this Library` 整节，列出三条「不收」的边界条件。注意 L31 再次强调 **Code that is meant for synthesis**（为综合而写的代码）不属于这里。

#### 4.2.4 代码实践

1. **实践目标**：能用「三类问题」框架，给一段陌生的 testbench 需求归类。
2. **操作步骤**：
   - 打开 [README.md:L22-L25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L22-L25)，把三条英文例子抄下来。
   - 对每条，写出它对应「哪一类问题」以及「它能让 testbench 少写什么代码」。
   - 例如：`Functions for checking values` → 你不用再每次手写 `assert ... report ...`，库会帮你拼好错误消息。
3. **需要观察的现象**：你会发现三类问题正好覆盖了「**驱动接口 / 判定对错 / 制造输入**」——也就是 testbench 的全部日常工作。
4. **预期结果**：得到一张「三类问题 ↔ 让 testbench 少做的事」对照表。
5. 说明：纯阅读归类，无需运行环境。

#### 4.2.5 小练习与答案

**练习 1**：「给 AXI 总线自动发一笔写事务并检查响应」属于三类问题中的哪一类？
**参考答案**：主要属于第 1 类 BFM（Bus-Functional-Models），因为它在替你驱动/检查总线接口；其中「检查响应」的部分也用到了第 2 类 checking values。

**练习 2**：你写了一个「把 ADC 采样数据做 FIR 滤波」的可综合模块，想把它放进 psi_tb 方便复用，对吗？
**参考答案**：不对。它是可综合代码，属于 `What does not belong` 里「Code that is meant for synthesis」，应放进综合库（如 psi_common），而不是 psi_tb。

---

### 4.3 维护者、作者与 PSI HDL Library License

#### 4.3.1 概念说明

用别人的库之前，必须先搞清楚两件事：**谁在维护它**（出了问题能找谁、会不会持续更新），以及**它用什么许可证**（我能不能在商业项目里用）。

psi_tb 的许可证叫 **PSI HDL Library License**。它的本质是 **LGPL（GNU 宽通用公共许可证）+ 一条专门为固件/FPGA 增加的例外条款**。这条例外非常关键：

> 普通 LGPL 对「链接」有严格要求，在 FPGA 世界里（把库综合进 bitstream）会引发很大争议。PSI HDL License 的例外条款明确允许：**包含本库的二进制产物（包括 FPGA bitstream）可以按你自己的条款使用、复制、分发**。这让它在工业 FPGA 项目里比纯 LGPL 友好得多。

#### 4.3.2 核心流程

```
PSI HDL Library License
        │
        ├── 基础：LGPL（GNU Library GPL）v2 或更高
        │        → 对库本身的源码修改必须继续开源
        │
        └── 例外（EXCEPTION NOTICE，第 15–21 行）
                 → 允许把库「综合/固化」进 FPGA bitstream、flash 镜像
                    这类二进制产物，按使用者自己的条款发布
                 → 例外不适用于你从 GPL 代码里复制进来的部分
```

也就是说：**你可以放心把 psi_tb 综合进你的 bitstream 并闭源发布那个 bitstream**；但如果你修改了 psi_tb 的 VHDL 源码本身，对源码的修改仍要遵循 LGPL。

#### 4.3.3 源码精读

- [README.md:L3-L4](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L3-L4)：`## Maintainer` —— 当前维护者 Benoît Stef。

- [README.md:L6-L8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L6-L8)：`## Authors` —— 作者 Oliver Bründler（同时是 License 版权人之一）与 Benoît Stef。

- [README.md:L10-L11](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L10-L11)：许可证一句话说明——PSI HDL Library License = LGPL + 固件开发相关的额外例外。

- [License.txt:L1-L2](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/License.txt#L1-L2)：许可证名称与版本（Version 1.0）。

- [License.txt:L4](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/License.txt#L4)：版权人 `Oliver Bründler, Julian Smart, Robert Roebling et al`（1998–2018）。

- [License.txt:L11](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/License.txt#L11)：许可证基础是 GNU Library General Public Licence v2 或更高版本（即 LGPL）。

- [License.txt:L15-L21](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/License.txt#L15-L21)：**EXCEPTION NOTICE** 整段。其中 L19 明确把 FPGA bitstream、flash 镜像列为「binary」，允许它们按使用者自己的条款发布——这是本许可证对 FPGA 工程最友好的地方。

#### 4.3.4 代码实践

1. **实践目标**：能判断「我的 FPGA 项目能否闭源使用 psi_tb」。
2. **操作步骤**：
   - 打开 [License.txt:L15-L21](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/License.txt#L15-L21)。
   - 找到 L19 中明确提到 `FPGA-bitstreams or flash images` 的那句。
   - 用一句话回答：「如果我只把 psi_tb 综合进 bitstream、不修改它的源码，发布 bitstream 时需要开源我的工程吗？」
3. **需要观察的现象**：你会看到例外条款把「binary」显式扩展到了 FPGA bitstream，这正是普通 LGPL 没说清的地方。
4. **预期结果**：答案是「不需要开源你的工程」（前提是你没有把 psi_tb 的 VHDL 源码本身改了之后还按闭源发布）。
5. 说明：法律条款以原文为准，本讲只做工程层面的理解引导。

#### 4.3.5 小练习与答案

**练习 1**：PSI HDL Library License 和纯 LGPL 的关键差别是什么？
**参考答案**：多了一条 EXCEPTION NOTICE，明确允许把库以二进制形式（包括 FPGA bitstream、flash 镜像）综合/固化后，按使用者自己的条款发布，从而解决了纯 LGPL 在 FPGA「链接/综合」场景下的歧义。

**练习 2**：psi_tb 的当前 maintainer 是谁？作者有哪两位？
**参考答案**：maintainer 是 Benoît Stef；作者是 Oliver Bründler 和 Benoît Stef。

---

### 4.4 版本演进：从 V1.00 到 3.0.0

#### 4.4.1 概念说明

`Changelog.md` 是项目的「成长日记」。psi_tb 采用**语义化版本号** `major.minor.bugfix`，README 的 Tagging Policy 把规则讲得很清楚：

- **major**（主版本号）：有不**完全向后兼容**的改动就 +1（接口会变，老代码可能要改）。
- **minor**（次版本号）：**新增功能**就 +1（向后兼容，老代码不用改）。
- **bugfix**（修订号）：只修 bug、无功能变化就 +1。

用一条简单关系表示版本号变化对应的「升级成本」：

\[
\text{升级成本} \;\propto\; \text{被改动的版本号层级}
\]

也就是说，只升 bugfix 几乎零成本；升 minor 一般安全；升 major 必须看 changelog 里「not reverse compatible」的说明，检查自己的 testbench 是否受影响。

#### 4.4.2 核心流程：版本演进时间线

把 Changelog 从旧到新串起来，psi_tb 的成长大致是「**先有比较工具 → 再有活动/激励工具 → 再有 AXI/I2C 总线模型 → 最后做 v3 重构对齐 psi_common**」：

```
V1.00  首次发布
  │
V1.01  加入「带正确消息输出」的比较函数（compare 雏形）
  │
1.1.x  加入 AXI 类型互转包(axi_conv)、文本文件自动施加/检查(textfile)
  │
1.2.0  加入 std_logic/整数比较、activity 包、textfile 写文件
  │
1.3.0  加入选通发生器 GenerateStrobe、实数比较 RealCompare
  │
2.0.0  ★ 首个开源发布；不向后兼容（textfile 过程改名、数据改整数以支持 GHDL）
  │
2.1.0~2.2.x  加入 time 比较、Signed/Unsigned 比较、WaitForValueXXX、AXI 突发、若干 bugfix
  │
2.3.0  加入 I2C 包(psi_tb_i2c_pkg)
  │
2.4.x  加入 CI 脚本、WaitForClockCycles/ClockedWaitTime、扩充 I2C
  │
2.5.0~2.6.0  加入函数文档、textfile 失效选项、SignCompare2、AXI 64 位支持
  │
3.0.0  ★ 为对齐 psi_common v3 而做的重构
```

带 ★ 的两个大版本（2.0.0、3.0.0）是**不向后兼容**的里程碑，升级时需要格外留意。

#### 4.4.3 源码精读

- [README.md:L44-L49](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L44-L49)：`## Tagging Policy`，定义了 `major.minor.bugfix` 三段版本号各自升位的条件。这是读懂 Changelog 的「钥匙」。

- [README.md:L33-L42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L33-L42)：`# Dependencies`，声明 psi_tb 依赖 PsiSim（≥2.2.0）与 psi_common（**≥3.0.0**）。这一行直接解释了 3.0.0 为何要做重构。

- [Changelog.md:L123-L124](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L123-L124)：`## V1.00 — First release`，整个项目的起点。

- [Changelog.md:L116-L121](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L116-L121)：`## V1.01`，第一次出现「带正确消息输出的比较函数」，这是后续 compare 包（u3）的源头。

- [Changelog.md:L76-L81](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L76-L81)：`## 2.0.0`，首个开源发布，并明确列出**不向后兼容**的两处改动（textfile 过程改名、数据格式改整数以支持 GHDL）。

- [Changelog.md:L31-L36](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L31-L36)：`## 2.3.0`，加入 `psi_tb_i2c_pkg`，I2C BFM 正式登场（对应单元 u7）。

- [Changelog.md:L4-L7](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L4-L7)：`## 2.6.0`，加入 `SignCompare2`（十六进制打印结果，支持 >32 位）与 AXI 64 位支持重载。

- [Changelog.md:L1-L2](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L1-L2)：`## 3.0.0`，条目内容很简洁，写作「Refacrtoring compatible 3.0.0 Features」（原文有笔误 Refacrtoring）。结合 [README.md:L40-L41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L40-L41) 里 psi_common 要求 ≥3.0.0，以及 git 历史中 `49b83e2 refactored for v3 of psi_common`、`8ee9c06 new release prepare changelog and readme 3.0.0` 两条提交，可以确认：**3.0.0 的主要变化是「为对齐 psi_common v3 而做的重构」**，是一次不向后兼容的 major 升级。

> 提示：3.0.0 在 Changelog 里只写了一行。当我们想了解 major 重构到底动了哪些接口时，单靠 Changelog 不够，需要结合 git 提交历史（`git log`、`git show 49b83e2`）去看。这也是后续高级讲义（u8-l1）会讲的方法。

#### 4.4.4 代码实践（对应本讲主实践的后半部分）

1. **实践目标**：能说清 **3.0.0 相对 2.x 的主要变化点**。
2. **操作步骤**：
   - 打开 [Changelog.md:L1-L2](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L1-L2)，记下 3.0.0 条目原文。
   - 打开 [README.md:L40-L41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L40-L41)，看 psi_common 的版本要求。
   - 用只读 git 命令确认重构来源：`git log --oneline | grep -i refactor` 或 `git show 49b83e2 --stat`（只读、安全）。
3. **需要观察的现象**：你会看到 3.0.0 本身没有新增面向用户的功能，而是为了和 psi_common v3 接口对齐而做的内部重构。
4. **预期结果**：能用一句话回答——「3.0.0 是为对齐 psi_common v3 而做的重构（major 升级、不向后兼容），本身未在 Changelog 中列出新功能」。
5. 说明：`git show` 输出可能较长，建议加 `--stat` 只看改了哪些文件。

#### 4.4.5 小练习与答案

**练习 1**：某次升级只把 bugfix 号从 `.1` 改成了 `.2`，按 Tagging Policy，你的 testbench 大概率需要改代码吗？
**参考答案**：大概率不需要。bugfix 号变化意味着「只修 bug、无功能变化」，理论上是向后兼容的最小更新。

**练习 2**：从 V1.00 到 2.0.0 之间，哪一类能力是 psi_tb 最早补齐的？
**参考答案**：**数值比较 / 检查类能力**。V1.01 就引入了「带正确消息输出的比较函数」，比 activity、AXI、I2C 都早，这也解释了为什么 compare 包是后续 activity / BFM 复用的底座（依赖链见第 7 节）。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成下面这一个**综合阅读任务**（对应本讲规格里指定的代码实践任务）：

> **任务**：阅读 `README.md` 与 `Changelog.md`，用**一段话**写出
> （a）psi_tb 解决的三类问题；
> （b）v3.0.0 相对 v2.x 的主要变化点。

**操作步骤**：

1. 打开 [README.md:L16-L31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L16-L31)，提取「What belongs」里列的三类工具。
2. 打开 [Changelog.md:L1-L2](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L1-L2) 与 [README.md:L40-L41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L40-L41)，定位 3.0.0 的变化来源。
3. 写一段话（建议 80–150 字），同时覆盖（a）（b）两点。

**参考答案示例**（写完后再对照）：

> psi_tb 是一个仅供 testbench 使用、无需可综合的 VHDL 工具库，主要解决三类问题：① 总线功能模型（BFM，如 AXI、I2C）；② 数值检查函数（自动比较期望值与实际值并打印可读错误）；③ 自动激励生成（如随机发生器、时钟同步等待、选通脉冲）。3.0.0 相对 2.x 的主要变化是**为对齐 psi_common v3 而做的重构**（major 升级、不向后兼容），Changelog 该条目本身未列出新增功能，需结合 README 的依赖要求（psi_common ≥3.0.0）与 git 提交（`refactored for v3 of psi_common`）才能确认。

**预期结果**：你能脱稿说出 psi_tb 的定位、三类问题，以及 3.0.0 重构的本质。

---

## 6. 本讲小结

- psi_tb 是 PSI 开源的 **VHDL testbench 工具库**，定位是「**只给 testbench 用、不需要可综合**」——这给了它使用 VHDL 全部语言能力的自由。
- 它解决**三类问题**：**BFM（总线功能模型）、数值检查函数、自动激励生成**，这三类正好对应后续单元 u3/u4/u5/u7。
- 库的边界由 README 的 `What belongs / does not belong` 两节划清：项目专用代码、可综合代码、别的库的代码**都不收**。
- 维护者是 Benoît Stef，作者是 Oliver Bründler 与 Benoît Stef；许可证是 **PSI HDL Library License（LGPL + 固件/FPGA 例外）**，允许把库综合进 bitstream 后按自己的条款发布。
- 版本号遵循 `major.minor.bugfix` 语义化规则；**3.0.0 是为对齐 psi_common v3 的重构**，是 major、不向后兼容。
- 阅读源码时要留意原文里的小笔误（如 `Bus-Functional-Modelsim` 实为 BFM、`Refacrtoring` 实为 Refactoring），不要被字面意思误导。

---

## 7. 下一步学习建议

本讲只读了三份**文档**，还没碰任何 VHDL 代码。建议接下来：

1. **先看目录与构建**：学习 **u1-l2（仓库结构与目录组织）**，弄清 `hdl/`、`testbench/`、`sim/`、`scripts/` 各放什么，以及 `sim/config.tcl` 如何把源码组织成一次编译。
2. **再学怎么跑起来**：学习 **u1-l3（仿真环境与 CI 构建流程）**，掌握用 PsiSim 在 ModelSim / GHDL 下跑通 `psi_tb_i2c_pkg_tb`，这样后续讲义里所有「代码实践」你都有环境可验证。
3. **之后进入源码**：从最底层的 **u2（psi_tb_txt_util 文本工具）** 开始，因为打印、比较、错误消息几乎都依赖它。

一句话：**先认人（u1-l1）→ 再认路（u1-l2）→ 会启动（u1-l3）→ 才读码（u2 起）。**
