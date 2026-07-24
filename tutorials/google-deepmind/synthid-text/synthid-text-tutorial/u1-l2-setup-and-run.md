# 运行与环境：安装、Notebook 与测试

## 1. 本讲目标

学完本讲，你应当能够：

1. 在一台干净的机器上，从零创建虚拟环境并从源码安装 SynthID Text。
2. 区分 `pip install` 的三种写法：仅安装核心库、`[notebook-local]`、`[test]`，并知道每种场景该用哪一个。
3. 本地启动 Jupyter Notebook 并打开官方示例 `.ipynb` 跑通端到端流程。
4. 用 `pytest` 执行测试套件，并理解 GitHub CI 是如何自动验证同一套测试的。
5. 理解为什么同一个仓库的依赖里会**同时出现 PyTorch 和 JAX/Flax**——这对应了上一讲提到的「施加用 PyTorch、检测用 JAX」的分工。

本讲**只讲"怎么跑起来"**，不深入任何算法源码；源码精读从 u2 开始。但在动手跑之前先弄懂依赖结构，能帮你避开大量"装好了却 import 报错"的坑。

## 2. 前置知识

### 2.1 虚拟环境（virtual environment）

Python 的第三方库会被装进一个全局/共享的位置。不同项目往往需要**不同版本**的同一个库（比如项目 A 要 `numpy==1.26`，项目 B 要 `numpy==2.0`），直接装到全局会互相覆盖。虚拟环境就是给每个项目隔离出一个独立的"库安装目录"，互不干扰。本仓库的依赖都是**锁死版本**的（下文会讲原因），所以**强烈建议**始终用虚拟环境。

### 2.2 pip 的可选依赖（extras）

`pyproject.toml` 里可以声明几组"可选依赖"。安装时用方括号语法选择：

```shell
pip install '.[test]'          # 核心依赖 + 名为 test 的一组额外依赖
pip install '.[notebook-local]'# 核心依赖 + 跑 Notebook 所需的额外依赖
pip install '.'                # 只装核心依赖
```

这里的 `.` 表示"当前目录（即项目根目录）从源码安装"。

### 2.3 承接上一讲

[u1-l1](u1-l1-project-overview.md) 已经讲清：这个仓库是 SynthID Text 的**参考实现**，分两个阶段——水印施加（PyTorch）和水印检测（JAX/Flax）。本讲你会发现：这两个框架**同时出现在依赖列表里**，正是因为这个分工。

## 3. 本讲源码地图

本讲围绕三个"工程门面文件"，它们决定了项目怎么装、怎么跑、怎么测：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `README.md` | 项目说明书 | "Local notebook use" 与 "Running the tests" 两段可复制的 shell 命令 |
| `pyproject.toml` | Python 打包与依赖声明 | 核心依赖（含被锁死的版本）、`notebook` / `notebook-local` / `test` 三个可选组 |
| `.github/workflows/ci.yaml` | GitHub Actions 持续集成配置 | CI 如何自动安装 + 跑测试，可把它当作"官方推荐的安装与测试命令" |

另外你会接触到两类运行产物：

- 测试文件：`src/synthid_text/logits_processing_test.py`、`src/synthid_text/synthid_mixin_test.py`。
- 示例 Notebook：`notebooks/synthid_text_huggingface_integration.ipynb`（官方端到端示例）与 `notebooks/testing_huggingface_integration.ipynb`。

## 4. 核心概念与源码讲解

### 4.1 虚拟环境与安装命令

#### 4.1.1 概念说明

"安装 SynthID Text"其实包含两件事：先把**核心库**（`synthid_text` 这个 Python 包及其依赖）装好，再按需追加**额外工具**（跑 Notebook 需要 Jupyter，跑测试需要 pytest）。`pyproject.toml` 把这两件事拆开声明：核心依赖写在 `[project].dependencies` 里，额外工具写在 `[project.optional-dependencies]` 下的几个组里。

理解这一点后，你就明白为什么 README 里会出现 `pip install '.[notebook-local]'` 和 `pip install '.[test]'` 两种不同写法——它们追加的是不同的"可选依赖组"，但**都包含完整的核心库**。

