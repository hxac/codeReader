# 异步通信流与 CUDA 事件

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `Buffer` 内部那条通信流（comm stream）是何时创建、带什么参数、何时销毁的，以及 `num_sms` / `num_sms_dedup` / `enable_pdl` / `comm_stream_priority` 四个调度旋钮各自控制什么。
2. 逐行读懂 `async_finish=True` 路径的五步握手：`record_stream` → 主流 `record_event` → 通信流 `wait_event` → `torch.cuda.stream(comm)` 里发射内核 → 通信流 `record_event` 返回事件。
3. 解释 `record_stream` 为什么是必需的：PyTorch 缓存分配器的流感知语义，以及漏掉它之后的隐蔽数据竞争。
4. 解释四个 API（`dispatch` / `prefetch_weight` / `combine` / `reduce_grad`）共享同一条通信流的串行化含义——为什么这不是性能偷懒，而是正确性设计。
5. 理解「`MOONEP_NUM_SMS_DEDUP` 用环境变量而非公开构造参数」这类 API 面取舍背后的工程逻辑。

本讲是第 6 单元（工程实践）的第一讲。u1-l4 已经把四个 API 当黑盒用过（`async_finish` 返回 CUDA event、读取前必须 wait）；本讲打开这个黑盒，看事件和流在宿主侧是如何被编排的。

## 2. 前置知识

### 2.1 CUDA 流与异步执行

- GPU 内核发射（launch）是**异步**的：宿主代码提交内核后立刻返回，内核在 GPU 上排队执行。
- **同一条流内的内核严格按发射顺序执行**；**不同流的内核可以并发**（抢 SM、抢显存带宽）。
- 默认流（current stream，训练框架里通常是主流）上的算子天然串行。想让通信与计算重叠，就需要第二条流。

### 2.2 CUDA 事件：一条单向的 happens-before 边

- `stream.record_event()`：在流 `stream` 的当前位置「拍快照」，事件在此前所有工作完成后视为已触发。
- `stream.wait_event(e)`：让 `stream` 等待事件 `e`，即把 `e` 之前的全部工作排到 `stream` 后续工作之前。
- 两个调用合起来，就在两条流之间建立了一条**单向偏序**：

\[ \text{主流写入} \to \text{input\_ready} \to \text{通信流内核} \to \text{done} \to \text{消费方读取} \]

### 2.3 PyTorch 缓存分配器与 `record_stream`

这是本讲最容易被忽视、也最容易造成偶发错数的一块前置知识：

- `torch.empty` 分配的显存来自**缓存分配器**（caching allocator）。每个显存块记录它的「分配流」。
- 张量的 Python 引用消失时，块**逻辑归还**给分配器；分配器默认认为「只有分配流会用它」，因此可以把块立刻再分给**同一条流**上的新张量。
- 如果你把张量交给**另一条流**上的内核使用，分配器并不知道。此时若原引用消失、块被复用、复用后的写内核与另一条流上还没跑完的读内核并发——就产生了数据竞争。
- `tensor.record_stream(stream)` 就是补上这个信息的调用：它告诉分配器「这条流也用了这块显存」，块要等到 `stream` 越过记录时刻的工作全部完成后才允许复用。

### 2.4 流优先级与 PDL

- CUDA 流优先级是**数值越小优先级越高**（常见有效范围是 `[-1, 0]`，默认 0）。优先级只在两条流竞争 SM 时影响调度先后，不改变任何依赖关系。
- PDL（Programmatic Dependent Launch）在 u4-l1 已详细讲过：前驱内核末尾 `pdl_trigger_dependents` 放行、后继内核开头 `pdl_wait_predecessor` 等待，让同一条流上相邻内核的「启动/收尾阶段」重叠。本讲只关心它在 `Buffer` 层面的总开关 `enable_pdl`。

### 2.5 与前几讲的衔接

- u1-l4：四个 API 的参数与返回值契约（本讲的「用户视角」已建立）。
- u4-l1：`launch_*` 函数的编译缓存与启动几何；PDL 原语。
- u4-l2 / u4-l6：dispatch / combine 内核内部的 `cross_rank_barrier`——本讲会用到「这些内核共享 `meta_buf` 里的屏障区」这一结论。
- u2-l2：VMM 对称内存的分配与映射——`destroy()` 为什么必须先同步后解除映射，根子在这里。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| `moonep/api.py` | **主角**。通信流创建、调度旋钮、四个 API 的 async 分支、`destroy` 的同步顺序 |
| `moonep/dispatch_epilogue.py` | `num_sms_dedup` 的消费点之一（epilogue 的 grid 大小） |
| `moonep/combine_prologue.py` | `num_sms_dedup` 的另一个消费点（prologue 的 grid 大小） |
| `moonep/dispatch.py` 等 8 个内核文件 | 证据：所有 `launch_*` 都取 `torch.cuda.current_stream()` 发射，因此流上下文可以「路由」内核 |
| `tests/test_e2e.py` | 调用方范本：async 事件如何被 wait |
| `tests/test_combine.py` | 第二个调用方范本 |
| `README.md` | 一行总述：四个 API 都支持 `async_finish=True` |

