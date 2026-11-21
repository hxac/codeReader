# 安装与构建：setup.py 与 CUDA 算子编译

## 1. 本讲目标

上一讲我们画好了仓库的代码地图，知道了 `deepspeed/` 是运行期主包、`op_builder/` 是安装期工具、`csrc/` 是 CUDA 内核源码。本讲回答一个衔接性的问题：**这三个目录是如何在"安装"这个时刻被粘合在一起的？**

学完本讲，你应该能够：

1. 读懂 `setup.py` 的完整执行流程，解释"torch 未安装时降级为 JIT 编译"的机制。
2. 说出 `ALL_OPS` 注册表是如何通过反射自动发现的，以及每个算子的 `BUILD_VAR`（如 `DS_BUILD_CPU_ADAM`）如何控制预编译。
3. 区分三种算子编译路径：纯 pip 安装（不编译）、`DS_BUILD_OPS=1` 预编译、运行时 JIT 编译。
4. 描述默认 compute capability（GPU 架构版本）的推导逻辑，理解 `TORCH_CUDA_ARCH_LIST` 的作用，包括本周期 #9085 带来的变化：**CUDA 13 的默认架构列表更新**。
5. 解释 #9085 修复的「架构列表泄漏」问题：过滤目标的算子（如 fp_quantizer）为什么不再把缩减后的列表写回 `TORCH_CUDA_ARCH_LIST`，以及这个 bug 如何让 10 个算子丢掉 sm_70/sm_75 代码。
6. 解释 #9085 修改后的 `BF16_AVAILABLE` 启用规则：为什么 nvcc >= 12.2 时即使目标包含 pre-Ampere 架构也启用 BF16 内核。
7. 理解安装产物 `deepspeed/git_version_info_installed.py` 如何记录"哪些算子已被预编译"，从而决定运行期走预编译加载还是 JIT。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用两三句话解释）：

- **Python 打包（setuptools）**：`setup.py` 是 Python 项目的"装机说明书"。`pip install .` 时，pip 会执行这个文件，文件里的 `setup(...)` 调用声明了包名、版本、依赖和要编译的扩展模块。`ext_modules` 参数列出的 C++/CUDA 扩展会在安装时被编译成 `.so`（Linux 共享库）文件。
- **CUDA 与 nvcc**：CUDA 是 NVIDIA 的 GPU 编程工具链，`nvcc` 是它的编译器。CUDA C++ 源码（`.cu` 文件）必须用 `nvcc` 编译。
- **compute capability（计算能力，简称 CC）**：NVIDIA 给每代 GPU 标的一个版本号，如 A100 是 8.0、H100 是 9.0。编译 CUDA 代码时必须指定目标架构（生成 `-gencode` 编译参数），架构不匹配则无法运行。习惯上把 8.0（Ampere）之前的架构叫 **pre-Ampere**（如 7.0 Volta、7.5 Turing）。
- **sm_XX 与 PTX**：`sm_XX` 是针对具体架构的机器码；`compute_XX`（PTX）是一种中间表示，驱动可以在**更新**的架构上把它即时翻译成机器码执行。所以给最高目标架构附带 `+PTX` 是一种"向前兼容"兜底。
- **`TORCH_CUDA_ARCH_LIST`**：PyTorch 约定的环境变量，用分号或空格分隔的目标架构列表（如 `"8.0;9.0"`，支持 `9.0a`、`10.0+PTX` 这类后缀）。预编译与 JIT 两条路径都读它；本讲的 4.3 节会看到它在 #9085 前后语义上的关键差别。
- **BF16**：bfloat16，一种指数位与 fp32 相同、尾数位更少的浮点格式。Ampere（8.x）及以后的 GPU 有原生 BF16 算术指令；更老的 GPU 上编译 BF16 内核需要 nvcc 12.2+ 用 float 模拟（详见 4.3）。
- **JIT（Just-In-Time，即时编译）**：与安装时预编译（AOT，Ahead-Of-Time）相对。JIT 指程序**运行中第一次用到某个算子时**才现场编译它，编译产物缓存在磁盘上，下次直接复用。
- **ninja**：一个轻量构建工具，PyTorch 的 `torch.utils.cpp_extension.load()` 用它来加速扩展编译。DeepSpeed 把 `ninja` 直接列为基础依赖。
- **环境变量开关**：DeepSpeed 大量使用"环境变量当开关"的模式，如 `DS_BUILD_OPS=1`、`DS_BUILD_AIO=1`。本讲会看到它们在源码中如何被读取。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [setup.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py) | 安装总入口：探测 torch、遍历算子注册表、决定预编译哪些扩展、生成版本信息文件 |
| [op_builder/builder.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py) | `OpBuilder` / `CUDAOpBuilder` 基类：算子的"编译配方"，含 compute capability 推导、BF16 规则与 JIT 加载（本周期 #9085 全面重构） |
| [op_builder/all_ops.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/all_ops.py) | 用反射自动发现所有 Builder 类，汇成 `ALL_OPS` 注册表 |
| [op_builder/cpu/cpu_adam.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/cpu/cpu_adam.py) | 一个具体算子的 Builder 样例（CPUAdam），展示"几行描述一个算子"的写法 |
| [op_builder/fp_quantizer.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/fp_quantizer.py) | 会过滤架构目标的算子样例：`MIN_COMPUTE_CAPABILITY = 8`（4.3 节的主角） |
| [requirements/requirements.txt](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/requirements/requirements.txt) | 基础依赖表；同目录下其余 `requirements-*.txt` 是各 extras 的依赖表 |
| [install.sh](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/install.sh) | 多机安装脚本：构建 wheel 并通过 pdsh 分发到 hostfile 列出的所有机器 |
| [deepspeed/git_version_info.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/git_version_info.py) | 运行期读取安装信息的入口：优先读安装时生成的 `git_version_info_installed.py` |
| [deepspeed/ops/adam/cpu_adam.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/ops/adam/cpu_adam.py) | 运行期触发 JIT 的调用点样例：`CPUAdamBuilder().load()` |
| [tests/unit/ops/test_op_builder.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/ops/test_op_builder.py) | #9085 配套的回归测试：架构列表不泄漏、BF16 规则、CUDA 版本检查等（本周期大幅扩充） |

---

## 4. 核心概念与源码讲解

### 4.1 setup.py 的 extras 体系与 requirements 依赖表

#### 4.1.1 概念说明

一个 Python 包的依赖分两类：

- **install_requires（基础依赖）**：任何用户装了这个包就一定需要的库。
- **extras_require（可选依赖组）**：按功能划分的可选集合，用户用 `pip install deepspeed[dev]` 这样的语法按需安装。

DeepSpeed 把"哪些依赖属于基础、哪些属于可选"完全外置到 `requirements/` 目录下的一组文本文件里，`setup.py` 只是逐个读取。这样做的好处是：依赖清单可以被 CI、Dockerfile、conda 环境文件（`environment.yml`）等其他工具直接复用，不必重复维护。

#### 4.1.2 核心流程

```text
requirements/requirements.txt ──────────────┐  (基础依赖 install_requires)
requirements/requirements-dev.txt ──────────┤
requirements/requirements-autotuning.txt ───┤
requirements/requirements-autotuning-ml.txt ┤
requirements/requirements-inf.txt ──────────┼─► fetch_requirements() 逐个读入
requirements/requirements-sd.txt ───────────┤     ↓
requirements/requirements-deepcompile.txt ──┤   extras_require 字典
requirements/requirements-torchembed.txt ───┤     ↓
requirements/requirements-readthedocs.txt ──┘   求并集 → extras_require['all']
```

#### 4.1.3 源码精读

先看 setup.py 开头的 torch 探测——这是整个安装流程的第一道分岔口：

[setup.py:L32-L38](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L32-L38)

```python
torch_available = True
try:
    import torch
except ImportError:
    torch_available = False
    print('[WARNING] Unable to import torch, pre-compiling ops will be disabled. ...')
```

