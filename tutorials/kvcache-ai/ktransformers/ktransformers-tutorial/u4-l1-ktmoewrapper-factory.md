# KTMoEWrapper 工厂与后端分发

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `KTMoEWrapper` 为什么是一个「工厂类」而不是普通类，以及它用 `__new__` 做分发的原理。
- 说出 `INFERENCE_METHODS` 与 `SFT_METHODS` 两套方法集合分别对应什么场景、各包含哪些 method。
- 给出任意一个 `method`（例如 `MXFP4`、`LLAMAFILE`、`AMXBF16_SFT`），判断它会被分发到 `AMXMoEWrapper` / `NativeMoEWrapper` / `LlamafileMoEWrapper` / `GeneralMoEWrapper` / `AMXSFTMoEWrapper` 中的哪一个。
- 写脚本验证上述分发关系，并能解释非法 `method`、`mode` 与 `method` 不匹配时抛出的错误。

本讲是进入「Python 推理 API」的第一篇，只聚焦**工厂分发**这一件事；各后端内部的权重加载、缓冲区、算子细节留给后续讲义（u5、u8）。

## 2. 前置知识

在读本讲前，先建立下面几个直觉（不要求已掌握细节，但要能理解名词）：

- **MoE（Mixture of Experts，专家混合）**：一层里有多个「专家」（本质是若干个独立的 FFN），每个 token 只激活其中 top-k 个。MoE 层是 KTransformers 在 CPU 上加速的核心对象。
- **method（方法）**：在本项目里，`method` 是一个字符串（如 `"AMXINT4"`、`"FP8"`），它同时编码了两件事——**用哪种量化精度**（INT4/INT8/FP8/BF16/MXFP4…）以及**走哪条 CPU 算子路径**（AMX / AVX2 / 通用 kernel / llamafile）。后端类的选择就是看这个字符串。
- **mode（模式）**：`"inference"`（推理，只要前向）或 `"sft"`（监督微调，需要前向+反向+LoRA）。两条线共享底层 C++ 算子，但 Python 包装层完全不同，所以工厂要先按 `mode` 分流。
- **工厂模式（Factory）**：调用方只面对一个统一入口 `KTMoEWrapper(...)`，由这个入口内部决定真正实例化哪个子类。调用方不需要记住「我要 INT4 就得 import AMXMoEWrapper」。
- **`__new__` vs `__init__`**：`__init__` 是「初始化一个已经创建好的对象」，而 `__new__` 才是「真正决定创建什么类型的对象」。工厂类正是利用 `__new__` 可以**返回任意类型实例**这一特性来实现分发——`KTMoEWrapper(...)` 返回的根本不是 `KTMoEWrapper` 的实例，而是某个后端子类的实例。

如果你已经读过 u1-l2（仓库结构）和 u2-l3（运行时 CPU 变体加载），就具备了理解本讲所需的全部背景。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [kt-kernel/python/experts.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py) | 本讲的主角。定义工厂类 `KTMoEWrapper`、两套方法集合，以及两个私有工厂函数 `_create_inference_wrapper` / `_create_sft_wrapper`。 |
| [kt-kernel/python/\_\_init\_\_.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py) | 包入口。把 `KTMoEWrapper` 作为公开 API 导出，并对 SFT 类做惰性导入。 |
| [kt-kernel/python/experts_base.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts_base.py) | 后端公共基类 `BaseMoEWrapper`（推理）与 `_MoEBase`（CPUInfer 单例）。本讲只在「为什么返回的是 BaseMoEWrapper 子类」时提及。 |
| [kt-kernel/python/utils/amx.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/amx.py) | 两个推理后端：`AMXMoEWrapper`（AMXINT4/AMXINT8）与 `NativeMoEWrapper`（FP8/BF16/RAWINT4/MXFP4…）。 |
| [kt-kernel/python/utils/llamafile.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/llamafile.py) | 推理后端 `LlamafileMoEWrapper`（GGUF 权重）。 |
| [kt-kernel/python/utils/moe_kernel.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/moe_kernel.py) | 推理后端 `GeneralMoEWrapper`（MOE_INT4/MOE_INT8，通用矩阵库路径）。 |
| [kt-kernel/python/sft/amx.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/sft/amx.py) | SFT 后端 `AMXSFTMoEWrapper`——目前 `_create_sft_wrapper` 唯一会实例化的类。 |

> 说明：本讲引用的代码全部来自 `kt-kernel/python/`，行号基于当前 HEAD `cb9f47d`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**工厂接口**、**方法集合**、**后端选择**。它们正好对应工厂分发的三个阶段——「入口校验 → 合法方法范围 → 选中具体后端类」。

### 4.1 工厂接口：KTMoEWrapper 与 `__new__`

#### 4.1.1 概念说明

