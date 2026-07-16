# 回调、Hub 版本推送与实验追踪

## 1. 本讲目标

本讲聚焦 open-r1 训练流水线的「周边设施」：训练过程中如何**在每一次存 checkpoint 时把模型推到 Hugging Face Hub 的独立分支**，以及如何**把实验指标接到 Weights & Biases（W&B）做实时追踪**。

学完后你应当能够：

- 说清「回调（callback）」「Hub revision/分支」「异步上传（Future）」「W&B 环境变量接线」这几个概念的含义。
- 读懂 `PushToHubRevisionCallback.on_save` 的触发逻辑，并解释为什么它要用 `DummyConfig` 绕开分布式状态。
- 拆解 `push_to_hub_revision` 的三步动作：`create_repo` → `create_branch` → `upload_folder(run_as_future=True)`。
- 理解 `init_wandb_training` 如何通过设置环境变量让 Trainer 自动上报指标。
- 自己写一个最小的 `TrainerCallback` 子类，并把它注册进 `CALLBACKS` 让 `get_callbacks` 能选中。

## 2. 前置知识

- **TrainerCallback**：transformers 提供的训练生命周期钩子基类。它在训练的各个时刻（`on_train_begin`、`on_step_end`、`on_save`、`on_train_end` …）提供回调入口，子类覆写其中某个方法即可插入自定义行为，而不必改动 Trainer 本体。
- **Hub repo 与 revision**：Hugging Face Hub 上的一个模型仓库（repo）类似一个 Git 仓库。**revision** 就是它的一个分支（branch）或标签。默认分支通常是 `main`。open-r1 的做法是给**每一个 checkpoint** 单独建一个分支，例如 `main-step-000000100`，这样可以独立评估、回溯每一个中间检查点。
- **`global_step`**：训练器内部计数器，每完成一次（含梯度累积的）优化器更新就 +1。checkpoint 目录名 `checkpoint-{global_step}` 与它一致，是定位「训练到哪了」的全局坐标。
- **`Future`（`concurrent.futures.Future`）**：一个「异步结果的占位符」。`run_as_future=True` 的上传调用会立即返回一个 `Future`，真正的网络上传在后台线程进行；你可以继续干活，之后通过 `Future` 取结果或挂一个「完成回调」。
- **W&B（Weights & Biases）**：常用的实验追踪平台。transformers/trl 集成它会读取一组 `WANDB_*` 环境变量来自动配置 run。
- **注册表模式（registry pattern）**：用一个字典把「字符串名」映射到「类/函数」。在 u3-l2 奖励函数、u5-l2 代码 Provider 里你已经见过同款套路，本讲的 `CALLBACKS` 是它的又一次复用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/utils/callbacks.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py) | 定义 `PushToHubRevisionCallback`、`CALLBACKS` 注册表与 `get_callbacks` 工厂；是「何时触发、触发谁」的总控。 |
| [src/open_r1/utils/hub.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py) | 定义 `push_to_hub_revision`（真正干活的 Hub 上传函数）与 `check_hub_revision_exists` 预检；本讲的核心动作层。 |
| [src/open_r1/utils/wandb_logging.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/wandb_logging.py) | 定义 `init_wandb_training`，用环境变量把 W&B 配置注入进程。 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | `SFTConfig` / `GRPOConfig` 上挂载的 `callbacks`、`benchmarks`、`hub_model_revision`、`wandb_*` 等字段来源。 |
| [src/open_r1/sft.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py) / [src/open_r1/grpo.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py) | 在 `main()` 里调用 `get_callbacks(...)` 和 `init_wandb_training(...)`，把本讲设施接进训练器。 |

一句话关系：**「何时触发」在 `callbacks.py`，「具体动作」在 `hub.py`，「实验追踪」在 `wandb_logging.py`，三者由 `sft.py`/`grpo.py` 的 `main()` 装配进 Trainer。**

## 4. 核心概念与源码讲解

### 4.1 回调注册表与 get_callbacks

#### 4.1.1 概念说明

回顾 u2-l1：`SFTTrainer` / `GRPOTrainer` 都接受一个 `callbacks=` 参数。open-r1 不直接传回调对象，而是传**字符串名**，再用一个注册表把字符串翻译成回调类。这样做的好处和奖励注册表（u3-l2）、代码 Provider 工厂（u5-l2）一致：**YAML 配方里只写可读的字符串名，具体实现可在代码侧自由替换**，配方与实现解耦。

这条链路是：

