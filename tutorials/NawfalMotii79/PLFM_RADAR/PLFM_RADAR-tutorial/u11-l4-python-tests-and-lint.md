# u11-l4 Python 测试、lint 与代码质量

## 1. 本讲目标

AERIS-10 雷达的 Python 代码（GUI V7、协议层、跨层测试工具）并非一次性写完就不动了，它会随着 FPGA RTL、STM32 固件、上位机功能的演进而持续被修改。本讲解决一个问题：**如何用一套自动化检查，让 Python 代码在每次提交时都「语法正确、风格干净、行为符合契约」**，而不必靠人肉 review。

学完本讲，你应当能够：

- 读懂 `pyproject.toml` 中那串「针对 LLM 遗留代码」的 ruff 规则集，并能说出每条规则家族要消除的代码异味。
- 理解 CI 里 `py_compile` 全仓语法扫描的工作原理，以及为什么它和 pytest 是两道不同的关卡；理解 `per-file-ignores` 为什么对测试文件和再导出模块「网开一面」。
- 看懂 `test_v7.py` 与 `test_GUI_V65_Tk.py` 两个 GUI 测试文件覆盖了什么、它们如何充当「协议契约守卫」和「死代码守卫」，并知道 `smoke_test.py` 为什么不是 pytest 的一部分。
- 在本地复现 CI 的 Python 三连检（ruff → py_compile → pytest），并能解释 `T20`（print）和 `ERA`（注释代码）这两条规则对长期维护质量的意义。

本讲是 U11（测试与验证体系）的一环，与 u11-l1（FPGA 回归）、u11-l2（MCU 单元测试）、u11-l3（跨层契约测试）并列，专门覆盖「Python 侧」的质量门禁。

## 2. 前置知识

在进入本讲前，请先回忆或了解以下概念（多数已在 u1-l4 与 u8-l1 建立）：

- **工具链 / 依赖管理器**：本项目用 `uv` 管理 Python 依赖，开发依赖（ruff、pytest、numpy、h5py）声明在 `pyproject.toml` 的 `dev` 组里，用 `uv sync --group dev` 安装、`uv run <cmd>` 执行。
- **linter（静态检查器）**：不运行代码、只读源码文本就能报问题的工具。ruff 是当前 Python 生态里最快、规则最全的 linter，且用 Rust 实现。
- **测试框架**：本项目测试用 Python 标准库的 `unittest`（类继承 `unittest.TestCase`，方法名以 `test_` 开头），但**通过 `pytest` 来驱动执行**——pytest 能发现并运行 unittest 风格的测试类。这一点很重要：你看到的 `unittest.TestCase` 和「用 pytest 跑」并不矛盾。
- **优雅降级（graceful degradation）**：u8-l1 讲过，GUI 在缺少 PyQt6/scipy/sklearn/filterpy 时不会崩溃，而是禁用相关功能。测试侧同理——依赖缺失的测试用 `@unittest.skipUnless(...)` 自动跳过，而不是报错失败。
- **LLM 生成代码的常见坏味道**：散落的调试 `print()`、永远不会被调用的函数参数、被注释掉的「备选方案」代码、用 `id`/`type`/`list` 这类内置名做变量名（遮蔽内置）等。这些是本讲 ruff 配置的「假想敌」。
- **opcode 契约**：u6-l2 与 u11-l3 讲过，主机命令是 4 字节 `{opcode, addr, value}`，Python 的 `Opcode` 枚举必须与 Verilog 的 `case(usb_cmd_opcode)` 逐项一致。本讲的测试会反复验证这条契约。

如果你对 `pyproject.toml` 的结构或 `uv` 命令还不熟，建议先回看 u1-l4。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 本讲定位 |
| --- | --- | --- |
| [`pyproject.toml`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml) | 项目元数据 + ruff 配置 | ruff 规则集与豁免规则的**唯一真源** |
| [`.github/workflows/ci-tests.yml`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml) | GitHub Actions CI | 定义「Python 三连检」步骤，是本讲的**执行入口** |
| [`9_Firmware/9_3_GUI/test_v7.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py) | V7 包单元测试 | 覆盖 v7.models/processing/workers/hardware/software_fpga/replay |
| [`9_Firmware/9_3_GUI/test_GUI_V65_Tk.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py) | 协议层与命令构造测试 | 验证命令/状态包解析与 `Opcode` 枚举匹配 RTL |
| [`9_Firmware/9_3_GUI/smoke_test.py`](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/smoke_test.py) | 板级冒烟测试脚本 | **独立脚本**（非 pytest），用 logging 而非 print |

阅读顺序建议：先 `pyproject.toml` 看规则「想要什么」，再看 `ci-tests.yml` 看规则「怎么被跑」，最后读两个测试文件看「测试实际守住了什么」。

## 4. 核心概念与源码讲解

本讲的三个最小模块，恰好对应 CI 里 `python-tests` 这个 job 的三道关卡：

1. **ruff 规则**：静态风格/常见缺陷检查。
2. **全仓语法检查**：`py_compile` 编译每个 `.py`，确保语法可解析。
3. **GUI pytest**：跑两个测试文件，验证行为符合协议契约、死代码没有复活。

下面逐个拆解。

