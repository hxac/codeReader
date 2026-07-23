# Scheduler 主循环与 Overlap Scheduling

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 Scheduler 的「主循环」到底在循环什么：收消息 → 调度下一批 → 前向 → 处理上一批结果。
- 理解 **Overlap Scheduling（重叠调度）** 的核心思想：用两条 CUDA stream，把「GPU 算当前批」和「CPU 处理上一批结果」叠在一起，从而把 CPU 开销藏进 GPU 计算时间里。
- 读懂 `overlap_loop` 与 `normal_loop` 两个循环的差异，以及为什么默认走 overlap 分支。
- 掌握环境开关 `MINISGL_DISABLE_OVERLAP_SCHEDULING` 如何在两种循环之间切换。
- 理解 `ForwardInput` / `ForwardData` 这组缓存结构为何是 overlap 能正确工作的关键。

本讲只聚焦「主循环本身」。至于循环内部「怎么挑下一批」（Prefill/Decode 调度）、「怎么和别的 rank 同步消息」、「前向里到底算了什么」，分别属于 u4-l3/u4-l4、u4-l2、u5，本讲只在必要时点到为止。

## 2. 前置知识

阅读本讲前，请确保你已经建立以下认知（来自前置讲义）：

- **CUDA stream（流）**：GPU 上的任务队列。把一个操作「提交」到某条 stream 后，CPU 不会等它算完就继续往下走（异步）。同一条 stream 内的算子按顺序执行；不同 stream 之间可以并行，需要时用 `wait_stream` / `Event` 来显式同步。这是理解 overlap 的物理基础。
- **Req / Batch / Context**（u2-l1）：`Req` 是一条请求在调度侧的状态，`Batch` 是若干 `Req` 的打包并带 `phase`（`"prefill"` / `"decode"`），`Context` 是进程级共享设施。本讲的循环每轮都在「组装一个 `Batch` → 送进 Engine → 收回结果」。
- **进程架构与消息流**（u1-l4、u2-l3、u3-l2）：Scheduler 是每个 GPU 一个的进程，rank 0 负责「对外」收发消息，`receive_msg` 拿到的是 `UserMsg` / `AbortBackendMsg` 等，处理完一批后用 `send_result` 把 `DetokenizeMsg` 送回 detokenizer。
- **Engine 前向**（u5 会展开，本讲只需知道结论）：`self.engine.forward_batch(batch, sample_args)` 把一个 batch 送进模型，返回一个 `ForwardOutput`，里面包含下一批 token 的 GPU/CPU 拷贝与一个「拷贝完成」事件。

一个直觉性的问题先放在脑子里：**如果不做任何特殊处理，主循环里 GPU 算的时候 CPU 在干嘛？CPU 处理结果的时候 GPU 又在干嘛？** 带着这个问题读下去。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [python/minisgl/scheduler/scheduler.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py) | Scheduler 主类，含主循环 | `ForwardInput/ForwardData`、两条 stream 的创建、`overlap_loop`、`normal_loop`、`run_forever`、`_process_last_data`、`_schedule_next_batch`、`_forward` |
| [python/minisgl/env.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py) | 环境变量单例 | `DISABLE_OVERLAP_SCHEDULING` 开关与 `MINISGL_` 前缀拼接 |
| [python/minisgl/engine/engine.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py) | Engine 执行引擎 | `forward_batch` 里的 stream 断言、异步拷贝与 `copy_done_event`（理解 overlap 的下游配合） |
| [benchmark/offline/bench.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench.py) | 离线吞吐基准 | 综合实践里用来对比 overlap 开/关的吞吐 |

## 4. 核心概念与源码讲解

本讲按「先讲跨迭代缓存的数据结构，再讲 overlap 主循环，再对照朴素循环，最后讲入口与开关」的顺序展开，对应四个最小模块：`ForwardInput/ForwardData`、`overlap_loop`、`normal_loop`、`run_forever`。

### 4.1 ForwardInput / ForwardData：跨迭代缓存的数据结构

#### 4.1.1 概念说明

overlap 的本质是「这一轮算的批，要等到下一轮才处理结果」。于是自然产生一个问题：**这一轮前向用到的输入（batch、采样参数、各种索引张量）和产出的输出，必须被原样「留」到下一轮**。如果下一轮在准备新批时把旧批的张量覆盖或回收了，仍在 GPU 上飞着的异步操作就会读到无效内存。

