# 环境准备与安装

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了 TileGym 的全局认知：它是基于 CUDA Tile 的 GPU 内核库，有「cuTile 教学场」与「LLM 加速示例库」两重身份，并且依赖多后端机制（cuTile/tilecpp/triton/cutile-rs）。本讲要解决一个非常现实的问题：**把 TileGym 在你自己的机器上装好、跑通**。

学完本讲后，你应该能够：

1. 理解为什么 torch/triton 必须单独安装，并能用正确的 `cu130` 索引安装它们。
2. 区分 `pip install tilegym`、`pip install tilegym[tileiras]`、`pip install .`、`pip install -e .` 四种安装方式的差异，并知道每种该在什么场景使用。
3. 说清 `cuda-tile` 包与 `tileiras` 运行时编译器之间的关系。
4. 阅读源码，判断四个后端（cuTile/tilecpp/triton/cutile-rs）各自的可用性探测逻辑与额外前置条件（CUDA 版本、nvcc、cargo 等）。
5. 用 `import tilegym; tilegym.get_available_backends()` 检查当前环境下哪些后端真的可用，并能解释为何某个后端「没出现」。

## 2. 前置知识

本讲面向初学者，但在动手前需要你具备下面这些常识：

- **Python 与 pip**：会用命令行运行 `python -c "..."` 和 `pip install ...`，理解「虚拟环境」是隔离依赖的好习惯。
- **CUDA 与 GPU**：知道 CUDA 是 NVIDIA 显卡的并行计算平台，有版本号（如 13.1）；`nvcc` 是 CUDA 的编译器。上一讲提到 TileGym 需要 Blackwell（CUDA 13.1+）或 Ampere（CUDA 13.2+）。
- **包与依赖**：理解一个 Python 库（如 `tilegym`）可以「依赖」别的库（如 `numpy`、`cuda-tile`）。这些依赖通常写在 `requirements.txt` 或 `setup.py` / `pyproject.toml` 里，`pip install` 时会自动一并装上。
- **运行时编译器**：某些库不是「装好就能跑」，它会在你**第一次调用**时把高层描述（DSL）编译成真正的 GPU 代码。TileGym 的 cuTile 后端就属于这一类，它的编译器叫 **tileiras**。

> 一个贯穿全讲的直觉：TileGym 把「装上包」和「能用某个后端」拆成了两件事。装上 `tilegym` 只是第一步，能不能跑 cuTile、tilecpp、triton、cutile-rs，取决于你机器上是否还具备各自的「额外前置条件」。本讲的一大半内容都在讲这套探测机制。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md) | 安装步骤、硬件要求、各后端说明的「权威文档」。本讲的命令大多出自这里。 |
| [requirements.txt](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/requirements.txt) | TileGym 的运行时依赖清单。注意：torch/triton **不在**里面。 |
| [setup.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/setup.py) | 构建脚本：读取 README、解析 requirements.txt、声明 extras（`tileiras`/`torch`/`dev`）。 |
| [pyproject.toml](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/pyproject.toml) | 声明构建后端（setuptools），并配置 ruff/pylint/pytest，但**不**在此声明依赖。 |
| [src/tilegym/\_\_init\_\_.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py) | 包入口：导入时先检查 torch，再初始化后端选择器，并导出 `get_available_backends` 等 API。 |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | **本讲核心**：四个后端的可用性探测函数，以及 `set_backend` / `get_available_backends`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：torch/triton 环境准备 → PyPI/源码安装与 editable 模式 → cuda-tile 与 tileiras 编译器依赖 → 各后端的可用性探测与额外前置条件。

### 4.1 torch 与 triton 环境准备

#### 4.1.1 概念说明

TileGym 是一个 GPU 内核库，它的「宿主」是 PyTorch——张量的创建、调度、autograd 都建立在 torch 之上；triton 则随 torch 一起提供。所以第一步永远是先有一个带 GPU 支持的 torch。

但这里有个**反直觉**的设计点：TileGym **不**把 torch 写进自己的依赖清单。原因很好理解——torch 的安装高度依赖你的硬件与 CUDA 版本，如果 TileGym 在 `requirements.txt` 里固定一个 torch 版本，很可能和你的显卡/CUDA 不匹配，反而帮倒忙。所以官方要求你**手动**装好 torch/triton，再装 TileGym。

