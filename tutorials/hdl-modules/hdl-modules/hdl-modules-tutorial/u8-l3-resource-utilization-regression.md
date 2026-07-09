# 资源占用回归：netlist 构建与检查器

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚「为什么要把资源占用做成 CI 回归」——它解决了什么类型的回归 bug。
2. 读懂任意一个 `module_*.py` 中的 `get_build_projects()`，知道它如何声明一个个 netlist 构建工程。
3. 理解 `build_result_checker` 里的 `EqualTo` / `TotalLuts` / `Ffs` / `Ramb36` / `Ramb18` / `MaximumLogicLevel` 各自断言什么，以及为什么资源数是**精确等于**而不是「小于某个上限」。
4. 解释 `fifo_netlist_build_wrapper` 这类「最小化顶层 wrapper」为什么存在——它如何把无关端口隔离掉，只留下被测特性。
5. 能根据一组 generic 取值预测资源变化的方向，并知道如何用 `tools/build_fpga.py` 跑一次 netlist 构建来验证。

## 2. 前置知识

本讲是专家层（advanced），假设你已经学过：

- **u1-l4**：`module_*.py` 的两个钩子——`setup_vunit`（仿真登记，u8-l2 讲过）和 `get_build_projects`（综合登记，本讲主角）。`BaseModule` 提供的 `self.library_name`、`self.add_vunit_config`、`self.netlist_build_name` 等辅助方法。
- **u4-l1 / u4-l2**：FIFO 的各个 generic（`enable_last`、`enable_packet_mode`、`enable_drop_packet`、`enable_peek_mode`、`enable_output_register`、`almost_full_level`）以及同步/异步 FIFO 的差别。本讲会反复用 FIFO 作为案例，因为它的 generic「逐层叠加」恰好把资源代价表成了一张阶梯图。

如果你对 FPGA 综合还比较陌生，先记住两个名词：

- **netlist（网表）**：VHDL 经过「综合（synthesis）」后得到的最底层逻辑网表，对应 Vivado 流程里的 `synth_design`。netlist 构建只跑综合、**不跑实现（implementation，即布局布线）**，所以快得多，适合反复跑做设计反馈。
- **资源占用（resource utilization）**：综合后工具报告用了多少基本原语——LUT（查找表）、FF（触发器）、RAMB36/RAMB18（块 RAM）、以及**逻辑级数（logic levels）**。逻辑级数是关键路径上串联的组合逻辑层数，层数越多、能跑到的频率越低。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `modules/fifo/module_fifo.py` | 本讲主角。`get_build_projects()` 声明了十几个 netlist 构建工程，每个工程都挂了一组 `build_result_checker` 断言。 |
| `modules/fifo/rtl/fifo_netlist_build_wrapper.vhd` | 「最小化顶层 wrapper」。只把 FIFO 的 barebone（裸接口）端口接出来，供资源断言用。 |
| `tools/build_fpga.py` | 构建入口脚本。调用 tsfpga 的 `get_build_projects()` 收集所有模块声明的工程，再用 Vivado 跑综合/实现。 |
| `modules/common/module_common.py` | 另一个范例。它的 `handshake_pipeline` 构建同样用 `EqualTo` 断言，可交叉对照。 |

> 说明：`build_result_checker`、`TsfpgaExampleVivadoNetlistProject`、`netlist_build_name` 都来自外部依赖 **tsfpga**（仓库外，需另装）。本讲依据项目中对它们的**真实调用方式**讲解其语义；涉及 tsfpga 内部实现细节处会标注「待确认」。

## 4. 核心概念与源码讲解

### 4.1 netlist 资源回归的动机与整体机制

#### 4.1.1 概念说明

hdl-modules 反复强调「资源占用始终是 FPGA 项目的关键指标」（见 [readme.rst](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/readme.rst)）。很多模块用 generic 开关特性，初衷就是「不开的功能零资源占用」（u1-l1）。

但这就带来一个隐患：**重构代码时，很容易意外让某个「本该免费」的特性变贵**。例如：

- 某次重构后，`enable_last`（FIFO 的包尾标记）原本只让 RAM 字宽 +1、几乎免费，结果却多消耗了 10 个 LUT——没人发现，直到客户板子资源不够。
- 某次优化后，关键路径逻辑级数从 6 变成 12，FIFO 跑不到目标频率——同样没人发现。

**资源回归（resource utilization regression）** 就是把这类回归当成 bug 来防：对每一个「有意思的 generic 组合」综合出一个 netlist，然后**断言它的资源数精确等于一个已知正确的期望值**。一旦代码改动让资源数偏离期望，构建立即失败，就像断言失败的单元测试一样。

