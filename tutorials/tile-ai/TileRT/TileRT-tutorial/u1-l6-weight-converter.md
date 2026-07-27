# 权重转换 weight_converter：从 HF checkpoint 到 8 卡分片

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **为什么** TileRT 必须把官方 HuggingFace 权重预先重排成「每卡一份」的布局，而不能直接加载原始 checkpoint。
- 看懂 `WeightConverter` 的三层主线：**按层分组加载 → 四类层 transform 委托给各算子的 `device_sharding` → 分片写出 + 生成新的 `index.json`**。
- 用一条命令 `python -m tilert.models.preprocess.weight_converter ...` 完成离线转换，并理解 `--test_mode` 在调试时的作用。
- 追踪一个具体的 HF 键名（如 `model.layers.{i}.mlp.gate.weight`）是如何变成 TileRT 键名（带 `layer_{i}_..._dev_{d}` 前缀/后缀）的。

> 本讲只讲「离线权重转换」这一步，不涉及运行时如何把这些分片加载进 8 张卡（那是 [u1-l5](u1-l5-generator-api-and-lifecycle.md) 里 `from_pretrained` / `ShowHandsDSALayer` 的职责，也是 [u2-l3](u2-l3-show-hands-dsa-layer.md) 多线程加载的主题）。

## 2. 前置知识

### 2.1 什么是 checkpoint / safetensors / index.json

大模型训练完，权重以 **checkpoint**（检查点）形式发布。HF 上一个典型 checkpoint 目录长这样：

```
DeepSeek-V3.2/
├── model.safetensors.index.json      ← 索引：张量名 → 所在分片文件
├── model-00001-of-000XX.safetensors  ← 一堆分片文件（每个几 GB）
├── model-00002-of-000XX.safetensors
└── ...
```

- **safetensors** 是一种高效的二进制张量存储格式，相比 pickle 更安全（不会执行任意代码）、加载更快。
- **index.json** 是一张「目录」：它告诉你每个张量（比如 `model.layers.0.mlp.gate.weight`）存放在哪个 `.safetensors` 分片文件里。读完它，你就能在不加载全部权重的前提下，按张量名定位到具体文件。

> 关键直觉：index.json 里记录的是 **HF 视角的命名与切分方式**。TileRT 转换的本质，就是读这张「旧目录」，重新切分、重新命名，再写出一张「新目录」。

### 2.2 为什么不能直接用 HF 权重

回忆 [u1-l1](u1-l1-project-overview.md)：TileRT 把模型权重分散在 8 张 B200 上协同计算，且为了让 tile 级运行时高效，每张卡需要一份 **已经按设备维度切好、并按算子内部布局重排过** 的权重。HF 的原始权重是「单机视角、按层平铺」的，有两个问题：

1. **没有按 8 卡切分**：例如一个形状为 `[N_experts, ...]` 的专家权重，需要沿专家维度拆给 8 张卡，每卡各拿一份。
2. **布局不匹配后端**：tile 级内核往往要求权重按特定的 tile / MMA 分块排布（FP8 矩阵乘需要 swizzle），否则运行时每次都要现场重排，抵消延迟优势。

因此 TileRT 选择 **离线一次性重排**，把结果存成新的 safetensors，运行时直接 `mmap` 加载。这正是本讲的主角 `weight_converter`。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| `device_sharding` | 每个算子类自带的方法：输入 HF 权重，输出「按 `num_devices` 切好」的张量，是转换的核心委托点。 |
| `ref_weights_alias` | 「参考/HF 侧」的权重别名列表——告诉转换器「我要从 HF state_dict 里取这几个键」。 |
| `tilert_weights_alias` | 「TileRT 侧」的权重别名列表——告诉转换器「切完后这几个张量在 TileRT 里叫什么名字」。 |
| dense 层 / MoE 层 / MTP 层 | 三种层类型：前 3 层是 dense（普通 MLP），中间 58 层是 MoE（混合专家），最后 1 层是 MTP（多 token 预测）。 |

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|------|------|----------------|
| [tilert/models/preprocess/weight_converter.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py) | 本讲主角：`WeightConverter` 类 + CLI 入口 | 全文 |
| [README.md](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md) | 给出转换命令与定位说明 | 第 131–158 行（Step 2） |
| [tilert/models/deepseek_v3_2/model_args.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py) | 模型超参，决定层结构（dense/MoE/MTP 边界、专家数） | `n_layers`、`n_dense_layers`、`n_routed_experts` |
| [tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py) | MoE 的「选专家 + up/gate + SiLU」融合算子 | 其 `device_sharding` 与权重别名 |
| [tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_down_allreduce.py) | MoE 的 down 投影 + allreduce 算子 | 其 `device_sharding`（两参数形式） |
| [tilert/models/deepseek_v3_2/ops/rmsnorm_up_gate_silu.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_up_gate_silu.py) | dense MLP 的 RMSNorm + up/gate + SiLU 算子 | 其 `device_sharding`（两参数形式） |
| [tilert/models/deepseek_v3_2/ops/rmsnorm_head_proj.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_head_proj.py) | 末层 RMSNorm + lm_head 投影算子 | 其 `device_sharding`（处理 head/norm） |