其中「8 个内核文件」指 `dispatch.py`、`dispatch_epilogue.py`、`combine.py`、`combine_prologue.py`、`planning.py`、`prefetch.py`、`grad_reduce.py`、`inter_rank_sync.py`——它们各自的 `launch_*` 宿主入口里都有同一行 `stream = cuda.CUstream(torch.cuda.current_stream().cuda_stream)`（如 [moonep/dispatch.py:L963](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L963)、[moonep/combine.py:L641](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L641)、[moonep/planning.py:L1177](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1177)、[moonep/prefetch.py:L432](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L432)、[moonep/grad_reduce.py:L536](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L536)、[moonep/inter_rank_sync.py:L147](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L147)、[moonep/dispatch_epilogue.py:L408](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L408)、[moonep/combine_prologue.py:L592](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L592)）。这行代码是整讲的关键前提：**内核永远发射在「当前流」上，所以 `with torch.cuda.stream(comm):` 这个上下文管理器就是宿主侧控制内核去向的唯一开关**。

## 4. 核心概念与源码讲解

本讲的两个最小模块：

- **4.1 comm stream 管理**：流的创建参数、三个调度旋钮（含环境变量）、销毁顺序。
- **4.2 async_finish 路径**：五步握手、`record_stream` 清单、共享流的串行化语义。

### 4.1 comm stream 管理

#### 4.1.1 概念说明

MoonEP 的通信内核（dispatch、combine、prefetch、grad_reduce，以及它们的辅助内核）都是长驻显存的 CUDA 内核。如果它们跑在主流上，就会和框架的计算内核（GEMM、attention 等）串行排队——通信期间 GPU 的计算单元大量闲置。让通信跑在一条独立的高优先级流上，通信与计算才能重叠：

\[ T_{\text{step}} \approx \max(T_{\text{comm}},\ T_{\text{overlap-comp}}) + T_{\text{serial}} \quad\text{而不是}\quad T_{\text{comm}} + T_{\text{comp}} \]

但「多开一条流」不是免费的：需要解决两个问题——

1. **内核如何上到这条流？** MoonEP 的做法非常克制：所有 `launch_*` 都取当前流，`Buffer` 在 async 分支里用流上下文切换「当前流」。这意味着同步模式（`async_finish=False`）下一切照旧跑在调用者的当前流上，零额外机制。
2. **这条流占多少 SM？** 通信内核是持久化（persistent）内核，grid 大小即 SM 占用。`num_sms`（默认 32）控制 dispatch/combine 等主力内核的占用；`num_sms_dedup`（默认取满整个设备）控制 dispatch_epilogue / combine_prologue 这两个「本地去重修补」内核的占用，并可通过 `MOONEP_NUM_SMS_DEDUP` 环境变量覆盖。

另外两个旋钮：`comm_stream_priority`（默认 -1，即比默认主流更高优先级）和 `enable_pdl`（默认 True，控制内核间是否用 PDL 衔接；False 则退化为普通同流串行发射，便于调试）。

#### 4.1.2 核心流程

`Buffer` 构造期的流与旋钮解析流程：

```text
Buffer.__init__(comm_stream_priority=-1, enable_pdl=True, ...)
  ├─ 校验 comm_stream_priority 是 int、enable_pdl 是 bool
  ├─ _create_context(...)
  │    ├─ num_sms 未指定 → 默认 32（主力通信内核的 grid）
  │    ├─ max_sms = 设备 SM 总数
  │    └─ num_sms_dedup = _num_sms_dedup_from_env(max_sms)
  │         ├─ 环境变量未设/为空 → max_sms（占满整卡）
  │         ├─ 非整数 或 超出 [1, max_sms] → ValueError
  │         └─ 否则 → 使用环境变量值
  │    └─ ctx['num_sms'] / ctx['num_sms_dedup'] 存入上下文
  └─ self._comm_stream = torch.cuda.Stream(device, priority=comm_stream_priority)
```

消费侧：

```text
ctx['num_sms']        → dispatch / combine / planning / prefetch / grad_reduce 的 grid
ctx['num_sms_dedup']  → dispatch_epilogue / combine_prologue 的 grid
self.enable_pdl       → launch_dispatch(pdl_trigger=…) / launch_dispatch_epilogue(pdl_launch=…)
                        / launch_combine_prologue(pdl_trigger=…) / launch_combine(pdl_launch=…)
self._comm_stream     → 仅在四个 API 的 async_finish 分支和 destroy() 中使用
```

`destroy()` 的销毁顺序（先同步、后解映射）：

