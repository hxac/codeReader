# u6-l1 Python 算子注册与 Custom Op

## 1. 本讲目标

本讲是「自定义算子三层体系」的第一层，聚焦 **Python 层算子注册**。读完本讲，你应该能够：

- 说清 `direct_register_custom_op` 的 **impl / fake / 注册 / 调用点** 四段式（impl/wrap 模式）各扮演什么角色。
- 读懂 `_maybe_chunk_residual`、`maybe_all_gather_and_maybe_unpad`、`maybe_pad_and_reduce` 等**序列并行（Sequence Parallel，SP）辅助算子**为什么必须做成「自定义算子」而不是普通函数。
- 区分 vllm-ascend 里**三套并存的注册机制**：`direct_register_custom_op`（注册函数算子）、`register_ascend_customop`（注册 CustomOp 层类）、`register_dummy_fusion_op`（注册占位融合算子），并知道它们各自在 `NPUWorker` 初始化时被谁触发。
- 仿照现有代码，独立写出一个「返回张量两倍」的最小自定义算子注册片段。

承接 [u4-l2 NPUModelRunner v1 主链路](u4-l2-model-runner-v1.md)：模型前向里出现的 `torch.ops.vllm.xxx(...)` 调用，正是本讲要拆解的对象——它们不是魔法，而是一批被精心注册过的 Python 自定义算子。

## 2. 前置知识

- **torch.library 与自定义算子（Custom Op）**。PyTorch 允许你把一个 Python 函数注册成 `torch.ops.<命名空间>.<算子名>`。注册后，它就成为一个「一等公民」算子：可以参与 `torch.compile`、可以被 `torch.fx` 图捕获、可以在图模式下被当成一个不可拆分的节点（black box）。
- **dispatch key（分发键）**。PyTorch 的 dispatcher 用 dispatch key 决定一个算子在哪种后端上跑。`CUDA` 是 GPU，`CPU` 是 CPU，而 `PrivateUse1` 是 PyTorch 给**自定义后端**（树外后端）预留的键。还记得 [u2-l1](u2-l1-npuplatform-core.md) 里 `NPUPlatform.dispatch_key = "PrivateUse1"` 吗？本讲的算子几乎都用 `dispatch_key="PrivateUse1"` 注册，所以它们专门在 NPU 后端上分发。
- **torch.compile 与 fake impl（抽象实现）**。`torch.compile` 在真正执行前会先用「假张量（fake tensor）」做一遍符号化跟踪，推断每个算子输出的形状/类型，但不真正计算。要让自定义算子支持这一步，必须提供一个 `fake_impl`（也叫 meta 函数），它只看输入形状、返回同形状的占位张量。
- **前向上下文（forward context）与 `_EXTRA_CTX`**。这是 [u2-l3](u2-l3-forward-context-and-moe-comm.md) 引入的机制：每次前向前，vllm-ascend 把「MoE 通信方式、SP 开关、padding 大小」等运行期信息注入前向上下文，深层算子通过 `_EXTRA_CTX` 读取。本讲的 SP 辅助算子高度依赖它。
- **序列并行（Sequence Parallel）**。一种把序列维度（token 维）切分到多卡并行的策略：注意力层之外，把 token 维分散在各卡；进入注意力/通信时再聚合。它是本讲 `maybe_*` 一族算子存在的根本原因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_ascend/ops/register_custom_ops.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py) | 本讲主角。用 `direct_register_custom_op` 注册一批函数型自定义算子（含 SP 辅助算子、量化、RoPE 等）。 |
| [vllm_ascend/ops/\_\_init\_\_.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/__init__.py) | 算子包入口。定义 `dummyFusionOp` 与 `register_dummy_fusion_op`，并靠 `import` 副作用触发各子模块注册。 |
| [vllm_ascend/utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py) | 提供 `register_ascend_customop`（注册 CustomOp 层类）、`enable_sp_by_pass`、`enable_custom_op` 等配套函数。 |
| [vllm_ascend/worker/worker.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py) | 在 `NPUWorker.__init__` 里依次调用 `register_dummy_fusion_op()` 和 `register_ascend_customop()`，是三套机制的触发点。 |
| [vllm_ascend/ops/layernorm.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/layernorm.py) | 典型**调用点**：`AscendRMSNorm.forward_oot` 里调用 `torch.ops.vllm.maybe_chunk_residual`。 |

## 4. 核心概念与源码讲解

### 4.1 Python 算子注册：direct_register_custom_op 的 impl/wrap 模式

