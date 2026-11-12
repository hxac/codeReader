# 安装、构建与第一次运行

## 1. 本讲目标

学完本讲，你应该能够：

1. 看一眼当前环境（torch 版本、CUDA 版本、Python 版本、GPU 架构），就知道该装哪个 wheel——或者为什么必须源码构建。
2. 用 `FAST_ULYSSES_CUDA_ARCH=<本机架构> pip install -e . --no-build-isolation` 完成一次快速源码构建，并能解释 `--no-build-isolation` 为什么是必需的而不是习惯。
3. 用 `torchrun` 跑通 README 的 quickstart 脚本和 `examples/dit_attention.py`，包括只有一张卡（或零张卡）时如何降级运行、哪些行为在 `world_size=1` 时仍然成立。
4. 读懂 `fast-ulysses doctor` 的三段输出（构建信息、设备列表、NVLink 矩阵），并据此判断这台机器能不能跑这个算子。

本讲是纯「工程上手」讲：不改任何算法代码，只解决「让它在你机器上跑起来」这件事。

## 2. 前置知识

- **wheel 与 ABI**：Python 的 wheel 是预编译的二进制包。包含 C++/CUDA 扩展的 wheel 不是「一个 Linux 包到处能跑」——它编译时链接的头文件、符号和数据结构布局，必须与运行时的环境一致。这种一致性约束统称 ABI（Application Binary Interface）。
- **torch 的版本坐标**：一个 torch 安装由两个数字定位——`torch.__version__`（如 `2.11.0`）和 `torch.version.cuda`（如 `12.8`）。前者的 **minor**（`2.11` 中的 `11`）决定 C++ ABI，后者的 **major**（`12`）决定链接哪份 `libcudart`。fast-ulysses 的 wheel 就是按「torch minor × CUDA major」这两个轴发布的。
- **GPU 架构（compute capability）**：NVIDIA GPU 按架构编号，如 H100 是 `sm_90`、A100 是 `sm_80`、B200 是 `sm_100`。CUDA 扩展编译时要显式列出为哪些架构生成代码；列得越多编译越慢。用 `torch.cuda.get_device_properties(0)` 可以读出本机卡的 `major`/`minor`。
- **torchrun**：PyTorch 自带的分布式启动器。`torchrun --nproc_per_node=N script.py` 会启动 N 个进程，每个进程拿到不同的 `RANK` / `LOCAL_RANK` 环境变量，你需要在脚本里 `torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))` 把每个进程绑到自己的卡。
- **PEP 440 local version**：Python 包版本可以带「本地标签」，写成 `0.2.0+torch210cu128` 这种 `版本+标签` 的形式。pip 在比较版本时会**给这些标签排序**——这正是本讲会反复踩到的一个坑的根源（见 4.1.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md) | 项目门面：Quick start 脚本、Install 一节的四条命令、Testing 命令 |
| [docs/install.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md) | 安装的权威文档：wheel 行表、索引选择、PEP 440 陷阱、构建变量表、导入失败排查 |
| [setup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py) | 源码构建入口：调用 CMake、生成 `_build_meta.py`、按内存限制并行度 |
| [pyproject.toml](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml) | 包元数据：`fast-ulysses` 命令行入口、包目录布局、pytest 配置 |
| [python/fast_ulysses/cli.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py) | `doctor` 与 `wheel-url` 两个子命令的实现 |
| [python/fast_ulysses/__init__.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py) | 包导入：先加载 torch 再 dlopen `_C`，失败时转成诊断报告 |
| [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py) | 第一个「真实」示例：序列并行注意力块，前向 + 反向 + 自校验 |
| [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu) | 标志位屏障实现；本讲只看它 `world_size == 1` 时直接返回这一个事实 |

## 4. 核心概念与源码讲解

### 4.1 wheel 选择：按「torch minor × CUDA major」对号入座

#### 4.1.1 概念说明

fast-ulysses 的核心是一个名为 `_C` 的 C++/CUDA 扩展（`.so` 文件）。这个扩展里嵌入了 torch 内部的 C++ 成分——`c10d::Work` 虚表、pybind11 模块、`TORCH_LIBRARY` 算子注册。torch 每升一个 minor（2.10 → 2.11），这些内部结构就可能变化，扩展立刻无法加载。CUDA major（12 → 13）则决定扩展链接的是 `libcudart.so.12` 还是 `libcudart.so.13`。所以 wheel 必须精确匹配 **torch 的 minor** 和 **CUDA 的 major**，这两条合起来就是发布矩阵的「行」。

一个发布是 36 个 wheel：8 个（torch minor × CUDA major）行 × 4 个 CPython 版本，再加 4 个去掉本地标签、发到 PyPI 的 wheel。

#### 4.1.2 核心流程

确定自己该装哪个 wheel 的流程：

1. 查询环境三元组：torch 版本、torch 的 CUDA 版本、Python 版本。
2. 在行表里找到（torch minor, CUDA major）对应的标签，如 `torch211cu128`。
3. 按下面的优先级选安装方式：
   - 想让 pip 自动解析 → 用 `whl/cu12` 或 `whl/cu13` 总索引；
   - 同一条命令里还要从 PyPI 装别的东西 → 用 `whl/<tag>` 单行索引；
   - 想要文件本身 → `fast-ulysses wheel-url` 打印的 `--find-links` 命令（版本必须写全）；
   - 你的 torch 不在表里 → 源码构建（4.2）。

