# 检查点保存与断点续训

## 1. 本讲目标

大模型训练动辄几小时甚至几天，中途机器掉电、显存溢出（OOM）、被人为 Ctrl+C 中断几乎是常态。如果每次中断都得从第 0 步重训，时间成本不可接受。本讲解决的就是这个问题：**如何把训练现场存下来，并在下次启动时精确还原。**

学完本讲，你应该能够：

1. 说出 MiniMind 一份检查点实际写出的**两个文件**（推理权重 `.pth` + 续训权重 `_resume.pth`）各自的作用与命名规则。
2. 读懂 `lm_checkpoint` 函数的「保存 / 加载」双模式，理解它如何用 `os.replace` 做**原子写入**、用 `**kwargs` 顺便保存 `scaler` 等额外状态。
3. 说清楚**跨 GPU 数量恢复**时，`step` 为什么被乘以 `saved_ws // current_ws`，并能手算结果。
4. 理解 `init_distributed_mode` 如何为续训提供 `world_size` 上下文，以及 wandb 如何靠 `wandb_id` 续接同一个 run。
5. 能够用 `--from_resume 1` 完成一次「跑几步→中断→重启→从断点继续」的完整闭环验证。

## 2. 前置知识

本讲建立在你已经学完 [u4-l1 训练公共工具](u4-l1-training-common-utils.md) 的基础上。那里我们讲过三个本讲会反复用到的概念：

- **`get_lr` 与全局步数驱动**：学习率由 `epoch * iters + step` 这条「全局步数」曲线决定，所以续训时必须让 `step` 接得上，否则学习率会跳变。
- **`SkipBatchSampler`**：在索引层丢弃前 N 个 batch，配合 `iters = len(loader) + skip` 让全局步数连续不错位。本讲会看到它正是续训「跳过已训练 batch」的实现。
- **`init_distributed_mode` 与 `is_main_process`**：自动探测是否处于 `torchrun` 启动的 DDP 环境，只有主进程（rank 0）才该写磁盘。

另外你需要一点 PyTorch 基础常识：

- **`model.state_dict()` / `optimizer.state_dict()`**：把模型权重、优化器动量（如 AdamW 的一阶/二阶矩）序列化成字典，`torch.save` 成 `.pth` 文件；恢复时用 `load_state_dict` 灌回去。
- **DDP 包装**：`DistributedDataParallel(model)` 后，真正的模型藏在 `model.module` 里，存权重前要先剥掉这层壳。
- **`torch.compile`**：编译后的模型真正参数藏在 `model._orig_mod` 里，也要剥掉。
- **混合精度 `GradScaler`**：fp16 训练时它内部维护一个动态 loss scale，状态同样需要存盘，否则恢复后缩放因子重置可能立即溢出。

如果这些名词你还觉得陌生，可以先回到 u4-l1 复习，再继续往下读。

## 3. 本讲源码地图

本讲只涉及两个源码文件，加上 README 里的一段续训说明：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [trainer/trainer_utils.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py) | 训练公共工具箱 | `lm_checkpoint`（双重保存 / 加载 / 跨卡换算）、`init_distributed_mode`（提供 world_size 上下文） |
| [trainer/train_pretrain.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py) | 预训练主脚本（续训装配模板） | 「检测 ckp → 恢复状态 → SkipBatchSampler 跳步」的完整链路 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 项目说明 | 「检查点暂停续训」使用说明 |

核心就一句话：**所有 `train_*.py` 脚本共用 `lm_checkpoint` 这一个函数完成存与读**，预训练脚本只是把它们装配起来的「标准模板」，看懂它就等于看懂了 SFT / LoRA / DPO / 蒸馏等所有脚本的续训逻辑。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `lm_checkpoint` 的双重保存与原子写入**（讲「存」）
- **4.2 跨 GPU 数量的 step 自动换算**（讲「读」时最巧妙的一步）
- **4.3 续训装配链路：`init_distributed_mode` + 主脚本装配 + 续训说明**（讲「怎么串起来」）

### 4.1 lm_checkpoint：双重保存与原子写入

#### 4.1.1 概念说明

很多教程只存一份权重就完事了，但 MiniMind 选择**一次保存写两个文件**，分别服务两种完全不同的用途：

| 文件 | 命名（dim=768 为例） | 内容 | 用途 |
| --- | --- | --- | --- |
| **推理权重** | `pretrain_768.pth` | 只有模型权重（fp16） | 直接拿去 `eval_llm.py` 推理、转格式、部署 |
| **续训权重** | `pretrain_768_resume.pth` | 模型权重 + 优化器 + scaler + epoch + step + world_size + wandb_id | 下次 `--from_resume 1` 时还原训练现场 |

