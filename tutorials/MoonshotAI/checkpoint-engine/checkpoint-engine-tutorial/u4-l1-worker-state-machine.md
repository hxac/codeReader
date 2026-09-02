# worker 侧状态机:update_weights_from_ipc

## 1. 本讲目标

本讲我们把视角从 ParameterServer(PS)切换到它的对端——运行在推理引擎进程里的 worker。读完后你应该能:

1. 说出 `update_weights_from_ipc` 收到的四类 payload(`list` / `Exception` / 第一个 `None` / 第二个 `None`)各自的语义,以及 `released` 标志如何区分两个 `None`。
2. 掌握 `_extract_weights` 如何用「字节切片 → `view(dtype)` → `view(shape)`」三步,从一块扁平的 uint8 共享显存里零拷贝地切出带名字的张量。
3. 解释为什么 worker 在本地出错时**不直接 `raise`**,而是把异常文本回传给 PS、等 PS 统一下发退出指令。
4. 能在**纯 CPU 环境**下用一个 mock 脚本把整个 REP 状态机跑起来,并观察每一步的消息交替。

## 2. 前置知识

本讲承接两篇前置讲义的结论,不再重复推导:

- **u1-l4(整体架构与三阶段数据流)**:一次 Broadcast 更新是「H2D 预取 → broadcast → reload」三阶段流水线;PS 侧有一块大小为 `2 × bucket_size` 的 IPC 双缓冲,`gidx % 2` 决定本轮写哪个半区。本讲的 worker 就是「reload」阶段的执行者。
- **u4-l3(CUDA IPC:TorchIPCHandler 与 reduce_tensor)**:PS 用 `IPCHandler.export()` 把设备缓冲导出成可 pickle 的句柄,消费端用 `attach(handle, device_id)` 重建出指向**同一块显存**的张量,`detach()` 负责清理。本讲的 worker 就是这个句柄的消费端。

此外需要几个通用概念:

- **ZMQ 的 REQ/REP 模式**:两种 socket 的消息必须严格交替——REQ 发一条、REP 收一条,REP 回一条、REQ 收一条,谁错序谁报错。PS 侧是 REQ 且 `bind`,worker 侧是 REP 且 `connect`(方向比较反直觉,但代码如此,见下文)。
- **`send_pyobj` / `recv_pyobj`**:ZMQ 在底层用 pickle 序列化任意 Python 对象,所以消息可以是 dict、list,也可以是 `None` 甚至 `Exception` 实例。
- **torch 的视图(view)语义**:`t[a:b]` 切片和 `.view(...)` 都不复制数据,只是新建一个指向同一块 storage 的张量描述符。`t.data_ptr()` 返回该描述符指向的首字节地址。
- **`gc.collect()` / `empty_cache()`**:分别回收 Python 对象与设备侧缓存块。状态机的清理分支会按固定顺序调用它们。

如果对「双缓冲为什么要 ACK」感到陌生,建议先回看 u1-l4 的第 4 节。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | 主角。`FlattenedTensorMetadata`、`_extract_weights`、`update_weights_from_ipc` 三个模块全在此文件 |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | 发送侧对照:`_to_named_tensor` 生成 worker 收到的 payload,`_update_per_bucket` 决定消息顺序 |
| [checkpoint_engine/ipc_handler.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py) | u4-l3 已精读,本讲只引用 `attach`/`detach` 契约 |
| [tests/test_update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py) | 端到端测试中 worker 侧的测试替身(`checker_proc` / `checker_proc_with_error`) |

一条提醒:worker.py 依赖 vLLM 的部分全部集中在文件底部的 `VllmColocateWorkerExtension` 类(那是 u4-l2 的主题),本讲精读的顶部 130 行不依赖 vLLM,可以在任何装了 torch + pyzmq 的环境里 import。

## 4. 核心概念与源码讲解

### 4.1 FlattenedTensorMetadata:worker 的「取货单」

#### 4.1.1 概念说明

广播阶段,PS 通过 `dist.broadcast` 把一个桶的字节写进了 IPC 双缓冲的某个半区。但 worker 拿到的 `buffer` 只是一维的 `uint8` 字节流——它不知道里面躺了几个张量、每个张量叫什么、是什么形状和 dtype、从第几个字节开始。

`FlattenedTensorMetadata` 就是随桶附带的「取货单」:每行描述一个张量在共享字节流里的位置。它是一个 `TypedDict`,四个字段:

- `name`:张量名(与模型参数名对齐,直接喂给推理引擎的 `load_weights`);
- `shape` / `dtype`:恢复张量形状与类型所需的信息;
- `offset`:**该张量在共享 ipc_buffer 中的起始字节偏移**。

注意源码注释里特别强调 offset 是"start offset of this tensor in shared ipc_buffer tensor"——它是相对整块 buffer 的**绝对**偏移,已经把双缓冲半区的基址算进去了。这一点是理解 `_extract_weights` 的钥匙。

#### 4.1.2 核心流程