```
YAML: callbacks: [push_to_hub_revision]
        │
        ▼  TrlParser 解析
train_config.callbacks == ["push_to_hub_revision"]   # list[str]
        │
        ▼  get_callbacks(train_config, model_config)
CALLBACKS["push_to_hub_revision"]  →  PushToHubRevisionCallback 类
        │
        ▼  实例化（注入 model_config）
[PushToHubRevisionCallback(model_config)]            # List[TrainerCallback]
        │
        ▼  交给 SFTTrainer / GRPOTrainer
trainer 在每个生命周期点调用对应 on_xxx
```

#### 4.1.2 核心流程

`get_callbacks` 的执行步骤：

1. 遍历 `train_config.callbacks`（一个字符串列表，来自 YAML 的 `callbacks:` 字段）。
2. 每个名字去 `CALLBACKS` 字典里查；查不到就抛 `ValueError`，给出明确报错。
3. 查到则用 `CALLBACKS[name](model_config)` 实例化——注意所有回调类的 `__init__` 都必须接受 `model_config` 这个位置参数。
4. 把实例收集进列表返回，交给 Trainer。

#### 4.1.3 源码精读

注册表本身极简，只有一个条目——把字符串 `"push_to_hub_revision"` 绑到 `PushToHubRevisionCallback` 类：

字符串名 → 回调类（[src/open_r1/utils/callbacks.py:80-82](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L80-L82)）

```python
CALLBACKS = {
    "push_to_hub_revision": PushToHubRevisionCallback,
}
```

工厂函数遍历配置、查表、实例化（[src/open_r1/utils/callbacks.py:85-92](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L85-L92)）

```python
def get_callbacks(train_config, model_config) -> List[TrainerCallback]:
    callbacks = []
    for callback_name in train_config.callbacks:
        if callback_name not in CALLBACKS:
            raise ValueError(f"Callback {callback_name} not found in CALLBACKS.")
        callbacks.append(CALLBACKS[callback_name](model_config))
    return callbacks
```

接线点在 `main()` 里，SFT 与 GRPO 完全一致（[src/open_r1/sft.py:108](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L108)、[src/open_r1/grpo.py:119](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L119)）：

```python
callbacks=get_callbacks(training_args, model_args),
```

而 `callbacks:` 字段是 open-r1 在 trl 基类之上新增的（[src/open_r1/configs.py:179-182](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L179-L182) 处的 `SFTConfig`，`GRPOConfig` 在 [L134-137](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L134-L137) 同理），默认空列表：

```python
callbacks: list[str] = field(
    default_factory=lambda: [],
    metadata={"help": "The callbacks to run during training."},
)
```

一个真实配方示例——Codeforces GRPO 同时注册了回调与训练后基准（[recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml:16-19](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L16-L19)）：

```yaml
callbacks:
- push_to_hub_revision
benchmarks:
- lcb_v4
```

#### 4.1.4 代码实践

**目标**：在不改源码的前提下，验证 `get_callbacks` 的查表 + 实例化行为。

```python
# 示例代码：探查注册表与工厂行为
from open_r1.utils.callbacks import CALLBACKS, get_callbacks

# 1. 看注册表里有哪些回调
print(list(CALLBACKS.keys()))  # 期望: ['push_to_hub_revision']

# 2. 构造一个只含 callbacks 字段的假 train_config
class FakeTrainConfig:
    callbacks = ["push_to_hub_revision"]

cbs = get_callbacks(FakeTrainConfig(), model_config=None)
print(len(cbs), type(cbs[0]).__name__)  # 期望: 1 PushToHubRevisionCallback

# 3. 故意写一个不存在的名字，观察报错
class BadTrainConfig:
    callbacks = ["does_not_exist"]

try:
    get_callbacks(BadTrainConfig(), model_config=None)
except ValueError as e:
    print("ValueError:", e)  # 期望: Callback does_not_exist not found in CALLBACKS.
```

**操作步骤**：在装好 open-r1 的环境里 `python -c "..."` 或写入脚本运行。

**观察现象与预期结果**：第 1 步打印注册表 key；第 2 步得到长度 1 的回调列表；第 3 步抛出 `ValueError` 并提示未找到。若机器未装 open-r1，可临时 `export PYTHONPATH=src` 后在仓库根目录运行。无法运行时标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 open-r1 要在 YAML 里写 `push_to_hub_revision` 字符串，而不是直接在 `sft.py` 里 `callbacks=[PushToHubRevisionCallback(...)]`？

> **答案**：为了把「用哪些回调」这个选择权交给配方（YAML/命令行），让代码侧的实现与配置侧的开关解耦；换回调只改配方，不动代码，与奖励、Provider 的注册表模式保持一致。

**练习 2**：若你在 YAML 里写了 `callbacks: [my_callback]` 但忘了把 `MyCallback` 注册进 `CALLBACKS`，会发生什么？

