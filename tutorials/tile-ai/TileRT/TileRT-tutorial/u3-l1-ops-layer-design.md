# 算子(ops)层设计：融合算子、algorithm 枚举与 device_sharding

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `tilert/models/deepseek_v3_2/ops/` 下任意一个融合算子类的**统一骨架**由哪几部分构成。
- 理解 `algorithm` 枚举如何驱动「选核 / 白名单校验 / 离线转换分发」三件事，并看懂 `ref_weights_alias` 与 `tilert_weights_alias` 这两套权重别名的分工。
- 准确说出 `device_sharding` 在哪两条路径上被复用（离线转换、运行时参考路径），以及为什么运行时 `init_tilert_weights` 反而**不**调用它。
- 具备从 `ops/__init__.py` 这个全局索引出发，快速定位任意算子源文件的能力。

本讲是单元 3 的第一篇。它把单元 2 讲过的 `TileRTModule` 基类契约与 `Dsa` 层组装，下沉到「一个叶子算子长什么样」这一最细颗粒度，为后续 u3-l2（解码主循环）、u3-l3（MTP 投机解码）提供算子视角。

## 2. 前置知识

在进入本讲前，请确认你已理解下面这些来自前置讲义的概念（本讲不再重复展开）：

- **TileRTModule 基类契约**（来自 u2-l1）：所有算子与容器都继承自 `TileRTModule`，强制实现两条前向——`golden_forward`（纯 PyTorch 参考实现，用于数值对拍）与 `tilert_forward`（调用后端 `.so` 高性能算子）。容器基类 `SerializableTileRTModule` 用 `exec_seq / prefix_seq / suffix_seq` 装配子算子。
- **Dsa 层组装与键名契约**（来自 u2-l4）：61 个 transformer 层用 `register_op(prefix, suffix)` 拼成算子树，统一键名模板 `layer_{层号}_{短别名}_dev_{卡号}`，prefix/suffix 只在最外层出现一次。
- **三层张量执行契约**（来自 u2-l5）：算子的权重归入 `params`、激活归入 `temp_vars`、KV 归入 `caches`，最终压扁成扁平张量列表交给 C++ 后端。
- **离线权重转换**（来自 u1-l6）：`WeightConverter` 把 HuggingFace checkpoint 切成「每卡一份」的布局，运行时直接 mmap 加载，无需现场重排。

本讲要回答的核心问题是：**当 `Dsa` 容器里挂着的某个叶子算子（比如 `RmsnormProjqWqi`）被实例化时，它内部到底定义了什么？它产出的权重在「离线转换」和「运行时加载」两条路径上分别经历了什么变换？**

几个本讲会用到的术语：

- **融合算子（fused op）**：把多个细粒度计算（如 RMSNorm + 投影 + 反量化）融进**一个**后端 kernel，减少 kernel 启动与访存开销。TileRT 的超低延迟很大程度来自这类融合。
- **MMA（Matrix Multiply-Accumulate）**：GPU Tensor Core 的矩阵乘累加指令。不同精度对应不同 layout（`fp8mma` / `fp16mma` / `bf16mma`），权重需要预先「swizzle」成 Tensor Core 友好的内存排布。
- **swizzle**：把权重视图按 `(2,8,2,4,...)` 这样的小块重排再转置，使其与 Tensor Core 的分块对齐。本讲只把它当作「一种 layout 变换」对待，不深究其数学。
- **FP8 权重与 scale 配对**：FP8 权重 `weight` 必须和一个 `weight_scale_inv` 成对出现，运行时按 block 反量化回高精度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilert/models/base.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py) | `TileRTModule` 基类、`TilertWeightsConverter.dispatch`、算法白名单校验。是所有算子的契约来源（u2-l1 已讲，本讲只引用）。 |
| [tilert/models/deepseek_v3_2/ops/__init__.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/__init__.py) | 算子层的**全局索引**：集中 re-export 近 30 个算子的类名、算法枚举、函数式包装。定位算子的入口。 |
| [tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py) | 主范例一：MLA 索引头投影算子。结构简单、权重复制型，适合讲「统一骨架」。 |
| [tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py) | 主范例二：MoE 专家选择+up/gate+SiLU 融合算子。结构复杂、权重真正按 `moe_inter_dim` 切分，适合讲「device_sharding 双用途」。 |
| [tilert/models/preprocess/weight_converter.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py) | 离线转换器，是 `device_sharding` 在「离线转换」一侧的调用方（u1-l6 已讲整体流程，本讲聚焦它如何调用算子）。 |

## 4. 核心概念与源码讲解

