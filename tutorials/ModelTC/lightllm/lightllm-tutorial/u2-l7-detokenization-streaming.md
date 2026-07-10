# Detokenization 与流式输出

## 1. 本讲目标

在上一讲（u2-l5 Router 调度循环）里，我们看清了 Router 如何把请求调度成 batch 并交给 ModelBackend 推理。但 ModelBackend 的产物只是**一串 token id**（整数），并不是用户最终看到的文字。把 token id 还原成可读文本、并逐段「流」给 HTTP 客户端，正是 **Detokenization 进程**的职责。

本讲学完后，你应该能够：

- 说清 Detokenization 进程在整个多进程架构中的位置：它从 Router 收请求登记、从共享内存读新生成的 token、把解码后的文本推回 HttpServer。
- 理解「增量解码」的原理：为什么不能每来一个 token 就全量 `decode` 一遍，而是用 `prefix_offset / read_offset` 两个游标做差量。
- 掌握 Detokenization 与 HttpServer 之间的 **zmq PUB/SUB + 共享内存**流式协作机制，并明白为什么线上只传一个 `None` 通知。
- 认识 tokenizer、特殊 token（special token、eos）在这一环节的处理方式。

## 2. 前置知识

- **token 与文本的关系**：LLM 内部只处理整数 token id。把文本切成 id 叫 **tokenize / encode**，把 id 还原成文本叫 **detokenize / decode**。同一个文本切成 id、再还原回来，应当一致。
- **BPE / SentencePiece 的边界问题**：很多 token 对应的并不是完整字符。例如一个 UTF-8 中文字可能被切成「半个字节序列」，或者一个词被切成「` ` + `ing`」。如果只单独 decode 最新那一个 token，结果可能与把它放回上下文里 decode 不一致。因此工程上采用「连同前几个 token 一起 decode，再裁掉已输出前缀」的做法。
- **zmq 的 PUSH/PULL 与 PUB/SUB**：PUSH/PULL 是「点对点投递，一对一」；PUB/SUB 是「广播订阅，一对多」。本讲里 Router → Detokenization 用 PUSH/PULL，Detokenization → HttpServer 用 PUB/SUB。
- **共享内存（shared memory）**：多个进程映射同一块物理内存，读写无需经过 zmq 拷贝。LightLLM 把大块数据（请求、生成的 token、输出文本）放在共享内存，线上只传索引或通知。这是 u2-l1、u2-l3 已建立的核心理念。
- **流式输出（streaming）**：HTTP 层常见的 SSE（Server-Sent Events），让服务器边生成边把一段段文本推给客户端，而不是等全部生成完才返回。

## 3. 本讲源码地图

本讲围绕三个核心文件展开，并辅以三个支撑文件：

| 文件 | 作用 |
| --- | --- |
| `lightllm/server/detokenization/manager.py` | Detokenization 进程的**主体**：`DeTokenizationManager` 负责收请求、解码、回推通知；`start_detokenization_process` 是进程入口。 |
| `lightllm/server/detokenization/decode.py` | 增量解码的核心函数 `decode_token`：用前缀差量算出本轮新增文本。 |
| `lightllm/server/tokenizer.py` | 统一的 tokenizer 装载入口 `get_tokenizer`，封装 HuggingFace `AutoTokenizer` 并处理多模态/特殊 tokenizer 分发。 |
| `lightllm/server/detokenization/decode_req.py` | `DecodeReq`：每个请求在 detoken 进程内的解码状态（输出 id、两个游标、停止串等）。 |
| `lightllm/server/detokenization/decode_mode_fix.py` | PD 分离模式下 decode 节点的特殊修复（预消费 prompt 最后一个 token）。 |
| `lightllm/server/core/objs/out_token_circlequeue.py` | `CircularQueue`：存放在共享内存里的环形输出队列，是 detoken 进程写、httpserver 读的「数据中转区」。 |

消费端（HttpServer 侧）的两个文件用于说明订阅与回流关系：

- `lightllm/server/httpserver/manager.py`：建立 SUB 订阅、`handle_loop` 拉取共享内存里的文本。
- `lightllm/server/router/manager.py`：Router 侧 PUSH 请求登记给 detoken 进程。

## 4. 核心概念与源码讲解

### 4.1 反 token 化进程总览（Detokenization）

#### 4.1.1 概念说明

「反 token 化」就是把模型生成的 token id 序列翻译回人类可读文本。你可能会问：HttpServer 不也能拿到 tokenizer 吗，为什么非要单独开一个进程来做这件事？

原因有三：