> 阅读建议：本讲以 `weight_converter.py` 为唯一主线，其他文件只在「理解 `device_sharding` 委托」时按需翻阅，不必逐行读完。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：

- **4.1 按层分组加载与索引文件**：怎么读 `index.json`、怎么把 HF 的扁平张量名归到「层」粒度。
- **4.2 四类层 transform 与 `device_sharding` 委托**：MLA / MLP / MoE / MTP 四类层各自怎么切。
- **4.3 分片写出与 `index.json` 生成**：切完怎么打包成 ≤5GB 的分片、怎么写出新索引。

### 4.1 按层分组加载与索引文件

#### 4.1.1 概念说明

HF checkpoint 的 `index.json` 是「张量名 → 文件名」的扁平映射，完全没有「层」的概念。但 TileRT 转换的核心调度单位是 **层**（一层一层来，每层独立调用对应的 transform）。所以第一步必须建立「层 → 这一层涉及哪些文件」的反向索引。

同时，有三类权重不属于任何一层：

- `model.embed_tokens.weight`（词嵌入）
- `model.norm.weight`（最终 RMSNorm）
- `lm_head.weight`（输出投影）

它们被单独拎出来，放进 `special_treated_params`，后续由专门的 `__process_embedding_weights` / `__process_head_weights` 处理。

#### 4.1.2 核心流程

```
读 model_dir/model.safetensors.index.json
        │
        ▼
对 weight_map 里每个 (param_name, file_name)：
        │
        ├── param 里含 "layers"？
        │       ├── 是 → 解析出 layer_num → 归入 files_by_layers["layer_{num}"]
        │       └── 否 → 放进 special_treated_params（embed/norm/head）
        ▼
得到 { "layer_0": {文件集合}, "layer_1": {文件集合}, ... }
     + special_treated_params = { embed: 文件, norm: 文件, head: 文件 }
```

层号是怎么从字符串里解析出来的？`WeightConverter` 假定 HF 命名形如 `model.layers.{N}.xxx`，于是用 `param.split(".")[2]` 取第 3 段当作层号。

#### 4.1.3 源码精读

构造函数里先根据 `model_args` 算出层结构，并决定 `target_layers`：

[weight_converter.py:51-L54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L51-L54) —— 由 `n_dense_layers`、`n_layers` 推出 dense / MoE / MTP 三段，`total_layers` 是它们的和。

以 DeepSeek-V3.2 为例，[model_args.py:62-L63](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L62-L63) 给出 `n_layers=61`、`n_dense_layers=3`，于是 dense=3、MoE=58、MTP=1，`total_layers=62`。

`test_mode` 只挑 3 层转换，用于快速验证管线：

[weight_converter.py:55-L58](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L55-L58) —— test 模式下 `target_layers = [0, 3, 61]`，正好各挑一个 dense（0）、一个 MoE（3）、一个 MTP（61）。

层号解析与分组：

[weight_converter.py:77-L84](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L77-L84) —— `__get_layer_num`：不含 `layers` 返回 `-1`（标记为特殊参数），否则取 `split(".")[2]` 为层号。

[weight_converter.py:86-L105](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L86-L105) —— `__group_by_layers`：读 index.json，把每个 param 归到 `layer_{num}` 这个桶（桶里存的是该层涉及的 **文件集合**，用 set 去重）；层号为 -1 的（embed/norm/head）丢进 `special_treated_params`。