### 4.1 融合算子统一骨架

#### 4.1.1 概念说明

`ops/` 目录下有近 30 个算子文件，但它们**不是各自为政的**，而是都套用同一套「六件套」骨架。掌握这套骨架后，读任何一个新算子都只需关心「它融合了哪些计算、权重怎么切」，其余部分可以速读。

一个典型算子文件由下面六部分组成：

1. **函数式包装**（如 `rmsnorm_projq_wqi_op`）：一个普通函数，内部一行 `torch.ops.tilert.<xxx>_op(...)`，把 Python 张量转发给后端 `.so` 注册的同名算子。
2. **Algorithm 枚举**（如 `RmsnormProjqWqiAlgorithm`）：列出该算子支持哪些融合精度/核，成员值是字符串。
3. **WeightsConverter**（如 `RmsnormProjqWqiWeightsConverter`）：把「通用格式权重」变换成「某 algorithm 指定的 Tensor Core layout」。
4. **两套权重别名 dataclass**：`...RefWeightsAlias`（HuggingFace 侧长名）与 `...TilertWeightsAlias`（TileRT 侧短名）。
5. **算子类**（继承 `TileRTModule`）：持有 `tilert_weights_alias` / `ref_weights_alias`、`_SUPPORTED_ALGORITHMS`、`device_sharding`、`init_tilert_weights`、`golden_forward`、`tilert_forward` 等。
6. **`__init__.py`** re-export：把上面这些名字集中暴露出去。

这六件套不是死规定，而是 `TileRTModule` 基类契约的自然产物——基类强制了双前向、权重别名、algorithm 字段，子类再各自补上函数式包装与转换器。

#### 4.1.2 核心流程

以一次 `tilert_forward` 调用为例，算子内部的数据流是：

```text
调用方传入激活张量 (temp_vars 里的某个槽)
   │
   ▼
op.tilert_forward(激活)
   │  内部 assert 自有权重/输出缓冲已就绪
   ▼
函数式包装 rmsnorm_projq_wqi_op(...)
   │  传入: 激活、tilert 权重、tilert scales、rmsnorm gamma、
   │        输出缓冲 iq、profile_logs、algorithm.value、arch_name
   ▼
torch.ops.tilert.rmsnorm_proj_qi_op(...)   ← 后端 .so 执行融合 kernel
   │  结果原地写入输出缓冲
   ▼
返回输出缓冲 (供下一个算子消费)
```

关键点：**输出不靠 return 传递，而是靠「预先分配的输出缓冲」原地写入**。这是因为整个模型会被 CUDA Graph 捕获（见 u2-l3），缓冲地址必须在捕获时固定，所以每个算子都在 `init_tilert_vars` 里预先 `torch.zeros` 好输出槽，forward 只往里写。

#### 4.1.3 源码精读

**函数式包装**——一行转发，是 Python 与后端 `.so` 的边界。`rmsnorm_projq_wqi_op` 把所有张量 + `algorithm` 字符串 + `model_arch` 透传给后端：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L21-L40](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L21-L40) —— 定义函数式包装，内部调用 `torch.ops.tilert.rmsnorm_proj_qi_op(...)`。

**算子类继承 `TileRTModule`**——构造时记录 `model_args / device_id / num_devices`，并实例化两套别名：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L183-L208](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L183-L208) —— `RmsnormProjqWqi` 类继承 `TileRTModule`，构造函数调用 `super().__init__(...)` 并挂上 `tilert_weights_alias` / `ref_weights_alias`。

**预先分配输出缓冲**——`init_tilert_vars` 在 forward 前被调用，分配 `iq` 输出张量与 profiling 张量：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L293-L299](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L293-L299) —— `init_tilert_vars` 分配 `iq = torch.zeros(...)`，并置 `is_var_init = True`。

**两条前向**——`golden_forward` 用纯 PyTorch 的 `rms_norm` + `matmul` 给出参考值；`tilert_forward` 调函数式包装走后端。注意 `tilert_forward` 末尾直接 `return self.iq`（原地写入的缓冲）：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L316-L340](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L316-L340) —— `tilert_forward` 调用 `rmsnorm_projq_wqi_op(...)` 并返回 `self.iq`。

> 这两条前向不是二选一：开发期跑 `golden_forward` 与 `tilert_forward` 做**数值对拍**（golden/tilert 对齐），验证后端 kernel 正确；线上只用 `tilert_forward`。

#### 4.1.4 代码实践

**实践目标**：用肉眼在源码里把「六件套骨架」对号入座，建立读新算子的肌肉记忆。

