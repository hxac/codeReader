# 进程间消息与序列化

## 1. 本讲目标

本讲聚焦 `python/minisgl/message/` 这一个包。读完本讲，你应该能够：

- 说出 Mini-SGLang 中 **backend / frontend / tokenizer 三类消息**各自代表什么、在进程之间朝哪个方向流动；
- 看懂 `serialize_type` / `deserialize_type` 这一对工具如何利用 `__dict__` 把任意 dataclass 对象**递归地**压平成一个纯字典（dict、list、标量、bytes），以及如何从字典里把它**原样重建**回来；
- 解释 `torch.Tensor` 是怎么被塞进这个「纯字典」世界的（先转 numpy 再 `tobytes()`，且目前**只支持 1D 张量**）；
- 说明每个消息基类的 `encoder` / `decoder` 是如何被当作回调函数注入到 `ZmqPushQueue` / `ZmqPullQueue` 里，和 `msgpack` + ZMQ 配合完成跨进程投递的。

本讲不涉及调度算法或 GPU 计算，只解决一个问题：**进程之间用什么语言对话，这句话怎么编码、怎么解码。**

## 2. 前置知识

在进入源码前，先建立三个直觉。

**(1) 为什么需要「序列化」。** 回顾 [u1-l4](u1-l4-process-architecture.md)：Mini-SGLang 是多进程架构——API Server、Tokenizer、Detokenizer、每张 GPU 一个 Scheduler，彼此是**独立的 Python 进程**，内存不共享。一个进程里的 Python 对象（比如一条带 `torch.Tensor` 的请求）不能直接「递」给另一个进程，必须先变成一段**连续的字节流**塞进管道，对方再从字节流「拼」回对象。这个「对象 → 字节」叫序列化（serialize / encode），「字节 → 对象」叫反序列化（deserialize / decode）。

