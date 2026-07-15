# run.py 装配：库、配置与多仿真器适配

## 1. 本讲目标

本讲是 U7「Python↔HDL 协同仿真验证流程」的第三段，也是收口的一段。前两讲（u7-l1、u7-l2）已经把验证闭环的两端讲清楚了：

- **cosim 脚本**（Python 参考模型）生成黄金数据文件；
- **VHDL 测试台**（TB）读取这些文件、驱动 UUT、逐拍比对。

但这两端本身并不会自动跑起来——它们之间还缺一个「装配层」：谁来把 RTL 源文件、测试台源文件、各种依赖库装进仿真工程？谁来告诉 VUnit「这个测试台在仿真之前要先跑某个 cosim 脚本」？谁来处理 GHDL、NVC、Modelsim 这些不同仿真器的编译/仿真选项差异？

这一层就是 `sim/run.py`，配合两个辅助脚本 `sim/common.py` 与 `sim/cosim_runner.py`。

学完本讲，你应当能够：

1. 说清楚 `sim/` 目录下三个 Python 脚本各自的职责，以及它们如何被 `run.py` 串联成一个完整的 VUnit 仿真工程。
2. 理解 `common.py` 如何在 VUnit 之外挂上自定义命令行参数（`--simulator` 等），并把它们翻译成 VUnit 能识别的环境变量与 VHDL 标准选择。
3. 理解 `create_test_suite` 如何按「库 + 源文件分组 + VHDL 标准」组织整个工程，以及为什么 RTL 和 TB 要用不同标准编译。
4. 掌握每个测试台如何通过 `add_config + pre_config + generics` 把「cosim 脚本路径、generic 参数、仿真前回调」三者绑在一起。
5. 理解 `cosim_runner` 用「双重检查锁 + 自禁用」保证同一个 cosim 脚本在线程并行下只执行一次。

---

## 2. 前置知识

本讲默认你已经学过 u7-l1（cosim 黄金数据生成）和 u7-l2（VUnit 测试台与文件 I/O）。我们把那两讲里最关键的结论再压缩一次，便于承接：

- **验证闭环**：Python 参考模型先在 `bittrue/cosim/` 下生成黄金数据（`a_fmt.txt`、`r_fmt.txt`、`rnd.txt`、`testN_output.txt` 等），VHDL 测试台再读取这些数据逐拍比对 UUT 输出。两端都准备好之后，需要一个「装配层」把它们纳入同一个仿真流程。
- **VUnit**：一个用 Python 驱动 VHDL 仿真的测试框架。它提供 `VUnit.from_args(...)` 构建工程、`add_library` / `add_source_files` 装入源文件、`test_bench(...).add_config(...)` 为测试台添加运行配置，最后 `vu.main()` 跑仿真。本讲几乎所有 API 都是 VUnit 的，`run.py` 只是把这些 API 按本项目的需要「编排」了一遍。
- **pre_config 回调**：VUnit 允许为每个测试配置注册一个 `pre_config` 函数，它会在该测试**仿真开始之前**被调用。本项目正是用它来触发「先跑 cosim 生成黄金数据，再仿真」。
- **meta_width / RegisterMode**：来自 U6。可流水线化组件 `en_cl_fix_round/saturate/resize` 有一个 `meta_width_g` generic，控制边带信号（meta）宽度；这三个组件对应的测试台也声明了 `meta_width_g`，所以需要用不同的 `meta_width` 值各跑一遍。

> 本讲是「纯 Python 装配」视角，不会进入任何 VHDL 函数体。如果你还没学过 U6 的流水线组件，只需记住「round/saturate/resize 这三个测试台有 `meta_width_g` 这个 generic」即可。

---

## 3. 本讲源码地图

本讲只涉及 `sim/` 目录下的三个 Python 脚本：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [sim/common.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py) | ~108 | 「公共底座」：导入 VUnit、定义 5 个自定义命令行参数、校验参数、设置仿真器环境变量、按仿真器选择 VHDL 标准、定义仿真结束后的 `post_run` 回调（合并覆盖率）。 |
| [sim/run.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py) | ~252 | 「装配总指挥」：构建 VUnit 工程，调用 `create_test_suite` 装入库与源文件、为每个测试台绑定配置、设置各仿真器选项，最后交给 `vu.main()` 运行。 |
| [sim/cosim_runner.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py) | ~73 | 「cosim 执行器」：线程安全地把一个 cosim 脚本（`cosim.py`）导入并在 `pre_config` 时执行其 `run()`，保证最多执行一次。 |

三者的调用关系（自顶向下）：

```
run.py (入口)
 ├─ import common          → 得到已解析好的 args、VUnit 类、vhdl_standard_*
 ├─ vu = VUnit.from_args(args)
 ├─ create_test_suite(vu, args)
 │    ├─ 装入 OSVVM / en_tb / lib(RTL+TB) 源文件
 │    ├─ class cosim(cosim_runner)        ← 每个 TB 专用化一个执行器
 │    └─ 对每个 TB 调用 add_config(pre_config=<cosim实例>.run, generics=...)
 └─ vu.main(post_run=common.post_run)     → 仿真前跑 pre_config(cosim)，结束后跑 post_run(覆盖率)
```

---

## 4. 核心概念与源码讲解

### 4.1 common.py：自定义命令行参数与仿真器/VHDL 标准选择

#### 4.1.1 概念说明

`common.py` 是三个脚本里最先被加载的——`run.py` 一开头就 `import common`。它解决一个核心问题：**VUnit 默认的命令行参数里，并没有「用哪个仿真器」「仿真器装在哪」这种项目特定的选项**。本项目支持 GHDL、NVC、Modelsim、Questa 四种仿真器，而且它们对 VHDL 标准的支持还各不相同。

所以 `common.py` 做了三件事：

1. 在 VUnit 的命令行解析器上**挂载自定义参数**（含环境变量兜底）。
2. **校验**必填参数，并把它们翻译成 VUnit 能识别的环境变量。
3. 根据**仿真器型号选择 VHDL 标准**，产出两个常量 `vhdl_standard_rtl` / `vhdl_standard_tb` 供 `run.py` 用。