**操作步骤**：

1. 打开 [rmsnorm_projq_wqi.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py)，从上到下找出六件套各自的行号区间。
2. 再打开 [expert_sel_up_gate_silu.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py)，重复一遍。你会发现虽然第二个文件长得多，但骨架完全一样。

**需要观察的现象**：两个文件的「区块顺序」基本一致——函数式包装 → Algorithm 枚举 → Converter → 别名 dataclass → 算子类。复杂度的差异几乎全在 Converter 的 swizzle 逻辑里。

**预期结果**：你能画出一张表，对每个算子文件标出「函数式包装/枚举/Converter/别名/类/forward」六列的行号。

**待本地验证**：无（纯阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tilert_forward` 不直接 `return torch.matmul(...)`，而是先分配 `self.iq` 再原地写入？

> **答案**：因为整张计算图会被 CUDA Graph 捕获并重放（见 u2-l3 的 `prepare_money`）。CUDA Graph 要求每次重放时张量地址固定不变，所以输出必须落在 `init_tilert_vars` 预先分配好的固定缓冲里，而不能每次 forward 新建。

**练习 2**：函数式包装 `rmsnorm_projq_wqi_op` 把 `algorithm` 作为字符串透传给后端，这样做的好处是什么？

> **答案**：后端 `.so` 里同一个 `_op` 入口可以根据 `algorithm` 字符串选择不同的 kernel 实现（如 `fp16mma` 走一种 Tensor Core 路径、`bf16mma` 走另一种），Python 侧与 C++ 侧通过字符串约定解耦，新增算法只需两边各加一个分支。

### 4.2 algorithm 枚举与权重别名

#### 4.2.1 概念说明

每个算子用一个 `Enum` 列出它支持的算法（融合核），成员值是字符串（如 `"fp8mma"`、`"fp16mma"`、`"bf16mma"`、`"general"`）。这个枚举在三处被用到，是算子的「身份证」：

1. **白名单校验**：基类 `TileRTModule._SUPPORTED_ALGORITHMS` 是一个 `ClassVar[dict[arch_name, list[Enum]]]`，`set_algorithm` 会校验「当前架构是否支持该算法」，不支持就抛错。这使得同一个算子类可以在 DeepSeek-V3.2 与 GLM-5 上支持**不同**的算法子集。
2. **离线转换分发**：转换器基类 `TilertWeightsConverter.dispatch` 用 `getattr(self, f"convert_to_{algorithm.value}")` 把枚举值拼成方法名，定位到对应的 layout 变换函数。这是「枚举值 ↔ 方法名」的隐式绑定。
3. **后端选核**：如上一节所述，`algorithm.value` 作为字符串透传给后端 `.so`。

而**两套权重别名**则回答「同一份权重，在 HuggingFace 和 TileRT 两个世界里分别叫什么名字」：

- `ref_weights_alias`（HF 侧）：长名，如 `self_attn.q_a_layernorm.weight`，离线转换时用它从 HF checkpoint 里**取**权重。
- `tilert_weights_alias`（TileRT 侧）：短名，如 `q_rmsnorm_gamma_qi`，运行时与转换器内部用它**认**权重。

两套别名一一对应、顺序一致，构成了「HF ↔ TileRT」的命名翻译表。

#### 4.2.2 核心流程

algorithm 驱动转换分发的关键是 `TilertWeightsConverter.dispatch`——它把枚举变成方法名：

```text
op.init_tilert_weights(state_dict)
   │  按 tilert_weights_alias() 从 state_dict 取出「通用格式」权重列表
   ▼
Converter(model_args, num_devices).dispatch(self.algorithm, weights)
   │  dispatch 内部: getattr(self, f"convert_to_{algorithm.value}")
   │  例如 algorithm=FP16MMA  →  调 self.convert_to_fp16mma(weights)
   │  例如 algorithm=FP8MMA   →  调 self.convert_to_fp8mma(weights)
   ▼
返回 swizzle 好的 Tensor Core layout 权重
   │  存入 op.tilert_wqi / op.tilert_wqi_scales / ...
   ▼
供 tilert_forward 使用
```

`set_algorithm` 的白名单校验则是一道护栏：

```text
op.set_algorithm(algorithm)
   │  arch = model_args.arch_name            # "deepseek_v3_2" 或 "glm_5"
   │  supported = _SUPPORTED_ALGORITHMS[arch]
   │  if algorithm not in supported: raise ValueError
   ▼