```text
destroy()
  ├─ comm_stream.synchronize()      # 通信流上所有在途内核完成
  ├─ torch.cuda.synchronize()       # 全设备兜底
  ├─ dist.barrier(group)            # 全组对齐：别人不再远程读写我的显存
  └─ 逐个 drop 掉 VMM/组播张量引用 → 解除映射、释放句柄
```

#### 4.1.3 源码精读

**（1）通信流的创建。** 构造函数先建好上下文（`_create_context`，其中清零屏障区等 CUDA 操作发生在主流上），然后才创建通信流：

- [moonep/api.py:L448-L508](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L448-L508)：`Buffer.__init__` 全文。关键参数 `comm_stream_priority: int = -1` 与 `enable_pdl: bool = True` 在签名上就有默认值，末尾创建通信流：

```python
self._comm_stream = torch.cuda.Stream(
    device=int(self._ctx['device']),
    priority=self.comm_stream_priority,
)
```

- [moonep/api.py:L479-L484](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L479-L484)：docstring 对两个参数的说明——`-1` 给通信流高于主流的优先级；`enable_pdl=False` 回退到普通同流串行发射。类 docstring（[moonep/api.py:L440-L446](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L440-L446)）明确写了「the async communication stream used by dispatch/prefetch/combine/reduce」——**一条流，四个 API 共享**。

**（2）`MOONEP_NUM_SMS_DEDUP` 环境变量。**

- [moonep/api.py:L82-L102](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L82-L102)：`_num_sms_dedup_from_env` 全文。未设置或空串返回 `max_sms`；非整数、越界都会抛出带上下文的 `ValueError`。docstring 直接写明了设计动机：

```python
``MOONEP_NUM_SMS_DEDUP`` is intentionally an environment override rather
than a Buffer argument so benchmark jobs can sweep it without touching the
public API surface.
```

- [moonep/api.py:L266-L267](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L266-L267)：`_create_context` 里取设备 SM 总数并解析覆盖值：

```python
max_sms = torch.cuda.get_device_properties(device).multi_processor_count
num_sms_dedup = _num_sms_dedup_from_env(max_sms)
```

- 结果存进 `ctx`（[moonep/api.py:L403](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L403)），并被日志函数打印出来（[moonep/api.py:L148-L149](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L148-L149)，rank 0 上输出 `num_sms=..., num_sms_dedup=...`）。
- 消费点一：[moonep/dispatch_epilogue.py:L383](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L383) `num_sms = int(ctx['num_sms_dedup'])`——epilogue 的持久化 CTA 数（u4-l4 讲过组批次 round-robin 到这个 grid）。
- 消费点二：[moonep/combine_prologue.py:L562](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L562) 同样的取值——prologue 的 grid。

注意默认值的反差：主力通信内核默认只用 **32** 个 SM，而两个去重修补内核默认**占满整卡**。可以读出的设计意图（笔者的解读，非源码注释）：dispatch/combine 是 NVLink 带宽敏感的持久内核，32 个 SM 足以打满链路，其余 SM 留给主流计算；epilogue/prologue 是纯本地显存搬运的短促突发，跑得越快越好，默认不设限，需要给主流让路时再用环境变量压小。

**（3）内核挂当前流的证据。** 以 dispatch 为例，[moonep/dispatch.py:L963](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L963)：

```python
stream = cuda.CUstream(torch.cuda.current_stream().cuda_stream)
```

八个内核文件都是这一行。所以 `_run_dispatch_on_current_stream` 这类方法名里的 current stream 才是字面意义：**谁调用它时的当前流，内核就上谁的流**。同步模式是调用者的主流，async 模式是 `with torch.cuda.stream(comm)` 里的通信流（见 4.2.3）。

**（4）销毁顺序。** [moonep/api.py:L551-L556](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L551-L556)：

```python
if self._comm_stream is not None:
    self._comm_stream.synchronize()
torch.cuda.synchronize()
group = ctx.get('group')
if dist.is_initialized():
    dist.barrier(group=group)
```

通信流先于全设备同步被单独 synchronize——因为 async 发射的通信内核可能还排在通信流上；随后 `dist.barrier` 保证全组 rank 都到齐，才开始 drop VMM/组播张量引用（[moonep/api.py:L560-L579](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L560-L579)），最后 `self._comm_stream = None`。如果跳过这一步直接解映射，还在飞行的内核会访问已解除映射的虚拟地址——u2-l2 讲过的 VMM 映射是有生命周期的。

#### 4.1.4 代码实践

**实践目标**：把三个调度旋钮（`num_sms`、`num_sms_dedup`、`enable_pdl` + `comm_stream_priority`）的「定义点 → 存储字段 → 消费点」整理成一张数据流表，验证你对本模块的理解。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 打开 [moonep/api.py:L448-L508](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L448-L508)，找出每个旋钮的默认值。
2. 在仓库里 `grep -n "num_sms_dedup" moonep/` 与 `grep -n "enable_pdl\|pdl_launch\|pdl_trigger" moonep/api.py`，补全消费点。
3. 写出这张表（文本文件或纸上即可）。