取货单由 PS 侧的 `_to_named_tensor(metas, offset)` 生成:从初始 `offset` 开始,每登记一个张量就累加它的 `aligned_size`(256 字节对齐后的占用),下一个张量从新的累计值开始。伪代码:

```text
offset ← base                      # base = (gidx % 2) * bucket_size
for meta in bucket.items:
    emit {name, dtype, shape, offset: offset}
    offset ← offset + meta.aligned_size
```

形式化地,桶内第 \( i \) 个张量的偏移为:

\[
\text{offset}_i \;=\; \underbrace{(g \bmod 2)\times B}_{\text{半区基址}} \;+\; \sum_{j<i} \text{aligned\_size}_j
\]

其中 \( B \) 是 bucket_size,\( g \) 是全局桶序号。也就是说:**相邻张量的间距不是它们的真实字节数,而是对齐后的槽位大小**,中间留着 padding 空洞(回顾 u2-l1:`aligned_size` 按 256 字节向上取整)。

#### 4.1.3 源码精读

worker 侧的定义——一个纯类型标注的 `TypedDict`,无任何运行时逻辑:

[checkpoint_engine/worker.py:31-36](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L31-L36)
```python
class FlattenedTensorMetadata(TypedDict):
    name: str
    shape: torch.Size
    dtype: torch.dtype
    # specify the start offset of this tensor in shared ipc_buffer tensor
    offset: int
```

PS 侧的生成方——注意 `offset` 参数每次递增 `aligned_size` 而非真实字节数:

[checkpoint_engine/ps.py:35-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48)
```python
def _to_named_tensor(metas: list[ParameterMeta], offset: int = 0) -> list[dict]:
    ret = []
    for meta in metas:
        size = meta.aligned_size
        ret.append({"name": meta.name, "dtype": meta.dtype,
                    "shape": meta.shape, "offset": offset})
        offset += size
    return ret
```

调用点把双缓冲基址传了进去——这就是 `offset` 天然是「绝对偏移」的原因:

[checkpoint_engine/ps.py:904](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L904)
```python
socket.send_pyobj(_to_named_tensor(bucket.items, gidx % 2 * bucket_size))
```

对照 PS 侧写半区的代码,可以看到写入起点与取货单基址是同一个表达式:

[checkpoint_engine/ps.py:876-877](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L876-L877)
```python
start = gidx % 2 * bucket_size
buffer_b: torch.Tensor = buffer[start : start + bucket.size]
```

一个设计取舍值得点出(承接 u2-l1 的结论):这个消息结构**不走 pydantic**。`FlattenedTensorMetadata` 只活在 PS → worker 的 pickle 通道里,不跨 JSON/HTTP 边界,所以用零运行时开销的 `TypedDict` 纯做类型标注;`_to_named_tensor` 返回的普通 dict 就是它的实例。

#### 4.1.4 代码实践

**实践目标**:在纯 CPU 环境验证 offset 的累计规则——间距是 `aligned_size` 而不是真实字节数。

**操作步骤**(示例代码,保存为临时脚本运行):

```python
# practice_metadata.py(示例代码,非项目原有文件)
import torch
from checkpoint_engine.data_types import ParameterMeta
from checkpoint_engine.ps import _to_named_tensor

# 两个张量:3x4 的 bf16 实际只有 24 字节,但登记的对齐槽位是 256
metas = [
    ParameterMeta(name="mlp.w1", dtype=torch.bfloat16,
                  shape=torch.Size([3, 4]), aligned_size=256),
    ParameterMeta(name="mlp.w2", dtype=torch.float32,
                  shape=torch.Size([5]), aligned_size=256),
]
first  = _to_named_tensor(metas, offset=0)
second = _to_named_tensor(metas, offset=4096)   # 模拟第二个半区的基址

for item in first:
    print(item["name"], item["offset"])
print([item["offset"] for item in second])
```

**需要观察的现象**:`mlp.w2` 的 offset 不是 24(真实字节数),而是 256(对齐槽位);换基址后整体平移 4096。

**预期结果**:`first` 的 offset 依次为 `0`、`256`;`second` 的 offset 为 `[4096, 4352]`。两个张量之间各留着 232 / 242 字节的 padding 空洞。本脚本依赖 pydantic 校验通过,具体输出待本地验证(算法本身由上面两段源码唯一确定)。

#### 4.1.5 小练习与答案

**练习 1**:某桶里有 3 个张量,`aligned_size` 分别为 512、256、1024,桶基址为 `bucket_size`。第三个张量的 `offset` 是多少?

**答案**:\( \text{bucket\_size} + 512 + 256 \)。基址加前两个张量的对齐槽位之和,与第三个张量自身大小无关。

**练习 2**:`offset` 是「相对桶起点」还是「相对整块 buffer 起点」?如果理解错了会发生什么?

**答案**:相对整块 buffer(含 `gidx % 2 × bucket_size` 的半区基址)。若理解成相对桶起点,`_extract_weights` 在第二个半区会从 buffer 头部取数——读到的是上一个桶的旧数据,且不报错,是最难排查的那类静默错误。

