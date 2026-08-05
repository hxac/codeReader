# Worker Patch 实战解析

## 1. 本讲目标

本讲是 Patch 机制系列的第三篇，承接 [u3-l1](u3-l1-patch-overview.md) 讲的「两阶段补丁」总纲与 [u3-l2](u3-l2-platform-patches.md) 讲的 Platform Patch。

学完本讲，你应当能够：

1. 说清楚「worker 补丁」在什么时候、由谁、以什么顺序被加载，并理解它为什么必须在每个 worker 子进程里重新打一遍。
2. 识别 vllm-ascend 里 worker 补丁的四种典型改写手法：**替换方法**、**替换模块属性/函数**、**替换整个类**、**复用 platform 补丁**。
3. 读懂 `patch_deepseek_v2.py`、`patch_eagle3_init.py` 这类**模型专属前向/初始化补丁**做了什么、为什么需要做。
4. 读懂 `patch_triton.py`、`patch_rejection_sampler.py` 这类 **NPU 算子替换补丁**，以及 `patch_cudagraph.py`、`patch_v2/` 系列在**图模式**下的作用。
5. 能够挑出任意一个 worker patch，说清「它在 `worker.__init__` 的哪一步被导入、替换了上游哪个方法」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前序讲义）：

- **Monkey-patch（猴子补丁）**：在运行时把一个模块/类里已存在的函数或属性替换成自己的实现，**不改动上游源码**。核心三步是：捕获原对象 → 定义替换实现 → 把替换实现重绑定回原模块。
- **两阶段补丁**：vllm-ascend 通过总闸 `adapt_patch(is_global_patch)` 分两个阶段打补丁。`is_global_patch=True` 触发 **platform 补丁**（在引擎核心 EngineCore 子进程生效，影响调度/配置等全局逻辑）；`is_global_patch=False` 触发 **worker 补丁**（本讲主角，在每个 worker 子进程生效，影响模型前向、算子、图模式）。
- **import 即打补丁**：vllm-ascend 的每个补丁都是一个普通 Python 模块，模块顶层语句（`Cls.method = new_func`）就是打补丁的动作。因此 `import` 这个模块的瞬间，补丁就生效了。
- **spawn 子进程不继承父进程补丁**：worker 子进程是用 `spawn` 拉起的全新解释器，父进程里打过的补丁不会带过来，所以 worker 必须自己再打一遍。

> 一句话区分：platform 补丁管「全局」，worker 补丁管「单卡前向」。本讲只讲后者。

## 3. 本讲源码地图

本讲涉及的文件都在 `vllm_ascend/patch/worker/` 目录下，这是 worker 补丁的「大本营」。

| 文件 | 作用 |
| --- | --- |
| `vllm_ascend/patch/worker/__init__.py` | worker 补丁的**总入口**。它按顺序 `import` 所有子补丁模块，import 即打补丁。 |
| `vllm_ascend/patch/worker/patch_deepseek_v2.py` | 模型专属补丁：重写 `DeepseekV2MLAAttention.__init__` 与 `DeepseekV2Model.forward`，处理 GLM-5.x 的稀疏 Indexer 权重布局与辅助隐状态收集。 |
| `vllm_ascend/patch/worker/patch_eagle3_init.py` | 模型专属补丁：重写 Eagle3 草稿模型的 `__init__`，修正流水线并行（PP）下的层号计算。 |
| `vllm_ascend/patch/worker/patch_triton.py` | 算子替换补丁：把上游 vLLM 里跑得不好或不支持的 Triton/FLA/Mamba 算子替换成 vllm-ascend 的 NPU 实现；并给 Triton 补上缺失的 `next_power_of_2`。 |
| `vllm_ascend/patch/worker/patch_rejection_sampler.py` | 算子替换补丁：把投机解码验证阶段的拒绝采样函数换成 NPU 版本。 |
| `vllm_ascend/patch/worker/patch_cudagraph.py` | 图模式补丁：重写 `CudagraphDispatcher._create_padded_batch_descriptor`，让上游的 FULL 图模式在 NPU 上可用。 |
| `vllm_ascend/patch/worker/patch_v2/` 子目录 | 面向 v2 model runner 的补丁集：替换 `BlockTables`、`InputBatch`、`ModelState`、投机解码的图管理器等。 |
| `vllm_ascend/worker/worker.py` | `NPUWorker`。在它的 `__init__` 里调用 `adapt_patch()` 触发本讲所有 worker 补丁。 |
| `vllm_ascend/utils.py` | 提供 `adapt_patch()` 这个总闸函数与 `is_310p()` 硬件判断。 |

此外，`vllm_ascend/patch/__init__.py` 顶部有一大段注释，把**每一个**补丁的「补了什么 / 为什么 / 怎么补 / 相关 PR / 未来计划」记录成登记簿。这是理解任意补丁的「说明书」。

## 4. 核心概念与源码讲解

### 4.1 Worker 补丁的总入口与加载时机

#### 4.1.1 概念说明

回忆两阶段补丁：`adapt_patch(is_global_patch=False)` 是 worker 补丁的开关。它本身只有两行，关键在于「import 一个包」会触发该包 `__init__.py` 的执行。

#### 4.1.2 核心流程

worker 补丁的加载链路是这样的：

