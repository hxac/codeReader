# 开发环境搭建与本地仿真运行

## 1. 本讲目标

学完本讲后，你应当能够：

- 按照 README 的步骤，用 **Python 虚拟环境（venv）** 安装 VUnit 及其依赖（`requirements.txt`）。
- 运行 [test_runner.py](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py) 完成一次**全量仿真**，并看懂它打印的「通过 / 失败」结果。
- 说清 `test_runner.py` → `run_all_testbenches_lib` → VUnit 这**三层调用关系**，以及为什么用户不需要直接碰 VUnit。
- 逐个理解 `run_all_testbenches_lib` 的关键参数（`tb_pattern`、`timeout_ms`、`gui`、`use_xilinx_libs`、`excluded_list` 等）的含义，并能动手改它们观察行为变化。
- 解释 VUnit 是**如何自动发现** `tb_*.vhd` 测试台和其中每一个测试用例的（即 `run_all_in_same_sim` 之外的发现机制）。

本讲是「从读到跑」的转折点：u1-l2 让你会找文件，本讲让你**把这些测试台真正运行起来**。

## 2. 前置知识

承接前两讲，你已经知道：

- 这是一个 **VHDL-2008 可复用 IP 核库**，每个 IP 都配了 `tb/tb_<ip>.vhd` 测试台（u1-l2 的「三件套」约定）。
- 验证三件套是 **VUnit + OSVVM + 仿真器**，其中 VUnit 负责「自动发现并批量跑测试台」（u1-l1）。
- 项目里有一个外部 git 子模块 `ip/vhdl_utils`，本地的 `run_all_testbenches_lib` 正是从它导入的。

本讲用到几个新术语：

- **VUnit**：一个用 Python 编写的 VHDL 验证框架。它在「上层」用 Python 脚本帮你扫描文件、调用仿真器、汇总结果；在「下层」提供一个 VHDL 库 `vunit_lib`，里面有 `check_equal`、`test_runner_setup` 等测试台用的过程。可以把它类比成 VHDL 版的 pytest。
- **仿真器（simulator）**：真正执行 VHDL 仿真的工具，如 ModelSim / QuestaSim / NVC / Aldec Riviera Pro。VUnit 自己不仿真，它负责「指挥」这些仿真器。本库本地默认用 ModelSim/QuestaSim，CI 里用 NVC（见 u1-l4）。
- **虚拟环境（venv）**：Python 的隔离环境，把 VUnit 等依赖装在里面，不污染系统 Python。
- **runner_cfg**：VUnit 约定的「魔法 generic」。一个 VHDL entity 只要带有 `runner_cfg : string` 这个类属端口，VUnit 就把它识别为「测试台」并接管它。

> 术语补充：**glbl 模块**是 Xilinx 仿真库里的一个全局模块（模拟上电复位 `GSR`）。一旦你的设计例化了 Xilinx 的 XPM 原语，仿真器就必须加载 `glbl`，否则报错——这正是 `use_xilinx_libs` 这个开关要解决的问题（4.4 节详述）。

## 3. 本讲源码地图

本讲的主角是三个文件，外加一个子模块声明：

| 路径 | 作用 |
| --- | --- |
| `ip/test_runner.py` | 本地仿真入口。一个薄包装器，把 VUnit 的复杂性藏起来。 |
| `ip/requirements.txt` | Python 依赖清单，核心是 `vunit_hdl`。 |
| `README.md` | 环境搭建与运行说明的权威来源（Minimum System Requirements / Initial Setup / Running simulation 三节）。 |
| `.gitmodules` | 声明 `ip/vhdl_utils` 子模块——`test_runner.py` 导入的 `run_all_testbenches_lib` 就在里面。 |
| `ip/communication/spi/tb/tb_spi_tx.vhd` | 一个真实 VUnit 测试台的样例，用来观察 `runner_cfg` generic 与 `run_all_in_same_sim` pragma。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 环境准备：Python venv 与 `requirements.txt`**
- **4.2 `test_runner.py`：VUnit 的薄包装器**
- **4.3 `run_all_testbenches_lib` 与 VUnit 测试发现机制**
- **4.4 关键参数与排错：`use_xilinx_libs` / `gui` / `timeout_ms`**

---

### 4.1 环境准备：Python venv 与 `requirements.txt`

#### 4.1.1 概念说明

要跑这个项目的仿真，光有 VHDL 仿真器还不够，还需要 **Python + VUnit** 这一层「调度层」。原因很简单：VUnit 是用 Python 写的，它先在 Python 这边完成「找文件 → 排编译顺序 → 调用仿真器 → 收集结果」，再把这些结果汇总成一份通过/失败报告。

所以最小系统需求里明确列了两类东西：

- **仿真器**：任何支持 **VHDL-2008** 的仿真器（ModelSim / QuestaSim / NVC / Riviera Pro 等）。
- **脚本执行环境**：**Python 3.11.4**，用来通过 VUnit 自动化测试。