这就是为什么本讲标题叫「回归」——它和单元测试的定位一致，只不过断言的不是功能，而是**面积/时序的数字**。

#### 4.1.2 核心流程

netlist 资源回归的整体链路：

```text
tools/build_fpga.py --netlist-builds
        │
        ▼
tsfpga.get_build_projects(modules, project_filters, include_netlist_not_full_builds=True)
        │   遍历每个模块，调用 module.get_build_projects()
        ▼
module_fifo.py: get_build_projects()
        │   返回一组 TsfpgaExampleVivadoNetlistProject
        │   每个 project 挂了 build_result_checkers=[...]
        ▼
Vivado 对每个工程跑 synth_design（只综合，不实现）
        │
        ▼
tsfpga 读取综合后的 utilization 报告
        │
        ▼
对每个 checker（如 TotalLuts(EqualTo(14))）做断言：
        实际 LUT 数 == 14 ？  否则 → 该构建失败（CI 红）
```

两个关键设计选择：

1. **只跑综合（netlist），不跑实现**：netlist 构建在 tsfpga 里被标记为「not full build」，用 `--netlist-builds` 开关拉起。它快、且资源数在综合阶段就已经确定（实现阶段的布局布线不会改变用了多少 LUT/FF/RAM）。
2. **断言是「精确等于」而非「小于上限」**：这迫使作者每次合理改动都要**主动更新**期望值。如果只写「LUT < 100」，那任何让资源慢慢爬升的回归都不会被发现——这正是回归测试要杜绝的。

#### 4.1.3 源码精读

构建入口 [tools/build_fpga.py:27-43](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L27-L43) 做的事很简洁：

- 用 tsfpga 的 `arguments()` 解析命令行（其中包含 `--netlist-builds`、`--project-filters` 等标准开关，由 tsfpga 提供，待本地确认具体拼写）。
- `get_modules(...)` 扫描 `modules/` 得到所有模块对象。
- `get_build_projects(modules=..., project_filters=..., include_netlist_not_full_builds=args.netlist_builds)` 收集工程——注意 `include_netlist_not_full_builds` 正是由 `--netlist-builds` 决定的，它控制「只综合的 netlist 工程」是否被纳入本次运行。
- 最后 `setup_and_run(...)` 用 Vivado 并行跑这些工程。

```python
project_list = BuildProjectList(
    projects=get_build_projects(
        modules=modules,
        project_filters=args.project_filters,
        include_netlist_not_full_builds=args.netlist_builds,
    )
)
```

> 关于 CI：仓库公开的 [.github/workflows/ci.yml](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/.github/workflows/ci.yml) 目前跑的是仿真（带 `--vivado-skip`，用 GHDL）。netlist 资源回归**依赖有授权的 Vivado**，通常在本地或带 Vivado 的环境里用 `tools/build_fpga.py --netlist-builds` 运行。

#### 4.1.4 代码实践

**实践目标**：在不装 Vivado 的前提下，确认 netlist 构建链路「能被列出来」。

**操作步骤**：