它还顺手定义了仿真结束后的 `post_run` 回调（合并覆盖率报告）。

#### 4.1.2 核心流程

`common.py` 在被 `import` 的瞬间就执行到底（它是「导入即配置」风格），流程如下：

```
1. 导入 VUnit（从仓库内 vendor 的 vunit 路径）
2. cli = VUnitCLI()
3. 给 cli.parser 追加 5 个自定义参数（每个都有 env 兜底）
4. args = cli.parse_args()
5. 校验 args.simulator / args.simulator_path 非空，否则抛异常
6. 设置 VUNIT_SIMULATOR / VUNIT_MODELSIM_PATH / ... 环境变量
7. 按仿真器选 vhdl_standard_rtl / vhdl_standard_tb
8. 定义 post_run(results) 回调（合并覆盖率）
```

最终对外暴露的关键名字是：`args`（解析好的参数对象）、`VUnit`、`VUnitCLI`、`vhdl_standard_rtl`、`vhdl_standard_tb`、`post_run`。`run.py` 通过 `from common import ...` 直接拿走这些名字。

#### 4.1.3 源码精读

**挂载自定义参数**——每个参数都遵循「命令行优先，环境变量兜底」的同一套写法，以 `--simulator` 为例：

[sim/common.py:31-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L31-L35) 定义 `--simulator`：默认值取环境变量 `EN_SIM_NAME`，若未设则 `None`；帮助文本列出允许的取值 `modelsim / questa / nvc / ghdl`。

[sim/common.py:36-41](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L36-L41) 定义 `-s / --simulator-path`：默认取环境变量 `EN_SIM_BIN`，指向仿真器二进制所在目录。

其余三个参数同理：`--vendor-lib`（[sim/common.py:42-46](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L42-L46)，`EN_SIM_LIB`）、`-c / --coverage`（[sim/common.py:47-53](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L47-L53)，开关量，默认 `False`）、`--disable-cosim`（[sim/common.py:54-59](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L54-L59)，开关量，用于跳过 cosim 自动执行）。

> 这种「命令行 / 环境变量双通道」的好处：CI 或脚本里可以固定传命令行参数，开发者本地也可以在 shell 里 `export EN_SIM_NAME=ghdl` 之后直接 `python run.py`，二者等价。

**校验 + 翻译成环境变量**：

[sim/common.py:64-68](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L64-L68) 校验 `simulator` 与 `simulator_path` 必须非空，否则抛出带用法示例的异常——这是「失败要响亮」的好实践。

[sim/common.py:70-79](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L70-L79) 把参数翻译成 VUnit 实际读取的环境变量。这里有一个**关键 workaround**：VUnit 支持 Questa（`vsim`），但还不接受字符串 `'questa'` 作为合法的 simulator 名。解决办法是：

```
if args.simulator == 'questa':
    environ["VUNIT_SIMULATOR"] = 'modelsim'   # 用 modelsim 这个名字
else:
    environ["VUNIT_SIMULATOR"] = args.simulator
environ["VUNIT_MODELSIM_PATH"] = args.simulator_path  # 但仍指向真正的 questa 安装目录
environ["VUNIT_GHDL_PATH"]    = args.simulator_path
environ["VUNIT_NVC_PATH"]     = args.simulator_path
```

即「名字骗 VUnit 说我是 modelsim，路径却给出真正的 Questa 目录」。

**按仿真器选择 VHDL 标准**：

[sim/common.py:81-89](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L81-L89) 是本模块最值得记住的一段，它产出了两个全局常量：

| 仿真器 | `vhdl_standard_rtl` | `vhdl_standard_tb` |
| --- | --- | --- |
| modelsim / questa | `93` | `2008` |
| ghdl / nvc | `2008` | `2008` |

为什么要分两套标准？因为 **RTL（`hdl/*.vhd`，要能被综合）必须严格保持 VHDL-93 兼容**——很多综合工具只吃 VHDL-93。在 Modelsim/Questa 这条「主验证路径」下，把 RTL 按 `93` 编译，能确保 RTL 真的没有用到 2008 才有的语法（等于「顺带做了综合合规性检查」）。而 **测试台（`tb/*.vhd`）只跑仿真、不综合**，可以用 VHDL-2008 的现代特性（OSVVM、protected type 等），所以固定 `2008`。GHDL/NVC 这条开源路径则把 RTL 也按 `2008` 编译（不再强制 93 约束），属于「能跑就行」的便捷路径。这两个常量会在 4.2 节被 `add_source_files(..., vhdl_standard=...)` 直接消费。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `common.py` 的「仿真器 → VHDL 标准」选择逻辑，并观察缺参数时的报错。

**操作步骤**：

1. 先不传任何参数，看校验如何拦截。在仓库根目录执行（注意需要在能 import 到 vunit 的环境里，**待本地验证**）：

   ```bash
   cd sim
   python run.py
   ```

2. 下面是一段**示例代码**（非仓库原有代码），把 [sim/common.py:81-89](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L81-L89) 的标准选择逻辑剥离出来，便于你单独实验：

   ```python
   # 示例代码：复现 common.py 的 VHDL 标准选择逻辑
   def pick_standard(simulator):
       if simulator in ('modelsim', 'questa'):
           return "93", "2008"
       elif simulator in ('ghdl', 'nvc'):
           return "2008", "2008"
       else:
           raise ValueError("allowed: modelsim, questa, nvc or ghdl")

   for s in ['modelsim', 'questa', 'ghdl', 'nvc']:
       rtl, tb = pick_standard(s)
       print(f"{s:9s} -> rtl={rtl}, tb={tb}")
   ```

**需要观察的现象 / 预期结果**：

