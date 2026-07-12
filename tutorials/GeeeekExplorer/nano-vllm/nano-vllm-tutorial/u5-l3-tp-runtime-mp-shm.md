# 张量并行运行时：多进程与共享内存 IPC

## 1. 本讲目标

本讲解决一个工程问题：当 `tensor_parallel_size > 1` 时，nano-vllm 是怎样把**一张模型切到多张 GPU 上**、又怎样让多个进程**步调一致地跑同一次前向**的。

读完本讲你应该能够：

1. 说清楚 `LLMEngine.__init__` 用 `spawn` 拉起 worker 进程的拓扑：**rank 0 住在主进程里，rank 1..n-1 是被 spawn 出来的子进程**。
2. 画出一次 `call("run", ...)` 的完整时序：rank 0 把调用 pickle 进共享内存 → 用 `Event` 唤醒 worker → 各进程各自执行 `run` → 靠 NCCL collective 隐式同步。
3. 解释 `write_shm` / `read_shm` 这套「4 字节长度前缀 + pickle 载荷」的广播协议，以及为什么只用一个单向 `Event` 就够（不需要 worker 回 ack）。
4. 理解**只有 rank 0 负责采样**的设计动机：`ParallelLMHead` 的 `gather` 把 logits 汇聚到 rank 0，且 rank 0 就是引擎主进程，采样结果天然就在主进程里，无需再用 IPC 把结果传回来。

本讲是 u4-l5（张量并行线性层）的运行时落地：u4-l5 讲的是「权重怎么切」，本讲讲的是「切完之后多个进程怎么跑起来」。

## 2. 前置知识

在进入源码前，先用通俗语言过一遍本讲需要的几个底层概念。

- **进程 vs 线程**：进程有独立的内存空间，线程共享内存。Python 有 GIL（全局解释器锁），多线程没法真正并行跑 CPU 密集的 Python 代码；更要命的是，**每个 CUDA context（GPU 上下文）绑定在一个进程上**，多线程混用多个 GPU 容易出问题。所以张量并行（TP）几乎都采用「每张 GPU 一个进程」的多进程方案。
- **spawn vs fork**：Unix 下 `fork` 复制父进程内存，快但会连带把父进程的 CUDA 状态、锁、线程一起复制过去，极易出错；`spawn` 则是**全新启动一个 Python 解释器**，重新 import 模块、重新初始化，干净安全。nano-vllm 选 `spawn`。
- **NCCL**：NVIDIA 的 GPU 集合通信库，PyTorch 里以 `torch.distributed` 的 `nccl` 后端提供，支持 `all_reduce`、`gather` 等**张量级**通信。它只能传 GPU 张量，不能传 Python 对象。
- **共享内存（SharedMemory）**：多进程都能映射到各自地址空间的同一段物理内存，是进程间传大批字节数据的最快方式（不走 kernel 管道拷贝）。
- **Event（事件）**：`multiprocessing.Event` 是一个进程安全的布尔标志：`wait()` 阻塞直到标志被 `set()`，`clear()` 复位。本讲里它充当「数据已就绪」的唤醒信号。
- **控制面 vs 数据面**：本讲的一个关键直觉。NCCL 负责数据面（前向时真正的张量通信）；共享内存 + Event 负责控制面（告诉每个进程「现在该调哪个方法、参数是什么」）。两者分工，是因为 NCCL 传不了 Python 对象。

> 名词约定：本讲中 **rank** 指进程编号（0 到 `tp_size-1`），**world_size** 即 `tensor_parallel_size`（GPU/进程数）。rank 0 是主进程，rank > 0 是 worker 子进程。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们一起构成了 TP 运行时的全部骨架。

| 文件 | 关键方法 | 作用 |
| --- | --- | --- |
| `nanovllm/engine/llm_engine.py` | `LLMEngine.__init__` / `exit` / `step` | spawn 拉起 worker、注册退出钩子、在主循环里发起 `call("run", ...)` |
| `nanovllm/engine/model_runner.py` | `__init__` / `loop` / `call` / `write_shm` / `read_shm` / `run` / `exit` | NCCL 建网、共享内存创建、worker 主循环、调用分发、单 rank 采样 |

