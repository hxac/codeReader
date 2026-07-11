# CUDA Graph 捕获与重放

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **CUDA Graph** 到底优化了什么、为什么它对 **decode（逐 token 生成）阶段** 的延迟改善最明显。
- 复述 LightLLM 中一张 CUDA Graph 的 **生命周期**：在 warmup 阶段如何对一组 batch size（或 token 数）逐一 **捕获（capture）**，推理时如何用 `find_closest_graph_batch_size` 选档并 **重放（replay）**。
- 解释 **「地址不变、内容可变」** 这一 CUDA Graph 的核心约束在源码里是如何靠 `copy_for_cuda_graph` 落地的。
- 对比 **decode CUDA Graph**（`CudaGraph`）与 **prefill CUDA Graph**（`PrefillCudaGraph`）在键、形状、捕获方式上的关键差异。
- 能够看懂 `gen_cuda_graph_batch_sizes` 生成的档位表，并知道 `--graph_split_batch_size` / `--graph_grow_step_size` 两个参数如何改变这张表。

本讲是第六单元「性能优化」的第一篇，依赖你已经读过 [u3-l2 prefill 与 decode 推理主流程](./u3-l2-prefill-decode-flow.md) 中关于 `_decode` / `_token_forward` 与 padding 的内容。

## 2. 前置知识

### 2.1 kernel launch 开销

在 GPU 上跑一次推理，CPU 并不是把整个网络「丢给 GPU」就完事。每一个算子（矩阵乘、layernorm、注意力核……）都要由 CPU **单独发起一次启动（kernel launch）**：CPU 填好参数、写入命令缓冲、通知 GPU。单次 launch 的开销在微秒级，几十微秒一次。

对 prefill 阶段，一个 batch 要算成千上万个 token，每个算子的计算量都很大，launch 开销占比可以忽略。但 decode 阶段不一样：每拍每个请求只新增 **1 个 token**，算子本身计算量极小（很多算子是「内存带宽瓶颈」而非「计算瓶颈」），此时 **CPU 发射 kernel 的时间反而比 GPU 算它的时间还长**——GPU 在等 CPU 派活。这就是 decode 阶段单 token 延迟居高不下的重要原因之一。

### 2.2 CUDA Graph 是什么

CUDA Graph 是 NVIDIA 提供的一种机制：把一整串 kernel 及其依赖关系 **录制（capture）** 成一张「图」，之后每次 **重放（replay）** 这张图时，CPU 只需发起一次「执行这张图」的命令，整串 kernel 就按录好的顺序和参数在 GPU 上跑起来。

直观类比：原来 CPU 像是给 GPU 一个一个地下达口令（「做矩阵乘」「做归一化」「做注意力」……），每次口令都有沟通成本；CUDA Graph 则像是把整套动作排练成一段「录像」，正式表演时只要按一下播放键，整段动作一气呵成。它把 N 次 kernel launch 摊销成 1 次，极大降低了 CPU 侧开销。

### 2.3 CUDA Graph 的核心约束：形状与地址必须固定

录像有个硬要求：**录的时候张量是什么形状、放在显存哪个地址，重放时就必须还是这个形状、这个地址**。你没法在录像里临时改「这次矩阵是 32×64，下次是 48×64」。这就引出两个工程问题，也是本讲源码要回答的：

1. **形状固定** → 推理时真实的 batch size 千变万化（1、3、7、33……），不可能给每个值都录一张图，怎么办？答：**只录一组离散的档位**，推理时把真实 batch **向上 padding 到最近的档位**。
2. **地址固定** → 录像里用的输入张量地址 A，重放时新请求的输入在地址 B，怎么让录像读到新数据？答：**把新数据 `copy_` 进录像里那个固定地址的张量**，即「地址不变、内容可变」。

抓住这两点，后面的源码就只是把它们具体化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lightllm/common/basemodel/cuda_graph.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py) | **decode 阶段的 CUDA Graph** 实现：档位生成、捕获、重放、warmup |
| [lightllm/common/basemodel/prefill_cuda_graph.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py) | **prefill 阶段的 CUDA Graph** 实现：以 token 数为键，捕获方式更复杂 |
| [lightllm/common/basemodel/basemodel.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py) | 在 `_decode` / `_context_forward` 里决定「走图还是走 eager」「捕获还是重放」 |
| [lightllm/common/basemodel/infer_struct.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py) | `InferStateInfo` 提供 `copy_for_cuda_graph` / `copy_for_prefill_cuda_graph`，是「地址不变、内容可变」的落地处 |
| [lightllm/server/api_cli.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py) | `--graph_split_batch_size`、`--graph_grow_step_size`、`--disable_cudagraph` 等参数 |

## 4. 核心概念与源码讲解

### 4.1 CUDA Graph：把一串 kernel 录成一张图

#### 4.1.1 概念说明

LightLLM 把 decode 阶段的整条前向（embedding → N 层 transformer → post 层 logits，即 [u3-l2](./u3-l2-prefill-decode-flow.md) 讲过的 `_token_forward`）当成「一串 kernel」整体录进一张 CUDA Graph。运行时，`_decode` 不再逐层调用 `_token_forward`，而是 **重放** 录好的图。

为什么要这么做？因为 decode 每拍只算 1 个 token/请求，算子小而多，CPU launch 开销成了主要矛盾。一张图把几十上百次 launch 压成 1 次，直接砍掉了 CPU 侧的等待，是降低 decode 单 token 延迟（latency）的关键手段。注意它改善的主要是 **延迟** 而非吞吐——吞吐更多由显存带宽与批大小决定。

#### 4.1.2 核心流程

一张 decode CUDA Graph 的完整生命周期分三段：