#### 4.1.3 源码精读

**第一步：查环境。** 文档给出的这条命令一次打全三个坐标：

```bash
python -c "import sys,torch; print(torch.__version__, torch.version.cuda, sys.version_info[:2])"
```

出处：[docs/install.md:L22-L24](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L22-L24)——用当前 torch 的实际输出去查下面这张表。

**第二步：查行表。** 8 个行标签覆盖 torch 2.10–2.13 × CUDA 12/13：

> | your torch | torch's CUDA | wheel tag |
> |---|---|---|
> | 2.10.x | 12.x | `torch210cu128` |
> | … | … | … |
> | 2.13.x | 13.x | `torch213cu130` |

出处：[docs/install.md:L26-L35](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L26-L35)。表中行标签就是 wheel 的 PEP 440 本地版本（`fast_ulysses-0.2.0+torch210cu128-cp312-…`）。

**第三步：选索引。** README 的 Install 一节给出首选命令：

```bash
pip install fast-ulysses --index-url https://triple-mu.github.io/fast-ulysses/whl/cu13/   # or cu12
```

出处：[README.md:L131-L138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L131-L138)。关键机制在 [docs/install.md:L51-L54](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L51-L54)：这个索引**只有 fast-ulysses 没有别的包**，所以 pip 无法靠安装一个新 torch 来满足最高候选者，只能一路回退到「`Requires-Dist: torch==2.11.*` 恰好被你已装的 torch 满足」的那一行。torch minor 不靠你选，靠这个回退解析。CUDA major 靠 URL 选（`cu12` / `cu13`），因为 wheel 元数据里不记录 CUDA 版本——这与 `download.pytorch.org/whl/cu128` 的目录组织是同一个道理（[docs/install.md:L46-L49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L46-L49)）。

**两个必须知道的坑：**

- **PyPI 坑**：PyPI 不接受带本地标签的版本，所以 PyPI 上只有 `torch213cu130` 一行（钉死 `torch==2.13.*`）。在任何其他 torch 上，裸的 `pip install fast-ulysses` **不会报错**——pip 会直接把你的 torch 升级到 2.13（一份和你所选不同的 CUDA 构建），并且把它报告成一次普通的依赖解析。见 [docs/install.md:L114-L124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L114-L124) 与 [README.md:L146-L147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L146-L147)。同理，不要 `--extra-index-url https://pypi.org/simple`——这等于亲手把 torch 交还给 pip（[docs/install.md:L71-L73](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L71-L73)）。
- **PEP 440 排序坑**：本地标签按段比较，而不带标签的 `==0.2.0` 会**完全忽略**标签。于是在任何持有多个行的文件集合（`--find-links`、wheelhouse 目录）上，不带标签的要求永远解析到最高的 `+torch213cu130`，与你装了什么 torch 无关、且无任何警告（[docs/install.md:L127-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L127-L145)）。所以用 `--find-links` 时版本必须写全：`pip install "fast-ulysses==0.2.0+torch210cu128" --find-links …`。

**让工具替你查：`wheel-url` 子命令。** 它读取当前环境的 torch/CUDA/Python/机器架构，按 machine → python → torch minor → CUDA major 的顺序匹配，打印出写全版本的安装命令：

出处：[python/fast_ulysses/cli.py:L187-L241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L187-L241)。匹配顺序（machine → python → torch → CUDA）的实现和每一步失败时的提示语在 [cli.py:L130-L184](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L130-L184)；注意它是**故意只匹配 CUDA major** 的，因为扩展只从 toolkit 链接 `libcudart.so.<major>`（[cli.py:L133-L137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L133-L137) 的 docstring）。没有任何 wheel 匹配时，每条提示语最后都落在同一条出路——源码构建（[cli.py:L113-L115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L113-L115)）。

#### 4.1.4 代码实践

1. **实践目标**：确定当前环境对应的 wheel 行。
2. **操作步骤**：
   ```bash
   python -c "import sys,torch; print(torch.__version__, torch.version.cuda, sys.version_info[:2])"
   ```
   把输出的 (torch minor, CUDA major) 对到 docs/install.md 的行表上；如果包已装好，再运行 `fast-ulysses wheel-url` 对照它的 `row:` 一行。
3. **需要观察的现象**：`wheel-url` 第一行打印 `running: torch X.Y, cuda A.B, cpNN, x86_64`，随后给出 `row: torch X.Y, CUDA A.B, …` 和一条写全 `+torchXXXcuYYY` 标签的安装命令；命令末尾附一段解释「为什么版本必须写全」的警告文字（[cli.py:L228-L240](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L228-L240)）。
4. **预期结果**：行表查出的标签与 `wheel-url` 报告的标签一致。若你的 torch 不在 2.10–2.13 内，`wheel-url` 会明确说出「published for torch …」并列出源码构建命令——这也算正确结果。
5. 本讲写作环境无可用 torch/CPU 环境，以上输出为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：同事在 torch 2.11 + CUDA 12.8 的环境里执行了 `pip install fast-ulysses --find-links ./wheelhouse`（wheelhouse 里有全部 36 个 wheel），结果 `import fast_ulysses` 报 `undefined symbol: _ZN3c10...`。为什么？