#### 4.1.2 核心流程

安装带 CUDA 支持的 torch，关键是**指定正确的 wheel 索引**。官方文档用的是 PyTorch 的 `cu130`（CUDA 13.0）预发布索引：

1. 用 `--pre` 获取预发布版本。
2. 用 `--index-url` 指向 `https://download.pytorch.org/whl/cu130`，这样 pip 才能找到编译好对接 CUDA 13.x 的 torch wheel。
3. torch 装好后，triton 会作为 torch 的附属包一并装上。

#### 4.1.3 源码精读

README 的安装小节明确写了这一步，并标明已验证 `torch==2.9.1` 可用：

> [README.md:42-50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L42-L50)
> 这是 README 中「1. Prepare `torch` and `triton` environment」一节，给出 `pip install --pre torch --index-url https://download.pytorch.org/whl/cu130`，并注明 triton 会随 torch 一起装上。

为什么 torch 要手动装？看 `requirements.txt` 第一行注释就明白了：

> [requirements.txt:5](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/requirements.txt#L5-L5)
> 注释 `# Note: torch and triton should be pre-installed in your environment`——明确声明 torch/triton 由用户预先准备，不计入 TileGym 的依赖。

而 `src/tilegym/__init__.py` 在导入时**强制**检查 torch 是否存在，否则给出友好的报错与修复建议：

> [src/tilegym/\_\_init\_\_.py:6-21](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L6-L21)
> `_check_torch_dependencies()` 尝试 `import torch`，失败时抛出 `ImportError`，提示你「CUDA 版本因设备而异，请手动安装匹配的版本」，并建议 `pip install tilegym[torch]`。函数在导入最顶层就调用（第 21 行），保证缺 torch 时立刻报错而不是在后续使用时才崩溃。

注意这个 `[torch]` 提示——它对应 setup.py 里的一个 extra（见 4.2 节），是「不想自己挑版本」时的便捷通道。

#### 4.1.4 代码实践

1. **实践目标**：在干净环境中准备好 torch/triton，并确认 torch 能看到 GPU。
2. **操作步骤**：
   ```bash
   # 1) 创建并激活虚拟环境
   python -m venv .venv && source .venv/bin/activate
   # 2) 安装对接 CUDA 13.x 的 torch（含 triton）
   pip install --pre torch --index-url https://download.pytorch.org/whl/cu130
   # 3) 确认 torch 与 CUDA
   python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available())"
   ```
3. **需要观察的现象**：第三步会打印 torch 版本（应为 2.9.x 或更新）、CUDA 版本（13.x），以及 `True`（表示 GPU 可用）。
4. **预期结果**：`torch.cuda.is_available()` 返回 `True`，`torch.version.cuda` 以 `13.` 开头。
5. **待本地验证**：如果你的机器没有 NVIDIA GPU 或 CUDA 驱动不匹配，`torch.cuda.is_available()` 可能返回 `False`；此时后续 TileGym 的算子无法实际运行，但 `import tilegym` 本身仍可能成功（仅 cuTile 探测会受影响，见 4.4 节）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 TileGym 不把 `torch==2.9.1` 直接写进 `requirements.txt`？
  - **参考答案**：因为带 GPU 支持的 torch wheel 与硬件、CUDA 版本强相关，固定一个版本很容易和用户的显卡/CUDA 不匹配。所以作者要求用户自行用正确的索引（`cu130`）安装匹配版本，并通过 `__init__.py` 的 `_check_torch_dependencies()` 在导入时兜底检查。
- **练习 2**：`pip install tilegym[torch]` 与 `pip install --pre torch --index-url .../cu130` 有何区别？
  - **参考答案**：`[torch]` extra 只是 `torch>=2.9.1`（见 setup.py 的 `extras_require`），从默认 PyPI 源装，**不一定**带 CUDA 13 支持；手动用 `cu130` 索引才能确保拿到对接 CUDA 13.x 的 wheel。所以推荐手动方式。

### 4.2 PyPI / 源码安装与 editable 模式

#### 4.2.1 概念说明

torch 准备好后，第二步是安装 TileGym 本身。TileGym 提供三种典型安装途径，对应不同使用场景：

- **从 PyPI 安装**（推荐给纯使用者）：`pip install tilegym[tileiras]`。
- **从源码安装**（想看代码或用非默认后端）：`git clone` 后 `pip install .[tileiras]`。
- **editable 模式**（要改 TileGym 代码）：`pip install -e .[tileiras]`，安装的是「指向源码的链接」，改代码立即生效。

要理解这三种方式的本质，关键是搞清楚 TileGym 是**怎么被打包**的——它用的是较传统的 `setup.py` + setuptools，而 `requirements.txt` 通过代码被读取并解析成依赖。

#### 4.2.2 核心流程

TileGym 的构建逻辑可以用下面伪代码概括：

```
pyproject.toml: 声明 [build-system] requires = ["setuptools>=45", "wheel"]
        ↓  （构建时调用 setuptools.build_meta）
setup.py:
   1. 读 README.md 当作 PyPI 上的长描述
   2. parse_requirements("requirements.txt")  →  得到依赖列表
   3. setuptools.setup(
          name="tilegym", version="1.4.0",
          packages=find_packages(where="src"),   # 源码在 src/ 下
          package_dir={"": "src"},               # 把 src/ 当作包根
          python_requires=">=3.10",
          install_requires=<上面解析出的依赖>,
          extras_require={"tileiras": [...], "torch": [...], "dev": [...]},
      )
```

注意 `pyproject.toml` **没有** `[project]` 表，也就是说依赖、版本、包发现都不在 `pyproject.toml` 里，而在 `setup.py` 里。`pyproject.toml` 只承担两件事：声明构建后端，以及配置 ruff/pylint/pytest 等开发工具。

#### 4.2.3 源码精读

先看构建后端的声明：

> [pyproject.toml:5-7](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/pyproject.toml#L5-L7)
> `[build-system]` 声明用 `setuptools>=45` 与 `wheel` 来构建，`build-backend = "setuptools.build_meta"`。这就是为什么 `pip install .` 会去执行 `setup.py`。

再看 `setup.py` 的关键配置。它把 `src/` 作为包根目录，并用一个 `parse_requirements` 函数把 `requirements.txt` 转成依赖列表：

> [setup.py:43-55](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/setup.py#L43-L55)
> `packages=find_packages(where="src")` + `package_dir={"": "src"}` 说明真正的包代码在 `src/tilegym` 下；`python_requires=">=3.10"`；`install_requires=parse_requirements("requirements.txt")` 把运行时依赖动态地从 `requirements.txt` 注入。

> [setup.py:13-27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/setup.py#L13-L27)
> `parse_requirements` 逐行读取 `requirements.txt`，跳过空行与整行注释，并剥离行内注释（如 `pkg  # comment`）。这正是 `requirements.txt` 里那些 `# ...` 说明能共存的原因。

最后看 extras（可选依赖分组），它们决定了你 `pip install tilegym[XXX]` 时额外装什么：

> [setup.py:56-67](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/setup.py#L56-L67)
> `extras_require` 定义三个分组：`dev`（pytest、ruff）、`torch`（`torch>=2.9.1`）、`tileiras`（`cuda-tile[tileiras]`）。其中 `tileiras` 这一组正是 `[tileiras]` 后缀的含义，会在 4.3 节展开。

而 README 的安装命令全部对应到上面这套配置：

> [README.md:56-78](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L56-L78)
> 给出从 PyPI 安装（`pip install tilegym[tileiras]`）、已有系统 tileiras 时省略 extra（`pip install tilegym`）、从源码安装（`git clone` + `pip install .[tileiras]`），以及 editable 开发模式（`pip install -e .`）。

#### 4.2.4 代码实践

1. **实践目标**：理解三种安装方式在 `site-packages` 里留下的不同痕迹。
2. **操作步骤**（建议在 4.1 节准备好的环境里做）：
   ```bash
   # 方式 A：从 PyPI 装
   pip install tilegym[tileiras]
   python -c "import tilegym, tilegym.__file__ as f; print(f)"
   pip show tilegym      # 注意 Version 与 Location

   # 方式 B：从源码 editable 装（需要先 clone 仓库）
   git clone https://github.com/NVIDIA/TileGym.git
   cd TileGym
   pip uninstall -y tilegym
   pip install -e .[tileiras]
   pip show tilegym      # Location 应指向 clone 出来的源码目录，且带 editable 标记
   ```
3. **需要观察的现象**：方式 A 的 Location 指向 `site-packages/tilegym`；方式 B 的 Location 指向你 clone 的源码目录（`.../TileGym/src/tilegym`），且 `pip show` 会标注 editable。
4. **预期结果**：editable 模式下，你修改 `src/tilegym/__init__.py`（例如改 `__version__`）后，重新 `import tilegym` 立即看到变化，无需重装。
5. **待本地验证**：不同 pip/setuptools 版本对 editable 标记的显示措辞略有差异，以你机器上的 `pip show` 实际输出为准。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `pyproject.toml` 里找不到 `[project.dependencies]`？
  - **参考答案**：TileGym 用的是传统 `setup.py` 写法，依赖在 `setup.py` 中通过 `install_requires=parse_requirements("requirements.txt")` 动态注入；`pyproject.toml` 只声明 `[build-system]` 和工具配置，不含 `[project]` 表。
- **练习 2**：`pip install tilegym` 与 `pip install -e .` 修了一个 bug 后，分别需要做什么才能让改动生效？
  - **参考答案**：前者装的是「拷贝」到 `site-packages` 的副本，改源码不生效，需要重新 `pip install`（最好先卸载）；后者是 editable 链接，改完源码直接重新 `import` 即可生效，无需重装。所以**改 TileGym 自身代码时一定要用 editable 模式**。

### 4.3 cuda-tile 与 tileiras 运行时编译器依赖

#### 4.3.1 概念说明

cuTile 是 TileGym 的**默认后端**，也是整个库的灵魂。但 cuTile 本身并不是 TileGym 的一部分——它来自一个独立的包 [`cuda-tile`](https://github.com/nvidia/cutile-python)（cuTile 的 Python DSL）。你用 `import cuda.tile as ct` 拿到的就是它。

而 `cuda-tile` 背后还依赖一个**运行时编译器** `tileiras`：你在 Python 里写的 `@ct.kernel` 是高层 tile 描述，真正运行前要由 `tileiras` 把它编译成 GPU 可执行的代码。这就是上一讲说的「运行时编译器依赖」。

这里有两种方式获得 `tileiras`：

1. **捆绑到 Python 环境**：`pip install cuda-tile[tileiras]`，把 `tileiras` 一起装进 Python 环境，开箱即用。这正是 `pip install tilegym[tileiras]` 的意义（`[tileiras]` extra 会触发 `cuda-tile[tileiras]`）。
2. **系统已自带**：如果你装了 CUDA Toolkit 13.1+，系统里可能已经有 `tileiras`，那么装基础包 `cuda-tile`（不带 extra）即可。

#### 4.3.2 核心流程

依赖关系可以用下面这张「包含图」理解：

```
pip install tilegym[tileiras]
        │  （触发 extras_require["tileiras"]）
        └──> 安装 cuda-tile[tileiras]
                  │  （cuda-tile 是 DSL；[tileiras] 捆绑编译器）
                  ├──> cuda-tile        （import cuda.tile）
                  └──> tileiras         （运行时把 @ct.kernel 编译为 GPU 代码）

pip install tilegym          （不带 extra）
        │
        └──> 安装 cuda-tile   （基础包；要求系统里已有 tileiras）
```

无论哪种方式，`cuda-tile>=1.3.0` 这个基础依赖都会被装上。

#### 4.3.3 源码精读

`requirements.txt` 里有一行带详细注释的依赖：

> [requirements.txt:13](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/requirements.txt#L13-L13)
> `cuda-tile>=1.3.0  # Or use: pip install cuda-tile[tileiras] for bundled tileiras compiler`——基础依赖是 `cuda-tile>=1.3.0`，注释提示若想要捆绑的 tileiras 编译器，可改用 `cuda-tile[tileiras]`。

`[tileiras]` extra 的对应声明在 setup.py：

> [setup.py:64-66](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/setup.py#L64-L66)
> `extras_require["tileiras"] = ["cuda-tile[tileiras]"]`，所以 `pip install tilegym[tileiras]` 会额外拉取带捆绑编译器的 `cuda-tile`。

README 用一段话把这两条路径讲清楚：

> [README.md:54-68](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md#L54-L68)
> 说明 TileGym 用 `cuda-tile`(≥1.3.0) 编程、运行时依赖 `tileiras` 编译器；`pip install tilegym[tileiras]` 会把 `tileiras` 捆绑进环境；若系统已自带 `tileiras`（如来自 CUDA Toolkit 13.1+），可直接 `pip install tilegym` 省略 extra。

那 cuTile 后端「可用」具体怎么判定？看 selector.py 的导入逻辑：能 `import cuda.tile` 且能 `import cuda.tile.tune`，就算 cuTile 可用：

> [src/tilegym/backend/selector.py:30-44](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L30-L44)
> 模块级 `try: import cuda.tile as ct; import cuda.tile.tune` → `CUTILE_AVAILABLE = True`；失败则发警告并把 `CUTILE_AVAILABLE` 置为 `False`，提示 `pip install cuda-tile`。注意 `cuda.tile.tune` 是每个 cuTile 算子都需要的，所以这里一并探测。

#### 4.3.4 代码实践

1. **实践目标**：确认 `cuda.tile` 可导入、并判断 `tileiras` 是否到位。
2. **操作步骤**：
   ```bash
   # 1) 确认 cuTile DSL 可用
   python -c "import cuda.tile as ct; print('cuda.tile OK', ct.__name__)"
   python -c "import cuda.tile.tune; print('cuda.tile.tune OK')"
   # 2) 看 tileiras 是否随 [tileiras] extra 装上了
   python -c "import tileiras; print(tileiras.__file__)" 2>/dev/null || echo "tileiras 不在 Python 路径中（可能由系统 CUDA 提供）"
   ```
3. **需要观察的现象**：第 1 步应打印 `cuda.tile OK cuda.tile`；第 2 步要么打印 tileiras 路径，要么打印「不在 Python 路径中」。
4. **预期结果**：装了 `[tileiras]` extra 后，tileiras 在 Python 路径中；只装基础包时，tileiras 来自系统 CUDA，第 2 步打印「不在 Python 路径中」并不代表它真的缺失。
5. **待本地验证**：tileiras 的具体导入名/路径以你的 `cuda-tile[tileiras]` 版本为准；真正的「能否编译内核」要到第一次调用算子时才见分晓。

#### 4.3.5 小练习与答案

- **练习 1**：`cuda-tile` 和 `tileiras` 是同一个东西吗？
  - **参考答案**：不是。`cuda-tile` 是 cuTile 的 **Python DSL**（你 `import cuda.tile as ct` 拿到的 API）；`tileiras` 是把这套 DSL **编译成 GPU 代码的运行时编译器**。`cuda-tile` 单独装时假定系统已有 `tileiras`，`cuda-tile[tileiras]` 则把 `tileiras` 捆绑进 Python 环境。
- **练习 2**：selector.py 用「能否 `import cuda.tile`」判定 cuTile 是否可用，这能保证算子一定能跑吗？
  - **参考答案**：不能完全保证。它只验证了 DSL 包导入成功（包括 `cuda.tile.tune`），但 `tileiras` 编译器是否真正可用、GPU 是否支持，要等到**第一次真正 launch 内核**时才会暴露。所以 cuTile 的可用性探测是「乐观」的。

### 4.4 各后端的可用性探测与额外前置条件

#### 4.4.1 概念说明

这是本讲最核心、也最贴近实战的一节。TileGym 有四个后端，每个后端的「能否使用」由 `src/tilegym/backend/selector.py` 里的探测函数决定。理解这些探测逻辑，你就能看懂 `get_available_backends()` 返回的集合里**为什么有/没有某个后端**。

四个后端与它们的额外前置条件：

| 后端 | 探测方式（来自 selector.py） | 额外前置条件 |
| --- | --- | --- |
| **cuTile**（默认） | 能 `import cuda.tile` + `cuda.tile.tune` | `cuda-tile` 包 + `tileiras` 编译器 |
| **triton** | 始终视为可用（`True`），实际分为 `nvt`/`oait` 两路 | 要用 nvtriton（CUDA Tile IR）需 `ENABLE_TILE=1` 并装 nvtriton wheel |
| **tilecpp** | 先廉价检查模块可导入，再延迟检查 `nvcc >= 13.3` | `nvcc >= 13.3`（CUDA Tile C++ 编译器） |
| **cutile-rs** | PATH 上有 `cargo` 或存在预编译 `.so` | Rust 1.89+ 的 `cargo`、CUDA 头文件（bindgen） |

#### 4.4.2 核心流程

selector.py 在模块加载时做两件事：

1. **`_initialize_available_backends()`**：对每个后端调用对应探测函数，把可用的加入全局集合 `_AVAILABLE_BACKENDS`。
2. **`_load_from_environment()`**：读环境变量 `CUTILE_TUTORIALS_BACKEND` 设定默认后端 `_CURRENT_BACKENDS`（默认 `"cutile"`）。

之后用户可以：

- `get_available_backends()`：返回当前可用的后端集合。
- `set_backend(name)`：切换当前后端（切换时会再校验一次 tilecpp 的 nvcc 要求，做到「快速失败」）。

注意一个重要的**设计权衡**：tilecpp 的 nvcc 版本检查是**延迟且缓存**的——`import tilegym` 时不立即跑 `nvcc --version`（避免在没有 CUDA 的机器上产生子进程开销），而是等到真正需要判断 tilecpp 时才跑，且结果被 `@functools.cache` 缓存。

#### 4.4.3 源码精读

先看把四个后端探测汇总的地方：

> [src/tilegym/backend/selector.py:188-195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L188-L195)
> `_check_backends_availability()` 返回 `{"cutile": is_cutile_available(), "triton": True, "tilecpp": _TILECPP_MODULE_IMPORTABLE, "cutile-rs": is_cutile_rs_available()}`。注意 `triton` 恒为 `True`，而 `tilecpp` 这里只用了**廉价**的模块可导入检查 `_TILECPP_MODULE_IMPORTABLE`，nvcc 版本检查被刻意推迟。

cuTile 的探测与「强制关闭」开关：

> [src/tilegym/backend/selector.py:47-51](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L47-L51)
> `is_cutile_available()`：若环境变量 `TILEGYM_DISABLE_CUTILE=1` 则强制返回 `False`（用于调试其他后端），否则返回模块级探测结果 `CUTILE_AVAILABLE`。

triton 后端细分为 nvtriton（CUDA Tile IR）与否：

> [src/tilegym/backend/selector.py:20-27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L20-L27)
> `is_nvt_available()`：尝试 `import triton.backends.tileir`，且要求 `ENABLE_TILE` 环境变量正好为 `1`。只有两者都满足才认为 nvtriton 可用，否则 triton 后端走 `oait` 路径（见 `get_available_triton_backend`，第 222-225 行）。

tilecpp 的延迟、缓存探测，以及 nvcc 版本要求：

> [src/tilegym/backend/selector.py:54-86](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L54-L86)
> `_TILECPP_MIN_NVCC = (13, 3)` 定义最低 nvcc 版本；`_nvcc_version_supported()` 解析 `$TILECPP_NVCC_PATH`（否则 PATH 上的 `nvcc`），运行 `nvcc --version`，用正则提取版本号，要求 `>= 13.3`。

> [src/tilegym/backend/selector.py:119-146](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L119-L146)
> `is_tilecpp_available()` 带 `@functools.cache`，是延迟执行的关键：第一次调用才真正跑 nvcc 检查，之后缓存。注释明确说明这样 `import tilegym` 在无 CUDA 主机上没有子进程开销。

cutile-rs 的「宽松探测」：有 cargo 或有预编译 `.so` 即可：

> [src/tilegym/backend/selector.py:149-181](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L149-L181)
> `is_cutile_rs_available()`：若 `CUTILE_RS_AUTOBUILD` 开启（默认），PATH 上有 `cargo` 就算可用（首次 dispatch 时懒编译 crate）；若关闭 autobuild，则必须有未过期的预编译 `libcutile_kernels.so`。注释（Rule 35）说明 libclang / CUDA 头文件 / tileiras 这些在 dispatch 时才验证，不在探测阶段。

set_backend 切换时的「快速失败」：

> [src/tilegym/backend/selector.py:232-248](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L232-L248)
> `set_backend("tilecpp")` 时会再调用 `is_tilecpp_available()` 做完整 nvcc 检查；不满足就抛 `ValueError`，避免「选择时通过、dispatch 时才回退」的静默错误。

默认后端来自环境变量：

> [src/tilegym/backend/selector.py:208-215](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L208-L215)
> `_load_from_environment()` 读 `CUTILE_TUTORIALS_BACKEND`（注意是历史命名 `TUTORIALS`），设为默认后端；若该值不在可用集合中则抛错。

最后，`get_available_backends` 是上一讲提到的顶层 API，它从 `tilegym` 包直接导出：

> [src/tilegym/\_\_init\_\_.py:34-40](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L34-L40)
> 包入口从 `.backend` 导入 `get_available_backends`、`set_backend`、`get_current_backend` 等，使其成为 `tilegym.get_available_backends()`。

#### 4.4.4 代码实践

1. **实践目标**：运行本讲指定的验证命令，并解释返回集合中每个后端的去留。
2. **操作步骤**：
   ```bash
   # 默认环境
   python -c "import tilegym; print(sorted(tilegym.get_available_backends()))"

   # 实验 A：强制关闭 cuTile
   TILEGYM_DISABLE_CUTILE=1 python -c "import tilegym; print(sorted(tilegym.get_available_backends()))"

   # 实验 B：尝试把默认后端设成 triton（注意历史命名 CUTILE_TUTORIALS_BACKEND）
   CUTILE_TUTORIALS_BACKEND=triton python -c "import tilegym; print(tilegym.get_current_backend())"
   ```
3. **需要观察的现象**：
   - 默认集合里至少有 `cutile` 和 `triton`；`tilecpp` 取决于是否有 nvcc≥13.3；`cutile-rs` 取决于是否有 `cargo`。
   - 实验 A 中 `cutile` 应从集合里**消失**。
   - 实验 B 中 `get_current_backend()` 应打印 `triton`。
4. **预期结果**：上述现象符合预期即说明探测逻辑被正确触发。
5. **待本地验证**：四个后端的具体去留完全取决于你机器上是否安装了 nvcc、cargo、nvtriton wheel 等；不同环境结果不同，这正是本节要传达的点。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `_check_backends_availability()` 里 `tilecpp` 只用 `_TILECPP_MODULE_IMPORTABLE`（廉价检查），而不直接调用 `is_tilecpp_available()`（含 nvcc 检查）？
  - **参考答案**：因为 `is_tilecpp_available()` 会 `subprocess.run(["nvcc", "--version"])`，在 `import tilegym` 时执行会产生不必要的子进程开销（尤其在没有 CUDA 的主机上）。作者把昂贵的 nvcc 检查推迟到真正判断/切换 tilecpp 时才跑，并用 `@functools.cache` 缓存，兼顾「可用性集合」的廉价计算与「真正使用」时的准确性。
- **练习 2**：`set_backend("tilecpp")` 为什么要再检查一次 `is_tilecpp_available()`？
  - **参考答案**：因为可用性集合里 tilecpp 的条目只反映「模块能导入」这个廉价条件，并不保证 nvcc≥13.3。`set_backend` 在用户**主动选择** tilecpp 时补做完整检查并快速失败，避免「选了 tilecpp 却在 dispatch 时静默回退到别的后端」这种难以排查的行为。
- **练习 3**：cutile-rs 的探测「宽松」体现在哪里？
  - **参考答案**：只要 PATH 上有 `cargo`（或存在预编译 `.so`）就认为可用，并不在探测阶段验证 libclang、CUDA 头文件、tileiras 这些；它们会在首次真正 dispatch（懒编译 crate）时才验证。这样无 Rust 工具链的环境能快速跳过 cutile-rs 测试，而不是在 import 阶段就报错。

## 5. 综合实践

把本讲四个模块串起来，完成一次**从零到验证**的干净安装。

**任务**：在一个全新的虚拟环境里，按文档安装 `tilegym[tileiras]`，记录每一步命令，最后运行验证命令并解读结果。

**操作步骤**：

```bash
# 1) 干净虚拟环境
python -m venv tg-venv && source tg-venv/bin/activate

# 2) 准备 torch/triton（4.1 节）
pip install --pre torch --index-url https://download.pytorch.org/whl/cu130

# 3) 安装 TileGym（4.2 + 4.3 节，捆绑 tileiras）
pip install tilegym[tileiras]

# 4) 验证：检查导入与可用后端（4.4 节）
python -c "import tilegym; print('version', tilegym.__version__); print('backends', sorted(tilegym.get_available_backends()))"
```

**需要观察并回答的问题**：

1. `tilegym.__version__` 是否为 `1.4.0`？（对应 setup.py 与 `__init__.py` 的 `__version__`）
2. `get_available_backends()` 返回了哪些后端？`cutile` 是否在内？
3. 如果返回集合里**没有** `tilecpp`，结合 selector.py 判断最可能的原因是什么？（提示：nvcc 版本）

**预期结果**：成功导入、版本为 `1.4.0`、`cutile` 与 `triton` 至少出现在可用集合中。`tilecpp`/`cutile-rs` 是否出现取决于机器工具链。

**待本地验证**：以上命令的实际输出依赖你的硬件（Blackwell/Ampere）、CUDA 版本、是否安装了 nvcc/cargo。请在自己的机器上如实记录结果，不要照抄「预期结果」。

## 6. 本讲小结

- TileGym **不**在 `requirements.txt` 里固定 torch/triton，要求你用 `cu130` 索引手动安装，导入时由 `_check_torch_dependencies()` 兜底检查。
- 三种安装方式各有用途：PyPI（使用）、源码（看代码/非默认后端）、editable（改 TileGym 自身）。`pyproject.toml` 只声明构建后端与工具配置，依赖在 `setup.py` 中从 `requirements.txt` 动态解析。
- cuTile 后端依赖 `cuda-tile`(≥1.3.0) 这个 DSL 包，以及运行时编译器 `tileiras`；`pip install tilegym[tileiras]` 通过 extra 把 tileiras 捆绑进环境。
- selector.py 用四种不同策略探测四个后端的可用性：cuTile 看 `import cuda.tile`、triton 恒为可用（nvtriton 需 `ENABLE_TILE=1`）、tilecpp 看 nvcc≥13.3（延迟+缓存）、cutile-rs 看 `cargo` 或预编译 `.so`。
- tilecpp 的探测刻意「廉价优先、延迟校验」，既保证 `import tilegym` 轻量，又让 `set_backend("tilecpp")` 快速失败。
- `tilegym.get_available_backends()` 是你检验「装好后到底能用哪些后端」的标准入口，结果因机器工具链而异。

## 7. 下一步学习建议

本讲结束后，你已经能装好 TileGym 并知道哪些后端可用。接下来的学习路径建议：

- **下一讲 u1-l3《第一次调用 TileGym 算子》**：用最小脚本真正调用一个算子（如 softmax），把本讲装的 cuTile 后端跑起来，并与 PyTorch 参考比较正确性。这是从「装好」到「用起来」的关键一步。
- **u1-l4《仓库目录结构导览》**：如果你对 `src/tilegym` 下的模块划分（ops/backend/transformers/suites 等）还不清楚，可以先读这篇建立目录地图。
- **进阶衔接 u2-l2《后端注册表与分发机制》**：本讲只讲了后端**可用性**；想知道算子调用时**如何按当前后端选中实现**，要看 dispatcher.py 的 `_REGISTRY` 与 `dispatch` wrapper——那也是后续所有算子讲义的基础。

建议在进入下一讲前，先完成本讲综合实践，确保你的 `get_available_backends()` 至少返回了 `cutile`。
