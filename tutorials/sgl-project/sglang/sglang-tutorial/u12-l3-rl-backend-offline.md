# RL 后端与离线批量推理

## 1. 本讲目标

学完本讲，你应该能够：

- 理解 SGLang 作为一个**进程内推理引擎**在强化学习（RL）训练后端中的角色：它不是「训练框架」，而是负责**高效 rollout（采样生成）+ 权重热更新**的推理骨干。
- 掌握 SGLang `Engine` 的两种离线推理入口：同步 `generate`（一把提交一个批）与异步 `async_generate`（并发提交、靠连续批处理动态拼批）。
- 读懂 `EngineBase` 定义的统一接口族，重点掌握 RL 场景下最常用的**权重热更新**接口（`update_weights_from_tensor` / `update_weights_from_distributed` / `update_weights_from_disk` 等），并能描述一次「rollout → 训练 → 更新权重 → 再 rollout」的完整调用流程。
- 注意一个贯穿本讲的工程事实：`Engine` 内部读取运行期配置已从直接读 `self.server_args` 迁移到 `runtime_context` 的命名空间访问器（如 `get_parallel()`），`server_args` 退化为只读留档；这一点与 [u2-l5 RuntimeContext](u2-l5-runtime-context-config-bags.md) 一脉相承。

---

## 2. 前置知识

本讲面向 `advanced` 阶段读者，建议先具备以下基础（对应前置讲义）：

- **进程内 Engine 入口**：知道 `sgl.Engine(...)` 与 `sglang serve` 共享同一套引擎，都会拉起 TokenizerManager（主进程）+ Scheduler + DetokenizerManager（子进程）三进程环。详见 [u1-l4 两种使用入口](u1-l4-server-vs-engine.md)。
- **一次前向的内部链路**：知道 `ScheduleBatch → ForwardBatch → ModelRunner.forward` 是怎么把一批请求算出 logits 的。详见 [u5-l1 ModelRunner 与前向执行路径](u5-l1-model-runner-forward.md)。
- **配置命名空间袋**：知道运行期配置已迁移到 `runtime_context`，读用 `get_parallel()`/`get_exec()` 等访问器，改写一律走 `get_context().override()`。详见 [u2-l5 RuntimeContext 与配置命名空间](u2-l5-runtime-context-config-bags.md)。

几个需要先澄清的术语：

