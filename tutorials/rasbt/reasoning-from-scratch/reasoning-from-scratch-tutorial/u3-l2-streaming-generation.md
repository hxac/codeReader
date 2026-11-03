# 流式生成与生成函数族

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ch02.py` 中四代生成函数（`generate_text_basic` → `generate_text_basic_cache` → `generate_text_basic_stream` → `generate_text_basic_stream_cache`）各自解决了什么问题、牺牲了什么。
2. 理解 Python `yield` 的工作方式：为什么一个带 `yield` 的函数「调用时不执行、消费时才推进」，以及这如何带来「第一个 token 立刻可见」的流式体验。
3. 解释 `generate_text_basic_stream_cache` 中 KV cache 的完整生命周期：谁创建、谁重置、谁销毁，以及为什么把缓存管理收进函数内部是安全的。
4. 读懂「跳过最后一次前向」这个看似不起眼、实则处处出现的小优化（它同样出现在 `generate_text_basic_cache` 和 `chat_multiturn.py` 里）。
5. 会用 `generate_stats` 统计生成耗时、tokens/sec 吞吐与 CUDA/XPU 峰值显存，并理解为什么统计前要先 `synchronize`。

## 2. 前置知识

本讲站在前两讲的基础上，先快速回顾三个概念。

**（1）Python generator（生成器）：被「消费」才执行的函数。**
普通函数调用后一口气跑完并返回结果；而函数体内一旦出现 `yield` 关键字，调用它得到的不是一个结果，而是一个「生成器对象」。每次向它索取下一个值（`for ... in` 循环或 `next()`），函数体才从上一次暂停的位置继续执行到下一个 `yield`，把值交出来后再暂停。这在 Python 里叫**惰性求值**（lazy evaluation）。对文本生成来说，它意味着：生成第 1 个 token 就可以立刻打印给用户，不必等全部 2048 个 token 算完——这正是 ChatGPT 那种「逐字蹦出来」体验的底层机制。

**（2）贪心解码（上一讲 u3-l1）。**
每步取模型输出 logits 的 `argmax` 作为下一个 token，输出确定、可复现。上一讲的结论是：不带 KV cache 时，设 prompt 长为 \(T\)、生成 \(N\) 个 token，第 \(k\) 步前向必须处理整段 \(T+k\) 个 token，总前向 token 量约为 \(TN + \sum_{k=1}^{N}k\)，生成量翻倍、耗时超翻倍。

**（3）KV cache 与位置游标（u2-l5）。**
带 cache 的生成分两阶段：**prefill**（整段 prompt 一次前向，全部位置的 K/V 写入缓存，模型游标 `current_pos` 从 0 前进到 prompt 长度）和 **decode**（每步只送 1 个新 token，旧 K/V 从缓存读取）。两条铁律：`KVCache` 容器与 `model.reset_kv_cache()` 必须**成对出现**；`current_pos` 在前向成功后才更新。本讲会看到这两条铁律在生成函数里的具体落法。

**（4）异步后端与计时。**
CUDA 等 GPU 后端的计算是**异步提交**的：Python 代码发出指令后立刻返回，GPU 在后台慢慢算。所以「掐表」量 GPU 耗时前必须先 `synchronize`（等 GPU 真正干完），否则量到的是「提交指令」的时间而非计算时间。这解释了 `generate_stats` 里的一行 `backend.synchronize()`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 本讲主角：四代生成函数与 `generate_stats` 全部在此 |
| [ch02/05_use_model/chat.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat.py) | 交互式 REPL，示范「消费 generator + 实时 decode + generate_stats」的标准姿势 |
| [ch02/05_use_model/generate_simple.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py) | 单轮版最小示例（u1-l2 已跑通），本讲引用其消费循环作对照 |
| [ch02/05_use_model/chat_multiturn.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py) | 多轮记忆版，展示「提前 break 生成器」与上下文裁剪 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 提供 `KVCache`、`Qwen3Model.forward` 的 `current_pos` 逻辑与 `reset_kv_cache`（复习 u2-l5） |
| [tests/test_ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py) | 用 DummyModel 驱动生成函数的单测，本讲实践会借鉴这一手法 |

## 4. 核心概念与源码讲解

先给四代函数拍一张「全家福」，后面三节再逐个精读。注意 `ch02.py` 源码里保留着一段演化注释：

[ch02.py:L60-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L60-L63) —— 作者说明：早期版本用非流式函数，`*_stream` 系列原本是练习题；`generate_text_basic_cache` 虽已不再被主流程使用，但为向后兼容而保留。所以「四代同堂」不是冗余，而是**教学演化路径的活化石**。

| 函数 | 交出结果的方式 | KV cache | 前向方式 | 一句话定位 |
| --- | --- | --- | --- | --- |
| `generate_text_basic`（L42） | `return` 整段张量 | 无 | 每步整段前向 | 上一讲的基准版 |
| `generate_text_basic_cache`（L64） | `return` 整段张量 | 有 | prefill + 每 token 单步前向 | 首次引入「跳过最后一次前向」 |
| `generate_text_basic_stream`（L94） | `yield` 逐 token | 无 | 每步整段前向 | 引入流式，先体验再优化 |
| `generate_text_basic_stream_cache`（L116） | `yield` 逐 token | 有 | prefill + 每 token 单步前向 | 仓库主力，`generate_simple.py`/`chat.py` 都用它 |

可以看到这是两个正交维度的组合：**「是否流式」×「是否用缓存」**。仓库最终量产的是第四代。

### 4.1 generate_text_basic_stream：用 yield 逐 token 产出

#### 4.1.1 概念说明

`generate_text_basic_stream` 解决的问题是**首 token 延迟**（time to first token）。非流式函数必须把所有 token 算完才返回；而聊天场景里，用户盯着空屏幕等待 30 秒的体验远差于「第 1 秒开始逐字蹦出」。把 `return` 换成 `yield`，函数就从「一次性交付」变成「流水线交付」：每生产一个 token 立即交货，消费端（终端、Web 前端）拿到就显示。

需要理解的 Python 语义：**含 `yield` 的函数体在调用时不执行任何一行**。`gen = generate_text_basic_stream(...)` 只得到生成器对象；`for token in gen:` 每转一圈，函数体才执行到下一个 `yield` 处暂停。因此「打印第一个 token」发生在「计算第二个 token」之前。

#### 4.1.2 核心流程

```text
输入: model, token_ids (1, T), max_new_tokens=N, eos_token_id
循环 N 次:
    1. 整段前向: out = model(token_ids)[:, -1]        # 只取最后一个位置的 logits
    2. next_token = argmax(out)                        # 贪心，形状 (1, 1)
    3. 若 next_token == eos_token_id: 跳出循环（EOS 不产出）
    4. yield next_token                                # 立刻交给消费端 ⬅ 暂停点
    5. token_ids = cat([token_ids, next_token])        # 消费端要下一个值时才恢复执行
