# 文档与实例生成工具链

## 1. 本讲目标

本讲是「工具链、二次开发与验证」单元的第一篇。前面十几讲我们一直在**读**这本书的电路模块，本讲反过来回答一个工程问题：**这本书自己是怎样被生产、维护、并对外发布的？**

具体地，读完本讲你应该能够：

1. 说清 `v2h.py` 如何把「带注释的 Verilog 源码」渲染成网页，以及它赖以成立的「一模块一文件、文件名=模块名」约定。
2. 用 `generate_file_skeleton.py`、`generate_instance.py`、`verilinter` 这三件小工具，把写新模块时的样板劳动自动化。
3. 理解 FuseSoC `.core` 文件（CAPI=2）的作用，知道它如何让整本书被当成一个 HDL 包导入到别的工程。
4. 按照 `CONTRIBUTING.md` 的四步流程，独立地向本书贡献一个新模块。

本讲不讲电路原理，只讲**工具链与协作流程**——它是你从「读者」走向「贡献者」的桥梁。

## 2. 前置知识

本讲假设你已经建立以下认知（来自前置讲义）：

- **u1-l2 仓库结构与开箱即用约束**：所有文件平铺在单一根目录；「一模块一文件、文件名=模块名」是承重墙；所有 `parameter` 默认值为 `0` 或空串，模块按定义不可直接综合，必须实例化设参数。
- **u2-l1 受限的 Verilog-2001 与 default_nettype**：每个 `.v` 文件开头必须有 `` `default_nettype none ``；逻辑值只用 0/1；所有寄存器都要初始化以避免 X 传播。

如果你对这两点还有疑问，请先回到对应讲义。本讲反复依赖它们。

另外，本讲会用到一点 Python 和 shell 的阅读能力，但**不要求你会写 Python**——我们只读这些脚本，理解它们在做什么。两个名词先说清楚：

- **Markdown**：一种用纯文本写排版的轻量标记语言（`#` 是标题、`-` 是列表等）。本书的网页正文就是从注释里的 Markdown 转出来的。
- **lint（静态检查）**：不运行代码、只读源码就能发现潜在错误的一类工具。

## 3. 本讲源码地图

本讲涉及的关键文件如下，全部位于仓库根目录：

| 文件 | 类型 | 作用 |
|------|------|------|
| `v2h.py` | Python 脚本 | 把带注释的 Verilog 渲染成 HTML 网页（注释即文档） |
| `generate_file_skeleton.py` | Python 脚本 | 输出一个符合本书风格的新模块骨架 |
| `generate_instance.py` | Python 脚本 | 由模块定义生成「填空式」实例化代码 |
| `verilinter` | shell 脚本 | 用 Verilator + Icarus + 额外 grep 检查 Verilog |
| `generate_fusesoc_core_file` | shell 脚本 | 重新生成 FuseSoC `.core` 包描述文件 |
| `FPGA_Design_Elements.core` | CAPI=2 数据 | FuseSoC 包描述：列出全部 RTL 与头文件 |
| `CONTRIBUTING.md` | 文档 | 贡献新模块的四步流程 |
| `index.html` | 手写 HTML | 全书目录页，含 Tools 一节介绍这些工具 |

这些文件共同回答一个问题：**一个新模块从「想法」到「出现在网页和包里」要经过哪些工序？** 答案就是本讲的三条主线。

## 4. 核心概念与源码讲解

### 4.1 v2h：注释即文档的渲染引擎

#### 4.1.1 概念说明

几乎所有项目的文档都会遇到同一个病：**文档和代码各写一份，慢慢就脱节了**。代码改了，文档没改；读者照着过期的文档踩坑。

`v2h.py`（读作 "v to h"，Verilog-to-HTML）用一个非常聪明的办法根治这个病：**它把文档和代码合并在同一个 `.v` 文件里**——文档就写在 Verilog 的行注释 `//` 里。规则只有两条：

1. **以 `//` 开头的行（或被空行隔开的连续注释块）当作 Markdown 正文**，渲染成网页段落。
2. **其余非空、非注释的行当作 Verilog 代码**，包进 `<pre>` 块原样显示。

这样代码一改，注释往往就跟着改（就在隔壁几行），再跑一次 `v2h.py` 网页就同步了。本书整本在线书的网页，就是这样由源码生成的——这就是 u1-l1 讲过的「注释即文档、永不脱节」的实现。