这两条来自 README 的「Minimum System Requirements」一节。

#### 4.1.2 核心流程：从 clone 到装好依赖

完整的本地准备流程可以画成一条线性链：

```text
  git clone（含子模块）
        │
        ▼
  python -m venv .venv          ← 创建虚拟环境
        │
        ▼
  source .venv/bin/activate     ← 激活（Windows 用 .\.venv\Scripts\activate）
        │
        ▼
  pip install -r ip/requirements.txt   ← 安装 VUnit + TerosHDL 工具链
        │
        ▼
  （还要确保仿真器在 PATH 里 / VUNIT_MODELSIM_PATH 已设置）
        │
        ▼
  运行 test_runner.py（4.2 节）
```

这里有一个**极易踩坑、但 README 只隐约提到**的点：仓库依赖一个 git 子模块 `ip/vhdl_utils`，而 `test_runner.py` 第一行导入的就是 `from vhdl_utils.run_all_testbenches_lib import ...`。如果你只是普通 `git clone`，`ip/vhdl_utils/` 会是**空目录**，运行 `test_runner.py` 会立刻报 `ModuleNotFoundError`。因此必须在 clone 后补一步初始化子模块：

```bash
git submodule update --init --recursive
```

> 子模块来源见 [.gitmodules:L1-L3](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/.gitmodules#L1-L3)：它指向 `https://github.com/nselvara/VHDL-Utils.git`，挂载在 `ip/vhdl_utils`。这个子模块的细节会在 u3-l2 专讲，本讲你只需记住「必须 init 子模块才能编译」。

#### 4.1.3 源码精读：`requirements.txt` 与系统需求

**最小系统需求** —— [README.md:L142-L156](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L142-L156)

README 在这里点明了两条硬性要求：仿真器要支持 VHDL-2008；Python 用 `3.11.4`。同时还推荐了一套 VSCode 插件（Python / Pylance / TerosHDL / VHDL-LS / Draw.io），用于代码导航与文档生成——这些插件是「开发体验」加分项，不是「跑仿真」的必需品。

**Python 依赖清单** —— [ip/requirements.txt:L1-L8](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/requirements.txt#L1-L8)

```text
vunit_hdl>=5.0.0.dev5

# packages for TerosHDL
teroshdl
cocotb
yowasp-yosys
edalize
vsg
```

- 第 1 行 `vunit_hdl>=5.0.0.dev5` 是**唯一与「跑仿真」直接相关**的依赖——它就是 VUnit 的 Python 包。版本约束 `>=5.0.0.dev5` 说明项目用的是 VUnit 5.x（一个较新的开发版），这会影响某些 API 行为。
- 第 3–8 行是 TerosHDL 工具链的依赖（`cocotb`、`yowasp-yosys`、`edalize`、`vsg` 等），用于文档生成、代码风格检查等，与仿真本身无关。如果你只想跑测试，最小安装其实只要 `vunit_hdl`，但按 README 流程全装最省事。

**虚拟环境的两种创建方式** —— [README.md:L166-L186](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L166-L186)

README 提供了 GUI 与终端两条路：

- GUI（L168–L176）：VSCode 里 `Ctrl+Shift+P` → `Python: Create Environment` → 选 `Venv` → 选 `requirements.txt`，由 VSCode 自动创建并激活。
- 终端（L178–L186）：

  ```bash
  python3 -m venv .venv               # Linux
  source .venv/bin/activate
  pip install -r requirements.txt
  ```

> 注意：README 的 `pip install` 写的是 `pip install -r requirements.txt`，而你实际运行时要在**仓库根目录**执行，且 `requirements.txt` 在 `ip/` 下，所以更稳妥的写法是 `pip install -r ip/requirements.txt`（或先 `cd ip`）。

#### 4.1.4 代码实践：核对依赖

1. **实践目标**：搞清楚「跑这个项目最少需要装什么」，并发现子模块陷阱。
2. **操作步骤**：
   1. 打开 `ip/requirements.txt`，圈出与仿真直接相关的那个包。
   2. 打开 `.gitmodules`，确认子模块挂载点。
   3. 在终端进入仓库根目录，执行 `ls ip/vhdl_utils/`，看目录是否为空。
3. **需要观察的现象**：
   - `requirements.txt` 第 1 行是 `vunit_hdl`。
   - `.gitmodules` 里子模块路径是 `ip/vhdl_utils`。
   - 如果你没执行 `git submodule update --init`，`ip/vhdl_utils/` 会是空目录。
4. **预期结果**：你能用一句话回答「最小依赖 = Python venv + vunit_hdl + 一个 VHDL-2008 仿真器 + 初始化 vhdl_utils 子模块」。
5. **待本地验证**：若 `ip/vhdl_utils/` 为空，执行 `git submodule update --init --recursive` 后重新 `ls`，应能看到子模块内的文件。

#### 4.1.5 小练习与答案

**练习 1**：如果你只 `git clone` 了仓库就运行 `test_runner.py`，最可能看到什么报错？怎么修？

> **参考答案**：会看到 `ModuleNotFoundError: No module named 'vhdl_utils'`，因为 `test_runner.py` 第 16 行 `from vhdl_utils.run_all_testbenches_lib import ...` 找不到子模块。修复：执行 `git submodule update --init --recursive` 拉取 `ip/vhdl_utils`。

**练习 2**：`requirements.txt` 里那么多包，哪个是「跑仿真」真正必需的？

> **参考答案**：`vunit_hdl>=5.0.0.dev5`。其余（`teroshdl`/`cocotb`/`yowasp-yosys`/`edalize`/`vsg`）是 TerosHDL 文档/工具链相关，仿真本身用不到。

---

### 4.2 `test_runner.py`：VUnit 的薄包装器

#### 4.2.1 概念说明

VUnit 虽然强大，但它的 Python API 有一定学习成本（要手动建 `VUnit` 对象、加 source 文件、设 library、配 compile option、设 test options……）。本项目不想让用户每次都写这些样板代码，于是写了一个 **`test_runner.py` 薄包装器（wrapper）**：它把所有 VUnit 细节封装进一个从子模块导入的函数 `run_all_testbenches_lib`，用户只需要执行这一个 `.py` 文件，就能「一键编译 + 仿真全部测试台」。

README 对它的定位说得很直白：**「The script `test_runner.py` acts as a wrapper, so you don't need to deal with VUnit internals.」**

#### 4.2.2 核心流程：三层调用关系

理解本库的仿真入口，关键是看清**三层调用链**：

```text
  用户执行                              你需要改的层
  ─────────                            ──────────
  ./.venv/Scripts/python.exe            （命令行）
        ip/test_runner.py
              │                          ← 本地包装器（可改参数）
              ▼
        run_all_testbenches_lib(...)      ← 来自 vhdl_utils 子模块（藏实现）
              │
              ▼
        VUnit Python API                  ← 真正的 VUnit 框架
              │
              ▼
        调用仿真器（ModelSim/QuestaSim）   ← 执行 VHDL 仿真
```

- **第一层 `test_runner.py`**：用户可见、可改。它只做两件事——调一次 `run_all_testbenches_lib(...)`，再根据返回码打印「Passed/Failed」。
- **第二层 `run_all_testbenches_lib`**：来自 `ip/vhdl_utils` 子模块，源码不在本仓库（待确认细节）。它把 VUnit 的建对象、加文件、配库、跑测试全部封装好。
- **第三层 VUnit**：真正的框架，最终会去调用仿真器。

这套分层的好处是：**普通用户只面对第一层**，改改参数就能用；想深入才需要去读第二层和第三层。

#### 4.2.3 源码精读：`test_runner.py` 逐行

整个文件不到 40 行，我们逐段看。

**文件头与导入** —— [ip/test_runner.py:L1-L17](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L1-L17)

```python
"""
Run all testbenches in the project using VUnit.
It functions as a wrapper to not bother the user with the details of VUnit.

WARNING: If your design uses Xilinx primitives (XPM, UNISIM, etc.),
you MUST set use_xilinx_libs=True to avoid "glbl" module errors.
See README.md for more details.
"""
import sys
import os

from vhdl_utils.run_all_testbenches_lib import main as run_all_testbenches_lib
from vhdl_utils.run_all_testbenches_lib import bcolours
```

- 开头的 docstring 直接点明了这个文件的角色：「wrapper，不让用户操心 VUnit 细节」，并复述了 Xilinx 库的警告。
- 第 16 行把子模块里的 `main` 函数**起个别名**叫 `run_all_testbenches_lib`，下文就用这个名字调用。第 17 行还导入了 `bcolours`——它是一个给终端输出上色的小工具（绿色表示通过、红色表示失败）。

**核心调用** —— [ip/test_runner.py:L19-L36](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L19-L36)

```python
def run_all_testbenches():
    returncode = run_all_testbenches_lib(
        path="./ip/",                 # Path where the HDL & tb files are located
        tb_pattern="**",              # Match all testbenches
        timeout_ms=1.0,               # Timeout in milliseconds
        gui=False,                    # Set to True to open ModelSim/QuestaSim GUI
        compile_only=False,           # Only compile, don't run simulations
        clean=False,                  # Clean before building
        debug=False,                  # Enable debug logging
        use_xilinx_libs=True,         # Add Xilinx simulation libraries, note set it true to load glbl module
        use_intel_altera_libs=False,  # Add Intel/Altera simulation libraries
        excluded_list=[],             # List of testbenches to exclude
        xunit_xml=None                # Output file for test results
    )
    print(
        f"hdl_offline_tests: {bcolours.OKGREEN + 'Passed' if returncode == 0 else bcolours.FAIL + 'Failed'}{bcolours.ENDC}"
    )
    return returncode
```

这段就是全文件的「心脏」：把 11 个参数喂给 `run_all_testbenches_lib`，拿到一个返回码 `returncode`。最后那行 `print` 用三目表达式决定打印绿色 `Passed`（返回码为 0）还是红色 `Failed`（非 0）。这就是你跑完仿真后看到的那行结论性输出。

**入口守卫** —— [ip/test_runner.py:L38-L39](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L38-L39)

```python
if __name__ == "__main__":
    exit(run_all_testbenches())
```

标准的 Python 入口：直接执行此文件时，调用 `run_all_testbenches()` 并把返回码作为进程退出码（0 = 成功，非 0 = 失败）。CI 可以据此判断测试是否通过。

> **重要提醒（README 与实际文件不一致）**：README 的「Optional Customization」示例块（[README.md:L280-L292](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L280-L292)）里写的是 `use_xilinx_libs=False` 和 `xunit_xml="./test/res.xml"`，但**磁盘上真实的 `test_runner.py`** 是 `use_xilinx_libs=True`（L28）和 `xunit_xml=None`（L31）。以**真实文件为准**。差异原因：本库的测试台普遍例化了 Xilinx XPM 原语，所以默认就开 `use_xilinx_libs=True` 以加载 `glbl` 模块；本地默认不产出 xunit 报告（报告是 CI 才需要的）。

#### 4.2.4 代码实践：跑第一次仿真

1. **实践目标**：亲手完成一次全量仿真，看到那行「Passed」输出。
2. **操作步骤**：
   1. 按本仓库 README，准备好 `VUNIT_MODELSIM_PATH` 环境变量（指向你的 ModelSim/QuestaSim 安装目录）。Linux 示例见 [README.md:L236-L244](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L236-L244)，Windows 示例见 [README.md:L246-L253](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L246-L253)。
   2. 激活虚拟环境后，在仓库根目录执行 README 给的命令：

      ```bash
      ./.venv/Scripts/python.exe ./ip/test_runner.py
      ```

      （Linux 下通常是 `.venv/bin/python ./ip/test_runner.py`。）
3. **需要观察的现象**：终端会先打印一连串「编译 / 加载 / 仿真」日志（由 VUnit 驱动仿真器产出），最后打印一行 `hdl_offline_tests: Passed`（绿色）或 `Failed`（红色）。
4. **预期结果**：所有 `tb_*.vhd` 测试台都被编译并仿真，最后输出 `Passed`。
5. **待本地验证**：实际输出取决于本机是否装好仿真器与厂商库。若报 `glbl.GSR` 相关错误，说明 `use_xilinx_libs` 或厂商库路径有问题（见 4.4）。若没有本地仿真器，可改用 README 的 **Option 1: EDA Playground**（[README.md:L193-L223](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L193-L223)）在网页里跑单个测试台。

> 说明：本讲不假装已经替你跑过命令——仿真结果依赖你本机的仿真器与厂商库是否就绪，这恰恰是 4.4 节要讲的排错重点。

#### 4.2.5 小练习与答案

**练习 1**：为什么本项目要写一个 `test_runner.py`，而不是让用户直接写 VUnit 的 Python 脚本？

> **参考答案**：VUnit 的原生 API 需要手动建对象、加 source、配 library、设 option 等一堆样板代码。`test_runner.py` 把这些封装进子模块里的 `run_all_testbenches_lib`，让普通用户只需面对十几个语义清晰的参数，不必学习 VUnit 内部 API。

**练习 2**：`test_runner.py` 最后一行 `print` 里的 `returncode == 0` 代表什么？为什么要把 `returncode` 再 `return` 出去？

> **参考答案**：VUnit 用 `0` 表示全部通过、非 `0` 表示有失败。把返回码打印成 Passed/Failed 是给人看；再 `return returncode` 并经 `exit()` 作为进程退出码，是给 CI 看——CI 据此判定整次构建是绿是红。

---

### 4.3 `run_all_testbenches_lib` 与 VUnit 测试发现机制

#### 4.3.1 概念说明

`run_all_testbenches_lib` 是 `test_runner.py` 调用的那个函数，它的实现在 `ip/vhdl_utils` 子模块里（本仓库未检入源码，故标注**待确认**）。但从它的参数名、README 说明以及测试台的实际写法，我们可以准确推断它做了什么：它把「扫描 `tb_*.vhd` → 识别测试台 → 提取每个测试用例 → 编译 → 仿真 → 汇总」这条流水线全部自动化。

这里要分清两个层次的「发现」：

1. **发现测试台（testbench）**：VUnit 在磁盘上找到「哪些文件是测试台」。
2. **发现测试用例（test case）**：一个测试台文件内部，往往包含多个用 `run("用例名")` 定义的小用例，VUnit 要把它们逐个识别出来。

#### 4.3.2 核心流程：VUnit 的两层发现机制

```text
  第一层：发现测试台（编译前，磁盘扫描）
  ─────────────────────────────────────
  run_all_testbenches_lib(path="./ip/", tb_pattern="**")
        │
        ├─ 在 ./ip/ 下递归找所有 tb_*.vhd（pattern "**"）
        │
        └─ 识别标志：entity 带有 runner_cfg : string 这个 generic
              → VUnit 把它判定为「测试台」并接管

  第二层：发现测试用例（运行时，由测试台自身声明）
  ─────────────────────────────────────
  每个 tb 在 test_suite 循环里用 run("用例名") 声明用例
        │
        ├─ 默认：每个用例单独一次仿真（独立 elaborate + load + run）
        │
        └─ 若文件顶部写了 -- vunit: run_all_in_same_sim
              → 该 tb 的所有用例合并到同一次仿真里跑（更快）
```

关键认知：**VUnit 不是靠文件名「猜」哪个是测试台，而是靠 `runner_cfg` generic 这个语义标志**。文件名 `tb_*.vhd` 只是 `run_all_testbenches_lib` 用来「缩小扫描范围」的通配符；真正让 VUnit 把一个 entity 当测试台对待的，是它声明了 `runner_cfg : string`（通常写成 `runner_cfg : string := runner_cfg_default`）。

#### 4.3.3 源码精读：测试台里的「发现标志」

用一个真实测试台 `tb_spi_tx.vhd` 来印证上面的机制。

**`run_all_in_same_sim` pragma** —— [ip/communication/spi/tb/tb_spi_tx.vhd:L8](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L8)

```vhdl
-- vunit: run_all_in_same_sim
```

这一行以 `-- vunit:` 开头的注释，是 VUnit 专用的**编译指示（pragma）**：它告诉 VUnit「这个测试台里的所有 `run(...)` 用例，请在同一次仿真里顺序跑完」，而不是每个用例各起一次仿真。好处是省去重复的 elaborate/load 开销，跑得更快；代价是用例之间共享同一次仿真的初始状态（所以测试台通常会在每个用例开头自己复位 DUT）。

> 这就回答了实践任务里那个问题——**除了 `run_all_in_same_sim` 之外，VUnit 是如何发现测试的？** 答案有两条：① 测试台层面，靠 `runner_cfg` generic 这个语义标志被发现（外加 `tb_*.vhd` 通配缩小范围）；② 测试用例层面，靠测试台 `test_suite` 循环里的每一个 `run("用例名")` 调用被发现。`run_all_in_same_sim` 不参与「发现」，它只影响「已发现的用例如何被编组执行」（合并到一次仿真 vs 各跑一次）。

**VUnit 库与 context** —— [ip/communication/spi/tb/tb_spi_tx.vhd:L15-L16](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L15-L16)

```vhdl
library vunit_lib;
context vunit_lib.vunit_context;
```

这两行引入 VUnit 的 VHDL 库，使测试台能用 `test_runner_setup`、`check_equal`、`runner_cfg_default` 等定义。没有这两行，上面的 `runner_cfg` 标志就无从谈起。

**测试台的「魔法 generic」** —— [ip/communication/spi/tb/tb_spi_tx.vhd:L25-L30](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L25-L30)

```vhdl
entity tb_spi_tx is
    generic (
        runner_cfg: string := runner_cfg_default;
        tb_path: string
    );
end entity;
```

- `runner_cfg : string := runner_cfg_default` 就是 VUnit 识别测试台的「身份证」。VUnit 在 elaboration 前会把这个字符串注入进来，里面编码了「当前要跑哪个用例、输出到哪、是否安静模式」等运行时信息。**任何一个 entity 只要带这个 generic，VUnit 就把它当测试台**——这是发现机制的核心。
- `tb_path : string` 是本项目自定义的类属，用来让测试台找到自己同目录的数据文件（比如 ROM 的初始化 hex 文件），由 `run_all_testbenches_lib` 自动填充。

**README 对发现机制的描述** —— [README.md:L267-L273](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L267-L273)

README 的「What the script does」用三点概括了包装器行为：内部用 `run_all_testbenches_lib`（藏实现）、在 `./ip/` 里查找、递归匹配 `tb_*.vhd`（pattern `**`）。这与我们上面的推断完全吻合。

#### 4.3.4 代码实践：观察测试台的「发现标志」

1. **实践目标**：亲眼确认 VUnit 发现测试台与测试用例所依赖的两类标志。
2. **操作步骤**：
   1. 打开 [ip/communication/spi/tb/tb_spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd)。
   2. 在文件顶部（前 30 行）找到：`-- vunit: run_all_in_same_sim`、`library vunit_lib`、`entity ... generic ( runner_cfg ... )`。
   3. 在文件正文中搜索 `test_suite` 与 `run("`，数一数这个测试台声明了几个用例（每个 `run("xxx")` 就是一个用例）。
3. **需要观察的现象**：文件顶部同时存在 pragma、VUnit context、`runner_cfg` generic；正文里 `test_suite` 循环下有多个 `run(...)` 调用。
4. **预期结果**：你能指出「测试台被发现的标志 = `runner_cfg` generic」「用例被发现的标志 = `run(...)` 调用」「合并执行 = `run_all_in_same_sim` pragma」三者各自的位置。
5. **待本地验证**：若本地装了 VUnit，可临时把 `runner_cfg` 那行 generic 注释掉再跑 `test_runner.py`，观察 VUnit 是否还能发现这个测试台（预期：不能，它会被当成普通源文件，不会被执行）。

#### 4.3.5 小练习与答案

**练习 1**：如果有一个文件叫 `tb_foo.vhd`，但它的 entity 里**没有** `runner_cfg` generic，VUnit 会把它当测试台吗？

> **参考答案**：不会。`tb_*.vhd` 只是缩小扫描范围的通配符；VUnit 真正判定测试台的依据是 `runner_cfg : string` 这个 generic。没有它，文件即使以 `tb_` 开头，也只会被当作普通设计源码编译，不会被执行。

**练习 2**：`run_all_in_same_sim` 改变了「发现」还是「执行」？

> **参考答案**：只改变**执行**方式——把同一个测试台里已发现的多个 `run(...)` 用例，合并到一次仿真里顺序跑（而非每个用例各起一次仿真）。它不参与「发现哪些用例」，发现仍由 `run(...)` 调用决定。

**练习 3**：`run_all_testbenches_lib` 这个函数来自哪里？为什么本仓库看不到它的实现？

> **参考答案**：它来自 `ip/vhdl_utils` 子模块（外部仓库 `VHDL-Utils`），通过 `from vhdl_utils.run_all_testbenches_lib import main as run_all_testbenches_lib` 导入。本仓库未检入子模块源码，所以看不到实现（实现细节标注为待确认，u3-l2 会专讲子模块）。

---

### 4.4 关键参数与排错：`use_xilinx_libs` / `gui` / `timeout_ms`

#### 4.4.1 概念说明

`run_all_testbenches_lib` 接受一长串参数，它们都集中在 [test_runner.py:L20-L32](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L20-L32)。这些参数决定了「在哪找、找什么、怎么跑、跑多久、要不要 GUI、要不要排除」。理解它们，你就能在不碰 VUnit 内部的前提下精细控制仿真。

README 也单独列出了这套参数及其注释，见 [README.md:L275-L293](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L275-L293)。

#### 4.4.2 核心流程：参数速查表

| 参数（真实文件中的取值） | 含义 | 改动后的影响 |
| --- | --- | --- |
| `path="./ip/"` | HDL 与 tb 文件的根目录 | 改了就找不到文件 |
| `tb_pattern="**"` | 测试台匹配模式（递归） | 可改成只跑某子目录，如 `"memories/**"` |
| `timeout_ms=1.0` | 单个测试用例的仿真超时（毫秒） | 调大可避免长测试被看门狗误杀 |
| `gui=False` | 是否打开 ModelSim/QuestaSim 的图形界面 | `True` 会弹窗，可手动看波形、单步调试 |
| `compile_only=False` | 只编译不仿真 | 用于快速检查语法/编译错误 |
| `clean=False` | 编译前先清理 | 解决「增量编译缓存导致诡异报错」 |
| `debug=False` | 打开调试日志 | 排查编译/加载问题时开启 |
| `use_xilinx_libs=True` | 加载 Xilinx 仿真库（含 `glbl`） | 用到 XPM/UNISIM 原语时**必须为 True** |
| `use_intel_altera_libs=False` | 加载 Intel/Altera 仿真库 | 选用 Intel 架构时开启 |
| `excluded_list=[]` | 要排除的测试台列表 | 临时跳过不稳定/不适用的 tb |
| `xunit_xml=None` | 测试结果 xunit XML 输出路径 | CI 用来汇总报告（本地默认不产出） |

其中最常被改、也最容易出问题的是三个：`use_xilinx_libs`、`gui`、`timeout_ms`。

#### 4.4.3 源码精读：三个关键参数

**① `use_xilinx_libs` 与 `glbl` 报错** —— [test_runner.py:L28](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L28) + [README.md:L295-L302](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L295-L302)

真实文件里这一行是：

```python
use_xilinx_libs=True,         # Add Xilinx simulation libraries, note set it true to load glbl module
```

行内注释直接点题：「设为 True 以加载 glbl 模块」。README 紧跟一个 `> [!WARNING]` 段落解释：一旦设计用了 Xilinx 原语（`xpm_cdc`、`xpm_memory` 等），不打开此开关就会报 `Failed to find 'glbl' in hierarchical name 'glbl.GSR'` / `Error loading design`；打开后会自动引入 `glbl` 模块和 `-L xpm -L unisims_ver -L secureip` 等仿真库。**因为本库的测试台普遍例化了 XPM 原语，所以默认就是 `True`。**

**② `gui` 与 `timeout_ms`** —— [test_runner.py:L23-L24](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L23-L24)

```python
timeout_ms=1.0,               # Timeout in milliseconds
gui=False,                    # Set to True to open ModelSim/QuestaSim GUI
```

- `timeout_ms=1.0` 是 VUnit **watchdog（看门狗）** 的超时：每个测试用例仿真超过 1 毫秒（仿真时间，非墙钟时间）还没结束，就判失败。本库大多数测试跑得很快，1ms 足够；但如果你写了一个需要长仿真时间的用例，就要调大它，否则会被误杀。
- `gui=False` 默认无界面、跑完即退（适合 CI 和快速回归）。设 `True` 会打开 ModelSim/QuestaSim 的波形窗口，你可以手动加信号、看波形、单步——这是调试时最常用的开关。

**③ `excluded_list` 与 `compile_only`** —— [test_runner.py:L25-L30](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L25-L30)

```python
compile_only=False,           # Only compile, don't run simulations
clean=False,                  # Clean before building
...
excluded_list=[],             # List of testbenches to exclude
```

- `excluded_list=[]`：本地的 `test_runner.py` 不排除任何测试台。但 CI 专用的 `test_runner_ci_cd.py` 会把 `tb_pll.vhd` 等需要硬核厂商资源的测试台放进排除列表（因为 CI 的 NVC 环境跑不了 PLL）。这个差异会在 u1-l4 详讲。
- `compile_only=False`：调成 `True` 可以「只编译、不仿真」，用来快速发现语法/编译错误，省去等仿真的时间。

#### 4.4.4 代码实践：改参数，看输出变化

1. **实践目标**：通过改 `test_runner.py` 的参数，直观感受它们对仿真的影响。
2. **操作步骤**（任选其一，本机有仿真器时推荐第 1 个）：
   1. **GUI 模式**：把 [test_runner.py:L24](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L24) 的 `gui=False` 改成 `gui=True`，重新运行。观察仿真器是否弹出图形窗口，并试着在里面加几根信号。
   2. **超时观察**：把 `timeout_ms` 改成一个极小值（如 `0.0001`），运行；预期大量用例因「仿真超时」被判失败。再改回 `1.0`，恢复正常。
   3. **只编译**：把 `compile_only=True`，运行；预期只看到编译日志、没有仿真波形，速度快很多。
   4. **缩小范围**：把 `tb_pattern="**"` 改成 `"debouncer/**"`，预期只编译并仿真 `tb_debouncer`。
3. **需要观察的现象**：每次改一个参数，对比终端输出的差异（GUI 是否弹窗、用例是否被超时杀掉、编译/仿真阶段的边界、跑的测试台数量）。
4. **预期结果**：
   - `gui=True` → 仿真器图形窗口弹出。
   - `timeout_ms` 过小 → 一批用例报 timeout 失败。
   - `compile_only=True` → 只有 compile 阶段，无 run 阶段。
   - `tb_pattern` 收窄 → 被发现的测试台数量减少。
5. **待本地验证**：以上现象依赖本机仿真器与厂商库就绪。若没有本地仿真器，可在 EDA Playground 上对单个测试台做类似实验（把 README Option 1 的设置当作「参数」来调整）。

> 改完参数**记得改回去**（尤其 `use_xilinx_libs` 必须保持 `True`，否则 XPM 相关测试台会大面积报 `glbl` 错误）。本实践只读不改源码，`test_runner.py` 属于配置脚本，实验后请还原以免影响后续学习。

#### 4.4.5 小练习与答案

**练习 1**：你跑仿真时大量用例报「timeout 失败」，但你确信设计没错。最该先调哪个参数？

> **参考答案**：先调大 `timeout_ms`（默认 `1.0` 毫秒）。VUnit 的 watchdog 把仿真超时的用例判失败；如果你的用例需要更长的仿真时间（比如等慢时钟域多个周期），1ms 不够就会误杀。

**练习 2**：README 示例块里写 `use_xilinx_libs=False`，而真实 `test_runner.py` 里是 `True`。以哪个为准？为什么本库默认要开它？

> **参考答案**：以**真实文件**为准（`use_xilinx_libs=True`）。因为本库的测试台普遍例化了 Xilinx 的 XPM 原语（如 `xpm_cdc_single`、`xpm_fifo_sync`），这些原语依赖 Xilinx 的 `glbl` 模块与仿真库。不开此开关会报 `Failed to find 'glbl' in hierarchical name 'glbl.GSR'`。README 的示例块只是「参数清单」，取值是示意，不代表本库的推荐默认值。

**练习 3**：CI 为什么要用单独的 `test_runner_ci_cd.py`，而不是直接用本地的 `test_runner.py`？

> **参考答案**：CI 环境（NVC 仿真器）无法运行需要硬核厂商资源的测试台（典型是 `tb_pll.vhd`，PLL 没有纯行为级实现），所以 CI 版脚本用 `excluded_list` 把这类测试台排除掉，并可能调整 `xunit_xml` 路径来产出报告。本地 `test_runner.py` 的 `excluded_list=[]` 不排除任何东西。这个差异会在 u1-l4 详讲。

---

## 5. 综合实践

**任务**：完成「从零到一次完整仿真」的全流程，并解释清楚 VUnit 的发现机制。

**步骤**：

1. **环境就绪**：按 4.1 的流程，创建 venv、安装 `ip/requirements.txt`，并执行 `git submodule update --init --recursive` 确保 `ip/vhdl_utils` 非空。
2. **配置仿真器**：按 [README.md:L227-L253](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/README.md#L227-L253) 设置 `VUNIT_MODELSIM_PATH` 环境变量（或改用 EDA Playground 的 Option 1）。
3. **跑全量仿真**：执行 `./.venv/Scripts/python.exe ./ip/test_runner.py`（Linux 用 `.venv/bin/python`），记录最后一行 `hdl_offline_tests: Passed/Failed`。
4. **改一个参数**：把 `test_runner.py` 里 `gui` 设为 `True`（或把 `tb_pattern` 收窄到 `"debouncer/**"`），再跑一次，对比输出差异。
5. **回答发现机制**：用一句话解释「除了 `run_all_in_same_sim` 之外，VUnit 是如何发现测试的」。

**验收标准**：

- 能给出第 3 步的 `Passed`/`Failed` 截图或日志（若本地无仿真器，给出 EDA Playground 的运行结果并说明）。
- 能解释第 4 步观察到的差异（GUI 弹窗 / 测试台数量变化）。
- 第 5 步的参考答案应包含两条：① 测试台层面靠 `runner_cfg` generic（外加 `tb_*.vhd` 通配）被发现；② 用例层面靠测试台 `test_suite` 循环里每个 `run("用例名")` 调用被发现。`run_all_in_same_sim` 只影响用例「如何编组执行」，不影响「发现」。

> 待本地验证：综合实践能否跑通，完全取决于本机仿真器与厂商库是否就绪。若暂不具备，可先完成「源码阅读型」子任务——逐行读懂 `test_runner.py` 并标注每个参数的作用，再在具备环境时补跑。

## 6. 本讲小结

- 跑仿真需要 **Python venv + `vunit_hdl` + 一个 VHDL-2008 仿真器**，外加必须初始化的 `ip/vhdl_utils` 子模块（否则 `test_runner.py` 导入即报错）。
- [ip/test_runner.py](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py) 是一个**薄包装器**：调一次子模块里的 `run_all_testbenches_lib(...)`，再据返回码打印绿色 `Passed` 或红色 `Failed`。
- 调用链是 **`test_runner.py` → `run_all_testbenches_lib`（子模块）→ VUnit → 仿真器** 三层，普通用户只面对第一层。
- VUnit 的**测试发现**靠两层标志：测试台层面靠 `runner_cfg` generic（`tb_*.vhd` 只是缩小范围），用例层面靠 `test_suite` 里的 `run(...)` 调用；`-- vunit: run_all_in_same_sim` 只决定用例**如何编组执行**，不参与发现。
- 关键参数中，`use_xilinx_libs`（真实文件为 `True`）解决 XPM 原语的 `glbl` 报错；`gui` 切换图形调试；`timeout_ms` 控制 watchdog；`excluded_list` 用于排除（CI 版脚本据此排除 PLL）。
- **重要提醒**：README 的参数示例块（`use_xilinx_libs=False`、`xunit_xml="./test/res.xml"`）与磁盘上真实 `test_runner.py`（`use_xilinx_libs=True`、`xunit_xml=None`）不一致，**一律以真实文件为准**。

## 7. 下一步学习建议

- 本讲让你「会跑测试」，下一讲 [u1-l4 工具链配置与持续集成](u1-l4-ci-and-toolchain.md) 会把这套流程放进 **CI**：讲 `vhdl_ls.toml` 的库声明、`.github/workflows/vunit.yml` 用 NVC + 厂商库跑全量测试、以及 `test_runner_ci_cd.py` 的 `excluded_list` 策略。
- 想立刻看真实 VHDL 源码，可直接打开 [ip/test_runner.py](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py) 与一个测试台 [ip/communication/spi/tb/tb_spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd) 对照本讲的讲解。
- 第 2 单元 [u2-l1 同一实体多架构模式](u2-l1-multi-architecture-pattern.md) 将正式进入设计源码，讲解本库最核心的「同一 entity + 多厂商 architecture」设计模式——它是后续所有 IP 讲义的钥匙。
- 第 11 单元 [u11-l1 VUnit 测试台结构](u11-l1-vunit-testbench-structure.md) 会从「写一个测试台」的视角，深入讲 `test_runner_setup` / `run()` / `watchdog` 等 VUnit API，与本讲的「运行视角」互补。
