# 安装、构建与第一次运行

## 1. 本讲目标

学完本讲，你应该能够：

1. 根据自己环境里 torch 的 **minor 版本**和 torch 所链接的 **CUDA 大版本**，从 36 个发布 wheel 中选出唯一正确的那一个（或判断「没有对应的 wheel，需要源码构建」）。
2. 用 `FAST_ULYSSES_CUDA_ARCH=<架构>` 加 `pip install -e . --no-build-isolation` 完成一次快速源码构建，并能解释 `--no-build-isolation` 为什么不是可选项。
3. 用 `torchrun --nproc_per_node=<N>` 跑通 README 的 quickstart 脚本和 `examples/dit_attention.py`，读懂它们各自打印的自检输出；没有多卡时知道 `--nproc_per_node=1` 下哪些行为仍然成立。
4. 运行 `fast-ulysses doctor` 并逐段解读它的输出（构建信息、设备列表、NVLink 矩阵），知道它证明什么、不证明什么。

## 2. 前置知识

- **wheel 与 ABI**：Python 的 wheel 就是打包好的安装文件（`.whl`）。纯 Python 代码的 wheel 到处能跑；但包含 C++/CUDA 编译产物（`.so`）的 wheel 只能在「编译时所依赖的二进制接口（ABI）」匹配的环境里运行。fast-ulysses 的 `.so` 深度嵌入 torch 的 C++ 内部（后面 4.1 会展开），所以 torch 换一个 minor 版本它就失效——这是本讲一切安装规则的根源。
- **PEP 440 与本地版本（local version）**：Python 包版本规范允许 `1.0+label` 这种「本地版本」写法。pip 在比较版本时会把 `+` 后面的标签纳入排序，而**不带标签的版本说明符（如 `==0.2.0`）会忽略标签**，于是 `0.2.0` 能匹配 `0.2.0+任何标签` 的所有文件并选最高的——这是本项目安装文档反复警告的「静默选错 wheel」陷阱。
- **torchrun 与 SPMD**：`torchrun` 是 PyTorch 自带的启动器，帮你以相同脚本拉起 N 个进程（rank 0..N-1），并注入 `RANK`、`WORLD_SIZE`、`LOCAL_RANK` 等环境变量。所有 rank 执行同一份代码（SPMD，单程序多数据），集合通信要求每个 rank 发出相同的调用序列——u1-l2 已经建立了这个契约。
- **CUDA 计算能力（arch / sm_XX）**：NVIDIA GPU 按架构命名，如 A100 是 sm_80、H100 是 sm_90、B200 是 sm_100、RTX 50 系是 sm_120。CUDA 扩展编译时必须指定要生成哪些架构的机器码；给不支持的架构装上 `.so`，会在第一次启动 kernel 时报 `no kernel image is available`。
- **build isolation**：pip 默认在一个隔离的临时环境里执行构建，只安装 `pyproject.toml` 的 `[build-system]` 声明的构建依赖。这对纯 Python 包很干净，但对「必须用用户环境里那份 torch 来编译」的扩展是致命的——本讲 4.2 会看到项目如何处理这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md) | 安装一节的速览（[L123-L152](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L123-L152)）：匹配规则、四条安装命令、PEP 440 陷阱提示；quick start 脚本（[L48-L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L48-L74)） |
| [docs/install.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md) | 安装的完整论证：36 行 wheel 的匹配表、三种安装途径、版本必须写全的原因、构建变量表、导入失败与建组失败的排查 |
| [setup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py) | 源码构建的全部逻辑：torch 必须可导入的强制检查、CMake 调用、`FAST_ULYSSES_CUDA_ARCH` 的传递、构建元数据 `_build_meta.py` 的生成 |
| [pyproject.toml](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml) | 构建系统声明（注意：**没有** torch）、`fast-ulysses` 命令行入口、pytest 的 `multigpu` 标记 |
| [python/fast_ulysses/cli.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py) | `doctor` 与 `wheel-url` 两个子命令的实现 |
| [python/fast_ulysses/__init__.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py) | 包导入顺序：先 torch 后 `_C`，失败时转交 `_diagnose.explain` 生成诊断报告 |
| [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py) | 第一次运行的完整示例：序列并行注意力块，前向 + 反向 + 与单卡参照对拍 + 5 个优化器步 |
| [CMakeLists.txt](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt) | 构建的 C++ 侧：架构列表的快照时序、Blackwell 需要 CUDA 12.8 的守卫（辅助阅读） |

## 4. 核心概念与源码讲解

### 4.1 wheel 选择：torch minor × CUDA major 的匹配规则

#### 4.1.1 概念说明

fast-ulysses 不是纯 Python 包：它编译出一个 `_C.so`，里面注册了 torch 算子（`TORCH_LIBRARY`）、一个 pybind11 模块，还嵌入了 `c10d::Work` 的 vtable。这些东西都依赖 torch 的 **C++ ABI**——torch 每升一个 minor 版本，这些内部符号就可能变化，`.so` 就加载不起来。同时它只从 CUDA 工具链链接 `libcudart.so.<大版本>`，所以 CUDA 大版本也必须一致。

结论：**一次发布 = 8 个 (torch minor, CUDA major) 组合 × 4 个 CPython 版本 = 36 个 wheel**，外加 PyPI 上 4 个不带标签的。你要做的就是确定自己属于哪一行。

