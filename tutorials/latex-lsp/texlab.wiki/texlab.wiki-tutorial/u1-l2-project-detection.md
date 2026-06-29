# 项目识别与根目录检测

## 1. 本讲目标

承接上一讲 **u1-l1「texlab 是什么」**：我们已经知道 texlab 是一个 LSP 语言服务器，负责把编辑器、TeX 引擎、PDF 阅读器缝合起来。本讲要回答一个更底层的问题——**texlab 怎么知道哪些 `.tex` 文件属于同一个文档？又怎么决定编译时从哪个目录、哪个文件开始？**

读完本讲，你应当能够：

- 理解 texlab 中 **「项目（project）」** 的含义：所有被编译进同一份文档的 `.tex` 文件集合。
- 讲清 **Discovery 算法**：向上遍历目录树 + 解析 `\input`/`\import` 构建依赖树，并**反复迭代直到不再变化**（不动点）。
- 掌握 **根目录检测的四步优先级**：`.texlabroot` → `Tectonic.toml` → `.latexmkrc` → 根源文件，以及每一步各自的影响（输出目录、src/build 目录、latexmkrc 推断等）。
- 说清楚 **为什么必须确定根目录**：因为 TeX 引擎的 `\input` 等命令基于「工作目录」而非「源文件所在目录」。

> 说明：本仓库 `latex-lsp/texlab.wiki` 是纯文档 wiki，本讲的「源码」主要是 [Project-Detection.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md)，并少量引用 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md) 中与输出目录相关的配置项。引用方式与读代码项目一致：给出永久链接和行号。

## 2. 前置知识

本讲假设你已经学完 u1-l1，并了解以下基础概念（不熟悉的给出最简解释）：

- **LaTeX 多文件组织**：一份长文档通常拆成多个 `.tex` 文件，主文件用 `\input{chapter1}` 或 `\import{路径}{文件}` 把子文件「包含」进来，编译时相当于把子文件内容原样插入。texlab 要识别项目，第一步就是解析这些「包含命令」。
- **`\input` 与 `\import` 的区别（直觉版）**：
  - `\input{foo}`：把 `foo.tex` 的内容插入当前位置，路径相对于「当前编译的工作目录」。
  - `\import{dir}{foo}`：类似 `\input`，但可以显式指定子目录，常用于更复杂的多目录工程。
  本讲你只需知道：**texlab 把这两类命令都当作「依赖边」**，顺着它们把更多文件拉进项目。
- **工作目录（working directory）vs 源文件目录**：这是本讲最关键的一个直觉。
  - 当你直接在终端跑 `pdflatex chapter1.tex`，工作目录就是终端的当前目录，`\input{shared/macros}` 会相对这个目录去找文件。
  - 而在多文件工程里，你希望编译永远从「主文件所在目录」出发。**TeX 引擎不会自动推断这一点**——它只认工作目录。所以 texlab 必须替你把工作目录设对，这个「对的目录」就是本讲的**根目录**。
- **构建产物（build artifacts）**：TeX 编译会产生一堆辅助文件，常见的有 `.aux`（交叉引用等辅助信息）、`.log`（编译日志）、`.pdf`（最终输出）。texlab 需要知道这些文件放在哪，才能读日志上报诊断、读 `.aux`/`.fls` 辅助项目信息。

> 如果你只记一句话，请记：**TeX 引擎的 `\input` 看的是「工作目录」，不是「源文件目录」——这正是 texlab 必须替你确定根目录的根本原因。**

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `Project-Detection.md` | 讲 texlab 如何识别「项目」并确定根目录 | 本讲的**主源码**，Discovery 算法与四步法都在这里 |
| `Configuration.md` | texlab 全部 `texlab.*` 配置项总表 | 根目录检测第一步/第三步会引用其中的 `auxDirectory`/`logDirectory`/`pdfDirectory` 与 `useFileList` |
| `Tectonic.md` | 用 tectonic 替代默认 TeX 引擎 | 根目录检测第二步 `Tectonic.toml` 的来龙去脉（细节见 u3-l3） |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**Discovery 算法**、**依赖树解析**、**根目录检测四步法**。

