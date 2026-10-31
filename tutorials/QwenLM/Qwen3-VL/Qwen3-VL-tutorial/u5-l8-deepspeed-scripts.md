# DeepSpeed 配置与多规格训练脚本

## 1. 本讲目标

本讲是微调单元的收官之讲。读完本讲,你应该能够:

1. 逐字段读懂 `zero2.json` / `zero3.json` / `zero3_offload.json` 三份 DeepSpeed 配置,说清 ZeRO-2、ZeRO-3、offload 三档各自切分/搬移了哪些训练状态,付出什么代价。
2. 横向对比 `sft_qwen3_4b.sh`、`sft_7b.sh`、`sft_32b.sh`、`sft_30a3b.sh` 等官方脚本模板,按模型规模挑出正确的起点脚本并改写出自己的版本。
3. 理解 Qwen3-VL MoE 模型(如 30B-A3B)不支持 ZeRO-3 这一官方限制,以及它对脚本选型的硬约束。
4. 用一套显存估算公式,在训练开始前判断"我的卡能不能跑这个模型",并据此在 ZeRO-2 / ZeRO-3 / offload / LoRA 之间做出选择。

## 2. 前置知识

**训练状态的三座大山。** 在混合精度(本仓库脚本全部用 `--bf16`)下做全参微调,显存里除了模型权重,还要常驻三样东西:

- **参数(权重)**:bf16 存储约 2 字节/参数。
- **梯度**:与参数同形状,bf16 约 2 字节/参数。
- **优化器状态**:AdamW 为每个参数维护 fp32 权重副本、一阶动量 \(m\)、二阶动量 \(v\),共约 12 字节/参数。

三者相加约 **16 字节/参数**——这就是为什么"能推理"不等于"能训练":一个 2B 模型推理只要约 4 GB 显存,全参训练的静态开销却要约 32 GB(还不算激活值)。

**数据并行的冗余。** 最朴素的分布式训练是数据并行(DP):每张卡各存一份完整模型,各自算一小批数据的梯度,再求平均。简单可靠,但上面那 16 字节/参数的状态**每张卡都完整复制一遍**,卡越多冗余越多。

**ZeRO 的思路。** DeepSpeed 的 ZeRO(Zero Redundancy Optimizer)核心想法一句话:这些训练状态其实每张卡只需要一小片,何不切成 \(N\) 份分摊?

- **ZeRO-1**:只切分优化器状态;
- **ZeRO-2**:再切分梯度;
- **ZeRO-3**:连参数本身也切分,前向/反向用到哪层就临时收集(gather)哪层。

代价是卡间通信量增加、ZeRO-3 还有参数收集的调度开销——**用通信换显存**。

**Offload。** 如果切分之后显存仍然不够,还可以把优化器状态(甚至参数)整体搬到 CPU 内存,GPU 用时再通过 PCIe 传输——进一步**用速度换显存**。

**MoE(混合专家)模型。** 把 Transformer 的 FFN 层替换为多个"专家"网络加一个路由器,每个 token 只激活其中少数专家。总参数量很大、单 token 激活的参数量小(30B-A3B 即总参数约 30B、每 token 激活约 3B)。这一结构差异会影响分布式训练方案的选择,本讲 4.3 会展开。

**承接前讲。** 本讲只关心"多卡怎么协同、显存怎么省";训练入口主流程见 u5-l1,参数三分类见 u5-l2,`data_flatten` 拼接训练配套的 varlen 注意力补丁见 u5-l6,LoRA 见 u5-l7。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `qwen-vl-finetune/scripts/zero2.json` | ZeRO-2 配置:切分优化器状态 + 梯度 |
| `qwen-vl-finetune/scripts/zero3.json` | ZeRO-3 配置:再切分参数,含流式参数调度字段 |
| `qwen-vl-finetune/scripts/zero3_offload.json` | ZeRO-3 + CPU offload 配置:优化器与参数搬到 CPU 内存 |
| `qwen-vl-finetune/scripts/sft_qwen3_4b.sh` | Qwen3-VL-4B 全参 SFT 模板(zero3) |
| `qwen-vl-finetune/scripts/sft_7b.sh` | Qwen2.5-VL-7B 全参 SFT 模板(zero3) |
| `qwen-vl-finetune/scripts/sft_32b.sh` | Qwen2.5-VL-32B 全参 SFT 模板(zero3) |
| `qwen-vl-finetune/scripts/sft_30a3b.sh` | Qwen3-VL-30B-A3B(MoE)全参 SFT 模板(zero2) |
| `qwen-vl-finetune/scripts/sft_30a3b_lora.sh` | 同上模型的 LoRA 模板(zero2) |
| `qwen-vl-finetune/README.md` | 依赖版本、32B 显存要求、MoE 的 ZeRO-3 限制声明 |
| `qwen-vl-finetune/qwenvl/train/train_qwen.py` | 训练入口,含 DeepSpeed 模式下的权重保存分支 |

## 4. 核心概念与源码讲解

### 4.1 ZeRO 三档配置对比