#### 4.1.2 核心流程

确定自己所在行的流程：

1. 先装好你想用的 torch（顺序不能反，见下面的「解析」机制）。
2. 打印三个事实：torch 版本、torch 的 CUDA 版本、Python 版本：

   ```bash
   python -c "import sys,torch; print(torch.__version__, torch.version.cuda, sys.version_info[:2])"
   ```

3. 用 torch 的 **minor**（如 2.11.3 → 2.11）× torch 的 CUDA **大版本**（如 13.2 → 13）查表定行。
4. 选择安装途径，按优先级：
   - **按 CUDA 大版本选索引**（推荐）：`--index-url .../whl/cu13/` 或 `.../whl/cu12/`；
   - **按整行选索引**：`--index-url .../whl/torch211cu128/`（一行四轮，旁边还要装别的 PyPI 包时用这个）；
   - **PyPI**：只有 `torch213cu130` 一行去了那里；
   - **`fast-ulysses wheel-url`**：打印当前环境对应的完整安装命令。

**为什么索引机制能防止选错**：`whl/cu13` 索引里只有 fast-ulysses 一个项目、没有 torch，pip 无法靠装一个新 torch 来满足依赖，只能从候选里回溯，直到某个 wheel 的 `Requires-Dist: torch==2.11.*` 被你已装的 torch 满足——行就这么被「解析」出来，而不是被你手工挑出来。这也解释了两条纪律：

- **先装 torch**。没有 torch 时任何候选都不满足，pip 以 `ResolutionImpossible` 结束并列出它试过的每一行——这是设计好的失败，修复就是先 `pip install torch`。
- **不要加 `--extra-index-url https://pypi.org/simple`**。那等于给了 pip 一个可装的 torch，第一个候选（最高标签 `torch213cu130`）就能解析成功——代价是把你的 torch 换成 2.13。

**PEP 440 陷阱（必须理解）**：同一版本号下，本地标签有全序：

```
0.2.0 < +torch210cu128 < +torch210cu130 < +torch211cu128 < +torch211cu130
      < +torch212cu129 < +torch212cu130 < +torch213cu129 < +torch213cu130
```

于是在任何同时暴露多行的文件集（`--find-links`、`--extra-index-url`、本地 wheelhouse 目录）上，不带标签的 `pip install fast-ulysses` 乃至 `==0.2.0` 都会**静默**选中 `+torch213cu130`。所以用 `--find-links` 时必须把标签写全：`"fast-ulysses==0.2.0+torch210cu128"`。

#### 4.1.3 源码精读

匹配规则的「为什么」在安装文档开篇，[docs/install.md:L15-L24](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L15-L24) 说明 36 个 wheel 的构成，并给出确定自己所在行的那条命令——这段同时点出了 `.so` 嵌入的三样东西（`c10d::Work` vtable、pybind11 模块、`TORCH_LIBRARY` 注册）「都活不过一次 minor 升级」，这就是 ABI 钉死的全部理由。

[docs/install.md:L26-L38](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L26-L38) 是行表：torch minor（2.10–2.13）× torch 的 CUDA（12.x / 13.x）→ 标签。注意 12.x 那列的 CUDA 小版本随 torch 走（2.10/2.11 是 cu128，2.12/2.13 是 cu129），但**匹配只看大版本**。

[docs/install.md:L40-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L40-L69) 解释索引安装与回溯解析，末尾的 `ResolutionImpossible` 示例输出就是「没装 torch」时你会看到的东西。[docs/install.md:L71-L84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L71-L84) 给出禁用 `--extra-index-url` 的理由和按行的索引。

[docs/install.md:L127-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L127-L145) 是 PEP 440 排序与「版本必须写全」的论证，包含上面那个全序链和三条对照命令。[docs/install.md:L114-L124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L114-L124) 说明 PyPI 只有一行且「在任何其他 torch 上这条命令不会失败」——pip 会直接把 torch 升到 2.13 来满足 pin。

README 的速览版在 [README.md:L125-L138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L125-L138)（要求与按 CUDA 大版本选索引），四条命令在 [README.md:L140-L144](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L140-L144)，PyPI 警告在 [README.md:L146-L152](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L146-L152)。

匹配逻辑的代码形态是 `wheel-url` 子命令里的 `_match()`（[python/fast_ulysses/cli.py:L130-L184](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L130-L184)）：按 machine → python → torch minor → CUDA major 的顺序逐轴检查，任何一轴没有 wheel 就返回一段指明该轴与出路的文字。本讲只需会用它，逐行精读留给 u8-l3。

最后，「装错了会发生什么」的闭环在 [python/fast_ulysses/__init__.py:L3-L10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L3-L10)：导入时先 `import torch`（让 libtorch 先加载），再导入 `_C`；失败则把加载错误转交 `_diagnose.explain`，生成「编译时 vs 现装」的对照报告再抛出。torch minor 不匹配的典型症状是 `undefined symbol: _ZN3c10...`，CUDA 大版本不匹配是 `libcudart.so.12: cannot open shared object file`（[docs/install.md:L187-L207](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L187-L207)），两者的修复是同一个：卸载、按索引重装。

#### 4.1.4 代码实践