> **答案**：`get_callbacks` 在遍历时发现 `"my_callback"` 不在 `CALLBACKS`，抛 `ValueError("Callback my_callback not found in CALLBACKS.")`，训练在组装 Trainer 前就失败。

---

### 4.2 PushToHubRevisionCallback.on_save 与 DummyConfig 绕过

#### 4.2.1 概念说明

`PushToHubRevisionCallback` 是注册表里目前唯一的回调，它解决一个具体需求：**训练过程中每存一个 checkpoint，就把它作为独立分支推到 Hub**，从而可以按 step 回溯、按 step 评估。这有别于 transformers 自带的 `push_to_hub`（训练结束时推一次到 `main`，见 [src/open_r1/sft.py:161-163](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L161-L163)）——后者只给你「最终态」，前者给你「全过程时间线」。

它覆写的是 `on_save`：transformers 在每次把 checkpoint 写到磁盘后会调用这个钩子。

#### 4.2.2 核心流程

`on_save` 的执行步骤（[src/open_r1/utils/callbacks.py:47-77](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L47-L77)）：

1. **只在 0 号进程执行**：用 `state.is_world_process_zero` 守卫，避免分布式训练时每个 rank 都去推一遍。
2. **构造 per-step 分支名**：`f"{args.hub_model_revision}-step-{global_step:09d}"`，9 位零填充保证分支名按字典序就是按步数序（如 `main-step-000000100`）。
3. **用 `DummyConfig` 打包参数**：因为不能直接复制/新建 `SFTConfig`（会破坏分布式状态，见下文），用一个轻量对象只携带上传所需的字段。
4. **调 `push_to_hub_revision(dummy_config, extra_ignore_patterns=["*.pt"])`**：异步上传，返回 `Future`；`*.pt` 用来排除优化器状态文件。
5. **可选：上传完成后跑基准**：若检测到 Slurm 集群，给 `Future` 挂一个完成回调，在该 checkpoint 推送成功后立即对它跑 `benchmarks` 评估。

#### 4.2.3 源码精读

完整回调类（[src/open_r1/utils/callbacks.py:43-77](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L43-L77)），关键部分如下：

```python
class PushToHubRevisionCallback(TrainerCallback):
    def __init__(self, model_config) -> None:
        self.model_config = model_config

    def on_save(self, args, state, control, **kwargs):
        if state.is_world_process_zero:
            global_step = state.global_step
            # WARNING: if you use dataclasses.replace(args, ...) the accelerator dist state
            # will be broken, so I do this workaround
            dummy_config = DummyConfig(
                hub_model_id=args.hub_model_id,
                hub_model_revision=f"{args.hub_model_revision}-step-{global_step:09d}",
                output_dir=f"{args.output_dir}/checkpoint-{global_step}",
                system_prompt=args.system_prompt,
            )
            future = push_to_hub_revision(dummy_config, extra_ignore_patterns=["*.pt"])
            if is_slurm_available():
                dummy_config.benchmarks = args.benchmarks

                def run_benchmark_callback(_):
                    print(f"Checkpoint {global_step} pushed to hub.")
                    run_benchmark_jobs(dummy_config, self.model_config)

                future.add_done_callback(run_benchmark_callback)
```

**为什么要 `DummyConfig` 这个「绕过」？** 这是本讲最微妙的设计点。源码注释点明了原因（[L57-58](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L57-L58)）：如果用 `dataclasses.replace(args, ...)` 复制一份训练配置，或 `new SFTConfig(...)` 新建一个，会**破坏 accelerate 的分布式状态**——因为这些配置对象在 Trainer 内部与进程组、device mesh 等运行时状态纠缠在一起，复制/重建会丢失或错乱这些句柄。

解决思路是：上传函数 `push_to_hub_revision` 其实只用到配置里的 4 个纯数据字段（`hub_model_id`、`hub_model_revision`、`output_dir`、`system_prompt`）。于是定义一个**不带任何分布式语义**的「哑配置」对象，只把这 4 个字段 setattr 上去，绕开所有配置重建的副作用。`DummyConfig` 的实现就是一个通用的属性兜底容器（[src/open_r1/utils/callbacks.py:37-40](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L37-L40)）：

```python
class DummyConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
```

**Slurm 检测与基准回调**：`is_slurm_available()` 通过尝试运行 `sinfo` 命令判断当前是否在 Slurm 集群里（[L28-34](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L28-L34)）。只有集群环境下，才把 `benchmarks` 字段补到 `dummy_config` 上，并给上传的 `Future` 挂一个「完成后跑 lighteval 基准」的回调（`run_benchmark_jobs`，定义于 [src/open_r1/utils/evaluation.py:106](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L106)，详见 u8-l1）。`Future.add_done_callback(fn)` 会在后台上传结束时调用 `fn(future)`，这里的 `_` 就是这个 future。

