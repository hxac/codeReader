# 项目定位：这是什么仓库

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应当能够：

- 说清楚这个仓库 `pku-minic/online-doc` 到底交付了什么、面向谁；
- 区分两个容易混淆的概念——「文档站本身」和「文档所教授的编译器」；
- 用一句话说清 SysY、Koopa IR、RISC-V 这三者在编译流水线里各自的角色，以及它们为什么被串在一起。

这是一篇纯概念性的入门讲义，不会涉及复杂代码。打好这个底子，后续讲「站点怎么跑起来」「链接检查器怎么写」时你才不会跑偏。

## 2. 前置知识

本讲几乎不需要任何前置知识。如果你了解下面这些名词会更轻松，但不是必需的：

- **编译器（compiler）**：把一种语言（源语言）翻译成另一种语言（目标语言）的程序。例如把 C 翻译成机器码。
- **汇编（assembly）**：贴近硬件的低级语言，每条指令大致对应一条机器指令。RISC-V 就是其中一种汇编格式。
- **静态网站（static site）**：内容由一堆固定文件（这里主要是 Markdown）组成、不需要后端服务器动态生成页面的网站。
- **Markdown**：一种用纯文本写带格式文档的标记语言，比如用 `#` 表示标题、用 ```` ``` ```` 包围代码块。

如果你对上面的名词完全陌生也没关系，本讲会边讲边解释。

## 3. 本讲源码地图

本讲只涉及三个「入口性质」的文件，它们共同回答「这是什么仓库」：

| 文件 | 作用 |
| --- | --- |
| `README.md`（仓库根目录） | 面向开发者的项目说明：告诉访客这是一个基于 Docsify 的文档站，以及如何在本地启动它。 |
| `docs/README.md` | 文档站的**首页正文**。用通俗的语言告诉学生：你要实现一个把 SysY 编译到 RISC-V 的编译器。 |
| `docs/preface/lab.md` | 「实验说明」。把整个编译器任务拆成 SysY→Koopa IR→RISC-V 两个步骤、十个阶段。 |

> 提示：注意有两个 `README.md`。根目录的那个写给「看 GitHub 仓库的人」，`docs/README.md` 才是文档站访问者第一眼看到的首页。这个区别在第 4.2 节会再次用到。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：①项目背景与目标；②文档站 vs 编译器本体；③SysY/Koopa IR/RISC-V 流水线概览。

### 4.1 项目背景与目标

#### 4.1.1 概念说明

这个仓库叫 `online-doc`，全称是 **PKU Compiler Course Online Documentation**——北京大学编译原理课程实践的**在线文档**。

它面向的人群很明确：选了「编译原理课程实践」的同学。它的目标也很明确：**用一系列循序渐进的文档，引导同学从零开始写出一个真正的编译器**。

要注意一个反直觉的点：这个仓库**本身不是编译器**，也不教你「读别人的编译器源码」。它是一份**教学手册**，输出的是「怎么一步步把编译器造出来」的方法。这一点是理解整个项目定位的关键，下一节会专门展开。

#### 4.1.2 核心流程

从「访客打开仓库」到「理解项目定位」，信息流向大致是：

```text
访客打开 GitHub 仓库
      │
      ▼
根目录 README.md（告诉你：这是文档站，用 Docsify 跑）
      │
      ▼
docs/ 目录（文档站的真正内容根）
      │
      ▼
docs/README.md（首页：你要写一个 SysY→RISC-V 编译器）
      │
      ▼
docs/preface/lab.md（实验说明：拆成两步、十个阶段）
```

也就是说，根目录 README 是「门牌」，`docs/` 才是「展厅」。

#### 4.1.3 源码精读

先看根目录 README 的开头，它一句话点明了项目身份：

> 项目说明：根目录 [README.md:L1-L5](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md#L1-L5) —— 标题是 “PKU Compiler Course Online Documentation”，正文写明 “Online documentation for PKU compiler course.”，并给出在线访问地址（GitHub Pages）。

关键几行原文（示例节选）：

```text
# PKU Compiler Course Online Documentation