另外会引用 `nanovllm/engine/sequence.py` 的 `__getstate__` / `__setstate__`，因为它决定了 Sequence 对象通过共享内存广播时的体积（这是 u2-l1 已讲过的内容，本讲只复用结论）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1 进程拓扑与初始化**：`LLMEngine.__init__` 怎么 spawn worker、`ModelRunner.__init__` 怎么建 NCCL 网和共享内存。
2. **4.2 共享内存广播协议**：`write_shm` / `read_shm` 的 pickle 协议与 `Event` 同步。
3. **4.3 调用分发与 worker 主循环**：`call` 与 `loop` 如何把一次方法调用扇出到所有 rank。
4. **4.4 单 rank 采样与 TP 执行协同**：`run` 里 `rank == 0` 的分支，以及为什么不需要把结果再广播回去。

### 4.1 进程拓扑与初始化

#### 4.1.1 概念说明

张量并行的前向计算需要每个 rank 各自持有**自己那一份分片权重**，并在 `RowParallelLinear` 处做 `all_reduce`、在 `ParallelLMHead` 处做 `gather`。要让这些 collective 成立，必须先有一个「进程组」：所有 rank 互相知道彼此存在，并且各自绑定到自己的 GPU。

nano-vllm 的拓扑选择很特别：

- **rank 0 不是子进程，它就是引擎主进程本身**。`LLMEngine` 在主进程里直接 `ModelRunner(config, 0, ...)` 构造出 rank 0。
- **rank 1..n-1 才是 spawn 出来的子进程**，每个子进程的入口就是 `ModelRunner(config, i, event)`——也就是说 worker 进程一辈子都在 `ModelRunner.__init__` 里（因为 `__init__` 末尾调用了 `self.loop()`，会一直循环到收到 `"exit"`）。

这种「主进程即 rank 0」的设计有一个直接好处：**rank 0 采样出的 token 天然就在主进程里**，引擎拿去 `postprocess` 和解码输出即可，结果不需要再做一次跨进程 IPC（见 4.4）。

#### 4.1.2 核心流程

初始化时序（`world_size > 1` 时）：

```text
主进程 LLMEngine.__init__:
  1. spawn 出 rank 1..n-1 的子进程（target=ModelRunner）
     └─ 每个子进程: ModelRunner.__init__(config, i, event)
        ├─ dist.init_process_group("nccl", tcp://localhost:2333, ...)  # 阻塞，等所有 rank 会合
        ├─ torch.cuda.set_device(i)
        ├─ 建模型 / 加载权重 / warmup / 分配 KV cache / 捕获 CUDA Graph
        ├─ dist.barrier()                  # 等 rank 0 创建好共享内存
        ├─ 打开共享内存 "nanovllm"
        └─ self.loop()                     # 进入无限循环，直到 "exit"
  2. 主进程构造 rank 0: ModelRunner(config, 0, events)
     └─ 同样 init_process_group / 建模型 / ... 
        ├─ SharedMemory(name="nanovllm", create=True, size=2**20)  # rank 0 负责创建
        └─ dist.barrier()                  # 放行 worker
  3. 加载 tokenizer、写回 eos、构造 Scheduler
  4. atexit.register(self.exit)            # 注册退出清理
```

两个同步点值得注意：

- `dist.init_process_group` 是第一个同步点：所有 rank 必须都调用它，进程组才建得起来。所以 spawn 出来的 worker 一启动就阻塞在这里，等主进程构造 rank 0 并加入。
- `dist.barrier()` 是第二个同步点：它保证 **rank 0 先创建好共享内存**，worker 才去打开它（否则 worker 会找不到名字）。

#### 4.1.3 源码精读

先看 `LLMEngine.__init__` 中拉起 worker 的部分：

[llm_engine.py:22-31](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L22-L31) —— 用 `spawn` 上下文为每个非零 rank 创建一个 `Event` 和一个 `Process`，`target` 直接是 `ModelRunner` 类本身；rank 0 不 spawn，而是在主进程里直接构造，并传入**所有 worker 的 Event 列表**。

关键点：