#### 4.1.1 概念说明

`scripts/` 下的三份 JSON 是 DeepSpeed 引擎的配置文件,经 `--deepspeed <文件>` 传给训练入口。它们控制:数值精度、批次大小的接管方式、以及 `zero_optimization` 块里最重要的——**用哪一档 ZeRO、切什么、搬哪里**。

三份文件是层层递进的关系:

- `zero2.json`:切分优化器状态 + 梯度;
- `zero3.json`:在 ZeRO-2 基础上再切分参数,并新增一组长长的 `stage3_*` 字段管理"参数按需收集"的调度;
- `zero3_offload.json`:ZeRO-3 之上,再把优化器状态和参数搬到 CPU 内存。

#### 4.1.2 核心流程

DeepSpeed 配置在 HF Trainer 下的工作方式可以概括为:

```text
torchrun 启动 N 个进程
  → train_qwen.py 解析 --deepspeed scripts/zeroX.json
  → HF Trainer 的 DeepSpeed 集成读取该 JSON
  → 凡是值为 "auto" 的字段,由 TrainingArguments 在运行时填充
     (batch size、梯度累积、lr、bf16 开关……)
  → DeepSpeed 引擎按 stage 初始化:切分优化器状态/梯度/参数,
     offload 配置则把张量移到 CPU(pinned memory)
  → 训练循环中按需 reduce 梯度 / gather 参数
```

关键点:**JSON 里大量出现的 `"auto"` 不是"默认值",而是"交给 HF Trainer 填"**。这意味着批次大小、学习率等只在命令行脚本里维护一份,JSON 与 shell 脚本不会打架——这是本仓库脚本体系的分工设计。

#### 4.1.3 源码精读

先看 `zero2.json` 的公共部分:

