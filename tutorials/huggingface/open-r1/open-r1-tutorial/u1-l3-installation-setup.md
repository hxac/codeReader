# 安装与环境搭建

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `uv` 从零创建虚拟环境，并按正确顺序安装 `vLLM`、`FlashAttention` 和项目本体。
- 看懂 `setup.py` 里 `extras_require` 的分组（`dev` / `code` / `eval` / `tests` / `quality`），知道不同使用场景该装哪一组。
- 理解 `Makefile` 中 `install` / `style` / `quality` / `test` 等目标的作用，能用一条 `make` 命令完成常见开发动作。

承接上一讲（u1-l1）的全局认知：open-r1 「简单至上」，核心只有 `sft.py`、`grpo.py`、`generate.py` 三个薄脚本，底层能力（训练、生成、评估）全部委托给 TRL / transformers / vLLM / lighteval。本讲要解决的，就是「把这些底层依赖正确地装到一个干净环境里」——这是后续跑通任何一条主链的前提。

## 2. 前置知识

在动手之前，先理解三个概念，它们解释了 open-r1 安装步骤「为什么是这个顺序」。

**虚拟环境（virtual environment）**
Python 的依赖版本很容易互相打架（比如 A 项目要 PyTorch 2.6，B 项目要 2.4）。虚拟环境为每个项目建一个独立的「依赖盒子」，盒子之间互不影响。本讲用 `uv`（一个用 Rust 写的、极快的 Python 包管理器）来创建和管理这个盒子。

**`uv` 与 `pip` 的关系**
`uv` 既能创建虚拟环境（`uv venv`），也能像 `pip` 一样安装包（`uv pip install`）。open-r1 的 `Makefile` 就是把这两件事串起来：先用 `uv venv` 建环境，激活后用 `uv pip install` 装依赖。

**`extras`（可选依赖分组）**
一个 Python 包可以声明「核心依赖」（谁装都得有）和「可选依赖分组」（按需安装）。例如 open-r1 的核心依赖里有训练用的 `trl`、`transformers`，但评估专用的 `lighteval`、代码沙箱专用的 `e2b-code-interpreter` 就放在 `extras` 分组里，只有做对应任务时才装。安装时用 `pip install -e ".[dev]"` 这种语法选中分组。

## 3. 本讲源码地图

本讲只涉及三个文件，它们共同回答「怎么装、装什么、装完怎么用」：

| 文件 | 作用 |
|------|------|
| [setup.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py) | 打包脚本：声明包名、版本、Python 版本要求，以及核心依赖 `install_requires` 和可选分组 `extras`。 |
| [Makefile](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile) | 把常用命令封装成 `make install` / `make style` / `make test` 等快捷目标，是日常开发的入口。 |
| [README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md) | 项目的「使用说明书」，`Installation` 一节给出了和 `make install` 等价的手工命令，适合理解每一步在做什么。 |

理解它们的关系：`README` 里手写了一套安装命令，`Makefile` 的 `install` 目标把同一套命令封装成 `make install` 一键执行，而这两套命令最终都依赖 `setup.py` 里声明的依赖清单。

## 4. 核心概念与源码讲解

### 4.1 用 uv 创建虚拟环境与依赖安装顺序

#### 4.1.1 概念说明

open-r1 的安装有一个**关键约束**：vLLM 的二进制包是针对 **PyTorch 2.6.0** 编译的，如果环境里的 PyTorch 版本不对，加载 vLLM 时会出现段错误（segmentation fault）。因此安装顺序必须是：

1. 先建空环境；
2. 先装 vLLM（它会顺带把 PyTorch 2.6.0 拉进来）；
3. 再装 FlashAttention，并且**禁用构建隔离**（`--no-build-isolation`），让它直接复用上一步已经装好的 PyTorch；
4. 最后装项目本体加可选分组。

如果顺序反了——比如先装了一个别的 PyTorch，再装 vLLM——版本不匹配就会出问题。

#### 4.1.2 核心流程

README 的 `Installation` 一节把这套流程写得很清楚，伪代码如下：