**答案**：不带标签的要求在 PEP 440 下忽略本地版本并取最高者，于是装上了 `+torch213cu130`，而它嵌的是 torch 2.13 的 C++ ABI，与已装的 torch 2.11 不匹配，dlopen 时出现 undefined symbol。修复：`pip uninstall -y fast-ulysses` 后用写全标签的 `pip install "fast-ulysses==0.2.1+torch211cu128" --find-links ./wheelhouse`（排查流程见 [docs/install.md:L187-L213](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L187-L213)）。

**练习 2**：为什么 `whl/cu13` 索引不需要你在版本里写 `+torchXXXcu130` 标签，而 `--find-links` 需要？

**答案**：索引里只有 fast-ulysses 一个项目，pip 装不了新 torch，只能回退到 `Requires-Dist: torch==<你的minor>.*` 被现有 torch 满足的那一行——torch minor 由依赖解析决定。而 `--find-links`/多索引场景下 pip 会对所有候选取最高版本，最高标签永远是 `torch213cu130`，必须写全标签才能钉住行（[docs/install.md:L51-L54](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L51-L54) 与 [L147-L152](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L147-L152)）。

**练习 3**：一个全新环境（没装 torch）执行 `pip install fast-ulysses --index-url https://triple-mu.github.io/fast-ulysses/whl/cu13/` 会发生什么？这是 bug 吗？

**答案**：没有任何候选者的 torch 钉能被满足，pip 以 `ResolutionImpossible` 结束并列出它试过的每一行。这是**有意的失败**——这套索引的设计原则就是「不替你解析 torch」（[docs/install.md:L56-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L56-L69)）；先 `pip install torch` 即可。

### 4.2 源码构建：`--no-build-isolation` 与 `FAST_ULYSSES_CUDA_ARCH`

#### 4.2.1 概念说明

当你的 torch 不在 8 个发布行里、或者你要改 C++/CUDA 源码做开发，就需要源码构建。它不是「备胎」：本学习手册后面每一讲带你改代码观察行为，用的都是 `pip install -e .`（可编辑安装）这条路。

源码构建有两个本项目特有的关键点：

- **`--no-build-isolation` 是硬性要求**。pip 默认在隔离的临时环境里跑构建，那个环境没有你的 torch；而 fast-ulysses 的构建**必须 import 你将要链接的那份 torch**（读它的版本、让 CMake 定位 libtorch）。这不是惯例，是设计约束——违反时构建直接失败并打印修复指引，而不是抛 traceback。
- **默认编译 4 个架构（`80;90;100;120`），很慢；用 `FAST_ULYSSES_CUDA_ARCH` 只编本机架构可以快几倍**。每个架构一次独立的设备端编译过程，nvcc 还会让它们并行跑，内存消耗也按架构数放大。

#### 4.2.2 核心流程

`pip install -e . --no-build-isolation` 触发的链条：

```text
pip（不隔离，用当前环境）
  └─ setup.py: CMakeBuild.build_extension
       ├─ 读取 FAST_ULYSSES_CUDA_ARCH（默认 "80;90;100;120"）
       ├─ 转写成 torch 的点分形式 → TORCH_CUDA_ARCH_LIST（90 → "9.0"）
       ├─ cmake -S . -B build -DCMAKE_CUDA_ARCHITECTURES=…  （configure）
       ├─ cmake --build build -jN                           （N 由内存预算决定）
       └─ 生成 fast_ulysses/_build_meta.py（构建事实快照）
```

并行作业数 \(N\) 不跟随 CPU 核数，而是按内存预算：每个 nvcc 作业对每个架构各跑一个设备端 pass，峰值并发是「作业数 × 架构数」，按每个 pass 约 3 GiB 预算：

\[
N \;=\; \max\!\Big(1,\ \min\big(\text{cpus},\ \big\lfloor \mathrm{MemAvail}_{\mathrm{GiB}} \,/\, (3.0 \cdot n_{\mathrm{arch}}) \big\rfloor\big)\Big)
\]

`CMAKE_BUILD_PARALLEL_LEVEL` 环境变量可以越过这个推导。

#### 4.2.3 源码精读

