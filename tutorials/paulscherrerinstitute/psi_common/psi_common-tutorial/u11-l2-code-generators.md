# Python 代码生成器 generators

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 psi_common 为什么需要「代码生成器」这一层，它解决了什么手工写 VHDL 解决不了的问题。
- 理解 `generators/` 目录里 **Python 脚本 + snippet 模板** 的三件套结构，并能讲出 `argparse` → 读模板 → `re.sub` 替换占位符 → 写文件的整体流水线。
- 区分库里的两族生成器：**多端口合并族**（`simple_cc_X` / `status_cc_X`，把若干不等宽信号拼成一条扁平总线）与 **定宽数组族**（`par_tdm_wX` / `tdm_par_wX`，为 TDM 转换器生成定宽的数组类型端口）。
- 会读懂并改写 `.bat` 调用示例，能为一个指定位宽/端口集合生成实例。
- 知道当前仓库里这些 snippet 模板的历史局限：它们仍是 v3.0.0 大重构**之前**的命名风格，直接生成的文件与现行组件端口对不上，需要配合命名刷新或 u11-l3 的迁移脚本使用。

## 2. 前置知识

本讲是「工程化」单元的一篇，依赖你已经读过：

- **u5-l2**（`simple_cc` / `status_cc` / `bit_cc`）：理解 `simple_cc` 只能跨一条扁平 `std_logic_vector` 总线、不带 ready 的 valid-only 握手；这正是「多端口合并族」生成器要补偿的短板——它把多个用户信号拼成一条总线喂给 `simple_cc`。
- **u8-l2**（`par_tdm` / `tdm_par`）：理解这两个 TDM 转换器的并行侧端口是一条 `ch_nb_g * ch_width_g` 位的**扁平大向量**，而不是「每通道一根」的数组；这正是「定宽数组族」生成器要补偿的短板——它生成一个带数组端口的薄外壳。

此外需要一点通用编程基础：

- **模板替换（template substitution）**：一段带占位符（如 `<WIDTH>`）的文本骨架，由程序读入、把占位符换成具体值后输出成品。这是本讲的核心机制。
- **正则替换 `re.sub`**：Python 标准库 `re` 的字符串替换函数，生成器用它做占位符替换。
- **命令行参数 `argparse`**：Python 标准库的命令行解析器，让脚本接受 `-width`、`-ports` 等开关。

为什么 VHDL 库要用 Python 生成代码？因为 VHDL-93/2002 在「端口声明区」不能写 `if`、也不能在 entity 端口表里直接展开「N 个不等宽信号」或「元素为定宽 slv 的数组」。要在编译期生成这类**形状可变**的端口，最干净的办法就是在 VHDL 之外用脚本预先把骨架写好。psi_common 选择「snippet 模板 + Python 脚本」这一最轻量、无第三方依赖的方案，而不是更重的代码生成框架。

## 3. 本讲源码地图

本讲只涉及 `generators/` 这一个目录，分三层：

| 路径 | 作用 |
| --- | --- |
| `generators/psi_common_simple_cc_X.py` | 多端口合并族生成器脚本：为 `simple_cc` 生成「多端口 → 单总线」外壳。 |
| `generators/psi_common_status_cc_X.py` | 同族，目标组件换成 `status_cc`，逻辑与 simple_cc 版几乎逐行相同。 |
| `generators/psi_common_par_tdm_wX.py` | 定宽数组族生成器脚本：为 `par_tdm` 生成带数组类型端口的定宽外壳。 |
| `generators/psi_common_tdm_par_wX.py` | 同族，目标组件换成 `tdm_par`，逻辑与 par_tdm 版几乎逐行相同。 |
| `generators/snippets/psi_common_simple_cc_X.vhd` | simple_cc 外壳的 VHDL 模板，含 `<WIDTH>`/`<POSTFIX>`/`<DATA_*>` 等占位符。 |
| `generators/snippets/psi_common_status_cc_X.vhd` | status_cc 外壳模板。 |
| `generators/snippets/psi_common_par_tdm_wX.vhd` | par_tdm 外壳模板（含一个 package 声明数组类型）。 |
| `generators/snippets/psi_common_tdm_par_wX.vhd` | tdm_par 外壳模板。 |
| `generators/examples/psi_common_simple_cc.bat` | Windows 批处理调用示例：演示如何给脚本传参。 |
| `generators/examples/*.bat` | 其余三个组件的同款示例。 |
| `generators/examples/.gitignore` | 把生成的 `*.vhd` 产物排除出版本管理——生成物不入库。 |