- **rollout**：RL 训练中，用**当前策略模型**对一批 prompt 采样生成响应（response）的过程。rollout 是 SGLang 作为 RL 后端的核心职责。
- **权重热更新（weight hot-update）**：训练框架每更新一步梯度得到新权重后，需要把这些新权重**灌进已经在运行的推理引擎**，而不重启进程。SGLang 提供了一族 `update_weights_*` 接口完成这件事。
- **异步推理（async generation）**：用 `async/await` 提交若干条请求，让它们**并发**进入调度器的连续批处理（continuous batching），而不是一条一条排队。这对模拟在线流量、混合长短请求很有用。
- **NCCL 进程组（process group）**：多 GPU 之间做集合通信（如 broadcast/all-reduce）的通信域。RL 的「分布式权重更新」就是靠在训练侧 worker 和 SGLang 侧 worker 之间建一个 NCCL 组，把新权重 broadcast 过来。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/sglang/srt/entrypoints/EngineBase.py` | 引擎抽象基类，定义 `generate`/`flush_cache`/`update_weights_from_tensor`/`release_memory_occupation`/`shutdown` 等统一接口，是 HTTP 引擎与进程内 `Engine` API 同构的根源。 |
| `python/sglang/srt/entrypoints/engine.py` | `Engine` 类——进程内推理引擎入口，含 `generate`/`async_generate`、子进程拉起、以及一整套 RL 权重更新接口。 |
| `examples/runtime/engine/offline_batch_inference.py` | **同步**离线批量推理示例：`sgl.Engine` + `llm.generate(prompts, sampling_params)`。 |
| `examples/runtime/engine/offline_batch_inference_async.py` | **异步**离线批量推理示例：`asyncio` + `engine.async_generate`，模拟在线式并发生成。 |
| `python/sglang/srt/runtime_context.py` | 运行期配置中枢，提供 `get_parallel()`、`publish()`、`get_context()` 等访问器，是本讲中 `Engine` 读取配置的新通道。 |

---

## 4. 核心概念与源码讲解

### 4.1 Engine 异步接口：generate 与 async_generate

#### 4.1.1 概念说明

`Engine` 同时提供**同步**与**异步**两套生成接口，二者背后调用的是**同一个** `tokenizer_manager.generate_request(obj, None)` 异步生成器，区别只在外层如何驱动它：

- `generate(...)`：用 `self.loop.run_until_complete(...)` 把异步生成器**阻塞式地**跑完一轮，适合「脚本式」的离线批量推理（来一批、出一批）。
- `async_generate(...)`：原生 `async`，`await generator.__anext__()`，返回一个可被 `asyncio` 并发调度的协程，适合**并发**提交大量请求、模拟在线流量，或与训练循环里的其他异步任务交错。

关键直觉：

- 离线同步批量推理（`llm.generate(prompts, sp)`）是把**一整批 prompt 当作一个请求对象**一次提交，引擎内部本来就是连续批处理，所以这一把是「批友好」的，吞吐高、写法最简单。
- 异步推理（`async_generate`）是把**每条 prompt 当作独立请求**用 `asyncio.create_task` 并发提交，请求会在运行期**陆续到达**调度器，由调度器动态拼批。它的价值不在于「比同步更快」，而在于**贴近在线服务的真实到达模式**，以及能在生成期间穿插别的事（比如边生成边收集）。

#### 4.1.2 核心流程

`generate` 的内部流程可以概括为三步：

```text
1. _resolve_routed_dp_rank()        # 解析数据并行路由 rank（DP>1 时选目标 worker）
2. GenerateReqInput(...)             # 把入参打包成进程内请求对象（不上 ZMQ 环的那一层）
3. tokenizer_manager.generate_request(obj, None)
      └── stream=True  → 用 loop.run_until_complete 逐 chunk yield（生成器）
      └── stream=False → run_until_complete 取第一个 chunk 即最终结果
```

`async_generate` 的流程完全对称，只是第 3 步直接 `await` 而不经过 `run_until_complete`：

```text
3. tokenizer_manager.generate_request(obj, None)
      └── stream=True  → 直接返回异步生成器
      └── stream=False → await generator.__anext__()