**练习 3**:为什么 `FlattenedTensorMetadata` 用 `TypedDict` 而不是 `ParameterMeta` 那样的 pydantic 模型?

**答案**:它只经 ZMQ pickle 在两个进程间传递,不需要 JSON 序列化与校验;`_to_named_tensor` 产出的就是普通 dict,`TypedDict` 以零运行时成本提供了类型提示。对比:`ParameterMeta` 要上 HTTP API 和 metas 文件,所以必须是 pydantic(见 u2-l1)。

### 4.2 _extract_weights:从扁平字节流零拷贝切出张量

#### 4.2.1 概念说明

`_extract_weights(payload, buffer)` 是 reload 阶段的核心:输入取货单和共享字节流,输出推理引擎 `load_weights` 期望的 `[(name, tensor), ...]`(这个类型别名就定义在文件顶部)。

它只做三步,每步都不复制数据:

1. **字节切片**:`buffer[offset : offset + size]` 得到一段长度为 `size` 的 uint8 视图;
2. **重解释**:`.view(dtype=dtype)` 把这些字节按目标 dtype 重新解释,长度变为 \( \text{size} / \text{itemsize} \);
3. **变形**:`.view(shape)` 把一维张量reshape 成目标形状。

其中 `size` 用的是**真实字节数** \( \text{itemsize} \times \text{numel} \),不含对齐 padding——padding 留在槽位之间的空洞里,被直接跳过。最终张量与 IPC buffer 共享同一块显存:这就是「零拷贝」,也是 u1-l4 说 reload 阶段不搬数据的落点。

#### 4.2.2 核心流程

对取货单里的每一项:

\[
\text{size}_i = \text{itemsize}(d_i) \times \text{numel}(s_i), \qquad
t_i = \text{buffer}[\text{offset}_i : \text{offset}_i + \text{size}_i].\text{view}(d_i).\text{view}(s_i)
\]

零拷贝可以用地址验证:

\[
\text{data\_ptr}(t_i) = \text{data\_ptr}(\text{buffer}) + \text{offset}_i
\]

另有一个防御性细节:代码接受 `shape` 以 `list` 或 `tuple` 形式到达(跨序列化边界时 `torch.Size` 可能退化为普通 list),先转回 `torch.Size` 再断言。`aligned_size` 按 256 对齐还有一个副作用:256 是任何常见 dtype itemsize(1/2/4/8)的倍数,所以相邻张量的起点天然满足 `view(dtype)` 的对齐要求。

#### 4.2.3 源码精读

整个函数只有 12 行,值得逐行读:

[checkpoint_engine/worker.py:18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L18)
```python
_WEIGHTS_TYPE = list[tuple[str, torch.Tensor]]
```
这是 vLLM `model.load_weights()` 的标准输入格式,`_extract_weights` 的返回值类型。

[checkpoint_engine/worker.py:39-51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L39-L51)
```python
def _extract_weights(payload: list[FlattenedTensorMetadata], buffer: torch.Tensor) -> _WEIGHTS_TYPE:
    assert buffer is not None
    weights: _WEIGHTS_TYPE = []
    for item in payload:
        shape = item["shape"]
        if isinstance(shape, list | tuple):
            shape = torch.Size(shape)
        assert isinstance(shape, torch.Size)
        dtype, offset = item["dtype"], item["offset"]
        size = dtype.itemsize * shape.numel()
        tensor = buffer[offset : offset + size].view(dtype=dtype).view(shape)
        weights.append((item["name"], tensor))
    return weights
```

- 第 40 行的 `assert buffer is not None`:释放资源后 `buffer` 会被置 `None`(见 4.3),这里防止把已释放的 buffer 传进来。
- 第 44-46 行:shape 的兜底转换与断言。
- 第 48 行:`size` 按真实字节数计算,padding 不进切片。
- 第 49 行:三步视图链,一步到位。

#### 4.2.4 代码实践

**实践目标**:在纯 CPU 上复刻三步视图链,并从两个维度验证「零拷贝」。

**操作步骤**(示例代码,自包含、无需导入本项目):

