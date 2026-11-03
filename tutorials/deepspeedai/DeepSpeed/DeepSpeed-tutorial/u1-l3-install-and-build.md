# 安装与构建：setup.py 与 CUDA 算子编译

## 1. 本讲目标

上一讲我们画好了仓库的代码地图，知道了 `deepspeed/` 是运行期主包、`op_builder/` 是安装期工具、`csrc/` 是 CUDA 内核源码。本讲回答一个衔接性的问题：**这三个目录是如何在"安装"这个时刻被粘合在一起的？**

学完本讲，你应该能够：

1. 读懂 `setup.py` 的完整执行流程，解释"torch 未安装时降级为 JIT 编译"的机制。
2. 说出 `ALL_OPS` 注册表是如何通过反射自动发现的，以及每个算子的 `BUILD_VAR`（如 `DS_BUILD_CPU_ADAM`）如何控制预编译。
3. 描述默认 compute capability（GPU 架构版本）的推导逻辑，理解 `TORCH_CUDA_ARCH_LIST` 的作用。
4. 区分三种算子编译路径：纯 pip 安装（不编译）、`DS_BUILD_OPS=1` 预编译、运行时 JIT 编译。
5. 解释 `op_builder/`（安装期视角）与 `deepspeed/ops/op_builder`（运行期视角）的"双轨"关系——它们其实是**同一批文件**。
6. 理解安装产物 `deepspeed/git_version_info_installed.py` 如何记录"哪些算子已被预编译"，从而决定运行期走预编译加载还是 JIT。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉也没关系，下面用两三句话解释）：

- **Python 打包（setuptools）**：`setup.py` 是 Python 项目的"装机说明书"。`pip install .` 时，pip 会执行这个文件，文件里的 `setup(...)` 调用声明了包名、版本、依赖和要编译的扩展模块。`ext_modules` 参数列出的 C++/CUDA 扩展会在安装时被编译成 `.so`（Linux 共享库）文件。
- **CUDA 与 nvcc**：CUDA 是 NVIDIA 的 GPU 编程工具链，`nvcc` 是它的编译器。CUDA C++ 源码（`.cu` 文件）必须用 `nvcc` 编译。
- **compute capability（计算能力，简称 CC）**：NVIDIA 给每代 GPU 标的一个版本号，如 A100 是 8.0、H100 是 9.0。编译 CUDA 代码时必须指定目标架构（生成 `-gencode` 编译参数），架构不匹配则无法运行。
- **JIT（Just-In-Time，即时编译）**：与安装时预编译（AOT，Ahead-Of-Time）相对。JIT 指程序**运行中第一次用到某个算子时**才现场编译它，编译产物缓存在磁盘上，下次直接复用。
- **ninja**：一个轻量构建工具，PyTorch 的 `torch.utils.cpp_extension.load()` 用它来加速扩展编译。DeepSpeed 把 `ninja` 直接列为基础依赖。
- **环境变量开关**：DeepSpeed 大量使用"环境变量当开关"的模式，如 `DS_BUILD_OPS=1`、`DS_BUILD_AIO=1`。本讲会看到它们在源码中如何被读取。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [setup.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py) | 安装总入口：探测 torch、遍历算子注册表、决定预编译哪些扩展、生成版本信息文件 |
| [op_builder/builder.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py) | `OpBuilder` / `CUDAOpBuilder` 基类：算子的"编译配方"，含 compute capability 推导与 JIT 加载 |
| [op_builder/all_ops.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/all_ops.py) | 用反射自动发现所有 Builder 类，汇成 `ALL_OPS` 注册表 |
| [op_builder/cpu/cpu_adam.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/cpu/cpu_adam.py) | 一个具体算子的 Builder 样例（CPUAdam），展示"三行描述一个算子"的写法 |
| [requirements/requirements.txt](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/requirements/requirements.txt) | 基础依赖表；同目录下其余 `requirements-*.txt` 是各 extras 的依赖表 |
| [install.sh](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/install.sh) | 多机安装脚本：构建 wheel 并通过 pdsh 分发到 hostfile 列出的所有机器 |
| [deepspeed/git_version_info.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/git_version_info.py) | 运行期读取安装信息的入口：优先读安装时生成的 `git_version_info_installed.py` |
| [deepspeed/ops/adam/cpu_adam.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/ops/adam/cpu_adam.py) | 运行期触发 JIT 的调用点样例：`CPUAdamBuilder().load()` |