```

两者都先调 `_resolve_routed_dp_rank` 解析「这条请求路由到哪个 DP worker」，这里有一个值得注意的迁移细节：它读取 `dp_size` 时**不再读 `self.server_args.dp_size`**，而是走命名空间访问器 `get_parallel().dp_size`。

#### 4.1.3 源码精读

`generate` 把入参打包成 `GenerateReqInput` 并驱动异步生成器：

[python/sglang/srt/entrypoints/engine.py:319-416](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L319-L416) —— `generate` 用 `GenerateReqInput` 打包请求，再用 `self.loop.run_until_complete` 阻塞地把 `tokenizer_manager.generate_request` 跑完（流式则逐 chunk `yield`）。

`async_generate` 与之同构，但用原生 `await`：

[python/sglang/srt/entrypoints/engine.py:418-510](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L418-L510) —— `async_generate` 返回 `AsyncIterator`，非流式时 `await generator.__anext__()` 直接拿到结果，可被 `asyncio` 并发调度。

`_resolve_routed_dp_rank` 展示了配置读取的新通道 `get_parallel().dp_size`，并对越界 rank 报错：

[python/sglang/srt/entrypoints/engine.py:304-317](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L304-L317) —— 解析 `routed_dp_rank`，用 `get_parallel().dp_size` 做范围校验（`0 <= routed_dp_rank < dp_size`），`dp_size<=1` 时忽略路由。

#### 4.1.4 代码实践

**实践目标**：体会同步与异步两种提交方式在「请求到达模式」上的差异。

**操作步骤**：

1. 用一个小模型（如 `Qwen/Qwen2.5-0.5B-Instruct`）启动一个 `Engine`。
2. 准备 100 条相同 prompt。
3. 分别用下面两种方式跑：
   - 同步：`llm.generate(prompts, sampling_params)`（参考 4.2 的示例）。
   - 异步：参考 `offline_batch_inference_async.py`，用 `asyncio.create_task` 为每条 prompt 建任务并发 `await`。

**需要观察的现象**：

- 两种方式最终都能拿到全部生成结果。
- 异步方式可以在「第一条还没生成完」时就把后续请求提交进调度器，请求的 `rid` 会陆续进入系统。

**预期结果**：异步方式更贴近在线服务的到达过程；同步一把批量在离线场景下通常总吞吐相当甚至略高（因为没有协程调度开销）。**待本地验证**：具体延迟与吞吐数字取决于 GPU 与模型，请在你本地实测。

#### 4.1.5 小练习与答案

**练习 1**：`generate` 和 `async_generate` 在 `stream=False` 时，返回结果前分别用什么手段拿到「第一个 chunk」？

> **答案**：`generate` 用 `self.loop.run_until_complete(generator.__anext__())` 阻塞获取；`async_generate` 用 `await generator.__anext__()` 非阻塞地（在协程内）获取。两者底层都是 `tokenizer_manager.generate_request` 这个异步生成器。

**练习 2**：为什么 `routed_dp_rank` 越界要报 `ValueError`，而 `dp_size<=1 且 routed_dp_rank==0` 只是 `logger.debug` 后返回 `None`？

> **答案**：`routed_dp_rank >= dp_size` 或 `<0` 是**真实错误**（路由到一个不存在的 worker），必须显式失败；而 `dp_size==1` 时只有单 worker，`routed_dp_rank==0` 是**无意义的冗余指定**，安全地降级为「不路由」即可，所以只记 debug 日志并返回 `None`。

---

### 4.2 离线批量推理：同步 vs 异步两种范式

#### 4.2.1 概念说明

「离线批量推理（offline batch inference）」指**不在 HTTP 服务里跑**，而是直接在 Python 脚本里 `import sglang as sgl`、构造 `sgl.Engine`、喂一批 prompt、收结果。它省去了 HTTP/uvicorn 那一层，是评测、数据集生成、RL rollout 最常用的形态（见 [u1-l4](u1-l4-server-vs-engine.md) 的两种入口对比）。

SGLang 给出两个对照示例：

- `offline_batch_inference.py`：**同步**。`llm.generate(prompts, sampling_params)` 一次提交全部 prompt，返回与 `prompts` 等长的结果列表。
- `offline_batch_inference_async.py`：**异步**。把每条 prompt 包成 `asyncio.create_task`，用 `async_generate` 并发提交。

两者的本质差别在第 4.1 节已说明：同步是一把批量（批友好），异步是逐条并发到达（在线友好）。

#### 4.2.2 核心流程

同步离线推理（最短可用代码）：

```text
server_args = ServerArgs.from_cli_args(args)
llm = sgl.Engine(**dataclasses.asdict(server_args))
outputs = llm.generate(prompts, sampling_params)   # 一把批量，阻塞返回
for prompt, output in zip(prompts, outputs):
    print(output["text"])
```

异步离线推理（关键骨架）：

```text
class InferenceEngine:
    def __init__(self, **kwargs): self.engine = sgl.Engine(**kwargs)
    async def generate(self, prompt, sp):
        return await self.engine.async_generate(prompt, sp)

inference = InferenceEngine(**asdict(server_args))
tasks = [asyncio.create_task(inference.generate(p, sp)) for p in prompts]  # 并发提交
for task in tasks:                                                          # 逐个收
    await task
    print(task.result()["text"])