> 注意桶里存的是「文件名集合」而非「张量名集合」。一层可能横跨多个 safetensors 文件，加载时会把这几个文件全读进来再合并。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在不下载几百 GB 真实权重的前提下，理解 `__group_by_layers` 的输出结构。
2. **操作步骤**：
   - 用 `Read` 工具或编辑器打开 [weight_converter.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py)，定位 `__group_by_layers`（第 86 行）。
   - 假设有一份极简的 `index.json`，其 `weight_map` 片段如下：

     ```json
     {
       "model.embed_tokens.weight": "model-00001-of-00005.safetensors",
       "model.layers.0.mlp.gate.weight": "model-00001-of-00005.safetensors",
       "model.layers.0.self_attn.q_a.weight": "model-00002-of-00005.safetensors",
       "model.layers.61.enorm.weight": "model-00005-of-00005.safetensors",
       "model.norm.weight": "model-00005-of-00005.safetensors",
       "lm_head.weight": "model-00005-of-00005.safetensors"
     }
     ```
   - 手动模拟 `__group_by_layers` 的遍历，预测输出。
3. **需要观察的现象**：`embed_tokens`、`norm`、`lm_head` 因不含 `layers` 被归入 `special_treated_params`；`layers.0.*` 的两个张量虽然分属两个文件，但都被归入同一个桶 `layer_0`，且桶里是 `{"model-00001...safetensors", "model-00002...safetensors"}`。
4. **预期结果**：

   ```
   files_by_layers = {
       "layer_0":  {"model-00001-of-00005.safetensors", "model-00002-of-00005.safetensors"},
       "layer_61": {"model-00005-of-00005.safetensors"},
   }
   special_treated_params = {
       "model.embed_tokens.weight": "model-00001-of-00005.safetensors",
       "model.norm.weight":         "model-00005-of-00005.safetensors",
       "lm_head.weight":            "model-00005-of-00005.safetensors",
   }
   ```

5. 若想用真实 checkpoint 验证，下载任意 HF 模型（不一定非要是 DSv3.2）后查看其 `model.safetensors.index.json` 即可——结构一致。**待本地验证**：不同模型发布者对 `layers` 的命名段位置可能不同（这里硬编码了 `[2]`），如果换了非标准 checkpoint 可能解析失败。

#### 4.1.5 小练习与答案

**练习 1**：如果 HF checkpoint 里某个张量名是 `model.layers.10.mlp.experts.3.gate_proj.weight`，`__get_layer_num` 返回什么？会被归入哪个桶？

**答案**：`"model.layers.10.mlp.experts.3.gate_proj.weight".split(".")` = `["model","layers","10","mlp","experts","3","gate_proj","weight"]`，第 2 个元素是 `"10"`，返回 `10`；归入桶 `layer_10`。

**练习 2**：为什么桶里存「文件集合」而不是直接存「张量 → 文件」映射？

**答案**：因为后续 `convert_a_layer` 是按层加载的，一层可能分散在多个文件里；先聚合出「这一层需要读哪几个文件」，加载时把这几个文件依次 `load_file` 后 `update` 合并即可，避免逐张量反复打开同一个文件。

---

### 4.2 四类层 transform 与 device_sharding 委托

#### 4.2.1 概念说明

每一层被加载后，要决定「切成什么形状」。TileRT 把这件事 **委托给算子自己**：每个算子类（如 `ExpertSelectUpGateSiLU`）都实现了一个 `device_sharding` 方法，它知道「我负责的这部分权重，应该怎么按 `num_devices` 切、怎么 swizzle」。`WeightConverter` 只是个调度器，它依次调用各算子的 `device_sharding`，把切好的张量按设备收集起来。

之所以这样设计，是因为「怎么切」高度依赖算子内部布局（FP8 MMA 的 swizzle、专家维度的切分点等），只有算子自己最清楚，集中写在 converter 里会变成几千行的意大利面。

一层的权重被拆成三类，分别由三个 transform 处理：

| transform | 何时调用 | 涉及算子 |
|-----------|----------|----------|
| `transform_mla` | **每一层都调**（注意力是公共组件） | `SparseSelectMlaV2`（0 卡）、`PureMlaV2`（其余卡） |
| `transform_mlp` / `transform_moe` | dense 层调 `mlp`，MoE 层调 `moe` | `RMSNormUpGateSiLU` + `DownAllReduce`（dense）；`ExpertSelectUpGateSiLU` + `ExpertDownAllReduce`（MoE） |
| `transform_mtp` | 仅最后 1 层（MTP 层） | `EHProjAllReduce` |

