# 项目总览与位真建模设计理念

> 本讲是 en_cl_fix 学习手册的**第一篇**。它不涉及任何公式推导，也不要求你会写 VHDL 或 MATLAB。读完它，你会清楚这个库**是什么、为什么存在、仓库怎么组织、怎么跑测试、版本号怎么读**——为后续所有讲义打好地基。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 用一句话说清 **en_cl_fix 解决的核心问题**——在 VHDL / MATLAB / Python 三种语言中实现**位真一致（bit-true）**的定点运算。
2. 理解**位真建模 / 协同仿真（co-simulation）**的工作模式：在高级语言里评估算法，再用 VHDL 实现，最后**逐位比对**两者输出。
3. 说出仓库里 `vhdl/`、`python/`、`matlab/`、`sim/`、`doc/` 五个目录各自的职责，以及每种语言源码的**组织方式差异**（单文件包 vs. 多模块 vs. 一函数一文件）。
4. 读懂 `major.minor.bugfix` 版本号策略，并能根据 `Changelog.md` 判断某个版本是新增功能还是只修缺陷。
5. 独立跑通 Python 单元测试，并描述 VHDL 仿真的判定逻辑。

---

## 2. 前置知识

本讲面向零基础读者，但下面几个名词先混个眼熟即可：

- **定点数（fixed-point number）**：跟浮点数相对。它的小数点位置是**固定**的，用一串二进制位表示，硬件（FPGA/ASIC）实现起来比浮点便宜得多。本讲只需要你知道“它是一串位 + 一个约定好的小数点位置”。具体格式 `[S,I,F]` 会在 [u1-l2](u1-l2-fixformat-type.md) 详讲。
- **VHDL**：一种硬件描述语言，用来写最终要综合成电路的逻辑。
- **MATLAB / Python**：高级数值计算语言，适合快速试算法、画图、做仿真。
- **Testbench（测试平台）**：在仿真阶段用来“喂输入、查输出”的一段代码，相当于硬件世界的单元测试。
- **位真（bit-true）**：两种实现对**同一个输入产生完全相同的二进制输出**，连最低位（LSB）都一致。这是本库最核心的理念，下面会反复出现。

> 不必现在就记住定点数细节。本讲只读文档与目录结构，**不碰任何公式**。

---

## 3. 本讲源码地图

本讲主要读**项目说明类文件**，几乎不读算法源码。涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|------|------|-----------|
| [README.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md) | 项目主文档：定位、许可证、维护者、测试入口、定点格式总览 | 几乎每个模块都要引用 |
| [Changelog.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md) | 版本变更历史，按 `major.minor.bugfix` 组织 | 用来讲版本策略与版本历史 |
| [License.txt](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/License.txt) | PSI HDL Library License（LGPL + FPGA 例外条款） | 用来讲许可证特性 |
| [sim/sim.tcl](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl) | Modelsim 仿真脚本：编译 + 跑 testbench + 判定成败 | 用来讲 VHDL 测试流程 |
| [python/unittest/en_cl_fix_pkg_test.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py) | Python 单元测试入口 | 用来讲 Python 测试流程 |

仓库整体结构（用 `git ls-files` 统计的真实文件数）：

```
en_cl_fix/
├── README.md            # 项目主文档
├── Changelog.md         # 版本变更
├── License.txt          # 主许可证（PSI HDL Library License）
├── LGPL2_1.txt          # 配套 LGPL 正文
├── .gitignore
├── vhdl/
│   ├── src/en_cl_fix_pkg.vhd        # 1 个文件：整个 VHDL 实现（单文件包）
│   └── tb/en_cl_fix_pkg_tb.vhd      # 1 个文件：VHDL testbench
├── python/
│   ├── src/en_cl_fix_pkg/           # 4 个文件：types / pkg / wide_fxp / __init__
│   └── unittest/en_cl_fix_pkg_test.py  # 1 个文件：单元测试
├── matlab/
│   └── src/             # 33 个 .m 文件：一个 cl_fix_ 函数对应一个文件
├── sim/sim.tcl          # Modelsim 仿真脚本
└── doc/                 # Doxygen 配置 + 生成的 VHDL HTML 文档（约 118 个文件）
```