#### 4.1.1 概念说明

vllm-ascend 需要在模型前向里插入很多「NPU 专属逻辑」，比如一次带 padding 的 all-gather、一次条件性的 reduce。这些逻辑如果直接写成普通 Python 函数被模型代码调用，会带来两个麻烦：

1. **`torch.compile` 会跟踪进函数体**，把里面的通信算子拆碎、试图融合，而集合通信（all-gather/all-reduce）**必须保持原子**，拆碎会导致正确性或性能问题。
2. **ACL Graph（NPU 版 CUDA Graph，见 u8-l3）需要为算子预留独立工作区**，普通函数不是一个「算子」，无法被图捕获为一个节点。

解决办法是把这些逻辑封装成 **PyTorch 自定义算子**：一旦注册成 `torch.ops.vllm.<name>`，编译器就把它当成一个不可见的「黑盒节点」处理，通信原子性得以保留，ACL Graph 也能正确捕获。

vllm-ascend 借用上游 vLLM 的工具函数 `direct_register_custom_op`（来自 `vllm.utils.torch_utils`），把一个算子写成 **四段式**，业界也常叫 **impl/wrap 模式**：

| 角色 | 命名约定 | 作用 |
| --- | --- | --- |
| **impl（实现）** | `_xxx_impl` | 真正的算子计算逻辑（可调 `torch_npu`、Triton、通信原语）。 |
| **fake（抽象）** | `_xxx_fake` 或 lambda | 只看输入形状、返回占位张量，供 `torch.compile` 跟踪用。 |
| **register（注册）** | `direct_register_custom_op(...)` | 把算子注册进 `torch.ops.vllm.<name>`，并绑定 dispatch key。 |
| **wrap（调用点）** | `torch.ops.vllm.<name>(...)` | 模型代码实际调用的形式，等价于「包装函数」。 |

> 注：在更早期的 vLLM 里，wrap 是一个显式的 `_xxx_wrap` 函数，内部调用 `torch.ops.vllm.xxx(...)`。当前版本里 `direct_register_custom_op` 已经自动注册到 `vllm` 命名空间，调用点直接写 `torch.ops.vllm.xxx(...)` 即可，所以我们用「调用点」来指代 wrap。

#### 4.1.2 核心流程

注册一个函数型自定义算子的完整步骤：

```text
1. 写 _xxx_impl(tensor, ...)        # 真实计算
2. 写 _xxx_fake(tensor, ...)        # 返回占位张量（形状推导）
3. direct_register_custom_op(
       op_name="xxx",
       op_func=_xxx_impl,           # ← 真实现
       fake_impl=_xxx_fake,         # ← 抽象实现
       mutates_args=[],             # 声明哪些参数被原地修改（空表示纯函数）
       dispatch_key="PrivateUse1",  # ← NPU 后端键
   )
4. 模型代码调用 torch.ops.vllm.xxx(tensor, ...)
```

`direct_register_custom_op` 在内部用 `torch.library` 把算子定义到 `vllm` 命名空间，并把 `_xxx_impl` 注册为 `PrivateUse1` 后端的实现，把 `_xxx_fake` 注册为 Meta（抽象）实现。当算子在 NPU 张量上被调用时，dispatcher 看到 `PrivateUse1` 键，就会路由到 `_xxx_impl`。

#### 4.1.3 源码精读

**注册工具的导入**——`register_custom_ops.py` 顶部直接从上游 vLLM 取来 `direct_register_custom_op`：

[vllm_ascend/ops/register_custom_ops.py:14](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L14) —— 这一行说明：vllm-ascend 不重新发明注册机制，而是复用上游 vLLM 的工具，只是把算子的 `dispatch_key` 指向 NPU。

**一个最小算子 `_quantize` 的完整三段式**——它把 `torch_npu.npu_quantize` 包成一个自定义算子，并带注释解释「为什么要包」：

[vllm_ascend/ops/register_custom_ops.py:163-178](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L163-L178) —— 注释点出动机：`aclnnAscendQuantV3` 与 `aclnnAddRmsNormQuantV2` 的 `div_mode` 不一致，必须同时传入 `input_scale` 和 `input_scale_reciprocal` 才能在融合 pass 里避免冗余的倒数计算。`_quantize_impl_fake` 直接复用 `npu_quantize`，是因为它在 NPU 上本身就能做形状推导。

随后把它注册成名为 `quantize` 的算子：

