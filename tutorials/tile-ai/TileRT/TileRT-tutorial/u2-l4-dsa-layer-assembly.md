# DSA 层组装：dense 层、MoE 层与 Head 的 register_op 调度

## 1. 本讲目标

本讲是「进阶层：模型组装与执行契约」的核心一讲。学完后你应当能够：

- 读懂 `dsa.py` 里 `Dsa` 这个容器是如何用「一个 for 循环 + `register_op`」把 61 个 transformer 层和 1 个 head 拼成一棵可加载权重的算子树；
- 说清楚 dense 层（前 3 层）和 MoE 层（中间 58 层）在 block 类型上的分界，以及 `MlpBlock` / `MoeBlock` 内部「一个 MLA 注意力 + 一个 FFN」的统一结构；
- 掌握 `register_op(prefix=..., suffix=...)` 如何把「短别名」拼接成与离线权重转换器完全一致的扁平 `state_dict` 键名，从而让运行时能精确取到每张卡的分片权重；
- 了解 `device_id == 0` 与其余 7 张卡在 MLA 类型选择（`SparseSelectMlaV2` vs `PureMlaV2`）上的差异及其缓冲区配置。

本讲承接 [u2-l1](u2-l1-tilert-module-base.md)（`TileRTModule` / `SerializableTileRTModule` 的容器装配与权重别名契约）与 [u2-l3](u2-l3-show-hands-dsa-layer.md)（`ShowHandsDSALayer` 如何把 `Dsa` 树搬上 8 卡），向下衔接 [u2-l6](u2-l6-mla-and-sparse-select.md)（MLA 内部）与 [u2-l7](u2-l7-moe-mlp-ffn.md)（MoE/MLP 内部算子链）。

## 2. 前置知识

在进入源码前，先用三段话把背景对齐（细节已在依赖讲义中讲过，这里只做最小回顾）：

- **TileRT 的算子都是「壳」**。每个融合算子类继承 `TileRTModule`，持有两套一一对应的权重别名：`ref_weights_alias`（HuggingFace 侧长名，离线转换时取）与 `tilert_weights_alias`（TileRT 侧短名，运行时认）。算子本身不存权重，只声明「我需要哪些短名」。

- **容器用 `exec_seq` 装配子算子**。`SerializableTileRTModule` 用四个等长的平行列表 `exec_seq` / `prefix_seq` / `suffix_seq` / `retain_weights_seq` 记录「子算子 + 它的前缀 + 它的后缀 + 是否保留权重」。聚合方法（`get_tilert_weights_alias` / `get_weights_list` / `device_sharding` 等）都是遍历 `exec_seq` 递归汇总，所以容器可以无限嵌套。

- **键名匹配只在外层做一次**。`init_tilert_weights` 按 `f"{prefix}{op_key}{suffix}"` 从扁平 `state_dict` 里把权重挑出来交给子算子；位置感知（哪一层、哪张卡）的隔离全部体现在 `prefix` / `suffix` 上，叶子算子只认短名。这就是本讲要重点讲清的「键名拼接契约」。

- **离线权重转换器是键名的另一半**。`weight_converter.py` 把 HF checkpoint 重排成「每卡一份」的布局，输出键名模板统一为 `layer_{层号}_{短别名}_dev_{卡号}`。运行时必须用同样的模板把权重取回来——本讲要验证的就是这条「转换器写出 ↔ Dsa 读回」的字符串往返（round-trip）。