- `ctx = mp.get_context("spawn")` 显式选 spawn，避免 fork 带来的 CUDA 状态污染。
- `ctx.Process(target=ModelRunner, args=(config, i, event))`：子进程启动后会执行 `ModelRunner(config, i, event)`，即调用 `__init__`，而 `__init__` 末尾的 `self.loop()` 让子进程就此常驻。
- `self.model_runner = ModelRunner(config, 0, self.events)`：rank 0 拿到的是 **Event 列表**（`self.events`），因为它要唤醒所有 worker；而每个 worker 拿到的是**单个 Event**。

再看退出清理：

[llm_engine.py:37-41](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L37-L41) —— `exit` 先对 rank 0 发一个 `call("exit")`（这会广播给所有 worker，让它们的 `loop` 跳出），再 `join` 回收每个子进程。`atexit.register(self.exit)` 保证解释器退出时也会清理。

然后是 `ModelRunner.__init__` 里建网与共享内存的部分：

[model_runner.py:26-27](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L26-L27) —— 以 `tcp://localhost:2333` 作为会合点（rendezvous）初始化 NCCL 进程组，并把当前进程绑定到 `rank` 号 GPU。所有 rank 必须连到同一个会合点才能互相发现。

[model_runner.py:41-48](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L41-L48) —— 共享内存的创建/打开分工：rank 0 用 `create=True` 建一块 `2**20`（1 MiB）的共享内存，再 `barrier` 放行；worker 先 `barrier` 等待，再以默认 `create=False` 打开同名内存，然后立刻进入 `self.loop()`。

注意两个硬编码：共享内存名 `"nanovllm"` 和会合端口 `2333`。这意味着**同一台机器上同时只能跑一个 nano-vllm 引擎实例**（否则名字/端口冲突），是极简实现的一个取舍。

#### 4.1.4 代码实践

**实践目标**：确认「rank 0 在主进程、worker 是子进程」的拓扑，并验证 spawn 会重新 import 模块。

**操作步骤**：

1. 阅读 [llm_engine.py:22-31](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L22-L31)，确认 `range(1, config.tensor_parallel_size)` —— 即 rank 0 不在循环里。
2. 在能跑的环境里用 `tensor_parallel_size=2` 启动一次推理（需要 2 张 GPU）。
3. 在另一个终端用 `nvidia-smi` 或 `ps -ef | grep python` 观察进程数：应该看到 1 个主进程 + 1 个 spawn 出来的子进程，各自占用一张 GPU。

**需要观察的现象**：主进程和子进程是两个独立的 Python 进程（PID 不同）；两张 GPU 上各有显存占用（权重分片 + KV cache）。

**预期结果**：进程数 == `tensor_parallel_size`，主进程即 rank 0。若只有单卡环境，本步标注「待本地验证」，改为纯源码阅读：在纸上画出 `__init__` 里谁 spawn 谁、谁在主进程。

#### 4.1.5 小练习与答案

**练习 1**：为什么 nano-vllm 用 `spawn` 而不是 `fork`？
**答案**：`fork` 会把父进程的 CUDA context、线程、锁一并复制到子进程，多 GPU 下极易死锁或崩溃；`spawn` 全新启动解释器、重新初始化 CUDA，干净安全。代价是子进程要重新 import 模块、重新建模型，启动慢一些，但推理引擎只启动一次，可接受。

**练习 2**：rank 0 为什么不也 spawn 出来，而要住在主进程里？
**答案**：因为引擎的 `step()` 在主进程里跑，`call("run", ...)` 的返回值（采样出的 token_ids）要直接被主进程拿去做 `postprocess` 和解码。rank 0 住在主进程，结果就天然在主进程，省掉一次「把结果从子进程传回主进程」的 IPC。

---

### 4.2 共享内存广播协议：write_shm / read_shm

#### 4.2.1 概念说明

进程组建好之后，每次推理主进程要告诉所有 worker：「现在请调用 `run`，参数是这批 `seqs` 和 `is_prefill`」。问题是 NCCL 只能传 GPU 张量，传不了 `list[Sequence]` 这种 Python 对象，更传不了方法名字符串。

nano-vllm 的解法是另开一条**控制面通道**：

- **共享内存**承载载荷：rank 0 把「方法名 + 参数」用 `pickle` 序列化成字节，写进共享内存。
- **Event** 承载唤醒信号：rank 0 写完后 `set` 每个 worker 的 Event，worker 从 `wait()` 醒来后去共享内存读取。

