# Python Store API：MooncakeDistributedStore

## 1. 本讲目标

本讲是「Store 主题」的 Python 实践篇。在 [u5-l1](u5-l1-store-architecture.md) 里我们建立了「控制面（Master）/ 数据面（Client↔Client 直传）」的整体认知，本讲要把这套架构落到一个**你在 Python 里真正会调用的类**——`MooncakeDistributedStore`。学完本讲你应该能够：

1. 用 `MooncakeDistributedStore` 完成「初始化 → 写入 → 读取 → 关闭」的完整生命周期，并说清 `setup()` 的每个参数控制什么。
2. 区分**字节级 API**（`put`/`get`/`put_batch`/`get_batch`）与**张量级 API**（`put_tensor`/`get_tensor`/`batch_*_tensor`），并理解张量在线缆上的存储格式 `[TensorMetadata][数据]`。
3. 掌握 `ReplicateConfig`：`replica_num`（副本数）、`with_soft_pin`/`with_hard_pin`（软/硬 pin）、`preferred_segments`（指定落在哪些段），并知道**带副本的张量写入必须用 `pub_tensor` 而不是 `put_tensor`**。
4. 分清两种「把内存登记给 Store」的方式：`mount_segment`/`allocate_and_mount_segment`（把内存**贡献进全局资源池**，扮演 store server）与 `register_buffer`（把用户缓冲区**注册为可被 RDMA 零拷贝读写的客户端内存**）。

> 本讲聚焦 Python 绑定层（pybind11）。底层 Master 分配/淘汰/租约的细节见 [u5-l2](u5-l2-master-service.md)，控制面/数据面时序见 [u5-l1](u5-l1-store-architecture.md)。

## 2. 前置知识

本讲默认你已经具备：

- **Store 总体架构（依赖 u5-l1）**：知道 `Put` = `PutStart`（控制面，问 Master 要副本位置）+ TE 直传数据 + `PutEnd`（控制面，标记可读）；`Get` = `GetReplicaList`（控制面）+ TE 直传。本讲所有 API 最终都落到这条链路上。
- **Python 基础**：了解 `bytes`、`memoryview`、`ctypes`（用来拿裸指针）、以及可选的 `torch`（张量 API 需要）。
- **pybind11 直觉（非必需）**：C++ 通过 pybind11 把类和方法暴露给 Python。本讲会反复看到一种固定模式——「持有 GIL 做参数校验/元数据提取，释放 GIL（`py::gil_scoped_release`）做实际的网络/传输」，这是 Python 绑定性能的关键。

### 一个关键直觉：Store 里的「内存」有两种身份

读本讲最容易卡住的地方是「内存」这个词的歧义。先建立这个直觉：

| | `mount_segment` / `allocate_and_mount_segment` | `register_buffer` |
|---|---|---|
| 角色 | **store server**：把这段内存**贡献进集群全局资源池**，别人写的副本可能落到这里 | **client**：把这段内存**注册**为「我自己读写时可以零拷贝直传」的缓冲区 |
| 谁来分配 | `mount_segment` 用你给的文件路径（mmap）；`allocate_and_mount_segment` 由 Store 内部分配 | 你自己分配（如 `ctypes` / `MooncakeHostMemAllocator`） |
| 典型配套 | 让本节点「能被别人写入/读取」 | 配合 `get_into` / `put_from` 做零拷贝 |

一句话：**`mount` 是「我把货架空出来给别人放货」，`register` 是「我把自己的周转箱登记一下，方便我自己取货/发货时零拷贝」**。4.5 节会展开。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [mooncake-integration/store/store_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp) | Python 绑定的**全部实现**（pybind11） | 本讲的主战场：`MooncakeDistributedStore` 类、`ReplicateConfig`、所有 `put/get/*_tensor/mount_segment/register_buffer` 方法都在这里 |
| [mooncake-store/include/real_client.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h) | `RealClient` 类声明（C++ 层客户端） | Python 绑定薄薄一层，真正干活的是 `RealClient` 的 `mountSegment`/`register_buffer`/`get_into`/`put_from` 等 |
| [mooncake-store/include/replica.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h) | `ReplicateConfig` 结构体定义 | 副本数、软/硬 pin、`preferred_segments` 的权威定义 |
| [mooncake-store/include/rpc_types.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h) | 控制面 RPC 的响应类型 | `create_copy_task`/`query_task` 等返回的 `QueryTaskResponse`、副本迁移相关的响应结构 |

调用链记忆：**Python 调用 → `MooncakeStorePyWrapper`（GIL 管理薄封装）→ `RealClient`（真正发控制面 RPC + 编排数据面）→ Master + Transfer Engine**。本讲主要在「Python → Wrapper」这一层讲解，必要时下探到 `RealClient` 的方法签名。

## 4. 核心概念与源码讲解

### 4.1 MooncakeDistributedStore：Python 入口与生命周期

#### 4.1.1 概念说明

`MooncakeDistributedStore` 是用户在 Python 里 `from mooncake.store import MooncakeDistributedStore` 拿到的类。但在 C++ 源码里，它的真名是 `MooncakeStorePyWrapper`——一个专门为 Python 设计的「包装器」，内部持有一个 `shared_ptr<PyClient>`（实际是 `RealClient` 或 `DummyClient`）。它的职责只有两件：

1. **管 GIL**：所有会阻塞的网络/传输调用，都在 C++ 里 `py::gil_scoped_release` 释放 GIL，让其它 Python 线程能继续跑；需要碰 Python 对象时再 `acquire`。
2. **做 Python 友好的类型转换**：把 `py::buffer`、`py::object`（torch tensor）、`py::dict` 转成 C++ 的 `std::span`、`TensorMetadata`、`ConfigDict`。

一个 store 实例的生命周期是：**构造 → `setup()`（连 Master + 挂段）→ 反复 put/get → `close()`（拆段 + 释放）**。

#### 4.1.2 核心流程

```
MooncakeDistributedStore()        # 构造一个空 wrapper（还没连任何东西）
        │
        ▼
store.setup(local_hostname,       # ① init_real_client()：创建 RealClient 并注册到 ResourceTracker
            metadata_server,      # ② setup_real()：连 Master、挂全局段、注册本地 buffer
            global_segment_size,  #    global_segment_size = 贡献进池的「货架」大小
            local_buffer_size,    #    local_buffer_size  = 自己的「周转箱」大小
            protocol, ...)        #    protocol = tcp/rdma/...
        │
        ▼
反复 put / get / put_tensor / ... # ③ 业务调用（每个调用内部 release GIL）
        │
        ▼
store.close()                     # ④ tearDownAll()：拆段、断开；store_ 被 reset
```

`setup` 有两个重载：一个是**位置参数版**（参数顺序固定），一个是**字典配置版**（传一个 `dict`，键名见下方源码注释）。

#### 4.1.3 源码精读

类本身在模块注册块里通过 pybind11 暴露为 `MooncakeDistributedStore`：

```cpp
py::class_<MooncakeStorePyWrapper>(m, "MooncakeDistributedStore")
    .def(py::init<>())
```
—— [mooncake-integration/store/store_py.cpp:2011-2012](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2011-L2012)：把 C++ 的 `MooncakeStorePyWrapper` 暴露成 Python 的 `MooncakeDistributedStore`，默认无参构造。

位置参数版 `setup`（注意默认值：段/buffer 都是 16MB，协议默认 `tcp`）：

```cpp
.def("setup",
    [](MooncakeStorePyWrapper &self, const std::string &local_hostname,
       const std::string &metadata_server,
       size_t global_segment_size = 1024 * 1024 * 16,
       size_t local_buffer_size = 1024 * 1024 * 16,
       const std::string &protocol = "tcp", ...) {
        auto real_client = self.init_real_client();   // 创建 RealClient
        ...
        return real_client->setup_real(local_hostname, metadata_server,
            global_segment_size, local_buffer_size, protocol, ...);
    }, ...)
```
—— [mooncake-integration/store/store_py.cpp:2019-2050](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2019-L2050)：`setup` 先 `init_real_client()`，再委托 `RealClient::setup_real` 完成连接与挂段。返回 0 表示成功。

