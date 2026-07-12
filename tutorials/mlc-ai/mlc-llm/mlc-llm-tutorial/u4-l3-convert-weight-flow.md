# convert_weight 全流程与预分片

## 1. 本讲目标

本讲打开 `mlc_llm convert_weight` 命令的「接口层」黑盒。前面 u4-l1 讲了 Loader 如何把 HuggingFace 权重低内存地读进来，u4-l2 讲了两张映射表如何对齐名字与形状，本讲把它们串成一条**完整流水线**：从「在哪找权重、读什么格式」一直到「落盘成 MLC 权重」。

学完本讲你应该能够：

- 说清 `convert_weight` 的完整阶段：定位权重 → 加载+映射 → 量化 → （可选）预分片 → 落盘。
- 解释 **preshard（预分片）** 为什么存在、它如何为多 GPU 张量并行提前把权重切好。
- 认识 MLC 权重在磁盘上的存储格式（`tvmjs` 的 ndarray cache），以及运行期如何把它读回来。

本讲只读不写源码，所有代码引用都来自当前 HEAD `a2bcc5c8`。

## 2. 前置知识

本讲承接 u4-l1（Loader 抽象）和 u4-l2（ExternMapping / QuantizeMapping）。复习三个关键词：

- **HF 原始参数**：HuggingFace 仓库里 `pytorch_model.bin` / `model.safetensors` 里的权重，名字和形状都按 PyTorch 习惯命名。
- **MLC 参数**：用 Relax nn 定义模型后 `export_tvm` 导出的参数，名字按 MLC 习惯（如把 `q_proj/k_proj/v_proj` 合并成 `qkv_proj`）。
- **映射表**：`ExternMapping` 把「MLC 名 → 源名列表 + 组合函数」绑定起来；`QuantizeMapping` 把「未量化 MLC 名 → 量化后 MLC 名列表 + 量化函数」绑定起来。

另外需要一点**张量并行（tensor parallelism）**的直觉：把一个大矩阵乘法拆到 N 张 GPU 上各算一部分，每张卡只持有「自己那份」权重。例如 `qkv_proj` 的输出维度按 head 数均分到 N 张卡，每张卡只需 `[总输出/N, 输入]` 的子矩阵。本讲的 preshard 就是**在权重转换阶段提前完成这个切分**，让每张卡运行时直接加载自己的那份，而不必在每次启动时现场切。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [python/mlc_llm/interface/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py) | 接口层。定义 `ConversionArgs` 参数信封与 `_convert_args` 主流程，是本讲的「主角」。 |
| [python/mlc_llm/support/auto_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_weight.py) | `detect_weight`：自动定位权重目录、猜测权重格式（torch / safetensor）。 |
| [python/mlc_llm/support/preshard.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py) | `apply_preshard`：把带 `shard_strategy` 的参数展开成多份，并编译切分函数。 |
| [python/mlc_llm/support/tensor_parallel.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py) | `ShardSingleDim`：描述「沿哪一维切、按什么段切」的策略，能生成 TIR 切分函数。 |
| [python/mlc_llm/loader/huggingface_loader.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py) | `HuggingFaceLoader.load`：在 yield 参数前应用 preshard 切分函数。 |
| [python/mlc_llm/interface/compile.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py) | `_apply_preproc_to_params_and_check_pipeline`：编译期的「分片配方」生成，与 preshard 共享 `ShardSingleDim`。 |
| [cpp/serve/function_table.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc) | 运行期 `LoadParameters`：根据是否预分片，选择两种加载方式之一把权重读回 GPU。 |

## 4. 核心概念与源码讲解

### 4.1 ConversionArgs 与转换主流程

#### 4.1.1 概念说明

`convert_weight` 的职责可以用一句话概括：**把 HuggingFace 原始权重，按 MLC 模型定义与量化方案的要求，重命名、重组、量化后，写成跨平台共享的 MLC 权重文件**。

它**不碰**模型库（那是 `compile` 的事），也**不碰** `mlc-chat-config.json`（那是 `gen_config` 的事），它只产出「权重产物」。回顾 u1-l4 的三类产物，本讲只关心第一类——MLC 权重。

这条命令的参数被收进一个 dataclass 信封 `ConversionArgs`，它和 u3-l1 的 `Model` 信封、u7-l1 的 `CompileArgs` 是同一种设计：把一堆结构化参数打包，方便在 CLI 层构造、在接口层消费。