> 设计要点：上传用 `run_as_future=True`（见 4.3）异步进行，所以训练不会被网络 I/O 阻塞；而「评估刚推上去的 checkpoint」被链在「上传完成」之后，时序上保证评估的是已上传的版本。

#### 4.2.4 代码实践

**目标**：验证 `on_save` 的触发与 0 号进程守卫，并体会「分支名按步数生成」。

```python
# 示例代码：手动触发 on_save，观察分支名生成逻辑（不真正上传）
from transformers import TrainerState, TrainerControl
from open_r1.utils.callbacks import PushToHubRevisionCallback

cb = PushToHubRevisionCallback(model_config=None)

# 构造一个假 args，提供 on_save 内部读取的字段
class FakeArgs:
    hub_model_id = "user/open-r1-test"
    hub_model_revision = "main"
    output_dir = "/tmp/run"
    system_prompt = None
    benchmarks = []

state = TrainerState()
state.is_world_process_zero = True

for step in (1, 100, 1000):
    state.global_step = step
    # 真正上传会联网，这里只演示「分支名如何生成」——直接复现其格式串：
    revision = f"{FakeArgs.hub_model_revision}-step-{state.global_step:09d}"
    print(step, "->", revision)
```

**观察现象与预期结果**：

```
1 -> main-step-000000001
100 -> main-step-000000100
1000 -> main-step-000001000
```

字典序排列这些分支名，正好等于按步数升序——这正是 9 位零填充的目的。若把 `state.is_world_process_zero` 改为 `False`，则真实 `on_save` 内部什么都不会做（被守卫挡掉）。

**说明**：真正调用 `cb.on_save(...)` 会触发 Hub 上传，需要联网与写权限；本实践只复现分支名格式串以避免副作用，**完整上传效果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `f"{...}-step-{global_step:09d}"` 里的 `:09d` 改成普通的 `{global_step}`，会有什么隐患？

> **答案**：分支名会变成 `main-step-1`、`main-step-10`、`main-step-100`，字典序下 `main-step-10` 排在 `main-step-2` 之前，导致 Hub 上按名排序时步数顺序错乱，难以按时间线回溯。零填充保证字典序 = 步数序。

**练习 2**：为什么 `on_save` 里要用 `state.is_world_process_zero` 而不是「每个进程都推」？

> **答案**：分布式训练中所有 rank 共享同一个 Hub 仓库，若每个进程都上传会重复写、互相覆盖甚至引发冲突；只在 0 号进程推一次即可，其他 rank 的 checkpoint 内容相同。

**练习 3**：`DummyConfig` 为什么不能直接用 `dataclasses.replace(args, hub_model_revision=...)` 替代？

> **答案**：注释明确说明 `dataclasses.replace`（或新建一个 `SFTConfig`）会破坏 accelerator 的分布式状态——这些配置对象与运行时的进程组/设备状态纠缠。`DummyConfig` 是一个不带分布式语义的纯属性容器，只携带上传所需的几个字段，从而绕开副作用。

---

### 4.3 push_to_hub_revision：仓库/分支创建与异步上传

#### 4.3.1 概念说明

`push_to_hub_revision` 是 `on_save` 调用的「动作层」，位于 [src/open_r1/utils/hub.py:39-67](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L39-L67)。它把「本地 `output_dir` 里的 checkpoint」上传到 Hub 上**指定 repo 的指定分支**。它接收一个形如 `SFTConfig | GRPOConfig` 的对象（实际由 `DummyConfig` 扮演），返回一个 `Future`。

它依赖 `huggingface_hub` 库的几个原语：`create_repo`（建仓库）、`create_branch`（建分支）、`upload_folder`（传文件夹）。

#### 4.3.2 核心流程

```
push_to_hub_revision(training_args)
        │
        ├─ create_repo(repo_id=hub_model_id, private=True, exist_ok=True)
        │       仓库不存在则（私有）创建；已存在则跳过
        │
        ├─ initial_commit = list_repo_commits(repo_id)[-1]
        │       取仓库最新一次提交，作为新分支的起点
        │
        ├─ create_branch(repo_id, branch=hub_model_revision,
        │                revision=initial_commit.commit_id, exist_ok=True)
        │       从该提交创建本次 checkpoint 的分支；已存在则跳过
        │
        ├─ ignore_patterns = ["checkpoint-*", "*.pth"] + extra_ignore_patterns
        │       排除嵌套 checkpoint 目录与旧权重格式
        │
        └─ upload_folder(repo_id, folder_path=output_dir,
                         revision=hub_model_revision,
                         run_as_future=True)   ← 后台异步上传，立即返回 Future
```

`return future`

#### 4.3.3 源码精读