1. **实践目标**：确定当前环境对应的 wheel 行，并用工具交叉验证。
2. **操作步骤**：
   - 运行 `python -c "import sys,torch; print(torch.__version__, torch.version.cuda, sys.version_info[:2])"`；
   - 手工查 docs/install.md 的行表，写出你的标签（如 `torch211cu130`）；
   - 若已装本库：运行 `fast-ulysses wheel-url`，看它打印的 `row:` 行是否与你查的一致；
   - 若本库导入失败：直接读导入时抛出的报告——它已经把「编译时用的」和「现在装的」两边都写出来了。
3. **需要观察的现象**：`wheel-url` 第一行 `running: torch x.y.z, cuda a.b, cpXY, x86_64`；随后是 `row:` 行或一段「no wheel for …」的说明。
4. **预期结果**：手工查表与 `wheel-url` 结论一致。（本讲写作环境无法替你运行，`待本地验证`。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pip install fast-ulysses --index-url .../whl/cu13/` 不需要写版本标签，而 `--find-links` 必须？

答案：索引里只有 fast-ulysses 一个项目，pip 装不了别的 torch，只能回溯到 `Requires-Dist` 被当前 torch 满足的那一行，行是被解析出来的；`--find-links` 暴露全部 36 个 wheel，PEP 440 对不带标签的说明符按「忽略标签、取最高」处理，于是永远选中 `+torch213cu130`，与你的 torch 无关且无任何警告。

**练习 2**：环境里是 torch 2.12.4 + CUDA 13.1。装完发现 `import fast_ulysses` 报 `undefined symbol: _ZN3c10...`。最可能发生了什么？如何修复？

答案：装的 wheel 行与 torch minor 不符（比如被 PyPI/`--find-links` 静默换成了 `torch213cu130`），或事后 torch 被挪动过。修复顺序：先 `python -c "import torch; ..."` 确认 torch 还是你选的那个（不是就先重装 torch），`pip uninstall -y fast-ulysses`（不可省，否则 pip 报 `Requirement already satisfied` 留着坏轮），再按 `whl/cu13` 索引重装。

**练习 3**：为什么匹配只看 CUDA 大版本，不看小版本？

答案：扩展从 CUDA 工具链只链接 `libcudart.so.<大版本>`，其余什么都没用（`_match` 的 docstring 在 [python/fast_ulysses/cli.py:L130-L137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L130-L137) 明说了这是刻意为之，且 `check_wheel.py` 断言的恰好就是这么多）。小版本不影响这条链接关系，所以 12.8/12.9 共用 `cu12`。

### 4.2 源码构建：FAST_ULYSSES_CUDA_ARCH 与 --no-build-isolation

#### 4.2.1 概念说明

当你的 torch 不在 36 行表里（比如刚发布的 torch minor），或者你改了源码，就走源码构建。它有两条铁律：

1. **`--no-build-isolation` 是必须的**。构建过程中 CMake 要通过「已安装的 torch」找到 libtorch 的头文件与库，`setup.py` 还要读出该 torch 的版本来生成运行时依赖 pin——所以构建用的 torch 必须就是你将来运行用的那一份。pip 的构建隔离会另装一份 torch 进临时环境，扩展就会链接那一份而不是你的。
2. **默认编译四个架构（80;90;100;120）很慢**，用 `FAST_ULYSSES_CUDA_ARCH` 只编你机器的架构能快几倍。

#### 4.2.2 核心流程

`pip install -e . --no-build-isolation` 触发的链条：

```
pip（不做隔离，直接用当前环境的 torch）
  └─ setup.py 的 CMakeBuild.build_extension()
       ├─ 1. _requires()：import torch，读 minor，生成 install_requires=["torch==2.11.*"]
       │      （import 失败 → RuntimeError，打印「先装 torch + --no-build-isolation」的指引）
       ├─ 2. 确定 build 目录（默认 ./build，持久保留 → 增量编译）
       ├─ 3. 读 FAST_ULYSSES_CUDA_ARCH（默认 "80;90;100;120"），
       │      转成 torch 的点分形式写入 TORCH_CUDA_ARCH_LIST（90 → 9.0）
       ├─ 4. cmake -S . -B build -DCMAKE_CUDA_ARCHITECTURES=<arch> ...
       │      （CMake 侧：project() 前快照 arch 请求；若含 Blackwell 而 nvcc < 12.8 → FATAL_ERROR）
       ├─ 5. cmake --build build -j<jobs>
       │      jobs = min(CPU 数, MemAvailable / (3 GiB × 架构数))   ← 受内存约束，不是核数
       └─ 6. 生成 _build_meta.py：记录版本、torch、CUDA、arch 列表等构建事实