self.algorithm = algorithm
```

#### 4.2.3 源码精读

**Algorithm 枚举**——`RmsnormProjqWqi` 只支持两种 MMA 精度：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L43-L48](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L43-L48) —— 定义 `RmsnormProjqWqiAlgorithm(Enum)`，成员 `FP16MMA="fp16mma"`、`BF16MMA="bf16mma"`。

**架构相关的算法子集**——同一个算子在两个模型上支持不同算法。注意 `glm_5` 只列了 `FP16MMA`：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L186-L192](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L186-L192) —— `_SUPPORTED_ALGORITHMS` 按 `arch_name` 分别列出支持的算法。

**两套别名 dataclass**——`RefWeightsAlias` 用 HF 长名，`TilertWeightsAlias` 用 TileRT 短名，二者通过 `ref_tensor_alias` / `tilert_tensor_alias` 各返回一个顺序一致的列表：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L151-L180](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L151-L180) —— 定义 `RmsnormProjqWqiRefWeightsAlias` 与 `RmsnormProjqWqiTilertWeightsAlias`，注意两者都是可调用对象（实现了 `__call__`）。

对比 MoE 算子的别名就能看到复杂度的跃升——`ExpertSelectUpGateSiLU` 的 HF 别名是用列表推导**展开 256 个专家**生成的：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L51-L74](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L51-L74) —— `ref_tensor_alias` 把 `mlp.experts.{i}.gate_proj.weight` 等 256 个专家键名展开成一个长列表，而 TileRT 侧只用 5 个聚合短名（`exp_gate_weights` 等容纳全部专家）。

**dispatch 把枚举拼成方法名**——这是整个转换体系的分发枢纽，位于基类：

[tilert/models/base.py:L30-L32](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L30-L32) —— `TilertWeightsConverter.dispatch` 用 `getattr(self, f"convert_to_{algorithm.value}")` 定位转换方法。

因此每个 Converter 只需实现 `convert_to_<algo>`，例如 `convert_to_fp16mma` 把权重 swizzle 成 FP16 MMA layout：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L138-L148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L138-L148) —— `convert_to_fp16mma` 解包 `[wqi, wqi_scale, q_norm_weight]` 并委托 `_common_to_tilert_fp16mma`。

**白名单校验**——`set_algorithm` 在基类里挡住不合法的 (arch, algorithm) 组合：

[tilert/models/base.py:L134-L148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L134-L148) —— `set_algorithm` 校验 algorithm 是否在该 arch 的支持列表内。

#### 4.2.4 代码实践

**实践目标**：亲手验证「枚举值 ↔ 方法名」的隐式绑定。

**操作步骤**（示例代码，可在任意能 import tilert 的环境运行；纯 CPU 即可，不触发后端）：

```python
# 示例代码：只演示 dispatch 的命名约定，不依赖 GPU
from tilert.models.deepseek_v3_2.ops.rmsnorm_projq_wqi import (
    RmsnormProjqWqiAlgorithm,
    RmsnormProjqWqiWeightsConverter,
)
from tilert.models.deepseek_v3_2.model_args import ModelArgs

algo = RmsnormProjqWqiAlgorithm.FP16MMA
# 拼出 dispatch 会去找的方法名
method_name = f"convert_to_{algo.value}"
print(algo, "->", method_name)
# 验证该方法确实存在于 Converter 上
converter = RmsnormProjqWqiWeightsConverter(ModelArgs(), num_devices=8)
print("has method:", hasattr(converter, method_name))
print("supported for deepseek_v3_2:",
      RmsnormProjqWqiWeightsConverter.__mro__)  # 仅占位，真正校验见下