[vllm_ascend/ops/register_custom_ops.py:241-247](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L241-L247) —— 注意 `mutates_args=[]`（纯函数）、`dispatch_key="PrivateUse1"`（NPU）。注册后即可用 `torch.ops.vllm.quantize(...)` 调用。

**调用点长什么样**——以 `maybe_chunk_residual` 为例，它在前向里被这样调用：

[vllm_ascend/ops/layernorm.py:71](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/layernorm.py#L71) —— `AscendRMSNorm.forward_oot` 里，在算 RMSNorm 前先调用 `torch.ops.vllm.maybe_chunk_residual(x, residual)` 把残差对齐到本卡的序列分片。这就是「wrap/调用点」：模型代码不关心注册细节，只把它当一个普通算子调用。

#### 4.1.4 代码实践

> **实践目标**：照搬 `direct_register_custom_op` 的四段式，写一个「返回张量两倍」的占位自定义算子 `my_double`，并验证它被注册到了 `torch.ops.vllm.my_double`。

下面是**示例代码**（不是项目原有代码，供你在本地或 UT 环境中模仿运行；若手头没有 NPU，可把 `dispatch_key` 改成 `"CPU"` 验证注册链路）：

```python
# 示例代码：最小自定义算子注册片段
import torch
from vllm.utils.torch_utils import direct_register_custom_op

# 1) impl：真实计算
def _my_double_impl(x: torch.Tensor) -> torch.Tensor:
    return x * 2

# 2) fake：抽象实现，只推导形状（返回同形状占位张量）
def _my_double_fake(x: torch.Tensor) -> torch.Tensor:
    return torch.empty_like(x)

# 3) register：注册到 torch.ops.vllm.my_double
direct_register_custom_op(
    op_name="my_double",
    op_func=_my_double_impl,
    fake_impl=_my_double_fake,
    mutates_args=[],
    dispatch_key="PrivateUse1",   # NPU 环境用；纯 CPU 验证可改 "CPU"
)

# 4) wrap/调用点
if __name__ == "__main__":
    t = torch.tensor([1.0, 2.0, 3.0], device="npu" if torch.npu.is_available() else "cpu")
    out = torch.ops.vllm.my_double(t)
    print(out)  # 预期：tensor([2., 4., 6.])
```

**操作步骤**：
1. 确认 `vllm` 已安装（`direct_register_custom_op` 来自上游）。
2. 把上面的片段存成 `demo_my_double.py` 并运行：`python demo_my_double.py`。
3. 若无 NPU，把 `dispatch_key` 设为 `"CPU"`、device 设为 `"cpu"`，验证注册链路本身可用。

**需要观察的现象**：
- `torch.ops.vllm.my_double` 在注册前访问会报「未定义算子」；注册后可正常调用。
- 输出 `tensor([2., 4., 6.])`。

**预期结果**：与输入逐元素两倍一致。若在无 NPU 的 UT 环境用 `dispatch_key="CPU"`，结果同样成立。**待本地验证**：`PrivateUse1` 路径需要真实 NPU 设备。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `fake_impl` 省略掉（传 `None`），`torch.compile` 时会发生什么？

**参考答案**：没有 `fake_impl`，`torch.compile` 在符号化跟踪阶段无法推导该算子的输出形状/类型，要么回退到 eager（在该算子处产生 graph break），要么直接报错要求提供 Meta 实现。所以函数型自定义算子一般都要配 `fake_impl`。

**练习 2**：`mutates_args=[]` 表示什么？如果算子会原地改写输入 `x`，该怎么改？

**参考答案**：`mutates_args=[]` 声明该算子不修改任何输入参数（纯函数），便于编译器做别名分析与缓存。若原地改写 `x`，应写成 `mutates_args=["x"]`，让 dispatcher 与图捕获知道 `x` 会被改变。

---

### 4.2 SP 辅助算子：maybe_chunk_residual / maybe_all_gather / maybe_pad_and_reduce

#### 4.2.1 概念说明

序列并行（SP）的核心矛盾是：**注意力和 MoE 通信要「整条序列」，而 RMSNorm、线性层想「本卡分片」以省显存**。于是模型在进入注意力前要把分散在各卡的 token 聚合（all-gather），在离开注意力后又要重新切分（reduce-scatter / chunk）。

问题在于，残差连接（residual）和这些切分点会错位：上一个 RMSNorm 输出的残差是「本地分片」，而下一个残差加法可能发生在「整条序列已恢复」的位置；反过来也成立。如果直接用普通函数处理，`torch.compile` 会跟踪进去把集合通信拆碎。

于是 vllm-ascend 把这些「条件性聚合/切分」逻辑做成了三个核心 SP 辅助算子：

| 算子 | 方向 | 作用 |
| --- | --- | --- |
| `maybe_chunk_residual` | 对齐残差 | 让残差在「本地分片」与「整条序列」之间互转。 |
| `maybe_all_gather_and_maybe_unpad` | 分片→整条 | 聚合各卡 token，并按需去掉 padding。 |
| `maybe_pad_and_reduce` | 整条→分片 | 加 padding 后做 reduce-scatter，回到本地分片。 |

它们都带 `maybe_` 前缀，表示**行为依赖运行期上下文**：只有当前处于 SP / FlashComm 通路时才真正通信，否则退化为直通（identity）或普通 all-reduce。这正是它们必须读 `_EXTRA_CTX` 的原因。

#### 4.2.2 核心流程

以 `maybe_all_gather_and_maybe_unpad` 为例，其决策流：

```text
调用 maybe_all_gather_and_maybe_unpad(x, label, is_ep_comm)
   │
   ├─ 取不到 forward_context？  → 直接返回 x（直通，供离线/单测调用）
   │
   ├─ flash_comm_v1_enabled 且 label 为真？
   │     │
   │     ├─ 非 EP 通信：tensor_model_parallel_all_gather(x, 0)，再去掉尾部 padding
   │     │
   │     └─ EP 通信：ep 组 all_gather；再按 dp 的 num_tokens_across_dp 重组去 padding
   │
   └─ 否则：原样返回 x
```

关键点：算子内部用 `try: get_forward_context() except AssertionError: return x` 做兜底。这保证它**既能在真实前向里做通信，也能在脱离前向上下文的单元测试里安全调用**（直接返回输入）。

#### 4.2.3 源码精读

**`maybe_chunk_residual` 的 impl**——处理残差与序列分片的形状错配：

[vllm_ascend/ops/register_custom_ops.py:22-42](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L22-L42) —— 三个分支：① 取不到上下文直接返回 `residual`；② `residual` 比 `x` 短（前一个 SP 的 RMSNorm 留了本地残差，但 MoE 通路已恢复整条序列）则 all-gather 残差；③ `residual` 比 `x` 长，则按 `tp_rank` 取 `torch.chunk` 的对应分片。注释里写得很清楚：「A preceding SP RMSNorm leaves a local residual, while a MoE communication path can restore the full sequence」。

**`maybe_all_gather_and_maybe_unpad` 的 impl**——SP 通路出口处的聚合+去 padding：

[vllm_ascend/ops/register_custom_ops.py:45-75](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L45-L75) —— 先 `try/except` 兜底；再用 `_EXTRA_CTX.flash_comm_v1_enabled`（或 `enable_sp_by_pass() and is_ep_comm`）判断是否进入 FlashComm v1 通路。EP 分支里还会读 `forward_context.dp_metadata.num_tokens_across_dp_cpu`，按每个 DP rank 的真实 token 数重组、去 padding——这就是「maybe unpad」的来源。

**`maybe_pad_and_reduce` 的 impl**——SP 通路入口处的 padding+reduce-scatter：

[vllm_ascend/ops/register_custom_ops.py:78-108](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L78-L108) —— 与上一个算子方向相反：先用 `_EXTRA_CTX.pad_size` 在尾部补 0，再做 `tensor_model_parallel_reduce_scatter` 回到分片；EP 分支则按 DP 维度逐卡 padding 成统一长度再 reduce-scatter。

**`maybe_all_reduce_tensor_model_parallel` 的 impl**——MoE 出口的条件 all-reduce：

[vllm_ascend/ops/register_custom_ops.py:129-137](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L129-L137) —— 这里直接对接 [u2-l3](u2-l3-forward-context-and-moe-comm.md) 的 `MoECommType`：当 MoE 走 `ALLTOALL/MC2/FUSED_MC2`（或 FlashComm v1）时，通信已经在 MoE 内部完成，这里就**跳过** all-reduce；否则才做一次 `tensor_model_parallel_all_reduce`。这正是「数据依赖运行期上下文」的典型例子。

**调用点**——它们在前向里被广泛使用。例如注意力后端聚合隐藏状态：

[vllm_ascend/ops/fused_moe/prepare_finalize.py:397-398](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/prepare_finalize.py#L397-L398) —— MoE 的 prepare/finalize 阶段，对 `hidden_states` 和 `router_logits` 分别调用 `maybe_all_gather_and_maybe_unpad(..., True, True)`（注意第三个参数 `is_ep_comm=True`，走 EP 通路）。

> 提示：这三个算子的真实行为高度依赖运行期的 `forward_context`。在脱离前向上下文的单测里，它们都会走 `try/except` 兜底分支，**表现为直通函数**。这一点在阅读相关 UT 时至关重要。

#### 4.2.4 代码实践

> **实践目标**：用「源码阅读型实践」追踪一个 SP 辅助算子的调用链，理解它的运行期分支。

**操作步骤**：
1. 打开 `vllm_ascend/ops/register_custom_ops.py`，定位 `_maybe_all_reduce_tensor_model_parallel_impl`（第 129 行）。
2. 用 Grep 在 `vllm_ascend/ops/fused_moe/fused_moe.py` 里搜索它的调用点（约第 165、176 行），观察它出现在 MoE 前向的哪个阶段。
3. 回到 impl，回答：它依据 `_EXTRA_CTX.moe_comm_type` 的哪些取值来决定「跳过 all-reduce」？

**需要观察的现象**：
- 该算子是 MoE 专家计算完成、合并 hidden states 之后被调用的「收尾」步骤。
- 当 `moe_comm_type` 属于 `{ALLTOALL, MC2, FUSED_MC2}` 或 FlashComm v1 开启时，直接返回输入；否则做 TP all-reduce。

**预期结果**：你会理解「为什么 ALLTOALL/MC2 模式下不需要再 all-reduce」——因为这些模式已经把跨卡结果搬运并合并好了，再 reduce 就是重复劳动。

**待本地验证**：在真实 NPU + 多卡环境下，结合 `select_moe_comm_method`（见 u2-l3）观察不同 token 数下走的分支。

#### 4.2.5 小练习与答案

**练习 1**：为什么这些 SP 算子都用 `try: get_forward_context() except AssertionError: return ...` 做兜底，而不是直接调用？

**参考答案**：为了让算子**既能用于真实前向（有上下文，执行通信），也能在单元测试或离线调用中安全运行（无上下文，退化为直通/普通 reduce）**。否则在脱离引擎的 UT 里一调用就会抛 `AssertionError`，无法独立测试算子逻辑。

**练习 2**：`maybe_pad_and_reduce` 与 `maybe_all_gather_and_maybe_unpad` 是一对「进 SP / 出 SP」的算子。请说明它们各自处理 padding 的方向。

**参考答案**：`maybe_all_gather_and_maybe_unpad` 在**聚合后去掉**尾部 padding（让结果回到真实 token 数）；`maybe_pad_and_reduce` 在 **reduce-scatter 之前补上** padding（把各卡不等的 token 数补成统一长度，保证 reduce-scatter 形状对齐）。一去一补，对应 SP 边界的两侧。

---

### 4.3 register_ascend_customop 与 dummy fusion op

前两节讲的是「函数型算子」（`direct_register_custom_op`）。vllm-ascend 还有**另外两套注册机制**，都在 `NPUWorker` 初始化时触发。它们解决的是不同层面的问题，必须区分清楚。

#### 4.3.1 概念说明

**（A）`register_ascend_customop`：注册 CustomOp 层类（面向对象层）**

上游 vLLM 有一个 `CustomOp` 机制：对于一些「值得为每个后端做优化」的标准层（`RMSNorm`、`SiluAndMul`、`RotaryEmbedding`、`ColumnParallelLinear`、`RowParallelLinear` 等），vLLM 允许后端**把整个 `nn.Module` 子类替换**成自己的高性能实现。这种替换叫 **register_oot**（out-of-tree，树外注册）。

`register_ascend_customop` 做的就是：构建一张「层名 → Ascend 子类」的映射表 `REGISTERED_ASCEND_OPS`，然后逐个调用 `CustomOp.register_oot` 把上游层重定向到 Ascend 版。注意，这**不是** `direct_register_custom_op` 那种函数级注册，而是**类级替换**——替换的是模型里整个层的 Python 类。

**（B）`register_dummy_fusion_op`：注册占位融合算子（图变换层）**

vllm-ascend 有一套 `torch.fx` 融合 pass（见 [u8-l2 融合 Pass](u8-l2-fusion-passes.md)），它们会把 `rms_norm + quant`、`add + rms_norm` 等子图融合成单个 NPU 算子，例如 `torch.ops._C_ascend.rms_norm_static_fp8_quant`。

问题：在 `torch.compile` 的符号化跟踪阶段，`torch.fx` 需要这些 `_C_ascend.*` 算子**作为属性真实存在**于 `torch.ops._C_ascend` 上，否则跟踪会失败。而真正的 `_C_ascend` 内核（来自 `vllm_ascend_C` 扩展）未必在跟踪时已就绪。于是 vllm-ascend 先注册一批 **dummy 占位对象**——它们没有真实计算，只为了让图跟踪和模式匹配能「看到」这些算子节点。真正的计算语义由后续融合 pass 注入。

`dummyFusionOp` 就是这样一个占位类，仅保存一个 `name` 属性：

[vllm_ascend/ops/\_\_init\_\_.py:36-40](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/__init__.py#L36-L40) —— 极简的占位类，目的只是「占住 `torch.ops._C_ascend` 上的一个名字」。

#### 4.3.2 核心流程

**`register_ascend_customop` 流程**（一次性、幂等）：

```text
1. 检查全局旗标 _ASCEND_CUSTOMOP_IS_REIGISTERED，已注册则直接 return
2. 构造 REGISTERED_ASCEND_OPS = { 层名: Ascend子类, ... }（~25 项）
3. 若是 DeepSeek MLA 模型：追加 "GateLinear" -> AscendGateLinear
4. 若是 310P：用 310P 专属子类覆盖部分项（如 SiluAndMul、RMSNorm...）
5. for name, op_cls in REGISTERED_ASCEND_OPS.items():
        CustomOp.register_oot(_decorated_op_cls=op_cls, name=name)
6. 置 _ASCEND_CUSTOMOP_IS_REIGISTERED = True
```

**`register_dummy_fusion_op` 流程**：把若干名字（`rms_norm`、`fused_add_rms_norm`、`static_scaled_fp8_quant`、`dynamic_scaled_fp8_quant`、`rms_norm_static_fp8_quant` 等）逐个赋值为 `dummyFusionOp(name=...)`，挂到 `torch.ops._C_ascend` 上。

**触发时机**：两者都在 `NPUWorker.__init__` 里被调用：

[vllm_ascend/worker/worker.py:110-118](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L110-L118) —— 注意顺序：先 `adapt_patch()` 打 worker 补丁，再 `register_dummy_fusion_op()`（占位算子），然后 A5 之外的卡注册 ATB 扩展，最后 `register_ascend_customop(vllm_config)`（层类替换）。这个顺序很重要：层类替换依赖前面补丁建立的执行环境。

#### 4.3.3 源码精读

**`register_ascend_customop` 全貌**——构建映射表并逐项 `register_oot`：

[vllm_ascend/utils.py:660-781](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L660-L781) —— 重点看三处：
- **幂等闸门**（第 666-668 行）：用全局 `_ASCEND_CUSTOMOP_IS_REIGISTERED` 保证每进程只注册一次，避免重复 `register_oot`。
- **核心映射表**（[第 705-732 行](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L705-L732)）：把上游层名（如 `"RMSNorm"`、`"SiluAndMul"`、`"ColumnParallelLinear"`、`"RotaryEmbedding"`）映射到 `AscendRMSNorm`、`AscendSiluAndMul` 等子类。
- **硬件分支**：DeepSeek MLA 追加 `GateLinear`（第 740-743 行）；310P 用 `_310p/ops` 下的子类覆盖部分项（第 746-776 行），呼应 [u11-l2 310P 适配](u11-l2-310p-adaptation.md) 的硬件分支思想。
- **最终注册循环**（[第 777-778 行](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L777-L778)）：`CustomOp.register_oot(_decorated_op_cls=op_cls, name=name)` 真正完成层类替换。

**配套开关 `enable_custom_op`**——控制某些 C++ 自定义算子的惰性初始化：

[vllm_ascend/utils.py:401-420](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L401-L420) —— 注释解释了动机：延迟初始化 `vllm_ascend_C`，避免 CANN 的 RTS 组件过早初始化，从而保证 `ASCEND_RT_VISIBLE_DEVICES` 在 `torch.npu.set_device()` 之前仍可动态修改。同时它会在 `VLLM_BATCH_INVARIANT` 或 A5 芯片时关闭部分自定义算子（A5 上算子编译/执行尚不完全可用）。

**`register_dummy_fusion_op` 全貌**——占位算子的注册：

[vllm_ascend/ops/\_\_init\_\_.py:43-51](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/__init__.py#L43-L51) —— 把 8 个融合算子名（`rms_norm`、`fused_add_rms_norm`、`static_scaled_fp8_quant`、`dynamic_scaled_fp8_quant`、`dynamic_per_token_scaled_fp8_quant`、`rms_norm_static_fp8_quant`、`fused_add_rms_norm_static_fp8_quant`、`rms_norm_dynamic_per_token_quant`）全部赋成 `dummyFusionOp`。这些名字正是后续融合 pass（如 `norm_quant_fusion_pass`）要 pattern-match 的目标算子。

**import 副作用触发注册**——这是容易被忽略的一环：

[vllm_ascend/ops/\_\_init\_\_.py:21-23](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/__init__.py#L21-L23) —— `import vllm_ascend.ops.register_custom_ops`（带 `# noqa`）不是「导入而不用」，而是**靠 import 的副作用**触发该模块顶层的 8 个 `direct_register_custom_op(...)` 调用。也就是说：只要 worker 里 `from vllm_ascend import ops`（worker.py:113），函数型算子就全部注册完毕。这是承接 [u3-l1](u3-l1-patch-overview.md)「import 即打补丁」思想的又一实例——这里则是「import 即注册算子」。

#### 4.3.4 代码实践

> **实践目标**：用「源码阅读型实践」对比三套注册机制，理解它们各自的「注册对象」与「生效层面」。

**操作步骤**：
1. 在 `vllm_ascend/ops/register_custom_ops.py` 中数一下共有多少处 `direct_register_custom_op(...)` 调用（提示：从第 201 行起到文件末尾）。
2. 在 `vllm_ascend/utils.py` 中阅读 `register_ascend_customop`，数一下 `REGISTERED_ASCEND_OPS` 映射表共有多少项（含 310P 覆盖项）。
3. 列一张对照表，填入：机制名、注册的是什么（函数 / 类 / 占位对象）、注册到哪个命名空间、由谁触发。

**需要观察的现象**：
- `register_custom_ops.py` 里有约 8 个函数型算子（`maybe_chunk_residual`、`maybe_all_gather_and_maybe_unpad`、`maybe_pad_and_reduce`、`maybe_all_reduce_tensor_model_parallel`、`matmul_and_reduce`、`quantize`、`npu_rotary_embedding`、`muls_add`）。
- `REGISTERED_ASCEND_OPS` 含约 25 个层类映射。
- 三套机制的触发点都汇聚在 `NPUWorker.__init__`（worker.py:115、118）以及 `ops.__init__` 的 import 副作用。

**预期结果**：你会清楚地区分——`torch.ops.vllm.xxx`（函数算子）、`CustomOp.register_oot`（层类替换）、`torch.ops._C_ascend.xxx`（占位融合算子）三者互不冲突、各司其职。

**待本地验证**：在真实 worker 启动日志里，可在 `register_ascend_customop` 前后打印 `CustomOp` 注册表，确认 Ascend 子类确实替换了上游层。

#### 4.3.5 小练习与答案

**练习 1**：`register_ascend_customop` 为什么要用全局旗标 `_ASCEND_CUSTOMOP_IS_REIGISTERED` 保证「每进程只注册一次」？

**参考答案**：`CustomOp.register_oot` 会修改全局层类映射；重复注册即使不报错，也是无意义的重复劳动，且在测试换配置（refresh）场景下可能造成状态混乱。用幂等旗标兜底，既避免重复，也为「显式 refresh」留出控制点。

**练习 2**：`dummyFusionOp` 没有任何真实计算，为什么还需要注册它？

**参考答案**：它服务于 **`torch.fx` 融合 pass 与 `torch.compile` 符号化跟踪**。跟踪阶段需要 `torch.ops._C_ascend.<name>` 作为属性存在，才能把这些算子捕获成图节点；真实内核要么由后续融合 pass 注入，要么由运行期的 `vllm_ascend_C` 扩展提供。dummy 的职责是「占位」，让图变换流水线在内核就绪前也能跑通。

**练习 3**：函数型算子用 `dispatch_key="PrivateUse1"`，层类替换（`register_oot`）需要指定 dispatch key 吗？为什么？

**参考答案**：不需要。`register_oot` 替换的是 **Python 层的 `nn.Module` 子类**，发生在 import/对象构造层面，不经过 PyTorch dispatcher；而 `direct_register_custom_op` 注册的是 **dispatcher 路由的算子**，必须指明后端键（`PrivateUse1`）才能在 NPU 张量上正确分发。两者的生效层面不同。

---

## 5. 综合实践

**任务**：把本讲三套机制串起来，读懂「一个 SP 算子从注册到被调用」的完整生命周期，并画出数据流。

请按下列步骤完成：

1. **注册阶段**：阅读 [register_custom_ops.py:201-207](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/register_custom_ops.py#L201-L207)，确认 `maybe_chunk_residual` 被注册为 `torch.ops.vllm.maybe_chunk_residual`，dispatch key 为 `PrivateUse1`。
2. **触发阶段**：阅读 [worker.py:113-118](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L113-L118)，说明 `from vllm_ascend import ops` 如何靠 import 副作用完成函数型算子注册，而 `register_ascend_customop` 如何完成层类替换。
3. **运行期阶段**：阅读 [layernorm.py:71](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/layernorm.py#L71)，确认 `AscendRMSNorm.forward_oot` 调用 `torch.ops.vllm.maybe_chunk_residual(x, residual)`。
4. **画图**：用一张时序图标注四个时刻——① worker init 时 import 注册；② worker init 时 `register_ascend_customop` 换层；③ 前向时 `set_ascend_forward_context` 注入 `_EXTRA_CTX`；④ RMSNorm 前向调用 `maybe_chunk_residual` 并按 `_EXTRA_CTX` 决定分支。
5. **动手**：仿照 4.1.4 的示例代码，再注册一个 `my_residual_align` 算子（impl 内部调用 `torch.ops.vllm.maybe_chunk_residual`，fake 返回 `torch.empty_like(x)`），验证你能在 `torch.ops.vllm.my_residual_align` 上调用它。

**预期结果**：你能向别人讲清——vllm-ascend 的 Python 算子体系是「函数算子（direct_register_custom_op）+ 层类替换（register_oot）+ 占位融合算子（dummy）」三位一体，三者都在 worker 初始化时落地，函数算子在运行期由前向上下文驱动其分支行为。

## 6. 本讲小结

- vllm-ascend 用 **`direct_register_custom_op`** 把 NPU 逻辑封装成 `torch.ops.vllm.<name>` 自定义算子，四段式为 **impl（真实现）/ fake（形状推导）/ register（注册到 vllm 命名空间 + `PrivateUse1`）/ wrap（调用点 `torch.ops.vllm.xxx`）**。
- **SP 辅助算子**（`maybe_chunk_residual`、`maybe_all_gather_and_maybe_unpad`、`maybe_pad_and_reduce`、`maybe_all_reduce_tensor_model_parallel`）处理序列并行边界处的聚合/切分/条件 all-reduce，行为依赖运行期的 `_EXTRA_CTX`，并用 `try/get_forward_context` 兜底以便单测。
- 把通信逻辑做成自定义算子的根本原因：**让 `torch.compile` 与 ACL Graph 把它们当原子黑盒**，避免集合通信被拆碎，并为融合 pass 提供干净的图节点。
- **`register_ascend_customop`** 是另一套机制：构建「层名 → Ascend 子类」映射，逐个 `CustomOp.register_oot` 替换上游层（类级，不走 dispatcher），幂等、支持 MLA/310P 分支。
- **`register_dummy_fusion_op`** 注册 `torch.ops._C_ascend.*` 占位对象，服务于 `torch.fx` 融合 pass 的图跟踪与模式匹配，真实内核由后续 pass 或 `vllm_ascend_C` 扩展提供。
- 三套机制都在 `NPUWorker.__init__`（worker.py:113-118）触发，函数型算子额外靠 `ops.__init__` 的 import 副作用注册——这是「import 即注册」的体现。

## 7. 下一步学习建议

- **u6-l2 Triton 算子集**：本讲的 `muls_add`、`npu_rotary_embedding` 已经把 Triton 内核（`muls_add_triton`、`rope_forward_oot`）作为 `op_func` 注册。下一讲深入 `ops/triton/`，看这些高性能内核本身怎么写。
- **u6-l3 C++/AscendC 内核**：本讲的 `dummyFusionOp` 占位的 `_C_ascend.*` 算子，其真实实现来自 C++/AscendC 内核；下一讲讲 `csrc/` 的 `op_host/op_kernel` 双文件结构与 CMake 构建。
- **u8-l2 torch.fx 融合 Pass**：`register_dummy_fusion_op` 占位的算子最终由融合 pass 注入语义，建议在学完编译层后回看本讲，体会「占位 → 匹配 → 融合」的完整闭环。
- **延伸阅读**：用 Grep 搜索 `direct_register_custom_op` 在 `ops/mla.py`、`ops/linear.py`、`ops/dsa.py` 等处的用法，对比不同算子如何选择 `dispatch_key` 与 `fake_impl`。