### 4.1 Discovery 算法：texlab 如何发现一个「项目」

#### 4.1.1 概念说明

当你在编辑器里打开**一个** `.tex` 文件时，texlab 收到的输入其实只有这一个文件。但一个 LaTeX 工程往往是**多文件**的：`main.tex` 里 `\input{chapter1}`，`chapter1.tex` 里又 `\input{section1}`……texlab 需要从这「一个文件」出发，把所有「编译进同一份文档」的文件都找出来。这个文件集合，texlab 称之为一个**项目（project）**。

为什么必须先建立项目？wiki 开篇就给了两个直接理由：

1. **跨文件可见性**：在 preamble（导言区，即 `\documentclass` 与 `\begin{document}` 之间）里 `\input` 进来的宏包/宏定义，应当**在其他项目文件里也可见**——比如补全、跳转、悬停提示都要能识别它们。
2. **确定根文档**：项目还用来确定交给 TeX 引擎编译的**根文档（root document）**——也就是那个最顶层、包含 `\documentclass` 的主文件。

#### 4.1.2 核心流程

Discovery 算法是一个**反复迭代、直到不再变化**的过程（即求不动点）。每一轮做两件事：

```text
初始化：workspace = { 你刚打开的那个 .tex 文件 }

重复：
  第 1 步（向上扩展）：从 workspace 里的文件所在目录开始，
                       逐级向「上」遍历父目录，直到遇到：
                       文件系统根 /、家目录、或一个「根目录」（见 4.3）。
                       把沿途每个目录里、尚未打开的 .tex 文件加入 workspace。
  第 2 步（向内扩展）：对 workspace 里的每个文件，解析其中的
                       \input / \import 等包含命令，把被引用的文件加入 workspace。
  若本轮 workspace 没有任何新增 → 停止；否则继续下一轮。
```

用更形式化的写法，这是一个**单调增长的不动点迭代**：

\[
W_{0}=\{\,\text{你打开的文件}\,\},\qquad
W_{n+1}=\text{expand}(\text{walkUp}(W_{n})\cup\text{resolveIncludes}(W_{n}))
\]

当 \(W_{n+1}=W_{n}\) 时停止。因为每轮只会「增加文件、永不删除」，且磁盘上的文件总数有限，所以这个过程**必然在有限步内收敛**。这个「必然收敛」的性质很重要：它意味着 texlab 不会陷入无限循环。

> 小提示：第 1 步「向上遍历」有一个明确的**上界**——遇到根目录就停（见 4.3）。这个设计很关键：否则 texlab 可能一路向上把你整个家目录的 `.tex` 都扫进来。也就是说，**根目录既是编译起点，也是 Discovery 向上扫描的「天花板」**。两个概念是耦合的。

#### 4.1.3 源码精读

先看 wiki 如何定义「项目」及其必要性：

[Project-Detection.md:1-3](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L1-L3) —— 原文：每当你打开一个 TeX 文件，texlab 会尝试找出所有属于**同一项目**的文件（即编译进同一文档的文件）；服务器需要这些信息来实现**大部分功能**，例如 preamble 里导入的宏包应在其他项目文件中也可见；项目还用于确定交给 TeX 引擎的**根文档**。

这一段同时回答了「什么是项目」「为什么需要项目」两个问题，是本讲的概念基石。

再看算法本身的三步描述：

[Project-Detection.md:5-12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L5-L12) —— `## Algorithm` 小节给出服务器识别项目的三个步骤（向上走目录树、构建依赖树、重复直到 workspace 不再变化）。

其中「向上走目录树」的具体边界在这里：

[Project-Detection.md:8-9](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L8-L9) —— 原文：沿目录树**向上**走，直到遇到文件系统根（`/`）、家目录或一个**根目录**；沿途把每个目录里**尚未打开**的 TeX 文件加载进来。

注意「尚未打开」三个字——它保证同一文件不会被重复处理，也呼应了上面「单调增长、必然收敛」的论述。

而「重复直到不变」这一句则是整个不动点迭代的收尾：

[Project-Detection.md:12](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L12) —— 原文：**重复**上述过程，直到 workspace 不再发生变化。