```

对比上一讲的 `generate_text_basic`：骨架完全相同，只是「append 到列表、最后 return」换成了「当场 yield」。计算量没有任何节省——每步仍是整段前向，本函数的价值纯粹在**交互体验**上。

#### 4.1.3 源码精读

主循环在 [ch02.py:L94-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L94-L113)，函数体仅 10 行左右：

[ch02.py:L103-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L103-L113) —— 每步整段前向取末位 logits，argmax 得到 `next_token`；EOS 检查通过则 `break`；否则先 `yield` 交货，再把它拼回 `token_ids` 供下一轮前向使用。

三个值得咀嚼的细节：

1. **`yield` 在 `torch.cat` 之前**。消费端拿到第 k 个 token 时，第 k+1 步的前向还没有发生。若消费端 `break` 提前退出，生成器对象被丢弃，函数体停在 `yield` 处永远不再恢复——拼接代码不再执行也无妨，因为所有状态（`token_ids`）都是函数局部变量，随生成器一起被垃圾回收。
2. **EOS 判断写法变了**。本函数用 `torch.all(next_token == eos_token_id)`（L107-L109），而上一讲的 `generate_text_basic` 用 `next_token.item()`（L52-L53）。`.item()` 隐含假设 batch 大小为 1；`torch.all` 则天然适配将来 batch>1 的场景（「所有序列都到 EOS 才停」）。
3. **无 cache 路径不碰位置游标**。由于调用 `model(token_ids)` 时不传 `cache`，[qwen3.py:L64](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L64) 中 `pos_start` 取 0，且 [qwen3.py:L72-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L72-L73) 只在 `cache is not None` 时更新 `current_pos`——所以这个函数即使中途被丢弃，模型对象上也不留任何脏状态。

#### 4.1.4 代码实践

**目标**：不下载权重，用一个「假模型」亲眼看到 generator 的惰性行为——消费一个、算一个。

**步骤**（以下为示例代码，可保存为 `tutorial_stream_demo.py` 在仓库根目录运行）：

```python
# 示例代码：借鉴 tests/test_ch02.py 的 DummyModel 手法
import torch
from reasoning_from_scratch.ch02 import generate_text_basic_stream