**(2) ZMQ + msgpack 的分工。** 进程之间的管道是 ZMQ（`pyzmq`），它只负责**搬字节**，不关心字节里是什么。字节的「装箱/拆箱」由 `msgpack` 完成：`msgpack.packb(dict)` 把一个**纯字典**压成一段紧凑字节，`msgpack.unpackb(bytes)` 再还原成字典。所以我们的消息对象只要能转换成「只含 dict / list / 标量 / bytes 的字典」，就能被 msgpack 接住。`msgpack` 与 `pyzmq` 都是项目依赖（见 [pyproject.toml:26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L26) 的 `msgpack`、[pyproject.toml:31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/pyproject.toml#L31) 的 `pyzmq`）。

**(3) 为什么不用 pickle。** Python 自带的 `pickle` 也能序列化对象，但它**不安全**（反序列化时会执行任意代码）、**跨语言不友好**、且对 torch 张量的处理偏重。Mini-SGLang 选择了一条更轻、更可控的路：自己写两个几十行的函数，把对象显式映射成纯字典，张量则退化成「裸字节 + dtype 字符串」。代价是只支持 1D 张量（见后文），但对传递 token id 这种 1D 整数数组已经足够。

> 术语提示：本讲里 **encoder** = 序列化器（对象→字典），**decoder** = 反序列化器（字典→对象）；而 `serialize_type` / `deserialize_type` 是真正干活的底层函数，encoder/decoder 只是对它们的薄封装。

## 3. 本讲源码地图

本讲涉及的核心文件都在 `python/minisgl/message/` 包内，外加两个「消费者」文件用来展示接线方式。

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| [message/\_\_init\_\_.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/__init__.py) | 汇总导出全部消息类 | 看「有哪些消息」的总清单 |
| [message/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py) | `serialize_type` / `deserialize_type` 及递归辅助函数 | 序列化的「引擎」，本讲主角 |
| [message/backend.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/backend.py) | `UserMsg` / `AbortBackendMsg` / `ExitMsg` 等发往 scheduler 的消息 | 「去后端」的消息族 |
| [message/frontend.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/frontend.py) | `UserReply` 等回给 API Server 的消息 | 「回前端」的消息族 |
| [message/tokenizer.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/tokenizer.py) | `TokenizeMsg` / `DetokenizeMsg` / `AbortMsg` 等 | 「进/出 tokenizer」的消息族 |
| [utils/mp.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py) | `ZmqPushQueue` / `ZmqPullQueue` 等队列 | 展示 encoder/decoder 如何接入 ZMQ |
| [scheduler/io.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py) | Scheduler 收发消息的接线 | 展示队列如何被实例化 |
| [tests/misc/test_serialize.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/misc/test_serialize.py) | 序列化往返测试 | 综合实践的样板 |

## 4. 核心概念与源码讲解

### 4.1 消息类型层次与流转方向

#### 4.1.1 概念说明

Mini-SGLang 把进程间传递的消息按「**目的地**」分成三族，每族一个基类：

- **backend 消息族**（`BaseBackendMsg`）：流向「后端」即 Scheduler。最典型的是 `UserMsg`——携带已经 tokenize 好的 `input_ids`，让 scheduler 去推理。还有 `AbortBackendMsg`（请求中止）和 `ExitMsg`（进程退出信号）。
- **frontend 消息族**（`BaseFrontendMsg`）：流回「前端」即 API Server。典型是 `UserReply`——携带一段增量文本 `incremental_output` 和 `finished` 标志，最终回流给 HTTP 客户端。
- **tokenizer 消息族**（`BaseTokenizerMsg`）：进/出 tokenizer/detokenizer 进程。`TokenizeMsg`（带原始文本，请求变成 token）、`DetokenizeMsg`（带一个 `next_token`，请求变回文本）、`AbortMsg`。

每族还各自有一个 `BatchXxxMsg`，它的 `data` 字段是一个消息列表，用来把多条同族消息**打包成一批**一次性投递（批处理是吞吐的关键）。

#### 4.1.2 核心流程

三族消息沿请求生命周期形成一条环（回顾 [u1-l4](u1-l4-process-architecture.md) 的 8 步），全程靠 `uid` 串起身份：

```
API Server
   │  TokenizeMsg (tokenizer 族, 入)
   ▼
Tokenizer ──UserMsg (backend 族)──► Scheduler(rank0)
                                        │  广播给其他 rank
                                        ▼
                                    Engine 前向
                                        │
Scheduler(rank0) ──DetokenizeMsg (tokenizer 族, 回)──► Detokenizer
                                                        │  UserReply (frontend 族)
                                                        ▼
                                                    API Server ──► HTTP 客户端
```

要点：**同一族的消息走同一类队列**。例如 scheduler 收 backend 消息、tokenizer 收 tokenizer 消息、API Server 收 frontend 消息，各自配一对 encoder/decoder。

#### 4.1.3 源码精读

先看三族基类各自定义的 `encoder` / `decoder`。它们都只是对 `serialize_type` / `deserialize_type` 的薄封装：

[backend.py:12-19](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/backend.py#L12-L19) 定义了 backend 族的基类与两个序列化入口：

```python
@dataclass
class BaseBackendMsg:
    def encoder(self) -> Dict:
        return serialize_type(self)

    @staticmethod
    def decoder(json: Dict) -> BaseBackendMsg:
        return deserialize_type(globals(), json)
```

这里有两处关键设计，初学者容易忽略：

1. **`encoder` 是实例方法**（`def encoder(self)`），调用时是 `obj.encoder()`；而 [frontend.py:11-13](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/frontend.py#L11-L13) 和 [tokenizer.py:13-15](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/tokenizer.py#L13-L15) 的 `encoder` 是 `@staticmethod`，签名是 `encoder(msg)`。两种写法在「队列内部统一调用 `self.encoder(obj)`」时**效果相同**（实例方法把 `obj` 当作 `self`），只是风格不统一——这是阅读时不必纠结的细节。
2. **`decoder` 把 `globals()` 当作 `cls_map` 传下去**。`globals()` 是 `decoder` 所在模块（如 `backend.py`）的全局命名空间。反序列化时要按类名查表重建对象，这个 `globals()` 就是那张「类名 → 类」的表。这个细节非常关键，我们在 4.3 节展开。

再看三个族里「真正运货」的具体消息长什么样。[backend.py:32-36](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/backend.py#L32-L36) 的 `UserMsg` 字段说明了一切：

```python
@dataclass
class UserMsg(BaseBackendMsg):
    uid: int
    input_ids: torch.Tensor  # CPU 1D int32 tensor
    sampling_params: SamplingParams
```

注意它的负载是「一个 int + 一个 1D 张量 + 一个嵌套对象 `SamplingParams`」——这正好覆盖了序列化要处理的全部三种难点：标量、张量、嵌套 dataclass。注释 `CPU 1D int32 tensor` 也直接预告了「只支持 1D」的限制。

对照看 frontend 族最常用的 `UserReply`，[frontend.py:25-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/frontend.py#L25-L29)：

```python
@dataclass
class UserReply(BaseFrontendMsg):
    uid: int
    incremental_output: str
    finished: bool
```

全是标量字段，没有任何嵌套对象或张量——因此 frontend 族的 `globals()` 里**不需要**导入 `SamplingParams`，反序列化最简单。

最后，[message/\_\_init\_\_.py:1-19](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/__init__.py#L1-L19) 把三族消息统一再导出，外部代码只需 `from minisgl.message import UserMsg, UserReply, ...` 即可，不必关心它们分属哪个子模块。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是亲手把「消息族 ↔ 队列」对应起来。

1. **实践目标**：确认每条 ZMQ 队列用的是哪一族的 encoder/decoder。
2. **操作步骤**：
   - 打开 [scheduler/io.py:36-62](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L36-L62)，看 `_recv_from_tokenizer` 用 `BaseBackendMsg.decoder`、`_send_into_tokenizer` 用 `BaseTokenizerMsg.encoder`。
   - 打开 [api_server.py:433-442](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/server/api_server.py#L433-L442)，看前端收 `BaseFrontendMsg.decoder`、发 `BaseTokenizerMsg.encoder`。
   - 打开 [tokenizer/server.py:43-45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/tokenizer/server.py#L43-L45)，看 tokenizer 同时持有 backend、frontend 两个发送队列，和一个 `BatchTokenizerMsg.decoder` 接收队列。
3. **需要观察的现象**：同一个进程往往**一边用一个族的 encoder 发、一边用另一个族的 decoder 收**（例如 scheduler 发 `DetokenizeMsg` 用 tokenizer 族 encoder，收 `UserMsg` 用 backend 族 decoder）。
4. **预期结果**：你能画出一张表——「scheduler↔tokenizer 这条管道上，去程走 backend 族、回程走 tokenizer 族」，这与 4.1.2 的流向图一致。

#### 4.1.5 小练习与答案

**练习 1**：`UserMsg` 走的是哪一族？它由谁发出、谁接收？
**答案**：属于 backend 族；由 Tokenizer 进程发出（把 tokenize 好的 `input_ids` 交给后端），由 Scheduler 的 rank0 接收。

**练习 2**：为什么 `UserReply` 的字段全是标量，而 `UserMsg` 却带张量和嵌套对象？
**答案**：`UserReply` 只回传增量文本和结束标志，文本本身已是字符串；而 `UserMsg` 要把 tokenize 后的 token id 数组（张量）和采样参数（嵌套对象）交给后端，负载更重。

---

### 4.2 serialize_type：把对象递归压平成字典

#### 4.2.1 概念说明

`serialize_type` 是序列化的核心。它的目标只有一个：把一个 dataclass 对象变成一个**只含 dict / list / tuple / 标量 / bytes 的字典**——也就是 msgpack 能直接吃下的形状。它不依赖任何「字段清单」，而是直接读 `self.__dict__`（dataclass 实例的所有字段都存在这里），所以**任何**带 `__dict__` 的对象都能被它处理，无需逐字段手写转换。这种「反射式」写法意味着：你给消息类新增字段，序列化代码**一行都不用改**。

#### 4.2.2 核心流程

`serialize_type` 的逻辑可以拆成两段：

```
serialize_type(self):
    if self 是 torch.Tensor:
        断言是一维 → 存 {"__type__": "Tensor", "buffer": 裸字节, "dtype": 字符串}
    else:  # 普通 dataclass
        存 __type__ = 类名
        for k, v in self.__dict__.items():
            data[k] = _serialize_any(v)   # 递归处理每个字段的值
```

其中 `_serialize_any` 是「按值类型分发」的递归函数，伪代码如下：

```
_serialize_any(v):
    if v 是 dict:        每个值递归
    elif v 是 list/tuple: 逐个递归（保留原容器类型）
    elif v 是 标量/None/bytes:  原样返回
    else:                当作「嵌套对象」→ serialize_type(v)
```

关键点是**最后一个 `else`**：任何不属于基础类型的值（比如 `SamplingParams`、嵌套的另一个 dataclass），都被当成「该用 `serialize_type` 继续递归」的对象。这就实现了**任意深度嵌套**的序列化。

一个值得注意的副作用：序列化结果里会出现一个特殊的键 `"__type__"`。它是反序列化时「按名查类」的路标——所以**你的业务字段不能取名叫 `__type__`**，否则会撞键。

#### 4.2.3 源码精读

先看递归分发的 `_serialize_any`，[utils.py:9-17](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py#L9-L17)：

```python
def _serialize_any(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _serialize_any(v) for k, v in value.items()}
    elif isinstance(value, (list, tuple)):
        return type(value)(_serialize_any(v) for v in value)
    elif isinstance(value, (int, float, str, type(None), bool, bytes)):
        return value
    else:
        return serialize_type(value)
```

注意第 13 行用 `type(value)(...)` 重建容器，所以 tuple 序列化后还是 tuple、list 还是 list——这对像 `TokenizeMsg.text: str | List[Dict[str,str]]` 这种字段很重要（chat 模板里 message 列表会被逐层递归处理）。

再看主函数 `serialize_type`，[utils.py:20-35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py#L20-L35)：

```python
def serialize_type(self) -> Dict:
    serialized = {}
    if isinstance(self, torch.Tensor):
        assert self.dim() == 1, "we can only serialize 1D tensor for now"
        serialized["__type__"] = "Tensor"
        serialized["buffer"] = self.numpy().tobytes()
        serialized["dtype"] = str(self.dtype)
        return serialized

    serialized["__type__"] = self.__class__.__name__
    for k, v in self.__dict__.items():
        serialized[k] = _serialize_any(v)
    return serialized
```

- 第 24-29 行是张量专属分支：`self.numpy().tobytes()` 把 1D 张量降级成裸字节，`str(self.dtype)`（如 `"torch.int32"`）记录元素类型。第 25 行的 `assert` 就是「只支持 1D」的硬约束来源。
- 第 32 行用 `self.__class__.__name__` 取类名当路标（注意不是写死字符串，所以子类也能正确标记）。
- 第 33-34 行遍历 `__dict__`——这正是「加字段免改代码」的原因。

#### 4.2.4 代码实践

这是一个**阅读 + 推演型实践**，配合现成的测试 `tests/misc/test_serialize.py`。

1. **实践目标**：看清一个嵌套对象被压平后的字典长什么样。
2. **操作步骤**：打开 [test_serialize.py:14-31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/misc/test_serialize.py#L14-L31)。其中定义了一个自指类型 `A`（`z: List[A]` 还嵌套了一个 `A`），并构造 `x = A(10, "hello", [A(20, "world", [], t)], t)`。
3. **需要观察的现象**：在本机装好 `torch` 后运行 `pytest tests/misc/test_serialize.py -s`（或在 REPL 里手动跑 `serialize_type(x)`），重点看 `logger.info(data)` 打印出的嵌套字典结构。
4. **预期结果**：你会看到一个深度嵌套的 dict，最外层 `{"__type__": "A", "x": 10, "y": "hello", "z": [{"__type__": "A", ...}], "w": {"__type__": "Tensor", "buffer": b"\x01...", "dtype": "torch.int32"}}`。如果当前环境无 GPU/无 torch，则此步标注为**待本地验证**，但结构可由源码直接推得。

#### 4.2.5 小练习与答案

**练习 1**：给 `UserMsg` 新增一个字段 `priority: int = 0`，`serialize_type` 需要改动吗？
**答案**：不需要。`serialize_type` 遍历 `__dict__`，新字段会自动被序列化。

**练习 2**：如果一个字段的值是 `set([1,2,3])`（集合），`_serialize_any` 会怎么处理？会有什么问题？
**答案**：`set` 不属于 dict/list/tuple/标量/bytes，会落入 `else` 调用 `serialize_type(set对象)`；而 `set` 没有 `__class__.__name__` 对应的可调用构造，反序列化时 `cls_map["set"]` 查不到，会出错。所以消息字段应避免使用 set。

---

### 4.3 deserialize_type：从字典重建对象（含 globals 关键设计）

#### 4.3.1 概念说明

`deserialize_type` 是 `serialize_type` 的镜像：拿到一个带 `__type__` 标记的字典，按类名找到对应的类，再用字典里其余键作为关键字参数 `cls(**kwargs)` 重建对象。它同样是递归的——遇到嵌套的 `__type__` 字典就继续往下重建。这里有一个**初学者最容易踩坑**的设计点：「按类名找类」需要一张 `cls_map`（类名→类）表，而这张表是 `decoder` 所在模块的 `globals()`。这意味着——**被嵌套引用的类（如 `SamplingParams`）必须能在该模块 import 到**，否则反序列化会 `KeyError`。

#### 4.3.2 核心流程

```
deserialize_type(cls_map, data):
    type_name = data["__type__"]
    if type_name == "Tensor":
        从 buffer + dtype 重建 1D 张量
    else:
        cls = cls_map[type_name]            # 按名查类
        for k, v in data.items() (跳过 __type__):
            kwargs[k] = _deserialize_any(cls_map, v)   # 递归
        return cls(**kwargs)
```

`_deserialize_any` 的分发与 `_serialize_any` 对称：dict 里若有 `__type__` 就当对象处理，否则按基础类型原样返回。

#### 4.3.3 源码精读

先看张量重建分支，[utils.py:52-61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py#L52-L61)：

```python
def deserialize_type(cls_map: Dict[str, Type], data: Dict) -> Any:
    type_name = data["__type__"]
    if type_name == "Tensor":
        buffer = data["buffer"]
        dtype_str = data["dtype"].replace("torch.", "")
        np_dtype = getattr(np, dtype_str)
        assert isinstance(buffer, bytes)
        np_tensor = np.frombuffer(buffer, dtype=np_dtype)
        return torch.from_numpy(np_tensor.copy())
```

三个细节值得注意：

1. 第 57 行 `.replace("torch.", "")`：序列化时 `str(torch.int32)` 得到 `"torch.int32"`，这里去掉前缀变成 `"int32"`，再用 `getattr(np, "int32")` 拿到 numpy dtype。于是 torch dtype 和 numpy dtype 之间靠字符串「搭桥」。
2. 第 59 行断言 `buffer` 必须是 `bytes`——这是张量负载的契约。
3. 第 61 行 `.copy()`：`np.frombuffer` 返回的是**只读**视图（底层 buffer 不可写），必须 `.copy()` 出一份可写副本，`torch.from_numpy` 才能正常使用。

再看普通对象的重建，[utils.py:63-69](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py#L63-L69)：

```python
    cls = cls_map[type_name]
    kwargs = {}
    for k, v in data.items():
        if k == "__type__":
            continue
        kwargs[k] = _deserialize_any(cls_map, v)
    return cls(**kwargs)
```

第 63 行 `cls_map[type_name]` 就是「按名查类」。回到 4.1 节埋的伏笔：`backend.py` 的 `decoder` 传入的是 `backend.py` 的 `globals()`。`UserMsg` 的字段里有 `sampling_params: SamplingParams`，反序列化它需要 `cls_map["SamplingParams"]`——而 [backend.py:7](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/backend.py#L7) 恰好 `from minisgl.core import SamplingParams`，所以 `SamplingParams` 在 `backend.py` 的 `globals()` 里，查得到。同理 [tokenizer.py:6](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/tokenizer.py#L6) 也导入了 `SamplingParams`（因为 `TokenizeMsg` 也用它）。这就是「decoder 所在模块必须 import 到所有被嵌套引用的类」这条隐形契约的来源。

> 旁证：`frontend.py` **没有**导入 `SamplingParams`，因为 `UserReply` 根本不含嵌套对象，它的 `globals()` 不需要这张表。

#### 4.3.4 代码实践

这是一个**思想实验 + 阅读型实践**，用来巩固 `globals()` 契约。

1. **实践目标**：亲手验证「类名查表」失败会发生什么。
2. **操作步骤**：阅读 [utils.py:38-49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py#L38-L49) 的 `_deserialize_any`，确认它对「带 `__type__` 的 dict」一律交给 `deserialize_type`。
3. **需要观察的现象**：假设你新建一个消息类 `class MyMsg(BaseBackendMsg): extra: SomeType`，但忘了在 `backend.py` 里 `import SomeType`。
4. **预期结果**：序列化能成功（`serialize_type` 不需要 import），但反序列化时 `cls_map["SomeType"]` 抛 `KeyError`。这条实践无需运行即可由源码推得，结论是：**新增带新类型字段的消息时，记得在对应消息模块顶部 import 该类型**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `deserialize_type` 要把 `__type__` 这个键跳过（第 66-67 行）？
**答案**：`__type__` 只是「类名路标」，不是对象的业务字段，不能作为 `cls(**kwargs)` 的参数传入，否则会触发 `unexpected keyword argument`。

**练习 2**：`UserReply` 的 `decoder` 用的是 `frontend.py` 的 `globals()`，它里面没有 `SamplingParams`，会出问题吗？
**答案**：不会。`UserReply` 只有 `uid/incremental_output/finished` 三个标量字段，反序列化时不会出现 `SamplingParams` 这个类名，因此不需要它在 `globals()` 里。

---

### 4.4 tensor 的序列化与 encoder/decoder 如何接入 ZMQ

#### 4.4.1 概念说明

前两节讲了张量的「压平」与「重建」算法，本节把它们放回工程上下文：encoder/decoder 不是孤立存在的，它们被当作**回调函数**注入到 `ZmqPushQueue` / `ZmqPullQueue` 里，与 `msgpack`、ZMQ 三者串成一条「对象 → 字典 → 字节 → 网络 → 字节 → 字典 → 对象」的完整链路。本节同时回答一个常见疑问：**为什么不直接序列化多维张量？** 因为控制平面上传的只是 token id 这类 1D 整数数组，重型多维张量（KV cache、激活）走的是另一条 GPU 直连通道（NCCL），根本不经过这里。

#### 4.4.2 核心流程

一次跨进程「发—收」的完整流程：

```
发送端 put(obj):
    dict   = encoder(obj)              # serialize_type → 纯字典（含 bytes）
    bytes  = msgpack.packb(dict)       # 纯字典 → 紧凑字节
    zmq.socket.send(bytes)             # 字节进 ZMQ 管道

        ⋮  ZMQ 把字节搬到对端  ⋮

接收端 get():
    bytes  = zmq.socket.recv()         # 收到字节
    dict   = msgpack.unpackb(bytes)    # 字节 → 纯字典
    obj    = decoder(dict)             # deserialize_type → 原对象
```

注意 encoder/decoder 是**可替换的回调**：队列本身对消息类型一无所知，全靠构造时传入的 encoder/decoder 决定「这一端讲的是哪一族语言」。

#### 4.4.3 源码精读

看 `ZmqPushQueue.put` 如何调用 encoder，[mp.py:24-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L24-L26)：

```python
def put(self, obj: T):
    event = msgpack.packb(self.encoder(obj), use_bin_type=True)
    self.socket.send(event, copy=False)
```

第 25 行 `self.encoder(obj)` 就是对 4.1 节 `encoder` 的回调调用；`use_bin_type=True` 让 `bytes` 字段（张量裸字节）以二进制类型编码，这正是传张量所必需的。第 26 行 `copy=False` 用零拷贝发送，避免大字节串重复复制。

对应接收端 `ZmqPullQueue.get`，[mp.py:66-68](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L66-L68)：

```python
def get(self) -> T:
    event = self.socket.recv()
    return self.decoder(msgpack.unpackb(event, raw=False))
```

第 68 行 `raw=False` 让 msgpack 把二进制字段还原成 Python `bytes`（而不是 `bytearray`/`str`），这样 [utils.py:59](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py#L59) 的 `assert isinstance(buffer, bytes)` 才成立。可见「发送端 `use_bin_type=True` + 接收端 `raw=False`」是一对必须配套的开关。

最后看一处真实接线：scheduler rank0 收 backend 消息，[io.py:36-40](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L36-L40)：

```python
self._recv_from_tokenizer: Final = ZmqPullQueue(
    config.zmq_backend_addr,
    create=True,
    decoder=BaseBackendMsg.decoder,
)
```

`decoder=BaseBackendMsg.decoder` 这一句把「backend 族语言」绑死在这条队列上——之后这条管道收到的任何字节，都会被按 backend 族反序列化。多 rank 广播时，rank0 还会用 `get_raw()` / `put_raw()` 直接搬运**未拆封的原始字节**给其他 rank（见 [io.py:92-106](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/scheduler/io.py#L92-L106)），省去「rank0 解一次、其他 rank 再封一次」的重复开销——这也是为什么 `ZmqPullQueue` 额外提供了 `get_raw` / `decode` 两个方法（见 [mp.py:70-74](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/mp.py#L70-L74)）。

#### 4.4.4 代码实践

这是一个**最小调用型实践**，专注张量的往返正确性。

1. **实践目标**：验证一个 1D 张量经「序列化 → msgpack 往返 → 反序列化」后数值与 dtype 完全一致。
2. **操作步骤**：在装好 torch + msgpack 的环境里，写一段最小脚本（**示例代码**，非项目原有文件）：
   ```python
   import msgpack, torch
   from minisgl.message.utils import serialize_type, deserialize_type
   t = torch.tensor([10, 20, 30], dtype=torch.int32)
   packed = msgpack.packb(serialize_type(t), use_bin_type=True)
   back = deserialize_type({}, msgpack.unpackb(packed, raw=False))
   print(back, back.dtype)        # 期望 tensor([10,20,30]) torch.int32
   print(torch.equal(back, t))    # 期望 True
   ```
3. **需要观察的现象**：把 `raw=False` 改成 `raw=True` 再跑一次，观察 `assert isinstance(buffer, bytes)` 是否失败。
4. **预期结果**：`raw=False` 时往返成功；`raw=True` 时因 buffer 变成 `bytearray` 触发断言。这正好印证 4.4.3 的开关配对结论。若无 torch/msgpack 环境，则标注为**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么张量只支持 1D？多维张量要传怎么办？
**答案**：`serialize_type` 里有 `assert self.dim() == 1`，因为控制平面只传 token id 这类 1D 数组；真正的多维重型张量（KV cache、激活）不走 ZMQ，而走 GPU 间的 NCCL/PyNCCL 直连（见 [u1-l4](u1-l4-process-architecture.md) 的通信分工）。

**练习 2**：`put_raw` / `get_raw` 相比普通 `put` / `get` 省了什么？
**答案**：省掉了「在 rank0 解包成对象、再重新打包」这一来一回的 msgpack + 序列化开销，直接搬运原始字节给其他 rank，解码放到最终消费方做。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**给消息类加字段 + 写往返测试**」的任务。这正好对应大纲里本讲的 practice_task。

**任务背景**：假设产品要求 `UserReply` 在返回增量文本时，同时带上一个 `num_input_tokens`（输入 token 数，整数）和一个可选的 `logprobs`（1D 浮点张量，便于上游做分析）。你要扩展消息并保证它能正确跨进程往返。

**操作步骤**：

1. **新增字段**：参考 [frontend.py:25-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/frontend.py#L25-L29)，给 `UserReply` 增加两个字段（**示例代码**，仅用于练习，不要真的去改源码仓库）：
   ```python
   @dataclass
   class UserReply(BaseFrontendMsg):
       uid: int
       incremental_output: str
       finished: bool
       num_input_tokens: int = 0
       logprobs: torch.Tensor | None = None
   ```
   并在 `frontend.py` 顶部补 `import torch`（满足 4.3 节的 globals 契约——张量走的是 `__type__=="Tensor"` 分支而非类名查表，但 `torch.Tensor` 类型注解本身需要可解析）。

2. **写往返测试**：模仿 [test_serialize.py:32-35](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/misc/test_serialize.py#L32-L35) 的写法，**示例代码**：
   ```python
   import torch
   from minisgl.message import UserReply
   lp = torch.tensor([-0.1, -0.2, -0.3], dtype=torch.float32)
   u = UserReply(uid=7, incremental_output="hi", finished=False,
                 num_input_tokens=12, logprobs=lp)
   r = u.decoder(u.encoder())
   assert r.uid == 7 and r.num_input_tokens == 12 and r.finished is False
   assert torch.equal(r.logprobs, lp) and r.logprobs.dtype == torch.float32
   ```

3. **验证 tensor 还原**：重点确认 `logprobs` 这个新张量字段经往返后数值与 dtype 不变（这正是 practice_task 要求的「验证 tensor 字段能被正确还原」）。

4. **对照源码解释**：在测试通过后，用自己的话回答——为什么你**不需要**修改 `serialize_type` / `deserialize_type` 任何一行？（因为它们靠 `__dict__` 反射，新字段自动纳入。）

**预期结果**：测试通过，且你能解释「新增标量字段零成本、新增张量字段也零成本（只要它是 1D）」。`logprobs` 为 `None` 时，`_serialize_any` 会把 `None` 原样保留，往返后仍是 `None`——这是边界情况，值得在测试里额外加一条用例。若当前环境无 GPU/无 torch，则将运行步骤标注为**待本地验证**，但全部断言可由源码逻辑推得。

## 6. 本讲小结

- Mini-SGLang 把进程间消息按目的地分成 **backend / frontend / tokenizer 三族**，每族一个基类 + 一个 `BatchXxxMsg` 打包类，外部统一从 `message/__init__.py` 导入。
- 序列化引擎是 [utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/message/utils.py) 里的 `serialize_type` / `deserialize_type`：靠 `__dict__` 反射遍历字段，**新增字段免改代码**，支持任意深度嵌套。
- 张量被降级成「`buffer`(bytes) + `dtype`(字符串)」塞进纯字典，**只支持 1D**（`assert dim()==1`），因为控制平面只传 token id；多维重型张量走 NCCL 而非 ZMQ。
- 反序列化用 `cls_map = globals()` 「按类名查类」重建对象，带来一条隐形契约：**被嵌套引用的类必须在 decoder 所在模块 import 到**（如 `backend.py`、`tokenizer.py` 都导入了 `SamplingParams`）。
- encoder/decoder 是注入 `ZmqPushQueue`/`ZmqPullQueue` 的**回调**，与 `msgpack`、ZMQ 串成完整链路；`use_bin_type=True` 与 `raw=False` 必须成对使用才能正确传张量；多 rank 广播用 `get_raw`/`put_raw` 直接搬字节省去重复编解码。

## 7. 下一步学习建议

本讲解决了「进程之间用什么语言对话」。接下来：

- **向「消费者」走**：带着本讲建立的消息族地图，去读 [u3-l1 API Server](u3-l1-api-server.md)（看 frontend 族如何回流成 HTTP 响应、客户端断连如何发 `AbortMsg`）和 [u3-l2 Tokenizer Worker](u3-l2-tokenizer-worker.md)（看 `TokenizeMsg`/`DetokenizeMsg` 如何在同一个 worker 里分流处理）。
- **向「广播」走**：本讲只点了 `get_raw`/`put_raw`，多 rank 同步的完整时序在 [u4-l2 Scheduler I/O 与多 rank 广播](u4-l2-scheduler-io.md) 里展开，那里会解释为什么用 CPU `broadcast` 同步「消息条数」而非直接转发张量。
- **对照测试体系**：本讲引用的 [tests/misc/test_serialize.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/tests/misc/test_serialize.py) 是序列化的唯一回归测试，建议结合 [u11-l2 测试体系](u11-l2-tests-quality.md) 理解它在整个测试网中的位置。