```

值得注意的工程细节：示例脚本都带 `if __name__ == "__main__":` 守卫。这是因为在多进程 `spawn` 模式下，`sgl.Engine` 会派生子进程；若没有 `__main__` 守卫，子进程会重新执行模块顶层代码，导致**无限递归地不断 spawn**。这一点在 [u1-l4](u1-l4-server-vs-engine.md) 已强调过，这里再次提醒。

#### 4.2.3 源码精读

同步示例——构造 `Engine` 后一把 `generate`，返回结果列表：

[examples/runtime/engine/offline_batch_inference.py:27-33](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/examples/runtime/engine/offline_batch_inference.py#L27-L33) —— `sgl.Engine(**dataclasses.asdict(server_args))` 构造引擎，`llm.generate(prompts, sampling_params)` 一次批量推理，`output['text']` 取生成文本。

异步示例——`InferenceEngine` 封装 + `asyncio.create_task` 并发：

[examples/runtime/engine/offline_batch_inference_async.py:19-57](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/examples/runtime/engine/offline_batch_inference_async.py#L19-L57) —— `InferenceEngine.generate` 用 `await self.engine.async_generate(...)`；`run_server` 为每条 prompt 建任务并发，最后逐个 `await task` 收集结果。

> 说明：`offline_batch_inference_async.py` 里收结果的循环用 `while True: if task.done(): ...` 轮询，这是**示例代码**的写法，仅为演示；工程里直接 `await task` 然后 `task.result()` 即可。

#### 4.2.4 代码实践

**实践目标**：用两种范式跑同一批 prompt，对比写法与请求进入调度器的节奏。

**操作步骤**：

1. 安装 SGLang（参考 [u1-l2](u1-l2-install-and-first-run.md)）。
2. 跑同步版：
   ```bash
   python3 examples/runtime/engine/offline_batch_inference.py \
     --model-path <小模型>
   ```
3. 跑异步版：
   ```bash
   python3 examples/runtime/engine/offline_batch_inference_async.py \
     --model-path <小模型>
   ```

**需要观察的现象**：两个脚本都打印出每条 prompt 的 `Generated text`；异步版的 prompt 列表被 `* 100` 放大（共 400 条），更能体现并发到达。

**预期结果**：都能得到完整生成结果。**待本地验证**：耗时与吞吐请以你本地 GPU 实测为准。

#### 4.2.5 小练习与答案

**练习 1**：把同步示例里的 `prompts` 从 4 条改成 400 条，`llm.generate` 的返回值长度会变成多少？为什么不需要改调用方式？

> **答案**：返回 400 条。因为 `generate` 接受「单条或列表」，传列表就是批量；引擎内部本来就支持连续批处理，调用方无需关心分批细节。

**练习 2**：为什么两个示例都必须放在 `if __name__ == "__main__":` 里？

> **答案**：`sgl.Engine` 在 `spawn` 多进程模式下会重新执行模块顶层代码来启动子进程；没有 `__main__` 守卫会形成「顶层构造 Engine → 子进程又执行顶层 → 再构造 Engine」的无限递归 spawn。守卫确保只有主进程入口才构造引擎。

---

### 4.3 RL 权重热更新：update_weights 接口族与一次 rollout 流程

#### 4.3.1 概念说明

RL 训练（如 PPO/GRPO）的每一轮大致是：

```text
rollout（用当前权重采样响应）→ 算 reward → 算 advantage → 反向传播更新权重 → 【把新权重同步到推理引擎】→ 下一轮 rollout
```

最朴素的做法是每轮重启 `sgl.Engine`，但重启要重新加载模型、重建 KV 缓存，开销巨大。SGLang 提供**权重热更新**接口，让推理引擎**不重启**地换上新权重。这正是 `EngineBase` 把 `update_weights_from_tensor` 列为抽象方法、`Engine` 提供一整套 `update_weights_*` 的根本原因。

SGLang 的权重更新接口族按「权重从哪里来」分为四类：

| 接口 | 权重来源 | 典型场景 |
| --- | --- | --- |
| `update_weights_from_tensor` | 进程内/经序列化的张量（`named_tensors`） | 训练框架直接把 GPU 张量 push 过来；veRL/OpenRLHF 的常用路径 |
| `update_weights_from_distributed` | 通过 NCCL 进程组 broadcast | 训练侧与推理侧建 NCCL 组，分布式传权重 |
| `update_weights_from_disk` | 磁盘上的检查点（`model_path`） | 训练保存了新 checkpoint，引擎重新加载 |
| `update_weights_from_ipc` | ZMQ IPC 句柄 | 与 checkpoint-engine 集成 |

配套接口还有：`init_weights_update_group` / `destroy_weights_update_group`（建/拆 NCCL 组）、`get_weights_by_name`（读取某参数当前值，常用于校验更新是否生效）、以及 `flush_cache`（更新后是否清空 KV 缓存）。

一个关键语义：**权重一变，旧的 KV 缓存就失效了**（旧 KV 是用旧权重算出来的）。所以默认 `flush_cache=True`，每次更新后清缓存；若你确知要连续多次更新、不想重复清缓存，可设 `flush_cache=False`，最后一次再清。

#### 4.3.2 核心流程

一次「rollout → 训练 → 更新权重」的典型调用流程（以张量直传为例）：

```text
# 0. 启动引擎（加载初始策略权重）
engine = sgl.Engine(model_path="<initial_policy>")