一个形象的比喻：`Dsa` 像一个多层抽屉柜，每个抽屉贴着 `layer_{i}_..._dev_{d}` 的标签；`register_op(prefix, suffix)` 就是给抽屉贴标签的机器；离线转换器则是往对应标签的抽屉里放权重的工人。两边必须用同一套标签规则，否则权重放进去取不出来。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `tilert/models/deepseek_v3_2/modules/dsa.py` | `Dsa` 容器：组装全部 transformer 层与 head | 层循环、dense/MoE 分界、`register_op` 的 prefix/suffix、MLA 类型选择 |
| `tilert/models/deepseek_v3_2/modules/mlp.py` | `Mlp` 与 `MlpBlock`（dense 层） | dense block 的「MLA + MLP」结构 |
| `tilert/models/deepseek_v3_2/modules/moe.py` | `Moe` 与 `MoeBlock`（MoE 层） | MoE block 的「MLA + MoE」结构与三段融合算子 |
| `tilert/models/base.py` | `SerializableTileRTModule` 基类 | `register_op` 与 `init_tilert_weights` 的键名拼接实现 |
| `tilert/models/preprocess/weight_converter.py` | 离线权重转换器 | 键名模板 `layer_{i}_{alias}_dev_{d}` 的生成端 |
| `tilert/models/deepseek_v3_2/model_args.py` | 模型超参 | `n_layers=61`、`n_dense_layers=3` 等分界常量 |

## 4. 核心概念与源码讲解

### 4.1 Dsa 层循环与 dense/MoE 分界

#### 4.1.1 概念说明

DeepSeek-V3.2 是一个「混合」架构：最底下若干层是普通的 dense MLP（全连接前馈），其余层是 MoE（混合专家）。`Dsa`（Decode-time Show-hands Assembler，名字承接 `ShowHandsDSALayer` 的「牌桌」比喻）的任务就是把这两种层按顺序拼起来，再在最顶端接一个用于输出 logits 的 head 投影。

关键的分界常量来自 `ModelArgs`：

- `n_layers = 61`：transformer 主干一共有 61 层；
- `n_dense_layers = 3`：前 3 层是 dense，剩下 58 层是 MoE。

[tilert/models/deepseek_v3_2/model_args.py:62-63](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L62-L63) 定义了这两个值（以及其余超参）。

#### 4.1.2 核心流程

`Dsa.__init__` 的主干是一个 `for layer_idx in range(n_layers)` 循环，循环体内按 `layer_idx < n_dense_layers` 二选一构造 `MlpBlock` 或 `MoeBlock`，然后用 `register_op` 挂到自身。循环结束后，再单独注册一个 `RMSNormHeadProj` 作为 head。伪代码如下：

```text
for layer_idx in 0 .. n_layers-1:          # 0..60
    if layer_idx < n_dense_layers:          # 0,1,2 → dense
        block = MlpBlock(...)
    else:                                   # 3..60 → MoE
        block = MoeBlock(...)
    register_op(block, prefix=f"layer_{layer_idx}_", suffix=f"_dev_{device_id}")

register_op(RMSNormHeadProj(...),
            prefix=f"layer_{n_layers}_",     # layer_61_
            suffix=f"_dev_{device_id}",
            retain_weights=True)
```

层布局一览（DSv3.2 默认超参）：

| layer_idx | block 类型 | prefix | 典型权重键（举例） |
| --- | --- | --- | --- |
| 0, 1, 2 | `MlpBlock`（dense） | `layer_0_` … `layer_2_` | `layer_0_gate_weights_dev_{d}` |
| 3 … 60 | `MoeBlock`（MoE） | `layer_3_` … `layer_60_` | `layer_3_exp_gate_weights_dev_{d}` |
| 61 | `RMSNormHeadProj`（head） | `layer_61_` | `layer_61_lm_head.weight_dev_{d}` |

> 小贴士：head 的前缀是 `layer_{n_layers}_`，即 `layer_61_`。这个编号恰好和离线转换器里 MTP 层的层号（`num_dense_layers + num_moe_layers = 3 + 58 = 61`）数值相同，但因为 head 的短别名（`lm_head.weight`、`model.norm.weight`）与 MTP 预处理层的短别名（`eh_proj_weights` 等）互不相同，键名不会冲突。MTP 层并不在 `Dsa` 里组装，它由独立的 MTP 模块加载。

#### 4.1.3 源码精读

层循环与 block 分发在 [tilert/models/deepseek_v3_2/modules/dsa.py:63-85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L63-L85)：循环按 `layer_idx < n_dense_layers` 选择 `MlpBlock` 或 `MoeBlock`，二者都接收同一组 MLA 相关参数（`mla_cls` / `mla_num_devices` / `mla_kwargs`）和可选的 `cached_ffn_ops`，然后用 `prefix=f"layer_{layer_idx}_"`、`suffix=f"_dev_{device_id}"` 注册。