字典版 `setup` 把 `dict` 转成 `ConfigDict`（所有值都 `py::str()` 成字符串），再调 `setup_internal(config)`；支持的键在注释里列得很全（`local_hostname`/`metadata_server` 必填，`global_segment_size`/`protocol`/`enable_ssd_offload` 等可选）：

```cpp
.def("setup",
    [](MooncakeStorePyWrapper &self, const py::dict &config_dict) {
        auto real_client = self.init_real_client();
        ConfigDict config;
        for (auto item : config_dict) {
            config[py::str(item.first)] = py::str(item.second);  // 全部转字符串
        }
        auto result = real_client->setup_internal(config);
        return result.has_value() ? 0 : static_cast<int>(result.error());
    }, py::arg("config"), ...)
```
—— [mooncake-integration/store/store_py.cpp:2051-2081](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2051-L2081)：字典配置版，注释列出了全部支持的键。

`close` 拆段并把内部的 `store_` 智能指针清空：

```cpp
.def("close", [](MooncakeStorePyWrapper &self) {
    if (!self.store_) return 0;
    int rc = self.store_->tearDownAll();
    self.store_.reset();
    return rc;
})
```
—— [mooncake-integration/store/store_py.cpp:2202-2208](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2202-L2208)：`close` 调 `tearDownAll()` 并 `reset()`，之后这个 store 实例就不可用了。

> **异常安全小知识**：`init_real_client()` 会把新建的 client 注册进一个全局 `ResourceTracker`（[real_client.h:31-66](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L31-L66)）。即使 Python 进程被信号杀死，它也会兜底清理，避免段残留。

#### 4.1.4 代码实践

**实践目标**：用两种方式（位置参数 / 字典）初始化同一个 store，观察 `setup` 的返回值。

**操作步骤**：

1. 先按 [u5-l1](u5-l1-store-architecture.md) 启动 Master（默认 `127.0.0.1:50051`）和 metadata server（如 etcd `127.0.0.1:2379` 或 HTTP `127.0.0.1:8080/metadata`）。
2. 运行下面这段「示例代码」：

```python
# 示例代码（非项目自带，需自行创建运行）
from mooncake.store import MooncakeDistributedStore

store = MooncakeDistributedStore()

# 方式 A：位置参数（注意 global_segment_size 与 local_buffer_size 的含义不同）
rc = store.setup(
    local_hostname="127.0.0.1:12345",
    metadata_server="127.0.0.1:2379",   # 换成你实际的 metadata server
    global_segment_size=256 * 1024 * 1024,  # 贡献进池的「货架」256MB
    local_buffer_size=64 * 1024 * 1024,     # 自己的「周转箱」64MB
    protocol="tcp",
    device_name="lo",                        # tcp 时网卡名可填 lo
    master_server_addr="127.0.0.1:50051",
)
print("setup(A) rc =", rc)

store.close()

# 方式 B：字典配置（键名见源码注释）
store2 = MooncakeDistributedStore()
rc2 = store2.setup({
    "local_hostname": "127.0.0.1:12346",
    "metadata_server": "127.0.0.1:2379",
    "protocol": "tcp",
    "master_server_addr": "127.0.0.1:50051",
})
print("setup(B) rc =", rc2)
store2.close()
```

**需要观察的现象**：`setup` 成功返回 `0`；若 Master/metadata 没起，会返回非 0 错误码（负数，对应 `ErrorCode`）。

**预期结果**：两行都打印 `rc = 0`。**实际运行结果待本地验证**（依赖你本地是否起了 Master/metadata 服务；若未起服务，预期返回负数错误码而非崩溃）。

#### 4.1.5 小练习与答案

**练习 1**：忘记调 `setup` 就直接 `put`，会发生什么？

> **参考答案**：`is_client_initialized()` 返回 false，方法会 `LOG(ERROR) << "Client is not initialized"` 并返回负的错误码（如 `to_py_ret(ErrorCode::INVALID_PARAMS)`），不会崩溃。参见字节级 `put` 绑定里的初始化检查（[store_py.cpp:2667-2670](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2667-L2670)）。

**练习 2**：`global_segment_size` 和 `local_buffer_size` 有什么区别？设错会怎样？

> **参考答案**：`global_segment_size` 是本节点**贡献给全局资源池**的内存（store server 角色，别人的副本会写到这里）；`local_buffer_size` 是**本客户端自己的零拷贝缓冲区**（client 角色，`get_into`/`put_from` 用）。前者影响集群可用容量，后者影响本端能零拷贝读写的最大对象尺寸。设得太小可能导致大对象写不下或副本分配失败（Master 返回 `NO_AVAILABLE_HANDLE`）。

---

### 4.2 字节级 put / get / batch

#### 4.2.1 概念说明

最基础的 API 把 Store 当成一个「分布式 `dict[str, bytes]`」：`put(key, value)` 写一段原始字节，`get(key)` 读回 `bytes`。`put_batch`/`get_batch` 是它们的批量版，一次 RPC 处理多个 key，摊薄控制面往返开销。

注意一个细节：**`get` 在 key 不存在时不是抛异常，而是返回长度为 0 的 `bytes`**（源码里的 `kNullString = pybind11::bytes("\\0", 0)`）。调用方要自己判断。

#### 4.2.2 核心流程

以 `put` 为例（`get` 对称）：

```
Python: store.put(key, value, config=ReplicateConfig())
   │  buf.request() 拿到 buffer_info（不拷贝，直接拿指针 + 长度）
   ▼
release GIL ──▶ store_->put(key, span<char>{info.ptr, info.size}, config)
   │                       │
   │                       └─▶ RealClient: PutStart(控制面) → TE 写(数据面) → PutEnd(控制面)
   ▼
acquire GIL ──▶ 返回 int（0 成功，负数错误码）
```

`put_batch` 同理，只是把多个 `(key, span)` 一次性传给 `store_->put_batch`。

#### 4.2.3 源码精读

`put` 绑定——注意它用 `py::buffer` 接收任意「单维、itemsize=1」的字节类对象（`bytes`/`bytearray`/`memoryview` 都行），并**零拷贝**地包成 `std::span`：

```cpp
.def("put",
    [](MooncakeStorePyWrapper &self, const std::string &key,
       py::buffer buf, const ReplicateConfig &config = ReplicateConfig{}) {
        py::buffer_info info = buf.request(/*writable=*/false);
        py::gil_scoped_release release;                       // 传输期间释放 GIL
        return self.store_->put(
            key, std::span<const char>(static_cast<char *>(info.ptr),
                                       static_cast<size_t>(info.size)),
            config);
    }, py::arg("key"), py::arg("value"), py::arg("config") = ReplicateConfig{})
```
—— [mooncake-integration/store/store_py.cpp:2721-2735](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2721-L2735)：`put` 把 Python buffer 零拷贝成 `std::span`，释放 GIL 后调 `RealClient::put`。

`get` 绑定在 wrapper 类方法里，返回 `bytes`，找不到时返回空 `bytes`：

```cpp
pybind11::bytes get(const std::string &key) {
    if (!is_client_initialized()) { ... return pybind11::bytes("\\0", 0); }
    const auto kNullString = pybind11::bytes("\\0", 0);
    {
        py::gil_scoped_release release_gil;
        auto buffer_handle = store_->get_buffer(key);   // 拿到 BufferHandle
        if (!buffer_handle) { acquire_gil; return kNullString; }   // key 不存在 → 空 bytes
        py::gil_scoped_acquire acquire_gil;
        return pybind11::bytes((char *)buffer_handle->ptr(), buffer_handle->size());
    }
}
```
—— [mooncake-integration/store/store_py.cpp:423-443](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L423-L443)：`get` 调 `get_buffer` 拿到 `BufferHandle`，再拷成 `bytes`；`get_buffer` 返回空指针 → 返回空 `bytes`。

