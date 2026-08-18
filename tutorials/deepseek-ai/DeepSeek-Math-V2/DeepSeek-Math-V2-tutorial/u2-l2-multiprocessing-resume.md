# 多进程队列与断点续跑：generate.py 的工程骨架

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `mp_generate_loop` 为什么先 `sleep(5)`、再在每个 worker 进程内部各自实例化一个 `APIModel`（进而各自拥有独立的事件循环），而不是在主进程建好再共享。
2. 画出主进程与 worker 进程之间的队列协议：`input_queue` 上的 `(batch_idx, input_data)` 任务、`(None, None)` 哨兵，以及 `output_queue` 上的 `(batch_idx, output_data)` 结果与 `(None, None)` 回声。
3. 描述 `.meta` pickle 文件里 `complete_batches` 集合如何实现「按批断点续跑 + 已完成批次去重」，以及为什么续跑时 `n` 与 `batch_size` 不允许改变（那个 `assert` 到底在防什么）。
4. 分析 `--n` 参数如何把每条输入复制 \( n \) 份实现多次采样，并推导「输出条数＝输入条数 × n」「API 请求数＝输入条数 × n」这两个计数公式。
5. 指出这套协议的一个真实弱点：崩溃发生在「半个批次已落盘、meta 尚未更新」的窗口内时会产生重复行——并动手验证。

本讲只聚焦 [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py) 中 `APIModel` 类之后的工程骨架（第 68-181 行）：`mp_generate` / `mp_generate_loop` 两个函数与 `__main__` 的批处理、收尾循环。上一讲 u2-l1 讲的是「单进程内怎么并发发请求」，本讲讲的是「怎么用多个进程把成百上千个批次吃掉，而且随时可以安全地中断重来」。

## 2. 前置知识

### 2.1 进程、线程与协程的分工

u2-l1 讲过协程与事件循环：一个事件循环就能让一批 I/O 请求并发推进。那为什么还需要多**进程**？

- **协程（asyncio）**：解决「一个进程内大量 I/O 等待」的并发。受限于单核 CPU 与单个事件循环，请求构造、JSON 序列化等 CPU 工作仍是串行的。
- **进程（multiprocessing）**：每个进程有独立的 Python 解释器和内存空间，可以分布到多个 CPU 核上。把不同**批次**分给不同进程，批次内部再用 asyncio 并发——这就是 generate.py 的两级并发结构。

一句话直觉：协程是「一个厨师同时看十口锅」，多进程是「雇十六个厨师，每人分几桌菜」。

### 2.2 multiprocessing.Queue 与生产者—消费者模型

- `multiprocessing.Queue` 是跨进程的安全队列：一端 `put`，另一端 `get`，数据在进程间通过管道传输，**传过去的是序列化后的副本**（内部用 pickle），不是共享内存。
- **生产者—消费者模型**：主进程当生产者，往 `input_queue` 里投任务；worker 进程当消费者，循环 `get()` 取任务、处理、把结果投回 `output_queue`。
- **哨兵（sentinel）**：一个约定的「结束信号」。队列本身没有「没有更多数据了」的概念（消费者会永远阻塞在 `get()` 上），所以生产者结束时按消费者数量投递特殊值，消费者收到后退出循环。本项目的哨兵是二元组 `(None, None)`。
- 队列是 FIFO 的，所以「所有真实任务都在哨兵之前投出」就能保证消费者先吃完所有任务再收到哨兵。

### 2.3 pickle 与幂等

- **pickle**：Python 内置的对象序列化库，可以把 dict、set 等直接转成二进制写进文件、之后再原样读回来。`.meta` 文件就是一个 pickle 过的字典。
- **幂等（idempotence）**：一个操作执行一次和执行多次效果相同。断点续跑追求的就是幂等——重跑整个程序，已完成的部分不再产生副作用（不重复请求、不重复写行）。
- **at-least-once 语义**：如果系统可能在「处理中」崩溃，重启后通常会把未确认完成的工作**至少再做一次**。本讲的批次协议正是批次级 at-least-once。

### 2.4 文件打开模式 `"a+"` 与逐行 JSONL

- `open(path, "a+")`：文件不存在则创建，存在则**追加**写。这是断点续跑在文件层面的配套设计：重跑时旧结果行原样保留，新结果接在后面。
- JSONL（每行一个 JSON 对象）天然适合追加写：不需要把整个文件读进内存改完再写回，`print(json.dumps(item), file=fw)` 一行一 行落盘即可。这呼应 u1-l2 的结论「中间产物一律用 JSONL」。

### 2.5 与前几讲的衔接

u1-l3 已经建立：`main.py` 通过三处 `os.system` 以命令行方式调用 `generate.py`，`--n` 的取值分别来自 `n_sample`（证明生成）、`args.n_verification_per_proof`（证明验证）等；计数公式「输出条数＝输入条数×n」正是在本讲的分发循环里兑现的。u2-l1 建立了 `APIModel.generate` 的同步门面（`asyncio.run` 驱动、`{**item, ...}` 合并输出、`finish_reason` 小写化）——本讲的 worker 每处理一个批次就会调用它一次。

## 3. 本讲源码地图

