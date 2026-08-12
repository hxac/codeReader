# OH! 项目总览与定位

## 1. 本讲目标

本讲是整套学习手册的第一篇，目标是让一个**从未接触过 OH!** 的读者，读完之后能够回答以下问题：

1. OH! 到底是什么？它给谁用、解决什么问题？
2. 它用什么技术栈（HDL、版本）写成？
3. 它的设计哲学是什么？这三条哲学如何指导后续所有代码？
4. 它的开源协议（License）是什么？谁在维护它？
5. README 里声称的内容，和仓库里的真实情况是否完全一致？（学会「带着怀疑读文档」）

本讲不涉及任何 Verilog 语法细节，也不要求读者动手编译。它是后续所有讲义的「地基」。

---

## 2. 前置知识

本讲几乎没有门槛，但下面几个名词会反复出现，先建立一个模糊印象即可：

- **HDL（Hardware Description Language，硬件描述语言）**：用来「写硬件」的编程语言。Verilog 是最常用的一种。你写的不是一行行顺序执行的指令，而是「电路的结构」。
- **Verilog 2005**：Verilog 语言的一个标准化版本（IEEE 1364-2005）。OH! 明确选用它，因为它被几乎所有 EDA 工具和 FPGA 综合器支持，最通用、最稳定。
- **ASIC（Application-Specific Integrated Circuit，专用集成电路）**：为某款产品专门设计的芯片（相对于通用 CPU）。「做 ASIC」意味着要把设计真正流片（tapeout）成物理芯片。
- **FPGA（Field-Programmable Gate Array，现场可编程门阵列）**：一种可以反复「重新编程」的芯片，常用来在流片前验证设计是否正确。
- **RTL（Register Transfer Level，寄存器传输级）**：Verilog 代码最常见的一种抽象层级，描述寄存器之间的数据流动。
- **开源协议（License）**：规定别人可以怎样合法使用、修改、再发布这份代码的法律条款。

如果你对其中几个词还一知半解，没关系，本讲只在概念层面用到它们。后续讲义会逐步展开。

---

## 3. 本讲源码地图

本讲只读仓库根目录下、与「项目定位」和「License」最相关的 4 个文件：

| 文件 | 作用 | 本讲用它来 |
|------|------|-----------|
| `README.md` | 项目的「门面」，写明定位、模块清单、哲学、规范、License | 理解 OH! 是什么、技术栈、设计哲学、模块状态 |
| `LICENSE` | 完整的开源协议文本 | 确认协议类型与版权人 |
| `setup.py` | 一个 Python 打包配置文件（疑似历史遗留） | 对照协议声明、培养「质疑文档」的习惯 |
| `AUTHORS` | 项目作者名单 | 了解维护者背景 |

> 提示：本讲涉及的「目录结构」只是顺带提及，完整对照表放在下一讲《目录结构与模块全景》(u1-l2)。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 项目定位**、**4.2 License 与作者**。

### 4.1 模块：项目定位（OH! 是什么）

#### 4.1.1 概念说明

一句话定位：**OH!（Open Hardware）是一个面向芯片设计者的开源硬件构建模块库**。

把它类比成「数字电路的乐高积木库」：

- 每一块小积木，就是一个用 Verilog 写好的、可复用的硬件模块——比如一个 FIFO（先进先出队列）、一个 SPI 接口、一个 GPIO 控制器、一段高速链路。
- 你做芯片时，不必每次都从零画这些常用电路，而是直接「拼」OH! 提供的积木。

它面向的用户是**芯片/硬件设计者**（chip designers），不是写普通软件的程序员。它的卖点之一是「silicon proven」——也就是说，这些设计不只是纸上谈兵，而是有真实的、跨工艺节点（从 0.35 微米到 28 纳米）的流片经验背书。

#### 4.1.2 核心流程：OH! 的内容构成与技术栈

OH! 不是单一程序，而是一组分层组织的硬件模块。从底层到顶层，可以粗略理解为这样的「金字塔」：

