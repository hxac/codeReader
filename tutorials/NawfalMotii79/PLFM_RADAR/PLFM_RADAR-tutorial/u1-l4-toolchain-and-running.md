# 工具链与本地运行方式

## 1. 本讲目标

前面三讲你已经知道 AERIS-10 是什么（u1-l1）、仓库怎么分层（u1-l2）、三大子系统的入口在哪（u1-l3）。但只要还没把代码「跑起来」，就始终隔着一层——你不知道一个改动是否真的通过了项目的检验。

本讲的目标是补上这块最后的拼图——**工具链与本地运行方式**：

1. 学会用 **uv** 安装开发依赖，理解「开发依赖」与「GUI 运行依赖」为什么分两套。
2. 学会用 **ruff** 做 Python 静态检查、用 **pytest** 跑 Python 单元测试。
3. 看懂 GitHub Actions 的 **四类 CI job**，并能把每个 job 对应到一条你可以在本地复现的命令。
4. 知道 **FPGA 用 iverilog、MCU 用 make test** 这两条本地运行路径。

学完本讲，你应该能在一台干净的 Linux/Mac 上，照着命令把项目的 Python 检查、MCU 测试、FPGA 回归分别跑一遍，并读懂它们的输出。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l1 项目定位**：知道 AERIS-10 跨硬件（FPGA + STM32）、软件（Python GUI）三大栈。
- **u1-l2 仓库目录结构**：知道代码集中在 `9_Firmware/` 下，再分 `9_1_Microcontroller` / `9_2_FPGA` / `9_3_GUI`。
- **u1-l3 关键入口文件**：知道 `radar_system_top.v`、`main.cpp`、`GUI_V7_PyQt.py` 这些门面文件。

本讲几乎不涉及雷达算法本身，讲的是「工程脚手架」。下面几个术语会反复出现，先做最小解释：

- **工具链（toolchain）**：把源代码变成「能跑、能验证」的状态所需要的一整套工具。对 Python 来说是依赖管理器 + linter + 测试框架；对 FPGA 来说是仿真器；对 MCU 来说是 C 编译器。
- **依赖管理器（dependency manager）**：自动帮你的项目装好它依赖的第三方库，并固定版本。本项目用 **uv**（Rust 写的、极快的 Python 包管理器）。
- **linter（静态检查器）**：不运行代码，只读源码文本就能挑出问题的工具。本项目用 **ruff**。
- **测试框架（test runner）**：自动跑一堆「断言（assert）」并告诉你通过/失败的程序。本项目 Python 侧用 **pytest**，MCU 侧用自己写的 Makefile + C 编译器，FPGA 侧用 **iverilog** 跑 testbench。
- **CI（Continuous Integration，持续集成）**：每次 push 或提 PR，云端自动跑一遍检查。本项目用 **GitHub Actions**。

> 一个直觉：工具链就是项目的「体检套餐」——linter 查语法与坏味道、单测查逻辑正确性、FPGA/MCU 测试查各层各自没坏。CI 把这套套餐搬到云端，保证每次提交都被体检过。

## 3. 本讲源码地图

本讲只看四个「工程配置」文件，它们各自定义了一类工具的运行方式：

| 文件 | 一句话职责 |
|------|-----------|
| `pyproject.toml` | Python 项目元数据：声明 Python 版本、开发依赖（ruff/pytest/numpy/h5py）、ruff 规则集 |
| `.github/workflows/ci-tests.yml` | GitHub Actions 配置：定义四类 CI job 及其命令 |
| `9_Firmware/9_3_GUI/requirements_v7.txt` | GUI 运行依赖清单（核心必需 + 可选优雅降级） |
| `9_Firmware/9_1_Microcontroller/tests/Makefile` | MCU 固件主机侧测试的编译与运行入口（`make test`） |

> 注意：这四个文件都不是「业务逻辑」，而是「告诉工具怎么干活」的配置。但它们恰恰是新人最容易跳过的文件——读懂它们，你就知道项目默认跑哪些检查、怎么跑。

## 4. 核心概念与源码讲解

### 4.1 uv 依赖管理