#### 4.2.2 核心流程

每一层在 `convert_a_layer` 里的统一编排：

```
convert_a_layer(layer_idx):
    加载这一层涉及的所有 safetensors 文件 → 合并成 weights_dict
    mla_weights  = transform_mla(weights_dict, layer_idx)        # 必做
    if layer_idx < n_dense_layers:
        mlp_weights = transform_mlp(weights_dict, layer_idx)     # dense
    else:
        mlp_weights = transform_moe(weights_dict, layer_idx)     # MoE
    if layer_idx >= n_dense_layers + num_moe_layers:              # 即最后一层
        mtp_weights = transform_mtp(weights_dict, layer_idx)
    return mla_weights, mlp_weights, mtp_weights
```

每个 transform 内部都遵循同一个套路：

1. **构造算子实例**（传入 `model_args`、`num_devices` 等）。
2. **取 HF 权重**：用算子的 `ref_weights_alias()`（或直接拼键名）从 `weights_dict` 里把所需张量挑出来。
3. **委托 `device_sharding`**：算子自己切成 `(num_devices, ...)` 的形状。
4. **按设备收集**：把切好的张量按 `dev_0` … `dev_7` 分发到结果字典。

> **两种 `device_sharding` 调用约定**（阅读源码时务必区分）：
> - **单参数**：`op.device_sharding(weights_map)`，返回 `dict[别名 → (num_devices,...) 张量]`。MLA 的 `SparseSelectMlaV2`/`PureMlaV2`、MoE 的 `ExpertSelectUpGateSiLU`、`RMSNormHeadProj` 用这种。
> - **双参数**：`op.device_sharding(weights_dict, key_prefix)`，返回「若干个 `(num_devices,...)` 张量」组成的 tuple。`ExpertDownAllReduce`、`DownAllReduce`、`RMSNormUpGateSiLU`、`EHProjAllReduce` 用这种。多出来的 `key_prefix` 是层作用域（如 `model.layers.3.mlp`），让算子能在完整 `weights_dict` 里自己拼键名。

#### 4.2.3 源码精读

**(a) `convert_a_layer`：一层的总编排**

[weight_converter.py:405-L435](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L405-L435) —— 先按 `files_by_layers[layer_N]` 加载并合并该层所有文件（第 412–420 行），再依次调 MLA、（dense/MoE 二选一的）FFN、（仅末层的）MTP。注意三种层类型的边界判断：`layer_idx < n_dense_layers` 是 dense，`layer_idx >= n_dense_layers + num_moe_layers` 是 MTP，中间是 MoE。

**(b) `transform_mla`：注意力层的不对称切分**

[weight_converter.py:243-L274](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L243-L274) —— 这里有一个 **关键的不对称**：

- **0 卡**（`dev_0`）用 `SparseSelectMlaV2` 且 `num_devices=1`（第 253 行），因为 0 卡额外承担「稀疏选择」职责，它单独切一份；
- **其余 7 卡**共享一个 `PureMlaV2` 且 `num_devices=7`（第 263 行），各取 `value[shard_idx]`。

这也是为什么后续运行时（见 [u2-l6](u2-l6-mla-and-sparse-select.md)）只有 0 卡需要算稀疏索引并广播。

**(c) `transform_moe`：MoE 层**

[weight_converter.py:276-L322](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L276-L322) —— MoE 层同时委托两个算子：

- `ExpertSelectUpGateSiLU`：负责「选专家 + up/gate + SiLU」，单参数 `device_sharding`，返回 dict（第 287–292 行）；
- `ExpertDownAllReduce`：负责 down 投影，**双参数** `device_sharding(weights_hf, "model.layers.{i}.mlp")`，返回 tuple（第 299–304 行）。

切完后按 `dev_id` 组装成每卡一份的字典（第 305–321 行）。

**(d) `transform_mlp` 与 `transform_mtp`**

[weight_converter.py:324-L378](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L324-L378) —— dense 层委托 `RMSNormUpGateSiLU` + `DownAllReduce`，都是双参数形式。

[weight_converter.py:380-L403](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L380-L403) —— MTP 层直接取 `enorm`/`hnorm` 两个 RMSNorm 权重，再委托 `EHProjAllReduce.device_sharding` 切 `eh_proj`。

**(e) `device_sharding` 长什么样（以 MoE 选专家算子为例）**

