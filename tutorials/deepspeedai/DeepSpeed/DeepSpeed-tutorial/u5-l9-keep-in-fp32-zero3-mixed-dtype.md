# ZeRO-3 混合 dtype：keep_in_fp32_modules 让点名参数留在 fp32

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「为什么 bf16 训练里有些张量必须留在 fp32」：不是精度洁癖，而是 bf16 的尾数位在特定数值区间根本分辨不开相邻的合法值（例如 GLM-5.2 路由 bias 的 256 个值在 bf16 下只剩 3~16 个不同值）。
2. 追踪 `data_types.keep_in_fp32_modules`（默认 `"auto"`）如何对接 transformers 的 `_keep_in_fp32_modules_strict` / `_keep_in_fp32_modules` 两张列表，`keep_tensors_in_fp32()` 如何在 `zero.Init` 收尾时就地把点名参数的 `ds_tensor` 分区转为 fp32。
3. 理解 ZeRO-3 优化器（`DeepSpeedZeroOptimizer_Stage3`）为支撑「一个模型、多种分区 dtype」所做的四处改造：按分区 dtype 拆子组、按累积 dtype 分平坦梯度缓冲、按通信 dtype 分 IPG 桶（含纯 fp32 桶的容量收缩）、按 dtype 拆分 all-reduce 取回。
4. 复述完整的能力判定与拒绝矩阵：什么配置下参数能保 fp32（仅 ZeRO-3 且无参数卸载），什么时候退化为「参数 cast 到训练 dtype + 告警」，什么时候直接 `ValueError` / `NotImplementedError`。

本讲对应 PR #8843，是把 `keep_in_fp32_modules` 从「只保 buffers」扩展到「参数与 buffers 都保」的完整机制解读。

## 2. 前置知识

- **bf16 的分辨间隔**：bf16 有 8 位指数、7 位有效尾数（含隐含 1）。在数值 \( v \) 附近，两个可分辨的 bf16 值至少相距 \( v \times 2^{-8} \)（相对分辨率 \( 2^{-7} \approx 0.78\% \)）。在 \( v = 8 \) 附近，间隔是 \( 8 \times 2^{-8} = 0.0625 \)。fp32 有 23 位尾数，同样位置间隔约 \( 8 \times 2^{-24} \approx 4.8 \times 10^{-7} \)。所以「值域正常但彼此太接近」的一组数，bf16 会整批舍到同一个网格点。
- **transformers 的两张 fp32 列表**：`_keep_in_fp32_modules_strict`（bf16 与 fp16 训练下都保 fp32）和 `_keep_in_fp32_modules`（仅 fp16 下保 fp32）。transformers 自己加载模型时会尊重它们；典型被点名的张量是 MoE 路由 bias `e_score_correction_bias`（DeepSeek-V3、GLM-4.5/GLM-5 等，buffer）、DeepSeek-V4 的超连接权重/attention sinks/norms、gpt-oss 的 attention sinks（参数）。
- **ZeRO-3 与 zero.Init**（见 u5-l2）：`zero.Init` patch `nn.Module.__init__`，子模块构造完成的瞬间就把参数切分成各 rank 的 `ds_tensor` 分区，`param.data` 变成 0 元素占位符。本讲的转换正是发生在这些分区上。
- **ZeRO-3 优化器的平坦缓冲**（见 u5-l6）：优化器把参数分区搬进连续平坦缓冲（`lp_param_buffers` / `fp16_partitioned_groups_flat`），梯度分区落在 `grad_partitions_flat_buffers`，反向梯度经 IPG 桶 reduce-scatter。#8843 之前这些结构都假设「单一 dtype」。
- **核心矛盾**：`zero.Init` 会把 `__init__` 里创建的每个浮点张量转成训练 dtype，之后 transformers 的 ZeRO-3 加载器把检查点值拷进这些张量——于是 transformers 的 fp32 列表在 ZeRO-3 下被无声丢弃。本讲讲的整套机制就是把它找回来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/keep_in_fp32.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py) | 模式匹配与就地转换的核心：`keep_in_fp32_pattern` / `tensors_to_keep_in_fp32` / `keep_tensors_in_fp32` |
| [deepspeed/runtime/zero/partition_parameters.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py) | `zero.Init`：读取配置、在 `_post_init_method` 收尾与 `module=` 两条路径上调用 `_keep_in_fp32`；`_all_gather_coalesced` 按 dtype 拆分 all-reduce 取回 |
| [deepspeed/runtime/zero/stage3.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py) | ZeRO-3 优化器的混合 dtype 支撑：子组拆分、平坦缓冲、IPG 桶、溢出检查、`offload_states` 拒绝 |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py) | 引擎侧能力判定 `_keeps_params_in_fp32`、混合精度 cast 时的保留、`model_dtype` 传递 |
| [deepspeed/runtime/config.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/config.py) | `data_types.keep_in_fp32_modules` 的解析与类型校验 |
| [docs/_pages/config-json.md](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/_pages/config-json.md) | 该配置项的用户文档（行为契约的权威表述） |
| [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py) | 全部行为的回归测试，也是本讲代码实践的蓝本 |
| [deepspeed/runtime/superoffload/superoffload_stage3.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/superoffload/superoffload_stage3.py) 与 [deepspeed/runtime/zenflow/engine_stage3.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zenflow/engine_stage3.py) | 两个 stage3 变体对多 dtype 的机械适配 |

## 4. 核心概念与源码讲解

### 4.1 keep_in_fp32 的模式匹配与就地转换（参数与缓冲）

#### 4.1.1 概念说明

这个模块回答两个问题：**哪些张量被点名**，以及**怎么转**。

配置入口是 `data_types.keep_in_fp32_modules`，三种取值：

- `"auto"`（默认）：用模型自己声明的 transformers 列表——`_keep_in_fp32_modules_strict`，加上 fp16 训练时的 `_keep_in_fp32_modules`；
- 一个模式字符串列表：`*` 代表任意字符，且模式可以匹配参数/缓冲全名的任意位置（与 transformers 的匹配规则一致）；
- `[]`：什么都不保（回到该选项存在之前的行为）。

模块文件头部的 docstring 把动机写得很直白，值得一读：GLM-5.2 的 `e_score_correction_bias` 值落在 5~20 之间、彼此相差不到 0.01，而 bf16 在该区间只能分辨 0.03~0.125 的差异，于是 256 个专家只剩 3~16 个不同值，**直接改变 token 被送去哪个专家**。

