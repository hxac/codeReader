# Triton / CUDA Kernel

## 1. 本讲目标

本讲是 PyTorch 后端「算子链路」的最后一站。在前几讲里我们反复看到一条规律：`models/*.py` 的重写类不写数学公式，只调用 `nn/` 积木；`nn/` 积木又用「薄包装 + 委托」把真正的计算丢给 `backends/`；而 `backends/` 里那些 `Impl` 的 `.forward()` 最终落点，就是 `kernels/` 目录下的 Triton/CUDA kernel。

学完本讲你应当能够：

1. 看懂 `lmdeploy/pytorch/kernels/` 的目录组织，理解 `cuda / default / dlinfer` 三个设备子目录的分工与各自的 `__init__.py`「导出表」。
2. 读懂 `kernels/dispatcher.py` 的 `FunctionDispatcher`——它是按**运行时设备**动态挑选 kernel 实现的分发器，理解「延迟加载 + 缓存 + 代码生成薄壳」三件套。
3. 精读 `cuda/w8a8_triton_kernels.py` 中的 `@triton.jit` kernel：掌握 Triton 的 `grid / program_id / block tiling / accumulator` 写法，以及 W8A8（SmoothQuant 风格的 INT8 权重 + INT8 激活）动态量化 GEMM 的入口与作用。
4. 理清「离线权重量化 → 在线激活量化 → 融合 GEMM」这条 W8A8 数据流，并知道 `rms_norm_dynamic_quant` 如何把归一化与量化融合进一个 kernel。

> 本讲承接 **u5-l4（算子后端分发 backends）**。u5-l4 讲的是 `backends/` 如何用 `OpType` 枚举 + 设备类层级选 `Impl`；本讲往下再钻一层：`backends` 的 `Impl` 调用的底层函数（`matmul_kernel_dynamic_quant` 等）从哪里来、如何按设备切换、kernel 内部长什么样。建议读者先读过 u5-l2（线性层与权重量化变体）中关于 W8A8/SmoothQuant 的部分。

---

## 2. 前置知识

### 2.1 什么是 Triton

**Triton** 是一个「用 Python 写 GPU kernel」的编译器。写 CUDA C++ kernel 需要手动管理线程块、共享内存、寄存器；Triton 把这些隐藏起来，让你在一个「程序（program）」里以**块（block）**为单位操作张量，编译器自动把它翻译成高效的 GPU 代码。

理解 Triton 只需要三个关键词：

- **`@triton.jit`（Just-In-Time）**：装饰一个函数，表示「这是个 kernel，运行时按参数编译」。
- **grid（网格）**：kernel 的一次启动会并发运行很多个「程序实例」，每个实例由 `program_id` 区分。`grid` 是一个元组，决定启动多少个程序。例如 `grid=(N,)` 表示启动 N 个一维程序。
- **block tiling（分块）**：每个程序处理输出的一小块（如 `BLOCK_M × BLOCK_N`），用 `tl.arange` 生成块内坐标，用 `tl.load / tl.store` 带掩码地读写全局显存，用 `tl.dot` 做块级矩阵乘并累加到 `accumulator`。

### 2.2 什么是 W8A8 / 动态量化

W8A8 = **W**eight 8-bit × **A**ctivation 8-bit，是 SmoothQuant 风格的量化方案（回顾 u5-l2）：

- **权重**在**离线**（量化/calibration 阶段）就量化好，存成 INT8（或 FP8），带一个**逐输出通道**的 `scale`。
- **激活**在**在线**（每次 forward）动态量化成 INT8，带一个**逐 token**的 `scale`（因为激活的数值范围每个 batch 都变）。

于是线性层 `Y = X · Wᵀ` 变成 INT8 GEMM 后再乘回两组 scale：

\[ Y = (X_q \cdot s_x) \cdot (W_q \cdot s_w)^\top = (X_q \cdot W_q^\top) \odot (s_x \cdot s_w^\top) \]

其中 \(X_q\) 是 INT8 激活、\(W_q\) 是 INT8 权重、\(s_x\) 是逐 token scale（形状 `(M,1)`）、\(s_w\) 是逐通道 scale（形状 `(1,N)`）。INT8 GEMM 的吞吐远高于 FP16，是 W8A8 加速的来源。本讲的 `_linear` kernel 就是把这个「INT8 GEMM + 乘 scale」融合成一次 kernel 启动。

### 2.3 一个关键提醒：两个 `w8a8_triton_kernels.py`

仓库里有**两个同名文件**，别混淆：