```text
[启动 warmup 阶段]
  对每个档位 batch_size b（从大到小）：
    1. 造一份假的 ModelInput（input_ids 全 1，长度 = b）
    2. 调 model.forward(model_input)          # 走正常 _decode 路径
    3. _decode 发现 need_capture(b)==True      # 该档位还没录过
    4. graph.capture_decode(_token_forward, infer_state)
       → torch.cuda.graph(...) 录制 → 存入 self.graph[b]
    5. 立即 replay 一次，产出合法输出

[正常推理 decode 阶段]
  对真实 batch（大小 r）：
    1. can_run(r, seq_len)？  否 → 走 eager（不录不重放）
    2. b = find_closest_graph_batch_size(r)   # 向上取到最近档位
    3. 把 batch 从 r padding 到 b
    4. need_capture(b)？
         True  → capture_decode(...)          # 该档位第一次遇到，补录
         False → replay(infer_state)          # 已录过，直接重放
    5. 把输出从 b unpadding 回 r
```

这里有个优雅的设计：**warmup 不直接调用 `capture_decode`，而是构造假输入跑一遍正常 `forward`**。捕获与否的判断在 `_decode` 内部完成——因为 warmup 用的档位都还没录过，`need_capture` 自然返回 True，于是「顺便」就把图录了。这样 warmup 与推理共用同一条代码路径，逻辑零分叉。

#### 4.1.3 源码精读

先看 `_decode` 中决定走图还是走 eager、捕获还是重放的核心分支（这一段是理解整个机制的入口）：

[lightllm/common/basemodel/basemodel.py:612-636](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L612-L636)

```python
if self.graph is not None and self.graph.can_run(
    batch_size=infer_batch_size, max_len_in_batch=model_input.max_kv_seq_len
):
    infer_batch_size = self.graph.find_closest_graph_batch_size(batch_size=infer_batch_size)
    model_input = self._create_padded_decode_model_input(
        model_input=model_input, new_batch_size=infer_batch_size
    )                       # 把真实 batch padding 到档位 b
    infer_state = self._create_inferstate(model_input)
    need_capture = self.graph.need_capture(infer_batch_size)
    infer_state.is_cuda_graph = need_capture
    ...
    if need_capture:
        model_output = self.graph.capture_decode(self._token_forward, infer_state)
    else:
        model_output = self.graph.replay(infer_state)
    model_output = self._create_unpad_decode_model_output(model_output, origin_batch_size=origin_batch_size)
else:
    ...                     # graph 为 None 或 can_run 失败 → 走 eager _token_forward
```

四个判断点串成一条决策链：

1. `self.graph is not None`：用户没加 `--disable_cudagraph` 时才建图（见 4.3.3）。
2. `can_run(batch_size, max_len_in_batch)`：真实 batch 和序列长度都没超过图的上限。
3. `find_closest_graph_batch_size`：把 batch 向上对齐到一个录过的档位。
4. `need_capture`：这个档位是第一次遇到（还没录）还是已经录过。

`can_run` 的实现极其简洁，它划定了图能覆盖的「形状边界」：

[lightllm/common/basemodel/cuda_graph.py:65-66](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L65-L66)

```python
def can_run(self, batch_size, max_len_in_batch):
    return batch_size <= self.max_batch_size and max_len_in_batch <= self.graph_max_len_in_batch
```

也就是说：**batch 太大**（超过 `--graph_max_batch_size`，默认 256）或 **序列太长**（超过 `graph_max_len_in_batch`，默认 8192）时，图用不了，回退到普通 eager 推理。这就是参数 help 里写的「It will turn into eager mode if encounters a larger value」的含义。

`need_capture` 的实现揭示了「补录」机制——档位选择永远成功（`find_closest` 总能返回一个值），但这个档位是否已录制决定走捕获还是重放：

[lightllm/common/basemodel/cuda_graph.py:68-81](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L68-L81)

```python
def need_capture(self, batch_size):
    find_batch_size = self.find_closest_graph_batch_size(batch_size)
    if find_batch_size is not None:
        return find_batch_size not in self.graph   # self.graph 是 dict，键=已录档位
    else:
        assert False, "dead code"

def find_closest_graph_batch_size(self, batch_size):
    index = bisect.bisect_left(self.cuda_graph_batch_sizes, batch_size)
    if index < len(self.cuda_graph_batch_sizes):
        return self.cuda_graph_batch_sizes[index]  # 升序表里第一个 >= batch_size 的值
    else:
        return None
```

`bisect.bisect_left` 在升序列表里找「插入点」，等价于返回 **大于等于 `batch_size` 的最小档位**。例如档位表是 `[1,2,...,32,48,64,...]`，真实 batch 是 33 时返回 48——于是 33 会被 padding 到 48 再重放。注意是 **向上** 取：宁可多算几个 padding 的空位，也不能少算真实的请求。

#### 4.1.4 代码实践

**实践目标**：在不实际加载大模型的前提下，亲眼看到「档位表」长什么样，并理解 `find_closest_graph_batch_size` 的对齐行为。

**操作步骤**（源码阅读 + 小脚本；如无法运行则按「待本地验证」理解）：

1. 打开 [cuda_graph.py:20-46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L20-L46)，对照 4.2.3 的讲解，手算默认参数（`graph_split_batch_size=32`、`graph_grow_step_size=16`、`max_batch_size=256`、`mtp_step=0`）下生成的档位表。
2. 写一个最小脚本（示例代码，需在已 `pip install` 好本仓库、且设置好启动参数的环境内运行）：

   ```python
   # 示例代码：仅演示档位对齐逻辑，不依赖 GPU
   import bisect
   # 抄自 gen_cuda_graph_batch_sizes 默认参数产物（见 4.2.3）
   cuda_graph_batch_sizes = list(range(1, 33)) + list(range(48, 256, 16)) + [256]

   def find_closest(batch_size):
       index = bisect.bisect_left(cuda_graph_batch_sizes, batch_size)
       return cuda_graph_batch_sizes[index]

   for r in [1, 3, 32, 33, 47, 48, 100, 255, 256]:
       print(f"真实 batch={r:>3} -> 对齐到档位 {find_closest(r)}")
   ```