head 的注册在 [tilert/models/deepseek_v3_2/modules/dsa.py:87-92](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L87-L92)：前缀用 `layer_{n_layers}_`（即 `layer_61_`），并显式传 `retain_weights=True`。

`cached_ffn_ops` 是一个可选的优化钩子：[dsa.py:58-64](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L58-L64) 允许外部传入预先构造好的 61 个 FFN 算子（每个 `Mlp` 或 `Moe`），跳过每张卡重复构造的开销；若不传则各 block 自行 new 一个。它带一个断言要求长度恰为 `n_layers`。

#### 4.1.4 MLA 类型选择：device_id == 0 与其余卡的差异

每层 block 内部都含一个 MLA 注意力，但 8 张卡用的 MLA 类型不一样。这个选择发生在 `Dsa.__init__` 进入循环之前：

[tilert/models/deepseek_v3_2/modules/dsa.py:34](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L34) —— `mla_cls = SparseSelectMlaV2 if device_id == 0 else PureMlaV2`。

- **卡 0** 用 `SparseSelectMlaV2`：它负责跑 NSA 稀疏索引、选出本轮要 attend 的 top-k 位置，再把选择结果广播给其余卡。为此它在 [dsa.py:39-47](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L39-L47) 预分配两个缓冲区并通过 `mla_kwargs` 传给 MLA：`peer_bufs`（记录其余 7 张卡接收缓冲地址的「通讯录」）和 `partial_buf`（汇聚部分结果的缓冲）。
- **卡 1..7** 用 `PureMlaV2`：它们只接收卡 0 广播的稀疏选择结果做真正的注意力，因此在 [dsa.py:48-52](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L48-L52) 只分配一个 `ll_buf`（接收选择结果的缓冲），尺寸由 `(num_mtp + 1) * index_topk * 2` 决定。

此外，[dsa.py:54-56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L54-L56) 还为非 0 卡设置 `mla_num_devices = num_devices - 1 = 7`：因为卡 0 的 MLA 是「1 卡自己一组」，而卡 1..7 的 MLA 是「7 卡一组」分摊头数，所以它们感知的设备数是 7 而不是 8。缓冲区指针的真正交换（把卡 1..7 的 `ll_buf` 地址回填到卡 0 的 `peer_bufs`）发生在更上层的 `ShowHandsDSALayer` 里，详见 [u2-l3](u2-l3-show-hands-dsa-layer.md)。

> 本讲只关注「Dsa 在构造每层 block 时如何挑选 MLA 类型并配上对应缓冲区」；MLA 内部算子链与稀疏选择细节留到 [u2-l6](u2-l6-mla-and-sparse-select.md)。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ModelArgs.n_dense_layers` 从 3 改成 5，`Dsa` 会构造几个 `MlpBlock`、几个 `MoeBlock`？head 的前缀会变成什么？

**参考答案**：`MlpBlock` 5 个（layer 0..4），`MoeBlock` 56 个（layer 5..60），head 前缀仍是 `layer_61_`（只取决于 `n_layers`，与 dense 层数无关）。

**练习 2**：为什么卡 1..7 的 `mla_num_devices` 是 7 而不是 8？

**参考答案**：卡 0 单独用 `SparseSelectMlaV2`（`num_devices=1`，自成一个组），卡 1..7 共用 `PureMlaV2` 并把这 7 张卡当作一个张量并行组来分摊注意力头数，因此它们感知的设备数是 `num_devices - 1 = 7`。

---

### 4.2 MlpBlock / MoeBlock 内部结构

#### 4.2.1 概念说明

无论是 dense 层还是 MoE 层，DeepSeek-V3.2 的每一个 transformer block 都遵循同一个骨架：**一个 MLA 注意力 + 一个前馈网络（FFN）**。两者的差别只在 FFN：

- dense 层的 FFN 是普通的全连接 MLP（`Mlp`）；
- MoE 层的 FFN 是混合专家（`Moe`）。

`MlpBlock` 与 `MoeBlock` 就是把「MLA + 对应 FFN」打包成一个小容器的胶水层。它们的代码几乎是镜像的，唯一区别是 FFN 字段是 `Mlp` 还是 `Moe`。

#### 4.2.2 核心流程

两个 block 的构造流程完全同构：

```text
block = MlpBlock / MoeBlock(model_args, device_id, num_devices, mla_cls, mla_num_devices, mla_kwargs, ffn)
  ├─ self.mla = mla_cls(...)          # MLA 注意力（SparseSelect 或 Pure）
  ├─ register_op(self.mla)            # 注册，prefix/suffix 默认空串
  ├─ self.mlp / self.moe = ffn or Mlp(...) / Moe(...)
  └─ register_op(self.mlp / self.moe) # 注册，prefix/suffix 默认空串