---

## 4. 核心概念与源码讲解

### 4.1 setup.py 的 extras 体系与依赖表

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
requirements/requirements-inf.txt ──────────┼─► fetch_requirements() 逐个读入
requirements/requirements-sd.txt ───────────┤     ↓
requirements/requirements-deepcompile.txt ──┤   extras_require 字典
requirements/requirements-torchembed.txt ───┤     ↓
requirements/requirements-readthedocs.txt ──┘   求并集 → extras_require['all']
```

#### 4.1.3 源码精读

先看 setup.py 开头的 torch 探测——这是整个安装流程的第一道分岔口：

[setup.py:L32-L38](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L32-L38)

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

[setup.py:L40-L43](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L40-L43)

```python
from op_builder import get_default_compute_capabilities, OpBuilder
from op_builder.all_ops import ALL_OPS, accelerator_name

from accelerator import get_accelerator
```

然后是依赖装载。`fetch_requirements` 只是把文本文件按行读成列表：

[setup.py:L58-L60](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L58-L60)

```python
def fetch_requirements(path):
    with open(path, 'r') as fd:
        return [r.strip() for r in fd.readlines()]
```

[setup.py:L79-L90](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L79-L90) 把 8 个依赖文件装配成 `extras_require` 字典，键就是 pip 的 extras 名，例如 `pip install deepspeed[autotuning]` 会装上 `requirements-autotuning.txt` 里的内容。注意 `'triton': []` 是空列表——注释说明这是为了向后兼容保留的键，如今 Triton 由 PyTorch 自带：

```python
install_requires = fetch_requirements('requirements/requirements.txt')
extras_require = {
    'readthedocs': ..., 'dev': ..., 'autotuning': ..., 'autotuning_ml': ...,
    'inf': ..., 'sd': ..., 'triton': [], 'deepcompile': ..., 'torchembed': ...,
}
```

[setup.py:L96-L101](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L96-L101) 对所有 extras 求并集，生成一个"全家桶"组 `[all]`。此外 [setup.py:L92-L94](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L92-L94) 有一处条件依赖：仅当 torch 可用、加速器是 CUDA 且非 ROCm 时，才把 `nvidia-ml-py`（NVML 的 Python 绑定）追加进基础依赖——这是"按硬件条件裁剪依赖"的范例。

基础依赖本体只有 10 行（[requirements/requirements.txt:L1-L11](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/requirements/requirements.txt#L1-L11)）：

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
- `py-cpuinfo`：用于探测 CPU 型号与指令集（AVX512/NEON 等），CPU 算子（如 cpu_adam）据此选择向量化编译参数。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 extras 体系的行为差异——装与不装 `dev` 组，环境里多出哪些包。

**操作步骤**：

1. 创建干净虚拟环境并只装基础包：`python -m venv ~/venv-ds-base && ~/venv-ds-base/bin/pip install -e .`
2. 记录包列表：`~/venv-ds-base/bin/pip list > base.txt`
3. 再创建一个环境装 dev 组：`python -m venv ~/venv-ds-dev && ~/venv-ds-dev/bin/pip install -e .[dev]`
4. 对比：`diff base.txt <(~/venv-ds-dev/bin/pip list)`（或肉眼比对）。

**需要观察的现象**：`dev` 环境额外出现 `pytest`、`pre-commit`、`transformers`、`tensorboard`、`wandb` 等开发期工具（对应 [requirements/requirements-dev.txt](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/requirements/requirements-dev.txt) 的内容）。

**预期结果**：两个环境都能 `import deepspeed` 成功——证明 dev 组的包对运行并非必需。（本实践涉及真实 pip 安装，若网络受限，预期结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nvidia-ml-py` 不直接写进 `requirements.txt`，而要在 setup.py 里条件追加？

