# Megatron-SWIFT 架构总览

## 1. 本讲目标

本讲是「分布式与高性能训练」单元里关于 **Megatron-SWIFT** 的第一篇。读完本讲，你应当能够：

- 说清 `swift/megatron` 这个子包的目录结构与各子模块职责，以及 `megatron` 命令是如何被注册和分发的；
- 理解 HF（Hugging Face，safetensors 格式）权重与 mcore（Megatron-Core，torch_dist 格式）权重之间互转的完整流程，并能读懂 `convert_hf2mcore` / `convert_mcore2hf` 的源码；
- 解释 mcore-bridge 这一外部桥接层「让 Megatron 像 transformers 一样易用」的设计意图，包括它如何屏蔽 torch_dist 格式、如何把权重装配成可部署的完整 HF 模型，以及它与可选的 `megatron-bridge` 后端的差异。

本讲只讲「架构与权重转换」这一层，**不**展开 TP/PP/CP/EP 等并行策略的具体训练流程——那是下一讲 `u9-l4` 的内容。本讲承接 `u5-l4`（SwiftSft 主流程）中「训练器装配」的认知，把它推广到 Megatron 后端。

## 2. 前置知识

阅读本讲前，建议你已经具备以下概念（不熟悉也没关系，下面会用通俗语言再点一遍）：

- **Megatron / Megatron-Core（mcore）**：NVIDIA 开源的大模型训练框架，核心价值是张量并行（TP）、流水并行（PP）、专家并行（EP）等高性能并行能力，但学习曲线陡、权重格式与 transformers 不同。
- **safetensors**：transformers / vLLM / SGLang 等推理框架通用的权重格式，单文件分片、加载快、安全。
- **torch_dist（分布式 checkpoint）**：Megatron 原生的权重存储格式，按 TP/PP 切片落盘，依赖 `torch.distributed.checkpoint`，**不能**被推理框架直接加载。
- **LoRA adapter**：轻量微调产出的增量权重（见 `u5-l3`）。Megatron 里同样有 LoRA，但保存格式与 transformers 的 peft 格式不同。
- **bridge（桥接）**：本讲的核心概念，指夹在「HF 世界」和「mcore 世界」之间的翻译层，负责两种权重格式的互转。

一个直白的类比：HF 权重像「人民币」，mcore 权重像「美元」，你拿着人民币去美国（用 Megatron 训练）得先换汇；训练完想用 vLLM 部署又得换回人民币。mcore-bridge 就是这个「货币兑换商」，而且兑换过程支持 LoRA 零钱、还能多机协作兑换超大额。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [swift/megatron/__init__.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/__init__.py) | megatron 子包入口，懒加载声明 + 导入期调用 `init_megatron_env()` |
| [swift/megatron/init.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/init.py) | 环境初始化与猴子补丁，含 `_patch_mcore_bridge()` |
| [swift/megatron/convert.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py) | 模块级 `convert_hf2mcore` / `convert_mcore2hf` 函数 |
| [swift/megatron/pipelines/export/export.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py) | `megatron export` 命令真正落到的 `MegatronExport` 管道 |
| [swift/megatron/model/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py) | `get_mcore_model` / `get_mcore_model_config`，桥接后端派发与 mcore-bridge 配置构建 |
| [swift/megatron/arguments/megatron_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py) | `MegatronArguments`，含 `bridge_backend` 字段及其校验 |
| [swift/cli/_megatron/main.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/_megatron/main.py) | `megatron` 命令的子命令路由表 |
| [swift/megatron/utils/megatron_lm_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py) | `save_mcore_checkpoint` / `load_mcore_checkpoint`，torch_dist 读写 |
| [swift/megatron/utils/convert_utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/convert_utils.py) | `test_convert_precision`，转换精度校验 |

## 4. 核心概念与源码讲解

### 4.1 megatron 模块结构

#### 4.1.1 概念说明

`swift/megatron` 是 ms-swift 中相对独立的一个子包，它的目标是：**在保留 Megatron 高性能并行能力的同时，复用 ms-swift 的参数体系、数据集、模板、训练器抽象**，让用户用一行 `megatron sft ...` 就能跑起 Megatron 训练，而不必去写 Megatron 原生那一大套脚本。

它和「普通 swift」（即 `swift sft`，走 transformers + DeepSpeed/FSDP）是两条并行的训练后端，共享上层骨架（参数 dataclass、pipelines 基类、模板、数据集），但底层模型与训练器换成 Megatron。

#### 4.1.2 核心流程

`megatron` 命令的整体链路如下：

1. `setup.py` 的 `console_scripts` 把 `megatron` 注册到 `swift.cli._megatron.main:cli_main`。
2. `cli_main` 拿到自己的 `ROUTE_MAPPING`（只有 `pt/sft/rlhf/export` 四个子命令），调用通用的 `swift.cli.main.cli_main(ROUTE_MAPPING, is_megatron=True)`。
3. 通用 `cli_main`（见 `u1-l4`）做子命令路由 + 按 `is_megatron=True` 决定是否套 torchrun，再用 `subprocess` 重启对应的脚本文件。
4. 脚本文件（如 `swift/cli/_megatron/export.py`）里直接 `from swift.megatron import megatron_export_main` 并调用。
5. `swift.megatron` 包在**被首次导入时**执行 `__init__.py`，其中调用 `init_megatron_env()` 完成环境打补丁，并用 `_LazyModule` 声明对外接口。