**补充：一个可选的「第三数据源」**。除了「向上走目录」和「解析 `\input`」，texlab 还能利用 TeX 引擎自己产出的 `.fls` 文件作为项目识别的**额外输入**。这在 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md) 里有专门一项：

[Configuration.md:54-62](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L54-L62) —— `texlab.build.useFileList`：设为 `true` 时，服务器会把 TeX 引擎产出的 `.fls` 文件作为项目识别的**额外输入**；但开启可能影响性能（默认 `false`）。

> `.fls` 是 TeX 引擎记录「本次编译实际读写过哪些文件」的清单，比静态解析 `\input` 更准（能抓到宏包内部再 `\input` 的文件），代价是要先编译一次。这与本模块的「静态 Discovery」是互补关系：静态算法快但可能漏，`.fls` 准但需要先有编译产物。

#### 4.1.4 代码实践

这是一个**结构理解型实践**，目标是让你亲手把「向上扩展 + 向内扩展 + 迭代」三步对号入座。

1. **实践目标**：用一个具体的多目录工程，推演 Discovery 算法每一轮 workspace 如何增长。
2. **操作步骤**：
   - 准备如下目录结构（先只建文件，不必编译）：
     ```text
     proj/
       main.tex            # 含 \documentclass 与 \begin{document}
       chapter1.tex        # 在 main.tex 中被 \input
       ch/
         section1.tex      # 在 chapter1.tex 中被 \input
       unrelated.tex       # 不被任何文件引用
     ```
   - `main.tex` 写 `\input{chapter1}`；`chapter1.tex` 写 `\input{ch/section1}`。
   - 假设你在编辑器里打开的是 `proj/ch/section1.tex`，**手动模拟** Discovery：
     - 初始 workspace = {`ch/section1.tex`}。
     - 第 1 轮第 1 步（向上）：从 `ch/` 走到 `proj/`，加载 `main.tex`、`chapter1.tex`、`unrelated.tex`（假设此时还没遇到根目录天花板）。
     - 第 1 轮第 2 步（向内）：解析 `\input`，从 `main.tex` 找到 `chapter1.tex`（已在），从 `chapter1.tex` 找到 `ch/section1.tex`（已在）。
     - 第 2 轮：没有新增 → 收敛。
3. **需要观察的现象**：`unrelated.tex` 虽然在 `proj/` 目录里被「向上扩展」扫进来了，但它既不被别人 `\input`，自己也不 `\input` 别人——它只是「物理上同目录」，未必属于同一逻辑文档。
4. **预期结果**：你能画出一张以 `main.tex` 为根的依赖树（`main → chapter1 → ch/section1`），并意识到「项目」更接近「依赖连通分量」，而非「同一文件夹」。
5. 本实践为纯推演，无需运行 texlab；若想真正观察 texlab 的判定，可调用后续 u4-l2 讲到的 `texlab.showDependencyGraph` 命令渲染 DOT 图对照——具体行为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：Discovery 算法为什么「必然在有限步内结束」？

> **答案**：因为每一轮只会向 workspace **新增**文件、永不删除（单调增长），而磁盘上的 `.tex` 文件总数有限，所以不可能无限增长，必然在某一轮没有新增时停止（达到不动点）。

**练习 2**：`texlab.build.useFileList` 开启后，Discovery 多了一个什么数据源？它的代价是什么？

> **答案**：多了 TeX 引擎产出的 `.fls` 文件作为项目识别的额外输入（比静态解析 `\input` 更准）。代价是可能影响性能，且需要先有一次编译产出 `.fls`，所以默认关闭。

### 4.2 依赖树解析：`\input`/`\import` 的迭代展开

#### 4.2.1 概念说明

4.1 讲的是 Discovery 的**整体框架**，本模块专门拆解其中的「第 2 步：构建依赖树」。

依赖树（dependency tree）是一棵以**根文档**为根、以「包含关系」为边的树：

```text
main.tex                 ← 根文档（含 \documentclass + document 环境）
 ├── chapter1.tex        ← 被 \input{chapter1}
 │    └── ch/section1.tex ← 被 \input{ch/section1}
 └── appendix.tex        ← 被 \input{appendix}
```