**pyproject.toml 的构建声明与命令行入口：**

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"
...
[project.scripts]
fast-ulysses = "fast_ulysses.cli:main"
```

出处：[pyproject.toml:L1-L3](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L1-L3) 与 [pyproject.toml:L20-L21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L20-L21)。注意 `version` 和 `dependencies` 是 `dynamic` 的（[pyproject.toml:L12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L12)）——都由 setup.py 在构建时从真实 torch 推导。第二行就是 `fast-ulysses doctor` / `fast-ulysses wheel-url` 这两个命令的来源。包本体在 `python/` 下（[pyproject.toml:L29-L34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L29-L34)），`wheel_matrix.json` 作为包数据一起安装。

**`--no-build-isolation` 的强制与解释**——`_requires()` 里的这段注释是全项目对这个问题最完整的论证：

```python
def _requires() -> list[str]:
    """... Reading it needs the torch this will link against, so the build cannot run under pip's
    build isolation -- and torch cannot be declared a build requirement either, because pip
    would then install its own copy into the isolated environment and the extension would link
    that one instead of the user's. Turn the bare ModuleNotFoundError into the instruction."""
    try:
        import torch
    except ImportError:
        raise RuntimeError(
            "fast-ulysses builds against the torch already installed in the target environment, "
            "so it cannot be built under pip's build isolation.\n"
            ...
```

出处：[setup.py:L59-L83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L59-L83)。要点有三：运行时依赖钉死为当前 torch 的 minor（`torch=={major}.{minor}.*`）；把 torch 声明为 build requirement 也不行——pip 会在隔离环境里装自己的 torch，扩展就链接到那一份去了；构建时 import 不到 torch 就抛出带修复命令的 `RuntimeError`。文档侧的对应说明在 [docs/install.md:L169-L177](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L169-L177)。

**架构变量的消费**——`build_extension` 把 `;` 分隔的架构串同时喂给 CMake 和 torch：

```python
arch = env.get("FAST_ULYSSES_CUDA_ARCH", "80;90;100;120")
env["TORCH_CUDA_ARCH_LIST"] = " ".join(
    f"{tok[:-1]}.{tok[-1]}" for tok in arch.split(";") if tok
)
```

出处：[setup.py:L100-L108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L100-L108)。torch 要点分形式（`90` → `9.0`、`100` → `10.0`），所以插入小数点的位置是「最后一个数字之前」。同一函数里还有：持久化构建目录 `build/`（默认值可被 `FAST_ULYSSES_BUILD_DIR` 覆盖），让重复构建增量进行（[setup.py:L95-L101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L95-L101)）；configure 输出被捕获而非直接流式打印，因为 `c10d::register_work` 探测的答案只出现在这里（[setup.py:L109-L141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L109-L141)）。

**内存预算的作业数推导**（即 4.2.2 的公式）在 `_build_jobs`：

出处：[setup.py:L31-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L31-L51)。docstring 记录了动机：小内存 CI 机器如果 `-j` 只跟核数，会在四个架构的构建中途被 OOM killer 杀掉。

**构建事实快照 `_build_meta.py`**——`_write_build_meta` 把 VERSION、TORCH_VERSION、TORCH_ABI、CUDA_VERSION、PYTHON_ABI、CUDA_ARCH_LIST、HAS_WORK_REGISTRY 写成一个小 Python 模块放在扩展旁边：

出处：[setup.py:L149-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L149-L171)。注释说明了为什么必须落成文件：当 dlopen 本身失败时（恰恰是最需要这些信息的时刻），运行时函数答不出来。下一节你会看到 `doctor` 打印的就是它。

完整的构建变量表（`FAST_ULYSSES_CUDA_ARCH` / `CUDACXX` / `FAST_ULYSSES_BUILD_DIR` / `FAST_ULYSSES_CMAKE_ARGS` / `CMAKE_BUILD_PARALLEL_LEVEL`）在 [docs/install.md:L179-L185](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L179-L185)。

#### 4.2.4 代码实践

1. **实践目标**：用单架构构建把源码安装时间压缩到最短，并读懂构建输出的关键字段。
2. **操作步骤**：
   ```bash
   # 0) 前置：torch 已装（版本随意，2.10+），且有 nvcc 和 cmake ≥ 3.18
   python -c "import torch; p = torch.cuda.get_device_properties(0); print(f'{p.major}{p.minor}')"   # 本机架构，如 90
   # 1) 单架构可编辑安装（把 90 换成上一步的输出）
   FAST_ULYSSES_CUDA_ARCH=90 pip install -e . --no-build-isolation
   # 2) 构建完成后验证导入
   python -c "import fast_ulysses; print(fast_ulysses.__version__)"
   ```
3. **需要观察的现象**：
   - configure 输出里的 CUDA 架构列表只有 `90` 一项（对比不加变量时的 `80;90;100;120`）；
   - 末尾出现的 `c10d::register_work available: ON/OFF` 探测行；
   - `build/` 目录被创建，且**第二次**运行同一命令明显更快（增量构建）；
   - 生成 `python/fast_ulysses/_build_meta.py`（`copy_extensions_to_source` 把它随扩展拷回源码树，[setup.py:L143-L147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L143-L147)）。
4. **预期结果**：`import fast_ulysses` 打印 `0.2.1`（当前 VERSION 文件内容）。故意去掉 `--no-build-isolation` 再跑一次，应看到 setup.py 抛出的那段带修复指引的 `RuntimeError`，而不是裸的 ModuleNotFoundError——这正是 [setup.py:L71-L80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L71-L80) 的效果。
5. 本讲写作环境无 GPU、无 nvcc，以上均为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `torch` 写进 `[build-system] requires` 不能解决隔离构建的问题，反而更糟？

**答案**：pip 会在隔离环境里安装**它自己的**一份 torch，扩展将链接那份 torch 而不是用户环境的 torch——装出来的 wheel 对用户环境来说 ABI 依然是错的。正确做法就是 `--no-build-isolation`，让构建直接看见目标环境的 torch（[setup.py:L60-L70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L60-L70) 的 docstring）。

**练习 2**：32 核、`MemAvailable` 约 24 GiB 的机器上，默认四架构构建的 `-j` 是多少？单架构又是多少？

**答案**：按公式 \(N=\max(1,\min(32,\lfloor 24/(3\cdot n)\rfloor))\)：四架构 \(n=4\) 得 \(N=2\)；单架构 \(n=1\) 得 \(N=8\)。这正是「单架构不仅少编 4 份代码、还能提高并行度」的量化体现（[setup.py:L31-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L31-L51)）。

**练习 3**：构建报 `CMakeCache.txt directory ... is different than ...`，怎么回事？

**答案**：持久化的 `build/` 目录是从另一个路径 configure 出来的（比如仓库被移动过）。CMake 缓存里记着源码绝对路径。`rm -rf build` 后重建即可（[docs/install.md:L232-L233](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L232-L233)）。

### 4.3 torchrun 启动：跑通 quickstart 与 DiT 示例

#### 4.3.1 概念说明

fast-ulysses 是一个**集合通信算子**：一个调用必须由进程组里的所有 rank 同时发出（SPMD 契约，u1-l2 已建立）。`torchrun` 就是把一个脚本复制成 N 个 rank 的启动器。本模块把 README 的 quickstart 和 examples 各跑一遍，并特别关注**只有一张卡时怎么跑、哪些行为仍然成立**——这决定了你能不能在一台单卡机器上做本手册后面的大部分练习。

关于 `world_size=1` 有一个可以直接从源码确认的事实：**屏障被整体跳过**。传输前后各有一次标志位屏障用来同步所有 rank；`ws=1` 时没有任何 peer 可等，`fast_barrier` 直接返回。构造函数也允许 `world_size` 为 1（合法区间是 [1, 8]）。

#### 4.3.2 核心流程

quickstart 脚本每个 rank 的执行序列：

```text
torchrun 启动 N 个进程，注入 RANK / LOCAL_RANK / WORLD_SIZE
  ├─ dist.init_process_group("nccl")          # 建立 NCCL 进程组
  ├─ torch.cuda.set_device(LOCAL_RANK)        # 每个进程绑自己的卡
  ├─ UlyssesGroup()                           # 集合操作：对称内存注册（下一单元细讲）
  ├─ x = randn(b, s_local, heads*ws, d)       # 每 rank 持有序列分片×全部头
  ├─ y = group.all_to_all_4d(x, mode=0)       # → (b, s_local*ws, heads, d)
  ├─ z = group.all_to_all_4d(y, mode=1)       # → 回到 x 的形状，数值还原
  └─ group.destroy(); dist.destroy_process_group()
```

#### 4.3.3 源码精读

**README 的 quickstart**（保存为 `example.py`，`torchrun --nproc_per_node=8 example.py` 运行）：

```python
dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))
group = UlyssesGroup()