它的关键依赖正是 u1-l2 强调的**「一模块一文件、文件名=模块名」约定**：脚本据此把代码里出现的模块实例名自动变成跨页超链接。它本身**不是解析器**，只是按行做简单的状态切换，因此极其简短（不到 200 行）。

#### 4.1.2 核心流程

`v2h.py` 对每个输入文件的处理流程可以画成：

```text
打开 .v 文件
   │
   ├─ process_first_paragraph：跳过 //# 标题与空行，
   │   抓第一段注释 → 用作网页 <meta description> 与标题
   │   （然后 seek 回文件头，重新开始）
   │
   ▼
主循环：逐行判断当前行的「种类」并分派
   ├─ 是注释行？  → process_comments：攒一段注释 → Markdown → XHTML 段落
   └─ 是代码行？  → process_code：攒一段代码 → 每行 add_file_links → 包进 <pre>
   │
   ▼
拼装 header（含 Source/License/Index 三链接）+ 正文 + footer
   │
   ▼
与已存在的 .html 比较：内容不同才覆盖写盘（否则跳过，省一次 git 改动）
```

两个细节值得记住：第一，判断「注释块何时结束 / 代码块何时结束」不用向前看（look-ahead），而是**靠函数返回「最后读到的哪一行」**，主循环据此继续分派——这是一种很干净的逐行状态机写法。第二，**只有生成的 HTML 与磁盘上旧文件不同时才写盘**，这能避免每次都产生无意义的 git diff。

#### 4.1.3 源码精读

脚本开头的 docstring 已经把整套思想讲清楚了，是本书最值得先读的一段注释：[v2h.py:3-21](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L3-L21) 说明：行注释当作 Markdown、其余当代码包进 `<pre>`、模块名按约定转链接、从而「代码/文档/展示三者同步」。

**网页头部模板**注入了每页顶部固定的三个链接：[v2h.py:29-44](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L29-L44)，其中 [v2h.py:40-42](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L40-L42) 分别是 Source（指向 `.v` 源码）、License（指向 `legal.html`）、Index（指向 `index.html`）。这正是 u1-l1 讲过的「每页顶部三链接」的来源——它们是脚本生成时注入的，不是手写的。

**行的种类判断函数**集中体现了「不是解析器」：

```python
def is_comment(line):
    return line.startswith("//")

def is_header(line):
    return line.startswith("//#")
```

见 [v2h.py:70-74](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L70-L74)。`//#`（行注释紧跟 `#`）被当作 Markdown 标题，单独处理：`process_first_paragraph` 会跳过所有 `//#` 与空行，抓到第一段普通注释当作网页的 meta 描述（[v2h.py:84-95](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L84-L95)）。

**注释块 → Markdown → XHTML** 的转换只用了两行核心：[v2h.py:97-106](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L97-L106)。它把连续的注释行（先 `lstrip("/")` 去掉行首斜杠）攒成一段，然后调 Python 的 `markdown` 库转成 HTML。这也是为什么本书要求**只用行注释、不用块注释 `/* */`**——块注释会被这段逻辑漏掉。

**最巧妙的是自动超链接**：[v2h.py:108-122](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L108-L122)。它逐词扫一行代码，把每个词还原成文件名（去掉引号、去掉 `.vh`），如果 `./这个词.html` 真实存在，就把这个词替换成指向它的链接。这就是为什么你在网页代码里看到 `Register`、`Counter_Binary` 这些实例名都是蓝字可点的——**只要目标模块已经渲染出 `.html`，引用它的地方就自动获得链接**。这把 u1-l2 的命名约定从「规矩」变成了「福利」。

**代码块的包装**在 [v2h.py:124-139](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L124-L139)：攒代码行直到遇到注释行，去掉末尾多余的空行，逐行套上 `add_file_links`，再整体包进 `<pre>`。

**主循环与「不同才写」**：[v2h.py:164-179](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L164-L179)。第 175 行 `if processed_contents != existing_file_contents:` 是关键——只有新内容与旧 `.html` 不一致才写盘并打印 `Updating ...`，否则静默跳过。

#### 4.1.4 代码实践

**目标**：亲手用 `v2h.py` 把一个已有的 Verilog 文件渲染成网页，观察「注释→段落、代码→`<pre>`、实例名→链接」三件事。