1. **职责隔离**：HttpServer 是 async 的 Web 层，Router 是调度层，ModelBackend 是 GPU 推理层。把「id → 文本」这件 CPU 密集、又涉及逐 token 增量处理的工作独立出来，避免拖慢任何一个关键路径。
2. **解耦生产与消费**：ModelBackend 在共享内存里不断写新 token，Detokenization 进程按自己的节奏消费、解码、聚合，互不阻塞。
3. **天然契合多进程 IPC**：Detokenization 既是下游（从 Router 收登记），又是上游（向 HttpServer 发通知），用 zmq + 共享内存能很干净地串起来。

在 u2-l1 的请求闭环里，Detokenization 处于「Router/ModelBackend 之后、HttpServer 之前」的中间环节：

```
HttpServer --(PUSH 请求索引)--> Router --(PUSH 登记索引)--> Detokenization
ModelBackend 把生成 token 写入共享内存的 Req
Detokenization 读共享内存 token --> 解码成文本 --> 写入共享内存 out_tokens_queue
Detokenization --(PUB 空通知)--> HttpServer(SUB) --> 从共享内存读文本 --> 流式返回客户端
```

注意一个关键点：**token 数据走共享内存，zmq 只传轻量通知**。这与 u2-l3 的「对象放共享内存、线上只传索引」一脉相承。

#### 4.1.2 核心流程

Detokenization 进程启动后进入一个无限循环（`handle_loop`），每轮做三件事：

```
loop:
    1. 用 NOBLOCK 从 PULL 套接字尽量多地收「新请求登记」（GroupReqIndexes）；
       没有了就转入解码。
    2. 调 gen_token_out()：
       遍历所有正在追踪的 DecodeReq，
         - 若该请求有未解码的新 token 且输出队列没满：
             取下一个 token id → 调 decode_token 得到增量文本 → 写入 out_tokens_queue
         - 只要任一请求本轮真的解码出了东西（exist_decode），
           就向 HttpServer PUB 一个通知（payload 是 None）。
    3. 回收已彻底完成的请求（remove_finished_reqs）。
    若这一轮没有任何请求需要解码（exist_need_detoken=False），sleep 一小段再循环。
```

请求在 detoken 进程内的生命周期：

- **登记**：`_add_new_group_req_index` 把请求登记进 `req_id_to_out` 字典，创建对应的 `DecodeReq`。
- **解码**：每轮循环增量解码新 token。
- **完成与回收**：当请求满足释放条件（见 `can_set_release_mark`），把 `can_released_mark` 置位、归还共享内存槽位、从字典移除。

#### 4.1.3 源码精读

先看进程入口与构造。`start_detokenization_process` 设置进程标题、注册优雅退出、构造 `DeTokenizationManager` 并通过 pipe 回报 `"init ok"`，最后进入 `handle_loop`：

[lightllm/server/detokenization/manager.py:169-183](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L169-L183) — 进程入口：构造 manager、回报初始化成功、进入主循环。

构造函数建立了两把 zmq 套接字和 tokenizer：

[lightllm/server/detokenization/manager.py:30-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L30-L43) — PULL 套接字 `bind` 在 `detokenization_port`（收 Router 的登记），PUB 套接字 `bind` 在 `http_server_port`（向 HttpServer 广播）；装载 tokenizer、缓存 `all_special_ids`、构造共享内存请求管理器。

> 这里有个容易混淆的命名：`http_server_port` 不是 HttpServer 自己 bind 的，而是 **Detokenization PUB bind、HttpServer SUB connect** 的那个端口——它被命名为「属于 httpserver 的端口」，因为 HttpServer 是这条广播的订阅方。

主循环 `handle_loop` 用一个 `recv_max_count` 自适应批读，避免队列积压时阻塞：

[lightllm/server/detokenization/manager.py:70-98](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L70-L98) — 收登记（最多批量收 128~256 个）、调 `gen_token_out` 解码；空闲时 `time.sleep(0.002)` 让出 CPU。

请求登记的核心：从 `GroupReqIndexes` 里拿到每个请求在共享内存里的索引，重新 link 出 prompt/logprobs 数组，再创建 `DecodeReq`；PD decode 模式或 token_healing 模式有额外初始化：

[lightllm/server/detokenization/manager.py:49-68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L49-L68) — 登记新请求，PD decode 模式调用 `decode_mode_fix`，token_healing 模式初始化前缀串。

解码主逻辑 `gen_token_out` 是本进程最关键的函数（4.2、4.3 还会回到这里）：

[lightllm/server/detokenization/manager.py:100-153](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L100-L153) — 遍历每个 `DecodeReq`，对有新 token 且输出队列未满者解码、追加文本、做停止串匹配、`push` 进 `out_tokens_queue`；只要本轮有解码产出就 PUB 通知；最后回收完成请求。

最后看完成回收——只有 `can_set_release_mark()` 为真（中止 / 停止串命中 / 正常 finish 且 token 已全部解码完）才会归还共享内存槽位：