b, s_local, heads, d = 1, 4096, 8, 128
ws = dist.get_world_size()
x = torch.randn(b, s_local, heads * ws, d, dtype=torch.bfloat16, device="cuda")

y = group.all_to_all_4d(x, mode=0)        # (b, s_local * ws, heads, d)
z = group.all_to_all_4d(y, mode=1)        # back to x's shape
```

出处：[README.md:L48-L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L48-L72)。注意两个细节：头的总数写成 `heads * ws`（每 rank 只持有 `heads` 个头对应的份额语义在 u1-l2 讲过）；脚本以 `group.destroy()` 结束，它也是集合操作。紧随其后的警告「Every rank must issue the same sequence of shapes」（[README.md:L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L74)）是全手册最重要的一句话——新形状的首次调用触发集合性的窗口分配，rank 间分歧的后果是挂起而非报错。

**DiT 注意力示例的启动方式与自校验结构**：

出处：[README.md:L76-L84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L76-L84) 给出命令 `torchrun --nproc_per_node=8 examples/dit_attention.py`。脚本自身的 docstring 说明了它的双重自检：与「完全不序列并行」的单卡参照比较输出**和输入梯度**，再打印几个优化器步的 loss（[examples/dit_attention.py:L1-L17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L1-L17)）。模块主体就是 u1-l2 讲过的三明治：qkv 打包 → mode 0 → SDPA → mode 1 → proj（[examples/dit_attention.py:L45-L63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L45-L63)）。逐段精读留给 u2-l4，本讲只负责跑通它。

**`world_size=1` 跳过屏障的证据**：

```cpp
void fast_barrier(cudaStream_t stream, const std::vector<uint64_t>& flag_ptrs, int rank)
{
    const int ws = static_cast<int>(flag_ptrs.size());
    if (ws == 1)
        return;
```

出处：[src/barrier.cu:L53-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L57)。没有 peer 就没有发布/等待，屏障内核根本不发射。而构造函数一侧对 world_size 的合法域检查：

```cpp
TORCH_CHECK(world_size_ >= 1 && world_size_ <= 8, "world_size must be in [1, 8] ...")
```

出处：[src/group.cc:L151-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L151-L153)——1 是一等公民，不是边界特例。

**无 GPU 环境的边界**：测试体系里 host-only 的布局测试不需要 GPU（`pytest -m "not multigpu"`，[README.md:L156-L159](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L156-L159)），但 quickstart 与 examples 都要真实 CUDA 设备（`device="cuda"` 且走 NCCL）。

#### 4.3.4 代码实践

1. **实践目标**：在可用的 GPU 数量 N（可为 1）下跑通两个脚本，并记录 `ws=1` 时哪些行为仍然成立。
2. **操作步骤**：
   ```bash
   # 把 README quickstart 存成 example.py 后：
   torchrun --nproc_per_node=<N> example.py           # N = 你的卡数；单卡就写 1
   torchrun --nproc_per_node=<N> examples/dit_attention.py
   # 单卡运行后，再做一次观测：
   python -c "
   import torch
   print(torch.cuda.device_count())                    # 确认确实只有 1 张卡"
   ```
   在 `example.py` 末尾临时加一行（不改库源码，只改你自己的脚本）：
   ```python
   print(rank_ok := torch.equal(x, z), flush=True)     # ws=1 时往返是否位精确还原
   ```
3. **需要观察的现象**：
   - `ws=1` 时 `x` 形状 `(1, 4096, 8, 128)`（`heads*ws=8`），`y` 为 `(1, 4096, 8, 128)`，`z` 还原；
   - 两个脚本都正常退出、无挂起；
   - `dit_attention.py` 打印与单卡参照的输出/梯度比对结果和若干步 loss。
4. **预期结果**：
   - `ws=1`：一切正常退出。可以从源码断言的部分：屏障被 `fast_barrier` 直接跳过（[src/barrier.cu:L56-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57)）；`world_size=1` 在构造函数的合法域内（[src/group.cc:L151-L153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L151-L153)）；mode 0 + mode 1 往返位精确（u1-l2 的结论，`ws=1` 同样成立）。**不能**从源码断言、需实测的部分：传输路径是否仍有窗口分配与拷贝（应仍有——这属于待本地验证的观测点）。
   - 多卡时 `torchrun --nproc_per_node=8`：`x` 为 `(1, 4096, 64, 128)`，`y` 为 `(1, 32768, 8, 128)`。
5. 本讲写作环境无 GPU：以上运行为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：quickstart 里为什么必须 `torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))`，而单进程脚本通常不用？

**答案**：torchrun 起 N 个进程共用同一台机器，默认都操作 0 号卡会互相踩踏。`LOCAL_RANK` 是「本进程在本节点内的编号」，set_device 把每个进程的默认 CUDA 上下文绑到自己的卡。单进程只有一个上下文，通常不需要。

**练习 2**：把 quickstart 里 `heads` 从 8 改成 3，让**偶数 rank 用 3、奇数 rank 用 4**，会发生什么？

**答案**：各 rank 发出的形状序列不同，而新形状的首次调用触发集合性的窗口分配——某个 rank 在分配点等其他人，表现为**挂起而非报错**（[README.md:L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L74)、[docs/quickstart.md:L90-L94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md#L90-L94)）。这是 u1-l2 所讲 SPMD 契约的运行期体现。

**练习 3**：`ws=1` 时 `group.all_to_all_4d(x, mode=0)` 返回的张量与 `x` 是什么关系？

**答案**：形状上，`(b, s, n, d)` → `(b, s, n, d)`（`s_local*ws = s_local`、`heads = heads*ws/ws`）；数值上仍是同一个重排语义（序列分片×全部头 ⇄ 全序列×头分片在 ws=1 时退化为恒等重排）。屏障环节被整体跳过，但调用本身照常执行（不抛错、不挂起）。

### 4.4 doctor 自检：读懂三段输出

#### 4.4.1 概念说明

`fast-ulysses doctor` 回答一个问题：**这台机器能不能跑这个算子**。它不做任何集合通信，只是把「不拉起进程组就能确定的事实」打出来：扩展加载情况、构建事实、设备与架构、以及两两 NVLink 连接矩阵。文档明确它的定位：绿色矩阵只是**必要条件**，不替代 `UlyssesGroup` 构造函数在实际进程组上做的检查。

之所以把 NVLink 当作门槛，是因为这个传输方式直接写 peer 的显存地址——没有 NVLink（比如走 PCIe）时带步长拷贝性能急剧劣化，`torch.distributed` 反而更快，所以构造函数会拒绝这样的组。

#### 4.4.2 核心流程

`doctor` 的输出分四段：

```text
extension: /path/to/_C.so          # ① 扩展加载成功与否
  version / torch_abi / cuda_version / python_abi / cuda_arch_list / has_work_registry ...