#### 4.1.2 核心流程

```
JSON data_types.keep_in_fp32_modules
        │
        ▼
keep_in_fp32_pattern(module, setting, dtype)   ← 编译出正则或 None
  · dtype 是 fp32 训练 → None（无需保留）
  · "auto"：遍历 module.named_modules()，
    收集每个子模块的 _keep_in_fp32_modules_strict（+ fp16 下的 _keep_in_fp32_modules）
    每个名字：'*'→'.*'，并加前缀 ^<module_name>\..*<pattern>
  · 列表：'{name}' → '{name 的 * 换成 .*}'
  · 无模式 → None
        │
        ▼
tensors_to_keep_in_fp32(module, pattern)       ← 枚举命中者
  遍历每个模块的本地参数与缓冲（recurse=False 逐层拼前缀），
  过滤 is_floating_point() 且 pattern.search(全名)
  产出 (全名, 张量, 是否 buffer) 列表
        │
        ▼
keep_tensors_in_fp32(module, pattern, include_params=True)   ← 就地转 fp32
  对每个命中张量：
    · 量化分区（有 ds_quant_scale）→ 保持量化，warning_once，跳过
    · 存储 dtype 已是 fp32 → 跳过
    · ZeRO-3 参数：ds_tensor.data = ds_tensor.data.float()
      （hpZ 的 ds_secondary_tensor 一并转；tensor.data 占位符也转，
       保证后续 gather/free 的 dtype 一致）
    · buffer：tensor.data = tensor.data.float()
  返回 (转换的参数数, 转换的缓冲数)
```

为什么转分区而不只是转 `param.data`？因为 ZeRO-3 参数的真数据躺在 `ds_tensor` 分区里，`param.data` 只是 0 元素占位符（gather 状态下才是全量张量）。只转占位符等于什么都没做；转了分区，后续 all-gather 出来的就是 fp32，检查点加载也会写入精确的 fp32 值。

#### 4.1.3 源码精读