[lightllm/server/detokenization/manager.py:155-166](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L155-L166) — 设置 `can_released_mark=True`、归还 `ShmReqManager` 槽位、从字典移除。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，画出单个请求在 Detokenization 进程里的完整生命周期。

**操作步骤**：

1. 打开 `lightllm/server/detokenization/manager.py`，定位到 `_add_new_group_req_index`（第 49 行）。
2. 追踪一个请求从被 `GroupReqIndexes` 登记开始，经历了哪些字段赋值（`prompt_ids`、`output_ids`、`prefix_offset`、`read_offset`）。
3. 跟到 `gen_token_out`（第 100 行），理解 `decode_req.need_detoken()` 与 `out_queue_is_full()` 这两个「闸门」如何决定本轮要不要解码。
4. 最后看 `remove_finished_reqs`（第 155 行）与 `DecodeReq.can_set_release_mark`（`decode_req.py` 第 85 行）。

**需要观察的现象**：`req_id_to_out` 字典里的 `DecodeReq` 在登记时被加入、在 `remove_finished_reqs` 里被 `pop` 掉。你可以想象在 `gen_token_out` 的 for 循环前后各打一条日志（**仅作练习想象，不要真的改源码**）：

```python
# 示例代码（仅作说明，非项目原有代码）
logger.debug(f"tracking reqs: {list(self.req_id_to_out.keys())}")
```

**预期结果**：一个请求 id 会持续出现在日志里若干轮，直到生成结束、token 全部解码完、`can_set_release_mark()` 为真后消失。

**待本地验证**：若你本地能跑起服务，可在 `handle_loop` 内统计每轮 `len(self.req_id_to_out)`，观察并发请求数变化曲线。

#### 4.1.5 小练习与答案

**练习 1**：Detokenization 进程为什么会用 `zmq.NOBLOCK` 批量收登记，而不是阻塞式一个一个收？

**参考答案**：因为登记（收新请求）和解码（`gen_token_out`）共用同一个循环。若阻塞收，一旦 zmq 队列暂时为空，进程就会卡住不去解码，导致已生成 token 的解码延迟变大。用 `NOBLOCK` 批量收「能收到的」，收完立刻去解码，保证解码及时；并通过自适应的 `recv_max_count`（128→256）在积压时一次多收、空闲时回调到 128。

**练习 2**：`gen_token_out` 返回的 `exist_need_detoken` 用来做什么？

**参考答案**：它告诉主循环「是否还有请求存在尚未解码的 token」。若为 `False`，说明本轮所有请求都无需解码（比如 token 还没被 ModelBackend 写出来），主循环就 `time.sleep(0.002)` 短暂休眠，避免空转烧 CPU；若为 `True` 则立刻进入下一轮，尽量低延迟地解码。

---

### 4.2 增量解码：如何只输出「新增」的文本

#### 4.2.1 概念说明

最朴素的解码思路是：每来一个新 token，就把「整条 prompt + 已生成 token」一起 `tokenizer.decode` 一遍，返回完整文本。问题在于：

- **重复计算**：长度越长，每轮 decode 的开销越大，且 HttpServer 还要自己想办法算出「这次比上次多了哪几个字」。
- **BPE 边界错位**：单独 decode 一个 token 的结果，和把它拼回上下文再 decode 的结果可能不同（典型如词首空格、多字节字符被切断）。

LightLLM（借鉴 vLLM 的做法）采用**差量解码**：维护两个游标 `prefix_offset` 和 `read_offset`，每轮 decode 一小段「包含边界安全余量」的 token 序列，裁掉前缀就是新增文本。

#### 4.2.2 核心流程

设某请求的共享内存 token 数组为 `arr`，prompt 长度为 `input_len`，已解码输出的 token 数为 `|output_ids|`。两个游标的含义：

- `prefix_offset`：**解码起点**，留出一小段安全余量（默认往前 5 个 token），用于吸收跨 token 边界的 BPE 合并。
- `read_offset`：**已提交文本对应的终点**。

每轮解码做两次 decode 并求差：

```
prefix_text = decode( arr[prefix_offset : read_offset] )          # 上次已提交的「参考文本」
new_text    = decode( arr[prefix_offset : input_len + |output_ids|] )  # 截至本次的「完整文本」

if new_text 比 prefix_text 长，且 new_text 不以替换符结尾:
    增量 = new_text[len(prefix_text):]        # 裁掉前缀 = 真正新增的文字
    prefix_offset = read_offset               # 游标前移
    read_offset    = input_len + |output_ids| # 游标前移
    return 增量
else:
    return ""                                  # 本轮不产出，等更多 token 再决定
```

游标推进关系可用公式表达。设本轮新终点为：

\[
E = \text{input\_len} + |\text{output\_ids}|
\]