**答案**：`nvidia-ml-py` 只对 NVIDIA GPU 有意义。若写死在基础依赖里，CPU/ROCm/其他加速器用户也会被迫安装它。setup.py 在 [L92-L94](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L92-L94) 里通过 `get_accelerator().device_name() == 'cuda'` 探测当前硬件，只在 NVIDIA 环境追加，实现了按硬件裁剪。

**练习 2**：`pip install deepspeed[triton]` 会多装什么？

**答案**：什么也不会多装。`'triton': []` 是空列表（[setup.py:L87](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L87)），注释写明它是为向后兼容保留的键——新版 PyTorch 已自带 Triton。

**练习 3**：`fetch_requirements` 极其简单，为什么不直接在 setup.py 里硬编码依赖列表？

**答案**：依赖外置成纯文本后，`requirements/` 目录可被 CI 配置、Dockerfile、`environment.yml`、`install.sh`（多机分发时会 `pdcp requirements/requirements.txt`）等场景直接引用，单一数据源避免多处维护漂移。

---

### 4.2 OpBuilder 构建入口：ALL_OPS 注册表与预编译决策

#### 4.2.1 概念说明

`OpBuilder` 是每个 CUDA/C++ 算子的"编译配方"。DeepSpeed 有约 20 个算子（fused_adam、cpu_adam、async_io、transformer……），如果每个都写一份编译脚本会大量重复。做法是：定义一个基类把"编译一个算子"的通用逻辑（找源码、探测 CUDA 版本、生成架构参数、调用编译器）全部封装，每个具体算子只需声明三件事：

1. **`BUILD_VAR`**：控制自己预编译的环境变量名，如 `DS_BUILD_CPU_ADAM`。
2. **`absolute_name()`**：编译产物（`.so`）的 Python 模块全名。
3. **`sources()`**：源码文件相对路径列表。

`ALL_OPS` 则是"算子名 → Builder 实例"的注册表，它**不是手写的**，而是用反射扫描 `op_builder/` 目录下所有以 `Builder` 结尾的类自动生成。

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

默认值由平台决定（[setup.py:L127-L130](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L127-L130)）：**Linux 默认不预编译（`0`），Windows 默认全预编译（`1`）**。Linux 选择 JIT 优先是为了让同一个 wheel 能在任何 GPU 架构上工作；Windows 上 JIT 体验差（需要管理员权限创建符号链接），所以倾向安装时编好。

#### 4.2.3 源码精读

**（1）注册表的自动发现。**