```

更准确的校验应走算子类的 `_SUPPORTED_ALGORITHMS`：

```python
from tilert.models.deepseek_v3_2.ops.rmsnorm_projq_wqi import RmsnormProjqWqi
print(RmsnormProjqWqi._SUPPORTED_ALGORITHMS)
# 预期: {'deepseek_v3_2': [...FP16MMA, BF16MMA], 'glm_5': [...FP16MMA]}
```

**需要观察的现象**：`algo.value == "fp16mma"`，拼出的方法名正好是 Converter 上定义的 `convert_to_fp16mma`；`_SUPPORTED_ALGORITHMS` 是按架构分的字典。

**预期结果**：打印出方法名匹配成功、`_SUPPORTED_ALGORITHMS` 的两个架构各有不同算法子集。

**待本地验证**：上述片段不调用后端，但 `import tilert.models...` 是否需要先 `load_backend` 取决于 import 链；若报缺后端错，可仅阅读源码确认 `convert_to_{algo.value}` 命名约定，结论一致。

#### 4.2.5 小练习与答案

**练习 1**：`ExpertSelectUpGateSiLU` 在 `deepseek_v3_2` 上支持 `FP8MMA/FP16MMA/BF16MMA` 三种，在 `glm_5` 上只支持前两种。请说明这套「按架构裁剪算法子集」的机制在哪里被强制执行。

> **答案**：在 `TileRTModule.set_algorithm` 里（[base.py:L134-L148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L134-L148)）。它读取 `self.model_args.arch_name`，从 `_SUPPORTED_ALGORITHMS[arch]` 取支持列表，若传入的 algorithm 不在列表内就 `raise ValueError`。

**练习 2**：为什么 `ref_weights_alias` 里是 256 个专家的长键名，而 `tilert_weights_alias` 里只有 5 个短名？

> **答案**：HF 把每个专家存成独立张量（`experts.{i}.gate_proj.weight`），所以取权重时要枚举 256 个键；TileRT 把全部专家的 gate 权重在 `device_sharding` 里 `torch.cat` 成一个聚合张量（见 4.3.3），运行时只需一个短名 `exp_gate_weights` 即可定位，省去了 256 次查表。

### 4.3 device_sharding 双用途

#### 4.3.1 概念说明

`device_sharding` 是本讲最关键、也最容易误解的方法。一句话定义：**它是一个算子「如何把自身权重切成 8 卡份」的唯一事实来源（single source of truth）**。

之所以叫「双用途」，是因为这一份切分逻辑同时服务两条路径：

| 路径 | 调用方 | 输入 | 产出 |
| --- | --- | --- | --- |
| **离线转换** | `WeightConverter.transform_moe / transform_mlp` | 整层 HF 权重 | 落盘的 `layer_{i}_{短别名}_dev_{d}` 分片 |
| **运行时参考路径** | `op.init_reference_weights` / `op.init_random_weights` | 同一份权重 | 给 `golden_forward` 用的 per-device 参考权重 |

而**运行时快速路径 `init_tilert_weights` 反而不调用 `device_sharding`**——因为切分在离线阶段已经烤进磁盘布局了，运行时只需按短别名读出「每卡那份」通用格式权重，再用 Converter 做 algorithm 相关的 swizzle。这是一个重要的分工：

- `device_sharding`：**跨卡切分**（HF 通用格式 → per-device 通用格式），与 algorithm 无关。
- `Converter.convert_to_<algo>`：**layout 变换**（per-device 通用格式 → per-device Tensor Core layout），与 algorithm 强相关。

两者串联才完成「HF 权重 → 8 卡 ready-to-run 权重」的全过程：离线时 `device_sharding` 切分并落盘，运行时 `convert_to_<algo>` 再 swizzle。

> **两套签名约定**：`device_sharding` 在不同算子上有两种返回风格。一种是**返回 dict**（键为 tilert 短别名、值为 `(num_devices, ...)` 张量），如 `RmsnormProjqWqi`、`ExpertSelectUpGateSiLU`；另一种是**返回 tuple**（每个元素是 `(num_devices, ...)` 张量，常带一个 HF 前缀字符串参数），如 `DownAllReduce`、`RMSNormUpGateSiLU`。后者是较早期的风格，离线转换器按位置解包；前者较新、更自描述。读代码时先看返回类型即可判断。

#### 4.3.2 核心流程

以 MoE 层为例，离线与运行时两条路径如何**共用** `ExpertSelectUpGateSiLU.device_sharding`：

```text
【离线转换】WeightConverter.transform_moe
   │  exp_sel = ExpertSelectUpGateSiLU(model_args, num_devices=8)
   │  用 exp_sel.ref_weights_alias() 从 HF 权重里筛出本算子的键
   │  exp_sharded = exp_sel.device_sharding(exp_weights_map)   ← 共用点①
   │  for dev_id in range(8):
   │      取 exp_sharded[alias][dev_id]  落盘成 layer_{i}_{alias}_dev_{dev_id}

【运行时参考路径】op.init_reference_weights(state_dict)
   │  sharded = self.device_sharding(state_dict)               ← 共用点②
   │  gate_weights = sharded[exp_gate_weights][self.device_id]
   │  weight_dequant → 存入 self.ref_gate, 供 golden_forward 对拍

【运行时快速路径】op.init_tilert_weights(state_dict)   ← 注意：不调 device_sharding
   │  state_dict 已是离线切好的 per-device 通用格式
   │  weights_list = [state_dict[a] for a in self.tilert_weights_alias()]
   │  Converter.dispatch(algorithm, weights_list)  ← 只做 swizzle
   │  存入 self.tilert_weights, 供 tilert_forward