为什么要分开？

- **推理权重必须干净**：它只存「参数」，体积极小（fp16），任何下游工具（ollama、vllm、transformers）都能直接 `load_state_dict`。把优化器动量塞进去反而碍事。
- **续训权重必须完整**：要精确还原训练，光有参数不够。AdamW 的动量、学习率走到哪一步（step）、当前在第几个 epoch、GradScaler 的 loss scale……缺一个都会让续训后的曲线「抖一下」。所以它是个**大字典**，把现场全打包。

命名规则延续了 u1-l1 介绍的约定：`{weight}_{hidden_size}{_moe?}.pth`，MoE 追加 `_moe`，续训文件再追加 `_resume`。这样不同维度、不同架构、不同阶段的检查点天然不会撞名。

#### 4.1.2 核心流程

`lm_checkpoint` 用一个函数承担两种模式，靠「`model` 参数是否为 `None`」来切换：

```
lm_checkpoint(lm_config, weight, model, optimizer, ...)
│
├─ model is not None  →  保存模式
│   1. 算出 ckp_path（推理）与 resume_path（续训）两个路径
│   2. 剥掉 DDP / torch.compile 的壳，拿到 raw_model
│   3. state_dict 全部 .half().cpu()
│   4. 原子写推理权重：存到 .tmp → os.replace 成正式文件
│   5. 组装 resume_data 大字典（含 world_size、wandb_id）
│   6. 把 kwargs 里带 state_dict 的对象（如 scaler）也塞进去
│   7. 原子写续训权重：同样 .tmp → os.replace
│
└─ model is None  →  加载模式（见 4.2）
    1. 若 resume_path 不存在 → 返回 None（从头训）
    2. 否则 torch.load 读出字典
    3. 若 world_size 变了 → 换算 step（4.2 详述）
    4. 返回 ckp_data 给主脚本去 load_state_dict
```

**原子写入（atomic write）** 是这里最值得学的工程细节。所谓原子，就是「写文件」这个动作要么完整成功、要么等于没发生，绝不存在「写了一半」的中间态。做法是：

1. 先把内容写到 `xxx.pth.tmp` 临时文件；
2. 写完且校验通过后，用 `os.replace(tmp, final)` 把临时文件**改名**成正式文件。

`os.replace` 在 POSIX 和 Windows 上都是原子操作。这样如果在第 1 步写盘时进程崩溃（断电、OOM、kill），崩溃的是 `.tmp`，正式的 `.pth` 依然是上一次完整的版本，下次续训仍能正确加载。如果没有这层保护，半成品文件会覆盖掉好文件，上一次的进度就真的丢了。

#### 4.1.3 源码精读

先看函数签名和路径计算。[trainer/trainer_utils.py:63-67](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L63-L67) 里，`moe_path` 用一个三目表达式决定是否加 `_moe` 后缀，两个路径仅差一个 `_resume`：

```python
def lm_checkpoint(lm_config, weight='full_sft', model=None, optimizer=None, epoch=0, step=0, wandb=None, save_dir='../checkpoints', **kwargs):
    os.makedirs(save_dir, exist_ok=True)
    moe_path = '_moe' if lm_config.use_moe else ''
    ckp_path = f'{save_dir}/{weight}_{lm_config.hidden_size}{moe_path}.pth'
    resume_path = f'{save_dir}/{weight}_{lm_config.hidden_size}{moe_path}_resume.pth'
```

接着是「剥壳 + 转半精度」。[trainer/trainer_utils.py:69-76](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L69-L76) 连剥两层：先剥 `DistributedDataParallel` 的 `.module`，再剥 `torch.compile` 的 `._orig_mod`，保证存出的 key 没有 `module.` 或 `_orig_mod.` 前缀；`.half().cpu()` 既压缩体积，又避免恢复时设备不对：

```python
    if model is not None:
        raw_model = model.module if isinstance(model, DistributedDataParallel) else model
        raw_model = getattr(raw_model, '_orig_mod', raw_model)
        state_dict = raw_model.state_dict()
        state_dict = {k: v.half().cpu() for k, v in state_dict.items()}
        ckp_tmp = ckp_path + '.tmp'
        torch.save(state_dict, ckp_tmp)
        os.replace(ckp_tmp, ckp_path)          # 原子写推理权重
```