配置解析与校验在 [deepspeed/runtime/config.py:559-571](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/config.py#L559-L571)：读取 `data_types` 块的 `keep_in_fp32_modules`，非 `"auto"` 且不是纯字符串列表时抛 `DeepSpeedConfigError`。

模式编译 [deepspeed/runtime/keep_in_fp32.py:39-62](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L39-L62)：训练 dtype 不是 fp16/bf16 直接返回 `None`；`auto` 模式遍历 `named_modules()` 收集两张 transformers 列表，每个名字按所属模块路径加前缀，最后 `re.compile("|".join(sorted(patterns)))`——注释指明这与 transformers 的 `core_model_loading.build_glob_alternation` + `re.search` 是同一条规则。

关键点：前缀是 `^{module_name}\..*{pattern}`。列表挂在哪个子模块上，就只匹配那个子模块子树里的名字——测试 `WrappedToyModel` 里 `other_child.inner.router_bias` 不被命中，正是因为列表挂在 `child` 上。

枚举与就地转换 [deepspeed/runtime/keep_in_fp32.py:65-120](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L65-L120)，其中 `keep_tensors_in_fp32` 的主体在 [L85-L120](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L85-L120)：

- [L97-L102](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L97-L102)：分区带 `ds_quant_scale`（`zero_quantized_nontrainable_weights` 存的是 int8 码 + scale）时保持量化并 `warning_once`——把 int8 码直接 `.float()` 得到的是码值而不是权重值，scale 也就对不上了。
- [L105-L115](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L105-L115)：`partition.data = partition.data.float()` 就地转分区；hpZ 场景下 `ds_secondary_tensor`（节点内第二副本）同步转换；最后 `tensor.data = tensor.data.float()` 转占位符或当前 gather 出来的全量张量，使下一次 gather 与 free 都保持 fp32。

用户文档对整个行为契约的表述在 [docs/_pages/config-json.md:1794-1798](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/_pages/config-json.md#L1794-L1798)。

#### 4.1.4 代码实践

1. **实践目标**：用 5 行代码亲眼看到 bf16 网格塌缩，再验证模式匹配规则。
2. **操作步骤**：
   - 在仓库根目录运行（纯 CPU，无需分布式）：

     ```bash
     python -c "
     import torch
     v = 8.0 + torch.arange(64, dtype=torch.float32) * 1e-4
     print(v.bfloat16().float().unique())   # 64 个值在 bf16 下剩几个？
     "
     ```

   - 再跑模式匹配单测：

     ```bash
     pytest tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestKeepInFp32Pattern -v
     ```

3. **需要观察的现象**：第一个命令输出只有一个值 `8.0`（或极少数几个）；第二个命令里 `test_auto_uses_lists_from_nested_modules` 等用例通过。
4. **预期结果**：`v` 彼此相差 1e-4，远小于 8.0 处 bf16 的间隔 0.0625，所以 64 个值全部舍到 8.0——这正是 `EXACT_VALUES`（[test_keep_in_fp32_modules.py:23-24](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L23-L24)）选择这组值的原因：任何 fp32 信息的丢失都会立刻表现为「值落在 bf16 网格点上」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `keep_in_fp32_pattern` 在 dtype 为 `torch.float32` 时返回 `None`？
**答案**：fp32 训练下所有张量本来就是 fp32，无需保留任何名单；返回 `None` 让所有调用方直接短路（见 `zero.Init._keep_in_fp32` 与引擎 cast 路径的判空）。

**练习 2**：`_keep_in_fp32_modules`（不带 `_strict`）的列表在什么条件下参与匹配？
**答案**：仅当训练 dtype 是 fp16 时（[keep_in_fp32.py:50-51](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L50-L51)）。bf16 指数位与 fp32 相同、动态范围完整，transformers 认为这些张量在 bf16 下不必保 fp32；fp16 会溢出，所以两张表都要。

**练习 3**：如果一个被点名的 ZeRO-3 参数同时开了 `zero_quantized_nontrainable_weights` 且被冻结，转换后它是什么形态？
**答案**：保持量化（int8 码 + `ds_quant_scale`），打印一条 `warning_once`；因为把码转成 fp32 并不能还原权重值，反而让 scale 失配。对应测试 `TestZeroInitKeepsQuantizedParams`。

### 4.2 zero.Init 的收尾转换钩子

#### 4.2.1 概念说明

转换的**时机**是这套机制里最讲究的部分。`zero.Init` 在每个子模块构造完成的瞬间就切分了它的参数，那 fp32 转换插在哪里？答案是两条路径：

1. **在线构建**（`with deepspeed.zero.Init(...): model = Model()`）：挂在 `_post_init_method` 末尾——即「模块自身 `__init__` 刚跑完」的钩子。对 transformers 模型，最外层模型 `__init__` 跑完时，它的 fp32 列表才完整（列表可能是 `__init__` 里设置的实例属性）、所有子模块已分区，而任何检查点还没加载——这是唯一能把「点名参数的分区」整体转 fp32 的窗口。
2. **已分配模型**（`deepspeed.zero.Init(module=model, ...)`）：`_convert_to_zero_parameters` 切分完直接调用。

参数卸载（`remote_device` 是 cpu/nvme）时只保 buffers：卸载分区躺在单一 dtype 的平坦 CPU/NVMe 缓冲里（见 4.3 的拒绝矩阵），无法逐参数换 dtype。

#### 4.2.2 核心流程

```
with zero.Init(config):                 │  zero.Init(module=model, config)
  Model.__init__ 逐层执行                │  _convert_to_zero_parameters(model)
  每个子模块 __init__ 结束 →             │        │
  _post_init_method(module):            │        ▼
    · 子参数 → _zero_init_param 分区     │  _keep_in_fp32(model)
    · _keep_in_fp32(module)  ←──────────┼──────┘
        pattern = keep_in_fp32_pattern(module, config 值, dtype)
        pattern 为 None → return
        params_offloaded = remote_device != local_device
        keep_tensors_in_fp32(module, pattern,
                             include_params=not params_offloaded)
        params_offloaded → warning（只保 buffers），每个 Init 进程只警告一次
```

注意 `_post_init_method` 对**每个**模块都会触发一次 `_keep_in_fp32`，但模式带了 `^<module_name>\.` 前缀，中间模块跑完时只会转换自己子树里「列表可见」的部分；最外层跑完时完成剩余的全量转换。幂等性由 `keep_tensors_in_fp32` 的「已是 fp32 就跳过」保证。

#### 4.2.3 源码精读

配置读取 [deepspeed/runtime/zero/partition_parameters.py:1079-1080](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1079-L1080)：`Init.__init__` 把 `_ds_config.keep_in_fp32_modules`（无配置时 `KEEP_IN_FP32_AUTO`）存到实例上。

已分配模块路径 [partition_parameters.py:1173-1177](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1173-L1177)：`_convert_to_zero_parameters` 之后紧接 `self._keep_in_fp32(module)`。

在线构建路径的收尾 [partition_parameters.py:1250-1253](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1250-L1253)：`_post_init_method` 在逐参数分区之后调用 `_keep_in_fp32(module)`，注释写明了时机约束——「transformers 模型的 fp32 列表在其自身 `__init__` 跑完后才完整，而此刻子模块已分区、检查点尚未加载」。

钩子本体 [partition_parameters.py:1255-1272](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1255-L1272)：

- `pattern` 为 `None` 直接返回（fp32 训练、`[]`、模型无列表的 `auto` 都走这里，零开销）；
- `params_offloaded = self.remote_device != self.local_device`（[L1161-L1165](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1161-L1165) 定义了这两个设备：remote_device 为 cpu/nvme 即视为卸载）；
- [L1263](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1263) 以 `include_params=not params_offloaded` 调用转换；
- [L1264-L1267](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1264-L1267) 卸载时 `logger.warning`（类级标记 `_warned_keep_in_fp32_offload` 保证一个进程只警告一次），并 `print_rank_0` 汇报转换数量。

#### 4.2.4 代码实践

1. **实践目标**：复现 `TestZeroInitKeepsParamsFp32`——验证 zero.Init 收尾后点名参数的**分区**是 fp32，且加载的精确值逐位存活。
2. **操作步骤**：在支持 bf16 的环境（CPU 加速器也支持，见 [cpu_accelerator.py:382-386](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/accelerator/cpu_accelerator.py#L382-L386)）运行：

   ```bash
   pytest tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestZeroInitKeepsParamsFp32 -v
   ```

   同时精读 [test_keep_in_fp32_modules.py:359-378](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L359-L378)：`_params_config(3, keep=...)` 建 ZeRO-3 配置，`zero.Init` 里构建 `ToyModelWithParams`（[L210-L221](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L210-L221)，`_keep_in_fp32_modules_strict = ["sink", "router_bias"]`），在 `GatheredParameters([model.sink], modifier_rank=0)` 里 `copy_(EXACT_VALUES)` 模拟 transformers 的 `_load_state_dict_into_zero3_model`。
3. **需要观察的现象**：`keep="auto"` 时 `_stored_dtype(model.sink) == torch.float32` 且 `_gathered(model.sink)` 逐位等于 `EXACT_VALUES`；`keep=[]` 时分区是 bf16、64 个值全部变成 8.0；两种情况下 `linear.weight` 分区都是 bf16。
4. **预期结果**：两组参数化全部通过。`_stored_dtype`（[test L280-L281](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L280-L281)）优先读 `ds_tensor.dtype`，这正是「转的是分区」的直接证据。hpZ 场景（`TestZeroInitKeepsParamsFp32Hpz`，world_size=4）验证第二副本 `ds_secondary_tensor` 也保住了 fp32 值。

#### 4.2.5 小练习与答案

**练习 1**：为什么转换不放在 `deepspeed.initialize` 里、而要放在每个模块 `__init__` 的收尾？
**答案**：transformers 的加载器在建模之后、`initialize` 之前就把检查点值拷进参数了。等到 `initialize` 再转 fp32，精确值已经被 bf16 舍过一次（8.01 → 8.0），无法恢复。收尾钩子保证「先转 dtype，再进数据」。

**练习 2**：`Init._warned_keep_in_fp32_offload` 为什么挂在类上而不是实例上？
**答案**：一个进程可能建多个模型/多次进出 `zero.Init`；卸载场景下告警是全局性的（参数分区注定单 dtype），类级标记避免每个模型重复刷屏，同时保留「至少警告一次」的可见性。

### 4.3 stage3 按分区 dtype 的子组与梯度缓冲

#### 4.3.1 概念说明

分区转成 fp32 只是第一步。ZeRO-3 优化器的整套簿记原本假设「一个模型一种 dtype」：参数组会被压平进单一 dtype 的平坦缓冲、梯度分区落进单一缓冲、`self.dtype` 直接取自参数。#8843 对 `DeepSpeedZeroOptimizer_Stage3` 做了四处结构性改造：

1. **训练 dtype 的判定**：`self.dtype` 不再「看参数是什么就是什么」——如果所有可训练参数恰好都是保真 fp32 参数（例如全冻结的 bf16 模型 + 点名的 fp32 参数），直接取参数 dtype 会把训练 dtype 误判成 fp32。现在引擎显式传 `model_dtype`；未传时取「第一个非 fp32 的参数 dtype，全 fp32 则取第一个」。
2. **子组按分区 dtype 拆分**：一个子组要压平进一块缓冲，一块缓冲只能一种 dtype——保真 fp32 参数自成子组。`lp_param_buffers` 变成 `Dict[torch.dtype, Tensor]`。
3. **梯度缓冲按累积 dtype 分桶**：`grad_partitions_flat_buffers` 变成 `Dict[torch.dtype, Tensor]`——保真参数的梯度在 fp32 缓冲里累积，其余参数用配置的 `gradient_accumulation_dtype`。
4. **参数卸载直接拒绝**：卸载路径的 CPU 平坦缓冲是单 dtype 的，多 dtype 时在布局阶段抛 `NotImplementedError`。

#### 4.3.2 核心流程

```
引擎 _configure_zero_optimizer
  └─ DeepSpeedZeroOptimizer_Stage3(..., model_dtype=bf16, gradient_accumulation_dtype=...)
        │
        ├─ self.dtype = model_dtype（或第一个非 fp32 参数 dtype）
        │
        ├─ _create_fp16_partitions_with_defragmentation
        │     └─ _create_fp16_sub_groups(参数组)
        │           └─ _split_by_partition_dtype：按 ds_tensor.dtype 分桶，
        │              每桶再按 sub_group_size 切 → 子组 = 一块缓冲一种 dtype
        │     ├─ 无参数卸载：lp_param_buffers[dtype] = defragment(该 dtype 的分区)
        │     │    fp16_partitioned_groups_flat 每段 narrow 自对应 dtype 的缓冲
        │     └─ 有参数卸载且分区 dtype > 1 种 → NotImplementedError
        │
        └─ _setup_for_real_optimizer
              ├─ IPG 桶按 dtype 定容（见 4.4）
              └─ grad_partitions_flat_buffers[dtype] = zeros(该桶参数的分区总量)
                   每个参数的梯度分区 = 对应缓冲的 narrow 视图
                   （__param_id_to_grad_partition 映射）
溢出检查 has_overflow：遍历全部 dtype 的梯度缓冲
```

#### 4.3.3 源码精读

训练 dtype 判定 [deepspeed/runtime/zero/stage3.py:234-239](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L234-L239)：`model_dtype` 为 `None` 时用生成器取第一个非 fp32 的参数 dtype。引擎侧在 [engine.py:2856](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2856) 显式传入（`model_dtype` 来自 `get_data_types()`，由 bf16/fp16 开关决定）。

子组拆分 [stage3.py:1259-1270](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1259-L1270)：`_split_by_partition_dtype` 按 `param.ds_tensor.dtype` 用 `defaultdict(list)` 分桶并以 `sort_dtypes`（按 dtype 名字符串排序，保证各 rank 顺序一致）返回；`_create_fp16_sub_groups` 发现多于一种 dtype 就递归地对每桶分别建组。

平坦参数缓冲按 dtype 分桶 [stage3.py:1000-1011](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1000-L1011)：`defragment` 一次只能处理一种 dtype，所以保真 fp32 参数拿到自己的缓冲；`lp_param_buffer` 指向训练 dtype 那块（不存在则第一块）。子组的平坦段由 [_set_fp16_partitioned_groups_flat, stage3.py:1357-1367](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1357-L1367) 从对应 dtype 的缓冲里 `narrow` 出来，每个 dtype 维护独立的写偏移。

参数卸载的拒绝 [stage3.py:1013-1016](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1013-L1016)：`_partition_dtypes()`（[L1656-L1657](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1656-L1657)，收集全部分区 dtype 的有序列表）多于一种且走卸载分支时抛 `NotImplementedError`——与 4.2 的「卸载只保 buffers」首尾呼应：Init 收尾时 `include_params=False`，根本不会产生 fp32 分区，这里是防御性兜底。

梯度缓冲按累积 dtype 分桶 [stage3.py:816-835](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L816-L835)：`_grad_accumulation_dtype(param)`（[L1651-L1654](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1651-L1654)）对保真参数返回 fp32、其余返回配置值；每个 dtype 一块 `torch.zeros` 缓冲（卸载时 pin），参数的梯度分区是各自缓冲的 `narrow` 视图，登记进 `__param_id_to_grad_partition`。`grad_partitions_flat_buffer` 指向主累积 dtype 那块。

溢出检查遍历全部缓冲 [stage3.py:3240-3253](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3240-L3253)：`has_overflow` 对 `grad_partitions_flat_buffers.values()` 逐块做 isinf/isnan 归约——注释点明「每个梯度 dtype 一块缓冲，保真参数有自己的那块」，漏查任何一块都会让 fp32 缓冲里的溢出逃过检查。

#### 4.3.4 代码实践

1. **实践目标**：跑通 `TestZero3TrainsKeptParamsInFp32` 的参数化矩阵，验证「一次小于 bf16 分辨率的更新在 fp32 参数中存活」，并理解断言为何这样写。
2. **操作步骤**：

   ```bash
   pytest "tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestZero3TrainsKeptParamsInFp32" -v
   ```

   重点精读 `_assert_kept_update_survives`（[test L336-346](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L336-L346)）与 `test_update_below_bf16_resolution_is_kept`（[L433-L445](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L433-L445)，参数化覆盖 `zero_init × contiguous × offload_optimizer × grad_accum` 共 16 种组合）。
3. **需要观察的现象**：`sink` 初始值全为 8.0，`lr=1e-3` 的 Adam 第一步把每个元素挪动约 1e-3；断言 `(forward_value - 8.0).abs().max() > 5e-4` 成立，且 `_gathered(sink)` 与 `safe_get_full_fp32_param(sink)` 逐位相等。
4. **预期结果**：全部通过。8.0 处 bf16 间隔 0.0625 ≫ 1e-3，若更新写进 bf16 权重会被舍回 8.0（`> 5e-4` 失败）——这个断言就是「参数真的以 fp32 参与更新」的数值证明。梯度侧的 `test_kept_param_gradient_is_fp32`（经 `_check_kept_param_gradient`，[L317-L333](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L317-L333)）用两个微批的 fp32 参考梯度与 `safe_get_full_grad` 做**逐位**相等比较：bf16 只有 8 位有效数字，归约或累加只要在 bf16 里做一次就不可能逐位相等。若在无 GPU 的 CI 上运行，SuperOffload 与量化用例会按 skipif 跳过（它们分别需要加速器），其余用例在 CPU 上可跑（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`_split_by_partition_dtype` 为什么要用 `sort_dtypes`（按名字符串排序）而不是直接迭代 dict？
**答案**：所有 rank 必须以相同顺序产出子组列表，否则各 rank 的子组编号错位、集合通信对不上。`sorted(dtypes, key=str)` 给出确定的全序（bfloat16 < float16 < float32 的字符串序）。

**练习 2**：如果一个模型的所有可训练参数都被点名保 fp32（比如全冻结 bf16 模型只训 fp32 的 LoRA 旁路），`self.dtype` 是什么？
**答案**：是引擎传入的 `model_dtype`（bf16）。若引擎没传，`next((d for d in param_dtypes if d != torch.float32), ...)` 会跳过 fp32 找到 bf16 的冻结权重——这正是注释「优化器的参数在全是保真参数时不显示训练 dtype」要处理的场景。

**练习 3**：`offload_states` 搬运 `lp_params` 时为什么直接拒绝多 dtype、而不是逐块搬？
**答案**：见 [stage3.py:4121-4126](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4121-L4126)：`lp_param_buffers` 有多块、`grad_partitions_flat_buffers` 也有多块时，搬 `lp_params`/`lp_grads` 抛 `NotImplementedError`（`hp_params` 有独立存储，可单独移动）。平坦搬运的簿记（单一 pin 缓冲 + `get_mapping_to_flat_buffer` 重定位）是按单缓冲设计的；与其静默搬错，不如显式拒绝并提示「设为 `[]` 或别把这些状态放进 include」。

### 4.4 按 dtype 的 IPG 桶与 all-reduce 取回拆分

#### 4.4.1 概念说明

梯度反向的路径也要认识 dtype：

- **IPG 桶（reduce 路径）**：stage3 为每种「通信 dtype」各建一个桶。保真参数的梯度用 **fp32** 归约——如果按惯例舍到 bf16 再归约，恰好抵消了保留 fp32 的意义（归约中的舍入与 bf16 累加都会吃掉那 1e-4 量级的差异）。
- **取回路径（fetch）**：`use_all_reduce_for_fetch_params` 开启时参数用 all-reduce 取回；一次 all-reduce 只能有一种 dtype，混合分桶时按 dtype 拆成多个子调用，包进 `MultipleAllGatherHandles`。

还有一个精巧的细节：**纯 fp32 桶可以缩小容量**。桶容量默认是 `reduce_bucket_size`；一个桶里如果**全部**参数都是保真参数，容量收缩到 `min(reduce_bucket_size, 这些参数的总元素数)`。混合桶不能这么做——原因写在了代码注释里：backward 中途抛异常会把桶里的梯度留到 `step()`，重试的 backward 会把同一批参数再加进桶，容量刚好等于参数总量会装满并在桶里出现同一参数两次。

#### 4.4.2 核心流程

```
反向：param.grad 就绪
  └─ reduce_independent_p_g_buckets_and_remove_grads(param)
       comm_dtype = get_param_comm_dtype(param)
         · 保真参数（self.dtype != fp32 且 ds_tensor.dtype == fp32）→ torch.float32
         · 其余 → 父类逻辑（communication_data_type / autocast / 参数自带 comm dtype）
       bucket = ipg_buckets[comm_dtype]        ← 按 dtype 选桶
       桶满（或跨进程组）→ __reduce_and_partition_ipg_grads(comm_dtype)
                            → 梯度分区写入对应 dtype 的 grad_partitions_flat_buffer

取回：use_all_reduce_for_fetch_params=True
  └─ _all_gather_coalesced(params, ...)
       dtypes = {p.ds_tensor.dtype for p in params}
       len(dtypes) > 1 → 按 dtype 递归拆分，每个 dtype 一块平坦缓冲 + 一次 all_reduce
                         → MultipleAllGatherHandles([...])
       len(dtypes) == 1 → 单块缓冲一次 all_reduce → AllReduceCoalescedHandle
```

#### 4.4.3 源码精读

三个判定函数 [stage3.py:1640-1654](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1640-L1654)：

- `_is_kept_in_fp32`：`self.dtype != torch.float32 and param.ds_tensor.dtype == torch.float32`——「训练 dtype 是低精度，而这个参数的分区是 fp32」即保真参数；
- `get_param_comm_dtype`：保真参数返回 `torch.float32`，注释直说「为归约把梯度舍到 bf16 会抵消保留它的意义」；
- `_grad_accumulation_dtype`：保真参数返回 fp32（决定它落在哪块梯度缓冲，见 4.3）。

IPG 桶的创建 [stage3.py:481-490](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L481-L490)：通信 dtype 集合 = autocast 场景下所有参数的 comm dtype（或配置的 `communication_data_type`），再并入每个参数的 `get_param_comm_dtype`；`ipg_buckets` 是 `Dict[torch.dtype, IPGBucketZ3]`。

桶容量收缩 [stage3.py:804-814](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L804-L814)：`contiguous_gradients` 开启时逐桶分配缓冲；纯保真参数的桶容量取 `min(reduce_bucket_size, sum(p.ds_numel))`，注释完整解释了「重试的 backward 会让参数进桶两次」的风险。

按 dtype 选桶的反向入口 [stage3.py:1662-1681](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1662-L1681)：`reduce_independent_p_g_buckets_and_remove_grads` 用 `get_param_comm_dtype` 定桶，跨进程组或桶满时按该 dtype flush；[__add_grad_to_ipg_bucket, L1716-L1739](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1716-L1739) 同样按 comm dtype 取桶并把梯度拷进桶缓冲。

all-reduce 取回的 dtype 拆分 [partition_parameters.py:1450-1460](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1450-L1460)：`_all_gather_coalesced` 在 `use_all_reduce_for_fetch_params` 且非量化路径下，发现桶内分区 dtype 多于一种时按 dtype 递归，产出一组 `MultipleAllGatherHandles`（每个 dtype 一块 fp32/bf16 平坦缓冲、一次 `dist.all_reduce`）。常规 all-gather 路径（[L1479-L1490](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1479-L1490)）同样按 `allgather_dtype` 用 `defaultdict` 分桶后逐 dtype 收集。

#### 4.4.4 代码实践

1. **实践目标**：验证 all-reduce 取回路径在混合 dtype 下正常训练（`test_all_reduce_fetch`），并从源码确认「fp32 与 bf16 分区各走一次集体通信」。
2. **操作步骤**：

   ```bash
   pytest "tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestZero3TrainsKeptParamsInFp32::test_all_reduce_fetch" -v
   ```

   然后对照 [partition_parameters.py:1451-1460](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1451-L1460) 阅读：`_params_config(3, allreduce_fetch=True)` 打开的就是 `zero_optimization.use_all_reduce_for_fetch_params`（[test L246-L248](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L246-L248)）。
3. **需要观察的现象**：训练 2 步后 `(_gathered(engine.module.sink) - 8.0).abs().max() > 5e-4`——更新在 fp32 参数上存活。
4. **预期结果**：通过。混合 dtype 桶被拆成「bf16 一次 all_reduce + fp32 一次 all_reduce」，两次句柄都收进 `MultipleAllGatherHandles`，对外透明。若想进一步看桶的数量，可在 `_setup_for_real_optimizer` 后打印 `{d: b.buffer.numel() for d, b in engine.optimizer.ipg_buckets.items()}`，应看到 bf16 与 fp32 两个键（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：把保真参数的梯度按 `communication_data_type`（例如 bf16）归约会发生什么？
**答案**：梯度先被舍到 bf16 网格（8.0 附近间隔 0.0625），归约后的和再舍一次——两个微批间 1e-4 量级的差异被彻底抹掉，`test_kept_param_gradient_is_fp32` 的逐位相等断言会失败。这就是 `get_param_comm_dtype` 对保真参数强制返回 fp32 的原因。

**练习 2**：为什么纯 fp32 桶可以按参数总量收缩容量，而「fp32+bf16 混住的桶」不能？
**答案**：桶容量只约束「何时触发 reduce」。混合桶保持 `reduce_bucket_size` 是因为异常中断的 backward 会把梯度留在桶里直到 step()，重试的 backward 会把同一批参数再次入桶；容量等于参数总量会让桶立即装满并出现重复参数。而纯保真参数的桶……其实同样面临重试，但由于保真参数通常很少（路由 bias、sink 等），收缩后桶也只是变小，重复入桶的空间仍由「桶满才 reduce」的触发条件兜底——代码注释给出的正是这层权衡，混合桶保守、纯桶激进。

**练习 3**：`MultipleAllGatherHandles` 里的多个句柄是串行还是并行完成的？
**答案**：每个子调用各自发起 `async_op=True` 的集合通信后立即返回句柄，多个 dtype 的 all_reduce 在各自流上重叠执行，等待时统一 join——这正是拆分成多句柄而非串行拼接的意义。

### 4.5 引擎侧能力判定与拒绝矩阵

#### 4.5.1 概念说明

最后一环是引擎的「能力判定」：**只有 ZeRO-3 且无参数卸载**的配置才能让点名参数留在 fp32。原因在 `BF16_Optimizer`、ZeRO-1/2 优化器里，每个优化器参数组被压平成**一块单 dtype 缓冲**；卸载的 ZeRO-3 模型分区也躺在单 dtype 的 CPU/NVMe 缓冲里。这些路径遇到「点名参数」只能把它 cast 到训练 dtype：

- `"auto"` 模式：`logger.warning`（模型自己的列表，用户没有显式要求，不该硬失败）；
- 显式列表：直接 `ValueError`（用户明确点名了参数，静默降级等于撒谎）。

buffers 在**所有**配置下都保 fp32（它们从不参与优化器压平）。

完整的拒绝矩阵：

| 配置 | 点名参数 | 点名 buffers |
| --- | --- | --- |
| ZeRO-3，无参数卸载 | **fp32 保留**，梯度 fp32 归约累积 | fp32 |
| ZeRO-3 + `offload_param`（cpu/nvme） | 训练 dtype，Init 收尾告警一次 | fp32 |
| ZeRO-0/1/2，或 bf16/fp16 优化器路径 | 训练 dtype；auto 告警 / 显式列表 `ValueError` | fp32 |
| ZeRO-3 多 dtype + `offload_states(include=[lp_params 或 lp_grads])` | `NotImplementedError`（`hp_params` 可单独移动） | 同左 |
| 冻结参数 + `zero_quantized_nontrainable_weights` | 保持 int8 量化，`warning_once` | — |

两个 stage3 变体做了机械适配：SuperOffload 覆写 `_create_fp16_sub_groups` 先按 dtype 拆分；zenflow 的 `partition_grads` 经由按 dtype 的 `__param_id_to_grad_partition` 把每个梯度分区落进自己 dtype 的缓冲。

#### 4.5.2 核心流程

```
引擎 _configure_distributed_model（fp16/bf16 启用时）
  keep_pattern = keep_in_fp32_pattern(module, config.keep_in_fp32_modules, param_dtype)
  _cast_module_mixed_precision(param_dtype, buffer_dtype, is_zero_init_model,
                               keep_pattern, keep_params=_keeps_params_in_fp32())
      │
      ├─ kept = 点名张量集合（buffers，或 buffers+参数）
      ├─ 非 zero-init：逐参数 cast 到 param_dtype，跳过 kept
      ├─ keep_tensors_in_fp32(...)：点名张量上转 fp32
      │    （zero-init 路径下这一步转的是分区——引擎是分区转换的第二道防线）
      ├─ keep_params=False 且点名命中了参数 → _warn_listed_params_not_kept
      │    · 显式列表 → ValueError
      │    · auto → logger.warning
      └─ buffer_dtype cast 跳过 kept
```

`_keeps_params_in_fp32()` = `zero_optimization_partition_weights()`（即 stage 3）且 `offload_param.device` 不是 cpu/nvme。它是整套判定的单一事实来源：`zero.Init` 侧的卸载告警（4.2）与引擎侧的告警/报错（本节）都源于同一条物理约束。

#### 4.5.3 源码精读

判定函数 [engine.py:1927-1936](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1927-L1936)：docstring 写明「只有无参数卸载的 ZeRO-3 为每种 dtype 持有一块分区缓冲；ZeRO-1/2 与 bf16/fp16 优化器把每个参数组压平成单 dtype 缓冲，卸载的 ZeRO-3 分区躺在单 dtype 的 CPU/NVMe 缓冲里」。

cast 主流程 [engine.py:1885-1925](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1885-L1925)：

- [L1896-L1902](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1896-L1902) 计算 `kept` 集合（`tensors_to_keep_in_fp32` 命中的 buffers，`keep_params` 为真时含参数）；
- [L1906-L1911](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1906-L1911) 非 zero-init 路径逐参数 cast 到训练 dtype，`kept` 跳过；
- [L1913-L1917](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1913-L1917) 再调 `keep_tensors_in_fp32` 上转点名张量（zero-init 路径下参数已在 4.2 转过分区，这里幂等；非 zero-init 的 ZeRO-3 由**这里**完成转换——引擎是第二道防线），`keep_params=False` 时进入告警分支；
- [L1920-L1925](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1920-L1925) `buffer_dtype` cast 同样跳过 `kept`。

告警与拒绝 [engine.py:1938-1947](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1938-L1947)：`_warn_listed_params_not_kept` 收集被点名却没被保留的参数名；显式列表（`!= KEEP_IN_FP32_AUTO`）`raise ValueError`，auto 模式 `logger.warning`。

调用点 [engine.py:2149-2161](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L2149-L2161)：`_configure_distributed_model` 在 fp16/bf16 启用时构造 `keep_pattern` 并传入 `_keeps_params_in_fp32()` 的结果。

`offload_states` 的拒绝 [stage3.py:4121-4126](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4121-L4126)：`lp_param_buffers` 或 `grad_partitions_flat_buffers` 多于一块时，搬运 `lp_params`/`lp_grads` 抛 `NotImplementedError`，报错信息直接给出出路（设 `[]` 或把这些状态排除出 include）。

SuperOffload 的适配 [superoffload_stage3.py:63-68](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/superoffload/superoffload_stage3.py#L63-L68)：覆写的 `_create_fp16_sub_groups` 先调 `_split_by_partition_dtype`，多 dtype 时按桶递归——注释明说「像基类那样先按 dtype 拆，保持下面的簿记按子组进行」。zenflow 侧 [zenflow/engine_stage3.py:457-489](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zenflow/engine_stage3.py#L457-L489) 的 `partition_grads` 通过 `__param_id_to_grad_partition`（现在是按 dtype 的缓冲映射）逐参数取到自己 dtype 的缓冲视图，`grad_partition.to(grad_buffer.dtype)` 对 fp32 缓冲是无损直拷。

#### 4.5.4 代码实践

1. **实践目标**：亲手复现拒绝矩阵的三个边界：ZeRO-2 下的 cast+告警/报错、参数卸载下仅保 buffers、`offload_states` 的 `NotImplementedError`。
2. **操作步骤**：

   ```bash
   pytest tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestStagesBelowThreeKeptParams -v
   pytest "tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestZero3TrainsKeptParamsInFp32::test_param_offload_keeps_only_buffers" -v
   pytest "tests/unit/v1/half_precision/test_keep_in_fp32_modules.py::TestZero3TrainsKeptParamsInFp32::test_checkpoint_round_trip" -v
   ```

   - `TestStagesBelowThreeKeptParams`（[test L523-L548](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L523-L548)）：stage 0/1/2 参数化验证 auto 模式下 `sink` 被 cast 到 bf16、`router_bias` 仍是 fp32；`test_explicit_parameter_pattern_rejected` 用 `keep=["sink"]` 验证 `ValueError`（注意它用 try/except 而不是 `pytest.raises`，注释解释了 DistributedTest worker 池会丢弃 BaseException 导致挂起）。
   - `test_param_offload_keeps_only_buffers`（[L477-L484](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L477-L484)）：`offload_param=True` 时 `_stored_dtype(sink) == torch.bfloat16` 而 `router_bias.dtype == torch.float32`。
   - `test_checkpoint_round_trip`（[L460-L475](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L460-L475)）：保存→重建→加载后 gather 值与 fp32 master 逐位相等。
3. **需要观察的现象**：stage 0/1/2 用例里参数变 bf16、buffer 留 fp32；显式列表用例抛出含 `keep_in_fp32_modules` 字样的 ValueError；卸载用例训练正常完成且只有 buffer 保住 fp32。
4. **预期结果**：全部符合矩阵。`offload_states` 的 `NotImplementedError` 在本测试文件中没有直接用例，属于「待本地验证」项：可参考 [stage3.py:4121-4126](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4121-L4126) 的判定条件，在 `TestZero3TrainsKeptParamsInFp32` 的脚手架上加一句 `engine.optimizer.offload_states(include=[OffloadStateTypeEnum.lp_params])` 验证报错（示例代码，非项目原有测试）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 auto 模式只告警、显式列表却报错？
**答案**：auto 的列表来自模型（transformers 类属性），用户未必知道它的存在，硬失败会把大量既有配置挡在门外；显式列表是用户亲手写的意图，静默把它降到训练 dtype 会让用户误以为参数真的在 fp32 里训练。**失败要响亮，降级要可见**。

**练习 2**：ZeRO-2（`DeepSpeedZeroOptimizer`）为什么结构上保不住 fp32 参数？
**答案**：它把每个优化器参数组的 fp32 主权重压平成单块平坦缓冲（u5-l3 讲过的 `single_partition_of_fp32_groups`），一块缓冲一种 dtype；要保 fp32 参数就得把整组拆成两种缓冲，等于重做 stage3 的这套改造——所以引擎直接判定「不可保留」。

**练习 3**：`_cast_module_mixed_precision` 里为什么 zero-init 路径跳过逐参数 cast、却仍调用 `keep_tensors_in_fp32`？
**答案**：zero-init 下参数在 `zero.Init` 里已经按配置 dtype 分区完毕，再逐参数 cast 反而会破坏分区占位符结构；但**非 zero-init 构建 + ZeRO-3 训练**（先建普通模型再 `deepspeed.initialize`，优化器构造时才切分）的点名参数转换只能由引擎完成——`keep_tensors_in_fp32` 此时作用在完整张量上（此时还没有 `ds_tensor`，转换的就是 `tensor.data` 本身），两条构建路径因此都能得到 fp32 参数。

## 5. 综合实践

把整条链路串成一个可复用的实验脚本（示例代码，基于 `ToyModelWithParams` 改写，建议放在仓库外运行）：

```python
# keep_fp32_e2e.py —— 贯穿：模式匹配 → zero.Init 收尾转换 → 混合 dtype 训练 → 检查点往返
import torch, deepspeed
from tests.unit.v1.half_precision.test_keep_in_fp32_modules import ToyModelWithParams, EXACT_VALUES, HIDDEN

config = {
    "train_micro_batch_size_per_gpu": 1,
    "gradient_accumulation_steps": 2,
    "optimizer": {"type": "Adam", "params": {"lr": 1e-3}},
    "zero_optimization": {"stage": 3, "stage3_param_persistence_threshold": 0},
    "bf16": {"enabled": True},
    "data_types": {"keep_in_fp32_modules": "auto"},   # ① auto 命中 sink + router_bias
}

with deepspeed.zero.Init(config_dict_or_path=config):    # ② 收尾钩子把 sink 分区转 fp32
    model = ToyModelWithParams()
with deepspeed.zero.GatheredParameters([model.sink], modifier_rank=0):
    model.sink.data.copy_(EXACT_VALUES)                  # ③ 模拟检查点加载（精确 fp32 值）

engine, _, _, _ = deepspeed.initialize(config=config, model=model,
                                       model_parameters=list(model.parameters()))
opt = engine.optimizer
print("partition dtypes:", opt._partition_dtypes())      # ④ 应为 [bfloat16, float32]
print("sub groups per dtype:", [ [p.ds_id for p in g] for g in opt.fp16_groups ][:4])
print("grad buffers:", {d: b.numel() for d, b in opt.grad_partitions_flat_buffers.items()})

x = torch.randn(1, HIDDEN, device=engine.device, dtype=torch.bfloat16)
for _ in range(4):                                       # ⑤ 混合 dtype 训练 2 步（gas=2）
    loss = engine(x); engine.backward(loss); engine.step()
with deepspeed.zero.GatheredParameters([engine.module.sink]):
    print("survived:", (engine.module.sink.data.float() - 8.0).abs().max())  # 应 > 5e-4
```

执行（单机双进程，bf16 在 CPU 加速器上可用；GPU 环境把命令换成 torchrun 等价形式）：

```bash
pytest --no-header -q -s 2>/dev/null || true   # 仅示意；实际用下面方式拉起双进程：
deepspeed --num_gpus 2 keep_fp32_e2e.py 2>/dev/null || torchrun --nproc_per_node=2 keep_fp32_e2e.py
```

**验收清单**（每项对应本讲一个模块）：④ 打印出两种分区 dtype 与分 dtype 的梯度缓冲（4.3）；训练无 NaN 且 ⑤ 的偏移大于 5e-4——小于 bf16 分辨率的更新存活（4.3/4.4）；`engine.module.router_bias.dtype == torch.float32`（4.1/4.2）。有条件时再接 `engine.save_checkpoint` / 新引擎 `load_checkpoint` 往返（对照 `test_checkpoint_round_trip`），以及把 `stage` 改成 2 观察 ValueError（4.5）。运行结果待本地验证。

## 6. 本讲小结

- **数值根因**：bf16 在 \( v \) 附近的可分辨间隔是 \( v \times 2^{-8} \)；GLM-5.2 路由 bias 的值彼此相差 < 0.01，bf16 直接改变 token 路由。`data_types.keep_in_fp32_modules`（默认 `"auto"`）对接 transformers 的 `_keep_in_fp32_modules_strict` / `_keep_in_fp32_modules`。
- **转换在分区上、在收尾时**：`zero.Init` 在每个模块 `__init__` 跑完（列表完整、子模块已分区、检查点未加载）调用 `keep_tensors_in_fp32`，就地把点名参数的 `ds_tensor`（及 hpZ 的 `ds_secondary_tensor`）与 buffers 转 fp32；量化分区保持量化并告警。
- **stage3 的四处结构性改造**：训练 dtype 由引擎传入（全保真参数也不误判）；子组按分区 dtype 拆分（`_split_by_partition_dtype`，一子组一缓冲一 dtype）；梯度缓冲按累积 dtype 分桶（保真参数在 fp32 缓冲累积）；溢出检查遍历全部 dtype 缓冲。
- **通信路径认 dtype**：保真参数的梯度用 fp32 归约（`get_param_comm_dtype`），纯 fp32 IPG 桶可按参数总量收缩容量；all-reduce 取回按 dtype 拆成 `MultipleAllGatherHandles`。
- **能力判定与拒绝矩阵**：仅 ZeRO-3 且无参数卸载可保参数 fp32；其余 cast 到训练 dtype（auto 告警 / 显式列表 ValueError）；参数卸载仅保 buffers；`offload_states` 对多 dtype 的 `lp_params`/`lp_grads` 抛 NotImplementedError；superoffload/zenflow 按同一套拆分做了适配。

## 7. 下一步学习建议

- **u5-l2（zero.Init）**：若你跳过了 Init 的分区机制（padding、`ds_numel_aligned`、句柄家族），回头补齐——本讲的分区转换完全建立在它之上。
- **u5-l6（stage3 主循环）**：本讲只改了缓冲的「分桶」，step 的时序（IPG flush → 溢出检查 → 逐子组更新 → 写回）在 u5-l6；两讲对照读 `stage3.py` 收益最大。
- **u4-l3（BF16 优化器）**：理解「单 dtype 平坦缓冲」为什么是 ZeRO-0/1/2 与 BF16_Optimizer 保不住 fp32 参数的结构性原因。
- **延伸阅读**：`deepspeed/runtime/torch_autocast.py` 的 `sort_dtypes`/`get_all_comm_dtypes`（autocast 与混合 dtype 的交汇点）；`tests/unit/v1/half_precision/test_keep_in_fp32_modules.py` 全文——它就是这份机制的行为规格说明。