```
            ┌─────────────────────────────┐
   顶层     │ parallella / padring         │  ← FPGA 板级顶层 / 芯片焊盘环
            ├─────────────────────────────┤
   系统     │ axi / edma / mio / elink     │  ← 总线桥、DMA、高速/轻量链路
            ├─────────────────────────────┤
   外设/协议│ emesh / gpio / spi / emailbox│  ← 片上网络协议、可配置外设
            ├─────────────────────────────┤
   基础库   │ stdlib / asiclib / stdcells  │  ← 触发器、FIFO、时钟、仲裁等原语
            └─────────────────────────────┘
```

技术栈非常克制，只有一条主线：

1. **语言**：标准 Verilog 2005（不是 SystemVerilog，不用最新特性，保证最大兼容性）。
2. **设计文件不含 `timescale`、不含延迟语句**——这些只属于仿真测试平台，不进可综合 RTL（这一点在后续讲义会反复看到）。
3. **可参数化**：几乎所有模块的位宽、深度都能用参数（parameter）配置，方便复用。

> 这个金字塔里每一层的具体目录，下一讲 (u1-l2) 会逐个对照。本讲你只需要记住「OH! 是分层积木库」这个直觉。

#### 4.1.3 源码精读

**(a) 项目定位与背书**——README 开宗明义：

[README.md:12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L12)

> OH! is an open-source library of hardware building blocks based on silicon proven design practices at 0.35um to 28nm. The library is being used by Adapteva in designing its next generation ASIC.

这段话给出三个关键信息：① 它是**开源**的硬件构建模块库；② 它基于 **0.35μm ~ 28nm 的流片实战经验**；③ 它被 **Adapteva** 公司用于下一代 ASIC 设计（Adapteva 是创始人 Andreas Olofsson 创办的并行计算芯片公司，这也是项目背景的一部分）。

**(b) 技术栈与规模**：

[README.md:14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L14)

> The library is written in standard Verilog (2005) and contains over 25,000 lines of Verilog code, over 150 separate modules. ...

注意两个数字：**「超过 25,000 行 Verilog 代码」「超过 150 个独立模块」**。这是 README 当年写下的规模。实际仓库现在已增长：仅 `rtl/`、`hdl/` 等设计目录下的 Verilog 就约 **2 万行**，全仓 `.v/.sv/.vh` 文件中的 `module` 声明合计约 **450+ 处**（含测试平台模块）。规模在膨胀，但「Verilog 2005」这条技术栈底线始终没变。

**(c) 设计哲学**——整个 OH! 的灵魂只有三句话：

[README.md:32-36](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L32-L36)

1. **Make it work**（先让它能跑）
2. **Make it simple**（再让它简单）
3. **Make it modular**（最后让它模块化、可复用）

这三条不是口号，而是**优先级**：先把功能做对，再追求简洁，最后才追求抽象和复用。读后续任何源码时，如果遇到「为什么这里写得这么朴素」，多半就是因为「Make it simple」压过了「炫技」。例如 stdlib 里的每个触发器（`oh_dffq.v` 等）都只做一件事、代码极短，正是这条哲学的体现。

**(d) 模块状态图例**——README 用一张表列出所有模块，并用三个字母标记「成熟度」：

[README.md:40-63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L40-L63)

| 标记 | 含义 |
|------|------|
| **SI** | Silicon validated——已在真实芯片上流片验证 |
| **FPGA** | FPGA validated——已在 FPGA 上验证 |
| **HH** | Hard hat area——「施工区」，即仍在开发、未必稳定 |

学习时优先读 **SI** 和 **FPGA** 的模块（如 `elink`、`emesh`、`gpio`、`spi`），它们最可靠；**HH** 模块可作进阶参考。

