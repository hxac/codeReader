# 入口 launch.py：从命令行到 PyTorch Lightning Trainer

## 1. 本讲目标

读完本讲，你应该能够：

1. 逐段解释 `launch.py` 的 `main()` 函数：GPU 选择 → 配置加载 → datamodule/system 构建 → callbacks/loggers 组装 → Trainer 构建与执行。
2. 说清 `--train` / `--validate` / `--test` / `--export`（外加 `--gradio`）五种模式分别走哪条分支、各自做什么。
3. 理解几个容易被忽略的工程细节：为什么 GPU 环境变量要在 `import pytorch_lightning` **之前**设置、日志为什么会变色、训练时为什么能自动从上次的检查点续训。
4. 掌握 callbacks（ModelCheckpoint、CodeSnapshot、ConfigSnapshot、进度条）与 loggers（TensorBoard、CSV）是如何被挂到 Trainer 上的，以及 `outputs/` 试验目录里每个子目录是谁生成的。
5. 写一个只调用 `load_config` 的小脚本，独立解析一份 yaml 配置并打印 `data_type` 与 `system_type`。

## 2. 前置知识

本讲会用到以下几个概念，用通俗语言先解释一遍：

- **入口脚本（entry point）**：你敲 `python launch.py --config xxx.yaml --train` 时最先被执行的那个 Python 文件。它负责把「命令行参数 + yaml 配置」翻译成一个个真实的 Python 对象，最后交给训练框架。
- **PyTorch Lightning（下称 PL）**：一个封装了 PyTorch 训练循环的框架。你不用自己写 `for epoch in ...: for batch in ...: loss.backward()`，只要实现 `training_step` 等钩子方法，PL 的 `Trainer` 对象会替你驱动整个训练/验证/测试流程。DreamCraft3D 里所有系统类都继承自 `pl.LightningModule`。
- **Trainer**：PL 的总指挥，构造时接收 `callbacks`（回调，在训练各阶段被自动调用的“挂件”）、`logger`（日志记录器）等参数；调用 `trainer.fit()` 就开始训练。
- **callback（回调）**：一种“插件”，在训练过程的特定时刻（如 `on_fit_start`、`on_train_batch_end`）被 PL 自动调用，用来做存检查点、拍代码快照、写进度文件等与训练本身无关的事。
- **OmegaConf**：一个 yaml 配置读写库，支持把多个 yaml 和命令行参数合并（merge）成一份配置对象。上一讲（u1-l3）我们知道了 `X_type` 的值是注册名、`X` 段是构造参数；本讲看这套机制如何被 `load_config` 落实。
- **CUDA_VISIBLE_DEVICES**：NVIDIA 的环境变量，限定本进程能看到哪些 GPU。进程一旦启动，GPU 可见性就固定了，所以它必须在导入任何 CUDA 相关库之前设置——这是本讲一个关键细节。

如果你还没读过 u1-l3 的注册机制（`@threestudio.register` 与 `threestudio.find`），建议先回顾，本讲的 `threestudio.find(cfg.data_type)` 正是那套机制的使用现场。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [launch.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py) | 唯一的训练/导出入口。命令行解析、GPU 选择、配置加载、组件构建、Trainer 组装全在这里，共约 250 行。 |
| [threestudio/utils/config.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py) | 配置系统：`ExperimentConfig` 数据类、`load_config`（yaml + 命令行合并）、OmegaConf resolver 注册。本讲只看与入口直接相关的部分，细节留到 u2-l2。 |
| [threestudio/utils/callbacks.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py) | 四个自定义回调：代码快照、配置快照、自定义进度条、Gradio 进度文件。 |
| [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml) | 粗阶段配置，实践环节的解析对象。其中 `trainer:` 与 `checkpoint:` 两段直接被 launch.py 消费。 |
| [threestudio/utils/misc.py](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py) | 工具函数库，本讲只用到 `get_rank()`（获取当前进程在多卡训练中的编号）。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：**命令行与五种模式**、**GPU 选择与延迟导入**、**组件构建与断点续训**、**callbacks/loggers/Trainer 组装**、**callbacks.py 四个自定义回调精读**。

### 4.1 命令行参数与五种运行模式

#### 4.1.1 概念说明

`launch.py` 既是库脚本的组装现场，也是一个命令行工具（CLI）。它接受两类命令行参数：

1. **脚本自身的参数**：`--config`、`--gpu`、`--train` 等，由 `argparse` 解析；
2. **配置覆盖参数**：剩下所有无法识别的参数（如 `system.prompt_processor.prompt="a burger"`），原样交给 OmegaConf 去覆盖 yaml 里的值。

这套“双层参数”设计让你不用改 yaml 就能微调任何配置项——README 里的示例命令正是这样做的。

#### 4.1.2 核心流程