Online documentation for PKU compiler course.

[Visit this documentation on GitHub Pages](https://pku-minic.github.io/online-doc/)
```

再看文档站首页 `docs/README.md` 的开头，它直接对学生喊话：

> 首页：[docs/README.md:L1-L5](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/README.md#L1-L5) —— 标题是「北大编译实践在线文档」，欢迎语之后立刻给出课程目标：“你将实现一个可将 SysY 语言编译到 RISC-V 汇编的编译器”。

这两段合起来，就把「项目背景与目标」说清了：**给北大编译实践课用的在线教学文档，目标是教会学生写一个 SysY→RISC-V 编译器。**

#### 4.1.4 代码实践

这是一个「源码阅读型实践」，目标是亲手确认上面的判断。

1. **实践目标**：通过阅读入口文件，确认「项目背景与目标」。
2. **操作步骤**：
   - 用编辑器或 `Read` 打开根目录 `README.md`，找到标题与 “Online documentation” 那一行。
   - 再打开 `docs/README.md`，读前 5 行。
3. **需要观察的现象**：两份 README 的语气和受众不同——根目录偏「工程说明」，`docs/README.md` 偏「对学生的开场白」。
4. **预期结果**：你能在根目录 README 里看到 Docsify / GitHub Pages 的字样；在 `docs/README.md` 里看到「编译器」「SysY」「RISC-V」等词。
5. 若无法在本地打开文件，可改为在 GitHub 网页上对照上面的永久链接查看，结论一致。

#### 4.1.5 小练习与答案

**练习 1**：根目录 `README.md` 和 `docs/README.md`，哪一个是文档站访问者最先看到的内容？
**参考答案**：是 `docs/README.md`。文档站的内容根是 `docs/`，访问者打开站点看到的是该目录下的 `README.md`；根目录的 `README.md` 只在 GitHub 仓库页面展示，给浏览源码的人看。

**练习 2**：项目的在线访问地址托管在哪里？（提示：看根目录 README）
**参考答案**：托管在 GitHub Pages，地址为 `https://pku-minic.github.io/online-doc/`。

---

### 4.2 文档站 vs 编译器本体

#### 4.2.1 概念说明

这是本讲最容易踩坑、也最重要的一个区分。

很多人一听说「编译器项目」，会本能地以为仓库里有一堆 `.c`/`.cpp`/`.rs` 的编译器源码，clone 下来就能 `make` 出一个编译器。**但这个仓库不是这样。**

它的角色对比如下：

| 维度 | 文档站（本仓库 `online-doc`） | 编译器（你要写的东西） |
| --- | --- | --- |
| 是什么 | 一堆 Markdown + 一个 Docsify 站点 | 一个把 SysY 翻译成 RISC-V 的程序 |
| 谁写的 | 课程组（已经写好） | **你**（学生，从零开始） |
| 交付物 | 教学说明、规范、示例 | 可运行、能通过 OJ 评测的编译器 |
| 语言 | Markdown 为主 | 通常是 Rust / C++（由你决定） |

一句话总结：**这个仓库交付的是「教人写编译器的文档」，而不是「编译器」本身。** 编译器是学生照着这份文档亲手造出来的成果，并不在这个仓库里。

#### 4.2.2 核心流程

如何用证据支撑上面这个判断？两条线索：

```text
线索 A（措辞）：docs/README.md 里写的是「你将实现一个……编译器」
              —— 主语是「你」，说明编译器由读者实现，而非仓库提供。

线索 B（文件）：仓库里没有任何编译器源码，
              唯一的程序代码是文档工具 scripts/check_links.py 和前端 JS。
```

两条线索都指向同一个结论：**仓库 = 文档，编译器 = 你的作业。**

#### 4.2.3 源码精读

先看措辞证据。`docs/README.md` 第一段：

> [docs/README.md:L1-L5](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/README.md#L1-L5) 中明确写：「在本课程中, **你**将实现一个可将 SysY 语言编译到 RISC-V 汇编的编译器」。注意「你将实现」——编译器是读者要实现的，不是仓库自带的。

作为对照，根目录 README 把仓库自己的身份说得很克制：

> [README.md:L1-L3](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/README.md#L1-L3) 自我定位就是 “Online documentation for PKU compiler course.”——一份「在线文档」。

再看文件证据。仓库里**确实没有编译器源码**。通篇只有 Markdown 文档、前端资源（`docs/assets/`）、静态站点配置（`docs/index.html`）、CI 配置（`.github/`），以及唯一的可执行脚本——一个用来检查文档链接的 Python 工具 `scripts/check_links.py`（它服务于文档，不服务于编译）。

#### 4.2.4 代码实践

这是一个「验证型实践」，用命令亲自确认「仓库里没有编译器」。

1. **实践目标**：证明本仓库不含编译器实现代码。
2. **操作步骤**：在仓库根目录运行只读命令查看所有被跟踪的文件类型：
   ```bash
   git ls-files | sed 's/.*\.//' | sort | uniq -c | sort -rn
   ```
   （统计每种扩展名出现的次数；如果你不方便跑这条命令，也可直接浏览 `git ls-files` 的输出。）
3. **需要观察的现象**：出现次数最多的是 `.md`（文档），此外会有少量 `.py`、`.js`、`.css`、`.png`、`.yml`、`.toml`、`.html` 等。
4. **预期结果**：你**不会**看到成片的 `.c`/`.cpp`/`.rs` 编译器源码；`.py` 文件只有 `scripts/check_links.py` 这一个文档工具。这正好印证「仓库 = 文档」。
5. 这一点已被笔者通过 `git ls-files` 核实：仓库内除文档与站点资源外，唯一的程序代码是 `scripts/check_links.py`。

#### 4.2.5 小练习与答案

**练习 1**：假设有同学说「我把这个仓库 clone 下来就能得到一个能用的编译器」，这句话对吗？为什么？
**参考答案**：不对。仓库里没有任何编译器源码，只有教学文档和站点资源。编译器是要由学生照着文档自己实现的产物，不包含在本仓库中。

**练习 2**：仓库里唯一的 Python 脚本 `scripts/check_links.py` 是做什么用的？它和「编译器」有关系吗？
**参考答案**：它是一个**文档链接检查器**，用于自动检测文档站里的 Markdown / HTML 链接是否失效。它服务的是「文档本身的质量」，和编译器没有任何关系。（它也是本手册第四单元的核心代码，后续会专门精读。）

---

### 4.3 SysY/Koopa IR/RISC-V 流水线概览

#### 4.3.1 概念说明

课程要你写的编译器，把一种语言翻译成另一种语言。这条流水线上有三个关键名词：

- **SysY**：课程的**源语言**。它是一种「精简版的 C 语言」，语法上和 C 很像（有 `int`、`if`、`while`、函数等），但砍掉了大量复杂特性，方便教学。你的编译器**吃进** SysY 程序。
- **Koopa IR**：课程**专门设计**的中间表示（IR, intermediate representation）。可以理解成「编译器内部的、比汇编高层一点但又比源语言底层一点」的一种表示。它形似 LLVM IR，但做了大量简化。它把「翻译」这件事拆成两段，降低难度。
- **RISC-V**：**目标语言**，一种新兴且热门的指令系统（ISA）。你的编译器最终**输出** RISC-V 汇编。文档里特意标注它读作 “risk-five”。

把三者串起来，编译器做的事情可以写成一次「函数复合」式的变换：

\[
\text{compile}: \quad \text{SysY} \;\xrightarrow{\text{前端}}\; \text{Koopa IR} \;\xrightarrow{\text{后端}}\; \text{RISC-V}
\]

为什么要中间塞一个 Koopa IR，而不是直接 SysY→RISC-V？因为一步到位太难。拆成两段后，「理解源语言」和「生成目标汇编」可以分别攻克。

#### 4.3.2 核心流程

文档在 `docs/preface/lab.md` 里把流水线拆成**两个步骤**：

```text
步骤 1（前端）：SysY  ──编译──▶  Koopa IR
步骤 2（后端）：Koopa IR  ──生成──▶  RISC-V 汇编
```

为了让难度循序渐进，文档又把这两步进一步细分成**十个阶段（Lv0–Lv9）**，外加可选的 **Lv9+**。每个 Lv 都给编译器「增加一点新能力」：

| 阶段 | 给编译器增加的能力（节选） |
| --- | --- |
| Lv0–Lv2 | 熟悉环境与框架，先做出只认 `main` + `return` 的迷你编译器 |
| Lv3 | 表达式（加减乘除模、比较、逻辑） |
| Lv4 | 变量与常量 |
| Lv5 | 语句块与作用域 |
| Lv6–Lv7 | `if` / `while` 控制流 |
| Lv8 | 函数与全局变量 |
| Lv9 | 数组 |
| Lv9+（可选） | 寄存器分配、优化、SSA 等进阶内容 |

这套「两步流水线 × 十个增量阶段」的设计，正是本仓库教学内容的主干。

#### 4.3.3 源码精读

两步流水线的定义在实验说明里写得很直白：

> [docs/preface/lab.md:L5-L9](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L5-L9) ——「需要开发一个将 SysY 语言编译到 RISC-V 汇编的编译器……我们把它分成了两个步骤：1. 将 SysY 语言编译到 Koopa IR。2. 将 Koopa IR 生成到 RISC-V 汇编。」

紧接着定义了什么是 Koopa IR：

> [docs/preface/lab.md:L10-L12](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L10-L12) ——「Koopa IR 是我们为编译原理课程实践设计的一种中间表示 (IR)……形式上类似于 LLVM IR, 但简化了相当多的内容」，并提到为它配备了配套的运行时库（`koopa`），帮你的编译器解析/生成 Koopa IR。

而「十个阶段」的总纲同样在这份文件里：

> [docs/preface/lab.md:L35-L43](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L35-L43) ——「这篇在线文档会按照 SysY 到 Koopa IR 再到 RISC-V 汇编的思路, 将两个步骤进一步细分为十个阶段」, 随后逐条列出 Lv0–Lv9 各自要加的能力。

作为直观印证，文档首页 `docs/README.md` 还直接给出了一段 SysY 源码（一个递归 `fib` 函数）和它「编译后」的 RISC-V 汇编示例：

> [docs/README.md:L7-L24](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/README.md#L7-L24) 给出 SysY 示例（类 C 语法）；[docs/README.md:L26-L74](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/README.md#L26-L74) 给出对应的 RISC-V 汇编示例，让你直观看到「输入长什么样、输出长什么样」。

#### 4.3.4 代码实践

这是一个「阅读 + 归纳型实践」。

1. **实践目标**：把「十个阶段」和「两步流水线」对应起来。
2. **操作步骤**：
   - 打开 [docs/preface/lab.md:L37-L43](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L37-L43)，逐条读 Lv0–Lv9 的描述。
   - 试着判断：每个 Lv 主要是在「步骤 1（SysY→Koopa IR，前端）」发力，还是在「步骤 2（Koopa IR→RISC-V，后端）」发力？（提示：Lv1 主要是搭框架、生成 IR；Lv2 才开始把 IR 生成到 RISC-V。）
3. **需要观察的现象**：你会看到难度是渐进的——从只处理 `return`，到表达式、变量、控制流，再到函数、数组。
4. **预期结果**：你能用一句话说出「Lv0–Lv9 是把两步流水线拆细后的增量路线图」。至于每个 Lv 精确属于前端还是后端，留给后续 `u3-l1`（实验分层与编译流水线映射）详细讨论。
5. 本实践为阅读型，不涉及运行命令；结论可在本地阅读文档后自行归纳。

#### 4.3.5 小练习与答案

**练习 1**：把 SysY 直接翻译成 RISC-V 一步到位，和「SysY→Koopa IR→RISC-V」两步走相比，课程为什么选择后者？
**参考答案**：因为一步到位难度太大。引入 Koopa IR 作为中间表示，可以把「理解源语言（前端）」和「生成目标汇编（后端）」解耦，分别攻克，降低实现难度，也更接近真实编译器（如 LLVM）的分层结构。

**练习 2**：Koopa IR 是现成的工业标准吗？它和 LLVM IR 是什么关系？
**参考答案**：不是工业标准。它是课程组**专门为本次实践设计**的中间表示，形式上**类似** LLVM IR，但做了大量简化，只保留教学需要的部分，并配有运行时库 `koopa` 辅助生成与解析。

**练习 3**：RISC-V 怎么读？它在流水线里是源语言还是目标语言？
**参考答案**：读作 “risk-five”。它是流水线的**目标语言**——你的编译器最终输出 RISC-V 汇编。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿性小任务（这也是本讲的核心实践）：

1. 仔细阅读 [docs/README.md:L1-L5](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/README.md#L1-L5) 和 [docs/preface/lab.md:L5-L12](https://github.com/pku-minic/online-doc/blob/d172f8994369fba0f826ac3f1c2f4d6bf63c9ea5/docs/preface/lab.md#L5-L12)。
2. 用你自己的话写一段**约 200 字**的说明，回答：这个仓库交付的是「编译器」还是「教人写编译器的文档」？请给出你的判断和至少一条来自源码的依据。
3. 在说明的末尾，**按顺序**列出 SysY→Koopa IR→RISC-V 三个阶段分别是什么，并各用一句话描述其角色（源语言 / 中间表示 / 目标语言）。

**参考答案要点**（供你对照）：

- 判断：交付的是「**教人写编译器的文档**」，不是编译器本身。
- 依据示例：根目录 README 自称 “Online documentation”；`docs/README.md` 写「**你**将实现一个……编译器」，主语是读者；`git ls-files` 显示仓库里没有任何编译器源码，唯一程序代码是文档工具 `scripts/check_links.py`。
- 三阶段：
  1. **SysY**——源语言，精简版 C，编译器的输入；
  2. **Koopa IR**——课程自设计的中间表示，把翻译过程拆成两段的「中转站」；
  3. **RISC-V**——目标语言（汇编），编译器的最终输出。

## 6. 本讲小结

- `online-doc` 是北京大学编译原理课程实践的**在线文档站**，基于 Docsify 构建，面向选课学生。
- 要时刻记住：仓库交付的是「**教人写编译器的文档**」，编译器本身由学生实现、不在仓库里；仓库唯一的程序代码是文档工具 `scripts/check_links.py`。
- 课程要你写的编译器，把 **SysY**（源语言，精简版 C）经 **Koopa IR**（课程自设计的中间表示）翻译到 **RISC-V**（目标汇编，读作 “risk-five”）。
- 文档把这条「SysY→Koopa IR→RISC-V」流水线拆成**两步**，再细分成 **Lv0–Lv9 十个增量阶段**（外加可选 Lv9+）。
- 仓库里有两个 `README.md`：根目录的写给 GitHub 访客，`docs/README.md` 才是文档站首页。

## 7. 下一步学习建议

理解了「这是什么」之后，下一步自然是「怎么把它跑起来」。建议进入：

- **u1-l2《在本地把文档站跑起来》**：动手安装 docsify-cli，用 `docsify serve docs` 在本地启动站点。
- **u1-l3《仓库目录结构一览》**：系统了解 `docs/`、`scripts/`、`.github/`、`assets/` 各自的职责。

如果你对「两步流水线 × 十个阶段」的精细对应关系感兴趣，可以跳到 **u3-l1《实验分层与编译流水线映射》**，但建议先完成 u1 的本地运行，有一个能点开的站点再读会更有体感。