一句话概括目录设计：**模板（snippets/）描述「形状」、脚本（.py）描述「参数→替换」、示例（examples/）描述「怎么调用」**，三者解耦。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：4.1 生成器整体流程、4.2 snippet 模板、4.3 Python 脚本、4.4 bat 示例与产物管理。4.3 里会专门点出一个必须知道的工程现实：**当前 snippet 模板相对 v3.0.0 重构已过期**。

### 4.1 生成器整体流程与动机

#### 4.1.1 概念说明：为什么需要代码生成器

psi_common 的核心组件（如 `simple_cc`、`par_tdm`）为了「全 generic 化、厂商无关、可综合」，端口都做成最朴素的形式：

- `simple_cc` 的数据端口是**一条** `std_logic_vector(width_g-1 downto 0)`——它只认识「一整条总线 + 一个 valid」。
- `par_tdm` / `tdm_par` 的并行侧是**一条扁平的** `ch_nb_g * ch_width_g` 位大向量，而不是「每个通道一根独立信号」。

但真实工程里，用户想跨时钟域的往往是**一组形状各异的信号**（一个 3 位状态、一个 1 位使能、一个 12 位计数器……），或者想用**「每通道一根数组」**的直观接口而不是手动拼接扁平向量。VHDL 在 entity 端口表里无法用循环展开「N 个不等宽端口」，于是 psi_common 在 VHDL 之外加了一层 Python 生成器：让用户声明「我要这几个端口、这几个宽度」，脚本据此吐出一个**薄外壳 entity**——外壳负责把多信号拼/拆成单总线，再例化底层核心组件。

#### 4.1.2 核心流程

所有四个生成器都走同一条流水线，只是参数和替换的占位符不同：

```text
命令行 (-width / -ports / -postfix / -dir)
        │
        ▼
[1] argparse 解析参数
        │
        ▼
[2] 打开 snippets/<组件>_X.vhd，读入模板原文
        │
        ▼
[3] 用 re.sub 把模板里的 <占位符> 依次替换成具体值
        │   （定宽族只换 <WIDTH>；
        │    合并族还要按端口列表动态生成 <DATA_IN/OUT/MERGE/UNMERGE> 四段）
        │
        ▼
[4] 把成品写到 <dir>/<组件>_<postfix 或 width>.vhd
```

关键点：脚本本身**不「理解」VHDL**，它只是字符串替换。所有硬件语义都在 snippet 模板里写死，脚本只负责「把形状填进去」。这也意味着：模板里的命名风格一旦和现行组件对不上，生成物就会编译失败（见 4.3.5）。

#### 4.1.3 源码精读：脚本定位模板目录

四个脚本都用同一个技巧来稳健地找到模板文件——用本脚本所在目录拼出 snippets 路径，与「在哪儿被调用」无关：