为此，Scheduler 定义了一个不可变容器 `ForwardInput`，把「一次前向所需的全部输入」打包；再用类型别名 `ForwardData` 把「输入 + 输出」成对地缓存起来，作为循环之间传递的「接力棒」。

> 名词解释：**IMA** —— 代码注释里写的 "avoid IMA" 指 *Illegal Memory Access*（非法内存访问），是 CUDA 里最常见的致命错误之一，通常由读到已释放/越界的显存引起。缓存 `ForwardInput` 的目的之一就是把张量生命周期延长到跨越 stream 边界，避免 IMA。

#### 4.1.2 核心流程

`ForwardInput` / `ForwardData` 在循环中的生命周期：

```
第 N 轮 overlap_loop:
  _schedule_next_batch() ──► forward_input(N)        # 组装本轮输入
  _forward(forward_input(N)) ──► forward_output(N)   # 本轮前向(异步,在 engine stream)
  ongoing_data = (forward_input(N), forward_output(N)) = ForwardData(N)
  return ongoing_data                                  # 作为下一轮的 last_data

第 N+1 轮 overlap_loop:
  入参 last_data = ForwardData(N)
  _process_last_data(last_data)                        # 处理上一轮的结果,读 batch.reqs / next_tokens_cpu
```

也就是说，`ForwardData` 是「上一轮的账本」，在 `_process_last_data` 里被核销。

#### 4.1.3 源码精读

`ForwardInput` 是一个 `NamedTuple`，字段正好是「`_prepare_batch` 产出、`_forward` 与 `_process_last_data` 都要用到」的那几样：

[python/minisgl/scheduler/scheduler.py:35-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L35-L42) —— 定义 `ForwardInput` 与 `ForwardData`。`batch` 是本轮的 `Batch`（带 reqs）；`sample_args` 是采样参数；`input_tuple` / `write_tuple` 是两组索引张量（读取输入 token、回写输出 token 时用）。

`ForwardInput` 由 `_prepare_batch` 产出，它把调度好的 batch「装备」上前向所需的全部元数据：

[python/minisgl/scheduler/scheduler.py:204-217](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L204-L217) —— `_prepare_batch` 调用 `pad_batch`、分配 page、构造 `positions` / `input_mapping` / `write_mapping` / `out_loc`、让注意力后端准备 metadata，最后组装成 `ForwardInput` 返回。

而 `_process_last_data` 正是消费 `last_data[0].batch` 与 `last_data[1]`（即 `ForwardOutput`）的地方：

[python/minisgl/scheduler/scheduler.py:138-144](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L138-L144) —— 把 `last_data` 解包成 `batch` 与 `(_, next_tokens_cpu, copy_done)`，先 `copy_done.synchronize()` 等异步拷贝完成，再逐个 req 处理。如果没有缓存的 `ForwardInput`，这里就拿不到上一轮的 `batch.reqs` 与索引。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `ForwardInput` 的每个字段都「有去有处」。
2. **步骤**：
   - 在 `scheduler.py` 中打开 `_prepare_batch`（204-217 行），记下 `ForwardInput` 四个字段分别由哪一行赋值。
   - 再打开 `_forward`（227-233 行）与 `_process_last_data`（138-167 行），找出每个字段在哪里被读取。