#### 4.1.2 核心流程

主流程在 `_convert_args` 里，可抽象成下面的伪代码：

```text
读环境变量 MLC_INTERNAL_PRESHARD_NUM（可能为空）
model_config = Model.config.from_file(config.json)          # 解析架构配置
若设置了 preshard 数：model_config.tensor_parallel_shards = N   # 覆盖分片数
model, quantize_map = Model.quantize[kind](config, quant)   # 建图 + 拿量化映射表
named_params  = model.export_tvm(get_default_spec())        # 拿到 MLC 参数名/形状/dtype 期望

若设置了 preshard 数：
    named_params, preshard_funcs = apply_preshard(...)      # 展开成 _shard-i 并编译切分函数
否则：
    preshard_funcs = None

def _param_generator():                                     # 一个生成器，逐个产出 (name, tensor)
    loader = LOADER[source_format](source, extern_map, quantize_map)
    for name, param in loader.load(device, preshard_funcs):
        _check_param(name, param)                          # 校验名字/形状/dtype 与期望一致
        yield name, param.copyto(cpu)                      # 拷回 CPU 等待落盘

tvmjs.dump_tensor_cache(_param_generator(), output_dir,    # 流式写盘 → MLC 权重
                        meta_data=_metadata_callback, encode_format="f32-to-bf16")
若 named_params 还有剩余 → 报错「源里缺参数」
```

注意三个设计要点：

1. **生成器驱动的流式处理**：`_param_generator` 是一个 `yield` 生成器，`dump_tensor_cache` 边消费边写盘。这意味着任意时刻内存里只持有「当前正在处理的那一小撮参数」，而不是整个模型——这正是 u4-l1 讲的「低内存」在接口层的体现。
2. **校验前置**：`_check_param` 把「形状/dtype 与模型期望不符」的错误拦截在写盘之前，避免跑到最后才发现名字拼错。
3. **preshard 是可插拔的中间环节**：没设环境变量时它完全不介入；设了之后它在「拿到 named_params」之后、「开始加载」之前介入。

#### 4.1.3 源码精读

`ConversionArgs` 把命令行参数结构化，字段含义如下：

[python/mlc_llm/interface/convert_weight.py:30-41](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L30-L41) —— 定义 `config / quantization / model / device / source / source_format / output / lora_adapter` 八个字段。注意 `model` 已经是 u3-l1 的 `Model` 信封实例（CLI 层用 `detect_model_type` 解析过），`quantization` 已经是 `Quantization` 实例（从 `QUANTIZATION` 注册表取出），所以接口层拿到的全是结构化对象，不再有含糊字符串。

主流程的第一步是「读架构配置 + 覆盖分片数」：

[python/mlc_llm/interface/convert_weight.py:102-115](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L102-L115) —— 先从环境变量读 `pre_shards_num`；再用 `Model.config.from_file` 解析 `config.json`；**只有当环境变量存在时**，才把 `model_config.tensor_parallel_shards` 覆盖成它。接着调用 `Model.quantize[kind](...)`，一次性拿到「建好的（已量化）模型」和「量化映射表 `quantize_map`」。`ft-quant` + 多卡在这里被直接拒绝（`NotImplementedError`）。

随后把模型导出成参数字典：

[python/mlc_llm/interface/convert_weight.py:116-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L116-L125) —— `model.export_tvm(spec=get_default_spec(), allow_extern=True)` 返回的 `_named_params` 就是「MLC 期望的参数集合」，键是 MLC 参数名，值带期望形状与 dtype。是否有 preshard，决定 `preshard_funcs` 是字典还是 `None`。

`_check_param` 是写盘前的守卫，校验名字、形状（允许动态维 `tirx.Var`）、dtype：

[python/mlc_llm/interface/convert_weight.py:127-157](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L127-L157) —— 校验通过后 `del named_params[name]`，把这个名字从「待匹配」集合里划掉。因此最后若 `named_params` 非空，就说明源里缺了这些参数。

真正的「加载+量化+（可选）preshard」全部封装在 `_param_generator` 里：

[python/mlc_llm/interface/convert_weight.py:164-180](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L164-L180) —— 用 `LOADER[source_format]`（u4-l1 的注册表）构造加载器，传入三样东西：`source` 路径、`args.model.source[source_format](...)` 构造的 `ExternMapping`、`quantize_map`。然后调用 `loader.load(device, preshard_funcs)` 开始 yield。每个参数都经 `_check_param` 校验，再 `copyto(cpu)` 等待写盘。注意 `total_bytes` / `total_params` 在这里累加，用于后续元数据。

