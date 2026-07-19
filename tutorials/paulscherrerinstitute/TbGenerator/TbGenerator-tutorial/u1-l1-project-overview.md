# 项目定位、依赖与许可

> 讲义编号：u1-l1　|　学习阶段：beginner　|　本系列第一篇，无前置讲义

## 1. 本讲目标

本讲是整本《TbGenerator 学习手册》的起点。读完后你应当能够：

- 用一句话说清楚 **TbGenerator 是做什么的工具**，以及它输出的两种 testbench 形态。
- 说出本工具的 **三类外部依赖**（PsiPyUtils、PyQt5、pyparsing）各自的角色。
- 看懂 `Changelog.md`，并指出当前 3.0.x 版本相对旧版的两个关键变化（PyQt5 迁移、PsiPyUtils 3.0.0 要求）。
- 说明本仓库采用的 **PSI HDL Library License** 的本质（LGPL + 针对 FPGA/硬件场景的例外条款）。
- 对照真实仓库结构，知道每个文件大致负责什么。

本讲不要求你写任何代码，重点是「读懂文档、理清定位」，为后面动手生成第一个 testbench 打好基础。

## 2. 前置知识

如果你对下面这些概念完全陌生，建议先花十分钟了解：

- **VHDL**：一种硬件描述语言，用来描述数字电路。TbGenerator 的输入就是 VHDL 文件。
- **DUT（Design Under Test）**：被测设计的简称，也就是你要测试的那段 VHDL 电路。
- **Testbench（测试台 / TB）**：围绕 DUT 写的一段「为了仿真而存在」的 VHDL 代码，它给 DUT 喂激励信号、观察输出，本身通常不可综合。
- **Python**：本工具完全用 Python 编写，因此后续会涉及 Python 模块、`argparse`、类等概念。

> 一个直觉比喻：DUT 是「待测的零件」，Testbench 是「夹具 + 示波器 + 信号发生器」。手写夹具很枯燥，TbGenerator 就是帮你自动生成这套夹具骨架的机器。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为仓库根目录下的真实文件）：

| 文件 | 作用 | 是否本讲精读 |
| --- | --- | --- |
| `README.md` | 项目总说明：用途、两种 TB 形态、依赖、目录结构、许可与版本标记策略 | 是 |
| `Changelog.md` | 版本演进记录，从 1.0.0 到当前 3.0.4 | 是 |
| `License.txt` | PSI HDL Library License 全文（LGPL + 例外条款） | 是 |
| `LGPL2_1.txt` | 许可证所基于的 LGPL 2.1 全文（参考） | 否 |
| `doc/TbGenerator.pdf` | 官方详细文档（README 指向它） | 参考，二进制不精读 |

此外，为了让你对仓库有整体印象，这里先给出完整目录结构（来自 `git ls-files`）：

```
TbGenerator/
├── README.md                  # 项目说明
├── Changelog.md               # 版本史
├── License.txt                # PSI HDL Library License
├── LGPL2_1.txt                # LGPL 2.1 全文
├── TbGen.py                   # CLI 入口 + TbGenerator 主类 + 生成主流程
├── TbGenGui.pyw               # GUI 入口（PyQt5）
├── DutInfo.py                 # DUT 信息模型 + 标签解析
├── TbInfo.py                  # 测试台信息模型
├── VhdlParse.py               # 基于 pyparsing 的 VHDL 解析器
├── MultiFileTb.py             # 多文件 TB 的包/用例生成
├── UtilFunc.py                # 输出格式化工具
├── doc/
│   ├── TbGenerator.docx       # 文档源文件
│   └── TbGenerator.pdf        # 文档 PDF
└── example/
    ├── simpleTb/              # 单文件 TB 示例
    │   ├── psi_common_async_fifo.vhd
    │   └── run.bat
    └── multiCaseTb/           # 多用例 TB 示例
        ├── psi_common_async_fifo.vhd
        └── run.bat
```