为什么需要「树」这种结构，而不是一个扁平的文件集合？因为 LaTeX 的包含是**有层次、可嵌套**的：

- `\input` 可以**任意嵌套**：`chapter1.tex` 里还可以再 `\input{section1}`。
- 因此**一趟扫描不够**：你必须先读到 `chapter1.tex`，才能发现它里面还 `\input` 了 `section1.tex`。这正是 4.1 把算法设计成「迭代到不动点」的直接原因——依赖边是**随着新文件加入而逐步暴露**的。

#### 4.2.2 核心流程

依赖树的构建可以看作一个**工作队列**驱动的展开过程：

```text
队列 Q = [ 初始打开的文件 ]
已处理集合 done = {}

while Q 非空：
    取出文件 f（出队）
    若 f ∈ done：跳过
    解析 f，找出所有 \input / \import 引用的文件 refs
    对每个 r ∈ refs：
        若 r 尚不在 workspace：加入 workspace，并入队 Q
    把 f 加入 done
```

每条「包含命令」就是依赖树上的一条**边**；把所有边连起来就得到整棵树。这棵树有两个用途：

1. **划定项目范围**：树上所有节点 = 项目成员。
2. **找根文档**：树的根节点（被别人包含、但自己不包含在别人里——准确说是「能到达所有文件」的最顶层主文件）就是根文档，也是 4.3 第 4 步「根源文件」判定的重要依据。

> 注意：texlab 的依赖树是**静态解析**得来的（读源码文本找 `\input`/`\import`），它和「编译后 `.fls` 给出的真实读写清单」可能不完全一致——后者更权威但需要先编译（见 4.1.3 的 `useFileList`）。

#### 4.2.3 源码精读

wiki 对依赖树这一步的描述虽然只有两行，但信息量很足：

[Project-Detection.md:10-11](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L10-L11) —— 原文：接着，通过解析 `\input` 或 `\import` 等**包含命令（include commands）**来构建**依赖树**；被引用的文件会被加入 workspace。

这两行确认了三个事实：

1. 依赖树的「边」由**包含命令**定义（典型代表 `\input`、`\import`）。
2. 解析结果组织成**树**结构（而非扁平集合）。
3. 被引用文件会被**加入 workspace**——这正是 4.1「向内扩展」的实现细节。

注意 wiki 用了 "such as `\input` or `\import`"（诸如）这种列举，意味着 texlab 识别的包含命令**不止这两种**，但 wiki 没有穷举全部（完整清单属于 texlab 主程序的实现细节，本 wiki 未给出，**待确认**）。

#### 4.2.4 代码实践

这是一个**调用链追踪型实践**，目标是顺着 `\input` 把依赖树画出来，并理解「为何要迭代」。

1. **实践目标**：构造一个**三层嵌套**的包含关系，验证「一趟扫描不够」。
2. **操作步骤**：
   - 建三个文件：
     - `a.tex`：`\documentclass{article}\begin{document}\input{b}\end{document}`
     - `b.tex`：`\input{c}`
     - `c.tex`：`Hello.`
   - 假设 texlab 打开 `a.tex`，推演展开过程：
     - 第 1 轮：解析 `a.tex` → 发现 `b.tex`，加入。
     - 第 2 轮：解析 `b.tex` → 发现 `c.tex`，加入。
     - 第 3 轮：解析 `c.tex` → 无包含命令，无新增 → 收敛。
   - 画出依赖树：`a → b → c`。
3. **需要观察的现象**：`c.tex` 直到第 2 轮才被发现——如果算法只跑一趟（只解析初始文件），就会漏掉 `c.tex`。
4. **预期结果**：你应能解释「为什么 wiki 要写 Repeat this procedure until there are no more changes」——正是因为包含关系可嵌套，单趟扫描必然漏文件。
5. 纯推演，无需运行；若本地装了 texlab，可用 `texlab.showDependencyGraph`（见 u4-l2）渲染 DOT 验证，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：假设 `a.tex` `\input{b}`，`b.tex` 又 `\input{a}`（循环包含）。texlab 的 Discovery 会无限循环吗？