```python
# practice_extract.py(示例代码,非项目原有文件)
import torch

ALIGN = 256
t1 = torch.arange(12, dtype=torch.float32).view(3, 4)   # 48 字节
t2 = torch.ones(5, dtype=torch.bfloat16)                 # 10 字节

def align(n):  # 复刻 _ALIGN_SIZE 语义
    return (n + ALIGN - 1) // ALIGN * ALIGN

n1, n2 = t1.numel() * t1.element_size(), t2.numel() * t2.element_size()
s1 = align(n1)                                          # 256

buffer = torch.zeros(s1 + align(n2), dtype=torch.uint8)
buffer[:n1].copy_(t1.flatten().view(torch.uint8))       # 布置第一个张量
buffer[s1 : s1 + n2].copy_(t2.view(torch.uint8))        # 第二个从对齐槽位开始

# 复刻 worker.py L48-L49 的两行
size1 = t1.dtype.itemsize * t1.shape.numel()
r1 = buffer[0:size1].view(dtype=t1.dtype).view(t1.shape)
r2 = buffer[s1 : s1 + n2].view(dtype=t2.dtype).view(t2.shape)

print(torch.equal(r1, t1), torch.equal(r2, t2))         # 数值一致
print(r1.data_ptr() == buffer.data_ptr(),               # 共享 storage
      r2.data_ptr() == buffer.data_ptr() + s1)
r1[0, 0] = 999.0                                        # 通过视图写入
print(buffer[:4].view(torch.float32)[0].item())         # buffer 字节同步变化
```

**需要观察的现象**:数值相等;`data_ptr` 满足上面的地址等式;修改 `r1` 后 `buffer` 里对应字节同步变化。

**预期结果**:输出 `True True`、`True True`、`999.0`。前两组证明恢复的张量是 buffer 的视图而非副本,第三组直观演示了「PS 写半区、worker 通过视图读到同一份字节」这一 IPC 共享的本质。逻辑由视图语义唯一确定,输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:`size` 为什么用 `itemsize × numel` 而不是 `aligned_size`?

**答案**:切片只需要张量的真实字节;`aligned_size` 与真实字节数之差是留给下一个张量对齐用的 padding。若按 `aligned_size` 切,多出来的 padding 字节会让 `view(dtype).view(shape)` 因元素数不匹配而报错。

**练习 2**:如果 PS 侧把 `aligned_size` 的对齐从 256 改成 4,哪些 dtype 组合仍能工作?

**答案**:itemsize 为 1、2、4 的 dtype(fp8、bf16/fp16、fp32)都可以,因为任何起点偏移仍是 4 的倍数;但 itemsize 为 8 的 dtype(fp64、部分 int64 场景)可能落在未按 8 对齐的起点上,`view(dtype)` 不再保证合法。256 的选择给所有 2 的幂 itemsize 留足了余量。

**练习 3**:怎么用一行代码证明 `_extract_weights` 返回的张量没有发生显存拷贝?

**答案**:比较地址——`assert t.data_ptr() == buffer.data_ptr() + offset`(data_ptr 相同即共享同一块 storage;拷贝会得到新地址)。

### 4.3 update_weights_from_ipc:REP 状态机全景

#### 4.3.1 概念说明

`update_weights_from_ipc` 是 worker 侧的唯一入口,运行在推理引擎进程里(生产环境经 vLLM 的 `collective_rpc` 调用,测试环境由子进程直接调用)。它一次调用对应 PS 的一整轮 `update`,阻塞到整轮权重更新结束才返回。

签名上有三个关键角色:

- `zmq_ctx` / `zmq_handle`:ZMQ 上下文与 PS 侧 `bind` 好的地址,worker 作为 REP `connect` 上去;
- `run`:装载回调——生产环境是 vLLM 的 `load_weights`,把切出来的张量写进模型;
- `post_hook`:整轮结束后的收尾回调——生产环境是 `process_weights_after_loading`(FP8 重排等重活,见 u4-l2)。

它是一个**由对端消息驱动的状态机**。PS 发什么,worker 就转换到什么状态;worker 唯一的本地状态变量是布尔标志 `released`。文件里的五条注释就是协议文档:

[checkpoint_engine/worker.py:78-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82)
```python
# State machine:
# + receive tensor_metadata -> update_weights
# + receive Exception -> raise and stop
# + receive None first time -> release resources
# + receive None second time -> call post_hook and stop
```

#### 4.3.2 核心流程

先看 worker 是怎么被召唤起来的。PS 的 `update` 第二个参数 `req_func` 负责把「(设备 UUID, ZMQ 地址) 清单」送达推理引擎(测试里就是往队列里 `queue.put`,见 [tests/test_update.py:167](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L167));每个 worker 按自己的设备 UUID 取出地址,连上对应 socket(生产链路的完整编排是 u6-l2 的主题)。

状态机的文字版状态图:

```text
[connect REP]
     │  recv: IPC 句柄
     ▼
 ATTACHED ──(attach 失败:回传错误文本,等 PS 强制退出信号,raise)
     │  send: b""
     ▼
 UPDATING ◄────────────────────────────┐
     │  recv: list(取货单)             │
     │  run(_extract_weights(...))      │
     │  send: b""  ────────────────────┘
     │
     │  recv: None(第一次)→ 释放 IPC/清理 → send b""
     ▼
 RELEASED(released = True)
     │  recv: None(第二次)→ post_hook() → send b"" → 结束
     ▼
   [finally 清理,函数返回]

任何时刻 recv: Exception → raise,立即终止
```

与 PS 侧消息序列逐拍对齐(左发右收):