devices: N                         # ② 可见卡数与每张卡的架构、显存
  [0] NVIDIA H100  sm_90  79 GiB   #    不在本次构建架构里的卡会被标出
nvlink, row=from col=to:           # ③ 两两 NVLink 矩阵（. 对角 / y 有 / N 无）
     0   1   2 ...
NOT NVLink-joined: ...             # ④ 结论：全部互连 / 有缺失对 / NVML 答不了
```

退出码：正常（全互连或单卡）为 0；扩展加载失败、无可见设备、存在非 NVLink 对为 1；NVML 无法回答拓扑时仍为 0（不拒绝、不背书——fail-open）。

#### 4.4.3 源码精读

**docstring 里的定位声明**——必要条件而非替代：

> "This reports what can be established without a process group -- the build, the devices, the NVLink matrix -- so a green matrix is a necessary condition, not a substitute for the check UlyssesGroup's constructor makes over the actual group."

出处：[python/fast_ulysses/cli.py:L1-L13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L1-L13)。

**① 构建段**：扩展路径与 `_build_meta` 的逐项打印：

```python
print(f"extension: {pkg._C.__file__}")
from fast_ulysses._diagnose import build_meta
for key, value in sorted(build_meta().items()):
    print(f"  {key.lower()}: {value}")
```

出处：[cli.py:L38-L49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L38-L49)。`build_meta` 读的正是 4.2.3 里 setup.py 生成的 `_build_meta.py`——即使 dlopen 失败它也在磁盘上。若扩展本身加载失败，这里打印 `extension: FAILED TO LOAD` 并返回 1。注意 `doctor` 依赖一次成功的加载（`fast-ulysses` 脚本先 import 包再进 main，而包的 `__init__` 在 `_C` 加载失败时会把 loader 错误转成诊断报告后 re-raise，[python/fast_ulysses/__init__.py:L3-L10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L3-L10)）。

**② 设备段**：每张卡的架构与「是否在本次构建里」的标记：

```python
arch = f"{p.major}{p.minor}"
mark = "" if arch in built else "  <-- NOT IN THIS BUILD"
print(f"  [{i}] {p.name}  sm_{arch}  {p.total_memory >> 30} GiB{mark}")
```

出处：[cli.py:L57-L70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L57-L70)。`built` 从扩展的 `build_info()` 读架构列表、只取前导数字（`90a` 与 `90` 视作同一张卡，[cli.py:L60-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L60-L64)）。这个标记直接衔接 4.2 的实践：如果你用 `FAST_ULYSSES_CUDA_ARCH=90` 只编了 sm_90，机器上的 sm_89 卡会在这里被点出来。

**③④ 矩阵段与三种结论**：

出处：[cli.py:L72-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L72-L103)。矩阵来自 `pkg._C.nvlink_matrix(list(range(n)))`；`None` 表示 NVML 答不了（打印「UlyssesGroup will build anyway; nothing here can say whether it should」并返回 0）；有缺失对时列出前 4 个并建议该处用 `torch.distributed`（返回 1）；单卡时专门打印「one visible device: no pair to check, and nothing above world_size 1 to run」；全互连则宣告「all N devices are mutually NVLink-joined」。NVLink 探测的实现细节在 u8-l1 展开，本讲只消费它的输出。

#### 4.4.4 代码实践

1. **实践目标**：用 `doctor` 判定当前机器，并把每段输出与上面 ①—④ 对上。
2. **操作步骤**：
   ```bash
   fast-ulysses doctor
   nvidia-smi topo -m          # 交叉验证 NVLink 矩阵（GPU 间应显示 NV# 而非 PIX/PHB/SYS）
   echo $?                     # 看 doctor 的退出码
   ```
3. **需要观察的现象**：
   - `extension:` 指向你 4.2 实践装出来的 `_C.so`；`cuda_arch_list` 是否只含你指定的架构；
   - 每张卡的 `sm_XX` 是否带 `NOT IN THIS BUILD` 标记；
   - 矩阵对角线为 `.`、其余为 `y` 或 `N`；与 `nvidia-smi topo -m` 一致；
   - 退出码与结论四选一：全互连 / 有缺失对 / 单卡 / NVML 答不了。
4. **预期结果**：
   - 单卡机器：打印「one visible device …」，退出码 0——能跑 `world_size=1`，没有更高可跑；
   - 无 GPU 机器：`devices: none visible to CUDA`，退出码 1——只能做 host-only 测试（`pytest -m "not multigpu"`）；
   - 多卡全 NVLink：`all N devices are mutually NVLink-joined`，退出码 0。
5. 本讲写作环境无 GPU：以上为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`doctor` 显示全绿矩阵，但 `UlyssesGroup()` 仍可能拒绝这个组。举一种情形。

**答案**：`doctor` 看的是「这台机器所有卡」两两之间的 NVLink；`UlyssesGroup` 检查的是「这个进程组实际选中的设备」集合（例如 `CUDA_VISIBLE_DEVICES` 或子组选出的子集落在不同拓实体上）。docstring 明说绿色矩阵是必要条件、不替代构造函数在实际组上的检查（[cli.py:L1-L13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L1-L13)）。另一种情形：两个 rank 共享同一张卡时构造函数会走不同的检查路径（u2-l1 细讲）。

**练习 2**：`doctor` 打出 `NVML could not report the link topology` 却返回 0（绿色退出码）。这是 bug 吗？

**答案**：不是。这是 fail-open 语义：探测无法回答时既不背书也不拒绝，`UlyssesGroup` 照常构造，判断权留给用户（[cli.py:L72-L76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L72-L76)，另见 [docs/install.md:L219-L221](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L219-L221)）。返回 0 表示「没有可报告的坏消息」，不等于「确认可用」。

**练习 3**：`doctor` 的设备段某张卡后面出现 `<-- NOT IN THIS BUILD`，运行时会怎样？怎么修？

**答案**：扩展里没有该架构的设备码，在这张卡上触发算子时会失败。修复就是重装：源码构建时把该卡的架构加进 `FAST_ULYSSES_CUDA_ARCH`（或直接用默认的 `80;90;100;120`；wheel 则本来按平台发布）。标记的判定逻辑在 [cli.py:L66-L70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L66-L70)。

## 5. 综合实践

**任务：从零到第一次成功运行的全链路记录。** 在一台你能拿到的机器上（单卡即可），完成下面 6 步，并把每步的**实际输出**记进一个 `setup-log.md`（放在你自己的目录，不要写进仓库）：

1. **环境定位**：`python -c "import sys,torch; print(torch.__version__, torch.version.cuda, sys.version_info[:2])"`，在行表里查出你的行标签。
2. **选择安装路径**并执行其一：
   - wheel：`pip install fast-ulysses --index-url https://triple-mu.github.io/fast-ulysses/whl/cu13/`（或 `cu12`）；
   - 源码：先 `python -c "import torch; p=torch.cuda.get_device_properties(0); print(f'{p.major}{p.minor}')"`，再 `FAST_ULYSSES_CUDA_ARCH=<该值> pip install -e . --no-build-isolation`。