**预期结果**（可对照）：

| 旋钮 | 默认值 | 定义/解析点 | 消费点 |
| --- | --- | --- | --- |
| `num_sms` | 32 | `Buffer.__init__` → `_create_context`（api.py:L253-255） | dispatch/combine/planning/prefetch/grad_reduce 的 grid |
| `num_sms_dedup` | 设备全部 SM 数 | `_num_sms_dedup_from_env`（api.py:L82-102），`MOONEP_NUM_SMS_DEDUP` 可覆盖 | dispatch_epilogue.py:L383、combine_prologue.py:L562 |
| `enable_pdl` | True | `Buffer.__init__` | api.py:L649/L653/L689/L695 四个 launch 调用的 pdl 参数 |
| `comm_stream_priority` | -1 | `Buffer.__init__` | api.py:L505-508 建流；四个 async 分支使用该流 |

**进阶（需多卡 + torchrun，待本地验证）**：分别以 `MOONEP_NUM_SMS_DEDUP=1`、`MOONEP_NUM_SMS_DEDUP=0`、`MOONEP_NUM_SMS_DEDUP=abc` 启动任意一个测试（如 `tests/test_planning.py`），观察前一种正常通过（epilogue/prologue 用 1 个 CTA）、后两种在 Buffer 构造期抛出 `ValueError`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MOONEP_NUM_SMS_DEDUP` 设计成环境变量，而 `comm_stream_priority`、`enable_pdl` 是构造函数参数？

**答案**：源码 docstring（api.py:L85-88）给出的直接理由是让基准测试任务能在不改任何调用方代码的情况下扫参。更深层的区别在于性质：`comm_stream_priority` / `enable_pdl` 改变的是调度与重叠**语义**，生产部署可能真要改（比如某些场景关 PDL 排查问题），属于 API 契约；`num_sms_dedup` 只影响去重修补内核的 SM 占用这一**性能维度**，正确性与它无关，没必要挤占公开 API 面。代价是可见性差（不读源码不知道有这个旋钮）且是进程级全局而非每 Buffer 独立。

**练习 2**：`destroy()` 里为什么必须先 `comm_stream.synchronize()`、再 `torch.cuda.synchronize()`、再 `dist.barrier()`，三者缺一各会出什么问题？

**答案**：缺第一步——async 发射的通信内核可能还在通信流上排队，后续解除 VMM 映射后内核访问已失效地址；缺第二步——主流或其他流上若有用户残留的引用通信缓冲的操作，同样可能在解映射后执行；缺第三步——本 rank 可能耗尽了本 rank 显存里的共享 chunk 上的所有工作，但**别的 rank** 的内核可能还在远程读写本 rank 的 chunk，提前释放等于拔掉别人脚下的地板。三步合起来把「本 rank 通信流 → 本 rank 全部流 → 全组所有 rank」三层在途工作全部排空。

**练习 3**：`num_sms_dedup` 的取值范围为什么上界是 `max_sms`（设备 SM 总数）而不是更大？

**答案**：`num_sms_dedup` 是持久化内核的 grid 大小（CTA 数）。grid 超过设备 SM 数没有意义——多出来的 CTA 只会排队分批执行，反而让「n_groups 设备端读取 + while 循环」的持久化设计失去意义（u4-l4/u4-l5）；代码用 `[1, max_sms]` 的检查（api.py:L98-101）直接拒绝了无意义的配置。

### 4.2 async_finish 路径

#### 4.2.1 概念说明

`async_finish=True` 让一次 API 调用改道通信流执行，并返回一个 CUDA 事件代替「同步等待」。要解决的问题：调用方（训练框架）的主流在通信期间还有别的事可做（上层的 layer norm、下层的 GEMM、别的 microbatch……），通信不该阻塞宿主。

但换流执行引入两个新义务，MoonEP 用一套固定的**五步握手**解决：

1. **生命周期声明**（`record_stream`）：输入输出张量都在主流上分配（`torch.empty_like` / `torch.empty` 发生在进入通信流上下文之前），却被通信流使用。必须逐一告诉缓存分配器，否则张量引用消失后显存块可能被主流复用，与在途通信内核竞争（见 2.3 节）。
2. **两条 happens-before 边**（事件握手）：
   - 入向：主流 `record_event` → 通信流 `wait_event`，保证通信内核读到的是主流上**已完成的**输入（比如路由权重的 producer 内核）；
   - 出向：通信流 `record_event` 得到 `done`，返回给调用方；调用方在自己要读输出的流上 `done.wait(...)` 之后才能读。

注意 `done` 事件**不是**「顺手附赠」，而是异步路径的正确性契约的一部分——u1-l4 已经强调「读取前必须 wait」，[tests/test_e2e.py:L256-L257](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L256-L257) 的注释写得很直白：`# Caller must explicitly wait before reading.`