```

关键结论：`device_sharding` 把「切分策略」集中在一处，离线与运行时参考路径都复用它，保证两边对「某权重在第 d 卡上是哪一片」的认知**完全一致**。

#### 4.3.3 源码精读

**范例一：复制型 sharding（返回 dict）**。`RmsnormProjqWqi` 的 IQ 权重不需要按头重分布，只需把 gamma/wqi/scales 在第 0 维 `repeat(num_devices)` 复制 8 份：

[tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py:L233-L250](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L233-L250) —— `device_sharding` 把三个权重各 `repeat(self.num_devices, ...)`，返回以 tilert 短别名为键、`(num_devices, ...)` 张量为值的 dict。

**范例二：真正切分型 sharding（返回 dict）**。`ExpertSelectUpGateSiLU.device_sharding` 把 shared + 256 个 routed 专家的 gate/up 权重各自 `torch.cat` 成聚合张量，其中 `process_gate_up_weights` 沿输入维 `reshape(num_devices, 1, in_dim_per_device, dim)` **真正切开**：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L439-L466](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L439-L466) —— `process_gate_up_weights` 把 `in_dim` 沿 `num_devices` 切分，返回 per-device 的 gate/up 权重与 scales。

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L468-L520](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L468-L520) —— `device_sharding` 收集 shared + 256 routed 专家，cat 成聚合张量，返回 5 个短别名为键的 dict。

**用途①：离线转换调用 device_sharding**。`WeightConverter.transform_moe` 实例化算子、用 `ref_weights_alias()` 筛 HF 键、调 `device_sharding` 得到分片，再按 `[dev_id]` 落盘：

[tilert/models/preprocess/weight_converter.py:L287-L304](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L287-L304) —— `transform_moe` 调 `ExpertSelectUpGateSiLU(...).device_sharding(exp_weights_map)`，并同样调 `ExpertDownAllReduce(...).device_sharding(...)`（这里是 tuple 风格）。

落盘键名在 `__post_process_weights` 里拼成统一的 `layer_{i}_{短别名}_{dev}`：

[tilert/models/preprocess/weight_converter.py:L475-L487](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L475-L487) —— `__post_process_weights` 把每个 param 拼成 `layer_{layer_idx}_{param_name}_{dev}` 写入对应设备的 dict。

**用途②：运行时参考路径复用 device_sharding**。`ExpertSelectUpGateSiLU.init_reference_weights` 调 `self.device_sharding(state_dict)` 再取 `[did]`，为 `golden_forward` 准备 per-device 参考权重：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L522-L552](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L522-L552) —— `init_reference_weights` 复用 `self.device_sharding` 得到分片，再 `weight_dequant` 还原成参考权重。

`init_random_weights`（测试用）同样复用它：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L638-L640](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L638-L640) —— `init_random_weights` 调 `self.device_sharding(ref_state_dict)` 后取 per-device 分片喂给 `init_tilert_weights`。

**反例：运行时快速路径不调 device_sharding**。`init_tilert_weights` 直接按短别名从 state_dict 取「已经切好的」per-device 权重，只调 Converter 做 swizzle：

[tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py:L558-L563](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L558-L563) —— `init_tilert_weights` 用 `self.tilert_weights_alias()` 取权重列表，调 `converter.dispatch(self.algorithm, weights_list)`，全程不碰 `device_sharding`。

> 对比 `RmsnormProjqWqi.init_tilert_weights`（[L261-L273](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L261-L273)）也是同样模式——按短别名取权重、调 Converter、不调 device_sharding。这印证了「切分归离线、swizzle 归运行时」的分工。

**全局索引 `__init__.py`**：定位任意算子的入口。它把每个算子文件的「类名 + 算法枚举 +（可选）函数式包装 + 别名」集中 re-export：

[tilert/models/deepseek_v3_2/ops/__init__.py:L99-L160](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/__init__.py#L99-L160) —— `__all__` 集中暴露近 30 个算子的公开符号。要找某个算子，先在这里看它从哪个子模块 import。

#### 4.3.4 代码实践

**实践目标**：以 `RmsnormProjqWqi` 为对象，亲自追踪它的 algorithm 枚举、两套别名，以及 `device_sharding` 与别名在「离线转换」和「运行时 `init_tilert_weights`」两条路径上分别如何被调用——并**发现一个反直觉的事实**：运行时快速路径并不调用 `device_sharding`。

**操作步骤**：

1. **列枚举与别名**。阅读 [rmsnorm_projq_wqi.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py)，填写下表（示例代码，纯阅读）：

   | 项目 | 值 / 行号 |
   | --- | --- |
   | `RmsnormProjqWqiAlgorithm` 成员 | `FP16MMA="fp16mma"`、`BF16MMA="bf16mma"`（L43-L48） |
   | `_SUPPORTED_ALGORITHMS` | `deepseek_v3_2` 两项、`glm_5` 仅 `FP16MMA`（L186-L192） |
   | `ref_weights_alias` 三个键 | `self_attn.q_a_layernorm.weight` / `self_attn.indexer.wq_b.weight` / `...weight_scale_inv`（L155-L157） |
   | `tilert_weights_alias` 三个键 | `q_rmsnorm_gamma_qi` / `wqi_weights` / `wqi_scales`（L171-L173） |

2. **追踪离线路径**。打开 [weight_converter.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py)。注意 `RmsnormProjqWqi` 属于 MLA 索引头，其实际离线调用发生在 `SparseSelectMlaV2` 容器内部（容器 `device_sharding` 会递归调用叶子算子的 `device_sharding`，见 [base.py:L298-L302](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L298-L302)）。请回答：离线时 `device_sharding` 的输入是 HF 通用格式还是 per-device 格式？产出落盘的键名模板是什么？

3. **追踪运行时 `init_tilert_weights`**。阅读 [rmsnorm_projq_wqi.py:L261-L273](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L261-L273)。回答：它**有没有**调用 `self.device_sharding`？它用哪套别名（ref 还是 tilert）从 `state_dict` 取权重？取出来的权重随后交给谁处理？

4. **对比 `init_reference_weights`**。阅读同文件 [L252-L259](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projq_wqi.py#L252-L259)，再对比 MoE 算子的 [expert_sel_up_gate_silu.py:L522-L552](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L522-L552)。回答：为什么 MoE 算子的参考路径调了 `device_sharding`，而 `RmsnormProjqWqi` 的参考路径没有？

**需要观察的现象**：第 3 步应观察到 `init_tilert_weights` **不**调用 `device_sharding`，而是用 `tilert_weights_alias` 取权重后调 `Converter.dispatch`；第 4 步应观察到「权重真正切分的算子（MoE）才在参考路径复用 device_sharding，纯复制的算子（RmsnormProjqWqi）直接读」。

**预期结果**：你能画出一张表，把 `device_sharding` 与两套别名在「离线 / 运行时参考 / 运行时快速」三条路径上的「是否调用 / 用哪套别名」填清楚。

**待本地验证**：本实践为源码阅读型，无需运行；如需运行时验证，可用 `ExpertSelectUpGateSiLU.init_random_weights()`（[L597-L640](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L597-L640)，需 CUDA）观察它内部确实调用了 `self.device_sharding`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `ExpertSelectUpGateSiLU.device_sharding` 里沿 `moe_inter_dim` 的切分逻辑改错（比如切成了 7 份而非 8 份），最先在哪个环节被发现？

> **答案**：会在**离线转换**阶段就埋下隐患——落盘的 `layer_{i}_exp_gate_weights_dev_{d}` 形状错乱；到运行时 `init_tilert_weights` 做 swizzle 时 `reshape` 会因尺寸不匹配而 assert 失败，或在 `tilert_forward` 调后端时 shape 校验报错。这正是「device_sharding 是切分唯一事实来源」的价值：错也只错在一处。

**练习 2**：为什么说「`device_sharding` 与 algorithm 无关，而 `convert_to_<algo>` 与 algorithm 强相关」？请用职责划分解释。

> **答案**：`device_sharding` 只回答「这份权重在第 d 卡上是哪一片」，这是张量**几何切分**问题，与用什么精度算无关；`convert_to_<algo>` 回答「这一片权重要排成哪种 Tensor Core layout（fp8/fp16 的 swizzle 不同）」，这取决于 algorithm。所以前者跨算法共享，后者按算法分叉。

**练习 3**：`DownAllReduce.device_sharding` 返回 tuple，`ExpertSelectUpGateSiLU.device_sharding` 返回 dict。这两种风格对调用方各有什么影响？

> **答案**：tuple 风格调用方必须按位置解包（如 `down_weights, down_scales = op.device_sharding(...)`），可读性差、易写错顺序；dict 风格按短别名取值（`sharded[alias.exp_gate_weights]`），自描述、不怕顺序。新算子倾向用 dict 风格。

## 5. 综合实践

**任务**：为 `ExpertSelectUpGateSiLU` 算子绘制一份「权重的一生」时序图，把本讲三个模块串起来。

要求在一张图（文字流程图即可）上标出一条 MoE 专家 gate 权重从 HuggingFace 到后端 kernel 的完整旅程，并标注每一步由谁负责、用了 algorithm 还是别名还是 device_sharding：

1. **HF checkpoint** 中名为 `model.layers.{i}.mlp.experts.{e}.gate_proj.weight` 的张量。
2. **离线转换**：`WeightConverter.transform_moe` 用 `ref_weights_alias()`（展开 256 专家）筛出它 → `device_sharding` 把 256+1 个专家 cat 成聚合张量并沿 `moe_inter_dim` 切 8 卡 → 落盘为 `layer_{i}_exp_gate_weights_dev_{d}`。
3. **运行时加载**：`Dsa` 容器 `init_tilert_weights` 用 `layer_{i}_` 前缀 + `exp_gate_weights` 短别名 + `_dev_{d}` 后缀匹配 state_dict（见 u2-l4 键名契约）→ 交给叶子算子 `ExpertSelectUpGateSiLU.init_tilert_weights`。
4. **运行时 swizzle**：`init_tilert_weights` 用 `tilert_weights_alias()` 取出 per-device 权重 → `Converter.dispatch(algorithm)` 按 `algorithm.value`（如 `fp8mma`）调 `convert_to_fp8mma` → swizzle 成 Tensor Core layout，存入 `self.tilert_weights`。
5. **执行**：`tilert_forward` 把 `self.tilert_weights` 连同激活透传给 `torch.ops.tilert.expert_select_up_gate_silu_op(...)`，后端按 `algorithm` 字符串选核执行。

完成后，请在图上用三种颜色（或标记）分别标出：**algorithm 起作用的环节**（步骤 4、5）、**device_sharding 起作用的环节**（步骤 2）、**两套别名起作用的环节**（步骤 2 用 ref、步骤 3 用 tilert）。这张图能帮你一眼看清「切分归离线、swizzle 归运行时、别名贯穿全程」的分工。

**预期结果**：一张标注完整的时序图。如果某个环节你不确定具体行号，标注「待确认」而非臆造。

## 6. 本讲小结

- `ops/` 下近 30 个融合算子共用一套「六件套骨架」：函数式包装 → Algorithm 枚举 → Converter → 两套别名 dataclass → 算子类（继承 `TileRTModule`）→ `__init__.py` re-export。
- `algorithm` 枚举是算子的身份证，同时驱动**白名单校验**（`set_algorithm` 按 arch 裁剪）、**离线转换分发**（`dispatch` 把枚举值拼成 `convert_to_<algo>` 方法名）、**后端选核**（`algorithm.value` 透传给 `.so`）。
- `ref_weights_alias`（HF 长名，取权重）与 `tilert_weights_alias`（TileRT 短名，认权重）一一对应，构成命名翻译表；MoE 类算子的 ref 别名会展开 256 个专家，tilert 别名只用少数聚合短名。
- **`device_sharding` 是切分策略的唯一事实来源**，被「离线转换」与「运行时参考路径（`init_reference_weights`/`init_random_weights`）」两条路径复用；运行时快速路径 `init_tilert_weights` **不**调用它，因为切分已烤进磁盘。
- 职责分工：`device_sharding` 做跨卡**几何切分**（与 algorithm 无关），`convert_to_<algo>` 做 **Tensor Core layout swizzle**（与 algorithm 强相关），两者串联完成「HF 权重 → 8 卡 ready-to-run 权重」。
- `device_sharding` 有 dict（新、自描述）与 tuple（旧、按位置）两种返回风格；定位任意算子从 `ops/__init__.py` 的 `__all__` 入手。

## 7. 下一步学习建议

- **u3-l2 生成主循环：非 MTP 的逐 token 解码**：本讲讲清了「一个算子内部如何工作」，下一讲把它们串成解码主循环，看 `decode_layer.forward` 如何驱动 `dsa_show_hands` 把 token 喂进算子树、再从 `TOKEN_OUT` 槽取出下一个 token。
- **u3-l3 MTP 多 token 预测与投机解码**：会用到本讲的 `ExpertSelectUpGateSiLU`（MTP 模块复用主模型 MoE 算子），届时可回来对照本讲的别名与 sharding 细节。
- **延伸阅读**：挑一个本讲没细讲的算子（如 [unproj_o_allreduce.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/unproj_o_allreduce.py) 或 [rmsnorm_projx_wqakis.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_projx_wqakis.py)），用本讲的六件套骨架速读，验证你是否能在 5 分钟内说清它的 algorithm、两套别名与 device_sharding 风格。