```

注意：**block 内部注册子算子时不传 prefix/suffix**（用默认空串）。这是因为位置信息（层号、卡号）已经由外层 `Dsa` 在注册 block 时通过 `prefix=f"layer_{i}_"`、`suffix=f"_dev_{d}"` 一次性给出，block 只负责把 MLA 和 FFN 这两个子树的短别名汇总上去。这一点是 4.3 节键名拼接能成立的关键。

FFN 内部则是两到三个融合算子的串联：

- `Mlp`（dense FFN，[mlp.py:13-35](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L13-L35)）：`RMSNormUpGateSiLU`（RMSNorm + up/gate 投影 + SiLU，算法固定为 `FP16MMA`）+ `DownAllReduce`（down 投影 + 跨卡 allreduce）。
- `Moe`（专家 FFN，[moe.py:19-49](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L19-L49)）：`RMSNormExpertProj`（专家门控打分）+ `ExpertSelectUpGateSiLU`（选专家 + up/gate + SiLU，算法 `BF16MMA`）+ `ExpertDownAllReduce`（专家 down 投影 + allreduce，算法 `BF16MMA`）。

可以用一张表对照两者的算子链：

| 阶段 | `Mlp`（dense） | `Moe`（专家） |
| --- | --- | --- |
| 门控/打分 | （无，直接进 RMSNorm） | `RMSNormExpertProj`（算专家分数） |
| up/gate 激活 | `RMSNormUpGateSiLU`（FP16MMA） | `ExpertSelectUpGateSiLU`（BF16MMA，含选专家） |
| down + 通信 | `DownAllReduce` | `ExpertDownAllReduce`（BF16MMA） |

#### 4.2.3 源码精读

`MlpBlock` 的结构与 MLA/MLP 注册见 [tilert/models/deepseek_v3_2/modules/mlp.py:38-75](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/mlp.py#L38-L75)：`mla_class` 与 `mla_nd` 都允许外部注入（默认回退到 `PureMlaV2` 与 `num_devices`），FFN 用外部传入的 `mlp` 或新建 `Mlp`，二者都 `register_op` 但不带 prefix/suffix。

`MoeBlock` 与之镜像，见 [tilert/models/deepseek_v3_2/modules/moe.py:52-85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L52-L85)。

`Moe` 内部三个融合算子的注册见 [moe.py:27-46](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L27-L46)，注意后两个算子在构造时就显式指定了 algorithm 枚举（`ExpertSelectUpGateSiLUAlgorithm.BF16MMA` 与 `ExpertDownAllReduceAlgorithm.BF16MMA`）——这决定了它们走哪个融合核，也决定了离线转换时调用算子的哪个 `convert_to_<algo>` 方法。

> 算子内部的权重别名、FP8 权重/scale 配对、专家选择细节是 [u2-l7](u2-l7-moe-mlp-ffn.md) 的主题，本讲只看 block 这一级的组装。

#### 4.2.4 代码实践

**实践目标**：验证 dense 层与 MoE 层的 FFN 算子链差异。

**操作步骤**：

1. 打开 `tilert/models/deepseek_v3_2/modules/mlp.py` 与 `moe.py`，并排对照 `Mlp` 与 `Moe` 的 `__init__`。
2. 分别数一数两者调用了几次 `self.register_op(...)`，记录每次注册的算子类名。

**需要观察的现象**：

- `Mlp` 注册 2 个算子（`RMSNormUpGateSiLU`、`DownAllReduce`）；
- `Moe` 注册 3 个算子（`RMSNormExpertProj`、`ExpertSelectUpGateSiLU`、`ExpertDownAllReduce`）。

**预期结果**：MoE 比 dense 多出一个「专家门控打分」阶段（`RMSNormExpertProj`），且 up/gate 与 down 阶段都换成了「专家版」算子。这与上表一致。

**待本地验证**：若你想确认每个算子实际持有的短别名，可在有后端的环境里构造 `Moe(ModelArgs(), device_id=0, num_devices=8)` 并打印 `moe.get_tilert_weights_alias()`（该调用需要先 `load_backend`，因为算子类在 import 链中可能触发后端注册）。

#### 4.2.5 小练习与答案

**练习 1**：`MlpBlock` 和 `MoeBlock` 在 `register_op(self.mla)` 时为什么不需要传 prefix/suffix？

**参考答案**：因为位置信息（`layer_{i}_` 与 `_dev_{d}`）已由外层 `Dsa` 在注册 block 时一次给定。block 内部所有子算子共享同一组 prefix/suffix，所以在 block 这一层用空串即可，短别名在 block 聚合后由 `Dsa` 统一套上外层 prefix/suffix。

**练习 2**：`Moe` 为什么比 `Mlp` 多一个 `RMSNormExpertProj`？

**参考答案**：MoE 需要先对每个 token 算「该路由到哪些专家」的分数（门控打分），这一步在 dense MLP 里不存在；`RMSNormExpertProj` 承担的就是这个专家路由打分职责，打分结果随后驱动 `ExpertSelectUpGateSiLU` 选专家。

---

### 4.3 prefix/suffix 键名拼接与权重匹配

#### 4.3.1 概念说明

这是本讲最关键的一节。TileRT 的权重加载是一个「字符串往返」问题：离线转换器把权重写进扁平 `state_dict`，运行时再从同一个 `state_dict` 取出来。两边必须用完全一致的键名规则。

统一键名模板是：

\[
\text{full\_key} \;=\; \underbrace{\text{layer}_{i}\_}_{\text{prefix}} \;\circ\; \underbrace{\text{alias}}_{\text{短别名}} \;\circ\; \underbrace{\text{\_dev}_{d}}_{\text{suffix}}
\]

即 `layer_{层号}_{短别名}_dev_{卡号}`，例如 layer 3 的专家门控权重在卡 5 上就是 `layer_3_exp_gate_weights_dev_5`。

- **写出端**（转换器）：遍历每层、每个算子的 `device_sharding` 输出，按 `f"layer_{layer_idx}_{param_name}_{dev}"` 命名落盘；
- **读回端**（运行时 `Dsa`）：用 `register_op(prefix=f"layer_{i}_", suffix=f"_dev_{d}")` 记录前缀后缀，加载时由基类 `init_tilert_weights` 按 `f"{prefix}{op_key}{suffix}"` 重构键名去 `state_dict` 取值。

#### 4.3.2 核心流程

读回端的匹配逻辑在 `SerializableTileRTModule.init_tilert_weights`（[base.py:320-341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L320-L341)）。它对 `exec_seq` 里的每个子算子做：

```text
for op, prefix, suffix, retain_weights in zip(exec_seq, prefix_seq, suffix_seq, retain_weights_seq):
    op_state_dict = {}
    for op_key in op.get_tilert_weights_alias():        # 子算子的所有短别名
        original_key = f"{prefix}{op_key}{suffix}"      # 重构扁平键名
        if original_key in state_dict:
            op_state_dict[op_key] = state_dict[original_key]
            if remove_selected:
                记录 original_key 待删除
    op.init_tilert_weights(op_state_dict)               # 把「短别名 → 张量」交给子算子
    if remove_selected and not retain_weights:
        删除已用键，释放显存