3. 真实启动服务时观察日志：LightLLM 在建图后会打印一行 `cuda graph batch_sizes: [...]`（见 [cuda_graph.py:63](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L63)），把这行和你手算的表对比。

**需要观察的现象**：脚本输出里，33、47 都对齐到 48；32 对齐到 32（精确命中不放大）；100 对齐到 112。

**预期结果**：每次 `find_closest` 都返回 `>=` 输入的最小档位，且档位表在小区间（1~32）是逐 1 密集、在大区间按 16 递增。

**待本地验证**：完整启动服务加载真实模型验证日志中的档位表是否与默认参数手算结果一致（受 `mtp_step`、`enable_tpsp_mix_mode` 影响时会不同，见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LightLLM 在 `_decode` 里用 `find_closest`（向上取）而不是向下取最近的档位？

**答案**：向下取会让真实请求中的某些 token 没被算到，输出错误；向上取虽然多算了一些 padding 空位（浪费少量算力），但保证了所有真实请求都参与计算，结果正确。这是「正确性优先于极致性能」的取舍。

**练习 2**：`can_run` 返回 False 时，`_decode` 会走哪条分支？输出还正确吗？

**答案**：走 `else` 分支，即不录不重放、直接调 `_token_forward(infer_state)` 的 eager 路径（见 [basemodel.py:637-651](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L637-L651)）。结果依然正确，只是这一拍没有 CUDA Graph 加速、延迟更高。

### 4.2 batch size 档位生成与对齐

#### 4.2.1 概念说明

既然每张图的形状固定，我们不可能为 1~256 的每一个 batch size 都录一张图（显存和捕获时间都受不了）。LightLLM 的做法是 **精心设计一张离散档位表**：小 batch 区间逐 1 录制（decode 时小 batch 最常见，延迟最敏感），大 batch 区间按固定步长稀疏录制（大 batch 本身计算量大，padding 几个空位的相对浪费小）。

这张表由静态方法 `gen_cuda_graph_batch_sizes` 生成，它是整个 CUDA Graph 机制最值得读懂的一段算法。

#### 4.2.2 核心流程

档位表由两段拼接而成（记 `mtp_size = mtp_step + 1`，默认 `mtp_step=0` 故 `mtp_size=1`）：

```text
设 S = graph_split_batch_size（默认 32）, G = graph_grow_step_size（默认 16）, M = max_batch_size

第一段（密集）：[1·mtp, 2·mtp, ..., S·mtp]            # 1 到 S 逐 1（再乘 mtp）
第二段（稀疏）：[S·mtp + G·mtp, S·mtp + 2·G·mtp, ...]  # 从 S+G 起、步长 G，直到 < M
末尾：          追加 M（保证最大档位一定在表里）
可选：          若 enable_tpsp_mix_mode，每档向上取整到 tp_world_size 的倍数
```

两段的「密度」不同：第一段在 `[1, S]` 逐 1 命中（小 batch 零 padding），第二段从 `S+G` 开始按 `G` 步长稀疏命中。`S` 就是「密集区与稀疏区的分界点」，`G` 就是「稀疏区的步长」——这正是两个 CLI 参数 `--graph_split_batch_size` / `--graph_grow_step_size` 的名字含义。

`mtp_size` 因子的出现，是因为 MTP 推测解码（见 [u7-l5](./u7-l5-mtp-speculative-decoding.md)）下，每个逻辑请求一拍会生成 `mtp_step+1` 个 token，档位必须是它的倍数，否则 padding 会对不齐。

#### 4.2.3 源码精读

[lightllm/common/basemodel/cuda_graph.py:20-46](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L20-L46)

```python
@staticmethod
def gen_cuda_graph_batch_sizes(max_batch_size=8, tp_world_size: int = 1):
    args = get_env_start_args()
    mtp_size = args.mtp_step + 1

    graph_split_batch_size = args.graph_split_batch_size * mtp_size
    graph_grow_step_size = args.graph_grow_step_size * mtp_size

    batch_sizes = [i * mtp_size for i in range(1, args.graph_split_batch_size + 1)]
    for _batch_size in range(graph_split_batch_size + graph_grow_step_size, max_batch_size, graph_grow_step_size):
        batch_sizes.append(_batch_size)

    batch_sizes = list(set([e for e in batch_sizes if e < max_batch_size]))
    batch_sizes.append(max_batch_size)
    batch_sizes.sort()
    if args.enable_tpsp_mix_mode:
        batch_sizes = [triton.cdiv(e, tp_world_size) * tp_world_size for e in batch_sizes]
        batch_sizes = list(set(batch_sizes))
        batch_sizes.sort()

    assert batch_sizes[-1] == max_batch_size
    return batch_sizes
```

逐行解读：

- 第 33 行：第一段，`range(1, S+1)` 生成 `[1,2,...,S]`，各乘 `mtp_size`。
- 第 34-35 行：第二段，`range(S·mtp + G·mtp, M, G·mtp)` 生成 `[S+G, S+2G, ...]`（均乘了 mtp）。
- 第 37-39 行：滤掉 `>= M` 的、去重、**补上 `M` 本身**、排序——保证表是升序且末项恰为 `max_batch_size`。
- 第 40-43 行：TPSP 混合并行模式下，每档用 `triton.cdiv(e, T)*T` **向上取整到 `tp_world_size` 的倍数**，因为 TPSP 把一个 batch 在张量并行维上均分，batch 数必须是 `T` 的倍数才能切齐。
- 第 45 行：末项断言，确保最大档位没在取整/去重中丢失。

> 注意第 33 行用的是 **原始的** `args.graph_split_batch_size`（未乘 mtp）做 `range` 上界，而第 30-31 行的 `graph_split_batch_size` / `graph_grow_step_size` 是乘了 mtp 的局部变量。这两个名字相似但含义不同的量是本段最容易看错的点。

默认参数（`S=32, G=16, M=256, mtp=1, 无 tpsp`）下，表是：

