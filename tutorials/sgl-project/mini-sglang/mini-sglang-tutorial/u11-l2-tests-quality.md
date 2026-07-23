# 测试体系与质量保证

## 1. 本讲目标

Mini-SGLang 是一个高性能推理引擎，读者在前面十多讲里已经读完了从 FastAPI 前端到 CUDA kernel 的全部主链路。但「读得懂」和「改了不崩」是两件事——本讲回答的是：**当我们改动代码后，怎么知道没有改坏？**

学完本讲，你应当能够：

- 说出 `tests/` 目录下三类测试分别覆盖哪个子系统、各自的最小运行条件（是否需要 GPU、是否需要下载模型）。
- 看懂 `tests/core/test_scheduler.py` 如何用 ZMQ 队列直接驱动一个真实的 Scheduler 子进程，跑通端到端推理。
- 读懂 `tests/core/test_cache_allocate.py` 对 `CacheManager` 的完整性断言，并解释 `check_integrity` 检测的两个不变量、它在调度器主循环里被触发的时机。
- 理解 `pyproject.toml` 里的 pytest 配置如何决定测试发现规则与覆盖率统计，以及项目里「pytest 风格」与「脚本风格」两种测试的运行差异。

本讲依赖 [u2-l3（进程间消息与序列化）](u2-l3-message-serialization.md) 与 [u4-l1（Scheduler 主循环）](u4-l1-scheduler-main-loop.md)：前者给出了 `serialize_type`/`deserialize_type` 与 ZMQ 消息的背景，后者给出了主循环里「blocking 等待」的语义——这两点正是理解本讲两个核心测试的钥匙。

## 2. 前置知识

### 2.1 什么是「测试」与「不变量」

测试的本质是：**给代码喂一组输入，断言输出符合预期**。其中最稳的一类断言不是「结果等于某个具体值」，而是「**不变量（invariant）**」——无论经历怎样复杂的操作序列，某个性质永远成立。例如「KV cache 的总页数 = 空闲页数 + 已缓存页数」就是一个不变量：分配、释放、淘汰都不应打破它。一旦打破，说明页簿记逻辑出了 bug（页泄漏或页重复）。

### 2.2 pytest 与「测试发现」

`pytest` 是 Python 最主流的测试框架。它的核心能力是**测试发现（test discovery）**：你只要把文件命名成 `test_*.py`、函数命名成 `test_*`，pytest 就会自动收集并运行它们，无需手写 `main`。

pytest 也可以直接跑「脚本风格的测试」——只要文件里的函数能被调用。本项目同时存在两种风格，这是本讲的一个重要看点。

### 2.3 三类测试与运行代价

| 测试类别 | 文件 | 主要验证 | 运行代价 |
| --- | --- | --- | --- |
| 序列化往返 | `tests/misc/test_serialize.py` | 消息编解码无损 | 低（CPU，但 import 链含 torch） |
| 端到端 | `tests/core/test_scheduler.py` | scheduler+engine 全链路出 token | 高（需 GPU + 下载模型） |
| cache 完整性 | `tests/core/test_cache_allocate.py` | 页分配/淘汰的页对齐与计数不变量 | 低（纯 CPU 张量） |
| kernel 正确性 | `tests/kernel/*.py` | 自定义 CUDA kernel 数值正确 | 高（需多卡 GPU） |

记住这张表，你就知道在「没有 GPU」的环境里，哪些测试仍然可跑、哪些只能「阅读型实践」。`test_cache_allocate.py` 是其中**唯一纯 CPU、不依赖 GPU** 的单元测试，这也是本讲把它作为重点的原因。

## 3. 本讲源码地图

本讲涉及的文件分为「被测代码」与「测试代码」两组：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| `tests/misc/test_serialize.py` | 测试 | 验证 `serialize_type`/`deserialize_type` 往返无损，含嵌套对象与 1D tensor |
| `tests/core/test_scheduler.py` | 测试 | 用 ZMQ 驱动真实 Scheduler 子进程，端到端生成一段文本 |
| `tests/core/test_cache_allocate.py` | 测试 | 验证 `CacheManager` 在「分配→淘汰」后页对齐与计数不变量 |
| `tests/kernel/test_*.py` | 测试 | 验证 `indexing`/`store_cache`/`test_tensor`/PyNCCL 通信 kernel |
| `python/minisgl/scheduler/cache.py` | 被测 | `CacheManager` 与本讲核心方法 `check_integrity` |
| `python/minisgl/scheduler/scheduler.py` | 被测 | `run_when_idle` 中触发 `check_integrity` |
| `python/minisgl/scheduler/io.py` | 被测 | `receive_msg` 在 blocking 分支调用 `run_when_idle` |
| `python/minisgl/utils/misc.py` | 工具 | `call_if_main` 装饰器，决定脚本风格测试如何运行 |
| `pyproject.toml` | 配置 | pytest 发现规则、coverage、dev 依赖 |