- `lmdeploy/pytorch/kernels/w8a8_triton_kernels.py`（顶层，只有 10 行）—— 一个**薄 re-export 封装**，把四个函数名转发出去，被 `models/q_modules.py` 等导入。
- `lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py`（cuda 子目录，约 600 行）—— **真正的 Triton kernel 实现所在地**，所有 `@triton.jit` 都在这里。

本讲「源码精读」聚焦后者（实现），「分发」聚焦 `dispatcher.py`（路由）。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读重点 |
| --- | --- | --- |
| `lmdeploy/pytorch/kernels/dispatcher.py` | **按设备分发 kernel 的路由器** | `FunctionDispatcher` 的延迟加载、缓存、代码生成 |
| `lmdeploy/pytorch/kernels/__init__.py` | kernels 包入口，构造四个分发壳函数 | `make_caller()` 如何造出统一入口 |
| `lmdeploy/pytorch/kernels/w8a8_triton_kernels.py` | 顶层薄封装，re-export 四个名字 | 为什么需要这层转发 |
| `lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py` | **真正的 W8A8 Triton kernel** | `@triton.jit` kernel 的 grid/block/scale |
| `lmdeploy/pytorch/kernels/cuda/__init__.py` | cuda 子包导出表 | cuda 设备暴露了哪些函数 |
| `lmdeploy/pytorch/kernels/default/__init__.py` | default 子包导出表（纯 PyTorch 兜底） | 兜底实现的最小集合 |
| `lmdeploy/pytorch/kernels/dlinfer/__init__.py` | ascend/npu/maca/camb 设备导出表 | 非 cuda 设备走 dlinfer |
| `lmdeploy/pytorch/kernels/default/w8a8_kernels.py` | `per_channel_quant` 默认实现 | 权重逐通道量化的纯 PyTorch 版本 |

---

## 4. 核心概念与源码讲解

### 4.1 设备子目录布局：cuda / default / dlinfer

#### 4.1.1 概念说明

`kernels/` 目录被切成三个**并列的设备子包**，每个子包用同名函数给出本设备的实现：