class CountingModel:
    """每次前向计数 +1，且永远把 token 2 推为最高分。"""
    def __init__(self):
        self.forward_calls = 0
        self.eval_called = False
    def eval(self):
        self.eval_called = True
        return self
    def __call__(self, token_ids):
        self.forward_calls += 1          # 观察：前向发生了几次？
        b, t = token_ids.shape
        out = torch.zeros(b, t, 5)
        out[..., 2] = 1.0                # argmax 恒为 token 2
        return out

m = CountingModel()
gen = generate_text_basic_stream(m, torch.tensor([[7, 8]]), max_new_tokens=3)

print("创建 generator 后、消费前：forward_calls =", m.forward_calls)   # 预期 0
first = next(gen)
print("拿到第 1 个 token 后：      forward_calls =", m.forward_calls)   # 预期 1
rest = [t for t in gen]                                               # 消费剩余
print("消费完剩余 token 后：      forward_calls =", m.forward_calls)   # 预期 3
print("全部产出:", torch.cat([first] + rest, dim=1))
```

**需要观察的现象**：
- 第一次打印是 0——证明「调用生成函数」本身不触发任何计算；
- `next(gen)` 一次只推进一个 token 的计算量。

**预期结果**：三行计数分别为 `0`、`1`、`3`；产出为 3 个 `2`。（本实践只依赖 torch 与本地包，逻辑与 `tests/test_ch02.py` 的 Dummy 手法一致，可本地直接验证。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `generate_text_basic_stream` 装进 `list()`，它和 `generate_text_basic` 的返回值有什么区别与联系？

**答案**：两者产出的 token 序列完全相同（同样的贪心 argmax、同样的 EOS 提前停止、EOS 都不计入产出）。区别在「交货方式」：`list(gen)` 前每个 token 已被逐个 yield（消费端有机会逐个处理），而 `generate_text_basic` 是最后一次性 `return` 张量；另外 `generate_text_basic` 返回张量形状 `(1, N)`，`list()` 得到的是 N 个 `(1, 1)` 张量的列表，需 `torch.cat` 才能对齐。

**练习 2**：消费端 `for token in gen: break` 之后，生成器里「已经 yield 的那个 token 会不会被拼接进 `token_ids`」？为什么这不会造成 bug？

**答案**：不会。`break` 后生成器对象失去引用（或被 GC 关闭），函数体停在 `yield` 处，其后的 `token_ids = torch.cat(...)` 永不执行。因为 `token_ids` 是生成器的局部变量、模型也不携带游标状态（无 cache 路径不更新 `current_pos`），生成器连同其状态一起被回收，不留任何副作用。

**练习 3**：本函数每个 token 的「交付延迟」和「总耗时」分别主要由什么决定？

**答案**：交付第 k 个 token 需要完成第 k 次整段前向（输入长 \(T+k\)），所以单 token 延迟随已生成长度近似线性增长；总耗时仍是上一讲推导的 \(TN + \sum_{k=1}^{N}k\) 量级 token 前向量。流式只改善「感知延迟」，不改善计算量——这正是下一节引入缓存的动机。

### 4.2 generate_text_basic_stream_cache：流式 × KV 缓存

#### 4.2.1 概念说明

第四代函数把两件事叠加：对外 `yield` 逐 token 流式，对内用 KV cache 把每步前向从「整段 \(T+k\) 个 token」降到「1 个 token」。它还悄悄完成了第三件事：**把 cache 的生命周期完全收编到函数内部**——调用方看不到 cache、不需要初始化、也不会忘记重置。这是一种「资源获取即初始化」（RAII 式）的接口设计：函数既创建缓存，也负责重置模型游标，用完即弃。

另外一个反复出现的小优化是**跳过最后一次前向**：产出最后一个 token 之后，再前向一次得到的 logits 没有任何消费者，纯属浪费，所以用 `if step + 1 < max_new_tokens` 挡掉。

#### 4.2.2 核心流程

```text
阶段 0（函数开头，每次调用都执行）:
    cache = KVCache(n_layers)          # 新建每层一个槽位的空缓存
    model.reset_kv_cache()             # 位置游标 current_pos 归零 —— 与新建缓存成对