`put_batch` 把多个 buffer 一次性转成 `vector<span>` 再调 `put_batch`（[store_py.cpp:2764-2787](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2764-L2787)）；`get_batch` 对应 `batch_get_buffer`（[store_py.cpp:445-473](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L445-L473)），返回 `list[bytes]`，任一 key 缺失对应位置是空 `bytes`。

> **为什么 `get` 不抛异常？** 因为在大批量读取（如推理时一次取几十个 KV cache 张量）里，逐个 try/except 代价高；返回哨兵值（空 `bytes` / `None`）更便于批量处理。

#### 4.2.4 代码实践

**实践目标**：写一条字节、读回校验；再对比 `get_batch` 在「部分 key 缺失」时的返回。

**操作步骤**：运行下列「示例代码」（前置：store 已 `setup` 成功）：

```python
# 示例代码
store.put("greeting", b"hello mooncake")
assert store.get("greeting") == b"hello mooncake"
assert store.get("not-exist-key") == b""    # 不存在 → 空 bytes，不抛异常

# batch：第二个 key 故意不存在
store.put("k1", b"v1")
res = store.get_batch(["k1", "k1-missing"])
print(res)   # 预期 [b'v1', b'']
```

**需要观察的现象**：`get("not-exist-key")` 返回空 `bytes` 而非抛异常；`get_batch` 对缺失 key 返回空 `bytes` 占位。

**预期结果**：打印 `[b'v1', b'']`。**运行结果待本地验证**（依赖 Master/metadata 服务）。

#### 4.2.5 小练习与答案

**练习 1**：用 `memoryview(bytearray(...))` 作为 `put` 的 `value` 有什么好处？

> **参考答案**：`py::buffer` 对 `bytes`/`bytearray`/`memoryview` 一视同仁，都通过 `buf.request()` 直接拿到底层指针，**零拷贝**包成 `std::span`。用 `bytearray`/`memoryview` 还能在写入侧就避免 `bytes` 的不可变拷贝。

**练习 2**：`put_batch` 返回的是「整体成功/失败」还是一个「逐 key 结果」？

> **参考答案**：字节级 `put_batch` 返回**单个 int**（[store_py.cpp:2783-2785](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2783-L2785)），整体成功才 0。相比之下，张量级 `batch_put_tensor` 返回**逐 key 的 `list[int]`**（见 4.3），设计上不一致，使用时要留意。

---

### 4.3 张量级 put_tensor / get_tensor

#### 4.3.1 概念说明

字节级 API 不知道你存的是张量。张量级 API（`put_tensor`/`get_tensor`/`batch_put_tensor`/`batch_get_tensor`）在字节流前面**多塞一段 `TensorMetadata` 头部**，记录 dtype、shape、数据长度，这样 `get_tensor` 才能把裸字节**重建**成一个形状/dtype 都正确的 `torch.Tensor`。在线缆上，一个张量对象的布局是：

```
┌──────────────────┬──────────────────────────┐
│  TensorMetadata  │       tensor 数据         │
│  (dtype/shape/…) │  (numel * element_size)   │
└──────────────────┴──────────────────────────┘
```

这一节要理解两件事：写入时如何从 torch tensor 提取出 `(metadata, 数据指针, 大小)`；读回时如何把缓冲区重新拼回 torch tensor。

#### 4.3.2 核心流程

**写入（`put_tensor`）**：

```
torch.Tensor
   │  extract_tensor_info(): 校验类名含 "Tensor" → contiguous() → data_ptr/numel/element_size
   │                            → get_tensor_dtype(dtype) → build_full_tensor_metadata()
   ▼
PyTensorInfo{ data_ptr, tensor_size, metadata }
   │  构造 values = [ span(metadata) , span(data) ]   ← 两段，零拷贝
   ▼
release GIL ──▶ store_->put_parts(key, values, config)   ← put_parts 支持多段拼成一个对象
   ▼
返回 int（0 成功）
```

**读取（`get_tensor`）**：

```
release GIL ──▶ store_->get_buffer(key)  → BufferHandle（含 [metadata][data]）
   ▼
acquire GIL ──▶ buffer_to_tensor():
   ① memcpy 出 TensorMetadata 头
   ② ParseTensorMetadata 解析 data_offset / data_bytes
   ③ 用 array_creators[dtype] 把数据包成 numpy 数组（可 take_ownership 零拷贝）
   ④ reshape 成原 shape → torch.from_numpy → 必要时 view 成 bfloat16/float16/float8
   ▼
返回 torch.Tensor（或 None）
```

#### 4.3.3 源码精读

`extract_tensor_info` 负责把一个 Python 对象「翻译」成 `PyTensorInfo`，关键步骤是先断言它确实是 torch tensor，再做 `contiguous()` + 取 `data_ptr/numel/element_size` + 解析 dtype：

```cpp
PyTensorInfo extract_tensor_info(const py::object &tensor, ...) {
    ...
    if (!(tensor.attr("__class__").attr("__name__").cast<std::string>()
            .find("Tensor") != std::string::npos)) {
        LOG(ERROR) << "Input ... is not a PyTorch tensor";
        return info;                       // 不是 tensor → 返回空 info
    }
    py::object contiguous_tensor = tensor.attr("contiguous")();
    info.owner = contiguous_tensor;        // 持有引用，防止数据被回收
    info.data_ptr = contiguous_tensor.attr("data_ptr")().cast<uintptr_t>();
    size_t numel = contiguous_tensor.attr("numel")().cast<size_t>();
    size_t element_size = contiguous_tensor.attr("element_size")().cast<size_t>();
    info.tensor_size = numel * element_size;
    ...
    info.metadata = build_full_tensor_metadata(tensor, dtype_enum, info.tensor_size);
    return info;
}
```
—— [mooncake-integration/store/store_py.cpp:90-145](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L90-L145)：从 torch tensor 抽取 `data_ptr`、`tensor_size` 和 `TensorMetadata`。注意它把 `contiguous_tensor` 存进 `info.owner`，保证底层内存在 `put_parts` 期间不被 Python 回收。

`put_tensor_impl` 把 metadata 和数据拼成两段 span，释放 GIL 后调 `put_parts`：

```cpp
int put_tensor_impl(const std::string &key, pybind11::object tensor,
                    const ReplicateConfig &config) {
    auto info = extract_tensor_info(tensor, key);
    if (!info.valid()) return to_py_ret(ErrorCode::INVALID_PARAMS);
    std::vector<std::span<const char>> values;
    values.emplace_back(reinterpret_cast<const char *>(&info.metadata),
                        sizeof(TensorMetadata));                  // 第 1 段：头部
    append_tensor_payload_span(values, info.data_ptr, info.tensor_size);  // 第 2 段：数据
    py::gil_scoped_release release_gil;
    int ret = store_->put_parts(key, values, config);
    ...
}
```
—— [mooncake-integration/store/store_py.cpp:674-693](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L674-L693)：张量被拆成「metadata + 数据」两段，零拷贝交给 `put_parts`。

`get_tensor` 读回时调 `get_buffer` 拿到整段缓冲，再交给 `buffer_to_tensor` 重建。重建函数会按 dtype 选择对应的 numpy 创建器，并对 `bfloat16/float16/float8_*` 做 `view` 修正（因为这些 dtype 在 numpy 侧没有直接对应）：

```cpp
pybind11::object buffer_to_tensor(BufferHandle *buffer_handle, char *usr_buffer, int64_t data_length) {
    ...
    memcpy(&metadata, exported_data, sizeof(TensorMetadata));   // 读头部
    auto parsed = ParseTensorMetadata(exported_data, total_length);
    ...
    py::object np_array = array_creators[dtype_index](exported_data, data_offset,
                                                      tensor_size, take_ownership);
    if (ndim > 0) np_array = np_array.attr("reshape")(tensor_shape_tuple(metadata));
    pybind11::object tensor = torch_module().attr("from_numpy")(np_array);
    if (dtype_enum == TensorDtype::BFLOAT16) tensor = tensor.attr("view")(torch_module().attr("bfloat16"));
    ...
}
```
—— [mooncake-integration/store/store_py.cpp:162-283](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L162-L283)：把 `[metadata][data]` 缓冲区重建为 torch tensor，含 dtype view 修正。