永久链接 base：`https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/`

## 4. 核心概念与源码讲解

### 4.1 序列化往返测试

#### 4.1.1 概念说明

[u2-l3](u2-l3-message-serialization.md) 讲过：Mini-SGLang 的所有跨进程消息（`TokenizeMsg`/`UserMsg`/`DetokenizeMsg`/`UserReply`）都靠 `message/utils.py` 的 `serialize_type`/`deserialize_type` 压平成字典，再用 msgpack 编码、ZMQ 传输。这条链路一旦有 bug（比如某个字段丢了、某个 tensor 维度对不上），整个多进程系统就会悄悄出错。

「序列化往返测试（round-trip test）」就是最直接的兜底：**把一个对象编码成字节，再解码回来，验证它和原来一模一样**。如果往返无损，那么传输一定无损。

#### 4.1.2 核心流程

这个测试文件很短，逻辑分两段：

1. **测通用机制**：定义一个自定义 dataclass `A`，故意让它「自引用」（字段 `z: List[A]`）并包含一个 `torch.Tensor`，构造一个嵌套实例，跑 `serialize_type` → `deserialize_type` 往返。
2. **测真实消息**：直接用项目里的 `BatchBackendMsg([UserMsg(...)])` 调它的 `encoder()`/`decoder()`，验证真实消息类的内置编解码也成立。

```
构造 A(嵌套, tensor) ──serialize_type──▶ dict ──deserialize_type──▶ A'  (应与 A 等价)
BatchBackendMsg([UserMsg]) ──encoder()──▶ bytes ──decoder()──▶ BatchBackendMsg' (应等价)
```

#### 4.1.3 源码精读

先看测试用的自定义数据类 `A`，它同时具备「嵌套自引用」和「tensor 字段」两个最容易出问题的特征：