然后组装续训大字典。[trainer/trainer_utils.py:85-104](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L85-L104) 是本模块的核心。`world_size` 通过 `dist.get_world_size()` 取（未初始化时返回 1），`wandb_id` 的取法则兼容了 swanlab（有 `get_run()`）和原生 wandb（有 `.id`）两种库；`**kwargs` 循环把所有「带 `state_dict` 方法的对象」自动序列化——这就是为什么主脚本只需多传一个 `scaler=scaler`，scaler 状态就被顺便存进去了：

```python
        resume_data = {
            'model': state_dict,
            'optimizer': optimizer.state_dict(),
            'epoch': epoch,
            'step': step,
            'world_size': dist.get_world_size() if dist.is_initialized() else 1,
            'wandb_id': wandb_id
        }
        for key, value in kwargs.items():
            if value is not None:
                if hasattr(value, 'state_dict'):
                    raw_value = value.module if isinstance(value, DistributedDataParallel) else value
                    raw_value = getattr(raw_value, '_orig_mod', raw_value)
                    resume_data[key] = raw_value.state_dict()
                else:
                    resume_data[key] = value

        resume_tmp = resume_path + '.tmp'
        torch.save(resume_data, resume_tmp)
        os.replace(resume_tmp, resume_path)    # 原子写续训权重
```

注意一个设计上的「巧」：`resume_data['model']` 复用了上面已经 `.half().cpu()` 过的 `state_dict`，没有重复存。写完后 `del state_dict, resume_data; torch.cuda.empty_cache()` 主动释放显存，避免长训练时累积内存。

> 关于 wandb：README 与命令行参数都叫 `--use_wandb`，但 [train_pretrain.py:127](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L127) 实际是 `import swanlab as wandb`，即把国产库 swanlab 别名为 wandb。`lm_checkpoint` 里 `hasattr(wandb, 'get_run')` 的分支正是为了兼容这两个 API 不同的库。

#### 4.1.4 代码实践

**实践目标**：用最小配置实跑几步预训练，亲眼看到 `checkpoints/` 目录里同时出现两个文件，并用 Python 读取续训文件，确认它是个「大字典」。

**操作步骤**：

1. 进入 trainer 目录并启动一次极小规模预训练，把保存间隔调到非常小，几步之内就会触发保存：

   ```bash
   cd trainer
   python train_pretrain.py \
     --epochs 1 --batch_size 2 --max_seq_len 64 \
     --save_interval 5 --log_interval 1 \
     --data_path ../dataset/pretrain_t2t_mini.jsonl
   ```

   按 Ctrl+C 在打印到 `step 6` 之后中断（让它至少存过一次盘）。

2. 列出检查点目录，预期看到 `pretrain_768.pth` 与 `pretrain_768_resume.pth` 两个文件：

   ```bash
   ls -lh ../checkpoints/
   ```

3. 写一段**示例代码**（不是项目原有脚本）读取续训文件，打印它包含哪些键：

   ```python
   # 示例代码：inspect_resume_ckp.py
   import torch
   ckp = torch.load('../checkpoints/pretrain_768_resume.pth', map_location='cpu')
   print("顶层键:", list(ckp.keys()))
   print("step =", ckp['step'], " epoch =", ckp['epoch'],
         " world_size =", ckp['world_size'], " wandb_id =", ckp['wandb_id'])
   print("model 参数个数 =", len(ckp['model']))
   print("optimizer 顶层键 =", list(ckp['optimizer'].keys()))
   ```

**需要观察的现象**：

- 两个 `.pth` 中，续训文件明显大于推理权重（多了 optimizer 动量）。
- `step` 等于中断时主进程打印的最大 step 值；`world_size` 等于你实际使用的 GPU 数（单卡为 1）。
- `optimizer` 字典里通常有 `state`（存一阶/二阶矩）和 `param_groups`（存学习率等）。

**预期结果**：续训文件是一个含 `model / optimizer / scaler / epoch / step / world_size / wandb_id` 七类（scaler 由 kwargs 注入）内容的大字典，证明「一次保存、两种用途」。若你的环境无法跑训练，则「待本地验证」，但第 3 步的读取脚本只要有任意一份 `_resume.pth` 即可独立验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么推理权重 `pretrain_768.pth` 里**不**包含优化器状态，而续训权重必须包含？

> **参考答案**：推理只需要模型参数算前向，优化器动量是「训练专属」的无用负担，去掉能减小体积、便于下游工具直接加载。续训则相反：AdamW 的一阶/二阶矩编码了「过去若干步梯度的滑动平均」，若不恢复，优化器相当于冷启动，续训初期会出现明显的 loss 抖动和学习率有效步长错配。

**练习 2**：如果把 `os.replace(ckp_tmp, ckp_path)` 直接换成 `torch.save(state_dict, ckp_path)`，最坏情况会发生什么？

