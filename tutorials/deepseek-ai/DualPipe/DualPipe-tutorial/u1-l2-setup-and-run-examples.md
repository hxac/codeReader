# 运行示例与环境准备

## 1. 本讲目标

上一讲我们从 README 建立了 DualPipe 的全局认知：它是一种**双向流水线并行**算法，用「数据相向喂入」和「计算/通信完全重叠」来压低流水线气泡。但概念再清晰，不跑起来就始终是纸上谈兵。

本讲的目标是让 DualPipe 在你手里**真正动起来**。学完后你应该能够：

1. 说出 DualPipe 的运行依赖（PyTorch 2.0+、多卡、NCCL），并理解 `setup.py` 是如何打包和生成版本的。
2. 看懂 `examples/` 里的脚本如何用 `torch.multiprocessing.spawn` 拉起多进程，每个进程对应一张 GPU。
3. 逐行讲清 `main(rank, pp_size)` 从 `dist.init_process_group` 到 `dualpipe_model.step(...)` 再到数值校验的完整链路。
4. 区分 DualPipe 与 DualPipeV 在「输入喂入方式」和「所需 GPU 数」上的差异。

本讲**不**深入 8 步调度的内部细节（那是第 3 单元的事），只关注「怎么装、怎么跑、跑完怎么验证对不对」。

## 2. 前置知识

在进入源码前，先用大白话补几个分布式训练的基础概念（上一讲已经讲过的流水线并行、微批次、气泡这里不再重复）：

- **rank（秩）**：在分布式训练里，每个独立参与计算的进程都有一个编号，叫 rank。rank=0 通常扮演「队长」，负责打印日志、汇总结果。本讲里 **rank 和 GPU 是一一对应的**：第 `i` 号进程使用第 `i` 号 GPU。
- **world_size（世界大小）**：所有参与分布式训练的进程总数。在本讲示例里，world_size 就等于流水线阶段数 `pp_size`，也等于使用的 GPU 数。
- **process group（进程组）**：所有 rank 通过某种方式「认识彼此」后形成的通信集合。建立进程组是分布式训练的第一步，建好之后才能用 `all_gather`、`send/recv` 这类集合通信或点对点通信。
- **NCCL（NVIDIA Collective Communications Library）**：英伟达提供的 GPU 间高速通信库，PyTorch 里 `backend='nccl'` 就是走它。DualPipe 的设备间数据搬运依赖它。
- **点对点通信（P2P）**：两个 rank 之间直接收发数据（send/recv），区别于「一对所有」的集合通信。DualPipe 相邻阶段之间传微批次用的就是 P2P。

如果你手头**没有多卡 GPU 环境**，也不用担心——本讲的代码实践专门设计了一条「纯阅读型」路线，跟着 `main` 函数一行行追踪调用链即可，不需要真的运行。

## 3. 本讲源码地图

本讲涉及三个关键文件，作用如下：

| 文件 | 作用 | 本讲解读重点 |
|------|------|--------------|
| `setup.py` | 把 `dualpipe` 打成可安装的 Python 包，并生成版本号 | 依赖声明缺失、版本号生成逻辑 |
| `examples/example_dualpipe.py` | DualPipe 的可运行示例（含正确性自校验） | `__main__` 入口、`main(rank, pp_size)` 全链路 |
| `examples/example_dualpipev.py` | DualPipeV（V 型变体）的示例 | 与 DualPipe 在输入喂入上的差异 |

另外会顺带引用 `dualpipe/__init__.py`，说明示例脚本 `import` 进来的那些名字分别从哪来（这一点的完整梳理在下一讲 u1-l3）。

## 4. 核心概念与源码讲解

### 4.1 依赖与安装：读懂 setup.py

#### 4.1.1 概念说明

DualPipe 是一个**纯 Python 库**，核心代码在 `dualpipe/` 包里。要让示例脚本能 `from dualpipe import DualPipe` 成功，要么把仓库根目录加到 `PYTHONPATH`，要么把 `dualpipe` 正式「安装」到环境里。`setup.py` 就是干第二件事的——它用标准库 `setuptools` 描述了这个包叫什么、包含哪些子包、版本号是多少。