```

关于并行度：每个编译任务 fork 一个 nvcc，而 nvcc 的 `-t0` 会让它对**每个架构**各跑一个 device 编译 pass，所以峰值并发 = 任务数 × 架构数，峰值内存随之增长。按每个并发 pass 约 3 GiB 预算：

\[ \text{jobs} = \max\!\left(1,\ \min\!\left(\text{cpus},\ \left\lfloor \frac{\text{MemAvailable (GiB)}}{3.0 \times n_{\text{arch}}} \right\rfloor\right)\right) \]

这就是为什么小内存 CI 机器上四架构构建会被 OOM 杀掉——也为什么单架构构建快得多（不只少编三份，还允许更高并行度）。`CMAKE_BUILD_PARALLEL_LEVEL` 环境变量可越过这个推导。

#### 4.2.3 源码精读

**为什么构建不能隔离**，最清楚的表述是 `_requires()` 的 docstring：[setup.py:L59-L83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L59-L83)。它解释了三层：pin 必须精确到 minor（ABI）；读 pin 需要导入将链接的 torch（所以不能隔离）；也不能把 torch 声明成构建依赖（pip 会在隔离环境里装自己的 torch，扩展就链接那一份）。`try: import torch / except ImportError: raise RuntimeError(...)` 把裸的 `ModuleNotFoundError` 变成了带三条命令的指引——你在隔离模式下忘记 `--no-build-isolation` 时看到的就是这段话。

对应地，[pyproject.toml:L1-L3](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L1-L3) 的 `[build-system]` 只有 setuptools，**没有 torch**——这是上面第二条禁令的落地。

**构建主流程**在 [setup.py:L91-L141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L91-L141)：

- [setup.py:L100-L101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L100-L101)：持久 build 目录（默认 `./build`，`FAST_ULYSSES_BUILD_DIR` 可改），让重构建走增量；CMake 在 `-D` 参数变化时自动重新 configure，不用手工清。
- [setup.py:L103-L108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L103-L108)：读 `FAST_ULYSSES_CUDA_ARCH`（默认 `80;90;100;120`），并做 `90 → 9.0`、`100 → 10.0` 的点分转换写进 `TORCH_CUDA_ARCH_LIST`——torch 的 CMake 模块吃点分形式，而 CMake 与本库约定吃整数形式。
- [setup.py:L112-L131](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L112-L131)：cmake configure 命令本身，`-DCMAKE_CUDA_ARCHITECTURES=<arch>` 就在这里传下去；configure 输出被捕获而非直接流式打印，是为了从里面解析 `c10d::register_work available: ON` 这个探针状态行（[setup.py:L137-L141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L137-L141)），它决定异步路径的运行时行为（u7-l3 展开）。
- [setup.py:L135-L136](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L135-L136)：jobs 取 `CMAKE_BUILD_PARALLEL_LEVEL` 或 `_build_jobs(arch)` 的推导，公式来自 [setup.py:L31-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L31-L51) 的 `/proc/meminfo` 读取（每并发 pass 3 GiB 预算，读不到 MemAvailable 时退回 CPU 数）。

**CMake 侧的两个细节**（本讲了解即可，u8-l2 精读）：[CMakeLists.txt:L7-L27](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L7-L27) 解释了为什么架构请求要在 `project()` **之前**快照（`project(LANGUAGES CUDA)` 总会把它定义成编译器默认值 52，之后再测就分不清「没要求」和「要求了 52」），以及为什么要在 `find_package(Torch)` **之后**再快照一次（Torch 会把它设成 `OFF` 改用自己的 `TORCH_CUDA_ARCH_LIST` 驱动）。[CMakeLists.txt:L40-L70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L40-L70) 是 Blackwell 守卫：请求 sm_100/sm_120 而 nvcc 低于 12.8 时直接 `FATAL_ERROR`，报错里给出了退路（`FAST_ULYSSES_CUDA_ARCH="80;90"`）——否则你会得到一个能加载但第一个 kernel 就报 `no kernel image` 的 `.so`。

最后，[setup.py:L149-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L149-L171) 把构建事实（版本、torch ABI、CUDA 版本、arch 列表、是否有 work registry）写成 `_build_meta.py` 放在扩展旁边——因为当 `.so` 本身 dlopen 失败时，`build_info()` 无法回答，而这个纯 Python 文件还能读。`doctor` 的第一段输出就来自它（4.4）。

构建变量速查（完整表在 [docs/install.md:L179-L185](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L179-L185)）：

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `FAST_ULYSSES_CUDA_ARCH` | 目标架构，`;` 分隔 | `80;90;100;120` |
| `CUDACXX` | CUDA 编译器 | `/usr/local/cuda/bin/nvcc` |
| `FAST_ULYSSES_BUILD_DIR` | CMake 构建树 | `./build`（保留，增量） |
| `FAST_ULYSSES_CMAKE_ARGS` | 透传给 CMake 的额外 flag | 空 |
| `CMAKE_BUILD_PARALLEL_LEVEL` | 覆盖任务数推导 | 按内存预算 |

#### 4.2.4 代码实践

1. **实践目标**：完成一次单架构源码构建，并观察 arch 变量如何一路传到 CMake。
2. **操作步骤**：
   - 先确认 `nvcc` 可用且 `python -c "import torch"` 正常（torch 版本就是你打算运行用的）；
   - 用 `nvidia-smi --query-gpu=compute_cap --format=csv` 或 `torch.cuda.get_device_properties(0)` 得到本机架构（如 H100 → `90`）；
   - 执行 `FAST_ULYSSES_CUDA_ARCH=90 pip install -e . --no-build-isolation`；
   - 对照观察：configure 输出中的 arch 列表、`c10d::register_work available: ...` 状态行；构建结束后 `python -c "import fast_ulysses; print(fast_ulysses.__version__)"`（当前仓库 VERSION 为 `0.2.1`，见 [setup.py:L22-L28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L22-L28) 对唯一版本字面量的说明）；
   - 再删掉 `build/` 后不带环境变量重构建一次，对比耗时差异（四架构 vs 单架构）。
3. **需要观察的现象**：单架构构建的 nvcc 调用次数明显更少、总耗时显著下降；两次构建后 import 均成功。
4. **预期结果**：`fast_ulysses.__version__` 打印 `0.2.1`（可编辑安装不带本地标签）。`待本地验证`（本讲环境无 nvcc/GPU，无法代跑）。
5. **故意触发一次错误**（可选，加深印象）：临时在一个隔离环境里 `pip install .`（不带 `--no-build-isolation`）——你应该看到 `_requires()` 抛出的那段指引文字而不是裸 traceback（[setup.py:L74-L80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L74-L80)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能把 `torch` 写进 `pyproject.toml` 的 `[build-system] requires` 来解决「构建时 torch 必须在场」？

答案：那会让 pip 在隔离环境里安装**它自己解析的** torch，扩展链接的就是那份而不是用户环境的 torch——恰好制造了本要避免的 ABI 错配。正确做法是禁用隔离、强制使用用户环境，并把忘记时的失败变成一条可执行的指引。

**练习 2**：一台 8 核、MemAvailable 约 12 GiB 的机器上做四架构构建，jobs 是多少？单架构又是多少？

答案：四架构：\(\lfloor 12/(3 \times 4) \rfloor = 1\)；单架构：\(\lfloor 12/(3 \times 1) \rfloor = 4\)，再与 cpus=8 取 min。四架构时每个 nvcc 内部还有 4 个并发 device pass，这正是小机器被 OOM 杀死的原因，也是单架构「快得多」的第二重来源（公式见 [setup.py:L31-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L31-L51)）。

**练习 3**：`FAST_ULYSSES_CUDA_ARCH=100` 在 CUDA 12.4 的机器上构建，会发生什么？为什么不在运行时报错？

答案：CMake 的 Blackwell 守卫直接 `FATAL_ERROR`，消息指明需要 CUDA 12.8+ 并给出 `FAST_ULYSSES_CUDA_ARCH="80;90"` 的退路。提前失败是因为 nvcc 根本编不出 sm_100；若放任产出一个不含该架构的 `.so`，加载能成功、直到第一个 kernel 启动才报 `no kernel image is available`——那种延迟爆炸正是守卫要避免的。

### 4.3 torchrun 启动：跑通 quickstart 与 examples

#### 4.3.1 概念说明

`torchrun`（PyTorch 内置的启动器）把一份脚本复制成 N 个 rank，设置好 rendezvous 与环境变量（`RANK`、`WORLD_SIZE`、`LOCAL_RANK`）。fast-ulysses 是 SPMD 库：每个 rank 跑同样的代码、持有序列的一个分片、发出相同的调用序列（u1-l2 的契约）。

第一次运行有两层意义：

1. **验证安装链路**——NCCL 进程组、对称内存分配、扩展加载全部走通；
2. **验证语义**——`examples/dit_attention.py` 自带双保险：与「不做序列并行的同块」对拍输出**和**输入梯度，再跑 5 个真实的优化器步。

#### 4.3.2 核心流程

README quickstart 脚本（`example.py`）的骨架：

```
dist.init_process_group("nccl")              # NCCL 后端（窗口能力来自 torch 的对称内存）
torch.cuda.set_device(LOCAL_RANK)            # 每个 rank 绑定自己可见范围内的那张卡
group = UlyssesGroup()                       # 集合操作：对称内存窗口的 bootstrap 注册
x = randn(b, s_local, heads*ws, d)           # 本 rank 的序列分片 × 全部头
y = group.all_to_all_4d(x, mode=0)           # → (b, s_local*ws, heads, d)：全序列 × 头分片
z = group.all_to_all_4d(y, mode=1)           # → 回到 x 的形状；数值上 z == x（位置精确重排）
group.destroy(); dist.destroy_process_group()  # 两个都是集合操作，所有 rank 都要到
```

`examples/dit_attention.py` 的 `main()` 则多四步（见源码精读）：构造模块 → 前向+反向 → `all_gather` 汇总与单卡参照比误差 → AdamW 走 5 步。

**没有 8 张卡怎么办**：`torchrun --nproc_per_node=1` 也能跑（`world_size` 在 `[1, 8]` 内都受支持，README 的 Limits 一节，[README.md:L104-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L104-L106)）。此时确实有一些环节退化/跳过——最明确的一个：屏障在 `ws == 1` 时直接返回（见下）。但形状契约、mode 0/1 的往返、自动微分这些**单 rank 内可观察**的行为全部照旧，所以它仍是有效的冒烟测试。

#### 4.3.3 源码精读

quickstart 脚本本体在 [README.md:L48-L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L48-L72)，启动命令在标题下一行（`torchrun --nproc_per_node=8 example.py`）；[README.md:L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L74) 是那句契约提醒：每个 rank 必须发出相同的形状序列（新形状的首次调用会触发集合性的窗口分配）。README 中 DiT 示例的启动命令在 [README.md:L79-L81](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L79-L81)。

示例本体 [examples/dit_attention.py:L77-L86](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L77-L86)：初始化进程组、按 `LOCAL_RANK` 设备、`torch.manual_seed(0)`（**所有 rank 同种子**，权重才能一致）、构造 `UlyssesAttention`。注意示例里传的是 `require_nvlink=False`（[examples/dit_attention.py:L85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L85)）——为了在没有完整 NVLink 互连的机器上也能演示；生产构造默认 `require_nvlink=True`。

校验的核心在 [examples/dit_attention.py:L99-L116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L99-L116)：把分片的结果与梯度用 `dist.all_gather` 拼回全局，与单 rank 参照（`reference()`，L66-L74）比最大误差，容差 `5e-2`——注释点明用 `allclose` 而非相等，因为两条路径的浮点归约顺序不同。rank 0 打印两行 `output / d/dinput max |diff| = ... OK/FAIL`，这就是你运行后要看的输出。

训练真实性的部分在 [examples/dit_attention.py:L118-L129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L118-L129)：AdamW 五步，每步打印 rank 0 的 loss；注释说明各 rank 的 loss 本就不必一致。收尾 [examples/dit_attention.py:L131-L132](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L131-L132)：`group.destroy()` 与 `dist.destroy_process_group()`——前者也是集合操作，漏掉它的某个 rank 会让别的 rank 挂在等待里。

`world_size=1` 跳过屏障的证据在 [src/barrier.cu:L53-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L57)：`fast_barrier` 一进来就 `if (ws == 1) return;`——没有 peer 可等，发布/等待阶段整个不发射。这正是单卡运行时「仍然成立的行为」清单里最有意思的一条：调用链照常走完，屏障阶段为空操作。（屏障内核本身的逐行精读在 u5-l3。）

#### 4.3.4 代码实践

1. **实践目标**：跑通两个入口，并对照源码读懂每一行输出。
2. **操作步骤**：
   - 把 [README.md:L52-L71](https://github.com/triple-mu/fast-urysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L52-L71) 的脚本存为 `example.py`（以仓库内 README 为准），运行 `torchrun --nproc_per_node=<N> example.py`（N = 你的 GPU 数，最少 1）；
   - 运行 `torchrun --nproc_per_node=<N> examples/dit_attention.py`；
   - 单卡用户额外做：确认脚本无改动地跑完，并记录哪些行为仍成立。
3. **需要观察的现象**：
   - `example.py`：无输出即成功（脚本没写 print）；可自行加一行 `print((z - x).abs().max().item())` 观察 mode 0→1 往返是否还原（预期 0，重排是位置精确的）；
   - `dit_attention.py`：rank 0 打印 `ws=... s_global=... n_global=...`、两行 `max |diff| = ... OK`、五行 `step k loss ...`（loss 通常下降）。
4. **预期结果**：两行 diff 都是 `OK`；5 步 loss 有下降趋势。`待本地验证`。
5. **单卡记录表**（`--nproc_per_node=1` 时填写）：
   - 集合构造 `UlyssesGroup()` 是否成功？（预期：是，`world_size` 下界是 1）
   - mode 0 输出形状是否仍是 `(b, s_local*1, heads, d)`？（预期：是，变换在 ws=1 下降维为恒等重排）
   - 屏障阶段被跳过（依据 [src/barrier.cu:L56-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57)，从外部只能通过「运行成功且无 peer 通信」间接确认）
   - 反向传播与优化器步是否照常？（预期：是）

#### 4.3.5 小练习与答案

**练习 1**：`example.py` 里为什么必须有 `torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))`？不写会怎样？

答案：torchrun 起的每个 rank 都能看到全部 N 张卡，`cuda` 设备的默认选择是 0 号——不绑定 `LOCAL_RANK` 的话所有 rank 都在 0 号卡上工作，对称内存注册与数据分片全部错乱。绑定后每个 rank 的「当前设备」唯一，后续 `device="cuda"` 的分配才落在自己的卡上。

**练习 2**：示例用 `torch.manual_seed(0)` 让所有 rank 同种子，但输入 `x_global` 又是随机张量——为什么不至于让不同 rank 拿到不同的数据？

答案：种子相同 + 确定性的生成调用（`torch.randn` 以相同形状与顺序调用）⇒ 所有 rank 生成出**同一个全局张量**，再各自切出自己的分片（[examples/dit_attention.py:L88-L94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L88-L94) 注释说明这正是为了让参照对拍落在完全相同的数据上）。权重同理：`nn.Linear` 在同种子下初始化一致。

**练习 3**：脚本结尾漏掉 `group.destroy()` 会发生什么？

答案：`destroy()` 是集合操作（释放对称内存窗口，README API 表 [README.md:L94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L94)）。多数情况下进程直接退出时资源随进程回收、看不出问题；但在同一进程里接着用同一个 group 再做事、或与其它集合操作交叠时，早退/缺席的 rank 会让其余 rank 阻塞在等待上。养成对称调用的习惯（init 与 destroy 成对）即可。

### 4.4 doctor 自检：build_info、设备与 NVLink 矩阵

#### 4.4.1 概念说明

`fast-ulysses doctor` 回答一个问题：**这台机器能不能跑这个算子**。它把判断拆成三段可独立观察的事实：

1. 扩展是否加载成功，加载的是哪份 `.so`、按什么配置编的；
2. 有哪些 GPU，每张的架构是否在这个构建的 arch 列表里；
3. 这些 GPU 两两之间是否被 NVLink 连接（矩阵）。

注意它的定位（cli 模块 docstring 明说）：绿矩阵是**必要条件**，不替代 `UlyssesGroup` 构造函数在实际进程组上做的检查——doctor 不建进程组。

#### 4.4.2 核心流程

`_doctor()` 的四个阶段，任一阶段失败都带着结构化的返回码（0 成功 / 1 有问题）：

```
1. 加载扩展     失败 → 打印 "extension: FAILED TO LOAD" + 原始异常，返回 1
2. build_meta   打印 _build_meta.py 里的构建事实（VERSION/TORCH_ABI/CUDA_VERSION/CUDA_ARCH_LIST/HAS_WORK_REGISTRY…）
3. 设备列表     torch.cuda.is_available() 为假 → "devices: none visible"，返回 1
   否则逐卡打印名字、sm_XX、显存；设备架构不在构建 arch 列表 → 标 "<-- NOT IN THIS BUILD"