```
1. uv venv openr1 --python 3.11          # 建虚拟环境 openr1，Python 3.11
2. source openr1/bin/activate            # 激活环境
3. uv pip install --upgrade pip          # 升级 pip
4. uv pip install vllm==0.8.5.post1      # 装 vLLM，顺带带入 PyTorch 2.6.0
5. uv pip install setuptools             # flash-attn 编译需要
6. uv pip install flash-attn --no-build-isolation   # 复用已有 PyTorch
7. GIT_LFS_SKIP_SMUDGE=1 uv pip install -e ".[dev]" # 装项目本体 + dev 分组
8. huggingface-cli login / wandb login   # 登录 HF 与 W&B
9. git-lfs --version                     # 确认 Git LFS 已装
```

第 7 步里的 `GIT_LFS_SKIP_SMUDGE=1` 是个小技巧：它告诉 Git **在安装时不要自动拉取大文件**（比如仓库里通过 LFS 存的模型权重），避免安装阶段就把磁盘塞满。

README 还贴心地提示：如果使用 Hugging Face 集群，可以把 `export UV_LINK_MODE=copy` 写进 `.bashrc`，以消除 `uv` 的缓存警告。

#### 4.1.3 源码精读

先看 README 里关于 CUDA 版本的警告，它解释了为什么安装前要先查环境：

[README.md:51-52](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L51-L52) —— 提示项目依赖 **CUDA 12.4**，遇到段错误要先用 `nvcc --version` 检查系统 CUDA 版本。

[README.md:62-64](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L62-L64) —— 创建虚拟环境的「起始指令」，与 `Makefile` 里的写法一致：`uv venv openr1 --python 3.11`，再激活、升级 pip。

[README.md:71-74](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L71-L74) —— 安装 vLLM 与 FlashAttention 的两行命令。注意 `flash-attn` 用了 `--no-build-isolation`。

[README.md:76-80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L76-L80) —— 说明 vLLM 会带入 PyTorch `v2.6.0`，并强调**必须使用这个版本**；随后给出安装剩余依赖的命令 `GIT_LFS_SKIP_SMUDGE=1 uv pip install -e ".[dev]"`。

再看 `Makefile` 的 `install` 目标，它把上面这套流程封装成了一条命令：

[Makefile:10-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L10-L16) —— `install` 目标用 `&&` 把「建环境→激活→升 pip→装 vLLM→装 setuptools→装 flash-attn→装项目本体 `[dev]`」串成一条原子链，任何一步失败都会中止。这与 README 的手工命令**一一对应**，只是省去了人工敲。

#### 4.1.4 代码实践

**实践目标**：在本地用 `uv` 创建虚拟环境并安装 open-r1 的开发依赖。

**操作步骤**（在有 GPU、CUDA 12.4 的机器上）：

1. 在仓库根目录执行 `make install`（等价于上面伪代码的 1–7 步）。
2. 激活环境：`source openr1/bin/activate`。
3. 确认安装成功：`pip show open-r1`。
4. 确认 PyTorch 版本与 vLLM 匹配：`python -c "import torch, vllm; print(torch.__version__)"`。

**无 GPU / 无 CUDA 的降级方案**：vLLM 和 FlashAttention 都需要 GPU，纯 CPU 环境装不上。此时可跳过它们，只装最小开发依赖：

```shell
uv venv openr1 --python 3.11
source openr1/bin/activate
uv pip install --upgrade pip
GIT_LFS_SKIP_SMUDGE=1 uv pip install -e ".[dev]"
```

注意：这样做环境里**没有 PyTorch**（`torch` 在 `Makefile` 里是单独通过 vLLM 引入的，不在任何 `extras` 分组中），所以无法真正跑训练，但可以验证 `open-r1` 这个包能否被正确安装、能否被 `import`。

**需要观察的现象**：

- `pip show open-r1` 应打印出 `Name: open-r1`、`Version: 0.1.0.dev0`、`Location: .../site-packages`。
- 完整安装时，`torch.__version__` 应为 `2.6.0`。

**预期结果**：`pip show open-r1` 能正常输出包信息，说明项目本体已成功以可编辑模式（`-e`）装好。

> 待本地验证：具体输出取决于你的机器是否有 GPU；若按降级方案安装，`import torch` 会失败，这是预期行为，不是错误。