> **参考答案**：失去原子性。若 `torch.save` 写到一半时进程被杀（断电、OOM、kill -9），`ckp_path` 会是一个截断、不完整的文件，覆盖掉了上一次完好的权重。下次续训或推理加载它会报反序列化错误，相当于把上一次的进度也丢了。`.tmp + os.replace` 保证正式文件要么是新完整版、要么仍是旧完整版。

**练习 3**：`lm_checkpoint` 是怎么做到「主脚本传一个 `scaler=scaler` 就能自动存盘」的？

> **参考答案**：靠 `**kwargs`。函数签名里收作 `**kwargs`，函数体里遍历 `kwargs.items()`，对「有 `state_dict` 方法的对象」调用 `raw_value.state_dict()` 存进 `resume_data`。scaler 恰好实现了 `state_dict()`，于是被自动序列化为 `resume_data['scaler']`。这是一种「按协议（duck typing）扩展」的写法，未来要加 scheduler 等也无需改 `lm_checkpoint`。

---

### 4.2 跨 GPU 数量的 step 自动换算

#### 4.2.1 概念说明

这是整个续训机制里最巧妙、也最容易被忽略的一步。场景很现实：

- 训练时用 4 张卡（`world_size=4`），存盘时 `step=1000`。
- 中断后，下次只剩 2 张卡可用（`world_size=2`），想接着练。

如果直接把 `step=1000` 灌回去，会出问题。原因在于 **DDP 下「每个进程的 step」并不等于「全局优化器更新次数」**，而是与 GPU 数量绑定的「每卡见过多少 batch」。换卡数后，旧的 `step` 数值在新环境里代表的进度变了。

`lm_checkpoint` 在加载时做了一次自动换算，让续训后的**全局数据吞吐量**保持不变，从而保证学习率曲线（由全局步数驱动）连续不跳变。README 也明确把这点列为续训特性之一（见 4.3.3）。

#### 4.2.2 核心流程

关键公式（保存的 `step` 是「每进程」步数）：

设全局已处理的微批次（micro-batch）总数为 \(N_{\text{global}}\)，每卡每 step 处理 1 个微批次，则有：

\[
N_{\text{global}} = \text{step} \times \text{ws}
\]

换卡续训时，希望 \(N_{\text{global}}\) 不变，于是新的每进程步数：

\[
\text{step}_{\text{new}} = \left\lfloor \text{step}_{\text{old}} \times \frac{\text{ws}_{\text{saved}}}{\text{ws}_{\text{current}}} \right\rfloor
\]

举几个例子：

| 保存时 ws | 当前 ws | 保存的 step | 换算后 step | 含义 |
| --- | --- | --- | --- | --- |
| 4 | 4 | 1000 | 1000 | 卡数没变，不变 |
| 4 | 2 | 1000 | 2000 | 卡减半，每卡要多走一倍才等价 |
| 2 | 4 | 1000 | 500 | 卡翻倍，每卡少走一半 |
| 4 | 1 | 1000 | 4000 | 退化成单卡，全部进度堆到一张卡上 |

注意一个**诚实的限制**：这个换算只保证「全局样本吞吐量」和「学习率曲线」连续，但 DDP 的 `DistributedSampler` 在不同 `world_size` 下切分数据的方式不同，所以**严格的数据顺序并不能完全复原**。它是一个「尽力而为」的恢复，对长训练来说足够实用，但不是数学上完美无缝。

#### 4.2.3 源码精读

换算逻辑藏在加载模式里。[trainer/trainer_utils.py:107-116](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L107-L116)：当 `model is None` 时进入加载分支，先看 `resume_path` 是否存在（不存在则返回 `None` 表示从头训）；存在则 `torch.load` 读出，比较保存时的 `world_size` 与当前 `world_size`，不等就做整数换算并打日志：

```python
    else:  # 加载模式
        if os.path.exists(resume_path):
            ckp_data = torch.load(resume_path, map_location='cpu')
            saved_ws = ckp_data.get('world_size', 1)
            current_ws = dist.get_world_size() if dist.is_initialized() else 1
            if saved_ws != current_ws:
                ckp_data['step'] = ckp_data['step'] * saved_ws // current_ws
                Logger(f'GPU数量变化({saved_ws}→{current_ws})，step已自动转换为{ckp_data["step"]}')
            return ckp_data
        return None
```

三个细节值得点出：