一个容易踩坑的点：**README 写了「Requirements: PyTorch 2.0 and above」，但 `setup.py` 并没有把 PyTorch 声明为安装依赖**。也就是说 `pip install` 这个包时不会自动帮你装 PyTorch，你必须**自己先装好 PyTorch**。这一点稍后在源码精读里会确认。

#### 4.1.2 核心流程

`setup.py` 做两件事：

1. **算一个版本后缀 `rev`**：优先用 `git rev-parse --short HEAD` 取当前提交的短哈希（前面加 `+`）；取不到（比如不是 git 仓库）就用当前时间戳兜底。
2. **调用 `setup(...)`**：包名 `dualpipe`，版本 = `"1.0.0" + rev`，只打包 `dualpipe` 这一个子包。

最终安装出来的版本号长这样：`1.0.0+030ce43`（有 git）或 `1.0.0+2026-06-23-...`（无 git）。

#### 4.1.3 源码精读

先看版本后缀的生成逻辑：

[setup.py:7-13](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/setup.py#L7-L13) —— 尝试取 git 短哈希，失败则退化为时间戳：

```python
try:
    cmd = ['git', 'rev-parse', '--short', 'HEAD']
    rev = '+' + subprocess.check_output(cmd).decode('ascii').rstrip()
except Exception as _:
    now = datetime.now()
    date_time_str = now.strftime("%Y-%m-%d-%H-%M-%S")
    rev = '+' + date_time_str
```

再看 `setup(...)` 调用本身：

[setup.py:15-19](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/setup.py#L15-L19) —— 注意这里**没有 `install_requires`**，所以不会自动拉 PyTorch：

```python
setup(
    name="dualpipe",
    version="1.0.0" + rev,
    packages=["dualpipe"],
)
```

> 小提示：因为版本号依赖 `git`，**在仓库目录里 `pip install -e .` 才能拿到带哈希的版本号**；如果在压缩包（非 git）里装，就会得到时间戳版本。

#### 4.1.4 代码实践

**实践目标**：确认环境里 PyTorch 版本 ≥ 2.0，并尝试以开发模式安装 dualpipe。

**操作步骤**：

1. 在仓库根目录执行（不需要 GPU 也能做前两步）：

   ```bash
   python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
   pip install -e .
   python -c "import dualpipe; print(dualpipe.__version__)"
   ```

2. 观察第一条命令输出的 PyTorch 版本是否 ≥ 2.0、`cuda.is_available()` 是否为 `True`。

**需要观察的现象**：

- `dualpipe.__version__` 应当打印类似 `1.0.0+030ce43` 的字符串（后缀取决于你的 git HEAD）。

**预期结果**：

- 若 PyTorch 已正确安装且版本 ≥ 2.0，`import dualpipe` 不会报错。
- 若 PyTorch 版本过低或缺失，`import dualpipe` 会因 `import torch` 失败而报错——这正是「依赖需自己装」的体现。

> 若你的环境没有 GPU，第二步 `cuda.is_available()` 会是 `False`，这不影响安装，只是稍后跑示例会受限（见 4.2 节）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pip install dualpipe`（从本仓库安装）后，还要单独确保 PyTorch 已安装？

**参考答案**：因为 `setup.py` 里没有 `install_requires`，PyTorch 不在自动依赖里；README 把它列为运行要求，但安装动作本身不会拉取它。

**练习 2**：在不是 git 仓库的目录里运行这个 `setup.py`，版本号会变成什么？

**参考答案**：会变成 `1.0.0+YYYY-MM-DD-HH-MM-SS` 这种带时间戳的形式，因为 `git rev-parse` 抛异常，走了 `except` 分支用当前时间兜底。

### 4.2 多进程启动：spawn 入口

#### 4.2.1 概念说明

分布式训练要跑起来，最朴素的方式是「**一张 GPU 配一个进程**」。手动开多个终端、设一堆环境变量去启动每个进程非常繁琐，所以 PyTorch 提供了 `torch.multiprocessing.spawn`：它能在**一个 Python 进程里**自动 fork 出 `nprocs` 个子进程，并给每个子进程分配一个从 0 开始的编号（这个编号就是 rank）。

DualPipe 的两个示例脚本都用这套机制：`__main__` 里先数一下本机有多少张 GPU，然后**从大到小**依次尝试不同 GPU 数量的配置，每种配置都 spawn 一组进程跑一遍自校验。这种「降配扫描」的写法很适合做兼容性测试——你有多少卡，它就尽力测多少种规模。

#### 4.2.2 核心流程

两个脚本的入口结构几乎一样，但**步长不同**：

1. 用 `torch.cuda.device_count()` 数 GPU。
2. DualPipe 把数量**向下取偶**，并以 **−2** 步长递减（因为 DualPipe 要求偶数个 rank，原因见 4.4 节）。
3. DualPipeV 不取偶，以 **−1** 步长递减（任意数量都行）。
4. 每个数量 `ngpus` 都调用 `test_xxx(ngpus)`，里面用 `spawn(main, args=(ngpus,), nprocs=ngpus)` 拉起 `ngpus` 个进程，每个进程执行 `main(rank=i, pp_size=ngpus)`。

#### 4.2.3 源码精读

DualPipe 的入口，注意 `// 2 * 2` 的「向下取偶」和 `-2` 步长：

[example_dualpipe.py:199-202](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L199-L202)：

```python
if __name__ == "__main__":
    num_gpus = torch.cuda.device_count() // 2 * 2
    for ngpus in range(num_gpus, 0, -2):
        test_dualpipe(ngpus)
```

`test_dualpipe` 把 `ngpus` 当作进程数和 `pp_size` 传进去：

[example_dualpipe.py:195-196](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L195-L196)：

```python
def test_dualpipe(ngpus):
    torch.multiprocessing.spawn(main, args=(ngpus, ), nprocs=ngpus, daemon=True)
```

对比 DualPipeV 的入口，步长是 `-1`、不取偶：

[example_dualpipev.py:180-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L180-L183)：

```python
if __name__ == "__main__":
    num_gpus = torch.cuda.device_count()
    for ngpus in range(num_gpus, 0, -1):
        test_dualpipev(ngpus)
```

> 关键点：`spawn` 的 `nprocs=ngpus` 决定了起几个进程，`args=(ngpus,)` 决定了传给 `main` 的第二个参数（即 `pp_size`）。两者都用 `ngpus`，所以**进程数 = 流水线阶段数 = GPU 数**，三者在本讲里始终相等。

#### 4.2.4 代码实践

**实践目标**：在不运行的前提下，手算入口会以哪些 GPU 数量各跑一次。

**操作步骤**：

1. 假设本机有 8 张 GPU，分别写出 `example_dualpipe.py` 和 `example_dualpipev.py` 的 `__main__` 会调用 `test_xxx` 多少次、每次 `ngpus` 是多少。
2. 再假设只有 1 张 GPU，回答 DualPipe 的入口会发生什么。

**需要观察的现象**（纯推理）：

- DualPipe：`num_gpus = 8 // 2 * 2 = 8`，`range(8, 0, -2)` → 依次为 8、6、4、2，共 4 次。
- DualPipeV：`range(8, 0, -1)` → 8、7、6、5、4、3、2、1，共 8 次。
- 只有 1 张卡时：DualPipe 的 `num_gpus = 1 // 2 * 2 = 0`，`range(0, 0, -2)` 为空 → **一次都不跑**；DualPipeV 则会以 `ngpus=1` 跑一次。

**预期结果**：DualPipe 至少需要 2 张卡（偶数）才有动作；DualPipeV 单卡即可启动。这条结论直接对应 README 对比表里「DualPipe 需 PP 个设备、DualPipeV 需 PP/2 个设备」的设备效率差异。

#### 4.2.5 小练习与答案

**练习 1**：`torch.multiprocessing.spawn(main, args=(ngpus,), nprocs=ngpus)` 中，`main` 函数会以什么参数被调用 `ngpus` 次？

**参考答案**：第 `i`（0 ≤ i < ngpus）次调用为 `main(rank=i, pp_size=ngpus)`。`spawn` 自动把 0..nprocs-1 作为第一个参数 `rank` 传入，`args` 里的 `ngpus` 作为第二个参数 `pp_size` 传入。

**练习 2**：为什么 DualPipe 入口要 `// 2 * 2` 取偶，而 DualPipeV 不用？

**参考答案**：DualPipe 是双向对称配对的流水线，需要偶数个 rank 才能成对（rank `i` 与 `pp_size-1-i` 互补），所以强制偶数；DualPipeV 是 V 型切半变体，对 rank 数没有偶数约束，因此任意数量都能跑。

### 4.3 main(rank, pp_size) 的初始化链路

#### 4.3.1 概念说明

每个被 spawn 出来的进程都会执行 `main(rank, pp_size)`。这是整个示例的**核心驱动函数**，它要完成「建通信 → 配设备 → 建模型 → 跑一步 → 验证」全流程。本模块先看**初始化链路**：建进程组、绑 GPU、设随机种子。

`dist.init_process_group` 是 PyTorch 分布式的「开机仪式」：调用之后，所有 rank 才算互相连通，后续的 `send/recv`、`all_gather` 才能工作。本例用 `init_method="env://"`，意味着进程组通过**环境变量**来完成「会合」：`rank` 和 `world_size` 由参数显式传入，而会合地址 `MASTER_ADDR`、端口 `MASTER_PORT` 从环境变量读取。

#### 4.3.2 核心流程

`main` 的初始化部分按顺序做这些事（行号见 4.3.3）：

1. 判断自己是不是首/末 rank（DualPipe 两种都要，DualPipeV 只要首 rank）。
2. `dist.init_process_group(...)` 建立进程组（NCCL 后端、env 会合、显式 world_size/rank）。
3. `torch.cuda.set_device(rank)` 把当前进程绑到第 `rank` 号 GPU。
4. `torch.set_default_device(...)` 让之后新建的张量默认落在自己的 GPU 上。
5. `torch.manual_seed(233)` 统一种子。
6. 设置 `CUBLAS_WORKSPACE_CONFIG` 以获得确定性的线性代数结果。

> 为什么所有进程都用同一个种子 `233`？因为每个进程都会独立地构造「完整模型」和「完整输入」（见 4.4 节），只有各进程生成的权重和输入**完全一致**，分布式跑出来的结果才能和单进程参考结果逐元素对比。CUDA 随机数在相同种子下跨设备产生相同序列，所以这一招在多卡下也成立。

#### 4.3.3 源码精读

DualPipe 的初始化链路（DualPipeV 几乎一致，只是少了 `is_last_rank`）：

[example_dualpipe.py:109-116](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L109-L116)：

```python
def main(rank, pp_size):
    is_first_rank = rank == 0
    is_last_rank = rank == pp_size - 1
    dist.init_process_group(backend='nccl', init_method="env://", world_size=pp_size, rank=rank)
    torch.cuda.set_device(rank)
    torch.set_default_device(f"cuda:{rank}")
    torch.manual_seed(233)
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
```

随后是配置区，并调用从包里 import 进来的两个全局通信设置函数：

[example_dualpipe.py:118-125](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L118-L125)：

```python
num_chunks = 20
micro_batch_size = 3
seq_len = 256
hidden_size = 512
if is_first_rank:
    print(f"{pp_size=}, {num_chunks=}, {seq_len=}, {hidden_size=}", flush=True)
set_p2p_tensor_shapes([(micro_batch_size, seq_len, hidden_size)])
set_p2p_tensor_dtype(torch.float32)
```

这里的 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype` 来自 `dualpipe/__init__.py` 的导出（[example_dualpipe.py:9](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L9)），它们设置的是**相邻阶段间 P2P 通信时张量的形状和数据类型**——本讲你只需知道「必须在跑 step 之前设置好」；底层细节在 u2-l2 详讲。

> 关于 `CUBLAS_WORKSPACE_CONFIG=":4096:8"`：它给 cuBLAS 分配确定性的工作区配置，目的是让矩阵乘法结果在多次运行间**位级一致**。这对本例至关重要——示例用 `cal_diff < 1e-13` 这种极严苛的余弦阈值校验梯度，任何数值抖动都会让校验失败。

#### 4.3.4 代码实践

**实践目标**：理解 `init_method="env://"` 的会合机制，补全运行所需的环境变量。

**操作步骤**：

1. 阅读上面 4.3.3 的源码，确认 `init_process_group` 用的是 `env://`。
2. 因为示例脚本本身**没有显式设置** `MASTER_ADDR`/`MASTER_PORT`，请你在真正运行前补上（单机多卡通常这样写）：

   ```bash
   export MASTER_ADDR=localhost
   export MASTER_PORT=29500
   python examples/example_dualpipev.py
   ```

3. 观察首 rank 是否打印出那行 `pp_size=..., num_chunks=20, ...`。

**需要观察的现象**：

- 若环境变量缺失，`init_process_group` 可能在某些 PyTorch 版本下报「MASTER_ADDR/MASTER_PORT 未设置」相关错误；补上后应能正常建组。
- 首进程会打印一行配置，其余进程不打印。

**预期结果**：进程组建立成功后，控制台出现首 rank 的配置行；之后才会进入建模型、跑 step 的阶段。

> 「待本地验证」：不同 PyTorch 版本对缺失 `MASTER_ADDR`/`MASTER_PORT` 的处理（报错 vs. 使用默认值）可能不同。请在你本地版本上确认行为，以决定是否必须显式导出这两个变量。

#### 4.3.5 小练习与答案

**练习 1**：`main` 里同时调用了 `torch.cuda.set_device(rank)` 和 `torch.set_default_device(f"cuda:{rank}")`，它们各自管什么？

**参考答案**：`set_device` 设置当前进程的「当前 GPU」（影响默认 stream、kernel 启动设备等 CUDA 上下文）；`set_default_device` 设置「新建张量默认落在哪个设备」。两者结合，确保本进程的所有计算和张量都落在自己那张卡上。

**练习 2**：为什么每个进程都调用 `torch.manual_seed(233)` 而不是让各进程用不同种子？

**参考答案**：因为每个进程都会独立重建同一份「完整模型」和「完整输入」，只有种子相同、权重和输入在各进程间完全一致，分布式结果才能与单进程参考结果对齐，校验才有意义。

### 4.4 step() 调用与端到端校验链路

#### 4.4.1 概念说明

初始化完成后，`main` 进入「建模型 → 跑一步 → 验证」阶段。这一段最能体现 DualPipe 的用法和它的「自校验」设计：脚本先跑一个**单进程参考实现** `ref_step`，再跑 **DualPipe 分布式版本**，最后把两者结果逐元素比较。如果完全一致，就证明这套复杂的双向调度在数值上是正确的。

这一段里有几个对初学者最重要的点：

- **每个 rank 持有两个 stage**：上一讲预告过「每设备持两个阶段」，这里第一次在代码里看到——`local_modules = nn.Sequential(PipelineStage, PipelineStage)`。
- **输入只在两端喂**（DualPipe）或**只在首端喂**（DualPipeV）：中间 rank 的输入是 `None`，数据靠 P2P 流过来。
- **校验三件套**：loss 校验、梯度校验（`cal_diff < 1e-13`）、输出校验。

#### 4.4.2 核心流程

以 DualPipe 为例，`main` 后半段顺序如下：

1. **建完整模型与输入**：`full_modules`（`pp_size` 个 stage 串起来）、`full_x`/`full_l`（完整输入和标签）。
2. **跑参考实现**：`loss_ref, output_ref = ref_step(full_x, full_l, full_modules, num_chunks)`——把完整 batch 切成 `num_chunks` 个微批次，逐个前向、算 loss、反向，作为「标准答案」。
3. **构造本 rank 的局部模块**：取 `full_modules[rank]` 和 `full_modules[pp_size-1-rank]` 两个 stage，加载到新的 `local_modules` 里，包成 `DualPipe(local_modules)`。
4. **分配本 rank 的输入**：首 rank 拿前半数据、末 rank 拿后半数据，其余 rank 输入为 `None`。
5. **训练步**：`dualpipe_model.step(x, num_chunks=..., criterion=..., labels=..., return_outputs=False)`。
6. **校验 loss / 梯度**：assert 各项一致。
7. **推理步**：`with torch.no_grad()` 再跑一次 `step(..., return_outputs=True)`，校验输出。

DualPipeV 的流程相同，但第 1 步建的是 `pp_size*2` 个 stage，第 4 步**只有首 rank** 有输入。

#### 4.4.3 源码精读

**差异点 A——模型规模**：DualPipe 建 `pp_size` 个 stage，DualPipeV 建 `pp_size*2` 个 stage：

[example_dualpipe.py:128](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L128) —— DualPipe：

```python
full_modules = nn.Sequential(*[PipelineStage(hidden_size) for _ in range(pp_size)])
```

[example_dualpipev.py:127](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L127) —— DualPipeV：

```python
full_modules = nn.Sequential(*[PipelineStage(hidden_size) for _ in range(pp_size * 2)])
```

**差异点 B——输入喂入方式**，这是本讲最值得对比的一段。

DualPipe：首末两端各喂一半数据，中间 rank 为 `None`：

[example_dualpipe.py:145-153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153)：

```python
if is_first_rank:
    x = full_x.chunk(2)[0]
    l = full_l.chunk(2)[1]
elif is_last_rank:
    x = full_x.chunk(2)[1]
    l = full_l.chunk(2)[0]
else:
    x = None
    l = None
```

DualPipeV：只有首 rank 持有数据，其余全为 `None`：

[example_dualpipev.py:143-146](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L143-L146)：

```python
if not is_first_rank:
    x = None
    l = None
```

> 这正是「双向」与「V 型」在用户层面的直观差别：DualPipe 从流水线**两头**相向喂数据（呼应上一讲「数据从两端相向喂入」），DualPipeV 只从**一头**喂，在末端做 V 型转折把数据折返。调度内部如何转折，留到 u4-l1 详讲。

**核心调用——训练步与推理步**。DualPipe 训练步（DualPipeV 行号不同但参数一致）：

[example_dualpipe.py:156](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L156)：

```python
loss, outputs = dualpipe_model.step(x, num_chunks=num_chunks, criterion=criterion, labels=(l,), return_outputs=False)
```

**校验三件套**。loss 校验（首末 rank 各拿对应那一半参考 loss）：

[example_dualpipe.py:159-165](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L159-L165)：

```python
if is_first_rank:
    assert torch.equal(loss, loss_ref.chunk(2)[1])
elif is_last_rank:
    assert torch.equal(loss, loss_ref.chunk(2)[0])
else:
    assert loss is None
assert outputs is None
```

梯度校验：各 rank 把自己两个 stage 的梯度 `all_gather` 汇总，互相补齐后，用 `cal_diff` 与参考梯度比，阈值 `1e-13`：

[example_dualpipe.py:168-177](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L168-L177)：

```python
for (p0, p1) in zip(local_modules[0].parameters(), local_modules[1].parameters()):
    p0all = torch.empty(pp_size, *p0.shape)
    p1all = torch.empty(pp_size, *p0.shape)
    dist.all_gather_into_tensor(p0all, p0.grad)
    dist.all_gather_into_tensor(p1all, p1.grad)
    p0.grad += p1all[pp_size - 1 - rank]
    p1.grad += p0all[pp_size - 1 - rank]
for ((n, p), p_ref) in zip(local_modules.named_parameters(), local_full_modules.parameters()):
    assert cal_diff(p.grad, p_ref.grad) < 1e-13
dualpipe_model.zero_grad()
```

> 关于 `cal_diff`：它定义在 [example_dualpipe.py:103-106](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L103-L106)，是一个**余弦相似度变形**的度量：把两个张量展平比较方向，值越小越接近（0 表示完全同向）。阈值 `1e-13` 极严苛，意味着要求结果几乎位级一致。这个度量的数学含义和阈值选择，到 u4-l3 实战讲义里会再细讲。

最后是推理步（`torch.no_grad()` + `return_outputs=True`），并校验输出：

[example_dualpipe.py:180-192](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L180-L192)：

```python
with torch.no_grad():
    loss, outputs = dualpipe_model.step(x, num_chunks=num_chunks, criterion=criterion, labels=(l,), return_outputs=True)

if is_first_rank:
    assert torch.equal(loss, loss_ref.chunk(2)[1])
    assert torch.equal(outputs, output_ref.chunk(2)[1])
elif is_last_rank:
    assert torch.equal(loss, loss_ref.chunk(2)[0])
    assert torch.equal(outputs, output_ref.chunk(2)[0])
else:
    assert loss is None
    assert outputs is None
```

DualPipeV 的校验链路结构相同，但因为只有首 rank 持有完整输入，loss/输出直接与整体的 `loss_ref`/`output_ref` 比较，见 [example_dualpipev.py:149-173](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L149-L173)。

#### 4.4.4 代码实践

**实践目标**：在不运行的前提下，对照源码梳理「参考实现 vs DualPipe」为什么结果应该一致。

**操作步骤**：

1. 阅读 `ref_step`（[example_dualpipe.py:90-100](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L90-L100)），写下它对每个微批次做了哪四件事。
2. 对照 DualPipe 训练步，解释为什么 `is_first_rank` 校验的是 `loss_ref.chunk(2)[1]`（后半）而不是前半。
3. 解释 `return_outputs=False` 的训练步里，为什么所有 rank 都 `assert outputs is None`。

**需要观察的现象**（纯推理）：

- `ref_step` 对每个微批次：前向算 `micro_y` → 算 loss → `loss.backward()` → 收集 `micro_y` 和 loss；最后 `cat`/`stack` 成完整结果。
- 首 rank 拿的是 `full_x.chunk(2)[0]`，但它的 loss 对应参考结果的**后半段** `loss_ref.chunk(2)[1]`——这是因为双向流水线里，首 rank 处理的微批次在「反方向」上对应参考序列的后半。这正是「双向相向」在数值校验上的体现。
- 训练步设 `return_outputs=False` 是为了省显存（反向阶段不需要保留前向输出张量返回给用户），所以 `outputs` 恒为 `None`；只有显式要输出的推理步才会返回。

**预期结果**：你能用自己的话讲清「为什么分布式跑出来的 loss/梯度/输出，能和单进程参考结果在 `1e-13` 内对齐」。

#### 4.4.5 小练习与答案

**练习 1**：DualPipe 训练步里，中间 rank（既非首也非末）的 `loss` 和 `outputs` 分别是什么？

**参考答案**：都是 `None`。中间 rank 不负责汇总 loss（loss 只在持有真实标签的首末两端计算并返回），而训练步 `return_outputs=False` 使得所有 rank 的 `outputs` 都为 `None`。

**练习 2**：`dualpipe_model.step(...)` 的 `num_chunks` 参数和 4.3 节里的 `set_p2p_tensor_shapes` 有什么分工？

**参考答案**：`num_chunks` 告诉 DualPipe 把本 rank 的输入切成多少个微批次去流水（这里 20）；`set_p2p_tensor_shapes` 告诉底层**每个微批次在相邻阶段间收发时的张量形状**（这里是 `(3, 256, 512)`）。前者是「切几份」，后者是「每份长什么样」。

**练习 3**：为什么校验梯度用的是 `cal_diff(...) < 1e-13` 而不是 `torch.equal`？

**参考答案**：分布式浮点运算（不同 GPU、不同计算顺序、通信归约）难免产生极微小的数值差异，`torch.equal` 要求位级相同会因这些抖动误报失败；`cal_diff` 用余弦度量放宽到「方向几乎一致」，既容许浮点抖动，又能抓住真正的计算错误，是更合理的数值校验方式。

## 5. 综合实践

本讲的综合实践把前面的知识串成一条线。请根据你的环境二选一完成。

### 路线 A：有 GPU 环境——实跑并记录

**实践目标**：成功运行 DualPipeV 示例，确认它的自校验全部通过。

**操作步骤**：

1. 确认 PyTorch ≥ 2.0 且 `torch.cuda.is_available()` 为 `True`，完成 `pip install -e .`。
2. 补全会合环境变量（如 4.3.4 所述）：

   ```bash
   export MASTER_ADDR=localhost
   export MASTER_PORT=29500
   python examples/example_dualpipev.py
   ```

3. 记录控制台输出：首 rank 打印的配置行、是否出现任何 `AssertionError`。
4. 若有多张卡，再尝试 `python examples/example_dualpipe.py`，记录它打印了哪些 `pp_size` 配置（应为偶数递减）。

**需要观察的现象**：

- 每种 `pp_size` 配置下，脚本应「安静地」跑完——没有 assert 失败、没有异常堆栈，就代表 loss/梯度/输出三项校验全部通过。
- DualPipe 入口只对偶数 `pp_size` 触发；DualPipeV 对每个 `pp_size` 都触发。

**预期结果**：脚本退出码为 0，无 `AssertionError`。

> 「待本地验证」：实际输出取决于你的 GPU 数和 PyTorch/CUDA 版本，请如实记录；若出现 assert 失败，先检查种子、`CUBLAS_WORKSPACE_CONFIG` 是否按源码设置。

### 路线 B：无 GPU 环境——阅读型追踪

**实践目标**：把 `main(rank, pp_size)` 从 `init_process_group` 到 `step` 校验的完整调用链画出来。

**操作步骤**：

1. 打开 `examples/example_dualpipev.py`，按下表逐格填写每个阶段的「行号范围」和「做了什么」。第一行已示范。

   | 阶段 | 行号范围 | 做了什么 |
   |------|----------|----------|
   | 建进程组/绑设备/种子 | 111-115 | init_process_group + set_device + manual_seed |
   | 配置与 P2P 形状设置 | 117-124 | （自行填写）|
   | 建完整模型 | 127 | （自行填写）|
   | 建输入 + 参考实现 | 130-134 | （自行填写）|
   | 构造局部模块并包成 DualPipeV | 137-141 | （自行填写）|
   | 分配输入（仅首 rank） | 143-146 | （自行填写）|
   | 训练步 | 149 | （自行填写）|
   | loss 校验 | 152-156 | （自行填写）|
   | 梯度校验 | 159-161 | （自行填写）|
   | 推理步 + 输出校验 | 164-173 | （自行填写）|

2. 用一句话总结：DualPipeV 与 DualPipe 在「输入喂入方式」上的唯一代码差别在哪几行。

**需要观察的现象**：填表过程会强迫你把整条链路读一遍，建立「初始化 → 建模型 → 跑步 → 校验」的整体画面。

**预期结果**：完成表格后，你能不看源码复述 `main` 的执行顺序，并指出 DualPipeV 的输入分配只用了 [example_dualpipev.py:143-146](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L143-L146) 这 4 行，而 DualPipe 用了 [example_dualpipe.py:145-153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153) 这 9 行（首末两端各喂一半）。

## 6. 本讲小结

- DualPipe 的运行依赖是 **PyTorch 2.0+**，但 `setup.py` 没有把 PyTorch 写进 `install_requires`，需要自己先装好；版本号由 git 短哈希或时间戳自动生成。
- 示例用 `torch.multiprocessing.spawn` 拉起多进程，**进程数 = 流水线阶段数 = GPU 数**；DualPipe 入口对 GPU 数「向下取偶、步长 −2」，DualPipeV「步长 −1」任意数量。
- `main(rank, pp_size)` 的初始化链路：`init_process_group(backend='nccl', init_method='env://')` → 绑 GPU → 同一种子 `233` → 设 `CUBLAS_WORKSPACE_CONFIG` 保证数值确定性。
- 端到端校验是 DualPipe 示例的精髓：先用单进程 `ref_step` 生成「标准答案」，再跑分布式 `step`，最后用 `torch.equal`（loss/输出）和 `cal_diff < 1e-13`（梯度）三件套逐项核对。
- 「双向」与「V 型」在用户层面的直观差别：DualPipe 从首末两端各喂一半数据，DualPipeV 只从首端喂、末端做 V 型转折；模型规模上 DualPipeV 的 stage 数是 DualPipe 的两倍。

## 7. 下一步学习建议

本讲你已经能把示例跑起来（或至少读通整条调用链），并接触到了 `dualpipe/__init__.py` 导出的 `DualPipe`、`DualPipeV`、`set_p2p_tensor_shapes`、`set_p2p_tensor_dtype`、`WeightGradStore` 这几个名字。下一步建议：

1. **先读 u1-l3（目录结构与包导出）**：弄清 `dualpipe/comm.py`、`utils.py`、`dualpipe.py`、`dualpipev.py` 之间的 import 关系，知道本讲 import 进来的每个符号分别来自哪个文件。
2. **再进入 u2 公共基础设施**：从 u2-l2 开始搞懂 `set_p2p_tensor_shapes`/`set_p2p_tensor_dtype` 背后的 P2P 通信层 `comm.py`，以及 `WeightGradStore` 如何实现零气泡。
3. **动手前的准备**：如果你想做路线 A 的实跑，建议先在一个小规模（如 2 张卡）上验证，再扩大；同时确认 `MASTER_ADDR`/`MASTER_PORT` 环境变量已正确设置。