```

由于 `op.get_tilert_weights_alias()` 是递归聚合的，当 `op` 是一个 `MoeBlock` 时，它会把内部 MLA + Moe 所有叶算子的短别名汇总成一个长列表；外层 `Dsa` 给它们统一套上 `layer_3_` / `_dev_5`，就能精确命中 `state_dict` 里所有 `layer_3_*_dev_5` 的键。

**嵌套的传递性**：`MoeBlock` 自身的 `init_tilert_weights` 同样是这段逻辑，但它的 `prefix_seq` / `suffix_seq` 全是空串。于是它从（已被外层剥到只剩短别名的）`op_state_dict` 里按短名再分发一次给 `mla` 和 `moe`；`moe` 再分发给它的三个算子。整条链路里，**prefix/suffix 只在最外层 `Dsa` 出现一次**，内层全部用短名中转——这就是「位置感知隔离在外层，叶子算子只认短名」的设计。

**显存管理**：`Dsa` 构造时传 `remove_selected=True`（[dsa.py:23-28](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L23-L28)），所以每处理完一个 block 就把对应的 `layer_{i}_*_dev_{d}` 键从 `state_dict` 删掉，降低峰值显存；唯独 head 用 `retain_weights=True`，保留 `layer_61_lm_head.weight_dev_{d}` 等键不删。

#### 4.3.3 源码精读

**写出端（转换器）**：键名模板在 [tilert/models/preprocess/weight_converter.py:475-487](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L475-L487) 的 `__post_process_weights`：`new_key = f"layer_{layer_idx}_{param_name}_{dev}"`，其中 `dev` 形如 `dev_5`。例如 layer 3 的 MoE 转换在 [weight_converter.py:276-322](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L276-L322) 的 `transform_moe` 里把短别名 `exp_gate_weights` 等填进每卡字典，最终落盘成 `layer_3_exp_gate_weights_dev_5`。

**读回端（运行时）**：

- `register_op` 把四元组压进平行列表：[base.py:272-278](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L272-L278)。
- `init_tilert_weights` 按 `f"{prefix}{op_key}{suffix}"` 重构键名并分发：[base.py:320-341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L320-L341)。
- `Dsa` 在注册每个 block 时给出层号前缀与卡号后缀：[dsa.py:85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L85)。

#### 4.3.4 代码实践

**实践目标**：验证 layer 3 的 MoE 权重键名 `layer_3_exp_gate_weights_dev_{d}` 在 `register_op(prefix="layer_3_", suffix="_dev_{d}")` 下能被正确匹配，并用一个不依赖后端的小函数模拟这条「转换器写出 ↔ Dsa 读回」的拼接逻辑。

**操作步骤**：

1. 阅读 `weight_converter.py` 的 `__post_process_weights`（[L475-487](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L475-L487)）与 `transform_moe`（[L276-322](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L276-L322)），确认写出端键名 = `f"layer_{layer_idx}_{param_name}_{dev}"`。
2. 阅读 `base.py` 的 `init_tilert_weights`（[L320-341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L320-L341)），确认读回端键名 = `f"{prefix}{op_key}{suffix}"`。
3. 运行下面的「示例代码」（纯 Python，无需 GPU 与后端），模拟两端的字符串拼接并断言它们相等。

示例代码（非项目代码，仅用于演示拼接逻辑）：

```python
# 示例代码：模拟 dsa.py 与 weight_converter.py 的键名往返