1. `ckp_data.get('world_size', 1)` 用 `.get` 且默认 1，是为了兼容**旧版本**没存 `world_size` 字段的检查点——典型的向前兼容写法。
2. 用整除 `//` 而非浮点除法，保证 `step` 仍是整数（采样器跳步需要整数）；代价是会有至多 1 步的舍入误差，对长训练可忽略。
3. `current_ws` 的取值依赖 `dist.is_initialized()`——这正引出下一个模块：`init_distributed_mode` 必须在调用 `lm_checkpoint` 加载之前完成，否则 `dist` 没初始化、`world_size` 永远是 1，换算就会出错。

#### 4.2.4 代码实践

**实践目标**：在没有多卡的环境下，也能验证换算公式的正确性，并理解它的「全局吞吐不变」语义。

**操作步骤**：写一段**示例代码**模拟换算，复现上表的几种场景：

```python
# 示例代码：simulate_step_convert.py
def convert_step(step, saved_ws, current_ws):
    return step * saved_ws // current_ws

for saved_ws, current_ws in [(4, 4), (4, 2), (2, 4), (4, 1)]:
    step = 1000
    new_step = convert_step(step, saved_ws, current_ws)
    # 全局吞吐量（step × ws）应保持不变
    g_old = step * saved_ws
    g_new = new_step * current_ws
    print(f"ws {saved_ws}->{current_ws}: step {step}->{new_step}, "
          f"全局 {g_old}->{g_new}")
```

**需要观察的现象**：除整除舍入外，`g_old` 与 `g_new` 几乎相等，证明换算守恒。

**预期结果**：输出显示「4→2」时 step 变为 2000、「2→4」时变为 500，全局吞吐量稳定在 4000 附近。

**进阶（可选，需多卡，待本地验证）**：在 `lm_checkpoint` 的换算行（[trainer_utils.py:113](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L113)）后临时加一行 `print(saved_ws, current_ws, ckp_data['step'])`，用 `torchrun --nproc_per_node 2` 跑一次中断，再换 `--nproc_per_node 1` 加 `--from_resume 1` 重启，观察日志里的 `GPU数量变化(2→1)，step已自动转换为...`。

#### 4.2.5 小练习与答案

**练习 1**：保存时 `ws=8, step=500`，恢复时只有 `ws=2`，换算后的 step 是多少？全局吞吐量前后各是多少？

> **参考答案**：`step_new = 500 * 8 // 2 = 2000`。全局吞吐：保存时 \(500 \times 8 = 4000\)，恢复后 \(2000 \times 2 = 4000\)，守恒。

**练习 2**：为什么换算用整除 `//` 而不是浮点 `* saved_ws / current_ws`？

> **参考答案**：`step` 后续被 `SkipBatchSampler` 当作「要跳过的整数 batch 数」使用，必须是整数。浮点除法会得到小数，且引入浮点精度问题；整除保证类型正确，代价仅是至多 1 步的舍入误差。

**练习 3**：假设你忘了先调用 `init_distributed_mode`，直接在单进程里 `lm_checkpoint(...)` 加载一份 `ws=4` 存的检查点，会发生什么？

> **参考答案**：`dist.is_initialized()` 为 `False`，`current_ws` 恒为 1，于是 `step = step * 4 // 1`，step 被放大 4 倍，可能超过单个 epoch 的 `iters` 导致 `SkipBatchSampler` 跳过全部 batch、本 epoch 直接空转。这就是为什么主脚本里 `init_distributed_mode` 必须排在 `lm_checkpoint` 之前。

---

### 4.3 续训装配链路：init_distributed_mode + 主脚本装配 + 续训说明

#### 4.3.1 概念说明

有了 `lm_checkpoint` 这个「存读一体」的工具函数，还要有人把它正确地**装配**到训练流程里。装配要回答四个问题：

1. **何时探测分布**：必须在读检查点之前初始化 DDP，才能拿到正确的 `world_size`。
2. **何时检测续训**：在创建模型与优化器之前，先看有没有 `_resume.pth`，以决定是冷启动还是热启动。
3. **如何恢复状态**：把字典里的 `model / optimizer / scaler` 灌回刚建好的对象。
4. **如何跳过已训练 batch**：用 `SkipBatchSampler`（u4-l1 讲过）丢掉前 `start_step` 个 batch，并把 `iters` 补回去，保证学习率曲线连续。

`init_distributed_mode` 在这里的角色是「**世界信息提供者**」：它本身不碰检查点，但它把进程组初始化好，让后续 `dist.get_world_size()` 能返回真实卡数。没有它，4.2 的换算就会失真。

#### 4.3.2 核心流程

以 [train_pretrain.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py) 为标准模板，9 步初始化里与续训相关的几步：