函数体（[src/open_r1/utils/hub.py:39-67](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L39-L67)）：

```python
def push_to_hub_revision(training_args, extra_ignore_patterns=[]) -> Future:
    """Pushes the model to branch on a Hub repo."""
    repo_url = create_repo(repo_id=training_args.hub_model_id, private=True, exist_ok=True)
    initial_commit = list_repo_commits(training_args.hub_model_id)[-1]
    create_branch(
        repo_id=training_args.hub_model_id,
        branch=training_args.hub_model_revision,
        revision=initial_commit.commit_id,
        exist_ok=True,
    )
    ...
    ignore_patterns = ["checkpoint-*", "*.pth"]
    ignore_patterns.extend(extra_ignore_patterns)
    future = upload_folder(
        repo_id=training_args.hub_model_id,
        folder_path=training_args.output_dir,
        revision=training_args.hub_model_revision,
        commit_message=f"Add {training_args.hub_model_revision} checkpoint",
        ignore_patterns=ignore_patterns,
        run_as_future=True,
    )
    return future
```

几个关键细节：

- **`exist_ok=True`**：`create_repo` 与 `create_branch` 都带它，意味着「已存在不报错」。这让每次 `on_save` 都能幂等调用——同一个分支第一次建出来，之后重复触发也不会因「分支已存在」而崩。
- **分支起点**：用 `list_repo_commits(repo_id)[-1]` 取仓库最新提交作为新分支的基底。第一次推模型时，`create_repo` 会产生一个初始提交；之后每个 checkpoint 分支都从当时的最新提交岔开。
- **忽略模式**：基础 `["checkpoint-*", "*.pth"]` 排除嵌套 checkpoint 子目录和 `.pth` 权重；回调侧又追加了 `["*.pt"]`（[callbacks.py:66-68](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L66-L68)），合起来排除所有优化器/训练状态文件，只推模型权重与配置。这避免了把巨大的 `optimizer.pt` 之类的文件塞满 Hub。
- **`run_as_future=True`**：`upload_folder` 不阻塞当前线程，而是把上传丢到后台线程，立即返回 `concurrent.futures.Future`（这也是返回类型标注 `-> Future` 的由来，对应 [hub.py:19](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L19) 的 `from concurrent.futures import Future`）。这就是 4.2 里「挂完成回调」能成立的基础。

**关于一个相关的预检函数**：[hub.py:70-86](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L70-L86) 还定义了 `check_hub_revision_exists`，用于在推送前检查「仓库/分支是否已存在、是否已有 README」，若已存在且未开 `overwrite_hub_revision` 则抛错以防覆盖。**注意：经全仓库检索，该函数目前在 `sft.py` / `grpo.py` 等 main 流程中并未被调用**（仓库内未发现调用点），属于预留的预检工具。如需启用「不覆盖已存在分支」的保护，需要使用者自行在入口处调用它。

> 提示：`hub.py` 还包含 `get_param_count_from_repo_id` 与 `get_gpu_count_for_vllm`（[L89-132](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L89-L132)），用于按模型大小推算 vLLM 所需 GPU 数，属于评估/服务侧工具，本讲不展开。

#### 4.3.4 代码实践

**目标**：在本地准备一个「假 checkpoint 目录」，调用 `push_to_hub_revision` 真正上传到一个**你自己的私有测试仓库**的某个分支，验证三步动作。

```python
# 示例代码：真实上传一个假 checkpoint 到你自己的 Hub 仓库（需联网 + 写权限）
import os, json
from open_r1.utils.callbacks import DummyConfig
from open_r1.utils.hub import push_to_hub_revision

os.makedirs("/tmp/fake_ckpt", exist_ok=True)
# 放一个 README 和一个占位权重，让分支有内容
with open("/tmp/fake_ckpt/README.md", "w") as f:
    f.write("# fake checkpoint\n")
with open("/tmp/fake_ckpt/model.safetensors", "w") as f:
    f.write("not a real weight, only for tutorial")

cfg = DummyConfig(
    hub_model_id="<你的用户名>/open-r1-tutorial-test",  # 必须改成你自己的 repo id
    hub_model_revision="main-step-000000042",
    output_dir="/tmp/fake_ckpt",
    system_prompt=None,
)

future = push_to_hub_revision(cfg)  # 立即返回 Future，上传在后台进行
future.result(timeout=120)          # 阻塞等待上传完成（或抛出错误）
print("done; 分支:", cfg.hub_model_revision)
```

**操作步骤**：

1. 先 `huggingface-cli login`（或设置 `HF_TOKEN`）。
2. 把 `hub_model_id` 改成你账号下的仓库 id（不存在会被 `create_repo` 自动私有创建）。
3. 运行脚本。
4. 到 Hub 网页查看该仓库的 **Branches**，应能看到 `main-step-000000042` 分支，里面有 `README.md` 与 `model.safetensors`。