| 文件 | 本讲关注范围 | 作用 |
| --- | --- | --- |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L68-L81) | 第 68-81 行 | `mp_generate`（worker 消费循环）与 `mp_generate_loop`（进程入口：错峰 + 自建 `APIModel`） |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L83-L125) | 第 83-125 行 | `__main__` 前半：argparse、`.meta` 初始化与一致性断言、建队列开文件 |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L127-L158) | 第 127-158 行 | 主进程生产者：起进程、切批、复制 n 份、跳过已完成批次 |
| [inference/generate.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L160-L181) | 第 160-181 行 | 收尾循环：发哨兵、收结果、逐行落盘、更新 `complete_batches`、join 进程 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L456-L462) | 第 456-462 行 | `main.py` 调用 `generate.py` 的命令拼装处之一，`--n {n_sample}` 的上游来源 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L480-L492) | 第 480-492 行 | 验证阶段的调用：`--n {args.n_verification_per_proof}`，u1-l3 说的「验证强度 ×16」落点 |

## 4. 核心概念与源码讲解

### 4.1 worker 侧：`mp_generate_loop` 与哨兵协议

#### 4.1.1 概念说明

worker 是被 `Process` 拉起的子进程，它的一生只做一件事：死循环地从 `input_queue` 取一个批次，调用上一讲的 `APIModel.generate` 把整批请求并发打出去，再把结果连同批次号投回 `output_queue`。

三个设计问题值得先想清楚：

1. **为什么每个进程自己 `APIModel()`，而不是主进程建好传进去？** `AsyncOpenAI` 客户端持有连接池，且 asyncio 的资源绑定在创建它的进程/线程上；`Process(target=mp_generate_loop, ...)` 的 target 函数体在**子进程内**执行，fork 之后各进程地址空间独立，跨进程共享一个客户端既不安全也无意义。所以正确做法就是每个 worker 各建各的客户端、各跑各的 `asyncio.run`（每个批次一个全新事件循环）。
2. **为什么 `sleep(5)`？** 源码没有注释说明意图（待确认）。合理的推断是**错峰启动**：16 个进程同时拉起、同时发起第一批各 16 个并发请求，会在启动瞬间产生 256 个请求的尖峰，`sleep(5)` 让启动风暴错开；同时也给进程创建与模块导入留出时间。
3. **为什么哨兵要「回声」？** worker 收到 `(None, None)` 后不是默默退出，而是往 `output_queue` 也 `put` 一个 `(batch_idx, None)` 再 `break`。这样主进程每收到一个「结果为 None」就计数减一，能**确认**每个 worker 都已排空队列并退出，而不是猜。

#### 4.1.2 核心流程

worker 进程的生命周期：

```
进程被 fork 并 start()
   │
   ▼
mp_generate_loop
   ├─ APIModel()            ← 本进程专属客户端
   ├─ sleep(5)              ← 错峰启动
   └─ mp_generate(...)      ← 进入死循环
         │
         ▼
      input_queue.get()      ← 阻塞直到有任务
         │
   ┌─────┴──────┐
   │ input_data  │ input_data is None?
   │  is None    │
   ▼             ▼
 put((None,None)) generate(input_data)   ← u2-l1 的同步门面：
   break          │                      整批 asyncio 并发
                  ▼
            output_queue.put((batch_idx, output_data))
                  │
                  └─ 回到 get() 继续下一个批次
```

#### 4.1.3 源码精读

worker 消费循环——从队列取二元组，`None` 即哨兵，回声后退出；否则整批生成并连同 `batch_idx` 投回结果队列：

[inference/generate.py:L68-L75](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L68-L75)

```python
    def mp_generate(self, input_queue: Queue, output_queue: Queue, sampling_params):
        while True:
            batch_idx, input_data = input_queue.get()
            if input_data is None:
                output_queue.put((batch_idx, None))
                break
            output_data = self.generate(input_data, sampling_params)
            output_queue.put((batch_idx, output_data))
```

注意解包写法 `batch_idx, input_data = input_queue.get()`：队列里流动的**所有**消息都必须是二元组，哨兵也得是 `(None, None)` 而不是裸 `None`，否则解包直接抛 `ValueError`。

进程入口函数——三行：建模型、睡 5 秒、进循环：

[inference/generate.py:L78-L81](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L78-L81)

```python
def mp_generate_loop(input_queue, output_queue, sampling_params):
    api_model = APIModel()
    sleep(5)
    api_model.mp_generate(input_queue, output_queue, sampling_params)
```

#### 4.1.4 代码实践

**实践目标**：用 3 分钟在 REPL 里验证「哨兵必须是二元组」这一协议约束。

**操作步骤**（示例代码，与仓库无关）：

```python
>>> from multiprocessing import Queue
>>> q = Queue()
>>> q.put(None)                     # 故意投一个裸 None 当哨兵
>>> batch_idx, input_data = q.get() # 模仿 mp_generate 的解包
Traceback (most recent call last):
  ...
TypeError: cannot unpack non-iterable NoneType object
>>> q.put((None, None))             # 正确的哨兵形态
>>> batch_idx, input_data = q.get()
>>> batch_idx, input_data
(None, None)
```

**需要观察的现象**：裸 `None` 在解包处抛异常；二元组解包后两个字段都是 `None`，`if input_data is None` 分支才会命中。

**预期结果**：确认队列协议中「任务 = (批次号, 数据列表)、哨兵 = (None, None)、结果 = (批次号, 数据列表)、回声 = (None, None)」四种消息形态一致，都靠元组解包消费。（本实践为本地小实验，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果把主进程的哨兵改成只投一个 `None`（`input_queue.put(None)`），程序会怎样？