#### 4.1.5 小练习与答案

**练习 1**：为什么 FlashAttention 要用 `--no-build-isolation` 安装，而不是先装它再装 vLLM？

**参考答案**：FlashAttention 安装时要从源码编译，编译过程需要链接到一个已安装的 PyTorch。如果开启构建隔离（默认），它会在一个隔离环境里临时拉一个 PyTorch 来编译，可能与目标版本不一致。`--no-build-isolation` 让它直接使用当前环境里由 vLLM 预先带入的 PyTorch 2.6.0，保证版本匹配。因此顺序必须是「先 vLLM（带入正确 PyTorch）→ 后 flash-attn（复用该 PyTorch）」。

**练习 2**：`GIT_LFS_SKIP_SMUDGE=1` 加在 `uv pip install -e ".[dev]"` 前面，目的是什么？

**参考答案**：在可编辑安装时跳过 Git LFS 自动拉取大文件，避免安装阶段就下载仓库里通过 LFS 存储的大体积资产（如权重），节省时间和磁盘。

---

### 4.2 setup.py 的 extras 分组与 install_requires

#### 4.2.1 概念说明

`setup.py` 是 Python 的打包脚本，它用 `setuptools.setup(...)` 向 pip 描述「这个包叫什么、要什么 Python 版本、依赖哪些库」。open-r1 的 `setup.py` 做了三件值得学习的事：

1. **集中声明依赖**：所有依赖（含版本号）先写在一个 `_deps` 列表里。
2. **名字 → 完整规格的反查表**：用一个正则把每条依赖拆成「短名字」和「带版本的完整规格」，建一张 `deps` 字典，方便后续按短名字引用。
3. **按用途分组**：用 `extras` 字典把依赖分成 `tests` / `torch` / `quality` / `code` / `eval` / `dev` 几组，调用方按需安装。

#### 4.2.2 核心流程

依赖处理的数据流是：

```
_deps (字符串列表, 每条带版本)
   │  正则解析 → {短名字: 完整规格}
   ▼
deps (反查字典)
   │  deps_list("a","b") 按短名字取出完整规格
   ▼
extras["tests"]/["code"]/... (各分组)
   │  传给 setup(extras_require=extras)
   ▼
pip install -e ".[dev]" 选中并安装
```

而**始终安装**的核心依赖写在 `install_requires`，与「按需的 `extras`」相对。

#### 4.2.3 源码精读

先看依赖的「单一真相源」`_deps` 列表：

[setup.py:43-77](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L43-L77) —— 所有依赖都在这里声明。注释说明：变动快的依赖（如 `trl`）要钉死精确版本。可以看到 `trl[vllm]==0.18.0`、`transformers==4.52.3`、`torch==2.6.0` 等都被精确锁定。

接着是巧妙的反查字典构建：

[setup.py:85](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L85) —— 用 `re.findall` 配合两个捕获组，把每条字符串解析成 `(完整规格, 短名字)`，再建成 `{短名字: 完整规格}` 字典。例如 `"distilabel[vllm,ray,openai]>=1.5.2"` 会被解析成短名字 `distilabel`，完整规格仍是整串。

[setup.py:88-89](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L88-L89) —— `deps_list(*pkgs)` 是个小工具：传入若干短名字，返回它们对应的完整规格列表，供 `extras` 复用，避免重复写版本号。

然后是核心：`extras` 分组定义：

[setup.py:92-98](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L92-L98) —— 各分组的成员。要点是第 98 行：

```python
extras["dev"] = extras["quality"] + extras["tests"] + extras["eval"] + extras["code"]
```

也就是说 `dev` 是**其余四个分组的并集**，装 `[dev]` 就等于一次性装齐「代码质量工具 + 测试 + 评估 + 代码沙箱」所需依赖。注意 `dev` **不包含** `torch` 和 `vllm`/`flash-attn`——这三者由 `Makefile` 单独安装，因为它们依赖 GPU 与 CUDA，不能放进普通 `extras`。

各分组用途一览：