阶段 1（prefill，恰好一次）:
    out = model(token_ids, cache=cache)[:, -1]
    # 整段 prompt 一次前向；K/V 全部入缓存；current_pos: 0 → T
阶段 2（decode，循环 N 次）:
    next_token = argmax(out)
    若 == eos_token_id: break
    yield next_token                   # 流式交货
    若 step+1 < N:                     # ⬅ 跳过最后一次前向
        out = model(next_token, cache=cache)[:, -1]
        # 只送 1 个新 token；旧 K/V 从缓存读；current_pos 每次 +1
```

前向量对比（设 prompt 长 \(T\)、生成 \(N\) 个 token）：

- 无缓存流式版：\(N\) 次前向，处理 token 总量 \(\sum_{k=1}^{N}(T+k) = TN + \frac{N(N+1)}{2}\)；
- 缓存版：1 次 \(T\)-token 的 prefill，加 \(N-1\) 次 1-token 前向（最后一个 token 产出后不再前向），总量 \(T + (N-1)\)。

生成越长，差距越悬殊——这就是 `chat.py` 敢把默认 `max_new_tokens` 设为 2048 的底气。

#### 4.2.3 源码精读

完整函数在 [ch02.py:L116-L141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L116-L141)。分三段读：

[ch02.py:L124-L128](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L124-L128) —— **cache 生命周期的起点**：每次调用都新建 `KVCache`（该容器在 [qwen3.py:L422-L437](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L422-L437)，本质是长度为 `n_layers` 的列表，每层存一个 `(keys, values)` 元组），并调用 `model.reset_kv_cache()` 把 [qwen3.py:L104-L105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L104-L105) 中的位置游标 `current_pos` 归零。新建容器 + 重置游标成对出现，正是 u2-l5 总结的铁律；放在函数开头意味着**即使上一次调用中途被 break 留下了脏游标，本次调用也会自愈**。随后整段 prompt 一次性前向完成 prefill。

[ch02.py:L129-L139](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L129-L139) —— **decode 主循环**：argmax、EOS 提前停止（`torch.all` 写法）、`yield` 交货；关键在最后一行的条件前向——只有 `step + 1 < max_new_tokens` 时才把刚产出的单 token 送进模型（此时输入张量形状为 `(1, 1)`，模型从 `current_pos` 处继续，旧 K/V 全部来自缓存）。产出最后一个 token 后循环结束，这一次「没有人消费的前向」被省掉了。

[ch02.py:L123](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L123)、[ch02.py:L137](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L137)、[ch02.py:L141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L141) —— 三行被注释掉的旧代码（`input_length`、`token_ids = torch.cat(...)`、`return ...`）是第三版向第四版演化的痕迹：改用缓存后不再需要拼接整段序列，改用 `yield` 后不再需要返回切片。读源码时，注释掉的行往往是最诚实的变更日志。

**同一优化在别处的回声**：非流式的 `generate_text_basic_cache` 在 [ch02.py:L86-L87](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L86-L87) 用同样的条件跳过最后一次前向（注释原文 "Skips the forward pass after the last token is generated"）；多轮聊天脚本 `chat_multiturn.py` 的裁剪函数在 [chat_multiturn.py:L123-L133](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L123-L133) 计算可保留长度时也特意注明「最后一个生成的 token 不需要再前向一次」（`keep_len` 里那个 `max_new_tokens - 1`）。一个概念在代码库里重复出现三次，说明它是系统性的，不是巧合。

**中途 break 的后果**：消费端提前退出（例如 `chat_multiturn.py` 在 [chat_multiturn.py:L156-L164](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat_multiturn.py#L156-L164) 命中自定义停止 token 时手动 `break`）后，生成器对象带着 cache 被回收，但模型上的 `current_pos` 停在半路、不为 0。这不构成 bug 的原因正是本函数开头的 `reset_kv_cache()`：下一次调用会先清零。这也解释了为什么多轮对话**每轮都重新 prefill 全部历史**，而不是跨轮复用缓存——缓存随生成器销毁了，复用需要更复杂的生命周期管理（那正是 chG 聊天界面一讲的主题）。

#### 4.2.4 代码实践

**目标**：验证「缓存版与无缓存版产出一致」并实测加速比；再用 DummyModel 数出「跳过最后一次前向」省下的那次前向。

**步骤 A：数前向次数（无需权重，可本地直接验证）**。

```python
# 示例代码：延续 4.1.4 的 Dummy 手法，为缓存路径补上 cfg 与 reset
import torch
from reasoning_from_scratch.ch02 import generate_text_basic_stream_cache