成功产出后，下一轮的参考区间就是本轮的有效区间：

\[
\text{prefix\_offset} \leftarrow \text{read\_old}, \qquad \text{read\_offset} \leftarrow E
\]

**为什么要有 `prefix_offset` 这个「回退 5 个 token」的余量？** 因为 BPE 的合并可能跨越 token 边界。如果直接从 `read_offset` 开始 decode 新 token，可能会丢掉与上一段结尾本应合并的字符。往前多 decode 一段（5 个 token）作为「重叠区」，再用字符串裁剪抵消，就能保证拼接正确。这个 5 由环境变量 `LIGHTLLM_DECODE_PREFIX_LENGTH` 控制。

**为什么检查 `new_text.endswith("�")`？** `�` 是 UTF-8 解码失败时的替换符（replacement character）。若 `new_text` 以它结尾，说明当前 token 序列正好切在一个多字节字符的中间，此刻产出会得到乱码。正确做法是**本轮先不产出**，等下一个 token 到来把字节序列补全，再一起 decode。

**特殊 token 处理**：

- `decode_token` 把请求的 `skip_special_tokens`、`spaces_between_special_tokens`（来自 `sample_params`）透传给 `tokenizer.decode`，让 tokenizer 自己决定要不要把 `<s>`、`</s>` 这类 special token 显示出来。
- **eos**：若新 token 是 eos 且 `sample_params.print_eos_token` 为 `False`，直接返回空串，不把 eos 当作可见文本输出。
- 在 `gen_token_out` 里还会用一个 `special` 标记（`new_token_id in self.all_special_ids`）记录该 token 是否为特殊 token，随队列传给 HttpServer，供客户端区分。

#### 4.2.3 源码精读

增量解码的核心函数 `decode_token`：