3. **自检**：`fast-ulysses doctor`，逐段抄录输出，标注 ①—④ 各对应哪几行，写明退出码。
4. **首跑**：把 README quickstart 存成 `example.py`，`torchrun --nproc_per_node=<卡数> example.py`，确认正常退出。
5. **示例**：`torchrun --nproc_per_node=<卡数> examples/dit_attention.py`，抄录输出/梯度比对与 loss 行。
6. **对照实验（单卡者必做，多卡者选做）**：把 `--nproc_per_node` 改成 1 再跑一遍第 4、5 步，在日志里写三行结论：(a) 脚本是否照常成功；(b) 依据 [src/barrier.cu:L56-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57) 说明屏障发生了什么；(c) 往返 `torch.equal(x, z)` 是否成立。

完成标准：日志里 ①—④ 每段都有真实输出对应；第 6 步的三个结论各有一句源码依据或实测证据。零 GPU 机器的替代版：完成第 1、2（源码路径需要 nvcc，不可用时改为纸面推演并注明）、3（记录 `devices: none visible to CUDA`），并跑 `pytest -m "not multigpu"` 作为本讲的收尾验证。

## 6. 本讲小结

- wheel 按「torch minor × CUDA major」两个轴发布（8 行 × 4 CPython + PyPI 单行）；扩展嵌入 `c10d::Work` 虚表、pybind11 模块与 `TORCH_LIBRARY` 注册，torch minor 一变就废，CUDA major 一变就找不到 `libcudart`。
- 选 wheel 首选 `--index-url` 总索引（只含本包，靠依赖回退解析出你的 torch minor）；裸 PyPI 安装与不带标签的 `--find-links` 都会**静默**把 torch 换成 2.13——PEP 440 给本地标签排序是根源，`fast-ulysses wheel-url` 帮你把版本写全。
- 源码构建的两个要点：`--no-build-isolation` 是硬性要求（构建必须 import 并链接你环境里的那份 torch，把它声明成 build requirement 反而会链接错）；`FAST_ULYSSES_CUDA_ARCH` 单架构构建既少编又提高并行度（作业数按「3 GiB × 并发 pass」的内存预算推导）。
- `torchrun --nproc_per_node=N` 跑 quickstart 与 `examples/dit_attention.py`；`world_size=1` 是合法一等公民，屏障在 `ws==1` 时被整体跳过，单卡机器也能做本手册大部分观察实验。
- `fast-ulysses doctor` 输出四段（扩展/构建事实/设备/NVLink 矩阵），绿色矩阵是必要条件而非替代；NVML 答不出时 fail-open，有缺失对时建议改用 `torch.distributed`。
- 集合调用的纪律从第一次运行就生效：所有 rank 必须发出相同形状序列，违反的后果是挂起而非报错。

## 7. 下一步学习建议

- **下一讲（u1-l4）**：仓库结构与阅读地图——`docs/develop.md` 的目录导航、`a2a_plan.cc` 为什么刻意写成无 CUDA/无 torch，以及从 `python/fast_ulysses/group.py` 到 `src/transfer.cu` 的完整调用链骨架。装好环境的你现在有条件边读边跑了。
- **本单元收尾（u2-l1 起）**：进入 Python API——`UlyssesGroup` 构造参数、NVLink 检查的失败开放语义、子组支持；本讲 4.4 的 `doctor` 输出会在那里从「消费者视角」翻到「实现视角」（`src/nvlink.cc`，u8-l1）。
- **延伸阅读**：[docs/install.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md) 的「When the import fails」与「Other problems」两节（NCCL NVLS 报错、CMake 缓存路径冲突），以及 [docs/quickstart.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/quickstart.md) 的「What will hang you」——排障时先翻这两处。
