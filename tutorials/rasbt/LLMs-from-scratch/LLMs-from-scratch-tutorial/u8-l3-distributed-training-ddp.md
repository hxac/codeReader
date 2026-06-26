# 多卡分布式训练（DDP）

## 1. 本讲目标

本讲聚焦一个工程问题：**当单卡训练太慢时，如何用多张 GPU 协同训练同一个模型**。

学完后你应该能够：

1. 说出数据并行（data parallel）与模型并行（model parallel）的区别，并解释为什么 DDP 属于数据并行。
2. 理解 **rank / world_size / local_rank** 三个进程标识的含义，以及进程组（process group）的作用。
3. 看懂 `ddp_setup`、`init_process_group`、`DistributedSampler`、`DDP(...)` 包装器这一组新组件各自负责什么。
4. 用 `mp.spawn` 或 `torchrun` 两种方式在 2 个进程上跑通本仓库的 DDP 示例脚本，并能解释各进程打印的损失为什么“看着不同但模型其实完全一致”。
5. 把这套模式迁移到本仓库第 4 章的 `GPTModel` 上。

本讲的全部内容都围绕附录 A 的一个独立脚本展开，它刻意用一个“玩具模型 + 玩具数据”来把 DDP 的三件套讲清楚——理解了它，把模型换成 124M 的 GPT 也只是改个类名。

## 2. 前置知识

本讲承接 [u8-l1 PyTorch 核心基础](u8-l1-pytorch-essentials.md)，默认你已经掌握下面这些“单卡”概念。如果你对其中任何一项陌生，建议先回看 u8-l1：

- **标准训练循环**：`model.train()` → 前向算 `loss` → `optimizer.zero_grad()` → `loss.backward()` → `optimizer.step()`。DDP 不改变这个骨架，只往里面“加料”。
- **`nn.Module` 与参数管理**：模型是一棵由子模块和 `nn.Parameter` 组成的树，`model.to(device)` 把所有参数搬到指定设备。
- **设备（device）**：PyTorch 里张量要么在 CPU 上，要么在某张 GPU 上（`cuda:0`、`cuda:1`……），参与同一个运算的张量必须在同一设备上。
- **梯度是累加的**：`backward()` 把梯度写进参数的 `.grad` 并累加，所以每步前要先 `zero_grad()` 清零（见 [u8-l2](u8-l2-training-loop-bells-whistles.md) 的梯度裁剪一节也强调了这一点）。

此外本讲用到两个纯 Python/操作系统层面的概念，先说清楚：

- **进程（process）**：操作系统里独立运行的程序实例，有自己的内存空间和 Python 解释器。DDP 的核心思路就是“每个 GPU 配一个独立的 Python 进程”，进程之间靠网络（或本机进程间通信）交换数据。注意它和“线程（thread）”不同——线程共享内存，进程不共享。
- **同步（synchronization）**：多个进程/多个卡在某一步停下来互相等、交换结果后再继续。DDP 的关键就在于“梯度同步”：所有卡各自算完梯度后，先互相求平均，再做相同的更新步。

> 一句话直觉：DDP 让 N 张卡像 N 个**完全同步的工人**，每个人处理一份数据，但每次更新前都要对齐“算出来的梯度”，于是大家手里的模型永远一模一样。

## 3. 本讲源码地图

本讲只涉及附录 A 目录下的两个脚本（外加一个 README）：

| 文件 | 作用 |
| --- | --- |
| [DDP-script.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py) | 书中正文配套脚本。用 `multiprocessing.spawn` **由脚本自己**派生进程，演示完整的多 GPU 训练流程。 |
| [DDP-script-torchrun.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script-torchrun.py) | 可选脚本。模型/数据/训练循环与上面完全相同，唯一区别是改用 `torchrun` 命令从外部启动进程，更适合多机训练。 |
| [README.md](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/README.md) | 说明 Notebook 只支持单卡，所以 DDP 必须写成 `.py` 脚本。 |

两个脚本的 `ToyDataset`、`NeuralNetwork`、`prepare_dataset`、`main`、`compute_accuracy` 完全一致，差异只在“谁来启动进程”。因此下文以 `DDP-script.py` 为主讲解源码，遇到 `torchrun` 版本的差异时单独点出。

---

## 4. 核心概念与源码讲解

### 4.1 DDP 初始化：搭好进程之间的“通信总线”