[expert_sel_up_gate_silu.py:468-L520](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L468-L520) —— 它把 1 个共享专家 + 256 个路由专家（见 [model_args.py:66](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L66)）的 gate/up 权重逐个按 `num_devices` 切，再沿专家维度 `torch.cat` 拼起来，最终返回一个 dict，键是 `tilert_weights_alias`（`exp_bias`/`exp_gate_weights`/…），值是形状 `(num_devices, ...)` 的张量。

与之配套的别名定义：

[expert_sel_up_gate_silu.py:51-L74](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L51-L74) —— `ref_tensor_alias`：HF 侧要取的键（`mlp.gate.e_score_correction_bias`、`mlp.shared_experts.gate_proj.weight`、`mlp.experts.{i}.gate_proj.weight` …）。

[expert_sel_up_gate_silu.py:77-L98](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/expert_sel_up_gate_silu.py#L77-L98) —— `tilert_tensor_alias`：TileRT 侧切完后的命名（`exp_bias`、`exp_gate_weights` …）。

#### 4.2.4 代码实践（键名映射追踪）

1. **实践目标**：追踪一个具体 HF 键名到 TileRT 键名的完整变换链。
2. **操作步骤**：先读下面这段 `transform_moe` 的核心片段：

   ```python
   # 示例代码：摘自 weight_converter.py:281-L322，已精简
   mlp_gate_weight = f"model.layers.{layer_id}.mlp.gate.weight"   # ← 这是「路由器」权重
   exp_sel_up_gate_silu = ExpertSelectUpGateSiLU(self.model_args, self.num_devices)
   exp_weights_map = {
       k: weights_hf[ref_scope + k] for k in exp_sel_up_gate_silu.ref_weights_alias()
   }
   exp_sharded = exp_sel_up_gate_silu.device_sharding(exp_weights_map)
   ```

   再追踪 `__post_process_weights`（[weight_converter.py:475-L487](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L475-L487)）把每个 param 重命名为 `layer_{layer_idx}_{param_name}_{dev}`。

3. **画出映射表**（这是本实践的重点，注意区分两个容易混淆的「gate」）：

   | HF 键名（来源） | 经过的算子 / 别名 | TileRT param_name | 最终 TileRT 键名（含 layer/dev） |
   |---|---|---|---|
   | `model.layers.{i}.mlp.gate.weight`（**路由器**，给专家打分） | 直接读，不经 `device_sharding` | `exp_proj_weights` | `layer_{i}_exp_proj_weights_dev_{d}` |
   | `model.layers.{i}.mlp.shared_experts.gate_proj.weight` + `model.layers.{i}.mlp.experts.{e}.gate_proj.weight`（e=0..255，共 257 个） | `ExpertSelectUpGateSiLU.ref_weights_alias` → `device_sharding` → `exp_gate_weights` | `exp_gate_weights` | `layer_{i}_exp_gate_weights_dev_{d}` |
   | `model.layers.{i}.mlp.gate.e_score_correction_bias` | 同上 → `exp_bias` | `exp_bias` | `layer_{i}_exp_bias_dev_{d}` |

4. **需要观察的现象（易错点）**：规格里常被简写成「`mlp.gate.weight` → `exp_gate_weights`」，但 **实际并非如此**。`mlp.gate.weight` 是 MoE **路由器**（router，一个 `[n_experts]` 维的打分向量），它被原样放进 `exp_proj_weights`；而 `exp_gate_weights` 是 257 个专家各自的 `gate_proj` 权重被拼起来再切分的结果。两者名字都含「gate」，含义完全不同。
5. **预期结果**：你能向别人讲清楚——`layer_3_exp_gate_weights_dev_0` 这一份权重，来自 257 个专家（1 shared + 256 routed）的 `gate_proj.weight` 沿专家维拼接后、属于 0 卡的那一片。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `transform_mla` 里要构造 **两个** MLA 算子实例（`SparseSelectMlaV2` 和 `PureMlaV2`），而不是一个 `num_devices=8` 的实例？

**答案**：因为 0 卡与其他 7 卡在 MLA 上的职责不同——0 卡要额外计算并保存稀疏选择索引（NSA），它的权重切分方式与只接收广播的其余卡不同，所以单独用一个 `num_devices=1` 的 `SparseSelectMlaV2` 处理 0 卡，再用 `num_devices=7` 的 `PureMlaV2` 统一处理其余卡。

**练习 2**：`convert_a_layer` 里判断「这一层是 dense 还是 MoE」的依据是什么？MTP 层又是怎么识别的？

**答案**：dense/MoE 用 `layer_idx < self.num_dense_layers`（DSv3.2 中 `n_dense_layers=3`，即 0/1/2 层是 dense，3–60 层是 MoE）；MTP 用 `layer_idx >= self.num_dense_layers + self.num_moe_layers`（即 `layer_idx == 61`，最后一层）。

---

### 4.3 分片写出与 index.json 生成

#### 4.3.1 概念说明

切完的权重按设备存在 `converted_weights_dict` 里：`{"dev_0": {...}, ..., "dev_7": {...}}`，外加一份全局的 embedding。但这些张量如果直接写成一个巨大的 safetensors 文件，会有两个问题：单文件过大不便于传输/校验，且加载时无法并行。所以要把它们切成若干 ≤5GB 的分片文件，并生成一张新的 `model.safetensors.index.json` 描述「每个 TileRT 张量在哪个分片里」。

这一步几乎与 HF checkpoint 的存储格式对称：**读进来是 HF 的 index+分片，写出去是 TileRT 的 index+分片**，只是命名与切分粒度变了。

#### 4.3.2 核心流程

```
__post_process_weights：把每个张量重命名为 layer_{i}_{param}_{dev}
__process_head_weights  ：处理 lm_head / model.norm（委托 RMSNormHeadProj）
__process_embedding_weights：单独取出 embedding（不按设备切，全局共享）
        │
        ▼
save_file_sharded(converted_weights_dict, "model.safetensors", max_shard_size="5GB"):
   ├── 第 1 个分片固定写 embedding（全局共享，不属任何 dev）
   ├── 依次遍历 dev_0 → dev_7：
   │     把每个张量塞进 current_shard，累加大小
   │     一旦超过 5GB → 落盘一个分片，开新的
   ├── 全部写完后，重命名分片为 -NNNNN-of-MMMMM.safetensors（总数对齐）
   └── 生成 model.safetensors.index.json：
        { "metadata": {"total_size": ...}, "weight_map": {张量名 → 分片文件名} }
```

#### 4.3.3 源码精读

**重命名：`__post_process_weights`**

[weight_converter.py:475-L487](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L475-L487) —— 对 MLA/MLP/MoE/MTP 切出的每个张量，统一套上 `layer_{layer_idx}_{param_name}_{dev}` 的命名模板，写入 `converted_weights_dict[dev]`。这个 `{dev}` 后缀（`dev_0`…`dev_7`）就是运行时 `load_device_weights` 按 `*_dev_{id}` 过滤的依据（见 [u2-l3](u2-l3-show-hands-dsa-layer.md)）。

**head 与 embedding 的特殊处理**

[weight_converter.py:437-L464](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L437-L464) —— `__process_head_weights`：把 `lm_head.weight` 和 `model.norm.weight` 委托给 `RMSNormHeadProj.device_sharding`（[rmsnorm_head_proj.py:161-L181](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/ops/rmsnorm_head_proj.py#L161-L181)），切分后键名为 `layer_{mtp_layer_idx}_lm_head.weight_dev_{d}` 和 `layer_{mtp_layer_idx}_model.norm.weight_dev_{d}`。注意它借用了 **MTP 层的层号**（`num_dense_layers + num_moe_layers` = 61）作为前缀。

[weight_converter.py:466-L473](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L466-L473) —— `__process_embedding_weights`：embedding 不按设备切，单独存进 `self.emb_weights_dict`。

**主循环：`to_tilert_weights`**

[weight_converter.py:489-L537](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L489-L537) —— 遍历 `target_layers`，逐层 `convert_a_layer` + `__post_process_weights`，最后处理 head/embedding，再调 `save_file_sharded`。

> 阅读小提示：第 527–530 行的 `sorted(...) + pprint` 只是把键名排序后 **打印** 出来供人查看，并不参与落盘；真正的写入顺序由 `save_file_sharded` 按 `dev_0..dev_7` 的插入顺序决定。

**分片落盘：`save_file_sharded`**

[weight_converter.py:133-L173](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L133-L173) —— 解析 `max_shard_size`（如 `"5GB"`），并 **固定把 embedding 当作第 1 个分片**（第 167–173 行，直接引用 `self.emb_weights_dict`，这是 converter 内部的一处隐式耦合：调用 `save_file_sharded` 前必须先跑过 `__process_embedding_weights`）。

[weight_converter.py:176-L207](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L176-L207) —— 依次遍历每个 device（`dev_0`…`dev_7`），逐张量累加，超过 5GB 就落盘开新片。所以最终分片布局是：`shard-1` = embedding，`shard-2..` = dev_0 的张量……dev_0 全部写完才轮到 dev_1。

[weight_converter.py:225-L241](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L225-L241) —— 生成 `model.safetensors.index.json`：`metadata.total_size` 是所有张量字节和，`weight_map` 是「TileRT 张量名 → 分片文件名」的映射。

**CLI 入口**

[weight_converter.py:648-L677](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L648-L677) —— `__main__`：解析 `--model_type`/`--model_dir`/`--save_dir`/`--test_mode`/`--append_mtp`，按 `model_type` 选 `ModelArgs`，构造 `WeightConverter(model_args, 8, ...)`——注意 **`num_devices` 硬编码为 8**（第 673 行），与 8× B200 硬件绑定。默认走 `to_tilert_weights()`，加 `--append_mtp` 则走增量追加（见下面小贴士）。

README 给出的官方用法（[README.md:131-L158](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L131-L158)）：

```bash
python -m tilert.models.preprocess.weight_converter \
  --model_type deepseek-v32 \
  --model_dir "/path/to/DeepSeek-V3.2" \
  --save_dir "/path/to/DeepSeek-V3.2-TileRT"
```

> **小贴士：增量追加 MTP。** 如果你之前已经用 `--test_mode` 之外的方式转好了 0–60 层、后来只想补上 MTP 层（第 61 层），可以用 `--append_mtp` 走 [weight_converter.py:539-L645](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L539-L645) 的 `append_mtp_weights_to_safetensors`，它只转换 MTP 层并把新分片合并进已有的 `index.json`，避免全量重转。

#### 4.3.4 代码实践（真机型，可选）

1. **实践目标**：跑通完整离线转换管线，并验证产出的 `index.json` 结构。
2. **操作步骤**：
   - 准备环境（见 [u1-l3](u1-l3-install-and-backend-loading.md)），转换 **不需要 GPU**（全程 `default_device = "cpu"`），但需要能装下整份权重的内存/磁盘。
   - 下载官方 DeepSeek-V3.2 HF checkpoint 到 `--model_dir`。
   - 先用 `--test_mode` 跑一次（只转 3 层，快）：

     ```bash
     python -m tilert.models.preprocess.weight_converter \
       --model_type deepseek-v32 \
       --model_dir "/path/to/DeepSeek-V3.2" \
       --save_dir "/tmp/dsv32-test" --test_mode
     ```

   - 打开 `/tmp/dsv32-test/model.safetensors.index.json`，检查 `weight_map`。
3. **需要观察的现象**：
   - `weight_map` 里应只包含 `layer_0_*`、`layer_3_*`、`layer_61_*` 三层（对应 test 模式的 `[0,3,61]`），外加 `model.embed_tokens.weight`。
   - 每个键名都带 `_dev_{0..7}` 后缀（embedding 除外）。
   - 文件名形如 `model.safetensors-00001-of-0000N.safetensors`。
4. **预期结果**：能在 `weight_map` 里找到形如 `layer_3_exp_gate_weights_dev_0` 的键，且它指向某个分片文件。
5. **待本地验证**：完整转换（不加 `--test_mode`）需要几百 GB 内存/磁盘与较长时间；如不具备条件，`--test_mode` 已足以验证管线正确性。

#### 4.3.5 小练习与答案

**练习 1**：`save_file_sharded` 为什么要把 embedding 单独作为第 1 个分片，而不是和 `dev_0` 的张量混在一起？

**答案**：embedding 是全局共享的词向量表，不属于任何一张设备，运行时所有卡都要查同一份；把它独立成片，加载时可以单独 `mmap` 给所有卡共享，逻辑上也更清晰。代码里它由 `__process_embedding_weights` 单独收集到 `emb_weights_dict`，与 `converted_weights_dict`（按设备分）是两套数据。

**练习 2**：如果将来硬件从 8 卡变成 16 卡，`weight_converter.py` 需要改哪里？

**答案**：CLI 入口里 `num_devices` 硬编码为 8（[第 673 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L673)），改这里即可让所有 `device_sharding` 按 16 切分；键名后缀会自动变成 `_dev_0`…`_dev_15`。但各算子内部 `device_sharding` 的切分点（如 `in_dim // num_devices`）是否仍对齐，需逐个核对，**待确认**。

## 5. 综合实践

把三个模块串起来，完成一次「键名侦探」任务：

**任务**：给定一份（哪怕极简的）HF `index.json` 和 `weight_converter.py` 源码，写一段 Python 脚本，**不调用** `WeightConverter`，而是手动模拟它对 **一个 MoE 层（layer_idx=3）** 的处理，预测该层转换后在 `converted_weights_dict["dev_0"]` 里会出现哪些键名。

**步骤**：

1. 用第 4.1 节的方法，从 `index.json` 里筛出 `model.layers.3.*` 的所有张量名，列出 `layer_3` 涉及的文件。
2. 对照 [transform_moe](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L276-L322)（第 276–322 行）和 [__post_process_weights](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/preprocess/weight_converter.py#L475-L487)（第 475–487 行），列出 `dev_0` 会收到的 `(param_name, 来源 HF 键)` 对照表。
3. 套上 `layer_3_{param_name}_dev_0` 模板，写出最终键名集合。
4. （可选）如果有真实 checkpoint，用 `--test_mode` 跑转换，然后 `python -c "import json; print(list(json.load(open('.../model.safetensors.index.json'))['weight_map']).__iter__().__next__())"` 或直接打开 index.json，核对第 3 步的预测是否出现在 `weight_map` 里。

**预期产出**：一份形如下表的映射（节选），能解释每一行的来源算子：

```
layer_3_exp_proj_weights_dev_0   ← model.layers.3.mlp.gate.weight（路由器）
layer_3_exp_gate_weights_dev_0   ← 257 个专家的 gate_proj.weight 拼接切片
layer_3_exp_bias_dev_0           ← model.layers.3.mlp.gate.e_score_correction_bias
layer_3_exp_down_weights_dev_0   ← 257 个专家的 down_proj.weight 拼接切片
layer_3_unproj_o_gamma_dev_0     ← model.layers.3.post_attention_layernorm.weight
...（MLA 相关键名见 transform_mla）
```

## 6. 本讲小结

- TileRT 必须把 HF 权重 **离线重排** 成「每卡一份 + 算子内部布局」的格式，原因是运行时追求极致延迟、不允许现场重排。`WeightConverter` 就是干这件事的。
- 转换主线三段式：**按层分组读 index.json → 每层委托各算子的 `device_sharding` 切分 → 按 ≤5GB 落盘 + 写新 index.json**。
- 每个算子自带 `device_sharding`、`ref_weights_alias`、`tilert_weights_alias` 三件套，把「怎么切」的知识封装在算子内部，converter 只是调度器；存在单参数（返回 dict）与双参数（返回 tuple）两种调用约定。
- 层分三类：dense（前 3 层，走 `transform_mlp`）、MoE（中间 58 层，走 `transform_moe`）、MTP（最后 1 层，走 `transform_mtp`）；MLA 每层都转，且 **0 卡与其余 7 卡不对称**。
- 键名统一模板 `layer_{i}_{param_name}_{dev_{d}}`，运行时按 `*_dev_{id}` 过滤就能拿到某张卡的权重；embedding 与 head/norm 走特殊路径。
- CLI 命令 `python -m tilert.models.preprocess.weight_converter --model_type ... --model_dir ... --save_dir ...`，`num_devices` 硬编码 8；`--test_mode` 只转三层用于快速验证管线。

## 7. 下一步学习建议

- 这些分片在运行时是怎么被 **8 卡并行** 加载的？继续读 [u2-l3 ShowHandsDSALayer：8 卡多线程权重加载](u2-l3-show-hands-dsa-layer.md)，看 `load_device_weights` 如何按 `*_dev_{id}` 过滤、`_init_weights` 如何用 `threading` 并行加载。
- 想理解算子三件套（`device_sharding` / `tilert_forward` / 权重别名）的全貌？读 [u3-l1 算子层设计](u3-l1-ops-layer-design.md)，那里系统讲解 ops 目录的统一骨架。
- 想知道转换出来的 `params/temp_vars/caches` 如何绑定进 C++ 后端？读 [u2-l5 三层张量执行契约](u2-l5-three-layer-tensor-contract.md)。
