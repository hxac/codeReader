# HTTP API 服务层:FastAPI 端点与 UDS 部署

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `_init_api` 暴露的全部 REST 端点与 `ParameterServer` 方法的一一映射关系。
2. 读懂 update 端点中 `update_func` 闭包的构造过程,特别是 `update_url is None` 早退与 `inference_group_ranks` 下标过滤这两段逻辑。
3. 理解 `request_inference_to_update` 如何用 httpx 的 `HTTPTransport(uds=...)` 在「HTTP over TCP」与「HTTP over Unix domain socket」之间无缝切换。
4. 掌握 `python -m checkpoint_engine --uds ...` 的服务化启动方式,以及为什么服务化场景必须 `auto_pg=True`。
5. 会用 `_init_api` + `MagicMock` + `TestClient` 在纯 CPU 环境下对 API 层做接口测试。

## 2. 前置知识

本讲是全手册里「最像普通 Web 开发」的一讲,只涉及控制面,不搬任何权重字节。你需要以下概念:

- **控制面与数据面**:回顾 u1-l2 与 u3-l6 的结论——checkpoint-engine 的控制面(HTTP/ZMQ 小消息)与数据面(ZMQ + 设备 IPC + NCCL 广播的 TB 级权重流)是分离的。本讲的 HTTP API 是控制面的最外层外壳:外部编排器(orchestrator)通过它驱动 `ParameterServer` 的生命周期。
- **REST 与 FastAPI**:REST 是「用 HTTP 方法表达意图」的接口风格,例如 `POST` 创建资源、`DELETE` 删除资源。FastAPI 是 Python 的异步 Web 框架,用装饰器(如 `@app.post("/v1/metas")`)注册路由,并借助 pydantic 自动完成请求体的校验与反序列化。
- **pydantic 请求模型**:FastAPI 端点函数的参数标注了 pydantic 模型(如 `UpdateRequest`)时,框架会先校验请求体;校验失败直接返回 422,**端点函数体根本不会执行**——这是后面测试里 `ps_mock.load_metas.assert_not_called()` 能成立的原因。
- **Unix domain socket(UDS)**:同一台主机内两个进程间的通信端点,用文件系统路径(如 `/tmp/ps.sock`)标识,不占 TCP 端口、不经过网络协议栈。HTTP 也可以跑在 UDS 之上——httpx 与 uvicorn 都原生支持。
- **httpx 的 transport 抽象**:`httpx.Client(transport=...)` 允许替换底层连接方式。`httpx.HTTPTransport(uds=path)` 表示「仍发送标准 HTTP 报文,但连接建立到指定 Unix socket 而不是解析域名走 TCP」。
- **闭包(closure)**:内层函数引用外层函数的局部变量并随之被返回/传递。本讲 update 端点把 HTTP 请求字段「编译」成一个闭包 `update_func` 交给 `ps.update`,这是理解过滤逻辑的关键。