# 1. rollout：用当前权重采样
responses = engine.generate(prompts, sampling_params)

# 2. 训练框架侧：算 reward、反向传播，得到新权重 named_tensors
#    （这一步在 torch 训练循环里，不在 SGLang）

# 3. 热更新：把新权重灌进引擎（默认清缓存）
engine.update_weights_from_tensor(named_tensors)   # 或 from_distributed / from_disk

# 4. （可选）校验
engine.get_weights_by_name("model.layers.0...")

# 5. 下一轮 rollout，用的是新权重
responses2 = engine.generate(prompts, sampling_params)
```

「分布式更新」路径多一步建立 NCCL 组：

```text
engine.init_weights_update_group(master_address, master_port, rank_offset,
                                 world_size, group_name)          # 建 NCCL 组
engine.update_weights_from_distributed(names, dtypes, shapes,
                                       group_name=group_name)     # broadcast 权重
engine.destroy_weights_update_group(group_name)                   # 用完拆组
```

这些接口在 `Engine` 里都是**薄包装**：把参数打包成 `*ReqInput` 结构体，转交给 `tokenizer_manager` 的同名异步方法，再用 `self.loop.run_until_complete` 阻塞等待——和 `generate` 的同步包装套路完全一致。最终真正的换权重动作发生在 Scheduler 子进程的 ModelRunner/worker 里（见 [u5-l1](u5-l1-model-runner-forward.md)）。

#### 4.3.3 源码精读

`EngineBase` 把权重更新列为统一抽象，HTTP 引擎与进程内 `Engine` 共用同一契约：

[python/sglang/srt/entrypoints/EngineBase.py:46-54](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/EngineBase.py#L46-L54) —— `update_weights_from_tensor` 抽象方法签名：接收 `named_tensors: List[Tuple[str, torch.Tensor]]`，可选 `load_format` 与 `flush_cache`。

[python/sglang/srt/entrypoints/EngineBase.py:64-72](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/EngineBase.py#L64-L72) —— RL 训练中常配对使用的 `release_memory_occupation` / `resume_memory_occupation`，用于在「不 rollout 的阶段」临时释放 GPU 显存给训练侧。

`Engine` 里建/拆 NCCL 组（分布式更新前置）：

[python/sglang/srt/entrypoints/engine.py:1026-1058](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L1026-L1058) —— `init_weights_update_group`/`destroy_weights_update_group` 把 NCCL 组参数打包成 `InitWeightsUpdateGroupReqInput`，转交 tokenizer_manager。

分布式与张量两种更新主路径：

[python/sglang/srt/entrypoints/engine.py:1060-1109](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L1060-L1109) —— `update_weights_from_distributed`（带 NCCL 组名）与 `update_weights_from_tensor`（直接传张量）。注意张量分支会按 `tp_size` 复制序列化结果给每个 TP worker；其中 `self.server_args.tp_size` 仍是当前代码里少数直接读 `server_args` 的遗留点（迁移尚未覆盖此处，实际读取请以你本地 HEAD 为准）。

磁盘与 IPC 两种更新路径 + 读取校验：

[python/sglang/srt/entrypoints/engine.py:1111-1150](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L1111-L1150) —— `update_weights_from_disk`（按 `model_path` 原地重载）、`update_weights_from_ipc`（ZMQ 句柄，配合 checkpoint-engine）、`get_weights_by_name`（读回参数当前值用于校验）。

#### 4.3.4 代码实践

**实践目标**：用 `offline_batch_inference_async.py` 跑一批 rollout，再定位 RL 场景的权重更新接口，描述一次 rollout 更新权重的调用流程。

**操作步骤**：

1. 跑一批离线推理当作「rollout」：
   ```bash
   python3 examples/runtime/engine/offline_batch_inference_async.py \
     --model-path <小模型>
   ```
2. 在 `EngineBase.py` 中找到 RL 常用的权重更新抽象方法 `update_weights_from_tensor`，并记录它的参数（`named_tensors`、`load_format`、`flush_cache`）。
3. 在 `engine.py` 中找到它的实现（4.3.3 已给行号），以及 `update_weights_from_distributed`、`update_weights_from_disk`、`init_weights_update_group`。
4. 用文字（或伪代码）写出一次「rollout → 训练 → 更新权重 → 再 rollout」的调用流程，标注每一步调用了哪个接口、`flush_cache` 取值如何选。

**需要观察的现象**：跑通的 rollout 能打印出每条 prompt 的 `Generated text`；你能清晰说出权重更新接口的「权重来源」分类与 `flush_cache` 语义。

**预期结果**：

- rollout 用 `async_generate`（或 `generate`）。
- 更新权重按来源二选一：训练框架直传张量用 `update_weights_from_tensor`；走 NCCL 组用 `init_weights_update_group` + `update_weights_from_distributed`；保存了 checkpoint 用 `update_weights_from_disk`。
- 默认 `flush_cache=True` 清空旧 KV；连续多次更新时中间步骤设 `flush_cache=False`。

**待本地验证**：实际的 RL 集成（如 veRL/OpenRLHF 调用上述接口的接线方式）需结合对应框架文档确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `update_weights_from_tensor` 默认 `flush_cache=True`？什么情况下你会显式传 `flush_cache=False`？

> **答案**：权重变化后，旧的 KV 缓存是用旧权重算出来的，已失效，必须清空，所以默认 `True`。当你**连续多次更新**（如分块/分层上传权重）时，为避免每次都重复清缓存，中间步骤设 `False`，只在最后一次更新时清。

**练习 2**：分布式权重更新（`update_weights_from_distributed`）在使用前必须先调哪个接口？为什么？

> **答案**：必须先调 `init_weights_update_group` 建立训练侧与推理侧之间的 NCCL 进程组（指定 `master_address`/`master_port`/`rank_offset`/`world_size`/`group_name`）。`update_weights_from_distributed` 只在该组上 broadcast 权重，没有组就无法通信；用完还要 `destroy_weights_update_group` 释放。

**练习 3**：`get_weights_by_name` 在 RL 流程里通常用来做什么？

> **答案**：读取引擎里某个参数的当前值，用于**校验**刚执行的权重更新是否真的生效（比对新权重与训练框架手里的张量是否一致），是一种正确性 sanity check。

---

### 4.4 配置访问迁移：Engine 与 runtime_context 的接线（update 重点）

#### 4.4.1 概念说明

本讲 `action=update` 的主要驱动是 `engine.py` 里配置读取方式的机械迁移：`self.server_args.<x>` 中若干处被替换为 `runtime_context` 的命名空间访问器，并新增了一次 `publish` 调用。这与 [u2-l5](u2-l5-runtime-context-config-bags.md) 介绍的「`server_args` 退化为只读留档、运行期配置走命名空间袋」一脉相承。`Engine` 是主进程里 TokenizerManager 的宿主，因此它的配置读取也必须遵循同一规范。

本次迁移涉及三处实质变化（来自 `git diff`）：

1. **DP 路由**：`_resolve_routed_dp_rank` 读 `dp_size` 改为 `get_parallel().dp_size`。
2. **多 tokenizer 路由的 publish**：`tokenizer_worker_num > 1` 时，父进程里的 `MultiTokenizerRouter` 不会自行 `publish`，所以 `Engine` 在构造它之前**主动 `publish(server_args, role="tokenizer")`**，否则命名空间访问器读不到已解析的配置。
3. **`get_server_info` 反映覆盖后配置**：上报信息时用 `get_context().resolved_server_args_dict(...)` 叠加 `override()` 之后的运行期改动（如权重版本、model path），而不是只读静态 `server_args` 快照。

#### 4.4.2 核心流程

`publish` 的职责是把进程级配置安装到 RuntimeContext：

```text
publish(server_args, role="tokenizer")
  └── 记录 role，set_server_args(server_args) → 投影出各命名空间袋