这是一套**单向广播**：rank 0 → 所有 worker。worker 读完后只 `clear` 自己的 Event，不回 ack。为什么不需要 ack？见 4.2.2 的同步分析。

#### 4.2.2 核心流程

共享内存的布局很简单——前 4 字节是长度前缀，后面是 pickle 字节流：

```text
偏移:   [0 .. 4)        [4 .. 4+n)
内容:   n (4字节小端)    pickle.dumps([method_name, *args]) 共 n 字节
```

一次广播的时序：

```text
rank 0 (write_shm)                      worker (read_shm)
─────────────────────                   ─────────────────────
data = pickle.dumps([method, *args])    event.wait()         # 阻塞等待
shm.buf[0:4] = len(data).to_bytes(4)    # 被 set 唤醒
shm.buf[4:4+n] = data                   n = int.from_bytes(shm.buf[0:4])
for ev in self.event: ev.set()  ──────► method, *args = pickle.loads(shm.buf[4:4+n])
                                        event.clear()
                                        return method, args
```

**为什么不需要 worker 回 ack？** 关键在于：rank 0 写完共享内存后，自己也会立刻进入 `run()`（见 4.3），而 `run()` 里的模型前向包含 NCCL collective（`all_reduce` / `gather`）。NCCL collective 是一个**隐式屏障**——rank 0 的 `all_reduce` 必须等所有 worker 也到达对应的 `all_reduce` 才能继续。

于是形成一条天然的保护链：

1. worker 从 `wait()` 醒来 → 读共享内存 → 进入 `run()` → 到达 `all_reduce`。
2. rank 0 写完共享内存 → 进入 `run()` → 到达 `all_reduce`。
3. rank 0 的 `run()` 要返回，必须先穿过所有 `all_reduce`，而这要求 worker 已经穿过读共享内存的阶段。

也就是说，**rank 0 不可能在 worker 还没读完共享内存时，就进入下一次 `write_shm` 覆盖掉数据**——因为下一次 `write_shm` 发生在下一次 `step()` 里，而那要求本次 `run()` 已经返回，本次 `run()` 返回又要求 worker 已经读过本次数据。NCCL collective 替我们做了「读完成」的同步。唯一的例外是最后的 `"exit"` 调用，它没有 collective，但它是最后一次调用，worker 读完就 `break` 了，覆盖与否都无所谓。

#### 4.2.3 源码精读

[model_runner.py:76-83](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L76-L83) —— `write_shm`：先 `assert` 只允许 rank 0 调用；用 `pickle.dumps` 把 `[method_name, *args]` 打包；把长度写成 4 字节小端整数放头部，载荷紧跟其后；最后遍历 `self.event`（worker Event 列表）逐个 `set()` 唤醒。

[model_runner.py:68-74](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L68-L74) —— `read_shm`：先 `assert` 只允许 `rank > 0` 调用；`event.wait()` 阻塞；从头部 4 字节读出长度 `n`；`pickle.loads` 反序列化 `buf[4:4+n]` 得到 `method_name` 和 `args`；`event.clear()` 复位后返回。

两个 assert 把协议方向钉死：写只能是 rank 0，读只能是 worker，避免误用。

**载荷体积约束**：共享内存固定 `2**20 = 1 MiB`。这意味着一次 `write_shm` 的 pickle 结果必须 < 1 MiB。这正是 `Sequence.__getstate__` 要做得那么精简的原因——见 [sequence.py:72-83](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L72-L83)：prefill 时只带整张 `token_ids`，decode 时只带 `last_token` 一个整数，丢弃采样参数等 worker 不需要的字段。一批序列 pickle 后必须塞进 1 MiB，等于间接约束了单步并发的序列数 × 单序列体积。

#### 4.2.4 代码实践

**实践目标**：用一段独立的最小脚本复现这套「共享内存 + Event」广播协议，在不依赖 GPU 的情况下直观看到 rank 0 写、worker 读的过程。

**操作步骤**：把下面这段「示例代码」存成 `demo_shm.py` 并运行（仅需标准库 `multiprocessing`）。