| 分组 | 包含（节选） | 用途 |
|------|-------------|------|
| `quality` | `ruff`, `isort`, `flake8` | 代码风格检查 |
| `tests` | `pytest`, `parameterized`, `math-verify`, `jieba` | 跑单元测试 |
| `eval` | `lighteval`, `math-verify` | 基准评估 |
| `code` | `e2b-code-interpreter`, `morphcloud`, `python-dotenv`, `pandas`, `aiofiles` | 代码沙箱奖励 |
| `dev` | 上述四组之和 | 日常开发，一步到位 |

再看「始终安装」的核心依赖：

[setup.py:101-120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L101-L120) —— `install_requires` 是无论装哪个分组都会带入的核心依赖，注释强调要「keep this to a bare minimum（尽量精简）」。里面有 `accelerate`、`bitsandbytes`、`datasets`、`deepspeed`、`transformers`、`trl`、`wandb` 等训练必需品，但**没有** `torch`、`vllm`、`lighteval`、`e2b-code-interpreter`——后者分别由 vLLM 安装步骤或 `extras` 提供。

最后是 `setup(...)` 调用里的关键字段：

[setup.py:136-138](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L136-L138) —— `extras_require=extras` 把分组交给 pip；`python_requires=">=3.10.9"` 声明最低 Python 版本；`install_requires=install_requires` 声明核心依赖。注意第 133–134 行 `package_dir={"": "src"}` 与 `find_packages("src")`：源码在 `src/open_r1` 下，所以包根被指向 `src`，这就是为什么能 `import open_r1`。

#### 4.2.4 代码实践

**实践目标**：不改源码，仅通过阅读和「思想实验」搞清某个依赖来自哪个分组，并用 pip 命令验证。

**操作步骤**：

1. 打开 `setup.py`，回答：要跑代码奖励（code reward），需要哪些第三方库？它们分别由哪个 `extras` 分组或 `install_requires` 提供？
2. 写出对应的安装命令：`uv pip install -e ".[code]"`。
3. 若已装好环境，用命令验证依赖确实被装上：`pip show e2b-code-interpreter`。

**需要观察的现象**：

- `e2b-code-interpreter` 只在 `extras["code"]`（第 96 行）里，**不在** `install_requires`。所以只跑 `pip install -e .`（不带分组）时，它是不会被装的。
- 执行 `pip install -e ".[code]"` 后，`pip show e2b-code-interpreter` 才有输出。

**预期结果**：能正确指出 `e2b-code-interpreter`、`morphcloud` 来自 `code` 分组；`lighteval` 来自 `eval` 分组；`pytest` 来自 `tests` 分组；而 `trl`、`transformers` 在 `install_requires` 里，无论如何都会装。

> 待本地验证：是否真的只装 `[code]` 而不装 `lighteval`，可在本地 `pip show lighteval` 确认其未被安装。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `torch` 不放在任何 `extras` 分组里？

**参考答案**：因为 vLLM 的二进制是针对特定 PyTorch 2.6.0 编译的，必须由 `uv pip install vllm==0.8.5.post1` 这一步精确带入正确版本。如果把 `torch` 放进 `extras`，pip 可能按 `torch==2.6.0` 单独解析并从 PyPI 装，版本虽然一致但来源/构建可能不同，容易与 vLLM 二进制不匹配。因此 open-r1 选择在 `Makefile` 里通过 vLLM 间接、可控地安装 PyTorch。

**练习 2**：装 `[dev]` 是否会自动装上 `lighteval`？为什么？

**参考答案**：会。因为第 98 行 `extras["dev"] = extras["quality"] + extras["tests"] + extras["eval"] + extras["code"]`，而 `lighteval` 属于 `extras["eval"]`（第 97 行），`dev` 把 `eval` 包含进来了。

---

### 4.3 Makefile 的开发工作流目标

#### 4.3.1 概念说明

`Makefile` 是一个「命令快捷方式集合」。open-r1 用它把 `Makefile` 里反复要敲的长命令封装成 `make <目标>`。本讲关注与安装和日常开发直接相关的目标：`install`、`style`、`quality`、`test`、`slow_test`。

需要先理解文件开头一个全局设置：