之后 get_parallel() / get_exec() 等访问器即可读到解析后的配置
```

值得注意：`role` 字符串（`tokenizer`/`scheduler`/`encoder`/…）目前主要作 provenance（来源标注），真正按角色做命名空间投影与强制校验是后续工作。

#### 4.4.3 源码精读

`MultiTokenizerRouter` 分支主动 publish（注释解释了为什么必须在这里 publish）：

[python/sglang/srt/entrypoints/engine.py:880-890](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L880-L890) —— 当 `tokenizer_worker_num > 1` 时，父进程的 `MultiTokenizerRouter` 自身不 publish，但它在父进程里通过 `get_parallel()` 等访问器读配置，所以构造它之前先 `publish(server_args, role="tokenizer")`；子 `TokenizerWorker` 各自在自己进程里独立 publish。

`publish` 的实现（只记 role + set_server_args，投影出命名空间袋）：

[python/sglang/srt/runtime_context.py:1076-1088](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/runtime_context.py#L1076-L1088) —— `publish(server_args, *, role)` 记录进程角色并安装配置，是「每进程一次」的入口。

`get_server_info` 叠加覆盖后配置：

[python/sglang/srt/entrypoints/engine.py:1007-1024](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/python/sglang/srt/entrypoints/engine.py#L1007-L1024) —— 用 `get_context().resolved_server_args_dict(base=dataclasses.asdict(...server_args))` 在静态 `server_args` 之上叠加 `override()` 之后的运行期改动，使上报信息反映当前真实配置（权重版本、model path 等）。

#### 4.4.4 代码实践

**实践目标**：验证「读运行期配置走命名空间访问器」这一迁移在本讲的代码里成立。

**操作步骤**：

1. 在 `engine.py` 中 `grep` 出所有 `get_parallel()`、`get_context()`、`publish(` 的调用点。
2. 对比本讲未迁移的遗留点（如 `update_weights_from_tensor` 里的 `self.server_args.tp_size`），说明「迁移进行中」的现状。

**需要观察的现象**：能区分「已迁移到访问器」与「仍读 `server_args`」的代码点。

**预期结果**：DP 路由的 `dp_size`、`get_server_info` 的配置叠加、`MultiTokenizerRouter` 的 publish 均走 `runtime_context`；少数静态字段（如 `tp_size`、`node_rank`、`enable_trace` 等）仍读 `server_args`。**待确认**：迁移是渐进的，具体哪些字段已迁请以你本地 HEAD 实际代码为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MultiTokenizerRouter` 分支需要在构造 router **之前** `publish`，而 `tokenizer_worker_num == 1` 的普通 `TokenizerManager` 分支不需要在 `Engine` 这里 publish？

> **答案**：普通 `init_tokenizer_manager_func` 内部会自行 publish；而 `MultiTokenizerRouter` 不 publish，但它在父进程里又通过 `get_parallel()` 等访问器读配置，访问器要求配置已安装，所以必须在构造它之前由 `Engine` 主动 `publish(server_args, role="tokenizer")`。

**练习 2**：`get_server_info` 改用 `resolved_server_args_dict(...)` 后，相比直接 `dataclasses.asdict(server_args)` 多反映了什么？

> **答案**：多反映了 `override()` 之后的运行期改动——例如 RL 热更新后的权重版本、被 override 改掉的 model path 或运行期可调参数。直接 `asdict` 只能看到 publish 时的静态快照，会漏掉这些「后来才变」的配置。

---

## 5. 综合实践

**任务**：用 `sgl.Engine` 模拟一个**最小 RL rollout + 权重热更新**闭环，并把本讲三个最小模块串起来。

**要求完成**：

1. **离线 rollout**：参考 `offline_batch_inference_async.py`，构造一个 `Engine`，对一批 prompt 用 `async_generate` 采样，收集响应（这就是「rollout」）。
2. **权重更新**：在 `EngineBase`/`engine.py` 中找到 RL 常用的 `update_weights_from_tensor`（或 `update_weights_from_disk`），说明它的参数与 `flush_cache` 语义，并写出一次「rollout → 训练（占位）→ update_weights → 再 rollout」的伪代码调用流程。
3. **配置读取**：在你的伪代码注释里标注，引擎内部若要读 `dp_size` 应该用 `get_parallel().dp_size` 而非 `server_args.dp_size`，并指出这是 `runtime_context` 迁移带来的变化（参考 4.4）。
4. **校验**：说明你会用 `get_weights_by_name` 在更新后做一次正确性 sanity check。

**验收标准**：

- 能正确说出同步 `generate` 与异步 `async_generate` 的差别与各自适用场景。
- 能列出至少 3 个 `update_weights_*` 接口，并按「权重来源」分类。
- 能解释 `flush_cache` 默认为 `True` 的原因，以及何时显式设 `False`。
- 能指出本讲涉及的 `runtime_context` 迁移点（`get_parallel()`、`publish`、`resolved_server_args_dict`）。

> **待本地验证**：本任务需要 GPU 与可用模型；若本地无 GPU，可降级为「源码阅读型实践」——只完成 2/3/4 的源码追踪与伪代码，不实际启动引擎。

---

## 6. 本讲小结

- SGLang 在 RL 训练后端中的角色是**高效 rollout + 权重热更新**的推理骨干，而非训练框架本身；离线批量推理（`sgl.Engine`）是它最常用的接入形态。
- `generate`（同步，一把批量）与 `async_generate`（异步，逐条并发到达）背后共用 `tokenizer_manager.generate_request`，差别在驱动方式与请求到达模式，分别适合离线批量与模拟在线流量。
- `EngineBase` 定义了 `generate`/`update_weights_from_tensor`/`release_memory_occupation`/`shutdown` 等统一抽象，使 HTTP 引擎与进程内 `Engine` 的 API 同构。
- RL 权重热更新按「权重来源」分四类：`update_weights_from_tensor`（张量直传）、`update_weights_from_distributed`（NCCL 组）、`update_weights_from_disk`（检查点重载）、`update_weights_from_ipc`（ZMQ/checkpoint-engine）；默认 `flush_cache=True` 因权重变了旧 KV 失效。
- 配置访问已迁移到 `runtime_context`：`dp_size` 走 `get_parallel()`、`MultiTokenizerRouter` 分支主动 `publish(role="tokenizer")`、`get_server_info` 用 `resolved_server_args_dict` 叠加覆盖后配置；`server_args` 退化为只读留档（迁移渐进，仍有少量遗留读取点）。

---

## 7. 下一步学习建议

- **想要更稳的在线 rollout**：阅读 `python/sglang/srt/entrypoints/engine.py` 中的 `encode`/`rerank`/`open_session`/`close_session` 等配套接口，理解 Engine 的完整能力面。
- **想要更细的权重更新实现**：跟踪 `update_weights_from_tensor` → `tokenizer_manager.update_weights_from_tensor` → Scheduler 子进程里 ModelRunner/worker 的真实换权重动作（承接 [u5-l1](u5-l1-model-runner-forward.md)）。
- **想要理解 DP 路由全貌**：结合 [u8-l2 数据并行与 DP 控制器](u8-l2-data-parallel-controller.md)，看 `routed_dp_rank` 如何在多 DP worker 间路由请求。
- **想要深入配置体系**：回到 [u2-l5 RuntimeContext 与配置命名空间](u2-l5-runtime-context-config-bags.md)，理解 `publish`/`override`/命名空间袋的完整设计与工程约束。