落盘这一步：

[python/mlc_llm/interface/convert_weight.py:182-198](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L182-L198) —— `_metadata_callback` 计算三条元数据：`ParamSize`（参数个数）、`ParamBytes`（总字节数）、`BitsPerParam`（平均每参数比特数）。三者关系为：

\[
\text{BitsPerParam} = \frac{\text{ParamBytes} \times 8}{\text{total\_params}}
\]

这个值会写进权重产物的元数据，是衡量量化效果（如 q4f16_1 应该接近 4.x bit/param）的关键指标。`dump_tensor_cache` 消费生成器并写盘（4.3 节详述）。

补充一个边角路径：`convert_weight` 的公开入口还支持 `--lora-adapter`，会在转换前用 `peft` 把 LoRA 权重合并进基座模型：

[python/mlc_llm/interface/convert_weight.py:235-250](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L235-L250) —— 合并后产生一个临时目录，对它重新 `detect_weight`，再用 `dataclasses.replace` 替换 `source` 字段，最后走同一条 `_convert_args`。理解主流程时可以先忽略这个分支。

#### 4.1.4 代码实践

**实践目标**：把伪代码里每一步落实到具体源码行号，确认「定位权重 → 加载映射 → 量化 → 落盘」的真实顺序。

**操作步骤**（源码阅读型实践）：