| 拍 | PS(REQ)发送 | worker(REP)接收 → 回复 | 对应源码 |
| --- | --- | --- | --- |
| 0 | IPC 句柄(`send_pyobj(handle)`) | `attach` → 回 `b""` | [ps.py:849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L849) / [worker.py:68-72](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L68-L72) |
| 1..n | 张量清单(`send_pyobj(_to_named_tensor(...))`) | `run(_extract_weights(...))` → 回 `b""` | [ps.py:904](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L904) / [worker.py:108-112](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L112) |
| 出错时 | — | worker 回错误文本(`send_string`),**不退出** | [worker.py:113-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L113-L117) |
| 强退 | `RuntimeError(...)`(投票后) | `raise payload` | [ps.py:900-903](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L900-L903) / [worker.py:118-121](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L118-L121) |
| 收尾 1 | `None`(释放信号) | 置 `released`、清理 → 回 `b""` | [ps.py:913](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L913) / [worker.py:94-107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L94-L107) |
| 收尾 2 | `None`(post_hook 信号) | `post_hook()` → 回 `b""` → break | [ps.py:931](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L931) / [worker.py:87-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L87-L93) |

u3-l6 已从两侧对齐过这条消息线;本讲的重点是 worker 内部每个分支**做什么、为什么这么做**:

- **每桶的 `b""` ACK 同时是背压信号**。双缓冲只能重叠相邻两桶,第 gidx+2 桶会复用第 gidx 桶的半区,所以 PS 必须等到第 gidx 桶的 ACK 才能覆盖同一半区。ACK 之前的 `synchronize()`(worker.py:111)保证设备侧装载真正完成,`load_weights` 里多为异步的 D2D 拷贝。
- **两次 `None` 之间隔着 PS 自己的清理**。PS 发出第一个 `None` 并收到 ACK 后,先删除自己的 buffer 视图、`gc`、`ipc_collect`、`empty_cache`(ps.py:915-929),确认**双方**都释放后才发第二个 `None` 放行 `post_hook`——因为 post_hook 里的权重量化重排需要腾出显存。两次 `None` 不能合并。
- **错误回传而非本地 raise**。见下文 4.3.3 的第 4 段,这是本讲最重要的设计点。

#### 4.3.3 源码精读

**(1) 连接方向:REP connect,不是 bind。**

[checkpoint_engine/worker.py:62-63](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L62-L63)
```python
socket = zmq_ctx.socket(zmq.REP)
socket.connect(zmq_handle)
```
PS 侧才是 `socket(zmq.REQ)` + `bind`(见 [ps.py:627-628](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L627-L628),地址形如 `ipc://@checkpoint-engine-<设备UUID>-<计数器>.sock`)。REQ bind / REP connect 是合法且被本项目使用的 ZMQ 形态。

**(2) 阶段 0:收句柄、attach、报平安。**

[checkpoint_engine/worker.py:64-77](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L64-L77)
```python
buffer: torch.Tensor | None = None
device_manager = DeviceManager()
ipc_handler: IPCHandler | None = None
try:
    ipc_handle = socket.recv_pyobj()
    ipc_handler = _ipc_handler_for_handle(ipc_handle)
    buffer = ipc_handler.attach(ipc_handle, device_id)
    assert buffer.dtype == torch.uint8
    socket.send(b"")
except Exception as e:
    msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    socket.send_string(msg)
    socket.recv()  # wait for ack
    raise
```
句柄格式先经 `_ipc_handler_for_handle` 分流:CUDA/NPU 的 `reduce_tensor` 元组走 `TorchIPCHandler`,带 `kind="xpu_sycl"` 标签的 dict 走 `XpuIPCHandler`:

[checkpoint_engine/worker.py:21-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L21-L28)
```python
def _ipc_handler_for_handle(handle: object) -> IPCHandler:
    if isinstance(handle, dict) and handle.get("kind") == XpuIPCHandler.kind:
        return XpuIPCHandler()
    return TorchIPCHandler()
```
`attach` 重建出指向同一块显存的 uint8 张量(契约见 [ipc_handler.py:47-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L47-L48),CUDA 路径的重建细节在 u4-l3)。attach 失败的路径值得细看:worker 先把 traceback 文本回传,再 `socket.recv()` 等 PS 的下一条消息——那将是 PS 投票后发来的 `RuntimeError`——收到后才 `raise` 本地异常。这样既保住了 REQ/REP 交替,也让退出时机由 PS 统一决定。

**(3) 主循环:一个 `released` 标志驱动的四路分派。**

先看 `released` 已置位时的分支(第二个 `None` 的归宿):

[checkpoint_engine/worker.py:87-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L87-L93)
```python
if released:
    assert payload is None, "Should not receive any payload after released"
    if post_hook is not None:
        post_hook()
    device_manager.device_module.synchronize()
    socket.send(b"")
    break
```
断言把协议不变式写成代码:释放之后再收到取货单就是违约,直接炸出来而不是静默读已释放的内存。

第一个 `None` 分支——按固定顺序释放资源:

[checkpoint_engine/worker.py:94-107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L94-L107)
```python
if payload is None:  # done signal
    device_manager.device_module.synchronize()
    released = True
    buffer = None
    if ipc_handler is not None:
        ipc_handler.detach()

    gc.collect()
    device_manager.ipc_collect()
    device_manager.device_module.empty_cache()
    device_manager.device_module.synchronize()
    socket.send(b"")
    continue
```
顺序有讲究:先 `synchronize` 确保设备上的装载全部完成,再切断 Python 引用(`buffer = None`)、`detach` IPC,然后 `gc.collect()` → `ipc_collect()` → `empty_cache()` 三连把显存真正还回去,最后再 `synchronize` 一次才回 ACK。

`list` 分支——正常装载一桶:

[checkpoint_engine/worker.py:108-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L117)
```python
if isinstance(payload, list):  # still updating weights
    try:
        run(_extract_weights(payload, buffer))
        device_manager.device_module.synchronize()
        socket.send(b"")
    except Exception as e:  # noqa: BLE001
        # Send exception back to Parameter Server.
        # Don't raise here. Because all workers should quit in the same way by receiving the exception from PS
        msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        socket.send_string(msg)
```

`Exception` 分支——PS 的强制退出信号,以及协议兜底:

[checkpoint_engine/worker.py:118-123](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L118-L123)
```python
elif isinstance(payload, Exception):
    raise payload
else:
    raise TypeError(f"Unexpected payload type: {type(payload)}")
```

**(4) 为什么出错不 `raise`——分布式一致退出。** 关键在 108-117 的注释:**所有 worker 必须以「收到 PS 下发的异常」这一相同方式退出**。设想 8 张卡上有 8 个 worker,如果 0 号卡装载失败直接 raise、进程退出,而其余 7 个还在等下一桶的取货单,集群就进入了「半死」状态。所以worker 把异常格式化成文本回传,自己留在循环里;PS 收到非空应答后置 `ret_code=1`,经 `all_reduce` 让**组内所有 rank** 都看到失败,再统一向各自的 worker 发 `RuntimeError("Some workers failed to update weights")`:

[checkpoint_engine/ps.py:891-903](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L891-L903)
```python
resp = socket.recv()
if resp != b"":
    ...
    ret_code.fill_(1)
dist.all_reduce(ret_code, op=torch.distributed.ReduceOp.SUM, group=ranks_group)
self.device_manager.device_module.synchronize()
if ret_code.item() != 0:
    # quit early if any rank failed
    socket.send_pyobj(RuntimeError("Some workers failed to update weights"))
    raise RuntimeError("Failed to update weights due to remote errors")
```
两侧的异常文本是配对的:PS 进程 raise `"Failed to update weights due to remote errors"`,worker 进程收到的是 `"Some workers failed to update weights"`——测试正是按这个约定断言的:

[tests/test_update.py:83-85](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L83-L85)
```python
except RuntimeError as e:
    assert str(e) == "Some workers failed to update weights"
```
注意一个不对称细节:阶段 0 失败时 worker 最终 raise 的是**自己的原始异常**(worker.py:73-77);循环内失败时 raise 的是 **PS 下发的 `RuntimeError`**(worker.py:121)。前者发生在任何桶广播之前,后者需要与全组同步退出。

**(5) `finally`:无论怎么退出都收尾。**

[checkpoint_engine/worker.py:125-131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L125-L131)
```python
finally:
    socket.close()
    del buffer
    if ipc_handler is not None:
        ipc_handler.detach()
    gc.collect()
    device_manager.device_module.empty_cache()
```
正常路径里第一个 `None` 分支已经 `detach` 过一次,这里再来一次。这是安全的:`TorchIPCHandler` 没有覆写 `detach`,用的是基类默认的空操作([ipc_handler.py:50-51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L50-L51));`XpuIPCHandler` 的 `detach` 把指针置 `None`,天然幂等。双保险是为了覆盖 `raise payload` 这类**没走过释放分支**的退出路径。

#### 4.3.4 代码实践

**实践目标**:在纯 CPU、无 GPU、无 vLLM 的环境下,把 `update_weights_from_ipc` 的完整状态机(阶段 0 → 两轮桶 → 两次 `None`)跑一遍,观察 REQ/REP 的逐拍交替。

**难点与解法**:`update_weights_from_ipc` 内部无条件构造 `DeviceManager()`,而它在无 GPU 机器上会抛 `TypeError`(见 [device_utils.py:222-230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L222-L230));IPC 句柄也需要真实设备。好在状态机只依赖这两个模块级名字——`DeviceManager` 和 `_ipc_handler_for_handle` 都在 worker 模块命名空间里被查找,我们可以在**不改源码**的前提下替换它们(这也是阅读收获:状态机对设备层的依赖就这么窄)。

**操作步骤**(示例代码,非项目原有文件):