```
Step 1  init_distributed_mode()     ← 初始化 DDP，得到 local_rank（也提供 world_size）
Step 2  lm_checkpoint(..., model=None)   ← 若 --from_resume 1，读出 ckp_data（含换算后的 step）
Step 4  wandb.init(id=ckp_data['wandb_id'], resume='must')  ← 续接同一个 run
Step 5  init_model(...) 建模型；optim.AdamW(...) 建优化器；GradScaler(...) 建 scaler
Step 6  if ckp_data: load_state_dict(model / optimizer / scaler); 记下 start_epoch, start_step
Step 8  skip = start_step; SkipBatchSampler(..., skip); iters = len(loader) + skip
        train_epoch(epoch, loader, iters, start_step, wandb)
```

wandb 续接是这条链路上的另一个亮点：保存时把 `wandb_id` 写进续训文件（4.1.3），恢复时读出来传给 `wandb.init(id=..., resume='must')`，新一段训练就会**接到原来那条 loss 曲线后面**，而不是另开一条新 run。这对长期观察训练趋势非常关键。

#### 4.3.3 源码精读

先看 `init_distributed_mode`。[trainer/trainer_utils.py:44-51](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L44-L51) 用环境变量 `RANK` 判断是否由 `torchrun` 启动：没有 `RANK` 就返回 0（单进程模式）；有则用 nccl 初始化进程组、绑定当前 GPU：

```python
def init_distributed_mode():
    if int(os.environ.get("RANK", -1)) == -1:
        return 0  # 非DDP模式
    dist.init_process_group(backend="nccl")
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    return local_rank
```

回到主脚本，看装配顺序。Step 1 先初始化分布，[train_pretrain.py:110-112](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L110-L112)：

```python
    local_rank = init_distributed_mode()
    if dist.is_initialized(): args.device = f"cuda:{local_rank}"
    setup_seed(42 + (dist.get_rank() if dist.is_initialized() else 0))
```

Step 2 在建模型之前探测续训，[train_pretrain.py:117](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L117) 注意它调用时 `model=None`，所以走的是 4.2 的加载分支：

```python
    ckp_data = lm_checkpoint(lm_config, weight=args.save_weight, save_dir='../checkpoints') if args.from_resume==1 else None
```

Step 4 wandb 续接，[train_pretrain.py:126-131](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L126-L131)，只有有 `wandb_id` 时才传 `resume='must'`：

```python
    if args.use_wandb and is_main_process():
        import swanlab as wandb
        wandb_id = ckp_data.get('wandb_id') if ckp_data else None
        resume = 'must' if wandb_id else None
        wandb.init(project=args.wandb_project, name=wandb_run_name, id=wandb_id, resume=resume)
```

Step 6 状态恢复，[train_pretrain.py:141-147](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L141-L147) 把三件套灌回，并记下 `start_epoch / start_step`：

```python
    start_epoch, start_step = 0, 0
    if ckp_data:
        model.load_state_dict(ckp_data['model'])
        optimizer.load_state_dict(ckp_data['optimizer'])
        scaler.load_state_dict(ckp_data['scaler'])
        start_epoch = ckp_data['epoch']
        start_step = ckp_data.get('step', 0)
```

Step 8 跳步装配，[train_pretrain.py:157-167](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L157-L167)。`skip` 只在「断点所在的那个 epoch」生效；关键一行是 `train_epoch(epoch, loader, len(loader) + skip, start_step, wandb)`——把 `iters` 补上 `skip`，让 `enumerate` 从 `start_step+1` 起步、却把总数当作没断过，从而学习率分母 `args.epochs * iters` 保持原计划不变：

```python
    for epoch in range(start_epoch, args.epochs):
        train_sampler and train_sampler.set_epoch(epoch)
        setup_seed(42 + epoch); indices = torch.randperm(len(train_ds)).tolist()
        skip = start_step if (epoch == start_epoch and start_step > 0) else 0
        batch_sampler = SkipBatchSampler(train_sampler or indices, args.batch_size, skip)
        loader = DataLoader(train_ds, batch_sampler=batch_sampler, num_workers=args.num_workers, pin_memory=True)
        if skip > 0:
            Logger(f'Epoch [{epoch + 1}/{args.epochs}]: 跳过前{start_step}个step，从step {start_step + 1}开始')
            train_epoch(epoch, loader, len(loader) + skip, start_step, wandb)
        else:
            train_epoch(epoch, loader, len(loader), 0, wandb)
```