4. NVLink 矩阵  _C.nvlink_matrix(设备号列表)
   返回 None（NVML 答不了）→ 打印 "无法报告拓扑"，但返回 0（不拒绝、不声称）
   有缺失对 → 列出缺失对并返回 1；全连通 → "all N devices are mutually NVLink-joined"
   只有一张卡 → "one visible device: no pair to check"
```

#### 4.4.3 源码精读

命令行入口的注册在 [pyproject.toml:L20-L21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L20-L21)：`fast-ulysses = "fast_ulysses.cli:main"`。

**阶段 1-2**，[python/fast_ulysses/cli.py:L38-L49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L38-L49)：`_load()` 尝试导入包；成功则打印 `_C.__file__`（哪份 `.so`），然后逐行打印 `_build_meta` 的键值。注释里有个重要细节：以控制台脚本方式运行时，包导入失败会在 `main` 之前就被 `__init__.py` 的诊断路径抛出（4.1.3 已读），`_load` 捕获到的是「以文件方式直接运行 cli.py」这条路径。构建事实来自 `_build_meta.py`，由 [setup.py:L158-L167](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L158-L167) 在构建时写入——`HAS_WORK_REGISTRY` 就是 4.2 里从 configure 输出解析出的那个探针。

**阶段 3**，[python/fast_ulysses/cli.py:L51-L70](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L51-L70)：CUDA 不可见直接返回 1；否则对每张卡取 `major``minor` 拼成 `sm_XX`，与构建时的 `cuda_arch_list` 比对——`built` 集合只取前导数字（[python/fast_ulysses/cli.py:L59-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L59-L64) 的注释解释了为什么：`90a` 和 `120-real` 与 `90`、`120` 命名同一批 GPU，CMakeLists 用同样的规则归一）。不在集合里就标 ` <-- NOT IN THIS BUILD`——这正是你用 `FAST_ULYSSES_CUDA_ARCH=90` 只编一个架构后，在别的架构机器上会看到的标记。

**阶段 4**，[python/fast_ulysses/cli.py:L72-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L72-L103)：调用 `_C.nvlink_matrix(...)` 拿到 `(i, j) → bool` 的矩阵（NVML 探测的实现在 `src/nvlink.cc`，u8-l1 精读）。三种结局：
- `None`（NVML 无法报告拓扑）→ 打印说明并**返回 0**：既不拒绝也不声称，构造函数照样会建组（fail-open，[docs/install.md:L220-L222](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L220-L222)）；
- 有缺失对 → 打印 `N` 的位置与示例缺失对，建议那些组用 `torch.distributed`，返回 1；
- 单卡（`n < 2`，[python/fast_ulysses/cli.py:L97-L101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L97-L101)）→ 「没有对可查，也没有超过 world_size 1 的东西可跑」，返回 0。

矩阵的打印格式是 `row=from col=to` 的字符矩阵：对角线 `.`、连通 `y`、缺失 `N`。

#### 4.4.4 代码实践

1. **实践目标**：运行 doctor 并把每段输出与上面的阶段对应起来。
2. **操作步骤**：
   - `fast-ulysses doctor`；
   - 若扩展加载失败：直接读抛出的报告（它已包含两侧信息），按 4.1 的修复路径处理后再来；
   - 把输出粘贴出来，逐段标注「阶段 1/2/3/4」。
3. **需要观察的现象**：`extension: /path/to/_C.so`；若干 `key: value` 的构建事实行；`devices: N` 加 N 行设备描述；一个字符矩阵和一句总结。
4. **预期结果**：
   - 你按 4.2 用 `FAST_ULYSSES_CUDA_ARCH=90` 构建并在 H100 上运行 → `cuda_arch_list: 90`，设备行无 `NOT IN THIS BUILD` 标记；
   - 同一份安装在 A100（sm_80）上跑 doctor → `[0] ... sm_80 ... <-- NOT IN THIS BUILD`；
   - 多卡 NVLink 全互连机器 → `all N devices are mutually NVLink-joined`。`待本地验证`。
5. **交叉验证**（有 GPU 时）：把 doctor 的矩阵与 `nvidia-smi topo -m` 对照，`NV#` 链路应对应 `y`，`PIX/PXB/PHY` 应对应 `N`。