#### 4.1.1 概念说明

为什么要分布式？训练真实 LLM 时，单卡要么“显存装不下模型”，要么“一轮 epoch 要跑好几天”。解决办法分两大流派：

- **模型并行（model parallel）**：把一个模型**切开**，不同层放在不同卡上。解决“装不下”。
- **数据并行（data parallel）**：每张卡上放一份**完整**的模型副本，把每个 batch **切分**给各卡并行处理。解决“跑得慢”。

DDP（**DistributedDataParallel**）属于数据并行。它的关键性质是：**每个进程都持有一份完整的模型**，区别只在各自吃到的数据不同。

数据并行要奏效，必须解决一个问题——**怎么保证 N 份模型副本不“越长越分叉”**？答案是：每次 `step()` 之前，先把所有进程算出来的梯度做一次 **all-reduce（全收集求平均）**，让每个进程拿到**完全相同**的平均梯度，于是各自执行相同的更新后，权重依然逐位相同。这个“对齐梯度”的动作，就是 DDP 在后台替你做的事。

要能互相通信，进程们必须先“认识彼此”——这就是**初始化**要干的事。

#### 4.1.2 核心流程

`ddp_setup` 做三件事，顺序基本固定：

1. **约定碰头地址**：设置两个环境变量 `MASTER_ADDR` 和 `MASTER_PORT`，指定 `rank 0` 进程在哪个地址、哪个端口“听”。所有进程启动后都去这个地址登记，称为 **rendezvous（会合）**。
2. **初始化进程组**：调用 `init_process_group`，告诉 PyTorch 用哪种**通信后端（backend）**、自己是什么 `rank`、一共多少个进程（`world_size`）。
3. **绑定本进程用的 GPU**：`torch.cuda.set_device(rank)`，让本进程默认在编号为 `rank` 的 GPU 上分配显存。

通信后端是一个容易困惑的点，列成表：

| 后端 | 全称 | 适用场景 |
| --- | --- | --- |
| `nccl` | NVIDIA Collective Communication Library | NVIDIA GPU，**Linux 默认**，速度最快 |
| `gloo` | Facebook Collective Communication Library | CPU 训练、Windows、或没有 NCCL 的环境 |

脚本用 `platform.system()` 判断系统：Windows 上退回 `gloo`，否则用 `nccl`。

训练结束后，调用 `destroy_process_group()` 干净退出分布式模式、释放通信资源。

#### 4.1.3 源码精读

先看新增的 import，所有 DDP 能力都来自这几个模块：

[DDP-script.py:13-18](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L13-L18) —— 引入进程派生（`mp`）、分布式采样器（`DistributedSampler`）、DDP 包装器、进程组的初始化与销毁。

接着是初始化函数本体：

[DDP-script.py:23-46](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L23-L46) —— 设置会合地址，按操作系统选后端，并绑定本进程 GPU。

其中后端选择的关键片段：

```python
# DDP-script.py:36-44
if platform.system() == "Windows":
    os.environ["USE_LIBUV"] = "0"
    init_process_group(backend="gloo", rank=rank, world_size=world_size)
else:
    init_process_group(backend="nccl", rank=rank, world_size=world_size)

torch.cuda.set_device(rank)
```

注意 `torch.cuda.set_device(rank)` 这一行只在 GPU 场景下有意义——它假设 `rank` 就是 GPU 编号（单机时确实如此）。这也是为什么这个脚本**不能直接在纯 CPU 上跑**，4.3 节的实践会给出 CPU 适配方案。

收尾的清理：

[DDP-script.py:179](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L179) —— 训练全部结束后销毁进程组，避免资源泄漏。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把“单卡训练脚本”和“DDP 脚本”做 diff，看清楚 DDP 到底新增了哪几样东西。

**操作步骤**：

1. 打开 `DDP-script.py`，对照 [u8-l1](u8-l1-pytorch-essentials.md) 学过的标准训练循环，找出所有带 `# NEW` 注释的行。
2. 把这些 `# NEW` 点按职责分成三类：① 初始化通信（`ddp_setup`）；② 切分数据（`DistributedSampler`）；③ 包装模型 + 设备绑定（`DDP(...)`、`to(rank)`）。
3. 试着回答：如果删掉 `init_process_group` 这一行，后面 `DDP(model)` 还能正常工作吗？为什么？