> **答案**：不会。因为 workspace 用「已处理集合 / 已加入集合」去重（见 4.1 的「尚未打开」、4.2 的 `done` 集合），重复出现的文件不会再次入队；加上文件总数有限，迭代仍会收敛。当然，循环包含本身是 LaTeX 源码的错误，但 Discovery 算法本身不会因此卡死。

**练习 2**：依赖树的「根节点」在 texlab 里有什么实际用途？

> **答案**：根节点就是**根文档（root document）**——交给 TeX 引擎编译的主文件。它也是 4.3 第 4 步「根源文件」判定的核心：那个含 `document` 环境的最顶层文件。

### 4.3 根目录检测四步法：决定编译从哪里出发

#### 4.3.1 概念说明

4.1、4.2 解决了「项目里有哪些文件」。本模块解决另一个同等重要的问题：**根目录（root directory）在哪？**

为什么根目录如此关键？wiki 用一句话点透了根因：

> TeX 引擎的 `\input` 等命令，是基于引擎的**工作目录**来解析的，而不是基于「调用 `\input` 的那个源文件所在目录」。

举个具体例子说明这种「错位」：

- 假设工程是 `proj/main.tex` 里 `\input{chapters/ch1}`，而你想从 `proj/chapters/` 目录下直接编译。
- 如果你把工作目录设成 `proj/chapters/`，TeX 引擎会去找 `chapters/ch1.tex`（相对工作目录拼接），结果路径变成 `proj/chapters/chapters/ch1.tex`——**找不到**。
- 正确做法是把工作目录设成 `proj/`（即根目录），这样 `\input{chapters/ch1}` 才能正确解析到 `proj/chapters/ch1.tex`。

**texlab 的职责**：在调用 TeX 引擎前，把工作目录设成正确的根目录。而「正确的根目录」由一套**四步优先级算法**决定——texlab 从当前文件所在目录开始向上走，**依次尝试**下面四个条件，**命中第一个即停止**。

#### 4.3.2 核心流程

四步优先级如下（**顺序就是优先级，先命中先用**）：

```text
从当前文件所在目录开始，逐级向上，在每个目录尝试：

第 1 步  该目录是否有 .texlabroot 或 texlabroot 文件？
         → 命中：以此目录为根目录；
                 用 texlab.build.auxDirectory / logDirectory / pdfDirectory
                 三个配置去定位构建产物与辅助文件。

第 2 步  该目录是否有 Tectonic.toml 清单？
         → 命中：以此目录为根目录；
                 使用其中的 src 与 build 目录；
                 并把 _preamble.tex、_postamble.tex（若存在）加入项目。

第 3 步  该目录是否有 .latexmkrc 或 latexmkrc 文件？
         → 命中：以此目录为根目录；
                 使用 latexmkrc 内定义的设置；
                 若推断失败，退回用 auxDirectory / logDirectory / pdfDirectory 兜底。

第 4 步  以上都没命中？
         → 使用「根源文件」所在目录作为根目录。
           根源文件 = 含 document 环境的 TeX 文档；
           注意：\documentclass{subfiles} 的文档不算根源文件（被排除）。
```

把这四步的「触发文件」与「影响」整理成表：

| 步骤 | 触发文件（命中即用） | 命中后的影响 |
| --- | --- | --- |
| 1 | `.texlabroot` / `texlabroot` | 锁定根目录；用三个 `texlab.build.*Directory` 配置定位产物 |
| 2 | `Tectonic.toml` | 锁定根目录；用其 `src`/`build` 目录；追加 `_preamble.tex`/`_postamble.tex` |
| 3 | `.latexmkrc` / `latexmkrc` | 锁定根目录；用 latexmkrc 内设置；推断失败则退回三个 Directory 配置 |
| 4 | （无上述文件）| 用含 `document` 环境的根源文件所在目录 |