3. **观察现象**：`input_tuple` / `write_tuple` 在 `_forward` 里被当作 `input_mapping` / `output_mapping` 用于 `token_pool` 的读写；`batch` 则贯穿到 `_process_last_data`。
4. **预期结果**：你会发现 `ForwardInput` 没有一个多余字段——每个都被前向或结果处理用到，这正是「缓存它」的全部理由。
5. 若无法运行，结论可直接由阅读得出，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ForwardInput` 改成只缓存 `batch`、丢掉 `input_tuple` 和 `write_tuple`，会在哪一步出错？
**答案**：在 `_forward` 里 `self.token_pool[input_mapping]` 与 `self.token_pool[output_mapping] = ...` 会拿不到索引张量；更隐蔽的是，overlap 下旧索引张量若被新批覆盖，仍在 engine stream 上排队的异步读写会触发 IMA。

**练习 2**：`ForwardData` 为什么用 `Tuple[ForwardInput, ForwardOutput]` 而不是再定义一个类？
**答案**：它只是「把输入和输出成对地传来传去」的临时载体，用类型别名表达足够清晰，也避免了又一层 `NamedTuple` 的样板代码。

### 4.2 overlap_loop：用两条 stream 交错执行

#### 4.2.1 概念说明

朴素循环里，一轮的时间大约是「CPU 准备 + GPU 前向 + CPU 收尾」三者相加。GPU 前向时 CPU 空等，CPU 收尾时 GPU 空等——两段空闲都是浪费。

`overlap_loop` 的解法是**双 stream**：

- **scheduler stream（`self.stream`）**：跑收消息、调度、准备元数据、处理上一批结果这类 CPU 主导的活，以及它们附带的小张量操作。
- **engine stream（`self.engine.stream`）**：跑模型前向与采样这类 GPU 重活。

两条 stream 在 `__init__` 里就建好了：

[python/minisgl/scheduler/scheduler.py:51-55](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L51-L55) —— 新建 scheduler stream，保存一个「切到 engine stream」的上下文管理器，并把当前默认流设成 scheduler stream。

于是「处理上一批结果（CPU）」和「算当前批（GPU）」可以同时进行，理想情况下每轮时间从求和降到取最大值：

\[
T_{\text{overlap}} \approx \max(T_{\text{cpu}},\ T_{\text{gpu}}), \qquad T_{\text{normal}} \approx T_{\text{cpu}} + T_{\text{gpu}}
\]

#### 4.2.2 核心流程

`overlap_loop` 伪代码（带 stream 标注）：

```
overlap_loop(last_data):                            # last_data = 上一轮的 ForwardData
  1. 决定是否阻塞等消息（见 4.2.3）
  2. for msg in receive_msg(blocking):              # [scheduler stream] 收消息
       _process_one_msg(msg)                        #   入队 / abort / 退出
  3. forward_input = _schedule_next_batch()         # [scheduler stream] 挑批 + _prepare_batch
  4. if forward_input is not None:
       with engine_stream_ctx:                      #   切到 engine stream
         engine.stream.wait_stream(self.stream)     #   ★等元数据准备好
         ongoing_data = (forward_input, _forward()) # [engine stream] 前向(异步,立即返回)
  5. _process_last_data(last_data)                  # [scheduler stream] 处理上一批,与第4步并行
  6. return ongoing_data                            # 成为下一轮的 last_data
```

关键在第 4 步和第 5 步：第 4 步把前向**异步**提交到 engine stream 后立刻返回，紧接着第 5 步在 scheduler stream 上处理**上一轮**的结果。两者时间上重叠。

#### 4.2.3 源码精读

完整函数：

[python/minisgl/scheduler/scheduler.py:83-106](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L83-L106) —— `overlap_loop`。逐段说明：

- **第 90-94 行的 `blocking`**：决定「收消息」要不要死等。只有当**既没有上一批待处理（`last_data is None`），也没有可调度的 prefill/decode** 时才阻塞。注释里的「don't block if we have a batch to be processed」正指 `last_data is not None`——手里有账要核销时绝不能卡在收消息上，否则流水线就空了。
- **第 101-103 行**：进入 engine stream 上下文后，先 `self.engine.stream.wait_stream(self.stream)`。这是一条** stream 同步**：engine stream 必须等 scheduler stream 把第 3 步的元数据（positions、索引张量、attn metadata）都准备完，才能开始前向，否则会读到半成品。随后 `_forward` 把前向异步提交并立即返回 `ongoing_data`。
- **第 105 行**：回到 scheduler stream（出了 `with` 块）处理 `last_data`。此时 engine stream 正在后台算当前批，二者并行。

下游配合在 Engine 里：`forward_batch` 会断言当前流是 engine stream，并在采样后发起 GPU→CPU 的异步拷贝、记录 `copy_done_event`：

[python/minisgl/engine/engine.py:191-206](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L191-L206) —— `next_tokens_cpu` 用 `non_blocking=True` 异步拷回，`copy_done_event.record(self.stream)` 记录在 engine stream 上。这个事件随后被 `_process_last_data` 里的 `copy_done.synchronize()` 等待，形成「engine stream 产出 → scheduler stream 消费」的正确交接。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理清 overlap 下两条 stream 的两次「交接」。
2. **步骤**：
   - 找出 engine stream 等 scheduler stream 的那一行（提示：`wait_stream`），说明它在等什么。
   - 找出 scheduler stream 等 engine stream 的那一行（提示：`copy_done.synchronize()`），说明它在等什么。
3. **观察现象**：两次同步方向相反——一次是「GPU 等元数据」，一次是「CPU 等 token 拷完」。
4. **预期结果**：能用一句话说清「谁等谁、等的是什么」，就说明 overlap 的正确性条件你已经掌握。
5. 无需运行，纯阅读即可。

#### 4.2.5 小练习与答案

**练习 1**：删掉第 102 行 `self.engine.stream.wait_stream(self.stream)` 会怎样？
**答案**：engine stream 可能在 scheduler stream 写完 `positions` / 索引张量之前就开始前向，读到未初始化或半成品的输入，导致结果错误甚至 IMA。

**练习 2**：为什么第 4 步用 `ongoing_data = (forward_input, self._forward(...))` 而不是先 `forward_output = self._forward(...)` 再判断？
**答案**：`_forward` 一旦调用就把 GPU 工作提交并立即返回，没有「先算再决定要不要」的余地；只有在 `forward_input is None`（没有可调度的批）时才跳过，所以包在 `if` 里即可。

### 4.3 normal_loop：朴素的串行循环

#### 4.3.1 概念说明

`normal_loop` 是 overlap 的「退化版」：不缓存上一轮、不交错，一轮之内**先前向、紧接着就处理本轮结果**，CPU 和 GPU 完全串行。它存在的意义有二：一是作为对照基线让你理解 overlap 省了什么；二是当 overlap 因环境/调试原因被关闭时，提供一个更简单、更易排查问题的回退路径。

#### 4.3.2 核心流程

```
normal_loop():                       # 没有跨轮的 last_data
  1. blocking = not (prefill runnable or decode runnable)
  2. for msg in receive_msg(blocking): _process_one_msg(msg)
  3. forward_input = _schedule_next_batch()
  4. if forward_input is not None:
       ongoing_data = (forward_input, _forward(forward_input))
  5. _process_last_data(ongoing_data)   # ★立刻处理【本轮】结果,不延后