```python
# 示例代码：最小复现 nano-vllm 的 shm + Event 广播协议
import pickle
from multiprocessing import Process, Event, get_context
from multiprocessing.shared_memory import SharedMemory

SHM_SIZE = 1 << 20  # 与 nano-vllm 一致：1 MiB

def worker(event, rank):
    shm = SharedMemory(name="demo")          # 打开 rank0 已创建的共享内存
    while True:
        event.wait()                          # 阻塞，等 rank0 set
        n = int.from_bytes(shm.buf[0:4], "little")
        method_name, *args = pickle.loads(shm.buf[4:4+n])
        event.clear()
        print(f"[worker {rank}] 收到 {method_name}{args}")
        if method_name == "exit":
            break
    shm.close()

if __name__ == "__main__":
    ctx = get_context("spawn")
    event = ctx.Event()
    p = ctx.Process(target=worker, args=(event, 1))
    p.start()
    # rank0 负责创建共享内存
    shm = SharedMemory(name="demo", create=True, size=SHM_SIZE)
    for method, args in [("run", [1, 2, 3]), ("run", [4]), ("exit", [])]:
        data = pickle.dumps([method, *args])
        shm.buf[0:4] = len(data).to_bytes(4, "little")
        shm.buf[4:4+len(data)] = data
        event.set()                            # 唤醒 worker
        # 真实代码里这里会进入带 NCCL collective 的 run()，天然形成同步屏障
    p.join()
    shm.close()
    shm.unlink()
```

**需要观察的现象**：worker 依次打印出 `run [1,2,3]`、`run [4]`、`exit []`，且主进程在 `set()` 后继续往下走。

**预期结果**：worker 每次都在 `event.set()` 之后才打印一行，验证了「写→唤醒→读」的单向广播。若运行报 `FileExistsError`，是因为上一次 `unlink` 没执行干净，可换一个 `name` 再试。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `SHM_SIZE` 改成很小（比如 64 字节），这段示例会发生什么？对应到 nano-vllm 又会怎样？
**答案**：示例里 `pickle.dumps([1,2,3])` 的结果放不下会越界报错。对应到 nano-vllm，1 MiB 的上限要求一批 Sequence 的 pickle 体积不能超限，超限会写坏共享内存或被截断，所以 Sequence 的 `__getstate__` 必须尽量精简，调度器也间接受此约束。

**练习 2**：`write_shm` 里 `for event in self.event: event.set()` 为什么是遍历一个列表？`read_shm` 里的 `self.event` 又是什么？
**答案**：rank 0 持有**所有 worker 的 Event 列表**（构造时传入 `self.events`），所以要遍历 `set` 每一个。worker 持有的是**单个 Event**，`read_shm` 里 `self.event.wait()` 等的就是它自己那个。

---

### 4.3 调用分发与 worker 主循环：call / loop

#### 4.3.1 概念说明

有了广播协议，还需要一个「分发层」把一次方法调用同时扇出到所有 rank。`call` 就是这个分发层：rank 0 调 `call("run", seqs, is_prefill)` 时，它**既广播给 worker，又本地执行**；worker 的 `loop` 收到广播后也**本地执行**。最终所有 rank 同时在跑同一个方法，靠方法内部的 NCCL collective 对齐。

这种「广播调用 + 各自执行」的模式，等价于把一个普通的方法调用变成了「跨进程的同步方法调用」——调用方（rank 0）和被调用方（worker）执行同名方法，集合通信负责把它们黏在一起。

#### 4.3.2 核心流程

`call` 的逻辑极其简短，但包含了两个角色的分流：

```text
ModelRunner.call(method_name, *args):
  if world_size > 1 and rank == 0:
      write_shm(method_name, *args)     # 只广播一次（rank 0）
  method = getattr(self, method_name)
  return method(*args)                  # 所有 rank 都本地执行
```

worker 的 `loop` 则是一个「读 → 调 → 判退出」的无限循环：

```text
ModelRunner.loop():
  while True:
      method_name, args = read_shm()    # 阻塞等 rank 0 广播
      self.call(method_name, *args)     # 注意：worker 调 call 时不会再 write_shm
      if method_name == "exit":
          break
```