#### 4.1.1 概念说明

一个 Python 项目通常依赖一堆第三方库（GUI 要 PyQt6、信号处理要 scipy……）。如果你手动 `pip install`，时间一长就没人记得「这个项目到底需要哪些库、什么版本」。**依赖管理器**就是来解决这个问题的：你把依赖写进配置文件，它自动按配置装好、装对版本。

本项目用 **uv**。它的配置写在仓库根目录的 `pyproject.toml` 里（这是 Python 项目的标准配置文件名）。

AERIS-10 在这里做了一个**很有讲究的分层**，初学者一定要看懂：

1. **开发依赖（dev group）**：只有开发者在本地跑检查时才需要——`ruff`、`pytest`、`numpy`、`h5py`。这些是「工具」，不会进最终产物。
2. **GUI 运行依赖**：真正运行 GUI 才需要——PyQt6、matplotlib、scipy 等。它们写在 `9_Firmware/9_3_GUI/requirements_v7.txt`，**故意不放进 `pyproject.toml` 的运行依赖里**。

为什么要这么分？因为 GUI 的很多依赖是「可选」的：没装 scipy，聚类功能禁用但 GUI 不崩；没装 pyusb，连不上真硬件但可以回放数据。把 GUI 依赖塞进主 `pyproject.toml` 会让「只想跑测试的人」也被迫装一堆 GUI 库。所以项目把测试需要的最小集合放 `pyproject.toml` 的 dev 组，GUI 的（部分可选）依赖单独放 `requirements_v7.txt`。

#### 4.1.2 核心流程

本地从零把 Python 工具链跑起来的流程：

```text
git clone 仓库
  │
  ▼
（系统已有 Python ≥ 3.12 与 uv）
  │
  ▼
uv sync --group dev       读 pyproject.toml 的 dev 组 → 建 .venv → 装 ruff/pytest/numpy/h5py
  │
  ▼
uv run ruff check .        用 ruff 扫全仓 Python
  │
  ▼
uv run pytest <测试文件>    跑 Python 单元测试
  │
  ▼
（可选）pip install -r 9_Firmware/9_3_GUI/requirements_v7.txt   装齐 GUI 运行依赖
```

关键点：`uv run <命令>` 的意思是「在 uv 管理的虚拟环境里跑这个命令」。这样 `ruff`、`pytest` 用到的就是 `uv sync` 装好的版本，而不是系统全局的旧版本，避免「我这里能跑、你那里报错」。

#### 4.1.3 源码精读

先看 `pyproject.toml` 怎么声明 Python 版本与「空的运行依赖」：

[pyproject.toml:L1-L9](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L1-L9) —— 项目名叫 `aeris-10-radar`，`requires-python = ">=3.12"`（L5）锁死最低 Python 版本；`dependencies = []`（L9）刻意留空，注释（L7-L8）说明运行依赖故意不写在这里，GUI 依赖走 `requirements_*.txt`。

开发依赖集中在 `dependency-groups` 的 `dev` 组：

[pyproject.toml:L11-L17](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L11-L17) —— `dev` 组含 `ruff>=0.5`、`pytest>=8`、`numpy>=1.26`、`h5py>=3.10`。这就是 `uv sync --group dev` 会装的四个包。

再看 GUI 那份「核心 + 可选」的依赖清单：