#### 4.4.5 小练习与答案

**练习 1**：doctor 打印 `NVLink: NVML could not report the link topology` 却返回 0（退出码成功）。矛盾吗？

答案：不矛盾。探测不到 ≠ 不连通，检查的语义是 fail-open：答不了就不拒绝，把决定留给 `UlyssesGroup` 构造函数在真实进程组上的检查（或者你用 `require_nvlink=False` 显式接管）。返回 0 表示「doctor 没有发现障碍」，不是「确认可以跑」。

**练习 2**：为什么阶段 3 的 `built` 集合要 `re.match(r"[0-9]+", a.strip())` 只取前导数字，而不是精确字符串比较？

答案：arch 列表携带的是「当时请求了什么」，可能有 `90a`、`120-real` 这类带后缀的写法，它们与 `90`、`120` 指同一批 GPU；精确比较会把合法安装误标为 `NOT IN THIS BUILD`。CMake 侧用同样的正则做归一（注释见 [python/fast_ulysses/cli.py:L58-L60](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L58-L60)）。

**练习 3**：`fast-ulysses wheel-url` 在扩展坏掉时还能用吗？`doctor` 呢？

答案：都不能完整工作——两者都是 `fast-ulysses` 脚本的子命令，脚本先 import 包、包再 import `_C`（[python/fast_ulysses/__init__.py:L3-L10](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L3-L10)），`_C` 坏则脚本以同一份诊断报告退出。但报告本身已把「编译时的 torch/CUDA」与「现装的」并列印出，修复它不需要这两个子命令（[docs/install.md:L110-L112](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L110-L112)）。