1. 阅读 [tools/build_fpga.py:20-24](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/build_fpga.py#L20-L24) 的 import，确认 `BuildProjectList`、`get_build_projects`、`get_modules` 都来自 tsfpga。
2. 在已装好 tsfpga/Vivado 的环境里运行（待本地验证）：
   ```bash
   python3 tools/build_fpga.py --netlist-builds --list-only
   ```
   （`--list-only` 是 tsfpga 标准参数，待确认；若不存在，可改用 `--project-filters fifo` 先只列 fifo 的工程。）

**需要观察的现象**：会打印出一长串工程名，形如 `fifo.minimal.width_32.depth_1024....`、`fifo.with_packet_mode....`、`asynchronous_fifo.resync_fifo....`。

**预期结果**：fifo 这一个模块就贡献了十几个 netlist 工程，每个对应一组 generic。这说明「资源回归」的覆盖面其实很细。

#### 4.1.5 小练习与答案

**练习 1**：为什么 netlist 构建只跑综合就够了，不需要跑实现（布局布线）就能断言 LUT/FF/RAM 数量？

**参考答案**：综合把 RTL 映射成 LUT/FF/RAM 等基本原语，**资源数量在这一步就已确定**。实现（布局布线）只决定这些原语摆在芯片哪里、连线多长，不会改变「用了多少个 LUT」。所以只综合就能拿到准确的 utilization 数字，且省下耗时的布局布线。

**练习 2**：如果有人把断言从 `TotalLuts(EqualTo(14))` 改成 `TotalLuts(LessThan(100))`，会失去什么？

**参考答案**：会失去「检测缓慢爬升」的能力。`EqualTo` 强制每次合理改动都主动更新期望值，资源一旦偏离立刻报警；`LessThan(100)` 则允许资源在 100 以内任意漂移，某次重构让 LUT 从 14 涨到 60 也照样通过，回归被静默吞掉。

---

### 4.2 get_build_projects 钩子：声明 netlist 构建工程

#### 4.2.1 概念说明

回忆 u1-l4：每个 `module_*.py` 里有一个继承 `BaseModule` 的 `Module` 类，提供两个钩子——`setup_vunit`（仿真，u8-l2）和 `get_build_projects`（综合，本讲）。两者由不同入口、在不同时机触发，互不依赖。

`get_build_projects()` 的职责非常单一：**返回一个 netlist 构建工程的列表**。它不跑综合、不读报告，只负责「声明」——「我想综合这样一组配置，并断言它用这么多资源」。真正的综合由 `tools/build_fpga.py` 在收集完所有模块的工程后统一执行。

#### 4.2.2 核心流程

以 fifo 为例，`get_build_projects()` 的内部结构：

```text
get_build_projects()
  ├── 设定目标器件 part = "xc7z020clg400-1"
  ├── get_hdl_modules(names_include=[fifo, common, math, resync])
  │       # 把本模块 + 它依赖的模块收集起来作为可用源码
  ├── _setup_fifo_build_projects(...)      # 同步 FIFO 的一组工程
  └── _setup_asynchronous_fifo_build_projects(...)  # 异步 FIFO 的一组工程
  return projects   # 一个 list[TsfpgaExampleVivadoNetlistProject]
```

每个工程对象 `TsfpgaExampleVivadoNetlistProject` 至少携带这些信息：

| 字段 | 含义 |
| --- | --- |
| `name` | 工程名，用 `self.netlist_build_name(...)` 生成，**包含 generic 取值以保证唯一**。 |
| `modules` | 这次综合要纳入哪些模块的源码（本模块 + 依赖）。 |
| `part` | 目标 FPGA 器件号。**资源数依赖器件**，所以必须钉死。 |
| `top` | 顶层实体名（如 `"fifo"` 或 `"fifo_netlist_build_wrapper"`）。 |
| `generics` | 这组 generic 取值。 |
| `build_result_checkers` | 一组资源断言（下一节详述）。 |

#### 4.2.3 源码精读

钩子本体 [modules/fifo/module_fifo.py:116-130](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L116-L130)：

```python
def get_build_projects(self) -> list[TsfpgaExampleVivadoNetlistProject]:
    from hdl_modules import get_hdl_modules  # 局部 import（见下文解释）

    projects = []
    modules = get_hdl_modules(names_include=[self.name, "common", "math", "resync"])
    part = "xc7z020clg400-1"

    self._setup_fifo_build_projects(projects, modules, part)
    self._setup_asynchronous_fifo_build_projects(projects, modules, part)

    return projects
```

几个细节值得注意：

- **局部 import**：`from hdl_modules import get_hdl_modules` 写在函数体内而非文件顶部，注释解释了原因——「hdl_modules 这个 Python 包在大多数使用场景下并不在 PYTHONPATH 上」（当别人把 fifo 模块拷进自己的工程时）。`get_build_projects` 只在本仓库用 `tools/build_fpga.py` 跑 netlist 构建时才会被调用（那时 PYTHONPATH 已正确设置），所以延迟到调用时再 import 是安全的。这正是 u1-l4 提到的「兼容未装 tsfpga 的用户」的同款手法。
- **依赖收集**：`names_include=[self.name, "common", "math", "resync"]` 显式声明 fifo 依赖哪些兄弟模块。异步 FIFO 需要 `resync`（指针跨域，u4-l2）、`common`（基础包）、`math`（位宽计算），所以一并纳入；少了任何一个，综合时会因找不到库而报错。
- **器件钉死**：`part = "xc7z020clg400-1"`（Xilinx Zynq-7000）。换器件，LUT/RAM 的原语定义不同，资源数会变——所以下面所有 `EqualTo` 的数字都是**针对这个器件**标定的。`tools/synthesize.py`（u9-l2）综合时用的是另一个器件 `xcku5p-sfvb784-3-e`，故两者资源数不可直接比较。

`self.netlist_build_name(...)` 是 `BaseModule` 提供的辅助方法（tsfpga），它把一个易读的名字 + generic 字典组合成一个唯一、确定性的工程名（例如 `fifo.minimal.width_32.depth_1024.use_asynchronous_fifo_false....`），既保证两次运行同名（便于缓存），又保证不同 generic 组合不撞名。

#### 4.2.4 代码实践

**实践目标**：跟踪「一个模块声明了多少个 netlist 工程」。

**操作步骤**：

1. 打开 [modules/fifo/module_fifo.py:132](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L132) 起的 `_setup_fifo_build_projects`。
2. 数一数里面调用了几次 `projects.append(TsfpgaExampleVivadoNetlistProject(...))`。
3. 同样地，到 [modules/fifo/module_fifo.py:326](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L326) 起的 `_setup_asynchronous_fifo_build_projects` 里数异步部分（注意里面的 `add_resync_config` 循环会被调用多次）。

**需要观察的现象**：同步部分约 9 个工程，异步部分约 10 个（含 5 个 `resync_fifo` 宽度配置）。

**预期结果**：单 fifo 模块就声明了约 19 个 netlist 构建工程，每个都带资源断言。可见作者把「generic 逐层叠加」拆成了很多个独立的断言点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `get_build_projects` 把 `from hdl_modules import get_hdl_modules` 写在函数体内，而不是文件顶部？

**参考答案**：因为当其他用户把 fifo 模块拷进自己的工程时，`hdl_modules` Python 包通常不在他们的 PYTHONPATH 上。把 import 放在函数体内，使得「只读 module_fifo.py 而不跑 netlist 构建」的场景（例如别人只想要 VHDL 源码）不会因为找不到 `hdl_modules` 而崩溃。该函数只在本仓库用 `tools/build_fpga.py` 跑构建时才被调用，那时 PYTHONPATH 已就绪。

**练习 2**：如果异步 FIFO 漏写了 `resync` 到 `names_include`，构建会在哪一步失败？

**参考答案**：会在综合阶段失败——Vivado 报告找不到 `resync` 库（因为 `asynchronous_fifo` 内部实例化了 `resync_counter` 等实体）。`names_include` 决定了哪些模块的源码被加入工程，缺了依赖就编译/链接不到。

---

### 4.3 build_result_checker：把资源数变成断言

#### 4.3.1 概念说明

`build_result_checker`（来自 tsfpga 的 `tsfpga.vivado.build_result_checker`）是把「Vivado utilization 报告里的一个数字」变成「一个布尔断言」的胶水。它由两部分组合：

- **资源类型**（度量什么）：`TotalLuts`（总 LUT）、`Ffs`（触发器）、`Ramb36` / `Ramb18`（两种容量的块 RAM）、`MaximumLogicLevel`（最大逻辑级数，时序指标）。
- **比较算子**（怎么比）：本仓库只用 `EqualTo(N)`——精确等于 N。

于是 `TotalLuts(EqualTo(14))` 读作「断言总 LUT 数精确等于 14」。一个工程通常挂 5 个这样的 checker，分别钉死 LUT、FF、RAMB36、RAMB18、逻辑级数。

#### 4.3.2 核心流程

fifo 的同步构建把 generic 逐层叠加，资源数也随之阶梯变化。下面这张表（数字直接取自源码的 checker）讲清了「每开一个特性，代价是多少」：

| 构建名（简） | 关键 generic 变化 | LUT | FF | RAMB36 | RAMB18 | 逻辑级数 | 代码注释要点 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `fifo.minimal` | barebone，w32/d1024 | 14 | 24 | 1 | 0 | 6 | 用 wrapper，只留裸端口 |
| `..._with_output_register` | + 输出寄存器 | 15 | 25 | 1 | 0 | 6 | 寄存器被吸进 BRAM 输出寄存器，几乎免费 |
| `fifo.with_levels` | + 非默认 almost_full_level | 27 | 35 | 1 | 0 | 6 | level 计数器 + 比较器增加资源 |
| `fifo.with_last` | + enable_last | 27 | 35 | 1 | 0 | 6 | **enable_last 不增加资源**（RAM 字宽 +1 而已） |
| `fifo.with_packet_mode` | + enable_packet_mode | 40 | 47 | 1 | 0 | 6 | 多一个包计数器 |
| `..._and_output_register` | + 输出寄存器 | 45 | 50 | 1 | 0 | **9** | 逻辑增加，逻辑级数升到 9 |
| `fifo.with_drop_packet` | + enable_drop_packet | 45 | 58 | 1 | 0 | 6 | 再多一个计数器 |
| `fifo.with_peek_mode` | + enable_peek_mode | 58 | 58 | 1 | 0 | 6 | 多一个读指针 + 复用选择 |
| `fifo.lutram_minimal` | barebone，w8/d32 | 32 | 22 | **0** | 0 | **3** | 小而浅→用分布式 RAM（LUTRAM），无块 RAM |

这张表就是 u4-l1 里「每个 generic 的资源代价」一节的**实证来源**——它不是凭感觉写的，而是 netlist 回归钉死的数字。几个亮点：

- `enable_last` 几乎免费（27→27）：验证了 u4-l1 的论断。
- 输出寄存器几乎免费（14→15），但**和 packet_mode 叠加时逻辑级数从 6 跳到 9**——一个纯时序代价，靠 `MaximumLogicLevel(EqualTo(9))` 守住。
- `lutram_minimal` 用 LUTRAM（RAMB36=0），逻辑级数只有 3——浅 FIFO 用分布式 RAM 更划算。

异步 FIFO 的表（见源码）有更微妙的现象，例如开 packet_mode 时 LUT 反而**下降**（68→60）但 FF 上升（112→123），因为 packet 模式省掉了 `read_level` 的 `resync_counter`；开 drop_packet 同样让资源下降。这些反直觉之处都被作者用代码注释解释了——见下一小节。

#### 4.3.3 源码精读

checker 的 import 列表 [modules/fifo/module_fifo.py:16-23](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L16-L23)：

```python
from tsfpga.vivado.build_result_checker import (
    EqualTo,
    Ffs,
    MaximumLogicLevel,
    Ramb18,
    Ramb36,
    TotalLuts,
)
```

最简单的「minimal」工程及其 5 个断言 [modules/fifo/module_fifo.py:143-158](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L143-L158)：

```python
projects.append(
    TsfpgaExampleVivadoNetlistProject(
        name=self.netlist_build_name("fifo.minimal", generics),
        modules=modules,
        part=part,
        top="fifo_netlist_build_wrapper",
        generics=generics,
        build_result_checkers=[
            TotalLuts(EqualTo(14)),
            Ffs(EqualTo(24)),
            Ramb36(EqualTo(1)),
            Ramb18(EqualTo(0)),
            MaximumLogicLevel(EqualTo(6)),
        ],
    )
)
```

作者的注释本身就是一份「设计决策说明书」。几个范例：

- [modules/fifo/module_fifo.py:200](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L200)：`# Enabling last should not increase resource utilization`——开 `enable_last` 后 checker 仍是 `TotalLuts(EqualTo(27))`，与上一层完全相同，**用断言强制保证「免费」这一承诺不被破坏**。
- [modules/fifo/module_fifo.py:239-240](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L239-L240)：`# Enabling the output register increases logic, but the register itself / should be packed into the RAM output register`——解释为何输出寄存器只多 1 FF。
- [modules/fifo/module_fifo.py:504-506](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L504-L506)（异步 drop_packet）：`# Enabling drop_packet support actually decreases utilization. Some logic is added / for handling the drop_packet functionality, but one resync_counter instance is saved / since the read_level value is not used.`——把一个反直觉的「资源下降」讲得清清楚楚。

异步部分的 `resync_fifo` 配置还展示了一种**数据驱动**写法：把每个位宽对应的 (LUT, FF, 逻辑级数) 打包进 `dataclass ResyncConfig`，循环登记 [modules/fifo/module_fifo.py:332-371](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L332-L371)，避免重复粘贴。这种写法在 `common` 模块的 `handshake_pipeline` 里更彻底——把多组 generic 和对应的资源数列成平行数组，循环断言（见 [modules/common/module_common.py:326-352](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L326-L352)），其中满吞吐 skid buffer 模式正是 u2-l1 提到的 `TotalLuts(EqualTo(41))`。

#### 4.3.4 代码实践

**实践目标**：把一张资源断言表「翻译」成「启用了哪些 generic」的因果说明。这是本讲的核心实践任务。

**操作步骤**：

1. 打开 [modules/fifo/module_fifo.py:132-324](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L132-L324)（同步 FIFO 的全部构建）。
2. 注意每段都先用 `generics = {...}` 或 `generics.update(...)` 改动 generic，再 `append` 一个带 checker 的工程。沿 `generics` 的变化追踪「相比上一个工程，新开了什么特性」。
3. 把结果填进一张三列表：**构建名 / 新开的 generic / LUT/FF 变化**。
4. **预测题**：假设要在 `fifo.with_peek_mode`（当前 LUT=58, FF=58）的基础上**额外**开 `enable_output_register`（且 depth+1），参考 `fifo.with_packet_mode_and_output_register` 这一档（packet_mode+输出寄存器时 LUT 从 40→45、FF 从 47→50、逻辑级数 6→9），预测新的 LUT/FF/逻辑级数大致会是多少。

**需要观察的现象**：

- `enable_last` 一档：LUT/FF 与上一层 `with_levels` **完全相同**（都 27/35）→ 证实「免费」。
- 每开一个计数器类特性（packet_mode / drop_packet / peek_mode），FF 都有可观上涨。
- 输出寄存器单独开几乎免费，但叠加 packet_mode 时**逻辑级数**从 6 跳到 9。

**预期结果（预测题）**：peek_mode 已是 58 LUT，再叠输出寄存器，参照 packet_mode 那一档「+5 LUT、+3 FF、逻辑级数 6→9」的增量规律，可粗略预测约 LUT≈60+、FF≈60+、逻辑级数≈8~9。**这是预测，真实数字待本地用 Vivado netlist 构建验证**（见 4.4.4 或综合实践）。重点不是猜中数字，而是说出「方向」：LUT/FF 小幅上升、逻辑级数很可能上升。

#### 4.3.5 小练习与答案

**练习 1**：为什么 fifo 的每个工程都同时断言 `Ramb36` 和 `Ramb18` 两个数，而不是只断言「块 RAM 总数」？

**参考答案**：因为 BRAM 的容量是分档的（RAMB36 = 36Kb、RAMB18 = 18Kb），同样的存储总量可能映射成「1 个 RAMB36」或「2 个 RAMB18」，二者占用的物理资源不同。分别钉死两个数，能捕捉到「综合器把 RAMB36 拆成两个 RAMB18」这类隐性回归。

**练习 2**：`MaximumLogicLevel(EqualTo(6))` 守护的是性能还是面积？它和 `TotalLuts` 有何不同？

**参考答案**：守护的是**时序/性能**。逻辑级数 = 关键路径上串联的组合逻辑层数，直接决定能跑到的最高频率（级数越多 fmax 越低）。`TotalLuts` 管「用了多少面积」，`MaximumLogicLevel` 管「关键路径有多深」——一个回归可能面积不变但逻辑级数翻倍（FIFO 还是那么大，却跑不到频率了），只有 `MaximumLogicLevel` 能抓住。

---

### 4.4 最小化顶层 wrapper：隔离被测特性

#### 4.4.1 概念说明

注意上面 `fifo.minimal` 等工程的 `top="fifo_netlist_build_wrapper"`，而不是直接 `top="fifo"`。这个 wrapper 就是本节主角。

为什么需要它？因为 **FIFO 实体有很多可选端口**（`level`、`almost_full`、`almost_empty`、`read_last`、`write_last`……）。如果你直接把 `fifo` 当顶层综合，Vivado 会发现这些输出端口没有任何负载、输入端口悬空——大多数情况下它会把这些无关端口优化掉，但**优化行为并不完全确定**：可能残留一些缓冲，也可能改变逻辑级数。这会让「最小 FIFO 到底多大」这个数字变得不稳定、不可重复。

解决办法：写一个**只接出 barebone（裸接口）端口**的薄 wrapper——只留 `clk`、读写 `ready/valid/data`，把 `level`/`almost_*`/`last` 等统统不接。这样综合的就是一个「纯粹的数据缓冲 FIFO」，资源数干净、稳定、可重复。

> 反过来，当你**正是要测「带 level 端口」的资源代价**时，就直接用 `top="fifo"` 并在 generic 里启用相应端口（如 `fifo.with_levels` 那一档）。所以「用 wrapper」还是「直接用实体」是按测试目的选择的。

#### 4.4.2 核心流程

wrapper 的设计要点：

```text
fifo_netlist_build_wrapper（顶层，只暴露 barebone）
  generic: use_asynchronous_fifo, width, depth, enable_output_register
  port:    clk, clk_read, clk_write,
           read_ready/read_valid/read_data,
           write_ready/write_valid/write_data
        │
        ▼ 完整端口映射（一一对应，不丢任何信号）
  fifo_wrapper（真正的 FIFO，按 generic 切同步/异步）
```

wrapper 自己**不写任何逻辑**，纯粹是端口转接。它的四个 generic 直接透传给 `fifo_wrapper`（u4-l2 讲过的统一封装，用 `use_asynchronous_fifo` 切同步/异步）。

#### 4.4.3 源码精读

实体声明 [modules/fifo/rtl/fifo_netlist_build_wrapper.vhd:18-38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/rtl/fifo_netlist_build_wrapper.vhd#L18-L38)，注意端口集刻意收窄到最小：

```vhdl
entity fifo_netlist_build_wrapper is
  generic (
    use_asynchronous_fifo : boolean;
    width : positive;
    depth : positive;
    enable_output_register : boolean
  );
  port (
    clk : in std_ulogic;
    clk_read : in std_ulogic;
    clk_write : in std_ulogic;
    read_ready : in std_ulogic;
    read_valid : out std_ulogic := '0';
    read_data : out std_ulogic_vector(width - 1 downto 0) := (others => '0');
    write_ready : out std_ulogic := '0';
    write_valid : in std_ulogic;
    write_data : in std_ulogic_vector(width - 1 downto 0)
  );
end entity;
```

文件头注释 [modules/fifo/rtl/fifo_netlist_build_wrapper.vhd:9-11](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/rtl/fifo_netlist_build_wrapper.vhd#L9-L11) 一句话点明用途：

> `A wrapper of the FIFO with only the "barebone" ports routed. To be used for size assertions in netlist builds.`

架构体里只有一个实例化，把 wrapper 的端口原样接到 `fifo_wrapper` [modules/fifo/rtl/fifo_netlist_build_wrapper.vhd:45-64](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/rtl/fifo_netlist_build_wrapper.vhd#L45-L64)，generic 透传：

```vhdl
fifo_wrapper_inst : entity work.fifo_wrapper
  generic map (
    use_asynchronous_fifo => use_asynchronous_fifo,
    width => width,
    depth => depth,
    enable_output_register => enable_output_register
  )
  port map (
    clk => clk, clk_write => clk_write, clk_read => clk_read,
    write_ready => write_ready, write_valid => write_valid, write_data => write_data,
    read_ready => read_ready, read_valid => read_valid, read_data => read_data
  );
```

正因为没有任何 `level`/`almost_*`/`last` 端口被接出，综合器面对的就是一个「纯数据缓冲」FIFO，`fifo.minimal` 的 14 LUT / 24 FF 才是一个干净、可重复的最小基线。

#### 4.4.4 代码实践

**实践目标**：亲手验证「wrapper 隔离无关端口」的效果（若有 Vivado）。

**操作步骤**：

1. 先读 wrapper，确认它的端口里**没有** `level`、`almost_full`、`almost_empty`、`read_last`、`write_last`（对比 `fifo.vhd` / `fifo_wrapper.vhd` 的完整端口）。
2. 用 `tools/build_fpga.py`（或更轻的 `tools/synthesize.py`，u9-l2）分别综合两个顶层（待本地验证 Vivado 环境）：
   ```bash
   # 方式一：用最小 wrapper
   python3 tools/synthesize.py fifo_netlist_build_wrapper \
       --generic use_asynchronous_fifo=false --generic width=32 --generic depth=1024

   # 方式二：直接综合 fifo 实体（端口更多）
   python3 tools/synthesize.py fifo --generic width=32 --generic depth=1024
   ```
3. 对比两者的 utilization 报告里的 LUT/FF。

**需要观察的现象**：方式一（wrapper）的资源数与 `fifo.minimal` 断言的 14 LUT / 24 FF 一致；方式二（直接 `fifo`）可能因为多余端口未被负载而产生略有不同的资源数或逻辑级数。

**预期结果**：wrapper 版本资源数稳定且最小；这正是它被选作 minimal 基线顶层的原因。若没有 Vivado，本步骤为「待本地验证」的源码阅读型实践——你至少能从端口表读出 wrapper 砍掉了哪些可选端口。

#### 4.4.5 小练习与答案

**练习 1**：既然 wrapper 只是把端口转接一下、不写逻辑，为什么不直接用 `top="fifo"` 配一组不开任何可选特性的 generic，岂不是更简单？

**参考答案**：因为即便 generic 关闭了可选特性，`fifo` 实体的可选**端口**（如 `level`）仍然存在于接口上。综合时这些无负载输出端口的处理依赖综合器的优化决策，不够确定、不够可重复。wrapper 在源码层面就**物理删掉**了这些端口，使「最小 FIFO」的资源数成为稳定基线。换言之，wrapper 是为了「确定性」，不是为了「省事」。

**练习 2**：那 `fifo.with_levels` 这一档为什么又改用 `top="fifo"` 而不用 wrapper？

**参考答案**：因为这一档**正是要测「启用 level 端口 + 非默认 almost_full_level」的资源代价**。wrapper 故意不接 `level`/`almost_*`，测不到这个特性；所以必须直接用 `fifo` 实体并设 `almost_full_level=800`，让这些端口和对应的比较逻辑真正出现在综合网表里。选 wrapper 还是选实体，取决于「这一档要测什么」。

---

## 5. 综合实践

**任务**：为 FIFO 的一个「新 generic 组合」补一条 netlist 资源回归断言，并把它的资源代价讲清楚。

**背景**：观察 [modules/fifo/module_fifo.py:283-299](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L283-L299) 的 `fifo.with_peek_mode`（58 LUT / 58 FF / 逻辑级数 6），它**没有**叠加输出寄存器。你的任务是预测并（若有 Vivado）实测「peek_mode + 输出寄存器」的资源数。

**步骤**：

1. **读依赖**：确认 `with_peek_mode` 的 `generics` 当前包含 `width=32, depth=1024(±), almost_full_level=800, enable_last=True, enable_packet_mode=True, enable_peek_mode=True, enable_output_register=False`（沿 `generics.update(...)` 从 `with_levels` 往下追）。
2. **找参照增量**：从 `fifo.with_packet_mode`（40/47/级6）到 `fifo.with_packet_mode_and_output_register`（45/50/级9）这一跳，读出输出寄存器的代价：**+5 LUT、+3 FF、逻辑级数 6→9**。
3. **预测**：把同样的增量套到 peek_mode（58/58/级6）上，预测 `fifo.with_peek_mode_and_output_register` ≈ **LUT 60+、FF 60+、逻辑级数约 9**。写下你的预测值。
4. **实测（若有 Vivado）**：在 `_setup_fifo_build_projects` 末尾仿照现有写法，临时加一个工程：
   ```python
   # 示例代码（仅演示结构，真实数字待 Vivado 实测后填入）
   generics.update(enable_output_register=True)  # depth 需配合 +1，见现有写法
   projects.append(
       TsfpgaExampleVivadoNetlistProject(
           name=self.netlist_build_name("fifo.with_peek_mode_and_output_register", generics),
           modules=modules, part=part, top="fifo", generics=generics,
           build_result_checkers=[
               TotalLuts(EqualTo(此处填实测值)),
               Ffs(EqualTo(此处填实测值)),
               Ramb36(EqualTo(1)),
               Ramb18(EqualTo(0)),
               MaximumLogicLevel(EqualTo(此处填实测值)),
           ],
       )
   )
   ```
   先把 checker 注释掉跑一次拿到 utilization，再把实测数字填进 `EqualTo(...)`，让断言变绿。
5. **反思**：实测值和你预测的方向一致吗？逻辑级数是否如预期上升？把结论写在你的预测旁边。

**验收标准**：

- 能用一句话说出 peek_mode 相比 packet_mode 多用了什么硬件（一个额外读指针 + 复用选择，见 [module_fifo.py:281-282](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/fifo/module_fifo.py#L281-L282) 的注释）。
- 能解释为什么 `MaximumLogicLevel` 在叠加输出寄存器后很可能变化，而单独的 minimal FIFO 叠输出寄存器时逻辑级数不变（14→15 那一档级数仍是 6）。
- 若无 Vivado，至少完成步骤 1–3 的纯源码预测，并标注「实测待本地验证」。

## 6. 本讲小结

- **资源回归**把「面积/时序意外变差」当成 bug 来防：对每个有意义的 generic 组合综合出 netlist，用 `build_result_checker` 断言资源数**精确等于**期望值，偏离即 CI 失败。
- 钩子 `get_build_projects()` 只负责**声明**工程列表（`TsfpgaExampleVivadoNetlistProject`），不跑综合；真正综合由 `tools/build_fpga.py` 经 tsfpga 的 `get_build_projects(...)` 收集后统一执行，并用 `--netlist-builds` 拉起「只综合」流程。
- 每个 checker = **资源类型**（`TotalLuts`/`Ffs`/`Ramb36`/`Ramb18`/`MaximumLogicLevel`）+ **比较算子**（`EqualTo`）。其中 `MaximumLogicLevel` 守时序，其余守面积；都用精确等于以捕捉缓慢爬升。
- FIFO 的同步构建表实证了 u4-l1 的论断：`enable_last` 几乎免费、各计数器特性（packet/drop/peek）逐级增加 FF、输出寄存器单独开近乎免费但叠加时逻辑级数会跳升；异步表还展示了 packet/drop 模式因省掉 `read_level` 同步而**资源反降**的反直觉现象。
- `fifo_netlist_build_wrapper` 这种「最小化顶层 wrapper」只接出 barebone 端口，目的是让「最小 FIFO」的资源数**干净、稳定、可重复**；要测某个可选端口的代价时，则直接用实体当顶层。
- 资源数**依赖器件**（fifo 钉死 `xc7z020clg400-1`），所有 `EqualTo` 数字都是针对该器件标定的；netlist 回归依赖有授权的 Vivado，公开 CI 目前跑的是仿真。

## 7. 下一步学习建议

- **横向对照**：阅读 [modules/common/module_common.py:321-353](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/common/module_common.py#L321-L353) 的 `handshake_pipeline` 构建，看它如何用平行数组把多组 generic 和资源数**数据驱动**地断言（这正是 u2-l1 里「满吞吐 skid buffer = 41 LUT」的来源）。
- **轻量综合**：学下一讲 **u9-l2（Netlist 综合与 FPGA 构建流程）**，掌握 `tools/synthesize.py` 如何用 `--generic name=v1,v2` 快速把任意实体综合成 netlist 做设计反馈——它和本讲的资源回归是「探索」与「守护」的互补关系。
- **扩展到自己的模块**：尝试为你自己写的一个实体仿照本讲模式，在它的 `module_*.py` 里加一个 `get_build_projects()`，用 wrapper 隔离端口、用 `EqualTo` 钉死基线资源数，把它纳入你工程的资源回归。
