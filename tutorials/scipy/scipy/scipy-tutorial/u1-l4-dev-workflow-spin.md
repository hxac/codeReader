# 开发工作流：spin 命令、测试与文档构建

## 1. 本讲目标

上一讲（u1-l2）我们学会了「如何用 meson-python 把 SciPy 从源码编译出来」。但真正参与 SciPy 开发时，你不会每次都手敲一长串 `meson setup ... && ninja ...`，而是会反复执行三类动作：**编译 → 跑测试 → 看文档效果**，外加代码检查与风格校验。

SciPy 把这些高频动作统一封装进一个叫 **spin** 的开发命令行工具里。本讲学完后，你应当能够：

1. 说清 spin 是什么、它与 meson/ninja/pytest/sphinx 之间的分工。
2. 看懂 `.spin/cmds.py` 中 `build / test / docs / lint / check` 五大命令的实现，知道它们各自转发给了哪个底层工具。
3. 理解 `pytest.ini` 与 `scipy/conftest.py` 如何规定测试的「跑法」，以及 `scipy.constants.test()` 这种入口是怎么挂上去的。
4. 对照 `CONTRIBUTING.rst` 和 `environment.yml` 搭建一套本地开发环境，并独立完成「编译 + 跑 constants 子包测试 + 构建文档」的完整闭环。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是「开发命令行工具」？** 项目越大，参与开发要记的命令就越多：编译要传一堆参数、测试要选子包和标记、文档要用 sphinx 构建、代码风格要跑 linter。如果全靠人脑记忆，新人很容易在「配环境」这一步就被劝退。于是社区约定俗成地写一个**薄薄的命令包装层**，把高频动作收拢成 `spin build`、`spin test` 这样好记的子命令。spin 不是 SciPy 专属的——它是 NumPy 生态共享的一个通用工具，SciPy 只是在自己的 `.spin/cmds.py` 里「定制」了属于自己的命令。

**spin 与 meson 的关系（承接 u1-l2）。** 真正干「编译」苦力的是 meson + ninja，spin 只是站在它们前面，帮你**组装好参数**（比如选用几个核、是否开 ASan、用哪个 BLAS），再调用进去。可以把 spin 想成「前端点单台」，meson/ninja 是「后厨」。

