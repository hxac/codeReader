# 重构与迁移脚本 scripts/refactoring

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 psi_common 在 v3.0.0「统一代码风格」大重构时，为什么需要一套**库级**（而非手工）的迁移工具；
- 读懂 `parse_library.py` 如何对比两个版本的库、自动生成「旧名 → 新名」的映射字典；
- 读懂 `hdlrefactor.py` 里四类改写函数（实体声明、例化、符号、TCL generic）各自的作用域与查表规则；
- 打开 `migration_from_v2_to_v3_db.json`，归纳出 v2 到 v3 的命名迁移规则，并理解它的结构与人手维护的性质。

本讲是纯 Python 工具的源码阅读课，**不涉及任何 VHDL 综合或仿真**。这些脚本是一次性的「历史迁移工具」——它们在 v3.0.0 发布时用过一次，平时不会再跑。但读懂它们，既能帮你维护依赖 psi_common 的旧工程，也能让你学到「用正则 + 字典做半结构化源码批量重命名」的通用套路。

## 2. 前置知识

- **VHDL 实体（entity）与例化（instantiation）**：一个 entity 有 `port`/`generic` 声明；别处用 `u1 : entity work.psi_common_xxx port map(...)` 来例化它。本讲的脚本就是在改这两处的名字。
- **命名规范（承接 u1-l4）**：psi_common v3 统一采用 snake_case，端口加 `_i`/`_o`/`_io` 方向后缀；而 v2 用的是 PascalCase（如 `Clk`、`InVld`、`OutData`）。本讲工具的全部工作就是把后者改成前者。
- **Python 基础**：`dict`、`re`（正则）、`argparse`、`pathlib.Path.rglob`、`json`。脚本只用标准库加 `pandas`（实际仅 import 未真正使用），不依赖第三方 VHDL 解析器。
- **regex 不是真正的 VHDL 解析器**：这是本讲最重要的前提。脚本用正则按行扫描，而不是构建语法树。因此它只能处理「命名整齐、风格统一」的代码——这恰恰是它要被应用到的对象。

> 名词对照：本讲反复出现「DB / database / 字典」，三者在脚本里指同一个东西——一个 `{组件名: {旧符号: 新符号}}` 的嵌套 dict，最终序列化成 JSON 文件。

## 3. 本讲源码地图

本讲涉及的全部文件都在 `scripts/refactoring/` 下（该目录与 `hdl/`、`testbench/` 同级，参见 u1-l2）：

| 文件 | 作用 | 角色 |
| --- | --- | --- |
| [parse_library.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py) | 对比「旧版库」与「新版库」两份 `hdl/`，自动生成映射 DB | 第一阶段：**生成** DB |
| [hdlrefactor.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py) | 提供查表函数 `conv_fun` 与四类改写函数 | 第二阶段的**工具箱** |
| [refactor_library_and_testbench.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/refactor_library_and_testbench.py) | 加载 DB，对 `hdl/`、`testbench/`、`sim/config.tcl` 跑一遍改写 | 第二阶段：**应用** DB（入口脚本） |
| [migration_from_v2_to_v3_db.json](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json) | v2→v3 的成品映射 DB（人手维护） | 两阶段之间的**数据契约** |
| [alpha.json](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/alpha.json) | DB 的早期快照（开发遗留，结构与成品 DB 相同） | 参考，不在流程中使用 |