> 注意：`README.md` 里提到的「folder structure」是指上游聚合仓库 [psi_fpga_all](https://github.com/paulscherrerinstitute/psi_fpga_all) 里的目录约定，并非本仓库内部结构。本仓库本身是扁平的，入口脚本就在根目录。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：README（依赖与目录结构）、Changelog（版本史）、License（许可说明）。

### 4.1 README 依赖与目录结构说明

#### 4.1.1 概念说明

`README.md` 是进入任何开源项目的「第一扇门」。对 TbGenerator 而言，它用极短的篇幅回答了四个问题：

1. **这是什么**：自动从 DUT 的 VHDL 文件生成 testbench 骨架。
2. **生成物长什么样**：单文件 TB 或多文件 TB 两种形态。
3. **依赖什么**：一个 Python 库 PsiPyUtils + 两个 pip 包（PyQt5、pyparsing）。
4. **怎么持续维护**：一套语义化版本标记策略（major.minor.bugfix）。

读懂这四点，你就掌握了工具的「定位」与「运行前提」，这比直接看代码更省力。

#### 4.1.2 核心流程

README 对工具行为的描述可以归纳为下面的流水线：

```
带 $$ 标签 $$ 注释的 VHDL DUT 文件
            │  （TbGenerator 解析 + 生成）
            ▼
   ┌────────────────────────┐
   │   testbench 骨架代码    │
   └────────────────────────┘
        │              │
   单文件 TB        多文件 TB
   （一个 .vhd）   （TB 包 + 每用例一个包）
```

其中「两种形态」是初学者最容易混淆的点，务必记住：**单文件 TB 把所有代码塞进一个文件、没有独立用例；多文件 TB 在声明了多个 testcase 时，为每个用例单独生成一个 package 文件，便于组织大型测试台。**

依赖侧则分成两层：Python 库层（PsiPyUtils）提供文件写入等基础能力，External 层（PyQt5、pyparsing）分别支撑 GUI 与 VHDL 文法解析。

#### 4.1.3 源码精读

README 的「Description」段落点明了工具用途与两种 TB 形态：

> [README.md:L15-L28](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/README.md#L15-L28) —— 工具说明：自动从 DUT VHDL 生成 testbench 骨架，附加设置（如时钟频率）可直接以注释形式标注在 VHDL 文件中，并定义了 Single File TB 与 Multi File TB 两种形态。

依赖部分明确列出了 PsiPyUtils 版本下限与两个外部 pip 包：

> [README.md:L37-L52](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/README.md#L37-L52) —— 依赖说明：Python 层需要 PsiPyUtils **3.0.0 或更高**，External 层需要 PyQt5 与 pyparsing；同时提示可使用 `psi_fpga_all` 聚合仓库获取正确的目录结构。

版本标记策略在文档里也很关键，它解释了为什么当前是 `3.0.x`：

> [README.md:L30-L35](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/README.md#L30-L35) —— Tagging Policy：不向后兼容的改动升 major、新增功能升 minor、仅修 bug 升 bugfix。

#### 4.1.4 代码实践

**实践目标**：亲手确认本机环境是否满足 README 列出的两个 pip 依赖。

**操作步骤**：

1. 打开终端，执行下面的命令查看是否已安装、版本是多少：

```bash
python -c "import pyparsing; print('pyparsing', pyparsing.__version__)"
python -c "import PyQt5; from PyQt5.QtCore import QT_VERSION_STR; print('PyQt5 Qt', QT_VERSION_STR)"
```

2. 如果某条命令报 `ModuleNotFoundError`，则用 pip 安装：

```bash
pip install pyparsing PyQt5
```

**需要观察的现象**：两条 `import` 是否都成功，分别打印出的版本号是多少。

**预期结果**：两条命令都成功打印版本号即说明依赖就绪。

**待本地验证**：由于结果取决于你本机已装的内容，具体版本号请以你实际看到的为准。若你的环境中 `python` 命令不存在，可改用 `py`（Windows）或 `python3`（Linux/macOS）。

#### 4.1.5 小练习与答案

**练习 1**：README 说 PsiPyUtils 需要「3.0.0 or higher」。如果一个系统里装的是 PsiPyUtils 2.5.x，能否直接用当前版本的 TbGenerator？为什么？

> **参考答案**：不能。根据 Tagging Policy 与 Changelog，3.0.0 版本是一次「不向后兼容」的升级，代码已经改为依赖 PsiPyUtils 3.0.0，不再兼容 2.x。需要先升级 PsiPyUtils。

**练习 2**：用一句话说明 Single File TB 与 Multi File TB 的本质区别。

> **参考答案**：单文件 TB 把所有测试代码放在一个 `.vhd` 里、无独立用例；多文件 TB 在声明了多个 testcase 时，为每个用例单独生成一个 package 文件，便于组织大型测试台。

---

### 4.2 Changelog 版本史

#### 4.2.1 概念说明

`Changelog.md` 记录了每个版本「做了什么改动」。对学习者来说它有两层价值：

- **理解现状**：知道当前 `3.0.x` 是怎么一步步演化来的，哪些行为是新近才稳定的。
- **排错线索**：当你遇到某个奇怪行为（比如「注释掉的 use 语句居然被处理了」），可以先翻 Changelog 看是不是已知并已修复的问题。

Changelog 的每个条目都遵循「版本号 + 分类（New Features / Bugfixes / 不向后兼容）」的结构，配合 README 的 Tagging Policy，就能把版本号和改动性质对应起来。

#### 4.2.2 核心流程

把 Changelog 当成一条时间轴，从下往上（旧→新）读：

```
1.0.0  首次发布
  │
1.1.0  新增 TBPKG 标签 + 多个 bugfix（时钟上升沿对齐、下划线、范围类型…）
  │
2.0.0  首个开源发布；升级到 PsiPyUtils 2.0.0（不向后兼容）
  │
3.0.0  升级到 PsiPyUtils 3.0.0（不向后兼容，不再支持 2.x）
  │
3.0.1  容忍 generic 声明中的注释行（bugfix）
3.0.2  不再把注释掉的 library use-clause 当真（bugfix）
  │
3.0.4  迁移到 PyQt5（当前稳定版，PyQt4 不再支持）
```

两个「分水岭」最值得记住：**2.0.0**（首个开源版本，旧历史已丢弃）和 **3.0.0**（依赖 PsiPyUtils 3.0.0，跨版本不兼容）。

#### 4.2.3 源码精读

当前最新版本 3.0.4 的条目记录了 GUI 框架的迁移：

> [Changelog.md:L1-L7](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/Changelog.md#L1-L7) —— 3.0.4：支持 PyQt5，PyQt4 不再支持；由 Oliver Bruendler 完成迁移并标记为稳定发布。

3.0.0 是依赖层面的关键分水岭：

> [Changelog.md:L19-L22](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/Changelog.md#L19-L22) —— 3.0.0：不向后兼容，代码改为适配 PsiPyUtils 3.0.0，不再支持 2.x。

两个 bugfix 条目对后续理解解析行为有帮助（注释行的处理是 VHDL 解析里的常见坑）：

> [Changelog.md:L9-L17](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/Changelog.md#L9-L17) —— 3.0.1：容忍 generic 声明中的注释行；3.0.2：不再把注释掉的 library use-clause 当作有效声明处理。

#### 4.2.4 代码实践

**实践目标**：把 Changelog 的版本条目与 README 的 Tagging Policy 对应起来，验证版本号变化与改动性质是否一致。

**操作步骤**：

1. 打开 [Changelog.md:L1-L50](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/Changelog.md#L1-L50)。
2. 列一张三列表格：**版本号 / 改动性质（新功能·修 bug·不向后兼容）/ 对应的 Tagging Policy 应升哪一位（minor/bugfix/major）**。
3. 重点核对：
   - 1.1.0 加了 `TBPKG` 标签 → 是否对应 minor？
   - 3.0.1、3.0.2 修了 bug → 是否对应 bugfix？
   - 3.0.0 改 PsiPyUtils → 是否对应 major？

**需要观察的现象**：Changelog 里标注的改动性质，是否与版本号最后一位的升降规则一致。

**预期结果**：完全一致。例如 3.0.0 是「不向后兼容」→ major 位从 2 跳到 3；3.0.1/3.0.2 仅修 bug → 只动 bugfix 位；3.0.4 的 PyQt5 迁移虽较大，但因对外行为对用户而言仍是「能跑的 GUI」，被归入稳定发布并落在 3.0.x 系列。

#### 4.2.5 小练习与答案

**练习 1**：Changelog 里 1.1.0 提到「Clocks are now rising edge aligned」。请结合 Tagging Policy 推断：这种改动通常应升 minor 还是 bugfix？为什么作者把它放进 1.1.0（一个 minor 升级）里？

> **参考答案**：严格说这更像行为修正，但它影响的是生成的时钟正确性、属于功能性变化（不是单纯的 bug 修复也无新接口），与同批次的新功能（TBPKG 标签）一起发布时归入 1.1.0（minor）是合理的。Tagging Policy 里「新增功能升 minor、仅修 bug 升 bugfix」，而这一版同时含新功能，故整体走 minor。

**练习 2**：如果有人问「我用的是 TbGenerator 3.0.2，为什么注释掉的 use 语句没被处理？」你该怎么回答？

> **参考答案**：这正是 3.0.2 修复的内容（不再把注释掉的 library use-clause 当真）。3.0.2 及之后版本都会跳过这类注释，属于预期行为。

---

### 4.3 License.txt 许可说明

#### 4.3.1 概念说明

在动手使用或修改任何开源工具前，必须先看清它的许可证。TbGenerator 采用的是 **PSI HDL Library License**，它本质上 = **LGPL 2.1 + 一条针对硬件/FPGA 场景的例外条款（EXCEPTION NOTICE）**。

为什么需要这条例外？LGPL 本是为「软件库」设计的，而 FPGA 开发里大量产物是「位流文件 / 固件镜像」这类二进制硬件配置。LGPL 的默认条款在这些场景下含义模糊，PSI HDL Library License 的例外条款正是为了澄清：**允许你在自己的条款下使用、复制、链接、修改并以二进制形式（含 FPGA 位流、flash 镜像）分发基于本库的成果。**

#### 4.3.2 核心流程

许可证的三层结构如下：

```
PSI HDL Library License
  ├── 基础：GNU LGPL 2.1（或更高）           ← 见 License.txt 第 11 行
  ├── 例外条款 EXCEPTION NOTICE              ← 见 License.txt 第 15-19 行
  │     1. 额外用途许可
  │     2. 二进制/硬件（含 FPGA 位流）可自由分发
  │     3. 从 GPL/LGPL 代码拷入的部分不享例外
  │     4. 你的修改可选是否保留例外
  └── 配套文件：LGPL2_1.txt（LGPL 全文）
```

简单说：**你可以放心地把 TbGenerator 生成的 testbench 和你的 DUT 一起用于闭源/商业 FPGA 项目；但如果你修改了 TbGenerator 本身的源码并分发，就仍受 LGPL 约束。**

#### 4.3.3 源码精读

许可证开头的版权与许可基础：

> [License.txt:L1-L13](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/License.txt#L1-L13) —— 许可证头部：版权人 Oliver Bründler 等，基础条款为 GNU LGPL（version 2 或更高），按「原样」提供、不承担任何担保。

最关键的例外条款（第 2 条）正是 FPGA 工程师关心的部分：

> [License.txt:L15-L19](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/License.txt#L15-L19) —— 例外条款：允许以二进制形式或包含二进制的硬件（明确包括 FPGA 位流、flash 镜像）按使用者自己的条款使用、复制、链接、修改与分发；但明确排除任何能还原库源码的数据。

README 里也用一句话总结了这一点：

> [README.md:L9-L10](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/README.md#L9-L10) —— License 说明：本库发布于 PSI HDL Library License，即 LGPL 加上若干为固件开发场景澄清条款的例外。

#### 4.3.4 代码实践

**实践目标**：判断在两种典型场景下，你是否需要公开自己修改后的源码。

**操作步骤**：

1. 阅读上面的 [License.txt:L15-L19](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/License.txt#L15-L19) 例外条款第 2、4 条。
2. 对以下两个场景给出判断（开放/不开放）：
   - **场景 A**：你只是用 TbGenerator 生成 testbench，把它和你的 DUT 一起综合成 FPGA 位流并用于商业产品。
   - **场景 B**：你修改了 `TbGen.py` 的生成逻辑，并把修改后的 TbGenerator 工具分发给第三方。

**需要观察的现象**：例外条款第 2 条是否覆盖场景 A；LGPL 基础是否对场景 B 的「源码分发」提出要求。

**预期结果**：场景 A 受例外条款第 2 条保护，位流可自由分发、无需开放；场景 B 属于对库源码本身的修改与分发，受 LGPL 基础约束，需遵守 LGPL 的源码公开义务（除非你按第 4 条选择删除例外声明并另行处理）。

> 提示：本实践是「源码阅读 + 条款理解」型练习，不涉及运行命令，结果靠对许可证文本的解读。

#### 4.3.5 小练习与答案

**练习 1**：PSI HDL Library License 与纯 LGPL 2.1 相比，多出来的核心内容是什么？

> **参考答案**：多了一条 EXCEPTION NOTICE，明确允许把基于本库的成果以二进制形式（含 FPGA 位流、flash 镜像）用于硬件分发，澄清了 LGPL 在固件场景下的模糊地带。

**练习 2**：例外条款第 3 条说「从 GPL/LGPL 代码拷进来的部分不享例外」。这条对实际使用有什么警示？

> **参考答案**：如果你把别处 GPL/LGPL 授权的代码片段直接拷进 TbGenerator 的文件里，那么这部分代码不能享受 PSI 例外的宽松条款，必须按原 GPL/LGPL 条款处理（通常意味着更强的开放义务），并应删除/调整该文件的例外声明以免误导他人。

---

## 5. 综合实践

本讲的综合实践就是规格里要求的核心任务：**通读 README 与 Changelog，写出 3.0.x 相对旧版的关键变化清单，并核验本机依赖。**

### 步骤

1. **阅读** [README.md:L1-L52](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/README.md#L1-L52) 与 [Changelog.md:L1-L50](https://github.com/paulscherrerinstitute/TbGenerator/blob/5bcf59e8370c12117054b0585a2ae0fc4df4e9f4/Changelog.md#L1-L50)。
2. **整理一张「3.0.x 关键变化表」**，至少包含以下几行（请自行补全「影响」列）：

   | 版本 | 关键变化 | 类别 | 影响 |
   | ---- | -------- | ---- | ---- |
   | 3.0.0 | 适配 PsiPyUtils 3.0.0，不再支持 2.x | 不向后兼容 | 待补 |
   | 3.0.1 | 容忍 generic 声明中的注释行 | bugfix | 待补 |
   | 3.0.2 | 不再把注释掉的 library use-clause 当真 | bugfix | 待补 |
   | 3.0.4 | 迁移到 PyQt5，PyQt4 不再支持 | 迁移 | 待补 |

3. **核验本机依赖**，运行：

```bash
python -c "import pyparsing; print('pyparsing', pyparsing.__version__)"
python -c "import PyQt5; from PyQt5.QtCore import QT_VERSION_STR; print('PyQt5 Qt', QT_VERSION_STR)"
```

4. 若要确认 PsiPyUtils 是否就绪（README 要求 ≥ 3.0.0），可执行：

```bash
python -c "import PsiPyUtils; print(getattr(PsiPyUtils, '__version__', '版本未知'))"
```

> 若 PsiPyUtils 是从 `psi_fpga_all` 仓库以源码形式引入的，`__version__` 可能不存在，此时请以上游仓库的版号为准。

### 需要观察的现象

- 3.0.x 系列里，哪些改动是「行为变化」、哪些只是「依赖或框架迁移」。
- 本机 pyparsing / PyQt5 / PsiPyUtils 是否齐备、版本是否达标。

### 预期结果

- 得到一张完整的「3.0.x 关键变化表」，并能用一句话解释每行对使用者的影响。
- 三条依赖检查命令都能成功执行；若某条失败，记录下来并安装对应依赖。

**待本地验证**：版本号以你本机实际输出为准；GUI 依赖 PyQt5 仅在需要图形界面时才必需，纯命令行使用（`TbGen.py`）其实只需要 pyparsing + PsiPyUtils。

## 6. 本讲小结

- **定位**：TbGenerator 是一个用 Python 写的「testbench 骨架自动生成器」，输入是带 `$$ ... $$` 注解的 VHDL DUT 文件，输出是可直接使用的测试台代码。
- **两种形态**：单文件 TB（一个 `.vhd`、无独立用例）与多文件 TB（声明多个 testcase 时，每用例一个 package 文件）。
- **三类依赖**：Python 库 PsiPyUtils（≥ 3.0.0）、外部 pip 包 PyQt5（GUI）与 pyparsing（VHDL 文法解析）。
- **版本演进**：当前 3.0.x；两个分水岭是 2.0.0（首个开源版本）与 3.0.0（依赖 PsiPyUtils 3.0.0、不向后兼容）；3.0.4 完成 PyQt5 迁移。
- **许可**：PSI HDL Library License = LGPL 2.1 + 针对硬件/FPGA 位流的例外条款，允许把生成物用于闭源硬件分发，但对工具源码本身的修改分发仍受 LGPL 约束。
- **仓库结构**：扁平布局，入口为 `TbGen.py`（CLI）与 `TbGenGui.pyw`（GUI），示例在 `example/simpleTb` 与 `example/multiCaseTb`。

## 7. 下一步学习建议

下一篇讲义 **u1-l2《仓库结构与入口文件》** 会带你逐个认识 `TbGen.py`、`TbGenGui.pyw` 等模块，区分 CLI 与 GUI 两种入口的调用方式，并初识 `TbGenerator` 主类——那是你真正开始「读代码」的地方。

如果你想提前热身，可以先在本机把 `python TbGen.py -h` 跑起来，看看 CLI 都提供了哪些参数（这正好是 u1-l2 的实践内容）。