```text
python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train system.guidance.scale=5.
        │
        ├─ argparse 解析出 args（--config/--train/...）
        ├─ 未识别的 ["system.guidance.scale=5."] 存入 extras
        └─ 根据 --gradio 决定是否重定向 stdout 后调用 main(args, extras)
```

五种模式中前四个互斥（必须且只能选一个），`--gradio` 是附加开关：

| 模式 | 触发的 Trainer 方法 | 用途 |
| --- | --- | --- |
| `--train` | `fit` → `test`（→ `predict`） | 训练；训练完自动跑一次测试渲染视频；gradio 模式下再顺带导出资产 |
| `--validate` | `validate` | 加载检查点跑验证集，快速看当前效果 |
| `--test` | `test` | 加载检查点渲染测试视频（多视角环绕） |
| `--export` | `predict` | 加载检查点导出 obj 网格等资产 |
| `--gradio`（附加） | 不单独成立 | 被 `gradio_app.py` 当子进程调用时使用，改变日志与进度输出方式 |

#### 4.1.3 源码精读

参数定义集中在文件末尾的 `if __name__ == "__main__":` 块中：

[launch.py:213-245](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L213-L245) 定义了全部命令行参数：`--config` 必填；`--gpu` 默认 `"0"`；四个模式参数放进 `add_mutually_exclusive_group(required=True)`，即“必须选一个且只能选一个”；随后 `parser.parse_known_args()` 把**不认识的参数**收集到 `extras` 列表——这正是配置覆盖参数进入系统的入口。

[launch.py:247-252](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L247-L252) 是程序真正的启动点：gradio 模式下用 `contextlib.redirect_stdout(sys.stderr)` 把标准输出重定向到标准错误再调用 `main()`（代码注释 FIXME 标明这其实没达到捕获 stdout 的预期效果，但使得日志统一从 stderr 流出，便于 `gradio_app.py` 解析）。

四种模式对应 `main()` 末尾的分支，详见 4.4.3 节的 [launch.py:194-210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L194-L210)。

#### 4.1.4 代码实践

**实践目标**：亲手验证“双层参数”机制——脚本参数与配置覆盖参数各走各的通道。

**操作步骤**：

1. 在仓库根目录运行只带 `--help` 的命令（不会真的启动训练）：

   ```bash
   python launch.py --help
   ```

2. 再故意写一个会被识别、一个不会被识别的参数，观察 `extras` 的内容。可在 `main()` 开头临时加一行 `print(extras)`（建议先 `cp launch.py launch_backup.py` 备份，实验后还原）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train system.guidance.scale=5. foo=bar
   ```

**需要观察的现象**：`--help` 输出里列出 `--config/--gpu/--train/--validate/--test/--export/--gradio/--verbose/--typecheck`；第二条命令中 `system.guidance.scale=5.` 和 `foo=bar` 不会让 argparse 报错，而是出现在 `extras` 里。

**预期结果**：`extras == ['system.guidance.scale=5.', 'foo=bar']`。随后这些字符串会被 `load_config` 里的 `OmegaConf.from_cli` 解析并覆盖配置（`foo=bar` 会成为配置对象上一个多余的新键）。注意：该命令在打印后若不中断会真正进入训练流程，请 `Ctrl+C` 及时停止；若环境中尚未装好依赖，则在本步只需观察 argparse 行为即可，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果执行 `python launch.py --config xxx.yaml`（不带任何模式参数）会发生什么？

**答案**：argparse 直接报错退出。因为四个模式参数所在的分组是 `add_mutually_exclusive_group(required=True)`（[launch.py:225](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L225)），缺一不可；同时选两个（如 `--train --test`）同样报错。

**练习 2**：`--gradio` 为什么不放在那个互斥分组里？

**答案**：`--gradio` 是附加开关而非独立模式——它总是与 `--train` 组合使用（[launch.py:231-233](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L231-L233) 的 help 文本 "if true, run in gradio mode"）。它的作用是改变日志格式、增加进度文件回调和训练后的自动导出（见 4.3/4.4 节），而不是切换到另一种执行流程，所以不参与互斥。

### 4.2 GPU 选择、延迟导入与日志着色

#### 4.2.1 概念说明

`main()` 的前 60 行处理三件“必须在导入 pytorch_lightning 之前做完”的事：

1. **GPU 选择**：通过 `CUDA_VISIBLE_DEVICES` 环境变量限定可用显卡。这个变量必须在 CUDA 上下文初始化（即首次 `import torch` 并触碰 CUDA）之前设置才有效，而 `import pytorch_lightning` 会连带导入 torch——所以你会看到 `main()` 把 `import pytorch_lightning` 刻意写在函数体中间（延迟导入），而不是文件顶部。
2. **日志着色**：训练日志按级别染上不同颜色，方便肉眼区分 warning/error。
3. **配置加载**：把 yaml + 命令行覆盖合并成 `ExperimentConfig`。

#### 4.2.2 核心流程

```text
os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    │
    ├─ CUDA_VISIBLE_DEVICES 已被外部设置？（SLURM / 上层脚本）
    │     是 → 尊重外部设置，n_gpus = 其中的 GPU 数
    │     否 → 用 --gpu 参数写入 CUDA_VISIBLE_DEVICES，n_gpus = --gpu 中逗号项数
    │
    ├─ 此后才 import pytorch_lightning / torch / Trainer
    └─ devices = -1：对 PL 而言"用所有可见 GPU"