> ⚠️ **真实情况提醒**：README 这张表里写的是 `src/<name>/README.md` 这样的路径，但仓库根目录下**并没有 `src/` 目录**，模块其实直接放在顶层（`elink/`、`gpio/`、…）。而且表里提到的 `accelerator`、`chip`、`common`、`pic`、`risc-v` 等在当前仓库中已不存在；反过来，实际存在的 `stdlib`、`asiclib`、`stdcells`、`padring` 反而没有出现在 README 表里。这说明 **README 比代码旧**。完整对照表见下一讲 (u1-l2)。这是第一个「文档与代码不一致」的例子，请记住：**读开源项目时，代码才是事实，文档可能滞后**。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**，不需要编译，目的是让你亲手核对 README 的说法。

**实践目标**：用自己的话复述 OH! 的三条设计哲学，并验证 README 对模块「成熟度」的标注。

**操作步骤**：

1. 打开 [README.md 的 Philosophy 段](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L32-L36)，把三条哲学抄下来。
2. 用一句中文重新表述每一条（不要直译，要写出「它反对什么」）。
3. 滚动到 [模块表](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L40-L58)，把所有 **STATUS = SI** 的模块名列出来。

**需要观察的现象 / 预期结果**：

- 三条哲学按优先级排列：先「能用」、再「简单」、最后「模块化」。
- 标记为 **SI**（流片验证）的模块应当是：`chip`、`common`、`elink`、`emesh`、`pic`（共 5 个，以 README 表格为准）。
- 你会发现：其中 `chip`、`common`、`pic` 这几个名字在当前仓库里**找不到对应目录**——这正是上一段提醒的「文档滞后」现象。

> ⚠️ 待本地验证：如果你想在本地确认某模块是否真的不存在，可以在仓库根目录执行 `ls <模块名>`（例如 `ls pic`）。本讲不强制执行，记住这个核对方法即可。

#### 4.1.5 小练习与答案

**练习 1**：OH! 为什么坚持用 Verilog 2005，而不是更新的 SystemVerilog？

> **参考答案**：为了最大兼容性。Verilog 2005 被几乎所有 EDA 工具、仿真器和 FPGA 综合器支持。OH! 面向 ASIC 流片这种「工具链昂贵且分散」的场景，选最通用、最稳定的语言，能避免「在某工具上跑不起来」的麻烦——这正是「Make it work」哲学的体现。

**练习 2**：README 标注「SI / FPGA / HH」分别代表什么？如果你是一个刚接手 OH! 的新人，应该优先学习哪一类？

> **参考答案**：SI = Silicon validated（已流片验证）；FPGA = FPGA validated（FPGA 上验证）；HH = Hard hat area（施工区/开发中）。应优先学习 SI 与 FPGA 模块，因为它们最可靠、最可能正确。

---

### 4.2 模块：License 与作者

#### 4.2.1 概念说明

开源不等于「随便用」。一个开源项目能被怎样使用、修改、再发布，由它的 **License（开源协议）** 规定。看懂 License 是使用任何开源代码前的第一步——尤其对硬件项目，因为硬件开源还可能涉及专利、物理实现等额外问题。

OH! 仓库里与 License/作者相关的文件有三个：`LICENSE`（协议全文）、`setup.py`（打包配置里也写了一句 license）、`AUTHORS`（作者名单）。**它们之间并不完全一致**，这本身就是一次很好的「读源码」练习。

#### 4.2.2 核心流程：开源项目如何声明协议

一个规范的开源仓库，通常通过三条路径互相印证协议：

1. **根目录的 `LICENSE` 文件**：协议的完整法律文本。这是**最权威**的来源。
2. **README 的 License 段落**：用人话总结协议类型，并指向 `LICENSE` 文件。
3. **打包/构建配置**（如 `setup.py`、`package.json`）：在元数据字段里再写一遍 license。

理想情况下，三处声明应当一致。出现冲突时，**以 `LICENSE` 全文为准**。下面我们逐个核对 OH! 的这三处。

#### 4.2.3 源码精读

**(a) 协议全文——最权威来源**：