记住一个对比：**VHDL 把所有功能塞进一个包文件，Python 按职责拆成几个模块，MATLAB 则一函数一文件**。这种差异源于三种语言的文化习惯，后面会用到。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应 README 里的四块内容：**General Information**、**Simulations and Testbenches**、**Tagging Policy**、**Changelog 版本历史**。

---

### 4.1 General Information：项目定位与位真建模理念

#### 4.1.1 概念说明

en_cl_fix 是一个**定点运算库**。它的特别之处不在于“能做定点运算”，而在于：**同一套算法，用三种语言各实现一遍，且保证位真一致**。

为什么要把一件事做三遍？这要回到硬件开发的现实痛点：

- **算法探索阶段**：工程师想用 MATLAB 或 Python 快速试各种算法、调参数、看波形，这时候要的是**开发快、画图方便**。
- **交付阶段**：最终跑在 FPGA/ASIC 上的必须是 **VHDL**，因为只有它能被综合成真实电路。
- **问题来了**：高级语言里的浮点/高精度参考模型，和 VHDL 里位宽受限、带舍入和饱和的定点实现，**结果很可能不一样**。怎么保证硬件做对了？

en_cl_fix 给出的答案是：**让三种语言共用同一套定点语义**。这样你可以在 MATLAB/Python 里建一个**和 VHDL 完全一致**的参考模型——同样的 `[S,I,F]` 格式、同样的舍入模式、同样的饱和规则——然后把同一份输入喂给两边，**逐位比对输出**。只要位真一致，就能在算法阶段就发现量化误差，并最终确认 VHDL 实现正确。这就是所谓 **bit-true co-simulation（位真协同仿真）**。

> 关键直觉：en_cl_fix 不是“又一个定点库”，而是“**一套跨语言、可互查的位真定点标准**”。

#### 4.1.2 核心流程

位真协同仿真的典型工作流（伪流程）：

```text
        ┌─────────────────────────────┐
        │  1. 在 MATLAB/Python 中      │
        │     用 en_cl_fix 建参考模型  │   ← 算法探索、调参、看量化误差
        └──────────────┬──────────────┘
                       │ 同一份输入数据
                       ▼
        ┌─────────────────────────────┐
        │  2. 在 VHDL 中用 en_cl_fix   │
        │     实现同样逻辑             │   ← 交付到硬件
        └──────────────┬──────────────┘
                       │ 同一份输入数据
                       ▼
        ┌─────────────────────────────┐
        │  3. 逐位比对两路输出         │   ← 位真一致 = 实现正确
        └─────────────────────────────┘
```

三个角色分工：

| 角色 | 语言 | 主要用途 |
|------|------|---------|
| 参考模型 | Python / MATLAB | 快速评估、量化误差分析、产生期望输出 |
| 交付实现 | VHDL | 综合成真实电路 |
| 比对依据 | 三者位真一致 | 让“比对”这件事有意义 |

#### 4.1.3 源码精读

README 的开篇 `General Information` 一句话点明了整个项目存在的理由：

- [README.md:1-3](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L1-L3) —— 项目定位原文。注意这一句里强调了 *“evaluate the behavior … in a high-level language … while implementing it later in VHDL”* 和 *“bit-true models … comparing outputs of both implementations”*，这正是上面流程图的两个支柱。

> 小提醒：README 第 3 行正文里只点名了 VHDL 和 MATLAB，但仓库里**实际还有一份 Python 实现**（见 `Dependencies` 与 `Simulations` 两节，以及 `python/` 目录）。可以把 Python 理解为“第三个、同样位真的高级语言参考模型”。三语言并存的现状会在 Changelog 里再次得到印证。

许可证与维护者同样在 `General Information` 下：