**答案**：worker 在 `batch_idx, input_data = input_queue.get()` 处解包失败抛异常（`TypeError: cannot unpack non-iterable NoneType object`），worker 进程死亡；主进程的 `remain_processes` 永远减不到 0，收尾循环卡死在 `output_queue.get()` 上。这也说明哨兵结构是协议的一部分，改一头必须两头一起改。

**练习 2**：为什么哨兵要投 `num_processes` 份、而不是一份？

**答案**：队列被所有 worker 共享，每个 worker 都阻塞在 `get()` 上等自己的「下班通知」。投一份只有一个 worker 能收到并退出，其余 worker 永远阻塞，主进程 `process.join()` 挂起。FIFO 性质保证真实任务全部排在哨兵之前，因此每个 worker 必然先吃完任务再收到哨兵。

**练习 3**：每个 worker 处理一个批次会调用一次 `asyncio.run`（在 `APIModel.generate` 里）。这意味着「事件循环」的粒度是什么？

**答案**：事件循环是**每批次一个**、用完即弃——`asyncio.run` 创建循环、跑完 `generate_all` 就关闭。同一时刻一个 worker 只有一个活跃循环，并发度上限是 `min(num_processes, 在途批次数) × batch_size` 个在途请求。

### 4.2 断点档案：`.meta` 文件的初始化与一致性断言

#### 4.2.1 概念说明

断点续跑需要一个「进度档案」回答两个问题：哪些批次已经完成？当初是用什么参数切的批？generate.py 把答案放在输出文件旁边的一个 pickle 文件里，路径就是 `输出路径 + ".meta"`（例如 `output.jsonl.meta`），结构是一个三键字典：

| 键 | 类型 | 含义 |
| --- | --- | --- |
| `n` | int | 每条输入复制几份（采样次数） |
| `batch_size` | int | 每批条数 |
| `complete_batches` | 集合（落盘时可能是 list 或 set） | 已完成并落盘的批次号 |

`n` 和 `batch_size` 被存进去不是为了记录历史，而是为了在**下次启动时做一致性校验**：批次号是按「复制 n 份、攒满 batch_size 切一刀」的规则编出来的，这两个参数任何一个变了，同一个 `batch_idx` 对应的数据就完全不同——比如 `batch_size` 从 16 改成 8，旧的 0 号批（16 条）和新的 0 号批（8 条）根本不是同一批数据，跳过它就等于漏数据。所以代码用一个 `assert` 拒绝参数不一致的续跑，并在报错信息里直接告诉你处理办法：把输出文件和 meta 一起删掉，从头再来。

一个小格式细节：首次创建时 `complete_batches` 存的是 `[]`（list），之后每次更新时内存里已被转成 `set` 再整体 dump，pickle 对 set 的序列化没有问题；下次读回时 `set(...)` 对 list 和 set 都兼容。两种形态共存但不影响正确性。

#### 4.2.2 核心流程

```
meta 路径 = f"{output_data_path}.meta"
   │
   ├─ 文件不存在？ ──是──► 写入初始档案 {"n", "batch_size", "complete_batches": []}
   │                        （注意：这一步在任何生成之前完成）
   ▼
读取并加载档案，complete_batches 转成 set
   │
   ▼
assert n 与 batch_size 与档案一致
   │ 失败 → 报错：请删除 output 文件与 .meta 后重跑
   │ 成功 ↓
继续后续流程
```

#### 4.2.3 源码精读

meta 路径拼接、首次初始化、读回并转 set：

[inference/generate.py:L104-L111](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L104-L111)

```python
    meta_data_path = f"{output_data_path}.meta"
    if not os.path.exists(meta_data_path):
        meta_data = {"n": n, "batch_size": batch_size, "complete_batches": []}
        with open(meta_data_path, "wb") as f:
            pickle.dump(meta_data, f)
    with open(meta_data_path, "rb") as f:
        meta_data = pickle.load(f)
    meta_data["complete_batches"] = set(meta_data["complete_batches"])
```

参数一致性断言——报错信息自带修复指引（删两个文件清空旧结果）：

[inference/generate.py:L113-L114](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L113-L114)

```python
    assert n == meta_data["n"] and batch_size == meta_data["batch_size"], \
        f"params n or batch_size are different from previous running setting({n}, {batch_size}) != ({meta_data['n']}, {meta_data['batch_size']}), you need to delete {output_data_path} & {meta_data_path} to clear existing results"
```

紧随其后构造采样参数字典（`**sampling_params` 在 u2-l1 的 `generate_one` 里展开），注意其中 `max_total_tokens` 并非 OpenAI 官方参数，是面向自建推理服务额外透传的（官方接口是否接受待确认）：

[inference/generate.py:L116-L121](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L116-L121)

```python
    sampling_params = dict(
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        max_total_tokens=max_tokens
    )
```

以及一个容易忽略的边缘情况——直接为输出**创建父目录**：

[inference/generate.py:L94-L95](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L94-L95)

```python
    input_data_path, output_data_path = args.input_data_path, args.output_data_path
    os.makedirs(os.path.dirname(output_data_path), exist_ok=True)
```

如果 `--output_data_path` 传一个不含目录的裸文件名（如 `out.jsonl`），`os.path.dirname` 返回空字符串，`os.makedirs("")` 会抛 `FileNotFoundError`。`main.py` 传的都是带目录的路径，所以正常流程踩不到；但自己单独调用 `generate.py` 时要注意。