**预期结果**：你会看到大约七八处 `# NEW`，正好对应“初始化 + 采样 + 包装”三大类。`DDP` 包装器在内部依赖进程组已经建立，所以删掉 `init_process_group` 会在包装或第一次反向传播时报错。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Windows 上要用 `gloo` 而不是 `nccl`？
**参考答案**：`nccl` 是 NVIDIA 专为其 GPU 编写的集合通信库，PyTorch 的 Windows 版本通常不带 `nccl` 支持；`gloo` 是跨平台、可在 CPU 与 GPU 上工作的通用后端，因此 Windows 退回 `gloo`。

**练习 2**：`MASTER_ADDR` 为什么指向 `localhost`？
**参考答案**：本脚本假设所有 GPU 都在**同一台机器**上，`rank 0` 进程就在本机，所以会合地址用 `localhost` 即可。如果是多机训练，`MASTER_ADDR` 要写成 `rank 0` 所在机器的网卡 IP。

---

### 4.2 进程组、rank 与两种启动方式

#### 4.2.1 概念说明

DDP 里有三个反复出现的标识，必须分清：

| 名称 | 含义 | 本脚本取值（2 卡） |
| --- | --- | --- |
| `world_size` | 进程组的**总进程数** | 2 |
| `rank` | 进程在组里的**全局唯一编号**，0 到 `world_size-1` | 0 或 1 |
| `local_rank` | 进程在**本机**上的 GPU 编号 | 单机时等于 `rank` |

`rank 0` 常被当作“主进程”：它负责打印日志、保存 checkpoint 等只需做一次的工作（否则每个进程都打印一次会很吵，写文件还会冲突）。

**进程组（process group）** 是把所有进程“编进同一个通信频道”的逻辑抽象。一旦组建立好，调用一次 `all-reduce`，PyTorch 就自动在组内所有进程间同步张量——你不必关心底层是走 PCIe、NVLink 还是网络。

启动这些进程有两种主流方式，本仓库各给了一个脚本：

1. **脚本自己派生（`mp.spawn`）**：`DDP-script.py` 用 `torch.multiprocessing.spawn` 在脚本内部按 `world_size` 派生进程，自己把 `rank` 传进去。简单直观，但多机协调要自己处理。
2. **外部启动器（`torchrun`）**：`DDP-script-torchrun.py` 由 PyTorch 的 `torchrun` 命令启动，启动器负责派生进程、设置一堆环境变量（`WORLD_SIZE`、`RANK`、`LOCAL_RANK`、`MASTER_ADDR`、`MASTER_PORT`），并处理多机会合。生产环境推荐这种方式。

#### 4.2.2 核心流程

**方式一（mp.spawn）的流程**：

```
__main__ 里：
  world_size = torch.cuda.device_count()      # 有几张卡就起几个进程
  mp.spawn(main, args=(world_size, num_epochs),
           nprocs=world_size)                  # spawn 自动把 rank=0..world_size-1 作为第一个参数传给 main
```

`mp.spawn` 的关键约定：它会把 `0, 1, …, nprocs-1` 作为**第一个参数**依次传给 `main`，所以 `main(rank, world_size, num_epochs)` 的 `rank` 是 spawn 自动塞进来的，不是我们手动传的。

**方式二（torchrun）的流程**：

```
命令行： torchrun --nproc_per_node=2 DDP-script-torchrun.py
         （torchrun 起两个进程，并在每个进程里设置好环境变量）
__main__ 里：
  world_size = int(os.environ["WORLD_SIZE"])   # 从环境变量读
  rank       = int(os.environ["LOCAL_RANK"])   # 从环境变量读
  main(rank, world_size, num_epochs)
```

因为 `torchrun` 已经设置好了 `MASTER_ADDR` 等变量，`torchrun` 版本的 `ddp_setup` 多了一层保护：只在变量没被设置时才给默认值，避免覆盖启动器的配置。

#### 4.2.3 源码精读

`mp.spawn` 派生进程：

[DDP-script.py:207-212](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L207-L212) —— 用 `nprocs=world_size` 每张卡起一个进程，`rank` 由 spawn 自动注入。

`torchrun` 版本从环境变量读 `rank`/`world_size`，且只在 `rank==0` 时打印，避免每个进程重复输出：