NUM_DEVICES = 8
LAYER = 3                       # 一个 MoE 层
SHORT_ALIASES = [               # ExpertSelectUpGateSiLU 等算子的 tilert 短别名（节选）
    "exp_bias", "exp_gate_weights", "exp_gate_scales",
    "exp_up_weights", "exp_up_scales",
    "exp_down_weights", "exp_down_scales",
]

def converter_writes(layer_idx, short_alias, dev_id):
    # 对应 weight_converter.__post_process_weights 的 f"layer_{i}_{param}_{dev}"
    return f"layer_{layer_idx}_{short_alias}_dev_{dev_id}"

def dsa_reads(layer_idx, short_alias, dev_id):
    # 对应 register_op(prefix=f"layer_{i}_", suffix=f"_dev_{d}") + base.init_tilert_weights
    prefix = f"layer_{layer_idx}_"
    suffix = f"_dev_{dev_id}"
    return f"{prefix}{short_alias}{suffix}"

for dev_id in range(NUM_DEVICES):
    for alias in SHORT_ALIASES:
        written = converter_writes(LAYER, alias, dev_id)
        read = dsa_reads(LAYER, alias, dev_id)
        assert written == read, f"mismatch: {written} != {read}"

print("layer_3_exp_gate_weights_dev_5 =", dsa_reads(3, "exp_gate_weights", 5))
print("全部", len(SHORT_ALIASES) * NUM_DEVICES, "个键名往返一致 ✓")
```

**需要观察的现象**：程序无 `AssertionError` 地跑完，并打印出 `layer_3_exp_gate_weights_dev_5 = layer_3_exp_gate_weights_dev_5`。

**预期结果**：写出端与读回端产出的字符串逐字符相同，证明 `register_op(prefix="layer_3_", suffix="_dev_5")` 配合短别名 `exp_gate_weights` 能精确命中转换器写出的键 `layer_3_exp_gate_weights_dev_5`。

**待本地验证**：若想看真实短别名清单，可在加载了 `deepseek_v3_2` 后端的进程里执行：

```python
from tilert.models.deepseek_v3_2.modules.moe import Moe
from tilert.models.deepseek_v3_2.model_args import ModelArgs
print(Moe(ModelArgs(), device_id=0, num_devices=8).get_tilert_weights_alias())
```

#### 4.3.5 小练习与答案

**练习 1**：head 用了 `retain_weights=True`，而所有 transformer 层的 block 都没有传这个参数（默认 `False`）。这会导致加载行为有什么差别？

**参考答案**：`Dsa` 设了 `remove_selected=True`，每处理完一个子算子就会把匹配到的键从 `state_dict` 删除以释放显存。block 的 `retain_weights` 默认 `False`，所以 layer 0..60 的权重用完即删；head 的 `retain_weights=True` 使得 `layer_61_lm_head.weight_dev_{d}`、`layer_61_model.norm.weight_dev_{d}` 这些键在加载后被**保留**在 `state_dict` 里不删除。

**练习 2**：假设有人把 `Dsa` 里注册 block 的 suffix 从 `_dev_{device_id}` 误改成 `dev_{device_id}`（少了下划线），运行时会发生什么？

**参考答案**：读回端会去 `state_dict` 找 `layer_3_exp_gate_weightsdev5` 这样的键，而转换器写出的仍是 `layer_3_exp_gate_weights_dev_5`，于是 `original_key in state_dict` 恒为 `False`，`op_state_dict` 为空，叶算子拿不到权重。由于代码只在键存在时才填值（不会抛 KeyError），这类错误往往表现为「权重静默缺失」而非显式报错，调试时需要特别注意 prefix/suffix 与转换器的逐字符一致。

**练习 3**：为什么 block 内部的 `register_op` 可以全部省略 prefix/suffix，而不会导致不同层的权重互相覆盖？

**参考答案**：因为不同层、不同卡的隔离完全由外层 `Dsa` 的 prefix（`layer_{i}_`）与 suffix（`_dev_{d}`）承担。block 只是 `Dsa.exec_seq` 的一个元素，它的短别名经 `Dsa.init_tilert_weights` 套上外层 prefix/suffix 后才去匹配扁平 `state_dict`；block 自身的 `init_tilert_weights` 拿到的是已经被外层「剥」到只剩短别名的子字典，所以内部用空串即可，且不同层的数据早已在外层被分开。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「源码阅读 + 键名核对」的小任务：

**任务**：为 DeepSeek-V3.2 在卡 5 上绘制 `Dsa` 的算子树（只画到 block 一级即可），并写出 layer 3（第一个 MoE 层）在该卡上的 5 个代表性权重键名，最后用一短段文字说明这些键名是如何由 `Dsa` → `MoeBlock` → `Moe` 三级 `register_op` 协同生成的。

**建议步骤**：

1. **画树**：根节点 `Dsa(device_id=5)` 下挂 61 个 block（layer 0..2 为 `MlpBlock`，layer 3..60 为 `MoeBlock`）和 1 个 `RMSNormHeadProj`；每个 `MoeBlock` 下挂 `mla`（`PureMlaV2`，因为 device_id≠0）与 `moe`；`moe` 下挂三个融合算子。标注每个 `register_op` 用的 prefix/suffix（`Dsa` 层用 `layer_{i}_` / `_dev_5`，其余层为空串）。

2. **列键名**：从 `transform_moe`（[weight_converter.py:307-321](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L307-L321)）可读出 layer 3 在卡 5 的代表性键名，例如：
   - `layer_3_exp_gate_weights_dev_5`
   - `layer_3_exp_up_weights_dev_5`
   - `layer_3_exp_down_weights_dev_5`
   - `layer_3_exp_proj_weights_dev_5`
   - `layer_3_unproj_o_gamma_dev_5`

3. **解释生成链路**：写一段话说明——`Dsa` 在 [dsa.py:85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/dsa.py#L85) 以 `prefix="layer_3_"`、`suffix="_dev_5"` 注册 `MoeBlock`；`MoeBlock`（[moe.py:78-84](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L78-L84)）以空前缀后缀注册 `mla` 与 `moe`；`Moe`（[moe.py:27-46](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/modules/moe.py#L27-L46)）注册三个融合算子；加载时 `Dsa.init_tilert_weights` 把这些短别名套上 `layer_3_` / `_dev_5`，逐一命中转换器写出的扁平键。

**自检标准**：你列出的每个键名，都应该能拆成 `layer_3_` + 某个短别名 + `_dev_5` 三段，且短别名能在 `Moe`/`Mlp` 内某个叶算子的 `get_tilert_weights_alias()` 里找到。

## 6. 本讲小结

- `Dsa` 是一棵「for 循环 + `register_op`」拼出的算子树：前 `n_dense_layers`(=3) 层用 `MlpBlock`，其余 58 层用 `MoeBlock`，最后以 `layer_{n_layers}_` 前缀挂一个 `RMSNormHeadProj` head。
- `MlpBlock` 与 `MoeBlock` 结构镜像，都是「一个 MLA 注意力 + 一个 FFN」；差别仅在 FFN：dense 用两段算子的 `Mlp`，MoE 用含专家门控的三段算子的 `Moe`。
- 统一键名模板 `layer_{层号}_{短别名}_dev_{卡号}` 是离线转换器与运行时 `Dsa` 之间的契约；`register_op(prefix, suffix)` 负责在运行时侧重构这个模板。
- prefix/suffix 只在最外层 `Dsa` 出现一次，block 与算子内部全部用空串中转，位置隔离集中在外层、叶算子只认短名。
- `device_id == 0` 用 `SparseSelectMlaV2`（带 `peer_bufs`/`partial_buf`），卡 1..7 用 `PureMlaV2`（带 `ll_buf`，`mla_num_devices=7`），缓冲区指针的真正交换发生在更上层的 `ShowHandsDSALayer`。
- `remove_selected=True` 让每层权重用完即删以省显存，唯独 head 用 `retain_weights=True` 保留键不删。

## 7. 下一步学习建议

- 想深入 MLA 的稀疏选择与 0 卡广播机制，继续读 [u2-l6 MLA 注意力模块与稀疏选择](u2-l6-mla-and-sparse-select.md)，对照 `mla_v2.py` 中 `SparseSelectMlaV2` 与 `PureMlaV2` 的算子链。
- 想搞清 MoE/MLP 内部的 FP8 权重、专家选择与 allreduce，继续读 [u2-l7 MoE / MLP 前馈模块](u2-l7-moe-mlp-ffn.md)，重点看 `ExpertSelectUpGateSiLU` 的短别名与 `device_sharding`。
- 想看这棵 `Dsa` 树如何被打包成 `params/temp_vars/caches` 四元组并交给后端，回看 [u2-l3 ShowHandsDSALayer](u2-l3-show-hands-dsa-layer.md) 与 [u2-l5 三层张量执行契约](u2-l5-three-layer-tensor-contract.md)。
- 推荐源码阅读顺序：`dsa.py` → `mlp.py` / `moe.py` → `base.py` 的 `init_tilert_weights` → `weight_converter.py` 的 `__post_process_weights`，把「读回端」与「写出端」两端对照阅读，键名契约会变得非常直观。