[requirements_v7.txt:L4-L8](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/requirements_v7.txt#L4-L8) —— Core（必需）：`PyQt6`、`PyQt6-WebEngine`、`numpy`、`matplotlib`。GUI 没这四个起不来。

[requirements_v7.txt:L10-L22](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/requirements_v7.txt#L10-L22) —— 全部标 optional：硬件接口（`pyusb`/`pyftdi`，L11-L12）、信号处理（`scipy`，L15）、聚类跟踪（`scikit-learn`/`filterpy`，L18-L19）、CRC 校验（`crcmod`，L22）。缺了它们 GUI 会「降级」而非崩溃（u8-l1 会详讲优雅降级机制）。

> 一句话总结这两份文件的关系：`pyproject.toml` 管「开发/测试工具」，`requirements_v7.txt` 管「GUI 运行时」。两套各管一摊，互不拖累。

#### 4.1.4 代码实践

**实践目标**：亲手把开发依赖装起来，确认 `uv` 能正确读取 `pyproject.toml` 的 dev 组。

**操作步骤**：

1. 确认系统有 Python ≥ 3.12（`python3 --version`）与 uv（`uv --version`）。没有 uv 可按官方文档用一行脚本安装。
2. 在仓库根目录执行 `uv sync --group dev`。
3. 观察 uv 是否在仓库下创建了 `.venv/` 虚拟环境目录，并下载了 ruff / pytest / numpy / h5py 四个包。

**需要观察的现象**：

- `uv sync` 的输出里应出现 `Resolved N packages` 与 `Installed ...` 字样，列出的核心包与 `pyproject.toml` L13-L16 对得上。
- 之后 `uv run python -c "import ruff"` 这种 import 检查能成功（ruff 本身是命令行工具，这里只是示例思路）。

**预期结果**：得到一个装好开发依赖的 `.venv`，后续所有 `uv run <cmd>` 都基于它。

> 由于 `uv sync` 需要联网下载包，且不同环境 Python 版本/网络情况不同，**实际安装耗时与是否一次性成功属于「待本地验证」**，本讲不假设结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pyproject.toml` 的 `dependencies = []`（L9）刻意留空，而不是把 PyQt6 直接写进去？

**参考答案**：因为 GUI 的依赖大多是可选的（硬件接口、scipy、sklearn 等），写成必填会让「只想跑 ruff/pytest 的人」也被迫装一大堆 GUI 库。把运行依赖分离到 `requirements_v7.txt`，并在代码里对缺失的可选依赖做优雅降级，是「核心必需 + 可选增强」的常见工程做法。

**练习 2**：如果某位开发者只想跑 Python 测试、完全不碰 GUI，他需要 `pip install -r requirements_v7.txt` 吗？

**参考答案**：不需要。他只要 `uv sync --group dev` 拿到 ruff/pytest/numpy/h5py 即可跑 ruff 与 pytest。GUI 依赖只在真正启动 GUI、且需要某个可选功能（如 USB 连接、聚类）时才按需补装。

---

### 4.2 ruff 与 pytest：Python 静态检查与单元测试

#### 4.2.1 概念说明

依赖装好之后，项目用两道关来保证 Python 代码质量：

1. **ruff（静态检查 / lint）**：不运行代码，只读文本，挑出潜在问题——未使用的 import、被注释掉的死代码、调试用的 `print()`、影子内置名（把变量命名为 `list`、`id`）、不可达分支等。ruff 极快（Rust 实现），可替代 flake8 + isort + pyupgrade 等一堆老工具。
2. **pytest（单元测试）**：实际运行一批「断言」，验证函数行为符合预期。

AERIS-10 的 ruff 配置有个鲜明特点：它的规则集是**针对「LLM 生成代码的常见坏味道」专门挑的**——这一点在 `pyproject.toml` 的行内注释里写得清清楚楚。

#### 4.2.2 核心流程

Python 侧的一道「三连检」：

```text
uv run ruff check .          全仓扫所有 .py，按 select 规则集报错
   │ （通过后）
   ▼
py_compile 全仓编译          逐个 .py 做字节码编译，抓语法错
   │ （通过后）
   ▼
uv run pytest <测试>         跑 GUI 的单元测试（test_v7.py / test_GUI_V65_Tk.py）
```

这三步从「静态文本」到「语法编译」再到「运行行为」层层递进，CI 里也是按这个顺序执行的。

#### 4.2.3 源码精读

先看 ruff 的基本参数——目标 Python 版本与行长：

[pyproject.toml:L22-L24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L22-L24) —— `target-version = "py312"`、`line-length = 100`。也就是说项目认定 Python 3.12 语法、每行不超过 100 字符。

最有信息量的是这段 `select`（启用的规则集），每条行内注释都说明了它防什么：

[pyproject.toml:L27-L45](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L27-L45) —— 启用的规则前缀与含义如下表（依据行内注释整理）：

| 规则前缀 | 全名 | 抓什么问题（按项目注释） |
|---------|------|------------------------|
| `E` | pycodestyle errors | 代码风格错误 |
| `F` | pyflakes | 未用 import、未定义名、重复键、`assert` 元组 |
| `B` | flake8-bugbear | 可变默认参数、不可达代码、`raise` 不带 `from` |
| `RUF` | ruff-specific | 无效 `noqa`、歧义字符、隐式 `Optional` |
| `SIM` | flake8-simplify | 死分支、可合并 `if`、多余 `pass` |
| `PIE` | flake8-pie | 无副作用表达式、多余展开 |
| `T20` | flake8-print | 漏网的 `print()`（LLM 常留调试打印） |
| `ARG` | flake8-unused-arguments | 定义了却从不使用的参数 |
| `ERA` | eradicate | 被注释掉的死代码（LLM 常留「备选方案」注释） |
| `A` | flake8-builtins | 影子内置名（`id`/`type`/`list`/`dict` 等被当变量名） |
| `BLE` | flake8-blind-except | 裸 `except` 或过宽异常捕获 |
| `RET` | flake8-return | `return` 后不可达、多余的 `else-after-return` |
| `ISC` | flake8-implicit-str-concat | 字符串列表里漏逗号导致的隐式拼接 |
| `TCH` | flake8-type-checking | 只用于类型注解的 import 应放进 `TYPE_CHECKING` |
| `UP` | pyupgrade | 对目标 Python 版本而言过时的语法 |
| `C4` | flake8-comprehensions | 不必要的 `list()`/`dict()` 包裹生成器 |
| `PERF` | perflint | 性能反模式（如循环里多余的 `list()`） |

再看针对个别文件的豁免（per-file-ignores）——这非常实用，说明「规则在测试代码里适度放宽」：

[pyproject.toml:L47-L51](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L47-L51) —— `test_*.py` 豁免 `ARG`/`T20`/`ERA`（测试里允许未用参数 fixture、调试 print、注释示例）；`v7/hardware.py` 豁免 `F401`（作为「再导出」模块，未用 import 是故意的）。

CI 里这三步检查是这样串起来的：

[.github/workflows/ci-tests.yml:L30-L31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L30-L31) —— CI 步骤「Ruff lint (whole repo)」执行的命令就是 `uv run ruff check .`，与本讲让你本地跑的命令完全一致。

[.github/workflows/ci-tests.yml:L34-L44](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L34-L44) —— 「Syntax check (py_compile)」：用一段内联 Python 脚本 `rglob("*.py")` 遍历全仓（跳过 `.git`/`__pycache__`/`.venv`/`venv`/`docs`），对每个文件 `py_compile.compile(..., doraise=True)`，任何语法错都会抛异常使 CI 失败。

[.github/workflows/ci-tests.yml:L47-L51](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L47-L51) —— 「Unit tests」：`uv run pytest` 跑两个 GUI 测试文件 `test_GUI_V65_Tk.py` 与 `test_v7.py`，加 `-v --tb=short`（详细、短回溯）。

#### 4.2.4 代码实践

**实践目标**：在本地复现 CI 的 ruff 检查与 GUI pytest，并对照 ruff 规则集理解报错。

**操作步骤**：

1. 在仓库根目录执行 `uv run ruff check .`。
2. 若有报错，对照 4.2.3 的规则表读懂每条违规属于哪个前缀（例如 `T201` 属于 `T20`，即漏网 `print`）。
3. 执行 `uv run pytest 9_Firmware/9_3_GUI/test_v7.py 9_Firmware/9_3_GUI/test_GUI_V65_Tk.py -v --tb=short`（与 CI L48-L51 同命令）。

**需要观察的现象**：

- ruff 若全绿，会打印类似 `All checks passed!`。
- pytest 会逐条列出 `PASSED` / `FAILED`，并在结尾给出 `N passed, M failed` 汇总。

**预期结果**：ruff 与 pytest 在干净的 HEAD 上应双双通过；若本地环境缺 PyQt6，`test_v7.py` 里依赖真实控件的用例可能无法运行——这正是「GUI 运行依赖可选」的体现。

> 本环境未实际执行上述命令（联网安装与 GUI 依赖受限），**具体通过项数与是否有环境性失败属「待本地验证」**。请不要把任何编造的「N passed」写进笔记，以你本机真实输出为准。

#### 4.2.5 小练习与答案

**练习 1**：ruff 规则里 `T20`（flake8-print）和 `ERA`（eradicate）被特别标注为「LLMs leave ...」。为什么这两条对一个长期维护的项目很重要？

**参考答案**：`print()` 往往是调试时随手加的，留在生产代码里会污染输出、甚至泄露内部信息；被注释掉的代码（ERA）则是「改了但没删干净」的备选实现，时间久了没人记得它还有没有用，会让阅读者困惑。这两类都是「不影响运行但严重降低可维护性」的坏味道，所以项目用 lint 强制清掉。

**练习 2**：为什么 `test_*.py` 要在 per-file-ignores 里豁免 `ARG`/`T20`/`ERA`（L49）？

**参考答案**：测试代码里 `print` 常用于本地调试输出、未使用的函数参数常是 pytest fixture 的占位、注释里也常保留示例代码。这些在「产品代码」里该禁，但在「测试代码」里是合理且有用的，所以按文件类型放宽，避免规则一刀切反而妨碍测试编写。

---

### 4.3 CI 矩阵：四类 job 与本地运行方式

#### 4.3.1 概念说明

AERIS-10 是个跨三栈的项目，光检查 Python 远远不够。项目在 GitHub Actions 里定义了**四个并行的 CI job**，每个 job 盯一层：

| CI job | 盯哪一层 | 本地对应命令 |
|--------|---------|-------------|
| `python-tests` | Python GUI 与工具 | `uv run ruff check .` + `uv run pytest ...` |
| `mcu-tests` | STM32 固件 | `make test`（在 `9_1_Microcontroller/tests` 下） |
| `fpga-regression` | FPGA Verilog | `bash run_regression.sh`（在 `9_2_FPGA` 下） |
| `cross-layer-tests` | 三层契约一致性 | `uv run pytest 9_Firmware/tests/cross_layer/...` |

理解这张映射表的意义在于：**CI 的每一条命令你都能在本地复现**。CI 不是黑盒，它只是把你本地的命令搬到 ubuntu-latest 容器里自动跑一遍。

#### 4.3.2 核心流程

CI 的触发与执行流程：

```text
push 到 main / develop， 或 向这两分支提 PR
  │
  ▼
GitHub Actions 同时启动四个 job（互不依赖，并行跑）
  ├── python-tests        装 uv → ruff → py_compile → pytest
  ├── mcu-tests           装 build-essential → make test
  ├── fpga-regression     装 iverilog → run_regression.sh
  └── cross-layer-tests   装 uv + iverilog → pytest 跨层契约
  │
  ▼
任一 job 失败 → 该次提交的 CI 标红
```

关键点：四个 job 是**并行独立**的，没有一个 job 依赖另一个。这样某层坏了不会拖累别的层，也缩短了总耗时。

#### 4.3.3 源码精读

先看触发条件——CI 在什么时候跑：

[.github/workflows/ci-tests.yml:L3-L7](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L3-L7) —— `on: pull_request` 与 `push`，目标分支都是 `[main, develop]`。也就是说往这两条分支 push、或向它们提 PR，都会触发全套四个 job。

逐个看四个 job 的核心命令。**python-tests**（本讲 4.2 已展开其三步检查）：

[.github/workflows/ci-tests.yml:L14-L16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L14-L16) —— job 名 `python-tests`，显示名 `Python Lint + Tests`，跑在 `ubuntu-latest`。

[.github/workflows/ci-tests.yml:L25-L28](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L25-L28) —— 用 `astral-sh/setup-uv@v5` 装 uv，然后 `uv sync --group dev` 装开发依赖。这就是你在本地要做的第一步。

**mcu-tests**——MCU 固件的主机侧测试，本地用 `make test`：

[.github/workflows/ci-tests.yml:L57-L69](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L57-L69) —— job 名 `mcu-tests`；先 `apt-get install -y build-essential`（装 gcc/make），再在 `working-directory: 9_Firmware/9_1_Microcontroller/tests` 下执行 `make test`（L68-L69）。注意 CI 的注释（L54-L55）写着「20 tests: Bug regression (15) + Gap-3 safety tests (5)」，但当前 Makefile 实际枚举的目标数更多——见下方 MCU Makefile 详解。

**fpga-regression**——FPGA RTL 回归，本地用 iverilog：

[.github/workflows/ci-tests.yml:L74-L86](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L74-L86) —— job 名 `fpga-regression`；先 `apt-get install -y iverilog`（装 Icarus Verilog），再在 `working-directory: 9_Firmware/9_2_FPGA` 下执行 `bash run_regression.sh`（L85-L86）。注释（L72）把它描述为「25 testbenches + lint」。

**cross-layer-tests**——跨层契约测试（Python↔Verilog↔C），同时需要 uv 与 iverilog：

[.github/workflows/ci-tests.yml:L93-L116](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L93-L116) —— job 名 `cross-layer-tests`；先 `uv sync --group dev`，再装 iverilog，最后 `uv run pytest 9_Firmware/tests/cross_layer/test_cross_layer_contract.py -v --tb=short`（L113-L116）。

最后深入看 MCU 的 `Makefile`——它是 mcu-tests job 在本地对应的东西。开头就把用法讲清楚了：

[9_Firmware/9_1_Microcontroller/tests/Makefile:L1-L16](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L1-L16) —— 注释说明：`make` = 构建并跑全部、`make build` = 只构建、`make test` = 只跑、`make clean` = 清产物、`make test_bug1` = 只跑某个 bug 测试。要求是任意 C11 编译器（Apple Clang 或 gcc）。

它最巧妙的设计是**用 shim 头文件覆盖真实 HAL 头**，从而在 PC 上编译跑嵌入式固件：

[Makefile:L18-L23](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L18-L23) —— `CC := cc`、`CFLAGS := -std=c11 -Wall -Wextra ...`；最关键的是 `INCLUDES := -Ishims -I. -I../9_1_1_C_Cpp_Libraries`——`-Ishims` 放在最前，让「假」的 STM32 HAL 头文件优先于真头文件被找到，这样固件源码不用改就能在主机上编译。

测试被分成几组，按「需要链接什么」分类（便于维护）：

[Makefile:L49-L84](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L49-L84) —— 五组测试目标：`TESTS_WITH_REAL`（连真 `adf4382a_manager.c` + mock，7 个，L49-L55）、`TESTS_MOCK_ONLY`（只需 mock，6 个，L58-L63）、`TESTS_STANDALONE`（纯逻辑，不需 mock，8 个，L66-L73）、`TESTS_WITH_PLATFORM`（需 platform_noos + mock，1 个，L76）、`TESTS_WITH_CXX`（C++ 的 AGC 外环测试，L79）、`TESTS_GPS`（GPS 驱动测试，L82），最后 `ALL_TESTS` 汇总（L84）。

`make test` 的运行与计数逻辑：

[Makefile:L95-L113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/tests/Makefile#L95-L113) —— `test:` 目标先 `build`，然后用 shell 循环逐个跑 `$(ALL_TESTS)` 里的二进制，统计 `pass`/`fail`，最后打印 `Results: N passed, M failed (of TOTAL total)`，其中 TOTAL 是 `$(words $(ALL_TESTS))`（动态计算目标个数），并以 `[ $$fail -eq 0 ]` 作为整个测试是否通过的退出码。

> **一个真实的「注释滞后」细节**（值得留意）：CI 的注释（ci-tests.yml L54-L55）写「MCU Firmware Unit Tests (20 tests): Bug regression (15) + Gap-3 safety tests (5)」。但若把 `Makefile` L49-L84 的五组目标实际数一数：bug 测试 bug1..bug15 共 **15** 个、gap3 测试共 **7** 个（`emergency_stop_rails`/`iwdg_config`/`temperature_max`/`idq_periodic_reread`/`emergency_state_ordering`/`overtemp_emergency_stop`/`health_watchdog_cold_start`），再加上 `test_agc_outer_loop` 与 `test_um982_gps` 各 1 个，合计 **24** 个。也就是说 `make test` 实际会跑 24 个、并打印「of 24 total」，而非注释里的「20」。CI 注释稍落后于代码演进——这恰好说明「读配置文件本身比读它上方的注释更可靠」。具体数字请以你本地 `make test` 的真实输出为准。

#### 4.3.4 代码实践

**实践目标**：把 CI 的四个 job 各自映射到一条本地命令，亲手验证其中两类（MCU、FPGA）能在本地跑。

**操作步骤**：

1. **MCU 测试**：`cd 9_Firmware/9_1_Microcontroller/tests && make test`（需要 gcc/make，Linux/Mac 通常自带；CI 里靠 `build-actable` 提供）。
2. **FPGA 回归**：`cd 9_Firmware/9_2_FPGA && bash run_regression.sh`（需要先装 iverilog，例如 `apt-get install -y iverilog` 或 `brew install icarus-verilog`）。想快速跑可加 `--quick` 跳过耗时集成测试。
3. 把四个 job 的命令填进本讲 5「综合实践」的对照表。

**需要观察的现象**：

- `make test` 会逐个打印 `--- Running test_xxx ---`，结尾给出 `Results: N passed, M failed (of 24 total)`（按上一节的实际计数）。
- `run_regression.sh` 会分阶段（Phase 0 lint、Phase 1+ 各 testbench）输出，并用颜色标注 PASS/FAIL。

**预期结果**：在干净 HEAD 上，`make test` 应全部通过（fail=0，退出码 0）；`run_regression.sh` 退出码也应为 0。

> `run_regression.sh` 完整跑一遍可能较慢（25 个 testbench + 全设计 lint），加 `--quick` 可跳过长流程；**本地是否一次通过、耗时多少属「待本地验证」**。

#### 4.3.5 小练习与答案

**练习 1**：mcu-tests 这个 CI job 并没有装 Python，却能把 STM32 的 C/C++ 固件「跑」起来。它是怎么做到「在没有真芯片的主机上测嵌入式代码」的？

**参考答案**：靠 `Makefile` 的 **shim + mock** 技巧——`INCLUDES := -Ishims -I. -I../9_1_1_C_Cpp_Libraries`（L23）把 `shims/` 目录放在头文件搜索路径最前，让假的 STM32 HAL 头文件（如 `stm32_hal_mock.c`、`ad_driver_mock.c`）优先于真头被链接。于是固件的真实源码（`adf4382a_manager.c`、`ADAR1000_AGC.cpp` 等）不用改一行，就能在 PC 上用 gcc 编译运行，再断言其行为。这是「主机侧测嵌入式」的典型手法（U11-l2 会深入）。

**练习 2**：四个 CI job 为什么设计成并行而不是串行？

**参考答案**：因为它们彼此无依赖——Python 检查不需要 MCU 通过、FPGA 回归也不依赖 Python。并行既缩短总耗时（墙钟时间≈最慢的那个 job，而非四者之和），也让「某一层坏了」时其它层的结论依然可信。这是 CI 拆分 job 的常见取舍：能并行就别串行。

---

## 5. 综合实践

把本讲三块内容串成一个任务：**在本地复现 CI，并填出「job → 命令」对照表**。

**实践目标**：亲手把项目的工具链跑通一遍，建立「CI 不是黑盒」的直觉。

**操作步骤**：

1. 执行 `uv sync --group dev`，装好开发依赖。
2. 执行 `uv run ruff check .`，记录是否 `All checks passed!`；若有违规，记下规则前缀。
3. 执行 `uv run pytest 9_Firmware/9_3_GUI/test_v7.py 9_Firmware/9_3_GUI/test_GUI_V65_Tk.py -v --tb=short`，记录 `N passed`。
4. （可选）执行 `make test`（在 `9_1_Microcontroller/tests`）与 `bash run_regression.sh`（在 `9_2_FPGA`），记录各自结果。
5. 填完下表：

| CI job（ci-tests.yml） | 本地等价命令 | 本地运行结果（待本地验证） |
|-----------------------|-------------|---------------------------|
| `python-tests`（L14） | `uv run ruff check .` + `uv run pytest ...` | （自填：通过/失败项数） |
| `mcu-tests`（L57） | `make test`（`9_1_Microcontroller/tests`） | （自填：N passed / 24 total） |
| `fpga-regression`（L74） | `bash run_regression.sh`（`9_2_FPGA`） | （自填：退出码 / 各阶段是否 PASS） |
| `cross-layer-tests`（L93） | `uv run pytest 9_Firmware/tests/cross_layer/test_cross_layer_contract.py` | （自填：通过项数） |

**需要观察的现象**：

- 同一条命令在本地与 CI 行为应基本一致；差异通常只来自「环境是否装了可选依赖」（如本地没装 PyQt6 导致 GUI 测试跑不全，但 CI 装了）。
- `make test` 打印的总数应与 `Makefile` 中 `ALL_TESTS` 实际目标数（24）一致，而不是 CI 注释里的「20」。

**预期结果**：你得到一张完整的「CI job → 本地命令 → 真实结果」表。从此看到 CI 报红，你能立刻知道是哪一层、用哪条本地命令去复现。

> 本环境未联网执行 `uv sync`/`make test`/`run_regression.sh`，**表中「本地运行结果」一列请以你本机真实输出填写（待本地验证）**，不要照抄任何示例数字。

## 6. 本讲小结

- 项目用 **Python 3.12 + uv** 管理依赖：`pyproject.toml` 的 `dev` 组（ruff/pytest/numpy/h5py）是开发工具，GUI 运行依赖单独放 `requirements_v7.txt`，且分「核心必需 + 可选降级」两档。
- Python 代码过**三道关**：`uv run ruff check .`（静态检查，规则集针对 LLM 常见坏味道如 `T20`/`ERA`/`A`）、`py_compile` 全仓语法编译、`uv run pytest` 跑 GUI 单元测试。
- CI 共**四个并行 job**：`python-tests`、`mcu-tests`、`fpga-regression`、`cross-layer-tests`，每个对应一层，且都能用本地命令复现。
- **MCU 测试**用 `make test`（`9_1_Microcontroller/tests`），靠 `Makefile` 的 `-Ishims` 让 mock HAL 覆盖真 HAL，从而在 PC 上用 gcc 跑嵌入式固件；当前实际枚举 **24** 个测试目标（注释里的「20」已滞后）。
- **FPGA 回归**用 `bash run_regression.sh`（`9_2_FPGA`），底层是 iverilog 仿真 + Vivado 级 lint。
- 读配置文件本身（pyproject.toml / Makefile / ci-tests.yml）比读它们上方的注释更可靠——注释会滞后，命令是真实执行的。

## 7. 下一步学习建议

工具链跑通之后，建议按「先看整条链路，再钻各层」的顺序继续：

1. **想看整条流水线怎么串起来**：进入 u2-l2「雷达信号处理流水线」，把 DAC→混频→波束→ADC→DDC→匹配滤波→Doppler→CFAR→USB→GUI 的每一步对应到文件。
2. **想深入 FPGA 仿真与回归**：本讲的 `run_regression.sh` 是入口，U11-l1「FPGA 回归测试与协同仿真」会讲它的四阶段与真实数据 cosim。
3. **想深入 MCU 测试**：本讲的 `make test` + shim/mock 是入口，U11-l2「STM32 单元测试与 bug 回归」会展开 `test_bug1..15` 与 gap3 安全测试各防什么缺陷。
4. **想深入 Python 测试与契约**：U11-l3「跨层契约测试」与 U11-l4「Python 测试、lint 与代码质量」承接本讲的 ruff/pytest 与 cross-layer job。

无论走哪条线，记得把本讲第 5 节的「CI job → 本地命令」对照表留在手边——它是你之后每次「CI 报红、去本地复现」的快速索引。