[DDP-script-torchrun.py:199-216](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script-torchrun.py#L199-L216) —— 读 `WORLD_SIZE`/`LOCAL_RANK`/`RANK`，并用 `if rank == 0` 守卫日志。

`torchrun` 版本的 `ddp_setup` 不覆盖启动器设置：

[DDP-script-torchrun.py:28-32](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script-torchrun.py#L28-L32) —— 只有 `MASTER_ADDR`/`MASTER_PORT` 尚未被 `torchrun` 设置时才填默认值。

两份脚本的 README 也讲清了取舍：

[README.md:12](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/README.md#L12) —— `torchrun` 的优势是自动处理分布式初始化与多机协调，简化搭建过程。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解两种启动方式下 `rank` 是怎么“到达” `main` 函数的。

**操作步骤**：

1. 在 `DDP-script.py` 里，`mp.spawn(main, args=(world_size, num_epochs), nprocs=world_size)` 并没有把 `rank` 写进 `args`。请追踪：`rank` 是从哪里来的？（提示：spawn 的第一个位置参数会被自动填充为进程编号。）
2. 在 `DDP-script-torchrun.py` 里，对比 `__main__` 如何用 `os.environ` 拿到 `rank`。
3. 把两个脚本的 `main` 函数签名比较一下，确认它们完全相同：`main(rank, world_size, num_epochs)`。

**预期结果**：两份脚本的 `main` 完全一致，差异只在“谁负责决定 `rank`/`world_size` 并调用 `main`”——`mp.spawn` 版在 Python 代码里决定，`torchrun` 版在命令行+环境变量里决定。这就是 README 所说 `torchrun` “简化搭建”的含义。

#### 4.2.5 小练习与答案

**练习 1**：`mp.spawn` 调用里没有出现 `rank` 这个字眼，进程是怎么拿到自己 `rank` 的？
**参考答案**：`mp.spawn` 的约定是把 `0` 到 `nprocs-1` 的整数作为**第一个参数**依次注入每个被派生的进程所调用的函数，因此 `main(rank, ...)` 的第一个形参 `rank` 自动等于该进程的编号。

**练习 2**：为什么 `torchrun` 版本要用 `if rank == 0:` 守卫打印语句？
**参考答案**：`torchrun` 会为每个进程都执行一遍脚本顶层代码。如果不加守卫，环境信息会被打印 `world_size` 次；只让 `rank 0` 打印，日志干净且无冲突。

---

### 4.3 分布式数据并行训练：切分数据 + 同步梯度

#### 4.3.1 概念说明

初始化只是搭好“通信总线”，真正实现数据并行训练的是两个组件：

**① DistributedSampler —— 切分数据，避免重复计算**

普通 `DataLoader` 的 `shuffle=True` 会让每个进程独立打乱并看到**全部**数据，这样 N 个进程等于把数据集重复训练了 N 遍。`DistributedSampler` 的作用是把数据集的索引**不重叠地**切成 `world_size` 份，每个 `rank` 只迭代属于自己的那一小份。于是：

- 必须把 `DataLoader` 的 `shuffle` 关掉（设为 `False`），因为打乱现在归 sampler 管。
- 每个 epoch 要调用 `sampler.set_epoch(epoch)`，否则每个 epoch 用同一个随机种子，打乱顺序永远不变，相当于数据顺序固定。

**② DDP 包装器 —— 同步梯度，保持副本一致**

`model = DDP(model, device_ids=[rank])` 把普通模型包一层。这层包装做两件事：

- 接管 `forward()`：输入先经它转发给真正的模型。
- 钩住 `backward()`：在反向传播时，对每个参数的梯度自动发起 **all-reduce**，让所有进程拿到**相同的平均梯度**。

梯度求平均的数学表达：

\[
\bar{g} = \frac{1}{N}\sum_{i=0}^{N-1} g_i
\]

其中 \(N\) 是 `world_size`，\(g_i\) 是第 \(i\) 个进程对自己那份局部数据算出的梯度。因为默认的 `cross_entropy` 用的是 `mean` 归约（对样本数取平均），所以这个平均梯度在数学上**近似等价于**单卡上用完整大 batch 训练一步的梯度。

最关键的推论：**每个进程都用同一个 \(\bar{g}\) 去更新同一份权重，所以更新后所有副本依然逐位相同**——这就是为什么 DDP 不需要额外“平均模型”的步骤，模型天然保持同步。

> 一个常被忽视的细节：因为每个进程吃的数据**不同**，所以你看到各进程打印的**每步训练损失会不一样**——这并不代表模型分叉了，只是它们在算不同批次的损失。验证模型是否一致的正确做法是：让它们在**同一份**数据上评估（本脚本的 `test_loader` 没有用 `DistributedSampler`，两个进程都跑完整测试集），此时打印的测试准确率应当**完全相同**。

#### 4.3.2 核心流程

把三大件串起来，DDP 版训练循环如下（伪代码，对照 [u8-l1](u8-l1-pytorch-essentials.md) 的单卡骨架，加粗处是新增）：

```
ddp_setup(rank, world_size)              # ① 建进程组
train_loader 用 DistributedSampler       # ② 数据按 rank 切分
model = model.to(rank)
model = DDP(model, device_ids=[rank])    # ③ 包装，接管 forward/backward 同步梯度

for epoch in range(num_epochs):
    train_loader.sampler.set_epoch(epoch)  # 每轮换打乱种子
    model.train()
    for features, labels in train_loader:
        features, labels = features.to(rank), labels.to(rank)
        logits = model(features)
        loss   = F.cross_entropy(logits, labels)
        optimizer.zero_grad()
        loss.backward()    # ← DDP 在此自动 all-reduce 平均梯度
        optimizer.step()   # 各进程用相同平均梯度做相同更新

destroy_process_group()    # 收尾
```

注意 `loss.backward()` 这一行和单卡代码**长得一模一样**——梯度同步的全部魔法都被 DDP 包装器藏在背后了。

#### 4.3.3 源码精读

`DistributedSampler` 接管数据切分，注意 `shuffle=False`：

[DDP-script.py:110-119](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L110-L119) —— `shuffle=False`（交给 sampler），`sampler=DistributedSampler(train_ds)` 把索引按 rank 不重叠切分。

模型搬到对应 GPU 后，用 DDP 包装：

[DDP-script.py:134-139](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L134-L139) —— `model.to(rank)` 绑卡，`DDP(model, device_ids=[rank])` 包装；注释提醒包装后真正的模型挂在 `model.module` 上。

每轮换打乱种子（否则数据顺序固定）：

[DDP-script.py:141-143](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L141-L143) —— `set_epoch(epoch)` 让每个 epoch 用不同随机种子打乱。

训练循环主体（与单卡几乎一致，仅 `.to(rank)` 和包装后的 `model(...)`）：

[DDP-script.py:145-159](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L145-L159) —— 标准的 `zero_grad → forward → loss → backward → step`，DDP 在 `backward` 处自动同步梯度；日志带 `[GPU{rank}]` 前缀，方便分辨是哪个进程打印的。

`compute_accuracy` 中 `test_loader` 没有 sampler，两个进程都评估完整测试集——这正是验证模型一致性的地方：

[DDP-script.py:163-167](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L163-L167) —— 训练后评估；`test_acc` 在各进程上应完全相同。

脚本里还有一段友好的错误提示：当数据集太小、某个进程分不到样本导致 `total_examples=0` 而触发 `ZeroDivisionError` 时，提示用户本脚本是为 2 张卡设计的：

[DDP-script.py:170-176](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L170-L176) —— 捕获除零错误，提示用 `CUDA_VISIBLE_DEVICES=0,1` 限定 2 卡，或解开 103–107 行扩充数据。

扩充数据的那几行（注释状态，供多于 2 卡时打开）：

[DDP-script.py:101-106](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/appendix-A/01_main-chapter-code/DDP-script.py#L101-L106) —— 用加噪声复制的方式把 5 条训练样本放大 `factor` 倍，从而喂得饱更多卡。

#### 4.3.4 代码实践（动手运行型）

**实践目标**：用 `torchrun` 在 2 个进程上跑通分布式训练，并验证“各 rank 模型权重完全一致”。

> 说明：仓库脚本默认面向 NVIDIA GPU（`backend="nccl"`、`torch.cuda.set_device`）。**如果你没有 GPU，需要做一份 CPU 适配**。下面给出适配要点（标注为示例代码，非仓库原文件）。

**步骤 1：GPU 环境（有 2 张 NVIDIA 卡）**

直接用 `torchrun` 版脚本，跑 2 个进程：

```bash
torchrun --nproc_per_node=2 appendix-A/01_main-chapter-code/DDP-script-torchrun.py
```

或用正文版脚本：

```bash
CUDA_VISIBLE_DEVICES=0,1 python appendix-A/01_main-chapter-code/DDP-script.py
```

**步骤 2：无 GPU 时的 CPU 模拟（示例代码）**

把 `DDP-script-torchrun.py` 复制一份，按下面三处改动（这些改动**不在**仓库原文件中）：

```python
# 示例代码：CPU-only DDP 适配
def ddp_setup(rank, world_size):
    if "MASTER_ADDR" not in os.environ:
        os.environ["MASTER_ADDR"] = "localhost"
    if "MASTER_PORT" not in os.environ:
        os.environ["MASTER_PORT"] = "12345"
    init_process_group(backend="gloo", rank=rank, world_size=world_size)
    # 删除 torch.cuda.set_device(rank)：CPU 模式没有“第 rank 号 GPU”

def main(rank, world_size, num_epochs):
    ddp_setup(rank, world_size)
    train_loader, test_loader = prepare_dataset()
    model = NeuralNetwork(num_inputs=2, num_outputs=2)
    # 删除 model.to(rank)：模型留在 CPU
    optimizer = torch.optim.SGD(model.parameters(), lr=0.5)
    model = DDP(model)          # device_ids 留空（CPU+gloo 时不要传 device_ids）
    ...
    # 训练循环里删除 features.to(rank)/labels.to(rank)，张量留在 CPU
```

然后用 `gloo` 后端起 2 个进程（即便只有 CPU）：

```bash
torchrun --nproc_per_node=2 DDP-script-torchrun-cpu.py
```

**步骤 3：观察并记录各 rank 的损失一致性**

运行后你会看到形如 `[GPU0] ... Loss: 0.xx` 和 `[GPU1] ... Loss: 0.yy` 的交替输出。

**需要观察的现象与预期结果**：

1. **每步训练损失 `0.xx` 与 `0.yy` 通常不相等**——预期如此，因为两个进程拿到的是数据集的不同子集。
2. **两个进程打印的 `Test accuracy` 完全相同**——这是关键一致性证据：证明经过梯度同步，两份模型副本权重逐位相同，所以在同一份测试集上给出完全一样的预测。
3. （可选进阶）在 `main` 末尾加一行，把 `model.module` 的某个参数 `.tolist()` 打印出来，确认两个进程的数值完全一致。

> 关于具体数值：本玩具数据集只有 5 条训练样本，且未训练起始即接近随机，**具体的损失/准确率数值待本地验证**；本实践要验证的是“两个进程的测试准确率是否相等”这一**一致性结论**，而非某个固定数字。

> 注意事项：本脚本数据量极小，`--nproc_per_node` 设大于 2 时某些进程可能分不到样本而触发 `ZeroDivisionError`，此时按脚本提示解开 103–106 行扩充数据。

#### 4.3.5 小练习与答案

**练习 1**：把 `DataLoader` 的 `shuffle` 设回 `True`、同时保留 `DistributedSampler`，会发生什么？
**参考答案**：PyTorch 会直接报错——`DistributedSampler` 与 `shuffle=True` 互斥。sampler 已经负责按 epoch 打乱数据，DataLoader 不应再重复打乱。

**练习 2**：如果忘记调用 `sampler.set_epoch(epoch)`，训练会出错吗？会变差吗？
**参考答案**：不会报错，但每个 epoch 都用同一个固定种子打乱，导致各 epoch 看到的样本顺序完全相同，等同于把数据顺序写死，可能影响收敛质量。`set_epoch` 的作用就是每轮换种子、让打乱顺序变化。

**练习 3**：为什么说“各进程训练损失不同”并不代表模型分叉？
**参考答案**：因为 DDP 在每步 `backward` 时已通过 all-reduce 把梯度平均，所有进程用相同的平均梯度更新相同的权重，模型始终逐位一致。训练损失不同只是因为各进程评估的是**不同批次**的数据；要验证一致性，应让它们在**同一份**数据（如完整测试集）上评估，结果必然相同。

---

## 5. 综合实践

**任务**：把本讲的三件套迁移到本仓库第 4 章的 `GPTModel` 上，做一个“最小可运行”的分布式预训练脚本骨架（不要求真的训出效果，只要求结构正确）。

参考 [ch04/01_main-chapter-code/gpt.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py) 里的 `GPTModel` 与 `GPT_CONFIG_124M`，结合本讲的 DDP 三件套，完成以下骨架（示例代码）：

```python
# 示例代码：把 DDP 三件套套到 GPTModel 上（仅结构示意）
def main(rank, world_size):
    ddp_setup(rank, world_size)

    # ① 数据：用 DistributedSampler 切分 ch02 的 create_dataloader_v1 产出的数据
    train_loader = DataLoader(train_ds, batch_size=...,
                              sampler=DistributedSampler(train_ds))

    # ② 模型：构建 GPTModel，绑卡后用 DDP 包装
    model = GPTModel(GPT_CONFIG_124M).to(rank)
    model = DDP(model, device_ids=[rank])

    optimizer = torch.optim.AdamW(model.parameters(), lr=4e-4)

    for epoch in range(num_epochs):
        train_loader.sampler.set_epoch(epoch)
        for input_batch, target_batch in train_loader:
            input_batch = input_batch.to(rank)
            target_batch = target_batch.to(rank)
            logits = model(input_batch)
            loss = torch.nn.functional.cross_entropy(
                logits.flatten(0, 1), target_batch.flatten())   # 复用 u5-l1 的展平技巧
            optimizer.zero_grad()
            loss.backward()    # DDP 自动同步梯度
            optimizer.step()

    destroy_process_group()
```

**验收要点**（自检清单）：

1. 是否调用了 `ddp_setup` / `init_process_group` 建立进程组？
2. `DataLoader` 是否用了 `DistributedSampler` 且 `shuffle=False`？
3. 模型是否在 `.to(rank)` **之后**才用 `DDP(...)` 包装？（顺序反了会丢设备绑定）
4. 每个 epoch 是否调用了 `sampler.set_epoch(epoch)`？
5. 训练循环主体是否与 [u5-l2](u5-l2-training-loop.md) 的 `train_model_simple` 完全同构，只是多了 `.to(rank)` 和 DDP 包装？
6. 是否在最后调用了 `destroy_process_group()`？

如果你能把这 6 点都说清楚，说明你已经掌握了 DDP 的全部关键结构。实际能否跑通、收敛如何，依赖你的硬件与数据，**待本地验证**。

## 6. 本讲小结

- **DDP 是数据并行**：每个进程持有一份**完整**模型副本，区别只在各自吃到的数据不同；这与“把模型切开”的模型并行是两回事。
- **三件套**：`init_process_group`（建通信总线）+ `DistributedSampler`（不重叠切分数据）+ `DDP(model)`（包装模型，在 `backward` 时自动 all-reduce 平均梯度）。
- **rank / world_size / local_rank** 是进程标识；`rank 0` 常作主进程负责日志与存盘；进程组建立后 `all-reduce` 让所有进程拿到相同平均梯度。
- **两种启动方式**：`mp.spawn` 由脚本内部派生进程（`DDP-script.py`），`torchrun` 由外部启动并设好环境变量（`DDP-script-torchrun.py`），后者更适合多机。
- **模型天然同步**：每个进程用相同的平均梯度更新相同权重，副本始终逐位一致；所以 DDP 不需要额外的“平均模型”步骤。
- **一致性验证看测试集**：各进程每步训练损失不同（数据不同），但在同一份测试集上的准确率必然相同——这才是判断模型是否同步的正确方法。

## 7. 下一步学习建议

- **回到真实模型**：把综合实践里的 GPTModel 骨架补全，对比单卡与双卡训练同样 epoch 数的墙钟时间，体会数据并行的加速比。
- **进阶分布式**：当模型大到单卡装不下时，DDP 不再够用，可进一步了解 **FSDP（Fully Sharded Data Parallel）** 与张量并行——它们在 DDP 的“数据并行”基础上叠加了“把模型也切开”的维度。
- **混合精度 + DDP**：结合 `torch.cuda.amp` 的自动混合精度训练，可进一步成倍提升多卡吞吐，是工业训练的标配组合。
- **承接本手册的工程优化主线**：本讲的 DDP 与 [u8-l2](u8-l2-training-loop-bells-whistles.md) 的 warmup/余弦衰减/梯度裁剪、[u10-l3](u10-l3-production-engineering.md) 的内存高效权重加载同属“训练工程”主题，建议把它们连起来读，建立完整的“大规模训练”工程视角。