这段代码尝试导入 torch，失败时只打印警告并把 `torch_available` 置为 `False`，**不会让安装失败**。这就是"torch 未安装时降级"的第一步：安装可以成功，但所有需要 torch 的编译步骤都会被跳过，算子留待运行时 JIT。

接着 setup.py 从安装期专属的顶层 `op_builder/` 和 `accelerator/` 目录导入工具（这两个目录在 u1-l2 已见过，它们不属于 `deepspeed` 包，只服务于安装阶段）：

[setup.py:L40-L43](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L40-L43)

```python
from op_builder import get_default_compute_capabilities, OpBuilder
from op_builder.all_ops import ALL_OPS, accelerator_name

from accelerator import get_accelerator
```

然后是依赖装载。`fetch_requirements` 只是把文本文件按行读成列表：

[setup.py:L58-L60](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L58-L60)

```python
def fetch_requirements(path):
    with open(path, 'r') as fd:
        return [r.strip() for r in fd.readlines()]
```

[setup.py:L79-L90](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L79-L90) 把 9 个依赖文件装配成 `extras_require` 字典，键就是 pip 的 extras 名，例如 `pip install deepspeed[autotuning]` 会装上 `requirements-autotuning.txt` 里的内容。注意 `'triton': []` 是空列表——注释说明这是为了向后兼容保留的键，如今 Triton 由 PyTorch 自带：

```python
install_requires = fetch_requirements('requirements/requirements.txt')
extras_require = {
    'readthedocs': ..., 'dev': ..., 'autotuning': ..., 'autotuning_ml': ...,
    'inf': ..., 'sd': ..., 'triton': [], 'deepcompile': ..., 'torchembed': ...,
}
```

[setup.py:L96-L101](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L96-L101) 对所有 extras 求并集，生成一个"全家桶"组 `[all]`。此外 [setup.py:L92-L94](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L92-L94) 有一处条件依赖：仅当 torch 可用、加速器是 CUDA 且非 ROCm 时，才把 `nvidia-ml-py`（NVML 的 Python 绑定）追加进基础依赖——这是"按硬件条件裁剪依赖"的范例。