**观察现象与预期结果**：分支被创建并包含两个文件；再次运行同一脚本不会报错（`exist_ok=True` 生效）。若无 Hub 写权限或断网，此实践无法完成，标注「待本地验证」。**严禁**向 `open-r1` 官方仓库或他人仓库写——只用自己的测试仓库。

#### 4.3.5 小练习与答案

**练习 1**：`create_repo` 和 `create_branch` 都设了 `exist_ok=True`，为什么这对 `on_save` 反复触发很重要？

> **答案**：`on_save` 在训练中会被触发多次（每次 checkpoint 一次），且不同 run 可能复用同一 `hub_model_id`。`exist_ok=True` 保证仓库/分支已存在时不报错，使每次推送都幂等，不会因「已存在」中途崩溃。

**练习 2**：为什么默认要 ignore 掉 `*.pth`、`*.pt`、`checkpoint-*`？

> **答案**：这些是优化器状态、训练状态和嵌套 checkpoint 目录，体积巨大且对「按 step 评估某个 checkpoint」无用。上传它们会塞满 Hub 配额、拖慢上传；只需保留权重与配置即可回溯与评估。

**练习 3**：`run_as_future=True` 让 `upload_folder` 立即返回。若没有这个机制、改为同步上传，会对训练造成什么影响？

> **答案**：同步上传是网络 I/O 密集型、耗时长，会让 0 号进程在每次 `on_save` 时阻塞，等于训练在存 checkpoint 后「卡住」等网络。异步上传把 I/O 移到后台线程，训练几乎不受影响；需要善后时再通过 `Future` 挂回调（如跑基准）。

---

### 4.4 init_wandb_training：用环境变量接线 W&B

#### 4.4.1 概念说明

