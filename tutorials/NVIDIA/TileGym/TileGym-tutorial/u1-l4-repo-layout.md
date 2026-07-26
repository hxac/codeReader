# 仓库目录结构导览

## 1. 本讲目标

读完前几讲，你已经知道 TileGym 是什么、怎么装、怎么调用第一个算子。本讲不学新算法，而是带你**画一张地图**：把整个仓库拆成几个大区，把 `src/tilegym` 包内的各个模块对应到具体职责上。

学完本讲，你应当能够：

- 说出仓库顶层每个目录（`src/`、`tests/`、`modeling/`、`julia/`、`skills/`）分别是干嘛的。
- 在 `src/tilegym` 内部，根据目录名快速判断某段代码属于「接口」「后端实现」「后端调度」「LLM 集成」「内核清单」中的哪一类。
- 看到一个新算子名（比如 `softmax`）时，能预测它在哪些目录下会各有一份文件。
- 自己动手用一条 `tree` / `ls` 命令或文件浏览器复现这张地图。

## 2. 前置知识

本讲假设你已经读过 u1-l1～u1-l3，熟悉下面几个词：

- **算子（op）**：一个数学运算的对外名字，例如 `softmax`、`matmul`。
- **后端（backend）**：同一个算子名可以有多种实现，分别叫 cuTile、tilecpp、triton、cutile-rs 四个后端。
- **统一入口 / stub**：`tilegym.ops` 是门面，里面每个算子函数只是一个「空壳」，自己只 `raise NotImplementedError`，真正干活的代码按当前后端动态查找（u1-l3 已讲）。
- **分发（dispatch）**：按「算子名 + 后端名」在一张注册表里找到真正实现并转发的机制。

如果你对「为什么要分成接口和实现两层」还有疑问，本讲的 4.3 节会用目录结构再讲一次，U2 会从代码层面深入。

## 3. 本讲源码地图

本讲涉及的「地图锚点」文件（它们是各个目录的入口 `__init__.py`，最能体现该目录的职责）：