\[
[1,2,\dots,32] \;\cup\; [48,64,80,\dots,240] \;\cup\; [256]
\]

共约 48 个档位。`find_closest_graph_batch_size` 就是在这张升序表上做二分。

对应的两个 CLI 参数定义在：

[lightllm/server/api_cli.py:574-594](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L574-L594)

```python
parser.add_argument("--graph_split_batch_size", type=int, default=32, help="""
    Controls the interval for generating CUDA graphs during decoding.
    CUDA graphs will be generated continuously for values ranging from 1 up to the specified
    graph_split_batch_size. For values from graph_split_batch_size to graph_max_batch_size,
    a new CUDA graph will be generated for every increment of graph_grow_step_size. ...""")
parser.add_argument("--graph_grow_step_size", type=int, default=16, help="""
    For batch_size values from graph_split_batch_size to graph_max_batch_size,
    a new CUDA graph will be generated for every increment of graph_grow_step_size. """)
```

help 文本把两段式策略讲得很清楚：`[1, split]` 连续生成，`[split, max]` 按 `grow_step` 步长生成。

#### 4.2.4 代码实践

**实践目标**：直观感受两个参数如何改变档位表的「密度」，从而改变 padding 浪费。

**操作步骤**：

1. 假想三组参数，手算（或用 4.1.4 的脚本改造）生成的档位表：
   - A：默认 `S=32, G=16, M=256`
   - B：`S=16, G=16, M=256`（把密集区缩短到 16）
   - C：`S=32, G=32, M=256`（把稀疏区步长放大到 32）
2. 对每组表，统计真实 batch 从 1 到 256 每个值经 `find_closest` 后的 **平均 padding 量**（对齐档位 − 真实值）。

**需要观察的现象**：A 在 `[1,32]` 内零 padding；B 在 `[17,32]` 内开始出现 padding；C 在 `[33,256]` 内每次最多浪费 31 个空位（比 A 的最多 15 更大）。

**预期结果**：密集区（`S`）越大、稀疏步长（`G`）越小，padding 越少但 **要录的图越多、warmup 越慢、占显存越多**。这是「延迟 vs 启动时间/显存」的三角权衡。

**待本地验证**：用真实模型分别以 A/B/C 启动，观察启动日志里 `cuda graph batch_sizes` 的档位数量与 warmup 耗时。

#### 4.2.5 小练习与答案

**练习 1**：把 `--graph_grow_step_size` 调大到 64，对 decode 延迟和启动时间分别有什么影响？

**答案**：稀疏区步长变大 → 档位更少 → warmup 录的图更少、启动更快、占显存更少；但大 batch 区间的 padding 浪费变大（最多浪费 63 个空位），极端情况下 decode 延迟可能不降反升（多算的 padding 抵消了图本身的收益）。

**练习 2**：为什么表里一定要 `batch_sizes.append(max_batch_size)` 并断言 `batch_sizes[-1] == max_batch_size`？

**答案**：`max_batch_size` 是 `can_run` 判定的上限，必须保证「恰好等于上限的 batch」也能对齐到一个已录档位，否则真实 batch 恰为上限时会因找不到档位（`find_closest` 越界返回 None）而出错。断言是防止 TPSP 取整/去重把末项弄丢的安全网。

### 4.3 捕获与重放：地址不变、内容可变

#### 4.3.1 概念说明

档位表解决了「形状固定」问题，「地址固定」要靠 **捕获与重放** 这对操作来解决。核心思路是：

- **捕获（capture）** 时，PyTorch 记下图中每个张量所在的 **显存地址**。我们把这次捕获用到的 `infer_state` 和 `model_output` **留存下来**，作为「固定地址的容器」。
- **重放（replay）** 时，我们 **不新建** infer_state，而是把新请求的数据 `copy_` 进留存容器的同名张量里（地址没变，内容变了），然后 `replay()` 录好的图，结果自然写进留存的 `model_output` 里。

这就是本讲反复强调的 **「地址不变、内容可变」** 模式。它在 [u3-l5 注意力后端机制](./u3-l5-attention-backends.md) 里也出现过（decode 注意力状态的 `copy_for_decode_cuda_graph`），是 LightLLM 用 CUDA Graph 的统一范式。

#### 4.3.2 核心流程

```text
[捕获 _capture_decode]
  1. 新建 graph_obj = torch.cuda.CUDAGraph()
  2. 把 infer_state 的形状相关字段钉死为捕获档位对应的固定值
     （max_kv_seq_len = graph_max_len_in_batch，
      total_token_num = graph_max_len_in_batch * batch_size）
  3. warmup 一次：用 copy.copy(infer_state) 跑一遍 decode_func，
     跑完删掉这次临时新增的属性（防止污染后续真正捕获）
  4. with torch.cuda.graph(graph_obj, pool=self.mempool):
         model_output = decode_func(infer_state)      # 真正录制
  5. self.graph[batch_size] = (graph_obj, infer_state, model_output)  # 留存容器
  6. graph_obj.replay()  # 立即重放一次，让 model_output 拿到合法值

[重放 _replay]
  1. batch_size = infer_state.input_ids.shape[0]
  2. (graph_obj, graph_infer_state, graph_output) = self.graph[batch_size]
  3. graph_infer_state.copy_for_cuda_graph(infer_state)  # 新数据灌进旧容器
  4. graph_obj.replay()                                   # 一键重放
  5. return graph_output                                  # 读旧容器里的结果
```

注意第 5 步：**留存的是「捕获时的那个 `infer_state` 对象本身」**，而不是它的拷贝。正因如此，第 3 步往里 `copy_` 数据，重放才能读到新数据、写出新结果。

#### 4.3.3 源码精读

先看捕获实现。这里最值得读的是 warmup 那段「记原始属性 → 跑一遍 → 删新增属性」的奇怪操作，它解决了一个真实的 bug：

[lightllm/common/basemodel/cuda_graph.py:83-112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L83-L112)