### 4.1 ruff 规则集：专为「LLM 遗留代码」定制

#### 4.1.1 概念说明

`ruff` 是一个用 Rust 写的 Python linter，速度极快（比传统 flake8 快一两个数量级），并且把几十个传统 flake8 插件的规则「合并」进了一个工具。ruff 的核心思想是：**代码异味（code smell）不需要运行就能被发现**。

ruff 把规则按「家族」组织，每个家族有一个短代号（如 `F`=pyflakes、`B`=bugbear、`T20`=print）。在配置里用 `select = [...]` 列出你启用的家族，ruff 就会扫描全仓并报告所有命中规则的地方。

AERIS-10 这个项目最特别的一点是：它的 ruff 配置注释里反复出现「LLM」字样——也就是说，这套规则集并不是泛泛地「写得规范点」，而是**有针对性地清除大语言模型生成代码时常见的坏味道**：散落的 `print()`、永远不会用的参数、被注释掉的「另一种写法」、遮蔽内置名的变量名等。读懂这些注释，就读懂了项目维护者对代码质量的「防御重点」。

#### 4.1.2 核心流程

ruff 在 CI 中的工作流程非常简单：

```text
CI 拉取代码
   │
   ▼
uv sync --group dev      # 安装 ruff（在 dev 依赖组里）
   │
   ▼
uv run ruff check .      # 扫描整个仓库（. = 当前目录）
   │
   ├── 无违规 ──► 退出码 0 ──► 该步通过，继续下一步
   └── 有违规 ──► 退出码 非0 ──► 该步失败，CI 标红
```

关键点：ruff 的退出码就是 CI 的判定依据，**任何一条违规都会让整个 Python job 失败**，没有「警告 vs 错误」之分。这是一种「零容忍」策略，强制每次提交都干净。

ruff 如何决定「什么算违规」？完全由 `pyproject.toml` 里的两段配置决定：

- `[tool.ruff.lint].select`：启用哪些规则家族（白名单）。
- `[tool.ruff.lint.per-file-ignores]`：对特定文件（按 glob 匹配）豁免哪些规则（例外表）。

不在 `select` 里的规则即便被触发也不会报；在 `per-file-ignores` 里被豁免的规则，对匹配文件不报。

#### 4.1.3 源码精读

先看 ruff 的全局开关——目标 Python 版本与行宽：

[pyproject.toml:L22-L24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L22-L24) 设定 `target-version = "py312"`（于是 `UP`/pyupgrade 会要求你用 3.12 的新语法）与 `line-length = 100`（一行不超过 100 字符，比默认 88 宽，适配科学计算里常见长表达式）。

规则集本身在 [pyproject.toml:L26-L45](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L26-L45)，每行注释说明了该家族抓什么。下表把它们整理成「代号—全名—抓的典型异味」：

| 代号 | 家族全名 | 抓的典型代码异味 |
| --- | --- | --- |
| `E` | pycodestyle errors | 缩进错误、行尾空格等基础风格问题 |
| `F` | pyflakes | 未使用的 import、未定义的变量名、字典重复键、`assert (a, b)` 这种「恒真断言」 |
| `B` | flake8-bugbear | 可变默认参数、不可达代码、`raise` 没有用 `raise ... from e` 串接异常 |
| `RUF` | ruff-specific | 无用的 `# noqa`、可疑的易混字符（如全角标点）、隐式 `Optional` |
| `SIM` | flake8-simplify | 死分支、可合并的 `if`、多余的 `pass` |
| `PIE` | flake8-pie | 无效表达式（`x == x` 这种）、不必要的展开 |
| `T20` | flake8-print | **散落的 `print()` 调用**——LLM 常留调试打印 |
| `ARG` | flake8-unused-arguments | **永远不会被用到的函数参数**——LLM 常生成用不上的形参 |
| `ERA` | eradicate | **被注释掉的代码**——LLM 常把「另一种写法」当注释留下 |
| `A` | flake8-builtins | **遮蔽内置名**：用 `id`/`type`/`list`/`dict`/`input`/`map` 做变量名 |
| `BLE` | flake8-blind-except | 裸 `except:` 或过宽的 `except Exception`（吞掉所有错误） |
| `RET` | flake8-return | `return` 后的不可达代码、`return` 后多余的 `else` |
| `ISC` | flake8-implicit-str-concat | 字符串列表里漏写逗号导致「隐式拼接」 |
| `TCH` | flake8-type-checking | 只在类型注解里用到的 import 应挪到 `TYPE_CHECKING` 之后 |
| `UP` | pyupgrade | 对 3.12 来说过时的写法（如旧式类型注解） |
| `C4` | flake8-comprehensions | 多余的 `list(...)`/`dict(...)` 包裹生成器 |
| `PERF` | perflint | 性能反模式（如 for 循环里多余的 `list()`） |

注意 `select` 里**没有 `D`（pydocstyle 文档字符串风格）**——项目不强求每个函数都有 Google 风格 docstring，避免规则噪音淹没真正的缺陷。

把这套规则与项目里真实代码对照，能看到「规则确实在生效」。例如 `smoke_test.py` 通篇输出信息，但它用的是 `logging`：