#### 4.2.2 核心流程

四个 API 的 async 分支结构**完全同构**，五步握手如下：

```text
─────────────── 同步分支（async_finish=False）───────────────
当前流（主流）上依次发射内核，直接返回结果，无事件。

─────────────── 异步分支（async_finish=True）───────────────
① self._record_streams(全部涉及的张量, comm)     # 生命周期声明
② input_ready = main_stream.record_event()       # 入向快照
③ comm.wait_event(input_ready)                   # 入向等待
④ with torch.cuda.stream(comm):
       _run_*_on_current_stream(...)             # 内核取 current_stream == comm
       done = comm.record_event()                # 出向快照
⑤ 返回值多带一个 done                             # 调用方 wait 后再读
```

时间线视角（一次 `dispatch(async_finish=True)`）：

```text
主流:   [生产 hidden_sh/weights 的内核] ─record(input_ready)─► [其他计算 ...] ─wait(done)─► [读 hidden_nvsh]
                 │                                                        ▲
                 ▼ wait_event                                             │
通信流:          └─► [inter_rank_sync → planning → dispatch → epilogue → copy_out] ─record(done)
```

四个 API 各自的 `record_stream` 清单（正确性清单，值得逐项理解）：

| API | 记录的张量 | 备注 |
| --- | --- | --- |
| `dispatch` | `hidden_sh`、`route_weights_sk`、`hidden_nvsh`、`route_weights_nvs`、plan 的 7 个运行时张量；fresh 规划时再加 `topk_flat`、`tokens_per_expert`、`cu_seqlens` | 输入、输出、plan、规划临时量四类都覆盖 |
| `prefetch_weight` | `plan.experts_to_copy`、三个权重张量、（可选）三个 scale 张量 | 只读输入 + 计划 |
| `combine` | `hidden_sh`（输出）、plan 的 7 个张量、`hidden_nvsh`、`route_weights_nvs`、`route_weights_sk` | 输出在主流分配、通信流写入，必须记录 |
| `reduce_grad` | `plan.experts_to_copy`、六个梯度/归约缓冲张量 | 全是输入侧 |

其中「plan 的 7 个运行时张量」由 `_plan_runtime_tensors` 统一给出（见 4.2.3）。

**共享同一条流的串行化含义**：四个 API 的 async 内核全部排进同一条 `self._comm_stream`，CUDA 保证同流按发射序执行。于是：

- `dispatch(async)` 之后紧接 `prefetch_weight(async)`，**不需要**等 dispatch 的事件——流序已经保证了 dispatch 的内核先跑完；且后返回的事件覆盖此前排入的全部工作；
- 更重要的是正确性：这些内核共享 `meta_buf` 的屏障区（`grid_sync_bar`、cross-rank barrier 槽）、规划暂存区、`hidden_buf` 本体。如果两路通信并发跑，会在这共享状态上竞争。一条流 = 用流序免费拿到互斥。

#### 4.2.3 源码精读

**（1）两个小助手。**

- [moonep/api.py:L599-L603](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L599-L603)：`_record_streams`——对每个非 None 张量调用 `record_stream`：

```python
@staticmethod
def _record_streams(tensors, stream: torch.cuda.Stream) -> None:
    for t in tensors:
        if t is not None:
            t.record_stream(stream)
```

- [moonep/api.py:L605-L615](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L605-L615)：`_plan_runtime_tensors`——plan 里会被内核实际读写的 7 个张量（`dst`、`experts_to_copy`、`zero_fill_ranges`、`remote_stats`、`dup_groups`、`dup_loffs`、`dup_counts`）。plan 是 frozen dataclass，但它的张量内容会在通信流上被内核读写，所以同样要记录。

**（2）dispatch 的 async 分支（主范例）。** [moonep/api.py:L825-L858](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L825-L858)：

```python
main_stream = torch.cuda.current_stream()
comm = self._comm_stream
assert comm is not None, "MoonEP Buffer communication stream is not initialized"

tensors_to_record = [
    hidden_sh,
    route_weights_sk,
    hidden_nvsh,
    route_weights_nvs,
    *self._plan_runtime_tensors(plan),
]
if planning_args is not None:
    tensors_to_record.extend(planning_args)
self._record_streams(tensors_to_record, comm)

input_ready = main_stream.record_event()
comm.wait_event(input_ready)

with torch.cuda.stream(comm):
    self._run_dispatch_on_current_stream(
        ctx, hidden_sh, route_weights_sk, planning_args, plan,
        hidden_nvsh, route_weights_nvs,
        inter_rank_sync=inter_rank_sync,
        zero_copy=zero_copy,
        route_weights_zero_copy=router_weights_zero_copy,
    )
    done = comm.record_event()

return hidden_nvsh, route_weights_nvs, cu_seqlens, plan, done
```

