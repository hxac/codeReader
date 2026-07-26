# Triton CUDA Tile IR 后端

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 `triton` 后端与 `cutile`、`tilecpp` 后端的本质区别——它用标准 Triton DSL（`@triton.jit`）写内核，而不是 cuTile 的 `@ct.kernel` 或 C++ 模板。
2. 理解 `triton` 后端是一个**部分后端**：只实现 norm / RoPE / dropout 这一小族「工具算子」，并不实现 softmax、matmul、attention 等展示型内核。
3. 掌握 `oait` 与 `nvt` 两种 Triton 编译器的区别，以及用 `PYTHONPATH=/opt/nvtriton ENABLE_TILE=1` 切换 `nvt` 的原理。
4. 解释 `ops.py` 中六个算子为何把 `fallback_backend` 设为 `"triton"`，而 softmax / matmul 等却不设。
5. 理解 `triton` 在 `_check_backends_availability()` 中恒为 `True` 的设计意图。

---

## 2. 前置知识

本讲依赖你已经学完 u2-l3（后端选择与可用性）和 u7-l1（tilecpp 后端）。在继续之前，请确认你理解以下概念：

- **后端（backend）**：同一算子名（如 `rms_norm`）在不同后端下有不同的实现，分发器按「当前后端」查全局注册表 `_REGISTRY` 路由。TileGym 有四个后端：`cutile`（默认）、`tilecpp`、`triton`、`cutile-rs`。
- **`@dispatch` 与 `@register_impl`**：`ops.py` 里的算子是只抛 `NotImplementedError` 的 stub，真正的实现在各后端目录用 `@register_impl("算子名", backend="...")` 注册。详见 u2-l1、u2-l2。
- **三级查找**：当前后端 → `fallback_backend` → default stub。详见 u2-l2 的 dispatcher 五个决策点。
- **可用性 vs 当前后端**：可用性是「机器属性」（这台机器能不能用某后端），当前后端是「进程级单值 `_CURRENT_BACKENDS`」。详见 u2-l3。

本讲要补的新知识是：当后端是 `triton` 时，内核到底是用什么写的、由谁编译的，以及它为什么经常「不亲自上场、而作为别人的兜底」。

几个本讲会用到的术语：

- **Triton DSL**：OpenAI Triton 提供的 Python 内核 DSL，用 `@triton.jit` 装饰、`tl.load` / `tl.store` / `tl.program_id` 等原语描述内核，由 `triton` 包自带的编译器编译成 GPU 代码。
- **Tile IR**：一种以「瓦片（tile）」为一等对象的中间表示，cuTile（`@ct.kernel`）就是直接产出 Tile IR 的；而 nvtriton（Triton-to-tile-IR）则是把标准 Triton DSL 先翻译成 Tile IR，再走同一条 lowering 路径。
- **oait / nvt**：本讲的核心区分，详见 4.2。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/triton/rms_norm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/rms_norm.py) | Triton 版 RMSNorm 内核 + autograd 封装 + 模块工厂，是本讲主样本之一 |
| [src/tilegym/ops/triton/rope.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/rope.py) | Triton 版 RoPE 内核（含 TMA 路径与 Liger-Kernel 风格通用路径） |
| [src/tilegym/ops/triton/dropout.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/dropout.py) | Triton 版 dropout，演示 `get_available_triton_backend()` 如何影响 autotune 配置 |
| [src/tilegym/backend/selector.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py) | 后端探测中心：`is_nvt_available`、`get_available_triton_backend`、`_check_backends_availability` |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | 统一算子接口，六个算子在此声明 `fallback_backend="triton"` |
| [src/tilegym/backend/dispatcher.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py) | 分发器，兜底查找逻辑在此 |

---

## 4. 核心概念与源码讲解

### 4.1 Triton 后端：用 `@triton.jit` 写内核

#### 4.1.1 概念说明

回顾三个后端写内核的语言：

| 后端 | 内核语言 | 编译器 | 入口装饰器 |
| --- | --- | --- | --- |
| `cutile` | cuTile Python DSL | 运行时 `tileiras` | `@ct.kernel` |
| `tilecpp` | CUDA Tile C++ 模板 | 离线 `nvcc` | `__tile_global__` |
| `triton` | 标准 Triton DSL | `triton` 包自带编译器 | `@triton.jit` |