#### 4.1.2 核心流程

以"跑测试"为目标的标准安装流程：

```text
创建虚拟环境  →  激活虚拟环境  →  git clone  →  进入目录
   →  pip install '.[test]'  →  pytest
```

其中：

1. `python3 -m venv <路径>`：在某处创建一个虚拟环境目录。
2. `source <路径>/bin/activate`：激活，之后所有 `pip install` 都装进这个隔离目录。
3. `git clone` + `cd`：拿到源码并进入根目录（`pyproject.toml` 所在目录）。
4. `pip install '.[test]'`：从源码安装核心库 + 测试依赖。
5. `pytest`：执行测试。

#### 4.1.3 源码精读

**核心依赖列表**——这是装了 SynthID Text 就一定会被拉下来的库：

[pyproject.toml:L15-L26](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L15-L26) 声明了核心依赖，里面同时包含 PyTorch（`torch==2.4.0`）和 JAX/Flax（`jax[cuda]`、`flax`、`optax`）两大体系，正对应"施加用 PyTorch、检测用 JAX"的分工。注意多个库被锁死在具体版本上：

```toml
dependencies = [
  "flax",
  "immutabledict==4.2.0",
  "jax[cuda]",
  "jaxtyping",
  "numpy==1.26.0",
  "optax",
  "scikit-learn",
  "torch==2.4.0",
  "tqdm",
  "transformers==4.43.3",
]
```

为什么锁版本？因为这是一份**研究参考实现**，作者希望你复现出的行为和他们一致。`numpy`、`torch`、`transformers` 这些库的大版本升级常常会改变数值行为或 API，锁死版本可以最大限度保证"g 值、得分"等结果可复现。

**Python 版本要求**：

[pyproject.toml:L14](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L14) 写明 `requires-python = ">=3.9"`，即至少 Python 3.9。注意：`classifiers` 里只标了 3.10、3.11（见 [pyproject.toml:L34-L36](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L34-L36)），但 CI 实际会测 3.9/3.10/3.11 三个版本（下文 4.3 节会看到）。

**README 的安装命令（本地 Notebook 用）**：

[README.md:L56-L68](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L56-L68) 给出了本地跑 Notebook 的完整 shell 流程，注意它用的是 `.[notebook-local]`：

```shell
python3 -m venv ~/.venvs/synthid
source ~/.venvs/synthid/bin/activate
git clone https://github.com/google-deepmind/synthid-text.git
cd synthid-text
pip install '.[notebook-local]'
python -m notebook
```

> 小贴士：`~/.venvs/synthid` 只是一个约定俗成的路径，你可以放在任何地方；关键是 `source` 的路径要和创建时一致。

#### 4.1.4 代码实践

1. **实践目标**：验证"核心库 + 三种可选组"分别会装进哪些包，理解 extras 的差别。
2. **操作步骤**：
   - 创建并激活一个新虚拟环境。
   - 依次执行：`pip install '.'`（只装核心），记录已安装包列表；再 `pip install '.[notebook-local]'`，观察新装了哪些包。
3. **需要观察的现象**：第二次安装是否多出了 `notebook`、`jupyter`、`pandas`、`tensorflow` 等；它们是否来自 `pyproject.toml` 的 `notebook` 组。
4. **预期结果**：`notebook-local` 组在 `notebook` 组基础上又追加了 `notebook`（Jupyter 服务端本身）。具体清单见 4.2.3。**待本地验证**（实际安装清单以你机器上的 `pip list` 为准）。
5. 命令本身不依赖 GPU，任何机器都能做这一步。

#### 4.1.5 小练习与答案

**练习 1**：如果不写方括号，只执行 `pip install '.'`，能不能跑测试？为什么？

> **答案**：能 import `synthid_text`，但**跑不了** `pytest`，因为 `pytest`、`absl-py`、`mock` 只在 `test` 可选组里（见 [pyproject.toml:L68-L72](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L68-L72)），核心依赖不包含它们。

**练习 2**：`numpy==1.26.0` 为什么要写死版本号？