```

#### 4.2.3 源码精读

[launch.py:43-61](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L43-L61) 是 GPU 选择逻辑。要点：

- `devices = -1` 表示让 PL 使用**所有可见** GPU——过滤工作已经由 `CUDA_VISIBLE_DEVICES` 提前完成，PL 层面不再挑选（代码注释明确说明了这个设计哲学："As far as Pytorch Lightning is concerned, we always use all available GPUs"）。
- 若外部环境已经设置 `CUDA_VISIBLE_DEVICES`（例如 SLURM 的 `srun` 或上层脚本），`--gpu` 参数被忽略，避免双重指定互相打架。

[launch.py:62-84](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L62-L84) 是延迟导入区：`pytorch_lightning`、`torch`、`Trainer`、各类 callback/logger、以及 `threestudio` 本身都在**GPU 环境变量设置完之后**才导入。其中 [launch.py:69-72](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L69-L72) 的 `--typecheck` 分支会给 `threestudio` 包装上 `jaxtyping + typeguard` 的动态类型检查钩子，导入期即校注解，便于开发调试（会拖慢速度，正常训练不开）。

[launch.py:9-40](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L9-L40) 定义 `ColoredFilter`：一个 `logging.Filter`，在日志记录经过时把级别名替换成带 ANSI 颜色码的字符串（WARNING 黄、INFO 绿、DEBUG 蓝、CRITICAL 品红、ERROR 红），并在消息末尾追加重置码。它并不“过滤”掉任何日志（`filter` 恒返回 `True`），只是顺手改写记录。

[launch.py:86-96](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L86-L96) 把这个过滤器装到 `pytorch_lightning` 的日志器上：非 gradio 模式用 `"%(levelname)s %(message)s"` 格式 + 彩色过滤器；gradio 模式改用 `"[%(levelname)s] %(message)s"` 的朴素格式（前端解析不需要颜色）。

#### 4.2.4 代码实践

**实践目标**：体会“环境变量必须在导入 torch 前设置”。

**操作步骤**：

1. 写一个 10 行小脚本（示例代码）：

   ```python
   # gpu_order_demo.py（示例代码）
   import os, sys

   mode = sys.argv[1] if len(sys.argv) > 1 else "after"
   if mode == "before":
       os.environ["CUDA_VISIBLE_DEVICES"] = "0"   # 先设环境变量
   import torch                                    # 后导入 torch
   if mode == "after":
       os.environ["CUDA_VISIBLE_DEVICES"] = "0"   # 导入后再设，为时已晚
   print("可见 GPU 数:", torch.cuda.device_count())
   ```

2. 分别运行 `python gpu_order_demo.py before` 和 `python gpu_order_demo.py after`（假设机器有多张卡）。

**需要观察的现象**：两种顺序下 `torch.cuda.device_count()` 的差异。

**预期结果**：多卡机器上 `before` 只能看到 1 张卡，`after` 仍能看到全部卡——这就是 launch.py 把 GPU 逻辑放在一切导入之前的理由。单卡机器上两者都是 1，无法体现差异；无 GPU 环境则 `device_count()` 返回 0。此实验依赖本地显卡数量，现象标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：在 8 卡机器上运行 `CUDA_VISIBLE_DEVICES=4,5 python launch.py --config x.yaml --gpu 0 --train`，实际会用哪几张卡？

**答案**：用物理编号 4、5 两张卡。因为 `CUDA_VISIBLE_DEVICES` 已被外部设置（[launch.py:54-56](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L54-L56)），`--gpu` 参数被忽略，`n_gpus = 2`，`devices = -1` 让 PL 用光这两张可见卡。

**练习 2**：为什么 `ColoredFilter.filter()` 永远返回 `True`？

**答案**：`logging.Filter` 的返回值决定日志记录是否被丢弃，`True` 表示放行。这个类的目的不是过滤而是**改写**（给 `record.levelname` 加颜色前缀、给 `record.msg` 加重置码），所以必须放行所有记录（[launch.py:35-40](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L35-L40)）。若返回 `False`，所有 PL 日志会凭空消失。

### 4.3 组件构建：custom_import、配置解析与断点自动续训

#### 4.3.1 概念说明

这一段是上一讲注册机制的“消费现场”：配置里的 `data_type`、`system_type` 字符串经过 `threestudio.find` 变成真正的 Python 类，再传入各自的参数段完成实例化。此外还有三个关键点：

- **custom_import**：允许从仓库外部注入扩展模块（导入即注册），是二次开发的钥匙。
- **逐卡种子**：`seed_everything(seed + rank)` 保证多卡训练时每张卡的随机相机采样互不相同。
- **断点自动续训**：`--train` 模式下若没显式指定 resume，会自动扫描 `trial_dir/ckpts/` 里最新的检查点继续训练——中断后原命令重跑即可，非常贴心。

#### 4.3.2 核心流程

```text
load_config(args.config, cli_args=extras, n_gpus=n_gpus)
    │  （yaml 合并 + OmegaConf.resolve 展开插值 + parse_structured 转 ExperimentConfig）
    ▼