注意 worker 在 `loop` 里也走 `call`，但因为 `call` 里的广播条件是 `rank == 0`，worker 不会二次广播——它只是用 `call` 来统一「getattr + 执行」这一步。这是一个很巧的复用：`call` 同时是入口（被引擎调）和中继（被 worker loop 调），靠 `rank == 0` 守卫避免广播风暴。

#### 4.3.3 源码精读

[model_runner.py:85-89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L85-L89) —— `call`：rank 0 先 `write_shm` 广播，然后所有 rank 统一用 `getattr(self, method_name)` 取到方法并执行。返回值只有 rank 0 的会被引擎用到。

[model_runner.py:61-66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L61-L66) —— `loop`：worker 的整个生命周期就在这个循环里——`read_shm` 拿到方法和参数，`call` 本地执行，遇到 `"exit"` 才 `break` 跳出、进而从 `__init__` 返回、进程结束。

再看引擎侧怎么发起调用：

[llm_engine.py:49-55](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L49-L55) —— `step` 里 `token_ids = self.model_runner.call("run", seqs, is_prefill)`：引擎只跟 rank 0 打交道，这一行既触发广播又触发本地前向，返回的就是 rank 0 采样出的 token_ids。

#### 4.3.4 代码实践

**实践目标**：把一次 `call("run", ...)` 的完整时序画成时序图，固化对「广播 + 各自执行 + NCCL 同步」的理解。

**操作步骤**：

1. 从 [llm_engine.py:52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L52) 的 `self.model_runner.call("run", seqs, is_prefill)` 出发。
2. 进入 [model_runner.py:85-89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L85-L89) 的 `call`：rank 0 调 `write_shm`（[76-83](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L76-L83)）广播，然后所有 rank 进入 `self.run(...)`。
3. 同时 worker 侧从 [model_runner.py:61-66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L61-L66) 的 `loop` 经 `read_shm`（[68-74](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L68-L74)）拿到同样的 `("run", [seqs, is_prefill])`，再 `call` 进入 `run`。
4. 在 `run` 内部标出 NCCL collective 出现的位置（`RowParallelLinear` 的 `all_reduce`、`compute_logits` 的 `gather`）。

**需要观察的现象**：rank 0 的 `write_shm` 与 worker 的 `read_shm` 是配对的；两边进入 `run` 后，第一次同步发生在模型前向里的第一个 `all_reduce`。

**预期结果**：得到一张两列时序图（rank 0 / worker），控制面（shm+Event）在上方一次往返，数据面（NCCL collective）在前向里多次往返。

#### 4.3.5 小练习与答案

**练习 1**：worker 在 `loop` 里也调 `call`，为什么不会引发「worker 再广播给其它 worker」的连锁反应？
**答案**：`call` 里的广播被 `if self.world_size > 1 and self.rank == 0` 守卫，只有 rank 0 会 `write_shm`。worker 调 `call` 时 `rank != 0`，直接跳过广播，只执行 `getattr + method(*args)`。

**练习 2**：引擎调用 `call("exit")` 时，worker 的 `loop` 会怎样？
**答案**：rank 0 `write_shm("exit")` 广播并本地执行 `self.exit()`；worker `read_shm` 拿到 `"exit"`，`call` 执行 `self.exit()`，随后 `if method_name == "exit": break` 跳出循环，进程结束。

---

### 4.4 单 rank 采样与 TP 执行协同：run

#### 4.4.1 概念说明

前面三节搭好了「怎么把调用送到每个 rank」的管道，本节看管道里跑的最重要的一次调用——`run`。`run` 做三件事：准备输入张量、跑模型前向、采样出 token。

TP 下的关键设计是：**只有 rank 0 采样**。原因有二，都来自 u4-l5：

1. `ParallelLMHead`（LM 头）用 `gather` 把各 rank 的词表分片 logits **汇聚到 rank 0**。也就是说前向结束后，**完整的词表 logits 只在 rank 0 上有意义**，worker 上的 logits 是残缺的。
2. 采样要在完整词表上做 softmax/argmax，自然只能在 rank 0 上做。

