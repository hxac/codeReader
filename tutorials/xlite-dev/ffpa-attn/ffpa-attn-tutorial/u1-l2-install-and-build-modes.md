# 安装、构建与三种构建模式

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说出 FFPA 的三种安装/构建方式（PyPI 预编译、源码 Triton-only、源码 + CUDA 前向扩展），并能判断每种方式的产物里**是否包含编译好的二进制扩展 `ffpa_attn._C`**。
2. 读懂 `setup.py` 里 `BUILD_CUDA_EXT` 这个开关分支，解释它在什么条件下才会真正去编译 CUDA 扩展。
3. 说清楚 `setup.py` 与 `env.py` 在构建过程中的**职责分工**：谁读环境变量、谁生成源码、谁驱动 nvcc。
4. 用 `tools/build_fast.sh` 加速源码构建，并理解 ccache、`MAX_JOBS`、`FFPA_NVCC_THREADS` 各自在省时间。

> 本讲承接 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)：你已经知道 FFPA 是面向大 head_dim 的注意力 kernel 库，本讲解决「它怎么装到我机器上」。

## 2. 前置知识

在动手之前，先建立几个直觉。如果你已经熟悉，可以跳到第 3 节。

- **Python 包的两种形态**：纯 Python 包只发 `.py` 文件；带 C/C++/CUDA 扩展的包还需要在安装时（或发布前）把源码编译成机器码，产出一个形如 `_C.cpython-3xx-xxx.so` 的二进制模块。FFPA 的手写 CUDA 前向 kernel 就属于后者，编译产物以 `ffpa_attn._C` 这个模块名暴露。
- **`pip install` 的「构建隔离」**：默认情况下，pip 会建一个**隔离的临时环境**来跑构建脚本，按 `[build-system].requires` 装构建期依赖。这对纯 Python 包没问题，但**对依赖特定 PyTorch（带特定 CUDA）的扩展**会很麻烦——构建时拉的 torch 可能和你运行时用的 torch 对不上。因此 FFPA 的源码构建普遍加 `--no-build-isolation`，让你当前环境里的 torch 直接参与编译。
- **`env` 开关 = 构建期 vs 运行期**：FFPA 用大量 `ENABLE_FFPA_*` / `FFPA_*` 环境变量来控制行为。其中一部分（如 `ENABLE_FFPA_CUDA_IMPL`、`FFPA_BUILD_ARCH`、`FFPA_DEV_HEADDIMS`）在**编译时**读取，决定要生成哪些翻译单元；另一部分（如 `ENABLE_FFPA_PREFETCH_QKV`）在**运行时**读取，决定 kernel 怎么跑。本讲只讲构建期那一批，运行期的留到 u7-l5。
- **`ffpa_attn._C` 与 `CUDA_FWD_AVAILABLE`**：`_C` 是编译出来的二进制扩展；如果没编译它，运行时 `from ffpa_attn import _C` 会失败，FFPA 用 `try/except` 优雅地把这个失败翻译成 `CUDA_FWD_AVAILABLE = False`，并自动回退到 Triton 后端。这正是「Triton-only 模式」能正常工作的关键。

## 3. 本讲源码地图

本讲涉及的文件不多，但每个都扮演明确角色：

| 文件 | 角色 |
| --- | --- |
| [pyproject.toml](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml) | 项目元数据主战场：包名、依赖、可选 extras、构建后端、`src/` 布局、以及**哪些目录被排除在 Python 包之外**。 |
| [setup.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py) | 只做一件事：在打开 `ENABLE_FFPA_CUDA_IMPL` 时，用 `torch.utils.cpp_extension` 编译出 `ffpa_attn._C`。 |
| [env.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py) | 构建期与运行期的「配置中枢」：读取所有 `FFPA_*` 环境变量、生成每个 head_dim 的 `.cu` 翻译单元、给出 nvcc 编译参数。`setup.py` 直接 `from env import ENV` 来用。 |
| [tools/build_fast.sh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh) | 加速构建的封装脚本：ccache shim、自动 `MAX_JOBS`、可选 editable / clean / tmpfs。 |
| [docs/env.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md) | 所有环境变量的官方清单（含默认值与一句话说明），遇到拿不准的开关都该来这里查。 |
| [src/ffpa_attn/cuda/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py) | 运行时导入 `_C` 的入口：用 `try/except` 把「扩展没编译」转化为 `CUDA_FWD_AVAILABLE = False`。 |

## 4. 核心概念与源码讲解

### 4.1 三种安装/构建模式总览

#### 4.1.1 概念说明

FFPA 落地到机器上有三条路，它们的「产物」差别可以用一张表说清：

