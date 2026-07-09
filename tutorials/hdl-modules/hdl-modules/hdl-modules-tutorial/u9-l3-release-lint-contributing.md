# 发布工程、Lint 与贡献规范

## 1. 本讲目标

本讲是手册第 9 单元（工具、文档与贡献）的最后一篇。前面 u9-l1 讲了「文档怎么被构建出来」，u9-l2 讲了「VHDL 怎么被综合成可量化的资源数字」，本讲收尾，回答两个工程化问题：

1. **项目怎么发版？** —— 一个稳定的版本号是怎么从代码里「长」出来、打上 git tag 的？
2. **怎么保证每一行提交进来的代码都达标？** —— 版权头、文件格式、Python 风格这三道 lint 闸门如何在 CI 里自动拦截劣质代码。

学完本讲，你应该能够：

- 说清 `tools/tag_release.py` 一次发版执行的「校验 → 改版本号 → 搬运发布说明 → 提交打 tag → 回到 dev」五个步骤。
- 理解 `test/lint/` 下三组测试（版权头、文件格式、Python lint）各自检查什么、用到了哪些 tsfpga 工具。
- 读懂 `pyproject.toml` 里的 ruff 配置：`select = ["ALL"]` + 一长串 `ignore` + 三套 `per-file-ignores` 的取舍。
- 按照贡献指南为一段新代码补齐版权头，并让它顺利通过本地 lint。

## 2. 前置知识

本讲是 `advanced` 层，但用到的概念都很基础，这里先讲透。