逐点说明：

- 输出张量 `hidden_nvsh` / `route_weights_nvs` 在**进入 async 分支之前**、也就是主流上分配（[moonep/api.py:L797-L808](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L797-L808)），分配流是主流——这正是它们必须被 `record_stream` 的原因。
- `_run_dispatch_on_current_stream`（[moonep/api.py:L617-L661](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L617-L661)）内部依次发射 `launch_inter_rank_sync` → `launch_planning`（fresh 时）→ `launch_dispatch` → `launch_dispatch_epilogue` → 边界 `copy_`。由于 4.1.3 第（3）点的机制，这些全部落在通信流上；**连 `hidden_nvsh.copy_(...)` 这类边界拷贝也在通信流上执行**（[moonep/api.py:L655-L661](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L655-L661)），因为它发生在流上下文内部。
- 同步分支（[moonep/api.py:L810-L823](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L810-L823)）没有任何流操作——同样的 `_run_dispatch_on_current_stream` 直接跑在调用者当前流上，返回值不带事件。**同一份内核编排代码服务两种模式**，这是「内核挂当前流」设计的直接收益。

**（3）另外三个 API 的同构分支。**

- `prefetch_weight`：[moonep/api.py:L928-L948](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L928-L948)。记录 `(plan.experts_to_copy, *weight_prefetch_args, *(scale_prefetch_args or ()))`，然后同样的握手。其 docstring（[moonep/api.py:L892-L895](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L892-L895)）写明了共享流的顺序收益：

> Calling this after ``dispatch(async_finish=True)`` preserves stream ordering, and the returned event also covers the queued dispatch.

- `combine`：[moonep/api.py:L1052-L1083](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1052-L1083)。注意它的记录清单里包含**输出** `hidden_sh`（在 [moonep/api.py:L1022-L1036](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1022-L1036) 于主流分配）以及输出 `route_weights_sk`——通信流的 combine 内核会写它们。
- `reduce_grad`：[moonep/api.py:L1143-L1159](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1143-L1159)，记录 `(plan.experts_to_copy, *grad_reduce_args)`。它的 docstring 有一句本讲最凝练的正确性注释（[moonep/api.py:L1119-L1120](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1119-L1120)）：

```python
Kept separate from ``combine``; running on the shared comm stream
keeps the Buffer's barrier/meta resources serialized.
```

**（4）调用方怎么消费事件。** [tests/test_e2e.py:L250-L257](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L250-L257) 是官方范本：

```python
(h_a, w_a, cu_a, plan_a, _dispatch_event) = buffer.dispatch(
    hidden2, weights2, topk2, tpe2, async_finish=True,
)
prefetch_event = buffer.prefetch_weight(
    plan=plan_a, async_finish=True, **async_prefetch_args,
)
# Caller must explicitly wait before reading.
prefetch_event.wait(torch.cuda.current_stream())
```