| 模式 | 一条命令 | 产物里有 `ffpa_attn._C`？ | 默认大 D 前向后端 |
| --- | --- | --- | --- |
| ① PyPI 预编译 wheel | `pip3 install -U ffpa-attn` | 取决于发布版（预编译，无需本地 nvcc） | Triton |
| ② 源码 Triton-only（默认） | `pip3 install -e . --no-build-isolation` | ❌ 否 | Triton |
| ③ 源码 + CUDA 前向扩展 | `ENABLE_FFPA_CUDA_IMPL=1 pip3 install -e .` | ✅ 是 | 可选 CUDA（前向） |

需要特别强调的两点（也是贯穿全讲的要点）：

- **默认源码构建是 Triton-only**：不设任何开关时，`setup.py` 不会去碰 nvcc，`ffpa_attn._C` 根本不会被生成；大 D 前向由 Triton 后端承担。这一点在 `setup.py` 顶部 docstring 写得很直白（[setup.py:8-14](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L8-L14)），也在 `docs/env.md` 的「Default build mode」里再次确认。
- **CUDA 后端只覆盖前向**：即便开启了模式 ③，手写 CUDA 也**只有前向**，反向仍走 Triton 或 SDPA。`env.py` 里 `enable_bwd_cuda_impl()` 永远返回 `False`（[env.py:270-272](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L270-L272)）。

#### 4.1.2 核心流程

三种模式的差异，本质上是「`setup.py` 走不走那个 `if BUILD_CUDA_EXT:` 分支」。用伪代码描述整条安装链路：

```
pip install .
  └─> setuptools 调用 build-backend (pyproject: setuptools.build_meta)
        └─> 读 pyproject.toml 的 [build-system].requires（含 torch、setuptools_scm）
        └─> 执行 setup.py
              ├─ _resolve_version()          # setuptools_scm 从 git tag 推版本
              ├─ SKIP_CUDA_EXT = FFPA_SKIP_CUDA_EXT 标志
              ├─ from env import ENV          # 读所有 FFPA_* 环境变量
              ├─ BUILD_CUDA_EXT = (非 SKIP) and ENV.enable_fwd_cuda_impl()
              │
              ├─ if BUILD_CUDA_EXT:           # 只在模式③为真
              │     ├─ ENV.generate_split_headdim_sources()  # 生成每个 head_dim 的 .cu
              │     ├─ ENV.get_build_sources()               # 收集 .cc/.cu 源文件
              │     ├─ ENV.get_build_cuda_cflags()           # 组 nvcc 参数
              │     ├─ ENV.get_build_arch_list()             # 解析目标 SM
              │     └─ CUDAExtension(name="ffpa_attn._C", sources=...) → BuildExtension
              └─ setup(ext_modules=ext_modules, ...)
```

模式 ① 相当于「别人在 CI 里替你跑了上面这套并打包成 wheel」，所以本地零编译。模式 ② 在 `BUILD_CUDA_EXT` 处判 False，直接跳过整段 CUDA 逻辑，`ext_modules=[]`。模式 ③ 把 `BUILD_CUDA_EXT` 判 True，进入编译分支。

#### 4.1.3 源码精读

先看 `pyproject.toml` 里和「装什么」最相关的两处。

运行期依赖清单（注意 `torch>=2.10.0`、`quack-kernels`、`nvidia-cutlass-dsl`、`apache-tvm-ffi`、`ray`，后四个分别是 CuTeDSL 后端、TVM-FFI 绑定、Ray 多卡自动调优的支撑）：

- [pyproject.toml:12-21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L12-L21) —— 运行期 `dependencies`，`pip install` 会自动拉取这一组。

构建后端与构建期依赖（注意构建期 `torch>=2.7.0` 比运行期 `torch>=2.10.0` 宽松，因为 `setup.py`/`env.py` 在构建时 `import torch` 只用到基础功能）：

- [pyproject.toml:75-78](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L75-L78) —— `[build-system]`，声明用 setuptools 做后端。

再来看一个容易被忽略、却解释了「为什么 Triton-only 模式不需要 `csrc/`」的关键点——`src` 布局与包发现排除规则：

- [pyproject.toml:85-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L85-L87) —— `package-dir = {"" = "src"}`，说明源码在 `src/` 下，安装后 `import ffpa_attn` 实际指向 `src/ffpa_attn/`。
- [pyproject.toml:92-106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L92-L106) —— `packages.find` 把 `csrc*`、`tests*`、`bench*`、`docs*`、`tools*` 等**排除在 Python 包之外**。也就是说 `csrc/cuffpa/` 下的 C++/CUDA 头文件**从不进入 Python wheel**，它们只在模式 ③ 被 `setup.py` 编译进 `_C`。这正是模式 ② 干净的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证「模式 ② 装出来的包里没有 `_C`」，建立对三种模式产物的直观感受。

**操作步骤**：

1. 在装好（带 CUDA 的）PyTorch 的环境里，于仓库根目录执行 Triton-only 源码构建：
   ```bash
   pip3 install -e . --no-build-isolation
   ```