基础依赖本体只有 11 行（[requirements/requirements.txt:L1-L11](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/requirements/requirements.txt#L1-L11)）：

```text
einops
hjson
msgpack
ninja
numpy
packaging>=20.0
psutil
py-cpuinfo
pydantic>=2.0.0
torch>=2.0.0
tqdm
```

值得注意的三个成员：

- `torch>=2.0.0`：torch 本身就是基础依赖，所以正常 `pip install deepspeed` 装完后 torch 一定存在；"torch 未安装"的场景主要出现在**先装 deepspeed 后装 torch**，或用 `--no-deps` 安装时。
- `ninja`：JIT 编译的必备工具，DeepSpeed 干脆把它声明成硬依赖（运行期 JIT 时若 ninja 缺失会直接报错，见 4.2 的 `jit_load`）。
- `py-cpuinfo`：用于探测 CPU 型号与指令集（AVX512/NEON/SVE 等），CPU 算子（如 cpu_adam）据此选择向量化编译参数。#9085 还给它加了 `lscpu` 兜底与 Apple Silicon 修复（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 extras 体系的行为差异——装与不装 `dev` 组，环境里多出哪些包。

**操作步骤**：

1. 创建干净虚拟环境并只装基础包：`python -m venv ~/venv-ds-base && ~/venv-ds-base/bin/pip install -e .`
2. 记录包列表：`~/venv-ds-base/bin/pip list > base.txt`
3. 再创建一个环境装 dev 组：`python -m venv ~/venv-ds-dev && ~/venv-ds-dev/bin/pip install -e .[dev]`
4. 对比：`diff base.txt <(~/venv-ds-dev/bin/pip list)`（或肉眼比对）。

**需要观察的现象**：`dev` 环境额外出现 `pytest`、`pre-commit`、`transformers`、`tensorboard`、`wandb` 等开发期工具（对应 [requirements/requirements-dev.txt](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/requirements/requirements-dev.txt) 的内容）。

**预期结果**：两个环境都能 `import deepspeed` 成功——证明 dev 组的包对运行并非必需。（本实践涉及真实 pip 安装，若网络受限，预期结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nvidia-ml-py` 不直接写进 `requirements.txt`，而要在 setup.py 里条件追加？

**答案**：`nvidia-ml-py` 只对 NVIDIA GPU 有意义。若写死在基础依赖里，CPU/ROCm/其他加速器用户也会被迫安装它。setup.py 在 [L92-L94](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L92-L94) 里通过 `get_accelerator().device_name() == 'cuda'` 探测当前硬件，只在 NVIDIA 环境追加，实现了按硬件裁剪。

**练习 2**：`pip install deepspeed[triton]` 会多装什么？

**答案**：什么也不会多装。`'triton': []` 是空列表（[setup.py:L87](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L87)），注释写明它是为向后兼容保留的键——新版 PyTorch 已自带 Triton。

**练习 3**：`fetch_requirements` 极其简单，为什么不直接在 setup.py 里硬编码依赖列表？

**答案**：依赖外置成纯文本后，`requirements/` 目录可被 CI 配置、Dockerfile、`environment.yml`、`install.sh`（多机分发时会 `pdcp requirements/requirements.txt`）等场景直接引用，单一数据源避免多处维护漂移。

---

### 4.2 OpBuilder 构建入口：ALL_OPS 注册表与三条编译路径

#### 4.2.1 概念说明

`OpBuilder` 是每个 CUDA/C++ 算子的"编译配方"。DeepSpeed 有约 20 个算子（fused_adam、cpu_adam、async_io、transformer……），如果每个都写一份编译脚本会大量重复。做法是：定义一个基类把"编译一个算子"的通用逻辑（找源码、探测 CUDA 版本、生成架构参数、调用编译器）全部封装，每个具体算子只需声明三件事：

1. **`BUILD_VAR`**：控制自己预编译的环境变量名，如 `DS_BUILD_CPU_ADAM`。
2. **`absolute_name()`**：编译产物（`.so`）的 Python 模块全名。
3. **`sources()`**：源码文件相对路径列表。

`ALL_OPS` 则是"算子名 → Builder 实例"的注册表，它**不是手写的**，而是用反射扫描 `op_builder/` 目录下所有以 `Builder` 结尾的类自动生成。

本周期 #9085 对这一层做了一次架构级重构，两条核心设计变化需要先记住：

- **`OpBuilder` 基类变成加速器中立**：不再检查 CUDA/ROCm，编译与 JIT 加载都走钩子（`extra_compile_args()`、`jit_load_kwargs()`、`libraries_args()`、`validate_torch_op_version()`）。`builder()`（预编译）与 `jit_load()`（JIT）**消费同一批钩子**，所以两条路径拿到完全相同的编译/链接参数——修复了此前"预编译与 JIT 用不同 flag"导致的链接错误（如 CPU 加速器上预编译的 `async_io` 缺 `-laio` 而加载失败）。
- **所有 CUDA/ROCm 专属逻辑集中到 `CUDAOpBuilder`**：nvcc 参数、架构列表、BF16 开关、CUDA 工具链检查都搬进这个子类；不需要 CUDA 的算子（如 `async_io`）改由 `CPUOpBuilder` 负责，顶层 `op_builder/async_io.py` 因此被删除。

#### 4.2.2 核心流程

安装时 setup.py 对每个算子做一次"三问决策"：

```text
for op_name, builder in ALL_OPS.items():
    ① op_enabled(op_name)?      # 读算子的 BUILD_VAR 环境变量；未设置则回落到 DS_BUILD_OPS
    ② builder.is_compatible()?  # 该算子在本机硬件/软件上能否编译
    ③ torch_available?          # 预编译必须有 torch

    ①且②且③ → ext_modules.append(builder.builder())   # 预编译，打进 wheel
    否则     → 跳过，运行期首次使用时 JIT
```

默认值由平台决定（[setup.py:L127-L130](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L127-L130)）：**Linux 默认不预编译（`0`），Windows 默认全预编译（`1`）**。Linux 选择 JIT 优先是为了让同一个 wheel 能在任何 GPU 架构上工作；Windows 上 JIT 体验差（需要管理员权限创建符号链接），所以倾向安装时编好。

运行期的分岔则由安装时写下的 `installed_ops` 表驱动：

```text
OpBuilder.load()
  ├─ installed_ops[name] 为 True 且加速器一致
  │    ├─ validate_torch_version / validate_torch_op_version  # 版本指纹校验
  │    └─ importlib.import_module(absolute_name())             # 直接加载预编译 .so
  └─ 否则 → jit_load()                                          # 现场编译并缓存
```

#### 4.2.3 源码精读

**（1）注册表的自动发现。**

[op_builder/all_ops.py:L9-L13](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/all_ops.py#L9-L13) 先解决"安装期能否 import 到加速器模块"的问题：安装时顶层 `accelerator/` 可见，安装后（运行期）则改从 `deepspeed.accelerator` 导入——同一段代码适配两个生命周期阶段：

```python
try:
    # during installation time accelerator is visible, otherwise return deepspeed.accelerator
    from accelerator import get_accelerator
except ImportError:
    from deepspeed.accelerator import get_accelerator
```

[op_builder/all_ops.py:L18-L32](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/all_ops.py#L18-L32) 是注册表本体：用 `pkgutil.iter_modules` 遍历 op_builder 目录下的每个模块，把所有名字以 `Builder` 结尾的类交给加速器工厂实例化，最后汇成字典：

```python
op_builder_dir = get_accelerator().op_builder_dir()
op_builder_module = importlib.import_module(op_builder_dir)
for _, module_name, _ in pkgutil.iter_modules([os.path.dirname(op_builder_module.__file__)]):
    if module_name != 'all_ops' and module_name != 'builder':
        module = importlib.import_module("{}.{}".format(op_builder_dir, module_name))
        for member_name in module.__dir__():
            if member_name.endswith('Builder'):
                builder = get_accelerator().create_op_builder(member_name)
                __op_builders__.append(builder)

ALL_OPS = {op.name: op for op in __op_builders__ if op is not None}
```

注意加速器工厂的作用：在 CUDA 机器上 `FusedAdamBuilder` 会实例化为 CUDA 版 Builder，在 CPU/HPU 等机器上会换成对应加速器的实现（这就是 u3-l1 将讲的加速器抽象在本讲的首次露面）。

**（2）一个算子配方的样子。** 以 CPUAdam 为例，全文不到 30 行：

[op_builder/cpu/cpu_adam.py:L9-L27](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/cpu/cpu_adam.py#L9-L27)

```python
class CPUAdamBuilder(CPUOpBuilder):
    BUILD_VAR = "DS_BUILD_CPU_ADAM"
    NAME = "cpu_adam"

    def absolute_name(self):
        return f'deepspeed.ops.adam.{self.NAME}_op'

    def sources(self):
        return ['csrc/adam/cpu_adam.cpp', 'csrc/adam/cpu_adam_impl.cpp']

    def include_paths(self):
        return ['csrc/includes']
```

声明式到极致：编译哪个文件、编出来叫什么模块，仅此而已。基类 `OpBuilder` 在 [op_builder/builder.py:L220-L233](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L220-L233) 把这两个方法定为抽象方法，强迫每个子类提供。

**（3）setup.py 的决策主循环。**

[setup.py:L147-L155](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L147-L155) 定义"算子是否被启用"：优先读算子专属的 `BUILD_VAR`（如 `DS_BUILD_CPU_ADAM`），没设置则回落到总开关 `DS_BUILD_OPS`：

```python
def op_envvar(op_name):
    return ALL_OPS[op_name].BUILD_VAR

def op_enabled(op_name):
    env_var = op_envvar(op_name)
    return int(get_env_if_set(env_var, BUILD_OP_DEFAULT))
```

[setup.py:L158-L177](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L158-L177) 是主循环，处理"想编但不兼容"（打警告跳过）、"ROCm 上未启用但兼容"（做 hipify 转换备用）、"想编且兼容"（加入 `ext_modules` 预编译）三种组合，其中预编译分支还有一个兜底断言 `assert torch_available`：

```python
install_ops = dict.fromkeys(ALL_OPS.keys(), False)
for op_name, builder in ALL_OPS.items():
    op_compatible = builder.is_compatible()
    ...
    if op_enabled(op_name) and op_compatible:
        assert torch_available, f"Unable to pre-compile {op_name}, please first install torch"
        install_ops[op_name] = op_enabled(op_name)
        ext_modules.append(builder.builder())
```

这段代码体现了"降级"的完整语义：torch 缺失时，即使你显式设置 `DS_BUILD_OPS=1`，预编译请求也会被 [setup.py:L132-L133](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L132-L133) 直接拒绝（`assert torch_available`）；而不设置开关时，一切算子静默留给 JIT。

循环之后还有一个依赖修正逻辑（[setup.py:L179-L192](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L179-L192)）：`async_io` 和 `gds` 在运行期会连带触发 `pin_memory` 算子的 JIT，若前者被预编译而后者没有，无编译器的部署环境就会在运行时翻车——所以这里强制把 `pin_memory` 一起预编译。这是"预编译清单要闭合"的一个真实案例。

**（4）运行期：load 与 jit_load 的分岔。** 安装期讲完了，再看运行期如何"兑现"。[op_builder/builder.py:L489-L505](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L489-L505) 的 `load()` 是运行期加载算子的统一入口：

```python
def load(self, verbose=False):
    if self.name in __class__._loaded_ops:
        return __class__._loaded_ops[self.name]

    from deepspeed.git_version_info import installed_ops, torch_info, accelerator_name
    from deepspeed.accelerator import get_accelerator
    if installed_ops.get(self.name, False) and accelerator_name == get_accelerator()._name:
        self.validate_torch_version(torch_info)      # 版本指纹校验
        self.validate_torch_op_version(torch_info)
        op_module = importlib.import_module(self.absolute_name())  # 直接 import 预编译 .so
        ...
        return op_module
    else:
        return self.jit_load(verbose)                # 现场编译
```

判断依据正是安装时写入的 `installed_ops` 表。预编译路径还要做两道安全检查：`validate_torch_version` 确认运行期 torch 版本与安装时一致（[op_builder/builder.py:L238-L247](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L238-L247)），CUDA 算子再校验 CUDA/HIP 版本（[op_builder/builder.py:L564-L584](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L564-L584)）——版本漂移时给出明确报错而不是让程序崩溃在莫名的 ABI 错误上。

JIT 路径 [op_builder/builder.py:L513-L548](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L513-L548) 依次：兼容性检查 → 校验 ninja 可用 → 调 PyTorch 的 `torch.utils.cpp_extension.load()`，把 `sources()`/`include_paths()`/编译参数全部传过去（这些参数经 [jit_load_kwargs](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L507-L511) 与预编译路径共享同一来源），最终产物缓存在 PyTorch 决定的扩展目录里，并记入类级缓存 `_loaded_ops` 避免重复编译。注意 #9085 删掉了 DeepSpeed 自己的 `DEFAULT_TORCH_EXTENSION_PATH = "/tmp/torch_extensions"` 常量——缓存位置现在完全遵循 PyTorch `torch.utils.cpp_extension` 的规则：优先 `TORCH_EXTENSIONS_DIR` 环境变量，其次 `~/.cache/torch_extensions`（不可写时回退 `/tmp/torch_extensions`）。

调用它的真实业务代码长这样（[deepspeed/ops/adam/cpu_adam.py:L122](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/ops/adam/cpu_adam.py#L122)）：

```python
self.ds_opt_adam = CPUAdamBuilder().load()
```

当你在 ZeRO-Offload 里第一次用到 `DeepSpeedCPUAdam` 时，这一行就会触发 cpu_adam 的 JIT 编译（若它没被预编译）。

顺带一提 [op_builder/builder.py:L117-L129](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L117-L129) 的 `probe_is_compatible()`：`import deepspeed` 时（[deepspeed/ops/__init__.py:L18-L22](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/ops/__init__.py#L18-L22)）会对每个算子做兼容性探测，它把"探测本身抛异常"（如有 GPU 但没装 CUDA 工具链）也当作"不兼容"处理，避免一个你根本不会用的算子让 `import deepspeed` 整体失败。`ds_report`（下一讲）同样复用这个函数。

#### 4.2.4 代码实践

**实践目标**：完整观察"pip 安装（不预编译）→ 运行期 JIT → 编译缓存生成"三步，验证双路径的存在。

**操作步骤**：

1. 创建干净环境并安装（Linux 上 `DS_BUILD_OPS` 默认为 0，即不预编译）：

   ```bash
   python -m venv ~/venv-ds && source ~/venv-ds/bin/activate
   pip install -e . 2>&1 | tee install.log
   ```

2. 确认版本与安装信息（安装脚本会生成 `deepspeed/git_version_info_installed.py`）：

   ```bash
   python -c "import deepspeed; print(deepspeed.__version__)"
   python -c "from deepspeed.git_version_info import installed_ops; print(installed_ops)"
   ```

3. 记录缓存目录的初始状态（JIT 还没发生过，应为空或不存在）：

   ```bash
   ls /tmp/torch_extensions/ 2>/dev/null; ls ~/.cache/torch_extensions/ 2>/dev/null
   ```

4. 主动触发一次 CPU 算子 JIT（cpu_adam 是纯 C++ 算子，**无 GPU 也能编**）：

   ```bash
   python -c "from deepspeed.ops.op_builder import CPUAdamBuilder; CPUAdamBuilder().load(verbose=True)"
   ```

5. 再次查看缓存目录：`ls -R ~/.cache/torch_extensions/ /tmp/torch_extensions/ 2>/dev/null`，找到 `cpu_adam` 子目录里的 `.so` 文件。

**需要观察的现象**：

- 步骤 2 中 `installed_ops` 打印出的所有算子值均为 `False`（因为 Linux 默认不预编译），版本号形如 `0.20.1+<git短哈希>`。
- 步骤 4 会输出一长串编译日志（首次约几十秒到几分钟），末尾打印加载成功。
- 步骤 5 出现 `cpu_adam` 编译产物目录与 `.so` 文件；第二次执行步骤 4 几乎瞬时完成（命中磁盘缓存与 `_loaded_ops` 内存缓存）。

**预期结果**：JIT 产物生成于 `~/.cache/torch_extensions/<python版本目录>/cpu_adam/`（HOME 不可写或设置了 `TORCH_EXTENSIONS_DIR` 时位置不同，以实际观察为准——此点待本地验证）。把 install.log 里 `DS_BUILD_OPS=0` 与 `Install Ops={...全 False...}` 两行截图归档，它们分别来自 [setup.py:L130](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L130) 和 [setup.py:L194](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L194)。

#### 4.2.5 小练习与答案

**练习 1**：在 Linux 上执行 `DS_BUILD_OPS=1 pip install -e .` 但机器上没装 torch，会发生什么？

**答案**：安装直接失败。`DS_BUILD_OPS=1` 时 [setup.py:L132-L133](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L132-L133) 的断言 `assert torch_available, "Unable to pre-compile ops without torch installed..."` 触发。反过来若不设该变量，即使 torch 缺失安装也能成功（算子全部留给 JIT），这正是"降级"设计。

**练习 2**：为什么 `ALL_OPS` 用反射扫描而不是手工维护一个列表？

**答案**：新增算子时只需在 `op_builder/` 下新建一个文件、定义一个 `XxxBuilder` 类，注册表自动收录（[op_builder/all_ops.py:L22-L32](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/all_ops.py#L22-L32)），不会出现"写了 Builder 却忘了注册"的疏漏；同时加速器厂商只需在自己的子目录（如 `op_builder/hpu/`、`op_builder/xpu/`）放实现，即可被同一机制发现。

**练习 3**：#9085 为什么把 `async_io` 从 CUDA builder 挪到 `CPUOpBuilder`？

**答案**：`async_io` 的内核（`io_uring`/`libaio` 相关）是纯主机端代码，不需要 CUDA 工具链。此前它挂在 CUDA builder 下，依赖"`build_for_cpu`"这种"找不到 CUDA 工具链就悄悄退化成 CPU 编译"的模糊行为；#9085 删除了该退化路径，让"主机端算子"与"设备算子"各归其位——`async_io` 归 `CPUOpBuilder`，`gds` 复用它，语义更清晰，也让预编译与 JIT 两条路径给它生成同样的链接参数（缺 `-laio` 的加载失败由此修复）。

---

### 4.3 arch 列表与 BF16 可用性规则：#9085 的修复

#### 4.3.1 概念说明

CUDA 算子编译时有两大"配置旋钮"：**给哪些架构生成代码**（arch 列表）与**是否启用 BF16 内核**（`BF16_AVAILABLE` 宏）。本周期 #9085（提交 `e35282bdb`，"Fix multiple issues in Op Builder and refactor implementation"）集中修了这一层的四个问题：

1. **架构列表泄漏**：有些算子声明了最低支持的 compute capability（如 fp_quantizer 要求 >= 8.0），构建时会把自己的目标列表**过滤**后再用。修复前，过滤后的列表会被写回 `TORCH_CUDA_ARCH_LIST` 环境变量——而 setup.py 在**同一个进程**里按字典序逐个构建所有算子，于是排在 fp_quantizer 之后的每个算子都继承了这份被削减的列表。提交说明给出的实测：请求 `7.0;7.5;8.0;9.0` 时，**10 个算子出厂时缺了 sm_70/sm_75 代码**，在 Volta/Turing 老卡上直接无法加载。
2. **BF16 内核被 pre-Ampere 目标"一票否决"**：旧规则是"所有目标都 >= sm_80 才启用 `BF16_AVAILABLE`"。默认架构列表里含 6.x/7.x，所以全量预编译时 `transformer_inference`、`inference_core_ops` 等算子根本没有 bf16 内核，88 个相关算子测试失败。而 nvcc 12.2+ 能为 pre-Ampere 目标编译这些内核（在 float 中计算 bf16 算术），所以新规则改为：**目标全为 Ampere+ 即启用；否则只要 nvcc >= 12.2 也启用**，老工具链保持旧规则。
3. **CUDA 13 默认列表**：默认列表 `"6.0;6.1;7.0"` 在 CUDA 13 下一项都编不了（CUDA 13 不再支持 7.5 以下架构），默认预编译直接失败。现在 CUDA 13 起默认列表改为从 7.5 开始。
4. **CUDA 版本检查表过时**：旧实现用一张手工维护的次版本兼容表（`cuda_minor_mismatch_ok`），每出新的 CUDA 版本都要先测再补，CUDA 13 没有条目导致 torch(CUDA 13.0) + nvcc 13.1 被误判不兼容。新实现改用 PyTorch 自己的规则：**主版本不匹配报错（`DS_SKIP_CUDA_CHECK=1` 可强行放行），次版本不匹配仅告警**，并且 JIT 路径也纳入检查（PyTorch 的 `load()` 本来不查）。

顺带被删除的还有 `csrc/includes/compat.h` 兼容层与 `version_dependent_macros`——它们只服务于 torch 1.x，如今最低要求已是 torch>=2.0。

#### 4.3.2 核心流程

`CUDAOpBuilder.compute_capability_args()` 是 arch 决策的中枢，JIT 与预编译两种模式走向不同出口：

```text
compute_capability_args()
  │
  ├─ jit_mode = True（运行期 JIT，为"眼前这台机器"编）
  │    优先级：jit_load() 暂存的调用方 TORCH_CUDA_ARCH_LIST
  │          > 环境变量 TORCH_CUDA_ARCH_LIST
  │          > 探测可见 GPU 的真实架构
  │          > （fork 子进程中无显式列表 → 直接报错，避免污染 CUDA 上下文）
  │    过滤 + 去重 + 给最高无后缀架构补 +PTX
  │    → 把过滤后的列表写入 TORCH_CUDA_ARCH_LIST、返回 []
  │      （让 PyTorch 自己生成 -gencode；jit_load() 结束后恢复调用方原值）
  │
  └─ jit_mode = False（setup.py 预编译，为"别的机器"编）
       优先级：TORCH_CUDA_ARCH_LIST > get_default_compute_capabilities()
       过滤 + 去重 + 给最高无后缀架构补 +PTX
       → 直接返回显式 -gencode 参数，【不碰】TORCH_CUDA_ARCH_LIST
         （setup.py 单进程构建全部算子，每个算子都必须从"请求的列表"出发）
```

BF16 决策挂在同一条链上：

```text
enable_bf16 = 所有目标架构 major >= 8 ?
              或 (存在 CUDA 工具链 且 nvcc 版本 >= (12, 2))
enable_bf16 → 给 cxx/nvcc 参数追加 -DBF16_AVAILABLE、
              并 -U__CUDA_NO_BFLOAT16*__ 一组宏（解除 nvcc 对 bf16 运算符的默认禁用）
```

#### 4.3.3 源码精读

**（1）默认架构列表与 CUDA 13。** 基类顶部的保守默认值（[op_builder/builder.py:L24](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L24)）：

```python
DEFAULT_COMPUTE_CAPABILITIES = "6.0;6.1;7.0"
```

（#9085 同时删掉了它旁边的 `DEFAULT_TORCH_EXTENSION_PATH` 常量，缓存目录交由 PyTorch 决定，见 4.2。）[op_builder/builder.py:L132-L151](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L132-L151) 的 `get_default_compute_capabilities()` 按本机 CUDA 大版本扩展这个基线，**新增的 CUDA 13 分支是本周期变化**：

```python
compute_caps = DEFAULT_COMPUTE_CAPABILITIES        # "6.0;6.1;7.0"
if installed_cuda_version()[0] == 11:
    ...逐级追加 "8.0" / "8.6" / "9.0"
elif installed_cuda_version()[0] == 12:
    compute_caps += ";8.0;8.6;9.0"
    if installed_cuda_version()[1] >= 8:
        compute_caps += ";10.0;12.0"
elif installed_cuda_version()[0] >= 13:
    # CUDA 13 cannot build for compute capabilities below 7.5, which rules out the whole base list
    compute_caps = "7.5;8.0;8.6;9.0;10.0;12.0"
```

也就是说：CUDA 12 环境下默认给 6.0～9.0（12.8 起含 10.0/12.0）全部编一遍——预编译的 wheel 因此"通吃"大多数 GPU，代价是编译时间成倍增长；CUDA 13 环境下则从 7.5 起步（7.5 以下编不了，基线整个作废）。这也解释了为什么 Linux 默认走 JIT：JIT 只编"眼前这块卡"的架构，编译又快又省。

其中反复调用的 `installed_cuda_version()` 现在带着一层进程级缓存（[op_builder/builder.py:L43-L67](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L43-L67)）：`nvcc -V` 的输出解析被包进 `@functools.lru_cache` 的 `_nvcc_version()`——每个算子都问一遍工具链版本，但同一个 nvcc 路径在一个进程里只真正执行一次（CPU 探测、`rocminfo` 同理）。

**（2）架构列表的解析。** #9085 引入了统一的 `CudaArch` 类型与解析器，JIT 和预编译两条路径共用（[op_builder/builder.py:L154-L182](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L154-L182)）：

```python
class CudaArch(NamedTuple):
    """One TORCH_CUDA_ARCH_LIST entry, such as 8.0, 9.0a or 10.0+PTX."""
    major: int
    minor: int
    suffix: str    # "a" marks an architecture-specific target
    ptx: bool

def _parse_cuda_arch_list(arch_list):
    ...
    match = re.fullmatch(r"(\d+)\.(\d+)(a?)(\+PTX)?", entry)
    if match is None:
        raise ValueError(f"Unsupported TORCH_CUDA_ARCH_LIST entry '{entry}'. ...")
```

修复前的两个坑：`9.0a`（Hopper/Blackwell 上架构特化的 TMA 等特性需要它）与带空项的 `8.0;8.6;` 都会 `ValueError` 崩溃；现在 `a` 后缀、`+PTX` 都被正确支持，`Ampere` 这类含糊的名字则给出清晰报错。

**（3）会过滤架构的算子。** `CUDAOpBuilder` 定义了类属性 `MIN_COMPUTE_CAPABILITY`（[op_builder/builder.py:L553-L556](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L553-L556)），[filter_ccs()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L769-L780) 据此砍掉太老的目标并打警告。fp_quantizer 就是一个典型（[op_builder/fp_quantizer.py:L9-L12](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/fp_quantizer.py#L9-L12)）：

```python
class FPQuantizerBuilder(CUDAOpBuilder):
    BUILD_VAR = "DS_BUILD_FP_QUANTIZER"
    NAME = "fp_quantizer"
    MIN_COMPUTE_CAPABILITY = 8
```

仓库里声明了这个属性的算子还有 `evoformer_attn`（7）、`inference_core_ops`（6）、`inference_cutlass_builder`（8）、`ragged_ops`（8）、`transformer_inference`（6）等——#9085 之前它们各自抄了七份几乎相同的目标过滤与 GPU 检查代码，现在收敛成一个类属性加两个基类方法。

**（4）决策中枢与"泄漏"修复。** [compute_capability_args()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L663-L767) 的完整逻辑分两支。JIT 分支解析目标的优先级写在 [L689-L705](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L689-L705)：暂存值 > 环境变量 > 探测可见 GPU；特别地，在"CUDA 已初始化的 fork 子进程"里探测会污染上下文，此时若无显式列表则直接抛出可操作的错误：

```python
if hasattr(torch.cuda, '_is_in_bad_fork') and torch.cuda._is_in_bad_fork():
    raise RuntimeError(
        f"DeepSpeed JIT builder for '{self.name}' cannot probe CUDA device capabilities "
        "in a forked subprocess ... Set TORCH_CUDA_ARCH_LIST to specify target architectures explicitly.")
```

（配套的 `cuda_capability_major()`，[op_builder/builder.py:L645-L661](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L645-L661)，在 CUDA 上下文未初始化时干脆返回 `None` 跳过检查——保证 `import deepspeed` 不会偷偷初始化 CUDA、不破坏基于 fork 的多进程。）

两支公共的收尾逻辑：`filter_ccs()` 过滤（[L713-L716](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L713-L716)，过滤后为空则报错）→ 按 (major, minor, suffix) 去重（[L724-L728](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L724-L728)，与 PyTorch 自己的去重规则对齐，`8.0;8.0+PTX` 折叠成一条 sm_80 加一条 PTX）→ 没有任何 `+PTX` 时给**最高的无后缀架构**补上 PTX（[L731-L736](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L731-L736)；#9085 起预编译产物也享受这个兜底，以前只有 JIT 有）。

真正的"泄漏"修复体现在两个出口的不对称上。JIT 出口（[L745-L753](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L745-L753)）：

```python
if self.jit_mode:
    # Before torch 2.9, load() adds -gencode flags from TORCH_CUDA_ARCH_LIST ... even
    # when extra_cuda_cflags already has some. So set the env var to the filtered arch list ...
    # jit_load() restores the caller's value.
    os.environ["TORCH_CUDA_ARCH_LIST"] = ";".join(str(cc) for cc in ccs)
    return []
```

JIT 模式确实会写环境变量——但写的是**过滤后**的列表（防止 PyTorch 把 `filter_ccs()` 砍掉的架构加回来），而且外层的 [CUDAOpBuilder.jit_load()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L841-L858) 在 `finally` 里**恢复调用方的原值**：

```python
def jit_load(self, verbose=True):
    torch_arch_list_present = "TORCH_CUDA_ARCH_LIST" in os.environ
    torch_arch_list = os.environ.get("TORCH_CUDA_ARCH_LIST")
    ...
    try:
        return super().jit_load(verbose)
    finally:
        if torch_arch_list_present:
            os.environ["TORCH_CUDA_ARCH_LIST"] = torch_arch_list
        else:
            os.environ.pop("TORCH_CUDA_ARCH_LIST", None)
```

预编译出口（[L755-L767](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L755-L767)）则**根本不碰环境变量**，直接返回显式 `-gencode`：

```python
# Non-JIT: return explicit flags per builder for extra_compile_args.
# BuildExtension uses these instead of TORCH_CUDA_ARCH_LIST, so leave the env var alone: setup.py
# builds every op in one process, and each op must start from the requested arch list.
args = []
for cc in ccs:
    num = f"{cc.major}{cc.minor}{cc.suffix}"
    args.append(f'-gencode=arch=compute_{num},code=sm_{num}')
    if cc.ptx:
        args.append(f'-gencode=arch=compute_{num},code=compute_{num}')
return args
```

注释把原因说得很清楚：`setup.py` 在一个进程里构建所有算子，**每个算子都必须从"请求的完整列表"出发**。修复前的行为是：fp_quantizer 先把列表过滤成 `8.0;9.0` 再写回 `TORCH_CUDA_ARCH_LIST`，后续算子（它们自己 `MIN_COMPUTE_CAPABILITY` 为 None 或更低）读到的是被削减过的列表——`7.0;7.5;8.0;9.0` 的请求最终让 10 个算子缺了 sm_70/sm_75。这个反差还有个讽刺的副产品：全量预编译本来会因默认列表里的 6.x/7.x 触发"BF16 一票否决"，恰好是这次泄漏砍掉了老架构才让 BF16 意外存活——两个 bug 互相掩盖。

对应的回归测试（本周期新增）在 [tests/unit/ops/test_op_builder.py:L231-L245](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/ops/test_op_builder.py#L231-L245)：

```python
def test_prebuilt_op_that_drops_archs_does_not_drop_them_for_the_ops_built_after_it():
    # setup.py pre-builds every op in one process. An op that drops the archs it cannot run on, as
    # fp_quantizer drops those below 8.0, must leave them in place for the ops built after it.
    def keep_ampere_and_newer(ccs):
        return [cc for cc in ccs if cc.major >= 8]

    with patch.dict(os.environ, {"TORCH_CUDA_ARCH_LIST": "7.0;7.5;8.0;9.0"}, clear=False):
        make_builder(jit_mode=False, filter_ccs=keep_ampere_and_newer).compute_capability_args()
        next_op_args = make_builder(jit_mode=False).compute_capability_args()

    assert next_op_args == [
        "-gencode=arch=compute_70,code=sm_70",
        "-gencode=arch=compute_75,code=sm_75",
        ...
```

第二个 builder 拿到的仍包含 sm_70/sm_75——这正是修复后的契约。

**（5）BF16 规则。** [op_builder/builder.py:L738-L743](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L738-L743)：

```python
# Ampere (8.x) and newer GPUs have native bf16 arithmetic. For older targets, nvcc 12.2+ computes it in
# float instead, while older toolkits fail to compile the BF16 kernels for them.
self.enable_bf16 = all(cc.major >= 8 for cc in ccs)
if not self.enable_bf16:
    from torch.utils.cpp_extension import CUDA_HOME
    self.enable_bf16 = CUDA_HOME is not None and installed_cuda_version() >= (12, 2)
```

启用后在 [extra_compile_args()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L860-L875) 里落到编译参数（cxx 与 nvcc 两边都加宏）：

```python
if self.enable_bf16:
    cxx_args.append("-DBF16_AVAILABLE")
    nvcc_args.append("-DBF16_AVAILABLE")
    nvcc_args.append("-U__CUDA_NO_BFLOAT16_OPERATORS__")
    nvcc_args.append("-U__CUDA_NO_BFLOAT162_OPERATORS__")
    nvcc_args.append("-U__CUDA_NO_BFLOAT16_CONVERSIONS__")
```

参数化的回归测试 [tests/unit/ops/test_op_builder.py:L178-L199](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/ops/test_op_builder.py#L178-L199) 把规则边界钉死：`7.0;8.0` + nvcc 12.1 → 不启用；`7.0;8.0` + nvcc 12.2 → 启用；`8.0;9.0` + nvcc 12.1 → 启用。

**（6）CUDA 版本检查的新规则。** [assert_no_cuda_mismatch()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L185-L209) 拿 `installed_cuda_version()`（nvcc 的版本）与 `torch.version.cuda`（torch 编译时用的 CUDA 版本）比对：主版本不一致抛 `CUDAMismatchException`（`DS_SKIP_CUDA_CHECK=1` 可强行放行，[L198-L204](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L198-L204)）；次版本不一致只打警告（[L193-L197](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L193-L197)）——理由写在注释里：CUDA 保证同一大版本内向后兼容。手工维护的 `cuda_minor_mismatch_ok` 表被整体删除。这个检查现在由 `extra_compile_args()` 统一调用（[L860-L864](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L860-L864)），而预编译与 JIT 都经过它，所以 JIT 路径也首次被覆盖。

另有一个小但相关的清理：[cxx_args()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L905-L910) 不再传任何 `-std` 旗标——PyTorch 会为自己的头文件加上所需 C++ 标准（torch 2.12 前是 C++17，之后是 C++20），命令行上后出现的 `-std` 会把它覆盖掉。

#### 4.3.4 代码实践

**实践目标**：用 #9085 的回归测试亲眼验证两条规则——架构列表不泄漏、BF16 依赖 nvcc 版本而非"全员 Ampere"。

**操作步骤**：

1. 环境里有 torch 即可（以下测试把 CUDA 探测全部 mock 掉了，**无 GPU 也能跑**）：

   ```bash
   pip install -e . && pip install pytest
   pytest tests/unit/ops/test_op_builder.py -v -k "prebuilt_op_that_drops or bf16_kernels" 
   ```

2. 精读 [test_prebuilt_op_that_drops_archs_does_not_drop_them_for_the_ops_built_after_it](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/ops/test_op_builder.py#L231)：注意它连续构造**两个** builder——第一个过滤掉 7.x，断言第二个仍拿到 sm_70/sm_75 的 `-gencode`。
3. 对照修复前行为做推演（源码阅读型实践）：假设 `compute_capability_args()` 在非 JIT 分支也执行 `os.environ["TORCH_CUDA_ARCH_LIST"] = ";".join(...)`，沿着 4.2.3(3) 的 setup.py 主循环走一遍——`ALL_OPS` 按注册表顺序构建，fp_quantizer（`MIN_COMPUTE_CAPABILITY = 8`）把它之后的算子目标列表削成 `8.0;9.0`。
4. 运行参数化的 BF16 测试并对照参数表：

   ```bash
   pytest tests/unit/ops/test_op_builder.py -v -k "test_bf16_kernels_are_built_for_pre_ampere_targets_only_from_nvcc_12_2"
   ```

**需要观察的现象**：

- 步骤 1 两条测试全绿；`-v` 输出里 BF16 测试按参数展开成 3 条子用例。
- 步骤 3 的推演结论：请求 `7.0;7.5;8.0;9.0` 时，所有排在过滤算子之后、且自身不声明 `MIN_COMPUTE_CAPABILITY` 的算子都丢了 sm_70/sm_75——提交说明统计为 10 个算子。

**预期结果**：能用一句话向同伴解释"为什么预编译出口必须返回显式 `-gencode` 而不是写环境变量"（答案：setup.py 单进程构建全部算子，环境变量是进程级共享状态，写它会让后续算子继承被削减的列表）。（pytest 实际输出待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 JIT 分支可以写 `TORCH_CUDA_ARCH_LIST`，预编译分支却不可以？

**答案**：写环境变量的影响范围是整个进程。JIT 发生在运行期，一次只构建一个算子，且 [CUDAOpBuilder.jit_load()](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L841-L858) 用 try/finally 保证结束后恢复调用方原值；预编译发生在 setup.py 的**单个进程**里，所有算子依次构建，任何一个算子写环境变量都会污染后续算子——fp_quantizer 过滤后的 `8.0;9.0` 曾这样"泄漏"给后面 10 个算子。所以预编译分支改为返回显式 `-gencode` 旗标（[L755-L767](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L755-L767)），各算子互不干扰。

**练习 2**：nvcc 12.1 与 12.4 下，同样的目标列表 `7.0;8.0;9.0` 编出的算子在 BF16 上有何差别？

**答案**：12.1 下 `all(cc.major >= 8)` 为 False 且版本 < (12, 2)，`BF16_AVAILABLE` 不定义——pre-Ampere 目标上编译 BF16 内核会失败，干脆整个算子不带 bf16 路径；12.4 下第二条件命中，宏被定义，nvcc 在 7.0 目标上用 float 模拟 bf16 算术，老卡（如 V100，对应 issue #6839 一类失败）也能跑 bf16 内核。这正是 [tests/unit/ops/test_op_builder.py:L178-L186](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/tests/unit/ops/test_op_builder.py#L178-L186) 参数表覆盖的三种组合。

**练习 3**：torch 用 CUDA 13.0 编译、系统 nvcc 是 13.1，修复前后各会发生什么？

**答案**：修复前，手工维护的 `cuda_minor_mismatch_ok` 表没有 CUDA 13 的条目，`assert_no_cuda_mismatch()` 抛 `CUDAMismatchException`，构建直接失败——每出一个新 CUDA 版本都得先人工测试再补表。修复后按 PyTorch 的规则处理：主版本一致（13 == 13）、次版本不一致只打警告（[op_builder/builder.py:L193-L197](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L193-L197)），构建继续。

---

### 4.4 版本指纹、双轨 op_builder 与 install.sh

#### 4.4.1 概念说明

前三个模块分别讲了"装什么依赖"、"怎么决定编哪些算子"和"怎么选架构与精度宏"。本模块补齐剩下的拼图：

- **版本指纹**：安装时把版本、git 哈希、已装算子表、torch/CUDA 版本写进一个 Python 文件，运行期直接 import 它，据此判断"预编译的算子还能不能用"。
- **双轨 op_builder**：顶层 `op_builder/`（安装期视角）与 `deepspeed/ops/op_builder`（运行期视角）看起来是两个包路径，实际在 Linux/macOS 上是**符号链接**指向同一批文件；Windows 不支持开发期符号链接，setup.py 会在构建 wheel 时把它复制成实体目录。

#### 4.4.2 核心流程

版本字符串的组装规则（[setup.py:L228-L246](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L228-L246)）：

```text
读取 version.txt (0.20.1)
  ├─ 设置了 DS_BUILD_STRING ？ → 追加构建后缀（官方发 wheel 用），写入 build.txt
  ├─ 存在 build.txt ？         → 追加其中的内容（从分发包安装）
  └─ 否则（源码安装）          → 追加 "+<git 短哈希>"
= 最终 version_str
```

随后 [setup.py:L267-L282](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L267-L282) 收集 `torch_info`（torch 版本、bf16 支持、CUDA/NCCL/HIP 版本，CPU-only 时各版本记为 `"0.0"`），把版本串、git 信息、`install_ops` 表、加速器名、`torch_info` 一起**写死**到 `deepspeed/git_version_info_installed.py`。安装产物与运行期的对接点在 [deepspeed/git_version_info.py:L6-L24](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/git_version_info.py#L6-L24)：能导入安装生成的文件就用它；导不进（比如未安装的源码树、readthedocs 环境）则回退——版本读 `version.txt`，`installed_ops` 由 `ALL_OPS` 生成全 `False` 字典，`torch_info` 各项填 `"0.0"` 哨兵值，交给运行期自行探测。

双轨结构的切换逻辑在 [op_builder/__init__.py:L17-L22](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/__init__.py#L17-L22)：优先把自己挂到 `deepspeed.ops.op_builder` 命名空间下（运行期），失败才退回顶层 `op_builder`（安装期）。安装期 torch 可能还没装，无法立即实例化 Builder，于是返回一个**闭包**延迟到运行期再构造（[op_builder/__init__.py:L28-L42](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/__init__.py#L28-L42) 的 `builder_closure`）。

#### 4.4.3 源码精读

**（1）符号链接证据。** 在仓库里直接可见：

```text
deepspeed/ops/csrc        -> ../../csrc          （内核源码）
deepspeed/ops/op_builder  -> ../../op_builder    （编译配方）
```

所以"安装期工具"与"运行期模块"共享同一份源码，`MANIFEST.in`（[L6-L9](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/MANIFEST.in#L6-L9)）确保 `csrc/`、`op_builder/`、`accelerator/` 里的文件也进 wheel——JIT 模式下用户机器上必须有源码才能现场编译。（#9085 删除了 `csrc/includes/compat.h`，但该目录下的其他公共头文件如 `cpu_adam.h`、`conversion_utils.h` 等仍在。）

**（2）Windows 的实体复制。** [setup.py:L216-L226](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L216-L226) 在 win32 平台上 `shutil.rmtree` 删除包内的链接目录、`shutil.copytree` 把 `csrc/`、`op_builder/`、`accelerator/` 复制进 `deepspeed/ops/` 下——这也呼应了 setup.py 文件头（[L8-L18](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L8-L18)）"Windows 构建需管理员权限创建符号链接目录"的说明。

**（3）版本指纹文件。** 安装时写入的关键内容（[setup.py:L276-L282](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L276-L282)）：

```python
with open('deepspeed/git_version_info_installed.py', 'w') as fd:
    fd.write(f"version='{version_str}'\n")
    fd.write(f"git_hash='{git_hash}'\n")
    fd.write(f"git_branch='{git_branch}'\n")
    fd.write(f"installed_ops={install_ops}\n")
    fd.write(f"accelerator_name='{accelerator_name}'\n")
    fd.write(f"torch_info={torch_info}\n")
```

`installed_ops` 与 `accelerator_name` 正是 4.2 中 `OpBuilder.load()` 分岔判断的数据来源：换了加速器或换了 torch 版本，预编译算子即被判为不可用，自动转 JIT 或报出清晰的版本不匹配错误。`install.sh` 的清理步骤（[install.sh:L128-L134](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/install.sh#L128-L134)）每次构建前删除旧的 `deepspeed/git_version_info_installed.py`、`dist`、`build`，避免陈旧指纹污染新 wheel。

**（4）多机安装脚本。** [install.sh:L154-L161](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/install.sh#L154-L161) 本机路径：`python -m build --wheel --no-isolation` 构建 wheel → `pip install dist/deepspeed*.whl` → 运行 `ds_report` 自检；存在 hostfile 时则走 [install.sh:L163-L181](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/install.sh#L163-L181)，用 `pdsh`/`pdcp` 把 wheel 和 requirements 分发到所有节点逐一安装——这在 GPU 集群上保证每台机器的编译环境一致。

#### 4.4.4 代码实践

**实践目标**：追踪"版本指纹"从生成到消费的全链路。

**操作步骤**：

1. 若已按 4.2.4 完成安装，打开仓库里的 `deepspeed/git_version_info_installed.py`（未安装则此文件不存在，这本身就是观察点），对照 [setup.py:L276-L282](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L276-L282) 逐行核对其六个字段。
2. 运行：

   ```bash
   python - <<'EOF'
   import deepspeed
   from deepspeed.git_version_info import version, git_hash, installed_ops, torch_info
   print("version =", version)
   print("git_hash =", git_hash)
   print("installed_ops =", installed_ops)
   print("torch_info =", torch_info)
   print("deepspeed.__version__ =", deepspeed.__version__)
   EOF
   ```

3. 用 `readlink deepspeed/ops/op_builder` 验证符号链接指向 `../../op_builder`，再用 `python -c "import deepspeed.ops.op_builder as m; print(m.__file__)"` 观察运行期解析出的路径。

**需要观察的现象**：步骤 2 的 `version` 带 `+<短哈希>` 后缀且与 `deepspeed.__version__` 一致；`torch_info` 中的 `cuda_version` 在无 GPU 环境下为 `"0.0"`。步骤 3 中 `__file__` 解析到 `deepspeed/ops/op_builder/__init__.py`（经由符号链接落到顶层 `op_builder/` 的真实文件）。

**预期结果**：版本指纹链路（`version.txt` → setup.py → `git_version_info_installed.py` → `deepspeed.git_version_info`）闭环验证通过。（符号链接行为在 Windows 上不同——会是复制出的实体目录，见 setup.py L216-L226；Linux 实测结果待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么运行期加载预编译算子前要校验 `torch_info`，而不是直接 import？

**答案**：`.so` 是针对特定 torch/CUDA ABI 编译的。若安装后又升级了 torch，旧的预编译产物可能无法加载或产生未定义行为。[op_builder/builder.py:L489-L503](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/op_builder/builder.py#L489-L503) 在 import 前调用 `validate_torch_version` / `validate_torch_op_version`，把潜在的诡异崩溃转换成"请重新安装 DeepSpeed"的明确提示。

**练习 2**：未安装的源码树上 `import deepspeed` 会读到什么版本号？`installed_ops` 是什么？

**答案**：由 [deepspeed/git_version_info.py:L9-L24](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/git_version_info.py#L9-L24) 的回退分支决定：版本读 `version.txt`（即 `0.20.1`，无哈希后缀），`git_hash` 为 `[none]`，`installed_ops` 由 `ALL_OPS` 生成全 `False`，`torch_info` 各版本为哨兵值 `"0.0"`。

**练习 3**：`install.sh` 为什么构建 wheel 而不是直接 `pip install .`，还要配合 pdsh？

**答案**：见 [install.sh:L154-L181](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/install.sh#L154-L181)。先产出 wheel 再分发，保证集群所有节点安装的是**同一次构建**的二进制（而不是各节点各自编译、结果不一致）；`pdcp` wheel + `pdsh` 安装 + `ds_report` 自检是多机环境的标准三连。

---

## 5. 综合实践

**任务：写一份《我的 DeepSpeed 安装画像》报告，把本讲四条线串起来。**

在一个干净虚拟环境中完成以下步骤并归档输出：

1. **依赖线**：`pip install -e .`，从安装日志中找出 `install_requires=[...]` 一行（来自 [setup.py:L284](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/setup.py#L284)），与 `requirements/requirements.txt` 逐项核对。
2. **算子线**：记录日志中的 `DS_BUILD_OPS=...` 与 `Install Ops={...}` 两行；然后对比第二次安装（`DS_BUILD_CPU_ADAM=1 pip install -e .`），观察 `Install Ops` 中 `cpu_adam` 变为 `1/True`、`ext_modules` 不再为空，以及 `import deepspeed.ops.adam.cpu_adam_op` 能直接成功（预编译产物）而无需 JIT。
3. **指纹线**：两次安装各打印一次 `deepspeed.__version__` 与 `installed_ops['cpu_adam']`，说明两次差异的来源。
4. **arch 线（#9085）**：跑通 4.3.4 的两条回归测试，然后用 200 字解释"架构列表泄漏"：修复前为什么 fp_quantizer 之后构建的 10 个算子会缺 sm_70/sm_75 代码，修复后预编译出口为什么改返回显式 `-gencode`。
5. 最后用 200 字总结：三种编译路径（pip 默认 JIT / DS_BUILD_OPS 全预编译 / 单算子 BUILD_VAR 预编译）各自的适用场景——提示：无编译器的生产环境用预编译 wheel；开发机上 JIT 更省时。

（GPU 机器上可加做一步：`TORCH_CUDA_ARCH_LIST="8.0" DS_BUILD_FUSED_ADAM=1 pip install -e .`，对照 4.3 讲的架构推导，确认只编了 sm_80。无 GPU 环境此步待本地验证。）

## 6. 本讲小结

- **extras 体系**：DeepSpeed 把依赖外置到 `requirements/*.txt`，setup.py 装配成 `install_requires` + 9 个 extras 组 + 自动求并集的 `[all]`；`nvidia-ml-py` 等硬件相关依赖按加速器类型条件追加。
- **ALL_OPS 注册表**：通过反射扫描 `op_builder/` 下所有 `*Builder` 类自动生成，每个算子用 `BUILD_VAR` + `absolute_name()` + `sources()` 完成声明式注册；#9085 后预编译与 JIT 共享同一批编译钩子，两条路径拿到的 flag 完全一致。
- **三种编译路径**：Linux 上 pip 默认安装不预编译任何算子（`DS_BUILD_OPS=0`）；设 `DS_BUILD_OPS=1` 或单个 `DS_BUILD_*` 变量触发预编译；未预编译的算子在运行期首次使用时经 `OpBuilder.load()` → `jit_load()` 现场 JIT，产物缓存在 `~/.cache/torch_extensions`（或 `TORCH_EXTENSIONS_DIR`）。
- **arch 列表规则（#9085）**：默认列表按 CUDA 版本扩展，CUDA 13 起从 7.5 起步；JIT 优先 `TORCH_CUDA_ARCH_LIST`（写入过滤后的列表并由 `jit_load` 恢复原值），预编译返回显式 `-gencode` 且不碰环境变量——修复了"过滤算子把削减后的列表泄漏给后续算子、10 个算子缺 sm_70/sm_75"的问题；两条路径都给最高无后缀架构补 `+PTX`。
- **BF16 可用性（#9085）**：目标全为 Ampere+ 即启用 `BF16_AVAILABLE`；否则 nvcc >= 12.2 也启用（pre-Ampere 上以 float 模拟 bf16 算术），老工具链保持旧规则。
- **CUDA 版本检查（#9085）**：手工兼容表换成 PyTorch 规则——主版本不匹配报错（`DS_SKIP_CUDA_CHECK=1` 可跳过）、次版本不匹配仅告警，且 JIT 路径也被纳入检查。
- **版本指纹与双轨**：安装时把 `installed_ops`/`torch_info` 写入 `git_version_info_installed.py`，运行期据此选择"直接 import 预编译 .so"或"转 JIT"，并做 torch/CUDA 版本防漂移校验；顶层 `op_builder/` 与 `deepspeed/ops/op_builder` 经符号链接共享同一份源码（Windows 复制实体）。

## 7. 下一步学习建议

安装构建打通后，下一讲 **u1-l4《启动与诊断：deepspeed 命令与 ds_report》**将进入"如何把训练跑起来"：`bin/deepspeed` 命令如何分发参数、`ds_report` 如何探测并汇总本讲提到的 torch/CUDA/ninja/算子状态。建议顺带阅读的源码：

- [deepspeed/env_report.py](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/deepspeed/env_report.py)——它消费的正是本讲的 `git_version_info`、`ALL_OPS` 与 `probe_is_compatible`，是天然的复习材料。
- [build_win.bat](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/build_win.bat) 与 [docker/](https://github.com/deepspeedai/DeepSpeed/blob/7681845b3e20b371e28f1affe6cd6b813f1aa237/docker) 目录——两种更重的构建交付形态。
- 若你想提前深挖算子工程，可跳读 u9-l1（OpBuilder JIT 体系），本讲 4.2/4.3 的 `load()/jit_load()/compute_capability_args()` 正是它的入口；#9085 的完整提交说明（`git show e35282bdb`）也值得一读。
