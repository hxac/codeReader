# 快速上手：SFT 训练到推理全流程

## 1. 本讲目标

本讲是入门层的「端到端」收尾篇。前面几讲我们分别认识了 ms-swift 的定位（u1-l1）、装好了环境（u1-l2）、看清了目录与 CLI 分发（u1-l3、u1-l4）。本讲把零散的认知串成一条真实的操作链路：

学完后你应当能够：

1. 看懂一条 `swift sft` 命令的每一个关键参数，并能照着 [examples/train/lora_sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/lora_sft.sh) 跑通一次 LoRA 微调。
2. 理解训练产物（checkpoint + `args.json`）如何被 `swift infer` 自动复用，从而做到「训练即所见，推理即所得」。
3. 用 `swift export` 把 LoRA adapter 合并回完整模型，或推送到 ModelScope/HuggingFace。

本讲只要求读者「跑通并看懂」，不深入参数解析、模板、数据集等子系统的内部实现——这些是进阶层（u2~u6）的任务。

## 2. 前置知识

在动手前，请确认以下概念（不熟悉也没关系，本讲会顺带解释）：

- **微调（Fine-Tuning）**：在一个已经预训练好的大模型基础上，用一份小数据继续训练，让它学会特定任务。
- **LoRA（Low-Rank Adaptation）**：一种「轻量微调」方法。它不改动模型原始的巨大权重矩阵，而是额外挂上一对很小的矩阵去做增量。训练时只更新这对小矩阵，因此显存占用小、训练快，产物（adapter）通常只有几十 MB。它的数学原理会在 4.1.2 给出。
- **adapter**：LoRA 训练后保存下来的那份「小权重」。它不能独立使用，必须挂到原始模型上才能生效。
- **SFT（Supervised Fine-Tuning，指令微调）**：用「问题—标准答案」对去教模型怎么回答，是最常见的一种微调任务。
- **checkpoint**：训练过程中每隔若干步保存出来的模型快照，ms-swift 把它写到 `output_dir` 下。

还需要你已经完成 u1-l2 的安装，并在终端能调用 `swift sft --help`（注意：根据 u1-l4，必须带子命令再 `--help`，否则会因为 `--help` 不在 `ROUTE_MAPPING` 路由表里而报错）。

## 3. 本讲源码地图

本讲涉及的关键文件按「训练→推理→导出」三阶段排列：

| 文件 | 作用 |
| --- | --- |
| [examples/train/lora_sft.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/lora_sft.sh) | 官方 LoRA SFT 示例脚本，是本讲训练命令的来源 |
| [README.md](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md) | Quick Start 章节，给出 sft/infer/export 三段命令 |
| [swift/cli/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py) | `swift sft` 的 CLI 入口，调用 `sft_main()` |
| [swift/pipelines/train/sft.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py) | `SwiftSft` 管道，串联模型/模板/数据集/训练器 |
| [swift/pipelines/base.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py) | `SwiftPipeline` 基类，统一「解析参数→run→计时」骨架 |
| [swift/cli/infer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/infer.py) | `swift infer` 的 CLI 入口 |
| [swift/pipelines/infer/infer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py) | `SwiftInfer` 管道，加载 adapter、选择推理后端、交互推理 |
| [swift/pipelines/utils.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/utils.py) | `prepare_model_template`/`prepare_adapter`，把 adapter 挂到模型上 |
| [swift/arguments/base_args/base_args.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py) | `args.json` 的保存（`save_args`）与回载（`load_args_from_ckpt`） |
| [swift/cli/export.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/export.py) | `swift export` 的 CLI 入口 |
| [swift/pipelines/export/export.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py) | `SwiftExport` 管道，按分支做 merge/quantize/push |
| [swift/pipelines/export/merge_lora.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/merge_lora.py) | `merge_lora` 函数，把 LoRA 增量合并进基座权重 |

## 4. 核心概念与源码讲解

### 4.1 SFT 训练：从 `swift sft` 到 `SwiftSft.run`

#### 4.1.1 概念说明

「训练」阶段要做的事情可以一句话概括：**把基座模型加载进来、挂上可训练的 LoRA 小矩阵、喂入数据、跑梯度下降、定期保存 checkpoint**。

ms-swift 把这些步骤封装进一个叫「管道（Pipeline）」的对象里。`swift sft` 这条命令最终调用的就是 `SwiftSft` 管道。理解了这条命令的参数和这个管道的 `run()` 方法，你就理解了整个训练阶段。