```python
# practice_worker_mock.py(示例代码,非项目原有文件)
import threading
import torch
import zmq

import checkpoint_engine.worker as worker_mod
from checkpoint_engine.worker import update_weights_from_ipc

BUCKET_SIZE, ALIGN = 4096, 256
align = lambda n: (n + ALIGN - 1) // ALIGN * ALIGN

class FakeDeviceManager:          # 状态机只用到这三个设备操作
    device_module = type("M", (), {
        "synchronize": staticmethod(lambda: None),
        "empty_cache": staticmethod(lambda: None)})()
    def ipc_collect(self): pass

class FakeIPCHandler:             # 句柄直接携带 buffer
    kind = "fake"
    def export(self, buffer): return {"kind": self.kind, "buffer": buffer}
    def attach(self, handle, device_id): return handle["buffer"]
    def detach(self): pass

worker_mod.DeviceManager = FakeDeviceManager
worker_mod._ipc_handler_for_handle = lambda handle: FakeIPCHandler()

def plan_bucket(tensors, base):   # 复刻 _to_named_tensor 的 offset 累计
    payload, cursor = [], base
    for name, t in tensors:
        payload.append({"name": name, "dtype": t.dtype,
                        "shape": list(t.shape), "offset": cursor})
        cursor += align(t.dtype.itemsize * t.shape.numel())
    return payload

buckets = [
    [("mlp.w1", torch.arange(12, dtype=torch.float32).view(3, 4)),
     ("mlp.w2", torch.ones(6, dtype=torch.bfloat16))],
    [("lm_head", torch.full((2, 2), 7.0, dtype=torch.float32))],
]
EXPECTED = {name: t for bucket in buckets for name, t in bucket}

# 注意:ZMQ pickle 会"拷贝"buffer,而真实 IPC 是共享。
# 所以必须在发句柄之前把两个半区都写好,PS 线程随后只发元数据。
buffer = torch.zeros(2 * BUCKET_SIZE, dtype=torch.uint8)
payloads = []
for gidx, bucket in enumerate(buckets):
    p = plan_bucket(bucket, (gidx % 2) * BUCKET_SIZE)
    for (name, t), meta in zip(bucket, p):
        n = t.dtype.itemsize * t.shape.numel()
        buffer[meta["offset"]: meta["offset"] + n].copy_(t.flatten().view(torch.uint8))
    payloads.append(p)

events = []
def fake_ps(url):
    s = zmq.Context().socket(zmq.REQ)   # 与 ps.py 相同:REQ bind
    s.bind(url)
    s.send_pyobj({"kind": "fake", "buffer": buffer})     # 阶段 0:句柄
    assert s.recv() == b""
    for gidx, p in enumerate(payloads):                  # 每桶:清单
        s.send_pyobj(p)
        if s.recv() != b"":
            s.send_pyobj(RuntimeError("Some workers failed to update weights"))
            return
        events.append(f"bucket {gidx} acked")
    s.send_pyobj(None); assert s.recv() == b""           # 第一次 None:释放
    events.append("worker released")
    s.send_pyobj(None); assert s.recv() == b""           # 第二次 None:post_hook
    events.append("worker post_hook done")
    s.close()
```

接着是 worker 侧的 `run` 回调与主流程:

```python
def run(weights):
    for name, t in weights:
        assert torch.equal(t, EXPECTED[name]), name      # 数值必须对得上
    events.append(f"run verified: {sorted(n for n, _ in weights)}")

threading.Thread(target=fake_ps, args=("tcp://127.0.0.1:5555",), daemon=True).start()
update_weights_from_ipc(
    zmq.Context(), "tcp://127.0.0.1:5555", device_id=0,
    run=run, post_hook=lambda: events.append("post_hook"))
print(*events, sep="\n")
```

**需要观察的现象**:事件序列严格按协议顺序出现,没有任何一步死锁;`run` 里的数值断言全部通过——证明 `list` 形式的 shape 也被 4.2 的兜底分支正确转换。

**预期结果**:

```text
run verified: ['mlp.w1', 'mlp.w2']
bucket 0 acked
run verified: ['lm_head']
bucket 1 acked
worker released
post_hook
worker post_hook done
```

「run verified 在前、bucket acked 在后」的顺序由协议保证(ACK 在 `run` 返回并 `synchronize` 之后才发出)。两处与真实系统的差异要知道:① 我们的假通道经 pickle **拷贝** buffer,所以必须预写两个半区;真实 IPC 是共享内存,PS 在每轮广播前才写半区。② 真实地址是 `ipc://@...` 抽象 UDS,这里用 tcp 便于跨平台。脚本行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:worker 在 `list` 分支出错后,下一条收到的消息是什么?它从哪来?

**答案**:PS 下发的 `RuntimeError` 实例。路径:worker `send_string(错误文本)` → PS `socket.recv()` 拿到非空应答 → `ret_code` 置 1 并 `all_reduce` → 组内所有 PS rank 都向各自 worker `send_pyobj(RuntimeError(...))` → worker 下一轮 `recv_pyobj` 收到后 `raise payload`。