```text
NPUWorker.__init__（每个 worker 子进程）
   │
   ├─ from vllm_ascend.utils import adapt_patch
   ├─ adapt_patch()                         # 注意：不传参 → is_global_patch=False
   │      └─ from vllm_ascend.patch import worker   # 触发 worker/__init__.py 执行
   │             └─ 逐条 import 各 patch_xxx 模块   # 每条 import 即打一个补丁
   │
   ├─ from vllm_ascend import ops           # 注册自定义算子
   └─ ... 继续初始化设备、分布式、模型 ...
```

注意一个关键点：`adapt_patch()` 必须在 **worker 真正使用上游被补丁的符号之前**执行。好在它被放在 `NPUWorker.__init__` 的最前面（第 108–110 行），早于模型加载与前向，所以时机是安全的。

#### 4.1.3 源码精读

总闸函数 `adapt_patch` 极其简洁，它的「魔法」全靠 import 的副作用：

```python
# vllm_ascend/utils.py
def adapt_patch(is_global_patch: bool = False):
    if is_global_patch:
        from vllm_ascend.patch import platform  # noqa: F401
    else:
        from vllm_ascend.patch import worker  # noqa: F401
```

完整代码见 [vllm_ascend/utils.py:533-537](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L533-L537)。`is_global_patch` 默认是 `False`，所以 worker 调用时走 `else` 分支，`import worker` 触发下面这个总入口。

`NPUWorker.__init__` 里调用它的位置，就在「register patch for vllm」注释下方：

```python
# vllm_ascend/worker/worker.py  （NPUWorker.__init__ 内）
# register patch for vllm
from vllm_ascend.utils import adapt_patch

adapt_patch()

# Register ops when worker init.
from vllm_ascend import ops
```

见 [vllm_ascend/worker/worker.py:107-113](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L107-L113)。

下面是 worker 补丁总入口 `patch/worker/__init__.py` 的骨架。它分四组，**import 的顺序就是补丁生效的顺序**：

```python
# vllm_ascend/patch/worker/__init__.py
from vllm.triton_utils import HAS_TRITON
from vllm_ascend.utils import is_310p

# 第 1 组：只有装了 Triton 才打的补丁
if HAS_TRITON:
    import vllm_ascend.patch.worker.patch_triton
    import vllm_ascend.patch.worker.patch_v2.patch_triton  # noqa

# 第 2 组：无条件补丁（所有硬件）
import vllm_ascend.patch.worker.patch_process_weights_after_loading  # noqa
import vllm_ascend.patch.worker.patch_distributed  # noqa
import vllm_ascend.patch.worker.patch_minimax_m2  # noqa
# ... 省略若干 ...

# 第 3 组：硬件分支——310P 与非 310P 走不同补丁
if not is_310p():
    import vllm_ascend.patch.worker.patch_qwen3_5  # noqa
    import vllm_ascend.patch.worker.patch_qwen3_dflash  # noqa
    import vllm_ascend.patch.worker.patch_qwen3vl  # noqa
else:
    import vllm_ascend.patch.worker.patch_idex_310  # noqa
import vllm_ascend.patch.worker.patch_rejection_sampler  # noqa

# 第 4 组：容错导入（CPU-only 环境没有 torchair，跳过）
try:  # noqa: SIM105
    import vllm_ascend.patch.worker.patch_npugraph_ex_triton  # noqa
except ImportError:
    pass

import vllm_ascend.patch.worker.patch_kimi_k25  # noqa
# ... patch_eagle3_init / patch_cudagraph / patch_deepseek_v2 ...
```