`triton` 后端的关键特点是：**内核源码就是一份普通的 Triton 程序**——用 `tl.program_id` 取块号、用 `tl.load` / `tl.store` 做指针级访存、用 `tl.arange` 生成下标。它不依赖 cuTile 的 `cuda.tile`，也不依赖 `nvcc`，只依赖 `import triton` 能成功。正因如此，`triton` 是四个后端里**部署门槛最低**的一个。

但低门槛是有代价的：`triton` 后端**只实现了一小族算子**。看一眼目录就明白：

```
src/tilegym/ops/triton/
├── dropout.py            # dropout
├── layer_norm_legacy.py  # layer_norm_legacy / persistent_layer_norm
├── rms_norm.py           # rms_norm + 模块工厂
└── rope.py               # apply_rope_base + RoPE 工厂
```

这里**没有** softmax、silu_and_mul、matmul、bmm、fmha、mla——TileGym 最想展示的那些高性能内核，`triton` 后端一个都没实现。换句话说，`triton` 是一个「**部分后端**（partial backend）」：它只覆盖每个 transformer 模型都需要的「工具原语」（归一化、位置编码、dropout），而把真正的算力展示留给 cuTile / tilecpp。

#### 4.1.2 核心流程

当当前后端是 `triton` 时，一次 `tilegym.ops.rms_norm(...)` 的完整路径：

```
ops.rms_norm(x, ...)                      # ops.py 里的 stub，仅抛 NotImplementedError
  └─> dispatch wrapper                     # dispatcher.py
        查 _REGISTRY["rms_norm"]["triton"] # 命中 triton 实现
        └─> triton/rms_norm.py: rms_norm   # @register_impl("rms_norm", backend="triton")
              └─> _RMSNorm.apply(...)      # torch.autograd.Function
                    └─> 选 mode (static_persistent / multi_wave_reload)
                          └─> triton 内核 [...grid...](...)  # @triton.jit，由 triton 编译器 JIT
```

注意两个反差点：

1. **autotune 机制不同**：cuTile 用 TileGym 自己的 `exhaustive_search`（u5-l3），而 triton 后端直接用 `@triton.autotune`（triton 包原生能力），见 rms_norm.py 里的 `_get_rms_norm_autotune_config()`。
2. **TMA 用法不同**：cuTile 用 `ct.load` 配 `padding_mode`，triton 用 `triton.tools.tensor_descriptor.TensorDescriptor`，并在主机侧用 `triton.set_allocator(alloc_fn)` 提供描述符显存。

#### 4.1.3 源码精读

先看 rms_norm.py 的导入与一个模块级变量——它是连接「内核」与「oait/nvt 选择」的桥梁：