子包内部采用与 swift 主包一致的扁平组织（一级目录 = 一个职责）：

```
swift/megatron/
├── __init__.py        # 入口：init_megatron_env() + _LazyModule
├── init.py            # 环境初始化、对 megatron/torch 的猴子补丁
├── convert.py         # hf<->mcore 权重互转（函数式）
├── arguments/         # MegatronArguments 等参数 dataclass
├── model/             # get_mcore_model：构建 mcore 模型 + 桥接派发
├── trainers/          # MegatronTrainer 及 DPO/GRPO/KTO 等
├── pipelines/         # megatron_sft_main / megatron_export_main 等管道
├── callbacks/         # 日志/可视化回调
└── utils/             # checkpoint 读写、并行工具、转换工具
```

#### 4.1.3 源码精读

**入口注册**：`megatron` 命令在 setup 里与 `swift` 并列注册，二者共用同一套 `cli_main` 调度逻辑。

[swift/setup.py:163](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/setup.py#L163) 注册了 `swift` 与 `megatron` 两个命令行入口。

`megatron` 的路由表只有四个子命令，比 `swift` 精简（没有 infer/deploy/eval 等——这些用普通 `swift` 即可）：

[swift/cli/_megatron/main.py:6-15](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/_megatron/main.py#L6-L15) 定义 `ROUTE_MAPPING` 并以 `is_megatron=True` 复用 swift 的 `cli_main`。注意 `is_megatron=True` 会让 `u1-l4` 提到的 `use_torchrun` 判定对 megatron 命令放宽——megatron 的所有子命令都允许走多进程。

**包入口与懒加载**：`swift/megatron/__init__.py` 延续了 `u1-l3` 讲过的 `_LazyModule` 模式，但在懒加载声明**之前**先执行了一件实事——调用 `init_megatron_env()`：

[swift/megatron/__init__.py:3-13](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/__init__.py#L3-L13) 在导入期就调用 `init_megatron_env()`，并兼容 Ascend NPU（导入 `mindspeed.megatron_adaptor`）。这意味着「只要 `import swift.megatron`，环境补丁就生效」。

[swift/megatron/__init__.py:28-48](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/__init__.py#L28-L48) 用 `_import_structure` 声明对外名字到子模块的映射，构造 `_LazyModule`。可见 megatron 子包对外暴露的能力分五类：`pipelines`（四个 `*_main`）、`convert`（两个转换函数）、`utils`（`prepare_mcore_model` / `initialize_megatron`）、`arguments`、`model`（`get_mcore_model`）、`trainers`。

**环境初始化**：`init_megatron_env()` 的职责是「让 Megatron 在 ms-swift 环境里跑得起来又跑得稳」，它打了一系列猴子补丁，但**不**直接打 mcore-bridge 的补丁（后者被推迟到参数校验阶段，见 4.3）：

[swift/megatron/init.py:208-224](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/init.py#L208-L224) 依次打补丁：`_patch_unified_memory`（避免 Megatron 创建 CUDA 统一内存池）、`_patch__batched_p2p_ops`（流水并行 P2P 通信 group 置 None）、`_patch_torch_FileSystemReader`（多线程加速 torch_dist checkpoint 读取）、`_patch_validate_non_overlapping_shards_metadata`（跳过过慢的元数据校验），最后打印 `megatron.core.__version__`。

> 小知识：这些补丁都是「打在第三方库（megatron / torch）身上的猴子补丁」，是 ms-swift 让 Megatron 适配自身运行环境的典型手段——不动 Megatron 源码，只在运行时替换其函数。

#### 4.1.4 代码实践

**实践目标**：在不真正跑训练的前提下，验证 `megatron` 命令的路由与 `swift.megatron` 包的对外接口是否符合本讲描述。

**操作步骤**：

1. 在已安装 ms-swift（含 megatron 可选依赖）的环境执行：
   ```bash
   megatron --help 2>&1 | head -20
   ```
   观察它是否报 `KeyError`（参考 `u1-l2`：`swift --help` 会报错，因为 `--help` 不在 `ROUTE_MAPPING`；`megatron --help` 同理）。再试：
   ```bash
   megatron export --help 2>&1 | head -40
   ```
   观察是否能正常打印 `MegatronExportArguments` 的参数列表。

2. 在 Python 里检查懒加载声明的对外名字：
   ```python
   import swift.megatron as mg
   # 懒加载下，直接访问结构表不可取，但可访问对外名字
   print(mg.megatron_export_main)      # 应为函数
   print(mg.convert_hf2mcore, mg.convert_mcore2hf)
   print(mg.get_mcore_model)
   ```

**需要观察的现象**：`megatron --help` 报错或无输出，而 `megatron export --help` 能列出参数；Python 里四个名字都能正常解析为对应对象（首次访问触发真实 import）。

**预期结果**：`megatron` 与 `swift` 共用 `cli_main`，故 `--help` 行为一致（子命令级才有效）；懒加载接口可按需访问。若未安装 megatron 依赖，第 2 步会在首次访问时抛 `ImportError`，属正常。

**待本地验证**：上述命令的实际输出依你本机安装的 megatron / mcore-bridge 版本而定，请以本地为准。

#### 4.1.5 小练习与答案

**练习 1**：`megatron` 命令的 `ROUTE_MAPPING` 为什么只有 `pt/sft/rlhf/export` 四项，而没有 `infer` / `deploy` / `eval`？

> **答案**：推理、部署、评测不需要 Megatron 的并行训练能力，复用普通 `swift infer/deploy/eval` 即可（它们加载的是转换后的 safetensors 模型）。`megatron` 命令只保留「必须用 Megatron 跑」的训练与导出子命令，职责最小化。

**练习 2**：`init_megatron_env()` 为什么要在 `swift/megatron/__init__.py` 的导入期就调用，而不是放在某个 pipeline 里？

> **答案**：因为 Megatron 的全局行为（P2P 通信、checkpoint 读取、统一内存）依赖这些补丁尽早生效；任何后续 `import megatron.core` 或分布式初始化都假定补丁已就位。放在导入期可保证「只要用到 megatron 子包，环境就是对的」，避免遗漏。

---

### 4.2 hf/mcore 权重互转

#### 4.2.1 概念说明

权重互转解决的是「同一个模型在两种格式间搬运」的问题：

- **HF 格式**（safetensors）：transformers / vLLM / SGLang 直接可读，单文件分片。
- **mcore 格式**（torch_dist）：Megatron 训练用的格式，按 TP/PP/EP 切片，目录里是 `.pt` 分片 + 元数据。

两者不仅文件形态不同，**参数命名与切分方式也不同**：例如一个 `q_proj` 权重，HF 里是一个完整张量，mcore 里可能被 TP 切成 `tensor_model_parallel_size` 份并改名。所以互转不是简单复制，而是「按规则逐张量搬运 + 改名 + 重切分」。

ms-swift 把这件事封装成 `megatron export` 命令，提供两个方向：

- `--to_mcore true`：HF safetensors → mcore torch_dist（准备给 Megatron 训练）。
- `--to_hf true`：mcore torch_dist → HF safetensors（训练完准备部署）。

并额外提供 `--test_convert_precision true`，在转换后用同一份输入对比 HF 模型与 mcore 模型的 logits，量化转换误差。

#### 4.2.2 核心流程

无论哪个方向，转换的骨架都是同一套「四步法」：

```
1. get_mcore_model(args, hf_config)   # 按 HF config 构建一个空的 mcore 模型骨架
2. bridge = mg_model.config.bridge    # 取出挂在模型上的桥接对象
3. bridge.load_weights(...)           # 把源格式权重灌进骨架（HF→mcore 用 load_weights）
4. save:                              # 把灌好权重的模型写成目标格式
     - 目标是 HF    → bridge.save_weights(...)   （safetensors）
     - 目标是 mcore → save_mcore_checkpoint(...)  （torch_dist）
```

关键点：**桥接对象 `bridge` 既是「加载器」又是「保存器」**，它挂在 `mg_model.config.bridge` 上，由 `get_mcore_model` 在建模型时一并装配（见 4.3）。源/目标格式不同时，走的函数也不同：从 HF 读用 `bridge.load_weights`，从 mcore 读用 `load_mcore_checkpoint`；存成 HF 用 `bridge.save_weights`，存成 mcore 用 `save_mcore_checkpoint`。

LoRA 的情况多一步：先 `prepare_mcore_model` 在 mcore 模型上挂 LoRA 增量，再灌 adapter 权重，可选 `merge_and_unload` 把增量并入基座。

转换用的模型骨架还带一组「转换友好」的参数（见下方 `convert_kwargs`），核心是 `use_cpu_initialization=True`——在 CPU 上初始化参数，避免转换大模型时把 GPU 显存撑爆。

#### 4.2.3 源码精读

**转换友好参数**：`convert.py` 顶部定义了一组转换专用 kwargs，关闭训练才需要的功能、开启省显存初始化：

[swift/megatron/convert.py:19-29](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py#L19-L29) 设定 `use_cpu_initialization=True`（CPU 建模型省显存）、`no_save_optim/no_load_optim/no_save_rng/no_load_rng=True`（转换时不碰优化器/RNG 状态）、`attention_backend='unfused'`、`padding_free=False`、`recompute_granularity='none'`。MoE 模型还会追加 `moe_grouped_gemm=True`。

**HF → mcore**（函数式版本）：

[swift/megatron/convert.py:32-56](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py#L32-L56) 是 `convert_hf2mcore` 的核心：先用 `prepare_model_template` 加载 HF 模型拿到 `hf_config`，再 `get_mcore_model(megatron_args, hf_config)` 建 mcore 骨架，然后 `bridge.load_weights([mg_model], args.model_info.model_dir)` 把 HF safetensors 灌进去，最后 `save_mcore_checkpoint(...)` 写成 torch_dist。`bridge` 取自 `mg_model.config.bridge`（第 54 行）。

**mcore → HF**（函数式版本）：

[swift/megatron/convert.py:67-101](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/convert.py#L67-L101) 是 `convert_mcore2hf`：方向反过来，先用 `load_mcore_checkpoint(..., load_arg='mcore_model')` 把 torch_dist 灌进骨架，再 `bridge.save_weights([mg_model], args.output_dir, args=megatron_args, processor=processor)` 写成 safetensors。第 92-96 行处理可选的 LoRA 合并。

**真正被 `megatron export` 调用的管道版本**：`MegatronExport` 是 `SwiftPipeline` 的子类（参考 `u5-l4` 的模板方法模式），`run()` 按 `to_hf` / `to_mcore` 分流：

[swift/megatron/pipelines/export/export.py:22-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L22-L28) `run()` 只做分流：`to_hf` 走 `convert_mcore2hf()`，`to_mcore` 走 `convert_hf2mcore()`。

管道版 `convert_mcore2hf` 与函数版逻辑一致，但更完整地处理了 LoRA 与精度测试：

[swift/megatron/pipelines/export/export.py:30-64](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L30-L64) 展示了「建骨架 → 取 bridge → 灌权重（mcore 或 HF 二选一）→ 可选挂 LoRA/合并 → `bridge.save_weights` 写 safetensors」的完整链条。注意第 39、43、58 行三处对 `bridge` 的使用：取、load、save。

管道版 `convert_hf2mcore`：

[swift/megatron/pipelines/export/export.py:83-116](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L83-L116) 方向反过来，最终用 `save_mcore_checkpoint(args, [mg_model], peft_format=save_peft_format)` 写 torch_dist（第 116 行）。`save_peft_format` 由 `tuner_type == 'lora' and not merge_lora` 决定，决定 checkpoint 是只存 LoRA 增量还是全量。

**两种保存格式背后的实现**：

[swift/megatron/utils/megatron_lm_utils.py:238-292](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/megatron_lm_utils.py#L238-L292) 是 `save_mcore_checkpoint`：它构造 state_dict，选 `TorchDistSaveShardedStrategy`（mcore 0.17+）或默认策略，包一层 `FullyParallelSaveStrategyWrapper` 按 DP 组并行存盘，最终调 `dist_checkpointing.save` 写出 torch_dist 目录。这就是「存成 mcore」的底层。

**转换精度校验**：`--test_convert_precision true` 会在转换后跑一次「同输入双模型前向对比」：

[swift/megatron/utils/convert_utils.py:198-229](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/utils/convert_utils.py#L198-L229) 是 `test_convert_precision`：用 template 编码同一份示例输入，分别喂给 HF 模型与 mcore 模型前向，取 logits 比较。多模态模型会忽略视觉模块只比语言模型部分（第 224-225 行 `ignore_modules`）。最终输出 `mean_diff` 等指标，用于判断转换是否引入精度偏差。

#### 4.2.4 代码实践

**实践目标**：用 `megatron export` 把一个 HF 模型转成 mcore 格式，再转回 HF，并用 `--test_convert_precision true` 观察往返误差。

**操作步骤**（需要已安装 megatron 与 mcore-bridge，且有多卡）：

1. safetensors → torch_dist：
   ```bash
   CUDA_VISIBLE_DEVICES=0,1 NPROC_PER_NODE=2 \
   megatron export \
       --model Qwen/Qwen2.5-0.5B-Instruct \
       --output_dir Qwen2.5-0.5B-mcore \
       --to_mcore true \
       --tensor_model_parallel_size 2 \
       --test_convert_precision true
   ```
2. torch_dist → safetensors（用第 1 步产物作为 `--mcore_model`）：
   ```bash
   CUDA_VISIBLE_DEVICES=0,1 NPROC_PER_NODE=2 \
   megatron export \
       --mcore_model Qwen2.5-0.5B-mcore \
       --output_dir Qwen2.5-0.5B-roundtrip \
       --to_hf true \
       --tensor_model_parallel_size 2 \
       --test_convert_precision true
   ```
3. 对比原始模型与往返模型某一张量的数值：
   ```python
   # 示例代码：加载两份权重，比较同名张量
   from safetensors.torch import load_file
   import glob, torch
   def load_dir(d):
       out = {}
       for f in glob.glob(f'{d}/*.safetensors'):
           out.update(load_file(f))
       return out
   a = load_dir('<原始模型目录>')      # HF hub 缓存目录
   b = load_dir('Qwen2.5-0.5B-roundtrip')
   k = sorted(set(a) & set(b))[0]
   print(k, (a[k] - b[k]).abs().max().item())  # 期望接近 0
   ```

**需要观察的现象**：两次 `megatron export` 都打印 `mean_diff` 很小（纯文本模型通常在 1e-5 ~ 1e-3 量级，受 dtype 影响）；第 3 步张量最大差值接近 0（bfloat16 下可能有轻微数值误差）。

**预期结果**：往返后权重数值一致（在浮点误差范围内），证明 hf↔mcore 互转是无损的（在给定 dtype 下）。`mean_diff (with loss)` 字段对纯文本模型应很小。

**待本地验证**：本实践依赖 megatron/mcore-bridge 安装与多卡环境，且实际 `mean_diff` 数值依模型与 dtype 而定，请以本地输出为准。若无 GPU 环境，可改为「源码阅读型实践」：跟踪 `MegatronExport.convert_hf2mcore`（[export.py:83-116](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L83-L116)），在每一行旁注明它调用的是 `bridge` 还是 `save_mcore_checkpoint`，以此理清「哪种方向走哪个函数」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `convert_kwargs` 要设 `use_cpu_initialization=True`？如果在 GPU 上初始化会怎样？

> **答案**：转换大模型时，先在 GPU 上建一份空骨架会占用与模型等量的显存，再灌权重时还要额外空间，容易 OOM。CPU 初始化让骨架常驻内存，把 GPU 留给实际的权重搬运与（可能的）前向校验。代价是初始化略慢，但转换任务对速度不敏感。

**练习 2**：`bridge.load_weights` 与 `load_mcore_checkpoint` 都能「往 mcore 模型里灌权重」，它们什么时候分别使用？

> **答案**：源是 HF safetensors 时用 `bridge.load_weights`（桥接负责把 HF 命名/切分翻译成 mcore）；源已经是 mcore torch_dist 时用 `load_mcore_checkpoint`（直接按 Megatron 的 dist_checkpointing 加载，无需翻译）。二者对应「跨格式」与「同格式恢复」两种场景。

**练习 3**：`megatron export --to_mcore` 与 `megatron export --to_hf` 能否同时为 true？

> **答案**：不能。`MegatronExport.run()` 用 `if args.to_hf: ... elif args.to_mcore: ...` 互斥分流（见 [export.py:25-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L25-L28)），`to_hf` 优先。一次 export 只做一个方向的转换。

---

### 4.3 mcore-bridge 设计

#### 4.3.1 概念说明

`mcore-bridge` 是一个**独立于 ms-swift 的外部包**（仓库 [modelscope/mcore-bridge](https://github.com/modelscope/mcore-bridge)），它是整个 Megatron-SWIFT 易用性的核心。它的设计目标是官方文档原话——「让 Megatron 训练像 transformers 一样简单」。

它做的事情可以归结为一句：**把 Megatron 的 torch_dist 格式与命名细节藏起来，让上层只看见 safetensors 进、safetensors 出**。具体包括：

1. 直接读 safetensors 训练、直接存 safetensors 部署，无需手动转换；
2. 双向转换兼容 LoRA 增量权重；
3. 支持 Megatron→vLLM 权重热同步（给 GRPO/GKD 等 RL 算法用，见 `u7-l4`、`u6-l2` 的 `GRPOVllmEngine`）；
4. 支持多机转换超大模型；
5. 兼容 Dense / MoE / 多模态多种架构。

ms-swift 通过 `bridge_backend` 参数在**两个**桥接后端间选择：默认的 `mcore-bridge`（功能全）和可选的 `megatron-bridge`（NVIDIA 官方 `megatron.bridge`，较新但功能受限）。

#### 4.3.2 核心流程

mcore-bridge 在 ms-swift 中的接入分三步：

```
① 建模型时派发：get_mcore_model(args, hf_config)
     └─ 据 args.bridge_backend 选 mcore-bridge 或 megatron-bridge
     └─ mcore-bridge 分支：get_mcore_model_config(args, hf_config) → ModelConfig → _get_mcore_model(config)
     └─ 把桥接对象挂到 model.config.bridge 上

② 用模型时取桥接：bridge = mg_model.config.bridge
     └─ bridge.load_weights(...)  /  bridge.save_weights(...)

③ 增强桥接：_patch_mcore_bridge() 给 GPTBridge.save_weights 打补丁
     └─ 让「存 safetensors」同时产出完整的可部署 HF 模型（config + tokenizer + 额外文件）
```

其中 `get_mcore_model_config` 把 HF config 翻译成 mcore `ModelConfig` 的过程，用了一个巧妙的「自动字段匹配」：遍历 `ModelConfig` 的所有 dataclass 字段，从 `args` 里同名字段取值。这样新增一个 mcore 配置项时，只要 `MegatronArguments` 里有同名字段，就自动透传，无需手写映射。

配置构建里还有一个值得记的工程细节——线程数自适应。转换大模型时，torch_dist 的分片保存用多线程加速，线程数按模型体积估算：

\[
\text{checkpoint\_size(GB)} = \frac{n_\text{params} \times \text{bits}}{8 \times 10^9}, \qquad
\text{thread\_count} = \max\!\left(\left\lceil \frac{\text{checkpoint\_size}}{10} \right\rceil,\ 2\right)
\]

即每 10GB 权重开一个线程，至少 2 个。这出现在 `convert.py` 的 `convert_hf2mcore` 与 `convert_mcore2hf` 里（通过 `patch_torch_dist_shard(args.thread_count)` 注入到保存策略）。

#### 4.3.3 源码精读

**外部包导入**：ms-swift 并不自己实现权重搬运，而是直接从 `mcore_bridge` 包导入核心构件：

[swift/megatron/model/utils.py:3-5](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L3-L5) 从 `mcore_bridge` 导入 `ModelConfig`（mcore 模型配置）、`get_mcore_model`（建模型）、`hf_to_mcore_config`（HF config → mcore kwargs 的翻译器）。这三个是 mcore-bridge 暴露给 ms-swift 的全部核心 API。

**HF config → ModelConfig**：`get_mcore_model_config` 是翻译的核心：

[swift/megatron/model/utils.py:39-81](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L39-L81) 先 `hf_to_mcore_config(hf_config)` 拿到基础 kwargs，再设置 `mcore_model_type`、`hf_config`；第 43-47 行遍历 `fields(ModelConfig)` 从 `args` 同名字段取值（自动匹配）；随后处理 dtype、PP 首尾层层数、FP8/FP4、MoE、attention backend、padding_free 等约束，最终 `ModelConfig(**kwargs)`。注意第 68-70 行：非 MoE 模型强制 `expert_model_parallel_size=1`、`expert_tensor_parallel_size=1`，避免无意义并行。

**后端派发**：`get_mcore_model` 是选择 mcore-bridge 还是 megatron-bridge 的总开关：

[swift/megatron/model/utils.py:198-205](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L198-L205) 据 `args.bridge_backend` 派发：`megatron-bridge` 走 `_get_megatron_bridge_model`（NVIDIA 后端），否则走默认的 mcore-bridge 分支（`get_mcore_model_config` + `_get_mcore_model`）。

**可选的 megatron-bridge 后端**：这是当前 HEAD（commit `3d61b9318`）刚加入的能力，用 `MegatronBridgeBackend` 适配 NVIDIA 的 `AutoBridge`：

[swift/megatron/model/utils.py:84-100](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L84-L100) `MegatronBridgeBackend` 类的文档字符串明确列出限制：**不支持 LoRA、不支持多模态、不支持 FP8 导出**。它的 `load_weights` / `save_weights` 在 `peft_format=True` 时直接 `raise NotImplementedError`（见 [utils.py:102-108](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L102-L108) 与 [utils.py:134-143](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L134-L143)），并提示「请用 `bridge_backend='mcore-bridge'` 做 LoRA」。

[swift/megatron/model/utils.py:208-295](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L208-L295) 是 `_get_megatron_bridge_model`：用 `AutoBridge.from_hf_config` 建 provider，同样用「遍历 provider 字段从 args 取值」的自动匹配（第 229-234 行），最后第 291-292 行把 `backend` 挂到 `model.config.bridge`——与 mcore-bridge 分支保持一致的挂载点，这样上层 `bridge.load_weights/save_weights` 代码可以复用。

**bridge_backend 参数与校验**：`MegatronArguments` 用一个 `Literal` 字段切换后端，并在 `__post_init__` 里校验：

[swift/megatron/arguments/megatron_args.py:670-671](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L670-L671) 定义 `bridge_backend: Literal['mcore-bridge', 'megatron-bridge'] = 'mcore-bridge'`，默认仍是功能全的 mcore-bridge。

[swift/megatron/arguments/megatron_args.py:769-783](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L769-L783) 是 `_check_bridge_backend`：选 `megatron-bridge` 时检查包是否安装、并禁止 LoRA（`tuner_type != 'full'` 就报错）；选 `mcore-bridge` 时要求 `mcore-bridge>=1.4.0` 并**在此处**调用 `_patch_mcore_bridge()`（注意：补丁不在导入期打，而是在确认要用 mcore-bridge 后才打，避免无谓修改）。

[swift/megatron/arguments/megatron_args.py:813-820](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L813-L820) 进一步限制：`megatron-bridge` 不支持多模态、不支持非 `causal_lm` 的 task_type。

**桥接增强补丁**：这是 mcore-bridge 设计的点睛之笔。`mcore_bridge.GPTBridge.save_weights` 原本只写 safetensors 张量，不写 config/tokenizer——那样产物无法直接部署。ms-swift 用猴子补丁包了一层，让保存同时产出完整 HF 模型：

[swift/megatron/init.py:119-125](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/init.py#L119-L125) `_patch_mcore_bridge()` 先 `require_version('mcore-bridge>=1.4.0')`，导入 `GPTBridge`，保存原始 `save_weights` 为 `origin_save_weights`。

[swift/megatron/init.py:126-205](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/init.py#L126-L205) 定义增强版 `save_weights`：先调 `origin_save_weights` 写张量（第 135 行），然后在 master rank 上补齐 HF 产物——第 140-148 行用 `torch.device('meta')` 建一个不占显存的 `hf_model` 元模型（或调 `get_hf_meta_model`）；第 151-168 行处理 peft 格式（写 `peft_config`，多模态时用 `get_multimodal_target_regex` 展开 target_modules）；第 169-201 行处理全量格式（写 `hf_config`，处理 MTP 层数回写、FP8 `quantization_config`、deepseek_v4 `expert_dtype`，调 `custom_object_save` 与 `save_checkpoint` 写 tokenizer/processor/额外文件）；最后第 203 行 `dist.barrier()` 保证所有 rank 写完。

> 这段补丁是理解 mcore-bridge 价值的关键：**桥接对象只管「张量怎么搬」，ms-swift 的补丁管「搬完后怎么变成一个能直接 `swift infer` / `vllm serve` 的完整模型目录」**。两者分工，才实现了「训练存盘即可部署」。

#### 4.3.4 代码实践

**实践目标**：通过阅读源码与（可选的）小实验，理解 mcore-bridge 如何被装配与增强，并能说明其价值。

**操作步骤**（源码阅读型 + 可选运行）：

1. **跟踪桥接对象的装配链**：从 `megatron export --to_hf true` 入口出发，按顺序找到这三处，体会「建模型 → 挂 bridge → 用 bridge」：
   - `MegatronExport.convert_mcore2hf` 调 `get_mcore_model(args, hf_config)[0]`（[export.py:37](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L37)）；
   - `get_mcore_model` 据 `bridge_backend` 派发并最终把 backend 挂到 `model.config.bridge`（[utils.py:198-205](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L198-L205)）；
   - 管道里 `bridge = mg_model.config.bridge` 后调 `bridge.save_weights(...)`（[export.py:39,58](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/pipelines/export/export.py#L39)）。

2. **观察补丁生效**（可选，需装 mcore-bridge）：在 Python 里触发一次 mcore-bridge 补丁，确认 `GPTBridge.save_weights` 已被替换：
   ```python
   # 示例代码
   from mcore_bridge import GPTBridge
   print('patched?', GPTBridge.save_weights.__qualname__)
   # 触发 _check_bridge_backend -> _patch_mcore_bridge
   from swift.megatron.arguments import MegatronArguments
   # 构造一个最小 args 触发 __post_init__（需补全必填字段，略）
   ```
   若已通过 `megatron` 命令跑过一次训练/导出，`save_weights.__qualname__` 应显示为 `save_weights`（模块级函数）而非原方法。

3. **对比两个后端**：阅读 [megatron_args.py:769-783](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L769-L783) 与 [model/utils.py:84-100](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/model/utils.py#L84-L100)，列出 `megatron-bridge` 相比 `mcore-bridge` 缺失的三项能力。

**需要观察的现象**：装配链三处代码连贯一致（同一 `bridge` 对象被取、被用）；补丁后 `save_weights` 是替换后的函数；`megatron-bridge` 缺 LoRA、多模态、FP8 导出。

**预期结果**：能用自己的话说出「mcore-bridge = 张量搬运（外部包） + 产物补全（ms-swift 补丁）」的分工，以及默认用 mcore-bridge 的原因（功能全）。

**待本地验证**：第 2 步的实际 `__qualname__` 与触发方式依 mcore-bridge 版本而定，请以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_patch_mcore_bridge()` 不在 `init_megatron_env()` 里（导入期）调用，而在 `_check_bridge_backend` 里调用？

> **答案**：因为只有选 `bridge_backend='mcore-bridge'` 时才需要这个补丁；选 `megatron-bridge` 时用的是 `MegatronBridgeBackend`，不需要给 `GPTBridge` 打补丁。推迟到参数校验阶段可以「按需打补丁」，避免在用 NVIDIA 后端时无谓修改 `GPTBridge`，也避免在只 `import swift.megatron` 但不实际训练时强依赖 mcore-bridge 包。事实上 commit `3d61b9318` 正是把这个调用从 `init.py` 移到了 `_check_bridge_backend`。

**练习 2**：增强版 `save_weights` 为什么要用 `torch.device('meta')` 建 `hf_model`？

> **答案**：保存时只需要 HF 模型的「结构信息」来写 config、判断 target_modules、调 `custom_object_save`，并不需要它的实际权重（权重在 mcore 模型里）。用 `meta` 设备建模型不分配真实显存/内存，避免为了写 config 而额外加载一份完整模型，省下大量内存。

**练习 3**：假如你要训练一个多模态 MoE 模型并导出 LoRA，应该选哪个 `bridge_backend`？为什么？

> **答案**：必须选默认的 `mcore-bridge`。因为 `megatron-bridge` 同时不支持多模态（[megatron_args.py:815-817](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L815-L817)）和 LoRA（[megatron_args.py:777-779](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/megatron/arguments/megatron_args.py#L777-L779)），会在 `__post_init__` 直接报错。只有 `mcore-bridge` 同时具备这两项能力。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「架构认知 → 转换实操 → 设计理解」的小任务：

**任务**：为团队新同学写一份一页纸的「Megatron-SWIFT 权重流转备忘」，要求包含以下内容，并全部用本讲源码佐证：

1. **入口**：画出从 `megatron export --to_mcore true` 命令到 `MegatronExport.run()` 的分发路径（参考 4.1：setup.py 注册 → `_megatron/main.py` 路由 → `cli_main(is_megatron=True)` → `swift/cli/_megatron/export.py` → `megatron_export_main`）。
2. **转换骨架**：用四步法描述 `convert_hf2mcore` 的执行过程，标注每一步调用的函数与所在文件（参考 4.2 的 `get_mcore_model` → `bridge.load_weights` → `save_mcore_checkpoint`）。
3. **桥接分工**：用一句话说清「mcore-bridge 外部包负责什么、ms-swift 的 `_patch_mcore_bridge` 补丁负责什么」，并指出 `bridge` 对象挂在模型的哪个属性上（参考 4.3：`mg_model.config.bridge`）。
4. **后端选择**：列一张表对比 `mcore-bridge` 与 `megatron-bridge` 的能力差异（LoRA / 多模态 / FP8 导出 / 默认值），给出选型建议。

**验证方式**：把备忘交给一位没读过 megatron 源码的同学，让他据此回答「训练完一个 MoE-LoRA 模型，该用哪条命令、哪个后端导出可部署权重」。如果他答出「`megatron export --to_hf true`（或训练时 `--save_safetensors true`） + `mcore-bridge` 后端」，说明你的备忘抓住了本讲的核心。

**待本地验证**：若条件允许，配合 4.2.4 的往返转换实践，把真实 `mean_diff` 数值填进备忘，作为「转换无损」的证据。

## 6. 本讲小结

- `swift/megatron` 是与「普通 swift」并行的 Megatron 训练后端，共享上层骨架（参数/模板/数据集/pipeline 基类），`megatron` 命令经 `setup.py` 注册、`_megatron/main.py` 的 `ROUTE_MAPPING`（仅 `pt/sft/rlhf/export`）分发，复用 `cli_main(is_megatron=True)`。
- 包入口在导入期调 `init_megatron_env()` 打一系列环境补丁（P2P、checkpoint 读取、统一内存等），并用 `_LazyModule` 声明对外接口，遵循 swift 全包的懒加载范式。
- hf↔mcore 权重互转遵循「建骨架 → 取 bridge → 灌权重 → 存目标格式」四步法；HF↔mcore 用 `bridge.load_weights/save_weights`，mcore↔mcore 用 `load_mcore_checkpoint/save_mcore_checkpoint`；`--test_convert_precision` 用同输入双模型前向对比 logits 量化误差。
- `megatron export` 真正落到 `MegatronExport` 管道，`run()` 按 `to_hf`/`to_mcore` 互斥分流；转换用 `convert_kwargs`（`use_cpu_initialization=True` 等）省显存。
- mcore-bridge 是外部包，负责「张量搬运」（`hf_to_mcore_config`/`get_mcore_model`/`ModelConfig` + `bridge.load/save_weights`），ms-swift 的 `_patch_mcore_bridge` 补丁负责「产物补全」（写 config/tokenizer/peft_config），二者分工实现「存盘即可部署」。
- `bridge_backend` 在 `mcore-bridge`（默认，功能全）与 `megatron-bridge`（NVIDIA 官方，不支持 LoRA/多模态/FP8 导出）间切换；补丁按需在 `_check_bridge_backend` 里打，而非导入期。

## 7. 下一步学习建议

- 下一篇 **u9-l4 Megatron 训练流程** 将进入 `MegatronArguments` 的并行参数（TP/PP/SP/CP/EP/VPP）与 `megatron_sft_main` / `MegatronTrainer` 的训练循环，把本讲的「模型与权重」接入「训练器与并行」。建议先熟悉 `examples/megatron/sft.sh` 与 `examples/megatron/mcore_bridge/` 下的脚本。
- 若想深入桥接的权重映射规则，可阅读外部包 [mcore-bridge](https://github.com/modelscope/mcore-bridge) 的源码，对照本讲 `get_mcore_model_config` 的字段自动匹配逻辑。
- 若关注 RL 场景的权重热同步，可结合 `u6-l2` 的 `GRPOVllmEngine` 与 `u7-l4` 的多轮 rollout，阅读 `swift/megatron/trainers/rollout_mixin.py` 及 mcore-bridge 的 Megatron→vLLM 同步能力。
- 官方文档 [docs/source_en/Megatron-SWIFT/Mcore-Bridge.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/docs/source_en/Megatron-SWIFT/Mcore-Bridge.md) 给出了大量 `megatron export` 与训练的可运行命令，可作为本讲实践的补充。