[lightllm/server/detokenization/decode.py:7-35](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/decode.py#L7-L35) — eos 抑制、两次 `tokenizer.decode`（带 special token 选项）、裁掉前缀得到增量文本、推进两个游标；若未变长或以替换符结尾则返回空。

两个游标的初值在 `DecodeReq.__init__` 里设定：

[lightllm/server/detokenization/decode_req.py:9](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/decode_req.py#L9) — 安全余量常量 `LIGHTLLM_DECODE_PREFIX_LENGTH = 5`（可由同名环境变量覆盖）。

[lightllm/server/detokenization/decode_req.py:23-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/decode_req.py#L23-L29) — `prefix_offset` 取「prompt 末尾往前 5」与 0 的较大值；`read_offset` 正常模式为 `len(prompt_ids)`，PD decode 模式为 `len(prompt_ids) - 1`（少一个，留给 decode_mode_fix 预消费）。

取出两段 token 的工具方法：

[lightllm/server/detokenization/decode_req.py:80-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/decode_req.py#L80-L83) — `get_decode_tokens` 切出 `prefix_tokens`（参考段）与 `read_tokens`（截至当前的完整段）。

取「下一个待解码 token」：

[lightllm/server/detokenization/decode_req.py:76-78](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/decode_req.py#L76-L78) — `src_index = input_len + |output_ids|`，从共享内存数组取该位置的 token id。

**PD 分离模式的特殊修复** `decode_mode_fix`：在 PD 架构里，prefill 节点算出第一个 token，连同 KV 一起迁给 decode 节点。decode 节点收到的 prompt_ids 末尾其实已经包含了「prefill 阶段产出的那个首个输出 token」，但 Detokenization 还没把它当输出处理过。于是这里主动「预消费」它一次：

[lightllm/server/detokenization/decode_mode_fix.py:13-16](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/decode_mode_fix.py#L13-L16) — 把 `prompt_ids` 的最后一个 id 当作新 token 调一次 `decode_token`，从而让游标、output_ids 与正常模式对齐，后续 decode 节点的逐 token 增量解码就能无缝衔接。

> 这正是本讲实践任务关注的点：`decode_mode_fix` 本身不是「增量解码算法」，而是 **PD decode 模式下让增量解码游标正确初始化的适配步骤**。真正的增量逻辑在 `decode_token` 里；`decode_mode_fix` 通过提前调用一次 `decode_token`，把「prefill 的首个输出 token」纳入正常的增量解码轨道。

最后看 tokenizer 的装载入口，理解 detoken 进程手里的 tokenizer 是怎么来的：

[lightllm/server/tokenizer.py:42-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/tokenizer.py#L42-L67) — `get_tokenizer` 封装 `AutoTokenizer.from_pretrained`，支持 slow/fast 模式与加载失败回退；后续还会按 `model_type` 分发到多模态专用 tokenizer（视觉/音频模型）。

detoken 进程在构造时调用它并缓存 `all_special_ids`：

[lightllm/server/detokenization/manager.py:37-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L37-L38) — 装载 tokenizer，并 `set(self.tokenizer.all_special_ids)` 得到 O(1) 判断 special 的集合。

#### 4.2.4 代码实践

**实践目标**：动手验证 `decode_token` 的增量裁剪逻辑，并解释 `decode_mode_fix` 的作用。

**操作步骤（源码阅读 + 本地小实验）**：

1. **读源码**：对照 `decode.py` 的第 18–35 行，写出当 `prefix_text="Hello"`、`new_text="Hello world"` 时，返回值与游标变化。
2. **本地小实验**（需要可用的 Python 环境与一个 HF tokenizer，**待本地验证**）：

   ```python
   # 示例代码（非项目原有代码，仅用于理解 decode_token 的裁剪原理）
   from transformers import AutoTokenizer
   tok = AutoTokenizer.from_pretrained("<某个本地模型目录>")
   # 模拟 prompt + 逐步增长的 output
   ids = tok("The capital of France is", return_tensors=None)["input_ids"]
   for word in [" Paris", ".", " It"]:
       ids = ids + tok(word, add_special_tokens=False)["input_ids"]
       prefix_text = tok.decode(ids[:-1])
       new_text = tok.decode(ids)
       if len(new_text) > len(prefix_text) and not new_text.endswith("�"):
           print("increment:", repr(new_text[len(prefix_text):]))
   ```

3. **理解 PD 修复**：阅读 `decode_mode_fix.py`，并对照 `decode_req.py` 第 25–29 行 `read_offset` 在 PD 模式下的初值差异，解释为什么 PD decode 模式要把 `read_offset` 设成 `len(prompt_ids) - 1`。

**需要观察的现象 / 预期结果**：

- 步骤 2 每轮应只打印「新增的片段」（如 ` Paris`、`.`、` It`），而不是整句重复——这正是增量解码的效果。
- 步骤 3：因为 PD decode 节点的 prompt_ids 末尾多塞了「prefill 的首个输出 token」，把 `read_offset` 退一格再让 `decode_mode_fix` 用 `decode_token` 消费它一次，相当于把首个输出 token 从「prompt 区」搬到「output 区」，后续解码才不会重复或漏掉它。

**待本地验证**：步骤 2 的实际输出取决于所用 tokenizer 的切分方式；步骤 3 属纯源码推理，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `LIGHTLLM_DECODE_PREFIX_LENGTH` 设成 0（即 `prefix_offset == read_offset`），可能会有什么问题？

**参考答案**：解码起点和已提交终点重合，`prefix_text` 会变成空串，裁剪逻辑退化。更关键的是丢失了「跨 token 边界」的安全余量：当一个字符/词由「上一段最后一个 token + 本轮新 token」共同决定时（如 BPE 的词首空格、被切断的多字节字符），没有重叠区就无法正确合并，可能出现首字符丢失或乱码。

**练习 2**：`new_text.endswith("�")` 时返回空串，那个被「卡住」的 token 会丢失吗？

**参考答案**：不会丢。它仍然在 `output_ids` 与共享内存数组里。本轮只是**不产出文本**、不推进 `read_offset`。等下一个 token 到来，`read_tokens` 区间变大，重新 decode 时该多字节字符被补全，`�` 消失，此时一次性把积攒的文字产出。游标随之正常推进。

**练习 3**：eos token 被解码时会发生什么？

**参考答案**：见 `decode.py` 第 14–16 行。若 `new_token_id in eos_id` 且 `sample_params.print_eos_token` 为假，函数直接 `return ""`——eos 不作为可见文字输出。但该 token 仍会被 `output_ids.append`，并通过后续流程参与 finish 判定。

---

### 4.3 流式推送：把文本一段段送到 HttpServer

#### 4.3.1 概念说明

解码出文本后，怎么把它「送」给 HttpServer？最直白的想法是：detoken 进程把每段新文本通过 zmq 直接发给 HttpServer。但 LightLLM 没这么做，而是采用 **「数据在共享内存、zmq 只发空通知」** 的模式：

- **数据载体**：`out_tokens_queue`，一个嵌在 `Req`（ctypes 结构体）里的 `CircularQueue`，本身就住在共享内存。detoken 进程往里 `push`，HttpServer 从里 `peek/pop`，全程零拷贝。
- **通知载体**：detoken 进程每轮若有产出，就 PUB 一个 `None`（没有 payload）。HttpServer 用 SUB 收到这个「唤醒信号」后，去共享内存把队列里的文本读出来。

为什么要这样设计？

1. **避免大文本反复经 zmq 序列化/网络栈**：文本可能很长，每段都 pickle 一遍发出去既慢又占带宽。放共享内存最省。
2. **解耦「有新的了」与「具体内容」**：通知只表达「该去看了」，真正的读取由 HttpServer 按自己的节奏批量进行。
3. **天然支持背压**：队列有容量上限（`is_full`），满了 detoken 就暂停写入，HttpServer 读走一批后自然缓解。

#### 4.3.2 核心流程

环形队列的容量与判满：

\[
N = \text{LIGHTLLM\_OUT\_TOKEN\_QUEUE\_SIZE} \;(\text{默认 } 8)
\]

环形队列用「牺牲一个槽位」来区分空和满：

\[
\text{is\_full} \iff (\text{tail}+1) \bmod N = \text{head}, \qquad \text{is\_empty} \iff \text{tail} = \text{head}
\]

也就是说实际可用容量是 \(N-1 = 7\) 个文本片段。

完整的流式回流链路：

```
[Detokenization 进程]
  gen_token_out:
    每解码出一个 token 文本片段 ->
        decode_req.req.out_tokens_queue.push(text, src_index, special, count)
    若本轮有产出 ->
        pub_to_httpserver.send_pyobj(None)        # 空通知，唤醒 HttpServer

        ↓ zmq PUB (bind http_server_port)

[HttpServer 进程]
  handle_loop (常驻 async 协程):
    await zmq_recv_socket.recv_pyobj()             # 收到 None 唤醒
    遍历 req_id_to_out_inf:
        对每个 req 的 out_tokens_queue:
            peek 取 (text, src_index, special, count)
            组装 metadata (logprob, special, prompt_cache_len ...)
            pop_no_ret()                            # 从共享内存移除
        收集到 token_list ->
            req_status.out_token_info_list.extend(token_list)
            req_status.event.set()                  # 唤醒等待该请求的协程

  _wait_to_token_package (每个请求一个消费者协程):
    等 event 被 set ->
        逐条 yield (sub_req_id, out_str, metadata, finish_status)
        -> 上层流式端点拼成 SSE / 非流式响应返回给客户端
```

这就是 u2-l2 已经介绍过的「事件 + 暂存区」中转模式在 detokenization 这一段的具体落点：detoken 是生产者、HttpServer 的 `handle_loop` 是搬运工、`_wait_to_token_package` 是消费者。

#### 4.3.3 源码精读

**生产端**：detoken 进程把文本片段 push 进共享内存队列，并发空通知：

[lightllm/server/detokenization/manager.py:142](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L142) — `decode_req.req.out_tokens_queue.push(new_text, src_index, special, count_output_tokens)`，把增量文本连同来源索引、是否特殊 token、输出计数写入共享内存环形队列。

[lightllm/server/detokenization/manager.py:148-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/detokenization/manager.py#L148-L149) — 只要本轮有任何解码产出（`exist_decode`），就向 HttpServer PUB 一个 `None`（`pickle.HIGHEST_PROTOCOL`）。

**数据载体**：环形队列实现。注意它整体是 ctypes 结构体，因此能嵌在 `Req` 里随共享内存被两个进程同时映射：

[lightllm/server/core/objs/out_token_circlequeue.py:11](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/out_token_circlequeue.py#L11) — 队列长度常量 `LIGHTLLM_OUT_TOKEN_QUEUE_SIZE = 8`（环境变量可覆盖）。

[lightllm/server/core/objs/out_token_circlequeue.py:71-83](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/out_token_circlequeue.py#L71-L83) — `is_full` 用「牺牲一格」判定；`push` 把文本（UTF-8 字节）写入 `QueueItem` 并前移 `tail`，超长会被截断。

[lightllm/server/core/objs/out_token_circlequeue.py:97-112](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/out_token_circlequeue.py#L97-L112) — `peek`（看队首不移除）与 `pop_no_ret`（移除不返回），HttpServer 用 peek+pop_no_ret 配合读取。

**消费端**：HttpServer 的 SUB 订阅。注意它 `connect` 到 `http_server_port` 并订阅全部（`b""`）：

[lightllm/server/httpserver/manager.py:102-104](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L102-L104) — 建立 SUB 套接字，connect 到 `http_server_port`，`SUBSCRIBE b""` 表示接收所有消息（不按前缀过滤）。

HttpServer 的 `handle_loop` 收到唤醒后，从共享内存把队列里的文本搬到每请求的暂存区，并用 `event.set()` 唤醒等待方：

[lightllm/server/httpserver/manager.py:880-940](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L880-L940) — `recv_pyobj`（丢弃值，仅作唤醒）；遍历每个 req 的 `out_tokens_queue`：若队列满则一次读 `LIGHTLLM_OUT_TOKEN_QUEUE_SIZE` 条，否则读 1 条；用 `peek` 取文本与来源索引、组装 `metadata`、`pop_no_ret` 消费；最后 `out_token_info_list.extend` 并 `event.set()`。

> 这里有个背压细节：`read_token_count` 默认是 1，但若发现 `out_tokens_queue.is_full()`，就一次读满 `LIGHTLLM_OUT_TOKEN_QUEUE_SIZE` 条——队列快满时加大搬运力度，避免 detoken 进程因队列满而停写。

最终消费者 `_wait_to_token_package` 被该 `event` 唤醒后，逐条 `yield` 文本片段给上层 HTTP 端点：

[lightllm/server/httpserver/manager.py:697-725](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L697-L725) — `event.clear()` 后检查暂存区；遍历 `out_token_info_list`，组装 `metadata`（含 `prompt_tokens`、`prompt_cache_len` 汇总等），`yield (sub_req_id, out_str, metadata, finish_status)` 给流式/非流式端点。

**对比：上游 Router → Detokenization 的 PUSH**。这条链路与本讲的 PUB/SUB 形成对照：Router 用 PUSH 把「请求登记索引」发给 detoken 进程（PULL）：

[lightllm/server/router/manager.py:85-86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L85-L86) — Router 侧 PUSH 套接字 connect 到 `detokenization_port`。

[lightllm/server/router/manager.py:421](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L421) — 在 `_add_req` 里把 `GroupReqIndexes`（只含请求在共享内存的索引列表）PUSH 给 detoken 进程登记。

#### 4.3.4 代码实践

**实践目标**：说清 Detokenization 向 `http_server_port` 的 PUB 与 HttpServer 的 SUB 之间的订阅关系，并解释为何通知是空的。

**操作步骤**：

1. 在 `detokenization/manager.py` 第 34–35 行找到 PUB 的 `bind`；在第 148–149 行找到发送内容（`send_pyobj(None)`）。
2. 在 `httpserver/manager.py` 第 102–104 行找到 SUB 的 `connect` 与 `SUBSCRIBE b""`；在第 882 行找到接收（`recv_pyobj()`，返回值未被使用）。
3. 回答两个问题（见预期结果）。

**需要观察的现象 / 预期结果**：

- **订阅关系**：Detokenization 进程是发布方，在 `http_server_port` 上 `bind` 一个 PUB 套接字；HttpServer 进程是订阅方，`connect` 到同一端口并用 `setsockopt(zmq.SUBSCRIBE, b"")` 订阅所有消息。一对一（单 HttpServer 场景），但语义上是广播/订阅。
- **为什么 payload 是 `None`**：真正要传给客户端的文本片段已经通过共享内存的 `out_tokens_queue`（`push`/`peek`）零拷贝传递了。zmq 上的消息只起「闹钟」作用——告诉 HttpServer「有新文本可能可读了，去共享内存看一眼」。这样既避免大文本反复序列化，又让 HttpServer 能批量、按节奏地搬运（甚至队列满时一次搬一批）。

**待本地验证**：可阅读 `out_token_circlequeue.py` 的 `push/peek/pop_no_ret`，确认 detoken 与 httpserver 操作的是同一块共享内存结构（`out_tokens_queue` 嵌在 `Req` 内，见 u2-l3）。

#### 4.3.5 小练习与答案

**练习 1**：既然 PUB 的内容是 `None`、HttpServer 还自带 0.05s 超时轮询（`handle_loop` 的 `wait_for(..., timeout=0.05)`），那这个 PUB 通知还有存在的必要吗？

**参考答案**：有必要。0.05s 超时是最坏情况下的兜底（比如 PUB 消息丢失或时序抖动），用它保证即使没收到通知，HttpServer 最多 50ms 也会去共享内存看一次。但正常情况下，PUB 通知能把端到端流式延迟从「最高 50ms」降到「近乎即时」——token 一解码完，HttpServer 立刻被唤醒搬运。两者是「低延迟主路径 + 兜底轮询」的关系。

**练习 2**：detoken 进程 `gen_token_out` 里有一个 `out_queue_is_full()` 检查（第 108 行）。如果删掉它会发生什么？

**参考答案**：`CircularQueue.push` 在满时会直接 `raise Exception("Queue is full")`（见 `out_token_circlequeue.py` 第 75–76 行），导致 `gen_token_out` 抛异常、`handle_loop` 进入异常处理甚至退出 detoken 进程。这个检查的作用是：当 HttpServer 还没来得及搬运、队列已满时，detoken 主动**跳过**对该请求本轮的写入（不 append、不 push），等下一轮 HttpServer 搬走一批、腾出空间再写。这是一种跨进程背压保护。

**练习 3**：把 `LIGHTLLM_OUT_TOKEN_QUEUE_SIZE` 调大（比如 32）会有什么利弊？

**参考答案**：好处是 detoken 进程可以攒更多片段、减少因队列满而停写的概率，在 HttpServer 繁忙时更不容易阻塞 detoken；坏处是每个 `Req` 结构体更大（共享内存占用上升），且 HttpServer 单次唤醒可能一次搬运很多条，端到端「每 token」的流式颗粒度可能变粗。默认 8 是延迟与内存之间的折中。

---

## 5. 综合实践

**任务**：以「一次流式 `/generate` 请求」为线索，把本讲三个模块串成一张完整的数据流图，并标注每一步发生在哪个进程、走 zmq 还是共享内存。

请按下面的检查点逐项对照源码填写（可画图或列表）：

1. **HttpServer encode**：请求进入后，HttpServer 把 prompt tokenize 成 id（u2-l2），分配共享内存 `Req`，把 id 写入 `shm_prompt_ids`。
2. **HttpServer → Router**：HttpServer 用 PUSH（`send_to_router`，`router_port`）发送 `GroupReqIndexes`（仅含索引）。
3. **Router 登记广播**：Router 在 `_add_req` 里既把请求塞进 `req_queue`，又用 PUSH（`send_to_detokenization`，`detokenization_port`）把同一份索引发给 Detokenization 登记（`manager.py:421`）。
4. **ModelBackend 写 token**：Router 调度、ModelBackend 推理，把生成的 token id 直接写进共享内存 `Req.shm_prompt_ids` 的尾部（u2-l4、u2-l5）。
5. **Detokenization 增量解码**：detoken 进程 `gen_token_out` 发现新 token → `decode_token` 算增量文本 → `out_tokens_queue.push`（共享内存）→ PUB 一个 `None`（`http_server_port`）。
6. **HttpServer 搬运**：`handle_loop` 被 SUB 唤醒 → 从 `out_tokens_queue` peek/pop 文本与 metadata → 写入 `out_token_info_list` → `event.set()`。
7. **HttpServer 流式返回**：`_wait_to_token_package` 被 event 唤醒 → 逐条 `yield` → 上层端点拼成 SSE 推给客户端。

**产出要求**：在每个检查点旁标注「进程名 + 通信方式（zmq PUSH/PULL、PUB/SUB 或 共享内存）+ 对应源码行」。完成后，你应当能一眼看出：**为什么这条链路上 zmq 只传索引和空通知，而真正的 token 与文本始终在共享内存里流动**。

> 说明：本综合实践为源码阅读型，无需真正运行服务；若本地具备 GPU 与模型权重，可结合 u1-l2 的 curl 请求实际观察流式输出，对照上图理解每个 chunk 是如何产生的（**待本地验证**）。

## 6. 本讲小结

- Detokenization 是一个独立进程，处在「ModelBackend 之后、HttpServer 之前」，专职把 token id 还原为文本；它通过 PULL 收 Router 的请求登记、通过 PUB 通知 HttpServer。
- 每个请求在 detoken 进程里由一个 `DecodeReq` 追踪，经历「登记 → 增量解码 → 完成回收」三个阶段。
- **增量解码**靠 `prefix_offset / read_offset` 两个游标：decode 一段含安全余量的 token，裁掉前缀即得新增文本；`�` 检查避免在多字节字符中间产出乱码。
- `decode_mode_fix` 是 **PD 分离模式下 decode 节点**的适配步骤——通过提前消费 prompt 末尾的「首个输出 token」，让增量解码游标与正常模式对齐。
- **流式推送**采用「数据走共享内存 `out_tokens_queue`、zmq PUB 只发空通知」的模式，HttpServer 用 SUB 唤醒后批量搬运，既低延迟又零拷贝。
- 特殊 token 的处理分散在三处：`skip_special_tokens`/`spaces_between_special_tokens` 透传给 `tokenizer.decode`、eos 用 `print_eos_token` 抑制、`all_special_ids` 用于打 special 标记传给客户端。

## 7. 下一步学习建议

- **横向打通整条链路**：回头重读 u2-l2（HTTP API 与请求分发）的「结果回流」一节，结合本讲的 PUB/SUB + `out_tokens_queue`，你应该能完整复述一次请求从 HTTP 进入到流式返回的全过程。
- **进入推理内核**：Detokenization 消费的是 ModelBackend 写入共享内存的 token。下一单元（u3 模型推理内核）将打开 ModelBackend 的黑盒，从 `TpPartBaseModel`（u3-l1）和 prefill/decode 主流程（u3-l2）开始，看清这些 token 是怎么被算出来的。
- **进阶延伸**：若你对 PD 分离感兴趣，可先记住本讲的 `decode_mode_fix`，到 u7-l1（PD 分离部署与 KV 迁移）时会看到 prefill 与 decode 节点如何配合、KV 如何迁移；多模态场景下 tokenizer 的分发逻辑（`get_tokenizer` 里的 `model_type` 分支）则会在 u7-l2 展开。