class CountingCacheModel:
    def __init__(self, n_layers=2):
        self.cfg = {"n_layers": n_layers}
        self.forward_calls = 0
        self.reset_calls = 0
    def eval(self): return self
    def reset_kv_cache(self): self.reset_calls += 1
    def __call__(self, token_ids, cache=None):
        self.forward_calls += 1
        b, t = token_ids.shape
        out = torch.zeros(b, t, 5)
        out[..., 3] = 1.0                # argmax 恒为 token 3
        return out

m = CountingCacheModel()
tokens = [t for t in generate_text_basic_stream_cache(m, torch.tensor([[9]]), 4)]
print("产出 token 数:", len(tokens))            # 预期 4
print("前向次数:", m.forward_calls)             # 预期 4 = 1 次 prefill + 3 次 decode
print("reset 次数:", m.reset_calls)             # 预期 1
```

注意与 4.1.4 对比：无缓存版生成 4 个 token 需要 **4 次**前向，缓存版同样 **4 次**——但缓存版后 3 次每次只算 1 个 token。若删掉源码里的 `if step + 1 < max_new_tokens` 保护，前向次数会变成 5 次（多出一次没人消费的），这正是该优化的意义。`tests/test_ch02.py` 的 [test_ch02.py:L128-L140](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L128-L140) 用几乎相同的 Dummy 断言了 EOS 与 reset 行为，可作对照。

**步骤 B：真实模型对比（需要已下载 0.6B base 权重，待本地验证）**。

```python
# 示例代码：双版本生成同一段文本，断言一致并计时
import time, torch
from pathlib import Path
from reasoning_from_scratch.ch02 import (generate_text_basic_stream,
                                         generate_text_basic_stream_cache)
from reasoning_from_scratch.qwen3 import (Qwen3Model, Qwen3Tokenizer,
                                          QWEN_CONFIG_06_B, download_qwen3_small)

download_qwen3_small(kind="base", tokenizer_only=False, out_dir="qwen3")
tok = Qwen3Tokenizer(tokenizer_file_path=Path("qwen3/tokenizer-base.json"))
model = Qwen3Model(QWEN_CONFIG_06_B)
model.load_state_dict(torch.load("qwen3/qwen3-0.6B-base.pth", map_location="cpu"))
model.to("cpu").eval()

ids = torch.tensor([tok.encode("Explain large language models in a single sentence.")])

def run(fn):
    t0 = time.perf_counter()
    out = torch.cat([t for t in fn(model, ids, 50, eos_token_id=tok.eos_token_id)], dim=1)
    return tok.decode(out[0].tolist()), time.perf_counter() - t0

text_a, t_a = run(generate_text_basic_stream)
text_b, t_b = run(generate_text_basic_stream_cache)
assert text_a == text_b, "两个版本 decode 结果应一致"
print(f"无缓存: {t_a:.2f}s | 有缓存: {t_b:.2f}s | 加速比 {t_a / t_b:.1f}x")
```

**需要观察的现象**：两版 decode 文本逐字相同；CPU 上 50 token 规模即可看到缓存版明显更快（通常数倍），token 越多差距越大。

**预期结果**：断言通过；加速比大于 1 且随 `max_new_tokens` 增大而增大。若个别后端出现极小的浮点差异导致个别 token 分叉，可放宽为比较前 20 个 token 或统计不一致位置数——理论上贪心 + 精确数学下两路 logits 完全一致（u2-l5 已验证缓存路径与整段前向一致），浮点舍入是唯一扰动源。本步骤涉及 1.2GB 权重下载与分钟级 CPU 计算，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cache = KVCache(...)` 和 `model.reset_kv_cache()` 必须放在函数开头、每次调用都执行，而不是提供一次性初始化？