- [README.md:5-6](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L5-L6) —— 许可证是 **PSI HDL Library License**，本质是 **LGPL 加上针对固件/FPGA 比特流的例外条款**（详见 4.1.4 的实践）。
- [README.md:8-9](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L8-L9) —— 维护者为 Martin Heimlicher（Enclustra）。
- [README.md:14-15](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L14-L15) —— VHDL 的详细文档由 **Doxygen** 生成，对应仓库里 `doc/` 目录。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手验证“三语言位真”这一说法。

1. **实践目标**：确认仓库里确实存在三套实现，并理解许可证的特殊性。
2. **操作步骤**：
   - 用 `git ls-files 'vhdl/src/*'`、`git ls-files 'python/src/*'`、`git ls-files 'matlab/src/*'` 分别列出三种语言的源文件。
   - 打开 [License.txt:15-19](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/License.txt#L15-L19)，阅读 **EXCEPTION NOTICE** 第 2 条。
3. **需要观察的现象**：
   - VHDL 只有 1 个源文件 `en_cl_fix_pkg.vhd`；Python 在 `en_cl_fix_pkg/` 下有 4 个模块文件；MATLAB 在 `src/` 下有 33 个 `.m` 文件。
   - License 例外条款明确允许把库**以二进制形式（包括 FPGA 比特流）**闭源分发，这是普通 LGPL 不允许的。
4. **预期结果**：三套实现并存；许可证对硬件交付友好。
5. **运行结果**：待本地验证（列出命令在你机器上的实际输出）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能直接用 MATLAB 自带的 `double` 浮点数当 VHDL 定点实现的参考模型？
> **参考答案**：因为浮点没有“位宽限制 + 舍入 + 饱和”，它的结果和定点实现**天然不一致**，无法逐位比对。en_cl_fix 让 MATLAB/Python 端也用相同的定点语义，才能做到位真。

**练习 2**：PSI HDL Library License 相比纯 LGPL 多了什么？为什么这对 FPGA 项目重要？
> **参考答案**：多了 EXCEPTION NOTICE，允许把包含该库的**二进制（含 FPGA 比特流、flash 镜像）**按自己的条款分发。FPGA 综合后的比特流属于二进制，纯 LGPL 对此规定模糊，例外条款消除了这个法律风险。

---

### 4.2 Simulations and Testbenches：三种语言的测试入口与目录结构

#### 4.2.1 概念说明

位真一致不是“声称”出来的，而是**测**出来的。README 的 `Simulations and Testbenches` 一节给出了三种语言的测试入口。这里有个重要事实先记住：

- **Python**：有完整的单元测试，可以直接跑。
- **VHDL**：有 testbench，通过 Modelsim 脚本自动判定成败。
- **MATLAB**：**目前没有测试**（README 原文：*Currently there are not tests for the MATLAB implementation*）。

也就是说，“三语言位真”目前的**自动化验证覆盖 Python 与 VHDL**；MATLAB 更多作为手工参考模型使用。

#### 4.2.2 核心流程

三种语言的测试流程对比：

| 语言 | 入口 | 关键动作 | 自动判定成败？ |
|------|------|---------|---------------|
| Python | `python/unittest/en_cl_fix_pkg_test.py` | `unittest` 断言（`assertEqual` / `assertWarns`） | 是（unittest 框架） |
| VHDL | `sim/sim.tcl`（在 Modelsim 里 `source`） | `vcom` 编译 → `vsim` 仿真 → 扫描 transcript | 是（脚本扫 `###ERROR###` / `Fatal:`） |
| MATLAB | 无 | —— | 否（无测试） |

Python 测试的组织方式很规整：**按被测函数分组**，每组一个 `unittest.TestCase` 子类。例如第一个类专门测 `cl_fix_width`：

- [en_cl_fix_pkg_test.py:16-38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L16-L38) —— `cl_fix_width_Test` 类，每个 `test_*` 方法断言一个格式对应的位宽。

注意文件开头这两行，它决定了**必须从 `python/unittest` 目录内运行**测试：

- [en_cl_fix_pkg_test.py:6-8](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L6-L8) —— 通过 `sys.path.append("../src")` 把上一级 `src` 加入搜索路径，再 `from en_cl_fix_pkg import *`。这个**相对路径**就是 README 要求“先 navigate 到 `python/unittest`”的原因。

VHDL 这边，`sim.tcl` 把“编译 → 跑 → 判定”串成一条流水线：

- [sim.tcl:7-14](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L7-L14) —— 用 `vcom -2008` 编译源文件与 testbench，再 `vsim … run -all` 跑完所有测试。
- [sim.tcl:16-28](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/sim/sim.tcl#L16-L28) —— 读取 `Transcript.transcript`，用正则（不区分大小写）查找 `###ERROR###` 或 `Fatal:`，命中则打印 *“ERRORS OCCURED”*，否则打印 *“SIMULATIONS COMPLETED SUCCESSFULLY”*。

#### 4.2.3 源码精读

README 对应的测试入口说明：

- [README.md:28-39](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L28-L39) —— 三种语言的测试方式原文。注意 Python 一栏要求 `python3 en_cl_fix_pkg_test.py`，VHDL 一栏要求在 Modelsim TCL 控制台 `source ./sim.tcl`，MATLAB 一栏明确写“无测试”。

Python 依赖单独列在一节：

- [README.md:24-26](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L24-L26) —— Python 实现依赖 **numpy**。原因是 Python 端函数对 `numpy.ndarray` 做向量化运算（一次处理整个数组），这能让仿真数据批量跑得很快。这个细节会在 [u2-l1](u2-l1-python-package-tests.md) 展开。

#### 4.2.4 代码实践

1. **实践目标**：跑通 Python 单元测试，亲眼看到“全绿”。
2. **操作步骤**：
   - 确保装了 numpy：`python3 -c "import numpy"`（不报错即可）。
   - **进入** `python/unittest` 目录（因为测试脚本用了相对路径 `../src`），再执行 `python3 en_cl_fix_pkg_test.py`。
3. **需要观察的现象**：终端会打印 `unittest` 的标准结果——每个 `test_*` 方法一个点（`.` 表示通过），最后给出类似 `OK (ran N tests)` 的汇总行。
4. **预期结果**：全部测试通过，结尾显示 `OK`。
5. **运行结果**：待本地验证（具体跑过的用例数 `N` 以你本机输出为准）。

> 反面提醒：如果你**不在** `python/unittest` 目录里运行，`../src` 会指向错误位置，报 `ModuleNotFoundError: No module named 'en_cl_fix_pkg'`。这正是相对路径带来的常见坑。

#### 4.2.5 小练习与答案

**练习 1**：为什么测试脚本要用 `sys.path.append("../src")` 而不是直接 `import en_cl_fix_pkg`？
> **参考答案**：因为 `en_cl_fix_pkg` 包不在 Python 的默认搜索路径里，它在上一级的 `src/` 目录下。`append("../src")` 临时把该目录加入搜索路径，`import` 才能成功。也正因如此，脚本必须从 `python/unittest` 目录运行。

**练习 2**：`sim.tcl` 是怎么判断 VHDL 仿真“成功”的？它依赖什么关键字？
> **参考答案**：它读取仿真产生的 `Transcript.transcript`，用不区分大小写的正则查找 `###ERROR###` 或 `Fatal:`。两者都不出现才打印 `SIMULATIONS COMPLETED SUCCESSFULLY`；只要出现一个，就判定失败。

**练习 3**：MATLAB 实现没有测试，这会动摇“三语言位真”的承诺吗？
> **参考答案**：不根本动摇，但降低了自动化保障。位真一致目前由 Python 与 VHDL 的测试把关；MATLAB 作为手工参考模型，依赖代码层面遵循同样的算法（同样的舍入到无穷等）。这也是后续讲义会反复对照三种实现的原因。

---

### 4.3 Tagging Policy：语义化版本号策略

#### 4.3.1 概念说明

en_cl_fix 用一套类似 **SemVer（语义化版本）** 的策略给发布打标签：`major.minor.bugfix`。它的价值在于：**只看版本号的变化，就能判断升级会不会破坏老代码**。这对把库综合进 FPGA 的工程师特别重要——重新综合一次成本很高，谁都不想因为升了个小版本而踩到不兼容。

#### 4.3.2 核心流程

版本号三位各自的“ bumped（递增）”条件（高位递增时，低位归零，这是惯例）：

| 变化类型 | 递增哪一位 | 含义 | 升级风险 |
|---------|-----------|------|---------|
| **不完全向后兼容** | `major` | 接口/行为破坏性变更 | 高，需改代码 |
| **新增功能** | `minor` | 加了新能力，旧用法不变 | 中，理论向后兼容 |
| **只修缺陷** | `bugfix` | 无功能变化，仅修 bug | 低，建议跟进 |

用数学化一点的表述：设版本为

\[
\text{version} = (\text{major},\ \text{minor},\ \text{bugfix})
\]

则一次发布只改动**一个**最高有效位：破坏兼容动 `major`，否则有新功能动 `minor`，否则动 `bugfix`。

#### 4.3.3 源码精读

策略原文写在 README 的 `Tagging Policy` 一节：

- [README.md:17-22](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L17-L22) —— 三条规则原文：破坏兼容升 `major`、新增功能升 `minor`、仅修缺陷升 `bugfix`。

用 `git tag` 查到的真实标签也印证了这套策略（当前仓库从 `1.0.0` 一路到 `1.2.0`）：

```text
1.0.0  1.1.0  1.1.1  1.1.2  1.1.3  1.1.4
1.1.5  1.1.6  1.1.7  1.1.8  1.2.0
```

可以看到：`1.0.0 → 1.1.0`（minor 跳，新增 Python 与 testbench）、`1.1.x` 连串（只修缺陷）、`1.1.8 → 1.2.0`（minor 跳，新增大位宽支持）。当前 HEAD `7f7aa80` 在 `1.2.0` 之后又前进了几格（`git describe` 显示 `1.2.0-4-g7f7aa80`），尚未打成新标签。

#### 4.3.4 代码实践

1. **实践目标**：用 git 工具验证版本策略，把“规则”落到“真实标签”。
2. **操作步骤**：
   - 运行 `git tag` 列出所有发布标签。
   - 运行 `git describe --tags` 看当前 HEAD 相对最近标签的位置。
   - 选一个 `minor` 跳变（如 `1.1.8 → 1.2.0`），用 `git log --oneline 1.1.8..1.2.0` 查看该区间提交，对照 Changelog 确认“确实是新增功能”。
3. **需要观察的现象**：标签按字典序排列成一条递增序列；`git describe` 输出形如 `1.2.0-4-g<短哈希>`，表示 HEAD 在 `1.2.0` 之后第 4 个提交。
4. **预期结果**：`git log 1.1.8..1.2.0` 的提交信息能与 [Changelog.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md) 1.2.0 条目（如 wide fixed-point 支持）对上。
5. **运行结果**：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：某次升级版本号从 `1.1.4` 变到 `1.1.5`，你能推断这次升级的性质吗？
> **参考答案**：`bugfix` 位递增，说明**只修缺陷、无功能变化**，升级风险最低，且理论向后兼容。

**练习 2**：为什么把“破坏向后兼容”单独留给 `major`，而不是混在 `minor` 里？
> **参考答案**：为了让使用者**只看版本号就能判断升级安全性**。破坏性变更必须显眼，否则工程师可能在不重新综合/不回归测试的情况下踩坑。把它隔离到 `major`，等于强制所有人意识到“这次改动可能要改代码”。

---

### 4.4 Changelog 版本历史

#### 4.4.1 概念说明

`Changelog.md` 是版本策略的**执行记录**：每个版本一段，分成 **Features（新功能）** 和 **Bugfixes（缺陷修复）** 两组。它和 `Tagging Policy` 是一对——策略规定“该怎么编号”，Changelog 记录“实际改了什么”。读 Changelog 是快速理解项目演进脉络的最便宜方式。

#### 4.4.2 核心流程

阅读 Changelog 的正确姿势（由新到旧）：

```text
最新版本（在最上面）
   ├─ Features   ──→ 对应一次 minor 或 major 跳变
   └─ Bugfixes   ──→ 对应一次 bugfix 跳变（或附属于上面那次发布）
…
最早版本 1.0.0（在最下面）──→ VHDL + MATLAB 首次发布
```

要点：**最新版本在最上方**；`Features: None` 表示该版本只有缺陷修复；`Bugfixes: None` 表示该版本只有新功能。

#### 4.4.3 源码精读

最新版本 1.2.0，带来一个重要新特性：

- [Changelog.md:1-9](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md#L1-L9) —— 1.2.0 条目。其中 *“Added wide fixed-point (> 53 bits) Python support”* 直接呼应了仓库里 `python/src/en_cl_fix_pkg/wide_fxp.py` 这个文件的存在（>53 位要用任意精度整数，IEEE754 双精度只有 53 位尾数）。这是 [u6-l1](u6-l1-narrow-wide-dispatch.md) 的伏笔。

Python 实现是什么时候加入的？往前翻到 1.1.0：

- [Changelog.md:69-75](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md#L69-L75) —— 1.1.0 条目：*“Added Python Implementation incl. Unit-Test”* 和 *“Added Testbench for VHDL Implementation”*。这说明 **Python 实现 + VHDL testbench 是同一次 minor 升级一起到来的**，两套测试在历史上是配套建设的——正好支撑了“Python 与 VHDL 互查位真”的工作流。

最早版本：

- [Changelog.md:77-78](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md#L77-L78) —— 1.0.0 条目：*“First Release containing VHDL and MATLAB implementations”*。所以项目最初只有 VHDL 和 MATLAB 两套，Python 是 1.1.0 才补上的第三套（这也解释了 4.1 里 README 正文只点名两语言、但仓库实际有三语言的历史原因）。

#### 4.4.4 代码实践

1. **实践目标**：用 Changelog 拼出项目的“语言演进时间线”。
2. **操作步骤**：
   - 打开 [Changelog.md](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/Changelog.md)，自上而下通读全部条目。
   - 标注出三个里程碑：1.0.0（VHDL+MATLAB 首发）、1.1.0（加入 Python 与 VHDL testbench）、1.2.0（加入 >53 位 Python 支持）。
3. **需要观察的现象**：多个 `1.1.x` 版本的 Features 都是 `None`，只有 Bugfixes——印证了它们都是 `bugfix` 级发布。
4. **预期结果**：你能用一句话回答“Python 实现从哪个版本开始有”“大位宽支持从哪个版本开始有”。
5. **运行结果**：纯文档阅读，无需运行；结论可直接在 Changelog 中核实。

#### 4.4.5 小练习与答案

**练习 1**：1.1.5 到 1.1.8 这几个版本的共同特征是什么？它们为什么只动 `bugfix` 位？
> **参考答案**：它们的 Features 都是 `None`，只有 Bugfixes（多为修复 >31 位大数处理的各类问题）。因为只修缺陷、无功能变化，按策略只递增 `bugfix` 位。

**练习 2**：某用户现在依赖 1.2.0 的 wide fixed-point 功能，他最低能用到哪个版本？为什么？
> **参考答案**：最低 1.2.0。因为 wide fixed-point（>53 位 Python 支持）是 1.2.0 才加入的 Features；1.1.x 没有这个能力。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**仓库勘察**任务（这是本讲规格里指定的综合实践）：

> **任务**：克隆仓库后，列出 `vhdl/src`、`python/src/en_cl_fix_pkg`、`matlab/src` 三个目录中的源文件数量与命名规律，并在 README 中找到 Python 依赖与三种语言测试入口，最后写一段约 100 字的中文总结，说明 en_cl_fix 解决的核心问题。

建议步骤：

1. **统计文件**：
   ```bash
   git ls-files 'vhdl/src/*' | wc -l        # 预期 1
   git ls-files 'python/src/en_cl_fix_pkg/*' # 预期 4 个文件
   git ls-files 'matlab/src/*' | wc -l       # 预期 33
   ```
2. **归纳命名规律**：
   - VHDL：单文件包 `en_cl_fix_pkg.vhd`，所有功能集中于此。
   - Python：按职责拆成 `en_cl_fix_types.py`（类型）、`en_cl_fix_pkg.py`（主函数）、`wide_fxp.py`（大位宽）、`__init__.py`（统一导出）。
   - MATLAB：每个 `cl_fix_<功能>.m` 对应一个公开函数，呈扁平“一函数一文件”结构。
3. **定位入口**（在 README 里）：
   - Python 依赖：`numpy`（[README.md:24-26](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L24-L26)）。
   - 三语言测试入口（[README.md:28-39](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L28-L39)）：Python 跑 `python3 en_cl_fix_pkg_test.py`；VHDL 在 Modelsim 里 `source ./sim.tcl`；MATLAB 无测试。
4. **写总结**（示例方向，请用自己的话改写）：
   > en_cl_fix 是一套跨 VHDL / MATLAB / Python 的**位真定点运算库**，三种语言实现同一套算法、保持逐位一致。它让你在高级语言里评估算法与量化误差，再用 VHDL 实现并通过比对输出验证正确性，是硬件定点算法“先建模、后实现、再互查”的标准基础设施。

完成后，你应当能脱稿回答：**这个库是什么、为什么是三语言、各自怎么测、版本号怎么读**。

---

## 6. 本讲小结

- en_cl_fix 是一套**跨 VHDL / MATLAB / Python 的位真定点运算库**，核心价值在于“同一套算法、三种实现、逐位一致”。
- **位真协同仿真**是它的设计动机：在 MATLAB/Python 建参考模型，在 VHDL 实现，再用相同输入逐位比对输出，从而在算法阶段就发现量化误差并最终确认硬件正确。
- 仓库按语言分目录：`vhdl/`（单文件包 + testbench）、`python/`（4 模块 + 单元测试）、`matlab/`（33 个一函数一文件）、`sim/`（Modelsim 脚本）、`doc/`（Doxygen 输出）。
- 测试覆盖**Python（unittest）与 VHDL（sim.tcl 自动扫 `###ERROR###`/`Fatal:`）**；MATLAB 目前无测试。
- 版本号遵循 `major.minor.bugfix`：破坏兼容升 major、新增功能升 minor、仅修缺陷升 bugfix；真实标签从 `1.0.0` 到 `1.2.0` 印证了策略。
- `Changelog.md` 记录了演进脉络：1.0.0 首发 VHDL+MATLAB，1.1.0 补上 Python 与 VHDL testbench，1.2.0 加入 >53 位 wide fixed-point 支持。

---

## 7. 下一步学习建议

本讲只读了文档与目录，**还没碰任何定点数本身**。建议按以下顺序继续：

1. **下一讲 [u1-l2：定点数格式 [S,I,F] 与 FixFormat 类型](u1-l2-fixformat-type.md)**：这是理解整个库的真正起点——学会用 `(Signed, IntBits, FracBits)` 描述任意定点格式，并在三种语言里找到对应类型定义。
2. 之后依次学习 [u1-l3 格式辅助函数](u1-l3-format-helpers.md)、[u1-l4 舍入模式](u1-l4-rounding-modes.md)、[u1-l5 饱和模式](u1-l5-saturation-modes.md)，把“格式—舍入—饱和”三大基础概念打牢。
3. 基础完备后，再进入 Unit 2 **真正跑测试**（[u2-l1](u2-l1-python-package-tests.md) Python、[u2-l2](u2-l2-vhdl-sim-testbench.md) VHDL、[u2-l3](u2-l3-matlab-model.md) MATLAB），亲手体会“位真一致性”。

> 阅读建议：本系列每一篇都会给出真实源码的永久链接（带 HEAD 哈希 `7f7aa80`）与行号，鼓励你**边读讲义边点开链接对照原文**，形成“讲义 ↔ 源码”的双向印证习惯。