加上 4.1 提到的「rank 0 就是主进程」，rank 0 采样出的 token_ids 直接就是 `call` 的返回值，引擎拿来 `postprocess` 即可——**结果无需再做跨进程传输**。worker 侧 `run` 返回 `None`，`loop` 也不关心返回值。

#### 4.4.2 核心流程

`run` 在两个 rank 上的分支差异（伪代码）：

```text
ModelRunner.run(seqs, is_prefill):
  input_ids, positions = prepare_prefill(seqs) if is_prefill else prepare_decode(seqs)
  temperatures = prepare_sample(seqs) if rank == 0 else None     # 只 rank0 准备温度
  logits = run_model(input_ids, positions, is_prefill)            # 所有 rank 都跑前向
            └─ 前向内: RowParallelLinear.all_reduce  (所有 rank 同步)
            └─ compute_logits: ParallelLMHead.gather (logits 汇到 rank0)
  token_ids = sampler(logits, temperatures).tolist() if rank == 0 else None  # 只 rank0 采样
  reset_context()
  return token_ids                                                  # worker 返回 None
```

注意 `run_model` 是**所有 rank 都执行**的——权重分片必须各自跑自己的前向，再靠 `all_reduce`/`gather` 合并。采样才是 rank 0 独占。

#### 4.4.3 源码精读

[model_runner.py:214-220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220) —— `run`：第 216 行 `prepare_sample` 只在 `rank == 0` 时调用；第 217 行 `run_model` 所有 rank 都跑；第 218 行 `sampler` 只在 `rank == 0` 时调用，worker 走 `else None`。

这里有几个值得品味的细节：

- `prepare_sample` 会构造一个 `temperatures` 张量并 `.cuda()`。worker 跳过它，省下一块显存和一次 H2D 拷贝。
- `sampler` 是个 `@torch.compile` 的 GPU 内核（u5-l2 讲过），只在 rank 0 上跑，因此**采样全程不涉及跨进程通信**。
- `reset_context()` 所有 rank 都调，保证每步 `Context` 不留残留（u4-l3 讲过）。
- worker 的 `run` 返回 `None`，`loop` 里 `self.call(method_name, *args)` 的返回值被丢弃——worker 不需要结果，它只需要「陪着 rank 0 跑完前向、对齐 NCCL collective」。

再呼应一下 4.2.2 的同步论证：正是因为 `run` 里**每次都有 NCCL collective**（`all_reduce`/`gather`），rank 0 的这一次 `run` 不返回，worker 就没法进入下一轮 `read_shm`，从而共享内存不会被提前覆盖。`run` 既是计算，也是隐式屏障。

#### 4.4.4 代码实践

**实践目标**：验证「只有 rank 0 采样」——在多 rank 下确认 worker 不持有完整的 `temperatures` 和采样结果。

**操作步骤**（运行型，需 2 张 GPU；单卡环境改为源码阅读型）：

1. 用 `tensor_parallel_size=2` 跑一次 `example.py`。
2. 阅读 [model_runner.py:216-218](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L216-L218)，理解三个 `if self.rank == 0` 分支。
3. （可选，需临时改源码）在 `run` 里给 `temperatures` 和 `token_ids` 各加一行 `print(self.rank, ...)`，重新跑一次，观察只有 rank 0 打印出非 None 值。

**需要观察的现象**：rank 0 打印出温度张量和采样出的 token_ids；rank 1 对应位置是 `None`。

**预期结果**：采样集中发生在 rank 0，worker 只参与前向的 collective。若不便于改源码运行，则标注「待本地验证」，改为纯阅读：在 [run](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220) 的三个 `rank == 0` 判断处各画一个标注，说明 worker 走 `else` 分支。

#### 4.4.5 小练习与答案

**练习 1**：如果把采样也改成「所有 rank 都采样」，会有什么问题？
**答案**：worker 上的 logits 经过 `gather` 后是残缺的（只有自己那部分词表），直接采样会得到错误 token；而且所有 rank 都采样会重复计算、浪费算力。集中到 rank 0 既正确又省算力，还顺带把结果留在主进程。

**练习 2**：为什么 worker 的 `run` 返回 `None` 不会让引擎出错？
**答案**：引擎只取 rank 0 的返回值（`self.model_runner` 就是 rank 0）。worker 的 `run` 是被 `loop` 里的 `self.call(...)` 调用的，`loop` 不使用返回值，所以 `None` 被直接丢弃。