[Makefile:4](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L4) —— `export PYTHONPATH = src`。它让所有 `make` 调用的 Python 进程都从 `src/` 目录找包，确保测试的是**本地检出**的源码，而不是已经 pip 安装的版本。这行注释也明确提醒：「don't use quotes!」。

[Makefile:6](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L6) —— `check_dirs := src tests`，定义了代码风格与质量检查要扫描的目录。

#### 4.3.2 核心流程

日常开发的典型循环是：

```
写代码 → make style (自动格式化) → make quality (检查风格) → make test (跑单元测试)
```

各目标的分工：

| 目标 | 作用 | 等价命令 |
|------|------|---------|
| `make install` | 一键建环境 + 装全部开发依赖 | 见 4.1.3 |
| `make style` | 用 ruff/isort **自动格式化**代码 | `ruff format` + `isort` |
| `make quality` | **只检查**风格，不改动代码 | `ruff check` + `isort --check-only` + `flake8` |
| `make test` | 跑快速单元测试（**忽略** `tests/slow/`） | `pytest -sv --ignore=tests/slow/ tests/` |
| `make slow_test` | 跑慢测试（需要代码沙箱等外部服务） | `pytest -sv -vv tests/slow/` |

注意 `style` 与 `quality` 的区别：`style` 会**改写**你的文件，`quality` 只**报告**问题、不改文件（CI 里用的是 `quality`）。

#### 4.3.3 源码精读

[Makefile:10-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L10-L16) —— `install` 目标（4.1.3 已详解）。

[Makefile:18-20](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L18-L20) —— `style`：`ruff format` 设定行宽 119、目标 Python 3.10，对 `src tests` 与 `setup.py` 自动格式化；再用 `isort` 整理 import 顺序。

[Makefile:22-25](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L22-L25) —— `quality`：`ruff check`（只检查不修改）、`isort --check-only`（只检查 import 顺序）、`flake8 --max-line-length 119` 三连。三者都通过才算合格。

[Makefile:27-28](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L27-L28) —— `test`：`pytest -sv --ignore=tests/slow/ tests/`。`-sv` 表示详细输出，`--ignore=tests/slow/` 主动跳过慢测试。慢测试需要真实代码沙箱（E2B/Morph/Piston），普通开发者机器上跑不了，所以默认忽略。

[Makefile:30-31](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L30-L31) —— `slow_test`：单独跑 `tests/slow/` 下的慢测试，需要外部服务支持。

#### 4.3.4 代码实践

**实践目标**：跑通「格式化 → 质量检查 → 单元测试」三连，确认本地开发环境可用。

**操作步骤**：

1. 确保已 `source openr1/bin/activate` 并装好 `[dev]`。
2. 运行 `make style` 格式化代码（这一步会修改源文件，请先 `git status` 确认工作区干净）。
3. 运行 `make quality` 检查风格，期望无报错。
4. 运行 `make test` 跑快速单元测试。

**需要观察的现象**：

- `make style` 执行后，`git diff` 应当几乎无变化（因为仓库里的代码本就符合风格）；若有变化，说明你本地之前的格式有偏差。
- `make quality` 退出码为 0，无报错输出。
- `make test` 会收集并运行 `tests/` 下的用例，跳过 `tests/slow/`。

**预期结果**：`make test` 全部通过，输出形如 `===== N passed in Xs =====`。

> 待本地验证：具体通过的用例数 N 取决于本地依赖是否齐全（如 `math-verify`、`jieba` 是否在 `tests` 分组里装好）。

#### 4.3.5 小练习与答案

**练习 1**：`make style` 和 `make quality` 有什么本质区别？CI 应该用哪个？

**参考答案**：`make style` 会**改写**文件（ruff format + isort 重排），`make quality` 只**检查**不改写（ruff check + isort --check-only + flake8）。CI 应该用 `quality`——因为 CI 不应自动修改代码，只该报告是否合规。

**练习 2**：为什么 `make test` 要加 `--ignore=tests/slow/`？

**参考答案**：`tests/slow/` 下的测试需要真实代码沙箱服务（E2B、Morph、Piston 等），普通开发机或 CI 上没有这些服务，跑了会失败。所以默认忽略，只有需要测沙箱逻辑时才手动 `make slow_test`。