#### 4.2.4 代码实践

**实践目标**：手推一批次划分，验证你对切批规则的理解（为 4.3 的源码做铺垫）。

**操作步骤**：设输入文件有 \( L = 7 \) 行，`n = 3`，`batch_size = 4`。先在纸上算：总共多少条复制品？切成几个批次？每批几条？`meta` 初次创建后 `complete_batches` 是什么？然后跑下面 5 行脚本核对（示例代码）：

```python
import math
L, n, b = 7, 3, 4
total = n * L
num_batches = math.ceil(total / b)
print(total, num_batches, [b if i < num_batches - 1 else total - b * (num_batches - 1) for i in range(num_batches)])
print({"n": n, "batch_size": b, "complete_batches": []})
```

**需要观察的现象**：输出 `21 6 [4, 4, 4, 4, 4, 1]` 与初始 meta 字典。

**预期结果**：21 条复制品切成 6 批（5 个满批 + 1 个尾巴批），批次号 0-5；首跑 meta 中 `complete_batches` 为空集合。若你的手算与脚本一致，说明切批规则已掌握。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `assert` 只校验 `n` 和 `batch_size`，不校验 `temperature`、`max_tokens`？

**答案**：`n` 与 `batch_size` 共同决定**批次划分的边界**（批次号到数据的映射），改了它们，`complete_batches` 里记录的批次号就指向了不同的数据，跳过逻辑会漏数据或重复请求——这是正确性问题。而 `temperature` 等只影响生成内容不影响切批结构，续跑时改温度只会让前后批次风格不一致（实验设计问题），不会破坏断点续跑的正确性，所以代码不强制。

**练习 2**：第一次运行在「批次还在分发、一条结果都没写」时就崩溃，`.meta` 文件存在吗？重跑会出问题吗？

**答案**：存在且有效——初始化发生在起进程之前，崩得再早也留下一份 `complete_batches` 为空的合法档案。重跑时所有批次都不在完成集合里，全部重新提交，行为等价于首跑。这正是「先建档案再干活」的价值。

**练习 3**：把 `.meta` 从 pickle 换成 JSON 需要改什么？

**答案**：`complete_batches` 是 set，JSON 没有集合类型，需要序列化时 `sorted(list(s))`、读回时 `set(...)`；其余两个键是 int 天然兼容。顺带一提，现读回代码里 `set(meta_data["complete_batches"])` 本来就兼容 list 形态，所以就算 pickle 里混存 list/set 也不会坏。

### 4.3 主进程生产者：切批、n 份复制与已完成批次跳过

#### 4.3.1 概念说明

主进程（`__main__` 继续往下走的部分）扮演生产者，同时是唯一的「账本记录者」。它做四件事：

1. 建两个队列、以只读打开输入、以追加模式 `"a+"` 打开输出，再拉起 `num_processes` 个 worker；
2. 逐行读输入 jsonl，**每行复制 `n` 份**塞进 `submit_batch`，攒满 `batch_size` 就成一个批；
3. 批次号 `batch_idx` 若已在 `complete_batches` 里则**跳过不发**（只计数 `num_skip`），否则投进 `input_queue`（计数 `num_input`）；
4. 投完所有批次后发哨兵，转入收尾循环（4.4）。

`--n` 的多次采样机制就在第 2 步：它**不使用** OpenAI 接口原生的 `n` 参数，而是在数据层把同一条输入重复 n 次，每次请求仍只采一个样本。配合 `temperature > 0`，n 份相同输入会得到 n 份不同输出。这个设计让引擎保持「一次请求＝一个样本」的极简模型，采样倍数完全交给数据复制控制——`main.py` 那边验证阶段的 `--n {args.n_verification_per_proof}`（run.sh 里是 64）就是靠这里膨胀成 64 次重复验证请求的。

由于跳过判断发生在**投递前**，已完成批次连序列化进队列的开销都没有，这就是「已完成批次去重」的实现位置（请求级的去重）。

#### 4.3.2 核心流程

```
逐行读 input.jsonl（tqdm "Waiting Input"）
   item = json.loads(line)
      │
      ▼  for i in range(n):        ← 复制 n 份
   submit_batch.append(item)
      │
      ▼  len(submit_batch) >= batch_size
   batch_idx ∈ complete_batches？
      ├─ 否 → input_queue.put((batch_idx, submit_batch)); num_input += batch_size
      └─ 是 → num_skip += batch_size                ← 断点续跑：直接跳过
      │
      ▼  batch_idx += 1; submit_batch = []          ← 批次号只增不减
（循环结束后尾巴批同样判断一次）
```

计数关系（\( L \) 为输入行数，\( b \) 为 batch_size）：

\[ \text{复制品总数} = nL, \qquad \text{批次数} = \left\lceil \frac{nL}{b} \right\rceil, \qquad \text{API 请求数} = nL \]

第二个等式成立因为每个复制品恰好触发一次 `chat.completions.create`（u2-l1 的 `generate_one`）。这也是 u1-l3 计数公式「输出条数＝输入条数×n」的机制源头。

#### 4.3.3 源码精读

建队列、开文件（注意输出是 `"a+"` 追加模式）、按个数拉起 worker 进程：