[zero2.json:L1-L15](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero2.json#L1-L15) 定义了精度与批次接管:`fp16` 块是动态 loss scaling 的全套参数(`loss_scale: 0` 表示自动伸缩,`initial_scale_power: 16`、`hysteresis: 2` 等),仅在启用 fp16 时生效;`bf16.enabled: "auto"` 则跟随命令行的 `--bf16` 开关——bf16 数值指数范围大,天然不需要 loss scaling,所以三份配置里 fp16 块实际处于待命状态。`train_micro_batch_size_per_gpu`、`train_batch_size`、`gradient_accumulation_steps` 三个 `"auto"` 把批次大小的解释权交还给 `per_device_train_batch_size` 等命令行参数。

[zero2.json:L16-L22](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero2.json#L16-L22) 是 ZeRO-2 的核心:`"stage": 2`;`overlap_comm: true` 让梯度归约与反向传播重叠以隐藏通信延迟;`contiguous_gradients: true` 把梯度放进连续内存块减少碎片;`sub_group_size: 1e9` 按约 10 亿参数一组分批处理归约,避免一次性开辟超大缓冲;`reduce_bucket_size: "auto"` 同样由集成层填充。

ZeRO-3 与 ZeRO-2 的差异全在 `zero_optimization` 块:

[zero3.json:L16-L27](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3.json#L16-L27) 把 stage 改为 3,并新增五个 ZeRO-3 专属字段:`stage3_prefetch_bucket_size` 与 `stage3_param_persistence_threshold`(均为 `auto`)控制参数的预取与常驻阈值——ZeRO-3 下参数平时以分片形式散落各卡,前向/反向要用某层时才临时 all-gather 完整参数,预取窗口决定"提前多少取",持久化阈值决定"多小的参数干脆不切分、常驻各卡";`stage3_max_live_parameters: 1e9` 与 `stage3_max_reuse_distance: 1e9` 限制同时在显存中存活的完整参数量与参数复用距离,防止收集的参数堆积撑爆显存。最后的 [zero3.json:L26](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3.json#L26) `"stage3_gather_16bit_weights_on_model_save": true` 尤其重要:ZeRO-3 下每张卡只有参数分片,**保存 checkpoint 时必须跨卡收集完整 bf16 权重**,否则存出来的是残缺模型(与 4.3 节保存分支呼应)。

`zero3_offload.json` 在 ZeRO-3 之上做两层扩展:

[zero3_offload.json:L13-L29](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3_offload.json#L13-L29) 新增顶层的 `optimizer`(AdamW)与 `scheduler`(WarmupLR)块,所有超参都是 `"auto"`——由 HF Trainer 从 `TrainingArguments`(如 `--learning_rate`、`--optim`、`--lr_scheduler_type`)注入,所以用这份配置**不需要**在 JSON 里写死学习率。

[zero3_offload.json:L32-L39](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3_offload.json#L32-L39) 是本文件的灵魂:`offload_optimizer` 与 `offload_param` 两块都设 `"device": "cpu"`、`"pin_memory": true`,即把优化器状态与参数都卸载到 CPU 锁页内存。锁页内存可以让 CPU↔GPU 传输走 DMA 直达,代价是这部分内存会被锁定不可换出。其余如 [zero3_offload.json:L40-L55](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3_offload.json#L40-L55) 的 `gradient_clipping: "auto"`(对应 `--max_grad_norm`)、`steps_per_print`、`wall_clock_breakdown` 属于常规开关。

一个值得注意的细节:同样语义的"保存时收集权重"字段,`zero3.json` 写作 `stage3_gather_16bit_weights_on_model_save`([zero3.json:L26](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3.json#L26)),而 `zero3_offload.json` 写作不带前缀的 `gather_16bit_weights_on_model_save`([zero3_offload.json:L48](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3_offload.json#L48))。两处写法语义相同,属于新旧两种键名风格并存——自己改配置时请保持字段拼写与所选 DeepSpeed 版本兼容,拼错的键会被静默忽略。

三份配置的对照表:

| 字段 | zero2 | zero3 | zero3_offload |
| --- | --- | --- | --- |
| `zero_optimization.stage` | 2 | 3 | 3 |
| 切分优化器状态 + 梯度 | ✅ | ✅ | ✅ |
| 切分参数(`stage3_*` 调度字段) | ❌ | ✅ | ✅ |
| `offload_optimizer` → CPU | ❌ | ❌ | ✅ |
| `offload_param` → CPU | ❌ | ❌ | ✅ |
| 顶层 `optimizer`/`scheduler` 块 | ❌ | ❌ | ✅ |
| 单卡静态显存(全参训练) | 最高 | 中 | 最低 |
| 相对速度 | 最快 | 略慢 | 最慢(PCIe 搬运) |

#### 4.1.4 代码实践

1. **实践目标**:用脚本自动对比三份 JSON,产出字段级差异表,代替肉眼比对。
2. **操作步骤**:在 `qwen-vl-finetune/` 目录外任选位置保存下面脚本(示例代码,非仓库原有文件)并运行:

```python
# compare_zero.py(示例代码)
import json, itertools

paths = {
    "zero2": "qwen-vl-finetune/scripts/zero2.json",
    "zero3": "qwen-vl-finetune/scripts/zero3.json",
    "zero3_offload": "qwen-vl-finetune/scripts/zero3_offload.json",
}
cfg = {name: json.load(open(p)) for name, p in paths.items()}

def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out

flat = {n: flatten(c) for n, c in cfg.items()}
all_keys = sorted(set().union(*[set(f) for f in flat.values()]))
for k in all_keys:
    vals = [flat[n].get(k, "—") for n in flat]
    mark = "" if len(set(map(str, vals))) == 1 else "  <-- 差异"
    print(f"{k:55s} {vals}{mark}")
```

3. **需要观察的现象**:输出中带 `<-- 差异` 的行应恰好落在 4.1.3 对照表列出的位置——`stage`、五个 `stage3_*` 字段、两个 `offload_*` 块、顶层 `optimizer`/`scheduler` 等;其余字段(如 `fp16`、`bf16`、三个批次 `"auto"`)三份完全一致。
4. **预期结果**:差异行数量在十行左右,且全部集中在 `zero_optimization` 块与 offload 新增块内。若发现差异出现在别处,说明仓库配置有更新,应以实际输出为准。
5. 该脚本只读 JSON,可放心运行;运行结果与表不符时**待本地验证**后修正笔记。

#### 4.1.5 小练习与答案

**练习 1**:为什么三份配置里 `fp16` 块的 `loss_scale: 0` 等参数实际不起作用?
**答案**:三份 shell 脚本都传了 `--bf16`,集成层据此把 `bf16.enabled` 置真、`fp16.enabled` 置假,fp16 的动态 loss scaling 参数随之失效;bf16 指数位宽,不需要 loss scaling。fp16 块保留是为了在纯 fp16 环境下复用同一份配置。

**练习 2**:把 `zero3.json` 里的 `stage3_gather_16bit_weights_on_model_save` 改成 `false` 会发生什么?
**答案**:保存 checkpoint 时各卡只落盘自己的参数分片,得到的不是完整模型,离线合并/推理会失败。训练过程本身不受影响,问题会在保存或加载时暴露。

**练习 3**:`zero2.json` 里没有 `stage3_*` 字段,为什么?
**答案**:ZeRO-2 不切分参数,参数在各卡上始终完整,不存在"按需收集参数"的调度问题,自然不需要预取桶、持久化阈值、最大存活参数等字段。

### 4.2 多规模脚本模板

#### 4.2.1 概念说明

官方为不同规格模型各给了一份 SFT 脚本:`sft_qwen3_4b.sh`(4B Dense)、`sft_7b.sh` 与 `sft_32b.sh`(Qwen2.5-VL 7B/32B Dense)、`sft_30a3b.sh` 与 `sft_30a3b_lora.sh`(Qwen3-VL 30B-A3B MoE,全参/LoRA)。它们**共享同一副骨架**,只改少数几个变量——学会读骨架,就能按需复制改写出自己的脚本。

#### 4.2.2 核心流程

每份脚本的骨架都是:

```text
环境变量兜底:MASTER_ADDR / MASTER_PORT / NNODES(默认单机)
  ↓
声明变量:deepspeed 配置、模型 ID、lr、batch_size、grad_accum_steps、数据集、输出目录
  ↓
拼装 args 长字符串(全部命令行参数)
  ↓
torchrun --nproc_per_node=${NPROC_PER_NODE} \
         --master_addr=... --master_port=... \
         qwenvl/train/train_qwen.py ${args}
```

五份脚本横向对比(逐项来自源文件):

| 脚本 | 模型 | deepspeed 配置 | lr | batch_size | grad_accum | 官方显存备注 |
| --- | --- | --- | --- | --- | --- | --- |
| `sft_qwen3_4b.sh` | Qwen3-VL-4B-Instruct | zero3.json | 1e-5 | 4 | 4 | — |
| `sft_7b.sh` | Qwen2.5-VL-7B-Instruct | zero3.json | 2e-7 | 4 | 4 | — |
| `sft_32b.sh` | Qwen2.5-VL-32B-Instruct | zero3.json | 2e-7 | 2 | 8 | 8×80G(README) |
| `sft_30a3b.sh` | Qwen3-VL-30B-A3B-Instruct | **zero2.json** | 1e-5 | 1 | 4 | 32×80G(脚本注释) |
| `sft_30a3b_lora.sh` | 同上 | **zero2.json** | 1e-5 | 1 | 4 | 同上,另加 LoRA |

规模越大,`batch_size` 越小、`grad_accum_steps` 越大——用梯度累积换回有效批次,这正是 u5-l2 讲过的"有效批次 = GPU 数 × per_device_train_batch_size × gradient_accumulation_steps"的实例。

#### 4.2.3 源码精读

以 `sft_30a3b.sh` 为例看骨架。开头三行做分布式环境兜底:[sft_30a3b.sh:L4-L6](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L4-L6) 用 Shell 的 `${VAR:-default}` 语法给 `MASTER_ADDR`(默认 127.0.0.1)、`MASTER_PORT`(默认用 `shuf` 在 20001-29999 随机取一个端口,避免多任务撞端口)、`NNODES`(默认 1)赋值,已设环境变量则尊重外部值,方便接入多机调度器。

[sft_30a3b.sh:L8-L11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L8-L11) 是全脚本最关键的注释:`# MoE model only supports zero2` 与 `# this script could run on 32 80G GPU`,然后 `deepspeed=./scripts/zero2.json`。这份 30B-A3B(MoE)脚本不用 zero3 不是偏好而是硬限制,细节在 4.3 节展开。

[sft_30a3b.sh:L13-L19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L13-L19) 集中声明可调变量:模型 ID、`lr=1e-5`、`batch_size=1`、`grad_accum_steps=4`——改脚本只需动这几行,不必翻动后面 30 行的 args。

[sft_30a3b.sh:L32-L62](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L32-L62) 的参数串与 u5-l2 的三分类一一对应:`--deepspeed` 挂配置、`tune_mm_vision=False` 而 `tune_mm_mlp/tune_mm_llm=True`(官方全参微调不训视觉编码器)、`--data_flatten True`(拼接训练,须配 u5-l6 的 varlen 补丁)、`--max_pixels 50176 --min_pixels 784`(注意:50176 = 64×28²,是 28 口径遗产,Qwen3-VL 的 32 口径下折合约 49 个视觉 token/图,承接 u5-l2)、`--model_max_length 8192`、`--gradient_checkpointing True`(以重算换激活显存,是这些大模型脚本能跑起来的前提之一)。

最后 [sft_30a3b.sh:L65-L68](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L65-L68) 用 torchrun 拉起训练:`--nproc_per_node` 取自环境变量 `NPROC_PER_NODE`(启动前需 `export NPROC_PER_NODE=8` 之类),配合 `--master_addr/--master_port` 完成进程组握手。这也承接了 u5-l1 的结论:**单卡也必须用 torchrun 启动**。

再看两份对照脚本确认"只改变量"的设计:[sft_32b.sh:L9-L17](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_32b.sh#L9-L17) 换成 zero3.json、32B 模型、`lr=2e-7`、`batch=2`、`accum=8`(模型变大→单卡批减半、累积翻倍);[sft_qwen3_4b.sh:L9-L17](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_qwen3_4b.sh#L9-L17) 则是 4B 起步模板,`lr=1e-5`、`batch=4`。其余三十多行参数五份脚本几乎逐字相同——这就是"模板"的含义。

一个需要带着审慎眼光看的点:README 的通用建议 [README.md:L313](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L313) 写 "the suggested learning rate is from 1e-6 to 2e-7",但 4B 与 30B-A3B 脚本实际用 `1e-5`。两者并不矛盾到底——README 区间更像针对大模型全参微调的保守建议,而脚本是最贴近官方实践的实例。**选脚本时以规模最接近的模板为准,再参考 README 区间微调。**

#### 4.2.4 代码实践

1. **实践目标**:吃透"五份脚本一副骨架",亲手把 4B 模板改写成单卡可跑的 2B 版本。
2. **操作步骤**:
   - 复制 `scripts/sft_qwen3_4b.sh` 为 `scripts/my_sft_2b.sh`(放在自己的实验目录亦可,勿提交);
   - 修改四处:`llm=Qwen/Qwen3-VL-2B-Instruct`;`datasets=` 换成你在 u4-l3 注册过的数据集名;`batch_size` 视显存从 1 起试;`report_to wandb` 若无账号改为 `none`;
   - 启动前设置 `export NPROC_PER_NODE=1`,再 `bash scripts/my_sft_2b.sh`。
3. **需要观察的现象**:日志开头 DeepSpeed 会打印初始化信息(含 zero stage);随后进入训练循环,每步打印 loss。
4. **预期结果**:2B 模型在 zero3 下单卡即可完成前向反向(显存是否够取决于卡的容量,24G 参考 4.4 的选型结论);loss 从首个 step 起为有限数值,不出现 NaN/显存 OOM。
5. 显存不足的报错与缓解方案**待本地验证**;若 OOM,按 4.4 的决策路径换 `zero3_offload.json` 或改走 LoRA。

#### 4.2.5 小练习与答案

**练习 1**:五份脚本为什么都用 `shuf` 随机选 MASTER_PORT?
**答案**:同机多任务(例如同时跑两个实验)时,固定端口会冲突导致 torchrun 握手失败;从 20001-29999 随机取值大幅降低撞端口概率,同时保留用环境变量 `MASTER_PORT` 显式指定的能力。

**练习 2**:32B 脚本 `batch_size=2、grad_accum_steps=8`,4B 脚本 `batch_size=4、grad_accum_steps=4`,有效批次差异说明了什么设计取向?
**答案**:假设卡数相同,两份脚本的有效批次分别是 16N 与 16N,数量级一致——模型变大时官方选择"单卡批减半 + 累积翻倍",在显存受限下维持相近的有效批次与训练稳定性,而不是牺牲总批次。

**练习 3**:`sft_30a3b_lora.sh` 与 `sft_30a3b.sh` 几乎相同,为什么还要单独给一份?
**答案**:它固定了 MoE 场景的 LoRA 参考配置([sft_30a3b_lora.sh:L41](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L41) 加了 `--lora_enable True`),把"MoE 只能 zero2"的约束连同 LoRA 路径一并示范,省去用户自己组合踩坑。

### 4.3 MoE 模型的 ZeRO-3 限制

#### 4.3.1 概念说明

仓库对 Qwen3-VL MoE 模型(如 30B-A3B)有一条**硬性约束:不支持 DeepSpeed ZeRO-3,只能用 ZeRO-2**。这不是脚本作者的偏好,而是 README 中明文声明的当前限制,并且还附带第二条:HF 官方实现暂不包含 MoE 的 load balancing loss(负载均衡辅助损失)。

#### 4.3.2 核心流程

这条限制如何落到工程上:

```text
README 声明限制(MoE 不支持 ZeRO-3)
  → sft_30a3b.sh / sft_30a3b_lora.sh 显式选用 zero2.json
     (脚本注释 "MoE model only supports zero2")
  → ZeRO-2 不切分参数 → 各卡常驻完整模型副本
     → 需要更多/更大的卡来装下 30B 权重(脚本注释:32×80G)
  → 保存 checkpoint 走完整参数路径(无需跨卡 gather)
```

#### 4.3.3 源码精读

限制的原文在 [README.md:L317](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L317):"The Qwen3VL MoE model does not support DeepSpeed with ZeRO-3. Additionally, Hugging Face's official implementation does not include support for load balancing loss currently."——两条信息:ZeRO-3 不可用;load balancing loss 缺失(训练 MoE 时路由器可能把过多 token 挤向少数专家而没有纠正项,这是效果层面的风险,仓库如实告知)。README 未展开技术原因;从通用背景理解,ZeRO-3 要求对参数做按需收集/释放,而 MoE 的专家被哪个 token 激活由路由动态决定,参数访问模式不再是普通 Dense 模型的顺序前向,给 ZeRO-3 的参数调度带来复杂性——此段是帮助理解的背景说明,官方未在仓库中给出详细论证。

脚本侧的落实即 [sft_30a3b.sh:L8-L11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L8-L11) 的注释与 `zero2.json` 选择;LoRA 版同样如此([sft_30a3b_lora.sh:L9-L11](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b_lora.sh#L9-L11))。

另一个与 DeepSpeed 强相关的源码点是保存分支:[train_qwen.py:L52-L58](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L52-L58) 中 `safe_save_model_for_hf_trainer` 检测到 `trainer.deepspeed` 存在时,先 `torch.cuda.synchronize()` 再直接 `trainer.save_model(output_dir)`——把保存完全交给 DeepSpeed/HF 集成处理(ZeRO-3 下由 4.1 讲的 gather 开关负责聚合完整权重);非 DeepSpeed 路径([train_qwen.py:L60-L64](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L60-L64))才手工把 state_dict 逐项搬去 CPU 再落盘。理解这个分支,排查"保存出来的 checkpoint 不完整"类问题就有了抓手:ZeRO-3 + 关闭 gather 开关 → 分片落盘;ZeRO-2 → 各卡本就有完整参数,无此顾虑。

#### 4.3.4 代码实践

1. **实践目标**:确认"限制只影响 MoE、且两份 MoE 脚本行为一致",并理解若强行换 zero3 会发生什么。
2. **操作步骤**:
   - 运行 `diff qwen-vl-finetune/scripts/sft_30a3b.sh qwen-vl-finetune/scripts/sft_30a3b_lora.sh`;
   - 运行 `grep -n "zero" qwen-vl-finetune/scripts/sft_30a3b*.sh`;
   - 做一个"思想实验"并写进笔记:把 30a3b 脚本的 `deepspeed=./scripts/zero2.json` 改成 `zero3.json`,预测运行结果。
3. **需要观察的现象**:diff 输出应只有寥寥数行(run_name、LoRA 参数、batch 写法差异);grep 确认两份 MoE 脚本都指向 zero2.json。
4. **预期结果**:diff 差异行数个位数,核心新增即 `--lora_enable True` 一行。思想实验的预期答案:README 已声明不支持,不应期待其正常训练;实际表现(报错位置与形式)与 deepspeed 版本相关,**待本地验证**——不建议在宝贵的 GPU 时间上真的去跑,写下预测再对照社区反馈即可。
5. 若你手头确有多卡环境并验证了行为,把报错原文记录到笔记,这是比任何文档都硬的证据。

#### 4.3.5 小练习与答案

**练习 1**:MoE 模型不能用 ZeRO-3,直接后果是什么?
**答案**:ZeRO-2 不切分参数,每张卡都要常驻完整模型权重,无法靠增加卡数摊薄"参数"这部分显存;30B-A3B 因此需要 32×80G 这样的配置(脚本注释),而不是像 32B Dense 那样 8×80G(README)。

**练习 2**:load balancing loss 缺失意味着什么?
**答案**:MoE 路由器缺少促使 token 均匀分布到各专家的辅助损失,微调时可能出现专家负载不均,影响训练效果与效率。这是仓库 README 明示的当前实现状态,选择微调 MoE 模型时需纳入考量。

**练习 3**:ZeRO-2 下 `safe_save_model_for_hf_trainer` 走 deepspeed 分支,为什么不必担心分片落盘?
**答案**:ZeRO-2 只切分优化器状态与梯度,参数在每张卡上是完整的,保存时无需跨卡 gather 参数;需要 gather 的是 ZeRO-3(参数也被切分)的场景。

### 4.4 显存估算方法

#### 4.4.1 概念说明

选哪档 ZeRO,本质是一道显存应用题。本节给出一套够用的估算方法,让你在训练开始前就能判断可行性。模型记参数量为 \(P\)(Qwen3-VL-2B 实际约 \(2.2\times10^9\),估算取 \(2\times10^9\) 即可),卡数为 \(N\),混合精度 + AdamW。

#### 4.4.2 核心流程

静态显存(不含激活)构成:

\[
M_{\text{静态}} = \underbrace{2P}_{\text{bf16 参数}} + \underbrace{2P}_{\text{bf16 梯度}} + \underbrace{12P}_{\text{AdamW fp32 状态}} = 16P \ \text{字节}
\]

ZeRO 各档在 \(N\) 卡上的每卡静态显存:

\[
\text{ZeRO-2}: \quad M = 2P + \frac{2P + 12P}{N} = 2P + \frac{14P}{N}
\]

\[
\text{ZeRO-3}: \quad M = \frac{2P + 2P + 12P}{N} = \frac{16P}{N}
\]

\[
\text{ZeRO-3 + offload}: \quad M \approx \frac{2P}{N}\ \text{起} \quad(\text{优化器与 fp32 参数驻留 CPU,bf16 参数按需上卡})
\]

再叠加**激活值**:与批次、序列长度、隐藏维度、层数正相关,且被 `gradient_checkpointing`(所有官方脚本均已开启)大幅压缩——以重算换取近似平方根量级的激活开销。粗略工程估算时,可在静态显存之外再预留 20%~40% 余量。

决策路径:

```text
单卡能装下 16P + 激活?(2B 模型 ≈ 32GB+)
 ├─ 是 → 单卡直接跑(zero2.json 即可,ZeRO 在 N=1 时无分摊收益)
 └─ 否 → 加卡?
      ├─ 是 → N 卡后 ZeRO-2 每卡 2P+14P/N 够吗?
      │        ├─ 是 → zero2.json(最快,MoE 唯一合法档)
      │        └─ 否 → 16P/N 够吗?是 → zero3.json(Dense 模型)
      └─ 否(只有一张卡/卡小)→
           ├─ CPU 内存充裕、能接受慢 → zero3_offload.json
           └─ 追求效率/显存仍紧 → LoRA(u5-l7,可训练参数骤减)
```

注意 ZeRO 的隐含前提:**\(N=1\) 时切分无从谈起**,`16P/N` 退回 `16P`。单卡显存不够,出路只有 offload 或 LoRA,而不是"换个更高的 ZeRO 档位"。

#### 4.4.3 源码精读

这套估算如何与仓库脚本互相印证?

对照 32B Dense:README [README.md:L315](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L315) 写明 8×80G。代入 ZeRO-3 公式:\(P=32\times10^9\),\(N=8\),每卡静态 \(16P/N = 16 \times 32\text{e}9 / 8 = 64\text{GB}\),加上激活与临时缓冲,80G 恰好是一个合理的紧配置——脚本的 `batch_size=2、accum=8`([sft_32b.sh:L15-L17](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_32b.sh#L15-L17))正是显存余量不足时压批次、保累积的选择。

对照 30B-A3B MoE:ZeRO-2 每卡 \(2P + 14P/N = 30\text{e}9 \times (2 + 14/32) \approx 79\text{GB}\),与脚本注释 [sft_30a3b.sh:L10](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L10) 的 "could run on 32 80G GPU" 吻合——MoE 不能用 ZeRO-3 压参数显存,于是只能堆卡数,公式直观解释了"为什么 MoE 反而要 32 张卡"。(`batch_size=1` 的保守设置,[sft_30a3b.sh:L17-L19](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/sft_30a3b.sh#L17-L19),同样是给激活留余量。)

而 `zero3_offload.json` 的用武之地在源码侧体现为 [zero3_offload.json:L32-L39](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/scripts/zero3_offload.json#L32-L39) 的两个 offload 块:GPU 装不下 \(16P/N\) 时,把占大头的优化器状态(12P)与参数一并搬去 CPU 锁页内存,代价是每步参数/状态在 PCIe 上往返,迭代明显变慢。它没有被任何官方脚本直接引用——官方脚本面向多卡集群——但它是单卡/小显存用户的现成逃生通道。

最后,精度基准是 [README.md:L33](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/README.md#L33) 锁定的 `deepspeed==0.17.1`:本讲所有公式与字段行为以该版本为准,升级版本时需复核 `stage3_*` 等字段名是否仍被接受。

**单卡 24G 跑 Qwen3-VL-2B 的选型结论(本讲实践任务的核心答案)**:

- 全参微调静态显存 \(16P = 16 \times 2\text{e}9 = 32\text{GB} > 24\text{GB}\),且单卡下 ZeRO 无分摊效果 → **zero2/zero3 均不可行**;
- 应选 **zero3_offload.json**:优化器状态与参数卸载到 CPU(需 ≥ 32GB 空闲内存),GPU 侧只承载当前层的 bf16 参数与激活,24G 足够;代价是训练速度明显下降;
- 若更在意速度与显存余量,退一步选 **LoRA**(可训练参数仅注意力低秩适配器,数千万量级),24G 绰绰有余,这也是单卡用户最常见的选择。

#### 4.4.4 代码实践

1. **实践目标**:写一个显存估算器,输入模型参数量与卡数,自动输出三档 ZeRO 的每卡静态显存与可行性结论。
2. **操作步骤**:运行下面脚本(示例代码,非仓库原有文件):

```python
# vram_est.py(示例代码)
GB = 1024**3

def report(P, N, vram_gb, label=""):
    m_plain = 16 * P / GB                 # 无 ZeRO,每卡
    m_zero2 = (2 * P + 14 * P / N) / GB   # 每卡
    m_zero3 = 16 * P / N / GB             # 每卡
    print(f"--- {label}: P={P/1e9:.1f}B, N={N}, 卡容量={vram_gb}G ---")
    for name, m in [("不用ZeRO", m_plain), ("ZeRO-2", m_zero2), ("ZeRO-3", m_zero3)]:
        ok = "可行(留激活余量)" if m < vram_gb * 0.6 else "不可行/需offload"
        print(f"{name:>8}: 每卡静态 {m:6.1f} GB -> {ok}")

report(2.2e9, 1, 24, "Qwen3-VL-2B 单卡24G")
report(4e9, 8, 80, "Qwen3-VL-4B 8×80G")
report(32e9, 8, 80, "32B Dense 8×80G(对照 sft_32b.sh)")
report(30e9, 32, 80, "30B-A3B MoE 32×80G(对照 sft_30a3b.sh)")
```

3. **需要观察的现象**:第三、四组输出应分别得出"ZeRO-3 在 8×80G 上可行""ZeRO-2 在 32×80G 上约 79GB/卡、逼近上限"的结论,与官方脚本配置对上;第一组应显示三档全部超限。
4. **预期结果**:第一组(单卡 24G)三档均判不可行 → 推导出"必须 zero3_offload 或 LoRA"的结论,与 4.4.3 的分析一致。0.6 的经验系数是给激活/缓冲留的余量(gradient_checkpointing 已开),可自行调整观察判定翻转的临界点。
5. 真实显存占用还与序列长度、batch、视觉 token 数相关,估算值仅供选型;精确值**待本地验证**(用 `nvidia-smi -l 1` 旁观训练头几百步的峰值)。

#### 4.4.5 小练习与答案

**练习 1**:8 卡 A100-80G 跑 Qwen3-VL-4B 全参,ZeRO-2 每卡静态显存约多少?该选哪档?
**答案**:\(2P + 14P/N = 4\text{e}9 \times (2 + 14/8) = 36\text{GB}\),80G 卡上可行,选 ZeRO-2 更快——这与官方 4B 脚本本可用 zero2 却给 zero3 并不矛盾:zero3 每卡仅 8GB,为大 batch/长序列留出巨量激活余量,是"显存富余换吞吐"的另一种取向。

**练习 2**:为什么说"单卡跑 2B 全参,换 ZeRO-3 也救不了"?
**答案**:ZeRO 的收益来自把状态切分到 \(N\) 张卡,\(N=1\) 时 \(16P/N = 16P\),三档退化等价;唯一出路是把状态移出 GPU(offload)或减少可训练参数(LoRA)。

**练习 3**:开启 `gradient_checkpointing` 影响公式里的哪一项?
**答案**:哪一项都不直接改——它压缩的是公式之外的**激活值**开销(由 O(层数) 量级降到近似 O(√层数),以一次额外前向重算为代价)。所有官方脚本都开启它,正是为了在大模型上把激活压进预算。

## 5. 综合实践

把本讲四块内容串成一个"选型报告"任务:

1. **配置对照表**:完成 4.1.4 的对比脚本,把带 `<-- 差异` 的字段整理成一张 Markdown 表,每个差异字段写一句"它改变了什么"。
2. **本机选型**:用 `nvidia-smi` 查看你本机(或目标机器)的 GPU 数量与单卡显存,运行 4.4.4 的估算器,代入你计划微调的模型参数量,得出 ZeRO-2 / ZeRO-3 / zero3_offload 三档的可行性判定。
3. **决策论证**:书面回答规格中的核心问题——"单卡 24G 跑 Qwen3-VL-2B 应选哪档及理由",要求包含数值推导(16P=32GB>24GB、单卡 ZeRO 无分摊)、两条出路(offload vs LoRA)的速度/显存取舍,以及若换 2×24G 双卡时结论如何变化(提示:ZeRO-3 每卡 16GB,静态可行)。
4. **脚本落地**:基于第 3 步结论,从 4.2 的模板复制改写出你的启动脚本,`--deepspeed` 指向所选配置;若选 offload,确认机器空闲 CPU 内存 ≥ 32GB(`free -g` 查看)。
5. 把以上产物整理为 `Qwen3-VL-tutorial/notes/u5-l8-selection.md`(笔记文件,自行管理),作为下次启动训练前的检查清单。

## 6. 本讲小结

- 三份 DeepSpeed 配置是递进关系:ZeRO-2 切优化器状态+梯度,ZeRO-3 再切参数并引入 `stage3_*` 流式调度字段,offload 再把优化器与参数搬到 CPU 锁页内存;JSON 中 `"auto"` 表示"由 HF Trainer 从命令行参数填充"。
- 五份 sft 脚本共享一副骨架(环境变量兜底 → 变量声明 → args 拼装 → torchrun 启动),模型越大 `batch_size` 越小、`grad_accum_steps` 越大;改脚本只需动头部变量区。
- Qwen3-VL MoE 模型不支持 ZeRO-3(README 明文声明),只能用 zero2,且 HF 实现暂缺 load balancing loss——这直接解释了 30B-A3B 需要 32×80G 而 32B Dense 只要 8×80G。
- 全参训练静态显存 ≈ 16 字节/参数;ZeRO-2 每卡 \(2P+14P/N\),ZeRO-3 每卡 \(16P/N\),\(N=1\) 时 ZeRO 无收益,单卡 24G 跑 2B 全参必须 zero3_offload 或 LoRA。
- DeepSpeed 模式下保存走 `trainer.save_model` 分支,ZeRO-3 依赖 `stage3_gather_16bit_weights_on_model_save` 聚合完整权重,这是排查"checkpoint 不完整"问题的关键开关。

## 7. 下一步学习建议

本讲之后,微调单元(u5)的源码已全部读完,你已具备从参数、数据、训练到分布式的完整视角。下一步:

1. 进入 u7-l4「端到端实战:自定义数据集微调与评测闭环」,把 u4 的数据制作与本讲的选型结论合成一个完整可复现的微调小项目。
2. 若想深挖分布式细节,可阅读 DeepSpeed 官方文档中 ZeRO 与 Activationcheckpointing 章节,对照 `zero3_offload.json` 逐字段验证本讲的解释。
3. 若关注 MoE 训练路线,可持续跟踪 transformers 中 Qwen3VLMoe 实现的更新(本仓库 README 声明的 ZeRO-3 限制与 load balancing loss 缺失均以 HF 实现现状为准),并回顾 u5-l1 中 MoE 代际判定的相关代码。