完整入口见 [vllm_ascend/patch/worker/__init__.py:18-75](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/__init__.py#L18-L75)。

这四组体现了三个值得记住的设计要点：

1. **按依赖能力 gating**：`HAS_TRITON`、`is_310p()` 这类运行期判断决定打哪些补丁，避免在不支持的环境里崩溃。`is_310p()` 的实现就是比较设备类型：

   ```python
   # vllm_ascend/utils.py
   def is_310p():
       return get_ascend_device_type() == AscendDeviceType._310P
   ```

   见 [vllm_ascend/utils.py:140-141](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L140-L141)。

2. **容错导入**：`torchair`/`npugraph_ex` 只在真 NPU 上可用，UT 跑在纯 CPU 环境没有它，所以用 `try/except ImportError` 包住，缺失就静默跳过。这段意图在源码注释里写得很明白：让 CPU-only 环境（如 UT runner）也能 import 这个模块而不崩。

3. **顺序即依赖**：有些补丁必须在另一些之前打。例如 `patch_triton` 排在最前，是因为后续模型/算子补丁可能依赖被它替换后的算子。

#### 4.1.4 代码实践

> **实践目标**：亲手验证「import 即打补丁」与「顺序即依赖」。

**操作步骤**：

1. 打开 [vllm_ascend/patch/worker/__init__.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/__init__.py)。
2. 数一数：在 `is_310p() == False`（即非 310P，主力 A2/A3 卡）的常规情况下，从第 22 行到第 75 行一共会 `import` 多少个补丁模块？（含 `patch_v2/` 子目录里的）
3. 找到 `patch_cudagraph` 在第 53 行，`patch_deepseek_v2` 在第 55 行。注意它们都在「第 4 组」容错块之后、`patch_v2` 系列之前。
4. 思考：如果把第 53 行 `import ...patch_cudagraph` 整行删掉，会发生什么？（提示：上游 `CudagraphDispatcher._create_padded_batch_descriptor` 不会被替换，FULL 图模式会报错。）

**需要观察的现象**：你应当能列出常规情况下约 **30 个**左右的 import 语句，并且意识到：**每删一行 import，就等于关掉一个补丁**。这也是排查「某个 NPU 行为异常」时的重要思路——去这个文件找对应补丁是否还在。

**预期结果**：能口述「worker 补丁 = `NPUWorker.__init__` 里调一次 `adapt_patch()` → 触发 `worker/__init__.py` 按序 import 各补丁模块 → import 即替换上游符号」这条链路。

（本实践为源码阅读型，无需 NPU。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 worker 补丁不能像 platform 补丁那样「整个进程只打一次」就够了？

**参考答案**：因为 worker 是用 `spawn` 拉起的全新 Python 解释器，父进程（引擎核心）里打过的补丁在子进程里不存在。每个 worker 子进程都得自己执行一遍 `adapt_patch()`，才能让本卡前向用到的上游符号被替换。

**练习 2**：`adapt_patch()`（不传参）和 `adapt_patch(is_global_patch=True)` 分别会 import 哪个包？

**参考答案**：前者 `is_global_patch` 默认 `False`，`import vllm_ascend.patch.worker`，触发 worker 补丁；后者 `import vllm_ascend.patch.platform`，触发 platform 补丁。

---

### 4.2 模型专属前向/初始化补丁

#### 4.2.1 概念说明

很多上游模型类（如 DeepSeek-V2、Eagle3 草稿模型）的 `__init__` 或 `forward` 里嵌入了 CUDA 专属假设，或者它们的权重布局与 vllm-ascend 要支持的某些 checkpoint 不完全一致。这时 vllm-ascend 选择**把整个方法换掉**，而不是去上游提 PR 慢慢等合并。

这类补丁的典型形态是：

```python
def _patched_xxx(self, ...):   # 在原类之外定义新方法
    ...
    nn.Module.__init__(self)   # 注意：不能写 super().__init__()
    ...

SomeClass.__init__ = _patched_xxx   # 把新方法绑回原类
```

> **关键陷阱**：新方法定义在原类之外，`self` 只是个普通参数，类还没真正创建。所以**不能写 `super().__init__()`**，必须显式调用基类构造，例如 `nn.Module.__init__(self)`。源码注释里专门提醒了这一点。

#### 4.2.2 核心流程

以 `patch_deepseek_v2.py` 为例，它做两件事：

1. **重写 `DeepseekV2MLAAttention.__init__`**：处理 GLM-5.x 的稀疏 Indexer 权重布局。
   - GLM-5.2 的 checkpoint 在「共享 Indexer」层上**省略**了 Indexer 权重；而 GLM-5.1 的 IndexCache override 只是跳过 top-k 计算、仍保留每层 Indexer 权重。两者不能混为一谈，否则权重加载会崩。
   - 补丁的判定逻辑：只有当某层**既**跳过 top-k **又**被 `indexer_types` 显式标记为 `shared` 时，才跳过 Indexer 的构造；MTP 层永远保留完整 Indexer。

2. **重写 `DeepseekV2Model.forward`**：在跨卡（TP/PP）场景下正确收集辅助隐状态（aux hidden states，用于 Eagle3 投机解码），必要时做 `tensor_model_parallel_all_gather`。

而 `patch_eagle3_init.py` 重写 Eagle3 草稿模型的 `__init__`，修正流水线并行下的层号：

- 上游用 `get_num_layers(parallel_config)`，在 PP>1 时返回的是**单个 PP stage 的层数**，导致草稿模型用错误的层号去拼参数名前缀（如 `model.layers.<start_layer_id + i>`），与 checkpoint 对不上，权重加载失败。
- 补丁改用 `get_total_num_hidden_layers()`，取**全局**总层数，与 checkpoint 的全局层号对齐。

#### 4.2.3 源码精读

先看 `patch_eagle3_init.py` 的核心修正——把 `target_layer_num` 从「单 stage 层数」改成「全局总层数」：

```python
# vllm_ascend/patch/worker/patch_eagle3_init.py
def _patched_eagle3_llama_init(self, *, vllm_config, prefix: str = ""):
    nn.Module.__init__(self)                 # 不能用 super().__init__()
    self.config = vllm_config.speculative_config.draft_model_config.hf_config
    ...
    # 关键修正：用「全局总层数」而非「单 PP stage 层数」
    target_layer_num = vllm_config.model_config.get_total_num_hidden_layers()
    self.config.target_layer_count = target_layer_num
    self.model = LlamaModel(vllm_config=vllm_config, prefix="model",
                            start_layer_id=target_layer_num)
    ...

Eagle3LlamaForCausalLM.__init__ = _patched_eagle3_llama_init
Eagle3DeepseekV2ForCausalLM.__init__ = _patched_eagle3_deepseek_v2_init
```

见 [vllm_ascend/patch/worker/patch_eagle3_init.py:59-124](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_eagle3_init.py#L59-L124)。注意末尾两条赋值就是把新方法绑回上游类，文件最后还打了一条 `logger.info` 记录补丁已生效。

再看 `patch_deepseek_v2.py`。判定是否跳过 Indexer 构造的辅助函数 `_should_skip_indexer_init`，只有同时满足「跳过 top-k」且「该层被标记为 shared」才返回 `True`：

```python
# vllm_ascend/patch/worker/patch_deepseek_v2.py
def _should_skip_indexer_init(config, prefix, skip_topk) -> bool:
    if not skip_topk:
        return False
    ...
    indexer_types = getattr(config, "indexer_types", None)
    indexer_type = (indexer_types[layer_id]
                    if indexer_types is not None and layer_id < len(indexer_types)
                    else None)
    return isinstance(indexer_type, str) and indexer_type.lower() == "shared"
```

见 [vllm_ascend/patch/worker/patch_deepseek_v2.py:36-54](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_deepseek_v2.py#L36-L54)。

新构造函数 `_deepseek_v2_mla_attention_init` 内部显式调用 `nn.Module.__init__(self)`（因为不能用 `super()`），随后构建 q/kv 投影、RMSNorm、RoPE、Indexer、`MLAModules` 与 `MultiHeadLatentAttentionWrapper`。其中关于 Indexer 的分支：

```python
# 同文件
skip_indexer_init = _should_skip_indexer_init(config, prefix, _skip_topk)
if self.is_v32 and not skip_indexer_init:
    self.indexer_rope_emb = get_rope(...)
    self.indexer = Indexer(...)
else:
    self.indexer_rope_emb = None
    self.indexer = None
```

见 [vllm_ascend/patch/worker/patch_deepseek_v2.py:229-255](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_deepseek_v2.py#L229-L255)。文件末尾完成两个方法替换：

```python
DeepseekV2MLAAttention.__init__ = _deepseek_v2_mla_attention_init
# ...
DeepseekV2Model.forward = _patched_forward
```

见 [vllm_ascend/patch/worker/patch_deepseek_v2.py:290](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_deepseek_v2.py#L290) 与 [第 357 行](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_deepseek_v2.py#L357)。

> 这两个补丁在 `patch/__init__.py` 的登记簿里都有对应条目（deepseek_v2 是第 3 条，eagle3_init 是第 6 条），写明了 Why / How / Related PR / Future Plan，遇到疑问先去翻登记簿。

#### 4.2.4 代码实践

> **实践目标**：用「替换方法」的手法，复刻一个最小的模型方法补丁骨架。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码：演示「替换方法」补丁骨架
import torch.nn as nn

class MyAttention(nn.Module):     # 假装这是上游类
    def __init__(self, hidden):
        super().__init__()
        self.hidden = hidden

def _patched_init(self, hidden):  # 在类外定义新 __init__
    nn.Module.__init__(self)      # 不能用 super().__init__()
    self.hidden = hidden * 2      # 改一下行为
    print("patched init called")

MyAttention.__init__ = _patched_init   # 绑回原类
a = MyAttention(64)
print(a.hidden)   # 预期打印：patched init called \n 128
```

**需要观察的现象**：把 `nn.Module.__init__(self)` 改成 `super().__init__()` 会怎样？答案是会 `TypeError`（因为 `_patched_init` 不在任何 class 体里，`super()` 找不到 `__class__` 上下文）。

**预期结果**：你能解释「为什么补丁里的 `__init__` 必须显式 `nn.Module.__init__(self)`」。

#### 4.2.5 小练习与答案

**练习 1**：`patch_eagle3_init.py` 把 `get_num_layers(parallel_config)` 换成了 `get_total_num_hidden_layers()`，解决的是什么问题？

**参考答案**：解决流水线并行（PP>1）下，草稿模型用「单 stage 层数」拼参数名前缀，与 checkpoint 的「全局层号」对不上、导致权重加载失败的问题。

**练习 2**：为什么 `_deepseek_v2_mla_attention_init` 里要写 `nn.Module.__init__(self)` 而不是 `super().__init__()`？

**参考答案**：因为这个函数定义在 `DeepseekV2MLAAttention` 类体之外，最终靠赋值替换 `__init__`。在类外定义时 `super()` 无法推导出正确的类上下文，所以必须显式调用基类构造 `nn.Module.__init__(self)`。

---

### 4.3 NPU 算子替换补丁

#### 4.3.1 概念说明

上游 vLLM 在采样、线性注意力（FLA）、Mamba 等路径里用了一批 **Triton 算子**。这些算子在 CUDA 上表现很好，但在昇腾 NPU 上要么没有对应实现、要么性能不佳。更麻烦的是，上游目前**没有统一的 Triton 算子分派机制**，无法让插件按后端选择实现。

vllm-ascend 的对策是：直接把这些上游模块里的**函数属性**替换成自己用 Triton-Ascend / AscendC 写的 NPU 版本。这类补丁的典型形态是：

```python
some_module.some_func = ascend_func   # 把模块属性指向 NPU 实现
```

由于 Python 的 `from ... import some_func` 会把函数绑到导入方的命名空间，**有些补丁需要同时替换多处绑定**才能彻底生效（这点和 platform 篇的 FusedMoE 补丁思路一致）。

#### 4.3.2 核心流程

`patch_triton.py` 做两类事：

1. **无条件替换**：把上游 FLA（flash-linear-attention）、Mamba、gumbel 采样里的若干函数换成 NPU 实现；并给 Triton 模块补上缺失的 `next_power_of_2`。
2. **条件兜底**：当 `HAS_TRITON` 为 `False`（如 310P 没有可用 Triton 后端）时，提供**纯 PyTorch 回退实现**，让 `qwen_gdn_linear_attn` 的 `from-import` 能在模型加载前拿到替换函数。

`patch_rejection_sampler.py` 则更精简：把投机解码验证阶段的三个函数（`apply_sampling_constraints`、`rejection_sample`、`expand_batch_to_tokens`）替换成 NPU 版本（含自定义 Triton 内核与 `npu_top_k_top_p`）。

#### 4.3.3 源码精读

`patch_triton.py` 顶部先把上游各模块和 vllm-ascend 的 NPU 实现都 import 进来，然后逐个重绑定：

```python
# vllm_ascend/patch/worker/patch_triton.py
import vllm.model_executor.layers.mamba.ops.causal_conv1d
import vllm.third_party.flash_linear_attention.ops as fla_ops
# ...
from vllm_ascend.ops.triton.fla.chunk import chunk_gated_delta_rule
from vllm_ascend.ops.triton.fla.layernorm_guard import LayerNormFn
from vllm_ascend.ops.triton.fla.sigmoid_gating import fused_recurrent_gated_delta_rule_fwd_kernel
from vllm_ascend.ops.triton.mamba.causal_conv1d import causal_conv1d_update_npu

# 1) 给 Triton 补 next_power_of_2（torch_npu 自带的 Triton 没这个函数）
triton.next_power_of_2 = next_power_of_2

# 2) 把上游算子换成 NPU 实现
vllm.model_executor.layers.mamba.ops.causal_conv1d.causal_conv1d_update = causal_conv1d_update_npu
fla_fused_recurrent.fused_recurrent_gated_delta_rule_fwd_kernel = fused_recurrent_gated_delta_rule_fwd_kernel
fla_layernorm_guard.LayerNormFn = LayerNormFn
fla_ops.chunk_gated_delta_rule = chunk_gated_delta_rule
```

见 [vllm_ascend/patch/worker/patch_triton.py:1-19](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_triton.py#L1-L19)。

特别值得注意的是给 Triton 补 `next_power_of_2` 这一行：torch_npu 自带的 Triton 版本缺少这个函数，而上游 vLLM 和 vllm-ascend 在 90 多处调用了它。补丁直接把 `vllm.utils.math_utils.next_power_of_2` 注入到 `triton` 模块上（登记簿第 24 条第 2 点有详细说明）。

当没有 Triton 时，补丁进入纯 PyTorch 回退分支，定义 `_fused_post_conv_prep_pytorch` 等函数并替换 `fla_ops.fused_post_conv_prep`，见 [vllm_ascend/patch/worker/patch_triton.py:25-66](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_triton.py#L25-L66)。这保证了在 310P 等无 Triton 后端的卡上也能跑通 qwen GDN 线性注意力。

`patch_rejection_sampler.py` 则是「替换模块属性/函数」手法的极简范例，整个文件只有几行：

```python
# vllm_ascend/patch/worker/patch_rejection_sampler.py
import vllm.v1.sample.rejection_sampler as rs
from vllm_ascend.sample.rejection_sampler import (
    apply_sampling_constraints, expand_batch_to_tokens, rejection_sample)

rs.apply_sampling_constraints = apply_sampling_constraints
rs.rejection_sample = rejection_sample
rs.expand_batch_to_tokens = expand_batch_to_tokens
```

见 [vllm_ascend/patch/worker/patch_rejection_sampler.py:1-9](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_rejection_sampler.py#L1-L9)。文件顶部的 `TODO` 注释也透露了未来计划：等这些函数被抽成 `RejectionSampler` 的类方法后，就可以建一个 `AscendRejectionSampler` 用继承替代 monkey-patch，从而删掉这个补丁文件。

#### 4.3.4 代码实践

> **实践目标**：理解「替换模块属性」补丁，并验证替换是否生效。

**操作步骤**（源码阅读型）：

1. 打开 [patch_rejection_sampler.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_rejection_sampler.py)。
2. 它替换了 `vllm.v1.sample.rejection_sampler` 模块里的哪三个函数？
3. 跟到 `vllm_ascend/sample/rejection_sampler.py`，看 NPU 版本的 `apply_sampling_constraints` 比上游多了什么（提示：登记簿第 21 条说它「add npu_top_k_top_p」）。
4. 思考：为什么补丁写成 `rs.apply_sampling_constraints = ...`（替换模块属性），而不是直接 `from vllm_ascend... import apply_sampling_constraints`？因为上游代码里是 `from vllm.v1.sample.rejection_sampler import apply_sampling_constraints`，补丁必须改的是**上游模块**里的那个名字，导入方拿到的引用才会是新版。

**需要观察的现象**：你会看到 NPU 版本在采样约束里多了一条 `npu_top_k_top_p` 的快路径，这是上游没有的 NPU 专用优化。

**预期结果**：能说清「算子替换补丁 = 改上游模块里的函数属性，让上游代码透明地调用到 NPU 实现」。

#### 4.3.5 小练习与答案

**练习 1**：`patch_triton.py` 为什么要执行 `triton.next_power_of_2 = next_power_of_2`？

**参考答案**：torch_npu 自带的 Triton 版本缺少 `next_power_of_2`，而上游 vLLM 与 vllm-ascend 在大量地方调用了它。补丁把 vLLM 的 `math_utils.next_power_of_2` 注入到 `triton` 模块上，避免 `AttributeError`。

**练习 2**：`patch_triton.py` 里 `if not HAS_TRITON` 分支的作用是什么？

**参考答案**：在没有可用 Triton 后端的 NPU（如 310P）上，提供纯 PyTorch 的回退实现（如 `_fused_post_conv_prep_pytorch`），替换掉原本依赖 Triton 的算子，保证模型仍能加载和前向。

---

### 4.4 图模式与 v2 架构补丁

#### 4.4.1 概念说明

vLLM 用 **CUDA Graph** 来捕获并回放计算图、减少 kernel launch 开销。昇腾 NPU 对应的机制叫 **ACL Graph**（由 vllm-ascend 的 `compilation/acl_graph.py` 实现，见 [u8-l3](u8-l3-aclgraph.md)）。但上游的图调度逻辑（`CudagraphDispatcher`）有几个地方在 NPU 上会出错，需要 worker 补丁来修正。

此外，vLLM 还在演进一套新的 **v2 model runner** 架构。vllm-ascend 在 `patch/worker/patch_v2/` 下放了一组补丁，把 v2 runner 里几个关键类（`BlockTables`、`InputBatch`、`ModelState` 等）换成 Ascend 版本，并把投机解码用的 **CUDA Graph 管理器**换成 **ACL Graph 管理器**。

这里出现第四种补丁手法：**替换整个类**。

```python
some_module.SomeClass = AscendSomeClass   # 把模块里的类整个换掉
```

#### 4.4.2 核心流程

- `patch_cudagraph.py`：重写 `CudagraphDispatcher._create_padded_batch_descriptor`。上游在「FULL 图模式」下会出错，补丁改写了其中的判定条件，让 FULL 模式在 NPU 上能被正确处理（登记簿第 1 条）。
- `patch_v2/patch_block_table.py`：把 `model_runner.BlockTables` 换成 `AscendBlockTables`，因为 NPU 需要把 slot mapping 初始化成 `int32`（上游默认 `int64`）。
- `patch_v2/patch_input_batch.py`：把 `cudagraph_utils.InputBatch` 和 `model_runner.InputBatch` 都换成 `AscendInputBatch`（注意要替换**两处**绑定）。
- `patch_v2/patch_eagle_speculator.py` 与 `patch_dflash_speculator.py`：把上游投机解码用的 `SpeculatorCudaGraphManager` / `DFlashCudaGraphManager` 换成对应的 **ACL Graph** 管理器。

还有一个值得单独提的手法——**复用 platform 补丁**。`patch_fused_moe.py` 和 `patch_v2/patch_use_v2_model_runner.py` 本身几乎不写逻辑，只是 `import` 对应的 platform 补丁模块。原因是 worker 子进程会重新 import 这些工厂/属性，如果再独立写一遍 monkey-patch，就会**把已经打过补丁的对象再包一层**（double-wrap）。所以让补丁逻辑只存在于 platform 那一个模块里，worker 侧复用 import 即可幂等生效。

#### 4.4.3 源码精读

`patch_cudagraph.py` 是「替换方法」手法的典型，它定义新函数后直接绑回上游类的方法：

```python
# vllm_ascend/patch/worker/patch_cudagraph.py
from vllm.config import CUDAGraphMode
from vllm.forward_context import BatchDescriptor
from vllm.v1.cudagraph_dispatcher import CudagraphDispatcher

def _create_padded_batch_descriptor(self, num_tokens, uniform_decode, has_lora,
                                    num_active_loras=0) -> BatchDescriptor:
    max_num_seqs = self.vllm_config.scheduler_config.max_num_seqs
    uniform_decode_query_len = self.uniform_decode_query_len
    num_tokens_padded = self._bs_to_padded_graph_size[num_tokens]

    # FULL 模式不应被当作 uniform decode 处理 —— 这是修复的关键条件
    if (uniform_decode
            and self.cudagraph_mode.has_mode(CUDAGraphMode.FULL)
            and self.cudagraph_mode != CUDAGraphMode.FULL):
        num_reqs = min(num_tokens_padded // uniform_decode_query_len, max_num_seqs)
        assert num_tokens_padded % uniform_decode_query_len == 0
    else:
        uniform_decode = False
        num_reqs = min(num_tokens_padded, max_num_seqs)

    return BatchDescriptor(num_tokens=num_tokens_padded, num_reqs=num_reqs,
                           uniform=uniform_decode, has_lora=has_lora,
                           num_active_loras=num_active_loras)

CudagraphDispatcher._create_padded_batch_descriptor = _create_padded_batch_descriptor
```

见 [vllm_ascend/patch/worker/patch_cudagraph.py:6-38](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_cudagraph.py#L6-L38)。

`patch_v2/patch_block_table.py` 是「替换整个类」手法的典型，整个文件核心就一行赋值：

```python
# vllm_ascend/patch/worker/patch_v2/patch_block_table.py
from vllm.v1.worker.gpu import model_runner
from vllm_ascend.worker.v2.block_table import AscendBlockTables

# NPU 需要把 slot mapping 初始化成 int32，上游默认是 int64
model_runner.BlockTables = AscendBlockTables
```

见 [vllm_ascend/patch/worker/patch_v2/patch_block_table.py:19-25](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_v2/patch_block_table.py#L19-L25)。

`patch_v2/patch_input_batch.py` 展示了「同一对象要替换多处绑定」的必要性。上游 `InputBatch` 既被 `cudagraph_utils` 引用，也被 `model_runner` 引用，两处都要换：

```python
# vllm_ascend/patch/worker/patch_v2/patch_input_batch.py
from vllm.v1.worker.gpu import cudagraph_utils, model_runner   # 显式导入，确保模块已加载
from vllm_ascend.worker.v2.input_batch import AscendInputBatch

cudagraph_utils.InputBatch = AscendInputBatch
model_runner.InputBatch = AscendInputBatch
```

见 [vllm_ascend/patch/worker/patch_v2/patch_input_batch.py:22-27](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_v2/patch_input_batch.py#L22-L27)。注释「显式导入模块，确保模块被加载后再进行 patch」点明了时序要求。

`patch_v2/patch_eagle_speculator.py` 把投机解码的图管理器从 CUDA Graph 换成 ACL Graph：

```python
# vllm_ascend/patch/worker/patch_v2/patch_eagle_speculator.py
from vllm.v1.worker.gpu.spec_decode.autoregressive import speculator as vllm_speculator_module
from vllm_ascend.worker.v2.spec_decode.eagle.aclgraph import EagleAclGraphManager

vllm_speculator_module.SpeculatorCudaGraphManager = EagleAclGraphManager
```

见 [vllm_ascend/patch/worker/patch_v2/patch_eagle_speculator.py:19-23](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_v2/patch_eagle_speculator.py#L19-L23)。

最后看「复用 platform 补丁」手法。`patch_fused_moe.py`（worker 侧）本身不打任何补丁，只 import platform 那一份，避免二次包装：

```python
# vllm_ascend/patch/worker/patch_fused_moe.py
# Reuse the platform patch. Keeping the monkey patch in one module avoids
# wrapping an already patched FusedMoE factory during worker initialization.
import vllm_ascend.patch.platform.patch_fused_moe  # noqa: F401
```

见 [vllm_ascend/patch/worker/patch_fused_moe.py:18-20](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_fused_moe.py#L18-L20)。`patch_v2/patch_use_v2_model_runner.py` 同理，见 [第 1-3 行](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_v2/patch_use_v2_model_runner.py#L1-L3)。

> 小结四种手法：**替换方法**（`Cls.m = f`）、**替换模块属性/函数**（`mod.f = f`）、**替换整个类**（`mod.Cls = NewCls`）、**复用 platform 补丁**（`import platform_patch`）。判断一个补丁用哪种，看它替换的目标是「类的方法」「模块的函数」还是「模块里的类」。

#### 4.4.4 代码实践

> **实践目标**：完成本讲指定的实践任务——挑一个 worker patch，说清它在 `worker.__init__` 里何时被导入、替换了哪个上游方法。

我们选 `patch_cudagraph.py` 作为范例。

**操作步骤**：

1. **定位导入时机**：打开 [patch/worker/__init__.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/__init__.py)，找到第 53 行 `import vllm_ascend.patch.worker.patch_cudagraph  # noqa`。它在容错块（46–49 行）之后、`patch_deepseek_mtp`/`patch_deepseek_v2`（54–55 行）之前被导入。也就是说，当 `NPUWorker.__init__` 调用 `adapt_patch()` 时，执行到第 53 行就会触发 `patch_cudagraph` 模块加载，从而打上补丁。

2. **定位替换目标**：打开 [patch_cudagraph.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_cudagraph.py)。它替换的上游方法是 `vllm.v1.cudagraph_dispatcher.CudagraphDispatcher._create_padded_batch_descriptor`（第 38 行的赋值）。

3. **说明补丁意图**：根据登记簿第 1 条，上游的 FULL 图模式在 NPU 上会报错；补丁改写了 `_create_padded_batch_descriptor` 里的判定条件（FULL 模式不被当作 uniform decode），使 FULL 模式可用。

4. **写出说明段落**（参考答案见下方「预期结果」）。

**需要观察的现象**：你能把「导入行号 → 替换的上游符号 → 补丁意图」三者串成一句话。

**预期结果**（参考说明）：

> `patch_cudagraph` 在 `patch/worker/__init__.py` 第 53 行被无条件 import。由于 worker 补丁总入口是由 `NPUWorker.__init__`（worker.py 第 110 行）调用 `adapt_patch()` 触发的，因此它在**每个 worker 子进程初始化时、注册自定义算子之前**就被加载。它把上游 `vllm.v1.cudagraph_dispatcher.CudagraphDispatcher._create_padded_batch_descriptor` 替换为本地实现，改写了 FULL 图模式的判定条件，让 NPU 上的 FULL 图模式不再报错。

（本实践为源码阅读型，无需 NPU；若想真的验证补丁是否生效，可在 NPU 环境对比打补丁前后 `CudagraphDispatcher._create_padded_batch_descriptor` 的 `__module__` 属性——待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`patch_v2/patch_input_batch.py` 为什么要把 `InputBatch` 同时赋给 `cudagraph_utils` 和 `model_runner` 两个模块？

**参考答案**：因为上游 `InputBatch` 在两个模块里都被引用（`from ... import InputBatch`）。如果只替换一处，另一处仍持有旧的类引用，补丁就不彻底。补丁必须覆盖所有持有该引用的模块。

**练习 2**：worker 侧的 `patch_fused_moe.py` 为什么只写一行 `import ... platform.patch_fused_moe`，而不自己重新做 monkey-patch？

**参考答案**：platform 补丁在引擎核心子进程已经把 `FusedMoE` 工厂重定向到 `AscendMoERunner`。worker 子进程会重新 import 该工厂，如果 worker 再独立打一遍补丁，会把「已经打过补丁的工厂」再包一层（double-wrap）。让补丁逻辑只存在于 platform 那一个模块、worker 侧复用 import，可以保证幂等。

## 5. 综合实践

> **贯穿任务**：给 vllm-ascend「假装」新增一个 worker 补丁，走完整套流程，把本讲四个模块的知识串起来。

**背景**：假设上游 `vllm.v1.cudagraph_dispatcher.CudagraphDispatcher` 多了一个 `_dummy_method`，你想在 NPU 上把它替换成空操作并打印一条日志。

**要求你完成**：

1. **新建补丁文件** `vllm_ascend/patch/worker/patch_dummy.py`（仅作练习构思，**不要真的改源码**），用本讲学到的四种手法之一（这里是「替换方法」）：

   ```python
   # 示例代码：练习用补丁骨架
   import logging
   from vllm.v1.cudagraph_dispatcher import CudagraphDispatcher

   logger = logging.getLogger(__name__)

   def _dummy_method(self, *args, **kwargs):
       logger.info("patch_dummy: _dummy_method replaced on NPU")
       return None

   CudagraphDispatcher._dummy_method = _dummy_method
   ```

2. **登记到总入口**：在 `patch/worker/__init__.py` 合适位置加一行 `import vllm_ascend.patch.worker.patch_dummy  # noqa`。思考：应该加在哪一组？（答：它是无条件、不依赖 Triton/310P 的，放第 2 组无条件区即可。）

3. **写登记簿**：仿照 `patch/__init__.py` 里其它条目的格式，补一段 What / Why / How / Related PR / Future Plan 的注释。

4. **追踪加载链路**：用一段话说明这个新补丁「在 `NPUWorker.__init__` 的哪一步、由谁触发、替换了上游哪个符号」。这正是 4.1.2 的流程图。

5. **自检时机**：确认补丁在「worker 真正调用 `_dummy_method` 之前」就被 import。由于总入口在 `NPUWorker.__init__` 最前面执行，这一条件天然满足。

**预期结果**：你能独立产出一个「文件 + 入口登记 + 登记簿注释 + 加载链路说明」的四件套，证明你已经掌握 worker 补丁的完整开发与接入流程。（本实践为源码阅读与构思型，无需 NPU，也**不要真的修改仓库源码**。）

## 6. 本讲小结

- **worker 补丁的加载链路**：`NPUWorker.__init__` → `adapt_patch()`（默认 `is_global_patch=False`）→ `import vllm_ascend.patch.worker` → 执行 `worker/__init__.py` → 按序 import 各补丁模块 → import 即替换上游符号。
- **为什么要每个 worker 重打**：spawn 出的 worker 子进程不继承父进程补丁，且各卡前向依赖的符号必须在本进程被替换。
- **总入口的四组结构**：按 `HAS_TRITON`、`is_310p()`、`try/except` 做能力 gating 与容错，import 顺序即依赖顺序。
- **四种改写手法**：替换方法（`Cls.m = f`）、替换模块属性/函数（`mod.f = f`）、替换整个类（`mod.Cls = NewCls`）、复用 platform 补丁（`import platform_patch`，防 double-wrap）。
- **模型专属补丁**（deepseek_v2、eagle3_init）：重写 `__init__`/`forward`，修正权重布局与 PP 层号；注意类外定义的 `__init__` 不能用 `super()`。
- **算子替换补丁**（triton、rejection_sampler）：把上游 Triton/采样函数换成 NPU 实现，无 Triton 时走纯 PyTorch 回退。
- **图模式与 v2 补丁**（cudagraph、patch_v2 系列）：修正 `CudagraphDispatcher`、把 v2 runner 关键类换成 Ascend 版、把投机解码的 CUDA Graph 管理器换成 ACL Graph 管理器。

## 7. 下一步学习建议

- **进入执行主链路**：worker 补丁打完后，worker 如何跑一次前向？建议接着读 [u4-l1 NPUWorker 生命周期](u4-l1-npuworker-lifecycle.md) 与 [u4-l2 NPUModelRunner v1 主链路](u4-l2-model-runner-v1.md)，把「补丁」与「真实前向」连起来。
- **深入图模式**：本讲多次提到 ACL Graph，它的捕获与回放在 [u8-l3 ACL Graph 捕获与回放](u8-l3-aclgraph.md) 详解。
- **动手贡献**：想真正新增一个补丁并提 PR？直接看 [u11-l5 二次开发实战：贡献一个新补丁](u11-l5-contribute-new-patch.md)，里面有命名、文档化、lint 与 Conventional Commits 的完整规范。
- **查阅登记簿**：遇到任何不理解的上游行为改写，第一站永远是 [`vllm_ascend/patch/__init__.py`](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py) 顶部的注释登记簿——它按字母序列出了每一个补丁的来龙去脉。