**操作步骤**：

1. `v2h.py` 依赖 Python 的 `markdown` 库（脚本第 23 行 `import markdown`）。先确认它已安装（待本地验证）：

   ```bash
   python3 -c "import markdown; print(markdown.__version__)"
   ```

   若报错，安装它：`pip install markdown`。

2. 选一个已有模块（比如 `Register.v`）单独渲染：

   ```bash
   python3 v2h.py Register.v
   ```

3. 用浏览器打开生成的 `Register.html`（若已存在则会被比较后按需更新）。

**需要观察的现象**：

- 终端应打印 `Updating Register.html`（若旧文件已存在且与新生成完全一致，则不打印、不改动）。
- 网页顶部应有 **Source / License / Index** 三个链接。
- 源码里以 `//` 开头的注释（如 Register.v 的 `Power-on-Reset` 一节）变成了排版好的段落；`//#` 开头的行变成了小节标题。
- 代码块里的实例名若对应到已存在的 `.html`，会变成可点的蓝字链接。

**预期结果**：渲染产物与本书在线站点上 `Register` 页面一致。若 `markdown` 库版本差异导致样式略有不同，属正常现象（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么本书规定「注释只用行注释 `//`，不用块注释 `/* */`」？

> **参考答案**：`v2h.py` 的 `is_comment` 只认以 `//` 开头的行（[v2h.py:70-71](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L70-L71)），`process_comments` 也只攒 `//` 注释行。块注释既不会被当 Markdown 正文，又会被当成普通代码塞进 `<pre>`，于是文档就丢失了。

**练习 2**：假设你新建了一个模块 `Foo_Bar.v`，但在网页代码里引用它时 `Foo_Bar` 没有变成链接，最可能的原因是什么？