最后看「存」的触发点。在 `train_epoch` 内部，[train_pretrain.py:61-71](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L61-L71) 用 `is_main_process()` 守卫，确保只有 rank 0 写盘；它先直接 `torch.save` 一份推理权重（这行是历史遗留的快路径），紧接着调用 `lm_checkpoint` 把完整的续训字典写出来，并把 `scaler` 通过 kwargs 传入：

```python
        if (step % args.save_interval == 0 or step == iters) and is_main_process():
            model.eval()
            ...
            torch.save({k: v.half().cpu() for k, v in state_dict.items()}, ckp)
            lm_checkpoint(lm_config, weight=args.save_weight, model=model, optimizer=optimizer,
                          scaler=scaler, epoch=epoch, step=step, wandb=wandb, save_dir='../checkpoints')
            model.train()
```

> 细心的读者会注意到：这里先 `torch.save` 写了一遍推理权重，`lm_checkpoint` 内部又写了一遍同样的推理权重。这是「先有一份能用的推理权重保底，再补全续训现场」的冗余写法，两份推理权重内容一致，属于可接受的重复。

README 对这套机制的官方说明在 [README.md:298-317](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L298-L317) 的「检查点暂停续训」折叠块里，要点与源码一一对应：自动存于 `./checkpoints/`、命名 `<权重名>_<维度>_resume.pth`、支持跨 GPU 数量恢复（自动调整 step）、支持 wandb 记录连续性。

#### 4.3.4 代码实践

**实践目标**：完成「跑几步→中断→重启→从断点继续」的完整闭环，亲眼看到 step 接续。

**操作步骤**：

1. 第一段训练，故意用很短的保存间隔，并在打印几个 step 后 Ctrl+C 中断：

   ```bash
   cd trainer
   python train_pretrain.py --epochs 1 --batch_size 2 --max_seq_len 64 \
     --save_interval 5 --log_interval 1 --from_resume 1 \
     --data_path ../dataset/pretrain_t2t_mini.jsonl
   # 看到 step 走到 6 以上后 Ctrl+C
   ```

2. **不要删除** `../checkpoints/pretrain_768_resume.pth`，用**完全相同的命令**再次启动：

   ```bash
   python train_pretrain.py --epochs 1 --batch_size 2 --max_seq_len 64 \
     --save_interval 5 --log_interval 1 --from_resume 1 \
     --data_path ../dataset/pretrain_t2t_mini.jsonl
   ```

3. 若开了 `--use_wandb`，再额外观察：重启后 swanlab/wandb 面板上的曲线是否接在原 run 后面，而不是新开一条。

**需要观察的现象**：

- 第二次启动时，日志应出现 `Epoch [...]: 跳过前 N 个step，从step N+1 开始`，其中 N 是第一次中断时的 step。
- `get_lr` 计算出的学习率与第一次中断那一刻的学习率**几乎相同**（因为全局步数接续）。
- 若前后 GPU 数不同，还会看到 `GPU数量变化(X→Y)，step已自动转换为...`。

**预期结果**：续训 loss 从中断时的水平继续下降，而不是从初始高位重来；学习率曲线无跳变。若本地无 GPU 或数据未下载，则「待本地验证」，但可在 [train_pretrain.py:164](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L164) 的 `Logger` 行确认跳步日志格式。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `lm_checkpoint`（加载）必须排在 `init_model`（建模型）**之前**调用？

> **参考答案**：因为加载分支要做跨 GPU 的 step 换算，需要 `dist.get_world_size()` 返回真实当前卡数；而 `dist` 是否初始化由 `init_distributed_mode` 决定，它在 Step 1 执行。若把加载放在建模型之后虽也能跑，但必须仍在 `init_distributed_mode` 之后。更本质的原因是：要先知道 `ckp_data` 是否存在，才能决定是冷启动（随机初始化）还是加载权重，避免白建一份又立刻覆盖。

**练习 2**：Step 8 里 `skip = start_step` 只在 `epoch == start_epoch` 时生效，后续 epoch 为什么 `skip=0`？

> **参考答案**：断点只发生在一个具体的 epoch 的某个 step。续训从这个 epoch 的下一步开始；一旦这个 epoch 跑完进入下一个 epoch，数据要重新打乱（`setup_seed(42+epoch)` + `set_epoch`），从头遍历新顺序，没有任何 batch 该被跳过，所以 `skip=0`。

**练习 3**：wandb 续接靠的是 `id=wandb_id, resume='must'`。如果保存时 `wandb` 为 `None`（没开 `--use_wandb`），`resume_data['wandb_id']` 会是什么？续训时又会怎样？