[LICENSE:1-3](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/LICENSE#L1-L3)

> The MIT License (MIT)
> Copyright (c) 2016 Andreas Olofsson

`LICENSE` 文件白纸黑字写明：协议是 **MIT License**，版权人是 **Andreas Olofsson**，年份是 2016。MIT 是一种非常宽松的协议——允许你几乎任意地使用、修改、再发布（包括商用），只要你在副本里保留这份版权声明即可。

**(b) README 的 License 段落**：

[README.md:205-206](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L205-L206)

> The OH! repository source code is licensed under the MIT license unless otherwise specified. ...

README 与 `LICENSE` **一致**：都是 MIT。同时它补了一句重要的话——「**除非另有说明**（unless otherwise specified）」，并且提到某些特定设计可能有自己的协议（在各自文件夹的 `LICENSE` 里，例如 `aes/LICENSE`）。也就是说：**仓库默认 MIT，但个别子模块可能例外**，使用某个具体模块前，最好再到它的目录下确认一次。

**(c) 打包配置——这里出现了矛盾！**

[setup.py:3-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/setup.py#L3-L15)

`setup.py` 是一个 Python `setuptools` 打包脚本。它的字段里写的是：

- `name='vsim'`（包名叫 `vsim`，而不是 `oh`）
- `package_dir={'': 'src'}`（源码目录指向 `src/`，但仓库里没有 `src/`）
- `license='Apache License 2.0'`（**协议写成了 Apache 2.0，与 `LICENSE` 的 MIT 冲突！**）

这是一个真实存在的**自相矛盾**：`LICENSE` 文件是 MIT，而 `setup.py` 声明 Apache 2.0。怎么判断？

- 按照「以 `LICENSE` 全文为准」的原则，OH! 的协议应认定为 **MIT**。
- `setup.py` 看起来是一份**历史遗留/未维护**的文件：它的包名 `vsim`、`src/` 目录都和当前仓库对不上，license 字段大概率是早期复制模板时遗留的错误。

这件事再次印证了 4.1 节的教训：**代码与文档都可能过时，多处声明要互相核对，以正式协议文件为准**。

**(d) 作者名单**：

[AUTHORS:1-5](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/AUTHORS#L1-L5)

OH! 的主要贡献者共 5 位：**Andreas Olofsson、Roman Trogan、Fred Huettig、Ola Jeppsson、Peter Saunderson**。其中 Andreas Olofsson 是项目发起人（也是 LICENSE 的版权人和 README 提到的 Adapteva 创始人）。了解作者背景有助于理解项目风格——这是一支有真实芯片交付经验的团队，所以代码风格偏「工程务实」而非「学术炫技」。

#### 4.2.4 代码实践

**实践目标**：亲手发现并解释 OH! 仓库里 License 声明的矛盾。

**操作步骤**：

1. 打开 [LICENSE](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/LICENSE#L1-L3)，记录：协议类型、版权人、年份。
2. 打开 [setup.py](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/setup.py#L3-L15)，记录其中的 `license=` 字段。
3. 对比两处，判断哪一个更可信，并说出你的依据。

**需要观察的现象 / 预期结果**：

- `LICENSE`：MIT，Copyright (c) 2016 Andreas Olofsson。
- `setup.py`：`license='Apache License 2.0'`，且包名是 `vsim`、源码目录是 `src/`（均与现状不符）。
- 结论：**以 `LICENSE` 为准，OH! 是 MIT 协议**。`setup.py` 的声明是过时/错误的——它整份文件都像未被维护的历史遗留。

> 💡 思考延伸：如果你要把 OH! 的某个模块用到一个商业芯片项目里，依据 MIT 协议你需要做什么？（答案：在分发时保留这份 MIT 版权声明即可；但仍需检查该模块目录下有没有「另有说明」的额外协议。）

#### 4.2.5 小练习与答案

**练习 1**：OH! 的默认协议是什么？依据是哪个文件？

> **参考答案**：MIT License。依据是根目录的 `LICENSE` 全文（`The MIT License (MIT)`，Copyright (c) 2016 Andreas Olofsson）。README 的 License 段落与之吻合，而 `setup.py` 中的 Apache 2.0 声明与之冲突，应以正式协议文件为准。

**练习 2**：README 说「unless otherwise specified」，这对使用者意味着什么？

> **参考答案**：意味着仓库整体是 MIT，但**个别子模块可能有独立的、不同的协议**。使用某个具体模块前，应到它的文件夹下查找是否存在单独的 `LICENSE` 文件，避免误用。

**练习 3**：MIT 协议对「商用」是什么态度？

> **参考答案**：MIT 非常宽松，明确允许商业使用、修改、再发布甚至闭源分发，核心义务只有一条——在副本中保留原始版权与许可声明。

---

## 5. 综合实践

**任务：制作一份《OH! 一页速览》（Cheat Sheet）**

把本讲学到的内容整理成一张单页文档（Markdown 或纯文本），必须包含以下 6 个板块，且每个板块至少给出一条**来自真实源码**的依据（带文件名）：

1. **一句话定位**：OH! 是什么？（依据：`README.md`）
2. **技术栈**：用什么语言、什么版本？（依据：`README.md`）
3. **三条设计哲学**：按优先级写出它们对你意味着什么。（依据：`README.md`）
4. **License**：协议类型、版权人、以哪个文件为准，以及你发现的矛盾。（依据：`LICENSE` + `setup.py`）
5. **作者**：列出至少 3 位贡献者。（依据：`AUTHORS`）
6. **一处「文档与代码不一致」**：用一句话记录你发现的 README 与实际仓库的差异。（依据：README 的 `src/` 路径 vs 实际顶层目录）

**验收标准**：

- 每个板块都能追溯到具体的文件名（最好带行号或永久链接）。
- 第 4 板块必须同时写出 MIT 和 Apache 2.0 两处声明，并给出「以哪个为准」的判断与理由。
- 第 6 板块必须是一个**真实的、可被核对**的差异，不能是泛泛而谈。

> 完成后，这张速览就是你的「学习手册封面」——后续每一讲，你都可以回头对照它，把新学的模块填进对应的金字塔层级里。

---

## 6. 本讲小结

- **OH! 是什么**：面向芯片设计者的开源硬件构建模块库，用标准 **Verilog 2005** 写成，背后是 0.35μm–28nm 的真实流片经验。
- **三条设计哲学**（按优先级）：Make it work → Make it simple → Make it modular。它们解释了后续源码「为什么这么朴素」。
- **模块成熟度图例**：SI（流片验证）> FPGA（FPGA 验证）> HH（施工区）；优先学习 SI/FPGA 模块。
- **协议**：默认 **MIT**（Copyright (c) 2016 Andreas Olofsson），以 `LICENSE` 全文为准；个别子模块可能「另有说明」。
- **关键习惯**：README 与 `setup.py` 都存在与现状不符的内容（`src/` 路径不存在、`setup.py` 的 license 写成 Apache 2.0）。**代码与协议文件才是事实，文档可能滞后**——这是贯穿全手册的阅读原则。
- **作者**：Andreas Olofsson 等 5 人，团队有真实芯片交付经验，代码风格偏工程务实。

---

## 7. 下一步学习建议

本讲只建立了「OH! 是什么」的宏观印象，还没有真正看过一行电路代码。下一讲建议进入：

- **u1-l2《目录结构与模块全景》**：把本讲提到的「金字塔」落实成一张完整的**目录对照表**，逐个搞清 `stdlib`、`asiclib`、`stdcells`、`emesh`、`elink`、`axi`、`edma`、`gpio`、`spi`、`padring`、`parallella` 等顶层目录的职责，并系统整理 README 与实际布局的差异。
- 在那之后，**u1-l3《仿真环境搭建》** 会教你用 iverilog + gtkwave 第一次把一个模块跑起来；**u1-l4《Verilog 2005 与 OH! 编码规范》** 则会带你读最简单的 stdlib 源码（如 `oh_dffq.v`）。

建议的延伸阅读（来自 README 的 Recommended Reading）：

- [docs/verilog_reference.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/verilog_reference.md)——OH! 自己整理的 Verilog 参考表。
- [docs/chip_glossary.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/chip_glossary.md)——芯片术语表，遇到不认识的名词随时查。