[smoke_test.py:L39-L44](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/smoke_test.py#L39-L44) 配置了 `logging.basicConfig(...)` 并取 `log = logging.getLogger("smoke_test")`，之后所有输出都是 `log.info(...)` / `log.error(...)` / `log.warning(...)`。**这正是规避 `T20`（flake8-print）的正确写法**：`print()` 被规则禁止，但 `logging` 不受限制，且 logging 还自带级别、时间戳、可开关等好处。

豁免规则在 [pyproject.toml:L47-L52](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/pyproject.toml#L47-L52)：

```python
[tool.ruff.lint.per-file-ignores]
# Tests: allow unused args (fixtures), prints (debugging), commented code (examples)
"test_*.py" = ["ARG", "T20", "ERA"]
# Re-export modules: unused imports are intentional
"v7/hardware.py" = ["F401"]
```

这两条例外各有道理：

- `"test_*.py"` 豁免 `ARG`/`T20`/`ERA`。测试函数常有 `def setUp(self)` 这类签名要求的 fixture 参数（`ARG` 会误报）、临时 `print` 调试（`T20`）、以及注释掉的示例断言（`ERA`），所以在测试里放宽这三条。
- `"v7/hardware.py"` 豁免 `F401`（import 但未使用）。u8-l1 讲过，`hardware.py` 是一个「再导出（re-export）」门面模块，它 `import` 一堆协议类是为了让外部 `from v7.hardware import X` 能拿到，本身不直接使用——对它报 `F401` 是误报。

> 术语小结：**select** 是规则白名单；**per-file-ignores** 是按文件 glob 的例外表；**F401** 是「import 未使用」的规则编号；**re-export** 指一个模块为方便外部导入而集中 import 再转出的设计。

#### 4.1.4 代码实践

**实践目标**：亲手让 ruff 报出 `T20` 和 `ERA` 两条违规，体会它们各自抓的是什么，再用合规写法消除。

**操作步骤**：

1. 在仓库根目录执行开发依赖安装（若未装）：`uv sync --group dev`。
2. 跑全仓 lint，记录基线结果：
   ```bash
   uv run ruff check .
   ```
   预期：当前 HEAD 应当返回 `All checks passed!`（退出码 0），因为 CI 在 main 分支上是绿的。**实际运行结果待本地验证**（本讲义写作环境未安装 ruff）。
3. 临时新建一个**示例代码**文件（不是项目原有代码，仅用于触发规则）`/tmp/demo_lint.py`：
   ```python
   # 示例代码：用于触发 T20 与 ERA，请勿入库
   def add(a, b, unused_param):      # ARG: unused_param（但 demo 不在 test_*.py，故会报）
       print("computing")            # T20: print
       # result = a - b              # ERA: 被注释掉的代码
       return a + b
   ```
4. 用 ruff 检查这个临时文件：
   ```bash
   uv run ruff check /tmp/demo_lint.py --select T20,ERA,ARG
   ```
5. 把 `print` 改成 `logging`、删掉注释代码与无用参数，再次检查，确认违规归零。

**需要观察的现象**：第 4 步应分别报 `T201`（print 使用）、`ERA001`（注释掉的代码），`ARG002`（未使用参数）。改写后这三条消失。

**预期结果**：你会直观看到——`T20` 抓「不该留在代码里的调试输出」，`ERA` 抓「以注释形式潜伏的废弃实现」。两者都不会让程序立刻出错，但会长期污染阅读体验、误导后来者。

**为什么这两条对维护质量重要**：

- **T20（print）**：生产 GUI 里散落的 `print()` 会把调试信息混进真正的日志流、在打包后突然弹出到 stdout、且无法按级别关闭。统一改用 `logging` 后，输出有级别（DEBUG/INFO/WARNING/ERROR）、有时间戳、可配置过滤。`smoke_test.py` 就是范例。
- **ERA（注释代码）**：注释掉的「旧实现」是版本控制的反模式——既然有 git，旧代码随时可查，没必要把「另一种写法」留在注释里。注释代码会让人误以为它「可能还在用」，长期累积后没人敢删，最终成为代码考古现场。规则强制：要嘛删掉，要嘛写成真正的注释（解释「为什么」，而不是「另一段可执行代码」）。

#### 4.1.5 小练习与答案

**练习 1**：项目里 `v7/hardware.py` 豁免了 `F401`，但 `test_v7.py` 没有豁免 `F401`。如果某个测试文件 `test_xxx.py` 里有一行 `import os` 却没用 `os`，ruff 会报错吗？

> **答案**：会报 `F401`。`per-file-ignores` 里 `test_*.py` 只豁免了 `ARG/T20/ERA`，不含 `F401`。豁免是「按规则逐条」给的，不是「测试文件一律放行所有规则」。

**练习 2**：为什么 `select` 里没有 `D`（文档字符串风格）家族？加入它可能带来什么副作用？

> **答案**：`D` 要求每个函数/类都有符合风格的 docstring。对一个含大量数据类、协议常量、脚本的项目，强制 `D` 会产生海量「缺 docstring」噪音，把真正重要的缺陷（未用变量、可变默认参数等）淹没。项目选择不启用 `D`，让规则集聚焦在高信号缺陷上。

**练习 3**：`smoke_test.py` 里有大量「输出」，为什么它能通过 `T20` 检查？

> **答案**：因为它用 `logging`（`log.info`/`log.error`），不用 `print()`。`T20`（flake8-print）只针对 `print` 调用，不针对 `logging`。这也示范了「规则不是禁止你输出，而是禁止你用错误的方式输出」。

### 4.2 全仓语法检查 py_compile 与按文件豁免（per-file-ignores）

#### 4.2.1 概念说明

ruff 是「风格与常见缺陷」检查，但它**不保证代码语法能被 Python 解析**（虽然 ruff 也会顺带抓一部分语法问题，但不是它的主战场）。CI 因此加了一道更底层、也更便宜的关卡：`py_compile` 全仓编译。

`py_compile` 是 Python 标准库模块，作用是把一个 `.py` 源文件**编译成字节码（.pyc）**。编译成功意味着「Python 能解析这个文件的语法」。它有两个关键特性：

1. **只检查语法，不执行代码、不解析 import**：它不会真的去 `import numpy`，所以即使你没有装运行依赖，也能跑语法检查。这与 pytest 形成互补——py_compile 快且无副作用，pytest 慢但能验证行为。
2. **`doraise=True` 时，语法错误会抛 `py_compile.PyCompileError`**：这正是 CI 用来判定「失败」的钩子。

为什么需要它？因为仓库里有大量「非入口」脚本（仿真脚本、工具脚本、被 `--live` 控制的分支），它们可能从未被 pytest 触达，也不在主 import 链路上。如果其中某个文件有语法错误（比如多了一个括号、缩进错乱），ruff 未必都报，pytest 也未必跑到——但 `py_compile` 全仓扫一遍就能兜住。

#### 4.2.2 核心流程

CI 里 `py_compile` 步骤的逻辑可以用下面这段伪代码概括（与实际脚本等价）：

```text
需要跳过的目录 = {".git", "__pycache__", ".venv", "venv", "docs"}
for 仓库中每个 *.py 文件 p:
    if p 的路径里包含任何「需跳过的目录」:
        continue                 # 不检查缓存、虚拟环境、文档目录里的 py
    py_compile.compile(p, doraise=True)   # 语法错误会抛异常 → CI 失败
```

它的执行顺序在 CI 的 `python-tests` job 里位于 ruff 之后、pytest 之前——即「先看风格，再看语法，最后看行为」，从便宜到昂贵层层把关。

#### 4.2.3 源码精读

实际的 CI 步骤写在 [ci-tests.yml:L33-L44](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L33-L44)，是一段内联 Python：

```python
import py_compile
from pathlib import Path

skip = {".git", "__pycache__", ".venv", "venv", "docs"}
for p in Path(".").rglob("*.py"):
    if skip & set(p.parts):
        continue
    py_compile.compile(str(p), doraise=True)
```

逐行解读：

- `Path(".").rglob("*.py")`：从仓库根递归找出所有 `.py` 文件。
- `skip & set(p.parts)`：把文件路径的每一段（目录名集合）与跳过集合求交集；只要文件位于任何一个被跳过的目录下（例如 `.venv/lib/.../x.py` 的路径段含 `.venv`），就 `continue` 跳过。这样既跳过了虚拟环境里的第三方库，也跳过了 `__pycache__` 里旧的 `.py`（一般没有，但稳妥起见）和 `docs` 站点目录。
- `py_compile.compile(str(p), doraise=True)`：`doraise=True` 让语法错误抛异常而非静默返回。因为 CI 步骤里任何异常都会让该 step 非零退出，于是「任一文件语法错」→ 整个 job 失败。

**per-file-ignores 在这里不起作用**——要区分清楚：`per-file-ignores` 是 ruff 的概念（4.1 节），只影响 ruff；`py_compile` 不读 ruff 配置，它对所有非跳过文件一视同仁地编译。两套机制是独立的。

还要注意它与 ruff 的分工差异：

| 关卡 | 检查内容 | 是否解析 import | 是否需要装依赖 | 速度 |
| --- | --- | --- | --- | --- |
| ruff | 风格 + 常见缺陷（部分语法） | 否 | 否（纯文本） | 最快 |
| py_compile | 纯语法（能否编译成字节码） | 否 | 否 | 快 |
| pytest | 实际行为（断言是否成立） | 是 | 是 | 最慢 |

一句话：**ruff 管「写得对不对」，py_compile 管「Python 认不认」，pytest 管「行为对不对」**。三者层层递进，缺一不可。

#### 4.2.4 代码实践

**实践目标**：体验 `py_compile` 抓语法错误、却不管 import 与行为。

**操作步骤**：

1. 在仓库根目录跑 CI 里同款的扫描（与 ci-tests.yml 等价）：
   ```bash
   uv run python -c "import py_compile; from pathlib import Path; \
   skip={'.git','__pycache__','.venv','venv','docs'}; \
   [py_compile.compile(str(p), doraise=True) for p in Path('.').rglob('*.py') \
    if not (skip & set(p.parts))]"
   ```
   预期：无输出、退出码 0，表示全部 `.py` 语法合法。**实际结果待本地验证**。
2. 新建一个**示例代码**文件 `/tmp/bad.py`，故意写错语法（比如括号不匹配）：
   ```python
   # 示例代码：故意语法错误
   def f(:        # 语法错误
       return 1
   ```
3. 运行 `uv run python -c "import py_compile; py_compile.compile('/tmp/bad.py', doraise=True)"`。
4. 再写一个**示例代码** `/tmp/missing_import.py`：语法正确，但 import 一个不存在的库：
   ```python
   # 示例代码：语法 OK，但 import 不存在的库
   import this_library_does_not_exist_anywhere
   ```
5. 对它同样跑 `py_compile`。

**需要观察的现象**：第 3 步抛 `py_compile.PyCompileError`（语法错误）；第 5 步**不报错**——因为 `py_compile` 不解析 import，只看语法。

**预期结果**：这正证明了 py_compile 的边界——它能兜住「语法写崩了」，但抓不到「import 了不存在的库」或「运行时才暴露的逻辑错误」，后两者要靠 pytest 和真实运行。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 `.py` 文件位于 `docs/` 目录下且语法错误，CI 的 py_compile 步骤会失败吗？

> **答案**：不会。`skip` 集合包含 `docs`，`docs/` 下的 `.py` 会被 `continue` 跳过。这是因为 `docs` 站点里的脚本属于「展示用」，不参与主代码质量门禁。

**练习 2**：ruff 的 `per-file-ignores` 给 `v7/hardware.py` 豁免了 `F401`。这会不会让 `py_compile` 也对它放行？

> **答案**：不会，两者无关。`per-file-ignores` 只影响 ruff；`py_compile` 根本不读 ruff 配置，它对所有非跳过文件都编译。`hardware.py` 本来语法就是合法的，所以 py_compile 对它自然通过，与豁免无关。

**练习 3**：为什么 CI 把 py_compile 放在 ruff 之后、pytest 之前，而不是最后？

> **答案**：成本递增原则。ruff 最便宜（纯文本，秒级），先跑能最快挡掉风格问题；py_compile 次之（要读文件并编译，但不需要装依赖）；pytest 最贵（要装依赖、要 import、要执行）。先便宜的关卡挡住明显问题，避免「为了一个 print 等半天 pytest」。

### 4.3 GUI pytest 测试矩阵：协议契约与「死代码守卫」

#### 4.3.1 概念说明

前两关（ruff、py_compile）都是「静态」的——不真正运行代码。真正验证「代码行为对不对」的是 pytest。AERIS-10 的 GUI 测试集中在两个文件：

- **`test_v7.py`**：V7 包的单元测试，覆盖 `v7.models`（数据类）、`v7.processing`（DSP）、`v7.workers`（线程）、`v7.hardware`（USB 接口再导出）、`v7.software_fpga`（FPGA 软件镜像）、`v7.replay`（回放引擎）。
- **`test_GUI_V65_Tk.py`**：协议层测试，验证命令构造、11 字节数据包/26 字节状态包解析、以及 `Opcode` 枚举与 FPGA RTL 的一致性。

这两个文件都用 `unittest.TestCase` 风格写，但 CI 用 pytest 来驱动（见 [ci-tests.yml:L46-L51](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/.github/workflows/ci-tests.yml#L46-L51)）。pytest 能发现 `unittest.TestCase` 的子类并按 `test_` 方法名执行，所以「unittest 写法 + pytest 驱动」完全兼容。

这两个文件体现了本项目测试的两大特色，值得专门讲：

1. **协议契约守卫**：很多测试在断言「Python 侧的命令格式 / opcode 编号 / 状态包位域」与 FPGA RTL 完全一致。这与 u11-l3 的跨层契约测试一脉相承——只是这里只比对 Python 内部（`Opcode` 枚举 vs 注释里标注的 RTL 行号），不跨到 Verilog 仿真。
2. **死代码守卫（回归式「负测试」）**：还有相当一部分测试在断言「某些已被删除的东西没有偷偷回来」。这是本项目非常突出的一个测试哲学。

此外还有一个 `smoke_test.py`，它**不是 pytest 的一部分**，而是一个独立的板级冒烟测试脚本，需要单独理解它的定位。

#### 4.3.2 核心流程

CI 的 pytest 步骤只显式指定这两个文件：

```text
uv run pytest test_GUI_V65_Tk.py test_v7.py -v --tb=short
   │
   ├── pytest 发现两个文件里所有 unittest.TestCase 子类的 test_* 方法
   │
   ├── 对每个测试方法：
   │     ├── 若依赖缺失（如 PyQt6/h5py 未装）→ skipUnless 跳过（不计失败）
   │     ├── 若需要 co-sim 真实数据但文件缺失 → self.skipTest(...) 跳过
   │     └── 否则运行断言，失败则记录（--tb=short 给出简短回溯）
   │
   └── 汇总 passed/failed/skipped，有 failed 则退出码非0 → CI 失败
```

注意 `smoke_test.py` **不在** pytest 命令行里——它是一个手动运行的脚本（`python smoke_test.py`），用于真实板子上电后的冒烟测试，属于 u10（板级 bring-up）范畴，不属于这里的单元测试门禁。

#### 4.3.3 源码精读

**(A) 协议契约守卫。** 看 `test_GUI_V65_Tk.py` 的 `TestOpcodeEnum`，它逐条断言 `Opcode` 枚举成员的编号与 FPGA RTL 一致：

[test_GUI_V65_Tk.py:L517-L564](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py#L517-L564) 里，`test_gain_shift_is_0x16` 断言 `Opcode.GAIN_SHIFT == 0x16` 并在 docstring 写明「matches radar_system_top.v:928」；`test_stream_control_is_0x04` 同样标注「matches radar_system_top.v:906」；`test_all_rtl_opcodes_present` 更进一步，把 RTL 里全部 opcode（`{0x01..0x04, 0x10..0x16, 0x20..0x2C, 0x30, 0x31, 0xFF}`）列成集合，断言每一个都在 `Opcode` 枚举里有对应成员。这就是 u6-l2 所说的「opcode 是跨层硬契约」的可执行体现——任何一侧增删 opcode，这条测试都会红。

命令构造与状态包解析也在被测：`TestRadarProtocol` 用 [test_GUI_V65_Tk.py:L34-L69](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py#L34-L69) 验证 `RadarProtocol.build_command` 把 `{opcode, addr, value}` 拼成大端 4 字节，并对 `value` 做掩码防越界（`test_build_command_value_clamp`）；状态包则用 [test_GUI_V65_Tk.py:L124-L163](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_GUI_V65_Tk.py#L124-L163) 的 `_make_status_packet` 构造器精确复刻 26 字节布局（6 个 32 位字 + 头尾），再喂给解析器做往返（round-trip）比对。

**(B) 死代码守卫。** 这是本项目最值得学的测试模式。看 `test_v7.py` 里这几处：

[test_v7.py:L50-L57](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py#L50-L57) 的 `test_no_stale_fields` 断言 `RadarSettings` 里**不再**有 `chirp_duration`、`freq_min/max`、`prf1/2` 等「陈旧字段」——这些字段一旦被误改回来（比如某次重构手滑恢复），测试立刻红。[test_v7.py:L91-L97](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py#L91-L97) 的 `TestNoCrcmodDependency` 断言 `models` 里**没有** `CRCMOD_AVAILABLE` 标志（crcmod 依赖已被移除）；[test_v7.py:L250-L261](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py#L250-L261) 断言 `USBPacketParser` **没有** `crc16_func`、`RadarProcessor` **没有** `multi_prf_unwrap`（u8-l3 讲过，多 PRF 解模糊已被删为死代码）。

这类「断言某物不存在」的测试叫**负测试 / 回归守卫**。它们的价值是：**防止被有意删除的坏设计，在未来某次改动中悄悄复活**。这与 u11-l2 的 bug 回归测试（`test_bug1..15`）是同一种哲学——不仅测「现在对」，还锁死「曾经错过的、不能再来」。

**(C) 软件镜像与 RTL 复位值对齐。** `test_v7.py` 的 `TestSoftwareFPGA` 把 Python 端的 `SoftwareFPGA` 寄存器复位值与 FPGA RTL 逐一核对：

[test_v7.py:L493-L509](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py#L493-L509) 的 `test_reset_defaults` 断言 `detect_threshold == 10_000`、`cfar_alpha == 0x30`、`agc_holdoff == 4` 等，注释写明「match FPGA RTL (radar_system_top.v)」。`SoftwareFPGA` 是 u8-l1 提到的「FPGA 软件镜像」，用于无硬件时在主机端复现 FPGA 的 DSP 行为（参见 `test_process_chirps_returns_radar_frame`）。这条测试保证了「软件镜像」和「真 FPGA」在复位默认值上不漂移。

**(D) 优雅降级的跳过机制。** 测试对可选依赖的缺失用 `skipUnless` 处理，而非报错：

[test_v7.py:L268-L276](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/test_v7.py#L268-L276) 定义 `_pyqt6_available()` 探针，再用 `@unittest.skipUnless(_pyqt6_available(), "PyQt6 not installed")` 装饰 `TestPolarToGeographic`。于是在没装 PyQt6 的 CI/纯算法环境，这些依赖 Qt 的测试自动 skip，不会让 job 变红。这与 u8-l1 讲的 GUI 运行时优雅降级是同一思想在测试侧的镜像。依赖真实 co-sim 数据的测试（如 `TestSoftwareFPGASignalChain`）则用 `self.skipTest("co-sim data not found")` 在运行时跳过。

**(E) smoke_test.py 的定位。** 别把它误当成 pytest 测试。看它的开头与主入口：

[smoke_test.py:L1-L25](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/smoke_test.py#L1-L25) 的 docstring 写明它是「Board Bring-Up Smoke Test — Host-Side Script」，用法是 `python smoke_test.py`（mock 模式）或 `python smoke_test.py --live`（真实 FT2232H 硬件）。它通过 `RadarProtocol.build_command(0x30, 1)` 触发自测试、用 `0x31` 回读结果（u10-l1 讲过这对 opcode），用 [smoke_test.py:L57-L63](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/smoke_test.py#L57-L63) 的 `TEST_NAMES` 字典把 5 位 flags 映射成可读子系统名。它**不在** CI 的 pytest 命令行里，因为它需要硬件（或 mock）并产出日志，是 bring-up 工具而非回归测试。把它放在本讲，是因为它示范了「用 logging 而非 print 通过 T20 检查」，以及它与单元测试在角色上的清晰分工。

> 术语小结：**契约守卫**＝断言两侧接口一致的测试；**负测试 / 死代码守卫**＝断言某坏东西不存在的测试；**SoftwareFPGA**＝FPGA 的主机端软件镜像；**round-trip**＝构造→解析→比对的往返验证；**skipUnless**＝依赖缺失时跳过而非失败的机制。

#### 4.3.4 代码实践

**实践目标**：在本地复现 CI 的 pytest 步骤，并亲手验证一条「死代码守卫」测试如何防止坏代码复活。

**操作步骤**：

1. 安装 GUI 运行依赖（按 `requirements_v7.txt`），再装开发依赖：
   ```bash
   uv sync --group dev
   ```
2. 进入 GUI 目录，跑两个测试文件（与 CI 同款命令）：
   ```bash
   cd 9_Firmware/9_3_GUI
   uv run pytest test_GUI_V65_Tk.py test_v7.py -v --tb=short
   ```
3. 观察输出的 `passed / skipped / failed` 统计。预期绝大多数 passed，少数 skipped（如缺 PyQt6/h5py/co-sim 数据时）。**实际数量待本地验证**。
4. **做一个回归实验**：临时在 `v7/models.py` 的 `RadarSettings` 里「复活」一个被删除的字段，例如加一行 `chirp_duration_1: float = 0.0`，然后重跑：
   ```bash
   uv run pytest test_v7.py::TestRadarSettings::test_no_stale_fields -v
   ```
5. 读测试失败信息，确认它正是因为 `chirp_duration_1` 又出现了而失败。
6. **撤销改动**（`git checkout -- 9_Firmware/9_3_GUI/v7/models.py` 或手动删掉那一行），重跑确认恢复绿色。

**需要观察的现象**：第 4 步该测试由 pass 变 fail，失败信息会指向「Stale field 'chirp_duration_1' still present」。第 6 步撤销后恢复 pass。

**预期结果**：你亲眼看到「死代码守卫」的价值——它不需要你理解 `chirp_duration_1` 当年为什么被删，只要有人试图把它加回来，CI 就会拦住。这就是「负测试」在长期维护里省下的返工成本。

**关于 `smoke_test.py` 的附加实践**（可选，理解角色分工）：

1. 在 GUI 目录跑 mock 模式：`python smoke_test.py`（不加 `--live`）。
2. 观察它打印 5 个子系统的 PASS/FAIL、`result flags: 0b11111`。这是 mock 模式 [smoke_test.py:L128-L131](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/smoke_test.py#L128-L131) 直接返回 `(0x1F, 0x00)` 模拟全通过的结果。
3. 注意：这一切输出走的是 `logging`，所以即便它「很能说」，也通过 ruff 的 `T20` 检查——因为它不在 `test_*.py` 里，所以 `T20` 对它生效，但它根本没用 `print`。

#### 4.3.5 小练习与答案

**练习 1**：`test_v7.py` 用的是 `unittest.TestCase`，但 CI 用 `pytest` 来跑。这矛盾吗？为什么项目要这么搭配？

> **答案**：不矛盾。pytest 能发现并执行 `unittest.TestCase` 子类里的 `test_*` 方法。这样搭配的好处是：测试用标准库 `unittest` 写（无额外依赖、`python -m unittest` 也能跑），同时又享受 pytest 更好的失败回溯（`--tb=short`）、更丰富的插件生态与更简洁的命令行。两个文件头注释也分别给出了 `pytest` 与 `unittest` 两种运行方式。

**练习 2**：`TestOpcodeEnum.test_all_rtl_opcodes_present` 列出了 RTL 的 opcode 集合去比对 Python 枚举。如果有人在 FPGA 那边新增了一个 opcode `0x05` 却忘了在 Python `Opcode` 里加，这条测试会怎样？

> **答案**：需要区分——这条测试是把「预期的 RTL opcode 集合」**硬编码在 Python 测试里**的，它断言这些值都在 `Opcode` 枚举中。如果有人只改 FPGA、没改 Python 测试里的集合，这条测试**不会**自动发现 `0x05`。真正能「跨层发现单边新增」的是 u11-l3 的跨层契约测试（它用解析器从 RTL 源码动态「发现」opcode）。本测试守住的是「已知的 RTL opcode 在 Python 侧都有映射」这一侧的一致性，是契约的 Python 内部视角。

**练习 3**：为什么 `smoke_test.py` 不放进 CI 的 pytest 命令，而 `test_v7.py` 要放？

> **答案**：两者角色不同。`test_v7.py` / `test_GUI_V65_Tk.py` 是「无硬件、纯逻辑」的回归测试，适合在 CI 里每次提交都跑。`smoke_test.py` 是「板级 bring-up 脚本」，默认连真实 FT2232H 硬件（mock 模式只是它的本地回退），且产出的是给人看的日志而非 pass/fail 断言矩阵；它属于 u10 的现场调试工具，不属于回归门禁。把硬件脚本塞进 CI 会拖慢构建、且在无硬件的 CI runner 上无意义。

## 5. 综合实践

把本讲三道关卡串起来，做一次「假装你是 CI」的端到端演练。

**任务背景**：假设你接到一个需求——在 `v7/models.py` 的 `WaveformConfig` 里新增一个字段 `max_chirps_per_frame: int = 32`，并在 `v7/processing.py` 里加一行临时调试 `print("cfg loaded")`。你要预测这次改动会不会被 CI 的三道关卡拦下，并验证你的预测。

**操作步骤**：

1. **先做预测**（不动代码，纯分析）：对每一道关卡写下「会过 / 会失败 / 会跳过」与理由。
   - ruff：`v7/processing.py` 不在 `per-file-ignores` 的 `test_*.py` 里，新增的 `print(...)` 会触发 `T20` → 预测**失败**。
   - py_compile：两处改动语法都合法 → 预测**通过**。
   - pytest：新增字段不删任何旧字段，死代码守卫不会触发；`WaveformConfig` 的现有测试（`TestWaveformConfig`）仍应通过 → 预测**通过**（除非新字段破坏了某个断言）。
2. **执行改动**（可临时做，记得最后撤销）：按上述加字段与 print。
3. **逐关验证**：
   ```bash
   uv run ruff check .                                              # 关卡 1
   uv run python -c "import py_compile,pathlib; ..."                # 关卡 2（用 4.2.4 的命令）
   cd 9_Firmware/9_3_GUI && uv run pytest test_v7.py -v --tb=short  # 关卡 3
   ```
4. **对照预测**：ruff 是否如预测在 `T20` 上失败？py_compile 是否通过？pytest 是否通过？
5. **修正**：把 `print(...)` 改成 `logging.getLogger(__name__).info(...)`（参照 `smoke_test.py` 的写法），重跑 ruff 确认转绿。
6. **撤销所有改动**，恢复仓库干净状态。

**预期结果**：你会切身体会到三道关卡各自拦的是哪类问题——`T20` 在第一关就拦下了 `print`，根本轮不到 pytest 去发现；而新增字段这类「语法合法、风格也干净」的改动，只能靠 pytest 的行为断言把关。这正是分层门禁的价值：**让每种缺陷在它最便宜的那一关就被挡住**。

> 注意：本综合实践涉及临时修改源码。请务必在完成后撤销改动（本讲义不得修改项目源码；这是给你练手用的临时操作，不是要你提交）。

## 6. 本讲小结

- AERIS-10 的 Python 质量门禁是 CI 里 `python-tests` 这个 job 的**三道关卡**：ruff（风格与常见缺陷）→ `py_compile`（全仓语法）→ pytest（行为），从便宜到昂贵层层把关。
- ruff 规则集（`pyproject.toml` 的 `select`）**专门针对 LLM 遗留代码异味**：`T20` 抓散落的 `print`、`ARG` 抓无用参数、`ERA` 抓注释代码、`A` 抓遮蔽内置名等；`per-file-ignores` 对 `test_*.py` 放宽 `ARG/T20/ERA`、对 `v7/hardware.py` 放宽 `F401`（再导出）。
- `py_compile` 全仓扫描只查「语法能否编译」，**不解析 import、不需要装依赖**，能兜住 ruff 与 pytest 都未必触达的脚本文件语法错误；它与 ruff 的 `per-file-ignores` 是两套独立机制。
- `test_v7.py` 与 `test_GUI_V65_Tk.py` 用 `unittest` 风格写、由 pytest 驱动；它们承担两类守卫——**协议契约守卫**（`Opcode` 枚举、命令/状态包往返与 RTL 对齐）与**死代码守卫**（断言 `chirp_duration`/`crcmod`/`multi_prf_unwrap` 等被删物没有复活）。
- 可选依赖缺失时测试用 `@unittest.skipUnless(...)` / `self.skipTest(...)` **跳过而非失败**，是 u8-l1 优雅降级思想在测试侧的镜像。
- `smoke_test.py` 是**独立的板级 bring-up 脚本**（非 pytest），用 `logging` 而非 `print`，因此能通过 `T20`；它属于 u10 范畴，不在 CI 的 pytest 命令里。

## 7. 下一步学习建议

- 想看「测试如何跨 Python↔Verilog↔C 三层验证同一契约」，继续读 **u11-l3 跨层契约测试**——本讲的 `TestOpcodeEnum` 只比对 Python 内部，u11-l3 会用解析器从 RTL 源码动态「发现」opcode，能力更强。
- 想理解「负测试 / 回归守卫」在嵌入式 C 侧的等价物，看 **u11-l2 STM32 单元测试与 bug 回归**——`test_bug1..15` 与本讲的「死代码守卫」是同一种「锁死曾经的问题」哲学。
- 想完整理解 FPGA 侧的回归门禁（iverilog 仿真、真实数据 exact-match），看 **u11-l1 FPGA 回归测试与协同仿真**，它与本讲共同构成「全栈四套测试体系」（Python / MCU / FPGA / 跨层）。
- 若你想给项目新增一条 ruff 规则或一个测试，建议先重读本讲的 `pyproject.toml` 注释与 `test_v7.py` 的负测试写法，保持「规则聚焦高信号缺陷、测试兼顾正负两面」的项目惯例。