---

## 5. 综合实践

把本讲三块内容串起来，完成一次「从零到可测试」的环境搭建与验证：

1. **规划**：先翻 `setup.py` 的 `_deps` 与 `extras`（4.2.3），列出你要跑「SFT 训练」需要哪些依赖：核心 `install_requires`（`trl`/`transformers`/`datasets`/`deepspeed`/`accelerate`）一定有；还要 `torch` + `vllm`（由 Makefile 单独装）；评估用的 `lighteval` 属于 `eval` 分组，训练时非必需。
2. **安装**：在有 GPU 的机器上跑 `make install`；记录它依次执行了哪 7 个子步骤（对照 4.1.2）。
3. **验证**：`pip show open-r1` 确认包名与版本 `0.1.0.dev0`；`python -c "import open_r1; print('ok')"` 确认可导入；`python -c "import torch; print(torch.__version__)"` 确认 PyTorch 为 2.6.0。
4. **质量门禁**：依次跑 `make style`、`make quality`、`make test`，确认本地代码风格合规、快速单元测试通过。
5. **反思**：写下如果你的机器没有 GPU，应该省略哪几步、改用哪条降级命令（参考 4.1.4），并说明降级后哪些功能不可用（不能 `import torch`/`vllm`，无法真正训练）。

**预期结果**：你能用一句话向别人解释「open-r1 的依赖被拆成 `install_requires` + 五个 `extras` 分组 + 单独的 torch/vllm 三层」，并能独立在干净机器上把环境搭好。

> 待本地验证：完整流程需 GPU + CUDA 12.4 环境；无 GPU 时只能完成降级方案的安装与 import 验证。

## 6. 本讲小结

- open-r1 的安装**顺序很重要**：先 `uv venv` 建环境 → 装 `vllm`（带入 PyTorch 2.6.0）→ 用 `--no-build-isolation` 装 `flash-attn` → 装项目本体 `[dev]`。`make install` 把这套流程封装成一条命令。
- `setup.py` 用一个 `_deps` 列表 + 正则反查字典 `deps` 集中管理依赖，再用 `deps_list` 按「短名字」取出完整规格，避免重复写版本号。
- 依赖分三层：**始终安装**的 `install_requires`（`trl`/`transformers`/`datasets` 等核心）、**按需的 `extras` 分组**（`tests`/`quality`/`eval`/`code`，`dev` 是它们之和）、以及**单独安装**的 `torch`/`vllm`/`flash-attn`（依赖 GPU，不进 `extras`）。
- `Makefile` 提供开发工作流目标：`make install`（装）、`make style`（格式化、会改文件）、`make quality`（只检查、CI 用）、`make test`（快速单测，忽略 `tests/slow/`）、`make slow_test`（需沙箱的慢测试）。
- `Makefile` 顶部的 `export PYTHONPATH = src` 保证测试的是本地检出源码而非已装版本；`GIT_LFS_SKIP_SMUDGE=1` 避免安装时自动拉取 LFS 大文件。
- 项目要求 Python ≥ 3.10.9、CUDA 12.4；遇到段错误先用 `nvcc --version` 排查 CUDA 版本。

## 7. 下一步学习建议

环境搭好后，下一步自然是「先跑通最小的训练」。建议按以下顺序继续：

- **u1-l4（配置系统与 YAML 训练配方）**：学习 `TrlParser` 如何把命令行参数与 `recipes/` 下的 YAML 合并，理解 `SFTConfig`/`GRPOConfig` 的字段——这是跑训练前必须掌握的配置层。
- **u2-l1（SFT 训练脚本主流程）**：进入 `src/open_r1/sft.py`，逐段拆解 `main` 函数，用最小配置（如 `Qwen3-0.6B-Base` + 小数据集）在本地或单卡上跑通一次 SFT，验证本讲搭建的环境是否真的可用。
- 阅读建议：先把本讲的 `setup.py` 第 92–98 行的 `extras` 分组记牢，后续看 `requirements` 类报错（如 `ModuleNotFoundError: lighteval`）时，就能立刻判断该补装哪个分组。