```python
def _capture_decode(self, decode_func, infer_state: InferStateInfo):
    graph_obj = torch.cuda.CUDAGraph()
    input_ids = infer_state.input_ids
    batch_size = input_ids.shape[0]
    infer_state.max_kv_seq_len = self.graph_max_len_in_batch
    infer_state.total_token_num = self.graph_max_len_in_batch * batch_size
    # warmup
    for _ in range(1):
        pure_para_set = set(vars(infer_state).keys())        # 记下原始属性名
        torch.cuda.synchronize()
        decode_func(copy.copy(infer_state))                  # 浅拷贝跑一遍
        torch.cuda.synchronize()
        for param_name in set(vars(infer_state).keys()):
            if param_name not in pure_para_set:
                delattr(infer_state, param_name)             # 删掉这次新增的属性

    with torch.cuda.graph(graph_obj, pool=self.mempool):
        model_output = decode_func(infer_state)
    self.graph[batch_size] = (graph_obj, infer_state, model_output)
    graph_obj.replay()
    return model_output
```

为什么要这么做？源码注释（[L90-96](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L90-L96)）解释得很到位：有些推理代码（注释举了 `deepseek2/triton_kernel/gqa_flash_decoding.py` 的例子）会在 **第一层** 时判断 infer_state 上有没有某个属性、没有就 **临时新建一个 tensor 挂上去**，后续层复用。如果直接拿这个 infer_state 去捕获图，因为它「已经有了」这个属性，临时初始化逻辑就不跑了，导致捕获到的图里 **缺少这个临时 tensor 的分配**，重放时崩溃。解决办法是：先拿 **浅拷贝** 跑一遍触发所有「首次初始化」，再 **删掉原对象上被污染的新属性**，让真正捕获时 infer_state 仍是「干净的」，从而让初始化逻辑在捕获过程中重新执行、把分配也录进图里。

注意 `copy.copy` 是 **浅拷贝**：它复制了 `vars(infer_state)` 这层字典，但 tensor 仍是同一份。所以「删属性」删的是浅拷贝回写不到的、或者直接对原对象的 `delattr`——这里其实是对 **原 infer_state** 做 `delattr`（因为循环里 `vars(infer_state)` 取的就是原对象）。理解这一点需要细看，要点是：warmup 的目的就是 **让原 infer_state 在捕获前保持「无临时属性」状态**。

捕获完，留存的容器元组里 **`infer_state` 就是捕获时用的那个对象**，这是「地址不变」的关键。

再看重放：

[lightllm/common/basemodel/cuda_graph.py:170-175](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L170-L175)

```python
def _replay(self, infer_state: InferStateInfo):
    batch_size = infer_state.input_ids.shape[0]
    graph_obj, graph_infer_state, graph_output = self.graph[batch_size]
    graph_infer_state.copy_for_cuda_graph(infer_state)   # 新 → 旧容器
    graph_obj.replay()
    return graph_output
```

三步走：取留存容器 → 把新 infer_state 的数据灌进旧容器 → 重放 → 返回旧容器里的输出。整个重放路径 **没有任何重新计算**，只是一次 `copy_` + 一次 `replay`，这正是延迟降低的来源。

「灌数据」的具体实现在 `InferStateInfo.copy_for_cuda_graph`：

[lightllm/common/basemodel/infer_struct.py:139-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L139-L149)

```python
def copy_for_cuda_graph(self, new_infer_state: "InferStateInfo"):
    for attr_name, attr_value in vars(new_infer_state).items():
        if isinstance(attr_value, torch.Tensor):
            attr_ = getattr(self, attr_name, None)
            if attr_ is not None and attr_.data_ptr() != attr_value.data_ptr():
                attr_.copy_(attr_value, non_blocking=True)   # 逐个 tensor 灌入
    self.decode_att_state.copy_for_decode_cuda_graph(new_infer_state.decode_att_state)
    if self.decode_att_state1 is not None:
        self.decode_att_state1.copy_for_decode_cuda_graph(new_infer_state.decode_att_state1)
    return
```

它遍历新 infer_state 的所有 **tensor 属性**，只要旧容器（`self`）里有同名且地址不同的 tensor，就 `copy_` 过去。`data_ptr() != attr_value.data_ptr()` 的判断是为了避免自己拷贝到自己（同地址）的无意义操作。注意力状态也走同样模式（见 [u3-l5](./u3-l5-attention-backends.md)）。

最后看图的「创建与预热」入口。`CudaGraph` 在模型初始化时被构造，并立刻 warmup：

[lightllm/common/basemodel/basemodel.py:265-279](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L265-L279)

```python
def _init_cudagraph(self):
    self.graph = (
        None
        if self.disable_cudagraph
        else CudaGraph(
            max_batch_size=self.graph_max_batch_size,
            max_len_in_batch=self.graph_max_len_in_batch,
            tp_world_size=self.tp_world_size_,
        )
    )
    if self.graph is not None:
        if get_env_start_args().enable_decode_microbatch_overlap:
            self.graph.warmup_overlap(self)
        else:
            self.graph.warmup(self)
```

`--disable_cudagraph` 时 `self.graph = None`，`_decode` 里第一个判断 `self.graph is not None` 就为假，直接走 eager——这是关闭 CUDA Graph 的总开关。`warmup` 的实现见 [cuda_graph.py:202-257](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L202-L257)：它对 `cuda_graph_batch_sizes[::-1]`（**从大到小**）逐一构造全 1 的假 `input_ids` 调 `model.forward`，借 `_decode` 的 `need_capture` 路径完成捕获。从大到小的顺序有好处——最大的档位最容易 OOM，先试它，失败早暴露。

> 关于 `mempool`：`CudaGraph.__init__` 里 `self.mempool = torch.cuda.graph_pool_handle()`（[L51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L51)），所有档位的图 **共享同一个显存池**，避免每张图各存一份中间张量、大幅省显存。4.4 会看到 prefill 图也复用这个池。