`init_wandb_training` 位于 [src/open_r1/utils/wandb_logging.py:4-13](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/wandb_logging.py#L4-L13)，作用是把训练配置里的 W&B 字段写进**进程环境变量**。为什么不直接调 `wandb.init(...)`？因为 transformers/trl 的 Trainer 已经内置了 W&B 集成：当 `report_to` 含 `wandb` 时，Trainer 会自动初始化 run，并优先读取 `WANDB_ENTITY` / `WANDB_PROJECT` / `WANDB_RUN_GROUP` 等环境变量。open-r1 只要在 Trainer 启动前把这些变量设好，后续的指标上报、run 归组就自动生效——无需自己手写 `wandb.init`。

#### 4.4.2 核心流程

```
main() 检测到 "wandb" in training_args.report_to
        │
        ▼
init_wandb_training(training_args)
        │
        ├─ 若 wandb_entity   非空 → os.environ["WANDB_ENTITY"]   = ...
        ├─ 若 wandb_project  非空 → os.environ["WANDB_PROJECT"]  = ...
        └─ 若 wandb_run_group 非空 → os.environ["WANDB_RUN_GROUP"] = ...
        │
        ▼
Trainer 启动，内置集成读取这些 env var，自动创建/归组 run
```

三个字段都来自 open-r1 自定义配置：`GRPOConfig` 的 [configs.py:155-166](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L155-L166)、`SFTConfig` 的 [configs.py:194-205](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L194-L205)。此外 `GRPOConfig` 还多一个 `wandb_log_unique_prompts`（[configs.py:149-154](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L149-L154)），用于按唯一 prompt 分 run，由 trl 的 GRPOTrainer 自身消费，不经过这个函数。

#### 4.4.3 源码精读

函数体非常短（[src/open_r1/utils/wandb_logging.py:4-13](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/wandb_logging.py#L4-L13)）：

```python
def init_wandb_training(training_args):
    """Helper function for setting up Weights & Biases logging tools."""
    if training_args.wandb_entity is not None:
        os.environ["WANDB_ENTITY"] = training_args.wandb_entity
    if training_args.wandb_project is not None:
        os.environ["WANDB_PROJECT"] = training_args.wandb_project
    if training_args.wandb_run_group is not None:
        os.environ["WANDB_RUN_GROUP"] = training_args.wandb_run_group
```

三个要点：

- **只设、不 init**：函数不调用任何 wandb API，只写 `os.environ`。真正的 run 创建交给 Trainer 内置集成，职责清晰。
- **「非空才覆盖」**：每个字段都先判 `is not None`，这意味着用户没配就不动对应环境变量，允许通过 shell 里预置 `WANDB_PROJECT` 等方式配置，不会被空值覆盖成空。
- **调用点守卫**：在 `main()` 里只在 `"wandb" in training_args.report_to` 时才调用（[src/open_r1/sft.py:84-85](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L84-L85)、[src/open_r1/grpo.py:70-71](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L70-L71)）：

```python
if "wandb" in training_args.report_to:
    init_wandb_training(training_args)
```

即：要启用 W&B，需在配方里设 `report_to: [wandb]`（transformers 字段）并提供 `wandb_project` 等。`report_to` 默认不含 wandb，所以这是 opt-in。

> 三者的含义：`WANDB_ENTITY`（属于哪个团队/账号）、`WANDB_PROJECT`（归到哪个项目）、`WANDB_RUN_GROUP`（把多次 run——如不同 seed、不同 checkpoint——归到一组便于对比）。同一 `wandb_run_group` 下的多个 run 在 W&B 面板里会聚成一组。

#### 4.4.4 代码实践

**目标**：验证 `init_wandb_training` 把配置字段正确翻译成环境变量，且「未配置不覆盖」。

```python
# 示例代码：验证环境变量接线逻辑（不联网、不依赖 wandb）
import os
from open_r1.utils.wandb_logging import init_wandb_training

class FakeArgs:
    wandb_entity = "my-team"
    wandb_project = "open-r1-debug"
    wandb_run_group = None  # 故意不配 group

# 清场，确保观察的是函数本身的效果
for k in ("WANDB_ENTITY", "WANDB_PROJECT", "WANDB_RUN_GROUP"):
    os.environ.pop(k, None)

init_wandb_training(FakeArgs())
print(os.environ.get("WANDB_ENTITY"))   # 期望: my-team
print(os.environ.get("WANDB_PROJECT"))  # 期望: open-r1-debug
print(os.environ.get("WANDB_RUN_GROUP"))  # 期望: None（未配置则不设置）
```

**观察现象与预期结果**：`WANDB_ENTITY`、`WANDB_PROJECT` 被设上；`WANDB_RUN_GROUP` 因配置为 `None` 而保持不存在（`os.environ.get` 返回 `None`）。把 `wandb_run_group` 改成字符串后应能看到对应变量被设上。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `init_wandb_training` 只写环境变量、不调用 `wandb.init`？

> **答案**：transformers/trl 的 Trainer 已内置 W&B 集成，会自动 `init` 并读取 `WANDB_*` 环境变量。自己再 `init` 一次会与 Trainer 的集成冲突/重复。只设环境变量是把「配置」与「集成」职责分离的最干净做法。

**练习 2**：用户在 shell 里 `export WANDB_PROJECT=foo`，但 YAML 里 `wandb_project` 留空，运行后 `WANDB_PROJECT` 是什么？

> **答案**：仍是 `foo`。因为函数对每个字段都判 `is not None` 才覆盖，`wandb_project` 为空时不会把环境变量改成空，保留了 shell 里的预置值——这正是「未配置不覆盖」的好处。

**练习 3**：要让一次训练真正把指标上报到 W&B，至少要在配方里设哪两个字段？

> **答案**：至少要 `report_to: [wandb]`（触发调用，且让 Trainer 开启集成）和 `wandb_project`（决定上报到哪个项目）；`wandb_entity` / `wandb_run_group` 视需要而定。当然还需本机 `wandb` 登录（`WANDB_API_KEY` 或 `wandb login`）。

---

## 5. 综合实践

把本讲三个设施串起来：**实现一个自定义回调，在每次 `on_save` 时打印 `global_step`，并把它注册进 `CALLBACKS`，用 `get_callbacks` 选中它，最后用一个最小测试验证 `on_save` 被正确触发**。这个任务综合了「注册表机制（4.1）」「`on_save` 生命周期钩子（4.2）」与「fake state/control 的测试手法（4.2.4）」。

### 步骤 1：写回调骨架

```python
# open_r1_tutorial_callback.py （示例代码：自定义回调，可放在任意可 import 的位置）
from transformers import TrainerCallback, TrainerState, TrainerControl


class PrintGlobalStepCallback(TrainerCallback):
    """每次存 checkpoint 时，在 0 号进程打印当前 global_step。"""

    def __init__(self, model_config) -> None:
        # 与 PushToHubRevisionCallback 保持一致的构造签名，
        # 这样 get_callbacks 用 CALLBACKS[name](model_config) 实例化时不会报错。
        self.model_config = model_config

    def on_save(self, args, state: TrainerState, control: TrainerControl, **kwargs):
        if state.is_world_process_zero:
            print(f"[PrintGlobalStepCallback] saved checkpoint at global_step={state.global_step}")
        return control
```

### 步骤 2：注册进 `CALLBACKS`

不动 open-r1 源码，直接在运行时给包级字典加一项：

```python
from open_r1.utils.callbacks import CALLBACKS, get_callbacks
from open_r1_tutorial_callback import PrintGlobalStepCallback

CALLBACKS["print_global_step"] = PrintGlobalStepCallback  # 注册
```

### 步骤 3：写最小测试

```python
# test_print_global_step.py （示例代码：单元测试）
from transformers import TrainerState, TrainerControl
from open_r1.utils.callbacks import CALLBACKS, get_callbacks
from open_r1_tutorial_callback import PrintGlobalStepCallback


def test_print_global_step_registered_and_triggered(capsys):
    # 1. 注册（若已注册则幂等）
    CALLBACKS["print_global_step"] = PrintGlobalStepCallback

    # 2. 假 train_config，声明要用的回调名
    class FakeTrainConfig:
        callbacks = ["print_global_step"]

    # 3. 工厂选中并实例化
    cbs = get_callbacks(FakeTrainConfig(), model_config=None)
    assert len(cbs) == 1
    assert isinstance(cbs[0], PrintGlobalStepCallback)

    # 4. 手动触发 on_save，验证打印内容
    state = TrainerState()
    state.global_step = 42
    state.is_world_process_zero = True
    cbs[0].on_save(args=None, state=state, control=TrainerControl())

    out = capsys.readouterr().out
    assert "global_step=42" in out

    # 5. 非 0 号进程应不打印
    state.is_world_process_zero = False
    cbs[0].on_save(args=None, state=state, control=TrainerControl())
    assert capsys.readouterr().out == ""
```

### 步骤 4：运行与观察

- `pytest test_print_global_step.py -q` 应全绿。
- 预期断言：`get_callbacks` 返回长度 1、类型正确；0 号进程打印 `global_step=42`；非 0 号进程不打印。
- 若想进一步贴近真实：在 `FakeTrainConfig` 里同时放 `["push_to_hub_revision", "print_global_step"]`，验证两个回调都被实例化（但**不要**真触发 `PushToHubRevisionCallback.on_save`，那会联网上传）。

**说明**：通过 `CALLBACKS["..."] = ...` 在运行时往包级字典注入是教学用的便捷做法；生产中更干净的方式是直接在 [callbacks.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py) 的 `CALLBACKS` 字典里加一行，使其随包一起导出。完整联网/真实训练效果待本地验证。

## 6. 本讲小结

- open-r1 用**注册表模式**把 YAML 里的回调字符串名翻译成回调类：`CALLBACKS` 字典 + `get_callbacks(train_config, model_config)` 工厂，与奖励（u3-l2）、Provider（u5-l2）同构。
- `PushToHubRevisionCallback.on_save` 在**每次存 checkpoint 且仅 0 号进程**时触发，按 `f"{revision}-step-{global_step:09d}"` 生成 per-step 分支名（9 位零填充保证字典序 = 步数序）。
- 它用 **`DummyConfig`** 这种轻量属性容器打包上传所需字段，刻意避开 `dataclasses.replace` / 新建 `SFTConfig`，因为那会破坏 accelerate 的分布式状态——这是本讲最关键的设计细节。
- `push_to_hub_revision` 三步走：`create_repo` → `create_branch`（均 `exist_ok=True`）→ `upload_folder(run_as_future=True)` 异步上传并返回 `Future`；通过 ignore 掉 `*.pt/*.pth/checkpoint-*` 只推权重不推优化器。
- 在 Slurm 集群里，回调还会给上传 `Future` 挂一个「完成后跑 lighteval 基准」的完成回调，实现「边训边评估每个 checkpoint」。
- `init_wandb_training` 只做一件事：把 `wandb_entity/project/run_group` 写进 `WANDB_*` 环境变量（非空才写），交给 Trainer 内置集成去自动上报，职责与集成分离。

## 7. 下一步学习建议

- **接续 u8-l1（LightEval 基准评估）**：本讲多次提到 `run_benchmark_jobs` / `run_lighteval_job`，下一讲会拆解基准任务如何注册、如何按模型大小选 GPU 数与 tensor_parallel，与本讲的「上传后自动评估」闭环。
- **回顾 u7-l1（Slurm 训练）**：理解 `is_slurm_available()` 分支为何只集群环境触发基准评估，以及 `hub_model_revision` 在多机训练产物中的角色。
- **动手扩展**：试着把综合实践里的 `PrintGlobalStepCallback` 改造成「`on_log` 时把 loss 写入一个本地 jsonl」，体会 `TrainerCallback` 不同钩子（`on_save` / `on_log` / `on_step_end`）的区别。
- **源码延伸阅读**：通读 [src/open_r1/utils/hub.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py) 末尾的 `get_param_count_from_repo_id` / `get_gpu_count_for_vllm`，看 Hub 元数据如何反哺 vLLM 的 GPU 规划。