数学上本讲只涉及一个极简的下标过滤:\( S' = \{ (u_i, a_i) \mid i \in R \} \),即从「rank 序的 (uuid, 地址) 列表」中挑出下标落在集合 \( R \)(inference_group_ranks)里的元素。

## 3. 本讲源码地图

| 文件 | 行数概览 | 职责 |
| --- | --- | --- |
| `checkpoint_engine/api.py` | 约 110 行 | 本讲主角。`_init_api(ps)` 工厂函数生成 FastAPI 应用;`request_inference_to_update` 是调用推理引擎的 HTTP 客户端 |
| `checkpoint_engine/__main__.py` | 29 行 | `python -m checkpoint_engine` 的入口,`run_from_cli` 解析 `--uds` 参数并用 uvicorn 把 API 挂到 Unix socket 上 |
| `tests/test_api.py` | 140 行 | 纯 CPU 的 metas 端点测试:`MagicMock` 顶替 PS、`TestClient` 发请求、`TypeAdapter` 做 JSON 往返校验 |
| `checkpoint_engine/ps.py`(节选) | — | 被包装的对象:`get_metas`/`load_metas`/`gather_metas`/`update`/`register_checkpoint`/`unregister_checkpoint`,以及 `_bind_zmq_socket` 产出的 `socket_paths` |
| `examples/update.py`(节选) | — | 对照材料:`req_inference` 是「进程内直调版」的 `update_func`,与 HTTP 版形成对比 |

依赖关系:`__main__.py` → `api.py` → `ps.py` + `data_types.py`。fastapi、uvicorn、httpx 都是 pyproject.toml 的基础依赖(参见 [pyproject.toml:L8-L18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L8-L18)),不需要额外安装。

## 4. 核心概念与源码讲解

### 4.1 `_init_api`:FastAPI 应用工厂与端点总览

#### 4.1.1 概念说明

`_init_api` 是一个**应用工厂**:输入一个 `ParameterServer` 实例,输出一个绑定了该实例的 FastAPI 应用。它解决的问题是把 PS 的生命周期方法(register → gather_metas → update → unregister)暴露成 HTTP 端点,让**不生活在同一 Python 进程里**的外部编排器也能驱动权重更新。

为什么写成工厂函数而不是模块级全局 `app = FastAPI()`?两个原因:

1. **PS 实例是运行期才创建的**,而且每个 rank 一个(见 4.3);
2. **可测试性**:测试可以传入 `MagicMock()` 顶替真实 PS(真实 PS 需要 torch 分布式环境),这正是 `tests/test_api.py` 的做法。

端点通过**闭包捕获** `ps` 变量,没有用 FastAPI 的依赖注入——这是一种刻意的极简设计。

#### 4.1.2 核心流程

`_init_api(ps)` 的组装过程:

```text
_init_api(ps)
├── app = fastapi.FastAPI()
├── 定义请求模型:RegisterRequest(files)、UpdateRequest(ranks/update_url/...)
├── 定义 wrap_exception:统一把异常转成 500 响应
└── 注册 7 个路由(闭包捕获 ps)
    ├── POST   /v1/checkpoints/{name}/files   → ps.register_checkpoint(name, files=...)
    ├── DELETE /v1/checkpoints/{name}          → ps.unregister_checkpoint(name)
    ├── GET    /v1/healthz                    → 直接 200(不碰 ps)
    ├── POST   /v1/checkpoints/{name}/gather-metas → ps.gather_metas(name)
    ├── GET    /v1/metas                      → ps.get_metas()
    ├── POST   /v1/metas                      → ps.load_metas(metas)
    └── POST   /v1/checkpoints/{name}/update  → ps.update(name, update_func, ranks=...)
return app
```

端点与 PS 方法的完整映射表:

| HTTP 端点 | PS 方法 | 请求体 | 成功响应 |
| --- | --- | --- | --- |
| `POST /v1/checkpoints/{name}/files` | `register_checkpoint(name, files=...)` | `{"files": ["/path/a.safetensors", ...]}` | 200 空体 |
| `DELETE /v1/checkpoints/{name}` | `unregister_checkpoint(name)` | 无 | 200 空体 |
| `GET /v1/healthz` | 无 | 无 | 200 空体 |
| `POST /v1/checkpoints/{name}/gather-metas` | `gather_metas(name)` | 无 | 200 空体 |
| `GET /v1/metas` | `get_metas()` | 无 | 200 + JSON |
| `POST /v1/metas` | `load_metas(metas)` | `dict[int, MemoryBufferMetaList]` 的 JSON | 200 空体 |
| `POST /v1/checkpoints/{name}/update` | `update(name, update_func, ranks=...)` | `UpdateRequest` 的 JSON | 200 空体 |

注意两点:一是端点路径里不带 `use_shared_memory_pool`、`force` 等可选参数,HTTP 层是能力子集(共享内存池等高级特性只能进程内调用);二是 `unregister` 端点没暴露 `force` 参数,即 HTTP 触发的注销对共享池只会「让位」不会「释放」(回顾 u2-l5 的两种注销语义)。

#### 4.1.3 源码精读

工厂函数的骨架,请求模型定义在函数体内(每个 app 一份,避免全局命名空间污染):

[checkpoint_engine/api.py:L46-L57](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L46-L57)

这段代码创建 FastAPI 应用,并定义了两个请求模型:`RegisterRequest` 只有一个必填的 `files` 字符串列表;`UpdateRequest` 的五个字段全部有默认值,意味着 update 请求体甚至可以是空 JSON `{}`。

统一异常包装器:

[checkpoint_engine/api.py:L59-L65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L59-L65)

`wrap_exception` 接收一个无参函数(通常是 lambda),成功返回空的 200 `Response`,任何异常都被 `logger.exception` 记录完整堆栈后转成**状态码 500、响应体是异常文本字符串**的响应。这就是全部端点的错误处理约定:HTTP 层不做重试、不分类错误码,把 `str(e)` 原样透传给调用方。

三个生命周期端点,注意它们如何用 lambda 把「带参方法调用」适配成「无参函数」:

[checkpoint_engine/api.py:L67-L81](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L67-L81)

注册端点把路径参数 `checkpoint_name` 与请求体里的 `files` 拼给 `ps.register_checkpoint`;注销与 gather-metas 只传名字。`healthz` 则完全静态,给编排器做存活探测——与 examples/update.py 里轮询 vLLM 的 `/health`([examples/update.py:L33-L48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L33-L48))是同一种用法。

metas 的导出与导入端点,是 u3-l3 讲过的 `get_metas`/`load_metas` 的 HTTP 化:

[checkpoint_engine/api.py:L83-L93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L83-L93)

被包装的 PS 方法本身非常薄——`get_metas` 直接返回内部全局元数据表的引用,`load_metas` 整体替换全局表并重建远端 RDMA 拓扑:

[checkpoint_engine/ps.py:L292-L303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L292-L303)

这里藏着 u2-l1 的伏笔:返回类型标注 `dict[int, MemoryBufferMetaList]` 让 FastAPI 用 pydantic 序列化,而 `MemoryBufferMetaList` 内部的 `torch.dtype`、`torch.Size` 之所以能变成 JSON,靠的是 data_types.py 里定制的 `_TorchDtype`/`_TorchSize` 序列化器。两个端点的错误通道略有差异:`GET /v1/metas` 抛 `HTTPException`(JSON 格式的 detail),其余端点经 `wrap_exception` 返回纯文本——测试对两种形态都做了断言。

#### 4.1.4 代码实践

**实践目标**:用 `MagicMock` + `TestClient` 验证「端点 → PS 方法」的映射关系,证明 API 层只是薄封装、不夹带私货。

**操作步骤**(以下为示例代码,保存为 `tests/test_api_extra.py`,与现有测试同目录):

```python
# 示例代码:tests/test_api_extra.py(纯 CPU 可运行)
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from checkpoint_engine.api import _init_api


def test_healthz_is_static():
    ps = MagicMock()  # 不会被碰到
    assert TestClient(_init_api(ps)).get("/v1/healthz").status_code == 200
    ps.assert_not_called()  # MagicMock 上的任何属性访问都会被记录,此处应为零


def test_register_maps_to_ps_method():
    ps = MagicMock()
    client = TestClient(_init_api(ps))
    resp = client.post(
        "/v1/checkpoints/step-100/files",
        json={"files": ["/dev/shm/a.safetensors", "/dev/shm/b.safetensors"]},
    )
    assert resp.status_code == 200
    ps.register_checkpoint.assert_called_once_with(
        "step-100", files=["/dev/shm/a.safetensors", "/dev/shm/b.safetensors"]
    )

    resp = client.delete("/v1/checkpoints/step-100")
    assert resp.status_code == 200
    ps.unregister_checkpoint.assert_called_once_with("step-100")


def test_register_error_becomes_500_with_text_body():
    ps = MagicMock()
    ps.register_checkpoint.side_effect = RuntimeError("no such file")
    client = TestClient(_init_api(ps))
    resp = client.post("/v1/checkpoints/bad/files", json={"files": ["/x"]})
    assert resp.status_code == 500
    assert "no such file" in resp.text
```

**运行方式**:`pytest tests/test_api_extra.py -v`(这些测试不涉及 GPU,可不加 marker 过滤)。

**需要观察的现象**:

1. 三个测试全绿,且 `assert_called_once_with` 的参数与映射表逐字一致;
2. 第三个测试里 500 响应体就是 `"no such file"` 这个纯文本(不是 JSON),印证 `wrap_exception` 的行为。

**预期结果**:API 层对 PS 的调用参数完全来自路径参数与请求体,没有任何隐式改写。若你手滑把端点路径写错(如 `/v1/checkpoints/files`),TestClient 会返回 404 而不是 500——路由匹配先于一切。

**待本地验证**:以上断言基于源码静态推演,请在本地实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `tests/test_api.py` 可以在没有 GPU、没有 torchrun 的环境运行?它替换掉了什么?

答案:它用 `MagicMock` 顶替了真实的 `ParameterServer`(见 [tests/test_api.py:L50-L54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L50-L54)),`_init_api` 只会把端点参数转发给这个假对象并记录调用。真实 PS 构造需要设备探测、TCPStore 等分布式环境,而 API 层与这些完全解耦,这正是工厂 + 闭包设计的回报。

**练习 2**:`POST /v1/metas` 收到一个语法合法但不匹配 `MemoryBufferMetaList` 结构的 JSON(如 `{"0": {"foo": "bar"}}`)时,会发生什么?

答案:FastAPI 在调用端点函数**之前**就完成 pydantic 校验,返回 422,`ps.load_metas` 不会被调用。[tests/test_api.py:L100-L109](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L100-L109) 的 `test_load_metas_rejects_schema_mismatch` 断言了这两点。而如果 JSON 结构正确但 PS 内部报错,才会走 `wrap_exception` 的 500 通道([tests/test_api.py:L112-L123](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L112-L123))——422 是「框架拒绝」,500 是「业务失败」。

**练习 3**:`GET /v1/metas` 的响应体为什么能被 `POST /v1/metas` 原样接受?

答案:因为序列化与反序列化用的是同一套 pydantic 模型。测试 `test_round_trip_get_then_load`([tests/test_api.py:L126-L139](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L126-L139))把 GET 的字节直接作为 POST 的请求体,断言 200 且 `load_metas` 收到等价对象。这个「往返性」是 join 复用模式(用文件或 URL 在实例间搬运 metas)能成立的基础。

### 4.2 update 端点与 `request_inference_to_update`:过滤逻辑与 HTTP/UDS 双通道

#### 4.2.1 概念说明

update 端点是全 API 层最复杂的一个,因为它要做一次「翻译」:把 HTTP 请求里的**声明式字段**翻译成 PS 需要的**命令式回调**。

回顾 u3-l4/u3-l6:`ps.update(checkpoint_name, req_func, ranks=...)` 里的 `req_func` 是 PS 在数据面就绪后、在**独立线程**里回调的函数,入参是 `socket_paths`——一个按 rank 排序的 `(设备UUID, ZMQ地址)` 列表。PS 自己并不知道推理引擎的 HTTP 地址,通知引擎「来连我」这件事被外包给了 `req_func`。

在 examples/update.py 里,`req_func` 由每个 rank 在进程内自己组装(组首判断 + 连续切片);而在 HTTP 服务化模式下,调用方不在 PS 进程里,只能通过请求体字段描述「通知谁、通知哪些卡」,由端点现场组装闭包。两个声明式字段承担了这个职责:

- `update_url`:推理引擎的 URL。为 `None` 表示「本 rank 只参与集合通信,不要通知引擎」——这是编排器的分流开关:所有 rank 都要 POST update(集合通信要求全员到场),但只有部分 rank 的请求体里带 `update_url`。
- `inference_group_ranks`:下标列表,从全局 `socket_paths` 中挑出目标推理实例所占据的那部分 rank。`socket_paths` 的下标 i 与 rank i 一一对应(见 4.2.3),所以整数列表就是 rank 列表。

`request_inference_to_update` 则是闭包最终发出的那「一炮」:向推理引擎的 `/collective_rpc`(vLLM)或等价端点 POST 一个 RPC 描述,让引擎回调 worker 扩展的 `update_weights_from_ipc` 方法(u4-l1/u4-l2 的 REP 状态机由此启动)。

#### 4.2.2 核心流程

一次 update 请求的完整时间线:

```text
编排器                        PS 进程(api.py)                    PS 数据面(ps.py)         推理引擎(vLLM)
  │ POST /v1/.../update          │                                   │                        │
  │  {ranks, update_url,         │                                   │                        │
  │   inference_group_ranks,     │                                   │                        │
  │   timeout, uds}              │                                   │                        │
  ├──────────────────────────────►│ 构造 update_func 闭包(捕获 req)  │                        │
  │                              ├───────────────────────────────────►│ ps.update(...)        │
  │                              │                                   │ 绑定 ZMQ、切桶、广播…  │
  │                              │                                   │ socket_paths 就绪      │
  │                              │                                   │ ┌─新线程────────────┐ │
  │                              │                                   │ │ update_func(     │ │
  │                              │                                   │ │   socket_paths)  │ │
  │                              │   update_url=None? → 直接 return  │ │  ↓                 │ │
  │                              │   过滤: socket_paths[i], i∈R     │ │                    │ │
  │                              │   dict(...) 转字典                │ │                  POST /collective_rpc
  │                              │   request_inference_to_update ────┼─┼─────────────────────►│ update_weights_
  │                              │                                   │ │                    │   _ipc(socket_paths)
  │                              │                                   │ │                    │ worker connect ZMQ…
  │ ◄──────── 200 ───────────────┤◄──────────────────────────────────┤ 数据面继续/完成        │ 装载权重、ACK…
```

过滤逻辑(本讲的核心三行):

```text
if req.update_url is None: return                     # 本 rank 不负责通知引擎
if req.inference_group_ranks:                         # 声明了目标实例的 rank 集合
    socket_paths = [socket_paths[i] for i in R]       # 下标即 rank,挑出目标实例的地址
request_inference_to_update(url, dict(socket_paths), timeout, uds)   # 列表 → {uuid: 地址}
```

HTTP/UDS 双通道:`request_inference_to_update` 只有一个 `uds` 参数位——`uds=None` 时走常规 TCP HTTP;`uds="/path/sock"` 时 httpx 把连接改道到该 Unix socket。对调用方而言 URL 写法不变,这种「同机走 UDS、跨机走 TCP」的切换让 colocated 部署不必暴露任何端口。

#### 4.2.3 源码精读

update 端点全文,闭包在端点内现场定义:

[checkpoint_engine/api.py:L95-L106](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L95-L106)

逐行拆解:第 97 行 `update_func` 的类型标注 `list[tuple[str, str]]` 与 PS 侧完全一致;第 98-99 行是「不通知」早退;第 100-101 行是下标过滤(注意只有 `inference_group_ranks` **非空**才过滤,空列表等于「全量转发」,这正是 Broadcast 全员更新场景的默认值);第 102-104 行把过滤结果转成 `{设备UUID: ZMQ地址}` 字典发出;第 106 行把闭包连同 `ranks` 透传给 `ps.update`——`ranks` 决定 Broadcast 还是 P2P(u3-l4),与「通知哪个引擎实例」是两个正交的维度。

`socket_paths` 的来源与顺序保证(为什么下标就是 rank):

[checkpoint_engine/ps.py:L622-L630](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630)

`_bind_zmq_socket` 用 `self._global_device_uuids`(u3-l3 里 gather_metas 按 rank 顺序收集的全局设备 UUID 表)逐个生成抽象 UDS 地址,返回的列表天然「下标 = rank」。因此 API 层的整数过滤 `[socket_paths[i] for i in R]` 实际上就是「按 rank 挑地址」。

PS 在独立线程里回调闭包,与 ZMQ 发送首条句柄并行:

[checkpoint_engine/ps.py:L842-L849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L849)

这解释了一个重要事实:**`update_func` 的执行时机由 PS 决定,不在 HTTP 请求处理线程里**。编排器收到的 200 只代表 `ps.update` 同步部分返回,闭包已在数据面线程中完成(或正在执行)。

HTTP 客户端 `request_inference_to_update`:

[checkpoint_engine/api.py:L15-L43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L15-L43)

三个要点:第一,`httpx.Client(transport=httpx.HTTPTransport(uds=uds))`([L34](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L34))——uds 为 None 时是普通 TCP 客户端,否则连接被改道到 Unix socket,URL 仍以完整 http 形式给出(主机名只用于构造请求行与 Host 头);第二,请求体是 vLLM `/collective_rpc` 的标准 RPC 格式,`method` 指名要调用 worker 扩展的 `update_weights_from_ipc`(u4-l1 的 REP 状态机入口),`args[0]` 就是过滤后的地址字典,引擎会把字典分发给各卡 worker、按自己的设备 UUID 认领地址并 connect(u4-l2);第三,`timeout` 一鱼两吃——既写进 JSON 让引擎侧控制等待,又作为 httpx 的客户端超时。

与进程内版 `req_inference` 的对照(它是同一逻辑的「切片版」):

[examples/update.py:L77-L93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L77-L93)

| 维度 | examples/update.py `req_inference` | api.py update 端点 |
| --- | --- | --- |
| 谁来过滤 | 每个 rank 自己判断 `rank == src`(实例组首才发) | 编排器通过 `update_url=None` 点名谁发 |
| 过滤方式 | 连续切片 `socket_paths[src:src+P]` | 任意下标列表 `inference_group_ranks` |
| uds 选择 | 命令行 `--uds` 一次确定 | 请求体字段 `uds` 每次请求可变 |
| 闭包在哪里 | 调用方进程内组装 | 端点内组装,经 `ps.update` 传递 |

#### 4.2.4 代码实践

**实践目标**:在不启动任何推理引擎的情况下,抓住 `request_inference_to_update` 实际发出的 HTTP 报文,并验证 `inference_group_ranks` 的过滤效果。

**操作步骤一:本地回显服务器**(示例代码,保存为 `echo_rpc_demo.py`):

```python
# 示例代码:echo_rpc_demo.py(纯 CPU 可运行,只需项目自带依赖 httpx)
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from checkpoint_engine.api import request_inference_to_update


class EchoHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        print("== 收到 POST ==")
        print("path:", self.path)
        print("body:", json.dumps(body, indent=2))
        self.send_response(200)
        self.end_headers()


server = HTTPServer(("127.0.0.1", 0), EchoHandler)  # 端口 0 = 随机空闲端口
threading.Thread(target=server.serve_forever, daemon=True).start()
port = server.server_address[1]

request_inference_to_update(
    f"http://127.0.0.1:{port}/collective_rpc",
    {"gpu-uuid-0": "ipc://@checkpoint-engine-gpu-uuid-0-0.sock",
     "gpu-uuid-1": "ipc://@checkpoint-engine-gpu-uuid-1-0.sock"},
    timeout=5.0,
)
```

运行 `python echo_rpc_demo.py`。

**需要观察的现象**:终端打印出的 `path` 是 `/collective_rpc`;`body` 含三个键——`method` 为 `"update_weights_from_ipc"`,`args` 是单元素列表(即地址字典),`timeout` 为 `5.0`。进程正常退出(无异常)。

**操作步骤二:验证端点闭包的过滤逻辑**(示例代码,追加到 `tests/test_api_extra.py`):

```python
# 示例代码:tests/test_api_extra.py(续)
from checkpoint_engine import api


def test_update_filters_socket_paths_by_group_ranks(monkeypatch):
    ps = MagicMock()
    captured = {}

    def fake_req(url, socket_paths, timeout=300.0, uds=None):
        captured.update(url=url, socket_paths=socket_paths, timeout=timeout, uds=uds)

    # update_func 通过模块全局名查找该函数,monkeypatch 后即被拦截
    monkeypatch.setattr(api, "request_inference_to_update", fake_req)

    client = TestClient(_init_api(ps))
    resp = client.post(
        "/v1/checkpoints/step-100/update",
        json={
            "ranks": [0, 1],
            "update_url": "http://127.0.0.1:8000/collective_rpc",
            "inference_group_ranks": [1, 2],
            "timeout": 30.0,
        },
    )
    assert resp.status_code == 200

    # ranks 原样透传给 ps.update
    assert ps.update.call_args.kwargs["ranks"] == [0, 1]
    # ps.update 只是记录了闭包;真正执行闭包的是 PS 数据面线程,这里手动驱动
    update_func = ps.update.call_args.args[1]
    update_func([("u0", "a0"), ("u1", "a1"), ("u2", "a2"), ("u3", "a3")])

    assert captured["socket_paths"] == {"u1": "a1", "u2": "a2"}  # 只留下下标 1、2
    assert captured["timeout"] == 30.0
```

**需要观察的现象**:`ps.update` 恰好被调用一次,`ranks` 与请求体一致;手动执行闭包后,`fake_req` 收到的字典只含 `u1`、`u2` 两项——尽管传入了 4 个地址。若把 `inference_group_ranks` 改成 `[]` 重跑,`captured["socket_paths"]` 会是全量 4 项(空列表不触发过滤);再把 `update_url` 改成 `None`,`fake_req` 根本不会被调用(早退分支)。

**预期结果**:三个分支(None 早退、空列表全量、非空列表过滤)行为与 4.2.2 的伪代码逐一吻合。

**待本地验证**:两段示例均未在编写本讲时实际运行,请本地执行确认输出。

#### 4.2.5 小练习与答案

**练习 1**:8 卡单机、两个 vLLM 实例各占 4 卡(实例 A 占 rank 0-3,实例 B 占 rank 4-7)。编排器只想更新实例 B,应该怎样构造发往各 rank 的 update 请求体?

答案:所有 8 个 rank 都要收到 POST(集合通信要求全员参与 `ps.update`);其中 rank 4(或实例 B 的任一指定通知者,通常选组首 rank 4)的请求体设 `update_url` 指向实例 B,`inference_group_ranks=[4,5,6,7]`,`ranks` 按更新方式选择——Broadcast 全员更新就留空,只想让 B 的权重变化可配合 `ranks=[4,5,6,7]` 走 P2P;其余 rank 的请求体 `update_url` 留 `None`,`update_func` 早退,不产生任何 HTTP 调用。

**练习 2**:为什么过滤要写成「任意下标列表」而不是像 examples/update.py 那样的连续切片?

答案:进程内版依赖 `rank // P * P` 推算本实例的连续区间,前提是「每个实例恰好占一段连续 rank」。HTTP 版面向更一般的编排场景:目标实例的 rank 可能不连续(例如异构混部、扩容后的碎片分布),`inference_group_ranks` 用显式列表消除一切区间假设。两者底层都是「从 rank 序的 socket_paths 里挑出目标实例的地址」。

**练习 3**:如果 `update_url` 填了,但对应的推理引擎迟迟不响应,会发生什么?

答案:客户端层面,httpx 会在 `timeout`(默认 300 秒,可由请求体覆盖)后抛 `httpx.RequestError`/超时异常;该异常发生在 PS 的 req_func 线程里,沿 u3-l4 讲过的错误传播链走:线程内异常 → PS 捕获 → `ret_code` 全体约减 → 全集群统一下发 `RuntimeError` 提前退出。API 层的 `request_inference_to_update` 自己只负责 `resp.raise_for_status()` 把 4xx/5xx 转成异常([checkpoint_engine/api.py:L43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L43)),不做重试。

### 4.3 `run_from_cli`:UDS 部署与服务启动

#### 4.3.1 概念说明

`__main__.py` 让包可以用 `python -m checkpoint_engine` 启动(Python 约定: `-m` 执行包内的 `__main__.py`)。它回答三个问题:

1. **监听在哪**:只支持 UDS,不提供 TCP 端口选项(`--uds` 为空直接断言失败)。这符合服务化部署的典型姿态——PS 与编排器/推理引擎同机,控制面不需要、也不应该暴露到网络上。
2. **PS 怎么建**:`ParameterServer(auto_pg=True)`。服务化场景下 HTTP 请求可能间隔数小时才来一次,进程组必须「按需建、用完毁」,否则上一次 update 的进程组会残留成脏状态,这正是 auto_pg 的语义(u3-l1 讲过:True 时 update/gather_metas 开头若未初始化会自动 `init_process_group`,结束时自动销毁)。
3. **怎么跑起来**:`uvicorn.run(app, uds=..., timeout_keep_alive=60)`——把 `_init_api(ps)` 生成的应用交给 uvicorn,监听在 Unix socket 上。

#### 4.3.2 核心流程

```text
python -m checkpoint_engine --uds /tmp/ps-rank0.sock
├── 解析参数:只有一个 --uds
├── 打印启动日志:参数 + MASTER_ADDR/MASTER_PORT 环境变量
├── assert --uds 非空(服务只认 UDS)
├── ps = ParameterServer(auto_pg=True)   # 读取 RANK/WORLD_SIZE 等环境变量
└── uvicorn.run(_init_api(ps), uds=..., timeout_keep_alive=60)
    └── 事件循环:每个 HTTP 请求 → 路由 → 闭包 → ps 方法
        (gather-metas / update 按需 init/destroy 进程组)
```

部署形态:通常由 torchrun 拉起 N 个进程,每个进程是一个 rank、各自监听一个 UDS(如 `/tmp/ps-rank{RANK}.sock`),编排器持有全量 socket 清单,对每个 rank 都要发请求(因为 gather_metas/update 是集合通信)。消费方(编排器或 join 模式下的新实例)用 `httpx` 的 `HTTPTransport(uds=...)` 即可访问这些端点。

#### 4.3.3 源码精读

`__main__.py` 全文只有 29 行,核心如下:

[checkpoint_engine/__main__.py:L10-L24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__main__.py#L10-L24)

逐行拆解:第 10 行 `@logger.catch(reraise=True)` 给整个启动流程兜底——任何异常先记完整日志再重新抛出,进程以非零码退出(对 systemd/k8s 这类监督器友好);第 14-17 行只有一个 `--uds` 参数;第 18-20 行把 `MASTER_ADDR`/`MASTER_PORT` 打进日志,方便排查分布式会合问题;第 22 行断言强制 UDS 部署;第 23 行构造 PS,`auto_pg=True` 是服务化的硬要求——对照 [checkpoint_engine/ps.py:L596-L597](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L596-L597)(请求到来时惰性初始化)与 [checkpoint_engine/ps.py:L610-L615](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L610-L615)(finally 里销毁子组与全局组并清显存缓存),可以看到一次 HTTP update 请求前后进程组的完整生死;第 24 行 `uvicorn.run` 阻塞式启动,`timeout_keep_alive=60` 让空闲超过 60 秒的 HTTP 连接被服务端关闭,避免编排器侧的连接池长期挂着过期连接。

入口守卫与模块导入的延迟策略:

[checkpoint_engine/__main__.py:L1-L12](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__main__.py#L1-L12) 与 [checkpoint_engine/__main__.py:L27-L28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__main__.py#L27-L28)

注意第 12 行把 `import uvicorn` 放在函数体内——只装了基础包而没装 uvicorn 的用户,`import checkpoint_engine` 不会因此失败(与 u1-l3 讲过的可选依赖延迟导入策略一致)。第 27-28 行是标准的 `python -m` 入口守卫。

顺带一提:examples/update.py 的 `--uds` 参数与这里的 `--uds` **不是同一个 socket**——前者是「vLLM 引擎监听的 UDS」,PS 侧作为客户端用 `httpx.HTTPTransport(uds=...)` 去连(见 [examples/update.py:L38-L42](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L38-L42) 的 `check_vllm_ready`);后者是「本 PS 服务监听的 UDS」。一个是客户端旋钮、一个是服务端旋钮,方向相反。

#### 4.3.4 代码实践

**实践目标**:在纯 CPU 环境验证启动参数校验与 UDS 部署形态,并亲手完成一次「UDS 上的 HTTP 调用」。

**操作步骤**:

1. 在仓库根目录运行 `python -m checkpoint_engine --help`,观察输出;
2. 运行 `python -m checkpoint_engine`(不带任何参数),观察退出行为;
3. 阅读下面这段「最小 UDS 服务 + UDS 客户端」示例代码(示例代码,保存为 `uds_roundtrip_demo.py`):

```python
# 示例代码:uds_roundtrip_demo.py(纯 CPU 可运行)
import subprocess
import sys
import tempfile
import time

import httpx

uds_path = tempfile.mktemp(prefix="ce-demo-", suffix=".sock")
proc = subprocess.Popen(
    [sys.executable, "-c", f"""
from checkpoint_engine.__main__ import run_from_cli
import sys
sys.argv = ["checkpoint-engine", "--uds", "{uds_path}"]
run_from_cli()
"""],
)
time.sleep(3)  # 等待 uvicorn 起来(真实场景应轮询 healthz)

# 通过 UDS 访问 healthz:URL 仍写 http,连接由 transport 改道到 Unix socket
resp = httpx.Client(transport=httpx.HTTPTransport(uds=uds_path)).get(
    "http://localhost/v1/healthz", timeout=5.0
)
print("status:", resp.status_code)
proc.terminate()
```

**需要观察的现象**:

- 步骤 1:`--help` 列出且仅列出 `--uds` 一个参数;
- 步骤 2:日志先打印 `Parameter Server args=Namespace(uds=None) ...`(若设置了 MASTER_ADDR/MASTER_PORT 环境变量也会一并打印),随后 `logger.catch` 记录 AssertionError(`assert args.uds and len(args.uds) > 0`),进程非零退出——断言发生在 `ParameterServer` 构造**之前**,所以不需要 GPU;
- 步骤 3:打印 `status: 200`,证明 HTTP 确实可以跑在 Unix socket 上,且客户端写法与普通 HTTP 几乎无异。

**预期结果**:三步现象如上。步骤 3 依赖 `ParameterServer(auto_pg=True)` 构造成功——它需要 `RANK`/`WORLD_SIZE`/`MASTER_ADDR`/`MASTER_PORT` 环境变量以及设备探测(u3-l1),在纯 CPU 机器上构造行为**待本地验证**;若构造因环境缺失而失败,可退而求其次:把 `run_from_cli` 中的 `ParameterServer(...)` 换成 `MagicMock()` 再跑通 UDS 往返,验证目标不变。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `run_from_cli` 里是 `assert args.uds and len(args.uds) > 0` 而不是给 UDS 一个默认路径?

答案:Unix socket 路径与部署拓扑强相关(每个 rank 一个,路径通常含 RANK),任何默认值都可能让多个 rank 绑到同一路径互相踩踏;而 TCP 部署有天然的端口分配手段。作者选择「不指定就报错」,把路径决策权完全交给部署方。此外注意 Python 的 falsy 陷阱:空字符串会让 `args.uds` 为假,所以 `and len(args.uds) > 0` 与前置 `args.uds` 一起同时排除 `None` 与空串(仓库最近的 fix: rank=0 falsy-value 修复处理的就是同类「0/空值被误判」问题)。

**练习 2**:假设编排器用 keep-alive 长连接每 10 分钟调一次 update,`timeout_keep_alive=60` 会有什么影响?

答案:两次调用间隔远超 60 秒,服务端会主动关闭空闲连接。编排器下一次复用该连接时会收到连接已关闭,httpx 通常会自动重连,但若使用不感知重试的客户端则可能报错。60 秒是「宁可让客户端重连、也不让半开连接堆积」的保守选择——服务端持有大量僵尸连接的代价(文件描述符、内存)高于偶发重连。

**练习 3**:如果想让 PS 同时服务跨机调用(编排器在另一台主机上),最少要改哪里?

答案:改 [checkpoint_engine/__main__.py:L24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__main__.py#L24),把 `uds=args.uds` 换成 `host`/`port` 参数(uvicorn 原生支持)。`_init_api` 生成的应用本身与传输无关,无需改动——这正是「应用工厂」设计的又一好处。但要注意:UDS-only 是有意的安全默认,开放 TCP 意味着任何能路由到该端口的人都能触发权重更新,需自行加访问控制。

## 5. 综合实践

**任务:把本讲三个模块串成一条「模拟编排器」全链路**(纯 CPU 可完成)。

场景设定:你是一个编排器,手里有一个(用 MagicMock 模拟的)PS 服务和一个(用本地回显服务器模拟的)推理引擎。请编写 `test_mini_orchestrator.py`,按真实生命周期的顺序完成:

1. `GET /v1/healthz` 确认服务存活;
2. `POST /v1/checkpoints/step-100/files` 注册(用 MagicMock 记录调用);
3. `POST /v1/checkpoints/step-100/gather-metas` 收集元数据;
4. `GET /v1/metas` 取回全局元数据,断言与 `ps_mock.get_metas.return_value` 序列化等价;
5. `POST /v1/checkpoints/step-100/update`,带 `update_url` 指向本地回显服务器(复用 4.2.4 步骤一的 `EchoHandler`)、`inference_group_ranks=[0,1]`,随后手动驱动捕获到的 `update_func`,断言回显服务器收到的方法名是 `update_weights_from_ipc` 且地址字典只剩两个条目;
6. `DELETE /v1/checkpoints/step-100` 注销。

骨架提示(示例代码):

```python
# 示例代码:test_mini_orchestrator.py 骨架
order = []
ps = MagicMock()
ps.get_metas.return_value = {...}  # 仿照 tests/test_api.py 的 _make_meta 构造
client = TestClient(_init_api(ps))
# ...按 1-6 逐步请求,每个断言通过后 order.append("步骤名")
assert order == ["healthz", "register", "gather", "metas", "update", "unregister"]
```

**验收标准**:

- 六个步骤全部 2xx;
- `order` 列表证明调用顺序与 u1-l1 讲过的生命周期「注册 → 收集 → 更新 → 注销」一致;
- 回显服务器捕获的请求体与 4.2.4 的观察结果一致。

**待本地验证**:整个脚本可在纯 CPU 环境运行,请实际执行并核对三项验收标准。

## 6. 本讲小结

- `_init_api(ps)` 是应用工厂:7 个 REST 端点通过闭包捕获把请求一一映射到 `ParameterServer` 方法,`wrap_exception` 把所有业务异常统一转成「500 + 异常文本」,pydantic 校验失败则是框架层的 422(PS 方法不会被调用)。
- update 端点把声明式请求体「编译」成命令式闭包 `update_func`:`update_url is None` 早退分流「谁去通知引擎」,`inference_group_ranks` 对 rank 序的 `socket_paths` 做下标过滤,`ranks` 字段则独立决定 Broadcast/P2P——通知维度与传输维度正交。
- `request_inference_to_update` 发出 `/collective_rpc` 风格的 RPC(method 为 `update_weights_from_ipc`,args 为 `{设备UUID: ZMQ地址}` 字典),`httpx.HTTPTransport(uds=...)` 一个参数位完成 TCP/UDS 双通道切换。
- `update_func` 的真正执行者是 PS 数据面的独立线程([ps.py:L842-L847](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L847)),不在 HTTP 请求线程里;200 响应只代表同步编排完成。
- `python -m checkpoint_engine --uds ...` 以 UDS-only 方式服务化部署,`auto_pg=True` 保证进程组随请求「按需建、用完毁」,`timeout_keep_alive=60` 及时回收空闲连接。
- `MagicMock` + `TestClient` + `monkeypatch`(`api.request_inference_to_update`)三件套可以在纯 CPU 环境覆盖 API 层几乎全部逻辑,现有 `tests/test_api.py` 是范本。

## 7. 下一步学习建议

本讲补完了「控制面外壳」。接下来两条线任选:

1. **横向补齐 P2P 与编排**:第 5 单元的 [u5-l5](./u5-l5-p2p-store-and-rdma.md) 会展开 `p2p_store_addr`/`rdma_device` 这些 metas 字段背后的 mooncake 传输引擎;第 6 单元的 [u6-l2](./u6-l2-examples-update-walkthrough.md) 与 [u6-l3](./u6-l3-join-and-metas-reuse.md) 分别精读 `examples/update.py` 的完整编排与 `--metas-url`/`/v1/metas` 支撑的 join 复用模式——后者正是本讲 `GET/POST /v1/metas` 端点的实战舞台。
2. **纵向下潜测试体系**:[u6-l1](./u6-l1-test-suite.md) 系统梳理 tests 目录,本讲的 `test_api_extra.py` 实践可以直接作为你给项目贡献的第一个测试的起点(先读 `tests/test_api.py` 的风格约定)。

建议动手前重跑一遍 `pytest tests/test_api.py -v`,对照本讲 4.1 的映射表,逐个测试说出它验证的是表格里的哪一行。