[tests/misc/test_serialize.py:L14-L19](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/misc/test_serialize.py#L14-L19) —— 这里 `z: List[A]` 让序列化器必须递归处理嵌套对象，`w: torch.Tensor` 则逼迫它走「tensor → numpy → bytes」的特殊分支（见 u2-l3）。

再看测试主体：

[tests/misc/test_serialize.py:L22-L35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/misc/test_serialize.py#L22-L35) —— 注意它构造了一个深度为 2 的嵌套对象（`A` 里套了一个 `A`），并复用同一个 tensor `t`；然后第 27-29 行做通用往返，第 32-35 行用真实的 `BatchBackendMsg` 走 `u.encoder(u.encoder())` 式的「内置往返」。

> 关键细节：`deserialize_type` 需要一个「类名 → 类」的映射表。这里第 29 行显式传了 `{"A": A}`，因为 `A` 是测试本地定义的、不在 `message` 包的全局命名空间里（u2-l3 讲过 decoder 靠 `globals()` 查类，被嵌套引用的类必须可见）。而第 32-35 行的 `BatchBackendMsg`/`UserMsg` 不需要传映射，因为它们本就在 `message` 包内、decoder 能自己找到。

#### 4.1.4 代码实践

**实践目标**：亲手验证「新增字段后序列化仍然无损」，体会 u2-l3 说的「新增字段免改代码」。

**操作步骤**：

1. 在 `tests/misc/test_serialize.py` 顶部给 `A` 加一个字段，例如 `m: dict`。
2. 构造 `A` 实例时填上 `m={"a": [1, 2, 3]}`。
3. 直接运行该文件（见 4.4 节关于运行方式的说明），观察 logger 打印出的 `data`（序列化后的字典）和 `y`（反序列化后的对象）。

**需要观察的现象**：序列化后的 `data` 里应出现 `m` 键，其值是纯字典/list/标量组成的结构；反序列化后的 `y.m` 应与原值相等。

**预期结果**：不修改 `serialize_type`/`deserialize_type` 任何一行，新增的 `m` 字段也能正确往返——这正是反射式序列化的好处。

> 说明：本实践未实际运行，请以本地执行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试要专门构造一个「自引用 + tensor」的嵌套对象，而不是只测一个扁平的 `A(10, "hello")`？

**参考答案**：扁平对象只能覆盖「标量字段」这一最简单路径；自引用逼迫序列化器递归（验证嵌套 dict/list 处理），tensor 逼迫它走「降级为 bytes」的特殊分支。一次构造覆盖尽可能多的代码路径，是单元测试的常见手法。

**练习 2**：如果把 `A` 从测试文件挪到另一个未被 import 的模块里，第 29 行的往返会发生什么？

**参考答案**：`deserialize_type` 在 decoder 端找不到类 `A`（因为它依赖 `cls_map` 里能查到类名），会抛出 `KeyError` 或类似错误。这正是 u2-l3 强调的「被嵌套引用的类必须在 decoder 所在模块可见」的隐形契约。

---

### 4.2 Scheduler 端到端测试

#### 4.2.1 概念说明

序列化测试是「单元测试」（测一个函数），而本节是「**端到端测试（end-to-end test）**」：不 mock 任何东西，启动一个真实的 Scheduler 子进程，喂一个真实的 prompt，收真实的生成 token。它验证的是 scheduler + engine + 模型 + 采样这条主链路能协同工作。

这个测试**绕过**了 API Server 和 tokenizer——它不发 HTTP，也不让 tokenizer 进程把文字转 token，而是主进程自己用 `AutoTokenizer` 编码后，直接把 `UserMsg` 通过 ZMQ 推进 Scheduler 的后端队列。这样既减少了参与的进程数，又能精确控制输入。

#### 4.2.2 核心流程

```
主进程                                  Scheduler 子进程 (rank0)
  │                                          │
  │── spawn ──────────────────────────────▶│ Scheduler(config).run_forever()
  │                                          │   (死循环：收消息→调度→前向→回送)
  │   q.put(None) ◀──────「我已就绪」─────────│
  │                                          │
  │── ZmqPushQueue.put(UserMsg(uid=0,...))──▶│ 收到 UserMsg → prefill → decode
  │                                          │
  │◀── ZmqPullQueue.get() ──DetokenizeMsg───│ 每生成一个 token 回送一条
  │   (拼到 ids 末尾)                         │
  │      … 重复直到 msg.finished …            │
  │── ZmqPushQueue.put(ExitMsg())──────────▶│ 收到 ExitMsg，退出循环
```

要点：子进程通过 `mp.Queue` 先回一个 `None` 表示「我已启动完毕」（对应 u1-l2 讲过的 `ack_queue` 就绪握手思想），主进程收到后才发请求；主进程在循环里把每个 `DetokenizeMsg.next_token` 拼到 `ids` 末尾，直到 `finished=True`。

#### 4.2.3 源码精读

子进程入口函数，包在 `@torch.inference_mode()` 下以关闭 autograd、节省显存：

[tests/core/test_scheduler.py:L16-L23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_scheduler.py#L16-L23) —— 第 19 行 `queue.put(None)` 是「就绪信号」，第 21 行进入 `run_forever()` 死循环（u4-l1 讲过）。

测试主函数构造配置并 spawn 子进程：

[tests/core/test_scheduler.py:L26-L40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_scheduler.py#L26-L40) —— 注意几个关键字段：`tp_info=DistributedInfo(0, 1)` 表示单卡（rank0、size=1）；`cuda_graph_bs=[2,4,8]` 预先指定要捕获 CUDA Graph 的 batch size（u5-l3）；`mp.set_start_method("spawn", force=True)` 与生产启动方式一致（u1-l2 讲过 spawn 是 GPU 多进程的正确起步方式）。

主进程装配 ZMQ 收发队列，**直接驱动 scheduler，不经过 tokenizer/api**：

[tests/core/test_scheduler.py:L42-L63](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_scheduler.py#L42-L63) —— 第 42-46 行的 `ZmqPushQueue` 连到 `config.zmq_backend_addr`（scheduler 收消息的地址，u2-l2 讲过），用 `BaseBackendMsg.encoder` 编码；第 48-52 行的 `ZmqPullQueue` 连到 `config.zmq_detokenizer_addr`（scheduler 回送结果的地址），用 `BaseTokenizerMsg.decoder` 解码。第 54-56 行主进程自己用 `AutoTokenizer.encode` 把 prompt 变成 int32 张量——这正是被绕过的 tokenizer 进程原本要做的事。

接收循环，逐 token 拼接直到结束：

[tests/core/test_scheduler.py:L65-L73](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_scheduler.py#L65-L73) —— 每收到一条 `DetokenizeMsg` 就断言类型、把 `next_token` 拼到 `ids`，`finished` 即跳出；最后第 73 行发 `ExitMsg()` 让 scheduler 干净退出。

> 这个测试是「冒烟测试（smoke test）」性质：它不断言生成的具体文字（因为采样结果不固定），只断言**链路能跑通、能正常结束**。能跑通本身就是对 scheduler+engine+采样+回送这条主链路的强验证。

#### 4.2.4 代码实践

**实践目标**：体会「直接用 ZMQ 驱动 scheduler」的测试手法，并观察采样参数对输出的影响。

**操作步骤**：

1. 确认本机有 GPU 且能下载 `meta-llama/Llama-3.1-8B-Instruct`（无此条件则改为阅读型实践）。
2. 直接运行：`python tests/core/test_scheduler.py`（脚本风格，见 4.4）。
3. 把 `SamplingParams(max_tokens=100)` 改成 `max_tokens=20`，再跑一次。

**需要观察的现象**：终端应打印出对 "What's the answer to life, the universe, and everything?" 的一段补全；改小 `max_tokens` 后输出明显变短、更早 `finished`。

**预期结果**：链路跑通，`finished` 正常触发，`ExitMsg` 让进程干净退出（无悬挂子进程）。

> 无 GPU / 无模型下载权限时，本实践转为阅读型：跟踪第 54-73 行，画出「主进程编码 → 推 UserMsg → 收 DetokenizeMsg 循环 → ExitMsg」的消息流，并指出哪些 ZMQ 地址来自 `SchedulerConfig`。

#### 4.2.5 小练习与答案

**练习 1**：为什么这个测试要绕过 tokenizer 进程、主进程自己 encode？

**参考答案**：减少参与进程数能让测试更聚焦于「scheduler+engine」这条链路，定位问题更简单；同时主进程直接控制 `input_ids` 张量，便于断言与调试。生产里 tokenizer 是独立进程（u3-l2），测试里把它「内联」到主进程是一种简化。

**练习 2**：`tp_info=DistributedInfo(0, 1)` 里的 `1` 代表什么？如果改成 `2` 但只 spawn 一个子进程，会发生什么？

**参考答案**：`1` 是 `size`，即张量并行 world_size=1（单卡）。若声明 size=2 却只起一个进程，scheduler 在初始化分布式通信组时会一直等待第二个 rank 加入而卡住（u4-l2 讲过多 rank 靠进程组同步、存在 slow-joiner 问题）。

---

### 4.3 Cache 分配完整性测试与 check_integrity

#### 4.3.1 概念说明

这是本讲最重要的一节。`CacheManager`（u6-l3）负责把请求的逻辑位置翻译成 KV cache 池的物理页下标，它在运行中会反复「分配页 / 释放页 / 从基数树淘汰页」。这些操作的组合很容易引入两类 bug：

- **页泄漏/页重复**：某页既不在空闲池里、也不在缓存里，或同时属于两边——总账对不上。
- **页未对齐**：`page_size > 1` 时，空闲池里的下标必须是 `page_size` 的整数倍（因为页是分配的最小单位），一旦混入非对齐值，后续 `page_table` 写入就会错位。

`check_integrity` 就是用来在运行时抓这两类 bug 的「自检方法」。而 `test_cache_allocate.py` 则是在 CPU 上模拟「分配→淘汰」循环、反复触发这些不变量，确保实现正确。

#### 4.3.2 核心流程

`check_integrity` 检测的两个不变量（设 `page_size = P`、总页数 = `N`）：

1. **页计数守恒**：`空闲页数 + 已缓存页数 == 总页数`，即

   \[
   \mathtt{len(free\_slots)} \;+\; \frac{\mathtt{total\_size}}{P} \;=\; N
   \]

   其中 `total_size = evictable_size + protected_size`（u6-l1/u6-l2 讲过的两桶计数）。不满足则 `raise RuntimeError`。

2. **页对齐**：当 `P > 1` 时，`free_slots` 中每个元素都必须满足 `x % P == 0`，否则 `assert` 失败。

测试侧的核心流程是「**耗尽空闲页 → 往基数树插入可淘汰页 → 再分配（逼迫淘汰）→ 断言返回的页仍对齐、不重叠、计数守恒**」：

```
_allocate(num_pages)            # 耗尽所有空闲页
  └─ free_slots 变空
insert_prefix(...)              # 把若干页插入基数树（变成 evictable）
_allocate(1)                    # 再要 1 页 → 空闲不足 → 触发 evict
  └─ evict 回收基数树叶节点 → 回填 free_slots
断言: 返回页都对齐 / free_slots 都对齐 / 无重叠 / check_integrity() 不抛错
```

#### 4.3.3 源码精读

**先看被测方法 `check_integrity` 本身**：

[python/minisgl/scheduler/cache.py:L81-L91](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L81-L91) —— 第 82 行先委托给前缀缓存自检（`RadixPrefixCache.check_integrity` 当前是 `pass`，见 [radix_cache.py:L187-L188](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kvcache/radix_cache.py#L187-L188)，留作扩展点）；第 83 行把缓存的 token 总数换算成页数；第 84-89 行检查守恒不变量（不满足抛 `RuntimeError`，带详细诊断）；第 90-91 行检查对齐不变量（用 `assert`）。

理解第 83 行的换算：`total_size` 是**token 数**（基数树里所有节点 value 的长度之和），除以 `page_size` 才得到**页数**。这与 `free_slots` 存的是「页起始 token 下标」、`len(free_slots)` 直接是页数，两者单位不同，必须统一到「页」才能相加。

**触发淘汰的 `_allocate`**：

[python/minisgl/scheduler/cache.py:L106-L113](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L106-L113) —— 第 107-110 行：当所需页数超过当前空闲页数时，向基数树 `evict` 申请淘汰（注意 evict 参数是 token 数，所以要乘 `page_size`），回收回来的下标 `[::page_size]` 取页起始再回填 `free_slots`；第 111-112 行从头部切走所需页。这个 `[::page_size]` 切片正是保证回填页对齐的关键，也是 `test_cache_allocate.py` 存在的理由。

**`check_integrity` 在调度器何时被触发**：

[python/minisgl/scheduler/scheduler.py:L78-L81](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L78-L81) —— 它住在 `run_when_idle()` 里。而 `run_when_idle` 在 `SchedulerIOMixin.receive_msg` 的 **blocking 分支**被调用，即单 rank 的 [io.py:L82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L82)、多 rank0 的 [io.py:L91](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L91)、多 rank1 的 [io.py:L112](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L112)。

回看 u4-l1：主循环里 `blocking = not (有批可算 or 有 prefill/decode 可跑)`。也就是说，**只有当 scheduler 当前没有 prefill 可调度、也没有 decode 可跑、且没有积压消息时，才会进入 blocking 接收，于是触发 `run_when_idle` → `check_integrity`**。这是一个精心选择的时机：空闲时系统没有在途计算，自检不会拖慢推理；同时空闲意味着上一轮的所有分配/释放/淘汰都已落定，正好清点账目。

**测试如何用 CPU 模拟这一切**。先看 fixture 与 helper：

[tests/core/test_cache_allocate.py:L14-L20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L14-L20) —— `autouse=True` 的 `reset_global_ctx` fixture 在每个测试前后清空/还原全局上下文 `_GLOBAL_CTX`（u2-l1 讲过这个模块级单例），避免测试间互相污染。

[tests/core/test_cache_allocate.py:L23-L28](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L23-L28) —— `_make_cache_manager` 用 `torch.empty((1,))`（**CPU 张量**）造一个假的 `page_table`，建一个 CPU `Context` 并设为全局，再 `CacheManager(..., type="radix")`。注意它不需要 GPU——`CacheManager.__init__` 里的 `device` 取自 `page_table.device`，于是 `torch.arange(..., device=device)` 也在 CPU 上，基数树本身是纯 Python。

两个自定义断言辅助，把「不变量」具体化：

[tests/core/test_cache_allocate.py:L36-L43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe49998176275667eb58f2/tests/core/test_cache_allocate.py#L36-L43) —— `_assert_all_page_aligned` 检查「每个元素都是 page_size 的倍数」。

[tests/core/test_cache_allocate.py:L46-L55](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L46-L55) —— `_assert_no_overlap` 把每个页起始展开成 `[p, p+page_size)` 的 token 区间集合，检查任意两页的区间不相交（防页重叠）。

最具代表性的测试用例，完整走一遍「耗尽→插入可淘汰→淘汰分配→断言」：

[tests/core/test_cache_allocate.py:L61-L80](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L61-L80) —— 第 68 行 `_allocate(num_pages)` 耗尽 4 页；第 72-75 行往基数树插 2 页可淘汰数据；第 78 行 `_allocate(1)` 因空闲为空而触发淘汰；第 79-80 行断言返回页与剩余 `free_slots` 都对齐。

最后一个用例直接调用被测的 `check_integrity`：

[tests/core/test_cache_allocate.py:L171-L199](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L171-L199) —— 第 190 行 `cm.check_integrity()` 断言「不抛错」（注释 `# This should not raise`），即页计数守恒；随后第 193-199 行再次耗尽并触发淘汰，确认自检在淘汰循环后依然通过。

#### 4.3.4 代码实践

**实践目标**（即本讲指定的实践任务）：针对 `CacheManager.check_integrity` 写一段说明，讲清「它检测什么不变量、在调度器何时被触发」。下面给出参考写法，请你对照源码自行核对并补充。

**它检测什么不变量**

`check_integrity`（cache.py:81-91）检测两个不变量：

1. **页计数守恒**：`len(free_slots) + (total_size // page_size) == num_pages`。含义是「每一页要么在空闲池里、要么在基数树缓存里，既不丢失也不重复」。`total_size` 是基数树里所有节点占用的 token 总数（`evictable_size + protected_size`），除以 `page_size` 换算成页数后才能与 `len(free_slots)`（直接是页数）相加。违反时抛 `RuntimeError`，并打印三者的实际值便于定位。
2. **页对齐**：`page_size > 1` 时，`free_slots` 的每个元素都满足 `x % page_size == 0`。含义是「空闲池只存放完整的页起始下标」，保证下次分配写 `page_table` 时不会把半页错位。违反时 `assert` 失败。

**在调度器何时被触发**

它唯一的运行时调用点在 `Scheduler.run_when_idle()`（scheduler.py:78-81），而 `run_when_idle` 只在 `receive_msg(blocking=True)` 时被调用（io.py:82 / 91 / 112）。结合 u4-l1 的主循环语义，`blocking=True` 发生于「当前没有 prefill 可调度、没有 decode 可跑、也没有积压消息」的空闲时刻。因此触发时机可总结为：**scheduler 进入空闲、准备阻塞等待新请求时，顺手对页账目做一次自检**。选这个时机有两个好处：空闲时无在途计算，自检不抢 GPU；且上一轮的所有分配/释放/淘汰均已落定，账目处于稳定可清点状态。

**操作步骤（可选，加深理解）**：

1. 运行 `pytest tests/core/test_cache_allocate.py -v`（纯 CPU，无需 GPU）。
2. 阅读 cache.py:81-91 与 scheduler.py:78-81，把上面两段说明里的每一条断言对应到具体行号。
3. 进阶：在 `_allocate`（cache.py:106-113）的第 109 行后人为注入一个错误（例如把回填改成 `evicted` 而非 `evicted[::page_size]`，**仅作本地实验、勿提交**），重跑测试，观察哪个断言先失败。

**需要观察的现象**：步骤 1 全部用例通过；步骤 3 应看到对齐断言或 `check_integrity` 抛错。

**预期结果**：正常实现下测试全绿；注入错误后测试在页对齐或计数守恒处失败。

> 步骤 3 属于「破坏性验证」，可帮助你理解每个断言的保护目标；请在本地临时分支实验。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `check_integrity` 里要把 `total_size` 除以 `page_size`，而 `len(free_slots)` 直接用？

**参考答案**：`free_slots` 存的是「页起始下标」，长度就是页数；而 `total_size` 是基数树节点占用的 **token 总数**，单位是 token 而非页。两者单位不同，必须统一到「页」才能相加比较，所以前者直接用、后者要除以 `page_size`。

**练习 2**：假如某次 bug 导致 `_allocate` 回填 `free_slots` 时漏了 `[::page_size]` 切片，`check_integrity` 的两个不变量哪个会先报警？

**参考答案**：对齐不变量（第 90-91 行的 `assert`）会先报警——因为未切片的回填会把非页对齐的下标混进 `free_slots`。计数守恒（第 84 行）可能仍成立（页的总数没变），所以单看计数会漏掉这类 bug，这正是需要两条不变量并存的原因。

**练习 3**：为什么 `run_when_idle` 是触发自检的好时机，而不是每轮主循环都自检？

**参考答案**：每轮主循环都自检会引入额外开销、拖慢吞吐；而空闲时既无在途计算（不抢资源），又处于账目稳定态（上一轮分配/释放/淘汰已落定），是低成本、高信噪比的清点时机。

---

### 4.4 pytest 配置与测试组织

#### 4.4.1 概念说明

光有测试文件还不够，还要告诉 pytest「去哪找测试、怎么算一个测试、要不要顺带统计覆盖率」。这些都在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 里。同时，本项目存在两种测试风格，理解它们的运行差异，才能在改代码后选对验证命令。

#### 4.4.2 核心流程

pytest 的发现规则由配置驱动：

```
testpaths        → 在哪些目录下找测试
python_files     → 哪些文件名算测试文件      (test_*.py / *_test.py)
python_classes   → 哪些类名算测试类          (Test*)
python_functions → 哪些函数名算测试函数      (test_*)
addopts          → 每次运行默认追加的参数    (强制 coverage)
```

而「两种风格」的区别在于：`test_cache_allocate.py` 是标准 pytest 风格（用 `@pytest.fixture`、`class Test*`、`def test_*`，**没有** `__main__` 守卫，直接被 pytest 收集）；其余测试（`test_serialize`/`test_scheduler`/`test_kernel/*`）是脚本风格，靠 `@call_if_main` 装饰器决定「何时运行」。

#### 4.4.3 源码精读

pytest 配置块：

[pyproject.toml:L106-L117](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L106-L117) —— `testpaths=["tests"]` 限定只扫 `tests/`；三个 `python_*` 规则定义发现口径；`addopts` 里的 `--strict-markers`/`--strict-config` 要求所有 marker 与配置必须合法（写错就报错，避免静默忽略）；`--cov=minisgl`/`--cov-report=term-missing`/`--cov-report=html` 让每次 pytest 都顺带统计 `minisgl` 包的覆盖率，终端显示未覆盖行、另存一份 html 报告。

测试与覆盖率工具来自 dev 依赖：

[pyproject.toml:L41-L52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L41-L52) —— `pytest`、`pytest-cov` 在 `[project.optional-dependencies].dev` 里，需 `uv pip install -e ".[dev]"` 才会装上（u1-l2 讲过安装流程）。

理解「脚本风格」的关键——`call_if_main` 装饰器：

[python/minisgl/utils/misc.py:L4-L17](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L4-L17) —— 它**不是**去读模块的 `__name__`，而是比较「传入的 `name` 参数」与字面量 `"__main__"`，因此用法不同效果不同：

- `@call_if_main(__name__)`：把真实模块名传进去。**作为脚本直接运行**时 `__name__=="__main__"`，进入 else 分支、`discard` 默认 `True`，返回 `lambda f: (f() or True) and None`——**立刻执行函数**；被 pytest **import** 时 `__name__` 是 `"tests.kernel.test_index"` 这类、不等于 `"__main__"`，进入 if 分支、`discard` 默认 `False`，返回 `lambda f: f`——**函数原样保留**，仍可被 pytest 收集。即「脚本可跑、pytest 也可收」的双模。`test_scheduler`、`test_indexing`、`test_store_cache` 用这种。
- `@call_if_main()`：不传参，`name` 取默认字面量 `"__main__"`，**永远**进入 else 分支——函数在**装饰（即 import）时立刻执行**。`test_serialize_deserialize`、`test_tensor` 的 `main` 用这种，相当于「一被 import 就跑」。

> 这个区别很关键：它决定了哪些测试「pytest 一收集就会真的跑」。kernel 测试即便被 pytest 收集，也会因需要 CUDA 而失败；所以在无 GPU 环境下，更稳妥的是只跑 `tests/core/test_cache_allocate.py` 这类纯 pytest 风格、且逻辑在 CPU 的测试。

#### 4.4.4 代码实践

**实践目标**：跑通 pytest 并读懂覆盖率报告。

**操作步骤**：

1. 安装 dev 依赖：`uv pip install -e ".[dev]"`（无 GPU 也可装，仅 pytest/cov 工具）。
2. 只跑 CPU 友好的测试：`pytest tests/core/test_cache_allocate.py -v`。
3. 看终端的 `--cov-report=term-missing` 输出，找到 `scheduler/cache.py` 的覆盖率与未覆盖行号。
4. （有 GPU 时）直接跑脚本风格测试：`python tests/core/test_scheduler.py`、`python tests/kernel/test_index.py`。

**需要观察的现象**：步骤 2 全绿；步骤 3 能看到一张「文件 → 语句数 → 缺失行」的表；步骤 4（若有 GPU）打印出推理结果或 kernel 带宽。

**预期结果**：`test_cache_allocate.py` 全部通过；覆盖率报告正常生成 `htmlcov/`。

> 无 GPU 环境下步骤 4 转为阅读型：对照 4.4.3 解释 `@call_if_main(__name__)` 与 `@call_if_main()` 的差异，指出每个 kernel 测试文件用的是哪一种。

#### 4.4.5 小练习与答案

**练习 1**：`--strict-config` 加在 `addopts` 里有什么好处？

**参考答案**：它要求 `pyproject.toml` 里的 pytest 配置必须全部合法——一旦写错键名或值，pytest 直接报错而不是静默忽略。这能在配置出错时尽早暴露，避免「以为自己配了覆盖率、其实没生效」的隐蔽问题。

**练习 2**：`@call_if_main(__name__)` 与 `@call_if_main()` 在「被 pytest 收集」时行为有何不同？

**参考答案**：前者被 import 时函数原样保留（`lambda f: f`），pytest 能正常收集并随后调用它；后者被 import 时函数立刻执行（`(f() or True) and None`），相当于「import 即运行」，pytest 收集到的是装饰后的 `None`。所以需要 GPU 的测试若用后者，import 阶段就会尝试跑 CUDA。

---

## 5. 综合实践

把本讲的「不变量」「端到端」「配置」三点串起来，完成下面这个小任务：

**任务**：为 `CacheManager` 的「空闲→淘汰→分配」路径设计一张**验证 checklist**，并落实其中可 CPU 运行的一项。

1. **列不变量**：阅读 [cache.py:L81-L91](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/cache.py#L81-L91) 的 `check_integrity`，列出它检测的两个不变量（页计数守恒、页对齐），并各举一个「会打破该不变量」的代码错误假设（例如 `_allocate` 漏写 `[::page_size]`）。
2. **定位触发点**：在 [scheduler.py:L78-L81](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/scheduler.py#L78-L81) 与 [io.py:L82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L82) 之间画一条调用链，说明自检只在「scheduler 空闲、blocking 接收」时发生。
3. **写一个新用例（CPU 可跑）**：仿照 [test_cache_allocate.py:L171-L199](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/core/test_cache_allocate.py#L171-L199)，新增一个测试 `test_integrity_holds_after_repeated_evict`：循环若干次「`_allocate` 耗尽 → `insert_prefix` 补可淘汰页 → `_allocate(1)` 触发淘汰」，每次循环后调用 `cm.check_integrity()` 断言不抛错。用 `pytest tests/core/test_cache_allocate.py::TestAllocateEvictPageAlignment::test_integrity_holds_after_repeated_evict -v` 运行（**示例任务，请作为练习自行实现并本地验证**）。
4. **解释选择**：说明为什么这个新用例选「pytest 风格」而非「`@call_if_main` 脚本风格」，并指出它能在无 GPU 环境运行的原因（CPU `page_table` + 纯 Python 基数树）。

> 说明：步骤 3 的测试代码需你自行编写；上面给出的是任务规格与运行命令，未代为运行。

## 6. 本讲小结

- `tests/` 分三类：序列化往返（misc）、端到端与 cache 完整性（core）、kernel 正确性（kernel）；运行代价从「纯 CPU」到「需多卡 GPU」递增，`test_cache_allocate.py` 是唯一不依赖 GPU 的单元测试。
- `test_serialize.py` 用「自引用 + tensor」的嵌套对象，一次覆盖递归序列化与 tensor 降级两条最易出错的路径，并顺带验证真实 `BatchBackendMsg` 的内置编解码。
- `test_scheduler.py` 是端到端冒烟测试：spawn 真实 Scheduler 子进程，主进程用 `ZmqPushQueue`/`ZmqPullQueue` 直接驱动、绕过 tokenizer/api，收 `DetokenizeMsg` 拼 token 直到 `finished`，最后发 `ExitMsg` 退出。
- `check_integrity`（cache.py:81-91）检测两个不变量：**页计数守恒**（`free + cached == total`）与 **页对齐**（`page_size>1` 时 `free_slots % page_size == 0`）；它只在 `run_when_idle` 里被调用，即 scheduler 空闲、blocking 接收新请求时——低成本、账目稳定的清点时机。
- pytest 配置集中在 `[tool.pytest.ini_options]`：`testpaths`/`python_*` 控制发现、`--cov=minisgl` 强制覆盖率、`--strict-*` 防配置静默失效；覆盖率与 pytest 工具在 `dev` 可选依赖里。
- 项目有两种测试风格：标准 pytest 风格（`test_cache_allocate.py`）与脚本风格（靠 `call_if_main`，`@call_if_main(__name__)` 双模、`@call_if_main()` 一被 import 即运行），选对运行命令取决于是否有 GPU。

## 7. 下一步学习建议

- **想深入「被测对象」**：重读 [u6-l3（CacheManager 页分配、回收与淘汰）](u6-l3-cache-manager.md)，把本讲的 `check_integrity` 不变量与 `allocate_paged`/`cache_req`/`lazy_free_region` 的实现一一对应，理解每个操作如何维护这两条不变量。
- **想扩展测试**：参照 `test_cache_allocate.py` 的 fixture/helper 写法，为 `RadixPrefixCache` 的 `match_prefix`/`evict` 写纯 CPU 单元测试（基数树是纯 Python，可脱离 GPU 验证）。
- **想跑通端到端**：在有 GPU 的机器上依次跑 `python tests/core/test_scheduler.py` 与 `python tests/kernel/test_index.py`、`test_store.py`，对照 [u10-l2（自定义 Kernel）](u10-l2-custom-kernels.md) 理解每个 kernel 测试验证的数值正确性。
- **想理解覆盖率盲区**：跑一次 `pytest --cov=minisgl` 后看 `htmlcov/index.html`，找出 `engine/`、`attention/` 下覆盖率低的文件——这些往往是需要 GPU 才能触达的路径，也是端到端测试（而非单元测试）覆盖它们的原因。