cfg.custom_import 非空？→ importlib.import_module 逐个导入（触发注册）
    ▼
pl.seed_everything(cfg.seed + get_rank())
    ▼
dm = threestudio.find(cfg.data_type)(cfg.data)          # 数据模块
    ▼
（--train 且未指定 resume）扫描 {trial_dir}/ckpts/* → 取字典序最后一个为 resume
    ▼
system = threestudio.find(cfg.system_type)(cfg.system, resumed=cfg.resume is not None)
system.set_save_dir({trial_dir}/save)
```

#### 4.3.3 源码精读

[launch.py:98-100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L98-L100) 调用 `load_config` 完成配置解析。对应实现在 [threestudio/utils/config.py:107-117](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L107-L117)：加载所有 yaml → `OmegaConf.from_cli(cli_args)` 解析命令行覆盖 → `OmegaConf.merge` 合并 → `OmegaConf.resolve` 展开所有 `${...}` 插值 → `parse_structured(ExperimentConfig, cfg)` 转成带类型的数据类实例。注意 `n_gpus=n_gpus` 作为 kwargs 也被 merge 进配置，它会在 `__post_init__` 里决定是否禁用时间戳（多卡时各进程时间戳可能不同会导致 trial_dir 不一致，见 [threestudio/utils/config.py:92-100](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L92-L100)）。

`ExperimentConfig` 本体在 [threestudio/utils/config.py:51-104](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/config.py#L51-L104)：除了 `data_type/system_type` 这对“类型+参数”外，还容纳 `trainer`（透传给 PL Trainer 的任意参数）与 `checkpoint`（透传给 ModelCheckpoint 的参数）两个 dict——yaml 里 `trainer: max_steps: 5000` 这些键最终就是从这两个 dict 展开进 `**cfg.trainer` 的。`__post_init__` 负责拼出 `trial_dir = outputs/<name>/<tag><timestamp>` 并立即创建该目录。

[launch.py:102-105](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L102-L105) 处理 `custom_import`：对配置里列出的每个扩展模块执行 `importlib.import_module`，让外部代码里的 `@threestudio.register` 装饰器得以执行、注册进 `__modules__` 字典，随后 `find` 才能找到它们。

[launch.py:106-107](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L106-L107) 设置随机种子。`get_rank()`（[threestudio/utils/misc.py:17-25](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/misc.py#L17-L25)）依次检查 `LOCAL_RANK/RANK/SLURM_PROCID` 等环境变量返回进程编号，因此第 0 张卡种子是 `seed+0`、第 1 张卡是 `seed+1`……各卡采样到不同的随机相机。

[launch.py:109](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L109) 一行完成数据模块构建：`threestudio.find(cfg.data_type)` 按注册名取类（粗阶段配置里 `data_type: "single-image-datamodule"`），再用 `cfg.data` 参数段实例化。

[launch.py:112-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L112-L118) 是自动续训逻辑：`--train` 且 `cfg.resume is None` 时，用 `glob` 扫描 `{trial_dir}/ckpts/*`，非空则取 `sorted(...)[-1]`（字典序最大的文件名，配合 `save_last: true` 产生的 `last.ckpt` 命名规则即为最新检查点）作为 resume 路径并打印提示。

[launch.py:120-123](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L120-L123) 构建 system：同样 `find(cfg.system_type)` 取类，传入 `cfg.system` 和 `resumed` 标志（后者告诉系统这次是从检查点恢复的，`BaseSystem` 会据此在 [threestudio/systems/base.py:58](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/systems/base.py#L58) 的 `set_resume_status` 中恢复训练步计数）。`set_save_dir`（定义于 [threestudio/utils/saving.py:26](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L26)）把 `{trial_dir}/save` 指定为系统渲染结果、导出资产的保存目录。

[launch.py:125-131](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L125-L131) 是 gradio 专属：额外挂一个 `FileHandler` 把日志写进 `{trial_dir}/logs` 文件，供前端页面展示。

#### 4.3.4 代码实践

**实践目标**：不启动训练，只用 `load_config` 解析 coarse-nerf 配置，验证你对“配置 → 组件类型”的理解。

**操作步骤**：

1. 在仓库根目录新建 `inspect_config.py`（示例代码）：

   ```python
   # inspect_config.py（示例代码）
   from threestudio.utils.config import load_config

   cfg = load_config(
       "configs/dreamcraft3d-coarse-nerf.yaml",
       cli_args=['system.prompt_processor.prompt="a tasty hamburger"'],
   )
   print("data_type   =", cfg.data_type)
   print("system_type =", cfg.system_type)
   print("trial_dir   =", cfg.trial_dir)
   print("trainer     =", dict(cfg.trainer))
   print("checkpoint  =", dict(cfg.checkpoint))
   ```

2. 在已完成 u1-l2 环境安装的 conda 环境中运行 `python inspect_config.py`。

**需要观察的现象**：打印出的类型字符串、试验目录路径，以及 trainer/checkpoint 两个 dict 的内容；命令行覆盖的 prompt 是否生效。

**预期结果**：

- `data_type = single-image-datamodule`，`system_type = dreamcraft3d-system`（与 [configs/dreamcraft3d-coarse-nerf.yaml:6](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L6) 和 [configs/dreamcraft3d-coarse-nerf.yaml:41](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L41) 一致）；
- `trial_dir` 形如 `outputs/dreamcraft3d-coarse-nerf/a_tasty_hamburger@20260817-xxxxxx`——注意 tag 来自 prompt 经过 `rmspace` resolver 把空格替换成下划线，且**运行脚本本身就会创建这个目录**（`__post_init__` 中的 `os.makedirs`）；
- `trainer` 含 `max_steps=5000`、`precision=16-mixed` 等键；`checkpoint` 含 `save_last=True`、`save_top_k=-1`。

依赖环境未就绪时此脚本无法运行，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cfg.resume` 取的是 `sorted(resume_file_list)[-1]` 而不是按文件修改时间？

**答案**：检查点文件名由 PL 的 ModelCheckpoint 按步数生成（形如 `epoch=0-step=000500.ckpt`，`every_n_train_steps` 等于 `max_steps`，另因 `save_last: true` 多出 `last.ckpt`）。字符串排序下数字零填充保证步数大的排后面，而 `last.ckpt` 以字母 `l` 排在所有 `epoch=...` 之后，恰好也是最新状态，所以字典序最大者即最新检查点。这依赖命名约定而非时间戳，更加稳定。

**练习 2**：如果不希望自动续训、想从头重训，该怎么做？

**答案**：自动续训只在 `cfg.resume is None` 时扫描 `ckpts/` 目录。要么删掉/改名整个 trial 目录（或其中的 `ckpts/` 子目录），要么在配置中显式给 `resume` 赋值。注意自动扫描的 glob 路径是 `cfg.trial_dir`——它由 name/tag/时间戳决定，更换 `tag` 或让时间戳生效也会落到新目录从而避开旧检查点。

### 4.4 callbacks、loggers 与 Trainer 的组装

#### 4.4.1 概念说明

组件构建完成后，`main()` 把 PL 需要的“外挂”装配起来：

- **callbacks**（训练期钩子）：存检查点、记录学习率、拍代码快照、拍配置快照，以及一个进度展示器；
- **loggers**（指标记录器）：TensorBoard、CSV，外加 system 自带的日志器；
- **Trainer**：接收上面两者，并从 `cfg.trainer` 展开 `max_steps`、`precision` 等训练参数。

只有 `--train` 模式才装配完整的 callbacks/loggers；validate/test/export 模式传入空列表，轻装上阵。

#### 4.4.2 核心流程

```text
--train？
 ├─ 是 → callbacks = [ModelCheckpoint, LearningRateMonitor,
 │                    CodeSnapshotCallback, ConfigSnapshotCallback,
 │                    ProgressCallback(gradio) / CustomProgressBar(终端)]
 │        loggers   = [TensorBoardLogger, CSVLogger] + system.get_loggers()
 │        建 tb_logs 目录；写 cmd.txt 记录完整命令行
 ├─ 否 → callbacks = []，loggers = []
 ▼
Trainer(callbacks=..., logger=..., inference_mode=False,
        accelerator="gpu", devices=devices, **cfg.trainer)
```

#### 4.4.3 源码精读

[launch.py:133-155](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L133-L155) 组装训练 callbacks：

- `ModelCheckpoint(dirpath={trial_dir}/ckpts, **cfg.checkpoint)`：yaml 的 `checkpoint:` 段原样透传（粗阶段为 `save_last: true; save_top_k: -1; every_n_train_steps: ${trainer.max_steps}`，即训练结束时才存、外加 last.ckpt）；
- `LearningRateMonitor(logging_interval="step")`：逐步记录学习率；
- `CodeSnapshotCallback` 与 `ConfigSnapshotCallback`：把当前代码与配置快照进试验目录（详见 4.5）；
- 进度展示二选一：gradio 模式用 `ProgressCallback`（写进度文件），终端模式用 `CustomProgressBar`（tqdm 进度条）。

[launch.py:157-177](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L157-L177) 组装 loggers：`rank_zero_only` 包裹的 lambda 保证只有 0 号进程执行（多卡时不重复写文件）；先建 `tb_logs` 目录消除 TensorBoardLogger 的告警，再挂 `TensorBoardLogger` 与 `CSVLogger`，最后拼接 `system.get_loggers()`（定义于 [threestudio/utils/saving.py:62](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/saving.py#L62)，DreamCraft3D 的系统类默认返回空列表，可被子类扩展）。`cmd.txt` 把完整命令行和解析后的 args 落盘，方便日后复现。

[launch.py:179-186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L179-L186) 构建 Trainer。两个值得注意的硬编码参数：`inference_mode=False`——推理（validate/test/predict）阶段也**不**进入 `torch.inference_mode`，因为导出网格时（DMTet 网格 + nvdiffrast 光栅化烘焙纹理）仍需要梯度；`accelerator="gpu"`、`devices=-1`（或全部可见卡）。`**cfg.trainer` 把 yaml `trainer:` 段的 `max_steps/log_every_n_steps/val_check_interval/precision` 等全部透传。

[launch.py:188-210](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L188-L210) 是模式分派的总出口：

- `--train`：`trainer.fit(system, datamodule=dm, ckpt_path=cfg.resume)` 后紧跟 `trainer.test(...)`（训练完自动渲染测试视频）；gradio 模式再加 `trainer.predict(...)` 顺带导出资产；
- 其余三个模式都先经 `set_system_status` 再调用对应 Trainer 方法。这个内嵌函数（[launch.py:188-192](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L188-L192)）用 `torch.load` 读出检查点里的 `epoch` 和 `global_step` 调 `system.set_resume_status` 写回系统状态——因为 PL 的 validate/test/predict 不会像 fit 那样自动恢复这两个计数字段，而 DreamCraft3D 的损失权重调度（`C()` 函数，u8-l1 会讲）依赖 `true_global_step`，不手动恢复会导致用错调度值。

#### 4.4.4 代码实践

**实践目标**：建立“试验目录子目录 → 生成者”的映射，训练一次后逐一对账。

**操作步骤**：

1. 通读本节引用的 [launch.py:133-186](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L133-L186)，在纸上列出每个回调/日志器写的路径；
2. 若环境可用，以极小步数跑一次粗阶段训练（示例代码）：

   ```bash
   python launch.py --config configs/dreamcraft3d-coarse-nerf.yaml --train \
       trainer.max_steps=8 trainer.val_check_interval=8
   ```

3. 训练结束后 `find outputs/dreamcraft3d-coarse-nerf/<tag>@<时间戳> -maxdepth 2` 查看目录结构。

**需要观察的现象**：`ckpts/`、`code/`、`configs/`、`save/`、`tb_logs/`、`csv_logs/`、`cmd.txt` 是否各就各位；`tb_logs` 里的事件文件能否被 `tensorboard --logdir <trial_dir>/tb_logs` 打开。

**预期结果**：`ckpts/`（ModelCheckpoint）出现 `last.ckpt`；`code/`（CodeSnapshotCallback）含整个仓库代码副本；`configs/`（ConfigSnapshotCallback）含 `parsed.yaml` 与 `raw.yaml`；`save/`（system.set_save_dir）含验证渲染图；`tb_logs/`、`csv_logs/` 分别来自两个 logger；`cmd.txt` 记录了本次命令。该实践需要完整 GPU 环境与预训练权重，未就绪时以目录结构推演代替，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `inference_mode=False` 对 DreamCraft3D 尤其重要？

**答案**：`--export` 走 `trainer.predict`，导出带纹理网格时 mesh-exporter 要对 DMTet 网格做多视角可微光栅化来烘焙纹理，这一步需要构建自动微分图。若 PL 在 predict 阶段默认套上 `torch.inference_mode()`，梯度无法回传，烘焙会失败（[launch.py:182](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L182)）。

**练习 2**：`--validate`、`--test`、`--export` 三个模式为什么都要调用 `set_system_status`？

**答案**：PL 只在 `fit` 时从检查点恢复 `epoch/global_step`，其余接口不恢复。DreamCraft3D 的许多行为（损失权重四元组调度、渐进式训练参数）按 `true_global_step` 取值，若不手动把检查点中的步数写回系统（[launch.py:188-192](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/launch.py#L188-L192)），validate/test/export 阶段就会以步数 0 的配置去渲染，结果与训练末期的实际状态不符。

### 4.5 callbacks.py 精读：快照、进度条与进度文件

#### 4.5.1 概念说明

`threestudio/utils/callbacks.py` 提供四个自定义回调，回答两个问题：**如何保证实验可复现**（代码快照 + 配置快照）和**如何向用户报告进度**（终端进度条 / Gradio 进度文件）。它们都是 PL `Callback` 的子类，通过上一节的 callbacks 列表挂载，`on_fit_start` 等钩子由 PL 在恰当时机自动调用。

#### 4.5.2 核心流程

```text
VersionedCallback（基类）
    ├─ 管理 save_root 下的 version_N 子目录（use_version=False 时直接写 save_root）
    ├─ CodeSnapshotCallback：on_fit_start → git ls-files 收集文件 → 整仓拷贝到 code/
    ├─ ConfigSnapshotCallback：on_fit_start → dump parsed.yaml + 拷贝 raw.yaml 到 configs/
    ├─ CustomProgressBar：继承 TQDMProgressBar，仅去掉进度条里的 v_num 字段
    └─ ProgressCallback：把百分比/阶段文字覆写进 progress 文件，供 Gradio 前端轮询
```

#### 4.5.3 源码精读

[callbacks.py:19-57](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L19-L57) 是基类 `VersionedCallback`：`_get_next_version` 扫描 `save_root` 下已有 `version_N` 目录取最大编号 +1；`savedir` 属性决定最终写入路径——launch.py 传的是 `use_version=False`，所以快照直接写进 `{trial_dir}/code`、`{trial_dir}/configs`，不带版本子目录。

[callbacks.py:60-94](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L60-L94) 是 `CodeSnapshotCallback`。`get_file_list`（L64-77）用两条 `git ls-files` 命令取并集：跟踪文件中排除 `load/*`（体积庞大的权重目录），加上未跟踪但未被 gitignore 的文件——即“当前工作区的真实代码状态”。`save_code_snapshot`（L79-86）装饰了 `@rank_zero_only`，多卡时只有 0 号进程拷贝，避免写冲突。`on_fit_start`（L88-94）用裸 `except` 兜底：不在 git 仓库或没装 git 时仅发出警告而不中断训练。

[callbacks.py:97-110](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L97-L110) 是 `ConfigSnapshotCallback`：把**解析后**的完整配置 dump 成 `parsed.yaml`（所有 `${...}` 插值已展开、命令行覆盖已合入），同时把**原始** yaml 拷贝为 `raw.yaml`。这两个文件是复现实验的关键——README 中导出网格的命令正是拿 `parsed.yaml` 作为 `--config` 输入。

[callbacks.py:113-118](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L113-L118) 是 `CustomProgressBar`：继承 PL 自带的 `TQDMProgressBar`，唯一改动是在 `get_metrics` 里弹掉 `v_num`（实验版本号），让进度条更干净。

[callbacks.py:121-156](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L121-L156) 是 `ProgressCallback`，Gradio 专用。`write` 方法（L133-138）每次 `seek(0)` + `truncate()` **覆写**同一个进度文件（而非追加），文件里永远只有一行当前状态；四个钩子分别在训练每批结束写百分比（`pl_module.true_global_step / trainer.max_steps * 100`）、验证/测试/导出开始时写阶段文字。`gradio_app.py` 前端轮询这个 `progress` 文件即可向用户展示实时进度。

#### 4.5.4 代码实践

**实践目标**：通过阅读 `ConfigSnapshotCallback` 的产物，理解“原始配置”与“解析后配置”的差别。

**操作步骤**：

1. 若 4.4.4 的训练已跑通，打开 `{trial_dir}/configs/raw.yaml` 与 `{trial_dir}/configs/parsed.yaml` 对比；
2. 重点看两处：`tag: ${rmspace:${system.prompt_processor.prompt},_}` 插值是否已被展开成真实字符串；`data.requires_normal: ${cmaxgt0:${system.loss.lambda_normal}}` 是否已被求值为布尔值；
3. 未跑训练时，可直接对比仓库里的 [configs/dreamcraft3d-coarse-nerf.yaml](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/configs/dreamcraft3d-coarse-nerf.yaml#L1-L17) 与 4.3.4 脚本打印的 `cfg`（`print(cfg)` 即可输出解析后的配置树）。

**需要观察的现象**：raw.yaml 中所有 `${...}` 在 parsed.yaml 中都变成了具体值；命令行覆盖的参数也体现在 parsed.yaml 中。

**预期结果**：以 coarse-nerf 为例，parsed 后 `tag` 变为 prompt 的下划线形式；`data.requires_normal` 因 `system.loss.lambda_normal` 为 `0.0` 经 `cmaxgt0` 求值为 `False`。训练未跑通时用 4.3.4 的 `print(cfg)` 替代，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `save_code_snapshot` 和 `save_config_snapshot` 都要加 `@rank_zero_only`？

**答案**：多卡训练时每个进程都会执行 `on_fit_start`，若都去拷贝文件、写 yaml，轻则浪费 IO，重则因并发写同一目标文件而损坏。`rank_zero_only`（来自 PL）保证仅 0 号进程执行函数体，其余进程跳过（[callbacks.py:79](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L79)、[callbacks.py:103](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L103)）。

**练习 2**：`ProgressCallback.write` 为什么用 `seek(0)` + `truncate()` 而不是 `open(path, "w")` 重开文件或追加写？

**答案**：Gradio 前端在持续读取该文件。反复 close/reopen 会造成读取方看到文件瞬间消失；追加写则会让文件无限膨胀且读不到"当前值"。覆写同一文件句柄（先回到文件头、截断、写入、flush）保证文件始终存在且内容恒为最新一行（[callbacks.py:133-138](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L133-L138)）。

**练习 3**：`CodeSnapshotCallback` 为什么排除 `load/*` 又额外收集 untracked 文件？

**答案**：`load/` 存放数 GB 的预训练权重，拷贝既慢又占空间，而复现代码并不需要它；反过来，你自己新写、还没 `git add` 的扩展文件（untracked 但未被 ignore）恰恰可能影响实验结果，必须一并快照。两条 `git ls-files` 取并集（[callbacks.py:64-77](https://github.com/deepseek-ai/DreamCraft3D/blob/5829ef116d36c871ce2b9e54a6153dd3856a1561/threestudio/utils/callbacks.py#L64-L77)）实现了"完整工作区代码、但不带权重"的快照。

## 5. 综合实践

**任务：为 `main()` 制作一张带行号的执行流程注释表，并验证一处配置覆盖。**

1. **通读加注**：复制 `launch.py` 为 `launch_annotated.py`（自己练习用的副本，不进 git），按下表在对应位置补上中文注释，注释必须用自己的话概括该段在做什么：

   | 代码位置 | 你的注释应覆盖的要点 |
   | --- | --- |
   | L43-61 | 环境变量先于 torch 导入；外部 `CUDA_VISIBLE_DEVICES` 优先于 `--gpu`；`devices=-1` 的含义 |
   | L98-105 | yaml+CLI 合并、`parse_structured`、`custom_import` 触发外部注册 |
   | L109 | `find(cfg.data_type)(cfg.data)`：注册名 → 类 → 参数实例化 |
   | L112-122 | 自动续训的 glob 扫描；`resumed` 标志传给 system |
   | L133-177 | 每个 callback/logger 写到哪个目录 |
   | L179-210 | `**cfg.trainer` 透传；`inference_mode=False` 的原因；四种模式分派与 `set_system_status` |

2. **脚本验证**：完成 4.3.4 的 `inspect_config.py`，并扩展打印 `cfg.trainer["max_steps"]` 与 `cfg.checkpoint`。
3. **对照检查**：把注释表与 4.3.4 脚本输出互相印证——例如你注释里写的“trainer 段透传给 Trainer”，应能在脚本输出里看到 `max_steps=5000` 与 yaml 中的值一致。
4. **进阶（可选）**：给脚本再加一个命令行覆盖 `trainer.max_steps=100`，确认输出随之变化，体会 README 示例命令里那些 `key=value` 参数的最终归宿。

预期产物：一份带注释的 `launch_annotated.py`、一张 24 行以内的执行流程表、以及脚本的一次成功输出（或明确的「待本地验证」记录）。

## 6. 本讲小结

- `launch.py` 是全项目唯一训练/导出入口：命令行参数经 `parse_known_args` 拆成“脚本参数 + 配置覆盖”，后者由 `load_config` 中的 `OmegaConf.from_cli` 合入配置。
- GPU 选择必须在 `import pytorch_lightning` 之前完成（延迟导入），外部 `CUDA_VISIBLE_DEVICES` 优先于 `--gpu`，PL 层面恒用 `devices=-1` 消费所有可见卡。
- 组件构建就是注册机制的消费现场：`threestudio.find(cfg.data_type/system_type)` 把配置字符串变成类；`custom_import` 支持外部扩展注入；逐卡种子 `seed + get_rank()` 保证多卡采样不同。
- `--train` 模式自带断点自动续训：未显式指定 `cfg.resume` 时自动取 `ckpts/` 下字典序最新的检查点。
- callbacks 生成试验目录的绝大部分内容：ModelCheckpoint→`ckpts/`、CodeSnapshot→`code/`、ConfigSnapshot→`configs/`（parsed.yaml + raw.yaml），loggers 产出 `tb_logs/`、`csv_logs/`，另有 `cmd.txt` 记录完整命令。
- 四种模式分派到 `fit/validate/test/predict`；`inference_mode=False` 保证导出时可微光栅化，`set_system_status` 在非 fit 模式手动恢复 `epoch/global_step` 以驱动步数感知调度。

## 7. 下一步学习建议

下一讲（u2-l1）将走进第一个“真数据”环节：`preprocess_image.py` 如何把一张普通图片变成训练所需的 RGBA 参考图与深度/法向图。如果你对配置系统意犹未尽，可以先跳读 u2-l2（OmegaConf resolver 与 `ExperimentConfig` 的细节），再回到单元二按顺序学习四阶段配置对比（u2-l3）与训练产物/网格导出（u2-l4）。后续架构线（u3）会深入 `BaseSystem` 内部，本讲 4.3 节出现的 `set_resume_status`、`get_loggers` 到时会有完整解释。