- **Lint（静态检查）**：不运行代码、只读源码文本，按一套规则挑出「风格不一致」「潜在坏味道」「格式错误」等问题。hdl-modules 用 Python 的 [ruff](https://docs.astral.sh/ruff/) 做 Python lint，用 tsfpga 自带的检查器做版权头与文件格式检查。
- **pytest**：Python 最主流的测试框架。hdl-modules 把 lint 检查也写成了 pytest 测试函数（函数名以 `test_` 开头），所以一条 `python3 -m pytest --verbose` 就能把 lint 和功能测试一起跑掉。
- **Semantic Versioning（语义化版本号）**：版本号写作 `MAJOR.MINOR.PATCH`，例如 `6.2.1`。不兼容的破坏性改动升 MAJOR，向后兼容的新功能升 MINOR，bug 修复升 PATCH。hdl-modules 的发版严格遵循它。
- **git tag**：给某个 commit 打一个「书签」。hdl-modules 用 `v6.2.1` 这种 `v` 开头的 tag 标记一个正式发布点。
- **CI（持续集成）**：每次提 PR、每次合并到 `main`、以及每晚定时，GitHub Actions 会自动跑一遍 `.github/workflows/ci.yml`，把 pytest、仿真、文档构建全过一遍。lint 不过 = CI 变红 = PR 不能合并。
- **本讲承接 u1-l3**：那一讲讲过工具链依赖（tsfpga / VUnit / hdl-registers）和「算根目录 → `sys.path.insert` → `import tools_pythonpath` → 再 import 第三方包」的引导套路。本讲的 `tag_release.py` 用的是同一套引导头；lint 测试也都依赖 tsfpga（`find_git_files`、`CopyrightHeader` 等都来自 tsfpga），所以同样需要先配好 PYTHONPATH 指向本地克隆的兄弟仓库。

> 一句话定位：hdl-modules 把「发版」和「代码质量」都固化成脚本与测试，让人工不可靠的步骤变成可复现的自动化流程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tools/tag_release.py` | 发版脚本：校验新版本号、更新版本、搬运发布说明、提交并打 tag。 |
| `hdl_modules/__init__.py` | 版本号的「单一信息源」，`__version__` 字符串写在这里。 |
| `doc/release_notes/unreleased.rst` | 「未发布」变更日志，发版时会被改名为 `X.Y.Z.rst`。 |
| `test/lint/test_copyright.py` | 版权头 lint：检查所有 `.py/.vhd/.tcl/.cpp/.h` 文件头部。 |
| `test/lint/test_file_format.py` | 文件格式 lint：编码、行尾、Tab、回车、尾随空格、行宽。 |
| `test/lint/test_python_lint.py` | Python lint：调用 `ruff check` 与 `ruff format --check`。 |
| `pyproject.toml` | ruff 的全部配置：选哪些规则、忽略哪些规则、按文件路径豁免。 |
| `doc/sphinx/contributing.rst` | 贡献指南：如何维护变更日志、如何发版、如何推送 tag。 |
| `.github/workflows/ci.yml` | CI 流水线，lint 测试随 `pytest` 一起跑。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：发版流程、lint 三件套、ruff 配置、贡献规范。

### 4.1 发布工程：tag_release.py 的发版流程

#### 4.1.1 概念说明

hdl-modules 的「正式发布」并不打包任何二进制——它只是一份 VHDL 源码仓库。所谓发版，本质上是做三件事：

1. 把版本号从开发态（如 `6.2.2-dev`）定格成一个正式号（如 `6.2.2`）。
2. 把「未发布的变更日志」冻结成这一版的发布说明。
3. 给对应的 commit 打一个 `v6.2.2` 的 git tag，作为全世界引用这一版的锚点。

这三件事如果让人手工做，很容易漏步骤（忘记改版本号、忘记搬日志、tag 打错 commit）。`tools/tag_release.py` 把整件事编成一个幂等的脚本，配合 tsfpga 提供的 `VersionNumberHandler` 等工具，把人能做到的事压缩到「跑一条命令」。

#### 4.1.2 核心流程

`tag_release.py X.Y.Z` 的一次执行顺序如下（伪代码）：

```
解析命令行，拿到 new_version = X.Y.Z
git_tag = verify_new_version_number(...)        # 校验：干净仓库、日志非空、版本递增、tag 不重复
handler = VersionNumberHandler(version_file=hdl_modules/__init__.py)
handler.update(new_version)                     # 把 __version__ 改成 X.Y.Z
move_release_notes(version=X.Y.Z)               # unreleased.rst -> X.Y.Z.rst，再建空 unreleased.rst
commit_and_tag_release(...)                     # 提交 + 打 tag vX.Y.Z
handler.bump_to_prelease()                      # __version__ 改回下一个 -dev
make_commit("Set pre-release version number")   # 再提交一次
```

关键设计有两点：

- **发版前要过校验**：仓库必须干净（`is_dirty()` 为假）、`unreleased.rst` 不能是空的、新版本号必须严格大于当前版本号、新 tag 不能与历史 tag 重名或更小。任一条不满足，脚本直接 `sys.exit` 退出，绝不带病发版。
- **发版后立即回到开发态**：打完 tag，脚本会把 `__version__` 再 bump 成一个 `-dev` 预发布号并提交，这样 `main` 分支上永远是「下一个开发版」，正式号只活在那个 tag 指向的 commit 上。

版本号的大小比较不是字符串比较，而是用 `packaging.version.parse` 做语义比较。这能正确处理预发布后缀：

\[
\text{parse}(6.2.2\text{-dev}) < \text{parse}(6.2.2)
\]

所以当前版本是 `6.2.2-dev` 时，发一个 `6.2.2` 是合法的（`6.2.2 > 6.2.2-dev`）。

#### 4.1.3 源码精读

脚本的引导头与 u1-l3、u9-l2 完全一致——`sys.path.insert(0, ...)` 优先本地检出，再 import 会改 PYTHONPATH 的 `tools_pythonpath`，然后才 import 第三方与本项目包：

[tools/tag_release.py:15-33](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tag_release.py#L15-L33) —— 引导头与依赖导入。注意第 32 行从 `hdl_modules` 导入 `__version__ as current_version`，这就是「当前版本号」的来源。

`main()` 把五步流程串起来，是整个发版动作的编排：

[tools/tag_release.py:38-59](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tag_release.py#L38-L59) —— `main()`：校验 → 改版本号 → 搬日志 → 提交打 tag → 回到 pre-release。

其中 `verify_new_version_number` 集中了所有「防呆」校验：

[tools/tag_release.py:61-84](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tag_release.py#L61-L84) —— 校验仓库干净、`unreleased.rst` 非空、新版本号严格大于当前与所有历史 tag。

```python
if repo.is_dirty():
    sys.exit("Must make release from clean repo")

unreleased_notes_file = RELEASE_NOTES / "unreleased.rst"
if read_file(unreleased_notes_file) in ["", UNRELEASED_EMPTY]:
    sys.exit(f"The unreleased notes file {unreleased_notes_file} should not be empty")

if parse(new_version) <= parse(current_version):
    sys.exit(f"New version {new_version} is not greater than current version {current_version}")
```

`move_release_notes` 把「未发布」日志改名为正式日志，并重建一个空的未发布文件，再把两个文件加进 git 索引以便进入提交：

[tools/tag_release.py:87-101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tag_release.py#L87-L101) —— 用 `shutil.move` 搬运日志，用 `create_file` 重建空文件。

版本号本身住在 `hdl_modules/__init__.py`，发版前后由 `VersionNumberHandler` 改写的就是这一行：

[hdl_modules/__init__.py:22](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/hdl_modules/__init__.py#L22) —— `__version__ = "6.2.2-dev"`，本仓库当前的版本号就这一处。

未发布日志的真实模样：

[doc/release_notes/unreleased.rst:1-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/release_notes/unreleased.rst#L1-L19) —— 按「keep a changelog」格式分 `Fixes` / `Breaking changes` 等小节，发版时整份被改名归档。

#### 4.1.4 代码实践

**目标**：在不真正发版的前提下，走查一遍发版脚本的校验逻辑，理解每条 `sys.exit` 在什么情况下触发。

**步骤**：

1. 配好 PYTHONPATH，使 `tsfpga` 与 `hdl_modules` 可被导入（见 u1-l3）。
2. 准备阅读脚本，但不执行真实发版。在仓库根目录运行：
   ```bash
   python3 -c "from hdl_modules import __version__; print(__version__)"
   ```
   确认当前版本号（预期为 `6.2.2-dev`）。
3. 阅读脚本里 `verify_new_version_number` 的四条 `sys.exit`，分别构造「触发条件」并口头预测退出信息：
   - 工作区有未提交改动时跑脚本 → 预期 `Must make release from clean repo`。
   - 把 `doc/release_notes/unreleased.rst` 清空成只有空白 → 预期提示该文件不应为空。
   - 传一个小于等于当前版本号的参数，如 `6.2.0` → 预期提示版本号未递增。
4. **不要真的执行 `tag_release.py`**（它会改版本号、打 tag、改 git 历史）。理解逻辑即可。

**需要观察的现象**：每条校验都在「真正的破坏性动作」（改文件、打 tag）之前发生，且任一失败立即退出，绝不带病继续。

**预期结果**：你能用自己的话复述「为什么必须从干净仓库发版」「为什么未发布日志不能为空」。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 在打完 tag 之后还要 `bump_to_prelease()` 并再提交一次？

> **答案**：这样 `main` 分支上的 `__version__` 永远是「下一个开发版」（`-dev` 后缀），正式版本号只存在于 `vX.Y.Z` tag 指向的那个 commit。后续任何开发中的提交都不会被误认为是某个已发布版本。

**练习 2**：如果有人想发一个 `6.2.2`，但历史里已经存在 `v6.3.0` 这个 tag，脚会发生什么？

> **答案**：`verify_new_version_number` 会遍历所有历史 tag，发现 `parse("6.2.2") <= parse("6.3.0")`，于是 `sys.exit` 提示新版本号不大于已存在的 tag，拒绝发版。版本号必须单调递增。

### 4.2 Lint 三件套：版权头、文件格式、Python lint

#### 4.2.1 概念说明

hdl-modules 把代码质量拆成三道独立的闸门，都放在 `test/lint/` 下，都写成 pytest 测试：

1. **版权头（copyright）**：每个源码文件开头必须有一段统一的 BSD 3-Clause 版权声明。这是开源项目合规的底线——少了它，别人无法确认授权。
2. **文件格式（file format）**：纯文本层面的卫生——只能用 ASCII、行尾必须是 UNIX 换行 `\n`、不得有 Tab、不得有 `\r`、不得有尾随空格、行宽有上限。这些是「不同编辑器/系统之间不会出幺蛾子」的保障。
3. **Python lint**：Python 代码风格与潜在问题，交给工业级工具 ruff。

三者有一个共同的扫描入口：tsfpga 的 `find_git_files`。它返回「被 git 跟踪的文件」，所以临时文件、生成物（如 `generated/`）天然不会被检查。

#### 4.2.2 核心流程

三组测试的执行模型一致：

```
find_git_files(REPO_ROOT, file_endings_include=...)   # 从 git 索引列出待检文件
for file in files:
    checker(file)                                      # 逐文件检查，不合格则 print 并置 test_ok=False
assert test_ok                                         # 只要有一个不合格，整个测试失败
```

- 版权头检查：对 `.py/.vhd/.tcl/.cpp/.h` 五种后缀，用 tsfpga 的 `CopyrightHeader.check_file()` 比对期望头。
- 文件格式检查：对除 `png/svg` 外的所有 git 文件，跑六项原子检查（编码、行尾、Tab、回车、尾随空格、行宽）。
- Python lint：不遍历文件，而是直接调 `ruff` 子进程对整个项目跑两遍——`ruff check`（查问题）和 `ruff format --check --diff`（查格式）。

CI 里这三组测试并不在单独的 job，而是和功能测试一起，被 `python3 -m pytest --verbose` 收集运行（见 `.github/workflows/ci.yml` 的 `pytest` job）。

#### 4.2.3 源码精读

**版权头检查**：`COPYRIGHT_TEXT` 是版权声明正文的四行，`COPYRIGHT_HOLDER` 是版权持有人。`files_to_check_for_copyright_header` 按五种后缀从 git 索引收集文件：

[test/lint/test_copyright.py:17-36](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/test/lint/test_copyright.py#L17-L36) —— 版权声明文本与待检文件后缀清单。

```python
COPYRIGHT_HOLDER = "Lukas Vik"
COPYRIGHT_TEXT = [
    "This file is part of the hdl-modules project, a collection of reusable, high-quality,",
    "peer-reviewed VHDL building blocks.",
    WEBSITE_URL,
    REPOSITORY_URL,
]
```

测试本体逐文件交给 `CopyrightHeader` 检查，失败时打印期望头方便修复：

[test/lint/test_copyright.py:39-48](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/test/lint/test_copyright.py#L39-L48) —— 遍历检查，失败打印期望版权头。

`CopyrightHeader`（来自 tsfpga）会按语言自动套用注释前缀——Python/Tcl 用 `#`，VHDL 用 `--`，所以期望头长得不同但「内容」一致：

[modules/resync/src/resync_level.vhd:1-8](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/resync/src/resync_level.vhd#L1-L8) —— VHDL 文件的版权头，用 `--` 注释。

[tools/tag_release.py:1-8](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/tools/tag_release.py#L1-L8) —— Python/Tcl 文件的版权头，用 `#` 注释。

**文件格式检查**：六项原子检查，每项对应一个测试函数，共享 `files_to_test()` 扫描器：

[test/lint/test_file_format.py:34-114](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/test/lint/test_file_format.py#L34-L114) —— 编码、行尾、Tab、回车、尾随空格、行宽六项检查；行宽检查对部分「RST/VHDL 语法无法断行」的文件做了 `excludes`。

值得注意的工程取舍：行宽检查显式排除了一批文件，理由都写得很具体——例如 `license.txt` 必须原样照抄许可正文、若干 `.vhd`/`.rst` 因语法无法断行。这说明 lint 不是「一刀切」，而是对每条规则的例外都留了据可查的出口。

**Python lint 检查**：极薄，本质就是两次 ruff 子进程调用：

[test/lint/test_python_lint.py:17-26](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/test/lint/test_python_lint.py#L17-L26) —— `_run_ruff` 用 `sys.executable -m ruff` 调用，`cwd=REPO_ROOT` 保证读到仓库根的配置。

```python
def test_ruff_check():
    _run_ruff(command=["check"])

def test_ruff_format():
    _run_ruff(command=["format", "--check", "--diff"])
```

`--check` 让 format 只检查不改文件，`--diff` 把差异打到 stdout，CI 里一眼能看出哪里没格式化。

#### 4.2.4 代码实践

**目标**：亲手让一条版权头 lint 失败，再修好它，体会闸门如何拦截。

**步骤**：

1. 在仓库根目录新建一个临时 Python 文件（**不要** `git add`，只是本地试验），故意不写版权头：
   ```bash
   cat > /tmp/probe_copyright.py <<'EOF'
   def hello():
       return "no copyright header here"
   EOF
   cp /tmp/probe_copyright.py ./probe_tmp.py
   ```
2. 直接调用 tsfpga 的检查器模拟 lint（在仓库根，已配 PYTHONPATH）：
   ```bash
   python3 -c "
   from tsfpga.test.lint.copyright_lint import CopyrightHeader
   from test.lint.test_copyright import COPYRIGHT_HOLDER, COPYRIGHT_TEXT
   c = CopyrightHeader('probe_tmp.py', COPYRIGHT_HOLDER, COPYRIGHT_TEXT)
   print('OK' if c.check_file() else 'FAIL')
   print(c.expected_copyright_header)
   "
   ```
3. 把打印出的 `expected_copyright_header` 粘到 `probe_tmp.py` 顶部，重跑第 2 步。
4. 清理：`rm probe_tmp.py`。

**需要观察的现象**：第一次打印 `FAIL` 并给出期望头；补齐后打印 `OK`。

**预期结果**：你直观看到「版权头 lint 就是比较文件头与一段固定模板」。运行结果待本地验证。

> 提示：`probe_tmp.py` 没有被 `git add`，所以不会进入 `find_git_files` 的结果，真正的 `test_copyright_header_of_all_checked_in_files` 其实查不到它——这里只是用检查器对象手动模拟一次。真正进 CI 的检查范围是「已 git 跟踪的文件」。

#### 4.2.5 小练习与答案

**练习 1**：为什么文件格式 lint 要排除 `png` 和 `svg`？

> **答案**：它们是二进制/图片文件，不是文本。对其做「行尾换行」「无 Tab」「纯 ASCII」检查毫无意义，还会误报，所以 `files_to_test()` 用 `file_endings_avoid=("png", "svg")` 排除。

**练习 2**：`test_python_lint.py` 里为什么要用 `sys.executable -m ruff` 而不是直接 `ruff`？

> **答案**：`sys.executable` 指向「当前正在跑的这个 Python 解释器」，`-m ruff` 保证用的是同一环境下安装的 ruff，避免落到 PATH 里别的解释器/版本的 ruff，保证 CI 与本地一致。

### 4.3 pyproject.toml 的 ruff 配置精读

#### 4.3.1 概念说明

ruff 的规则是一个庞大的集合（pyflakes、pycodestyle、pylint、isort、docstring 等几十个规则族）。hdl-modules 的策略很激进也很务实：

- **`select = ["ALL"]`**：默认开启全部规则，能用上的全用上，追求最高一致性。
- **`ignore = [...]`**：把「确实不适合本项目」的少数规则显式关掉，每条都写明理由。
- **`per-file-ignores`**：对工具脚本、测试代码、模块脚本这三类「本就该宽松」的代码，按路径豁免特定规则。

这是一种「默认严格、显式放行」的治理思路——比起「默认宽松、想起一条加一条」，它能更早暴露问题。

#### 4.3.2 核心流程

ruff 在被 `test_python_lint.py` 调用时，会自动从仓库根的 `pyproject.toml` 读取 `[tool.ruff]` 配置。匹配流程：

```
读 pyproject.toml
  → line-length=100 决定行宽基线
  → lint.select=["ALL","D213"] 决定启用规则全集 + 一条额外规则
  → lint.ignore=[...] 关掉一批不适用规则
  → per-file-ignores 按文件 glob 追加豁免
  → lint.isort 自定义 import 分区与顺序
  → lint.pylint.max-args 放宽参数个数
对每个 .py 文件套用 → 输出违规或通过
```

#### 4.3.3 源码精读

行宽与被排除的文件（自动生成的 `conf.py` 命名怪异，排除）：

[pyproject.toml:1-10](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L1-L10) —— `line-length = 100`，并排除 `doc/sphinx/conf.py`。

「全部启用 + 一条额外」：

[pyproject.toml:12-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L12-L19) —— `select = ["ALL", "D213"]`，D213 是「docstring 摘要放第二行」的项目约定。

一长串 `ignore`，每条都附注释说明为什么关，几个有代表性的：

[pyproject.toml:21-65](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L21-L65) —— 全量忽略清单。

```toml
ignore = [
    "COM812",   # 与 formatter 冲突，formatter 在用就不该再 lint
    "TRY003",   # 在异常里直接写字符串其实很合理
    "D200",     # 打破项目「docstring 分隔符独占一行」的约定
    "D100",     # 不强制每个 public 函数都有 docstring
    "T201",     # 允许 print
    "S101",     # 测试代码里的 assert 不该被当成安全问题
    ...
]
```

三套 `per-file-ignores`，体现「按角色差异化治理」：

[pyproject.toml:67-93](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L67-L93) —— 工具脚本、测试、模块脚本各自的豁免清单。

| 路径 glob | 豁免规则 | 理由（来自注释） |
| --- | --- | --- |
| `tools/**/*.py` | `E402`, `PT015`, `S101` | 工具脚本要先 import `tools_pythonpath` 再 import 第三方包（位置就是靠后，E402 不适用）；从不带 `-O` 跑。 |
| `**/test_*.py` | `ANN`, `PT011`, `PLR0913`, `S101`, `ERA001` | 测试代码非公开、可以宽松异常捕获、允许注释掉的调试代码。 |
| `modules/**/module_*.py` | `INP001` | 这些不是真正的 Python 包，不想塞 `__init__.py` 破坏模块目录结构。 |

isort 自定义了一个 `tools_pythonpath` 分区，让 `import tools.tools_pythonpath` 排在标准库与第三方之间（承接 u1-l3 讲过的「它要先于外部 import 执行」）：

[pyproject.toml:96-109](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L96-L109) —— 自定义 isort 分区与顺序。

最后放宽 pylint 的参数个数上限：

[pyproject.toml:112-116](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/pyproject.toml#L112-L116) —— `max-args = 6`（默认 5）。

#### 4.3.4 代码实践

**目标**：在本地跑一遍 ruff，对照 `pyproject.toml` 解释它输出的（或没输出的）每类规则。

**步骤**：

1. 安装 ruff：`python3 -m pip install ruff`。
2. 在仓库根目录运行检查：
   ```bash
   python3 -m ruff check
   ```
3. 运行格式检查：
   ```bash
   python3 -m ruff format --check --diff
   ```
4. 对照 `pyproject.toml` 的 `ignore` 与 `per-file-ignores`，挑一条你感兴趣的规则（如 `T201` 允许 print、`S101` 允许 assert），尝试用一个最小 `probe.py` 触发它，再验证本项目确实豁免了它。

**需要观察的现象**：在一个干净检出的仓库上，第 2、3 步应当无任何输出（退出码 0），表示全仓库已达标；若有输出，则是你本地工作区有未格式化的改动。

**预期结果**：你能说清「为什么本项目允许 `print` 而某些项目不允许」——因为 `T201` 在 `ignore` 里。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tools/**/*.py` 要单独豁免 `E402`（import 位置）？

> **答案**：每个工具脚本必须在最顶部先 `sys.path.insert` 再 `import tools.tools_pythonpath`，之后才能 import 第三方包。这必然导致「标准库 import 之后、第三方 import 之前」插入了非 import 语句，触发 E402。这是项目引导机制（u1-l3）的硬性要求，所以按路径豁免。

**练习 2**：`select` 里同时写了 `"ALL"` 和 `"D213"`，`"D213"` 是否多余？

> **答案**：不多余。`ALL` 是规则族的集合，但 ruff 里同一族内有些规则互斥（例如 `D212`「摘要放第一行」与 `D213`「摘要放第二行」是对立的）。项目把 `D212` 放进 `ignore`、把 `D213` 显式 `select`，正是为了在互斥规则里明确选边——确立「多行 docstring 摘要写在第二行」的项目约定。

### 4.4 contributing 指南与贡献规范

#### 4.4.1 概念说明

`doc/sphinx/contributing.rst` 是给贡献者的「操作手册」，回答两件事：日常怎么维护变更日志，以及怎么把一次开发收尾成一个正式发布。它的核心思想是**把容易出错的人工步骤变成有据可查的清单**——尤其是发版时推 tag 那一步，指南里有明确的 WARNING。

#### 4.4.2 核心流程

贡献者日常 → 发版的衔接流程：

```
日常开发：每改一处行为，就更新 doc/release_notes/unreleased.rst（持续维护，非发版时才写）
      ↓
准备发版：先在 main 上跑一次 CI 确保全绿
      ↓
审查 unreleased.rst：按 keep a changelog 补齐
      ↓
按语义化版本号决定新版本 X.Y.Z
      ↓
python3 tools/tag_release.py X.Y.Z   # 脚本自动改版本、搬日志、提交、打 tag
      ↓
git push origin vX.Y.Z               # 只推这一个 tag（警告：勿用 --tags）
git push origin HEAD:main            # 手动 fast-forward 推 main
```

#### 4.4.3 源码精读

变更日志的维护约定——强调「持续更新、而非发版时才补」：

[doc/sphinx/contributing.rst:7-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/contributing.rst#L7-L16) —— 按 keep a changelog 格式，`unreleased.rst` 应持续更新。

发版的完整步骤清单（CI 预跑 → 审日志 → 定版本号 → 跑脚本 → 推 tag）：

[doc/sphinx/contributing.rst:19-91](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/contributing.rst#L19-L91) —— 五步发版流程。

其中最关键的两段是「跑脚本」与「推 tag」。脚本调用一行命令：

[doc/sphinx/contributing.rst:50-60](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/contributing.rst#L50-L60) —— `python3 tools/tag_release.py X.Y.Z`，正是 4.1 节精读的脚本。

推 tag 那段带有醒目 WARNING，解释了「为什么必须命令行手动推 main」这一反直觉的规定：

[doc/sphinx/contributing.rst:63-91](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/doc/sphinx/contributing.rst#L63-L91) —— 警告勿用 `git push --tags`；并说明 GitHub PR 的「rebase and merge」会改 commit SHA，导致刚推的 tag 对不上 main，所以必须命令行 fast-forward。

这一段把 4.1 节脚本里「打 tag」之后的人工动作讲透了：脚本只负责本地提交与打 tag，推送到远程、尤其是把 main 推成 fast-forward，仍需有完整权限的贡献者在命令行小心操作。

#### 4.4.4 代码实践

**目标**：以贡献者身份，为「一段假设的新代码」走完从写版权头到过 lint 的最小闭环。

**步骤**：

1. 复制 `tools/tag_release.py` 第 1–8 行的版权头模板（`#` 注释版）。
2. 新建一个 `probe_contrib.py`，粘上版权头后写一个最小函数，例如：
   ```python
   # --------------------------------------------------------------------------------------------------
   # Copyright (c) Lukas Vik. All rights reserved.
   #
   # ... (粘贴完整 4 行 COPYRIGHT_TEXT)
   # --------------------------------------------------------------------------------------------------

   def add(a: int, b: int) -> int:
       """Return the sum."""
       return a + b
   ```
3. 运行 ruff 确保这段新代码符合本项目风格：
   ```bash
   python3 -m ruff check probe_contrib.py
   python3 -m ruff format --check probe_contrib.py
   ```
4. 若 ruff 报错（例如 docstring 风格不符 D213、行太长），按提示修到全绿。
5. 清理：`rm probe_contrib.py`（它未被 git 跟踪，不影响仓库）。

**需要观察的现象**：补齐版权头 + 符合 ruff 规则后，两条 ruff 命令均无输出（退出码 0）。

**预期结果**：你完成了一次「贡献者提交前的本地自检」闭环——版权头齐、Python lint 过。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：贡献指南为什么强调 `unreleased.rst` 要「持续更新」而不是「发版时一次性补」？

> **答案**：发版时一次性补，很容易遗漏期间做过的改动，且记忆模糊导致描述不准。持续维护意味着每次合并一个改动就顺手记一笔，保证日志与代码改动一一对应、描述准确。`tag_release.py` 还会校验该文件非空，持续更新也避免了发版时才发现日志为空的窘境。

**练习 2**：指南警告「勿用 `git push --tags`」，原因是什么？

> **答案**：`--tags` 会把本地所有 tag 一次性全推上去，可能把误建的、旧的、废弃的 tag 也推到远程，污染发布历史。正确做法是只推当前这一条：`git push origin vX.Y.Z`。

## 5. 综合实践

把本讲四个模块串起来，模拟一次「最小贡献 → 自检 → 理解发版」的全流程。**全部在本地只读/临时文件上进行，不要改真实仓库、不要真发版。**

1. **写一段合规代码**：新建临时文件 `probe_all.py`，按 4.4.4 的模板补齐版权头，写一个带类型注解与 docstring 的函数，docstring 摘要放在第二行（遵守 `D213`）。
2. **过 Python lint**：`python3 -m ruff check probe_all.py` 与 `python3 -m ruff format --check probe_all.py`，修到全绿。
3. **过文件格式 lint**：确认文件是 UTF-8/ASCII、行尾 `\n`、无 Tab、无尾随空格、无行超 100。
4. **过版权头 lint**：用 4.2.4 的 `CopyrightHeader` 手动检查一次，确认返回 `OK`。
5. **对照发版脚本**：阅读 `tools/tag_release.py` 的 `main()`，在一张纸上写下「如果我此刻要发版 `X.Y.Z`，脚本会按什么顺序改哪些文件、打什么 tag」。再阅读 `contributing.rst` 的发版小节，补上「脚本之后，人工还要做哪两步推送」。
6. 清理所有 `probe_*.py` 临时文件。

**验收标准**：你能不查资料地讲清——(a) 一段新代码要同时满足版权头、文件格式、ruff 三道闸门；(b) 一次发版由脚本完成本地改动与打 tag，由人工完成受控推送；(c) 为什么推 main 必须命令行 fast-forward。

## 6. 本讲小结

- `tools/tag_release.py` 把发版编成五步幂等脚本：校验 → 改版本号 → 搬运发布说明 → 提交打 tag → 回到 `-dev`，并用 `verify_new_version_number` 做了四重防呆校验。
- 版本号唯一信息源是 `hdl_modules/__init__.py` 的 `__version__`，比较用 `packaging.version.parse` 而非字符串比较。
- 三道 lint 闸门都在 `test/lint/` 下，共享 tsfpga 的 `find_git_files` 扫描「已 git 跟踪文件」：版权头检五种后缀、文件格式检六项文本卫生、Python lint 调两次 ruff。
- `pyproject.toml` 的 ruff 策略是「`select = ["ALL"]` 默认最严 + `ignore` 与 `per-file-ignores` 显式放行」，每条豁免都附理由；isort 自定义 `tools_pythonpath` 分区以承接 u1-l3 的引导机制。
- CI 把 lint 随 `python3 -m pytest --verbose` 一起跑，lint 不过即 CI 变红、PR 不能合并。
- `contributing.rst` 规定了变更日志持续维护、发版五步走，并对「推 tag / 推 main」给出了带 WARNING 的人工操作清单。

## 7. 下一步学习建议

- 本讲是第 9 单元也是全手册的最后一篇。如果是从头读到这里，你已经掌握了从「项目导览 → 握手/基础包 → 跨时钟域 → FIFO → AXI 总线 → 寄存器/流处理 → 专用 IP → 验证方法论 → 工具与贡献」的完整链条。
- 想把本讲的「质量门禁」用起来：建议在你的下一个 FPGA 工程里复刻这套 `test/lint/` 三件套（hdl-modules 与 tsfpga 都是 BSD 许可，可直接借鉴），把版权头与文件格式纳入 CI。
- 想深入发版自动化：阅读 tsfpga 的 `VersionNumberHandler`、`commit_and_tag_release`、`make_commit` 实现（本讲只用了它们的接口），理解「改版本号 + 打 tag」在库层面如何被原子化。
- 想理解 ruff 的全貌：对照 [ruff 官方规则表](https://docs.astral.sh/ruff/rules/) 逐条核对 `pyproject.toml` 的 `ignore` 清单，思考「每条规则为什么在本项目被关掉」，这是把别人的配置内化为自己工程规范的最佳练习。