一个关键设计点是：ms-swift 在训练开始后会把**所有参数**写进 checkpoint 目录下的 `args.json`。这个文件是后续 `infer`/`export` 能够「免配置」复用训练设置的关键，4.2 节会展开。

#### 4.1.2 核心流程

先把 README Quick Start 里的训练命令贴出来（这是本讲实践的基准命令）：

```shell
# 13GB 显存
CUDA_VISIBLE_DEVICES=0 \
swift sft \
    --model Qwen/Qwen3-4B-Instruct-2507 \
    --tuner_type lora \
    --dataset 'AI-ModelScope/alpaca-gpt4-data-zh#500' \
              'AI-ModelScope/alpaca-gpt4-data-en#500' \
              'swift/self-cognition#500' \
    --torch_dtype bfloat16 \
    --num_train_epochs 1 \
    --per_device_train_batch_size 1 \
    --learning_rate 1e-4 \
    --lora_rank 8 \
    --lora_alpha 32 \
    --target_modules all-linear \
    --gradient_accumulation_steps 16 \
    --save_steps 50 \
    --max_length 2048 \
    --output_dir output \
    --model_author swift \
    --model_name swift-robot
```

参数按功能分组理解：

| 分组 | 参数 | 含义 |
| --- | --- | --- |
| 基座 | `--model` | 要微调的模型 id（默认从 ModelScope 下载）；想换模型只改这一行 |
| 微调方式 | `--tuner_type lora` | 用 LoRA；可选 `full`/`llamapro` 等（见 u1-l1 的三维度） |
| 数据 | `--dataset 'xxx#500'` | 数据集 id，`#500` 是「只取 500 条」的子集语法，**不是训练 500 步** |
| LoRA 超参 | `--lora_rank/--lora_alpha/--target_modules` | 控制增量矩阵的秩、缩放、作用范围 |
| 训练超参 | `--num_train_epochs/--learning_rate/--per_device_train_batch_size/--gradient_accumulation_steps` | 标准 HF 训练器参数 |
| 自我认知 | `--model_author/--model_name` | 仅当数据含 `swift/self-cognition` 时生效，决定模型「自报家门」的内容 |
| 输出 | `--output_dir output` | checkpoint 与 `args.json` 的保存根目录 |

> ⚠️ 一个初学者极易踩的坑：`#500` 是**采样条数**（每个数据集取 500 条），不是训练步数。本例三个数据集各取 500 条共 1500 条，配合 `per_device_train_batch_size=1`、`gradient_accumulation_steps=16`（等效 batch=16），跑 1 个 epoch 约为 \( \lceil 1500/16 \rceil \approx 94 \) 步。如果你确实想固定训练 500 步，应把 `--num_train_epochs 1` 换成 `--max_steps 500`。

**LoRA 的数学原理（一句话版）**：把权重更新量近似为两个小矩阵的乘积：

\[
W' = W + \Delta W = W + \frac{\alpha}{r} B A,\qquad B\in\mathbb{R}^{d\times r},\ A\in\mathbb{R}^{r\times k}
\]

其中 \(r\) 就是 `--lora_rank`（本例 8），\(\alpha\) 就是 `--lora_alpha`（本例 32），缩放系数为 \(32/8=4\)。因为 \(r \ll \min(d,k)\)，可训练参数量被极大压缩，这就是 LoRA 又快又省显存的原因。

**训练阶段的执行顺序**（对应 `SwiftSft.run()`）：

```text
swift sft ...                       # 终端命令
   └─ cli/main.py: ROUTE_MAPPING 路由到 swift/cli/sft.py   # 见 u1-l4
        └─ sft_main()  →  SwiftSft(args).main()
              ├─ __init__: _prepare_model_tokenizer / _prepare_template   # 加载模型与模板
              ├─ run():
              │    ├─ _prepare_dataset()      # 下载+预处理+编码数据集
              │    ├─ args.save_args()        # ★ 写出 args.json
              │    ├─ prepare_model(...)      # 挂载 LoRA、冻结其余参数
              │    ├─ TrainerFactory.get_trainer_cls(args)   # 选训练器
              │    └─ self.train(trainer)     # trainer.train() 真正训练
              └─ main(): 计时 + 日志
```

#### 4.1.3 源码精读