- **`cuda/`**：NVIDIA GPU。是主力，也是唯一有 Triton kernel 的地方（Triton 目前主要面向 CUDA）。包含大量融合算子（attention、fused_moe、rms_norm、awq、w8a8……）。
- **`default/`**：**纯 PyTorch 兜底实现**。任何设备都至少能用它跑通（虽然慢）。它只暴露了极小的集合——主要是 `per_channel_quant` 和 `multinomial_sampling`，因为这两个用纯 PyTorch 写就足够。
- **`dlinfer/`**：华为昇腾 NPU、寒武纪、海光 MACA 等非 NVIDIA 设备。它复用 [dlinfer](https://github.com/dlinfer) 项目提供的设备算子。

关键设计：**同名函数，不同实现**。比如 `per_channel_quant` 在 `default/` 是纯 PyTorch，在 `cuda/` 直接复用 `default` 的版本（cuda 没有更快的需求），而在 `dlinfer/` 也复用 `default`。而 `rms_norm` 则三个目录各有一份。这种「按目录分设备、按文件名分算子」的布局，是 `dispatcher.py` 能用字符串拼出导入路径的前提。

#### 4.1.2 核心流程

每个设备子包的 `__init__.py` 都是一张**导出表**——`from .xxx import yyy` 把本设备实现的函数收集到子包命名空间，并写进 `__all__`。分发器只需 `importlib.import_module('lmdeploy.pytorch.kernels.<device>')` 再 `getattr(mod, func_name)`，就能拿到该设备的实现。所以：

```
设备子包 __init__.py 的 __all__  ==  该设备「支持哪些算子」的清单
```

#### 4.1.3 源码精读

cuda 子包把 W8A8 的三个 Triton 函数从 `.w8a8_triton_kernels` 导出，`per_channel_quant` 则复用 default：

[cuda/\_\_init\_\_.py:L12-L12](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/__init__.py#L12) —— cuda 设备从本目录的 `w8a8_triton_kernels` 导入三个 Triton kernel，`per_channel_quant` 复用 default 的纯 PyTorch 版本。

default 子包导出表极小，只有两个函数：

[default/\_\_init\_\_.py:L2-L8](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/default/__init__.py#L2-L8) —— default 设备（纯 PyTorch 兜底）只暴露 `multinomial_sampling` 与 `per_channel_quant`，是最小可用集合。

dlinfer 子包则把大量算子重新映射到自己的设备实现：

[dlinfer/\_\_init\_\_.py:L2-L12](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/dlinfer/__init__.py#L2-L12) —— ascend/npu/maca/camb 设备经 dlinfer 提供 `rms_norm / paged_attention_fwd / linear / fused_moe` 等；注意第 2 行 `multinomial_sampling` 与 `per_channel_quant` 仍回退到 default。

default 的 `per_channel_quant` 是理解 W8A8 权重量化的最好入口——逐通道求绝对值最大，除以量化上限得到 scale，再量化并钳位：

[default/w8a8_kernels.py:L5-L30](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/default/w8a8_kernels.py#L5-L30) —— 纯 PyTorch 版 `per_channel_quant`：`scale = x_absmax / q_max`，`x_q = clamp(round(x / scale), q_min, q_max)`。它既是 default 兜底实现，也被 cuda/dlinfer 复用。

#### 4.1.4 代码实践

1. **目标**：用目录结构回答「某设备支持哪些算子」。
2. **步骤**：在仓库根执行 `ls lmdeploy/pytorch/kernels/cuda/`、`ls .../default/`、`ls .../dlinfer/`；再打开三个 `__init__.py` 看 `__all__`。
3. **现象**：cuda 文件最多（20+ 算子），default 最少（2 个），dlinfer 居中。
4. **预期结果**：cuda 有 `flashattention.py`、`fused_moe.py` 等高性能融合算子；default 与 dlinfer 都复用 default 的 `per_channel_quant`，说明该算子不需要设备特化。
5. 待本地验证（无 GPU 也能执行 `ls` 与读文件部分）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `default/` 只暴露 2 个函数，而 cuda 暴露 20+？

**答**：default 是「兜底」，目标是「能跑通」而非「快」，所以只把那些**纯 PyTorch 写也必须存在**的基础算子（采样、权重量化）放进来；其余高性能融合算子（attention/MoE）若用纯 PyTorch 实现既慢又无意义，干脆不给兜底，逼使用方走设备实现。

**Q2**：`per_channel_quant` 的输入要求 `x.ndim == 2`（见 default 实现第 19 行），为什么是 2 维？

**答**：因为它是**逐输出通道**量化权重，权重本身就是 `(out_features, in_features)` 的 2 维矩阵；`axis=1` 上求 max 即得到每个输出通道的 scale。

---

### 4.2 kernel dispatcher：按运行时设备动态分发

#### 4.2.1 概念说明

4.1 讲的是「静态布局」——按目录放好各设备实现。但**调用方不该关心自己在哪个设备上**。理想写法是：

```python
from lmdeploy.pytorch.kernels import per_channel_quant
x_q, scale = per_channel_quant(x, dtype)   # 自动用对设备实现
```

这正是 `dispatcher.py` 的 `FunctionDispatcher` 提供的能力。它有三个关键设计：

1. **延迟加载（lazy import）**：直到函数**第一次被调用**，才按当时的设备去 `importlib` 导入对应实现。这避免了进程启动时把所有设备的 kernel 全编译一遍（Triton 编译很慢）。
2. **按设备缓存**：导入结果存进 `impl_map[device]`，下次同设备调用直接命中缓存。
3. **设备变更失效**：当设备上下文切换（如从 cuda 切到 ascend），已缓存的实现作废，下次调用重新解析。

它和 u5-l4 的 `backends/selector.py` 是**两层不同的分发**：

| | `backends/selector.py` | `kernels/dispatcher.py` |
| --- | --- | --- |
| 分发依据 | 设备 + **`OpType` 算子枚举** | 仅设备 |
| 形态 | 类层级（`OpsBackend` 子类） | 函数级（同名函数多实现） |
| 粒度 | 给 `nn/` 积木选 `Impl` | 给底层裸函数选实现 |

`backends` 是「积木→算子实现」的桥；`kernels/dispatcher.py` 是「裸函数→设备实现」的桥。很多地方二者并存：`backends/cuda/qmodules.py` 直接 `from ...kernels.cuda.w8a8_triton_kernels import ...`（写死 cuda），而跨设备代码则走 dispatcher。

#### 4.2.2 核心流程

`FunctionDispatcher` 的生命周期分两阶段：**构造期造壳**、**调用期解析**。

```
构造期（import 时，每个函数一次）：
  __init__ → 注册设备变更回调 → dispatched_func = load_and_call
  make_caller → 用 exec() 生成一个薄壳函数 per_channel_quant(...)
                它的函数体只有一行：return dispatcher.dispatched_func(...)

调用期（每次 per_channel_quant(x, ...)）：
  薄壳 → dispatcher.dispatched_func(...)
  首次/换设备 → load_and_call:
      device = 当前 DeviceContext.device_type      # 'cuda' / 'ascend' / ...
      target = device_map[device]                  # 'cuda'→'cuda', 'ascend'→'dlinfer'
      若 impl_map 无 device:
          load_func(device):
              import lmdeploy.pytorch.kernels.<target>
              func = getattr(mod, 'per_channel_quant')
              impl_map[device] = func       # 失败则回退 kernels.default
      dispatched_func = impl_map[device]    # 后续直接走缓存
  调用真正的 func
```

`device_map` 是「物理设备名 → 子目录名」的翻译表，是非 cuda 设备都归到 `dlinfer` 的关键。

#### 4.2.3 源码精读

**设备映射表**——把五种物理设备名翻译成三个子目录名：

[dispatcher.py:L67-L67](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/dispatcher.py#L67) —— `device_map = {'cuda':'cuda', 'ascend':'dlinfer', 'npu':'dlinfer', 'maca':'dlinfer', 'camb':'dlinfer'}`。这就是 dispatcher「在 cuda/ascend 间选择」的全部秘密：ascend/npu/maca/camb 一律落到 `dlinfer` 子目录。

**加载与回退**——先试目标设备，失败再回退 default：

[dispatcher.py:L73-L88](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/dispatcher.py#L73-L88) —— `load_func`：`import_module('lmdeploy.pytorch.kernels.<device>')` 后 `getattr(mod, func_name)`；若设备子包没有该函数（抛异常），则 `import_module('...kernels.default')`，default 也没有就 `raise RuntimeError`。`logger.debug` 把失败静默成调试日志。

**首次调用解析 + 缓存**：

[dispatcher.py:L90-L96](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/dispatcher.py#L90-L96) —— `load_and_call`：读 `device_manager.current_context().device_type` 决定设备；`impl_map` 未命中才触发 `load_func`；最后把 `dispatched_func` 指向缓存实现，**后续调用直接走第 96 行跳过解析**。

**代码生成薄壳**——`make_caller` 用 `inspect.signature` 读出参数列表，再 `exec` 一段动态生成的源码，造出一个签名正确的薄函数：

[dispatcher.py:L98-L118](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/dispatcher.py#L98-L118) —— 生成的函数体就是 `return dispatcher.dispatched_func(全部参数)`。注意 `make_caller()` 默认 `api=_default_api`（一个空函数），所以生成的壳函数**没有任何类型注解**——它只是个透明转发器。`kernels/__init__.py` 就是用它造出四个公开函数的：

[kernels/\_\_init\_\_.py:L4-L10](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/__init__.py#L4-L10) —— 四行 `FunctionDispatcher('<name>').make_caller()`，分别造出 `per_channel_quant / matmul_kernel_dynamic_quant / per_token_quant_int8 / rms_norm_dynamic_quant` 四个跨设备入口。

**设备变更回调**——注册到 `DeviceManager`，切换设备时把 `dispatched_func` 重置回 `load_and_call`，强制下次重新解析：

[dispatcher.py:L66-L71](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/dispatcher.py#L66-L71) —— 构造时 `register_context_callback(self.device_callback)`；`device_callback` 把 `dispatched_func` 还原为 `load_and_call`，使缓存的 cuda 实现在切到 ascend 后自动失效重载。

> 补充：`DeviceContext` 是个极简 dataclass，只有 `device_type` 一个字段，默认 `'cuda'`；`DeviceManager` 是单例（`@singleton`），用 `current_context()` 暴露当前设备——见 [devices/device_manager.py:L8-L38](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/devices/device_manager.py#L8-L38)。

#### 4.2.4 代码实践

1. **目标**：验证 dispatcher 的「延迟加载 + 回退」行为，且无需 GPU。
2. **步骤**：写一段脚本（示例代码，非项目原码）：

   ```python
   # 示例代码：观察 dispatcher 如何解析实现
   from lmdeploy.pytorch.kernels.dispatcher import FunctionDispatcher
   d = FunctionDispatcher('per_channel_quant')
   print('初始 dispatched_func:', d.dispatched_func.__name__)   # load_and_call
   # 模拟一次调用（会触发 load_and_call → 解析到当前设备实现）
   import torch
   x = torch.randn(4, 8)
   out = d.dispatched_func(x, torch.int8)   # 第一次：走 load_and_call
   print('impl_map keys:', list(d.impl_map.keys()))            # ['cuda'] 或回退的 default
   print('解析后 dispatched_func:', d.dispatched_func.__module__)
   ```
3. **现象**：第一次调用 `dispatched_func` 是 `load_and_call`，调用后 `impl_map` 多出一个设备键，`dispatched_func` 变成真实实现模块的函数。
4. **预期结果**：在有 cuda 的机器上 `impl_map` 出现 `'cuda'`；在无 cuda 的纯 CPU 环境下，`load_func` 会因 `kernels.cuda` 导入失败而回退到 `kernels.default`，`impl_map` 出现 `'cuda'` 键但指向 default 的纯 PyTorch 实现。
5. 待本地验证（取决于本机设备与是否安装 triton）。

#### 4.2.5 小练习与答案

**Q1**：为什么 `make_caller` 要用 `exec` 动态生成函数，而不是直接返回 `lambda *args, **kwargs: self.dispatched_func(*args, **kwargs)`？

**答**：为了让生成的函数有**正确的命名与参数签名**（`def per_channel_quant(...)`），这样在 traceback、IDE 提示、文档里都显示成「正常函数」，而不是匿名 `lambda`。`ParamParser` 负责把每个参数还原成「带默认值、带 `*`/`**`」的字符串形式拼进源码。

**Q2**：若 `device='ascend'` 但 `kernels.dlinfer` 里没有 `per_token_quant_int8`，会发生什么？

**答**：`load_func` 的 `try` 块抛异常 → 进入 `except`，尝试 `import_module('lmdeploy.pytorch.kernels.default')`，若 default 有 `per_token_quant_int8` 就用它（实际上 default 没有，只有 `per_channel_quant` 与 `multinomial_sampling`），default 也没有则 `raise RuntimeError('<per_token_quant_int8> default and <ascend> implementation not exists.')`（见 dispatcher.py 第 84-86 行）。

---

### 4.3 W8A8 Triton kernel 精读

#### 4.3.1 概念说明

本节精读 `cuda/w8a8_triton_kernels.py`。它提供四个公开函数（前三个是 Triton kernel 的 Python 包装，第四个复用自 default）：

| 函数 | 作用 | 是否 Triton |
| --- | --- | --- |
| `per_channel_quant` | 离线把权重逐通道量化（复用 default） | 否 |
| `rms_norm_dynamic_quant` | 在线：RMSNorm + 逐 token 量化（可融合残差） | 是 |
| `per_token_quant_int8` | 在线：把激活逐 token 量化成 INT8 | 是 |
| `matmul_kernel_dynamic_quant` | INT8 GEMM，并把 rms_scale × linear_scale 乘回结果（可融合残差/偏置） | 是 |

它们组成 SmoothQuant 推理的一条流水线（见 4.3.2）。值得注意的是：这里的「INT8 GEMM」**不是**直接调用 cuBLAS，而是用 Triton 手写——因为要在 GEMM 出口**就地乘回两组 scale**并可选融合残差，一次 kernel 启动完成，省掉多次显存读写。

#### 4.3.2 核心流程

一次 W8A8 线性层（`QRMSNorm → QLinear`，见 `models/q_modules.py` 与 `backends/cuda/qmodules.py`）的数据流：

```
权重侧（离线，calibrate 时算一次）：
  W (fp16, N×K) ──per_channel_quant──▶ W_q(int8, N×K) + linear_scale(1×N)

激活侧（在线，每次 forward）：
  x (fp16, M×K)
   │
   ├─ rms_norm_dynamic_quant ──▶ rms_out(int8, M×K) + rms_scale(M×1)
   │      （RMSNorm 与逐 token 量化融合在一个 kernel）
   │
   └─ matmul_kernel_dynamic_quant(rms_out, W_q, rms_scale, linear_scale)
          ▶ C = rms_out @ W_qᵀ  (INT8 GEMM, Triton)
          ▶ C *= rms_scale · linear_scaleᵀ   （就地乘回 scale）
          ▶ 可选：C += residual / C += bias
          ▶ 输出 fp16
```

逐 token 量化的数学定义（对一行激活 \(x\in\mathbb{R}^K\)）：

\[ s = \frac{\max(|x|)}{Q_{\max}}, \quad x_q = \mathrm{round}\!\left(\frac{x}{s}\right) \]

其中 \(Q_{\max}=127\)（INT8）。`per_token_quant_int8` kernel 给每一行算一个 \(s\)，输出量化张量与 scale 向量。

#### 4.3.3 源码精读

##### (a) `_linear`：分块 INT8 GEMM + 乘回 scale

这是本讲的「主角 kernel」。装饰器两层：先 `@triton.autotune`（在两组 tile 配置间自动择优，以 `['N','K']` 为重编译键），再 `@triton.jit(do_not_specialize=['M'])`（M 作为运行时值不特化，避免 M 变一点就重编译）：

[cuda/w8a8_triton_kernels.py:L17-L32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L17-L32) —— `_linear` 的 `@triton.autotune` 给出两组 `Config(BLOCK_M/BLOCK_N/BLOCK_K + num_stages/num_warps)`，`key=['N','K']` 表示只有权重形状变化才重新择优；`@triton.jit(do_not_specialize=['M'])` 让 M 不参与特化。

**grid 与 program 到 tile 的映射**——经典的「grouped tiling」布局，让连续 program 访问的块在 L2 上尽量相邻：

[cuda/w8a8_triton_kernels.py:L61-L69](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L61-L69) —— 从一维 `pid` 反解出二维 `(pid_m, pid_n)`：按 `GROUP_SIZE_M` 分组，组内先走 M 方向。每个 program 负责 `BLOCK_M × BLOCK_N` 的输出块。

**输入张量与主循环**——A 是激活（`(M,K)`，行主序）、B 是权重（`(N,K)` 已转置成行主序，故 `stride_bk/stride_bn`）、C 是输出。沿 K 维分块累加：

[cuda/w8a8_triton_kernels.py:L71-L82](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L71-L82) —— 用 `tl.arange` 生成块内坐标，算出 A/B 的指针；`for k in range(...)` 沿 K 维分块，`tl.load`（带掩码处理 K 不整除 BLOCK_K 的尾部）后 `tl.dot(a,b,accumulator)` 累加。`accumulator` 的 dtype 由 `ACCUMULATOR_DTYPE` 决定（浮点输入用 fp32，整数输入用 int32）。

**就地乘回 scale**——GEMM 算完后，在写回 C 之前乘 `rms_scale(M,1) × linear_scale(1,N)`，正是 2.2 节公式的融合落点：

[cuda/w8a8_triton_kernels.py:L83-L93](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L83-L93) —— `c = accumulator.to(float32)`；`rms_scale = tl.load(rms_scale_ptr + offs_am)[:,None]`（逐行）、`linear_scale = tl.load(linear_scale_ptr + offs_bn)[None,:]`（逐列）；`c = c * rms_scale * linear_scale`；最后带掩码 `tl.store` 写回。

> 同文件还有 `_linear_add`（[L111-L159](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L111-L159)），在 `_linear` 基础上额外 `c += residual`，把残差连接也融合进来，用于「Add + RMSNorm + QLinear」三合一。

##### (b) `matmul_kernel_dynamic_quant`：Python 包装与 grid 函数

kernel 本身只认块级指针，**输入张量维度的展平、输出分配、grid 计算**都在 Python 包装函数里：

[cuda/w8a8_triton_kernels.py:L162-L181](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L162-L181) —— `matmul_kernel_dynamic_quant(a, b, rms_scale, linear_scale, residual=None, bias=None, output_dtype=fp16)`：断言 `a.shape[-1]==b.shape[-1]`、`b` 二维且连续；把 `a` 展平成 `(M,K)`；`grid` 函数返回 `(cdiv(M,BLOCK_M) * cdiv(N,BLOCK_N),)`——**grid 的总程序数 = 输出分块数**，这正是 4.3.3(a) 里 `pid` 反解 `(pid_m,pid_n)` 的对应。

调用处（[L183-L217](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L183-L217)）：按 `residual is not None` 二选一启动 `_linear_add[grid](...)` 或 `_linear[grid](...)`，把所有 stride、scale 指针、`ACCUMULATOR_DTYPE` 传进去；最后 `if bias is not None: c += bias`（bias 是逐输出通道的一维，用 PyTorch 原地加）。

##### (c) `per_token_quant_int8`：逐行量化 kernel

[cuda/w8a8_triton_kernels.py:L224-L260](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L224-L260) —— `_per_token_quant_int8`：`row = tl.program_id(0)` 每个程序处理一行；`_absmax = max(|y|)`；`y_s = _absmax / Q_MAX`；`y_q = y / y_s`；非浮点 dtype 再 `round → int8`。这正是 4.3.2 公式的 kernel 实现。Python 包装 `per_token_quant_int8`（[L263-L295](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L263-L295)）用 `grid=(M,)`（每个 token 一个程序），并按 `BLOCK=next_power_of_2(N)` 启发式选 `num_warps`。

##### (d) `rms_norm_dynamic_quant`：RMSNorm + 量化融合

[cuda/w8a8_triton_kernels.py:L309-L341](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L309-L341) —— `rms_norm_quant_kernel`：每个程序处理一行，先算 RMSNorm（`_compute_rms_norm`，[L298-L306](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L298-L306)：`var = mean(x²)`，`out = x * rsqrt(var+eps) * w`），再对该行求 `scale = max(|out|)/Q_MAX` 并量化、钳位。**归一化与量化在一个 kernel 内完成**，省去中间 fp16 张量的显存往返。同目录还有 `add_rms_norm_quant_kernel`（[L344-L387](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L344-L387)）融合残差。

##### (e) 文件末尾：自测与基准

该文件自带 `if __name__ == '__main__'` 测试入口（[L551-L604](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L551-L604)），用 `test_rms_and_linear` 把 Triton INT8/FP8 路径与纯 PyTorch fp16 路径对比，打印余弦相似度与 TFLOPS——这是本讲「综合实践」的可运行依据。

#### 4.3.4 代码实践

1. **目标**：找到一个 `@triton.jit` kernel，说清它的 grid 与输入张量。
2. **步骤**：打开 `cuda/w8a8_triton_kernels.py`，定位 `_linear`（第 32 行）与它的 Python 包装 `matmul_kernel_dynamic_quant`（第 162 行）。
3. **要回答的问题**：
   - **grid 是什么？** `matmul_kernel_dynamic_quant` 第 180-181 行的 `grid(META)` 返回 `(cdiv(M,BLOCK_M) * cdiv(N,BLOCK_N),)`，即「输出矩阵被切成多少个 `BLOCK_M×BLOCK_N` 块，就启动多少个程序」。
   - **输入张量有哪些？** `a`（激活，`(M,K)`）、`b`（权重，`(N,K)` 已转置连续）、`rms_scale`（逐 token，`(M,)` 或 `(M,1)`）、`linear_scale`（逐通道，`(N,)`），可选 `residual`、`bias`。
   - **kernel 内部如何用它们？** 第 71-82 行沿 K 分块做 `tl.dot`，第 83-87 行把结果乘回 `rms_scale·linear_scale`。
4. **预期结果**：你能向同伴讲清「一个 program 算输出的一块 `BLOCK_M×BLOCK_N`，沿 K 累加，最后乘两组 scale」。
5. 待本地验证（阅读部分无需运行）。

#### 4.3.5 小练习与答案

**Q1**：`_linear` 里 `accumulator` 的 dtype 是怎么定的？为什么 INT8 输入也要用 fp32 累加？

**答**：见 `matmul_kernel_dynamic_quant` 第 178 行：`accumulator_dtype = tl.float32 if a.is_floating_point() else tl.int32`。即便输入是 INT8，`tl.dot` 的累加也用更高精度的类型（这里走 fp32 分支，因为 W8A8 的激活/权重在 kernel 入口仍是浮点或量化后的表示），避免大量乘加的舍入误差累积。

**Q2**：`@triton.autotune` 的 `key=['N','K']` 是什么意思？为什么不含 `M`？

**答**：`key` 列表里的参数**变化时**才重新择优 tile 配置。权重形状 `(N,K)` 对一个线性层是固定的，把它设成 key 能针对不同层选不同配置；而 `M`（token 数）每个 batch 都在变，若设为 key 会频繁触发重编译——所以 `key` 不含 M，且 `@triton.jit(do_not_specialize=['M'])` 进一步明确 M 不特化。

**Q3**：为什么 `rms_norm_dynamic_quant` 要把「归一化」和「量化」融合成一个 kernel，而不是分两步？

**答**：分两步会在中间产生一个 fp16 的归一化结果张量，需要写回显存再读回；融合后中间结果留在寄存器里，直接量化成 INT8 写出，**减少一次显存往返**——对带宽受限的推理场景收益明显。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从设备分发到 kernel 内部」的完整追踪，并（在有 CUDA 的机器上）真的跑一次 W8A8 Triton kernel。

**任务 A：源码追踪（无 GPU 也能做）**

追踪 SmoothQuant 一次线性层的完整调用链，填出下表的「来源文件」：

| 调用点 | 调用的函数 | 该函数实现来自哪个文件 | 经 dispatcher 还是直接 import |
| --- | --- | --- | --- |
| `backends/cuda/qmodules.py:28,72` | `rms_norm_dynamic_quant` / `per_token_quant_int8` | ? | 直接 import（`from ...kernels.cuda.w8a8_triton_kernels import ...`） |
| `backends/cuda/qmodules.py:77` | `matmul_kernel_dynamic_quant` | ? | ? |
| `models/q_modules.py:8` | （import 自 `..kernels.w8a8_triton_kernels`） | 顶层 re-export → ? | ? |

参考答案：cuda 后端的 `qmodules.py` 直接写死从 `kernels.cuda.w8a8_triton_kernels` 导入（不走 dispatcher，因为它已经知道自己在 cuda 上）；`models/q_modules.py` 走的是顶层薄封装 `kernels/w8a8_triton_kernels.py`（见 [kernels/w8a8_triton_kernels.py:L3-L8](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/w8a8_triton_kernels.py#L3-L8)），它只 re-export 四个名字，真正实现在 cuda 子目录。

**任务 B：真跑 kernel（需 CUDA + triton）**

1. 运行文件自带的测试入口（项目原码，非示例）：

   ```bash
   LMDEPLOY_LOG_LEVEL=DEBUG python -m lmdeploy.pytorch.kernels.cuda.w8a8_triton_kernels
   ```
2. 观察输出：会打印 `perchannel error`、`Output cos`（应非常接近 1.0，说明 INT8 与 fp16 结果几乎一致）、以及一张 TFLOPS 基准表（`triton_int8` vs `torch_fp16`，INT8 应明显更高）。
3. 若机器支持 FP8（`device_capability[0] >= 9`），还会自动追加 `triton_fp8_e4m3` / `triton_fp8_e5m2` 两组对比（见 [L562-L564](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/kernels/cuda/w8a8_triton_kernels.py#L562-L564)）。
4. 待本地验证（无 CUDA 时此命令会失败，可退而求其次只做任务 A）。

**任务 C：观察 dispatcher 选设备（无需 GPU）**

按 4.2.4 的示例脚本，在纯 CPU 环境构造 `FunctionDispatcher('per_channel_quant')` 并触发一次调用，确认 `impl_map` 出现 `'cuda'` 键但指向 `kernels.default` 的回退实现（因为本机没有 triton/cuda）。这验证了「设备加载失败 → 回退 default」的容错路径。

---

## 6. 本讲小结

- `kernels/` 按**设备切目录**（`cuda`/`default`/`dlinfer`），每个子包 `__init__.py` 是该设备「支持哪些算子」的导出表；`default` 是纯 PyTorch 兜底，只暴露最小集合。
- `dispatcher.py` 的 `FunctionDispatcher` 提供**按运行时设备动态分发**：`device_map` 把 ascend/npu/maca/camb 都映射到 `dlinfer`；首次调用 `load_and_call` 才延迟 `importlib` 导入实现并缓存进 `impl_map`；加载失败回退 `default`；设备切换时回调把缓存实现作废。
- `make_caller` 用 `inspect` + `exec` **代码生成**一个签名正确的薄壳函数，对外暴露统一的 `per_channel_quant` 等入口——`kernels/__init__.py` 四行即造出全部跨设备函数。
- `cuda/w8a8_triton_kernels.py` 是 W8A8 的 Triton 实现所在地：`_linear` 用 `@triton.autotune`+`@triton.jit` 做分块 INT8 GEMM，grid = 输出分块数，每个 program 处理 `BLOCK_M×BLOCK_N`、沿 K 累加，并**就地乘回 rms_scale·linear_scale**（可融合残差/偏置）。
- `rms_norm_dynamic_quant` 与 `per_token_quant_int8` 把「归一化/量化」融合进单个 kernel，减少显存往返；权重侧的 `per_channel_quant` 离线算好、三个设备都复用 default 的纯 PyTorch 版本。
- 本讲与 u5-l4 互补：`backends/selector.py` 按「设备+OpType」选 `Impl`，`kernels/dispatcher.py` 按「设备」选裸函数；二者常配合使用，cuda 后端也常直接 `import` 写死设备实现以省一次间接。

---

## 7. 下一步学习建议

1. **横向对比其它 cuda kernel**：本讲只精读了 W8A8 的线性类 kernel。建议接着读 `kernels/cuda/awq_kernels.py`（W4A16 的 Marlin 风格 kernel）、`kernels/cuda/blocked_gemm_fp8.py`（分块 FP8 GEMM），对比它们与 `_linear` 在分块策略、scale 处理上的异同——这能巩固「Triton 分块 GEMM」的通用模式。
2. **追 MoE 的 kernel 落点**：u5-l3 讲了 `FusedMoE` 的 dispatch/gemm/combine 流水线，其底层就在 `kernels/cuda/fused_moe.py`、`w8a8_fused_moe.py`、`blocked_fp8_fused_moe.py`。带着 u5-l3 的三段流水线去读，会发现它们复用了本讲的「分组 tile + scale 融合」思想。
3. **向上回看调用方**：回到 `backends/cuda/qmodules.py` 与 `models/q_modules.py`，确认 `QRMSNorm.forward` / `QLinear.forward` 如何把 `rms_norm_dynamic_quant` 的 `(q, scale)` 输出喂给 `matmul_kernel_dynamic_quant`，闭合「积木 → backends Impl → kernels」整条链。
4. **设备扩展**：若你需要支持一个新设备，本讲给出了清晰边界——在 `kernels/<新设备>/` 下放同名函数实现，并在 `dispatcher.py` 的 `device_map` 里加一行映射即可，无需改动 cuda 实现与上层积木。