2. 确认能 import，并查看 `_C` 是否存在：
   ```bash
   python3 -c "import ffpa_attn; print('ok', ffpa_attn.__version__)"
   python3 -c "import ffpa_attn._C"          # 预期：ImportError
   python3 -c "from ffpa_attn.cuda import CUDA_FWD_AVAILABLE; print(CUDA_FWD_AVAILABLE)"  # 预期：False
   ```
3. 对照 `pyproject.toml` 的 `packages.find` 排除规则，确认安装目录里**没有** `csrc/`：
   ```bash
   python3 -c "import ffpa_attn, os; print(os.path.dirname(ffpa_attn.__file__))"
   # 进入该目录 ls，应该看不到任何 csrc/cuffpa 的痕迹
   ```

**需要观察的现象**：第 1 步构建过程里**不会出现 nvcc 调用**（终端看不到 `gencode`/`--threads` 之类输出，也不会打印 `ENV.list_ffpa_env()` 那张环境表）；第 2 步 `_C` 导入失败、`CUDA_FWD_AVAILABLE` 为 `False`。

**预期结果**：`import ffpa_attn` 成功，但 `ffpa_attn._C` 不可用——这就是「Triton-only 产物」的特征。本机运行时具体耗时与日志细节**待本地验证**（取决于机器与已装 torch 版本）。

#### 4.1.5 小练习与答案

**练习 1**：为什么源码构建命令普遍带 `--no-build-isolation`？去掉它会怎样？

> **答**：FFPA 的 `setup.py` 在顶部 `from env import ENV`，而 `env.py` 一开头就 `import torch`，构建脚本深度依赖 torch。开构建隔离时，pip 会按 `[build-system].requires` 临时拉一个 torch（可能与你运行时的 CUDA 版本不一致）来编译，编译产物可能和你真正要用的 torch ABI 不匹配。`--no-build-isolation` 让构建直接用你当前环境里那把 torch，保证「编译用的 torch == 运行用的 torch」。

**练习 2**：模式 ②（Triton-only）的 wheel 里，会不会包含 `csrc/cuffpa/ffpa_attn_fwd.cuh` 这种文件？为什么？