## 5. 综合实践

**任务：从零走完「构建 → 运行 → 自检」全链路，并留下一份环境记录。**

1. **环境确认**：记录 `python -c "import sys,torch; print(torch.__version__, torch.version.cuda, sys.version_info[:2])"`、GPU 型号与架构、`nvcc --version`。
2. **构建**：`FAST_ULYSSES_CUDA_ARCH=<本机架构> pip install -e . --no-build-isolation`。构建中记录两件事：configure 输出里的 arch 行与 `c10d::register_work available:` 状态行（后者写进了 `_build_meta.py` 的 `HAS_WORK_REGISTRY`）。
3. **自检**：`fast-ulysses doctor`，按 4.4 的四阶段标注输出；确认设备行没有 `NOT IN THIS BUILD`。
4. **运行 quickstart**：把 README quick start 存为 `example.py`，`torchrun --nproc_per_node=<N> example.py`。多卡用户加打一行往返误差（见 4.3.4）；单卡用户填写 4.3.4 的「单卡记录表」——重点确认：构造成功、mode 0/1 形状往返正确、`ws==1` 时屏障被跳过这一事实与 [src/barrier.cu:L56-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57) 的代码一致（外部表现：运行正常结束、没有跨卡通信）。
5. **运行示例**：`torchrun --nproc_per_node=<N> examples/dit_attention.py`，抄下 rank 0 的全部输出，对照 [examples/dit_attention.py:L110-L129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L110-L129) 给每行写一句「它证明了什么」。
6. **（无 GPU 环境）替代路径**：完成 4.1.4 与 `pytest -m "not multigpu"`（host-only 布局测试，[README.md:L157-L159](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L157-L159)），并把 4.2/4.3/4.4 的运行步骤改写成「纸面推演 + 预期输出」。