> **答案**：参考实现强调**可复现性**。`numpy` 大版本升级可能改变随机数/数值行为，写死版本能让 g 值、得分等结果在所有人机器上一致。

### 4.2 Notebook 运行流程

#### 4.2.1 概念说明

SynthID Text 的"主入口"其实不是某个 `main.py`，而是一个 **Jupyter Notebook**（`notebooks/synthid_text_huggingface_integration.ipynb`）。它是一个自包含（self-contained）的端到端示例：加载带水印能力的模型 → 生成文本 → 重算 g 值与掩码 → 打分。README 把它定位为"既是教程，也是参考实现"。

要在本地跑它，除了核心库，你还需要 Jupyter 服务端、数据集工具、可视化组件等——这正是 `[notebook-local]` 这一组可选依赖要补齐的东西。

#### 4.2.2 核心流程

```text
安装 [notebook-local]  →  python -m notebook 启动服务  →
浏览器打开 .ipynb  →  选 kernel 逐格运行
```

注意硬件门槛（来自 README）：

- Gemma 2B IT：建议 16GB 显存（如 T4）。
- Gemma 7B IT：建议 32GB 显存（如 A100）。
- GPT-2：任何配置都能跑，High-RAM CPU 或任意 GPU 更快。

#### 4.2.3 源码精读

**Notebook 相关的可选依赖组**：

[pyproject.toml:L54-L66](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L54-L66) 定义了两个相关组：

```toml
notebook = [
  "datasets", "huggingface_hub", "ipywidgets", "pandas", "tensorflow",
]
notebook-local = [
  "synthid-text[notebook]",
  "notebook",
]
```

读法：`notebook-local` 先把 `notebook` 组整个拉进来（含 `datasets`、`tensorflow` 等），再追加 Jupyter 服务端（`notebook`）。`tensorflow` 只在 Notebook 场景才出现，核心库不需要它。

**README 对 Notebook 的说明**：

[README.md:L29-L36](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L29-L36) 给出了不同模型的硬件建议；[README.md:L66-L70](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L66-L70) 说明用 `python -m notebook` 启动服务，"kernel 跑起来后导航到 `.pynb` 文件执行"。另外 [pyproject.toml:L52](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L52) 把该 Notebook 登记为项目的 `Demo` 链接，可见它是官方主推的入门入口。

> 提醒：README 第 70 行原文写的是 `.pynb`（一个笔误），实际文件后缀是 `.ipynb`，目录下确有 `notebooks/synthid_text_huggingface_integration.ipynb`。

#### 4.2.4 代码实践

1. **实践目标**：把官方 Notebook 跑起来，至少完成"加载模型 + 生成一段文本"。
2. **操作步骤**：
   - 按 4.1.3 的命令安装 `.[notebook-local]` 并 `python -m notebook` 启动。
   - 浏览器打开 `notebooks/synthid_text_huggingface_integration.ipynb`，选当前虚拟环境的 kernel。
   - 从头逐格运行（GPT-2 对硬件要求最低，建议先用它）。