[generators/psi_common_simple_cc_X.py:L1-L5](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py#L1-L5) 导入 `re/argparse/os` 并用 `os.path.abspath(__file__)` 取脚本自身绝对路径，`dirname` 得到 `generators/` 目录存入 `FILE_DIR`。这样无论你从 `examples/` 还是仓库根调用脚本，它都能正确找到 `generators/snippets/` 下的模板。

[generators/psi_common_tdm_par_wX.py:L5-L10](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_tdm_par_wX.py#L5-L10) 定宽族脚本用了完全相同的 `FILE_DIR` 定义和 `argparse` 开场，体现两族脚本是同一套骨架的复制。

#### 4.1.4 代码实践：用目录树看清三件套

1. **实践目标**：建立「模板 / 脚本 / 示例」三层对应的直觉。
2. **操作步骤**：在仓库根执行 `ls generators generators/snippets generators/examples`，对照本讲「3. 本讲源码地图」的表格。
3. **需要观察的现象**：四个核心组件各有一个 `.py`、一个 `snippets/*.vhd`、一个 `examples/*.bat`，一一对应；`examples/` 里还有 `.gitignore`。
4. **预期结果**：你会确认「每个被生成包装的组件 = 一个脚本 + 一个模板 + 一个调用示例」的整齐对应关系。
5. 运行结果待本地验证（取决于你的 shell 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么生成器要把模板和脚本分成两个文件，而不是在 Python 里用大字符串拼 VHDL？
**答案**：模板是「形状」，脚本是「参数→替换」。分开后，改端口布局只动模板（仍是合法 VHDL，IDE 能高亮、可读性好），改替换逻辑只动脚本；模板也能被非程序员直接审阅。

**练习 2**：四个脚本里 `FILE_DIR` 解决了什么问题？
**答案**：让脚本无论从哪个工作目录被调用，都能用绝对路径找到 `snippets/` 下的模板，避免相对路径错位。

### 4.2 snippet 模板：带占位符的 VHDL 骨架

#### 4.2.1 概念说明

snippet 是一个**合法 VHDL 文件**，但里面散布着用尖括号标记的占位符（`<WIDTH>`、`<POSTFIX>`、`<DATA_IN>` 等）。它本身就是外壳的成品骨架：entity 名、信号宽度、端口表、例化语句都已写好，只差把占位符换成具体值。占位符分两类：

- **标量占位符**：如 `<WIDTH>`、`<POSTFIX>`，一个占位符换成一个常量字符串。
- **块占位符**：如 `<DATA_IN>`、`<DATA_MERGE>`，一个占位符换成「多行文本」（由脚本按端口数量循环拼出）。

#### 4.2.2 核心流程

模板在被替换后，必须形成一条完整的「拼总线 → 例化核心 → 拆总线」数据通路：

```text
用户多端口 ──<DATA_MERGE>──> MergedA(扁平总线) ──> simple_cc ──> MergedB(扁平总线) ──<DATA_UNMERGE>──> 用户多端口
```

合并族模板（simple_cc/status_cc）里这四段占位符的位置是固定的：`<DATA_IN>` 在端口表里声明用户输入端口、`<DATA_OUT>` 声明输出端口、`<DATA_MERGE>` 在架构体里把输入拼成 `MergedA`、`<DATA_UNMERGE>` 把 `MergedB` 拆回输出。

#### 4.2.3 源码精读：simple_cc 外壳模板

[generators/snippets/psi_common_simple_cc_X.vhd:L14-L21](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_simple_cc_X.vhd#L14-L21) entity 名里的 `<POSTFIX>` 让每次生成得到一个独立名字（如 `psi_common_simple_cc_test`），端口表里 `<DATA_IN>` 处会被替换成多条 `xxxA : in std_logic_vector(...)` 声明。注意端口命名用了 `名字+A`（输入域）/`名字+B`（输出域）的约定。

[generators/snippets/psi_common_simple_cc_X.vhd:L37-L42](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_simple_cc_X.vhd#L37-L42) 架构体声明两条内部总线 `MergedA`/`MergedB`，宽度由 `<WIDTH>` 决定（= 所有端口宽度之和）；`<DATA_MERGE>` 占位符处将填入把各输入端口拼进 `MergedA` 的赋值。

[generators/snippets/psi_common_simple_cc_X.vhd:L44-L61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_simple_cc_X.vhd#L44-L61) 例化底层 `psi_common_simple_cc`，把 `MergedA`/`MergedB` 接到核心组件的 `DataA`/`DataB`，并用 `<WIDTH>` 设其数据宽度；`<DATA_UNMERGE>` 处填入把 `MergedB` 拆回各输出端口的赋值。

> ⚠️ **重要现实（见 4.3.5）**：这段例化用的 generic 名 `DataWidth_g` 与端口名 `ClkA`/`DataA`/`VldA` 是 **v3.0.0 重构之前** 的命名；现行 `psi_common_simple_cc` 的 generic 是 `width_g`、端口是 `a_clk_i`/`a_dat_i`/`a_vld_i`。因此这份模板**按原样生成出来的文件无法直接编译**，需先做命名刷新。

#### 4.2.4 源码精读：tdm_par 外壳模板（定宽数组族）

定宽族的模板更巧妙：它**同时生成一个 package 和一个 entity**，因为「元素为定宽 slv 的数组类型」必须先在 package 里声明，entity 才能在端口里用它。

[generators/snippets/psi_common_tdm_par_wX.vhd:L19-L23](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_tdm_par_wX.vhd#L19-L23) 声明 package `psi_common_tdm_par_w<WIDTH>_pkg`，里面定义数组类型 `..._a is array (natural range <>) of std_logic_vector(<WIDTH>-1 downto 0)`。这正是 u2-l3 讲过的「元素宽度钉死、长度无约束」数组类型，只不过这里用生成器按位宽实例化。

[generators/snippets/psi_common_tdm_par_wX.vhd:L34-L50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_tdm_par_wX.vhd#L34-L50) entity `psi_common_tdm_par_w<WIDTH>` 保留一个 generic `ChannelCount_g`（通道数仍可运行时配，只有位宽被钉死），并行侧端口 `Parallel` 直接用上面那个数组类型，让用户拿到「每通道一根」的直观接口，而不必手动拼扁平向量。

[generators/snippets/psi_common_tdm_par_wX.vhd:L56-L77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_tdm_par_wX.vhd#L56-L77) 架构体用一条扁平 `ParallelMerged` 信号，靠 `g_merge : for i in 0 to ChannelCount_g-1 generate` 循环把数组 `Parallel(i)` 逐通道拼进扁平向量，再例化底层 `psi_common_tdm_par`（其并行侧本就是扁平向量）。这个 `for generate` 正是「数组 ↔ 扁平向量」的桥。

#### 4.2.5 代码实践：手工预演一次替换

1. **实践目标**：理解块占位符如何被多行文本替换。
2. **操作步骤**：假设端口为 `SomePort=3, OtherPort=1, LastPort=12`，在模板 [generators/snippets/psi_common_simple_cc_X.vhd:L21](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_simple_cc_X.vhd#L21) 的 `<DATA_IN>` 位置，手写出你期望替换进去的三行端口声明。
3. **需要观察的现象**：端口名带 `A` 后缀、宽度依次为 2/0/11（即 width-1）；三行顺序按端口名字典序。
4. **预期结果**：与 4.3.3 里脚本循环生成的文本完全一致（脚本用 `sorted(ports.items())` 保证字典序）。

#### 4.2.6 小练习与答案

**练习 1**：tdm_par 模板为什么要同时生成一个 package，而 simple_cc 模板不需要？
**答案**：tdm_par 要在端口里用「元素为定宽 slv 的数组类型」，这种类型必须先在 package 里声明；simple_cc 只用普通 `std_logic_vector`，无需新类型，故不需要 package。

**练习 2**：模板里 `<POSTFIX>` 和 `<WIDTH>` 都是「标量占位符」，它们分别决定生成物的什么？
**答案**：`<POSTFIX>` 决定 entity/package 的名字后缀（让多次生成互不冲突），`<WIDTH>` 决定内部总线的物理位宽。

### 4.3 Python 脚本：argparse + re.sub 的模板替换

#### 4.3.1 概念说明

脚本只做三件事：**解析命令行参数 → 读模板 → 用 `re.sub` 替换占位符 → 写成品**。两族脚本的差异只在「替换哪些占位符」：

- **定宽族**（`par_tdm_wX.py` / `tdm_par_wX.py`）：只换一个 `<WIDTH>`，最简单。
- **合并族**（`simple_cc_X.py` / `status_cc_X.py`）：除 `<WIDTH>`/`<POSTFIX>` 外，还要按用户给的端口列表，循环拼出四段文本（`<DATA_IN/OUT/MERGE/UNMERGE>`）再替换。

#### 4.3.2 核心流程：合并族脚本的端口建模

合并族脚本要回答两个问题：(a) 总线宽度是多少？(b) 每个用户端口在总线里的位段在哪？答案是：

- 总线宽度 \(W = \sum_i w_i\)，即所有端口宽度之和。
- 按端口名字典序排列，第 k 个端口（累计起始下标为 `nextIdx`，宽度 `w`）占据总线位段 \([nextIdx + w - 1,\ nextIdx]\)，即：

\[
\text{bitrange}_k = [\,\text{nextIdx}_k + w_k - 1,\ \text{nextIdx}_k\,], \qquad \text{nextIdx}_{k+1} = \text{nextIdx}_k + w_k
\]

这个「低位居前、顺序累加」的小端布局，与 u8-l2 讲过的 wconv/par_tdm「先到的字放低位」约定一致。

#### 4.3.3 源码精读：simple_cc 脚本全流程

[generators/psi_common_simple_cc_X.py:L7-L11](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py#L7-L11) 用 `argparse` 声明三个开关：`-postfix`（entity 名后缀，必填）、`-dir`（输出目录，默认 `.`）、`-ports`（`nargs="+"` 接收形如 `名字=宽度` 的多个参数，必填）。

[generators/psi_common_simple_cc_X.py:L14-L20](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py#L14-L20) 把每个 `名字=宽度` 字符串切成两半存进字典 `ports`，并算出 `sumWidth = sum(ports.values())`——这就是 `<WIDTH>` 的值（总总线宽）。

[generators/psi_common_simple_cc_X.py:L22-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py#L22-L32) 先 `print` 一张端口表给用户确认（按字典序），再读入模板原文，用 `re.sub` 把 `<WIDTH>` 换成 `sumWidth`、`<POSTFIX>` 换成用户后缀。这两次替换处理两个标量占位符。

[generators/psi_common_simple_cc_X.py:L33-L47](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py#L33-L47) 这是合并族的核心：初始化四个空列表，遍历 `sorted(ports.items())`，用 `nextIdx` 累加下标，**循环生成四段文本**：

- `data_in` / `data_out`：端口声明，名字分别加 `A`/`B` 后缀，宽度为 `w-1`；
- `data_merge`：`MergedA(高位 downto 低位) <= 名字A;`
- `data_unmerge`：`名字B <= MergedB(高位 downto 低位);`

每段用 `"\n".join(...)` 拼成多行字符串，再分别 `re.sub` 替换四个块占位符。

[generators/psi_common_simple_cc_X.py:L49-L50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py#L49-L50) 把成品写到 `<dir>/psi_common_simple_cc_<postfix>.vhd`。

> 旁注：`status_cc_X.py` 与本脚本逐行相同，只是模板文件名、输出文件名里的组件名换成 `status_cc`——可对照 [generators/psi_common_status_cc_X.py:L28-L49](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_status_cc_X.py#L28-L49)。

#### 4.3.4 源码精读：tdm_par 脚本（定宽族极简版）

[generators/psi_common_tdm_par_wX.py:L7-L21](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_tdm_par_wX.py#L7-L21) 定宽族脚本短得多：只需 `-width`（整数）和 `-dir` 两个开关，读模板后**只做一次** `re.sub("<WIDTH>", str(width), ...)`，写到 `<dir>/psi_common_tdm_par_w<width>.vhd`。注意它没有任何端口循环——因为通道数留给 entity 的 generic `ChannelCount_g`，脚本只钉死位宽。`par_tdm_wX.py` 与之逐行对称。

#### 4.3.5 ⚠️ 工程现实：snippet 模板相对 v3.0.0 已过期

这是阅读本目录时**最容易踩坑**的一点，必须单独强调。

psi_common 在 v3.0.0 做过一次「统一代码风格、不向下兼容」的大重构（见 u1-l1、u1-l4）：端口统一成 snake_case 加 `_i`/`_o` 后缀、generic 统一小写化。而 `generators/` 目录自最初提交 `719e12c DEVEL: Implemented generator scripts where reasonable` 之后**再未更新**，snippet 模板仍停留在重构前的 PascalCase 风格。直接对照即可看出不一致：

| 项 | 模板里的写法（旧） | 现行组件的写法（新） |
| --- | --- | --- |
| simple_cc 数据宽度 generic | `DataWidth_g` | `width_g` |
| simple_cc A 域时钟端口 | `ClkA` | `a_clk_i` |
| simple_cc A 域数据端口 | `DataA` | `a_dat_i` |
| simple_cc A 域 valid 端口 | `VldA` | `a_vld_i` |

现行的 `width_g` / `a_clk_i` / `a_dat_i` 见 [hdl/psi_common_simple_cc.vhd:L20-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L20-L32)。

**结论**：模板替换机制本身工作正常（脚本会忠实生成 VHDL 文本），但**按当前 snippet 原样生成的文件无法直接编译**——它的例化语句引用了底层组件已不存在的 generic/端口名。要真正使用，需要二选一：

1. **手工/脚本刷新 snippet 命名**：把模板里的 `DataWidth_g`→`width_g`、`ClkA`→`a_clk_i`、`DataA`→`a_dat_i`、`VldA`→`a_vld_i`、`RstInA`→`a_rst_i`、`RstOutA`→`a_rst_o`（B 域同理）。
2. **套用 u11-l3 的 v2→v3 迁移工具**：`scripts/refactoring/migration_from_v2_to_v3_db.json` 正是记录这类命名映射的规则库，可对生成物批量改名。

定宽族（`par_tdm_wX`/`tdm_par_wX`）同理：模板里例化的是旧命名（`ChannelCount_g`/`ChannelWidth_g`/`Clk`/`Tdm`），而现行 `psi_common_par_tdm` 的 generic 是 `ch_nb_g`/`ch_width_g`、端口是 `clk_i`/`dat_i`（见 [hdl/psi_common_par_tdm.vhd:L24-L40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_par_tdm.vhd#L24-L40)），也需要同样的刷新。学习本讲时，请把「模板替换机制」与「模板内容是否最新」分开看：机制值得学，内容需校对。

#### 4.3.6 代码实践：跑一次 simple_cc 生成器

1. **实践目标**：亲眼看「参数 → 模板替换 → 成品文件」的全过程，并对照 4.3.5 验证命名差异。
2. **操作步骤**：
   ```bash
   cd generators/examples
   python3 ../psi_common_simple_cc_X.py -postfix test \
       -ports SomePort=3 OtherPort=1 LastPort=12
   ```
   （`.bat` 示例用 Windows 的 `py -3`，Linux/macOS 直接用 `python3`。）
3. **需要观察的现象**：
   - 终端先打印一张端口表（按字典序：`LastPort 12 / OtherPort 1 / SomePort 3`）。
   - 当前目录生成 `psi_common_simple_cc_test.vhd`。
   - 打开它，确认 `<WIDTH>` 已变成 16（=3+1+12）、`<POSTFIX>` 已变成 `test`、`<DATA_IN/OUT/MERGE/UNMERGE>` 四处已变成三行文本。
4. **预期结果**：生成物是一份语法完整的 VHDL 外壳，entity 名为 `psi_common_simple_cc_test`，内部声明 16 位 `MergedA`/`MergedB` 并例化 `psi_common_simple_cc`。
5. **额外校对**：把生成物里例化语句的 generic/端口名，与 [hdl/psi_common_simple_cc.vhd:L20-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L20-L32) 对照，亲眼看 4.3.5 指出的命名不匹配。该生成物能否直接编译通过，**待本地验证**（预期：不能，需先按 4.3.5 刷新命名）。

#### 4.3.7 小练习与答案

**练习 1**：合并族脚本用 `re.sub("<WIDTH>", str(sumWidth), content)` 做替换。为什么用 `re.sub` 而不是普通字符串 `str.replace`？
**答案**：功能上两者都能替换纯文本占位符；用 `re.sub` 是项目一致风格，也方便日后扩展成正则匹配。对当前 `<...>` 字面量占位符，二者等价。

**练习 2**：如果两个用户端口同名（如 `-ports Foo=3 Foo=5`），脚本会怎样？
**答案**：端口被解析进字典 `ports`，同名键会被后者覆盖（`Foo=5` 生效，`Foo=3` 丢失），且不报错。这是脚本的一个隐含限制，使用时应避免重名。

**练习 3**：定宽族脚本只换 `<WIDTH>`，通道数由谁决定？
**答案**：通道数不在生成期决定，而是留给生成物 entity 的 generic `ChannelCount_g`（默认 8），由使用者在例化时配置。

### 4.4 bat 示例与生成产物管理

#### 4.4.1 概念说明

`examples/*.bat` 是 Windows 批处理，作用是**记录一条「该怎么调用脚本」的范本命令**——它本身不是必须的（你完全可以在命令行手敲），但作为「可执行文档」让人一眼看懂参数格式。`examples/.gitignore` 则把关「生成物不入库」：生成的 `*.vhd` 是派生产物，应随用随生成，不污染版本库。

#### 4.4.2 核心流程

调用约定的两种形态：

```text
# 合并族：-postfix + -ports（可多个）
py -3 ..\<脚本>_X.py -postfix <名字> -ports <名>=<宽> <名>=<宽> ...

# 定宽族：-width（单个整数）
py -3 ..\<脚本>_wX.py -width <宽度>
```

#### 4.4.3 源码精读

[generators/examples/psi_common_simple_cc.bat:L1](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/examples/psi_common_simple_cc.bat#L1) 合并族调用范本：`py -3 ..\psi_common_simple_cc_X.py -postfix test -ports SomePort=3 OtherPort=1 LastPort=12`。`..\` 说明示例预设从 `examples/` 目录运行，脚本在其上一层。

[generators/examples/psi_common_tdm_par.bat:L1](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/examples/psi_common_tdm_par.bat#L1) 定宽族调用范本：`py -3 ..\psi_common_tdm_par_wX.py -width 15`，只需一个整数宽度。

[generators/examples/.gitignore:L1-L2](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/examples/.gitignore#L1-L2) 用 `*.vhd` 忽略规则把所有生成物排除——这传达了一个设计原则：**生成器脚本和模板入库，生成物不入库**。

#### 4.4.4 代码实践：仿写一个调用

1. **实践目标**：掌握两族调用约定的差异。
2. **操作步骤**：参考 [generators/examples/psi_common_status_cc.bat:L1](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/examples/psi_common_status_cc.bat#L1)，仿写一条调用 `psi_common_tdm_par_wX.py` 生成 16 位实例的命令。
3. **需要观察的现象**：定宽族不需要 `-postfix`、不需要 `-ports`，只要 `-width 16`。
4. **预期结果**：在 `examples/` 下生成 `psi_common_tdm_par_w16.vhd`。
5. 运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `examples/.gitignore` 要忽略 `*.vhd`？
**答案**：生成的 VHDL 是脚本 + 模板的派生产物，会随参数变化；把派生物入库会造成重复与不一致，应只入库「源头」（脚本与模板）。

**练习 2**：`.bat` 示例用 `py -3`，在 Linux 上该怎么改？
**答案**：把 `py -3 ..\<脚本>` 换成 `python3 ../<脚本>`（路径分隔符 `\`→`/`）。

## 5. 综合实践

把本讲四块知识串起来，完成一次「读懂→生成→校对→修正」的完整流程：

1. **读懂**：打开 [generators/psi_common_simple_cc_X.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py) 和 [generators/snippets/psi_common_simple_cc_X.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_simple_cc_X.vhd)，画出「四个块占位符分别由脚本哪几行循环生成」的对照表。
2. **生成**：按 4.3.6 跑脚本，产出 `psi_common_simple_cc_test.vhd`，检查终端打印的端口表与文件内容是否吻合。
3. **校对**：把生成物里对底层组件的例化，与现行 [hdl/psi_common_simple_cc.vhd:L20-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_simple_cc.vhd#L20-L32) 逐项对照，列出所有对不上的 generic/端口名（验证 4.3.5 的结论）。
4. **修正**：用编辑器或 `sed` 把生成物里的旧命名刷新成新命名（`DataWidth_g`→`width_g`、`ClkA`→`a_clk_i`、`DataA`→`a_dat_i`、`VldA`→`a_vld_i`、`RstInA`→`a_rst_i`、`RstOutA`→`a_rst_o`，B 域同理），尝试让它能通过 VHDL 语法检查。
5. **延伸**：思考能否把这个「命名刷新」自动化——这正好引出 u11-l3 的 `scripts/refactoring/migration_from_v2_to_v3_db.json` 迁移规则库。

> 本综合实践的第 4、5 步能否完全跑通（取决于你的仿真器与是否手工修正到位），**待本地验证**。

## 6. 本讲小结

- psi_common 在 `generators/` 用 **Python 脚本 + snippet 模板** 为 `simple_cc`/`status_cc`/`par_tdm`/`tdm_par` 生成「形状可变」的薄外壳，补偿这些核心组件端口过于朴素（单总线 / 扁平向量）的短板。
- 整体流水线统一为：`argparse` 解析参数 → 读模板 → `re.sub` 替换占位符 → 写成品；脚本不「理解」VHDL，只做字符串替换。
- 两族生成器：**合并族**把多个不等宽信号拼成单总线（`<WIDTH>` + `<POSTFIX>` + 四个块占位符 `<DATA_IN/OUT/MERGE/UNMERGE>`）；**定宽族**只钉死位宽、生成带数组类型端口的 package+entity（只换 `<WIDTH>`）。
- 合并族端口在总线里按字典序、低位居前累加，位段为 \([nextIdx + w - 1,\ nextIdx]\)。
- `examples/*.bat` 是「可执行文档」，记录调用约定；`examples/.gitignore` 用 `*.vhd` 表达「生成物不入库」。
- ⚠️ **关键现实**：`generators/` 自首次提交后未再更新，snippet 模板仍是 v3.0.0 重构前的 PascalCase 命名（如 `DataWidth_g`/`ClkA`），与现行组件（`width_g`/`a_clk_i`）对不上，生成物需命名刷新或借助 u11-l3 迁移脚本才能编译。

## 7. 下一步学习建议

- **承接迁移**：本讲暴露的「snippet 命名过期」问题，正是 u11-l3「重构与迁移脚本」要解决的——接着读 `scripts/refactoring/migration_from_v2_to_v3_db.json`，看它如何把这类旧命名映射成新命名，并可尝试用它批量刷新本讲生成的文件。
- **回看被包装组件**：若想理解外壳到底补偿了什么，回头精读 u5-l2（`simple_cc`/`status_cc` 的 valid-only 单总线接口）与 u8-l2（`par_tdm`/`tdm_par` 的扁平并行向量接口）。
- **贡献角度**：若你打算为库新增一个「形状可变端口」的组件，可仿照本目录三件套（脚本 + snippet + bat 示例）贡献自己的生成器，并务必让模板命名跟上 v3.0.0 风格，避免重蹈本讲的过期问题——这一点与 u11-l4 的贡献规范呼应。