1. 打开 [cli/convert_weight.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py)，定位到第 97-101 行，确认 CLI 层在调用 `convert_weight(...)` 之前先调用了 `detect_weight(...)`，把 `auto` 字符串解析成「真实权重路径 + 真实格式」。
2. 进入 `detect_weight`：阅读 [auto_weight.py:76-90](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/auto_weight.py#L76-L90)，理解 `weight_format="auto"` 时会调用 `_guess_weight_format`，否则用 `CHECK_FORMAT_METHODS` 里的校验函数（`_check_pytorch` / `_check_safetensor`）逐个探测。
3. 回到 [convert_weight.py:164-180](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L164-L180)，确认加载顺序：构造 Loader → `loader.load(...)` 逐个 yield → `_check_param` → `copyto(cpu)`。
4. 跟进 `loader.load`：阅读 [huggingface_loader.py:119-130](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L119-L130)，确认对一个 MLC 参数，先 `_load_mlc_param`（按 `ExternMapping` 取并组合源张量），再 `_load_or_quantize`（按 `QuantizeMapping` 决定是否量化、可能一生多）。

**需要观察的现象**：在 `loader.load` 的循环里，量化与 preshard 是**嵌套**关系——`_load_or_quantize` 先把一个 MLC 参数拆成 0~多个量化参数，再对每个量化参数判断是否要 preshard 切分。

**预期结果**：你能画出一条从 `cli/main` → `detect_weight` → `convert_weight` → `_convert_args` → `_param_generator` → `loader.load` → `_load_or_quantize` 的调用链，并能指出「量化」发生在「preshard」之前。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_param_generator` 用生成器（`yield`）而不是先收集成一个完整 dict 再传给 `dump_tensor_cache`？

> **答案**：为了把峰值内存压到最低。生成器是「惰性、逐个」产出的，`dump_tensor_cache` 边收边写盘，写完即可释放该参数。若先攒成完整 dict，整个模型的所有参数会同时驻留内存，大模型很容易 OOM。这与 u4-l1 讲的 HuggingFaceLoader 迭代式加载是同一个思想。

**练习 2**：如果源权重里缺少某个 MLC 需要的参数，错误会在哪一行被抛出？为什么能定位到「具体缺哪个」？

> **答案**：在 [convert_weight.py:197-198](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L197-L198) 抛出。因为 `_check_param` 每校验通过一个就 `del named_params[name]`，全部跑完后 `named_params` 里剩下的键就是「源里没提供」的参数，报错信息能精确列出它们的名字。

### 4.2 预分片（preshard）：为张量并行切权重

#### 4.2.1 概念说明

多 GPU 张量并行要求每张卡只持有「自己那份」权重。MLC 提供了两条路来实现这一点：

- **路 A（运行时切，默认）**：磁盘上存的是**未切分的完整权重**，每次启动引擎时由运行期加载器 `mlc.multi_gpu.LoadMultiGPU` 现场切成 N 份分发到各卡。优点是权重产物与单卡通用；缺点是每次启动都要切一遍，启动慢、且临时占用更多显存/内存。
- **路 B（提前切，preshard）**：在 `convert_weight` 阶段就把权重切成 N 份分别落盘，运行期用 `mlc.multi_gpu.LoadMultiGPUPresharded` 直接各取一份。优点是启动快、运行期零额外开销；缺点是产物绑定卡数 N，不能换 N。

选哪条路由环境变量 `MLC_INTERNAL_PRESHARD_NUM` 控制：**设了它就走路 B（提前切），不设就走路 A**。这个开关只在两个地方被读取——转换期 [convert_weight.py:102](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L102) 和运行期 [function_table.cc:172](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L172)，两端必须一致。

「在哪切、怎么切」由模型定义里的 **`shard_strategy`** 属性声明。回顾 u3-l2 与下面的代码：Llama 在建每一层时，给 `qkv_proj / o_proj / gate_up_proj / down_proj` 四个权重挂上 `tp.ShardSingleDim(...)` 策略，其余权重（如 RMSNorm）不挂——不挂的参数在多卡间复制，挂了的参数按策略切。

#### 4.2.2 核心流程

`apply_preshard` 做三件事：**展开参数名 → 生成并编译切分函数 → 绑定回调**。

```text
对 named_params 里每个 param：
    若 param 带 shard_strategy：
        把 "foo.weight" 展开成 N 份：foo.weight_shard-0, ..., foo.weight_shard-{N-1}（形状仍是单卡形状）
        记录 param_to_shard_func["foo.weight"] = shard_strategy.name
        用 BlockBuilder 把该策略编译成一个 Relax 函数（去重：同名策略只编一次）
    否则：
        保持原名不动

把所有 Relax 切分函数用 dlight 调度 + relax.build 编译成一个 VirtualMachine
把 param_to_shard_func 里的「策略名」替换成 VM 里对应的可调用函数
返回 (new_named_params, param_to_shard_func)
```

每个 Relax 切分函数内部是固定三步（见 `_create_shard_func` 的注释）：

1. 调用 TIR 切分函数 `call_tir(shard_tir, weight)`，输入是**完整形状**（单卡维度 × N），输出形状是 `[N, *单卡形状]`。
2. `split` 沿第 0 维（即 N 那一维）切成 N 份。
3. 每份 `squeeze` 掉第 0 维，得到 N 个「单卡形状」的张量。

这套 Relax 函数随后会被 loader 在 yield 前调用。关键点在于：**源 ExternMapping 总是产出完整形状的张量**（见 standard_loader 里对 HF q/k/v 的 `np.concatenate`），而 preshard 切分函数正好把它切成 N 份单卡形状——preshard 就是「完整源张量」与「单卡模型期望」之间的桥梁。

#### 4.2.3 源码精读

先看模型如何声明切分策略。Llama 的每一层在构造时给四个权重挂上 `ShardSingleDim`：

[python/mlc_llm/model/llama/llama_model.py:181-202](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/model/llama/llama_model.py#L181-L202) —— `_set_tp` 给 `qkv_proj` 挂 `ShardSingleDim("_shard_qkv", segs=[q, k, v], dim=0)`（按 q/k/v 三段、沿输出维 0 切），给 `o_proj` 挂 `ShardSingleDim("_shard_o", dim=1)`（沿输入维 1 切），`gate_up_proj` 按 `[i, i]` 两段切，`down_proj` 沿 dim=1 切。注意这里的 `q/k/v/i` 都已经是**除以 `tensor_parallel_shards` 之后**的单卡段长。

`ShardSingleDim` 是个 dataclass，核心方法 `gen_tir` 生成切分的 TIR 函数：

[python/mlc_llm/support/tensor_parallel.py:36-83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L36-L83) —— 入参 `weight` 是单卡形状，`gen_tir` 用 `_compute_in_shape` 推出**完整输入形状**（`shape[dim] * shards`），然后对每一段做 `compute → reshape 成 [shards, seg, …] → transpose 把 shards 提到前维 → concatenate`，最终得到 `[shards, *单卡形状]` 的输出。这个「reshape+transpose」技巧的作用是让 head **均匀交错**分布到各卡，而不是前一半 head 给卡 0、后一半给卡 1。

`apply_preshard` 的主体：

[python/mlc_llm/support/preshard.py:92-123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L92-L123) —— 遍历 `named_params`：带策略的参数展开成 `_shard-0…_{N-1}`（用 `_sharded_param_name` 拼名），不带策略的原样保留；同名策略只 `_create_shard_func` 一次（去重）。最后 `bb.finalize()` 得到 IRModule，`_compile_shard_funcs` 编译成 VM，再把 `param_to_shard_func` 里的名字换成 `vm[name]` 这个可调用对象。

切分函数的 Relax 构造（三步：`call_tir` → `split` → `squeeze`）：

[python/mlc_llm/support/preshard.py:31-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L31-L51) —— 注意 `weight_shape[shard_strategy.dim] = weight_shape[...] * tensor_parallel_shards`，即函数签名里输入张量沿切分维放大 N 倍，正是「完整源张量」的形状。

编译用的是 TVM 的 dlight 默认调度（含 Matmul/GEMV/Reduction 等），保证切分函数本身在 GPU 上也高效：

[python/mlc_llm/support/preshard.py:54-67](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L54-L67)。

loader 在 yield 前应用切分函数：

[python/mlc_llm/loader/huggingface_loader.py:124-130](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/loader/huggingface_loader.py#L124-L130) —— `preshard_funcs[name](loader_param)` 把一个完整张量切成 N 份，每份用 `_sharded_param_name(name, shard_id)` 命名后 yield。这与接口层 `_check_param` 期望的 `_shard-i` 名字对得上。

运行期两端一致地读这个环境变量：

[cpp/serve/function_table.cc:172-178](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L172-L178) —— `getenv("MLC_INTERNAL_PRESHARD_NUM") == nullptr` 时用 `LoadMultiGPU`（路 A，现场切），否则用 `LoadMultiGPUPresharded`（路 B，读已切好的）。这就是为什么说「转换期和运行期必须设同一个环境变量」。

最后看**编译期**是如何为「路 A」准备配方的——这与 preshard 共享同一个 `ShardSingleDim`，是理解本讲实践任务的关键：

[python/mlc_llm/interface/compile.py:62-82](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L62-L82) —— 当 `tensor_parallel_shards > 1` 时，对每个带 `shard_strategy` 的参数，调用 `gen_shard_info` 生成一条「分片配方」（含 `func_name / in_shape / out_shape / out_dtype`）追加进 `param.attrs["preprocs"]`，并用 `gen_tir` 生成对应的 TIR 切分函数塞进 `extra_tirs`（同名去重）。这条配方最终随模型库一起导出，供运行期 `LoadMultiGPU` 现场切分使用。

> **两套机制的对照**：preshard（本节）在 **convert_weight** 期就**真的切**了权重，落盘成 `_shard-i`；`preprocs`（compile.py）只在模型库里**记录切分配方**，权重仍是完整的，由运行期按配方现场切。两者共用 `ShardSingleDim.gen_tir`，区别只在「何时切」。

#### 4.2.4 代码实践

**实践目标**：解释当 `tensor_parallel_shards > 1` 时，分片信息是如何为每个参数生成的，并对比 preshard（转换期）与 preprocs（编译期）两条路。

**操作步骤**（源码阅读型实践）：

1. 假设要对 Llama 做张量并行，设环境变量 `MLC_INTERNAL_PRESHARD_NUM=2` 跑 `convert_weight`。阅读 [convert_weight.py:111-125](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L111-L125)，确认：`model_config.tensor_parallel_shards` 被覆盖成 2 → 模型按 2 卡建图（head 数减半）→ `export_tvm` 得到的 `named_params` 里 `qkv_proj.weight` 是**单卡形状**且带 `shard_strategy` → `apply_preshard` 把它展开成 `qkv_proj.weight_shard-0`、`qkv_proj.weight_shard-1`。
2. 阅读 [preshard.py:36-51](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L36-L51) 与 [tensor_parallel.py:94-97](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/tensor_parallel.py#L94-L97)，说明切分函数的**输入形状是单卡维度 × 2**（即完整源形状），输出是 2 个单卡形状张量。
3. 对照编译期配方：阅读 [compile.py:67-82](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/compile.py#L67-L82)，确认 `gen_shard_info` 与 `gen_tir` 用的是**同一个** `ShardSingleDim`，但这里只把结果写进 `preprocs` 元数据和 `extra_tirs`，并不真的切权重。

**需要观察的现象**：转换期 preshard 真的改变了落盘的参数名（多了 `_shard-i` 后缀）和参数个数；编译期 preprocs 不改变参数名，只给每个参数多挂一条「怎么切」的配方。

**预期结果**：你能用一句话回答任务里的问题——「当 `tensor_parallel_shards > 1` 时，`ShardSingleDim.gen_shard_info` 为每个带 `shard_strategy` 的参数生成一条 `{func_name, in_shape=单卡维×N, out_shape=(N, *单卡形状), out_dtype}` 的配方，挂到 `param.attrs["preprocs"]`，同时 `gen_tir` 生成同名的 TIR 切分函数随模型库导出；这套配方与 preshard 共用 `ShardSingleDim`，区别是 preprocs 记配方、运行时切，preshard 直接切好落盘。」

> 待本地验证：实际跑 `MLC_INTERNAL_PRESHARD_NUM=2 mlc_llm convert_weight ...` 后，可用 `python -c "import json; print(list(json.load(open('输出目录/tensor-cache.json'))['records'].keys())[:5])"` 观察参数名是否带 `_shard-0/_shard-1` 后缀。

#### 4.2.5 小练习与答案

**练习 1**：如果一个模型的 `named_params` 里没有任何参数带 `shard_strategy`，却设了 `MLC_INTERNAL_PRESHARD_NUM=2`，会发生什么？

> **答案**：`apply_preshard` 里 `has_shard_strategy` 保持 `False`，会打一条 warning（[preshard.py:111-116](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L111-L116)），然后照常以「非预分片」方式继续。也就是说不会报错，但权重不会被切，运行期若按预分片方式加载会出问题——所以环境变量应只用于确实声明了切分策略的模型。

**练习 2**：为什么 `qkv_proj` 用 `dim=0`（输出维）切，而 `o_proj` 用 `dim=1`（输入维）切？

> **答案**：这是张量并行的标准切法。注意力里 `qkv_proj` 把 hidden 投影到 QKV 空间，输出维按 head 切，每卡算一部分 head；`o_proj` 把注意力输出投影回 hidden，它的输入正是按 head 切好的注意力输出，所以沿输入维（dim=1）切，各卡算自己那部分 head 的输出后再 all-reduce 求和（对应 llama_model 里的 `ccl_allreduce`）。

### 4.3 MLC 权重存储格式与落盘

#### 4.3.1 概念说明

权重经过加载、映射、量化、（可选）preshard 之后，最终要落盘成「跨平台共享」的格式。MLC 用的是 TVM 的 `tvmjs` ndarray cache 格式，它由两部分组成：

- 若干个**二进制分片**文件（命名形如 `params_shard_0.bin`、`params_shard_1.bin` …），把所有参数打包成连续字节流，分多片以便分段加载。
- 一个 **`tensor-cache.json` 索引文件**，记录每个参数落在哪个分片、偏移量、形状、dtype 等元数据，以及 `_metadata_callback` 返回的 `ParamSize / ParamBytes / BitsPerParam`。

这个格式是**跨平台**的（同一份权重能被 CUDA、Metal、Vulkan、WebGPU 运行期读取），因为存的是与设备无关的原始字节，由运行期按目标设备上传。回顾 u1-l4：MLC 权重（`params_shard_*.bin`）跨平台共享，模型库（`.so` 等）才是平台专用的——本节讲的就是前者。

落盘时还有一个细节：`encode_format="f32-to-bf16"`，即遇到 float32 权重会自动转成 bfloat16 存储，省一半空间且对推理精度无损（量化后的 int4/int8 权重本身已是小 dtype，不受影响）。

#### 4.3.2 核心流程

```text
接口层：tvmjs.dump_tensor_cache(param_generator, output_dir, meta_data, encode_format="f32-to-bf16")
    ↓ 按 encode_format 转换 dtype，累积到一定大小就切成一个 params_shard_*.bin
    ↓ 写出 params_shard_0.bin, params_shard_1.bin, ...
    ↓ 写出 tensor-cache.json（每个参数的 [分片, 偏移, 形状, dtype] + 顶层元数据）

运行期（C++ function_table.cc LoadParameters）：
    use_disco（多卡）路径：
        读 output_dir/tensor-cache.json
        按 preshard 与否选 LoadMultiGPUPresharded / LoadMultiGPU，由 disco 分发到各 worker
    单卡路径：
        vm.builtin.tensor_cache.load(output_dir, device)        # 按 tensor-cache.json 把分片读进设备
        vm.builtin.param_array_from_cache[_by_name]            # 按名字/顺序取出参数张量
        vm.builtin.tensor_cache.clear()                         # 清掉磁盘缓存副本
```

注意「**先加载到 cache，再按名取出，最后清缓存**」这个三步模式：运行期先把整片 `.bin` 载入一个临时缓存，模型函数再按名字从缓存里取走自己需要的张量，取走后即可清缓存释放内存。

#### 4.3.3 源码精读

落盘调用：

[python/mlc_llm/interface/convert_weight.py:190-196](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L190-L196) —— `tvmjs.dump_tensor_cache` 把生成器 `_param_generator` 流式写盘，`meta_data=_metadata_callback` 注入元数据，`encode_format="f32-to-bf16"` 控制 dtype 转换，`show_progress=False` 关闭自带进度条（因为外层 Loader 已有自己的 tqdm）。`tvmjs` 来自 `from tvm.contrib import tvmjs`（[convert_weight.py:14](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L14)），其完整实现位于 TVM 仓库的 `python/tvm/contrib/tvmjs.py`（属 3rdparty/tvm 子模块，本仓库未展开，此处待确认其内部版本细节）。

元数据回调：

[python/mlc_llm/interface/convert_weight.py:182-187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L182-L187) —— 三个字段都会进入 `tensor-cache.json`，`BitsPerParam` 是判断量化是否达标的第一眼指标。

运行期单卡加载（三步模式）：

[cpp/serve/function_table.cc:181-203](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L181-L203) —— `tensor_cache.load` 读盘到设备；若模型元数据带参数名表（`model_metadata_.params` 非空）则用 `param_array_from_cache_by_name` 按名取，否则用 `param_array_from_cache("param", -1)` 按顺序取；最后 `tensor_cache.clear` 清缓存。

运行期多卡（disco）加载：

[cpp/serve/function_table.cc:155-179](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L155-L179) —— 多卡走 disco 分布式会话。若模型库没内嵌参数元数据，直接读 `tensor-cache.json` 并用 `runtime.disco.ShardLoader` + `ShardLoaderLoadAll` 加载；否则按是否 preshard 选 `LoadMultiGPUPresharded` / `LoadMultiGPU`（即 4.2 节的路 B / 路 A）。

#### 4.3.4 代码实践

**实践目标**：亲手看到 MLC 权重产物的磁盘结构，把「文件 ↔ 源码」对应起来。

**操作步骤**：

1. 找一个已转换好的 MLC 权重目录（例如从 HF:// 拉一个 `*-MLC` 模型，或本地跑过一次 `convert_weight`）。列出目录内容，应该看到若干 `params_shard_*.bin` 和一个 `tensor-cache.json`。
2. 用编辑器打开 `tensor-cache.json`，定位到顶层元数据里的 `ParamSize / ParamBytes / BitsPerParam`，对照 [convert_weight.py:182-187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L182-L187) 确认来源；再定位到某条参数记录，看它的「所属分片 + 偏移 + 形状 + dtype」。
3. 对照 [function_table.cc:181-187](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L181-L187)，确认运行期正是按 `tensor-cache.json` 把 `params_shard_*.bin` 的字节切片还原成各参数张量。

**需要观察的现象**：`BitsPerParam` 应接近量化名所声明的位宽（例如 q4f16_1 大约 4.x bit/param）；同一参数的 dtype 与量化方案一致（如 group quantization 的 `weight` 是 `uint32`、`scale` 是 `float16`）。

**预期结果 / 待本地验证**：能描述「`params_shard_*.bin` 存连续字节，`tensor-cache.json` 存索引，二者配合让运行期按需切片读取」。如果本地没有现成权重，可改为源码阅读型：只做步骤 2 的「读 json 字段 → 对照源码」部分，并标注「待本地验证产物结构」。

#### 4.3.5 小练习与答案

**练习 1**：`encode_format="f32-to-bf16"` 会不会影响已经量化的 int4 权重？

> **答案**：不会。该选项只对 float32 的权重做 bf16 转换；量化后的权重（如 group quantization 产出的 `uint32` weight + `float16` scale）本身不是 float32，`dump_tensor_cache` 会原样存储，不做转换。它的主要收益是把「未量化的 fp32 副权重」（如某些 embedding）压成 bf16。

**练习 2**：运行期为什么要在取出参数后调用 `tensor_cache.clear()`？

> **答案**：`tensor_cache.load` 把整个 `.bin` 分片读进了一个临时缓存；模型函数按名取走各自张量后，这些张量已被 `params_` 引用持有，缓存里的副本就不再需要。及时 `clear` 可以释放掉这部分重复内存，避免「缓存副本 + 模型参数副本」同时占用双倍显存。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「带预分片的权重转换」端到端跟踪。

**任务**：假设要在 2 张 GPU 上张量并行部署某 Llama 模型，你打算用 preshard 提前切好权重。请按下面的脚本阅读源码并回答问题。

1. **定位权重**：追踪 [cli/convert_weight.py:97-101](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/cli/convert_weight.py#L97-L101) → `detect_weight`，说清 `--source auto` + `--source-format auto` 是如何自动找到 safetensor 权重并确定格式的。
2. **建图与期望形状**：设 `MLC_INTERNAL_PRESHARD_NUM=2`，追踪 [convert_weight.py:104-120](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L104-L120)，说明此时 `qkv_proj.weight` 的**期望形状**（单卡）是多少、为什么。
3. **预分片**：追踪 [convert_weight.py:122-123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L122-L123) → `apply_preshard` → [preshard.py:92-123](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/support/preshard.py#L92-L123)，解释「完整源张量」如何被切成 2 份单卡张量、名字如何变成 `_shard-0/_shard-1`。
4. **落盘与运行期**：追踪 [convert_weight.py:190-196](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/interface/convert_weight.py#L190-L196) 的落盘，再对照 [function_table.cc:172-178](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/function_table.cc#L172-L178)，说明运行期如何**因为同一个环境变量**而选择 `LoadMultiGPUPresharded` 直接各取一份。

**交付物**：一张包含四列的表——「阶段 / 涉及源码行 / 输入 / 输出」，覆盖定位→建图→preshard→落盘→运行期加载。

## 6. 本讲小结

- `convert_weight` 的接口层用 `ConversionArgs` 收集参数，主流程 `_convert_args` 是一条「定位权重 → 建图拿期望 → （可选）preshard → 生成器流式加载+量化 → 校验 → 落盘」的流水线，全程生成器驱动以压低内存。
- 加载顺序在 `loader.load` 里是嵌套的：`ExternMapping`（取+组合源张量）→ `QuantizeMapping`（量化，一生多）→ `preshard_funcs`（切分，一生 N）。
- **preshard** 由 `MLC_INTERNAL_PRESHARD_NUM` 触发，把带 `shard_strategy` 的参数在转换期就切成 N 份落盘；它与编译期的 `preprocs` 共享 `ShardSingleDim`，区别是「preshard 真切、preprocs 只记配方」。
- `ShardSingleDim.gen_tir` 用 reshape+transpose 让 head 均匀交错分布到各卡；`apply_preshard` 把它包成 Relax 函数（call_tir→split→squeeze）并编译成 VM，由 loader 在 yield 前调用。
- MLC 权重以 `tvmjs` ndarray cache 格式存储：`params_shard_*.bin` + `tensor-cache.json` 索引 + 元数据（`ParamSize/ParamBytes/BitsPerParam`），跨平台共享。
- 运行期 `function_table.cc` 的 `LoadParameters` 按「是否 preshard / 是否 disco」选择加载方式，并遵循「load → 按名取 → clear」的三步模式控制显存。

## 7. 下一步学习建议

- **量化细节**：本讲把 `QuantizeMapping` 当黑盒（只调用 `quantize_map`），具体量化层如何替换、量化权重如何布局，请进入 u5（量化体系），重点读 u5-l2 的 group quantization。
- **编译期的分片配方**：本讲提到了 `compile.py` 的 `_apply_preproc_to_params_and_check_pipeline`，完整编译主流程在 u7-l1（compile 接口）。
- **多 GPU 运行期**：preshard 产物最终被 disco 多卡加载，运行期的多进程会话与多 GPU loader 在 u12-l1（多 GPU 与张量并行）展开，可读 `cpp/multi_gpu/multi_gpu_loader.cc`。
- **模型定义侧**：想理解 `shard_strategy` 是怎么挂在每一层上的，回顾 u3-l2（用 Relax nn 编写模型），重点看 `LlamaDecoderLayer._set_tp`。