[src/tilegym/ops/triton/rms_norm.py:13-17](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/rms_norm.py#L13-L17) —— 导入标准 `triton` / `triton.language`，并在模块加载时调用 `get_available_triton_backend()` 把当前的 triton 子后端（oait/nvt）存进 `backend` 变量（本讲后续会用到）。

内核本身是地道 Triton 写法。看非持久化的 `_rms_norm_kernel`：

[src/tilegym/ops/triton/rms_norm.py:60-101](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/rms_norm.py#L60-L101) —— `@triton.jit` 内核：`row = tl.program_id(0)` 取行号，`tl.arange(0, BLOCK_SIZE)` 生成列下标，`tl.load(..., mask=cols < N, other=0.0)` 做带边界掩码的指针加载，`tl.sum(_rms, axis=0)` 做跨列归约，最后 `tl.store` 写回。这套写法和 cuTile 的 `ct.gather` / `ct.scatter` 形似但语义不同：这里是**裸指针 + 偏移**，cuTile 是「锚点 + 矩形 shape」。

注册环节和 tilecpp 完全同构——后端无关：

[src/tilegym/ops/triton/rms_norm.py:341-361](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/rms_norm.py#L341-L361) —— `@register_impl("rms_norm", backend="triton")` 把这个函数挂到全局注册表的 `rms_norm → triton` 键下，与 cuTile 版的 `rms_norm → cutile` 共享同一个算子名。这正是 u7-l1 强调的「算子名是全局键、后端是子键」。

RoPE 内核同理，且更明确地标注了来源：

[src/tilegym/ops/triton/rope.py:445-470](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/rope.py#L445-L470) —— `@register_impl("apply_rope_base", backend="triton")`，函数体只是 `_TritonRopeFunction.apply(...)` 的薄封装。其上方的 `_rope_kernel` 注释写着「Adapted from Liger-Kernel」，说明 triton 后端的内核不少是社区成熟实现的移植，而非 TileGym 从零自研——这恰好呼应了它「可靠兜底」的定位。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `triton` 后端可用、且能算出与 PyTorch 参考一致的 RMSNorm。

**操作步骤**：

1. 确认环境里有 `triton`（它是 TileGym 的必装依赖，通常已在）。
2. 写一段约 15 行脚本（**示例代码，未运行**）：

```python
import torch
import tilegym
from tilegym import ops

tilegym.set_backend("triton")           # 切到 triton 后端
print("available:", tilegym.get_available_backends())  # triton 一定在里面

M, N = 4096, 2048
x = torch.randn(M, N, device="cuda")
w = torch.ones(N, device="cuda")
y = ops.rms_norm(x, None, w, eps=1e-6)   # 走 triton 实现

# PyTorch 参考
ref = x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + 1e-6)
print("max abs err:", (y - ref).abs().max().item())
```

3. 把 `set_backend("triton")` 换成 `set_backend("cutile")` 再跑一次，比较两者结果。

**需要观察的现象**：

- 两次都能输出一个很小的最大绝对误差（fp32 下应在 `1e-5` 量级）。
- 切到 `triton` 时**不应**看到「falling back to ... backend」的告警（因为 triton 自己有实现）；切到 `cutile` 时也不应告警（cutile 也有实现）。

**预期结果**：`triton` 后端独立可工作，结果与 cutile 一致。

**待本地验证**：具体误差数值取决于 GPU 与 triton 版本，请在本地确认。

#### 4.1.5 小练习与答案

**练习 1**：目录里没有 `softmax.py`，如果当前后端是 `triton`，调用 `tilegym.ops.softmax(x)` 会发生什么？

**答案**：dispatch wrapper 先查 `_REGISTRY["softmax"]["triton"]`——不存在；再查 fallback（softmax 的 `fallback_backend` 是默认的 `"pytorch"`，注册表里也没有 `"pytorch"` 键）；最后落到 default stub，抛出 `softmax is not implemented for triton`。也就是说 `triton` 后端不支持 softmax，且不会静默兜底。

**练习 2**：rms_norm.py 里同时出现了 `@triton.autotune` 和 `_RMSNorm(torch.autograd.Function)`，它们各起什么作用？

**答案**：`@triton.autotune` 负责**内核层**的配置搜索（BLOCK_SIZE_M、num_warps、num_stages 的组合），是 triton 包原生能力，编译期生效；`_RMSNorm(torch.autograd.Function)` 负责**算子层**的前向/反向封装与 PyTorch 计算图登记。两者是不同层次，互不替代。

---

### 4.2 oait 与 nvt：同一份源码、两个 Triton 编译器

#### 4.2.1 概念说明

`triton` 后端内部还藏着一层细分：你 `import triton` 时，到底 import 的是**哪一个 `triton` 发行版**？TileGym 区分两种：

- **oait**（OpenAI Triton）：PyPI 上的标准 `triton` 包。它把 `@triton.jit` 内核经 LLVM 流水线编译成 PTX / SASS。这是默认情形。
- **nvt**（nvtriton）：NVIDIA 维护的 [Triton-to-tile-IR](https://github.com/triton-lang/Triton-to-tile-IR) 分支。它接受**同一份 `@triton.jit` 源码**，但先翻译成 **Tile IR**，再走与 cuTile 相同的 lowering 路径。这也是本讲标题里「Triton CUDA Tile IR」的由来。

关键点：**oait 与 nvt 共用同一份内核源码**。rms_norm.py、rope.py 里的 `@triton.jit` 内核在两种发行版下都能编译，区别只在「谁来编译、编译到什么 IR」。nvt 发行版额外提供一个可导入的 `triton.backends.tileir` 模块，这正是探测它的依据。

既然源码相同，为什么还要区分？因为两个编译器的性能特征不同（tile IR 路径更贴近 cuTile 的优化空间），内核里会据此挑选不同的 autotune 配置。

#### 4.2.2 核心流程

选择子后端由 selector.py 的两个函数完成：

```
is_nvt_available():
    能 import triton.backends.tileir  AND  ENABLE_TILE == 1   ──>  nvt
    否则                                                        ──>  oait

get_available_triton_backend():
    return "nvt" if is_nvt_available() else "oait"
```

注意「双条件」：单装了 nvtriton wheel 但不设 `ENABLE_TILE=1`，仍判为 oait；反之设了 `ENABLE_TILE=1` 但 wheel 不可 import（`triton.backends.tileir` 缺失），也判为 oait。两个条件必须同时满足。

切换到 nvt 的标准做法（README 给出）分两步：

1. **把 nvtriton wheel 装进独立目录**，而不是主 site-packages：

   ```bash
   pip install --target /opt/nvtriton <nvtriton-wheel-for-your-python>.whl
   ```

2. **运行时用 PYTHONPATH 让它优先**，并用 `ENABLE_TILE=1` 显式开启：

   ```bash
   PYTHONPATH=/opt/nvtriton ENABLE_TILE=1 python your_script.py
   ```

为什么装到独立目录而不是直接 `pip install`？因为 nvtriton 是一个**替代性的 `triton` 发行版**——它的顶层包名也叫 `triton`。若装进主 site-packages，会**覆盖**标准 oait 的 `triton`，你就再也回不去 oait 了。装到 `/opt/nvtriton` 再用 `PYTHONPATH` 前置，等于「按调用决定用哪个 triton」：设了 `PYTHONPATH=/opt/nvtriton` 就用 nvt，不设就用默认的 oait，无需反复装卸。`ENABLE_TILE=1` 则是给 TileGym 的明确信号——「我现在确实在用 nvt，请按 nvt 走」。

#### 4.2.3 源码精读

探测函数：

[src/tilegym/backend/selector.py:20-27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L20-L27) —— `is_nvt_available()`：`try import triton.backends.tileir` 判断 wheel 是否就位，再 `and int(os.environ.get("ENABLE_TILE", -1)) == 1` 判断是否显式开启。两个条件相与。

子后端判定：

[src/tilegym/backend/selector.py:222-225](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L222-L225) —— `get_available_triton_backend()`：一行三元表达式返回 `"nvt"` 或 `"oait"`。

「子后端」这个字符串怎么用？看 dropout 里依据它挑选 autotune 候选：

[src/tilegym/ops/triton/dropout.py:13-27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/triton/dropout.py#L13-L27) —— `_get_dropout_configs()`：当 `get_available_triton_backend() == "nvt"` 且设备是 sm80（Ampere）时，返回一组带 `occupancy` 字段的配置；否则返回标准配置。这就是「源码相同、调优不同」的实例——nvt 编译器对 occupancy 提示有不同响应，所以单独给它一套候选。

（rms_norm.py 顶部那个 `backend = get_available_triton_backend()` 变量也是同一用途的预留入口，供内核按子后端分支。）

#### 4.2.4 代码实践

**实践目标**：弄懂 `PYTHONPATH=/opt/nvtriton ENABLE_TILE=1` 这条命令每一部分的作用。

**操作步骤**（源码阅读型，无需真实 nvtriton wheel）：

1. 读上面引用的 `is_nvt_available()`，写下它返回 `True` 必须满足的两个条件。
2. 在没有 nvtriton wheel 的普通环境里执行：

   ```bash
   python -c "from tilegym.backend.selector import get_available_triton_backend; print(get_available_triton_backend())"
   ```

3. 再试（即使 wheel 不存在，看环境变量的独立影响）：

   ```bash
   ENABLE_TILE=1 python -c "from tilegym.backend.selector import is_nvt_available; print(is_nvt_available())"
   ```

**需要观察的现象**：

- 步骤 2 应输出 `oait`（默认）。
- 步骤 3 即使设了 `ENABLE_TILE=1`，由于 `import triton.backends.tileir` 失败，`is_nvt_available()` 仍返回 `False`——这验证了「双条件缺一不可」。

**预期结果**：仅设环境变量不足以切到 nvt，必须同时让 `triton.backends.tileir` 可被 import（即真正装了 nvtriton wheel 并通过 PYTHONPATH 暴露）。

**待本地验证**：步骤 2/3 的实际输出以本地环境为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 nvtriton wheel 要装到 `/opt/nvtriton` 而不是直接 `pip install` 进主环境？

**答案**：因为 nvtriton 的顶层包名也是 `triton`，直接安装会覆盖标准 oait 的 `triton`，导致无法回退。装到独立目录再按需 `PYTHONPATH` 前置，可以「按调用」在 oait 与 nvt 间切换，互不破坏。

**练习 2**：`get_available_triton_backend()` 的返回值（`"nvt"` / `"oait"`）和 `set_backend("triton")` 里的 `"triton"` 是同一个概念吗？

**答案**：不是。`"triton"` 是**四大后端之一**（与 cutile/tilecpp/cutile-rs 并列），由 `set_backend` 选择；`"nvt"` / `"oait"` 是 triton 后端**内部**的子模式（用哪个 triton 编译器），由 `get_available_triton_backend()` 探测。后者只在前者内部有意义。

---

### 4.3 后端探测：triton 为何恒为可用、nvt 为何需单独探测

#### 4.3.1 概念说明

回顾 u2-l3：每个后端都有一套「可用性探测」策略。四个后端的探测成本与方式各不相同：

| 后端 | 探测方式 | 代价 | 在 `_check_backends_availability` 的取值 |
| --- | --- | --- | --- |
| `cutile` | `import cuda.tile` | 一次 import | 真实探测 |
| `tilecpp` | 廉费模块检查 + 延迟 `nvcc --version` 子进程 | 昂贵、延迟、缓存 | 仅廉价预筛 |
| `cutile-rs` | `cargo` 在 PATH 或预编译 `.so` | 中等 | 真实探测 |
| `triton` | **恒为 `True`** | 零 | **硬编码 `True`** |

`triton` 为什么是硬编码的 `True`？因为 `triton`（与 `torch` 一样）是 TileGym 的**必装依赖**——只要你能 `import tilegym`，就一定能 `import triton`（导入期还有 `_check_torch_dependencies()` 兜底）。既然必然可用，就不必再探测，直接写死 `True`。这也赋予了 `triton` 一个独特角色：**它是唯一一个在所有合法环境里都保证可用的后端**，这正是 4.4 把它选作兜底目标的前提。

而 nvt 子后端则**不能**这么乐观：它依赖一个额外的、默认不存在的 wheel（`triton.backends.tileir`）。所以 nvt 必须单独探测，且采用「双条件」——既要有 wheel，又要显式 `ENABLE_TILE=1`。

#### 4.3.2 核心流程

```
import tilegym
  └─ _initialize_available_backends()
       └─ _check_backends_availability()
             ├─ cutile:  is_cutile_available()      # import cuda.tile
             ├─ triton:  True                        # 恒真
             ├─ tilecpp: _TILECPP_MODULE_IMPORTABLE  # 仅廉价预筛
             └─ cutile-rs: is_cutile_rs_available()
```

注意：`_check_backends_availability()` **根本不调用** `is_nvt_available()`。也就是说，nvt 是否可用，**不影响** `triton` 是否进入 `_AVAILABLE_BACKENDS`——`triton` 永远在。nvt 的探测是「按需」的：只有内核内部（如 dropout 选配置）主动调 `get_available_triton_backend()` 时才会触发。

#### 4.3.3 源码精读

[src/tilegym/backend/selector.py:188-195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L188-L195) —— `_check_backends_availability()`：注意 `"triton": True` 是字面量硬编码，不调用任何探测函数；与同表里 `cutile`、`tilecpp`、`cutile-rs` 都调用真实探测函数形成对比。

对比 nvt 的独立探测：

[src/tilegym/backend/selector.py:20-27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/selector.py#L20-L27) —— `is_nvt_available()` 既查 wheel 又查环境变量，是个「重」探测，但它**不在** import 期被调用，只在内核需要子后端信息时才执行，避免给不需要 nvt 的用户增加开销。这与 tilecpp 把昂贵的 nvcc 检查延迟到首次 dispatch 是同一种工程哲学（u2-l3 已讲）。

#### 4.3.4 代码实践

**实践目标**：验证 `triton` 永远出现在可用后端列表里，且与 nvt 探测相互独立。

**操作步骤**：

1. 在任何能 `import tilegym` 的环境里运行：

   ```python
   import tilegym
   from tilegym.backend.selector import is_nvt_available, get_available_triton_backend
   print("backends:", tilegym.get_available_backends())
   print("nvt?:", is_nvt_available())
   print("triton sub:", get_available_triton_backend())
   ```

2. 观察 `triton` 是否总在第一个打印里，而后两个打印是否可能为 `False` / `oait`。

**需要观察的现象**：即使 `is_nvt_available()` 为 `False`，`triton` 依然在 `get_available_backends()` 中。

**预期结果**：`triton` 的可用性与 nvt 的可用性完全解耦——前者恒真，后者条件成立才真。

**待本地验证**：实际打印值以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_check_backends_availability()` 不把 `is_nvt_available()` 的结果算进 `triton` 那一项？

**答案**：因为 `triton` 后端在 oait 下就已经可用，nvt 只是它的一种「增强子模式」。把 nvt 探测塞进可用性表会让没有 nvtriton wheel 的环境误判 `triton` 不可用，反而破坏了「triton 恒可用」这一兜底前提。所以 nvt 探测被设计成按需、独立、不在 import 期触发。

**练习 2**：`triton` 探测代价为零、`tilecpp` 探测代价高，二者在 import 期的处理有何不同？

**答案**：`triton` 直接写死 `True`；`tilecpp` 在 import 期只做廉费的 `_TILECPP_MODULE_IMPORTABLE` 预筛，把昂贵的 `nvcc --version` 子进程延迟到首次 dispatch（`is_tilecpp_available`，`@functools.cache` 全程一次）。两者都体现了「贵的检查要推迟」的原则，只是 triton 因为必装连推迟都不需要。

---

### 4.4 `fallback_backend="triton"`：可降级算子与硬错误算子

#### 4.4.1 概念说明

u2-l1 讲过：`@dispatch` 的 `fallback_backend` 是**逐算子**属性，决定「当前后端没实现时，退到哪个后端」。`dispatch` 装饰器的默认值是 `fallback_backend="pytorch"`——但注册表里根本没有 `"pytorch"` 键，所以默认值实际意味着「**没有优雅降级**，缺失即抛 NotImplementedError」。

`ops.py` 里有六个算子**特意**把 `fallback_backend` 改成了 `"triton"`：

- `get_apply_rope_func`
- `apply_rope_base`
- `rms_norm`
- `dropout`
- `layer_norm_legacy`
- `persistent_layer_norm`

仔细对照就会发现：**这六个正是 `ops/triton/` 目录下实现了的算子**。这不是巧合，而是设计——把兜底目标设为 `triton` 的前提，是 triton 真的提供了该算子的实现，且 triton 永远可用（4.3）。于是当 cutile 或 tilecpp 还没实现（或暂未实现）某个工具算子时，库会自动、优雅地退到 triton 版本继续工作，而不是崩掉。

为什么只给这六个兜底，而不给 softmax / matmul / fmha 兜底？因为这两类算子的定位不同：

- **工具原语**（norm / rope / dropout）：每个模型都要用、但不是 TileGym 的展示重点。它们必须「随时能用」，所以用永远可用的 triton 做兜底，保证模型能跑通。
- **展示型内核**（softmax / GEMM / attention）：这些是 TileGym 存在的意义——展示 cuTile / tilecpp 的高性能。如果你的主后端连这些都没有，那「跑一个慢吞吞的 triton 版」既不是项目目标，也容易让人误以为「这就是 TileGym 的性能」。所以它们默认 `fallback_backend="pytorch"`（即不降级），直接硬错误，逼用户正视后端能力。

#### 4.4.2 核心流程

分发器的兜底查找逻辑（u2-l2 详述过五个决策点，这里聚焦兜底段）：

```
wrapper(*args, **kwargs):
    current_backend = 显式 backend= 参数  或  get_current_backend()
    # tilecpp 健康检查（略）

    1. 查 _REGISTRY[name][current_backend]  ──命中──> 直接用
    2. 查 _REGISTRY[name][fallback_backend] ──命中──> 用（首次告警一次）
                                           └─ DISABLE_FALLBACK=1 则抛错
    3. 都没有 ──> default stub（抛 NotImplementedError）
                  └─ DISABLE_FALLBACK=1 则提前抛错
```

对 `rms_norm`（`fallback_backend="triton"`）：

- 当前后端 `cutile` 且 cutile 有实现 → 走 cutile。
- 当前后端 `cutile` 但 cutile 无实现（假设） → 查 `triton` → 命中 → 走 triton，首次打一条 warning。
- 当前后端 `triton` → 直接走 triton。

对 `softmax`（默认 `fallback_backend="pytorch"`）：

- 当前后端 `cutile` 有实现 → 走 cutile。
- 当前后端 `triton` → 查 `triton`（无）→ 查 `pytorch`（无）→ default stub → 抛 `softmax is not implemented for triton`。

#### 4.4.3 源码精读

先看「有兜底」的算子，以 rms_norm 为例：

[src/tilegym/ops/ops.py:134-159](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L134-L159) —— `@dispatch("rms_norm", fallback_backend="triton")`，函数体仅 `raise NotImplementedError(...)`。同样的还有 `get_apply_rope_func` / `apply_rope_base`：

[src/tilegym/ops/ops.py:27-41](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L27-L41) —— RoPE 工厂算子也显式声明 `fallback_backend="triton"`。

再看「无兜底」的对照，silu_and_mul：

[src/tilegym/ops/ops.py:172-193](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L172-L193) —— `@dispatch("silu_and_mul",)` 没有写 `fallback_backend`，因而取默认值 `"pytorch"`。由于注册表无 `"pytorch"` 键，等于「不降级」。

分发器的兜底段：

[src/tilegym/backend/dispatcher.py:99-115](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/backend/dispatcher.py#L99-L115) —— 当当前后端未命中，查 `fallback_backend`：命中则用（受 `DISABLE_FALLBACK=1` 抑制、用 `_LOGGED_WARNINGS` 三元组去重保证每种组合只告警一次）。这段正是 `fallback_backend="triton"` 真正生效的地方。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次「cutile → triton」的兜底降级，观察告警。

**操作步骤**（源码阅读 + 可选运行）：

1. 在 `ops.py` 里用 `grep fallback_backend` 列出所有声明该参数的算子，确认它们就是 `ops/triton/` 目录下的那一族。
2.（可选运行）写一个脚本，临时**只**给 rms_norm 注册 triton 实现、不给 cutile 实现（可通过 `tilegym.backend.dispatcher._REGISTRY` 观察），或更简单地：直接把后端切到一个「该算子无实现」的组合，例如 `set_backend("tilecpp")` 后调用 rms_norm（若该机器 tilecpp 无 rms_norm 实现），观察是否回落到 triton 并打印一次 warning。
3. 设置 `DISABLE_FALLBACK=1` 再跑一次同样调用，观察是否变成抛 `NotImplementedError`。

**示例代码（观察注册表，未运行）**：

```python
from tilegym.backend.dispatcher import get_registry_info
import json
info = get_registry_info()
for op in ("rms_norm", "softmax", "silu_and_mul", "apply_rope_base"):
    print(op, "->", info.get(op))
```

**需要观察的现象**：

- `rms_norm`、`apply_rope_base` 等的注册表里同时含 `triton` 键（和 `cutile` 键）；`softmax`、`silu_and_mul` 则**没有** `triton` 键。
- 触发降级时会看到一条 `Current backend '...' has no implementation for '...', falling back to 'triton' backend` 的告警，且**同组合只出现一次**。
- `DISABLE_FALLBACK=1` 时不再降级，而是抛错。

**预期结果**：`fallback_backend="triton"` 的算子能优雅降级；默认 fallback 的算子不能。

**待本地验证**：注册表内容随运行环境（哪些后端被导入）而变，请在本地确认实际键集合。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `softmax` 的 `fallback_backend` 不设成 `"triton"`？

**答案**：因为 `ops/triton/` 下没有 softmax 实现，`_REGISTRY["softmax"]` 里没有 `"triton"` 键。即便把 `fallback_backend` 设成 `"triton"`，分发器在兜底段也查不到，最终仍会落到 default stub 抛错——没有任何收益。更深层的原因是 softmax 属于展示型内核，项目不希望它静默跑一个不存在的「慢版」。

**练习 2**：兜底告警为什么用 `_LOGGED_WARNINGS` 做去重，而不是每次都打印？

**答案**：一次模型推理可能调用 rms_norm 等成百上千次。若每次降级都打印告警，日志会被同一句话刷爆。用 `{name}_{current}_{fallback}` 三元组去重，保证「每种算子×每种缺失组合」只告警一次，既提示了用户、又不产生噪声。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「全链路追踪」任务：

**背景**：你要向同事解释「为什么 TileGym 在一台没装 cuda-tile 的机器上，跑 Llama 推理时 RMSNorm 仍然能出正确结果」。

**任务**：

1. **定位实现**：在源码中找到 `rms_norm` 的 triton 实现，确认它用 `@triton.jit` 写、由 `triton` 包编译（引用 rms_norm.py 的内核行号）。
2. **追踪兜底**：说明在这台机器上，`import tilegym` 后 `cutile` 不可用、`triton` 可用（引用 selector.py 的 `_check_backends_availability`）；当 monkey-patch 把模型指向 `tilegym.ops.rms_norm` 时，分发器因当前后端无 cutile 实现而走到 `fallback_backend="triton"`（引用 ops.py 与 dispatcher.py 的兜底段）。
3. **解释子后端**：写出判断这台机器用的是 oait 还是 nvt 的两步检查（`is_nvt_available` 的双条件），并说明若想切到 nvt 需要执行的命令（`pip install --target /opt/nvtriton ...` + `PYTHONPATH=/opt/nvtriton ENABLE_TILE=1 ...`）。
4. **画出数据流**：用一张文字流程图，标出「`ops.rms_norm` → dispatch wrapper → `_REGISTRY["rms_norm"]["triton"]` → `_RMSNorm.apply` → `@triton.jit` 内核」这条最短路径。

**验收标准**：你的解释里应同时出现「triton 恒可用（4.3）」「fallback_backend=triton 的算子集合 = triton 已实现算子集合（4.4）」「oait/nvt 双条件（4.2）」三个结论，并能给出至少四处带行号的源码引用。

---

## 6. 本讲小结

- `triton` 后端用**标准 Triton DSL**（`@triton.jit`）写内核，部署门槛最低，但只覆盖 norm / RoPE / dropout 一族工具算子，是**部分后端**。
- `triton` 后端内部分 **oait**（标准 OpenAI Triton）与 **nvt**（Triton-to-tile-IR，先翻译成 Tile IR）；二者共用同一份 `@triton.jit` 源码，靠 `triton.backends.tileir` 是否可 import + `ENABLE_TILE==1` 双条件区分。
- 切到 nvt 的标准做法是把 wheel 装进独立目录 `/opt/nvtriton`（避免覆盖 oait），再 `PYTHONPATH=/opt/nvtriton ENABLE_TILE=1` 运行；子后端字符串会被内核用来挑选不同的 autotune 配置。
- `triton` 在 `_check_backends_availability()` 里**恒为 `True`**（因为 triton 是必装依赖），是四个后端里唯一保证在所有合法环境都可用的——这是它能当兜底的前提。
- `ops.py` 里六个算子声明 `fallback_backend="triton"`，恰好就是 triton 已实现的六个；当主后端缺失时优雅降级到 triton（首次告警、可被 `DISABLE_FALLBACK=1` 抑制）；而 softmax / matmul 等展示型内核默认不降级，缺失即硬错误。

---

## 7. 下一步学习建议

- 下一讲 **u7-l3（cuTile-rs Rust FFI 后端）** 会讲第四个后端：用 Rust 写内核、编译成 `.so`、经 cffi 绑定，并用 CUPTI 做 autotune。学完后你将集齐全部四个后端的完整图景。
- 想加深对「兜底」机制的理解，可重读 u2-l1（`@dispatch` 与 `fallback_backend`）与 u2-l2（dispatcher 五个决策点），并把本讲的 `fallback_backend="triton"` 案例代入那套框架验证一遍。
- 想了解 triton 后端的内核如何被 monkey-patch 进真实模型，可跳读 `src/tilegym/transformers/monkey_patch.py`，看 rms_norm / rope 的替换点（u8-l1 会系统讲解）。
- 对「Triton-to-tile-IR」编译路径本身感兴趣，可参阅 README 里给出的外部仓库 [triton-lang/Triton-to-tile-IR](https://github.com/triton-lang/Triton-to-tile-IR)。