**① CLI 入口**：`swift sft` 经过 u1-l4 讲过的路由后落到 [swift/cli/sft.py:13-20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/sft.py#L13-L20)，它只做几件准备工作（单卡模式、可选 unsloth、可选 ray），然后调用 `sft_main()`：

```python
if __name__ == '__main__':
    from swift.cli.utils import try_use_single_device_mode
    try_use_single_device_mode()
    try_init_unsloth()
    from swift.ray_utils import try_init_ray
    try_init_ray()
    from swift.pipelines import sft_main
    sft_main()
```

**② 管道骨架**：所有 `swift xxx` 命令都继承自 [swift/pipelines/base.py:14](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L14) 的 `SwiftPipeline`。它的 [`main()` 方法](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/base.py#L49-L54) 只负责「解析参数 → 调 `run()` → 打印起止时间」，真正的活儿在每个子类的 `run()` 里：

```python
def main(self):
    logger.info(f'Start time of running main: {...}')
    result = self.run()
    logger.info(f'End time of running main: {...}')
    return result
```

**③ SwiftSft 的准备阶段**：[`SwiftSft.__init__`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L26-L31) 在构造时就完成了模型与模板的准备（这解释了为什么训练命令一启动就会打印 `model_info`）：

```python
def __init__(self, args=None):
    super().__init__(args)
    self.train_msg = {}
    self._prepare_model_tokenizer()   # 加载模型与 processor
    self._prepare_template()          # 取对话模板并切到 train 模式
    self._prepare_flash_ckpt()
```

**④ 训练主循环 `run()`**：这是本模块最核心的一段，[swift/pipelines/train/sft.py:160-185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L160-L185)。注意 `args.save_args()` 这一行——它把训练参数落盘成 `args.json`，是连接推理/导出阶段的关键：

```python
def run(self):
    args = self.args
    train_dataset, val_dataset = self._prepare_dataset()
    ...
    args.save_args()  # ★ 写出 args.json，供 infer/export 回载
    # 挂载 LoRA 等可训练参数、冻结其余参数
    self.model = self.prepare_model(self.args, self.model, template=self.template, train_dataset=train_dataset)
    ...
    trainer_cls = TrainerFactory.get_trainer_cls(args)   # 选 Trainer
    trainer = trainer_cls(model=self.model, args=self.args.training_args,
                          template=self.template, train_dataset=train_dataset,
                          eval_dataset=val_dataset, **self._get_trainer_kwargs())
    return self.train(trainer)   # 真正训练
```

**⑤ `args.json` 的写入**：`save_args` 定义在 [swift/arguments/base_args/base_args.py:303-310](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L303-L310)，把当前所有参数以 JSON 写进 `output_dir/args.json`：

```python
def save_args(self, output_dir=None):
    ...
    fpath = os.path.join(output_dir, 'args.json')
    with open(fpath, 'w', encoding='utf-8') as f:
        json.dump(check_json_format(self.__dict__), f, ensure_ascii=False, indent=2)
```

> 实例脚本对照：本模块的命令结构与 [examples/train/lora_sft.sh:3-30](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/train/lora_sft.sh#L3-L30) 完全一致，只是该示例用的是 7B 模型、额外带了 `--system` 与 `--dataset_num_proc` 等参数。读这份脚本是理解训练参数最快的方式。

#### 4.1.4 代码实践

**实践目标**：在单卡上跑通一次 LoRA 自我认知微调，确认训练产物（checkpoint + `args.json`）正常生成。

**操作步骤**：

1. 确认显卡可用（本例约需 13GB 显存；若显存不足，可把 `--model` 换成更小的模型，或加 `--quantization_type` 走 QLoRA，详见 u5-l3）。
2. 直接复制本节 4.1.2 的命令到终端运行（来源即 [README.md:168-193](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/README.md#L168-L193) 的 Quick Start）。
3. 如果想固定训练步数便于观察，把 `--num_train_epochs 1` 改成 `--max_steps 500`（其余不变）。

**需要观察的现象**：

- 启动日志会依次打印：`model_info`（确认基座模型加载成功）、`Dataset Token Length` 统计、`model_parameter_info`（可训练参数占比，LoRA 下应远小于 1%）。
- 训练过程中每隔 `logging_steps` 打印一次 `train/loss`，loss 总体应呈下降趋势。
- 终端会打印一行类似 `run sh:` 的最终执行命令（见 u1-l4），这是排查参数是否正确生效的第一线索。

**预期结果**：

- `output/` 下出现形如 `output/vx-xxx/checkpoint-yyy/` 的目录（`vx-xxx` 是版本目录，`checkpoint-yyy` 是按 `save_steps` 保存的快照）。
- 每个 checkpoint 目录里除权重外，**必须有一个 `args.json`**——这是 4.2、4.3 两节能「免配置」工作的前提。

> 待本地验证：实际 checkpoint 目录名（`vx-xxx`/`checkpoint-yyy` 的具体编号）由训练过程动态生成，请以你本地的实际输出为准，后续命令里把 `output/vx-xxx/checkpoint-xxx` 替换成真实路径。

#### 4.1.5 小练习与答案

**练习 1**：把 `--tuner_type lora` 改成 `--tuner_type full`，模型可训练参数量会发生什么变化？为什么 LoRA 更适合快速实验？

**参考答案**：`full` 会解冻并训练模型全部参数，可训练参数量等于模型总参数量（4B 模型就是约 40 亿），显存占用巨大、速度慢；LoRA 只训练挂上去的小矩阵（占比通常远小于 1%），显存小、速度快、产物小，适合快速迭代和验证想法。

**练习 2**：本例中 LoRA 的缩放系数是多少？它由哪两个参数决定？

**参考答案**：缩放系数为 \(\alpha/r = 32/8 = 4\)，由 `--lora_alpha`（32）除以 `--lora_rank`（8）决定。

**练习 3**：训练日志里出现 `Successfully loaded .../args.json` 之前，是谁在何时写出了这个 `args.json`？

**参考答案**：是 `SwiftSft.run()` 中的 `args.save_args()` 写出的（[sft.py:167](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L167)），它在数据集准备之后、模型挂载与训练之前执行。

---

### 4.2 infer 推理：加载 adapter 与多后端切换

#### 4.2.1 概念说明

训练结束后，我们手里有一份 LoRA adapter。要让它生效，必须把它「挂」回原始基座模型上，然后做生成（generate）。`swift infer` 就是这件事的命令行封装。

ms-swift 的推理有两个亮点：

1. **免配置复用训练设置**：因为 checkpoint 里有 `args.json`，你只需要 `--adapters <checkpoint路径>`，框架会自动读回 `model`、`system`、`template` 等训练时的设置，不必重复写一长串参数。
2. **多后端**：同一套对话格式（template）和请求协议（`RequestConfig`），底层可在 `transformers` / `vllm` / `sglang` / `lmdeploy` 之间切换，用 `--infer_backend` 控制。

#### 4.2.2 核心流程

README 给出两条推理命令，第一条用原生 transformers 交互式推理，第二条合并 LoRA 后用 vLLM 加速：

```shell
# ① 交互式（transformers 后端）
swift infer \
    --adapters output/vx-xxx/checkpoint-xxx \
    --stream true \
    --temperature 0 \
    --max_new_tokens 2048

# ② 合并 LoRA + vLLM 加速
swift infer \
    --adapters output/vx-xxx/checkpoint-xxx \
    --stream true \
    --merge_lora true \
    --infer_backend vllm \
    --vllm_max_model_len 8192 \
    --temperature 0 \
    --max_new_tokens 2048
```

注意第二条命令里**没有** `--model`、`--system`——它们由 `args.json` 自动回载。`--adapters` 指向训练产出的 checkpoint 目录即可。

推理阶段的执行顺序：

```text
swift infer --adapters <ckpt> ...
   └─ SwiftInfer.__init__:
        ├─ 解析参数时，因 --adapters 触发 load_args_from_ckpt()  # 回载 args.json
        ├─ if args.merge_lora: merge_lora(args, device_map='cpu')  # 可选：先合并
        ├─ transformers 后端: prepare_model_template(args)        # 加载基座+挂 adapter
        │                  → TransformersEngine(model, template)
        └─ 其他后端: get_infer_engine(args, template)             # vllm/sglang/lmdeploy
   └─ run() → infer_cli()   # 进入交互问答循环
```

#### 4.2.3 源码精读

**① CLI 入口**：[swift/cli/infer.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/infer.py) 极薄，直接调 `infer_main()`：

```python
from swift.pipelines import infer_main
if __name__ == '__main__':
    infer_main()
```

**② `args.json` 自动回载**：这是「免配置」的关键。参数类里 [`load_args` 字段](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L69-L73) 默认对推理/导出为 `True`。当传入 `--adapters` 时，[`_init_ckpt_dir`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L236-L244) 会定位到 checkpoint 目录并触发回载：

```python
def _init_ckpt_dir(self, adapters=None):
    ...
    self.ckpt_dir = get_ckpt_dir(model, adapters)
    if self.ckpt_dir and self.load_args:
        self.load_args_from_ckpt()
```

[`load_args_from_ckpt`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L246-L301) 读取 `args.json`，把 `model`、`template`、`system`、`torch_dtype` 等键回填（仅当当前值为空时才覆盖，避免与你显式传入的参数冲突），并打印 `Successfully loaded .../args.json`：

```python
def load_args_from_ckpt(self) -> None:
    args_path = os.path.join(self.ckpt_dir, 'args.json')
    ...
    for key, old_value in old_args.items():
        ...
        if key in load_keys and (value is None or isinstance(value, (list, tuple)) and len(value) == 0):
            setattr(self, key, old_value)
    logger.info(f'Successfully loaded {args_path}.')
```

> 想关闭这个行为？加 `--load_args false` 即可强制使用命令行参数。

**③ 加载模型 + 挂载 adapter**：[`SwiftInfer.__init__`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py#L24-L40) 根据 `infer_backend` 分两条路。transformers 后端会调用 `prepare_model_template`：

```python
def __init__(self, args=None):
    super().__init__(args)
    args = self.args
    if args.merge_lora:
        merge_lora(args, device_map='cpu')          # ② 可选合并
    ...
    if args.infer_backend == 'transformers':
        model, self.template = prepare_model_template(args)   # ③ 加载基座+挂 adapter
        self.infer_engine = TransformersEngine(model, template=self.template, max_batch_size=args.max_batch_size)
    else:
        self.template = args.get_template()
        self.infer_engine = self.get_infer_engine(args, self.template)
```

而 [`prepare_model_template`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/utils.py#L37-L47) 内部调用 `prepare_adapter` 把 adapter 挂到基座上：

```python
def prepare_model_template(args, **kwargs):
    model, processor = args.get_model_processor(**kwargs)
    template = args.get_template(processor)
    if model is not None:
        ...
        model = prepare_adapter(args, model, adapters=adapters)   # ★ 挂载 adapter
    return model, template
```

[`prepare_adapter`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/utils.py#L15-L34) 的核心就一行 `tuner.from_pretrained(model, adapter)`——这与 peft 的 `PeftModel.from_pretrained` 是一回事：

```python
def prepare_adapter(args, model, adapters=None):
    ...
    adapters = adapters if adapters is not None else args.adapters
    for adapter in adapters:
        model = tuner.from_pretrained(model, adapter)   # 把 LoRA 增量挂到基座上
    return model
```

**④ 多后端分发**：[`get_infer_engine`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py#L50-L93) 用一串 `if/elif` 按 `infer_backend` 选引擎类（`TransformersEngine`/`VllmEngine`/`SglangEngine`/`LmdeployEngine`），后端细节在 u6-l2 详讲，本节只需知道「换后端只改一个参数」。

**⑤ 交互推理循环**：[`infer_cli`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py#L133-L179) 是一个 `while True` 循环，读取你的输入、调用 `infer_single` 生成回复、支持流式打印（`--stream true` 时逐字输出）。输入 `exit` 或 `quit` 退出。

#### 4.2.4 代码实践

**实践目标**：用训练产出的 adapter 进行交互推理，验证「自我认知」效果，并亲眼看一次 `args.json` 自动回载。

**操作步骤**：

1. 把 4.1 实践生成的真实 checkpoint 路径填进 `--adapters`。
2. 运行本节 4.2.2 的命令①（transformers 后端交互式）：

   ```shell
   CUDA_VISIBLE_DEVICES=0 swift infer \
       --adapters output/vx-xxx/checkpoint-xxx \
       --stream true --temperature 0 --max_new_tokens 2048
   ```

3. 在交互提示里输入：「你是谁？」和「你叫什么名字？」

**需要观察的现象**：

- 启动日志里应出现一行 `Successfully loaded .../checkpoint-xxx/args.json`，证明训练参数被自动读回。
- 因为训练数据含 `swift/self-cognition` 且设置了 `--model_name swift-robot`，模型应当自报为 `swift-robot`（而非 Qwen 的默认身份）。

**预期结果**：模型以 `swift-robot` 身份回答；流式模式下回答会逐字打印。

**进阶（可选）**：把命令换成 4.2.2 的命令②（`--merge_lora true --infer_backend vllm`），对比 vLLM 与 transformers 的推理速度。注意此时会先把 adapter 合并进基座（`merge_lora`，见 4.3.3 同一函数），再用 vLLM 加载合并后的模型。

> Python 方式（无需命令行）：参考 [examples/infer/demo_lora.py](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/infer/demo_lora.py)，用 `BaseArguments.from_pretrained(adapter_path)` 也能触发同样的 `args.json` 回载，再用 `TransformersEngine` 推理——逻辑与命令行完全一致。

#### 4.2.5 小练习与答案

**练习 1**：如果训练时设了 `--system 'You are a helpful assistant.'`，推理时不想用这个 system，应该怎么办？

**参考答案**：因为 `args.json` 会回载 `system` 字段，推理时需加 `--load_args false` 关闭回载，然后自行用 `--system` 指定新的系统提示（或不传，用模板默认）。

**练习 2**：`--adapters` 指向的必须是「adapter 目录」还是「完整模型目录」？为什么？

**参考答案**：必须是 adapter 目录（即 `swift sft` 产出的 checkpoint）。框架在 [`ModelArguments.__post_init__`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L186-L190) 里会校验：若 `tuner_type` 是内置 tuner，则每个 adapter 都必须通过 `_check_is_adapter`，否则会提示「请改用 `--model` 传递」。

**练习 3**：命令②里同时有 `--merge_lora true` 和 `--infer_backend vllm`，这两件事的先后关系是什么？

**参考答案**：先合并、后推理。`SwiftInfer.__init__` 里 `merge_lora` 在构造引擎之前执行（[infer.py:27-28](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/infer/infer.py#L27-L28)），合并后 `args.model` 被指向合并产物、`args.adapters` 被清空，随后 vLLM 引擎加载的就是已合并的完整模型。

---

### 4.3 export 合并与推送

#### 4.3.1 概念说明

LoRA adapter 是「外挂」的，有些场景（部署到 vLLM/SGLang、上传到模型库、给别人独立使用）需要一份**合并好的完整模型**。`swift export` 就是做「后处理」的管道，它支持多种互斥的操作：

- `--merge_lora`：把 LoRA 增量合并进基座权重，输出完整模型。
- `--quant_method`：对模型做量化（GPTQ/AWQ/FP8/BNB）。
- `--to_ollama`：导出为 ollama 可用格式。
- `--to_mcore` / `--to_hf`：HF ↔ Megatron-Core 权重互转（见 u9-l3）。
- `--push_to_hub`：把模型推送到 ModelScope/HuggingFace。

本节聚焦最常用的两件：**合并 LoRA** 与 **推送模型**。

#### 4.3.2 核心流程

合并命令（来自 [examples/export/merge_lora.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/export/merge_lora.sh)，注释点明了「无需重复指定 `--model`」）：

```shell
swift export \
    --adapters output/vx-xxx/checkpoint-xxx \
    --merge_lora true
```

推送命令（来自 [examples/export/push_to_hub.sh](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/export/push_to_hub.sh)）：

```shell
swift export \
    --adapters output/vx-xxx/checkpoint-xxx \
    --push_to_hub true \
    --hub_model_id '<model-id>' \
    --hub_token '<sdk-token>' \
    --use_hf false
```

`--use_hf false` 表示推送到 ModelScope；推送到 HuggingFace 则设 `--use_hf true`。

执行顺序：

```text
swift export --adapters <ckpt> --merge_lora true
   └─ SwiftExport.run():
        ├─ if args.merge_lora: merge_lora(args)
        │     ├─ prepare_model_template(args)   # 加载基座+挂 adapter
        │     ├─ Swift.merge_and_unload(model)  # ★ 把增量并入基座权重
        │     ├─ save_checkpoint(...)           # 保存合并后的完整模型
        │     ├─ args.model = output_dir        # 后续步骤改用合并产物
        │     └─ args.adapters = []
        └─（后续分支：quant / ollama / push_to_hub，均作用于合并后的模型）
```

#### 4.3.3 源码精读

**① 分支式 `run()`**：[`SwiftExport.run()`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/export.py#L20-L50) 用一串 `if/elif` 决定做什么。注意 merge_lora 是「优先执行的附加步骤」——它做完后，`args.model` 已指向合并产物，后续的 quant/push 都作用在合并模型上：

```python
def run(self):
    args = self.args
    ...
    if args.merge_lora:
        output_dir = args.output_dir
        if args.to_peft_format or args.quant_method or args.to_ollama or args.push_to_hub:
            args.output_dir = None      # 临时置空，让 merge_lora 自动生成「-merged」目录
        merge_lora(args)
        args.output_dir = output_dir    # 恢复
    if args.quant_method:
        quantize_model(args)
    elif args.to_ollama:
        export_to_ollama(args)
    ...
    elif args.push_to_hub:
        model_dir = args.adapters and args.adapters[0] or args.model_dir
        args.hub.push_to_hub(args.hub_model_id, model_dir, token=args.hub_token, ...)
```

**② `merge_lora` 函数**：[swift/pipelines/export/merge_lora.py:27-61](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/export/merge_lora.py#L27-L61)。核心两步：先 `prepare_model_template` 加载基座并挂上 adapter（与 infer 复用同一个函数），再 `Swift.merge_and_unload` 把增量永久并入权重，最后 `save_checkpoint` 保存：

```python
def merge_lora(args, device_map=None, replace_if_exists=False):
    output_dir = getattr(args, 'output_dir', None) or f'{args.adapters[0]}-merged'
    ...
    model, template = prepare_model_template(args)   # 加载基座 + 挂 adapter（同 4.2）
    logger.info('Merge LoRA...')
    Swift.merge_and_unload(model)                     # ★ 增量并入基座
    model = model.model
    save_checkpoint(model, template.processor, output_dir, ...)   # 保存完整模型
    ...
    args.model = output_dir      # 后续步骤改用合并产物
    args.model_dir = output_dir
    args.adapters = []           # adapter 已并入，清空
```

注意最后三行：合并后 `args.model` 被重新指向合并产物，`args.adapters` 被清空——这解释了为什么 `--merge_lora` 之后再接 `--push_to_hub`，推送的就是合并后的完整模型而不是 adapter。

**③ 推送的底层**：`push_to_hub` 分支调用 `args.hub.push_to_hub(...)`，`hub` 是 ModelScope 或 HuggingFace 的上传句柄（由 `--use_hf` 决定）。`--hub_model_id` 是目标仓库 id，`--hub_token` 是 SDK 鉴权 token（在 ModelScope 个人中心获取）。

#### 4.3.4 代码实践

**实践目标**：把 4.1 训练得到的 LoRA adapter 合并成完整模型，确认合并产物可独立加载。

**操作步骤**：

1. 运行合并命令（替换为你的真实 checkpoint 路径）：

   ```shell
   CUDA_VISIBLE_DEVICES=0 swift export \
       --adapters output/vx-xxx/checkpoint-xxx \
       --merge_lora true
   ```

2. 合并完成后，用合并产物直接推理（**不再需要** `--adapters`，因为它已是完整模型）：

   ```shell
   CUDA_VISIBLE_DEVICES=0 swift infer \
       --model output/vx-xxx/checkpoint-xxx-merged \
       --infer_backend vllm
   ```

   （合并产物的确切目录名以本地输出为准；若未显式指定 `--output_dir`，默认是 `<adapters>-merged`。）

**需要观察的现象**：

- 日志依次出现 `Merge LoRA...` → `Saving merged weights...` → `Successfully merged LoRA and saved in ...`。
- 合并产物目录里是完整的模型权重（不再是几十 MB 的 adapter，而是与基座同量级的文件）。

**预期结果**：合并模型用 `--model` 直接加载即可推理，效果与 4.2 中「挂 adapter 推理」一致。

**进阶（可选·推送）**：如果你有 ModelScope 账号，准备 `hub_token` 后运行 4.3.2 的推送命令，把模型上传到个人仓库。注意推送是**外发操作**（内容会公开/入库），请确认模型与数据许可后再执行。

> 待本地验证：合并与推理的具体耗时、产物体积取决于模型规模与硬件，请以本地实测为准。

#### 4.3.5 小练习与答案

**练习 1**：`swift export --merge_lora true` 之后，`args.model` 和 `args.adapters` 分别变成了什么？为什么这样设计？

**参考答案**：`args.model` 被指向合并产物目录，`args.adapters` 被清空为 `[]`。这样设计是为了让 merge 之后的后续步骤（quant/push 等）自动作用在「已合并的完整模型」上，而不必让用户重新指定 `--model`。

**练习 2**：`merge_lora` 和 infer 里的「挂 adapter 推理」有何本质区别？

**参考答案**：挂 adapter 推理是**运行时**把增量叠加上去，基座权重不变、adapter 仍独立存在；`merge_lora` 则把增量**永久写入**基座权重（`Swift.merge_and_unload`），输出一份不含 adapter 的完整模型，之后不再需要 adapter 文件。

**练习 3**：想把同一个合并后的模型同时推送到 ModelScope 和 HuggingFace，需要跑几次 `swift export`？关键参数差异是什么？

**参考答案**：跑两次（或合并后分别推送）。关键差异是 `--use_hf`：推 ModelScope 用 `--use_hf false`，推 HuggingFace 用 `--use_hf true`，并分别提供对应平台的 `--hub_token`。

---

## 5. 综合实践

把三节串成一条完整的「训练 → 推理 → 导出」流水线，并在关键节点加一次人工核查：

1. **训练**：运行 4.1.2 的 `swift sft` 命令（若显存紧张，改 `--max_steps 100` 快速跑通即可）。训练结束后，进入 checkpoint 目录，**用文本编辑器打开 `args.json`**，找到并记录 `model`、`template`、`system`、`tuner_type`、`lora_rank` 五个字段的值——它们就是后续被自动回载的内容。
2. **推理（免配置验证）**：运行 4.2.4 的 `swift infer --adapters <ckpt>`，确认日志打印了 `Successfully loaded .../args.json`，且无需你再传 `--model`。提问验证自我认知效果。
3. **导出**：运行 4.3.4 的 `swift export --merge_lora true`，得到合并模型；再用 `--model <合并产物>` 直接推理，对比效果是否与步骤 2 一致。

**核查点**：步骤 2 的回复身份 与 步骤 3 的回复身份 应当**完全一致**——如果一致，说明你真正理解了「adapter 是可合并的增量」这一贯穿三节的核心概念。

> 提示：若中途某步报错，第一件事是看终端打印的 `run sh:` 那一行（u1-l4），确认框架最终执行的命令是否如你所愿。

## 6. 本讲小结

- `swift sft` 的关键参数分四组：基座（`--model`）、微调方式（`--tuner_type lora`）、数据（`--dataset ...#N` 子集语法）、LoRA 超参（`rank/alpha/target_modules`）；其中 `#N` 是采样条数而非训练步数。
- 训练主循环在 [`SwiftSft.run()`](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/pipelines/train/sft.py#L160-L185)，顺序是「准备数据集 → `save_args()` 写 `args.json` → 挂载 LoRA → 选 Trainer → `train()`」。
- `args.json` 是连接三阶段的纽带：训练时由 `save_args` 写出，推理/导出时由 `load_args_from_ckpt` 回载，实现「训练即所见，推理即所得」。
- `swift infer` 通过 `--adapters` 加载训练产物，用 `prepare_adapter` 把 adapter 挂到基座；`--infer_backend` 可在 transformers/vllm/sglang/lmdeploy 间切换。
- `swift export` 是分支式管道：`--merge_lora` 用 `Swift.merge_and_unload` 把增量永久并入基座；`--push_to_hub` 推送到 ModelScope/HuggingFace。
- 合并后 `args.model` 指向合并产物、`args.adapters` 清空，因此 merge 后再 push 推送的就是完整模型。

## 7. 下一步学习建议

本讲只让你「跑通并看懂参数」。要真正理解每一步内部发生了什么，建议按依赖顺序继续：

1. **u2（参数与配置体系）**：深入 `SftArguments` 的多继承组合，搞清 `args.json` 里每个字段的来源与 `parse_yaml_args` 如何把 YAML 展开成命令行参数。
2. **u3（模型与模板）**：理解 `--model` 如何经 `MODEL_MAPPING` 定位、`get_template` 如何把对话编码成 token 序列（本讲里的 `_prepare_template` 背后做了什么）。
3. **u4（数据集处理）**：搞清 `--dataset 'xxx#500'` 的 `#500` 子集语法、Alpaca/messages 格式如何被预处理为统一结构。
4. **u5（训练器与轻量微调）**：吃透 `prepare_model` 如何挂载 LoRA 并冻结其余参数、`TrainerFactory` 如何选 Trainer，以及 LoRA 之外的轻量方法。
5. **u6（推理引擎）**：展开 `get_infer_engine` 的多后端细节与 `InferRequest`/`RequestConfig` 协议。

建议在进入下一讲前，先确保本讲的「训练 → 推理 → 导出」三步能在本地真实跑通一次——它是后续所有源码阅读的「锚点」。