**click 是什么？** spin 的每个子命令都用一个叫 [click](https://click.palletsprojects.com/) 的库来定义「选项 + 帮助文本」。你在终端看到的 `--submodule`、`-m`、`--fix` 这些，都是用 click 的装饰器（`@click.option`）一行行声明出来的。本讲你不需要会写 click，只要能「读懂」这些装饰器声明了哪些开关即可。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用来看什么 |
| --- | --- | --- |
| `.spin/cmds.py` | spin 命令的**全部实现**（约 1260 行） | `build/test/docs/lint/check` 五个函数如何转发给底层工具 |
| `pyproject.toml` | 声明 spin 要加载哪些命令、如何分组 | `[tool.spin.commands]` 段把命令分成 6 组 |
| `pytest.ini` | pytest 的全局配置 | 警告策略 `filterwarnings` 与测试标记 `markers` |
| `scipy/conftest.py` | pytest 的项目级钩子 | `pytest_configure` 如何注册 SciPy 专属标记 |
| `scipy/constants/__init__.py` | 子包入口 | 末尾如何挂上 `.test()` 方法 |
| `environment.yml` | conda 开发环境清单 | spin/pytest/ruff 等开发依赖从哪来 |
| `CONTRIBUTING.rst` | 贡献指南 | 提交 PR 前的三条硬性要求 |

---

## 4. 核心概念与源码讲解

### 4.1 spin 命令注册机制：一个函数 = 一条命令

#### 4.1.1 概念说明

spin 的工作方式很简洁：它去读 `pyproject.toml` 里的 `[tool.spin.commands]`，把里面登记的「命令地址」（形如 `.spin/cmds.py:test`）加载成可执行的子命令，并按分组显示在帮助里。也就是说：

- **命令的「实现」**写在 `.spin/cmds.py`（每个命令一个 Python 函数）。
- **命令的「注册与分组」**写在 `pyproject.toml` 的 `[tool.spin.commands]`。
- 这两处配合，决定了你在终端敲 `spin --help` 时能看到什么。

这种「实现」与「注册」分离的设计，让你可以轻松新增命令：写个函数，再去 pyproject 里登记一行即可。

#### 4.1.2 核心流程

```text
spin <命令>
   │
   ▼
读取 pyproject.toml 的 [tool.spin.commands]
   │  （得到「命令分组 → 命令地址列表」的映射）
   ▼
importlib 动态导入 .spin/cmds.py 里的目标函数
   │
   ▼
click 解析命令行选项（--submodule / -m / ...）
   │
   ▼
函数体执行：组装参数 → 转发给 meson / pytest / sphinx / tools 脚本
```

#### 4.1.3 源码精读

先看「注册」侧。`[tool.spin]` 声明了被管理的包就是 `scipy`：

[pyproject.toml:224-L225](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L224-L225) —— 指定 `package = 'scipy'`，告诉 spin 它要管理的是哪个包。

接着 `[tool.spin.commands]` 把命令分成 6 个组（Build & Develop / Environments / Documentation / Release / Metrics 等）：

[pyproject.toml:269-L296](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pyproject.toml#L269-L296) —— 每一行形如 `".spin/cmds.py:test"`，冒号前是模块路径，冒号后是函数名。注意有些命令地址是 `spin.cmds.meson.run`、`spin.cmds.pip.install`，这些来自 spin **自带**的命令库，SciPy 直接复用；而带 `.spin/cmds.py:` 前缀的是 SciPy **自定义**的。

再看「实现」侧的文件头。`.spin/cmds.py` 一开头就 import 了 spin 框架与 meson 命令库：

[.spin/cmds.py:14-L21](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L14-L21) —— `from spin.cmds import meson` 引入 meson 命令库；`PROJECT_MODULE = "scipy"` 是后续拼接子包路径（如 `scipy.constants`）时用到的常量。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「注册表」与「终端命令」的对应关系。
2. **操作步骤**：
   - 打开 `pyproject.toml` 的 `[tool.spin.commands]` 段，数一下 SciPy 自定义命令（`.spin/cmds.py:` 开头）共有几条。
   - 在已装好 spin 的环境里运行 `spin --help`，观察输出的命令分组是否与 pyproject 一致。
3. **需要观察的现象**：`spin --help` 应该按「Build & Develop / Environments / Documentation / Release / Metrics」分组列出命令。
4. **预期结果**：你能把 `--help` 里看到的每条命令，都映射回 pyproject.toml 里的一行注册地址。
5. **注意**：若环境中没有安装 spin，此命令会失败——请先按本讲 4.4 的步骤创建 `scipy-dev` 环境。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`[tool.spin.commands]` 里同时出现了 `spin.cmds.meson.run` 和 `.spin/cmds.py:python`，它们有什么本质区别？

> **参考答案**：前者是 spin **内置**提供的命令（无需 SciPy 自己实现），后者是 SciPy **自定义**、在 `.spin/cmds.py` 里有对应函数体的命令。

**练习 2**：如果你想新增一条 `spin greet` 命令，需要改哪两个文件？

> **参考答案**：在 `.spin/cmds.py` 里写一个用 `@click.command`（或 `@spin.util.extend_command`）装饰的 `greet` 函数；再到 `pyproject.toml` 的 `[tool.spin.commands]` 某个分组里登记 `".spin/cmds.py:greet"`。

---

### 4.2 build 与 test：编译与跑测试的统一入口

#### 4.2.1 概念说明

`build` 和 `test` 是开发中最常用的两条命令。

- **`spin build`**：编译并安装 SciPy 到本地构建目录（`build-install/`）。它**继承（extend）**了 spin 内置的 `spin.cmds.meson.build`，只是在调用前帮你**追加一批 meson 参数**（关闭字节码编译、设置并发任务数、按需切 BLAS 等）。这就是为什么 spin 的 build 命令既能用 meson 的全部能力，又有 SciPy 专属的便捷开关。
- **`spin test`**：在「已构建好的」SciPy 上跑 pytest。它同样继承了内置的 `spin.cmds.meson.test`，并提供了 SciPy 专属的便捷选项：`-s/--submodule` 只跑某个子包、`-m/--mode` 控制测试标记、`-b/--array-api-backend` 切换数组后端。

「继承并扩展」是这里的关键设计模式：通过装饰器 `@spin.util.extend_command(...)`，SciPy 不必重写整个命令，只需在父命令执行前「插入」自己的参数处理逻辑。

#### 4.2.2 核心流程

`spin build` 的流程：

```text
解析选项 (--debug / --asan / --with-scipy-openblas / --tags ...)
   │
   ▼
向 meson_args 追加 SciPy 专属参数
   ├── -Dpython.bytecompile=-1          （关闭每次重编的字节码编译，省时间）
   ├── --werror / -Db_sanitize=...      （按 --werror / --asan 开关）
   └── --tags=runtime,python-runtime,tests,devel  （决定安装哪些产物）
   │
   ▼
parent_callback(...)  ← 把组装好的参数交给 spin 内置的 meson.build 真正执行
```

`spin test -s constants` 的流程：

```text
submodule="constants"
   │
   ▼
tests = "scipy" + "." + "constants"  →  "scipy.constants"
   │
   ▼
默认追加 -m 'not slow'  （除非用 -m full 跑全量）
   │
   ▼
parent_callback(...)  ← 交给 spin 内置的 meson.test 执行 pytest
```

#### 4.2.3 源码精读

**build 命令**用 `@spin.util.extend_command(spin.cmds.meson.build, doc="")` 继承父命令，函数体先组装参数再回调父命令：

[.spin/cmds.py:63-L84](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L63-L84) —— 装饰器声明「继承 meson.build」，函数 docstring 同时充当 `spin build --help` 的帮助文本；help 里给出的 `spin build --setup-args=...` 正是开发者日常用法。

[.spin/cmds.py:89-L93](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L89-L93) —— 关键细节：`meson_args += ("-Dpython.bytecompile=-1",)`，注释写明「避免每次重装都做昂贵的字节码编译」，这是 SciPy 为了缩短反馈循环做的性能优化。

[.spin/cmds.py:144-L161](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L144-L161) —— 当 `jobs` 未指定时，用物理核数（而非 ninja 默认的 `2N+2`）以避免内存爆掉；最后通过 `parent_callback(...)` 把全部参数交给父命令执行。

**test 命令**同样继承父命令，并多了 `-s/--submodule`、`-m/--mode`、`-b/--array-api-backend` 三个 SciPy 专属选项：

[.spin/cmds.py:174-L177](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L174-L177) —— `-m/--mode` 的**默认值是 `'not slow'`**，意味着 `spin test` 默认会跳过标了 `slow` 的测试。想跑全量得显式 `spin test -m full`。

[.spin/cmds.py:187-L215](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L187-L215) —— test 函数的装饰器与 docstring；docstring 里列举了 `spin test scipy/linalg`、`spin test -- -k "geometric"` 等典型用法，是理解 test 命令能力的第一手资料。

[.spin/cmds.py:255-L267](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L255-L267) —— 核心逻辑：`-s constants` 会被拼成 `tests = "scipy.constants"`；若用户没传任何参数，默认对整个 `scipy` 跑测试；并默认追加 `-m 'not slow'`。

#### 4.2.4 代码实践

1. **实践目标**：用 `spin test -s constants` 只跑 scipy.constants 子包的测试，验证「子包选择」生效。
2. **操作步骤**：
   - 先确保已构建：`spin build`（承接 u1-l2，已构建过可跳过）。
   - 运行 `spin test -s constants`。
   - 对比 `spin test --durations=5 -s constants`，观察它会额外打印最慢的 5 个测试耗时。
3. **需要观察的现象**：终端应只收集 `scipy/constants/tests/` 下的测试（`test_constants.py`、`test_codata.py`），而不会去跑 linalg、stats 等其他子包。
4. **预期结果**：测试全部通过（passed），且默认模式下带 `slow` 标记的用例被跳过（如果 constants 有的话）。耗时通常在几秒到十几秒。具体数字待本地验证。
5. **说明**：本讲不假装已运行命令；若环境中 SciPy 尚未构建，请先完成 4.1.4 的环境准备。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `spin build` 默认不指定 `jobs` 时，要改用「物理核数」而不是 ninja 默认的 `2N+2`？

> **参考答案**：注释引用了 gh-17941 / gh-18443——ninja 默认并发过高会在大型多语言项目里导致内存不足（OOM）。改用物理核数更稳妥。

**练习 2**：`spin test` 与 `spin test -m full` 的区别是什么？

> **参考答案**：前者默认追加 `-m 'not slow'`，跳过慢测试；后者把标记表达式设为 `full`，不再排除任何测试，等价于跑全量（含 `slow`）。

**练习 3**：`-Dpython.bytecompile=-1` 这条参数解决了什么痛点？

> **参考答案**：关闭每次重新安装都触发的 Python 字节码编译（`.pyc` 生成），显著缩短开发时的「改完即重装」反馈时间。

---

### 4.3 docs、lint 与 check：文档构建与代码质量

#### 4.3.1 概念说明

编译和测试之外，开发中还有两类高频需求：**看文档效果**和**保证代码质量**。

- **`spin docs`**：调用 Sphinx 构建 SciPy 文档。它继承 `spin.cmds.meson.docs`，并强制关闭了 `sphinx_gallery_plot`（避免每次构建都重新生成示例图，提速）。常用 `spin docs html` 生成网页，或 `spin docs dist` 打包。
- **`spin lint`**：跑代码风格检查。它依次调用 `tools/lint.py`（基于 ruff 的风格与导入排序）、`tools/check_unicode.py`（禁止非法 Unicode 字符）、`tools/check_test_name.py`（检查测试命名规范）。支持 `--fix` 自动修复、`--diff-against main` 只检查改动的文件。
- **`spin check`**：一组「代码库专项体检」。与 lint 不同，check 每次**只能跑一项**（如 `--xp-markers` 校验数组后端标记、`--installed-files` 校验安装产物完整性、`--loaded-sharedlibs` 查看导入了哪些动态库）。这些检查偏 CI/维护用途，因此不单独占一个顶级命令，而是收拢在 `check` 下。

#### 4.3.2 核心流程

```text
spin docs html
   └─ parent_callback(... sphinx_gallery_plot=False ...) → meson.docs → sphinx-build

spin lint --diff-against main
   ├─ tools/lint.py --diff-against=main [--fix]   （ruff 风格 + 导入排序）
   ├─ tools/check_unicode.py                       （非法 Unicode）
   └─ tools/check_test_name.py                     （测试命名规范）

spin check --installed-files
   └─ 先自动 build → tools/check_installation.py <install_dir>
```

注意 `lint` 和 `check` 的一个重要区别：`lint` 一次跑完三个脚本；`check` 强制要求**恰好选一个**选项（见源码里的 `sum(options) == 1` 校验），否则直接报错退出。

#### 4.3.3 源码精读

**docs 命令**：继承父命令并剥离/覆盖了 `sphinx_gallery_plot` 参数：

[.spin/cmds.py:278-L298](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L278-L298) —— `remove_args=("sphinx_gallery_plot", )` 在继承时移除该选项，函数体里又把 `sphinx_gallery_plot=False` 显式传给父命令，从而默认禁用示例图绘制以加速构建。

**lint 命令**：把三段检查串成一个流水线：

[.spin/cmds.py:614-L657](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L614-L657) —— 选项 `--diff-against`（默认 `main`，只 lint 改动文件）、`--files`、`--all`、`--fix` 互斥/组合；函数体依次 `util.run(...)` 三个脚本。默认 `--no-cython=True`，即默认不跑 cython-lint（更快）。

**check 命令**：用「选项之和必须为 1」来保证单次只跑一种体检：

[.spin/cmds.py:660-L734](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/.spin/cmds.py#L660-L734) —— `options = [xp_markers, installed_files, symbol_hiding, loaded_sharedlibs]`，`if not sum(options) == 1:` 时打印红色错误并 `sys.exit(1)`；注释明确说明这些检查「值得在 CI 跑，但不值得各自占一个顶级命令」。

#### 4.3.4 代码实践

1. **实践目标**：用 `spin lint` 检查你（或当前分支）改动过的文件，并尝试构建一次文档。
2. **操作步骤**：
   - 运行 `spin lint --diff-against main`（在没有改动时可加 `--all` 检查全部，但会比较慢）。
   - 尝试 `spin docs html`（首次构建文档较慢，可能需要数分钟）。
3. **需要观察的现象**：
   - lint 会打印它实际执行的命令行（蓝色），随后是 ruff 等工具的输出；若有可自动修复的问题，加 `--fix` 后再次运行应减少报错。
   - docs 构建完成后，会在某个 `_build/html/` 目录下生成网页（具体路径见 sphinx 输出，待本地验证）。
4. **预期结果**：lint 在干净的代码上应无报错；docs 成功生成 HTML。两者首次执行都偏慢，属正常现象。
5. **说明**：文档构建依赖 sphinx、pydata-sphinx-theme 等一堆包，必须先按 `environment.yml` 装好（见 4.4）。若机器资源有限，文档构建这一步可标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `spin docs` 要默认禁用 `sphinx_gallery_plot`？

> **参考答案**：绘制 gallery 示例图很慢；开发期反复构建文档时，默认关掉它能大幅缩短反馈时间，发布时再单独开启。

**练习 2**：`spin check --xp-markers --installed-files` 会发生什么？

> **参考答案**：会报错退出。check 要求「恰好选一个」选项，这里选了两个，`sum(options) == 1` 不成立，会打印 `Exactly one option to 'check' should be given` 并 `sys.exit(1)`。

**练习 3**：`spin lint` 与 `spin check` 在「一次跑几项」上的设计差异是什么？

> **参考答案**：lint 是「流水线」，一次顺序跑完 lint.py / check_unicode.py / check_test_name.py 三个脚本；check 是「单选开关」，每次强制只能执行一种体检，避免不同检查互相干扰或在不同平台上误报。

---

### 4.4 pytest 配置、测试组织与贡献流程

#### 4.4.1 概念说明

`spin test` 最终调用的还是 pytest，所以理解测试「跑法」必须回到 pytest 的配置层。

- **`pytest.ini`**：项目级 pytest 配置。它做了两件影响所有测试的事：(1) `filterwarnings = error`——把警告**当作错误**抛出，倒逼代码处理每一个 warning；(2) `markers`——声明 SciPy 自定义的测试标记（`slow`、`xslow`、`xfail_on_32bit`、`array_api_backends` 等），这就是 4.2 里 `-m 'not slow'` 之所以生效的根源。
- **`scipy/conftest.py`**：pytest 的项目级钩子，`pytest_configure` 函数再次注册这些标记（与 pytest.ini **双写**，防止某些插件场景下漏注册而产生 `PytestUnknownMarkWarning`）。
- **`scipy/constants/__init__.py` 末尾**：每个子包都挂了一个 `test = PytestTester(__name__)`，于是你可以写 `scipy.constants.test()` 来跑该子包测试——这与 `spin test -s constants` 是殊途同归的两种入口。
- **`CONTRIBUTING.rst`** + **`environment.yml`**：贡献流程的「硬件」与「软件」。前者规定 PR 的三条硬性要求（单测/文档/风格、许可证、AI 政策）；后者给出 `scipy-dev` 这个 conda 环境的完整依赖清单，包括 spin 本身和 `click<8.3.0`（因 click 8.3.0 对 spin 是破坏性的，见 gh-23642）。

#### 4.4.2 核心流程

测试标记的「双写」与生效链路：

```text
pytest.ini  的 markers 段   ─┐
                              ├─→  pytest 认识 slow/xslow/... 标记
scipy/conftest.py 的           │     → -m 'not slow' 才能正确过滤
   pytest_configure()  再注册 ─┘
```

子包测试入口的挂载：

```text
每个子包 __init__.py 末尾：
   from scipy._lib._testutils import PytestTester
   test = PytestTester(__name__)     ←  于是 scipy.<子包>.test() 可用
   del PytestTester
```

#### 4.4.3 源码精读

`pytest.ini` 把警告升级为错误，并声明标记：

[pytest.ini:9-L24](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pytest.ini#L9-L24) —— 首行 `filterwarnings = error` 是默认基线（警告即错误），其下每一行 `ignore:...` 是有选择地放行特定第三方警告（如 IPython、JAX、cupy 的已知噪声）。

[pytest.ini:27-L40](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/pytest.ini#L27-L40) —— `markers` 段声明了 `slow`、`xslow`、`xfail_on_32bit`、`array_api_backends`、`thread_unsafe`、`fail_asan` 等 SciPy 专属标记；注释提醒「更新这里时也要同步更新 `scipy/conftest.py`」。

`scipy/conftest.py` 在 `pytest_configure` 里**再次**注册同样的标记：

[scipy/conftest.py:41-L60](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/conftest.py#L41-L60) —— 函数 docstring 明说「需要在 pytest.ini **以及** 这里都注册一遍」，这是为了避免在某些插件加载顺序下出现 `PytestUnknownMarkWarning`。

子包入口挂载 `PytestTester`（以 constants 为例）：

[scipy/constants/__init__.py:355-L357](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/constants/__init__.py#L355-L357) —— 三行代码就让 `scipy.constants.test()` 成为可调用对象，它内部会以该模块名为根去收集并运行 pytest。这正是上一讲（u1-l3）提到的「`PytestTester` 统一了 `scipy.test()` 与各子包 `.test()` 入口」的具体落点。

贡献流程与环境：

[CONTRIBUTING.rst:10-L30](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/CONTRIBUTING.rst#L10-L30) —— 列出 PR 的三条硬性要求：单测/文档/风格到位、许可证兼容、遵守 AI 政策并附声明。

[environment.yml:1-L5](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/environment.yml#L1-L5) —— 文件头注释给出创建环境的命令：`conda env create -f environment.yml` 再 `conda activate scipy-dev`。

[environment.yml:61-L66](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/environment.yml#L61-L66) —— 明确列出开发用 CLI 依赖 `spin` 与 `click<8.3.0`（注释指向 gh-23642，说明 click 8.3.0 会破坏 spin），以及 lint 用的 `ruff>=0.12.0`、`cython-lint`。这正是本讲所有 `spin` 命令得以运行的来源。

#### 4.4.4 代码实践

1. **实践目标**：从「环境准备」到「跑测试」走一遍完整闭环，并验证两种测试入口等价。
2. **操作步骤**：
   - 用 `conda env create -f environment.yml` 创建 `scipy-dev` 环境并 `conda activate scipy-dev`。
   - `spin build` 编译；随后 `spin test -s constants`。
   - 进入 Python（`spin python`）执行 `import scipy.constants as c; c.test()`，观察它是否与 `spin test -s constants` 收集到同一批测试。
3. **需要观察的现象**：`scipy.constants.test()` 应收集 `scipy/constants/tests/` 下的测试，与 `spin test -s constants` 结果一致。
4. **预期结果**：两种入口跑出的用例数、通过数一致。具体耗时与通过数待本地验证。
5. **说明**：创建 conda 环境会下载较多包，耗时取决于网络；若不使用 conda，也可参照 environment.yml 用 pip 手动装关键依赖（spin、meson-python、pytest>=8.0.0、ruff 等），但官方推荐 conda 路线。

#### 4.4.5 小练习与答案

**练习 1**：`pytest.ini` 里 `filterwarnings = error` 会带来什么后果？为什么 SciPy 还要在下面列一堆 `ignore`？

> **参考答案**：基线 `error` 让任何未处理警告都升级为错误，迫使开发者修复或显式忽略；列 `ignore` 是为了放行那些来自第三方库（IPython/JAX/cupy 等）的已知、不可避免的噪声，避免它们误伤测试。

**练习 2**：为什么标记要同时在 `pytest.ini` 和 `scipy/conftest.py` 注册？

> **参考答案**：在某些插件加载顺序下，仅 pytest.ini 注册可能仍触发 `PytestUnknownMarkWarning`；conftest 的 `pytest_configure` 里再注册一次作为双保险（源码 docstring 明确说明）。

**练习 3**：`spin test -s constants` 和 `python -c "import scipy.constants as c; c.test()"` 本质上有什么共同点？

> **参考答案**：两者最终都是以 `scipy.constants` 为根去收集并运行 pytest——前者是 spin 帮你拼好参数后调用 meson.test→pytest，后者直接用子包自带的 `PytestTester(__name__)` 入口。殊途同归。

---

## 5. 综合实践

把本讲四块内容串起来，完成一次「**模拟贡献**」闭环。假设你刚 clone 了 SciPy，目标是：搭好环境 → 编译 → 只跑 constants 测试 → 跑 lint → 构建文档首页。

1. **建环境**：`conda env create -f environment.yml && conda activate scipy-dev`（对应 4.4）。
2. **编译**：`spin build`（对应 4.2，承接 u1-l2 的 meson 构建）。
3. **定向测试**：`spin test -s constants --durations=3`，确认 constants 子包全绿，并记录最慢 3 个用例（对应 4.2）。
4. **质量检查**：故意在某测试文件里加一行多余的空格或用 `np.long`（已弃用），运行 `spin lint --diff-against main --fix`，观察 ruff 是否报错并尝试自动修复；再撤回改动（对应 4.3）。
5. **文档构建**：`spin docs html`，构建完成后在生成的 `_build/html/` 中用浏览器打开 `index.html`，确认 SciPy 文档首页能正常渲染（对应 4.3）。

> 要求：把每一步的**实际命令、耗时、关键输出**记录成一张表。如果某一步失败，先对照本讲源码精读部分定位（例如 docs 失败多半是缺 sphinx 相关依赖，lint 失败多半是 ruff 版本或新引入的违规）。具体耗时与输出待本地验证。

---

## 6. 本讲小结

- **spin 是 SciPy 的开发命令行前端**：实现写在 `.spin/cmds.py`，注册与分组写在 `pyproject.toml` 的 `[tool.spin.commands]`；一个函数对应一条命令。
- **核心命令都「继承并扩展」spin 内置命令**：`build/test/docs` 用 `@spin.util.extend_command(...)` 在父命令前插入 SciPy 专属参数，再回调父命令执行，避免了重复造轮子。
- **`spin test -s constants` 的机制**：`-s` 把子包名拼成 `scipy.constants`，默认追加 `-m 'not slow'`，最终交给 pytest 执行。
- **`spin lint` 是流水线、`spin check` 是单选开关**：前者一次跑 lint/unicode/test-name 三个脚本，后者强制每次只跑一种体检。
- **测试「跑法」由 pytest.ini + conftest.py 双重规定**：`filterwarnings=error` 把警告升级为错误；标记在两处双写注册；每个子包通过 `PytestTester` 挂上 `.test()` 入口。
- **开发环境与流程有明确出处**：`environment.yml` 给出 `scipy-dev` 环境与 spin/click/ruff 等依赖；`CONTRIBUTING.rst` 规定 PR 的三条硬性要求。

---

## 7. 下一步学习建议

- **横向巩固**：动手把 4.4.4 的闭环在本机跑通，确保 `spin build/test/lint/docs` 都能用，这是后续所有子包讲义（u3 起）的前置能力。
- **纵向深入测试体系**：本讲只讲了测试的「入口与配置」。若想了解 SciPy 如何写参数化测试、基于 hypothesis 的属性测试、以及 `PytestTester` 的内部实现，可直接阅读 `scipy/_lib/_testutils.py`，并预读大纲中 u13-l2（测试体系：pytest、_testutils 与 hypothesis）。
- **回到构建细节**：若对 `spin build` 背后的 meson 细节（`--tags`、BLAS 选择、ASan）感兴趣，可回头结合 u1-l2 复习 `meson.build` / `meson.options`，并预读 u13-l1（用 Cython/Fortran 添加底层函数）了解如何新增编译扩展。
- **公共 API 治理预告**：本讲提到的 `PytestTester`、`tools/check_*` 系列脚本，与 u13-l4（架构演进与公共 API 治理）主题相关，届时会系统讲解。