> 两个要点：
> - **优先级是「就近 + 类型」双重的**：既要向上找到最近的触发文件，又要按 1→2→3→4 的类型顺序。准确说是「在向上走的每一级，按类型顺序探测」。
> - **第 4 步有个反直觉的排除**：`\documentclass{subfiles}` 的文档虽然有 `document` 环境，但**不算**根源文件——因为 `subfiles` 包的设计就是让子文件被主文件包含编译，它本身不是独立根。

#### 4.3.3 源码精读

先看 wiki 对「为什么需要根目录」的论述：

[Project-Detection.md:14-17](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L14-L17) —— `# Root directory` 小节开头：由于 TeX 引擎的设计，`\input` 等命令基于引擎的**工作目录**而非父源文件所在目录解析，所以服务器必须自行确定根目录；texlab 沿文档的目录树向上走，并**按以下顺序**尝试各步。

这段是整个四步法的「动机声明」，理解了它，后面四步就只是「怎么找」的工程实现。

下面逐条精读四步原文。

**第 1 步——`.texlabroot`**：

[Project-Detection.md:19-20](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L19-L20) —— 检查是否存在 `.texlabroot`/`texlabroot` 文件；若存在，服务器用 `texlab.build.auxDirectory`、`texlab.build.logDirectory`、`texlab.build.pdfDirectory` 三个设置来定位构建产物与辅助文件。

这是 texlab 的「显式标记法」：你只要在想要作为根目录的文件夹里放一个空的 `.texlabroot` 文件，就强制把根目录钉在那里。三个 Directory 配置的含义见 [Configuration.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md)：

- [Configuration.md:66-75](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L66-L75) —— `texlab.build.auxDirectory`：非 latexmk 时，定义存放 `.aux` 文件的目录；用 latexmkrc 时 texlab 会自动推断；默认 `.`（与根目录同目录）。
- [Configuration.md:79-88](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L79-L88) —— `texlab.build.logDirectory`：定义存放编译日志的目录（行为同上）。
- [Configuration.md:92-101](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L92-L101) —— `texlab.build.pdfDirectory`：定义存放输出文件（如 PDF）的目录（行为同上）。

**第 2 步——`Tectonic.toml`**：

[Project-Detection.md:21](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L21) —— 检查是否存在 `Tectonic.toml` 清单；若存在，服务器使用其中的 `src` 与 `build` 目录，并把 `_preamble.tex`、`_postamble.tex`（若存在）加入项目。

`Tectonic.toml` 是 tectonic 引擎的项目清单文件（类似 `Cargo.toml` 之于 Rust）。这一步的妙处在于：**用 tectonic 的工程，texlab 直接复用它的目录约定**，不用你再单独放 `.texlabroot`。`_preamble.tex`/`_postamble.tex` 是 tectonic 项目里常见的「全局导言/结尾」文件，texlab 主动把它们纳入项目，保证补全/诊断覆盖到。tectonic 的具体配置见 [Tectonic.md](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Tectonic.md)（细节留到 u3-l3）。

**第 3 步——`.latexmkrc`**：

[Project-Detection.md:22](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L22) —— 检查是否存在 `.latexmkrc`/`latexmkrc` 文件；若存在，服务器使用 latexmkrc 内定义的设置；若推断失败，则退回用 `auxDirectory`/`logDirectory`/`pdfDirectory` 兜底。