> **关于 dtype 支持**：`extract_tensor_info` 用 `get_tensor_dtype` 把 torch dtype 映射到 `TensorDtype` 枚举，未知 dtype 会被拒绝（`TensorDtype::UNKNOWN`）。张量最大维度由 `kMaxTensorDims` 限制（超出会报错）。

#### 4.3.4 代码实践

**实践目标**：写入一个多维 float32 张量并读回，校验 shape、dtype、数值都一致；顺便验证 `batch_get_tensor` 的逐项返回。

**操作步骤**：运行下列「示例代码」（前置：store 已 `setup`，且装有 `torch`）：

```python
# 示例代码
import torch
from mooncake.store import MooncakeDistributedStore

store = MooncakeDistributedStore()
store.setup("127.0.0.1:12345", "127.0.0.1:2379",
            256 * 1024 * 1024, 64 * 1024 * 1024, "tcp", "lo",
            "127.0.0.1:50051")

t = torch.tensor([[1.0, 2.0], [3.0, 4.0]], dtype=torch.float32)
assert store.put_tensor("w", t) == 0

back = store.get_tensor("w")
print("shape:", tuple(back.shape), "dtype:", back.dtype)
assert tuple(back.shape) == (2, 2)
assert back.dtype == torch.float32
assert torch.allclose(t, back)

# batch 读取（第二个 key 不存在 → None）
store.put_tensor("w2", torch.zeros(3))
print(store.batch_get_tensor(["w", "w2-missing"]))   # 预期 [Tensor, None]
```

**需要观察的现象**：读回的张量 shape `(2,2)`、dtype `float32`、数值与原张量 `allclose`；`batch_get_tensor` 对缺失 key 返回 `None`。