外部代码（包括 SGLang 集成、examples 脚本、SFT 训练器）要跑一个 MoE 层时，只需要这样写：

```python
from kt_kernel import KTMoEWrapper

wrapper = KTMoEWrapper(
    layer_idx=0, num_experts=8, num_experts_per_tok=2,
    hidden_size=4096, moe_intermediate_size=14336,
    gpu_experts_mask=mask,
    cpuinfer_threads=32, threadpool_count=2,
    weight_path="/path/to/weights",
    chunked_prefill_size=25600,
    method="AMXINT4",          # 关键：决定走哪个后端
    mode="inference",          # 关键：决定走推理还是 SFT
)
```

注意三点：

1. `KTMoEWrapper` 自己**几乎不含业务逻辑**，它只负责「看 `mode` 和 `method`，然后决定实例化谁」。
2. 真正返回的对象**不是** `KTMoEWrapper` 的实例，而是某个后端类（如 `AMXMoEWrapper`）的实例。也就是说 `type(wrapper).__name__` 会是 `"AMXMoEWrapper"` 而不是 `"KTMoEWrapper"`。
3. 这是典型的**工厂入口**设计：把「选哪个后端」的复杂判断集中在一处，调用方只面对一个名字。

#### 4.1.2 核心流程

`KTMoEWrapper(...)` 被调用时，Python 会先执行 `__new__`，其执行顺序如下（伪代码）：

```text
KTMoEWrapper.__new__(cls, mode, method, ...):
    1. 校验 mode：必须是 "inference" 或 "sft"，否则 ValueError
    2. 校验 method 与 mode 是否匹配：
       - mode=="inference" → method 必须在 INFERENCE_METHODS 中
       - mode=="sft"        → method 必须在 SFT_METHODS 中
       否则 ValueError
    3. 按 mode 分流：
       - "inference" → _create_inference_wrapper(...)
       - "sft"        → _create_sft_wrapper(...)
    4. （在工厂函数内部）按 method 选 backend_cls，实例化并返回
    # __new__ 直接把后端实例 return 出去，于是 KTMoEWrapper(...)
    # 拿到的就是这个后端实例
```

关键点：**校验全部发生在分发之前**。这意味着传一个非法 method 会立刻报错，而不会走到后续需要真实权重/C++ 扩展的地方。这一点在后面的实践中很有用。

#### 4.1.3 源码精读

工厂类的定义与文档字符串在这里，注意它只是一个普通 `class`，没有任何实例字段：

[kt-kernel/python/experts.py:70-118](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L70-L118) — `class KTMoEWrapper:` 的类定义，docstring 里给出了推理与 SFT 两种典型用法。

`__new__` 的签名收集了所有后端可能用到的参数（推理参数 + SFT 参数混在一起，由 `mode` 决定哪些生效）：

[kt-kernel/python/experts.py:120-156](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L120-L156) — `def __new__(...)` 的完整参数列表。注意默认值：`method="AMXINT4"`、`mode="inference"`，所以「什么都不指定」就是 AMX INT4 推理。

两道校验（mode 合法性、method 与 mode 匹配性）：

[kt-kernel/python/experts.py:192-207](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L192-L207) — 第 193-194 行校验 `mode`；第 197-202 行校验推理 method；第 203-207 行校验 SFT method。报错信息里用 `sorted(INFERENCE_METHODS)` 列出所有合法值，方便排错。

按 mode 分流到两个私有工厂函数：

[kt-kernel/python/experts.py:209-255](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L209-L255) — `if mode == "inference":` 调用 `_create_inference_wrapper(...)`，否则（经过校验后必然是 `"sft"`）调用 `_create_sft_wrapper(...)`。注意第 232-237 行：SFT 分支会**显式拒绝** `swiglu_limit != 0.0`（这是 V4-2604B 推理专属的 SwiGLU 截断，SFT 不实现），而不是默默丢掉参数。

此外，工厂类还把几个与缓冲区相关的静态方法**转发**到基类，让调用方只需认 `KTMoEWrapper` 一个名字：

[kt-kernel/python/experts.py:257-300](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L257-L300) — `set_capture_batch_sizes` / `get_capture_batch_sizes` / `clear_buffer_cache` / `clear_sft_buffer_cache` 都是 `@staticmethod`，内部委托给 `BaseMoEWrapper` 或 `KExpertsSFTBuffer`。这是「工厂即唯一入口」设计的一部分。

最后看一眼这个类是怎么被导出的：