**答案**：因为消费端随时可能提前 `break`（命中 EOS、用户按 Ctrl+C、多轮脚本命中自定义停止词），生成器被丢弃时模型上的 `current_pos` 停在半路、缓存随之销毁。把「新建缓存 + 清零游标」放在每次调用开头，使函数对任何历史脏状态自愈，调用方无需知道缓存存在——接口因此保持「传 model 和 token_ids 就能跑」的极简形态。

**练习 2**：把 `if step + 1 < max_new_tokens` 去掉，功能会出错吗？损失的是什么？

**答案**：功能不会错——多算一次前向得到的 logits 只是没人用。损失的是每次生成固定多出一次前向；在缓存版里它是 1-token 前向（代价小），但这个模式在非缓存场景代价会大得多，且「最后一步不白算」的思想还能用于上下文预算（`chat_multiturn.py` 的 `keep_len` 就用它为生成预留空间）。

**练习 3**：缓存版 decode 阶段每步送入模型的张量形状是什么？模型凭什么知道新 token 的「绝对位置」？

**答案**：形状 `(1, 1)`——只有刚产出的那一个 token。绝对位置来自模型自己的游标 `current_pos`：prefill 后它等于 prompt 长度，之后每次前向 `pos_start` 取游标值并随前向成功加 1（[qwen3.py:L64-L73](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L64-L73)），RoPE 以该偏移旋转新 token，与缓存中已旋转的旧 K 同一坐标系。

### 4.3 generate_stats：耗时、吞吐与显存统计

#### 4.3.1 概念说明

有了流式生成，自然要回答「多快」「多占显存」。`generate_stats` 是仓库统一的轻量统计出口：输入消费端收集到的 token 张量和一对起止时间戳，打印总耗时、tokens/sec 吞吐，以及在 CUDA/XPU 上打印峰值显存。它的设计有两个朴素但重要的取向：

1. **计时口径是端到端的**：时间戳由调用方在「消费循环」外掐取（如 `chat.py` 在进入循环前 `time.time()`、循环结束后再取一次），因此 decode、打印、收集列表的开销全部计入——这是用户真实感受到的吞吐，而不是模型单独的 FLOPs 速度。
2. **面向多种后端防御式编程**：GPU 后端各异（CUDA/XPU 是否可用、是否支持按设备传参、是否异步），函数逐项探测、逐项降级，保证在任何机器上都不抛异常。

#### 4.3.2 核心流程

```text
输入: output_token_ids (任意形状的张量), tokenizer(兼容保留), start_time, end_time
1. total_time = end_time - start_time
2. 打印耗时与 tokens/sec = numel / total_time
3. 对 CUDA、XPU 两个后端:
   a. 后端可用? 否 → 跳过
   b. 张量不在该后端设备上? → 发 warning（显存统计可能为 0）
   c. synchronize()          # 异步后端必须先等 GPU 算完
   d. max_memory_allocated() → 字节 → 换算 GB → 打印
   e. reset_peak_memory_stats()  # 清零峰值，不污染下一次统计
```

#### 4.3.3 源码精读

主体在 [ch02.py:L173-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L173-L199)：

[ch02.py:L175-L178](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L175-L178) —— 吞吐计算：`tokenizer` 参数目前未使用，仅为向后兼容保留（注释言明）；token 数用 `output_token_ids.numel()` 统计，因此 `(N,)` 与 `(1, N)` 两种形状都兼容。tokens/sec = token 数 / 总耗时。

[ch02.py:L180-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L180-L199) —— 显存部分：遍历 `("CUDA", "XPU")` 两个后端；先 `is_available()` 探测；若张量的 `device.type` 与后端不符（比如 CUDA 可用但你把模型放在 CPU 上跑）则发 `warnings.warn` 提示统计可能为 0；随后 `synchronize()`——因为 CUDA 计算异步提交，不等待就把「峰值已分配显存」读出来可能偏小；读 `max_memory_allocated()` 并除以 \(1024^3\) 换算 GB；最后 `reset_peak_memory_stats()` 把峰值计数器清零，使**下一次**调用量到的是下一次的峰值而不是历史累计最大值。

旁边的 [ch02.py:L144-L170](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L144-L170)（`_print_peak_memory_stats`，下划线开头表示内部函数）是同一套防御逻辑的「按设备」变体：用 `getattr(torch, device.type, None)` 拿后端模块、`hasattr` 检查 `is_available`、对 `synchronize(device=...)` 与无参签名做 `try/except TypeError` 兼容。它不参与本讲主流程，但展示了作者对「后端 API 参差不齐」的通用应对套路，在阅读 chE 批量推理等进阶代码时会再次见到类似写法。