**练习 3**：`run` 里如果不调用 `reset_context()`，多步推理会出什么问题？
**答案**：`Context` 是模块级全局，上一步的 `slot_mapping`/`block_tables` 等会残留到下一步，导致 Attention 读到错误的历史 KV 位置。`reset_context` 保证每步开始前 Context 是干净的（详见 u4-l3）。

## 5. 综合实践

**任务**：用 `tensor_parallel_size=2` 跑一次推理，画出**一次 `run` 调用从 rank 0 `write_shm` 广播到各 worker `loop` 执行、再到 NCCL 协同、最后 rank 0 采样返回**的完整时序图。

**建议步骤**：

1. 通读以下五处源码，确认每个角色的入口：
   - 引擎发起：[llm_engine.py:52](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L52)
   - 分发：[model_runner.py:85-89](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L85-L89)
   - 广播：[model_runner.py:76-83](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L76-L83)
   - 接收：[model_runner.py:61-74](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L61-L74)
   - 执行与采样：[model_runner.py:214-220](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L214-L220)
2. 画一张两列时序图（左 rank 0 / 主进程，右 worker rank 1），按时间从上到下标出：`call("run")` → `write_shm` → `event.set` →（worker）`event.wait` 返回 → `read_shm` → 两边进入 `run` → `prepare_*` → `run_model` 内的 `all_reduce`/`gather` → rank 0 `sampler` → 返回 token_ids。
3. 在图上用虚线标出两处同步：① Event 唤醒（控制面），② NCCL collective（数据面）。
4. 在图下写一句话：为什么 rank 0 不会在 worker 读完共享内存前就覆盖它（用 4.2.2 的隐式屏障论证回答）。

**预期产出**：一张时序图 + 一句同步论证。若没有 2 卡环境，可只做源码阅读部分，运行部分标注「待本地验证」。

## 6. 本讲小结

- nano-vllm 的 TP 拓扑是「**rank 0 住主进程 + spawn 出 rank 1..n-1 worker**」，用 `spawn` 上下文避免 fork 的 CUDA 污染；worker 进程的入口就是 `ModelRunner.__init__`，末尾 `self.loop()` 让它常驻。
- 初始化有两个同步点：`dist.init_process_group`（NCCL 会合，端口 `2333`）和 `dist.barrier()`（保证 rank 0 先建好名为 `"nanovllm"` 的 1 MiB 共享内存）。
- 控制面走「**共享内存 + Event**」：rank 0 把 `[方法名, *参数]` pickle 进共享内存（前 4 字节长度前缀），`set` 所有 worker 的 Event；worker `wait` 醒来后读取并 `clear`。NCCL 只留给数据面的张量通信。
- `call` 是分发层：rank 0 广播一次后所有 rank 各自 `getattr + 执行`；worker 的 `loop` 是「读 → 调 → 判退出」的无限循环，靠 `rank == 0` 守卫避免二次广播。
- **只有 rank 0 采样**：`gather` 把 logits 汇到 rank 0，采样只能在 rank 0 做；又因 rank 0 就是主进程，采样结果天然回到引擎，无需结果回传 IPC。
- `run` 内部的 NCCL collective 既是计算也是**隐式屏障**——它保证 rank 0 不会在 worker 读完共享内存前进入下一次 `write_shm`，所以单向 Event 无需 ack。

## 7. 下一步学习建议

- 本讲把 TP 的「运行时」讲完了，但「权重怎么从磁盘装进各 rank 的分片」还没展开。建议接着读 u5-l4「safetensors 权重加载与 packed_modules_mapping」，看 `load_model` 如何配合各并行层的 `weight_loader` 把 HF 权重写到正确的分片位置。
- 若想验证本讲的时序图，可在有 2 张 GPU 的机器上用 `tensor_parallel_size=2` 跑 `example.py`，并对照 `nvidia-smi` 观察进程与显存。
- 进阶可思考：如果把硬编码的共享内存名 `"nanovllm"` 和端口 `2333` 改成可配置，需要改动哪些地方？这是把 nano-vllm 从「单实例」推向「同机多实例」的起点。