[kt-kernel/python/\_\_init\_\_.py:53-54](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/__init__.py#L53-L54) — `from .experts import KTMoEWrapper`，于是 `from kt_kernel import KTMoEWrapper` 即可使用。SFT 的 `AMXSFTMoEWrapper` 走 `__getattr__` 惰性导入（第 56-63 行），未安装 SFT 依赖时返回 `None` 而不是报错。

#### 4.1.4 代码实践

**目标**：亲手验证「`KTMoEWrapper(...)` 返回的不是 `KTMoEWrapper` 实例」以及「校验发生在分发之前」。

**操作步骤**（前置：你已按 u2-l1 安装好可 import 的 `kt_kernel`）：

```python
# inspect_factory.py —— 仅做静态检查，不会真正加载权重
import inspect
from kt_kernel import KTMoEWrapper
from kt_kernel.experts import _create_inference_wrapper, _create_sft_wrapper

# 1) KTMoEWrapper 自身没有 __init__，分发完全靠 __new__
print("has __init__ :", "__init__" in KTMoEWrapper.__dict__)
print("has __new__  :", "__new__"  in KTMoEWrapper.__dict__)

# 2) 两个私有工厂函数确实存在，且签名里都带 method
print("_create_inference_wrapper params:",
      list(inspect.signature(_create_inference_wrapper).parameters)[:3], "...")
print("_create_sft_wrapper      params:",
      list(inspect.signature(_create_sft_wrapper).parameters)[:3], "...")

# 3) 校验在前：非法 mode 不需要任何权重就会立刻报错
try:
    KTMoEWrapper(layer_idx=0, num_experts=8, num_experts_per_tok=2,
                 hidden_size=64, moe_intermediate_size=128,
                 gpu_experts_mask=None, cpuinfer_threads=4, threadpool_count=1,
                 weight_path="/tmp/不存在", chunked_prefill_size=512,
                 method="AMXINT4", mode="not_a_mode")
except ValueError as e:
    print("非法 mode -> ValueError:", str(e)[:60], "...")
```

**需要观察的现象**：

1. `has __init__` 应为 `False`（类体里只定义了 `__new__` 和静态方法）。
2. `has __new__` 应为 `True`。
3. 第 3 步即使 `weight_path` 指向一个不存在的目录，也不会因为「找不到权重」报错——而是在 mode 校验处就抛出 `ValueError: Unknown mode: 'not_a_mode'...`。

**预期结果**：三步全部命中，证明「入口只做校验和分发，不碰真实资源」。

**待本地验证**：如果 `import kt_kernel` 失败（未安装或未编译扩展），请先回到 u2-l1/u2-l3 完成安装与 CPU 变体加载。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KTMoEWrapper` 用 `__new__` 而不是写一个普通的 `create(...)` 静态方法？

**参考答案**：用 `__new__` 可以让调用方写 `KTMoEWrapper(...)`（像构造对象一样自然），同时让「真正创建的对象类型」由内部决定。普通静态方法 `KTMoEWrapper.create(...)` 也能做到同样的事，但语义上不如「构造即分发」直观；这也是 PyTorch 等库里常见的工厂写法（构造函数返回子类实例）。

**练习 2**：第 3 步实践中，为什么 `weight_path="/tmp/不存在"` 不会触发 `FileNotFoundError`？

**参考答案**：因为 mode 校验（第 193-194 行）发生在分发与后端实例化之前。后端类（如 `LlamafileMoEWrapper`）里检查 `os.path.exists(weight_path)` 的代码还没机会执行，就已经因为 `mode` 非法而抛 `ValueError` 退出。

---

### 4.2 方法集合：INFERENCE_METHODS 与 SFT_METHODS

#### 4.2.1 概念说明

工厂要能判断「这个 method 合不合法」，就需要一份**合法名单**。本项目把名单分成两套：

- `INFERENCE_METHODS`：推理（`mode="inference"`）允许的 method，覆盖各种量化精度 × 算子路径。
- `SFT_METHODS`：微调（`mode="sft"`）允许的 method，目前全部是 AMX 系列的训练变体。

两套名单都是 `frozenset`（不可变集合），定义在模块顶层，意味着「合法 method」是编译期/导入期就固定的——你只能在这些值里挑。

#### 4.2.2 核心流程

method 名单的「语义」可以按下表理解（仅做归类，便于记忆；具体每个 method 的精度含义见后续 u5、u7 讲义）：

| method | 归属集合 | 大致含义 |
| --- | --- | --- |
| `AMXINT4` / `AMXINT8` | 推理 | AMX 指令集下的 INT4/INT8 量化专家 |
| `RAWINT4` | 推理 | 直接使用权重里已有的 INT4（无需再量化） |
| `FP8` / `FP8_PERCHANNEL` | 推理 | FP8 原生精度（per-tensor / per-channel） |
| `BF16` | 推理 | BF16 原生精度（CPU/GPU 共享同一份权重） |
| `GPTQ_INT4` | 推理 | GPTQ 格式 INT4 |
| `MXFP4` / `MXFP8` | 推理 | 微缩放浮点（E2M1 nibble / E4M3 字节 + ue8m0 组缩放） |
| `LLAMAFILE` | 推理 | GGUF 格式，走 llamafile 后端 |
| `MOE_INT4` / `MOE_INT8` | 推理 | 通用矩阵库（BLIS/KML）路径 |
| `AMXBF16_SFT` / `AMXINT8_SFT` / `AMXINT4_SFT` / … | SFT | AMX 训练方法（BF16/INT8/INT4/INT4_1/KGroup…） |
| `*_SkipLoRA`（如 `AMXBF16_SFT_SkipLoRA`） | SFT | 反向阶段跳过 LoRA 计算，只算 base weight grad_input |

校验流程就是一次集合查找：

```text
if mode == "inference":
    method in INFERENCE_METHODS ?  否 -> ValueError
else:  # "sft"
    method in SFT_METHODS ?        否 -> ValueError
```

#### 4.2.3 源码精读

两套方法集合的定义：

[kt-kernel/python/experts.py:34-49](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L34-L49) — `INFERENCE_METHODS`，共 12 个推理 method。注意第 43-44 行的注释解释了 `MXFP4`/`MXFP8` 分别对应哪类模型（DeepSeek-V4-Flash routed experts / MiniMax-M3-Preview）。

[kt-kernel/python/experts.py:51-67](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L51-L67) — `SFT_METHODS`，前 6 个是「完整 LoRA」变体，后 6 个是「SkipLoRA」变体。第 59 行注释说明 SkipLoRA 在反向阶段跳过全部 LoRA 计算、只算 base weight 的 grad_input。

校验逻辑（已在 4.1.3 引用过，这里聚焦集合查找）：

[kt-kernel/python/experts.py:197-207](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L197-L207) — `if method not in INFERENCE_METHODS` 与 `if method not in SFT_METHODS`，报错时用 `sorted(...)` 给出有序的合法值列表。

#### 4.2.4 代码实践

**目标**：枚举两套方法集合，并验证「mode 与 method 必须匹配」的交叉校验。

**操作步骤**：

```python
# inspect_methods.py
from kt_kernel.experts import INFERENCE_METHODS, SFT_METHODS
from kt_kernel import KTMoEWrapper

common = dict(layer_idx=0, num_experts=8, num_experts_per_tok=2,
              hidden_size=64, moe_intermediate_size=128,
              gpu_experts_mask=None, cpuinfer_threads=4, threadpool_count=1,
              weight_path="/tmp/fake", chunked_prefill_size=512)

print("INFERENCE_METHODS:", sorted(INFERENCE_METHODS))
print("SFT_METHODS      :", sorted(SFT_METHODS))
print("两者交集（应为空）:", sorted(INFERENCE_METHODS & SFT_METHODS))

# 交叉错配：把 SFT 方法塞进 inference 模式 —— 会在 method 校验处报错
for bad in [("AMXBF16_SFT", "inference"), ("AMXINT4", "sft")]:
    try:
        KTMoEWrapper(method=bad[0], mode=bad[1], **common)
    except ValueError as e:
        print(f"method={bad[0]!r:14} mode={bad[1]!r:10} -> ValueError（前 50 字）: {str(e)[:50]}")
```

**需要观察的现象**：

1. 两套集合打印出来后，`INFERENCE_METHODS & SFT_METHODS` 的交集为空——名字完全不重叠。
2. 把 `AMXBF16_SFT`（SFT 方法）放进 `mode="inference"` 会报 `ValueError`，提示它「not supported for inference mode」；把 `AMXINT4`（推理方法）放进 `mode="sft"` 同样报错。

**预期结果**：交集为空集合 `[]`；两条交叉错配均抛 `ValueError`。这两个错误都来自第 197-207 行的集合校验，**在**任何后端实例化**之前**，因此不需要真实权重。

**待本地验证**：不同版本下 method 名单可能增减，以你本地 `sorted(INFERENCE_METHODS)` 的实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把推理和 SFT 的 method 合并成一个大集合？

**参考答案**：因为同一个 method 字符串在不同 mode 下语义不同（推理只前向，SFT 要前向+反向+LoRA），且对应完全不同的后端类。分成两套集合可以在入口就拦截「用推理方法跑训练」这类错配，避免把错误的参数（如 `lora_rank`）传给推理后端。

**练习 2**：`AMXBF16_SFT` 和 `AMXBF16_SFT_SkipLoRA` 的区别会在哪一层体现？

**参考答案**：在反向（backward）阶段。完整版会计算 LoRA 部分的梯度，SkipLoRA 版只计算 base weight 的 `grad_input`、跳过 LoRA 计算（见第 59 行注释）。这些差异由 SFT 后端 `AMXSFTMoEWrapper` 与自定义 autograd（u10-l2）处理，本讲的工厂只负责把它分发出去。

---

### 4.3 后端选择：method → backend_cls 映射

#### 4.3.1 概念说明

校验通过后，工厂函数 `_create_inference_wrapper` / `_create_sft_wrapper` 才真正「按 method 选后端类」。推理侧的映射规则是**多对一**的：多个 method 共用同一个后端类。映射关系如下（这是本讲最重要的一张表）：

| method | backend_cls | 定义位置 |
| --- | --- | --- |
| `AMXINT4`, `AMXINT8` | `AMXMoEWrapper` | utils/amx.py:222 |
| `RAWINT4`, `FP8`, `BF16`, `FP8_PERCHANNEL`, `GPTQ_INT4`, `MXFP4`, `MXFP8` | `NativeMoEWrapper` | utils/amx.py:523 |
| `LLAMAFILE` | `LlamafileMoEWrapper` | utils/llamafile.py:21 |
| `MOE_INT4`, `MOE_INT8` | `GeneralMoEWrapper` | utils/moe_kernel.py:29 |
| 任意 SFT method | `AMXSFTMoEWrapper` | sft/amx.py:57 |

注意：SFT 目前**只有** `AMXSFTMoEWrapper` 一个后端（见 `_create_sft_wrapper` 里的注释「Currently only AMX SFT methods are supported」），所有 `*_SFT` 方法都进这一个类，由它内部再按 method 区分训练精度。

#### 4.3.2 核心流程

推理后端选择是一段朴素的 `if/elif` 链（不是字典查找），流程如下：

```text
_create_inference_wrapper(method, ...):
    if   method in [AMXINT4, AMXINT8]:         backend_cls = AMXMoEWrapper
    elif method in [RAWINT4,FP8,BF16,...]:     backend_cls = NativeMoEWrapper
    elif method == LLAMAFILE:                  backend_cls = LlamafileMoEWrapper
    elif method in [MOE_INT4, MOE_INT8]:       backend_cls = GeneralMoEWrapper
    else: raise NotImplementedError   # 理论上走不到（前面已校验）

    # swiglu_limit 只在 MXFP4/MXFP8 路径有意义，需严格按 method 判定
    extra_kwargs = {}
    if method in (MXFP4, MXFP8):               extra_kwargs = {swiglu_limit, swiglu_alpha}
    elif swiglu_limit != 0.0:                  raise ValueError   # 防止脏环境变量污染

    return backend_cls(method=method, ..., **extra_kwargs)
```

这里有一个**容易踩坑的设计点**：`swiglu_limit`（V4-2604B 推理用的 SwiGLU 截断阈值）只在 `MXFP4`/`MXFP8` 两个 method 下生效。代码**故意按 `method` 判定**而不是按「后端类是不是 `NativeMoEWrapper`」判定——因为 `NativeMoEWrapper` 同时服务 RAWINT4/FP8/BF16 等多个 method，若按后端类判定，一个残留的环境变量 `SGLANG_DSV4_2604_SUBMODE=2604B` 就会把 `10.0` 错误地灌进非 MXFP4 的后端，导致 `act_fn` 把 gate/up 默默截断到 ±10 而无任何告警。源码注释里把这段背景标为「Origin: kt-sglang 耦合」。

#### 4.3.3 源码精读

后端类的导入（注意是按需从各 utils 子模块导入）：

[kt-kernel/python/experts.py:27-30](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L27-L30) — 导入四个推理后端：`AMXMoEWrapper`、`NativeMoEWrapper`、`LlamafileMoEWrapper`、`GeneralMoEWrapper`。

推理后端选择的 `if/elif` 链：

[kt-kernel/python/experts.py:335-346](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L335-L346) — 按 method 选 `backend_cls`。最后的 `else` 抛 `NotImplementedError`，注释说明「由于 `__new__` 已做校验，理论走不到这里」，这是防御性编程。

`swiglu_limit` 的严格 method 判定（含背景注释）：

[kt-kernel/python/experts.py:349-366](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L349-L366) — 第 356-358 行：仅当 `method in ("MXFP4","MXFP8")` 时才把 `swiglu_limit`/`swiglu_alpha` 放进 `extra_kwargs`；第 359-366 行：否则若 `swiglu_limit != 0.0` 直接抛 `ValueError`，并提示这通常是环境里残留了 `SGLANG_DSV4_2604_SUBMODE=2604B`。

实例化后端并返回：

[kt-kernel/python/experts.py:367-383](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L367-L383) — `return backend_cls(method=method, ..., **extra_kwargs)`。这一步才真正进入后端 `__init__`（开始加载权重、创建 CPUInfer 等），是「重活」开始的地方。

SFT 后端选择（目前唯一路径）：

[kt-kernel/python/experts.py:386-433](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/experts.py#L386-L433) — `_create_sft_wrapper` 内部 `from .sft.amx import AMXSFTMoEWrapper`（第 413 行，函数内导入，避免没装 SFT 依赖时整个 experts 模块导入失败），然后直接 `return AMXSFTMoEWrapper(...)`。第 415 行注释「Currently only AMX SFT methods are supported」点明了当前限制。

四个推理后端类的定义位置（便于跳转阅读）：

- [kt-kernel/python/utils/amx.py:222](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/amx.py#L222) — `class AMXMoEWrapper(BaseMoEWrapper):`，服务 AMXINT4/AMXINT8。
- [kt-kernel/python/utils/amx.py:523](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/amx.py#L523) — `class NativeMoEWrapper(BaseMoEWrapper):`，服务 RAWINT4/FP8/BF16/FP8_PERCHANNEL/GPTQ_INT4/MXFP4/MXFP8。
- [kt-kernel/python/utils/llamafile.py:21](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/llamafile.py#L21) — `class LlamafileMoEWrapper(BaseMoEWrapper):`，服务 LLAMAFILE（GGUF）。
- [kt-kernel/python/utils/moe_kernel.py:29](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/utils/moe_kernel.py#L29) — `class GeneralMoEWrapper(BaseMoEWrapper):`，服务 MOE_INT4/MOE_INT8（通用矩阵库路径）。

SFT 后端：

- [kt-kernel/python/sft/amx.py:57](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/sft/amx.py#L57) — `class AMXSFTMoEWrapper(BaseSFTMoEWrapper):`，所有 `*_SFT` 方法的唯一落点。

#### 4.3.4 代码实践

**目标**：在不加载任何真实权重的前提下，遍历所有推理 method，**观察它被分发到哪个后端类**，并验证 `swiglu_limit` 的严格判定。

**思路**：直接构造 `KTMoEWrapper` 会进入后端 `__init__` 去加载权重，成本太高。但 `_create_inference_wrapper` 里 `backend_cls = AMXMoEWrapper` 这种写法是**在调用时按模块全局名查找**的——所以我们可以把 `kt_kernel.experts` 模块里的四个后端类临时换成「只记录 method、不干重活」的替身，从而拦截分发、看到真正的 `backend_cls`。

**操作步骤**：

```python
# observe_dispatch.py —— 拦截后端实例化，只观察分发结果
import kt_kernel.experts as ex
from kt_kernel import KTMoEWrapper

# 用轻量替身替换四个真实推理后端：__init__ 不加载权重，只记 method
class _Stub:
    def __init__(self, **kwargs):
        self.method = kwargs.get("method")
    def __repr__(self):
        return f"<{type(self).__name__} method={self.method!r}>"

ex.AMXMoEWrapper       = type("AMXMoEWrapper",       (_Stub,), {})
ex.NativeMoEWrapper    = type("NativeMoEWrapper",    (_Stub,), {})
ex.LlamafileMoEWrapper = type("LlamafileMoEWrapper", (_Stub,), {})
ex.GeneralMoEWrapper   = type("GeneralMoEWrapper",   (_Stub,), {})

common = dict(layer_idx=0, num_experts=8, num_experts_per_tok=2,
              hidden_size=64, moe_intermediate_size=128,
              gpu_experts_mask=None, cpuinfer_threads=4, threadpool_count=1,
              weight_path="/tmp/fake", chunked_prefill_size=512)

print("method         -> backend_cls")
print("-" * 40)
for m in ["AMXINT4", "AMXINT8", "RAWINT4", "FP8", "BF16", "FP8_PERCHANNEL",
          "GPTQ_INT4", "MXFP4", "MXFP8", "LLAMAFILE", "MOE_INT4", "MOE_INT8"]:
    w = KTMoEWrapper(method=m, mode="inference", **common)
    print(f"{m:14s} -> {type(w).__name__}")

# 验证 swiglu_limit 严格判定：非 MXFP4 路径传非零值应报错
print("-" * 40)
try:
    KTMoEWrapper(method="BF16", mode="inference", swiglu_limit=10.0, **common)
except ValueError as e:
    print("BF16 + swiglu_limit=10.0 -> ValueError（符合预期）")
```

**需要观察的现象**：分发表应当与本讲 4.3.1 的表格完全一致，例如：

```text
method         -> backend_cls
----------------------------------------
AMXINT4        -> AMXMoEWrapper
AMXINT8        -> AMXMoEWrapper
RAWINT4        -> NativeMoEWrapper
FP8            -> NativeMoEWrapper
BF16           -> NativeMoEWrapper
FP8_PERCHANNEL -> NativeMoEWrapper
GPTQ_INT4      -> NativeMoEWrapper
MXFP4          -> NativeMoEWrapper
MXFP8          -> NativeMoEWrapper
LLAMAFILE      -> LlamafileMoEWrapper
MOE_INT4       -> GeneralMoEWrapper
MOE_INT8       -> GeneralMoEWrapper
----------------------------------------
BF16 + swiglu_limit=10.0 -> ValueError（符合预期）
```

**预期结果**：12 个推理 method 各归其位；`BF16 + swiglu_limit=10.0` 抛 `ValueError`（来自第 359-366 行）。

**为什么这个实践有效**：`__new__` 返回的是替身实例而非 `KTMoEWrapper` 实例，Python 不会再去调 `KTMoEWrapper.__init__`（而且它本来也没有 `__init__`），所以整个过程不会触碰真实权重、CPUInfer 或 C++ 扩展——纯 Python 的分发逻辑就被完整观察到了。

**待本地验证**：如果替身替换后仍报后端相关错误，请确认你替换的是 `kt_kernel.experts` 模块命名空间里的名字（即 `ex.AMXMoEWrapper = ...`），而不是 `kt_kernel.utils.amx` 里的原定义——因为 `_create_inference_wrapper` 引用的是 `experts` 模块里的全局名。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RAWINT4` 和 `MXFP4` 都分到 `NativeMoEWrapper`，而 `AMXINT4` 却单独分到 `AMXMoEWrapper`？

**参考答案**：`AMXMoEWrapper` 专门处理需要 AMX 指令集的 INT4/INT8 量化（项目自己做的量化，权重布局对齐 AMX tile）；而 `NativeMoEWrapper` 处理的是「原生精度/压缩格式」（FP8/BF16/RAWINT4/MXFP4…），这些方法往往能让 CPU 与 GPU 共用同一份权重，省去额外量化。两类后端的权重格式与算子路径差异较大，故拆成两个类。具体细节见 u5-l1 与 u7-l3。

**练习 2**：如果把 `swiglu_limit` 的判定从「按 method」改成「按 `backend_cls is NativeMoEWrapper`」，会发生什么隐患？

**参考答案**：`NativeMoEWrapper` 同时服务 RAWINT4/FP8/BF16/GPTQ_INT4 等多个 method，若按后端类判定，一个残留的环境变量（如 `SGLANG_DSV4_2604_SUBMODE=2604B`）产生的 `swiglu_limit=10.0` 会被静默地传给非 MXFP4 的后端，使 `act_fn` 把 gate/up 错误截断到 ±10，精度受损却无告警。这正是第 349-366 行注释强调要严格按 `method` 判定的原因。

---

## 5. 综合实践

把三个最小模块串起来，完成一个「KTMoEWrapper 分发探测器」小任务。

**任务**：写一个函数 `probe(method, mode)`，返回该方法会被分发到的后端类名（字符串），并能在 method/mode 非法时返回描述性的错误信息而不抛异常。然后：

1. 用它打印一张「method × mode → 后端类」的完整对照表（覆盖全部 `INFERENCE_METHODS` 与 `SFT_METHODS`）。
2. 单独验证 SFT 路径：因为 `_create_sft_wrapper` 里是**函数内导入** `AMXSFTMoEWrapper`，你的探测器要么也走函数内导入，要么对 SFT 直接返回 `"AMXSFTMoEWrapper（唯一）"` 并说明理由。
3. 故意构造 3 类错误并确认它们的报错来源不同：
   - 非法 `mode`（应来自第 193-194 行）；
   - `mode` 与 `method` 不匹配（应来自第 197-207 行）；
   - 推理 method 合法但 `swiglu_limit` 与 method 冲突（应来自第 359-366 行）。

**参考实现骨架**（基于 4.3.4 的替身拦截法）：

```python
import kt_kernel.experts as ex
from kt_kernel import KTMoEWrapper
from kt_kernel.experts import INFERENCE_METHODS, SFT_METHODS

class _Stub:
    def __init__(self, **kw): self.method = kw.get("method")

ex.AMXMoEWrapper       = type("AMXMoEWrapper",       (_Stub,), {})
ex.NativeMoEWrapper    = type("NativeMoEWrapper",    (_Stub,), {})
ex.LlamafileMoEWrapper = type("LlamafileMoEWrapper", (_Stub,), {})
ex.GeneralMoEWrapper   = type("GeneralMoEWrapper",   (_Stub,), {})
# SFT 后端也换成替身，避免真正 import SFT 依赖
class _SftStub:
    def __init__(self, **kw): self.method = kw.get("method")
import kt_kernel.sft.amx as _sft_amx  # 若 SFT 未安装可改为直接返回字符串
_sft_amx.AMXSFTMoEWrapper = type("AMXSFTMoEWrapper", (_SftStub,), {})

COMMON = dict(layer_idx=0, num_experts=8, num_experts_per_tok=2,
              hidden_size=64, moe_intermediate_size=128,
              gpu_experts_mask=None, cpuinfer_threads=4, threadpool_count=1,
              weight_path="/tmp/fake", chunked_prefill_size=512,
              num_gpu_experts=0, lora_rank=16, lora_alpha=32.0)

def probe(method, mode):
    try:
        w = KTMoEWrapper(method=method, mode=mode, **COMMON)
        return type(w).__name__
    except ValueError as e:
        return f"ValueError: {str(e)[:40]}..."

print("== 推理 ==")
for m in sorted(INFERENCE_METHODS):
    print(f"{m:16s} -> {probe(m, 'inference')}")
print("== SFT ==")
for m in sorted(SFT_METHODS):
    print(f"{m:22s} -> {probe(m, 'sft')}")

print("== 错误来源验证 ==")
print("非法 mode        :", probe("AMXINT4", "bogus"))          # L193-194
print("mode/method 不匹配:", probe("AMXBF16_SFT", "inference")) # L197-202
print("swiglu 冲突      :", probe2_swiglu())                    # 见下方说明
```

> `probe2_swiglu()` 需单独写：传 `method="BF16", mode="inference", swiglu_limit=10.0`，捕获并返回 `ValueError`。注意它**必须**走到第 359-366 行，所以替身替换不影响它（该校验在实例化之前）。

**预期结果**：

- 推理表 12 行，分到 `AMXMoEWrapper` / `NativeMoEWrapper` / `LlamafileMoEWrapper` / `GeneralMoEWrapper` 四类。
- SFT 表 12 行，**全部**分到 `AMXSFTMoEWrapper`。
- 三类错误分别命中不同行号区间的 `ValueError`，证明「入口校验 → method 校验 → swiglu 校验」是层层递进的。

**待本地验证**：SFT 后端替身替换那一步依赖 `kt_kernel.sft` 可导入；若你的环境没装 SFT 依赖（`has_sft_support()` 为假），可跳过 SFT 表，并在报告里注明「SFT 未安装，跳过」。

## 6. 本讲小结

- `KTMoEWrapper` 是一个**工厂类**：它用 `__new__` 完成校验与分发，自身不含业务逻辑，`KTMoEWrapper(...)` 返回的实际上是某个后端子类的实例（`type(wrapper).__name__` 不是 `KTMoEWrapper`）。
- 合法 method 分成两套不可变集合：`INFERENCE_METHODS`（12 个推理方法）与 `SFT_METHODS`（12 个训练方法，含 6 个 SkipLoRA 变体），二者**无交集**，且 `mode` 与 `method` 必须匹配，否则在分发前就抛 `ValueError`。
- 推理侧 method 到后端类是**多对一**映射：`AMXINT4/AMXINT8→AMXMoEWrapper`、`RAWINT4/FP8/BF16/…/MXFP8→NativeMoEWrapper`、`LLAMAFILE→LlamafileMoEWrapper`、`MOE_INT4/MOE_INT8→GeneralMoEWrapper`。
- SFT 侧目前**只有** `AMXSFTMoEWrapper` 一个后端，所有 `*_SFT` 方法都进这一个类（函数内导入，避免无 SFT 依赖时影响主模块）。
- `swiglu_limit` 是一个**易踩坑参数**：只在 `MXFP4`/`MXFP8` 下生效，代码故意按 `method`（而非按后端类）严格判定，以防止脏环境变量把截断阈值灌进错误后端。
- 工厂类还把 `set_capture_batch_sizes` 等缓冲区静态方法转发到基类，让外部只需认 `KTMoEWrapper` 一个入口。

## 7. 下一步学习建议

- **u4-l2 BaseMoEWrapper 与 CPUInfer 引擎**：本讲只说「返回的是 `BaseMoEWrapper` 子类」，下一讲就钻进 `BaseMoEWrapper` 与 `_MoEBase`，看后端实例化时如何创建 `CPUInfer` 单例、配置 NUMA 子池、做基础参数校验。
- **u4-l4 GPU 专家掩码与放置**：本讲的 `gpu_experts_mask` 参数是怎么来的？下一组讲义会讲 `generate_gpu_experts_masks` 如何按激活频率选 top-k 专家放 GPU。
- **u5 系列后端实现**：本讲只说「`AMXMoEWrapper` 服务 AMXINT4/AMXINT8」，u5-l1 会展开 `utils/amx.py` 里 `NativeMoEWrapper` 与 `AMXMoEWrapper` 的回退选择逻辑（AMX/AVX2/AVX-VNNI），u5-l2/u5-l3 讲 llamafile 与通用 kernel 后端。
- **u8-l1 pybind 绑定层**：想理解「后端类内部调用的 `AMXInt4_MOE`/`MOEConfig` 等 C++ 类从哪来」，可以跳到 u8-l1 看 `ext_bindings.cpp` 如何把它们暴露给 Python。
- **阅读建议**：先把本讲的「method → 后端类」映射表记牢，再带着这张表去读 u5——你会发现 u5 的每个后端类正好对应这里的一组 method，学习曲线会顺很多。