| 文件 | 作用 |
| --- | --- |
| [`src/tilegym/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py) | 整个包的总入口：依赖检查、后端初始化、对外暴露 `set_backend` 等 |
| [`src/tilegym/ops/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py) | 算子门面：按可用性条件加载各后端实现、再导出统一接口 |
| [`src/tilegym/ops/ops.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 统一算子签名：一堆带 `@dispatch` 的 stub |
| [`src/tilegym/ops/cutile/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py) | cuTile 后端实现目录的入口：批量 `import` 所有内核模块 |
| [`src/tilegym/backend/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/__init__.py) | 后端管理目录入口：导出 dispatch / selector 的公共函数 |
| [`src/tilegym/suites/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/suites/__init__.py) | suites（外部内核库的复刻实现）入口 |
| [`src/tilegym/transformers/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/transformers/__init__.py) | HuggingFace 集成入口：导入各模型的 monkey-patch 函数 |

下面按「先看大区，再看主包，再看 ops，最后看辅助目录」的顺序逐层展开。

## 4. 核心概念与源码讲解

### 4.1 顶层目录概览

#### 4.1.1 概念说明

打开仓库根目录，第一眼会看到一批顶层目录和若干配置文件。TileGym 采用了**「主包 + 辅助子项目」**的布局：

- 一个核心 Python 包 `src/tilegym`（真正被 `import` 的东西）。
- 几个相对独立的辅助目录：`tests`（测试与基准）、`modeling`（端到端 LLM 推理示例）、`julia`（Julia 语言版 cuTile，自带依赖、不依赖 Python 包）、`skills`（给 Claude 等工具用的内核编写技能包）。
- 一圈工程文件：`pyproject.toml` / `setup.py` / `requirements.txt`（打包与依赖）、`pytest.ini`（测试配置）、`format.sh`（格式化）、`README*.md` / `ROADMAP.md` / `CONTRIBUTING.md`（文档）、`.github/`（CI）。

理解这个布局的关键是：**只有 `src/tilegym` 是「被安装的库」**，其它目录要么是测试、要么是可独立运行的子项目、要么是文档与工程配置。`modeling/` 和 `julia/` 各自有自己的依赖文件（`modeling/transformers/pyproject.toml`、`julia/Project.toml`），README 里也明确说明 Julia 内核「自包含在 `julia/` 目录里，不要求安装 Python TileGym 包」。

#### 4.1.2 核心流程

用一张「大区表」来记忆：

```
TileGym/
├── src/tilegym/      ← 核心库（被 import 的就是它）
├── tests/            ← 功能测试 + 微基准
├── modeling/         ← 端到端 LLM 推理示例（HF transformers）
├── julia/            ← Julia 版 cuTile.jl（自包含子项目）
├── skills/           ← 给工具用的「如何写/转内核」技能包
├── .github/          ← CI 与 infra 脚本
├── pyproject.toml / setup.py / requirements.txt  ← 打包与依赖
├── README.md / ROADMAP.md / CONTRIBUTING.md       ← 文档
└── pytest.ini / format.sh                         ← 测试与格式化配置
```

读代码时的路线通常是：先看 `README.md` 建立印象 → 进入 `src/tilegym` 找主逻辑 → 跳到 `tests/ops` 看怎么调用 → 想看真实模型就进 `modeling/`。

#### 4.1.3 源码精读

README 的「Quick Start」一节正好把这几个顶层目录和「三种用法」一一对应起来，是最好的目录用途说明：

[src/tilegym/README.md（节选自 README，非源码文件）](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/README.md) 中明确写道：

- 内核实现都在 `src/tilegym/ops/`，单算子用法见 `tests/ops/README.md`。
- 微基准在 `tests/benchmark`，入口是 `bash run_all.sh`。
- 端到端 LLM 示例在 `modeling/transformers/`。
- Julia 内核「self-contained in the `julia/` directory」（自包含在 julia 目录）。

这段说明同时印证了：**目录 = 功能边界**。`src/tilegym/ops/` 放实现，`tests/benchmark/` 放测速，`modeling/transformers/` 放落地，`julia/` 是另一种语言的同构实验。

> 说明：以上几条对应 README 的 L106-L108、L115-L119、L121-L123、L140-L142，行号可能随文档更新漂移，链接已指向当前 HEAD。

#### 4.1.4 代码实践

**实践目标**：用一条命令看清顶层有几个大目录，避免被散落的配置文件干扰。

**操作步骤**：

1. 在仓库根目录执行：
   ```bash
   git ls-files | cut -d/ -f1 | sort -u
   ```
2. 把输出按「目录 vs 文件」分两类。

**需要观察的现象**：输出里会同时出现目录名（`src`、`tests`、`modeling`、`julia`、`skills`、`.github`）和一堆散文件（`README.md`、`pyproject.toml`、`setup.py`、`requirements.txt`、`pytest.ini`、`format.sh` 等）。

**预期结果**：目录大致就是上面「大区表」列出的几个；散文件几乎都是打包/测试/文档配置。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `julia/` 目录需要自带 `Project.toml`，而不是像 Python 部分那样共用根目录的 `pyproject.toml`？

**参考答案**：因为 Julia 用的是自己的包管理器（Pkg）和依赖格式，且 README 明确说 Julia 内核是自包含子项目、不依赖 Python 包，所以它必须用自己的 `Project.toml` 描述依赖，与 Python 侧互不干扰。

**练习 2**：如果你只想看「某个算子是怎么写的」，应该进哪个顶层目录？

**参考答案**：`src/tilegym/ops/`（README L108 明确：「All kernel implementations are located in the `src/tilegym/ops/` directory」）。

---

### 4.2 src/tilegym 包结构

#### 4.2.1 概念说明

`src/tilegym` 是被 `import` 的库本体。它内部又按职责分成若干模块，本讲只需要你记住**四个职责区**：

1. **总入口**：`__init__.py` —— 负责依赖检查、触发后端探测、对外暴露公共 API。
2. **后端管理区**：`backend/` —— 负责「哪个后端可用」「按后端查实现」。
3. **算子区**：`ops/` —— 统一接口（stub）+ 四个后端的实现。
4. **生态区**：`suites/`（外部内核库复刻）、`transformers/`（HF 集成）、`kernel_inventory/`（内核清单生成）。

另外还有几个独立小文件：`autotune.py`（自动调优全局机制）、`experimental.py`（实验内核告警补丁）、`logger.py`（日志）、`kernel_utils.py`（内核工具）。它们不属于主链路，先留个印象即可。

#### 4.2.2 核心流程

`src/tilegym` 的目录骨架（仅列关键）：

```
src/tilegym/
├── __init__.py            # 总入口：依赖检查 + 后端初始化 + 公共 API
├── autotune.py            # 自动调优全局开关与机制
├── experimental.py        # 实验内核的一次性告警补丁
├── logger.py / kernel_utils.py
├── backend/               # 后端管理
│   ├── dispatcher.py      #   _REGISTRY 注册表 + dispatch 分发
│   ├── selector.py        #   后端可用性探测 + set_backend
│   └── cutile_rs/         #   cuTile-rs 专属的 autotuner/utils
├── ops/                   # 算子（接口 + 多后端实现）
│   ├── ops.py             #   统一 @dispatch stub（门面）
│   ├── attn_interface.py  #   注意力接口工厂
│   ├── moe_interface.py / activation.py / fused_mlp.py
│   ├── cutile/            #   后端①：cuTile（Python @ct.kernel）
│   ├── tilecpp/           #   后端②：CUDA Tile C++（.cuh + .py）
│   ├── triton/            #   后端③：Triton
│   └── cutile_rs/         #   后端④：Rust FFI
├── suites/                # 外部内核库的复刻（liger/flashinfer/unsloth）
├── transformers/          # HuggingFace 集成（monkey-patch + 各模型子包）
└── kernel_inventory/      # 内核清单/代码生成（generation.py 等）
```

注意一条贯穿全包的线索：**接口永远在 `ops/` 顶层，实现永远在 `ops/<后端>/` 下**。这条「上层接口、下层实现」的分层会一直陪伴你到 U2。

#### 4.2.3 源码精读

**（1）总入口的初始化顺序。** [`src/tilegym/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L6-L21) 先做依赖检查（PyTorch 必须存在），这一段说明总入口承担「环境兜底」职责：