#### 4.3.4 代码实践

**实践目标**：理解「留存容器」与「重放」的零计算特性，定位一条真实的重放调用链。

**操作步骤**（源码阅读型实践）：

1. 在 [basemodel.py:632-634](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L632-L634) 找到 `capture_decode` 与 `replay` 的两个调用点。
2. 顺着 `self.graph.replay(infer_state)` 进入 [cuda_graph.py:195](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L195)，再到 [`_replay` L170-175](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/cuda_graph.py#L170-L175)，再到 [`copy_for_cuda_graph` L139-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L139-L149)。
3. 在纸上画出：捕获时 `self.graph[48]` 这个元组里的 `infer_state`、`model_output` 各自的 `data_ptr`；重放时新 `infer_state` 的 tensor 如何 `copy_` 进旧容器。

**需要观察的现象**：重放路径上 **没有出现** `_token_forward`、`forward`、任何 `layer.context_forward` 之类的计算调用——只有 `copy_` 和 `graph_obj.replay()`。

**预期结果**：能口述「重放 = 一次 tensor 拷贝 + 一次 graph 重放，无 Python 层前向计算」，从而理解延迟为何显著降低。

#### 4.3.5 小练习与答案

**练习 1**：`_capture_decode` 里 warmup 为什么用 `copy.copy(infer_state)` 而不是直接 `decode_func(infer_state)`？

**答案**：用浅拷贝是为了让「首次初始化产生的临时属性」挂在拷贝对象上、便于识别哪些是新增的；同时配合捕获后对原对象 `delattr` 新增属性，保证真正捕获时原 `infer_state` 是「干净」的，让图内捕获到临时 tensor 的分配逻辑（否则重放会因缺 tensor 而崩）。根本目的是规避「按属性存在性做一次性初始化」的代码与 CUDA Graph 捕获的冲突。

**练习 2**：如果某个 tensor 属性在旧容器里不存在（`attr_ is None`），`copy_for_cuda_graph` 会怎样？

**答案**：跳过该属性（`if attr_ is not None` 守卫）。这符合「地址不变」约束：旧容器里没有这个固定地址的 tensor，就无法把新数据灌进去，所以宁可不灌也不能凭空新建。这也解释了为什么捕获时必须把所有需要的 tensor 都建好。

### 4.4 prefill CUDA Graph 的差异

#### 4.4.1 概念说明

decode 图按 **batch size**（请求数）录，因为 decode 每拍每请求只算 1 个 token，形状由请求数决定。prefill 恰好相反：它一次要算 **一大段 token**（一个长 prompt 或 chunked prefill 的一块），决定形状的是 **要处理的 token 总数**，而不是请求数。

因此 `PrefillCudaGraph` 另起炉灶：以 **`handle_token_num`（本拍要处理的非前缀缓存 token 数）** 为键，档位表也是一组 **token 数** 而非 batch size。它默认关闭，需用 `--enable_prefill_cudagraph` 显式开启，且目前仅支持 llama/qwen 等非 EP-MoE 模型（见 [api_cli.py:555-560](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L555-L560)）。

#### 4.4.2 核心流程

prefill 图与 decode 图的对照：

| 维度 | decode 图（`CudaGraph`） | prefill 图（`PrefillCudaGraph`） |
| --- | --- | --- |
| 键 | `batch_size`（请求数） | `handle_token_num`（处理 token 数） |
| 档位表 | 两段式算法生成（4.2） | 手工设计的多段 token 数表（4.4.3） |
| 容器内容 | `(graph_obj, infer_state, model_output)` | `(infer_state, input_tensors, output_tensors)`，graph_obj 挂在 infer_state 内 |
| 共享池 | `self.mempool` | 复用 decode 图的 `mempool` |
| 数据灌入 | `copy_for_cuda_graph`（infer_state） | `copy_` input_tensors + `copy_for_prefill_cuda_graph`（infer_state） |
| 对齐策略 | 向上 padding 到最近档位 | **必须精确命中档位**（断言相等） |
| 是否默认开 | 是（`--disable_cudagraph` 关） | 否（`--enable_prefill_cudagraph` 开） |

#### 4.4.3 源码精读

先看档位表——它是手工设计的「小密大疏」token 数表：

[lightllm/common/basemodel/prefill_cuda_graph.py:36-57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py#L36-L57)

```python
graph_handle_token_nums = (
    list(range(4, 33, 4))            # 4,8,...,32        步长 4
    + list(range(48, 257, 16))       # 48,64,...,256     步长 16
    + list(range(288, 513, 32))      # 288,320,...,512   步长 32
    + list(range(576, 1024 + 1, 64)) # 576,640,...,1024  步长 64
    + list(range(1280, 4096 + 1, 256))
    + list(range(4608, self.max_handle_token_num + 1, 512))
)
graph_handle_token_nums = [e for e in graph_handle_token_nums if e <= self.max_handle_token_num]
graph_handle_token_nums.append(self.max_handle_token_num)
```

设计哲学与 decode 表一致：token 数越小档位越密（4 步长）、越大越疏（512 步长）。`max_handle_token_num` 由 `--prefill_cudagraph_max_handle_token`（默认 8192）与 `--batch_max_tokens` 取 `min` 决定（[L32-34](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py#L32-L34)）。

`__init__` 还揭示了一个关键共享：**prefill 图复用 decode 图的 mempool**：

[lightllm/common/basemodel/prefill_cuda_graph.py:22-28](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py#L22-L28)

```python
def __init__(self, decode_cuda_graph: CudaGraph, tp_world_size: int):
    self.graph = {}
    self.tp_world_size = tp_world_size
    if decode_cuda_graph is not None:
        self.mempool = decode_cuda_graph.mempool  # prefill 和 decode 共享一个 mempool
    else:
        self.mempool = torch.cuda.graph_pool_handle() if torch.cuda.is_available() else None
```

注释 `prefill 和 decode 共享一个 mempool` 点明了设计意图：两类图共用同一块显存池，避免重复占用。

捕获实现与 decode 差异较大——它把 graph_obj **挂到 infer_state 内部** 管理，并使用独立的 `graph_input_tensors` 缓冲：

[lightllm/common/basemodel/prefill_cuda_graph.py:77-95](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py#L77-L95)

```python
def _capture_prefill(self, prefill_func, input_tensors, infer_state):
    handle_token_num = infer_state.total_token_num - infer_state.prefix_total_token_num
    infer_state.mem_pool = self.mempool
    infer_state.prefill_cuda_graph_create_graph_obj()                # 建图并挂到 infer_state
    infer_state.prefill_cuda_graph_get_current_capture_graph().__enter__()
    graph_input_tensors = [torch.empty_like(e) for e in input_tensors]   # 独立的静态输入缓冲
    graph_out_tensors = prefill_func(graph_input_tensors, infer_state)   # 在捕获上下文内跑一次
    graph_out_tensors = [e.contiguous() for e in graph_out_tensors]
    infer_state.prefill_cuda_graph_get_current_capture_graph().__exit__(None, None, None)

    graph_input_tensors = [tensor_to_no_ref_tensor(e) for e in graph_input_tensors]
    graph_out_tensors = [tensor_to_no_ref_tensor(e) for e in graph_out_tensors]

    self.graph[handle_token_num] = (infer_state, graph_input_tensors, graph_out_tensors)
    self.replay(input_tensors, infer_state)
    return graph_out_tensors
```

几个要点：

- `handle_token_num = total_token_num - prefix_total_token_num`：只算 **需要新算的那部分**（命中 RadixCache 前缀缓存的 token 不算，见 [u4-l2](./u4-l2-radix-prefix-cache.md)），所以图是按「本拍实际计算量」键控的。
- `graph_input_tensors = [torch.empty_like(e) ...]`：**新建一组和输入同形状的空 tensor 作输入缓冲**，重放时把新数据 `copy_` 进它们——这是「地址不变」的 prefill 版本。
- `tensor_to_no_ref_tensor`：把 tensor 转成 **无引用计数** 的包装（通过 cupy 用原始 device_ptr 重新包一层，见 [tensor_utils.py:13-25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/tensor_utils.py#L13-L25)）。注释说明这是为了「避免 cuda graph 捕获时的引用计数问题，导致 prefill cuda graph 的中间 tensor 无法释放和共享」——这是 prefill 图特有的需求，decode 图不需要。
- graph_obj 被挂进 `infer_state.prefill_cuda_graph_exe_list`（见 [infer_struct.py:350-356](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L350-L356)），这个列表可以 **交错存放多个图与 CPU 回调函数**（`prefill_replay` 按序执行，见 [L378-385](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L378-L385)），为 prefill 中穿插的 TPSP 通信预留了扩展位。

重放与 decode 思路相同，但多一步「拷输入 tensor 列表」：

[lightllm/common/basemodel/prefill_cuda_graph.py:134-144](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py#L134-L144)

```python
def _replay(self, input_tensors, infer_state):
    handle_token_num = infer_state.total_token_num - infer_state.prefix_total_token_num
    graph_infer_state, graph_input_tensors, graph_output_tensors = self.graph[handle_token_num]
    for graph_in_tensor, in_tensor in zip(graph_input_tensors, input_tensors):
        graph_in_tensor.copy_(in_tensor)                         # 灌输入
    graph_infer_state.copy_for_prefill_cuda_graph(new_infer_state=infer_state)  # 灌状态
    graph_infer_state.prefill_replay(infer_state)                # 重放（可能含穿插的 CPU 回调）
    return graph_output_tensors
```

最后看一个 **重要差异**：prefill 图要求精确命中档位，不做 padding。在 `_context_forward` 里：

[lightllm/common/basemodel/basemodel.py:674-690](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L674-L690)

```python
handle_token_num = infer_state.input_ids.shape[0]
if self.prefill_graph is not None and self.prefill_graph.can_run(handle_token_num=handle_token_num):
    finded_handle_token_num = self.prefill_graph.find_closest_graph_handle_token_num(handle_token_num=handle_token_num)
    assert finded_handle_token_num == handle_token_num          # 必须精确命中，不 padding
    if self.prefill_graph.need_capture(handle_token_num=finded_handle_token_num):
        output_tensors = self.prefill_graph.capture_prefill(...)
    else:
        output_tensors = self.prefill_graph.replay(...)
else:
    ...                                                         # 走 eager prefill_func
```

`assert finded_handle_token_num == handle_token_num` 这一行是 prefill 图与 decode 图最大的策略差异：decode 是「向上 padding」，prefill 是 **「精确命中才用图，否则走 eager」**（这里 assert 直接要求相等；实际是否每次都恰好命中档位，取决于 chunked prefill 的切块策略，待本地验证）。

#### 4.4.4 代码实践

**实践目标**：对比两类图的「键」与「对齐策略」，理解为何 prefill 图默认关闭。

**操作步骤**（源码阅读型实践）：

1. 在 [basemodel.py:676-695](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L676-L695) 的 prefill 分支与 [basemodel.py:612-636](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L612-L636) 的 decode 分支之间做一张对照表，记录：键、padding 与否、容器元组、是否默认开启。
2. 阅读参数 help [api_cli.py:555-566](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L555-L566)，注意 `--enable_prefill_cudagraph` 标注的「currently only for llama and qwen model, not support ep moe model」。

**需要观察的现象**：prefill 分支有 `assert finded == handle_token_num`；decode 分支没有这个断言，而是 `_create_padded_decode_model_input`。

**预期结果**：能解释「为什么 decode 图几乎总是能用（小 batch 命中密集档位），而 prefill 图受限于精确命中且不支持复杂模型，所以默认关闭」。

**待本地验证**：开启 `--enable_prefill_cudagraph` 加载 llama 模型，观察启动日志 `prefill cuda graph graph_handle_token_nums: [...]`（[prefill_cuda_graph.py:57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/prefill_cuda_graph.py#L57)）打印的 token 档位表。

#### 4.4.5 小练习与答案

**练习 1**：为什么 prefill 图要复用 decode 图的 `mempool`，而不是自己开一个？

**答案**：所有 CUDA Graph 共享同一个 mempool 可以让中间 tensor 在不同图之间复用同一块显存，大幅降低显存占用；若各自开池，每张图都要为中间张量独占一份显存，几百张图会撑爆显存。注释 `prefill 和 decode 共享一个 mempool` 明确表达了这一意图。

**练习 2**：`_capture_prefill` 里为什么要把 `graph_input_tensors` 单独建一组 `torch.empty_like`，而不是直接用传进来的 `input_tensors`？

**答案**：传进来的 `input_tensors` 每次推理地址都不同，违反「地址固定」约束；单独建一组静态缓冲作为「固定地址容器」，重放时把新数据 `copy_` 进去，才能让录好的图读到新输入。这与 decode 图留存固定 `infer_state` 是同一个套路的不同实现。

## 5. 综合实践

把本讲三个最小模块（CUDA Graph、捕获与重放、batch 对齐）串起来，完成下面这个 **「档位表 + 决策链」综合追踪** 任务：

**背景**：假设你用默认参数（`graph_split_batch_size=32`、`graph_grow_step_size=16`、`graph_max_batch_size=256`、`mtp_step=0`、无 tpsp）启动了一个 llama 服务，decode 图已 warmup 完成。

**任务**：

1. **算档位表**：手写（或用脚本）输出 `gen_cuda_graph_batch_sizes(256, 1)` 的完整结果。
2. **追决策链**：对以下三个真实 decode batch，分别给出 `_decode` 中 `can_run`、`find_closest_graph_batch_size`、`need_capture` 三个函数的返回值，并指出最终走 `capture_decode` 还是 `replay` 还是 eager：
   - (a) batch=20，序列长度 1024
   - (b) batch=40，序列长度 1024
   - (c) batch=40，序列长度 9000
3. **解释现象**：为什么 (b) 和 (c) 同样是 batch=40，命运却不同？
4. **设计优化**：如果你的服务 decode batch 绝大多数集中在 30~50，你会如何调整 `--graph_split_batch_size` / `--graph_grow_step_size` 来减少 padding？说出取舍。

**参考答案**：

1. 档位表：`[1..32] ∪ [48,64,80,...,240] ∪ [256]`。
2. (a) `can_run=True`、`find_closest=20`（精确命中密集档位）、`need_capture=False` → `replay`。(b) `can_run=True`、`find_closest=48`、`need_capture=False` → `replay`（batch 从 40 padding 到 48）。(c) `can_run=False`（9000 > 8192 的 `graph_max_len_in_batch`）→ eager。
3. (b) 序列没超长，图可用，靠 padding 跑； (c) 序列超长，`can_run` 在序列长度维度返回 False，回退 eager。说明 `can_run` 同时把关 batch 和序列长度两个维度。
4. 把 `--graph_split_batch_size` 提到 50 以上（让 30~50 全进密集区逐 1 录制），或把 `--graph_grow_step_size` 调小到 8。取舍：padding 减少 → decode 延迟更稳更低，但 warmup 录的图变多、启动变慢、显存占用上升。

## 6. 本讲小结

- **CUDA Graph 把一串 kernel 录成一张图**，重放时 CPU 只发起一次命令，把 decode 阶段几十上百次 kernel launch 摊销成 1 次，是降低 decode 单 token **延迟** 的关键。
- 它有 **形状固定、地址固定** 两大约束：形状靠 **离散档位表 + 向上 padding** 解决，地址靠 **留存捕获时的 infer_state/output 作固定容器 + 重放前 `copy_` 灌新数据** 解决（「地址不变、内容可变」）。
- **档位表** `gen_cuda_graph_batch_sizes` 用两段式策略：`[1, graph_split_batch_size]` 逐 1 密集，`[split, max]` 按 `graph_grow_step_size` 稀疏；`find_closest_graph_batch_size` 用 `bisect_left` 向上取最近档位。
- **决策链** 在 `_decode` 内：`self.graph is not None` → `can_run`（batch 与序列长度双把关）→ `find_closest`（对齐档位）→ `need_capture`（首次则捕获，否则重放）。
- **捕获** 用 `torch.cuda.graph(...)` 录制并留存 `(graph_obj, infer_state, model_output)`；warmup 时一段「浅拷贝跑一遍再删新增属性」的怪操作，是为了规避「按属性存在性一次性初始化」代码与捕获的冲突。
- **prefill 图** 以 `handle_token_num` 为键、复用 decode 的 mempool、要求 **精确命中档位**（不 padding），且默认关闭、仅支持部分模型，是比 decode 图更受限的优化。

## 7. 下一步学习建议

- 阅读 [u6-l2 microbatch overlap 与 TPSP 混合并行](./u6-l2-microbatch-overlap-tpsp.md)，看 `enable_decode_microbatch_overlap` 如何把两张 decode 图交错重放（对应本讲的 `_capture_decode_overlap` / `warmup_overlap`），进一步隐藏通信延迟。
- 结合 [u3-l5 注意力后端机制](./u3-l5-attention-backends.md) 中的 `copy_for_decode_cuda_graph`，理解「地址不变、内容可变」模式如何贯穿注意力状态与 infer_state 两层。
- 想深入 PyTorch 层面，可阅读 PyTorch 官方文档中 `torch.cuda.CUDAGraph` 与 `torch.cuda.graph_pool_handle` 的说明，理解 mempool 共享的底层语义。
- 动手实验：用一个真实小模型（如 llama）分别以 `--disable_cudagraph`、默认参数、`--enable_prefill_cudagraph` 三种方式启动，对比 warmup 日志中的档位表与启动耗时，建立对「图数量 vs 启动开销」的直觉。