**消费端标准姿势**来自 `chat.py` 的 `run_once`：[chat.py:L99-L128](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat.py#L99-L128) —— encode → `unsqueeze(0)` 成 `(1, T)` 张量 → `start_time = time.time()` → `for token in generate_text_basic_stream_cache(...)` 消费生成器，每个 token `squeeze(0).item()` 取标量 id、`decode([id])` 且 `end="", flush=True` 实时打印（`flush=True` 对管道/重定向输出尤为重要，否则缓冲会毁掉流式效果），同时收集进 `all_token_ids` → `end_time` → 调用 `generate_stats(torch.tensor(all_token_ids), tokenizer, start_time, end_time)`（[chat.py:L121-L127](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat.py#L121-L127)）。`generate_simple.py` 的 [generate_simple.py:L116-L131](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L116-L131) 是一模一样的单轮版。REPL 外层循环（[chat.py:L131-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/chat.py#L131-L150)）每轮输入都调 `run_once`，而缓存安全恰恰由 4.2 节的「函数内自愈」保证——两讲知识在此合流。

单元测试 [test_ch02.py:L143-L155](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_ch02.py#L143-L155) 用 `monkeypatch` 把 `torch.cuda.is_available` 打成 `False` 后调用 `generate_stats`，断言输出包含 `"Time:"` 与 `"tokens/sec"`——测试也默认了 CPU 环境下显存分支被安全跳过。

#### 4.3.4 代码实践

**目标**：亲手复刻 `run_once` 的「流式打印 + 统计」骨架，并观察 CPU 与 GPU 下输出的差异。

**步骤**（示例代码，假模型版可本地直接验证；真实模型版待本地验证）：

```python
# 示例代码：CPU 上即可运行，验证统计输出与 flush 效果
import time, torch, warnings
from reasoning_from_scratch.ch02 import generate_stats, generate_text_basic_stream_cache

# 用 4.2.4 的 CountingCacheModel + 一个极简 tokenizer 即可跑通：
class DummyTok:
    def decode(self, ids): return "".join(f"<{i}>" for i in ids)

m = CountingCacheModel()          # 4.2.4 中定义的假模型
tok = DummyTok()

start = time.time()
collected = []
for t in generate_text_basic_stream_cache(m, torch.tensor([[1, 2]]), 20):
    tid = t.squeeze(0).item()
    print(tok.decode([tid]), end="", flush=True)   # 流式打印的关键两参数
    collected.append(tid)
print()
generate_stats(torch.tensor(collected), tok, start, time.time())
```

**需要观察的现象**：
- token 逐个即时出现在终端（把 `flush=True` 去掉并重定向到文件 `python demo.py | cat`，可观察到输出变成最后一次性涌出——这正是流式被缓冲毁掉的样例）；
- CPU 机器上只打印 `Time` 与 `tokens/sec` 两行，无显存行；
- 若你的机器装了 CUDA 但模型在 CPU，会先看到一条 `UserWarning: CUDA is available but tensors are on cpu ...`。

**预期结果**：打印类似 `Time: 0.00 sec`、`20 tokens/sec`（假模型近乎瞬时，吞吐数字很大属正常）。真实模型上（`chat.py` 或 4.2.4 步骤 B 脚本末尾追加 `generate_stats` 调用），CPU 约 5~20 tokens/sec、消费端打印开销也计入分母——**具体数值待本地验证**，量级可对照 `generate_simple.py` 运行输出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `generate_stats` 要在读取 `max_memory_allocated()` 之前调用 `synchronize()`？

**答案**：CUDA/XPU 的内核是异步执行的，Python 侧计时结束时 GPU 可能还没算完；显存分配峰值由 GPU 侧的分配器记账，不等待同步就读取，量到的可能是「尚未执行到显存高峰」时的读数，偏小。`synchronize()` 强制等待队列清空，保证峰值读数覆盖完整计算过程。

**练习 2**：函数末尾的 `reset_peak_memory_stats()` 如果删掉，连续两次统计会发生什么？

**答案**：峰值计数器不清零，第二次读到的是「两次中的历史最大值」。例如第一次生成 2048 token 达到 3.5 GB 峰值，第二次只生成 16 token 实际 1.2 GB，却仍会打印 3.5 GB，让使用者误判第二次的显存需求。清零让每次统计彼此独立。

**练习 3**：`generate_stats` 的计时为什么由调用方掐表（在消费循环外取 `start_time`/`end_time`），而不是在生成函数内部计时？

**答案**：生成器是惰性的，「生成函数内部」根本不知道消费端何时消费完——最后一步前向发生在消费端索取最后一个 token 时。调用方在循环外掐表，量到的是包含 decode、打印、收集在内的端到端耗时，与用户体验一致；也让同一个统计函数能公平地比较不同消费方式的开销。

## 5. 综合实践

把本讲三个模块串成一个「迷你基准脚本」`tutorial_bench.py`（示例代码）：

1. **加载**：按 u1-l3 的 `download_qwen3_small(kind="base")` 下载权重与 tokenizer，构造 `Qwen3Model` 并加载（与 `generate_simple.py` L63-L88 相同的样板）。
2. **正确性**：用同一个 prompt（如 `"Explain large language models in a single sentence."`），分别消费 `generate_text_basic_stream` 与 `generate_text_basic_stream_cache` 各生成 50 token，`torch.cat` 对齐后 `assert` 两个 token 序列完全一致，再分别 decode 打印。
3. **性能**：对两个版本各用 `time.perf_counter()` 在消费循环外计时（每版先跑一次预热再计一次），打印各自耗时与加速比；缓存版消费循环里仿照 `chat.py` 逐 token `decode` + `flush=True` 打印。
4. **统计**：对缓存版调用 `generate_stats(torch.tensor(all_ids), tokenizer, start, end)`，记录 tokens/sec；若有 GPU，观察峰值显存行，并尝试 `--compile` 前后各测一次为下一讲铺垫。
5. **思考题**（写在脚本输出末尾的注释里）：把 `max_new_tokens` 从 50 提到 200，加速比如何变化？用 4.2.2 的公式 \(TN + \frac{N(N+1)}{2}\) 对比 \(T + (N-1)\) 预测趋势，再与实测对照。

预期：断言通过；加速比随生成长度增加而上升，与公式预测的差距扩大方向一致。涉及权重下载与 GPU/CPU 差异，具体数值**待本地验证**。

## 6. 本讲小结

- `ch02.py` 的四代生成函数是「是否流式 × 是否缓存」两个正交维度的组合，`generate_text_basic_stream_cache` 是最终主力；被注释掉的旧代码行本身就是演化日志。
- `yield` 使函数成为生成器：调用不执行、消费才推进，首 token 延迟从「全部算完」降到「一步算完」；消费端 `break` 即安全弃置，因为无缓存路径不触碰模型的 `current_pos`。
- 缓存版把 cache 生命周期收进函数内部：开头「新建 `KVCache` + `reset_kv_cache()` 清零游标」成对执行，让函数对任何历史脏状态自愈，调用方零负担。
- 「跳过最后一次前向」（`if step + 1 < max_new_tokens`）在缓存版、非缓存版与 `chat_multiturn.py` 的上下文预算里三处出现：产出的最后一个 token 没有消费者，不为它再付一次前向。
- `generate_stats` 用调用方掐的端到端时间算 tokens/sec，在 CUDA/XPU 上先 `synchronize` 再读峰值显存、读完即 `reset`，是一套防御式、多后端安全的轻量统计出口。

## 7. 下一步学习建议

- **下一讲 u3-l3（torch.compile 加速）**：在缓存版生成函数不变的前提下，只给模型套一层 `torch.compile` 再测一次吞吐，观察「编译一次、受益每次」与首次编译开销；建议先把本讲综合实践脚本备好，直接复用。
- **顺手通读** `ch02/03_optimized-LLM/README.md` 与 `compare_inference.py`，那里把本讲的 `generate_stats`/峰值统计用于正式基准对比，是 4.3 的进阶应用。
- **向后衔接**：第 4 章的 `generate_text_temp_stream_cache`（温度采样）与 `generate_text_top_p_stream_cache`（top-p）都是在本讲的缓存流式骨架上加「logits 加工」步骤，读 `ch04.py` 时先找熟悉的 prefill/decode/跳过最后前向三件套，再学新增量。