所有运行步骤本讲均未代跑，`待本地验证`。

## 6. 本讲小结

- wheel 的选择由 **torch minor × CUDA 大版本** 决定（`.so` 嵌入 `c10d::Work` vtable、pybind11 模块与 `TORCH_LIBRARY` 注册，活不过 minor 升级；只链接 `libcudart.so.<major>`）；专用索引靠「无 torch 可装」迫使 pip 回溯解析出正确的行。
- PEP 440 对本地版本的排序让不带标签的说明符**静默**选中最高的 `+torch213cu130`：`--find-links` 场景必须写全标签，PyPI 单独对待。
- 源码构建的两条铁律：`--no-build-isolation`（CMake 与 pin 推导都要用你环境里的那份 torch，构建依赖里反而不能声明 torch）；`FAST_ULYSSES_CUDA_ARCH` 只编本机架构（默认四架构，jobs 按内存每并发 pass 3 GiB 预算推导）。
- `torchrun` 起的每个 rank 是同一份 SPMD 代码：绑 `LOCAL_RANK`、同种子构造、集合调用序列一致、init/destroy 成对；`world_size=1` 是受支持的合法配置，屏障在该点被显式跳过。
- `examples/dit_attention.py` 用「与单卡参照对拍输出+梯度（容差 5e-2）+ 5 个优化器步」同时证明正确性与可训练性。
- `doctor` 输出构建事实、设备/架构匹配、NVLink 矩阵三段，绿矩阵是必要条件；NVML 答不了时 fail-open。

## 7. 下一步学习建议

- **u1-l4（仓库结构与阅读地图）**：本讲你已经按过不少门铃（setup.py、cli.py、__init__.py），下一讲把整个仓库的目录职责与「一次调用的调用链骨架」系统化，为进入 C++ 源码做准备。
- 精读 [docs/develop.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md)：开发工具链与测试布局——你刚用过的 `pytest -m "not multigpu"` 的完整语境在那里。
- 若你走的是 wheel 路线且想彻底吃透匹配算法，提前读 [python/fast_ulysses/cli.py:L130-L184](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/cli.py#L130-L184) 的 `_match`（正式讲解在 u8-l3）。
- 运行成功后，带着「这两次 all_to_all 之间为什么不会数据竞争」的问题进入 u2/u5：屏障（本讲瞥见的 [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu)）是答案的一半。