- 步骤 1 应当打印出 `ERROR: please use --simulator <name> ...` 的异常信息（即 [sim/common.py:65-66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L65-L66) 的报错）。
- 步骤 2 的输出应与你对照 [sim/common.py:81-89](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L81-L89) 推出的表格完全一致：modelsim/questa → `93/2008`，ghdl/nvc → `2008/2008`。

> 如果本地没有装 vunit 导致步骤 1 报的是 `ModuleNotFoundError` 而非业务报错，说明 vendor 的 vunit 路径未就绪——这本身也验证了 [sim/common.py:24-27](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L24-L27) 那行 `sys.path.insert(...)` 的存在意义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--simulator` 的默认值写成 `environ["EN_SIM_NAME"] if "EN_SIM_NAME" in environ else None`，而不是直接 `default=None`？

**答案**：为了实现「命令行优先、环境变量兜底」。如果用户在 shell 里 `export EN_SIM_NAME=ghdl`，那么不传 `--simulator` 也能用；显式传 `--simulator` 时则覆盖环境变量。直接 `default=None` 会丢掉环境变量这条通道。

**练习 2**：假设某天 VUnit 升级后开始原生支持 `'questa'` 这个名字，[sim/common.py:71-74](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L71-L74) 的这段 workaround 还需要吗？

**答案**：不需要了。这段 `if args.simulator == 'questa': VUNIT_SIMULATOR = 'modelsim'` 的存在完全是为了绕开「VUnit 不认 questa 这个名字」的局限；一旦 VUnit 原生支持，可直接 `environ["VUNIT_SIMULATOR"] = args.simulator` 一行通吃。

---

### 4.2 create_test_suite：库与源文件的组织

#### 4.2.1 概念说明

`create_test_suite(vu, args)` 是 `run.py` 的核心函数（[sim/run.py:30](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L30)）。它负责把所有需要编译的 VHDL 文件，按「**库（library）+ VHDL 标准**」两个维度装进 VUnit 工程。

VHDL 的「库」是一个编译命名空间：不同库里的同名实体互不冲突。本项目刻意把文件分成几组、放进不同库，是为了隔离依赖与避免命名冲突——这一点在前置讲义 u1-l2 里已经提过（`en_tb` 库隔离）。本节只关心「哪些文件进了哪个库、用什么标准编译」。

#### 4.2.2 核心流程

```
create_test_suite(vu, args):
  1. 加入 VUnit 自带库：OSVVM、verification_components、random
  2. 加入 en_tb 库（来自 lib/en_tb/hdl/*.vhd），标准 = vhdl_standard_tb(2008)
  3. 新建 lib 库：
       - RTL：      ../hdl/*.vhd       ，标准 = vhdl_standard_rtl
       - TB 工具：  ../tb/util/*.vhd   ，标准 = vhdl_standard_tb(2008)
       - TB 主体：  ../tb/*.vhd        ，标准 = vhdl_standard_tb(2008)
  4. （后续 4.3 节）为每个测试台绑定运行配置
  5. （后续 4.5 节）设置各仿真器编译/仿真选项
```

注意一个细节：步骤 3 把三类源文件都装进了**同一个 `lib` 库**，但它们的 VHDL 标准可以不同（RTL 用 `vhdl_standard_rtl`，TB 用 `vhdl_standard_tb`）。VUnit 允许在 `add_source_files` 调用级别指定标准。

#### 4.2.3 源码精读

**VUnit 自带库**：[sim/run.py:32-34](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L32-L34) 加入 OSVVM 验证方法库、验证组件库与随机库——这些是测试台里 `assert` 计数、随机化等机制所依赖的（u7-l2 提到的 OSVVM）。

**en_tb 库（带防重复保护）**：

[sim/run.py:37-41](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L37-L41) 把 `lib/en_tb/hdl/*.vhd`（不可综合的测试台基础子库）装入名为 `en_tb` 的库，标准 `vhdl_standard_tb`。这里用 `try/except ValueError` 包住 `add_library("en_tb")`——因为 `en_tb` 是被外部共享的子库，可能已经被别的（上层）工程创建过，重复创建会抛 `ValueError`，这里捕获后打印 `en_tb already created, skip it...` 跳过，保证 `run.py` 既能独立运行，也能作为子工程被嵌入。

**lib 库（本项目的 RTL + TB）**：

[sim/run.py:43-50](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L43-L50) 是最关键的一段，新建 `lib` 库并分三批装入文件：

| 调用 | 文件 | VHDL 标准 | 含义 |
| --- | --- | --- | --- |
| `add_source_files("../hdl/*.vhd")` | RTL 包与组件 | `vhdl_standard_rtl` | 被测的定点库本身（93 或 2008，按仿真器） |
| `add_source_files("../tb/util/*.vhd")` | `en_cl_fix_fileio_pkg.vhd` 等 | `vhdl_standard_tb` | TB 用的「格式感知文件 I/O」糖衣（见 u7-l2） |
| `add_source_files("../tb/*.vhd")` | 各 `*_tb.vhd` | `vhdl_standard_tb` | 测试台主体（含 UUT 例化与检查进程） |

这三批文件虽然同在 `lib` 库，但 RTL 与 TB 的标准可能不同——这正是 4.1 节那两个常量 `vhdl_standard_rtl` / `vhdl_standard_tb` 的用武之地。

#### 4.2.4 代码实践

**实践目标**：核对「哪些文件会被装进 `lib` 库、分别用什么标准」。

**操作步骤**：

1. 用 glob 数一下三个目录各有多少 `.vhd`：

   ```bash
   ls ../hdl/*.vhd ../tb/util/*.vhd ../tb/*.vhd   # 在 sim/ 目录下执行
   ```

2. 对照 [sim/run.py:43-50](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L43-L50)，把每个文件归到上表的某一格。

**需要观察的现象 / 预期结果**：

- `../hdl/` 下应有 5 个 `.vhd`（u1-l2 提到：主包、私有包、round/saturate/resize 三个可流水线化组件），它们将按 `vhdl_standard_rtl` 编译。
- `../tb/` 下的 `*_tb.vhd` 与 `../tb/util/` 下的工具包，将按 `vhdl_standard_tb`（2008）编译。

> 如果你想真正看到 VUnit 把文件分到哪个库、用哪个标准，可以在装好仿真器后执行 `python run.py --simulator=ghdl --simulator-path=<path> --compile`（`--compile` 是 VUnit 自带选项，只编译不仿真），观察输出里每个文件归属的库与标准。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `en_tb` 库用 `try/except ValueError` 保护，而 `lib` 库不用？

**答案**：`en_tb` 是被设计成可被多个工程共享的外部子库，可能已经被上层工程创建过，重复 `add_library` 会报 `ValueError`，故需保护。`lib` 是 `run.py` 自己新建的本项目专用库，不会重复创建，故无需保护。

**练习 2**：在 Modelsim 下，`hdl/en_cl_fix_pkg.vhd` 会按哪个 VHDL 标准编译？为什么？

**答案**：按 `vhdl_standard_rtl = "93"`（见 [sim/common.py:82-84](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L82-L84)）。因为 RTL 必须保持 VHDL-93 综合兼容，在 Modelsim 这条主路径下用 93 编译等于顺带做综合合规检查。

---

### 4.3 为每个测试台绑定配置：add_config + pre_config + meta_width

#### 4.3.1 概念说明

把源文件装进库之后，VUnit 还不知道「这些测试台该怎么跑」。`add_config` 就是用来给一个测试「配置运行方式」的：它接受 `name`（配置名，会出现在测试列表里）、`generics`（VHDL generic 的取值）、`pre_config`（仿真前的 Python 回调）等参数。

本节要讲清两件事：

1. **`pre_config` 如何把 cosim 串进来**：每个 TB 的 `pre_config` 绑定到对应 cosim 实例的 `.run` 方法，于是「仿真开始前自动生成黄金数据」。
2. **`meta_width` 多配置**：round/saturate/resize 三个测试台有 `meta_width_g` generic，所以同一个测试要分别用 `meta_width_g=0` 和 `=8` 各配一遍。

#### 4.3.2 核心流程

`run.py` 为每个 TB 写了一个「配置块」，分两种形态：

**形态 A（多数算术 TB）**——单一配置、无 generic：

```
xxx_cosim = cosim("cl_fix_xxx")              # 建专用 cosim 执行器
xxx_tb    = lib.test_bench("cl_fix_xxx_tb")  # 取出该 TB
for test in xxx_tb.get_tests("test"):        # 遍历名为 "test" 的测试
    test.add_config(name="Test",
                    generics=dict(),
                    pre_config=xxx_cosim.run)
```

**形态 B（round/saturate/resize）**——多配置、带 `meta_width_g`：

```
xxx_cosim = cosim("cl_fix_xxx")
xxx_tb    = lib.test_bench("cl_fix_xxx_tb")
test = xxx_tb.get_tests("test")[0]           # 取唯一一个 "test"
for meta_width in [0, 8]:                    # 用两个 meta 宽度各配一遍
    test.add_config(name=f"MetaWidth={meta_width}",
                    generics=dict(meta_width_g=meta_width),
                    pre_config=xxx_cosim.run)
```

两者共享同一条核心链路：**cosim 脚本路径（构造 `cosim(...)` 时确定）→ generics（`add_config` 时给定）→ pre_config 回调（绑定 `xxx_cosim.run`）**。

#### 4.3.3 源码精读

**专用化 cosim 执行器**：[sim/run.py:56-63](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L56-L63) 先定好 cosim 根目录 `COSIM_PATH`，再定义一个内嵌子类 `cosim`，它把「子目录名」翻译成「完整 cosim 脚本路径」并传给基类 `cosim_runner`：

```python
COSIM_PATH = join(root, "../bittrue/cosim")
class cosim(cosim_runner):
    def __init__(self, dirname):
        cosim_subdir = join(COSIM_PATH, dirname)        # 如 ../bittrue/cosim/cl_fix_round
        super().__init__(args.disable_cosim, cosim_subdir)
```

所以 `cosim("cl_fix_round")` 就指向了 `bittrue/cosim/cl_fix_round/cosim.py`（u7-l1 讲过的那个脚本）。注意：构造时只是**导入**该脚本（定义了 `run()`），**并不会执行 `run()`**——执行权交给后面的 `pre_config`（详见 4.4）。

**形态 A 范例：cl_fix_add**（[sim/run.py:68-74](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L68-L74)）：

```python
cl_fix_add_cosim = cosim("cl_fix_add")
cl_fix_add_tb    = lib.test_bench("cl_fix_add_tb")
for test in cl_fix_add_tb.get_tests("test"):
    test.add_config(name=f"Test",
                    generics=dict(),
                    pre_config=cl_fix_add_cosim.run)
```

`generics=dict()` 表示不给任何 generic（这些算术 TB 没有 `meta_width_g`，只测纯函数行为）。`pre_config=cl_fix_add_cosim.run` 把回调指向执行器。

**形态 B 范例：cl_fix_round**（[sim/run.py:156-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L156-L165)）——这是本节重点：

```python
cl_fix_round_cosim = cosim("cl_fix_round")
cl_fix_round_tb    = lib.test_bench("cl_fix_round_tb")

test = cl_fix_round_tb.get_tests("test")[0]
for meta_width in [0, 8]:
    name     = f"MetaWidth={meta_width}"
    generics = dict(meta_width_g=meta_width)
    test.add_config(name=name,
                    generics=generics,
                    pre_config=cl_fix_round_cosim.run)
```

逐行拆解这条「串联」链路：

1. `cosim("cl_fix_round")` —— **cosim 脚本路径**：指向 `bittrue/cosim/cl_fix_round/cosim.py`。它内部的 `run()` 会清空 `data/`、穷举 `a_fmt/r_fmt/rnd`、用 `cl_fix_round` 算黄金输出并落盘（u7-l1）。
2. `lib.test_bench("cl_fix_round_tb")` —— 取出从 `tb/cl_fix_round_tb.vhd` 编译进来的 TB 实体。
3. `get_tests("test")[0]` —— 该 TB 只声明了一个名为 `"test"` 的测试（对应 TB 里 `run("test")`，见 u7-l2），取这唯一一个。
4. `for meta_width in [0, 8]` + `add_config(...)` —— **generics**：用 `meta_width_g=0` 和 `=8` 各添加一个配置，名字分别是 `MetaWidth=0`、`MetaWidth=8`。最终 VUnit 列表里会看到 `cl_fix_round_tb.test.MetaWidth=0` 和 `.MetaWidth=8` 两条测试。
5. `pre_config=cl_fix_round_cosim.run` —— **pre_config 回调**：两条配置都绑定到**同一个** `cl_fix_round_cosim.run`。

这里有一个看起来「危险」的点：两条配置（`MetaWidth=0` 与 `MetaWidth=8`）都绑了同一个 `cl_fix_round_cosim.run`，而 VUnit 默认会**并行（多线程）跑多个测试**。如果不加保护，两个线程会同时触发 `run()`，导致 cosim 把 `data/` 目录写两遍、甚至互相覆盖文件。正是这个问题催生了 4.4 节的 `cosim_runner`——它保证 `run()` 最多执行一次。

**为什么只有 round/saturate/resize 三个 TB 用形态 B？** 因为只有这三个 TB 声明了 `meta_width_g` generic（你可以用 `grep meta_width_g tb/` 验证，命中正是 `cl_fix_round_tb.vhd`、`cl_fix_saturate_tb.vhd`、`cl_fix_resize_tb.vhd`）。它们例化的是 U6 那三个**可流水线化组件**（带 meta 边带端口），所以要验证 meta 透传；而算术 TB 测的是纯函数，没有 meta 端口，故用形态 A。saturate 与 resize 的配置块（[sim/run.py:170-179](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L170-L179)、[sim/run.py:184-193](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L184-L193)）与 round 完全同构。

#### 4.3.4 代码实践

**实践目标**：把 cl_fix_round 配置块的「三件套串联」用自己的话讲一遍，并解释为什么不会重复跑 cosim。

**操作步骤**：

1. 打开 [sim/run.py:156-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L156-L165)。
2. 在一张纸上画出下面这条链，并填入具体取值：

   ```
   cosim 脚本路径  = ?   （由 cosim("cl_fix_round") 决定）
   generics        = ?   （两个值）
   pre_config 回调 = ?   （绑定到哪个方法）
   ```

3. 对照 [sim/run.py:170-193](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L170-L193) 确认 saturate、resize 两个块与 round 完全同构。

**需要观察的现象 / 预期结果**：

- 你应当能写出：脚本路径 = `bittrue/cosim/cl_fix_round/cosim.py`；generics = `{meta_width_g: 0}` 与 `{meta_width_g: 8}`；pre_config = `cl_fix_round_cosim.run`。
- 能解释：两条配置绑同一个 `.run`，但 `cosim_runner`（4.4）保证它只执行一次，所以黄金数据只生成一遍，两条测试共用同一份 `data/`。

#### 4.3.5 小练习与答案

**练习 1**：形态 A 用 `for test in tb.get_tests("test")`，形态 B 用 `tb.get_tests("test")[0]`。为什么形态 B 只取第 0 个？

**答案**：round/saturate/resize 这三个 TB 各自只声明了一个名为 `"test"` 的测试，`get_tests("test")` 返回长度为 1 的列表，取 `[0]` 就是那个唯一测试对象，然后对它 `add_config` 两次（两种 meta_width）。形态 A 的 `for` 循环则是为了兼容「可能有多个名为 test 的子测试」的更通用写法。

**练习 2**：如果把 `pre_config=cl_fix_round_cosim.run` 改成 `pre_config=None`，仿真还能跑吗？会发生什么？

**答案**：仿真仍能跑（VUnit 允许 `pre_config=None`），但**不会再自动生成黄金数据**。此时 TB 去 `data/` 读文件，要么读到上一次残留的旧数据（结果可能错），要么读到空（直接报错）。`pre_config` 正是「仿真前确保黄金数据新鲜」的那一环。

---

### 4.4 cosim_runner：线程安全的单次执行保证

#### 4.4.1 概念说明

`cosim_runner` 是 [sim/cosim_runner.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py) 里定义的类，它要解决两个问题：

1. **导入而不执行**：构造时把 cosim 脚本（`cosim.py`）作为模块导入，拿到它的全局字典（里面有 `run` 函数、可能还有 `COSIM_CONFIG`），但**不调用 `run()`**——`run()` 要等到 VUnit 的 `pre_config` 时机才执行。
2. **线程安全的单次执行**：`run()` 这个回调可能被 VUnit 在多线程下并发触发（4.3 末尾的 round 例子就是两条配置绑同一个 `.run`），必须保证 cosim 真正「只跑一次」。

这两个问题分别用两个锁解决。

#### 4.4.2 核心流程

```
构造 cosim_runner(disable, cosim_path, module_name="cosim"):
  enable = not disable
  lock   = Lock()                         # 局部锁：保证 run() 单次执行
  with COSIM_PATH_THREADLOCK:             # 全局锁：串行化 sys.path 操作
      sys.path.insert(1, cosim_path)
      module_dict = runpy.run_module("cosim")   # 导入脚本（不执行 run）
      sys.path.remove(cosim_path)

回调 run():  （作为 pre_config 被 VUnit 调用）
  if not enable: return True              # 第一次检查（锁外，快速短路）
  with lock:
      if enable:                          # 第二次检查（锁内，防「等锁期间别人已跑」）
          module_dict["run"]()            # 真正执行 cosim
          enable = False                  # 自禁用
  return True                             # 必须 True，告诉 VUnit 成功
```

这是经典的**双重检查锁定（double-checked locking）**单例模式，外加一个全局锁保护 `sys.path`。

#### 4.4.3 源码精读

**两个锁**：[sim/cosim_runner.py:25-27](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L25-L27) 定义全局锁 `COSIM_PATH_THREADLOCK`，注释点明了它的用途——保证「同一时刻只有一个 cosim 脚本被加入 sys.path 并执行」，因为**所有 cosim 脚本都叫 `cosim.py`**（只是分处不同子目录）。[sim/cosim_runner.py:36-37](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L36-L37) 在每个实例内部再建一个局部锁 `self.lock`，专门用于「单次执行」保证。

**导入而不执行**：[sim/cosim_runner.py:39-48](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L39-L48) 是构造函数的主体，注释明确强调两件事：「这**不会**执行 cosim 的 `run()` 函数」「为支持多个同名 `cosim.py`，只临时修改 sys.path 再还原，由全局锁保证线程安全」：

```python
with COSIM_PATH_THREADLOCK:
    sys.path.insert(1, self.cosim_path)        # 临时把该 cosim 目录插到最前
    self.module_dict = runpy.run_module(self.module_name)   # 导入，得到模块全局字典
    sys.path.remove(self.cosim_path)           # 立刻还原
```

为什么 `run_module` 不会触发 `run()`？因为 cosim 脚本里真正的执行包在 `if __name__ == '__main__': run()` 守卫里——见 [bittrue/cosim/cl_fix_round/cosim.py:130-131](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L130-L131)。`runpy.run_module("cosim")` 导入时该模块的 `__name__` 是 `"cosim"` 而非 `"__main__"`，所以这个守卫块被跳过，`run()` 只是作为函数被定义进 `module_dict`，留待回调时调用。

**可选的配置读取**：[sim/cosim_runner.py:50-57](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L50-L57) 的 `get_config()` 尝试从模块字典里读 `COSIM_CONFIG` 对象返回，没有则返回 `None`。这是给 `run.py` 预留的「不跑 cosim 也能拿到配置（如测试集数量）」的钩子。需要说明的是：**当前所有 cosim 脚本都没有定义 `COSIM_CONFIG`**（你可以 `grep COSIM_CONFIG bittrue/cosim/` 验证，无命中），所以 `get_config()` 目前对每个脚本都返回 `None`，是一个尚未启用的扩展点。

**线程安全的单次执行回调**：[sim/cosim_runner.py:59-72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L59-L72) 的 `run()` 是整段的精华，逐行解读：

```python
def run(self):
    if self.enable:                      # ① 锁外快检：已禁用就直接返回，不必等锁
        with self.lock:                  # ② 进局部锁
            if self.enable:              # ③ 锁内复检：等锁期间别的线程可能已跑过并禁用
                self.module_dict["run"]()# ④ 真正执行 cosim 的 run()
                self.enable = False      # ⑤ 自禁用，确保之后任何线程都跳过
    return True                          # ⑥ 必须返回 True，告诉 VUnit pre_config 成功
```

三个要点：

- **为什么需要两次检查 `self.enable`**：若只有锁外检查①，多线程会都在锁外通过检查、然后排队进锁，第一个执行后禁用，后面的仍会执行——锁外检查无法阻止它们，因为它们已经过了检查点。锁内复检③ 挡住了「在锁外通过、却在等锁期间被别人抢先执行」的线程。
- **`self.enable = False` 是核心**：执行一次后自禁用，之后所有线程（无论 4.3 里 round 的两条配置，还是别的）调到① 就直接短路返回。这就是「最多执行一次」的保证。
- **`--disable-cosim` 如何生效**：构造时 `self.enable = not disable`（[sim/cosim_runner.py:33](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L33)），若命令行传了 `--disable-cosim`，则 `enable=False`，① 直接跳过，cosim 完全不执行——用于「黄金数据已存在、只想快速重跑仿真」的场景。

#### 4.4.4 代码实践

**实践目标**：用一个独立的最小例子重现「双重检查锁 + 自禁用」，直观看到并发下只执行一次。

**操作步骤**：下面是**示例代码**（非仓库代码），把 `cosim_runner.run` 的并发控制逻辑抽出来模拟：

```python
# 示例代码：模拟 cosim_runner 的单次执行保证
from threading import Lock, Thread

class FakeRunner:
    def __init__(self):
        self.enable = True
        self.lock = Lock()
        self.run_count = 0
    def run(self):
        if self.enable:               # ① 锁外快检
            with self.lock:           # ② 进锁
                if self.enable:       # ③ 锁内复检
                    self.run_count += 1   # ④ 模拟执行 cosim
                    self.enable = False   # ⑤ 自禁用
        return True                   # ⑥

r = FakeRunner()
threads = [Thread(target=r.run) for _ in range(8)]   # 模拟 8 个 VUnit 线程并发触发
for t in threads: t.start()
for t in threads: t.join()
print("实际执行次数 =", r.run_count)
```

**需要观察的现象 / 预期结果**：无论开多少个线程，`实际执行次数` 恒为 `1`。把这映射回 cosim：即使 `MetaWidth=0` 与 `MetaWidth=8` 两条配置并行触发 `cl_fix_round_cosim.run`，cosim 脚本也只跑一次，`data/` 只生成一遍。

> 想验证真实行为：在装好仿真器后，跑 `python run.py --simulator=ghdl --simulator-path=<path> cl_fix_round_tb`（指定只跑 round TB），观察 cosim 打印的 `Cosim generated N tests.` 只出现一次，即便有 `MetaWidth=0` 和 `MetaWidth=8` 两条测试。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果去掉锁内复检③（只保留锁外检查① 和锁），并发下 `run_count` 可能是多少？

**答案**：可能等于线程数（最坏情况下所有线程都在锁外通过①，然后依次进锁各执行一次）。锁外检查挡不住「已经过了检查点、正在排队等锁」的线程，所以必须用锁内复检 + 自禁用才能真正保证「只跑一次」。

**练习 2**：`run()` 末尾为什么必须 `return True`？

**答案**：因为它是作为 VUnit 的 `pre_config` 回调被调用的。VUnit 约定 `pre_config` 返回真值表示「准备成功，可以继续仿真」，返回假值则会中止该测试。所以即便 `--disable-cosim` 跳过了执行，也必须 `return True`。

---

### 4.5 多仿真器编译/仿真选项适配（GHDL / NVC / Modelsim）

#### 4.5.1 概念说明

装完源文件、绑完配置，最后一步是「告诉每种仿真器该怎么编译、怎么仿真」。不同仿真器的命令行选项完全不同：GHDL 用 `a_flags`/`elab_flags`/`sim_flags`，NVC 用 `global_flags`/`heap_size`，Modelsim 用 `vcom_flags`/`vlog_flags`/`vsim_flags`。VUnit 用「选项键名」来区分，对未使用的仿真器，不匹配的键名会被安全忽略。

本节梳理 `run.py` 末尾为四类仿真器分别设置的选项，以及一个 GUI 波形自动加载的小技巧。

#### 4.5.2 核心流程

```
设置编译/仿真选项（按仿真器分组，互不干扰）:
  GHDL:    a_flags(全局+lib)、elab_flags、sim_flags(栈大小)
  NVC:     a_flags(--check-synthesis)、global_flags(内存)、heap_size
  Modelsim/Questa: vcom_flags(覆盖率+综合检查)、vlog_flags、vsim_flags、
                   (仅 questa + 非GUI) three_step_flow
  全仿真器: disable_ieee_warnings、(可选) enable_coverage
  GUI 波形: 为每个 TB 绑定各仿真器的 wave 脚本（仅 GUI 模式生效）
```

#### 4.5.3 源码精读

**GHDL 选项**（[sim/run.py:210-214](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L210-L214)）：

```python
vu.add_compile_option("ghdl.a_flags", ["--warn-no-hide"])
lib.set_compile_option("ghdl.a_flags", ["-frelaxed", "--warn-no-hide", "--warn-no-specs"])
lib.set_sim_option("ghdl.elab_flags", ["-frelaxed"])
lib.set_sim_option("ghdl.sim_flags", ["--max-stack-alloc=0"])
```

注意 `add_compile_option`（全局，作用于所有库）与 `set_compile_option`（仅 `lib` 库）的区别。`--max-stack-alloc=0` 是为了避免 GHDL 在运行期栈分配上的限制问题。

**NVC 选项**（[sim/run.py:216-219](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L216-L219)）：

```python
vu.add_compile_option("nvc.a_flags", ["--relaxed", "--check-synthesis"])
lib.set_sim_option("nvc.global_flags", ["-M 256m"])   # 大设计时调大
lib.set_sim_option("nvc.heap_size", "64m")            # 大数据量时调大
```

`--check-synthesis` 让 NVC 在编译期做可综合性检查；`-M 256m` / `heap_size 64m` 是为大数据量（穷举测试要跑成千上万个用例）预留内存。

**Modelsim / Questa 选项**（[sim/run.py:221-226](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L221-L226)）：

```python
lib.set_compile_option("modelsim.vcom_flags", ["+cover=sbceft", "-check_synthesis", "-coverdeglitch", "0", "-suppress", "143"])
lib.set_compile_option("modelsim.vlog_flags", ["+cover=sbceft"])
lib.set_sim_option("modelsim.vsim_flags", ["-t 1ps", "-voptargs=+acc"])
if args.simulator == 'questa' and args.gui == False:
    lib.set_sim_option("modelsim.three_step_flow", True)
```

`+cover=sbceft` 开启覆盖率统计，`-check_synthesis` 做可综合性检查，`-suppress 143` 抑制某条已知无害的警告（编号 143）。最后一行是 Questa 专属优化：仅在 Questa 且非 GUI 时启用 `three_step_flow`（一种加速大批量仿真的流程）。

> `args.gui` 是 VUnit 自带的命令行选项（`-g`），不在 `common.py` 的自定义参数里——它是 VUnit `VUnitCLI` 默认就提供的。

**全仿真器通用选项**（[sim/run.py:228-231](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L228-L231)）：`disable_ieee_warnings=True` 关闭 IEEE 库的警告（测试里常有意触发的边界情况会产生大量 ieee 警告），`enable_coverage` 仅在 `--coverage` 时打开。

**GUI 波形自动加载**（[sim/run.py:233-237](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L233-L237)）：遍历所有 TB，为每个 TB 绑定对应仿真器的波形初始化脚本（`scripts/<tb>_wave.do` 给 Modelsim、`scripts/<tb>_wave.cmd` 给 GHDL/NVC）。这些选项只在 GUI 模式（`-g`）下生效，命令行批跑时自动忽略——这是个让「批跑」与「调试」共用同一份 `run.py」的优雅设计。

#### 4.5.4 代码实践

**实践目标**：把每条选项归到正确的仿真器，理解「键名隔离」。

**操作步骤**：

1. 打开 [sim/run.py:210-231](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L210-L231)。
2. 仿照下表，把每个选项键填上它服务的仿真器与作用：

   | 选项键 | 仿真器 | 作用（用自己的话） |
   | --- | --- | --- |
   | `ghdl.a_flags` | | |
   | `ghdl.sim_flags`=`--max-stack-alloc=0` | | |
   | `nvc.a_flags`=`--check-synthesis` | | |
   | `nvc.heap_size` | | |
   | `modelsim.vcom_flags` | | |
   | `disable_ieee_warnings` | 全部 | |

**需要观察的现象 / 预期结果**：你应当发现——当你用 GHDL 跑时，NVC 与 Modelsim 的键名被 VUnit 自动忽略；反之亦然。这就是为什么 `run.py` 可以把四类仿真器的选项「平铺」写在一起而不互相干扰。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ghdl.a_flags` 用了 `add_compile_option`（加在 `vu` 上），而 `nvc.global_flags` 用 `set_sim_option`（加在 `lib` 上）？

**答案**：`add_compile_option` 是「全局追加」（作用于所有库的编译），`set_sim_option` 是「在本库设置仿真选项」。作者按需要选择作用域：GHDL 的某些编译警告想全局生效，而 NVC 的内存/堆参数只需对本项目 `lib` 库生效即可。

**练习 2**：如果某条 Modelsim 警告（如编号 143）其实是有用的，想看到它，该改哪里？

**答案**：去掉 [sim/run.py:222](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L222) 里 `modelsim.vcom_flags` 列表中的 `"-suppress", "143"` 两项即可。这是项目针对已知无害警告做的「静音」处理。

---

## 5. 综合实践

把本讲三个脚本串起来，完成下面这个「全链路追踪」小任务。

**任务**：从一条仿真命令出发，追踪到 cosim 生成黄金数据、再到 TB 读取比对的完整装配过程。

**操作步骤**：

1. **起点命令**（来自 README「Running Tests」一节，[README.md:196](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L196)）：

   ```bash
   python run.py --simulator=ghdl --simulator-path='C:/msys64/mingw64/bin'
   ```

2. **追踪参数流**：这条命令的 `--simulator=ghdl` 在哪里被解析？→ [sim/common.py:31-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L31-L35)。它如何影响 VHDL 标准？→ [sim/common.py:85-87](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L85-L87)（ghdl → rtl=2008, tb=2008）。

3. **追踪源文件装载**：哪些文件按 `2008` 编译？→ [sim/run.py:43-50](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L43-L50)。注意 GHDL 下 RTL 也用 2008（与 Modelsim 下的 93 不同）。

4. **追踪一个 TB 的配置**：选 `cl_fix_round`。它的 cosim 路径、generics、pre_config 各是什么？→ [sim/run.py:156-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L156-L165)。

5. **追踪 pre_config 的执行**：`cl_fix_round_cosim.run` 在 VUnit 仿真 round 之前被调用，它如何保证只跑一次？→ [sim/cosim_runner.py:59-72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L59-L72)。它调用的 `module_dict["run"]()` 是哪个函数？→ [bittrue/cosim/cl_fix_round/cosim.py:38](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_round/cosim.py#L38) 的 `run()`，它在 `data/` 下生成 `a_fmt.txt`、`r_fmt.txt`、`rnd.txt`、`testN_output.txt`（u7-l1）。

6. **追踪 TB 读取**：仿真启动后，`cl_fix_round_tb` 从这些文件读取黄金数据逐拍比对（u7-l2）。

**产出**：画一张「命令 → 参数解析 → VHDL 标准 → 源文件装载 → TB 配置 → pre_config(cosim) → 黄金数据 → TB 比对」的流程图，并在每个节点标注对应的源码链接与行号。

**预期结果**：你能用这一张图把 u7-l1（cosim）、u7-l2（TB）、u7-l3（装配）三讲完整闭环，并说清「`run.py` 在这中间扮演的就是把两端粘合起来的装配层」。

---

## 6. 本讲小结

- `sim/` 下三个 Python 脚本分工明确：`common.py` 是「公共底座」（参数 + 环境变量 + VHDL 标准），`run.py` 是「装配总指挥」（装库、绑配置、设选项），`cosim_runner.py` 是「cosim 执行器」（线程安全单次执行）。
- `common.py` 在 VUnit 命令行上挂了 5 个自定义参数（`--simulator` 等），每个都有环境变量兜底；并按仿真器产出 `vhdl_standard_rtl` / `vhdl_standard_tb` 两个常量——Modelsim/Questa 下 RTL 用 93、TB 用 2008，GHDL/NVC 下都用 2008。
- `create_test_suite` 把 RTL（`hdl/`）、TB 工具（`tb/util/`）、TB 主体（`tb/`）分批装入 `lib` 库，外加共享的 `en_tb` 子库与 VUnit 自带的 OSVVM 库；`en_tb` 用 `try/except` 防重复创建。
- 每个 TB 通过 `add_config` 把三件套绑在一起：cosim 脚本路径（`cosim("xxx")` 构造时定）、generics、`pre_config` 回调（绑到 `xxx_cosim.run`）。round/saturate/resize 三个 TB 因有 `meta_width_g`，用 `[0, 8]` 两种宽度各配一遍。
- `cosim_runner` 用「全局锁保护 sys.path + 局部双重检查锁 + 自禁用」保证同名 `cosim.py` 安全导入、且 `run()` 在并行测试下最多执行一次；`--disable-cosim` 通过初始 `enable=False` 短路。
- 末尾按 GHDL / NVC / Modelsim 分别设置编译/仿真选项，键名互不干扰；并为 GUI 模式自动绑定各仿真器的波形脚本，使批跑与调试共用同一份 `run.py`。

---

## 7. 下一步学习建议

本讲讲完之后，U7「协同仿真验证流程」三段（cosim 生成 → TB 比对 → run.py 装配）已经闭环。接下来：

- **如果想深入「工程规避」**：进入 U8。建议先读 u8-l2「工具链 bug 规避与综合友好性」，因为本讲已经接触到了不少「为特定工具打补丁」的代码（questa→modelsim 名字 workaround、`-suppress 143`、`--max-stack-alloc=0`、`real_mod` 等），U8 会系统梳理这类规避手段。
- **如果想看「另一条验证线」**：读 u8-l3「Python 单元测试与格式最优性验证」，了解 `bittrue/tests/python/` 下的纯 Python 测试如何独立于仿真器验证算法正确性与格式预测的「充分且必要」性。
- **如果想在 `run.py` 上动手**：可以尝试仿照 round 块，为某个目前用形态 A 的算术 TB（如 `cl_fix_add`）添加一组带 generic 的多配置；或为某个 cosim 脚本补一个 `COSIM_CONFIG` 字典并验证 `get_config()`（[sim/cosim_runner.py:50-57](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L50-L57)）能否读到它——这是目前尚未启用的扩展点。
- **源码延伸阅读**：对照 [bittrue/cosim/cosim_utils.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cosim_utils.py)（cosim 公共脚手架 `clear_directory` / `get_data` / `ProgressReporter`），理解 cosim 脚本与 cosim_runner 之间的契约。