> **参考答案**：`lm_checkpoint` 里 `wandb_id` 初始为 `None`，未开 wandb 时不会进 `if wandb:` 分支，故 `resume_data['wandb_id'] = None`。续训时 `ckp_data.get('wandb_id')` 为 `None`，于是 `resume = None`，`wandb.init` 会新开一个 run——这是合理的，因为本来就没开追踪。

---

## 5. 综合实践

把三个模块串起来，设计一个「**给续训机制加一行诊断日志**」的小任务，既验证理解，又产出一份可观察的证据。

**任务**：在**不修改任何源码逻辑**的前提下（你可以临时加 print，验证后删掉），完成以下端到端验证，并提交一张「续训前后学习率与 step 对照表」。

1. **准备**：确认 `../dataset/pretrain_t2t_mini.jsonl` 存在；进 `trainer/` 目录。
2. **冷启动**：跑 `train_pretrain.py`，参数同 4.1.4，记录 step=5 时的 `lr` 与 `loss`，然后在 step=6 之后 Ctrl+C。
3. **热启动**：加 `--from_resume 1` 重启，在 [train_pretrain.py:56](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_pretrain.py#L56) 的 `current_lr` 行后临时加 `print('resume check:', step, current_lr)`，确认第一个打印的 step 紧接中断前的值、且 lr 与中断时几乎一致。
4. **跨卡换算（选做，待本地验证）**：若有条件，先用 `torchrun --nproc_per_node 2` 跑一段，再换 `--nproc_per_node 1 --from_resume 1`，截图日志里的 `GPU数量变化(2→1)，step已自动转换为...`。
5. **清理与结论**：删掉临时 print；写一段话解释——为什么续训后 loss 不从初始值开始、lr 却能接得上。

**预期结论**：loss 接续（因为模型权重、优化器动量都已恢复），lr 接续（因为全局步数 `epoch*iters+step` 经 SkipBatchSampler 与 `iters=len(loader)+skip` 补偿后连续），两者都依赖 `_resume.pth` 这个「完整现场字典」。这正是 MiniMind 续训机制的设计目标：**让中断对训练曲线几乎不可见。**

## 6. 本讲小结

- `lm_checkpoint` 是「存读一体」的单一函数，靠 `model` 是否为 `None` 切换模式，**一次保存写两个文件**：干净的推理权重 `.pth` 与完整的续训字典 `_resume.pth`。
- 续训字典打包了 `model / optimizer / scaler / epoch / step / world_size / wandb_id`，其中 `scaler` 等扩展状态靠 `**kwargs` + duck typing（有 `state_dict` 就存）自动序列化。
- 两份文件都用 `.tmp + os.replace` **原子写入**，断电/OOM 时不会留下半成品覆盖好文件。
- **跨 GPU 数量恢复**：`step_new = step_old * saved_ws // current_ws`，守恒量是「全局已处理微批次数」\(N_{\text{global}}=\text{step}\times\text{ws}\)，目的是让学习率曲线连续；它保证吞吐连续但不保证数据顺序完全复原。
- `init_distributed_mode` 必须排在 `lm_checkpoint` 之前，因为它提供了换算所需的 `world_size` 上下文。
- 主脚本用 `SkipBatchSampler(skip=start_step)` + `iters=len(loader)+skip` 把「跳过已训练 batch」与「学习率分母不变」统一成同一段装配代码；wandb 靠 `wandb_id + resume='must'` 续接同一个 run。

## 7. 下一步学习建议

本讲把「单机/多机训练现场如何存与续」讲透了，但还没有正面讲多卡训练本身的**并行与混合精度**机制。建议：

1. **下一讲 [u4-l3 DDP 分布式、混合精度与梯度累积](u4-l3-ddp-and-amp.md)**：本讲反复出现的 `DistributedDataParallel` 包装、`autocast` 混合精度、`GradScaler`、梯度累积、`torchrun --nproc_per_node`，会在那里被完整拆解，与本讲形成「续训存读 ↔ 并行训练」的闭环。
2. **回头深读源码**：用 `Grep` 搜 `lm_checkpoint` 在 `train_full_sft.py / train_dpo.py / train_lora.py / train_ppo.py` 等脚本里的调用，对比它们传入的 kwargs 差异（如 PPO 多了 `critic_model / critic_optimizer`），体会「同一套续训机制如何适配不同算法」。
3. **进阶思考**：尝试为本讲末尾提到的「严格数据顺序不能复原」设计一个改进思路（例如把 `DistributedSampler` 的 epoch 与已消费的绝对样本数一起存盘），作为理解工业级训练框架（如 HuggingFace Accelerate、DeepSpeed）恢复语义的跳板。