[op_builder/all_ops.py:L9-L13](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/all_ops.py#L9-L13) 先解决"安装期能否 import 到加速器模块"的问题：安装时顶层 `accelerator/` 可见，安装后（运行期）则改从 `deepspeed.accelerator` 导入——同一段代码适配两个生命周期阶段：

```python
try:
    # during installation time accelerator is visible, otherwise return deepspeed.accelerator
    from accelerator import get_accelerator
except ImportError:
    from deepspeed.accelerator import get_accelerator
```

[op_builder/all_ops.py:L18-L32](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/all_ops.py#L18-L32) 是注册表本体：用 `pkgutil.iter_modules` 遍历 op_builder 目录下的每个模块，把所有名字以 `Builder` 结尾的类交给加速器工厂实例化，最后汇成字典：

```python
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

**（2）一个算子配方的样子。** 以 CPUAdam 为例，全文只有 20 行：

[op_builder/cpu/cpu_adam.py:L5-L25](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/cpu/cpu_adam.py#L5-L25)

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

声明式到极致：编译哪个文件、编出来叫什么模块，仅此而已。基类 `OpBuilder` 在 [op_builder/builder.py:L149-L162](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L149-L162) 把这两个方法定为抽象方法，强迫每个子类提供。

**（3）setup.py 的决策主循环。**

[setup.py:L147-L155](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L147-L155) 定义"算子是否被启用"：优先读算子专属的 `BUILD_VAR`（如 `DS_BUILD_CPU_ADAM`），没设置则回落到总开关 `DS_BUILD_OPS`：

```python
def op_envvar(op_name):
    return ALL_OPS[op_name].BUILD_VAR

def op_enabled(op_name):
    env_var = op_envvar(op_name)
    return int(get_env_if_set(env_var, BUILD_OP_DEFAULT))
```

[setup.py:L158-L177](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L158-L177) 是主循环，处理"想编但不兼容"（打警告跳过）、"ROCm 上未启用但兼容"（做 hipify 转换备用）、"想编且兼容"（加入 `ext_modules` 预编译）三种组合，其中预编译分支还有一个兜底断言 `assert torch_available`：

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

这段代码体现了"降级"的完整语义：torch 缺失时，即使你显式设置 `DS_BUILD_OPS=1`，预编译请求也会被 [setup.py:L132-L133](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L132-L133) 直接拒绝（`assert torch_available`）；而不设置开关时，一切算子静默留给 JIT。

循环之后还有一个依赖修正逻辑（[setup.py:L179-L192](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L179-L192)）：`async_io` 和 `gds` 在运行期会连带触发 `pin_memory` 算子的 JIT，若前者被预编译而后者没有，无编译器的部署环境就会在运行时翻车——所以这里强制把 `pin_memory` 一起预编译。这是"预编译清单要闭合"的一个真实案例。

**（4）compute capability 推导。** 这是 CUDA 编译最微妙的部分。基类里预置了一个保守默认值（[op_builder/builder.py:L24-L25](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L24-L25)）：

```python
DEFAULT_TORCH_EXTENSION_PATH = "/tmp/torch_extensions"
DEFAULT_COMPUTE_CAPABILITIES = "6.0;6.1;7.0"
```

[op_builder/builder.py:L44-L62](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L44-L62) 的 `installed_cuda_version()` 通过 `nvcc -V` 的输出解析本机 CUDA 工具链版本；[op_builder/builder.py:L108-L131](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L108-L131) 的 `assert_no_cuda_mismatch()` 拿它与 `torch.version.cuda`（torch 编译时用的 CUDA 版本）比对：主版本必须一致，次版本不一致时查一张兼容表（`cuda_minor_mismatch_ok`，[L101-L105](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L101-L105)）放行，例如 torch 用 CUDA 11.0 编译、系统装的是 11.2 是允许的；实在不匹配还可以用 `DS_SKIP_CUDA_CHECK=1` 强行跳过（风险自负）。

默认架构列表则由 [op_builder/builder.py:L80-L96](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L80-L96) 的 `get_default_compute_capabilities()` 按本机 CUDA 大版本扩展：

```python
compute_caps = DEFAULT_COMPUTE_CAPABILITIES        # "6.0;6.1;7.0"
if installed_cuda_version()[0] == 11:
    ...逐级追加 "8.0" / "8.6" / "9.0"
elif installed_cuda_version()[0] == 12:
    compute_caps += ";8.0;8.6;9.0"
    if installed_cuda_version()[1] >= 8:
        compute_caps += ";10.0;12.0"
```

也就是说：CUDA 12 环境下默认给 6.0～9.0（12.8 起含 10.0/12.0）全部编一遍——预编译的 wheel 因此"通吃"大多数 GPU，代价是编译时间成倍增长。这也解释了为什么 Linux 默认走 JIT：JIT 模式下（`CUDAOpBuilder.compute_capability_args`，[op_builder/builder.py:L706-L760](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L706-L760)）优先级是 `TORCH_CUDA_ARCH_LIST` 环境变量 > 探测当前设备的真实架构，只编"眼前这块卡"的架构，并把最高架构自动加上 `+PTX`（PTX 是中间表示，可在更新架构上 JIT 兜底），编译又快又省。setup.py 侧对应的补丁在 [setup.py:L117-L123](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L117-L123)：非 CUDA 加速器环境下若用户没设 `TORCH_CUDA_ARCH_LIST`，就帮其设上默认值，规避交叉编译/Docker 构建报错。

**（5）运行期：load 与 jit_load 的分岔。** 安装期讲完了，再看运行期如何"兑现"。[op_builder/builder.py:L597-L614](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L597-L614) 的 `load()` 是运行期加载算子的统一入口：

```python
def load(self, verbose=False):
    if self.name in __class__._loaded_ops:
        return __class__._loaded_ops[self.name]
    from deepspeed.git_version_info import installed_ops, torch_info, accelerator_name
    ...
    if installed_ops.get(self.name, False) and accelerator_name == get_accelerator()._name:
        self.validate_torch_version(torch_info)      # 版本指纹校验
        ...
        op_module = importlib.import_module(self.absolute_name())  # 直接 import 预编译 .so
        ...
        return op_module
    else:
        return self.jit_load(verbose)                # 现场编译
```

判断依据正是安装时写入的 `installed_ops` 表。预编译路径还要做两道安全检查：`validate_torch_version` 确认运行期 torch 版本与安装时一致（[op_builder/builder.py:L170-L179](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L170-L179)），CUDA 算子再校验 CUDA 版本（[L181-L200](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L181-L200)）——版本漂移时给出明确报错而不是让程序崩溃在莫名的 ABI 错误上。

JIT 路径 [op_builder/builder.py:L616-L668](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L616-L668) 依次：兼容性检查 → 校验 ninja 可用 → 调 PyTorch 的 `torch.utils.cpp_extension.load()`，把 `sources()`/`include_paths()`/编译参数全部传过去，最终产物缓存在 `/tmp/torch_extensions`（`DEFAULT_TORCH_EXTENSION_PATH`）或 `TORCH_EXTENSIONS_DIR` 指定的目录，并记入类级缓存 `_loaded_ops` 避免重复编译。

调用它的真实业务代码长这样（[deepspeed/ops/adam/cpu_adam.py:L93](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/ops/adam/cpu_adam.py#L93)）：

```python
self.ds_opt_adam = CPUAdamBuilder().load()
```

当你在 ZeRO-Offload 里第一次用到 `DeepSpeedCPUAdam` 时，这一行就会触发 cpu_adam 的 JIT 编译（若它没被预编译）。

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

5. 再次查看缓存目录：`ls -R /tmp/torch_extensions/`，找到 `cpu_adam` 子目录里的 `.so` 文件。

**需要观察的现象**：

- 步骤 2 中 `installed_ops` 打印出的所有算子值均为 `False`（因为 Linux 默认不预编译），版本号形如 `0.19.9+<git短哈希>`。
- 步骤 4 会输出一长串编译日志（首次约几十秒到几分钟），末尾打印加载成功。
- 步骤 5 出现 `cpu_adam` 编译产物目录与 `.so` 文件；第二次执行步骤 4 几乎瞬时完成（命中缓存与 `_loaded_ops` 内存缓存）。

**预期结果**：JIT 产物确实生成于 `/tmp/torch_extensions/<python版本目录>/cpu_adam/` 下（若设置了 `TORCH_EXTENSIONS_DIR` 环境变量则在对应目录；`~/.cache` 下是否有产物取决于该变量，需以实际观察为准——此点待本地验证）。把 install.log 里 `DS_BUILD_OPS=0` 与 `Install Ops={...全 False...}` 两行截图归档，它们分别来自 [setup.py:L130](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L130) 和 [setup.py:L194](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L194)。

#### 4.2.5 小练习与答案

**练习 1**：在 Linux 上执行 `DS_BUILD_OPS=1 pip install -e .` 但机器上没装 torch，会发生什么？

**答案**：安装直接失败。`DS_BUILD_OPS=1` 时 [setup.py:L132-L133](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L132-L133) 的断言 `assert torch_available, "Unable to pre-compile ops without torch installed..."` 触发。反过来若不设该变量，即使 torch 缺失安装也能成功（算子全部留给 JIT），这正是"降级"设计。

**练习 2**：为什么 `ALL_OPS` 用反射扫描而不是手工维护一个列表？

**答案**：新增算子时只需在 `op_builder/` 下新建一个文件、定义一个 `XxxBuilder` 类，注册表自动收录（[op_builder/all_ops.py:L22-L32](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/all_ops.py#L22-L32)），不会出现"写了 Builder 却忘了注册"的疏漏；同时加速器厂商只需在自己的子目录（如 `op_builder/hpu/`、`op_builder/xpu/`）放实现，即可被同一机制发现。

**练习 3**：JIT 模式下 `compute_capability_args` 为什么给最高架构自动追加 `+PTX`？

**答案**：见 [op_builder/builder.py:L758-L760](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L758-L760)。`sm_XX` 是针对具体架构的机器码，`compute_XX`（PTX）是中间表示，可在比它新的架构上由驱动即时翻译执行。带上 `+PTX` 后，即使日后把这份缓存拷到更新架构的 GPU 机器上也能兜底运行。

---

### 4.3 requirements 依赖表、双轨 op_builder 与安装产物

#### 4.3.1 概念说明

前两个模块分别讲了"装什么依赖"和"怎么决定编哪些算子"。本模块补齐剩下的拼图：

- **版本指纹**：安装时把版本、git 哈希、已装算子表、torch/CUDA 版本写进一个 Python 文件，运行期直接 import 它，据此判断"预编译的算子还能不能用"。
- **双轨 op_builder**：顶层 `op_builder/`（安装期视角）与 `deepspeed/ops/op_builder`（运行期视角）看起来是两个包路径，实际在 Linux/macOS 上是**符号链接**指向同一批文件；Windows 不支持开发期符号链接，setup.py 会在构建 wheel 时把它复制成实体目录。

#### 4.3.2 核心流程

版本字符串的组装规则（[setup.py:L228-L246](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L228-L246)）：

```text
读取 version.txt (0.19.9)
  ├─ 设置了 DS_BUILD_STRING ？ → 追加构建后缀（官方发 wheel 用），写入 build.txt
  ├─ 存在 build.txt ？         → 追加其中的内容（从分发包安装）
  └─ 否则（源码安装）          → 追加 "+<git 短哈希>"
= 最终 version_str
```

随后 [setup.py:L267-L282](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L267-L282) 收集 `torch_info`（torch 版本、bf16 支持、CUDA/NCCL/HIP 版本，CPU-only 时各版本记为 `"0.0"`），把版本串、git 信息、`install_ops` 表、加速器名、`torch_info` 一起**写死**到 `deepspeed/git_version_info_installed.py`。安装产物与运行期的对接点在 [deepspeed/git_version_info.py:L6-L24](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/git_version_info.py#L6-L24)：能导入安装生成的文件就用它；导不进（比如未安装的源码树、readthedocs 环境）则回退——版本读 `version.txt`，`installed_ops` 由 `ALL_OPS` 生成全 `False` 字典，`torch_info` 各项填 `"0.0"` 哨兵值，交给运行期自行探测。

双轨结构的切换逻辑在 [op_builder/__init__.py:L17-L22](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/__init__.py#L17-L22)：优先把自己挂到 `deepspeed.ops.op_builder` 命名空间下（运行期），失败才退回顶层 `op_builder`（安装期）。安装期 torch 可能还没装，无法立即实例化 Builder，于是返回一个**闭包**延迟到运行期再构造（[op_builder/__init__.py:L28-L38](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/__init__.py#L28-L38) 的 `builder_closure`）。

#### 4.3.3 源码精读

**（1）符号链接证据。** 在仓库里直接可见：

```text
deepspeed/ops/csrc        -> ../../csrc          （内核源码）
deepspeed/ops/op_builder  -> ../../op_builder    （编译配方）
```

所以"安装期工具"与"运行期模块"共享同一份源码，`MANIFEST.in`（[L6-L9](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/MANIFEST.in#L6-L9)）确保 `csrc/`、`op_builder/`、`accelerator/` 里的文件也进 wheel——JIT 模式下用户机器上必须有源码才能现场编译。

**（2）Windows 的实体复制。** [setup.py:L216-L226](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L216-L226) 在 win32 平台上 `shutil.rmtree` 删除包内的链接目录、`shutil.copytree` 把 `csrc/`、`op_builder/`、`accelerator/` 复制进 `deepspeed/ops/` 下——这也呼应了 setup.py 文件头（[L8-L18](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L8-L18)）"Windows 构建需管理员权限创建符号链接目录"的说明。

**（3）版本指纹文件。** 安装时写入的关键内容（[setup.py:L276-L282](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L276-L282)）：

```python
with open('deepspeed/git_version_info_installed.py', 'w') as fd:
    fd.write(f"version='{version_str}'\n")
    fd.write(f"git_hash='{git_hash}'\n")
    fd.write(f"git_branch='{git_branch}'\n")
    fd.write(f"installed_ops={install_ops}\n")
    fd.write(f"accelerator_name='{accelerator_name}'\n")
    fd.write(f"torch_info={torch_info}\n")
```

`installed_ops` 与 `accelerator_name` 正是 4.2 中 `OpBuilder.load()` 分岔判断的数据来源：换了加速器或换了 torch 版本，预编译算子即被判为不可用，自动转 JIT 或报出清晰的版本不匹配错误。`install.sh` 的清理步骤（[install.sh:L128-L134](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/install.sh#L128-L134)）每次构建前删除旧的 `deepspeed/git_version_info_installed.py`、`dist`、`build`，避免陈旧指纹污染新 wheel。

**（4）多机安装脚本。** [install.sh:L154-L161](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/install.sh#L154-L161) 本机路径：`python -m build --wheel --no-isolation` 构建 wheel → `pip install dist/deepspeed*.whl` → 运行 `ds_report` 自检；存在 hostfile 时则走 [install.sh:L163-L181](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/install.sh#L163-L181)，用 `pdsh`/`pdcp` 把 wheel 和 requirements 分发到所有节点逐一安装——这在 GPU 集群上保证每台机器的编译环境一致。

#### 4.3.4 代码实践

**实践目标**：追踪"版本指纹"从生成到消费的全链路。

**操作步骤**：

1. 若已按 4.2.4 完成安装，打开仓库里的 `deepspeed/git_version_info_installed.py`（未安装则此文件不存在，这本身就是观察点），对照 [setup.py:L276-L282](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L276-L282) 逐行核对其六个字段。
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

#### 4.3.5 小练习与答案

**练习 1**：为什么运行期加载预编译算子前要校验 `torch_info`，而不是直接 import？

**答案**：`.so` 是针对特定 torch/CUDA ABI 编译的。若安装后又升级了 torch，旧的预编译产物可能无法加载或产生未定义行为。[op_builder/builder.py:L597-L612](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/op_builder/builder.py#L597-L612) 在 import 前调用 `validate_torch_version` / `validate_torch_op_version`，把潜在的诡异崩溃转换成"请重新安装 DeepSpeed"的明确提示。

**练习 2**：未安装的源码树上 `import deepspeed` 会读到什么版本号？`installed_ops` 是什么？

**答案**：由 [deepspeed/git_version_info.py:L9-L24](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/git_version_info.py#L9-L24) 的回退分支决定：版本读 `version.txt`（即 `0.19.9`，无哈希后缀），`git_hash` 为 `[none]`，`installed_ops` 由 `ALL_OPS` 生成全 `False`，`torch_info` 各版本为哨兵值 `"0.0"`。

**练习 3**：`install.sh` 为什么构建 wheel 而不是直接 `pip install .`，还要配合 pdsh？

**答案**：见 [install.sh:L154-L181](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/install.sh#L154-L181)。先产出 wheel 再分发，保证集群所有节点安装的是**同一次构建**的二进制（而不是各节点各自编译、结果不一致）；`pdcp` wheel + `pdsh` 安装 + `ds_report` 自检是多机环境的标准三连。

---

## 5. 综合实践

**任务：写一份《我的 DeepSpeed 安装画像》报告，把本讲三条线串起来。**

在一个干净虚拟环境中完成以下步骤并归档输出：

1. **依赖线**：`pip install -e .`，从安装日志中找出 `install_requires=[...]` 一行（来自 [setup.py:L284](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/setup.py#L284)），与 `requirements/requirements.txt` 逐项核对。
2. **算子线**：记录日志中的 `DS_BUILD_OPS=...` 与 `Install Ops={...}` 两行；然后对比第二次安装（`DS_BUILD_CPU_ADAM=1 pip install -e .`），观察 `Install Ops` 中 `cpu_adam` 变为 `1/True`、`ext_modules` 不再为空，以及 `import deepspeed.ops.adam.cpu_adam_op` 能直接成功（预编译产物）而无需 JIT。
3. **指纹线**：两次安装各打印一次 `deepspeed.__version__` 与 `installed_ops['cpu_adam']`，说明两次差异的来源。
4. 最后用 200 字总结：三种编译路径（pip 默认 JIT / DS_BUILD_OPS 全预编译 / 单算子 BUILD_VAR 预编译）各自的适用场景——提示：无编译器的生产环境用预编译 wheel；开发机上 JIT 更省时。

（GPU 机器上可加做一步：`TORCH_CUDA_ARCH_LIST="8.0" DS_BUILD_FUSED_ADAM=1 pip install -e .`，对照 4.2 讲的架构推导，确认只编了 sm_80。无 GPU 环境此步待本地验证。）

## 6. 本讲小结

- **extras 体系**：DeepSpeed 把依赖外置到 `requirements/*.txt`，setup.py 装配成 `install_requires` + 8 个 extras 组 + 自动求并集的 `[all]`；`nvidia-ml-py` 等硬件相关依赖按加速器类型条件追加。
- **ALL_OPS 注册表**：通过反射扫描 `op_builder/` 下所有 `*Builder` 类自动生成，每个算子用 `BUILD_VAR` + `absolute_name()` + `sources()` 三件事完成声明式注册。
- **三种编译路径**：Linux 上 pip 默认安装不预编译任何算子（`DS_BUILD_OPS=0`）；设 `DS_BUILD_OPS=1` 或单个 `DS_BUILD_*` 变量触发预编译；未预编译的算子在运行期首次使用时经 `OpBuilder.load()` → `jit_load()` 现场 JIT，产物缓存于 `/tmp/torch_extensions`。
- **降级机制**：torch 缺失时安装不失败（预编译静默禁用）；但显式要求预编译而 torch 缺失时会果断断言报错——"默认宽容、显式严格"。
- **compute capability 推导**：预编译用 `get_default_compute_capabilities()` 按 CUDA 版本扩展默认架构列表（兼容多代 GPU）；JIT 优先 `TORCH_CUDA_ARCH_LIST`，否则探测本机设备并给最高架构追加 `+PTX` 兜底。
- **版本指纹与双轨**：安装时把 `installed_ops`/`torch_info` 写入 `git_version_info_installed.py`，运行期据此选择"直接 import 预编译 .so"或"转 JIT"，并做 torch/CUDA 版本防漂移校验；顶层 `op_builder/` 与 `deepspeed/ops/op_builder` 经符号链接共享同一份源码（Windows 复制实体）。

## 7. 下一步学习建议

安装构建打通后，下一讲 **u1-l4《启动与诊断：deepspeed 命令与 ds_report》**将进入"如何把训练跑起来"：`bin/deepspeed` 命令如何分发参数、`ds_report` 如何探测并汇总本讲提到的 torch/CUDA/ninja/算子状态。建议顺带阅读的源码：

- [deepspeed/env_report.py](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/deepspeed/env_report.py)——它消费的正是本讲的 `git_version_info` 与 `ALL_OPS`，是天然的复习材料。
- [build_win.bat](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/build_win.bat) 与 [docker/](https://github.com/deepspeedai/DeepSpeed/blob/dc046b15d7a58f6c8dacba435508b52b1ebc620c/docker) 目录——两种更重的构建交付形态。
- 若你想提前深挖算子工程，可跳读 u9-l1（OpBuilder JIT 体系），本讲 4.2 的 `load()/jit_load()` 正是它的入口。