**练习 2**:如果去掉第一个 `None` 分支里的 `synchronize()`,最先可能出什么问题?

**答案**:设备上的装载可能还没完成就置 `released = True` 并把 `buffer` 引用清掉、`detach` IPC。虽然张量视图还持有 storage 引用不会被回收,但 IPC 资源的释放与后续 `empty_cache` 可能把尚在执行的操作置于未定义状态;同时回给 PS 的「已释放」ACK 也是谎言。`synchronize` 把「释放完成」变成可承诺的事实。

**练习 3**:`finally` 里的 `ipc_handler.detach()` 与第一个 `None` 分支里的 `detach()` 重复执行,为什么不会出错?

**答案**:`TorchIPCHandler` 未覆写 `detach`,继承基类的空实现([ipc_handler.py:50-51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L50-L51));`XpuIPCHandler` 的 `detach` 执行后把两个指针都置回 `None`,第二次调用直接跳过。这是刻意的幂等设计,让 `finally` 能无条件兜底覆盖强退路径。

## 5. 综合实践

在 4.3.4 脚本的基础上注入一次装载失败,完整走一遍**错误传播链**,并与 GPU 端到端测试对照:

1. **改造 `run`**:保留数值校验,但在看到 `lm_head` 时抛异常(模仿 [tests/test_update.py:72-76](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L72-L76) 的 `error_run`,它在 sleep 后于指定 rank 抛 `RuntimeError`):

   ```python
   def run(weights):
       for name, t in weights:
           assert torch.equal(t, EXPECTED[name]), name
       events.append(f"run verified: {sorted(n for n, _ in weights)}")
       if any(name == "lm_head" for name, _ in weights):
           raise RuntimeError("boom: lm_head mismatch")
   ```

2. **用 `try/except` 包住主调用**,打印捕获到的异常文本。
3. **对照断言**(全部来自源码语义,待本地验证):
   - 捕获的异常文本是 `Some workers failed to update weights`——worker raise 的是 **PS 下发**的 `RuntimeError`,不是自己的 `boom: ...`;
   - 事件表里 `bucket 1 acked`、`worker released`、`post_hook` **都不出现**——失败的桶不会被 ACK,释放与收尾分支被整体跳过;
   - `fake_ps` 走进错误分支后发出 `RuntimeError` 即返回,不再发任何 `None`。
4. **阅读对照**:通读 [tests/test_update.py:52-85](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L52-L85) 的 `checker_proc_with_error`,确认它与你的 mock 做的是同一件事,只是把「假 PS」换成了真实的 `ParameterServer` + `dist.all_reduce` 投票;该测试由 `torchrun` 多进程驱动并标记为 `pytest.mark.gpu`([tests/test_update.py:239](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L239)),需要至少 2 张 GPU 才能真跑。

## 6. 本讲小结

- worker 侧是一个由对端消息驱动、`released` 布尔标志加持的 **REP 状态机**:`list` 装载一桶、`Exception` 强制退出、第一个 `None` 释放资源、第二个 `None` 执行 `post_hook` 后收工;阶段 0 先 `attach` IPC 句柄拿到共享显存。
- `FlattenedTensorMetadata` 是「取货单」,`offset` 是**含双缓冲半区基址的绝对偏移**,间距按 `aligned_size`(256 对齐)累计;`_extract_weights` 用「切片 → `view(dtype)` → `view(shape)`」三步零拷贝还原张量。
- 每桶的 `b""` ACK 在 `synchronize()` 之后发出,既是装载完成信号也是双缓冲的背压信号;两个 `None` 之间隔着 PS 侧自己的清理,保证 `post_hook` 重跑量化前显存已腾空。
- worker 本地出错**只回传文本不退出**,由 PS 经 `ret_code` 全体约减后统一下发 `RuntimeError`,全集群同生共死;正常清理与强退路径都由幂等的 `finally` 兜底。
- 整个状态机对设备层的依赖只有 `DeviceManager` 与 IPC 句柄两个接缝,可在纯 CPU 上用替身完整驱动。

## 7. 下一步学习建议

- **u4-l2(VllmColocateWorkerExtension)**:看 `run` 与 `post_hook` 在生产环境的真实实现——`load_weights`、MTP drafter 的处理与 `process_weights_after_loading`,以及 `collective_rpc` 如何把本讲的函数召唤起来。
- **u4-l4(XPU SYCL IPC)**:本讲只消费句柄;那一讲讲 XPU 句柄的产生、`detach` 里打开/导出两侧的释放时序。
- **回头对照 u3-l6 的 PS 侧代码**:把本讲的状态机表与 `req_func`、`_bind_zmq_socket` 的发送顺序并排读,协议两侧就完全闭环了。
- 若手头有 GPU,跑一次 `pytest tests/test_update.py -k "test_with_remote_error"`(需 torchrun 多进程,参考 [tests/test_update.py:262-290](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L262-L290) 的启动方式),在真实错误传播下复观本讲的结论。