[inference/generate.py:L123-L132](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7fa8a8b244ea313f3cfcc346f/inference/generate.py#L123-L132)

```python
    input_queue, output_queue = Queue(), Queue()
    fr = open(input_data_path, "r", encoding="utf-8")
    fw = open(output_data_path, "a+", encoding="utf-8")

    processes = []
    
    for i in range(num_processes):
        process = Process(target=mp_generate_loop, args=(input_queue, output_queue, sampling_params))
        process.start()
        processes.append(process)
```

分发循环的核心——复制、攒批、按 `complete_batches` 跳过：

[inference/generate.py:L139-L150](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L139-L150)

```python
    for line in tqdm(fr, desc="Waiting Input"):
        item = json.loads(line)
        for i in range(n):
            submit_batch.append(item)
            if len(submit_batch) >= batch_size:
                if batch_idx not in meta_data["complete_batches"]:
                    num_input += batch_size
                    input_queue.put((batch_idx, submit_batch))
                else:
                    num_skip += batch_size
                batch_idx += 1
                submit_batch = []
```

注意 `batch_idx += 1` 无条件执行——被跳过的批次**也占用批次号**，这保证批次号在任何一次运行中都与数据一一对应，正是 4.2 那个 `assert` 想守护的不变量。尾巴批（不足 `batch_size`）单独处理：

[inference/generate.py:L151-L157](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L151-L157)

```python
    if len(submit_batch) > 0:
        if batch_idx not in meta_data["complete_batches"]:
            input_queue.put((batch_idx, submit_batch))
            num_input += len(submit_batch)
        else:
            num_skip += len(submit_batch)
    print(f"Total Input Samples: {num_input} (Skip {num_skip} Samples)")
```

`main.py` 侧的调用证据——`--n` 的三个上游取值分别来自证明生成、证明验证、元验证阶段（此处列验证阶段一处）：

[inference/main.py:L489](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L489)

```python
        --n {args.n_verification_per_proof}
```

#### 4.3.4 代码实践

**实践目标**：单独实现并观察「复制 n 份 + 切批 + 跳过」逻辑，不涉及任何 API。

**操作步骤**：把下面脚本存为 `replicate_and_batch.py`（示例代码，可在仓库外任意目录运行），用一个假想的 `complete_batches` 模拟「第 1、3 批已完成」：

```python
import json

n, batch_size, complete_batches = 3, 4, {1, 3}

submit_batch, batch_idx, num_input, num_skip = [], 0, 0, 0
for line in ["{\"id\": %d}\n" % i for i in range(7)]:
    item = json.loads(line)
    for _ in range(n):
        submit_batch.append(item)
        if len(submit_batch) >= batch_size:
            if batch_idx not in complete_batches:
                num_input += batch_size
                print(f"提交批次 {batch_idx}: {batch_size} 条")
            else:
                num_skip += batch_size
                print(f"跳过批次 {batch_idx}: {batch_size} 条")
            batch_idx += 1
            submit_batch = []
if submit_batch:
    if batch_idx not in complete_batches:
        num_input += len(submit_batch)
        print(f"提交尾巴批 {batch_idx}: {len(submit_batch)} 条")
    else:
        num_skip += len(submit_batch)
print(f"Total Input Samples: {num_input} (Skip {num_skip} Samples)")
```

**需要观察的现象**：6 个批次中 1、3 号被跳过；尾巴批是 1 条；末行统计 `Total Input Samples: 13 (Skip 8 Samples)`。

**预期结果**：`13 + 8 = 21 = 3 × 7`，即「提交 + 跳过 = 复制品总数」恒成立；跳过的批次照样消耗批次号。与 4.2.4 的手算对上即通过。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接给 API 传原生的 `n=64` 参数，而要在数据层复制 64 份？

**答案**：数据层复制让引擎保持「一个请求一个样本」的最简契约：`generate_one` 无需处理多选择结构，重试/断点/批次的粒度都统一到「条」；同时兼容任何只支持 `n=1` 的 OpenAI 兼容服务（自建 vLLM/SGLang 对 `n` 支持程度不一）。代价是请求体重复传输 prompt、失去服务端批量采样的计算共享（同一 prompt 的 64 次采样在 prefix 缓存不命中时各自完整计算）。这是典型的「工程简单性换理论效率」的取舍。

**练习 2**：`num_input + num_skip` 在什么条件下恒等于 `n × 输入行数`？会不会有第三种计数？

**答案**：无条件恒等。每条复制品要么进被提交的批次（计入 `num_input`），要么进被跳过的批次（计入 `num_skip`），分发循环对复制品的覆盖是完整且不重叠的；尾巴批的 `if/else` 两个分支也各自计数。所以这个恒等式可以当作断点续跑正确性的快速自检。

**练习 3**：分发循环里 `input_queue.put` 会因为队列满而阻塞吗？

**答案**：不会。`Queue()` 不传 `maxsize` 时是无界队列，`put` 不阻塞。代价是极端情况下所有待办批次都会堆在队列里占用内存；对竞赛规模（几百题 × 几十复制品）完全无害。若要防内存膨胀，可以 `Queue(maxsize=...)` 让生产者背压。

### 4.4 收尾循环：落盘、`complete_batches` 更新与进程汇合

#### 4.4.1 概念说明

投完批次后主进程做三件事：给每个 worker 发一份哨兵；进入收尾循环从 `output_queue` 收结果；每收到一个批次就**先逐行写入输出文件（立即 flush）、再把批次号加进 `complete_batches` 并整体重写 `.meta`**。最后 `join` 所有进程。

这里藏着本讲最重要的工程细节——**提交与确认的顺序**决定了容错语义：

- 一个批次被确认为「完成」的时机是「全部行已 flush 到磁盘 **且** meta 已重写」。崩溃发生在确认之后：重启时该批次被跳过，幂等成立。
- 崩溃发生在确认**之前**（哪怕部分行已经写进文件）：该批次不在 `complete_batches` 里，重启后整批重跑——而输出文件是 `"a+"` 追加打开的，**先前已 flush 的部分行不会消失**，于是同一批次的部分行 + 重跑后的完整行 = **重复行**。

也就是说这套协议是「批次级 at-least-once」，在「半批落盘」这个窗口内不幂等。`main.py` 阶段产物的行数在正常路径上恰好等于 `输入条数 × n`，但经历半批崩溃后可能偏多；下游按 jsonl 逐行消费不会崩，但统计与聚合会被重复行污染。（综合实践会带你复现并修复这个窗口。）

另外注意结果的**全局顺序是不确定的**：哪个批次先完成先落盘，批次间顺序取决于调度；但批次内部顺序由 `asyncio.gather` 保证与输入一致（u2-l1）。下游所有消费都按行自描述字段（`problem_idx` 等）工作，不依赖全局顺序。

#### 4.4.2 核心流程

```
input_queue.put((None, None)) × num_processes     ← 每个 worker 一份哨兵
   │
   ▼
收尾循环（tqdm "Waiting Output", total=num_input）
   output_queue.get()
      ├─ output_data is None → remain_processes -= 1（某 worker 已退出）
      └─ 正常结果：
            ① 逐行 print(json.dumps(item), file=fw, flush=True)   ← 立即落盘
            ② complete_batches.add(batch_idx)
            ③ 整体重写 .meta（pickle.dump）                       ← 确认点
            ④ fw.flush()
   │
   ▼ remain_processes == 0 时退出循环
print Total Output Samples → fw.close() → join 全部进程
```

容错语义的形式化：设批次 \( b \) 的确认点为「步骤 ③ 完成的时刻」\( t_b \)。

\[ \text{重启后批次 } b \text{ 被重跑} \iff \text{崩溃时刻} < t_b \]

而步骤 ① 的部分行在崩溃时刻可能已持久化，故重复行仅在「步骤 ① 进行中崩溃」时出现——窗口为单个批次的写入时长。

#### 4.4.3 源码精读

发哨兵——一 worker 一份：

[inference/generate.py:L160-L161](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L160-L161)

```python
    for i in range(num_processes):
        input_queue.put((None, None))
```

收尾循环——回声计数、逐行落盘、确认批次、汇合进程：

[inference/generate.py:L163-L181](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/generate.py#L163-L181)

```python
    remain_processes = num_processes
    num_output = 0
    with tqdm(desc="Waiting Output", total=num_input) as pbar:
        while remain_processes > 0:
            batch_idx, output_data = output_queue.get()
            if output_data is None:
                remain_processes -= 1
                continue
            for item in output_data:
                print(json.dumps(item, ensure_ascii=False), file=fw, flush=True)
                num_output += 1
                pbar.update(1)
            meta_data["complete_batches"].add(batch_idx)
            with open(meta_data_path, "wb") as f:
                pickle.dump(meta_data, f)
            fw.flush()
    print(f"Total Output Samples: {num_output}")
    fw.close()
    [process.join() for process in processes]
```

三个值得停留的细节：

1. `print(..., flush=True)` 每行强制刷盘，行级持久化；末尾的 `fw.flush()` 是冗余的保险。
2. `.meta` 是**整体重写**（`"wb"` 模式）而非增量追加，原子性依赖「重写一个很小的文件耗时极短」这一事实，没有用临时文件 + `os.replace` 的原子替换。对几百个批次号的集合来说实际风险很低。
3. `while remain_processes > 0` 而不是 `while num_output < num_input`：以「所有 worker 都退出」为终止条件更稳——即使某批次结果丢失（如 4.4.1 讨论的崩溃场景重启前的残留队列），循环也不会永久卡住等凑数。

#### 4.4.4 代码实践

**实践目标**：亲眼看到「半批落盘 + meta 未更新」窗口造成的重复行（本实践是综合实践修复任务的问题复现半成品）。

**操作步骤**：先完成第 5 节的 `reproduce_resume.py`，然后在其收尾循环里插入一行模拟半批崩溃（示例代码，改动只加在你自己的副本上）：

```python
            for k, item in enumerate(output_data):
                if k == 2 and os.environ.get("CRASH_MID_BATCH"):
                    import sys; sys.exit(1)          # 第 3 行已写入、批次未确认
                print(json.dumps(item, ensure_ascii=False), file=fw, flush=True)
```

以 `CRASH_MID_BATCH=1` 运行一次（进程退出码非 0），再不带该环境变量重跑一次，最后 `wc -l` 统计输出行数、用 `sort | uniq -d` 查看重复行。

**需要观察的现象**：崩溃那次已写入若干行；重跑后该批次整批重写，总行数**超过** \( n \times L \)；`uniq -d` 能看到内容完全相同的重复行（同一 `line_id` 的输出）。

**预期结果**：证明该协议的幂等性边界在「批次级」而非「行级」。若你的崩溃恰好在行写入之间，重复行数 = 崩溃时该批次已写入的行数。待本地验证（具体重复几行取决于崩溃时机）。

#### 4.4.5 小练习与答案

**练习 1**：为什么终止条件用 `remain_processes == 0` 而不是「收够了 `num_input` 条」？

**答案**：`num_input` 是**本趟提交**的条数，而 output_queue 里可能混有上一趟崩溃遗留语义之外的情况（如本趟某些批次结果因为主进程崩溃从未被收取）。以 worker 全部退出为终止条件，语义是「队列里再也不会有新消息」，既不会提前退出也不会永久等待；代价是要靠 `num_output` 事后核对是否收齐。

**练习 2**：主进程在收尾循环中被 Ctrl+C 杀死，worker 进程会怎样？

**答案**：worker 是非 daemon 进程，会继续阻塞在 `input_queue.get()` 上成为孤儿进程（哨兵还没发或没发全）。在终端里 Ctrl+C 通常以信号发给整个前台进程组，子进程一起收到 SIGINT 才退出；但通过脚本/CI 杀掉主进程 PID 时孤儿会残留，需要 `pkill -f generate.py` 清理。`.meta` 因为是逐批确认的，重启后仍能正确续跑——孤儿进程问题影响的是机器资源，不是断点正确性。

**练习 3**：如何用最小改动把「半批崩溃产生重复行」的窗口堵上？给出方案思路即可。

**答案**：思路一（检查点截断）：meta 里额外记录「已确认落盘的行数」，启动时用 `os.truncate`/seek 把输出文件截断到该行数再追加，重跑批次从头写。思路二（临时文件原子替换）：每个批次先写 `batch.tmp`，全批写完再以追加方式合并进主文件并立即确认 meta。思路三（行级确认）：每行落盘即更新 meta（代价是 meta 重写频率 ×batch_size）。思路一改动最小：一个字段 + 启动时一次截断。

## 5. 综合实践

**任务**：编写 `reproduce_resume.py`，用 `multiprocessing.Queue` 与两个子进程离线复刻 generate.py 的「分发—哨兵—收集—meta 确认」协议（把 `APIModel.generate` 换成假生成函数，全程不需要 API Key），先在半途模拟主进程崩溃，再重跑验证断点续跑的三条性质：已完成批次被跳过、总行数恰为 \( n \times L \)、批间无重复。

**操作步骤**：

1. 准备输入 `toy_input.jsonl`（7 行）：

```bash
python -c "print('\n'.join('{\"line_id\": %d}' % i for i in range(7)))" > toy_input.jsonl
```

2. 编写 `reproduce_resume.py`（示例代码，逐行对应 generate.py 的 L104-L181）：

```python
import os, sys, json, pickle
from multiprocessing import Queue, Process
from time import sleep

CRASH_AFTER = int(os.environ.get("CRASH_AFTER", "0"))   # >0：收到第 K 批后模拟主进程被 Ctrl+C

def fake_generate(batch):                                # 替代 APIModel.generate（无网络）
    return [{**item, "output": "proof-of-%d" % item["line_id"], "finish_reason": "stop"} for item in batch]

def mp_generate_loop(input_queue, output_queue):         # 对应 generate.py L78-L81 + L68-L75
    sleep(0.2)                                           # 缩小版的 sleep(5)
    while True:
        batch_idx, input_data = input_queue.get()
        if input_data is None:
            output_queue.put((batch_idx, None))          # 哨兵回声
            break
        output_queue.put((batch_idx, fake_generate(input_data)))

def main(input_path, output_path, n=3, batch_size=4, num_processes=2):
    meta_path = f"{output_path}.meta"                    # 对应 L104-L111
    if not os.path.exists(meta_path):
        with open(meta_path, "wb") as f:
            pickle.dump({"n": n, "batch_size": batch_size, "complete_batches": []}, f)
    with open(meta_path, "rb") as f:
        meta = pickle.load(f)
    meta["complete_batches"] = set(meta["complete_batches"])
    assert n == meta["n"] and batch_size == meta["batch_size"]

    input_queue, output_queue = Queue(), Queue()
    fr = open(input_path, encoding="utf-8")
    fw = open(output_path, "a+", encoding="utf-8")       # 追加模式是续跑前提
    processes = [Process(target=mp_generate_loop, args=(input_queue, output_queue))
                 for _ in range(num_processes)]
    [p.start() for p in processes]

    submit_batch, batch_idx, num_input, num_skip = [], 0, 0, 0
    for line in fr:                                      # 对应 L139-L157：复制 n 份、切批、跳过
        item = json.loads(line)
        for _ in range(n):
            submit_batch.append(item)
            if len(submit_batch) >= batch_size:
                if batch_idx not in meta["complete_batches"]:
                    num_input += batch_size
                    input_queue.put((batch_idx, submit_batch))
                else:
                    num_skip += batch_size
                batch_idx, submit_batch = batch_idx + 1, []
    if submit_batch:
        if batch_idx not in meta["complete_batches"]:
            input_queue.put((batch_idx, submit_batch)); num_input += len(submit_batch)
        else:
            num_skip += len(submit_batch)
    print(f"Total Input Samples: {num_input} (Skip {num_skip} Samples)")
    fr.close()
    for _ in range(num_processes):                       # 对应 L160-L161：发哨兵
        input_queue.put((None, None))

    done, remain, num_output = 0, num_processes, 0
    try:                                                 # 对应 L163-L179：收尾循环
        while remain > 0:
            bidx, output_data = output_queue.get()
            if output_data is None:
                remain -= 1
                continue
            for item in output_data:
                print(json.dumps(item, ensure_ascii=False), file=fw, flush=True)
                num_output += 1
            meta["complete_batches"].add(bidx)
            with open(meta_path, "wb") as f:
                pickle.dump(meta, f)
            done += 1
            if CRASH_AFTER and done >= CRASH_AFTER:
                raise KeyboardInterrupt                   # 模拟主进程在批次确认后被杀
    except KeyboardInterrupt:
        print(f"[模拟崩溃] 已确认 {done} 个批次后中断")
    finally:                                             # 真实 Ctrl+C 依赖进程组信号，这里显式收尾防孤儿
        fw.close()
        for p in processes: p.terminate()
        for p in processes: p.join()
    print(f"Total Output Samples: {num_output}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

3. 第一次运行，收到 2 个批次后崩溃：

```bash
CRASH_AFTER=2 python reproduce_resume.py toy_input.jsonl toy_output.jsonl
```

4. 检查 `.meta` 里的 `complete_batches`：

```bash
python -c "import pickle; m=pickle.load(open('toy_output.jsonl.meta','rb')); print(sorted(m['complete_batches']))"
wc -l toy_output.jsonl
```

5. 第二次完整运行，并验证三条性质：

```bash
python reproduce_resume.py toy_input.jsonl toy_output.jsonl
wc -l toy_output.jsonl        # 应恰为 21 = 3 × 7
sort toy_output.jsonl | uniq -d | wc -l   # 应为 0（无重复行）
```

**需要观察的现象**：

- 第 3 步输出 `[模拟崩溃] 已确认 2 个批次后中断`，`toy_output.jsonl` 只有 8 行左右（2 个满批），`complete_batches` 含 2 个批次号（具体是哪两个取决于两个 worker 的抢占顺序，不一定是 0 和 1）。
- 第 5 步首行 `Total Input Samples: 13 (Skip 8 Samples)`——已完成批次在**投递前**就被跳过；结束时 `Total Output Samples: 13`。
- 最终 `toy_output.jsonl` 共 21 行，且无重复行。

**预期结果**：三条性质全部成立——断点续跑跳过已完成批次（去重发生在提交侧）、追加写不清空旧结果、总条数收敛到 \( n \times L = 21 \)。**待本地验证**：批次完成顺序受进程调度影响，中间数字可能不同，但最终 21 行、0 重复是确定的。进阶：叠加 4.4.4 的 `CRASH_MID_BATCH` 实验，观察总行数**超过** 21 且出现重复行，然后用练习 3 的「检查点截断」思路改造本脚本（meta 记录已确认行数、启动时截断输出文件），使任意时机崩溃后重跑都严格收敛到 21 行。

## 6. 本讲小结

- **两级并发**：进程级（`num_processes` 个 worker 抢 `input_queue` 里的批次）+ 协程级（每批次内部 `asyncio.gather` 并发 `batch_size` 个请求），`mp_generate_loop` 在每个子进程内 `sleep(5)` 后自建 `APIModel` 与事件循环，避免客户端跨进程共享和启动风暴。
- **队列协议**：任务 `(batch_idx, items)`、哨兵 `(None, None)` 每 worker 一份、结果 `(batch_idx, outputs)`、回声 `(None, None)` 供主进程倒数 `remain_processes` 确认全员退出；所有消息都是二元组，靠元组解包消费。
- **断点档案**：`{输出路径}.meta` 以 pickle 存 `n`、`batch_size`、`complete_batches`；批次确认点 = 「整批行 flush 落盘之后、meta 整体重写之时」，跳过判断发生在投递前，因此已完成批次既不重发请求也不重写行。
- **参数不变量**：`batch_idx` 到数据的映射由 `n` 和 `batch_size` 唯一决定（被跳过的批次也占号），所以续跑改这两个参数会被 `assert` 拦下并提示删文件重跑。
- **n 份复制即多次采样**：不用 API 原生 `n` 参数，而在数据层把每行复制 \( n \) 份，计数公式为：复制品总数 = 请求数 = \( nL \)，批次数 = \( \lceil nL/b \rceil \)。
- **已知弱点**：协议是批次级 at-least-once——「半批已落盘、meta 未更新」窗口内崩溃，重跑会整批重写而旧的部分行残留（`"a+"` 追加），产生重复行；可用 meta 记录已确认行数 + 启动时截断输出文件修复。

## 7. 下一步学习建议

到这里，generate.py 这个「流水线唯一发请求的引擎」已经全部读完：u2-l1 讲它单进程内的异步抽象，本讲讲它多进程分发与断点续跑的工程骨架。下一讲进入 **u3-l1「四大提示词模板：math_templates.py 逐段精读」**：队列里流动的 `input.jsonl` 每一行终究要变成发给模型的提示词——建议先自己浏览 [inference/math_templates.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/math_templates.py)，数一数里面定义了几个模板字符串、各自要 `format` 哪些占位符，再带着「验证器怎么给证明打 0/0.5/1 分」这个问题去读。如果想在读模板前巩固本讲，可以把综合实践的 `reproduce_resume.py` 改造成支持 `--num_processes 4` 与不同 `batch_size` 的版本，观察跳过行为与批次边界的变化。