> **答**：不会。`pyproject.toml` 的 `packages.find` 把 `csrc*` 排除在 Python 包之外（见 [pyproject.toml:94-106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/pyproject.toml#L94-L106)）。`csrc/` 下的源文件只在模式 ③ 被 `setup.py` 编译进二进制 `_C`，从不作为数据文件随包发布。

---

### 4.2 `setup.py` 的 `BUILD_CUDA_EXT` 分支

#### 4.2.1 概念说明

`setup.py` 是整个构建的「最后一公里」。它的 docstring 把自己的定位说得很谦虚：项目元数据都在 `pyproject.toml`，`setup.py` 存在的唯一理由就是「在需要时驱动那个可选的 CUDAExtension 编译」。换句话说：

- **默认情况下**，`setup.py` 几乎什么都不做：`ext_modules=[]`、`cmdclass={}`，等价于一个纯 Python 包安装。
- **只有当 `BUILD_CUDA_EXT` 为真**，它才会去 `import` `torch.utils.cpp_extension`、生成源码、组装 `CUDAExtension`、把 `BuildExtension` 挂进 `cmdclass`。

这个设计的好处是：在没有 nvcc / 没有 GPU 的环境（比如 ReadTheDocs 文档构建、`check-mkdocs` CI）里，FFPA 依然能作为一个「能 import、能取 docstring」的纯 Python 包被安装。

#### 4.2.2 核心流程

`BUILD_CUDA_EXT` 这个布尔值由两个环境变量共同决定，逻辑很短但很关键：

```
SKIP_CUDA_EXT   = FFPA_SKIP_CUDA_EXT 是否为真        # 强制跳过
BUILD_CUDA_EXT  = (not SKIP_CUDA_EXT)                # 没被强制跳过
                  and ENV.enable_fwd_cuda_impl()     # 且显式开了 CUDA 扩展
```

进入 `if BUILD_CUDA_EXT:` 分支后，依次做四件事：

1. 决定 wheel 的平台标签（`manylinux_2_34_x86_64` 之类），让带二进制的 wheel 能上传 PyPI；
2. `ENV.list_ffpa_env()` 打印一张当前生效的环境变量表，方便排查；
3. 按 `ENV.get_build_arch_list()` 给每个目标 SM 拼 `-gencode arch=compute_X,code=sm_X`；
4. 构造一个 `CUDAExtension(name="ffpa_attn._C", sources=..., extra_compile_args=..., include_dirs=...)`，并交给 `BuildExtension`。

#### 4.2.3 源码精读

`setup.py` 顶部 docstring 已经把三种行为讲清楚（值得逐行读一遍）：

- [setup.py:1-14](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L1-L14) —— 明确写出默认 Triton-only、`ENABLE_FFPA_CUDA_IMPL=1` 编译 `_C`、`FFPA_SKIP_CUDA_EXT=1` 强制跳过。

决定是否编译的「总开关」就一行：

- [setup.py:83-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L83-L87) —— `SKIP_CUDA_EXT = _env_flag("FFPA_SKIP_CUDA_EXT")`，`BUILD_CUDA_EXT = (not SKIP_CUDA_EXT) and ENV.enable_fwd_cuda_impl()`。

辅助函数 `_env_flag` 把字符串 `"1"/"true"/"yes"/"on"` 统一解析成布尔（构建脚本里所有「开关型」环境变量都该这么解析）：

- [setup.py:79-80](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L79-L80) —— `_env_flag` 实现。

进入编译分支后，构造 `ffpa_attn._C` 这个扩展（注意 `name`、`sources` 来自 `ENV.get_build_sources`、`nvcc` 参数来自 `ENV.get_build_cuda_cflags`，**真正的脏活都在 `env.py`**）：

- [setup.py:112-119](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L112-L119) —— 引入 `BuildExtension`/`CUDAExtension`，并按目标 SM 列表拼 `-gencode`。
- [setup.py:121-144](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L121-L144) —— `CUDAExtension(name="ffpa_attn._C", ...)`，`include_dirs` 指向 `csrc/cuffpa`，`cmdclass["build_ext"] = BuildExtension`。

#### 4.2.4 代码实践

**实践目标**：定位控制 CUDA 扩展的两个环境变量，并写出开启 CUDA 前向扩展的完整命令（本讲规格指定的核心实践）。

**操作步骤**：

1. 打开 `setup.py`，定位 [setup.py:83-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L83-L87)。你能看到两个控制变量：
   - `ENABLE_FFPA_CUDA_IMPL`（被 `ENV.enable_fwd_cuda_impl()` 间接读取）——**正面开关**，置 1 才会编译；
   - `FFPA_SKIP_CUDA_EXT`——**负面开关**，置 1 时即便前者开了也会强制跳过（供无 nvcc 的文档/CI 环境使用）。
2. 结合 README 的安装段（[README.md:42-43](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/README.md#L42-L43)），写出开启 CUDA 前向扩展的完整命令：
   ```bash
   ENABLE_FFPA_CUDA_IMPL=1 MAX_JOBS=32 pip3 install -e .
   ```
3. （可选）想验证 `FFPA_SKIP_CUDA_EXT` 的优先级，可以做对照实验：
   ```bash
   ENABLE_FFPA_CUDA_IMPL=1 FFPA_SKIP_CUDA_EXT=1 pip3 install -e .
   # 预期：仍走 Triton-only，不编译 _C（因为 SKIP 优先）
   ```

**需要观察的现象**：第 2 步执行时，终端会先打印一张 `FFPA-ATTN ENVs` 表（来自 `ENV.list_ffpa_env()`），随后是一长串 nvcc 编译日志（每个 head_dim × 每个 dtype 一个翻译单元），最后链接出 `src/ffpa_attn/_C*.so`。第 3 步则不应出现 nvcc 日志。

**预期结果**：第 2 步完成后 `python3 -c "import ffpa_attn._C"` 不再报错，且 `from ffpa_attn.cuda import CUDA_FWD_AVAILABLE` 应为 `True`。具体编译耗时**待本地验证**（在 L20 上全量冷编译约 200 秒，见 [tools/build_fast.sh:113-117](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L113-L117) 的基准数据）。

#### 4.2.5 小练习与答案

**练习 1**：`BUILD_CUDA_EXT = (not SKIP_CUDA_EXT) and ENV.enable_fwd_cuda_impl()` 里，把两个条件调换顺序（写成 `enable_fwd_cuda_impl() and not SKIP_CUDA_EXT`）会影响结果吗？会有别的影响吗？

> **答**：结果（布尔值）不变，因为 `and` 满足交换律。但从「短路」效率看，当前写法先判便宜的 `_env_flag`（只读一个本地环境变量），再判 `ENV.enable_fwd_cuda_impl()`（需要 `from env import ENV`，触发 `env.py` 模块加载与 `import torch`）。先排掉 SKIP 的情况，可以避免在文档构建等场景白加载一遍 torch，是个小优化。

**练习 2**：为什么把扩展命名为 `ffpa_attn._C` 而不是 `ffpa_attn._cuda` 或顶层 `ffpa_attn_cuda`？

> **答**：`_C` 放在 `ffpa_attn` 包内部（`name="ffpa_attn._C"`，见 [setup.py:121-124](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L121-L124)），编译产物 `src/ffpa_attn/_C*.so` 随包一起安装，运行时 `from ffpa_attn import _C` 即可。下划线前缀是 Python 惯例，表示「内部模块、不要直接依赖」。这与 PyTorch 自身很多扩展（如 `torch._C`）的命名约定一致。

---

### 4.3 `env.enable_fwd_cuda_impl` 与构建期配置

#### 4.3.1 概念说明

如果说 `setup.py` 是「司机」，`env.py` 就是「调度中心」。`setup.py` 只决定「要不要编译」，而**编译什么、用什么参数**全都由 `env.py` 说了算。`env.py` 是一个名为 `ENV` 的类，里面塞满了类属性（在模块加载时一次性读取环境变量）和 `@classmethod`/`@staticmethod`。

本模块聚焦三个最关键的构建期入口：

- `enable_fwd_cuda_impl()` —— 回答「要不要生成 CUDA 前向」；
- `get_enabled_headdims()` —— 回答「为哪些 head_dim 生成翻译单元」；
- `get_build_sources()` / `get_build_cuda_cflags()` / `get_build_arch_list()` —— 回答「收集哪些源文件、给 nvcc 什么参数、编译到哪些 SM」。

#### 4.3.2 核心流程

`enable_fwd_cuda_impl()` 背后的环境变量解析带一个兼容别名，逻辑是：

```
ENABLE_FFPA_CUDA_IMPL  = int( ENABLE_FFPA_CUDA_IMPL 环境变量
                              or 旧名 ENABLE_FFPA_FWD_CUDA_IMPL 环境变量
                              or "0" )
enable_fwd_cuda_impl() = bool(ENABLE_FFPA_CUDA_IMPL)
```

构建期「生成哪些 head_dim」的优先级链（从高到低）：

```
FFPA_DEV_HEADDIMS（显式子集，如 "256,512"）
   └─ 为空则看 ENABLE_FFPA_ALL_HEADDIM
        ├─ =1 → range(32, 1025, 32)     # 全集，步长 32
        └─ =0 → range(256, 1025, 64)    # 默认，步长 64
```

> 为什么要按 head_dim 拆翻译单元？因为「Split-D」对每个 head_dim 都要实例化一份重型模板。把这些实例化拆进**每个 head_dim 一个 `.cu` 文件**，`MAX_JOBS` 就能同时跑很多个 nvcc 进程并行编译，大幅缩短墙钟时间。这个拆分由 `generate_split_headdim_sources()` 完成，详见 u7-l3，本讲只需知道它存在。

#### 4.3.3 源码精读

`ENABLE_FFPA_CUDA_IMPL` 的定义（含旧名兼容与「反向不再生成」的注释）：

- [env.py:118-130](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L118-L130) —— 默认 `0`，注释里明确「Native CUDA backward is no longer generated」。

三个构建期开关的环境变量定义：

- [env.py:132-155](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L132-L155) —— `FFPA_BUILD_ARCH`、`FFPA_NVCC_THREADS`、`FFPA_PTXAS_VERBOSE`、`FFPA_DEV_HEADDIMS`。

`enable_fwd_cuda_impl()` 本身只有一行，但它就是 `setup.py` 里 `BUILD_CUDA_EXT` 依赖的那个方法：

- [env.py:262-264](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L262-L264) —— `return cls.ENABLE_FFPA_CUDA_IMPL`。

head_dim 集合的三级优先级解析：

- [env.py:389-416](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L389-L416) —— `get_enabled_headdims()`，先看 `FFPA_DEV_HEADDIMS`，再看 `ENABLE_FFPA_ALL_HEADDIM`，最后用默认 `range(256, 1025, 64)`。

构建源文件收集（先生成再过滤出 `.cu`，再拼上 `ffpa_attn_api.cc` 这个 pybind 统一入口）：

- [env.py:734-758](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L734-L758) —— `get_build_sources()`。

nvcc 编译参数组装（`-O3`、`-std=c++17`、`--use_fast_math`、`--threads`、ptxas 等级……）：

- [env.py:760-791](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L760-L791) —— `get_build_cuda_cflags()`，末尾按 `FFPA_NVCC_THREADS` 追加 `--threads=N`。

目标 SM 解析（接受数字 `80,89,90` 或别名 `ampere,ada,hopper`，空则用当前设备能力）：

- [env.py:161-198](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L161-L198) —— `get_build_arch_list()`，无 GPU 且未设变量时会 `raise RuntimeError`（这正是文档 CI 要用 `FFPA_SKIP_CUDA_EXT=1` 的原因）。

运行时优雅降级：当 `_C` 没编译时，`cuda/__init__.py` 把导入失败转成 `CUDA_FWD_AVAILABLE = False`，并在真正调用时给出「Rebuild with ENABLE_FFPA_CUDA_IMPL=1」的清晰提示：

- [src/ffpa_attn/cuda/__init__.py:4-15](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L4-L15) —— `try/except` 包裹 `from .. import _C`。
- [src/ffpa_attn/cuda/__init__.py:46-51](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cuda/__init__.py#L46-L51) —— 调用时的报错信息，提示重新构建。

#### 4.3.4 代码实践

**实践目标**：用源码阅读的方式，确认「`setup.py` 读到的 `enable_fwd_cuda_impl()` 来自哪个环境变量、有哪些别名」，并能解释一次完整 CUDA 构建里 `env.py` 的贡献。

**操作步骤**：

1. 在 `env.py` 里搜索 `enable_fwd_cuda_impl`，跳到 [env.py:262-264](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L262-L264)，再回溯到类属性 [env.py:124-130](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L124-L130)。确认旧脚本里若用了 `ENABLE_FFPA_FWD_CUDA_IMPL` 也能被识别。
2. 做一次「最小子集」构建来感受 `get_enabled_headdims()` 的效果：
   ```bash
   ENABLE_FFPA_CUDA_IMPL=1 FFPA_DEV_HEADDIMS="512" \
   MAX_JOBS=32 pip3 install -e .
   ```
3. 观察编译日志：相比全量构建，这次只会为 head_dim=512 生成并编译 `.cu` 翻译单元（fp16 + bf16 两个），翻译单元数量大幅减少。

**需要观察的现象**：构建开始时打印的 `FFPA-ATTN ENVs` 表里，`FFPA_DEV_HEADDIMS` 一行会显示 `512`；编译阶段只出现 `ffpa_attn_fwd_fp16_hdim512.cu`、`ffpa_attn_fwd_bf16_hdim512.cu` 之类文件名（命名规则见 `generate_split_headdim_sources`，详见 u7-l3）。

**预期结果**：编译时间明显短于全量构建。具体省多少**待本地验证**；`tools/build_fast.sh` 注释里给出的参考是「`FFPA_DEV_HEADDIMS=256,512` 冷编译约 48s，而全量冷编译约 207s」（[tools/build_fast.sh:113-117](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L113-L117)）。

#### 4.3.5 小练习与答案

**练习 1**：在一台**没有 GPU** 的 CI 机器上，直接 `ENABLE_FFPA_CUDA_IMPL=1 pip install .` 会发生什么？该如何修复？

> **答**：会 `RuntimeError`。因为 `get_build_arch_list()` 在 `FFPA_BUILD_ARCH` 为空时会回退去查「当前可见 CUDA 设备的 compute capability」（[env.py:191-198](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L191-L198)），无 GPU 时 `torch.cuda.is_available()` 为 False，于是抛错。修复有两种：(a) 显式 `FFPA_BUILD_ARCH=80,89,90` 指定目标 SM；(b) 直接 `FFPA_SKIP_CUDA_EXT=1` 退回 Triton-only（文档 CI 用的就是这条路，见 [docs/env.md:11](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L11)）。

**练习 2**：`ENABLE_FFPA_ALL_HEADDIM=1` 与 `FFPA_DEV_HEADDIMS="256,512"` 同时设置时，最终编译哪些 head_dim？为什么？

> **答**：只编译 `256` 和 `512`。因为 `get_enabled_headdims()` 的优先级是 `FFPA_DEV_HEADDIMS` 最高（[env.py:400-413](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L400-L413)），只要它非空就直接返回这个子集，`ENABLE_FFPA_ALL_HEADDIM` 只在它为空时才起作用。这是「开发期精确控制」压过「全局开关」的设计。

---

### 4.4 `tools/build_fast.sh` 加速源码构建

#### 4.4.1 概念说明

模式 ③ 的全量 CUDA 构建很重（L20 上冷编译约 200 秒），日常迭代会让人抓狂。`tools/build_fast.sh` 就是用来缓解这个痛点的封装脚本，它把几项「构建提速」手段打包到一起：

1. **ccache shim 覆盖 nvcc**：用 ccache 缓存 `.cu` 翻译单元的编译结果，干净重建时几乎 100% 命中。
2. **`MAX_JOBS` 自动 sizing**：默认取 `min(nproc, 32)`，避免内存被打爆。
3. **`FFPA_NVCC_THREADS=4`**：每个翻译单元内部再用 4 线程。
4. **可选 tmpfs 构建目录**（`FFPA_BUILD_IN_SHM=1`）：把 `build/` 链到 `/dev/shm`，缓解 IO 瓶颈。
5. **两种安装形态**：默认 `python setup.py build_ext --inplace`（原地构建），`FFPA_EDITABLE=1` 则改成 `pip install -e .`（editable 安装）。

#### 4.4.2 核心流程

脚本主干（去掉细节后）：

```
1. 可选 FFPA_CLEAN=1：删 build/、*.so、csrc/cuffpa/generated/*.{cu,h}
2. 若装了 ccache：
     在 build/.ccache_cuda_home/ 造一个「影子 CUDA_HOME」
     —— 把真 CUDA_HOME 的所有条目软链进来，唯独把 bin/nvcc 换成 ccache shim
     export CUDA_HOME=<影子>   # 骗过 torch「按 $CUDA_HOME/bin/nvcc 找 nvcc」的解析
3. MAX_JOBS = min(nproc, 32)（除非已预设）
4. FFPA_NVCC_THREADS = 4（默认）
5. 可选 FFPA_BUILD_IN_SHM=1：build/ -> /dev/shm/...
6. 跑构建：
     FFPA_EDITABLE=1 ?  pip install -e . --no-build-isolation --no-deps
                     :  python setup.py build_ext --inplace
```

> **为什么 ccache 要造「影子 CUDA_HOME」？** 因为 `torch.utils.cpp_extension` 找 nvcc 不是查 `PATH`，而是直接拼 `$CUDA_HOME/bin/nvcc`。所以要骗过它，就得造一个目录布局一模一样、唯独 `bin/nvcc` 被替换成 shim 的「假 CUDA_HOME」。shim 本体（`tools/nvcc`）干的事就是 `exec ccache <真nvcc> "$@"`。

#### 4.4.3 源码精读

脚本头部的用法说明（含三种调用方式）：

- [tools/build_fast.sh:17-22](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L17-L22) —— 默认原地构建、`FFPA_EDITABLE=1` editable、`FFPA_CLEAN=1` 全清重建。

ccache「影子 CUDA_HOME」的实现（关键技巧）：

- [tools/build_fast.sh:41-72](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L41-L72) —— 软链真 CUDA_HOME 除 `bin/` 外的所有条目，`bin/` 内除 `nvcc` 外全部软链，最后用 shim 覆盖 `bin/nvcc`。

`MAX_JOBS` 与 `FFPA_NVCC_THREADS` 自动设置：

- [tools/build_fast.sh:74-82](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L74-L82) —— `MAX_JOBS` 上限 32；`FFPA_NVCC_THREADS` 默认 4。

editable vs 原地的分支：

- [tools/build_fast.sh:101-106](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L101-L106) —— `FFPA_EDITABLE=1` 走 `pip install -e .`，否则 `python setup.py build_ext --inplace`。

构建提速基准数据（最直观的「值不值」证据）：

- [tools/build_fast.sh:113-117](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L113-L117) —— L20 上：无 ccache 冷编译 ~207s；ccache 冷填充 ~214s；**ccache 热重建 ~23s（约 9× 提速）**；子集冷编译（256,512）~48s。

> ⚠️ 脚本尾部有一条重要告警：**不要**用 ccache 包裹 `CC`/`CXX`（[tools/build_fast.sh:179-180](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L179-L180)）。因为 `torch.cpp_extension` 会校验编译器名，发现不是「libtorch 当初编译时用的那个编译器」就会拒绝。所以脚本只对 nvcc 做 ccache，不动主机 `g++`。

#### 4.4.4 代码实践

**实践目标**：用 `build_fast.sh` 做一次「冷→热」对照，亲眼看到 ccache 的提速效果。

**操作步骤**：

1. 确保装了 ccache：`ccache --version`。
2. 第一次（冷，填充缓存），开启 CUDA 扩展：
   ```bash
   ENABLE_FFPA_CUDA_IMPL=1 bash tools/build_fast.sh
   ```
   记下结尾打印的 `total elapsed: Ns`。
3. 全清后立刻重建（热，命中缓存）：
   ```bash
   FFPA_CLEAN=1 ENABLE_FFPA_CUDA_IMPL=1 bash tools/build_fast.sh
   ```
   再记下 `total elapsed`。
4. 对比两次耗时；同时观察第二次构建日志里 ccache 的命中情况（`ccache -s` 可看统计）。

**需要观察的现象**：第一次会看到 nvcc 真正逐个编译翻译单元（日志长、耗时大）；第二次绝大多数翻译单元被 ccache 直接命中（日志里多为 cache hit），耗时骤降。

**预期结果**：根据脚本自带基准（[tools/build_fast.sh:113-117](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L113-L117)），热重建应在 ~20–30 秒量级，相对冷编译约 9× 提速。你机器上的绝对数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`FFPA_EDITABLE=1 bash tools/build_fast.sh` 与默认的原地构建相比，后续工作流有什么不同？

> **答**：editable 安装（`pip install -e .`）会把 `ffpa_attn` 注册为「指向源码目录」的可编辑包。之后只要你改的是 `src/ffpa_attn/` 下的 **Python** 文件，无需重新跑脚本、`import` 立即生效；只有改了 **C++/CUDA** 源码（`csrc/` 或生成文件）才需要重新跑脚本编译 `_C`（见 [tools/build_fast.sh:154-158](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L154-L158)）。原地构建（`build_ext --inplace`）则只是把 `.so` 放到源码树里，不会改 `pip` 的安装记录。

**练习 2**：为什么 `MAX_JOBS` 要封顶 32，而不是越高越好？

> **答**：`MAX_JOBS` 是「外层并行编译几个翻译单元」。每个 nvcc 进程编译 Split-D 重型模板时都很吃内存（且 `FFPA_NVCC_THREADS` 还会让每个进程内部再开几线程）。`MAX_JOBS` 过大会导致内存爆掉或被 OOM killer 干掉。脚本结合经验取 `min(nproc, 32)`（[tools/build_fast.sh:74-79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tools/build_fast.sh#L74-L79)），是个稳妥的上限；机器内存小的话应当手动调小。

---

## 5. 综合实践

把本讲的三种模式、`setup.py`/`env.py` 分工、以及 `build_fast.sh` 串起来，完成下面这个**贯穿任务**：

> **任务**：在本地为 FFPA 搭建一套「日常开发」构建环境，要求：(a) 先以 Triton-only 模式确认能跑；(b) 再切到 CUDA 扩展模式并用 `build_fast.sh` 提速；(c) 最后用 head_dim 子集做一次快速迭代构建。

建议步骤：

1. **Triton-only 基线**（确认环境健康）：
   ```bash
   pip3 install -e . --no-build-isolation
   python3 -c "import ffpa_attn; from ffpa_attn.cuda import CUDA_FWD_AVAILABLE; print(CUDA_FWD_AVAILABLE)"
   ```
   预期：能 import，`CUDA_FWD_AVAILABLE=False`（此刻还没编 `_C`）。
2. **打开 CUDA 扩展 + editable + ccache**（日常开发主力命令）：
   ```bash
   ENABLE_FFPA_CUDA_IMPL=1 FFPA_EDITABLE=1 bash tools/build_fast.sh
   ```
   构建完成后再次 `print(CUDA_FWD_AVAILABLE)`，预期变为 `True`；`import ffpa_attn._C` 不报错。
3. **head_dim 子集快速迭代**（只改了 `ffpa_attn_fwd.cuh`，想快速验证）：
   ```bash
   FFPA_CLEAN=1 ENABLE_FFPA_CUDA_IMPL=1 FFPA_DEV_HEADDIMS="512" \
     FFPA_EDITABLE=1 bash tools/build_fast.sh
   ```
   预期：只重编 head_dim=512 相关翻译单元，配合 ccache 命中，秒级完成。
4. **画一张本讲关系图**：把 `pip` → `setuptools` → `setup.py`(`BUILD_CUDA_EXT`) → `env.py`(`enable_fwd_cuda_impl`/`get_build_sources`/`get_build_cuda_cflags`) → `ffpa_attn._C` → 运行时 `CUDA_FWD_AVAILABLE` 这条链路手绘出来，并在每个节点标注「哪个环境变量控制它」。

> ⚠️ 本任务需要一台有 NVIDIA GPU、装好匹配 PyTorch 与 CUDA toolkit 的 Linux 机器。以上命令的具体耗时与日志**待本地验证**。在没有 GPU 的环境里，只能完成第 1 步（Triton-only），第 2、3 步会因 `get_build_arch_list()` 抛错而失败——届时请改用 `FFPA_SKIP_CUDA_EXT=1` 或显式 `FFPA_BUILD_ARCH`。

## 6. 本讲小结

- FFPA 有三种落地方式：**PyPI 预编译 wheel**（零编译）、**源码 Triton-only**（默认，不编 `_C`）、**源码 + CUDA 前向扩展**（`ENABLE_FFPA_CUDA_IMPL=1`，编译 `ffpa_attn._C`）。
- 是否编译 `_C` 完全由 `setup.py` 的 `BUILD_CUDA_EXT` 决定，它等于 `(not FFPA_SKIP_CUDA_EXT) and ENV.enable_fwd_cuda_impl()`（[setup.py:83-87](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/setup.py#L83-L87)）。
- **`setup.py` 决定「要不要编」，`env.py` 决定「编什么、怎么编」**：head_dim 集合、源文件清单、nvcc 参数、目标 SM 全部来自 `env.py` 的 `ENV` 类。
- 默认源码构建是 Triton-only；CUDA 后端**只有前向**，反向仍走 Triton/SDPA。
- `tools/build_fast.sh` 用 ccache shim + `MAX_JOBS` + `FFPA_NVCC_THREADS` 把全量冷编译从 ~200s 压到热重建 ~20–30s。
- `pyproject.toml` 的 `packages.find` 把 `csrc*` 排除在 Python 包之外，所以 Triton-only 产物里不含任何 C++ 源码；`csrc/` 只在模式 ③ 被编译进二进制 `_C`。

## 7. 下一步学习建议

- 想知道装好之后**怎么用**一行代码替换 SDPA？→ 下一讲 [u1-l4 一行代码替换 SDPA：ffpa_attn_func 与 monkey-patch](./u1-l4-one-line-sdpa-monkey-patch.md)（以及前置的 [u1-l3 仓库目录结构与代码地图](./u1-l3-repo-layout-code-map.md)）。
- 想深入 `env.py` 的运行期开关（精度/swizzle/persist/launch）？→ 留到 u7-l5「运行时 kernel 选择开关」。
- 想看「每个 head_dim 一个翻译单元」的代码生成细节？→ 留到 u7-l3「每个 head_dim 代码生成与 C++ pybind 分发」。
- 遇到拿不准的环境变量，永远先查 [docs/env.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md)，那里有全部变量的默认值与一句话说明。