**预期结果**：`shape: (2, 2) dtype: torch.float32`，且断言全过。**运行结果待本地验证**（依赖 Master/metadata 服务与 torch 安装）。这个流程与项目自带测试 [mooncake-wheel/tests/test_put_get_tensor.py:67-91](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/tests/test_put_get_tensor.py#L67-L91) 的断言一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `extract_tensor_info` 要先 `tensor.contiguous()` 再取 `data_ptr`？

> **参考答案**：非连续张量（如 transpose/slice 的结果）的 `data_ptr` 指向的内存不是「按 shape 紧密排列」的，直接按 `numel*element_size` 连续拷贝会拿到错乱的数据。先 `contiguous()` 得到一个紧密排列的副本，`data_ptr` 才能被当作一段连续字节安全传输。

**练习 2**：`get_tensor` 对一个「不存在的 key」返回什么？对一个「存在但内容损坏（metadata 非法）」的 key 呢？

> **参考答案**：key 不存在 → `get_buffer` 返回空指针 → `buffer_to_tensor` 返回 `py::none()`（[store_py.cpp:163-164](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L163-L164)）。内容损坏（如 `ParseTensorMetadata` 失败、ndim 越界、dtype 未知）→ 同样记 `LOG(ERROR)` 并返回 `py::none()`（[store_py.cpp:199-238](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L199-L238)）。两种情况都返回 `None`，调用方需自行判断。

---

### 4.4 ReplicateConfig：副本数、软/硬 pin、preferred_segments

#### 4.4.1 概念说明

`ReplicateConfig` 是写入时告诉 Master「这个对象要怎么放、放几份、能不能被淘汰」的配置。它在 Python 里是一个可空构造、字段直接赋值的简单对象：

```python
from mooncake.store import ReplicateConfig
cfg = ReplicateConfig()
cfg.replica_num = 2
cfg.with_hard_pin = True
```

它的 C++ 定义在 `replica.h`，Python 侧通过 `def_readwrite` 把每个字段一一暴露。核心字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `replica_num` | `size_t`（默认 1） | 内存副本数。`replica_num=2` 表示对象在**两个不同段**各放一份 |
| `nof_replica_num` | `size_t`（默认 0） | NVMe-oF（RDMA SSD）副本数，多级存储用 |
| `with_soft_pin` | `bool` | 软 pin：VIP 对象，淘汰时**优先保留**（但仍可能被淘汰） |
| `with_hard_pin` | `bool` | 硬 pin：对象**永不被淘汰** |
| `preferred_segments` | `list[str]` | 指定副本应落在哪些段（长度必须等于 `replica_num`） |
| `preferred_nof_segments` | `list[str]` | NoF 副本的目标段 |
| `prefer_alloc_in_same_node` | `bool` | 尽量把副本分配在本节点 |
| `data_type` | `ObjectDataType` | 对象分类（KVCACHE/TENSOR/WEIGHT/…），影响调度与淘汰 |

#### 4.4.2 核心流程

写入时 `ReplicateConfig` 随 `put*`/`pub_tensor` 一路传到 `RealClient`，最终在 `PutStart` 阶段被 Master 读取：

```
Python: cfg.replica_num = 2
   │
   ▼  (随 put/pub_tensor 的 config 参数下传)
RealClient::put / put_parts / put_tensor_impl(key, ..., config)
   │
   ▼
Master::PutStart(key, size, config)   ← Master 读 replica_num/preferred_segments/soft_hard_pin
   │   按 config 分配 2 个副本（在不同段），返回 2 个 replica 描述符
   ▼
TE 把数据分别写到 2 个副本 → PutEnd 标记全部 COMPLETE
```

**副本数的代价**：`replica_num = R` 意味着写入要复制 R 份数据、占用 R 倍空间；好处是读时有 R 个副本可选（读带宽可聚合），且任意 R-1 个副本所在节点宕机仍可读（见 4.5 实践里的容错测试）。

#### 4.4.3 源码精读

C++ 侧 `ReplicateConfig` 的权威定义：

```cpp
struct ReplicateConfig {
    size_t replica_num{1};
    size_t nof_replica_num{0};
    bool with_soft_pin{false};
    bool with_hard_pin{false};                 // Hard pin: 对象不能被淘汰
    std::vector<std::string> preferred_segments{};   // 优先分配到的段
    std::string preferred_segment{};           // 已弃用：单个优先段（向后兼容）
    std::vector<std::string> preferred_nof_segments{};
    bool prefer_alloc_in_same_node{false};
    ObjectDataType data_type{ObjectDataType::UNKNOWN};
    std::optional<std::vector<std::string>> group_ids{};
    ...
};
```
—— [mooncake-store/include/replica.h:81-97](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L81-L97)：字段默认值即「1 副本、不 pin、不指定段」。

Python 绑定把每个字段用 `def_readwrite` 暴露（可读可写），并提供了 `__str__` 方便调试：

```cpp
py::class_<ReplicateConfig>(m, "ReplicateConfig")
    .def(py::init<>())
    .def_readwrite("replica_num", &ReplicateConfig::replica_num)
    .def_readwrite("nof_replica_num", &ReplicateConfig::nof_replica_num)
    .def_readwrite("with_soft_pin", &ReplicateConfig::with_soft_pin)
    .def_readwrite("with_hard_pin", &ReplicateConfig::with_hard_pin)
    .def_readwrite("preferred_segments", &ReplicateConfig::preferred_segments)
    .def_readwrite("preferred_nof_segments", &ReplicateConfig::preferred_nof_segments)
    .def_readwrite("preferred_segment", &ReplicateConfig::preferred_segment)
    .def_readwrite("prefer_alloc_in_same_node", &ReplicateConfig::prefer_alloc_in_same_node)
    .def_readwrite("data_type", &ReplicateConfig::data_type)
    .def_readwrite("group_ids", &ReplicateConfig::group_ids)
    .def("__str__", [](const ReplicateConfig &config) { ... });
```
—— [mooncake-integration/store/store_py.cpp:1741-1760](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1741-L1760)：`ReplicateConfig` 的 Python 绑定，全部字段 `def_readwrite`。

`preferred_segments` 有一个**校验约束**：如果你填了 `preferred_segments`，它的长度必须等于 `replica_num`，否则 `pub_tensor` 会拒绝：

```cpp
int validate_replicate_config(const ReplicateConfig &config = ReplicateConfig{}) {
    if (!config.preferred_segments.empty() &&
        config.preferred_segments.size() != config.replica_num) {
        LOG(ERROR) << "Preferred segments size (...) must match replica_num (...)";
        return to_py_ret(ErrorCode::INVALID_PARAMS);
    }
    return 0;
}
```
—— [mooncake-integration/store/store_py.cpp:1471-1482](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1471-L1482)：`preferred_segments.size()` 必须等于 `replica_num`。这个校验在 `pub_tensor`/`batch_pub_tensor` 等「带 config 的发布」入口被调用。

> **软 pin vs 硬 pin**（详见 [u5-l2](u5-l2-master-service.md) 的租约三层）：硬 pin（`with_hard_pin`）= 永不淘汰；软 pin（`with_soft_pin`）= VIP 对象，淘汰时优先保留但极端情况下仍可能被淘汰。普通对象两者皆 false。

#### 4.4.4 代码实践

**实践目标**：构造不同 `ReplicateConfig`，观察 `preferred_segments` 长度不匹配时的返回码；并打印 `__str__` 看序列化结果。

**操作步骤**：运行下列「示例代码」（**不需要**起 Master，因为下面的非法配置在客户端校验阶段就被拦下）：

```python
# 示例代码
from mooncake.store import MooncakeDistributedStore, ReplicateConfig
import torch

store = MooncakeDistributedStore()
store.setup("127.0.0.1:12345", "127.0.0.1:2379",
            256 * 1024 * 1024, 64 * 1024 * 1024, "tcp", "lo",
            "127.0.0.1:50051")

# 合法：replica_num=2，不指定段
cfg_ok = ReplicateConfig()
cfg_ok.replica_num = 2
print(cfg_ok)   # 调 __str__

# 非法：replica_num=2，但只给了 1 个 preferred_segment
cfg_bad = ReplicateConfig()
cfg_bad.replica_num = 2
cfg_bad.preferred_segments = ["only-one-seg-id"]
rc = store.pub_tensor("x", torch.zeros(4), cfg_bad)
print("pub_tensor(illegal cfg) rc =", rc)   # 预期负数（INVALID_PARAMS）
```

**需要观察的现象**：`cfg_ok` 打印出形如 `ReplicateConfig: { replica_num: 2, ... }` 的字符串；非法配置下 `pub_tensor` 直接返回负数错误码，**不会**发起 PutStart（客户端校验阶段 `validate_replicate_config` 就拦下了）。

**预期结果**：`pub_tensor(illegal cfg) rc =` 一个负数。**运行结果待本地验证**（即便 Master 没起，这一步也会因客户端校验返回负数；若 `setup` 本身失败则需先解决服务依赖）。

#### 4.4.5 小练习与答案

**练习 1**：`preferred_segments` 和 `preferred_segment`（单数）有什么关系？该用哪个？

> **参考答案**：`preferred_segment`（单数）是**已弃用**字段，仅保留向后兼容（[replica.h:88-89](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L88-L89)）；新代码应使用 `preferred_segments`（复数，`list[str]`），长度等于 `replica_num`，分别指定每个副本的目标段。

**练习 2**：把一个权重张量设成 `with_hard_pin=True`，对 Master 的淘汰线程意味着什么？

> **参考答案**：硬 pin 的对象**永不被淘汰**（[replica.h:85](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/replica.h#L85)）。Master 的 `BatchEvict`/`EvictionThreadFunc` 会跳过它。代价是这段空间被永久占用，可能挤占其它对象——适合「常驻权重」这类不该被驱逐的对象，但不适合易变的 KV cache。

---

### 4.5 pub_tensor：带副本配置的张量发布（put_tensor vs pub_tensor）

#### 4.5.1 概念说明

这是本讲**最容易踩坑**的一个区别：

- **`put_tensor(key, tensor)`**：只接受 `(key, tensor)` 两个参数，**内部固定用默认 `ReplicateConfig{}`**（即 `replica_num=1`）。你想传 `config`？没这个参数。
- **`pub_tensor(key, tensor, config=ReplicateConfig{})`**：接受可选的 `config`，会先 `validate_replicate_config(config)` 校验，再写入。

所以**「用 `ReplicateConfig` 指定副本数写入张量」必须用 `pub_tensor`，不能用 `put_tensor`**。两者底层都调 `put_tensor_impl`，差别只在「config 从哪来」和「是否做 `validate_replicate_config`」。

同理：批量场景用 `batch_pub_tensor(keys, tensors, config)` 而非 `batch_put_tensor(keys, tensors)`。

#### 4.5.2 核心流程

```
put_tensor(key, tensor):
   └─▶ put_tensor_impl(key, tensor, ReplicateConfig{})     ← 固定默认 config，replica_num=1

pub_tensor(key, tensor, config=ReplicateConfig{}):
   ├─▶ validate_replicate_config(config)                   ← 校验 preferred_segments 长度等
   └─▶ put_tensor_impl(key, tensor, config)                ← 用用户给的 config
                │
                └─▶ extract_tensor_info → [metadata span, data span]
                      → store_->put_parts(key, values, config)
                            → Master::PutStart(按 config 分配 R 个副本) → TE 写 R 份 → PutEnd
```

#### 4.5.3 源码精读

`put_tensor`——注意签名里**没有** `config` 参数，硬编码传 `ReplicateConfig{}`：

```cpp
int put_tensor(const std::string &key, pybind11::object tensor) {
    if (!is_client_initialized() || use_dummy_client_) { ... return INVALID_PARAMS; }
    return put_tensor_impl(key, tensor, ReplicateConfig{});  // 默认 config（replica_num=1）
}
```
—— [mooncake-integration/store/store_py.cpp:695-703](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L695-L703)：`put_tensor` 无 config 参数，固定单副本。

`pub_tensor`——接受 `config`，先校验再写入：

```cpp
int pub_tensor(const std::string &key, pybind11::object tensor,
               const ReplicateConfig &config = ReplicateConfig{}) {
    if (!is_client_initialized() || use_dummy_client_) { ... return INVALID_PARAMS; }
    int validate_result = validate_replicate_config(config);
    if (validate_result) return validate_result;            // 校验失败直接返回
    return put_tensor_impl(key, tensor, config);            // 用用户 config
}
```
—— [mooncake-integration/store/store_py.cpp:1522-1534](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1522-L1534)：`pub_tensor` 多了 `config` 参数和 `validate_replicate_config`。Python 绑定见 [store_py.cpp:2274-2276](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2274-L2276)。

> **dummy client 不支持张量**：两个方法开头都检查 `use_dummy_client_`，dummy 模式下直接返回 `INVALID_PARAMS`。dummy client 是一种不连 Master、用本地共享内存模拟的简化模式（`setup_dummy`），主要用于单进程测试。

#### 4.5.4 代码实践

**实践目标**：用 `pub_tensor` + `ReplicateConfig(replica_num=2)` 写一个张量，在**另一个 store 实例**上读回，校验形状与数值一致；再验证「写端宕机后，副本端仍可读」的容错性。

**操作步骤**：本实践需要**两个 store 实例**（两个不同的 `local_hostname:port`，各自 `setup` 后都贡献了内存段，Master 才能把 2 个副本分到不同段）。这等价于项目测试 [mooncake-wheel/tests/test_replicated_distributed_object_store.py:267-305](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/tests/test_replicated_distributed_object_store.py#L267-L305) 的张量版。运行下列「示例代码」：

```python
# 示例代码
import torch
from mooncake.store import MooncakeDistributedStore, ReplicateConfig

META = "127.0.0.1:2379"
MASTER = "127.0.0.1:50051"
SEG = 256 * 1024 * 1024
BUF = 64 * 1024 * 1024

def make_store(host):
    s = MooncakeDistributedStore()
    rc = s.setup(host, META, SEG, BUF, "tcp", "lo", MASTER)
    assert rc == 0, f"setup failed: {rc}"
    return s

writer = make_store("127.0.0.1:12345")
reader = make_store("127.0.0.1:12346")     # 第二个实例，提供第二个副本落脚点

t = torch.arange(12, dtype=torch.float32).reshape(3, 4)

cfg = ReplicateConfig()
cfg.replica_num = 2                        # ← 2 副本：关键
rc = writer.pub_tensor("replicated_tensor", t, cfg)
assert rc == 0, f"pub_tensor failed: {rc}"

# 从「另一个端」读取，校验形状与数值
back = reader.get_tensor("replicated_tensor")
assert back is not None
assert tuple(back.shape) == (3, 4)
assert back.dtype == torch.float32
assert torch.equal(t, back)
print("replicated read-back OK:", tuple(back.shape), back.dtype)
```

**需要观察的现象**：`pub_tensor` 返回 0；`reader.get_tensor` 拿到的张量 shape `(3,4)`、dtype `float32`、数值与原张量 `equal`。若把 `replica_num` 改成 `1`，单副本也能读，但失去容错；若把 reader 换成 writer 自己读，自然也读得到（读的是任一可用副本）。

**进阶观察（容错，待本地验证）**：仿照项目测试，写入后 `writer.close()`（模拟写端宕机），再从 `reader.get_tensor("replicated_tensor")` 仍应读回正确数据——因为 2 个副本里至少有一个不在 writer 节点上（前提是 Master 把副本分到了不同段）。是否能严格保证「两副本必在不同节点」取决于 Master 分配策略，**待本地验证**。

**预期结果**：打印 `replicated read-back OK: (3, 4) torch.float32`。**完整运行结果待本地验证**（依赖两个 store 实例都成功 setup、Master 把 2 副本分配成功）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `pub_tensor` 比 `put_tensor` 多一个 `validate_replicate_config` 步骤？

> **参考答案**：`put_tensor` 固定用默认 config（`replica_num=1`、`preferred_segments` 为空），天然满足「preferred_segments 长度 == replica_num」（0 == 1 的校验只在非空时触发）。`pub_tensor` 允许用户传任意 config，`preferred_segments` 长度可能和 `replica_num` 不匹配，所以必须在客户端先把这种明显错误拦下，避免发无意义的 PutStart RPC（[store_py.cpp:1471-1482](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1471-L1482)）。

**练习 2**：`replica_num=2` 但集群里只有一个节点挂了段，会发生什么？

> **参考答案**：Master 在 `PutStart` 阶段要为 2 个副本各分配一段空间。如果可用段不足以放下 2 个副本（比如只有 1 个段），Master 会返回 `NO_AVAILABLE_HANDLE`（参见 [u5-l2](u5-l2-master-service.md) 的 PutStart 错误码），`pub_tensor` 把这个负数错误码透传回 Python。这就是为什么本节实践要建**两个** store 实例。

---

### 4.6 内存段登记：mount_segment / allocate_and_mount_segment vs register_buffer

#### 4.6.1 概念说明

回到第 2 节那张表。Store 里有两组看似都在「处理内存」的 API，但用途完全不同：

**A 组：贡献内存进池（store server 角色）**

- `mount_segment(path, size, offset=0, protocol="tcp", location="")`：打开你给的**文件路径** `path`，`mmap` 出 `size` 字节，作为一段内存挂进全局资源池。返回 `{"ret": int, "segment_ids": list[str]}`。如果 `size` 超过单段上限，会被拆成多段，`segment_ids` 就有多个。
- `allocate_and_mount_segment(size, protocol="tcp", location="")`：不要求你给文件，Store **内部直接分配** `size` 字节并挂载。返回 `{"ret", "segment_ids", "allocated_size"}`。`allocated_size` 可能比请求的 `size` 大（向上对齐到 Slab 粒度）。
- 对应的卸载：`unmount_segment(segment_ids, grace_period_seconds=0)` / `unmount_and_free_segment(segment_ids, ...)`（后者还释放 `allocate_and_mount_segment` 分配的内存）。

**B 组：注册用户缓冲区（client 角色）**

- `register_buffer(buffer_ptr, size)`：把你**自己**分配的内存（如 `ctypes` 缓冲、`MooncakeHostMemAllocator.alloc` 返回的地址）注册给 Transfer Engine，使其能被 RDMA 零拷贝读写。配合 `get_into(key, buffer_ptr, size)`（零拷贝读入）和 `put_from(key, buffer_ptr, size)`（零拷贝写出）使用。
- `unregister_buffer(buffer_ptr)`：注销。

**一句话区分**：A 组让「别人能往我这写/从我这读」（我提供存储空间）；B 组让「我自己读写时绕过额外拷贝」（我提供传输用缓冲）。注意 `setup()` 已经隐式做了一次 A 组（挂 `global_segment_size`）和一次 B 组（注册 `local_buffer_size` 的本地缓冲），所以日常使用常常不需要显式调用这两组 API——它们是「需要额外/更精细控制内存」时才用的进阶接口。

#### 4.6.2 核心流程

**`mount_segment`（文件-backed）**：

```
Python: store.mount_segment(path="/dev/shm/seg0", size=1GB, protocol="tcp")
   │  release GIL
   ▼
RealClient::mountSegment(path, offset, size, protocol, location, &out_segment_ids)
   │   ① open(path) + mmap  size 字节
   │   ② （若 size > max_mr_size）拆成多 chunk，逐 chunk mmap
   │   ③ 对每个 chunk 调 Master::MountSegment 登记进全局池
   ▼
返回 {"ret": 0, "segment_ids": ["uuid0", ...]}
```

**`allocate_and_mount_segment`（内部分配）**：与上面类似，但第 ① 步换成「Store 内部分配（`allocate_buffer_allocator_memory`）」，并向 `out_allocated_size` 写入实际分配大小（对齐到 `Slab::kSize`）。

**`register_buffer`（注册用户缓冲）**：

```
Python: buf = (ctypes.c_ubyte * N)(); ptr = ctypes.addressof(buf)
        store.register_buffer(ptr, N)
   │  release GIL
   ▼
RealClient::register_buffer(buffer, size)
   │   ① 把 [buffer, buffer+size) 注册进 Transfer Engine 的本地内存表
   │      （RDMA 模式下会做 MR 注册）
   ▼
返回 0
# 之后即可零拷贝：store.get_into(key, ptr, N) / store.put_from(key, ptr, N)
```

#### 4.6.3 源码精读

`mount_segment` 的 wrapper：先确认是 `RealClient`，释放 GIL 调 `mountSegment`，再把 `segment_ids` 装进返回字典：

```cpp
py::dict mount_segment(const std::string &path, size_t size, size_t offset,
                       const std::string &protocol, const std::string &location) {
    py::dict result;
    result["ret"] = -1;
    result["segment_ids"] = py::list();
    auto real_client = std::dynamic_pointer_cast<RealClient>(store_);
    if (!real_client) { LOG(ERROR) << "mount_segment requires RealClient"; return result; }
    std::vector<std::string> segment_ids;
    int ret;
    {
        py::gil_scoped_release release;
        ret = real_client->mountSegment(path, offset, size, protocol, location, segment_ids);
    }
    result["ret"] = ret;
    ... // 把 segment_ids 填进 result["segment_ids"]
    return result;
}
```
—— [mooncake-integration/store/store_py.cpp:341-367](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L341-L367)：`mount_segment` 返回 `{"ret", "segment_ids"}`。Python 侧绑定（含默认参数）在 [store_py.cpp:2106-2108](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2106-L2108)。

`allocate_and_mount_segment` 多返回一个 `allocated_size`：

```cpp
py::dict allocate_and_mount_segment(size_t size, const std::string &protocol, ...) {
    ...
    size_t allocated_size = 0;
    { py::gil_scoped_release release;
      ret = real_client->allocateAndMountSegment(size, protocol, location, segment_ids, &allocated_size); }
    result["ret"] = ret;
    result["segment_ids"] = py::cast(segment_ids);
    result["allocated_size"] = allocated_size;     // 实际分配大小（可能 > 请求 size）
    return result;
}
```
—— [mooncake-integration/store/store_py.cpp:380-405](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L380-L405)：`allocate_and_mount_segment` 内部分配并挂载，返回实际 `allocated_size`。

底层 `RealClient::mountSegment` / `allocateAndMountSegment` 的语义在头文件里有权威注释：

```cpp
// Mount a shared memory file region and return segment ids.
// 若 size > max_mr_size，会拆成多 chunk 分别挂载。RealClient 内部 open(path)+mmap。
int mountSegment(const std::string &path, size_t offset, size_t size,
                 const std::string &protocol, const std::string &location,
                 std::vector<std::string> &out_segment_ids);

// Allocate memory internally and mount segments to master.
// 内存由 allocate_buffer_allocator_memory 分配；实际大小（对齐到 Slab::kSize）写入 out_allocated_size。
int allocateAndMountSegment(size_t size, const std::string &protocol, ...,
                            std::vector<std::string> &out_segment_ids,
                            size_t *out_allocated_size = nullptr);
```
—— [mooncake-store/include/real_client.h:704-731](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L704-L731)：`mountSegment`（文件-backed）与 `allocateAndMountSegment`（内部分配）的权威声明。

`register_buffer` 绑定——注意它接收的是**整数指针**（`uintptr_t`），不是 Python 对象本身：

```cpp
.def("register_buffer",
    [](MooncakeStorePyWrapper &self, uintptr_t buffer_ptr, size_t size) {
        void *buffer = reinterpret_cast<void *>(buffer_ptr);
        py::gil_scoped_release release;
        return self.store_->register_buffer(buffer, size);
    }, py::arg("buffer_ptr"), py::arg("size"),
    "Register a memory buffer for direct access operations")
```
—— [mooncake-integration/store/store_py.cpp:2586-2596](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2586-L2596)：`register_buffer` 把整数指针 cast 成 `void*` 后调 `RealClient::register_buffer`。底层声明见 [real_client.h:104](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L104)。

配套的零拷贝读写：`get_into(key, buffer_ptr, size)` 把对象**直接读进**已注册缓冲区（[store_py.cpp:2608-2618](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2608-L2618) → `RealClient::get_into`，[real_client.h:117-127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L117-L127)），`put_from(key, buffer_ptr, size)` 则从已注册缓冲区**直接写出**（[store_py.cpp:2662-2677](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L2662-L2677) → `RealClient::put_from`，[real_client.h:168-181](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L168-L181)）。它们的文档都强调：**缓冲区地址必须落在 Store 管理的已注册内存里**，否则无法零拷贝。

> **真实用例**：`mooncake_store_service.py` 在「预填充↔解码」切换时，正是用 `mount_segment(path, size, offset, protocol, location)` 动态挂载/卸载大段内存（[mooncake_store_service.py:215](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_store_service.py#L215)、[:303](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_store_service.py#L303)），并用 `unmount_segment` 配合 `grace_period_seconds` 平滑卸载（[:346](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_store_service.py#L346)）。

#### 4.6.4 代码实践

**实践目标**：用 `register_buffer` + `get_into`/`put_from` 走一条零拷贝字节链路；并单独演示 `allocate_and_mount_segment` 的返回结构。

**操作步骤**：运行下列「示例代码」（前置：store 已 `setup`）：

```python
# 示例代码
import ctypes
from mooncake.store import MooncakeDistributedStore

store = MooncakeDistributedStore()
store.setup("127.0.0.1:12345", "127.0.0.1:2379",
            256 * 1024 * 1024, 64 * 1024 * 1024, "tcp", "lo",
            "127.0.0.1:50051")

# ---- B 组：register_buffer + 零拷贝读写 ----
N = 64
buf = (ctypes.c_ubyte * N)()
buf_ptr = ctypes.addressof(buf)
assert store.register_buffer(buf_ptr, N) == 0          # 注册用户缓冲

payload = b"zero-copy-roundtrip"
ctypes.memmove(buf, payload, len(payload))             # 把数据放进缓冲
assert store.put_from("zc", buf_ptr, len(payload)) == 0  # 零拷贝写

# 清空缓冲后零拷贝读回
ctypes.memset(buf, 0, N)
n = store.get_into("zc", buf_ptr, N)                   # 零拷贝读，返回读到的字节数
print("get_into bytes =", n, "content =", bytes(buf[:n]))
assert bytes(buf[:n]) == payload

store.unregister_buffer(buf_ptr)

# ---- A 组：allocate_and_mount_segment（额外贡献一段内存进池）----
res = store.allocate_and_mount_segment(128 * 1024 * 1024, "tcp", "")
print("allocate_and_mount ret =", res["ret"],
      "segment_ids =", res["segment_ids"],
      "allocated_size =", res["allocated_size"])
```

**需要观察的现象**：`register_buffer` 返回 0；`get_into` 返回读到的字节数（等于写入长度），缓冲区内容与原始 `payload` 一致——说明 `put_from`/`get_into` 借助已注册缓冲完成了零拷贝往返。`allocate_and_mount_segment` 返回的 `allocated_size` 通常 **≥** 请求的 128MB（向上对齐到 Slab 粒度），且 `segment_ids` 非空。

**预期结果**：`get_into bytes = 19 content = b'zero-copy-roundtrip'`；`allocate_and_mount ret = 0 segment_ids = [...] allocated_size = ...`（≥ 128MB）。**完整运行结果待本地验证**（依赖 Master/metadata 服务；`allocated_size` 的具体对齐值取决于底层 Slab 粒度，**待本地验证**）。

#### 4.6.5 小练习与答案

**练习 1**：`mount_segment` 和 `allocate_and_mount_segment` 的本质区别是什么？分别适合什么场景？

> **参考答案**：`mount_segment` 要求你提供一个**文件路径**，它 `open+mmap` 这个文件作为段内存（适合用 `/dev/shm/xxx` 共享内存文件、或 hugetlbfs 文件等已有文件来贡献内存）；`allocate_and_mount_segment` 不需要文件，Store **内部直接分配**（适合「我只想要大小，不关心文件」的场景）。底层差异见 [real_client.h:704-731](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L704-L731)。

**练习 2**：为什么 `register_buffer` 接收的是 `buffer_ptr`（整数）而不是 Python 的 `bytes`？

> **参考答案**：`register_buffer` 的目的是把**你已分配好的一段内存**登记给 Transfer Engine（供 RDMA MR 注册），它需要的是「这块内存的起始地址 + 大小」，且这块内存的生命周期由**你**控制。Python 的 `bytes` 是不可变、且其底层缓冲地址不保证稳定暴露；用 `ctypes` 分配 + `addressof` 拿到的整数指针才符合「稳定地址 + 用户自管生命周期」的契约。注册后，这段地址范围内的内存才能被 `get_into`/`put_from` 零拷贝使用（[real_client.h:117-127](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L117-L127)、[168-181](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L168-L181)）。

---

## 5. 综合实践

**任务**（对应规格要求）：编写一个 Python 程序——初始化 `MooncakeDistributedStore`、mount 一段内存、用 `ReplicateConfig` 指定 **2 副本**写入一个 tensor，再从**另一端**读取并校验形状与数值一致。把本讲的「初始化 → 张量写入 → 副本配置 → 跨端读取」串成一条完整链路。

**完整示例代码**：

```python
# 综合实践示例代码：mooncake_replicated_tensor_demo.py
import os
import torch
from mooncake.store import MooncakeDistributedStore, ReplicateConfig

META   = os.getenv("MC_METADATA_SERVER", "127.0.0.1:2379")
MASTER = os.getenv("MASTER_SERVER", "127.0.0.1:50051")
PROTO  = os.getenv("PROTOCOL", "tcp")
DEV    = os.getenv("DEVICE_NAME", "lo")
SEG    = 256 * 1024 * 1024   # 每个实例贡献 256MB 进池
BUF    = 64 * 1024 * 1024    # 每个实例 64MB 本地缓冲

def make_store(host):
    s = MooncakeDistributedStore()
    rc = s.setup(host, META, SEG, BUF, PROTO, DEV, MASTER)
    if rc != 0:
        raise RuntimeError(f"setup({host}) failed: {rc}")
    return s

# ① 两个实例：writer 提供 1 个副本落脚点，reader 提供第 2 个
writer = make_store("127.0.0.1:24001")
reader = make_store("127.0.0.1:24002")

# ② 显式再 mount 一段内存（演示 mount_segment 族 API）
#    这里用 allocate_and_mount_segment（无需现成文件）；返回 allocated_size（向上对齐）
extra = writer.allocate_and_mount_segment(128 * 1024 * 1024, PROTO, "")
assert extra["ret"] == 0, extra
print("mounted extra segment ids:", extra["segment_ids"],
      "allocated_size:", extra["allocated_size"])

# ③ 构造 tensor，用 ReplicateConfig(replica_num=2) 发布
#    注意：带副本的张量写入必须用 pub_tensor，不能用 put_tensor
t = torch.arange(24, dtype=torch.float32).reshape(2, 3, 4)
cfg = ReplicateConfig()
cfg.replica_num = 2
rc = writer.pub_tensor("demo:replicated_tensor", t, cfg)
assert rc == 0, f"pub_tensor failed: {rc}"

# ④ 从「另一端」读取，校验形状与数值
back = reader.get_tensor("demo:replicated_tensor")
assert back is not None, "get_tensor returned None"
assert tuple(back.shape) == (2, 3, 4), back.shape
assert back.dtype == torch.float32, back.dtype
assert torch.equal(t, back), "数值不一致"
print("OK shape:", tuple(back.shape), "dtype:", back.dtype, "values equal:", torch.equal(t, back))

# ⑤ 清理（可选：卸载额外挂载的段）
writer.unmount_and_free_segment(extra["segment_ids"])
```

**步骤与要点**：

1. **服务依赖**：先启动 Master（`127.0.0.1:50051`）和 metadata server（`127.0.0.1:2379`），否则 `setup` 会失败。可参考 [u5-l1](u5-l1-store-architecture.md) 的启动说明或项目的 `scripts/run_ci_test.sh`。
2. **两个实例是 2 副本的前提**：`replica_num=2` 需要 Master 能把副本分到（至少）两个不同段，所以 writer/reader 各自 `setup` 贡献了一段。`allocate_and_mount_segment` 演示了「在 setup 之外再追加一段」。
3. **必须用 `pub_tensor`**：`put_tensor` 没有 `config` 参数，固定单副本。这是本实践的核心考点。
4. **校验三件套**：`shape`、`dtype`、`torch.equal`（数值）。注意大shape 的张量用 `torch.equal`/`allclose`，而非逐元素 Python 比较。

**需要观察的现象**：`allocate_and_mount_segment` 返回非空 `segment_ids` 且 `allocated_size ≥ 128MB`；`pub_tensor` 返回 0；`get_tensor` 返回的张量与原张量在 shape/dtype/数值上完全一致。

**预期结果**：打印 `mounted extra segment ids: [...] allocated_size: ...` 和 `OK shape: (2, 3, 4) dtype: torch.float32 values equal: True`。

**待本地验证项**（明确标注）：
- 完整运行需要 Master + metadata 服务在线、两个 `setup` 都返回 0、且 Master 成功分配 2 副本；**实际输出待本地验证**。
- `allocated_size` 的具体对齐值取决于底层 `Slab::kSize`，**待本地验证**。
- 若想验证「写端宕机后副本端仍可读」的容错，可仿照 [test_replicated_distributed_object_store.py:285-301](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/tests/test_replicated_distributed_object_store.py#L285-L301) 在 ④ 之后加 `writer.close()` 再读，**待本地验证**两副本是否被分到了不同节点。

## 6. 本讲小结

- `MooncakeDistributedStore` 在 C++ 侧的真名是 `MooncakeStorePyWrapper`：一个专门管 **GIL**（阻塞操作 `release_gil`）和做 **Python↔C++ 类型转换**（`py::buffer`→`std::span`、torch tensor→`TensorMetadata`）的薄封装，真正干活的是 `RealClient`。
- 生命周期是 `setup()`（连 Master + 挂全局段 + 注册本地缓冲）→ 反复读写 → `close()`（`tearDownAll` + `reset`）。`setup` 有位置参数版和字典配置版两种。
- **字节级**（`put`/`get`/`put_batch`/`get_batch`）把 Store 当 `dict[str, bytes]`；`get` 在 key 缺失时返回**空 `bytes`**而非抛异常，便于批量处理。
- **张量级**（`put_tensor`/`get_tensor`/`batch_*_tensor`）在字节流前加 `TensorMetadata` 头，对象布局为 `[metadata][data]`；写入用 `extract_tensor_info` + `put_parts`（两段零拷贝），读回用 `get_buffer` + `buffer_to_tensor`（重建并修正 bfloat16/float16/float8 的 dtype view）。
- **`ReplicateConfig`** 控制 `replica_num`（副本数）、`with_soft_pin`/`with_hard_pin`（软/硬 pin）、`preferred_segments`（长度须 == `replica_num`）。**带副本的张量写入必须用 `pub_tensor`/`batch_pub_tensor`**（带 `config` 参数并做 `validate_replicate_config`），`put_tensor` 固定单副本、无 `config` 参数。
- **两组「内存登记」要分清**：`mount_segment`/`allocate_and_mount_segment` 把内存**贡献进全局资源池**（store server 角色，返回 `segment_ids`）；`register_buffer` 把**用户缓冲**注册为零拷贝传输内存（client 角色，配合 `get_into`/`put_from`）。

## 7. 下一步学习建议

- **张量并行与 ReadTarget**：本讲只覆盖了最基础的 `*_with_tp` 一族（`put_tensor_with_tp`/`get_tensor_with_tp`，按 `key_tp_<rank>` 分片）和带 `parallelism` 的 `*_with_parallelism` API。如果你做 TP/EP 训推，建议精读 `store_py_parallel_write.h` / `store_py_parallel_read.h`（被 `store_py.cpp:1165` / `:1220` include）与 `ParallelAxisSpec`/`TensorParallelismSpec`/`ReadTargetSpec` 绑定（[store_py.cpp:1907-1957](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1907-L1957)）。
- **副本迁移与任务 API**：`create_copy_task`/`create_move_task`/`query_task` 返回的 `QueryTaskResponse` 定义在 [rpc_types.h:193-219](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/rpc_types.h#L193-L219)，`ReplicaStatus` 状态机（UNDEFINED→INITIALIZED→PROCESSING→COMPLETE→…）在 [store_py.cpp:1762-1769](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1762-L1769)。结合 [u5-l2](u5-l2-master-service.md) 的副本状态机一起读。
- **零拷贝深入**：想理解 `register_buffer` 背后的 MR 注册与 `get_into`/`put_from` 的地址解析，可读 `RealClient::resolve_writable_buffer_region`（[real_client.h:114-115](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/real_client.h#L114-L115)）及其 Python 包装（[store_py.cpp:1174-1197](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/store/store_py.cpp#L1174-L1197)），对应 Transfer Engine 的 `registerLocalMemory`。
- **SSD offload 与多级存储**：`setup` 的 `enable_ssd_offload`/`ssd_offload_path`、`ReplicateConfig.nof_replica_num`/`preferred_nof_segments` 都指向多级存储。可阅读 `mooncake-store/tests/test_offload_on_eviction.py`、`test_promotion_on_hit.py` 了解内存未命中如何回源 SSD。
- **推荐动手入口**：直接跑 `mooncake-wheel/tests/` 下的 `test_put_get_tensor.py`、`test_replicated_distributed_object_store.py`、`test_distributed_object_store.py`，它们覆盖了本讲全部 API，是最权威的「可运行示例」。