> **参考答案**：`add_file_links` 只在 `./Foo_Bar.html` 真实存在时才加链接（[v2h.py:120](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/v2h.py#L120)）。多半是你还没对 `Foo_Bar.v` 跑过 `v2h.py`，或文件名/模块名与命名约定不符。

---

### 4.2 实例/骨架生成与 lint：把样板劳动自动化

#### 4.2.1 概念说明

本书主张「极度模块化」（见 u4-l1），后果是：**写新代码时，很大一部分时间花在「写模块实例」这种重复劳动上**——把模块定义的端口，原样誊抄成 `.port(connection)` 的命名连接形式。这是纯体力活，又容易抄错。

作者写了三个小工具来消灭这类样板：

1. **`generate_file_skeleton.py`**：吐出一个符合本书风格（含 `` `default_nettype none ``、参数默认 0、`initial` 初始化）的空模块骨架，省去每次手敲前奏。
2. **`generate_instance.py`**：吃一个模块定义，吐出「填空式」实例化代码，自动完成 99% 的誊抄。
3. **`verilinter`**：在综合之前，先用 Verilator 和 Icarus Verilog 两套 linter 加几条自定义 grep，把常见 Verilog 错误（未初始化的 reg、缺少 `default_nettype none` 等）提前揪出来。

三者合起来，就是把「u2-l1 / u1-l2 的那些硬规矩」用工具强制执行——规矩写在文档里是劝告，写在脚本里是保证。

#### 4.2.2 核心流程

**骨架生成**最简单：`generate_file_skeleton.py` 内部就一个大字符串模板，`print()` 出来即可。它把本书所有「开篇规矩」一次性固化。

**实例生成**是一个**只向前、不回头的逐行扫描器**，结构上和 `v2h.py` 如出一辙：

```text
跳过开头注释/空行
   │
读 "module NAME" 行 → 取最后一个词当模块名
   │
若下一行以 "#(" 开头 → 进入参数块：
   逐行找 "parameter"，按 "=" 切出参数名 → 输出 .PARAM  () 形式
   直到遇到以 ")" 开头的行
   │
输出 instance_name 占位与 "(" → 进入端口块：
   逐行找 input/output/inout，取最后一个词当端口名 → 输出 .PORT  () 形式
   直到遇到以 ");" 开头的行
   │
输出 ");" 结束。文件的其余部分从不读取。
```

它**只读模块定义头部**，读到 `);` 就停。

**lint** 则是 shell 脚本对每个文件依次跑：Verilator 一遍、Icarus 一遍、再用三条 `grep` 做本书关心的额外检查。

#### 4.2.3 源码精读

**骨架模板**值得完整看一眼，因为它就是「本书模块长什么样」的浓缩：[generate_file_skeleton.py:6-37](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_file_skeleton.py#L6-L37)。其中三个关键点直接对应前置讲的规矩：

- [generate_file_skeleton.py:10](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_file_skeleton.py#L10)：`` `default_nettype none ``（u2-l1）。
- [generate_file_skeleton.py:14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_file_skeleton.py#L14)：`parameter WORD_WIDTH = 0`——参数默认 0（u1-l2），保证忘设参数会吵闹失败。
- [generate_file_skeleton.py:24-28](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_file_skeleton.py#L24-L28)：用 `{WORD_WIDTH{1'b0}}` 复制定宽常量（u2-l2），并在 `initial` 里给 `reg` 输出端口赋初值（u2-l1）。注意标题 `//# Title` 没有前导空格——这是 v2h 对 `//#` 的要求。

**实例生成器**的 docstring 开门见山警告「This is a hack, not a real parser!」，并说明它**只认本书这种固定排版**（`#(`、`)` 必须独占一行）：[generate_instance.py:3-21](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L3-L21)。这正是它能这么短的原因——它赌的就是本书风格一致。

判断行的种类同样用一组小函数：[generate_instance.py:42-50](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L42-L50)，其中 `is_port` 认 `input/output/inout` 开头。

**参数块处理**：[generate_instance.py:90-127](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L90-L127)。它按 `=` 切出参数名，输出 `.PARAM  ()`（带逗号与否取决于原行末尾有没有逗号），还能保留行尾 `//` 注释当备忘。

**实例名占位**：[generate_instance.py:130](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L130) 输出 `instance_name` 占位，等你填真实实例名。

**端口块处理**：[generate_instance.py:134-165](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L134-L165)，逻辑与参数块对称，取端口名输出 `.PORT  ()`。最终用 `expandtabs(4)` 把制表符展开成 4 空格对齐（`tabstop = 4`，见 [generate_instance.py:27](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L27)）。

**lint 工具**对每个文件依次跑三关。前两关是两套业界 linter：

- [verilinter:11](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilinter#L11)：用 `verilator -Wall +1364-2001ext+v -y . --lint-only` 按 Verilog-2001 检查，`-y .` 让它在当前目录找子模块定义。
- [verilinter:16](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilinter#L16)：再用 `iverilog -Wall ... -g2001 -y .` 跑一遍，两套工具能抓到不同的错。

第三关是三条**针对本书规矩的自定义 grep**：[verilinter:23-25](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilinter#L23-L25)，分别抓「声明了却没初始化的 reg（会引入 X）」「给 wire 赋初值（会制造多驱动）」「缺少 `` `default_nettype none ``」。这三条正是 u2-l1 的核心纪律，现在由工具兜底。

#### 4.2.4 代码实践

**目标**：用 `generate_instance.py` 由 `Register.v` 自动生成实例化代码，体会「誊抄自动化」。

**操作步骤**：

1. 在仓库根目录运行（注意要重定向到编辑器或文件）：

   ```bash
   ./generate_instance.py Register.v
   ```

2. 观察输出。它应类似（缩进为 4 空格）：

   ```verilog
   Register
       #(
       .WORD_WIDTH  (),
       .RESET_VALUE ()
   )
       instance_name
   (
       .clock        (),
       .clock_enable (),
       .clear        (),
       .data_in      (),
       .data_out     ()
   );
   ```

3. 把输出粘进一个上层模块，把 `instance_name` 和各 `()` 填上你的连线，即得到一个合法实例。

**需要观察的现象**：参数块与端口块都被原样誊成 `.名()` 命名连接；行内注释被保留；缩进自动对齐。

**预期结果**：输出与上面结构一致（具体空格数以本地实际输出为准，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`generate_instance.py` 为什么要求 `#(` 和 `)` 各自独占一行？如果把 `module Foo #(parameter A = 0) (...)` 写成一行会怎样？

> **参考答案**：脚本靠 `line.strip().startswith("#(")` 和 `startswith(")")` 来识别参数块的起止（[generate_instance.py:90](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L90)、[:97](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_instance.py#L97)）。写成一行会让这些边界判断失灵，参数被漏掉——所以它「只认本书的固定排版」。

**练习 2**：`verilinter` 为什么要同时跑 Verilator 和 Icarus Verilog 两套 linter？

> **参考答案**：注释明说「they catch different things」（[verilinter](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/verilinter) 文件头注释）。两套工具的检查侧重不同，互为补充，能比单用一套抓到更多角落问题。

**练习 3**：`generate_file_skeleton.py` 的模板里，为什么 `WORD_WIDTH` 默认是 `0` 而不是某个「合理」的默认值比如 `32`？

> **参考答案**：这是 u1-l2 讲过的有意设计——默认 0 会让 `[WORD_WIDTH-1:0]` 退化成非法的 `[-1:0]`，综合/lint 吵闹失败，杜绝使用者忘记设参数而静默用错位宽（[generate_file_skeleton.py:14](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_file_skeleton.py#L14)）。

---

### 4.3 FuseSoC 集成与贡献流程

#### 4.3.1 概念说明

前两节解决了「单模块怎么写、怎么渲染」。本节解决两件更「外向」的事：

**FuseSoC 集成。** FuseSoC 是 HDL 圈的「包管理器 + 构建工具」（类似 npm/cargo 之于软件）。它用一个 `.core` 文件（CAPI=2 格式）描述「这个包叫什么、包含哪些源文件、怎么构建」。本书提供了 `FPGA_Design_Elements.core`，于是**别人在自己的 FuseSoC 工程里就能一行引用整本书的全部模块**，不用手动一个个拷 `.v`。这正是 README 里「All files are in one directory, so you can use it as a library」的工程化延伸。

**贡献流程。** `CONTRIBUTING.md` 把「向本书加一个新模块」浓缩成四步，是一条任何人都能照着走的流水线。本讲前面学的所有工具，在这条流水线里各司其职。

#### 4.3.2 核心流程

`.core` 文件的结构很简单：

```text
CAPI=2:                          # 采用 CAPI=2 版本的 core 描述格式
name: ::FPGA_Design_Elements:1.0.0   # 包的全名 :版本
description: ...                 # 一句话描述（libc 类比）
fileset:
    rtl:
        - xxx_function.vh:       # 头文件，标记 is_include_file: true
            is_include_file: true
        - Module_A.v             # 普通源文件
        - Module_B.v
        ...
    file_type: VerilogSource     # 这些文件都按 Verilog 源码对待
targets:
    default:                     # 默认构建目标：包含上面那个 rtl 文件集
        filesets:
            - rtl
```

这个文件**不是手写的**，而是由 `generate_fusesoc_core_file` 脚本扫描目录下所有 `*.vh` 与 `*.v` 自动生成的。所以每当你新增或删除一个模块文件，重跑这个脚本就能刷新包描述——它把「文件清单」和「实际文件」绑定在一起，不会漏。

贡献流程（来自 `CONTRIBUTING.md`）则是：

```text
1. 写/改 .v 文件（先看 verilog.html 规范或仿照已有模块）
2. 用 v2h.py 生成对应的 .html
3. 如有必要，在 index.html 里加一个指向新模块的链接
4. 作为 Pull Request 提交
```

#### 4.3.3 源码精读

**`.core` 文件头部**：[FPGA_Design_Elements.core:1-6](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/FPGA_Design_Elements.core#L1-L6) 声明 `CAPI=2:`、包名 `::FPGA_Design_Elements:1.0.0`，以及那句著名的「hardware analog to the C Standard Library (libc)」描述。

**头文件条目的写法**与普通源文件不同：[FPGA_Design_Elements.core:7-8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/FPGA_Design_Elements.core#L7-L8) 给 `*.vh` 标了 `is_include_file: true`，告诉 FuseSoC 这是被 `` `include `` 的头文件、不要单独当编译单元。

**文件集类型与默认目标**：[FPGA_Design_Elements.core:129-134](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/FPGA_Design_Elements.core#L129-L134) 把整组文件标记为 `VerilogSource`，并定义一个名为 `default` 的目标，它包含 `rtl` 文件集。

**生成脚本**正是用 `find` 扫描目录重建上述清单：[generate_fusesoc_core_file:13-29](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_fusesoc_core_file#L13-L29)，`output_include_files` 找所有 `*.vh` 并标 `is_include_file: true`，`output_source_files` 找所有 `*.v`，二者 `sort` 后拼进模板。所以新增一个 `.v` 后，重跑此脚本，它会自动出现在 `.core` 里。

**贡献流程**全文很短：[CONTRIBUTING.md:1-8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CONTRIBUTING.md#L1-L8) 就是上面那四步。注意第 3 步「在 index.html 加链接」是**手工**的——因为 `index.html` 是手写的目录页（u1-l1 讲过），`v2h.py` 不会自动改它。

**工具总览**则写在了 `index.html` 的 Tools 一节：[index.html:43-46](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L43-L46) 是该节标题，[index.html:47-51](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/index.html#L47-L51) 是对 `v2h.py` 的官方介绍（强调「no block comments」）。这一节是这些工具的「用户手册」。

#### 4.3.4 代码实践

**目标**：把 `CONTRIBUTING.md` 的四步流程在本地走一遍（不真的发 PR），体会工具如何串成流水线。详细的端到端版本见第 5 节综合实践；这里先做最小一步：刷新 `.core`。

**操作步骤**：

1. 在仓库根目录运行生成脚本（它会覆盖 `FPGA_Design_Elements.core`）：

   ```bash
   ./generate_fusesoc_core_file
   ```

2. 用 `git diff FPGA_Design_Elements.core` 看变化（如果文件清单没变，理论上应无 diff 或仅排版差异）。

**需要观察的现象**：脚本根据当前目录的 `*.vh` / `*.v` 重新生成清单；若你之前没增删文件，结果应与现有 `.core` 一致。

**预期结果**：无 diff，或仅时间无关的等价差异。该脚本要求在仓库根目录运行（它写死 `COREFILE="./FPGA_Design_Elements.core"`，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `*.vh` 头文件在 `.core` 里要标 `is_include_file: true`，而 `*.v` 不用？

> **参考答案**：`*.vh` 是被 `` `include `` 进 `.v` 的头文件（如 `clog2_function.vh`），不是独立编译单元。标记 `is_include_file: true`（[FPGA_Design_Elements.core:7-8](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/FPGA_Design_Elements.core#L7-L8)）告诉 FuseSoC 不要把它单独送给综合器，否则会因没有 module 顶层而报错。

**练习 2**：你新增了一个模块 `My_Adder.v` 并跑了 `v2h.py`，网页上也有了它的页面，但目录页 `index.html` 里找不到它。为什么？

> **参考答案**：`CONTRIBUTING.md` 第 3 步明确「Add a link in index.html if necessary」是**手工**的（[CONTRIBUTING.md:6-7](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/CONTRIBUTING.md#L6-L7)）。`index.html` 是手写目录，`v2h.py` 只生成模块页、不改目录页，所以你需要自己去 `index.html` 的相应分类下加一行 `<li><a href="./My_Adder.html">My Adder</a></li>`。

**练习 3**：`generate_fusesoc_core_file` 和 `v2h.py` 在「清单/内容是否会过期」这个问题上，分别采用了什么策略？

> **参考答案**：`generate_fusesoc_core_file` 是**自动重生**——`find` 扫描目录，文件增删后重跑即同步（[generate_fusesoc_core_file:13-29](https://github.com/laforest/FPGADesignElements/blob/2450a548eeee2a4dc1c06878b7c00617dd94e2a8/generate_fusesoc_core_file#L13-L29)）。`v2h.py` 则是**注释即文档**——文档和代码同处一个文件，改码时顺手改注释，重跑即同步。两者哲学不同（一个扫文件系统、一个扫文件内容），但都把「保持同步」从人的自觉变成了脚本的动作。

## 5. 综合实践

本实践把本讲全部内容串成一条线：**亲手造一个新模块，让它走完「骨架→代码→lint→网页→目录→包」的完整流水线**。这是 `CONTRIBUTING.md` 四步流程的扩展版。

**实践目标**：新建一个最简模块 `My_Identity.v`（输入直通输出，作为练手），让它最终同时出现在网页、目录页和 `.core` 里。

**操作步骤**：

1. **生成骨架**（u4.2 工具）：

   ```bash
   ./generate_file_skeleton.py > My_Identity.v
   ```

   打开 `My_Identity.v`，把 `NAME` 改成 `My_Identity`，把示例端口改成你需要的（例如保留 `clock`、`clear` 和一个 `data_in`/`data_out`）。确认它带有 `` `default_nettype none ``、`WORD_WIDTH = 0`、`initial` 初始化。

2. **lint 自检**（u4.2 工具，确保不违反 u2-l1 规矩）：

   ```bash
   ./verilinter My_Identity.v
   ```

   需要本机已安装 `verilator` 与 `iverilog`。修掉所有报错（待本地验证）。

3. **生成网页**（u4.1 工具，CONTRIBUTING 第 2 步）：

   ```bash
   python3 v2h.py My_Identity.v
   ```

   应生成 `My_Identity.html`，终端打印 `Updating My_Identity.html`。

4. **在目录页加链接**（CONTRIBUTING 第 3 步，手工）：编辑 `index.html`，在合适的分类（如 Boolean Logic 或新建一个小节）下加：

   ```html
   <li> <a href="./My_Identity.html">My Identity</a>
   ```

5. **刷新 FuseSoC 包描述**（u4.3 工具）：

   ```bash
   ./generate_fusesoc_core_file
   git diff FPGA_Design_Elements.core
   ```

   `My_Identity.v` 应出现在文件清单里（按字母序）。

6. **（可选）生成实例化代码**试用于上层：`./generate_instance.py My_Identity.v`。

**需要观察的现象 / 预期结果**：

- `My_Identity.html` 顶部有 Source/License/Index 三链接；注释段变成正文，代码进 `<pre>`。
- `index.html` 里点新加的链接能跳到该页。
- `FPGA_Design_Elements.core` 的 `rtl` 文件集里多了 `My_Identity.v`。
- 若你删掉 `My_Identity.v`，再跑 `generate_fusesoc_core_file`，它会从 `.core` 消失——验证了「清单自动同步」。

> 说明：本实践不要求你真去 GitHub 发 Pull Request；若要按 `CONTRIBUTING.md` 第 4 步贡献，需 fork 仓库、提交、发 PR。运行 `verilinter` 与 `v2h.py` 的具体输出以本地环境为准，相关步骤标注为「待本地验证」。

## 6. 本讲小结

- `v2h.py` 用「行注释当 Markdown、其余当代码」把文档与代码合二为一，赖以成立的是「一模块一文件、文件名=模块名」约定；它还会把代码里出现的、已有 `.html` 的模块名自动变成跨页链接。
- `generate_file_skeleton.py` 固化了本书模块的开篇规矩（`` `default_nettype none ``、参数默认 0、`initial` 初始化），是「规矩即模板」的体现。
- `generate_instance.py` 是一个**只向前扫描**的「填空式」实例生成器，能省掉高度模块化设计中绝大部分誊抄劳动，但它只认本书的固定排版。
- `verilinter` 用 Verilator + Icarus 双 linter 再加三条 grep，把 u2-l1 的核心纪律（reg 必须初始化、wire 不可赋初值、必须有 `default_nettype none`）变成可执行的检查。
- FuseSoC `.core`（CAPI=2）让整本书可作为一个 HDL 包被引用；它由 `generate_fusesoc_core_file` 扫描目录自动重生，文件增删自动同步。
- `CONTRIBUTING.md` 的四步流程（写 `.v` → 跑 `v2h.py` → 必要时改 `index.html` → 发 PR）把上述工具串成一条贡献流水线；注意 `index.html` 的目录链接是手工维护的。

## 7. 下一步学习建议

- **本单元下一篇 u18-l2（仿真、测试台与综合验证）**：本讲解决了「模块怎么写出来、怎么进网页和包」，下一篇解决「模块怎么验证它是对的」——将讲解 `Simulation_Clock` 的无竞争仿真时钟惯用法、`Synthesis_Harness_Input/Output` 综合测试桩，以及用 cocotb（SystemVerilog）在 `tests/` 目录下写自检测试台。建议接着读。
- **回看工具源码**：把本讲的四个脚本（`v2h.py`、两个 `generate_*.py`、`verilinter`）当作「逐行状态机」的阅读练习，它们都不长，是体会「小工具解决重复劳动」的好范本。
- **动手贡献**：找一个 `index.html` 里**尚未实现**（没有超链接）的规划项，按本讲第 5 节的流水线试着实现它——这是把全书知识用起来的最佳方式。