注意只 wait 了 `prefetch_event` 而没 wait `_dispatch_event`——因为两者同流，后者已被前者传递性地覆盖。同样的模式见 [tests/test_combine.py:L322-L328](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_combine.py#L322-L328) 和 [tests/test_e2e.py:L396-L406](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L396-L406)（combine 事件 + reduce_grad 事件的链式 wait）。[README.md:L81](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L81) 一句话概括：四个 API 都接受 `async_finish=True`，在通信流上运行并返回 CUDA 事件。

#### 4.2.4 代码实践

**实践目标**：用一个**单卡、不依赖 MoonEP** 的脚本复刻 api.py 的五步握手，然后故意漏掉 `record_stream`，亲眼看到（或至少理解为什么经常看不到）生命周期竞争。

**操作步骤**：

1. 保存以下脚本为 `dual_stream_demo.py`（**示例代码**，非项目源码）：

```python
# dual_stream_demo.py —— 单卡即可运行，不需要安装 MoonEP
# 复刻 moonep/api.py async 分支的五步握手（api.py L825-L858 的骨架）
import torch

N_ELEMS = 16 * 1024 * 1024  # 64 MiB fp32，尺寸够大便于分配器命中同一块


def async_comm_copy(src: torch.Tensor, comm: torch.cuda.Stream, record: bool):
    """模仿 Buffer.dispatch(async_finish=True)：主流产生输入，通信流执行拷贝。"""
    main = torch.cuda.current_stream()
    dst = torch.empty_like(src)          # 输出在主流上分配（模仿 api.py 的 torch.empty_like）
    if record:                           # ① 生命周期声明（模仿 _record_streams）
        src.record_stream(comm)
        dst.record_stream(comm)
    input_ready = main.record_event()    # ② 主流拍快照
    comm.wait_event(input_ready)         # ③ 通信流等待
    with torch.cuda.stream(comm):
        dst.copy_(src)                   # ④ 拷贝内核挂在通信流
        done = comm.record_event()       # ⑤ 完成快照
    return dst, done


def scenario(record: bool, iters: int) -> int:
    """src 在通信拷贝飞行途中被删；主流随后分配同尺寸张量触发块复用。
    返回 dst 读到脏数据的轮数。"""
    comm = torch.cuda.Stream(priority=-1)
    bad = 0
    for _ in range(iters):
        src = torch.full((N_ELEMS,), 1.0, dtype=torch.float32, device="cuda")
        dst, done = async_comm_copy(src, comm, record)
        del src                          # 引用消失：块归还缓存分配器
        # 主流不等 done，立刻分配同尺寸张量 —— 分配器大概率复用刚归还的块，
        # 并用主流写内核覆盖它，与通信流上还没跑完的 copy_ 竞争
        junk = torch.full((N_ELEMS,), 2.0, dtype=torch.float32, device="cuda")
        junk.mul_(3.0)
        done.wait(torch.cuda.current_stream())   # 调用方显式 wait（test_e2e.py 同款）
        torch.cuda.synchronize()
        bad += int(not torch.all(dst == 1.0).item())
        del dst, junk
    return bad


if __name__ == "__main__":
    iters = 50
    torch.manual_seed(0)
    for record in (True, False):
        bad = scenario(record, iters)
        print(f"record_stream={record}: {bad}/{iters} 轮输出被污染")
```

2. 运行 `python dual_stream_demo.py`。

**需要观察的现象**：

- `record_stream=True`：恒为 `0/50`。
- `record_stream=False`：**可能**出现若干轮 `dst != 1.0`（混入 `6.0`），也可能一次都不出现——这取决于当时 GPU 上两条流的调度，是一个真竞态。

**预期结果**：加上 `record_stream` 后逐轮全对；漏掉时输出是否被污染是概率性的。**这个「概率性」本身就是本实践最重要的一课**：生命周期 bug 在稳定性测试里往往抓不住，只在真实训练的负载压力下偶发错数。如果 50 轮没命中，可把 `N_ELEMS` 与 `iters` 调大、或在 `del src` 与分配 `junk` 之间插入一个主流重计算内核拉开窗口，再试（待本地验证）。

3. （可选）把 `async_comm_copy` 里的 `done.wait(...)` 移到 `torch.all(dst == 1.0)` 之后重新运行，观察「不 wait 就读」造成的错误更容易复现——这对应 u1-l4 强调的契约：async 返回值读取前必须 wait。

#### 4.2.5 小练习与答案

**练习 1**：`dispatch(async_finish=True)` 里，如果漏掉对 `hidden_sh`（调用方的输入张量）的 `record_stream`，什么条件下会出错？

**答案**：三个条件同时满足：(a) 调用方在 dispatch 返回后不再持有 `hidden_sh` 的引用（比如它是上一个算子的临时输出，Python 引用计数归零）；(b) 缓存分配器把这块显存复用给主流上的新张量（尺寸越匹配越容易命中）；(c) 复用后的主流写内核与通信流上还在读这块内存的 dispatch 内核并发。三者都是概率事件，所以症状是「偶发错数」，且通信内核越重（S×K×H 越大）窗口越宽。

**练习 2**：为什么 `combine` 的记录清单里要包含输出 `hidden_sh`，而 `prefetch_weight` 没有任何输出需要记录？

**答案**：`combine` 在主流上 `torch.empty` 出 `hidden_sh`（api.py:L1022-L1027），再由通信流上的 combine 内核写入——「主流分配、通信流使用」正是需要 `record_stream` 的组合。`prefetch_weight` 没有返回张量：它写入的是用户传入的权重张量的 `[E, E+B)` 行和 ctx 持有的缓冲，前者已在记录清单里，后者由 Buffer 长期持有、不会提前释放，无需额外记录。

**练习 3**：假设把四个 API 改成各自创建一条独立通信流以图并发，最可能先坏什么？

**答案**：最先坏的是共享状态：所有内核共用 `meta_buf` 的屏障区（`grid_sync_bar`、cross-rank barrier 槽位）与规划暂存区，还有 `hidden_buf` 本体（combine 的输入就是 dispatch 的输出区）。两条流上的 dispatch 与 combine 并发时，一个内核翻转 barrier 相位的同时另一个内核在等待该相位，自复位屏障的轮次假设被打破，轻则死锁/超时（u4-l1 讲过的看门狗会触发），重则读到半新半旧的 plan。api.py:L1119-L1120 的注释正是为此把 `reduce_grad` 与 `combine` 钉在同一条流上。

## 5. 综合实践

**任务**：把本讲两个模块串成一个「迷你 MoonEP 编排器」，用一个脚本同时验证 (a) 五步握手的正确性、(b) 同流串行化让后一个事件覆盖前一个、(c) 不 wait 就读会读错。

在 4.2.4 的脚本基础上扩展（**示例代码**，单卡可运行）：

```python
# mini_pipeline.py —— 用两条流 + 事件复刻 MoonEP 四 API 的编排约束
import torch

N = 8 * 1024 * 1024
comm = torch.cuda.Stream(priority=-1)   # 模仿 Buffer 的 self._comm_stream
main = torch.cuda.current_stream()

def dispatch_like(x):                   # 模仿 dispatch(async_finish=True)
    dst = torch.empty_like(x)
    x.record_stream(comm); dst.record_stream(comm)
    comm.wait_event(main.record_event())
    with torch.cuda.stream(comm):
        dst.copy_(x)
        return dst, comm.record_event()

def prefetch_like(x):                   # 模仿 prefetch_weight(async_finish=True)：同流，无需等前一个事件
    with torch.cuda.stream(comm):
        x.mul_(2.0)
        return comm.record_event()

x = torch.full((N,), 1.0, device="cuda")     # 主流产生输入
mid, _e1 = dispatch_like(x)                  # e1 直接丢弃：同流保证被 e2 覆盖
del x
e2 = prefetch_like(mid)

# 反例：先读后 wait —— 此刻 comm 上的工作多半还没完成
early = mid.clone()
e2.wait(main)                                 # 正例：先 wait 后读
torch.cuda.synchronize()
late = mid.clone()
print("early == 2.0 的比例:", (early == 2.0).float().mean().item())
print("late  == 2.0 的比例:", (late == 2.0).float().mean().item())
```

**检查点**：

1. `late` 应 100% 等于 2.0（握手正确）。
2. `early` 通常混有 1.0（没 wait 就读）；若你的机器上 `early` 恰好全对，说明拷贝太快已经跑完——把 `N` 调大或让主流先排队一堆计算再试。这正是「事件是契约而非装饰」的直观体验。
3. 回答一个问题：如果 `prefetch_like` 里需要读 `mid`，为什么它不需要 `mid.wait_event(_e1)`？（答案：它与 dispatch_like 的内核在同一条 `comm` 流上，流序已经保证 copy_ 先于 mul_ 执行——对应 api.py:L892-L895 的语义。）

**（可选，需多卡 + NVLink，待本地验证）**：在真实环境运行 `torchrun --nproc_per_node=8 -m pytest tests/test_e2e.py`（该文件只有一个 `test_e2e` 用例，其中 L250-L257 与 L396-L406 两段正是链式 wait），确认你在这个迷你脚本里理解的握手与官方测试的用法一致。

## 6. 本讲小结

- `Buffer` 在构造末尾创建**唯一一条**通信流（默认 `priority=-1`，高于主流），四个 API 的 async 分支共享它；同步分支完全不动流，内核编排代码两用。
- 所有 `launch_*` 都取 `torch.cuda.current_stream()` 发射，因此 `with torch.cuda.stream(comm)` 是把整套内核路由到通信流的唯一开关。
- 五步握手 `record_stream → 主流 record_event → comm wait_event → 流上下文内发射内核 + record_event → 返回 done`，在 dispatch / prefetch_weight / combine / reduce_grad 四处逐字同构；调用方必须 `done.wait(当前流)` 后才能读输出。
- `record_stream` 解决「主流分配、通信流使用」的缓存分配器生命周期问题；漏掉它不会立刻报错，而是埋下偶发错数的竞态。
- 共享一条流换来两件事：同流发射序即执行序（后一个事件传递性覆盖前一个），以及共享 `meta_buf` 屏障/暂存资源的天然互斥——`reduce_grad` 的 docstring 把这点写成了设计说明。
- 调度旋钮分两类：语义类（`comm_stream_priority`、`enable_pdl`）进构造参数；纯调优类（`MOONEP_NUM_SMS_DEDUP`，默认取满整卡、控制 epilogue/prologue 的 grid）走环境变量，让基准测试免改 API 扫参。

## 7. 下一步学习建议

本讲解决了「通信如何与计算重叠」的宿主侧编排；下一讲 **u6-l2 零拷贝模式：视图别名与安全约束**沿同一条主线推进：当 `dispatch(zero_copy=True)` 直接返回通信缓冲视图、省掉边界 `copy_` 时，输出张量的生命周期问题从「分配器层面」升级为「视图别名层面」——视图会被本 Buffer 的下一次通信覆盖，autograd 保存它即出错。建议带着本讲的两个实验脚本去读 u6-l2 的 `data_ptr()` 断言与 `router_weights_zero_copy` 分层默认值，体会「异步」与「零拷贝」这两组风险模型如何叠加。之后再进入 u6-l3 的训练四象限全链路，把本讲的握手模式放回完整的 fwd/bwd 语境。