整条工具链的全部代码首次出现在同一个提交 `57aa852 Devel/v3 refactoring (#50)`，对应 `Changelog.md` 3.0.0 条目里的「new script added to update your current project and replace instantiation with json database file」（见 [Changelog.md:8-11](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/Changelog.md#L8-L11)）。

## 4. 核心概念与源码讲解

### 4.1 库级重构的整体思路：两阶段流程

#### 4.1.1 概念说明

v3.0.0 是一次「不向下兼容」的大重构（见 u1-l1）：全库 60 多个组件的端口、generic 从 PascalCase 统一成 snake_case 并补齐方向后缀。如果一个下游工程里例化了上百个 psi_common 组件，靠人手改名字既慢又易错。

psi_common 的作者们没有写一个「全自动重命名器」，而是把问题拆成**两阶段**，中间用一份 JSON 解耦：

1. **生成 DB（build）**：拿「v2 旧库」和「v3 新库」两份源码，逐个 entity 对比，自动推断出每个组件每个符号该从什么改成什么，写成 JSON。
2. **应用 DB（apply）**：拿这份 JSON，去改写**任意**工程（库自身、testbench、甚至你的下游工程）里所有 `.vhd` 和 `config.tcl` 中的旧名字。

两阶段解耦的好处：DB 是一份可读、可审计、可手工修正的数据文件；应用阶段是确定性的字符串替换。你可以先 `git diff` 审查 JSON，再决定是否应用。

#### 4.1.2 核心流程

整体数据流如下：

```
        ┌──────────── 第一阶段：生成 DB ────────────┐
        │                                            │
  v2 hdl/*.vhd ─┐                                   │
               ├─→ parse_library.py ─→ migration.json
  v3 hdl/*.vhd ─┘        (entity_declaration_parser)│
        └────────────────────────────────────────────┘
                                │
                                ▼ （人手审查 / 修正 JSON）
        ┌──────────── 第二阶段：应用 DB ────────────┐
        │                                            │
        │   set_refactor_database(json)  载入内存    │
        │            │                               │
  hdl/*.vhd ─┐       ├─→ entity_declaration_refactor │
             ├─→ refactor_library_and_testbench.py   │
  tb/*.vhd  ─┘       ├─→ instantiation_refactor      │
                     ├─→ symbol_refactor             │
  sim/config.tcl ───→└─→ tcl_generics_refactor       │
        └────────────────────────────────────────────┘
```

关键设计假设（贯穿全讲）：**v3.0.0 是「纯改名」重构，组件端口的顺序与结构没变，只是名字变了。** 正因如此，第一阶段的自动对比才成立——按声明顺序把旧名和新名一一配对即可。

#### 4.1.3 源码精读

第二阶段的入口脚本 [refactor_library_and_testbench.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/refactor_library_and_testbench.py) 只有 30 多行，却把整条应用流程讲清楚了：

[scripts/refactoring/refactor_library_and_testbench.py:10-34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/refactor_library_and_testbench.py#L10-L34) — 先载入 DB，再对 `hdl/`、`testbench/` 里每个 `.vhd` 依次跑「实体声明改写 → 例化改写 → 符号改写」三步，最后单独处理 `sim/config.tcl` 的 generic。注意它的相对路径 `../../hdl`——脚本假定你从 `scripts/refactoring/` 目录内执行（参见 u1-l2 的工作副本结构约定）。

核心三行：

```python
set_refactor_database("./migration_from_v2_to_v3_db.json")   # 载入 DB
...
entity_declaration_refactor(path, path)   # 输入=输出文件名 → 原地改写
instantiation_refactor(path, path)
symbol_refactor(path, path)
```

所有改写函数都接受 `(file_name_i, file_name_o)` 两个文件名，传同一个名字就是**原地覆盖**。这点很重要：脚本会直接改你的源文件，应用前务必先 `git commit` 或备份。

#### 4.1.4 代码实践

1. **实践目标**：建立对两阶段流程的直观认识，不实际运行（脚本会改写源码，禁止在本仓库执行）。
2. **操作步骤**：
   - 打开 [refactor_library_and_testbench.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/refactor_library_and_testbench.py)，数一下它调用了几个 `*_refactor` 函数、分别针对哪几类文件。
   - 在 [parse_library.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py) 里找到写 JSON 的那一行（提示：`json.dump`）。
3. **需要观察的现象**：应用阶段（`refactor_library_and_testbench.py`）只**读** JSON、不**写** JSON；生成阶段（`parse_library.py`）只**写** JSON、不调用任何 `*_refactor`。两者唯一的耦合就是这份 JSON 文件名。
4. **预期结果**：你能用一句话说清「谁生成 DB、谁消费 DB」。
5. **待本地验证**：若要真正跑一次，需准备一份 v2 的 `hdl/` 旧拷贝，本仓库已全是 v3，无法直接演示生成阶段。

#### 4.1.5 小练习与答案

**练习 1**：为什么把工具拆成「生成 DB」和「应用 DB」两阶段，而不是写一个直接扫描重命名的脚本？

> **答案**：拆分后，DB 成为可读、可审查、可手改的中间产物。生成阶段需要两份完整库做对比（成本高、只做一次），应用阶段是确定性替换（便宜、可对任意工程反复跑）。两者解耦后，应用阶段不再需要旧库源码，只需一份 JSON。

**练习 2**：入口脚本里 `entity_declaration_refactor(path, path)` 为什么两个参数相同？

> **答案**：第一个是输入文件名、第二个是输出文件名。相同即「原地覆盖」——直接把改写结果写回原文件。这也是为什么应用前必须先备份。

---

### 4.2 解析库结构：parse_library.py

#### 4.2.1 概念说明

`parse_library.py` 负责**第一阶段：生成 DB**。它的输入是两个目录——v2 旧库的 `hdl/` 与 v3 新库的 `hdl/`，输出是一个 JSON 文件。

它的核心技巧很巧妙：**它不去「理解」改名规则，而是利用「端口顺序不变」这个假设，把旧名和新名按下标配对。** 对每个组件：

- 从旧库 entity 里抽出端口的「名字列表」（顺序保留），形如 `["Clk", "Rst", "InVld", ...]`；
- 从新库同名 entity 里抽出同样的列表，形如 `["clk_i", "rst_i", "vld_i", ...]`；
- 用 `zip` 按位置配对，得到 `{旧名: 新名}`。

这套机制成立的前提，正是 4.1 说的「v3 是纯改名重构」。

#### 4.2.2 核心流程

记旧库某组件的端口名有序列为 \( O = (o_1, o_2, \dots, o_n) \)，新库同名组件的端口名为 \( N = (n_1, n_2, \dots, n_n) \)，则该组件的映射为：

\[
\text{map}_{\text{comp}} = \{\, o_i \mapsto n_i \mid i = 1..n \,\}
\]

整个 DB 是所有组件映射的并集，再并上一个全局公共映射 `#ALL#`：

\[
\text{DB} = \{\,\text{#ALL#} \mapsto \text{公共映射}\,\}\ \cup\ \bigcup_{\text{comp}} \{\,\text{comp} \mapsto \text{map}_{\text{comp}}\,\}
\]

`#ALL#` 放的是「所有组件共享、与具体组件无关」的符号（主要是 `psi_common_logic_pkg` 里的函数名，如 `BinaryToGray → binary_to_gray`），由脚本硬编码作为默认值，见下文。

注意 `zip` 配对**完全依赖顺序一致**。若 v3 某组件比 v2 多了一个端口、或调换了端口顺序，配对就会整体错位——这是这套方案最脆弱的地方，也是 DB 必须人工复核的原因。

#### 4.2.3 源码精读

脚本用 `argparse` 接收三个位置参数（旧库目录、新库目录、输出 JSON 名）：

[scripts/refactoring/parse_library.py:21-31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py#L21-L31) — 命令行约定为 `python parse_library.py <旧库hdl> <新库hdl> <输出.json>`。

接着硬编码一份 `#ALL#` 默认映射作为 DB 的起点：

[scripts/refactoring/parse_library.py:35-58](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py#L35-L58) — 这一段把 `logic_pkg` 的函数名（`ZerosVector→zeros_vector` 等）和几个公共 generic（`Ratio_g→ratio_g`、`HandleRdy_g→handle_rdy_g` 等）写死。这些符号在所有组件里都可能出现，所以放在全局表。

随后定义一个**黑名单**：

[scripts/refactoring/parse_library.py:61](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py#L61) — `blacklist = ["rst_pol_g", "a_rst_pol_g", "b_rst_pol_g"]`。复位极性 generic 在自动生成时被剔除（复位策略在 v3 有单独考量，留给人工决定），不会被写进自动生成的 DB。

第一遍循环扫旧库，把每个组件的端口名以「自映射」`{名: 名}` 形式存入 DB：

[scripts/refactoring/parse_library.py:63-70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py#L63-L70) — 此时 `database[k]` 的键就是**旧**端口名（值暂时等于键）。`entity_declaration_parser` 返回的就是 `{端口名: 端口名}` 这种恒等字典，只用来记录「端口集合 + 顺序」。

第二遍循环扫新库，**用新库的值替换旧库的值**，这是全脚本最关键的一行：

[scripts/refactoring/parse_library.py:72-82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py#L72-L82) — 核心是：

```python
merge = dict(zip(database[k].keys(), v.values()))
```

`database[k].keys()` 是旧库端口名（保序），`v.values()` 是新库端口名（保序）。`zip` 按位置配对后 `dict()` 转成 `{旧名: 新名}`——这正是迁移映射。最后写盘：

[scripts/refactoring/parse_library.py:84-85](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/parse_library.py#L84-L85) — `json.dump(database, f, indent=3)`。

> 关于 `entity_declaration_parser` 本身：它在 [hdlrefactor.py:160-208](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L160-L208) 定义，逻辑是「进入 `entity X is` 后，用正则 `(\w+)\s*:` 抓取每个端口名，直到 `end entity`」。它被 `parse_library.py` 通过 `from hdlrefactor import entity_declaration_parser` 复用——两个脚本共享一个工具箱。

#### 4.2.4 代码实践

1. **实践目标**：理解「按位置配对」如何生成映射。
2. **操作步骤**：在纸上模拟一遍。假设旧库某 entity 声明顺序为 `Clk, Rst, InVld, InData, OutVld, OutData`，新库同名 entity 为 `clk_i, rst_i, vld_i, dat_i, vld_o, dat_o`。手写 `zip` 配对后的 `dict`。
3. **需要观察的现象**：配对结果里 `Clk→clk_i`、`InVld→vld_i`、`OutData→dat_o`。
4. **预期结果**：你得到 6 条 `{旧名: 新名}`，且顺序与原声明一致。
5. **延伸思考**：若新库在 `Rst` 后面多插了一个 `en_i` 使能端口，配对会怎样？答：从 `InVld` 起全部错位（`InVld→en_i`、`InData→vld_i`……），整张映射作废。这正是 DB 必须人工复核的根本原因。

#### 4.2.5 小练习与答案

**练习 1**：`parse_library.py` 里 `database[k]` 在扫完旧库后、扫新库前，存的是什么形状的数据？

> **答案**：`{旧端口名: 旧端口名}`，即值与键相同的恒等字典，仅用于记录端口的「集合与顺序」。

**练习 2**：为什么需要 `#ALL#` 这张全局表，不能全部靠逐组件对比得到？

> **答案**：像 `BinaryToGray`、`Ratio_g` 这类符号来自 `logic_pkg`/`math_pkg`，会在很多组件的内部代码里出现，但它们不属于任何单个 entity 的 port/generic 声明，逐组件对比抓不到。所以用一张全局硬编码表统一处理。

---

### 4.3 重构规则：hdlrefactor.py 的查表与四类改写函数

#### 4.3.1 概念说明

`hdlrefactor.py` 是工具箱，提供：

- 1 个载入函数：`set_refactor_database`（把 JSON 读进内存并做后处理）；
- 1 个查表函数：`conv_fun`（给「组件 + 旧符号」查「新符号」）；
- 4 个改写函数：`entity_declaration_refactor`、`instantiation_refactor`、`symbol_refactor`、`tcl_generics_refactor`。

四个改写函数的**区别在于作用域**——它们各自只在自己负责的语法区域里做替换：

| 函数 | 改写的语法区域 | 典型目标 |
| --- | --- | --- |
| `entity_declaration_refactor` | `entity ... end entity` 内的 `port`/`generic` 声明 | 改组件**定义**里的端口名 |
| `instantiation_refactor` | `port map(...)` / `generic map(...)` 内的 `名字 =>` 关联 | 改别人**例化**该组件时用的端口名 |
| `symbol_refactor` | **整行所有单词**（最宽） | 改函数调用、generic 引用等散落符号 |
| `tcl_generics_refactor` | `config.tcl` 里 `-g名字` 的 generic 覆盖值 | 改仿真回归命令行里的 generic 名 |

前两者只查「组件专属表」，第三个额外回退到 `#ALL#` 全局表（因为函数名不属于任何单个组件）。

#### 4.3.2 核心流程

查表函数 `conv_fun(comp, signal)` 是一颗三级决策树：

\[
\text{conv\_fun}(c, s) =
\begin{cases}
\text{DICT}[c][s] & \text{若组件 } c \text{ 的表里有 } s \\
\text{DICT}[\text{#ALL#}][s] & \text{若 } \text{use\_all=true} \text{ 且全局表里有 } s \\
s & \text{否则原样返回（不改）}
\end{cases}
\]

「原样返回」是安全兜底：查不到的名字不动，避免误伤。`use_all` 只有 `symbol_refactor` 和 `tcl_generics_refactor` 设为 `True`——因为散落符号（如 `BinaryToGray(...)` 调用）需要回退到全局表；而端口/例化关联是组件专属的，不该用全局表乱套。

每个改写函数的通用骨架是：

```
for 每一行:
    剥离注释（-- 之后）        # 避免改到注释里的字
    if 当前行匹配「作用域起点」: 记录 comp_name、进入作用域
    if 在作用域内 and 当前行匹配「目标模式」:
        用 conv_fun(comp_name, 捕获到的旧名) 替换
    拼回注释，写输出
```

注释剥离是个细节但很关键：脚本用 `l.split('--')` 取前半段做匹配，后半段原样拼回，从而不会把注释里的 PascalCase 单词也改掉。

#### 4.3.3 源码精读

**载入与后处理** [scripts/refactoring/hdlrefactor.py:14-57](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L14-L57)：`set_refactor_database` 读 JSON 后做三步后处理——

- `fix_case=True`（行 19-25）：为每个旧名额外加一份**全小写键**，使查表大小写不敏感（VHDL 本身大小写不敏感，旧代码里可能写成 `CLK`、`Clk`、`clk`）。
- `add_tb=True`（行 26-33）：为每个组件额外复制一份 `<组件>_tb` 键。因为 testbench 例化 DUT 时用的是同一组端口名，同一张映射可直接复用。
- `add_dict`（行 34-56）：打几个针对 AXI 主机 TB 包名改动的**专用补丁**（如 `..._tb_pkg → ..._tb`），处理自动规则覆盖不到的特殊情况。

**查表** [scripts/refactoring/hdlrefactor.py:59-77](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L59-L77)：`conv_fun` 用两层 `try/except` 实现三级回退，对应上面的决策树。注意行 62 的 `raise "..."` 是 Python 里不规范写法（应 `raise Exception(...)`），但功能上是「未载入 DB 就报错」。

**例化改写** [scripts/refactoring/hdlrefactor.py:80-155](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L80-L155)：先用正则 `entity work.(psi_common_\w+)` 识别当前例化的是哪个组件（行 109），再用 `generic map`/`port map ... )` 圈定作用域（行 107-108），在作用域内匹配 `名字 =>` 关联（行 114）并替换左侧名字（行 144-150）。

**实体声明改写** [scripts/refactoring/hdlrefactor.py:210-261](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L210-L261)：在 `entity ... end entity` 内，匹配 `名字 :` 形式的端口声明并替换名字（行 226、253-255）。

**符号改写** [scripts/refactoring/hdlrefactor.py:263-314](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L263-L314)：最宽。行 304 用 `re.sub` 对整行所有 `\w+` 单词逐个过 `conv_fun(comp, word, make_lower_case=True, use_all=True)`——大小写不敏感、且回退全局表，所以能改掉散落的 `BinaryToGray(...)` 调用。

**TCL generic 改写** [scripts/refactoring/hdlrefactor.py:317-356](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py#L317-L356)：针对 `config.tcl`。行 327 用正则 `(-g)(\w+)` 抓 `-g名字` 形式的 generic 覆盖（如 `-ga_freq_clk_g=...`，参见 u1-l3 的 `config.tcl` 结构），行 328 用 `create_tb_run "组件名"` 确定当前 generic 属于哪个组件，再查表替换。注释剥离改用 `#` 分隔（TCL 注释是 `#` 而非 `--`，行 341）。

#### 4.3.4 代码实践

1. **实践目标**：验证「作用域」与「查表回退」两个机制。
2. **操作步骤**：
   - 在 [hdlrefactor.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/hdlrefactor.py) 里找到四个 `*_refactor` 函数各自的「作用域起点正则」（提示：`start_entity_declaration`、`start_instantiation_map`、`start_entity_declaration`、`create_tb_run`）。
   - 对比 `instantiation_refactor`（行 148）与 `symbol_refactor`（行 304）调用 `conv_fun` 时传入的 `use_all` 参数差异。
3. **需要观察的现象**：例化改写调用 `conv_fun(comp_name, grp[2])`（`use_all` 默认 `False`），符号改写调用 `conv_fun(comp_name, m.group(1), True, True)`（`use_all=True`）。
4. **预期结果**：你能解释「为什么符号改写需要全局回退而例化改写不需要」——例化关联的左侧名字一定是某组件的端口，属于组件专属表；散落符号可能是任意 package 函数，必须回退 `#ALL#`。
5. **待本地验证**：可复制一份 `.vhd` 到临时目录，手工构造一份小 JSON，调用单个函数实验——但**不要**在仓库 `hdl/` 上运行。

#### 4.3.5 小练习与答案

**练习 1**：四个改写函数中，哪个会改动注释里的内容？为什么？

> **答案**：都不会。每个函数在匹配前都先用 `l.split('--')`（或 TCL 的 `#`）把注释剥离，只对代码部分做替换，最后把注释原样拼回。

**练习 2**：`set_refactor_database` 的 `add_tb=True` 为什么要给每个组件额外加一份 `_tb` 键？

> **答案**：testbench 文件里例化 DUT 时用的端口名与组件定义里的一致，同一张 `{旧名: 新名}` 映射可直接套用。但 `symbol_refactor` 靠文件里出现的 `entity ... is` 来确定 `comp_name`，TB 文件里的 entity 名是 `<组件>_tb`，故需要一张 `<组件>_tb` 同名映射，查表才能命中。

---

### 4.4 v2→v3 迁移映射与 JSON 规则库

#### 4.4.1 概念说明

`migration_from_v2_to_v3_db.json` 是第一阶段产出、第二阶段消费的**数据契约**，也是本讲最适合人眼阅读的文件。它把 v2→v3 的全部命名规则固化成一张查表。理解了它的结构，你就理解了 v3.0.0「统一风格」到底统一了什么。

需要特别说明：这份 JSON **不是** `parse_library.py` 纯自动生成的产物，而是**人手维护**的成品。证据有二：(1) `parse_library.py` 的黑名单会剔除 `rst_pol_g`，但成品 JSON 里仍含 `rst_pol_g` 条目（如 `trigger_digital`）；(2) JSON 里甚至能发现个别未完全小写的小瑕疵（见 4.4.3）。这说明自动生成只提供初稿，人工再做补充与修正——这也是两阶段解耦的价值所在。

#### 4.4.2 核心流程

JSON 的顶层是一个对象，键分两类：

- `"#ALL#"`：全局公共映射，对所有组件生效。
- `"psi_common_<组件>"`：组件专属映射，键是该组件的 v2 旧符号，值是 v3 新符号。

查表时，应用阶段会先查组件专属表，查不到再（在 `symbol_refactor` 里）回退 `#ALL#`。`#ALL#` 主要收录 `logic_pkg` 函数与几个公共 generic。

#### 4.4.3 源码精读（命名迁移规则归纳）

**全局表 `#ALL#`** [scripts/refactoring/migration_from_v2_to_v3_db.json:2-25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json#L2-L25)：收录公共函数与 generic，如 `BinaryToGray→binary_to_gray`、`PpcOr→ppc_or`、`Ratio_g→ratio_g`、`HandleRdy_g→handle_rdy_g`。

下面用三个真实组件归纳出 v2→v3 的命名规则。

**`psi_common_sdp_ram`**（纯改名 + 方向后缀） [scripts/refactoring/migration_from_v2_to_v3_db.json:718-732](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json#L718-L732)：

| v2 | v3 | 规则 |
| --- | --- | --- |
| `Depth_g` | `depth_g` | PascalCase generic → snake_case |
| `Clk` | `wr_clk_i` | 加方向/功能前缀 `wr_` + 方向后缀 `_i` |
| `RdClk` | `rd_clk_i` | 同上 |
| `WrData` | `wr_dat_i` | `Data→dat`、加方向后缀 |
| `RdData` | `rd_dat_o` | 输出加 `_o` |

**`psi_common_pl_stage`**（无前缀、纯方向后缀） [scripts/refactoring/migration_from_v2_to_v3_db.json:316-327](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json#L316-L327)：`InVld→vld_i`、`InRdy→rdy_o`、`InData→dat_i`、`OutVld→vld_o`、`OutRdy→rdy_i`、`OutData→dat_o`。这里 `In/Out` 前缀被**去掉**，改为用 `_i/_o` 后缀表达方向——这正是 u1-l4 讲的 AXI-S 握手命名规范。

**`psi_common_async_fifo`**（保留前缀、双时钟域） [scripts/refactoring/migration_from_v2_to_v3_db.json:640-659](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json#L640-L659)：`InClk→in_clk_i`、`InData→in_dat_i`、`OutData→out_dat_o`、`AlmFullLevel_g→afull_lvl_g`。注意此处 `In/Out` 前缀**保留**（因为异步 FIFO 有两个时钟域，需要 `in_/out_` 区分），与 `pl_stage` 不同——同一个 `InData`，在不同组件里映射到不同新名（`dat_i` vs `in_dat_i`），这正是「必须按组件查表、不能用一张全局表」的根本原因。

> 规则归纳（本讲实践任务的核心）：
> 1. **generic**：PascalCase → snake_case（`Depth_g→depth_g`）。
> 2. **端口方向**：用 `_i/_o/_io` 后缀取代 `In*/Out*` 前缀；单时钟域组件去掉前缀（`pl_stage`），多时钟域组件保留 `in_/out_` 前缀（`async_fifo`）。
> 3. **缩写统一**：`Data→dat`、`AlmFull→afull`、`AlmFullLevel→afull_lvl`、`Clock→clk`、`Address→addr`。
> 4. **AXI 信号**：`M_Axi_AwAddr→m_axi_awaddr`（去下划线大写、整体小写、保留通道缩写）。
> 5. **函数（logic_pkg）**：PascalCase → snake_case，放 `#ALL#` 全局表。

最后看一处体现「人手维护」的小瑕疵：[scripts/refactoring/migration_from_v2_to_v3_db.json:751](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json#L751) 在 `psi_common_axi_multi_pl_stage` 里，`InWReady` 被映射到 `In_wready`（首字母 `I` 未小写、且漏了前缀统一），与同组其他信号（`in_wvalid`、`in_wlast`）风格不一致。这类细节证明 JSON 是人工编辑的，也提醒我们：**应用 DB 后必须 `git diff` 逐文件复核**，不能盲信。

#### 4.4.4 代码实践（对应本讲主任务）

1. **实践目标**：阅读 `migration_from_v2_to_v3_db.json`，亲自归纳命名迁移规则。
2. **操作步骤**：
   - 打开 [migration_from_v2_to_v3_db.json](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json)。
   - 任选 3 个组件（建议 `psi_common_spi_master` 行 684、`psi_common_i2c_master` 行 144、`psi_common_delay` 行 499），把它们的 `{旧: 新}` 整理成表格。
   - 对照本节给出的 5 条规则，逐条标注每个映射属于哪条规则。
   - 找一个「不符合任何规则」的特例（如某处缩写不统一），记下来。
3. **需要观察的现象**：大多数映射都能归入「PascalCase→snake_case + 方向后缀 + 缩写」三条；少数（如 `AlmFullLevel_g→afull_lvl_g`）带有约定俗成的缩写需要单独记。
4. **预期结果**：你能用自己的话写出一份 5 条左右的「v2→v3 命名迁移速查表」。
5. **待本地验证**：若手边有依赖 v2 psi_common 的旧工程，可挑一个 `.vhd`，对照 JSON 手工预测几个例化端口的新名，再与 v3 文档核对。

#### 4.4.5 小练习与答案

**练习 1**：`InData` 在 `pl_stage` 和 `async_fifo` 里分别映射成什么？为什么不同？

> **答案**：`pl_stage` 里是 `dat_i`，`async_fifo` 里是 `in_dat_i`。因为 `pl_stage` 是单时钟域，方向只用 `_i/_o` 表达，`In` 前缀去掉；`async_fifo` 有两个时钟域，需要 `in_/out_` 前缀区分读写侧，故保留。这证明了必须按组件查表。

**练习 2**：JSON 里 `#ALL#` 表为什么不能合并进各组件表？

> **答案**：`#ALL#` 里的符号（如 `BinaryToGray`）是 package 函数，不属于任何 entity 的端口/generic 声明，逐组件 `entity_declaration_parser` 抓不到。它们散落在各组件的**内部代码**里，靠 `symbol_refactor` 的全局回退来改。合并进组件表既冗余也无法覆盖「组件内部代码」这一作用域。

**练习 3**：发现 JSON 第 751 行 `InWReady→In_wready` 这种小瑕疵后，正确做法是什么？

> **答案**：在应用 DB 后，对每个被改写的文件 `git diff` 逐行复核，手工修正这类不一致；必要时直接编辑 JSON 修正映射再重跑。绝不能盲信自动改写结果——这也是工具被设计成「生成可读 JSON + 确定性替换」而非「一键全自动」的原因。

## 5. 综合实践

把整讲串起来，完成一次「模拟迁移评审」：

1. **情境**：假设你维护一个 2019 年基于 psi_common v2.17 的 FPGA 工程，现在要升到 v3.0.0。工程里有几十个 `.vhd` 例化了 psi_common 组件，还有一个自定义 `config.tcl`。
2. **任务**：
   - (a) 用本讲的两阶段流程，写出升级步骤：先 `git clone` 一份 v2 和 v3 的库，跑 `python parse_library.py v2/hdl v3/hdl my_db.json` 生成初稿 DB；打开 `my_db.json` 与仓库的 [migration_from_v2_to_v3_db.json](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/migration_from_v2_to_v3_db.json) 对比，用后者（人工维护版）覆盖初稿。
   - (b) 把 [refactor_library_and_testbench.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/refactor_library_and_testbench.py) 的路径改成指向你的工程目录，备份后运行。
   - (c) `git diff` 审查改动，重点关注：组件例化的 `port map`、内部对 `logic_pkg` 函数的调用、TCL 的 `-g` generic。
3. **验收标准**：你能指出改动里至少一处「自动改写可能出错、需要人工确认」的地方（例如某组件端口顺序在 v2→v3 间发生过真实变化，或某处符号恰好与库外自定义信号同名被误改）。
4. **反思**：这套工具为什么用正则而不是真正的 VHDL 语法树？答：依赖少、对「风格统一」的 v2 代码足够有效，且把不确定性留给 JSON 与人工评审——工程上更可控。

## 6. 本讲小结

- psi_common 把 v3.0.0 大重构做成**两阶段**工具：`parse_library.py` 对比新旧库自动生成「旧名→新名」JSON，`refactor_library_and_testbench.py` 加载 JSON 对 `hdl/`、`testbench/`、`config.tcl` 做确定性改写，中间用 JSON 解耦。
- 自动生成映射的关键假设是 **v3 是纯改名重构、端口顺序不变**，因此可用 `dict(zip(旧名, 新名))` 按位置配对；一旦顺序变了映射就错位，所以 DB 必须人工复核。
- `hdlrefactor.py` 的核心是查表函数 `conv_fun`（组件表 → `#ALL#` 全局表 → 原样返回的三级回退）和四个**作用域不同**的改写函数（实体声明、例化、符号、TCL generic）。
- 所有改写函数都先剥离注释再匹配，避免误改注释；`symbol_refactor` 是唯一对整行所有单词做替换、且回退全局表的函数。
- `migration_from_v2_to_v3_db.json` 是人手维护的数据契约，可归纳出 5 条命名规则：generic 转 snake_case、用 `_i/_o` 方向后缀取代 `In/Out` 前缀、缩写统一（`Data→dat`、`AlmFull→afull`）、AXI 信号整体小写、package 函数进 `#ALL#`。
- 同一个符号（如 `InData`）在不同组件映射不同（`dat_i` vs `in_dat_o`），这就是「必须按组件查表」、不能靠一套全局正则搞定的根本原因。

## 7. 下一步学习建议

- 若想看「自动生成 vs 人工维护」的另一面，阅读 [generators/](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators) 目录（u11-l2）——那是一套用 Python + snippet 模板「正向生成」VHDL 的工具，与本讲的「反向迁移」对照学习。
- 结合 u1-l4 复习 v3 命名规范（snake_case、`_i/_o/_io`、AXI-S 握手），你会更深刻地理解本讲工具「为什么把这些旧名改成这些新名」。
- 想了解库的依赖与发布流程，可接着读 u11-l4（贡献流程与发布管理）以及 [scripts/dependencies.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/dependencies.py)。
- 进阶练习：尝试用 Python 写一个**最小版** `entity_declaration_parser`，只抓某个 `.vhd` 的端口名列表，验证你对正则作用域的理解。