```python
def _check_torch_dependencies():
    """Verify that PyTorch is installed with helpful error message."""
    try:
        import torch
    except ImportError:
        raise ImportError(...)
```

随后总入口拉起后端管理区并按可用性挂钩实验补丁，最后导入 `ops`（[src/tilegym/__init__.py:L43-L50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L43-L50)）：

```python
if is_backend_available("cutile"):
    from .experimental import _apply_patch as _apply_experimental_patch
    _apply_experimental_patch()

from . import ops  # Unified ops module
```

这几行体现了「总入口 → 后端管理区 → 算子区」的依赖方向：先确定后端可用性，再去导入算子实现。

**（2）后端管理区的对外清单。** [`src/tilegym/backend/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/__init__.py#L9-L19) 把 `dispatcher.py`（分发）和 `selector.py`（选择）的公共函数 re-export 出来：

```python
from .dispatcher import dispatch, register_impl, get_registry_info, ...
from .selector import set_backend, is_backend_available, get_current_backend, ...
```

也就是说 `backend/` 这个目录对外的「脸」就是：分发（dispatch/register_impl）+ 选择（set_backend/is_backend_available）。`dispatcher.py` 管「按名字查实现」，`selector.py` 管「谁可用、当前用谁」，分工清晰。

**（3）算子门面的条件加载。** [`src/tilegym/ops/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/__init__.py#L15-L39) 用三段 `if is_backend_available(...)` 分别决定要不要加载 cuTile / tilecpp / triton 后端：

```python
if is_backend_available("cutile"):
    try:
        from . import cutile
    except (ImportError, RuntimeError):
        ... cutile = None
...
if is_backend_available("tilecpp"):
    from . import tilecpp
```

这正是「多后端」在目录结构上的体现：**每个后端是一个子目录，能否被加载取决于可用性探测**，而探测逻辑就住在 4.2 节里的 `backend/selector.py`。

**（4）统一接口的 stub 形态。** [`src/tilegym/ops/ops.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L27-L41) 给出了「接口长什么样」的样板（以 RoPE 为例），`@dispatch` 装饰器把名字 `"get_apply_rope_func"` 登记进注册表，函数体只抛 `NotImplementedError`：

```python
@dispatch("get_apply_rope_func", fallback_backend="triton")
def get_apply_rope_func(model: str = "llama"):
    raise NotImplementedError(...)
```

记住这张「接口在顶层 `ops.py`、实现在子目录」的图，4.3 节就是讲实现在子目录里怎么排。

#### 4.2.4 代码实践

**实践目标**：验证「总入口的导出清单」与「目录职责」一致。

**操作步骤**：

1. 打开 [`src/tilegym/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/__init__.py#L57-L72) 的 `__all__`（L57-L72）。
2. 把列表里的名字逐个归类：哪些来自 `backend`（后端管理）、哪个是 `ops`（算子）、哪些是 logger（日志）。

**需要观察的现象**：`__all__` 里有 `set_backend`、`get_current_backend`、`get_available_backends` 这一组（来自 `backend/`），也有 `ops` 这个模块名，还有 `warn_once`、`get_logger` 这一组（来自 `logger.py`）。

**预期结果**：你能把 `__all__` 的每个名字对应到 4.2.2 目录树里的某个目录/文件。这就证明了「对外 API 是各目录职责的投影」。

#### 4.2.5 小练习与答案

**练习 1**：`backend/dispatcher.py` 和 `backend/selector.py` 的职责分别是「查实现」和「判断可用性」，那为什么 `ops/__init__.py` 要在导入后端实现前先调用 `is_backend_available`？

**参考答案**：因为后端实现子目录（cuTile 等）依赖各自的外部编译器/工具链（如 `cuda.tile`、nvcc、cargo），缺失时直接 import 会报错。先用 `selector.py` 的 `is_backend_available` 探测，避免在不可用时把整个包拖崩——这恰好是 `selector.py` 存在的意义。

**练习 2**：`suites/`、`transformers/`、`kernel_inventory/` 三个目录，哪个最像「核心算子的扩展」、哪个最像「工程落地工具」？

**参考答案**：`suites/` 是核心算子的扩展（用同一套 dispatch 机制复刻 liger/flashinfer/unsloth 的算子）；`kernel_inventory/` 偏工程工具（生成内核清单/代码）；`transformers/` 是落地（把内核接到真实 LLM）。三者都不属于「算子接口/后端实现」这条主链。

---

### 4.3 ops 后端目录组织

#### 4.3.1 概念说明

`ops/` 目录是 TileGym 最核心、也最容易让人迷路的地方。它的组织原则只有一句话：**同一个算子名，在每个已实现的后端子目录里各有一份文件**。所以你会看到 `softmax`、`matmul`、`rms_norm` 这些名字在四个子目录里反复出现——它们不是重复，而是「同一算子的四种实现」。

四个后端子目录及其实现语言/组织方式差异很大：

| 子目录 | 后端 | 内核文件形态 | 典型算子数 |
| --- | --- | --- | --- |
| `ops/cutile/` | cuTile（默认） | 单个 `.py`（Python `@ct.kernel`） | 最多，主力 |
| `ops/tilecpp/` | CUDA Tile C++ | `.cuh`（内核源）+ `.py`（包装）成对出现 | 与 cuTile 基本对齐 |
| `ops/triton/` | Triton | 单个 `.py`（`@triton.jit`） | 较少（dropout/rms_norm/rope/layer_norm_legacy） |
| `ops/cutile_rs/` | cuTile-rs（Rust FFI） | 每个 op 一个 `<op>_kernel/` 目录，内含 `ffi.rs` + `kernel.rs`，另有一个 `cutile_kernels/` Cargo crate | 子集（matmul/bmm/swiglu/silu_and_mul/attention_sink） |

#### 4.3.2 核心流程

```
ops/
├── ops.py               ← 统一接口（@dispatch stub），所有后端共用
├── attn_interface.py    ← 注意力的接口工厂
├── moe_interface.py / activation.py / fused_mlp.py   ← 其它接口
│
├── cutile/              ← 后端①：每个 op 一个 .py，如 softmax.py / matmul.py
│   ├── __init__.py      ←   批量 import 所有内核模块并登记实现
│   ├── activation/      ←   激活函数子类（gelu/geglu/relu）
│   └── experimental/    ←   实验性内核（swa_attention/sparse_mla/…）
│
├── tilecpp/             ← 后端②：.cuh（源）+ .py（包装）成对
│   ├── *.cuh / *.py     ←   如 softmax.cuh + softmax.py
│   ├── autotuner.py
│   └── utils/           ←   编译/缓存工具（_cuda_utils.py）
│
├── triton/              ← 后端③：仅少数 op
│   └── rms_norm.py / rope.py / dropout.py / layer_norm_legacy.py
│
└── cutile_rs/           ← 后端④：Rust FFI
    ├── matmul_kernel/   ←   内含 ffi.rs + kernel.rs
    ├── swiglu_kernel/   ←   …
    └── cutile_kernels/  ←   Cargo crate（Cargo.toml + src/lib.rs）
```

记忆诀窍：**接口在 `ops/` 顶层，实现在 `ops/<后端>/` 下**；后端不同，文件组织方式（单 py / py+cuh / py+rs 目录）也不同。

#### 4.3.3 源码精读

**（1）cuTile 后端的批量登记。** [`src/tilegym/ops/cutile/__init__.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/__init__.py#L10-L37) 用一个 `if is_backend_available("cutile"):` 把所有内核模块一次性 import 进来：

```python
if is_backend_available("cutile"):
    from . import activation
    from . import attention
    ...
    from . import softmax
    from . import swiglu
```

注意：`import` 这些模块的「副作用」之一，就是让模块内的 `@register_impl("softmax", backend="cutile")` 把实现登记进注册表（U2 会详讲）。所以这个 `__init__.py` 既是「目录入口」，也是「后端实现的注册开关」。

**（2）tilecpp 的「成对文件」约定。** 在 `ops/tilecpp/` 下，几乎每个算子都有同名 `.cuh` 和 `.py` 两个文件（例如 `softmax.cuh` + `softmax.py`、`matmul.cuh` + `matmul.py`、`rms_norm.cuh` + `rms_norm.py`）。`.cuh` 是用 CUDA Tile C++ 写的内核源码，`.py` 是负责编译/缓存/启动并同样用 `@register_impl` 登记的包装层。这与 cuTile「单个 .py 全包」形成对比。另有一个 [`ops/tilecpp/utils/_cuda_utils.py`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/tilecpp/utils/_cuda_utils.py) 负责 nvcc 编译与 cubin 缓存。

**（3）triton 后端的「子集」特征。** `ops/triton/` 只有 5 个文件（`__init__.py` + `dropout/layer_norm_legacy/rms_norm/rope`），明显比 cuTile 少。这印证了 README/selector 的说法：triton 常作为「兜底后端」（`fallback_backend="triton"`），只实现了一部分算子。

**（4）cutile-rs 的「一 op 一 Rust 目录」结构。** `ops/cutile_rs/` 里每个算子是一个独立子目录，例如 `matmul_kernel/` 含 `ffi.rs` + `kernel.rs`；另有一个 `cutile_kernels/` Cargo crate（含 [`Cargo.toml`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile_rs/cutile_kernels/Cargo.toml) 和 `src/lib.rs`）负责编译成动态库。这套布局是为了 Rust FFI 的独立编译。

> 上述「成对文件」「子集」「一 op 一目录」的结论来自 `git ls-files` 的真实文件清单（如 `ops/tilecpp/softmax.cuh`、`ops/tilecpp/softmax.py`、`ops/triton/rms_norm.py`、`ops/cutile_rs/matmul_kernel/kernel.rs` 均真实存在），可直接复现。

#### 4.3.4 代码实践

**实践目标**：亲手验证「同名算子在四个后端目录下各有一份」。

**操作步骤**：

1. 执行下面四条命令，分别在四个后端目录里找 `softmax` 或 `rms_norm`：
   ```bash
   ls src/tilegym/ops/cutile/   | grep -E 'softmax|rms_norm'
   ls src/tilegym/ops/tilecpp/  | grep -E 'softmax|rms_norm'
   ls src/tilegym/ops/triton/   | grep -E 'softmax|rms_norm'
   ls src/tilegym/ops/cutile_rs | grep -E 'softmax|rms_norm'
   ```
2. 记录每个目录命中的文件名形态（`.py` / `.cuh+.py` / Rust 目录）。

**需要观察的现象**：

- `cutile/` 里能找到 `softmax.py`、`rms_norm.py`（单 .py）。
- `tilecpp/` 里能找到 `softmax.cuh`+`softmax.py`、`rms_norm.cuh`+`rms_norm.py`。
- `triton/` 里能找到 `rms_norm.py`，但**没有** `softmax`（triton 没实现 softmax）。
- `cutile_rs/` 里**没有** softmax/rms_norm（它只实现了 matmul/bmm/swiglu/silu_and_mul/attention_sink 等子集）。

**预期结果**：你将直观看到「后端不同，实现的有无与文件形态都不同」——这正是多后端目录组织的真实含义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ops/triton/` 里没有 `softmax.py`，但你在 u1-l3 仍然能用 `tilegym.ops.softmax`？

**参考答案**：因为默认后端是 cuTile，`softmax` 由 `ops/cutile/softmax.py` 提供实现。triton 只是兜底后端，只实现了一部分算子，没实现的算子在没有显式指定 backend 时不会落到 triton 上（除非该算子声明了 `fallback_backend="triton"` 且当前后端无实现）。

**练习 2**：tilecpp 用 `.cuh` + `.py` 成对文件，而 cuTile 用单个 `.py`。这两种组织方式分别把「内核源码」放在哪？

**参考答案**：cuTile 把内核源码直接写在 `.py` 里（`@ct.kernel` 装饰的 Python 函数，由运行时编译器 tileiras 编译）；tilecpp 把内核源码写在 `.cuh`（C++ 头文件）里，`.py` 只负责编译/缓存/启动。所以「源码载体」不同是两者最直观的目录差异。

---

### 4.4 tests / modeling / julia 辅助目录

#### 4.4.1 概念说明

最后这一组顶层目录都是「辅助性质」：它们不参与 `import tilegym`，但承载了测试、基准、端到端示例和跨语言实验。

- `tests/`：功能正确性测试 + 性能微基准。功能测试约定一套统一的 `PyTestCase` 基类（u9 会详讲），基准有独立目录和 `run_all.sh`。
- `modeling/`：端到端把 TileGym 内核接到真实 HuggingFace 模型上跑推理/测速，是一个**独立可打包的子项目**（自带 `pyproject.toml`、`Dockerfile`、`README`）。
- `julia/`：Julia 语言的 cuTile.jl 实验，完全自包含，不依赖 Python 包。

#### 4.4.2 核心流程

```
tests/
├── common.py / conftest.py / config.py   ← 测试基础设施（PyTestCase 等）
├── ops/                 ← 每个算子一个 test_*.py（功能正确性）
├── ops/README.md        ← 测试约定文档
├── benchmark/           ← 微基准（bench_*.py + run_all.sh）
│   └── experimental/ · suites/   ← 实验性 / suites 专属基准
├── suites/              ← liger/flashinfer/unsloth 各自的测试
├── transformers/        ← HF 集成测试
└── kernel_inventory/ · test_utils/

modeling/
└── transformers/
    ├── README.md / Dockerfile / infer.py
    ├── pyproject.toml              ← 独立子项目的依赖
    ├── sample_inputs/              ← 示例输入文本
    └── src/tilegym_hf_bench/       ← HF 推理基准 CLI（_cli.py / profiling/ …）

julia/
├── Project.toml / Manifest.toml    ← Julia 依赖
├── kernels/                        ← add.jl / matmul.jl / softmax.jl
└── test/                           ← runtests.jl + 各 test_*.jl
```

注意 `tests/` 的组织有一个与 `src/tilegym` **同构**的特点：`tests/ops/` 对应 `ops`、`tests/suites/` 对应 `suites`、`tests/benchmark/suites/` 也是按 suite 分。也就是说，**测试目录在镜像源码目录的结构**，这是非常实用的导航线索。

#### 4.4.3 源码精读

**（1）测试约定文档。** [`tests/ops/README.md`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md) 规定了测试类的写法（`Test_` 前缀、继承 `common.PyTestCase`、实现 `reference` 静态方法、用 `@pytest.mark.parametrize`、调用 `self.assertCorrectness`）。这说明 `tests/common.py` 是测试基础设施的「源头」，`tests/ops/` 则是按算子落地的测试集。

**（2）独立子项目的标志。** [`modeling/transformers/`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/README.md) 自带 `pyproject.toml`、`Dockerfile`、`src/tilegym_hf_bench/`（一个完整的包）和 `sample_inputs/`。它不是 `src/tilegym` 的一部分，而是一个**调用** `tilegym` 的下游示例项目。这印证了 4.1 节的论断：`modeling/` 是「落地示例」，不是「核心库」。

**（3）Julia 自包含。** `julia/` 有自己的 `Project.toml`（Julia 包依赖），`kernels/` 放内核，`test/runtests.jl` 是测试入口。README L142 明确：它「do not require the Python TileGym package」（不依赖 Python 包）。所以 `julia/` 在目录树上像「另一个小项目」。

> 行号提醒：README 引用的 L142、`tests/ops/README.md` 的内容均可在当前 HEAD 直接核对；若文档后续更新行号会漂移，以链接指向的版本为准。

#### 4.4.4 代码实践

**实践目标**：验证「测试目录镜像源码目录」这条导航规律。

**操作步骤**：

1. 执行：
   ```bash
   ls tests/ops | head
   ls tests/suites
   ls src/tilegym/ops | grep -v '\.py$' || true
   ```
2. 把 `tests/ops/` 里出现的算子名（如 `test_softmax.py`、`test_matmul.py`）与 `src/tilegym/ops/cutile/` 里的算子名做对照。

**需要观察的现象**：`tests/ops/` 里几乎每个 `test_<op>.py` 都能在 `src/tilegym/ops/cutile/` 找到同名 `<op>.py`；`tests/suites/` 下有 `liger/`、`flashinfer/`、`unsloth/`，正好对应 `src/tilegym/suites/` 下的同名目录。

**预期结果**：你会得到一张「源码目录 ↔ 测试目录」的对照表，以后想找某算子的测试，直接把源码路径里的 `src/tilegym` 换成 `tests` 即可。

**如果无法确定运行结果**：以上 `ls` 命令依赖具体环境，若在只读/受限环境运行可改用 GitHub 网页浏览对应目录，结论不变。

#### 4.4.5 小练习与答案

**练习 1**：你要给 `ops/cutile/silu_and_mul.py` 找功能测试，应该打开哪个文件？

**参考答案**：`tests/ops/test_silu_and_mul.py`（遵循「源码 `<op>.py` ↔ 测试 `test_<op>.py`」的镜像规律）。

**练习 2**：`modeling/transformers/` 为什么自带 `pyproject.toml`，而不是依赖根目录的 `pyproject.toml`？

**参考答案**：因为它是「调用 tilegym 的下游示例子项目」，有自己额外的依赖（如 `accelerate`、HuggingFace 推理相关包），独立打包/容器化运行（还有 `Dockerfile`），所以需要自己的 `pyproject.toml`，和核心库的依赖隔离开。

---

## 5. 综合实践

**任务：手工产出一份 `src/tilegym` 目录职责地图。**

请按下面的步骤把本讲内容串起来：

1. **画目录树**：用你顺手的工具（`tree src/tilegym -L 2` 或文件浏览器），画出 `src/tilegym` 两层深的目录树。
2. **写一句职责**：为下列每个目录/文件，用**一句话**写出它的职责（要求只用本讲学到的概念，不查源码内部细节）：
   - `src/tilegym/__init__.py`
   - `src/tilegym/backend/`
   - `src/tilegym/ops/ops.py`
   - `src/tilegym/ops/cutile/`
   - `src/tilegym/ops/tilecpp/`
   - `src/tilegym/ops/triton/`
   - `src/tilegym/ops/cutile_rs/`
   - `src/tilegym/suites/`
   - `src/tilegym/transformers/`
   - `src/tilegym/kernel_inventory/`
3. **交叉验证**：挑任意一个算子（如 `rms_norm`），在 `ops/ops.py`、`ops/cutile/`、`ops/tilecpp/`、`ops/triton/`、`tests/ops/` 五处分别确认它「有没有文件、文件叫什么」，把结果填进一张表。
4. **导航自测**：假设你想知道「fmha（注意力）在 cuTile 后端怎么启动内核」，仅凭本讲的地图，你应该打开哪个目录的哪个文件？（答案：`src/tilegym/ops/cutile/attention.py`。）

**验收标准**：第 2 步的每一句话都能归入「总入口 / 后端管理 / 统一接口 / 后端实现 / 生态扩展」五类之一，且第 3 步的表格与真实文件清单一致。

## 6. 本讲小结

- 仓库顶层是「主包 + 辅助子项目」布局：`src/tilegym` 是核心库，`tests`/`modeling`/`julia`/`skills` 是测试、落地示例、跨语言实验与技能包。
- `src/tilegym` 内部按职责分四区：总入口（`__init__.py`）、后端管理（`backend/`）、算子（`ops/`）、生态（`suites/`、`transformers/`、`kernel_inventory/`）。
- 一条贯穿全包的分层线索：**接口在 `ops/` 顶层（`ops.py` 的 `@dispatch` stub），实现在 `ops/<后端>/` 子目录**。
- 四个后端子目录文件形态各异：cuTile 单 `.py`、tilecpp「`.cuh`+`.py`」成对、triton 子集 `.py`、cutile-rs「一 op 一 Rust 目录 + Cargo crate」。
- 后端能否加载由 `backend/selector.py` 的 `is_backend_available` 决定，`ops/__init__.py` 据此条件 import。
- 测试目录（`tests/ops`、`tests/suites`）镜像源码目录，是找测试的快捷导航。

## 7. 下一步学习建议

本讲只画了「地图」，没有进入任何目录的代码细节。建议下一步：

- **进 U2**：进入 `src/tilegym/backend/`，搞清楚 `dispatcher.py` 的 `_REGISTRY`、`register_impl`、`dispatch` 如何把 4.3 节里的「接口 stub」与「后端实现」真正连起来。
- **进 U3**：进入 `src/tilegym/ops/cutile/`，以 `softmax.py` 为例，学第一个真实 cuTile 内核的写法。
- **顺手读文档**：把 [`tests/ops/README.md`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md) 和 [`modeling/transformers/README.md`](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/modeling/transformers/README.md) 通读一遍，你会对 `tests/` 和 `modeling/` 两个辅助目录有更具体的认识。