```

与 `overlap_loop` 对照，差别只在两处：`blocking` 不再考虑 `last_data`；以及第 5 步处理的是**本轮**的 `ongoing_data`，而不是上一轮的。

#### 4.3.3 源码精读

[python/minisgl/scheduler/scheduler.py:108-118](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L108-L118) —— `normal_loop`。注意：

- 第 109 行 `blocking` 只看两个 manager 是否 runnable，没有 `last_data` 项——因为 normal 模式下没有跨轮缓存。
- 第 116 行前向**没有**包在 `with self.engine_stream_ctx` 里。这是因为在 `run_forever` 的 normal 分支里，整个 `while True` 已经被包在 `with self.engine_stream_ctx` 中（见 4.4.3），当前流已经是 engine stream，所以这里不需要再切。
- 第 118 行 `_process_last_data(ongoing_data)` 处理的就是本轮刚算完的结果，前向与收尾被串在一起，没有重叠。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：把 `normal_loop` 与 `overlap_loop` 做一张精确的差异表。
2. **步骤**：把两个函数并排放，逐行比较：`blocking` 的条件、`_forward` 是否在 `engine_stream_ctx` 里、`_process_last_data` 的参数是 `ongoing_data` 还是 `last_data`、返回值。
3. **观察现象**：差异点很少，但每一处都直接决定了「是否重叠」。
4. **预期结果**：你能指出 normal 的致命点在于第 118 行处理的是 `ongoing_data`——前向刚提交完就要等它、处理它，GPU 与 CPU 无法错开。
5. 纯阅读即可。

#### 4.3.5 小练习与答案

**练习 1**：`normal_loop` 为什么不返回任何值？
**答案**：它没有跨轮状态需要传递——每轮自己前向、自己处理结果，互不欠账，所以 `run_forever` 里直接 `while True: self.normal_loop()`，无需接返回值。

**练习 2**：在 normal 模式下，`copy_done.synchronize()`（在 `_process_last_data` 里）会阻塞更久还是更短？
**答案**：相对更长。因为本轮前向刚结束就要立刻读 `next_tokens_cpu`，异步拷贝很可能还没完成，必须干等；而 overlap 模式下，等到下一轮处理时拷贝往往早已完成，`synchronize` 几乎不阻塞。

### 4.4 run_forever：入口与 DISABLE_OVERLAP_SCHEDULING 开关

#### 4.4.1 概念说明

`run_forever` 是 Scheduler 真正开始干活后的「死循环」入口（返回类型标注为 `NoReturn`）。它做两件事：用 `@torch.inference_mode()` 关掉自动求导（纯推理，省显存）；然后根据环境开关 `DISABLE_OVERLAP_SCHEDULING` 选择走 overlap 还是 normal。

这个开关定义在 `env.py` 的环境变量单例里，对外暴露成 `ENV.DISABLE_OVERLAP_SCHEDULING`。环境变量名按「`MINISGL_` 前缀 + 字段名」拼成 `MINISGL_DISABLE_OVERLAP_SCHEDULING`。

#### 4.4.2 核心流程

```
run_forever():                         # @torch.inference_mode()
  if ENV.DISABLE_OVERLAP_SCHEDULING:
    with engine_stream_ctx:             # 整个循环都跑在 engine stream 上
      engine.stream.wait_stream(self.stream)
      while True: normal_loop()
  else:
    assert current_stream == self.stream   # 默认流必须是 scheduler stream
    data = None
    while True: data = overlap_loop(data)  # 用 data 把上一轮结果接力下去