3. **需要观察的现象**：首次加载模型时是否要下载权重、是否需要 HuggingFace 登录令牌；生成步骤是否输出了带水印的文本。
4. **预期结果**：能跑通到生成阶段；Gemma 2B/7B 因显存不足可能 OOM，此时改用 GPT-2。**待本地验证**（取决于你的机器与网络）。
5. 若没有 GPU，强烈建议用 GPT-2 路径，避免因显存问题误判为"装错了"。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tensorflow` 放在 `notebook` 组而不是核心依赖里？

> **答案**：核心库的水印施加（PyTorch）和检测（JAX）都不需要 TensorFlow；`tensorflow` 只是 Notebook 里加载数据/示例时用到的辅助库，所以归入可选组，避免给只想要核心功能的用户强加一个沉重依赖。

**练习 2**：本地跑 Notebook 时，README 推荐用哪条 `pip install` 命令？它比 `pip install '.[notebook]'` 多装了什么？

> **答案**：推荐 `pip install '.[notebook-local]'`。它在 `notebook` 组基础上**额外追加了 `notebook`（Jupyter 服务端）**（见 [pyproject.toml:L63-L66](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L63-L66)），这样 `python -m notebook` 才能启动。

### 4.3 pytest 测试与 CI

#### 4.3.1 概念说明

仓库自带"一套小测试"用来验证库工作正常。这些测试用 `pytest` 组织，放在 `src/synthid_text/` 下、与被测模块同名加 `_test` 后缀（如 `logits_processing_test.py` 对应 `logits_processing.py`）。`pytest .` 会从当前目录递归发现所有 `*_test.py` 并运行其中以 `test_` 开头的函数。

GitHub Actions 的 CI 配置（`.github/workflows/ci.yaml`）做的事情和你在本地手动跑测试**几乎一样**：拉代码、装 `test` 依赖、跑 `pytest`。所以 CI 文件可以当成"官方权威的安装与测试命令"来读。

#### 4.3.2 核心流程

本地跑测试：

```text
创建/激活 venv  →  git clone + cd  →  pip install '.[test]'  →  pytest .
```

CI 跑测试（每个 Python 版本重复一遍）：

```text
checkout  →  setup-python  →  pip install -e '.[test]'  →  pytest -v
```

注意两处细微差别：

- README 用普通安装 `.[test]`，CI 用**可编辑安装** `-e '.[test]'`（`-e` 表示以"软链接"方式装，源码改动立即生效，适合开发）。
- README 用 `pytest .`（指定当前目录），CI 用 `pytest -v`（`-v` 打印每个测试用例名，更详细）。

#### 4.3.3 源码精读

**测试依赖组**：

[pyproject.toml:L68-L72](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L68-L72) 声明了跑测试需要的三个额外包：

```toml
test = [
  "absl-py",
  "mock",
  "pytest",
]
```

其中 `absl-py`（Abseil）和 `mock` 是测试辅助库，`pytest` 才是测试运行器。

**README 的测试命令**：

[README.md:L77-L89](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L77-L89) 给出了从零跑测试的完整 shell 流程，关键两行是 `pip install '.[test]'` 和 `pytest .`。

**CI 配置**：

[.github/workflows/ci.yaml:L16-L44](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/.github/workflows/ci.yaml#L16-L44) 是完整的 CI 定义。几个要点：

- 触发条件 [.github/workflows/ci.yaml:L18-L22](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/.github/workflows/ci.yaml#L18-L22)：在 `main`、`dev` 分支上的 push 和 pull_request 触发。
- 矩阵 [.github/workflows/ci.yaml:L27-L29](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/.github/workflows/ci.yaml#L27-L29)：Python `3.9`、`3.10`、`3.11` 三个版本各跑一遍（所以即便 `classifiers` 只列了 3.10/3.11，3.9 也是被 CI 覆盖支持的）。
- 安装步骤 [.github/workflows/ci.yaml:L41-L42](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/.github/workflows/ci.yaml#L41-L42)：`python -m pip install -e '.[test]'`（可编辑安装）。
- 测试步骤 [.github/workflows/ci.yaml:L43-L44](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/.github/workflows/ci.yaml#L43-L44)：`pytest -v`。

**被测的测试文件**：仓库里实际存在两个测试模块——`src/synthid_text/logits_processing_test.py`（测水印施加内核）和 `src/synthid_text/synthid_mixin_test.py`（测 HuggingFace 集成）。它们的具体断言会在 [u7-l2](u7-l2-test-suite.md) 详解，本讲只需知道"pytest 会自动发现并运行它们"。

#### 4.3.4 代码实践

1. **实践目标**：本地复现 CI 的测试流程，确认库在你机器上工作正常。
2. **操作步骤**：
   ```shell
   python3 -m venv ~/.venvs/synthid
   source ~/.venvs/synthid/bin/activate
   git clone https://github.com/google-deepmind/synthid-text.git
   cd synthid-text
   pip install '.[test]'
   pytest -v
   ```
3. **需要观察的现象**：终端打印的**测试用例总数**（`pytest -v` 会逐条列出 `test_...` 函数，结尾有 `passed`/`failed` 汇总）；是否有用例失败、失败原因是什么。
4. **预期结果**：全部用例通过（`... passed in X.XXs`）；用例数量与失败情况**待本地验证**——请把"用例总数 + 是否全过"记下来。如果出现失败，优先排查 Python 版本（建议用 3.9–3.11）与依赖版本是否被正确锁定。
5. 若想更贴近 CI，可改用 `pip install -e '.[test]'`（可编辑安装）。

#### 4.3.5 小练习与答案

**练习 1**：`pip install '.[test]'`（README 写法）和 `pip install -e '.[test]'`（CI 写法）有什么区别？

> **答案**：`-e` 是 editable（可编辑）安装：它把包以指向源码目录的方式装上，你修改 `src/synthid_text/` 下的源码后**无需重装**即可被测试看到。README 的非 `-e` 写法会把代码复制进 site-packages，改源码后需要重装才生效。

**练习 2**：CI 在哪几个 Python 版本上跑测试？这和 `pyproject.toml` 的声明一致吗？

> **答案**：CI 跑 `3.9`、`3.10`、`3.11`（[.github/workflows/ci.yaml:L27-L29](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/.github/workflows/ci.yaml#L27-L29)）。`requires-python` 只要求 `>=3.9`（[pyproject.toml:L14](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L14)），`classifiers` 只列了 3.10/3.11，但 CI 实际也覆盖了 3.9——所以三者略有出入，以 CI 为"实际被验证"的版本。

## 5. 综合实践

把本讲三块内容串起来，完成一次"模拟 CI"的本地验证：

1. 在一台干净机器上创建虚拟环境并激活。
2. 克隆仓库并进入目录。
3. 执行 `pip install -e '.[test]'`（和 CI 完全一致的安装方式）。
4. 执行 `pytest -v`，记录：
   - 测试用例总数（看结尾汇总）。
   - 是否全部通过。
   - 跑了多久。
5. 再执行 `pip install '.[notebook-local]'`，启动 `python -m notebook`，确认能打开 `notebooks/synthid_text_huggingface_integration.ipynb`（不必跑完，能打开即说明环境就绪）。

**验收标准**：`pytest -v` 全绿、Notebook 能在浏览器打开。把"用例数 + 通过情况"作为你本讲的学习记录。**完整运行结果待本地验证。**

## 6. 本讲小结

- SynthID Text 的安装核心在 `pyproject.toml`：核心依赖同时含 **PyTorch（施加）** 与 **JAX/Flax/optax（检测）**，且关键库被锁死版本以保证可复现。
- 三种安装写法：`pip install '.'`（只核心）、`.[notebook-local]`（加 Jupyter/数据集工具）、`.[test]`（加 pytest/absl/mock）。
- 官方"主入口"是一个 Notebook（`notebooks/synthid_text_huggingface_integration.ipynb`），硬件门槛按模型区分：Gemma 2B 需 16GB、7B 需 32GB，GPT-2 任意配置。
- 跑测试只需 `pip install '.[test]'` + `pytest`；测试文件是 `logits_processing_test.py` 与 `synthid_mixin_test.py`。
- GitHub CI（`ci.yaml`）在 Python 3.9/3.10/3.11 上用 `pip install -e '.[test]'` + `pytest -v` 自动验证，可当成"权威安装/测试命令"。
- README 与 CI 有两处小差异：README 用 `.[test]` + `pytest .`，CI 用 `-e '.[test]'` + `pytest -v`。

## 7. 下一步学习建议

环境就绪后，下一步建议：

1. 先读 [u1-l3 目录结构与源码地图](u1-l3-repo-structure.md)，把 `src/synthid_text/` 下每个文件的职责记清楚，建立"看源码时的导航"。
2. 再读 [u1-l4 端到端流程总览](u1-l4-end-to-end-pipeline.md)，对照 Notebook 走一遍"施加→生成→检测"的数据流，建立全局直觉。
3. 之后再进入 u2，开始啃核心概念（水印配置、哈希函数、g 值）。

一句话：本讲只解决"怎么装、怎么跑、怎么测"，算法源码留给后续讲义。