注意这里的「**自动推断**」与「**兜底**」双层逻辑：texlab 会尝试读懂你的 latexmkrc（比如里面 `$out_dir = 'build'`），从而知道产物在哪；只有读不懂时才退回三个 Directory 配置。这也解释了为什么 [Configuration.md:71](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Configuration.md#L71) 三个目录项都注明「用 latexmkrc 时 texlab 会自动推断」——它们的「自动推断」正是发生在第 3 步。

**第 4 步——根源文件**：

[Project-Detection.md:23](https://github.com/latex-lsp/texlab.wiki/blob/bc7c636b3535c80821f6e50463375cea2a2790bd/Project-Detection.md#L23) —— 使用根源文件所在目录；根源文件是含有 `document` 环境的 TeX 文档；`\documentclass{subfiles}` 的文档被排除。

这是「没有任何标记文件」时的兜底策略：回到 4.2 的依赖树，找到那棵树的根（含 `document` 环境的主文件），它所在目录就是根目录。这也是最「自然」的判定——多数简单工程无需任何标记文件就能正确工作。

#### 4.3.4 代码实践

这是一个**预测 + 验证型实践**，目标是让你体会「四步优先级」如何决定根目录。这也是本讲规格指定的核心实践。

1. **实践目标**：在同一个多文件工程里，分别放置不同标记文件，预测 texlab 会选中哪个目录作为根目录。
2. **操作步骤**：
   - 建如下结构：
     ```text
     workspace/
       main.tex          # \documentclass{article} + \begin{document} ... \input{chapter1} ... \end{document}
       chapter1.tex      # 普通正文
     ```
   - **场景 A**：在 `workspace/` 放一个空文件 `.texlabroot`。预测：第 1 步命中，根目录 = `workspace/`，产物目录由三个 `texlab.build.*Directory` 决定（默认 `.`，即 `workspace/` 本身）。
   - **场景 B**：删掉 `.texlabroot`，在 `workspace/` 放一个 `Tectonic.toml`。预测：第 2 步命中，根目录 = `workspace/`，使用其 `src`/`build` 目录，并尝试纳入 `_preamble.tex`/`_postamble.tex`。
   - **场景 C**：删掉 `Tectonic.toml`，在 `workspace/` 放一个 `.latexmkrc`（内容如 `$out_dir = 'build';`）。预测：第 3 步命中，根目录 = `workspace/`，产物目录由 texlab 从 latexmkrc 推断（应为 `build/`）。
   - **场景 D**：删掉所有标记文件。预测：落到第 4 步，根目录 = 含 `document` 环境的 `main.tex` 所在目录 = `workspace/`。
3. **需要观察的现象**：四个场景下，**根目录都是 `workspace/`**（因为标记文件和 `main.tex` 同目录），但**产物目录的来源不同**——A 来自配置项、B 来自 `Tectonic.toml`、C 来自 latexmkrc 推断、D 默认 `.`。换言之，四步优先级在这个例子里主要影响「产物去哪找」，而不是「根目录是谁」。
4. **预期结果**：你能说清「同样是 `workspace/` 作为根目录，四种触发条件下产物目录的来源各不相同」。
5. **验证方式**：要真正观察 texlab 的判定，需要本地安装 texlab 并查看其日志/调用 `texlab.showDependencyGraph`（见 u4-l2）；本讲只能给出基于算法的预测，具体运行结果**待本地验证**。

> 进阶变式：把标记文件放到 `workspace/` 的**父目录** `repo/`，再在更深的子目录打开一个 `.tex` 文件，观察 texlab 向上走到哪一级停下。这能更明显地体现「向上遍历 + 命中即停」的行为。

#### 4.3.5 小练习与答案

**练习 1**：如果一个目录里**同时**有 `.texlabroot` 和 `Tectonic.toml`，texlab 会用哪个？

> **答案**：用 `.texlabroot`（第 1 步优先级高于第 2 步）。四步是「按顺序、命中即停」，第 1 步命中后不会再看 `Tectonic.toml`。

**练习 2**：为什么第 4 步要**排除** `\documentclass{subfiles}` 的文档？

> **答案**：`subfiles` 包的设计意图是让子文件既能独立编译、又能被主文件包含；它的文档虽有 `document` 环境，但**逻辑上不是整个工程的根**，真正的根是包含它的主文件。若不排除，texlab 会误把 subfiles 子文档当成根，导致编译起点错误。

**练习 3**：根目录检测和 4.1 的 Discovery 算法有什么耦合关系？

> **答案**：根目录既是「交给 TeX 引擎的编译工作目录」，也是 Discovery「向上遍历」的**天花板**——Discovery 第 1 步明确写到「向上走直到遇到根目录」。所以确定根目录会反过来限制项目扫描的范围，二者互相制约。

## 5. 综合实践

把本讲三个模块（Discovery 算法、依赖树解析、根目录检测）串起来，完成下面这个贯穿性任务。

**任务**：搭建一个多目录 LaTeX 工程，手动推演 texlab 的「项目识别 + 根目录检测」全过程，并用一张图把两者画在一起。

1. **搭建工程**：
   ```text
   repo/
     main.tex              # \documentclass{article} + document 环境，\input{chapters/ch1}、\input{appendix}
     appendix.tex          # 普通正文
     chapters/
       ch1.tex             # \input{shared/macros}
       shared/
         macros.tex        # 一些 \newcommand
     notes/                # 与本文档无关的笔记
       todo.tex
   ```
2. **推演 Discovery（4.1 + 4.2）**：假设你在编辑器打开 `repo/chapters/ch1.tex`，写出 workspace 每一轮的变化，直到收敛；并画出依赖树（应以 `main.tex` 为根）。
3. **推演根目录检测（4.3）**：分四种情况预测根目录与产物目录来源——
   - 不放任何标记文件；
   - 在 `repo/` 放 `.texlabroot`；
   - 在 `repo/` 放 `Tectonic.toml`；
   - 在 `repo/` 放 `.latexmkrc`（含 `$out_dir`）。
4. **画综合图**：在一张图里同时标出——
   - **依赖树**（用箭头表示 `\input` 关系）；
   - **根目录**（高亮标出选中的目录）；
   - **Discovery 的向上扫描范围**（从打开文件向上到根目录天花板）。
5. **自我验证**：回答两个问题——
   - `notes/todo.tex` 会被算进项目吗？（提示：它在向上扫描路径上吗？被任何文件 `\input` 吗？）
   - 为什么说「根目录同时是编译起点和扫描天花板」？
6. **预期结果**：一张能同时讲清「项目里有哪些文件（依赖树）」和「编译从哪出发、产物去哪（根目录）」的图，外加四种标记文件场景下的预测表。
7. 本实践为推演 + 搭建型，不依赖运行 texlab；若本地装了 texlab，可用 `texlab.showDependencyGraph`（u4-l2）渲染 DOT 图与你的手画图对照，**待本地验证**。

## 6. 本讲小结

- **项目（project）** = 所有编译进同一份文档的 `.tex` 文件集合；texlab 几乎所有功能都依赖它（跨文件可见性、确定根文档）。
- **Discovery 算法**是一个**不动点迭代**：每轮做「向上走目录树加载 `.tex`」+「解析 `\input`/`\import` 拉入被引用文件」，直到 workspace 不再增长；由于单调增长且文件有限，必然收敛。
- **依赖树**以包含命令为边、以根文档为根；因为 `\input` 可嵌套，所以必须**迭代**展开，单趟扫描必然漏文件。
- **根目录**之所以必须确定，是因为 TeX 引擎的 `\input` 基于**工作目录**而非源文件目录——texlab 替你把工作目录设对。
- **根目录检测四步法**（按优先级）：`.texlabroot` → `Tectonic.toml` → `.latexmkrc` → 根源文件（含 `document` 环境，排除 `subfiles`）；前两步/第三步还会连带决定产物目录的来源。
- **根目录与 Discovery 耦合**：根目录既是编译起点，也是 Discovery 向上扫描的「天花板」。

## 7. 下一步学习建议

- 下一讲进入 **第 2 单元 u2-l1「配置总览：命名空间、类型与占位符」**，系统讲解 `texlab.*` 配置模型。本讲反复提到的 `texlab.build.auxDirectory`/`pdfDirectory` 等就是这套配置语言的具体项，学完 u2 你会彻底看懂它们的 Type / Default / 占位符三要素。
- 若你想先看「根目录决定后，编译到底怎么跑」，可跳读 `Previewing.md`（对应 u3-l1），但建议先过一遍 u2 的配置模型，否则 `build.args`、`forwardSearchAfter` 等会难以理解。
- 想亲眼验证本讲的依赖树与根目录判定，可留意 **u4-l2「workspace/executeCommand 工作区命令」** 里的 `texlab.showDependencyGraph`——它能把你手画的依赖树渲染成 DOT 图，是检验本讲推演的最佳工具。
- 对 tectonic 工程里 `Tectonic.toml` 如何同时驱动根目录检测与编译感兴趣的同学，可预习 `Tectonic.md`（对应 u3-l3）。