```

#### 4.4.3 源码精读

[python/minisgl/scheduler/scheduler.py:120-131](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L120-L131) —— `run_forever`。

- **第 122 行**：`if ENV.DISABLE_OVERLAP_SCHEDULING` 走 normal 分支。第 123-124 行先把当前流切到 engine stream 并做一次 `wait_stream`，之后 `while True: self.normal_loop()` 全程在 engine stream 上串行执行。
- **第 127-131 行**：默认（overlap）分支。先断言当前流是 scheduler stream（这个流在 `__init__` 第 55 行由 `torch.cuda.set_stream(self.stream)` 设定），再用 `data = None` 起步，把每轮 `overlap_loop` 的返回值当成下一轮的入参接力传递——这正是 `ForwardData` 缓存的用武之地。

开关本身在 `env.py`：

[python/minisgl/env.py:69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L69) —— `DISABLE_OVERLAP_SCHEDULING = EnvBool(False)`，默认 `False`，即默认开启 overlap。

[python/minisgl/env.py:50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L50) 与 [python/minisgl/env.py:78-84](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/env.py#L78-L84) —— 前缀 `MINISGL_` 与 `__init__` 里对每个字段调用 `_init(f"{MINISGL_ENV_PREFIX}{attr_name}")` 读取真实环境变量。拼起来就是 `MINISGL_DISABLE_OVERLAP_SCHEDULING`；`_TO_BOOL` 把 `"1"` / `"true"` / `"yes"` 识别为真。

#### 4.4.4 代码实践（源码阅读型 + 可选运行）

1. **目标**：确认开关变量名与取值规则。
2. **步骤**：
   - 读 `env.py` 的 `EnvBool` / `_TO_BOOL` / `_init`，确认 `MINISGL_DISABLE_OVERLAP_SCHEDULING=1` 会让 `ENV.DISABLE_OVERLAP_SCHEDULING` 为真。
   - （可选，待本地验证）在 `run_forever` 第 122 行前加一行 `logger.info_rank0("overlap=%s", not ENV.DISABLE_OVERLAP_SCHEDULING)`，分别用默认与 `=1` 启动，观察日志。
3. **观察现象**：设 `=1` 时日志应打印 `overlap=False`，且进入 normal 分支。
4. **预期结果**：变量名是 `MINISGL_DISABLE_OVERLAP_SCHEDULING`（注意全大写、带前缀），取 `1`/`true`/`yes` 生效。

#### 4.4.5 小练习与答案

**练习 1**：为什么 normal 分支要用 `with self.engine_stream_ctx` 把整个循环包起来，而 overlap 分支不用？
**答案**：normal 模式只有一条有效工作流，把循环整体置于 engine stream 上，可让前向与收尾都顺次发生在同一条流上、避免无谓切换；overlap 模式则需要在 scheduler stream 与 engine stream 之间反复切换来实现重叠，所以不能整体包死。

**练习 2**：如果把默认流（`__init__` 第 55 行的 `set_stream`）注释掉，第 128 行的断言会怎样？
**答案**：当前默认流不再是 scheduler stream，断言失败、直接抛错。这条断言正是为了在进入 overlap 循环前确保「默认流 = scheduler stream」这一前提成立。

## 5. 综合实践

把本讲的几个模块串起来，做一个**对比 overlap 开/关的离线吞吐实验**。

**任务**：用 `benchmark/offline/bench.py` 在 overlap（默认）与 `MINISGL_DISABLE_OVERLAP_SCHEDULING=1` 两种模式下各跑一次，记录吞吐并解释差异。

**步骤**：

1. 确认已按 u1-l2 安装好 Mini-SGLang 与 GPU 环境（需 NVIDIA GPU + sgl_kernel/flashinfer）。
2. 阅读基准脚本 [benchmark/offline/bench.py:10-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/benchmark/offline/bench.py#L10-L42)：它用 `LLM`（offline 模式，单进程内收发消息，绕过 ZMQ/tokenizer，见 u11-l1）跑 256 条随机 prompt，先 warmup 再计时，最后打印 `Throughput`。
3. 第一次运行（默认 overlap）：
   ```bash
   python benchmark/offline/bench.py
   ```
4. 第二次运行（关闭 overlap）：
   ```bash
   MINISGL_DISABLE_OVERLAP_SCHEDULING=1 python benchmark/offline/bench.py
   ```
5. 记录两次输出的 `Time` 与 `Throughput`。

**需要观察的现象**：

- 默认（overlap）模式的吞吐应**高于**关闭模式，时间更短。
- 差距大小取决于 CPU 收尾开销（`_process_last_data` 里的 cache 管理、ZMQ/消息处理）与 GPU 前向时长的比值——CPU 开销占比越大，overlap 收益越明显。

**解释（对照代码）**：

- 默认模式每轮耗时约 \(\max(T_{\text{cpu}}, T_{\text{gpu}})\)：第 4 步 `_forward`（engine stream，[scheduler.py:100-103](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L100-L103)）与第 5 步 `_process_last_data`（scheduler stream，[scheduler.py:105](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L105)）并行。
- 关闭模式每轮耗时约 \(T_{\text{cpu}} + T_{\text{gpu}}\)：前向与结果处理串行（[scheduler.py:116-118](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L116-L118)），CPU 等待时 GPU 空转。

> 说明：本实践依赖真实 GPU。若当前环境无 GPU，则按「源码阅读型」完成——对照上面的行号，用语言复述两种模式每轮的时间构成，并预测 overlap 会更快。具体数值「待本地验证」。

## 6. 本讲小结

- Scheduler 的主循环就是「收消息 → 调度下一批 → 前向 → 处理上一批结果」的周而复始；`run_forever` 是它的 `@torch.inference_mode()` 死循环入口。
- **Overlap Scheduling** 用两条 CUDA stream（scheduler stream `self.stream` 处理 CPU 活与元数据，engine stream `self.engine.stream` 跑前向）把「算当前批」和「处理上一批」叠在一起，把每轮耗时从 \(T_{\text{cpu}}+T_{\text{gpu}}\) 降到约 \(\max(T_{\text{cpu}},T_{\text{gpu}})\)。
- `overlap_loop` 把本轮的 `(ForwardInput, ForwardOutput)` 作为 `ongoing_data` 返回，下一轮当作 `last_data` 处理；两次反向的 stream 同步（`wait_stream` 与 `copy_done.synchronize()`）保证正确性。
- `normal_loop` 是不缓存、不交错的串行回退：前向后立刻处理本轮结果，简单但更慢。
- `ForwardInput` / `ForwardData` 是 overlap 的「接力棒」与「保活容器」，把张量生命周期延长到跨越 stream 边界，避免 IMA。
- 开关 `MINISGL_DISABLE_OVERLAP_SCHEDULING=1`（默认关闭）可强制走 normal 分支，便于调试或对照。

## 7. 下一步学习建议

- 想知道 `receive_msg` 在多 rank 下如何广播、各卡如何同步消息条数，请读 **u4-l2 Scheduler I/O 与多 rank 广播**。
- 想知道 `_schedule_next_batch` 里 prefill 如何按 token budget 挑请求、长 prompt 如何切块，请读 **u4-l3 Prefill 调度与 Chunked Prefill**。
- 想知道 decode 批如何维护 running_reqs、`table_idx` 与 token_pool 怎么配合，请读 **u4-l4 Decode 调度、TableManager 与 TokenPool**。
- 想知道 `forward_batch` 内部如何在 CUDA graph 回放与普通前向间选择、采样如何批量打包，请读 **u5-l2 Engine forward 与采样**。
- 推荐顺带阅读 `_process_last_data` 串联的 `cache_manager.cache_req` / `lazy_free_region`（u6-l3），理解结果处理如何与 KV cache 回收联动。
