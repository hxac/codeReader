# XPU SYCL IPC:原生扩展与 JIT 编译

## 1. 本讲目标

上一讲(u4-l3)我们拆解了 CUDA/NPU 路径的 `TorchIPCHandler`:它把 `torch.multiprocessing.reductions.reduce_tensor` 产出的 `(func, args)` 元组当作跨进程句柄,worker 侧重建出共享显存。本讲进入第三条腿:**Intel XPU 上的零拷贝共享**。

torch 并没有给 XPU 提供等价于 CUDA IPC 的 Python API,所以 checkpoint-engine 自己写了一个约 110 行的 C++ 原生扩展 `sycl_ipc.cpp`,包装 SYCL 实验性的 `ipc_memory` API,并在运行时用 `icpx` 编译器 JIT(即时)编译加载。学完本讲,你应该能:

1. 说出 `XpuIPCHandler` 的 dict 线格式,以及 `kind` 标签如何让 worker 不依赖任何带外信息就能选对解码器。
2. 读懂 `sycl_ipc.cpp` 的 5 个导出函数,理解"自包含可移植字节 blob"与 CUDA IPC 句柄的本质区别。
3. 理解 `_find_icpx` 的三路候选探测、头文件硬性过滤与"数值版本排序"的坑。
4. 讲清楚 `load_ext` 的三层缓存/重试语义,以及 `prewarm` 为什么必须放在 `ParameterServer.__init__`。
5. 解释 detach 中"生产者释放导出句柄 / 消费者解除映射"的时序约束——它源自 level-zero-v2 UR 适配层上的一个真实竞态。

## 2. 前置知识

### 2.1 Intel XPU、SYCL 与 oneAPI 工具链

- **XPU** 是本项目里对 Intel GPU 的统称。torch 为其提供 `torch.xpu` 设备模块(等价于 `torch.cuda`),张量可放在 `xpu:0` 这样的设备上。
- **SYCL** 是 Khronos 组织的 C++ 异构并行编程标准;Intel 的实现叫 DPC++,对应的 C++ 编译器是 **icpx**。本讲的 `sycl_ipc.cpp` 就是一段 SYCL C++ 源码。
- **oneAPI** 是 Intel 的工具链发行版,通常装在 `/opt/intel/oneapi`,用 `setvars.sh` 配置环境;`CMPLR_ROOT` 环境变量指向编译器子树的根。这些路径都会出现在 `_find_icpx` 的探测逻辑里。
- 关键约束:torch 的 XPU 版自带**自己的 libsycl(SYCL 运行时)**。扩展必须与 torch 链接同一个运行时、使用同一个 SYCL context,否则映射出的指针在 torch 张量里不可用——这是 `sycl_ipc.cpp` 里所有 context 都取自 `c10::xpu` 的原因。

### 2.2 SYCL ipc_memory:什么是"可移植句柄"

跨进程共享设备内存从不拷贝数据,只传递一个"名字"(句柄),另一个进程拿句柄把**同一块物理显存**映射进自己的虚拟地址空间。SYCL 的实验性 `ipc_memory` 扩展提供四步原语:

- `ipc::get(ptr, ctx)`:生产者按设备指针导出句柄;
- `handle.data()`:句柄的内容,是一个**自包含的可移植字节 blob**——fd、偏移等全部信息都编码在字节串内部;
- `ipc::open(blob, ctx, dev)`:消费者从字节串还原出一个设备指针(内部指针的偏移已包含在内);
- `ipc::put(handle, ctx)` / `ipc::close(ptr, ctx)`:分别释放导出侧句柄与消费侧映射。

对比 CUDA IPC(u4-l3):`reduce_tensor` 的 15 元组里,句柄 `cudaIpcMemHandle` 只是一个驱动层的名字,重建函数、设备槽位、偏移都散落在元组其他位置(所以才需要 `_rebuild_ipc` 改写下标 6 的设备槽)。SYCL 的 blob 则是一切尽在字节串里,这就是模块文档里 "no dma-buf fd, no offset to carry" 的含义。

### 2.3 Level-Zero 与 UR 适配层

Level-Zero 是 Intel GPU 的底层驱动 API;UR(Unified Runtime)是 SYCL 运行时之下新一代的统一运行时抽象,**level-zero-v2** 是它针对 Level-Zero 的新版适配层。本讲的源码注释指出:在该适配层下,生产者若在消费者 `open` 之前就 `put`(释放导出句柄),会提前释放导出侧的 fd,与消费者的 `open` 形成竞态。这个驱动层约束直接决定了 `XpuIPCHandler` "延迟到 detach 才释放"的设计。

### 2.4 torch 的 C++ 扩展 JIT 编译

`torch.utils.cpp_extension.load()` 的流程是:给定 `.cpp` 源文件 → 调用编译器现场编译成 `.so` → 直接 import 返回 Python 模块。要点:

- Python↔C++ 绑定由 **pybind11** 完成(源码末尾的 `PYBIND11_MODULE`);
- `with_sycl=True` 是 XPU 构建开关,提供 SYCL 头文件路径与 device link;
- 构建依赖 ninja,产物有磁盘缓存(源码不变时跳过重编);
- pybind11 的 STL 自动绑定把 `std::vector<uint8_t>` 映射为 Python 的 int 列表——这解释了后面 Python 薄包装里 `bytes(...)` / `list(...)` 的显式转换。

### 2.5 承接 u4-l3:三段契约不变

`IPCHandler` 抽象基类定义的 `export → attach → detach` 三段契约、PS 侧 `with` 上下文保证退出即 detach、worker 侧按消息形状反查 handler——这些骨架在上一讲已建立,本讲的 `XpuIPCHandler` 是同一契约的 XPU 实现。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `checkpoint_engine/ipc_handler.py` | IPC 抽象与两种实现 | `XpuIPCHandler`:kind 标签、dict 线格式、延迟释放、detach 时序 |
| `checkpoint_engine/xpu_ipc/__init__.py` | JIT 加载器 + 5 个薄包装 | `_find_icpx`、`_icpx_version_key`、`load_ext`、`is_available`、`prewarm` |
| `checkpoint_engine/xpu_ipc/sycl_ipc.cpp` | 原生 C++ 扩展(唯一一个) | 5 个 pybind 函数、`g_handles` 导出登记表、命名空间探测 |
| `checkpoint_engine/worker.py` | 消费者调用点 | `_ipc_handler_for_handle` 按线格式反查;第一个 None 时 detach |
| `checkpoint_engine/ps.py` | 生产者调用点 | `build_ipc_handler` 选择、`export(buffer)`、`__init__` 里 prewarm |
| `checkpoint_engine/device_utils.py` | 能力开关 | `supports_device_ipc` 委托给 `xpu_ipc.is_available()` |
| `tests/test_ipc_handler.py` | **CPU 可跑**单测 | 线格式、延迟释放、幂等 detach、消费者 close(mock 原生扩展) |
| `tests/test_xpu_parity.py` | **CPU 可跑**单测 | 编译器探测、版本排序、PATH 处理、缓存策略(全 mock) |
| `tests/test_xpu_ipc.py` | GPU 硬件门控测试 | 同进程 roundtrip、内部指针、跨进程广播(`pytest.mark.gpu`) |

依赖方向:`ipc_handler.py` 只在方法体内**延迟导入** `xpu_ipc`(编译只发生在真正用到时);`ps.py`/`worker.py` 只依赖 `ipc_handler.py` 的抽象。这意味着 `import checkpoint_engine` 不会触发任何编译。

## 4. 核心概念与源码讲解

### 4.1 XpuIPCHandler:dict 线格式与延迟释放

#### 4.1.1 概念说明

`TorchIPCHandler` 是"白嫖 torch 现成能力",`XpuIPCHandler` 则是"自己造轮子再套上同一层壳"。它解决三个问题:

1. **线格式**:跨 ZMQ 传输的句柄必须可 pickle。SYCL blob 是字节串,天然可 pickle;项目再包一层 dict,加上 `kind` 标签与 `nbytes`,构成自描述消息:

   ```python
   {"kind": "xpu_sycl", "handle_bytes": <bytes>, "nbytes": <int>}
   ```

2. **消费者如何选解码器**:worker 进程看到的只是一条 pickle 消息。`_ipc_handler_for_handle` 按**消息形状**反查——dict 且 `kind == "xpu_sycl"` 用 `XpuIPCHandler`,否则(包括任意其他 dict)用 `TorchIPCHandler`。这是自描述协议设计:不需要版本协商,也不需要带外配置。
3. **释放时序**:level-zero-v2 竞态(2.3 节)决定了导出句柄**不能**在 export 后立刻释放,必须推迟到消费者 open 之后,即 detach 时。

#### 4.1.2 核心流程

一次 Broadcast 更新中 XPU IPC 句柄的完整生命周期(PS 为生产者,worker 为消费者;两侧在同一台机器、同一块 GPU 上,colocated 部署):

```text
PS (REQ, 生产者)                                worker (REP, 消费者)
────────────────────────────────────           ────────────────────────────────────
update():
  with build_ipc_handler(dm) as h:             收到 ipc_handle(dict):
    h.export(buffer)                             _ipc_handler_for_handle(dict)
      ptr = buffer.data_ptr()                    → XpuIPCHandler
      bytes = get_handle(ptr)   ← ipc::get      attach(handle, device_id)
      记 _exported_ptr = ptr                       ptr = open_handle(bytes, dev) ← ipc::open
      (不释放!)                                    记 _opened_ptr = ptr
      dict{kind, handle_bytes, nbytes}            buffer = wrap_tensor(ptr, nbytes, dev)
    ────── ZMQ send_pyobj(dict) ─────────────→    ← b"" ACK
    ... 逐桶 dist.broadcast(worker 装载权重) ...
    with 块退出 / 收到异常:
      detach():                                 收到第一个 None:
        release_handle(_exported_ptr) ← ipc::put   synchronize()
                                                    detach(): close_handle(ptr) ← ipc::close
                                                  收到第二个 None: post_hook
```

三条时序规则:

- **export 只登记不释放**:`_exported_ptr` 记下指针,`ipc::put` 推迟到 detach;
- **消费者 unmap 前必须同步**:`torch.xpu.synchronize()` 保证没有在飞的设备读,才能 `close`;
- **一个实例只有一种身份**:生产者实例只会有 `_exported_ptr`,消费者实例只会有 `_opened_ptr`,detach 分别走对应分支,且都幂等(置回 `None`)。

#### 4.1.3 源码精读

先看类头与 export([checkpoint_engine/ipc_handler.py:75-96](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L75-L96)):

```python
class XpuIPCHandler(IPCHandler):
    kind = "xpu_sycl"  # wire tag: identifies this handle format to the consumer

    def export(self, buffer: torch.Tensor) -> Any:
        from checkpoint_engine import xpu_ipc
        ptr = buffer.data_ptr()
        handle_bytes = xpu_ipc.get_handle(ptr)
        # Release only in detach(): freeing before the consumer opens can drop the
        # fd under the level-zero-v2 UR adapter.
        self._exported_ptr = ptr
        return {"kind": self.kind, "handle_bytes": handle_bytes, "nbytes": buffer.nbytes}
```

这段是生产者侧:取 `data_ptr()`(内部指针也合法,偏移编码在 blob 里)、换出字节串、登记指针、返回三键 dict。注释明确写出延迟释放的原因。生产者的调用点在 `_update_per_bucket` 里——被导出的正是 u3-l4 讲过的 2 倍桶大小双缓冲([checkpoint_engine/ps.py:824-833](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L824-L833)),`build_ipc_handler` 则在 update 的 `with` 块里按设备类型分流([checkpoint_engine/ps.py:600-603](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L600-L603))。

再看消费者侧 attach([checkpoint_engine/ipc_handler.py:98-108](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L98-L108)):

```python
    def attach(self, handle: Any, device_id: int) -> torch.Tensor:
        from checkpoint_engine import xpu_ipc
        assert isinstance(handle, dict) and handle.get("kind") == self.kind, ...
        ptr = xpu_ipc.open_handle(handle["handle_bytes"], device_id)
        self._opened_ptr = ptr
        buffer = xpu_ipc.wrap_tensor(ptr, handle["nbytes"], device_id)
        assert buffer.dtype == torch.uint8
        return buffer
```

与 CUDA 路径的 `_rebuild_ipc` 对比:XPU **不需要改写线格式里的设备槽位**——设备索引是 `open_handle` 的显式参数,不在 blob 里,天然规避了两个进程 `CUDA_VISIBLE_DEVICES` 编号不一致的问题(u4-l3 的 `list_args[6] = device_id` 技巧在此无需存在)。

最后是本模块的核心 detach([checkpoint_engine/ipc_handler.py:110-127](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L110-L127)):

```python
    def detach(self) -> None:
        # Consumer unmaps its opened pointer; producer releases its exported handle.
        # At most one of the two is set on any given instance.
        from checkpoint_engine import xpu_ipc
        if self._opened_ptr is not None:
            try:
                torch.xpu.synchronize()  # no in-flight reads before unmapping
                xpu_ipc.close_handle(self._opened_ptr)
            except Exception as e: ...
            self._opened_ptr = None
        if self._exported_ptr is not None:
            try:
                xpu_ipc.release_handle(self._exported_ptr)
            except Exception as e: ...
            self._exported_ptr = None
```

三个要点:先同步再 unmap;两个分支各自 try/except(清理是尽力而为,不能让清理失败炸掉主流程);置 `None` 保证幂等。消费端的 detach 由 worker 状态机在"第一个 None"消息时触发([checkpoint_engine/worker.py:94-107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L94-L107)),finally 里还有一次幂等兜底([checkpoint_engine/worker.py:125-131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L125-L131))。

反查函数在 worker 侧([checkpoint_engine/worker.py:21-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L21-L28)):

```python
def _ipc_handler_for_handle(handle: object) -> IPCHandler:
    if isinstance(handle, dict) and handle.get("kind") == XpuIPCHandler.kind:
        return XpuIPCHandler()
    return TorchIPCHandler()
```

#### 4.1.4 代码实践

**实践目标**:在纯 CPU 机器上验证 XPU IPC 的线格式契约与释放时序(仓库已备好 mock 测试,不需要任何 Intel GPU)。

**操作步骤**:

1. 确认环境已安装本包与测试依赖(`pip install -e .` + `pytest`);
2. 运行 CPU 单测:`pytest tests/test_ipc_handler.py -v`;
3. 重点阅读四个 XPU 相关测试:
   - `test_xpu_export_returns_self_contained_handle`([tests/test_ipc_handler.py:53-60](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_ipc_handler.py#L53-L60)):mock 掉 `xpu_ipc.get_handle` 后断言 export 结果恰好是三键 dict;
   - `test_xpu_export_defers_release_until_detach`([tests/test_ipc_handler.py:63-78](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_ipc_handler.py#L63-L78)):断言 export 后 `release_handle` **未被调用**,detach 后恰好调用一次且指针正确,二次 detach 不重复释放;
   - `test_xpu_handler_detach_is_safe_when_unused`([tests/test_ipc_handler.py:81-89](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_ipc_handler.py#L81-L89)):未 export/attach 过的实例 detach 不抛错、不碰原生扩展;
   - `test_xpu_consumer_detach_closes_opened_mapping`([tests/test_ipc_handler.py:92-110](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_ipc_handler.py#L92-L110)):消费者 detach 只 `close_handle` 打开的指针,不会去 release 一个从未导出的句柄。

**需要观察的现象**:全部用例 PASS,且 mock 的调用断言与 4.1.2 节的时序图一一对应。

**预期结果**:延迟释放、幂等 detach、消费者/生产者分支互斥这三条契约都被测试固化。若你修改 `XpuIPCHandler`(比如把 release 提前到 export),`test_xpu_export_defers_release_until_detach` 应当立刻失败——这就是这些测试存在的意义。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么消费者在 `close_handle` 之前必须 `torch.xpu.synchronize()`,而生产者 `release_handle` 之前不需要?

**答案**:close 解除的是消费者自己的映射;若仍有内核在异读这块设备内存(异步 load_weights 尚未完成),unmap 会产生未定义行为。而 release 释放的是**导出侧登记的句柄**(一个名字/资源记录),不涉及本进程地址空间的去映射,且此时消费者早已 open 完毕(时序上 detach 在广播循环结束之后)。

**练习 2**:如果对同一个 buffer 连续调用两次 `export`,会发生什么?会泄漏吗?

**答案**:两次都会各自返回一份新的字节串(`get_handle` 内部 `h.data()` 是独立拷贝),第二次会在 C++ 登记表里先 `put` 掉同指针的旧句柄再存入新的(见 4.2.3 的 `ipc_get_handle`),所以不会泄漏;但 `self._exported_ptr` 只记一个指针,detach 只释放最后登记的那个。本项目的用法里每个 update 只 export 一次,这是协议约定而非代码强制。

**练习 3**:`_ipc_handler_for_handle` 收到 `{"foo": "bar"}` 时返回 `TorchIPCHandler`,为什么不直接报错?

**答案**:反查逻辑的契约是"只有带 `kind == "xpu_sycl"` 标签的 dict 才是 XPU 消息",其余一律走默认的老协议(CUDA tuple)。这是一种保守的兼容策略:未知 dict 落回默认分支后,`TorchIPCHandler.attach` 里的 `isinstance(handle, tuple)` 断言仍会把真正的错误拦下来——错误不会静默,但分发逻辑保持简单。

### 4.2 sycl_ipc.cpp:SYCL ipc_memory 的 C++ 封装

#### 4.2.1 概念说明

`sycl_ipc.cpp` 是整个项目唯一的原生 C++ 扩展,职责是把 SYCL 实验性 `ipc_memory` 的四步原语(get/put/open/close)包装成 5 个 pybind11 函数,另加一个"把裸指针包成 torch 张量"的工具函数。它的三条设计原则写在文件头注释里([checkpoint_engine/xpu_ipc/sycl_ipc.cpp:1-5](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/sycl_ipc.cpp#L1-L5)):

1. 句柄自包含:消费者只凭字节串就能 open,不需要 fd、不需要偏移;
2. context 和 device 一律取自 torch(`c10::xpu`),保证映射落在 torch 自己的 SYCL context 上;
3. get 接受内部指针(子分配),偏移已编码在 blob 内。

此外它还内置一张**导出侧登记表** `g_handles`:按 `ptr → handle` 存放已导出句柄,这是"延迟释放"在 C++ 侧的落地——Python 侧只留指针当钥匙,真正的句柄值必须有人保管到 put 为止。

#### 4.2.2 核心流程

五个函数与 Python 薄包装的一一对应:

| C++ 函数 | Python 包装 | 使用方 | 对应 SYCL 原语 |
| --- | --- | --- | --- |
| `ipc_get_handle` | `xpu_ipc.get_handle` | 生产者 | `ipc::get` |
| `ipc_release_handle` | `xpu_ipc.release_handle` | 生产者 | `ipc::put` |
| `ipc_open_handle` | `xpu_ipc.open_handle` | 消费者 | `ipc::open` |
| `ipc_close_handle` | `xpu_ipc.close_handle` | 消费者 | `ipc::close` |
| `ipc_wrap_tensor` | `xpu_ipc.wrap_tensor` | 消费者 | (torch::from_blob) |

生产者序列:`ipc_get_handle(ptr)` → 查/存登记表 → 返回 blob 字节;更新结束后 `ipc_release_handle(ptr)` → 从表里摘除并 `put`。
消费者序列:`ipc_open_handle(blob, device)` → 在指定设备上映射 → 返回裸指针;`ipc_wrap_tensor` 把它包成不拥有所有权的 uint8 张量;用完 `ipc_close_handle(ptr)` 解除映射。

#### 4.2.3 源码精读

先看前向兼容的命名空间探测([checkpoint_engine/xpu_ipc/sycl_ipc.cpp:19-27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/sycl_ipc.cpp#L19-L27)):

```cpp
#if __has_include(<sycl/ext/oneapi/experimental/detail/ipc_common.hpp>)
namespace ipc = sycl::ext::oneapi::experimental::ipc::memory;
namespace ipc_types = sycl::ext::oneapi::experimental::ipc;
#else
namespace ipc = sycl::ext::oneapi::experimental::ipc_memory;
namespace ipc_types = sycl::ext::oneapi::experimental::ipc_memory;
#endif
```

上游 SYCL 已把这套 API 拆分(函数移入 `ipc::memory`,类型留在父命名空间)并废弃了扁平的 `ipc_memory`,但还没有 oneAPI 版本实际发布新布局——所以用 `__has_include` 探测,让同一份源码在旧/新头文件布局下都能编译。这是编写跨版本原生扩展的常用手法。

导出与登记表([checkpoint_engine/xpu_ipc/sycl_ipc.cpp:36-64](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/sycl_ipc.cpp#L36-L64)):

```cpp
std::mutex g_handles_mu;
std::unordered_map<uintptr_t, ipc_types::handle> g_handles;

std::vector<uint8_t> ipc_get_handle(uintptr_t ptr) {
  sycl::context ctx = c10::xpu::get_device_context();
  ipc_types::handle h = ipc::get(reinterpret_cast<void*>(ptr), ctx);
  ipc_types::handle_data_t data = h.data();  // owning copy of the blob
  {
    std::lock_guard<std::mutex> lk(g_handles_mu);
    auto it = g_handles.find(ptr);
    if (it != g_handles.end()) {
      ipc::put(it->second, ctx);  // release stale handle for a reused address
      it->second = h;
    } else {
      g_handles.emplace(ptr, h);
    }
  }
  return {/* data 字节范围 */};
}
```

四个细节:context 取自 torch;`h.data()` 是 blob 的**独立所有权拷贝**,返回值与 `h` 的生命周期解耦;登记表按指针索引,同指针重复导出时先 put 旧句柄防泄漏(地址复用场景);注释再次强调不能提前 put(level-zero-v2 竞态)。

消费者的映射与包装([checkpoint_engine/xpu_ipc/sycl_ipc.cpp:82-103](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/sycl_ipc.cpp#L82-L103)):

```cpp
uintptr_t ipc_open_handle(const std::vector<uint8_t>& blob, int64_t device) {
  sycl::context ctx = c10::xpu::get_device_context();
  sycl::device dev = c10::xpu::get_raw_device(static_cast<c10::DeviceIndex>(device));
  void* p = ipc::open(to_bytes(blob), ctx, dev);
  return reinterpret_cast<uintptr_t>(p);
}

torch::Tensor ipc_wrap_tensor(uintptr_t dptr, int64_t nbytes, int64_t device) {
  auto opts = torch::TensorOptions().dtype(torch::kUInt8).device(
      torch::kXPU, static_cast<c10::DeviceIndex>(device));
  return torch::from_blob(reinterpret_cast<void*>(dptr), {nbytes}, [](void*) {}, opts);
}
```

`ipc_wrap_tensor` 是"XPU 版 rebuild_cuda_tensor":`torch::from_blob` 配一个**空的删除器**,张量不拥有内存、只提供视图;映射的生命周期由 `open_handle`/`close_handle` 这对显式调用管理。注释里明确把它类比为 CUDA 路径的 `rebuild_cuda_tensor`。`ipc_release_handle` 的实现([L66-80](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/sycl_ipc.cpp#L66-L80))则先在锁内摘表、锁外 `put`(缩小临界区),未登记时是 no-op。

Python 侧的薄包装把 C++ list 与 bytes 收敛([checkpoint_engine/xpu_ipc/__init__.py:136-143](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L136-L143)):

```python
def get_handle(ptr: int) -> bytes:
    return bytes(load_ext().ipc_get_handle(ptr))   # list[int] → bytes

def open_handle(handle_bytes: bytes, device: int) -> int:
    return load_ext().ipc_open_handle(list(handle_bytes), device)  # bytes → list[int]
```

pybind11 的 STL 绑定把 `std::vector<uint8_t>` 映射为 Python int 列表,所以进/出要做 `bytes` ↔ `list` 的显式收敛;线上统一使用 bytes。

#### 4.2.4 代码实践

**实践目标**:源码阅读型实践——画出"函数映射表"并用硬件门控测试验证自包含句柄的两个关键性质。

**操作步骤**:

1. 对照 4.2.2 的空表(盖住右三列),凭记忆填写每个 C++ 函数的 Python 包装、使用方与 SYCL 原语;
2. 阅读 `tests/test_xpu_ipc.py` 的两个同进程测试:
   - `test_sycl_ipc_same_process_roundtrip`([tests/test_xpu_ipc.py:80-95](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_ipc.py#L80-L95)):`get_handle → open_handle → wrap_tensor → torch.equal → close_handle` 的最小闭环,证明同进程内映射回来的字节与原张量完全一致;
   - `test_sycl_ipc_interior_pointer_offset_preserved`([tests/test_xpu_ipc.py:98-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_ipc.py#L98-L117)):对一个 `big[1024:1280]` 的**内部指针**导出再打开,断言内容与视图一致——这正是"偏移编码在 blob 里、无需单独携带"的直接证据;
3. 若有 Intel GPU 与 oneAPI >= 2026.0 环境,运行 `pytest tests/test_xpu_ipc.py -v`(无 GPU 时该文件被 `pytest.mark.gpu` 跳过,CPU CI 用 `-m "not gpu"` 即可验证跳过行为)。

**需要观察的现象**:无 GPU 环境下 `pytest tests/test_xpu_ipc.py -v` 显示 3 个 SKIPPED,理由是 "Intel XPU with buildable SYCL ipc_memory extension required"(门控逻辑在 [tests/test_xpu_ipc.py:17-30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_ipc.py#L17-L30))。

**预期结果**:映射表填写结果与 4.2.2 一致;两个测试的断言分别对应"可移植 blob"与"内部指针偏移"两个性质。跳过行为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:`ipc_wrap_tensor` 的删除器是空的,那这块内存谁来释放?

**答案**:没有任何人通过张量释放——张量只是视图,不拥有内存。映射的生命周期由 `ipc_open_handle`/`ipc_close_handle` 显式管理(worker 在第一个 None 时 close);而生产者侧真正的 buffer(PS 的双缓冲)由 torch 的 XPU allocator 管理,与 IPC 句柄无关。

**练习 2**:为什么 `ipc_get_handle` 里要先拿 `h.data()` 的拷贝,而不是直接返回 `h` 内部的数据?

**答案**:`handle_data_t data = h.data()` 产生一份独立所有权的 blob 拷贝,返回的字节串与 `h`(以及登记表里后续的 put/erase)**生命周期解耦**;否则 put 之后字节串可能悬空。Python 侧 `bytes(...)` 再拷一次,拿到不可变的线格式载荷。

**练习 3**:如果把 `#include <c10/xpu/XPUFunctions.h>` 去掉、自己 `sycl::context ctx(...)` 新建上下文,会发生什么?

**答案**:扩展会落在与 torch 不同的 SYCL context 上;跨 context 访问设备内存是非法的,torch 张量(以及 `ipc::open` 要求的 torch context)与映射无法互操作。正确做法正是源码的做法:context、device 一律从 `c10::xpu` 取,与 torch 共享同一运行时状态。

### 4.3 _find_icpx:编译器探测与版本排序

#### 4.3.1 概念说明

JIT 编译的前置问题是"找到一台能编、且编出来能用的编译器"。这里有一个隐蔽但致命的坑,模块文档写得非常直白([checkpoint_engine/xpu_ipc/__init__.py:47-53](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L47-L53)):

> 没有 ipc_memory 头文件的旧 icpx 也能"成功编译",但它产出的设备镜像 torch 的新版 libsycl 无法加载——错误发生在 dlopen 时的 **进程 abort(SIGABRT)**,Python 层根本 catch 不到。

所以探测策略是"宁可找不到,不能用错的":用**头文件存在性**作为编译器是否够新(oneAPI >= 2026.0)的判据,不达标的候选直接跳过,绝不试编。

#### 4.3.2 核心流程

候选按优先级收集,再统一过滤:

```text
candidates = [
    $CMPLR_ROOT/bin/icpx,                        # ① 显式指定,最高优先
    .../oneapi/compiler/*/bin/icpx 按版本降序,      # ② 标准安装位置,最新优先
    shutil.which("icpx"),                         # ③ PATH 兜底
]
return 第一个 exists(c) 且 _has_ipc_memory(c) 的候选;否则 None
```

版本排序的坑在排序键 `_icpx_version_key`:路径形如 `/opt/intel/oneapi/compiler/<ver>/bin/icpx`,`<ver>` 取 `path.split("/")[-3]`。若按字符串比较,`"2026.9" > "2026.10"`(因为字符 `'9' > '1'`),会把旧版本排在前面——所以必须拆成整数列表比。另外 `latest` 是指向最新安装的符号链接,不是数字版本,给它最大键让它稳居第一。

#### 4.3.3 源码精读

头文件探测([checkpoint_engine/xpu_ipc/__init__.py:26-32](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L26-L32)):

```python
def _has_ipc_memory(icpx: str) -> bool:
    """Whether this icpx ships the SYCL IPC memory header (oneAPI >= 2026.0)."""
    root = os.path.dirname(os.path.dirname(icpx))
    header = os.path.join(
        root, "include", "sycl", "ext", "oneapi", "experimental", "ipc_memory.hpp"
    )
    return os.path.exists(header)
```

判据只有一个:icpx 上两级的 `include/sycl/ext/oneapi/experimental/ipc_memory.hpp` 是否存在——不调用编译器、不解析版本号,最便宜也最可靠。

版本排序键([checkpoint_engine/xpu_ipc/__init__.py:35-44](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L35-L44)):

```python
def _icpx_version_key(path: str) -> tuple[int, list[int]]:
    version = path.split("/")[-3]
    parts = version.split(".")
    if not all(p.isdigit() for p in parts):
        return (1, [])          # latest 等符号链接:最大键,降序时排最前
    return (0, [int(p) for p in parts])  # 数值版本:整数列表逐位比较
```

`(1, []) > (0, 任意数字列表)` 恒成立(元组比较先看首元素),所以降序排序时 `latest` 第一、`2026.10` 第二、`2026.9` 第三——整数比较下 `10 > 9`,字符串比较的陷阱被绕开。

主探测函数([checkpoint_engine/xpu_ipc/__init__.py:54-71](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L54-L71)):

```python
    candidates: list[str] = []
    root = os.getenv("CMPLR_ROOT")
    if root:
        candidates.append(os.path.join(root, "bin", "icpx"))
    candidates += sorted(
        glob.glob("/opt/intel/oneapi/compiler/*/bin/icpx"),
        key=_icpx_version_key, reverse=True,
    )
    which = shutil.which("icpx")   # 覆盖非 /opt 布局与 source setvars.sh 未导出 CMPLR_ROOT 的情况
    if which:
        candidates.append(which)
    return next((c for c in candidates if os.path.exists(c) and _has_ipc_memory(c)), None)
```

三路候选的顺序是"显式配置 → 标准位置 → 环境兜底",过滤条件 `exists && _has_ipc_memory` 把不合格编译器挡在构建之前。CPU 单测对这三个分支都有固化:[tests/test_xpu_parity.py:93-102](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L93-L102) 验证数值排序与 latest 夺冠;[L105-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L105-L117) 验证 PATH 兜底;[L120-131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L120-L131) 验证无头文件的编译器被拒;[L134-145](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L134-L145) 验证头文件判据本身。

#### 4.3.4 代码实践

**实践目标**:在纯 CPU 机器上亲手驱动探测逻辑,验证排序键与三路候选(全部不需要 Intel GPU)。

**操作步骤**:

1. 跑仓库的探测单测:`pytest tests/test_xpu_parity.py -v -k "icpx or has_ipc"`;
2. 在无 oneAPI 的机器上运行探测(`_find_icpx` 不依赖 torch):

   ```bash
   python -c "from checkpoint_engine import xpu_ipc; print(xpu_ipc._find_icpx())"
   ```

3. 用临时目录伪造一棵 oneAPI 目录树,验证判据与 CMPLR_ROOT 分支(示例代码):

   ```python
   # 示例代码:xpu_ipc_probe_demo.py
   import os, tempfile
   from pathlib import Path
   from checkpoint_engine.xpu_ipc import _has_ipc_memory, _find_icpx

   root = Path(tempfile.mkdtemp())
   (root / "bin").mkdir(parents=True)
   (root / "bin" / "icpx").touch()                      # 假编译器可执行文件
   print(_has_ipc_memory(str(root / "bin" / "icpx")))   # 预期 False:没有头文件

   hdr = root / "include" / "sycl" / "ext" / "oneapi" / "experimental" / "ipc_memory.hpp"
   hdr.parent.mkdir(parents=True)
   hdr.touch()                                          # 放入头文件
   print(_has_ipc_memory(str(root / "bin" / "icpx")))   # 预期 True

   os.environ["CMPLR_ROOT"] = str(root)
   print(_find_icpx())                                  # 预期 <root>/bin/icpx
   ```

**需要观察的现象**:第 2 步在无 oneAPI 环境输出 `None`;第 3 步三个 print 依次输出 `False`、`True`、伪造的 icpx 路径;第 1 步全部 PASS。

**预期结果**:如上。三步的具体输出**待本地验证**(第 3 步逻辑与仓库测试 [tests/test_xpu_parity.py:134-145](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L134-L145) 同构,可交叉印证)。

#### 4.3.5 小练习与答案

**练习 1**:为什么候选顺序是 `CMPLR_ROOT` → glob → `which`,而不是反过来?

**答案**:显式配置的意图最强,应无条件优先;`/opt/intel/oneapi` 是 oneAPI 的标准安装位置,比 PATH 上可能存在的任意 icpx 更可控、版本更明确;PATH 只作兜底,覆盖"source 了 setvars.sh 但没导出 CMPLR_ROOT"和"装在非标准前缀"两种情况。测试 [tests/test_xpu_parity.py:105-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L105-L117) 的注释专门说明了没有这个兜底时会"错误地宣布 XPU IPC 不可用"。

**练习 2**:假设同时存在 `2025.3` 和 `2026.10` 两个安装,`_find_icpx` 返回哪个?如果只有 `2025.3` 呢?

**答案**:有 `2026.10` 时,数值降序把它排在 `2025.3` 前面,且它有 ipc_memory 头文件,返回 `2026.10` 的 icpx。只有 `2025.3` 时,它虽存在但 `_has_ipc_memory` 为 False,被过滤;若 PATH 上也没有合格编译器,函数返回 `None`,`load_ext` 随后抛 RuntimeError——这正是"宁可找不到,不能用错的"。

**练习 3**:`latest` 符号链接为什么直接给最大键,而不解析链接指向?

**答案**:`latest` 按约定永远指向该机器上最新的安装,给它最大键即可让它夺冠,无需额外一次 `readlink`;解析反而引入失败模式(链接断掉时探测出错)。这是用约定换健壮性的取舍。

### 4.4 load_ext:JIT 编译、三层缓存与 prewarm

#### 4.4.1 概念说明

`load_ext` 是"找到编译器 → 编译 → 加载"的封装。它要同时回答三个问题:**什么时候编**(懒加载 + prewarm)、**编一次的代价怎么摊**(三层缓存)、**失败怎么办**(成功才缓存,失败可重试)。这三层缓存/重试语义是本模块的设计核心:

| 层 | 机制 | 缓存什么 | 失败后 |
| --- | --- | --- | --- |
| 进程内构建 | `@functools.lru_cache(maxsize=1)` | 编译成功返回的模块对象 | lru_cache 不缓存异常,下次重试 |
| 磁盘构建产物 | torch `cpp_extension.load` 的标准缓存(默认 `~/.cache/torch_extensions/`,可用 `TORCH_EXTENSIONS_DIR` 调整) | `.so` 构建产物;源码不变时跳过重编 | —— |
| 可用性探测 | 模块级 `_AVAILABLE` 布尔 | 仅"探测成功" | 失败不置位,允许重试(应对临时性故障) |

而 **prewarm** 回答"什么时候编":编译耗时以秒计,而 update 路径上有超时、又处在训练循环的关键路径上——所以把编译提前到 `ParameterServer.__init__`。

#### 4.4.2 核心流程

```text
import checkpoint_engine            # 不编译(xpu_ipc 延迟导入,模块级不碰 torch)
ParameterServer.__init__()         # device_type == "xpu" 时:
    └─ xpu_ipc.prewarm() ──► is_available()
            ├─ torch.xpu 不存在/不可用 → False(不编译)
            ├─ load_ext():
            │     ├─ _find_icpx() 为 None → RuntimeError("needs oneAPI >= 2026.0")
            │     ├─ 把 icpx 所在目录临时插到 PATH 头部
            │     ├─ torch.utils.cpp_extension.load(
            │     │      name="checkpoint_engine_sycl_ipc",
            │     │      sources=[sycl_ipc.cpp], extra_cflags=["-O2"],
            │     │      with_sycl=True)          # 首次秒级;命中磁盘缓存则极快
            │     └─ finally 恢复 PATH
            ├─ 异常 → logger.warning + False      # 不置位 _AVAILABLE,下次可重试
            └─ 成功 → _AVAILABLE = True           # 本进程此后直接放行
update() 时 get_handle/open_handle/... → load_ext() 命中 lru_cache,零编译开销
```

两个容易忽略的细节:

- **PATH 手术**:`with_sycl=True` 会注入 SYCL 头与 device link,但 torch shell 出来的是裸命令名 `icpx`,所以必须把编译器所在目录临时加进 PATH,构建结束 `finally` 恢复——环境副作用被严格限定在构建窗口内;
- **-O2 不能省**:源码注释指出不加 `-O2` 时 host 侧对象文件会按 `-O0` 编译。

#### 4.4.3 源码精读

`load_ext` 主体([checkpoint_engine/xpu_ipc/__init__.py:74-105](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L74-L105)):

```python
@functools.lru_cache(maxsize=1)
def load_ext() -> "ModuleType":
    """JIT-compile (``with_sycl``, linking torch's libsycl) and cache the SYCL IPC extension."""
    icpx = _find_icpx()
    if icpx is None:
        raise RuntimeError(
            "no icpx with SYCL ipc_memory support found (needs oneAPI >= 2026.0); "
            "cannot build XPU IPC extension"
        )
    from torch.utils.cpp_extension import load
    src = Path(__file__).with_name("sycl_ipc.cpp")
    # with_sycl=True supplies the SYCL include paths and device link, but torch invokes
    # a bare "icpx", so it must be on PATH; keep -O2 or the host object is built -O0.
    prev_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.path.dirname(icpx) + os.pathsep + prev_path
    try:
        module = load(name="checkpoint_engine_sycl_ipc", sources=[str(src)],
                      extra_cflags=["-O2"], with_sycl=True, verbose=False)
    finally:
        os.environ["PATH"] = prev_path
    return module
```

注意 `sources` 用 `Path(__file__).with_name("sycl_ipc.cpp")` 定位源码——扩展与加载器永远同目录发布,不依赖安装布局。

成功才缓存的可用性探测([checkpoint_engine/xpu_ipc/__init__.py:108-133](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L108-L133)):

```python
_AVAILABLE: bool = False

def is_available() -> bool:
    global _AVAILABLE
    if _AVAILABLE:
        return True
    try:
        import torch
        if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
            return False
        load_ext()
    except Exception as e:
        logger.warning(f"xpu sycl ipc unavailable: {e}")
        return False
    _AVAILABLE = True
    return True

def prewarm() -> bool:
    """Build the extension ahead of time (outside any weight-update timeout)."""
    return is_available()
```

`torch` 的导入也在函数体内——纯 CPU 的 torch 连 `torch.xpu` 属性都可能没有,所以先 `hasattr` 防御。`prewarm` 只是 `is_available` 的语义化别名,差别在调用时机。

prewarm 的消费点在 `ParameterServer.__init__`([checkpoint_engine/ps.py:253-264](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L253-L264)):

```python
        # Build the JIT SYCL IPC extension now, so its multi-second compile is outside
        # the first weight-update window.
        if self.device_manager.device_type == "xpu":
            from checkpoint_engine import xpu_ipc
            if xpu_ipc.prewarm():
                logger.info(f"[rank{self._rank}] XPU SYCL ipc_memory extension prebuilt")
            else:
                logger.warning(... "weight updates will fail until it can be built")
```

另一个消费点是能力开关:`DeviceManager.supports_device_ipc` 对 cuda/npu 直接返回 True,对 xpu 委托给 `xpu_ipc.is_available()`([checkpoint_engine/device_utils.py:289-301](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L289-L301))——"XPU IPC 可用"不是一个静态事实,而是"扩展能否构建"的动态结论。

#### 4.4.4 代码实践

**实践目标**:在 CPU 上验证 JIT 加载的环境处理与缓存策略被测试固化。

**操作步骤**:

1. 运行 `pytest tests/test_xpu_parity.py -v`(整个文件都是 CPU 单测);
2. 重点读两个测试:
   - `test_load_ext_puts_icpx_on_path_and_restores_it`([tests/test_xpu_parity.py:60-90](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L60-L90)):用假的 `load` 捕获构建瞬间看到的 PATH,断言 `"/opt/oneapi/bin:/usr/bin"`(icpx 目录在最前)、`with_sycl is True`、`extra_cflags == ["-O2"]`,且构建后 PATH 恢复为 `/usr/bin`;
   - `test_is_available_caches_only_success_and_retries_failure`([tests/test_xpu_parity.py:148-170](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L148-L170)):让 `load_ext` 第一次抛异常、第二次成功,断言探测结果依次为 `False → True → True` 且 `load_ext` 恰好被调两次——失败不缓存、成功后不再重探。

**需要观察的现象**:全部 PASS;第二个测试里 `calls["n"] == 2` 是"成功才缓存"的直接量化证据。

**预期结果**:如上,**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:为什么 prewarm 放在 `ParameterServer.__init__`,而不是第一次 `update` 时再编?

**答案**:JIT 编译耗时以秒计,而 update 在训练循环的关键路径上且有超时约束;init 阶段一次性付掉编译成本,后续每次 update 都零等待。源码注释 "so its multi-second compile is outside the first weight-update window" 说的就是这件事。prewarm 失败也只 warning 不抛——留到 update 时才真正报错,不阻断初始化。

**练习 2**:`lru_cache(maxsize=1)` 与 `_AVAILABLE` 各缓存什么?为什么需要两个?

**答案**:`lru_cache` 缓存**构建成功**返回的模块对象(进程内后续 `get_handle` 等调用直接复用);`_AVAILABLE` 缓存**可用性探测成功**的布尔,供 `is_available`/`supports_device_ipc` 高频查询。两者都允许失败后重试(lru_cache 不缓存异常,`_AVAILABLE` 失败不置位),分工是"拿到模块"与"回答可用吗"两件事。

**练习 3**:PS 与 worker 是两个进程,都首次触发 `load_ext`,会发生两次完整编译吗?

**答案**:各进程独立执行 JIT,但第二次通常命中 torch 的磁盘构建缓存(按 `name` 与源码组织,源码不变直接复用已编译产物),所以完整的编译流程一般只发生一次,另一进程近似直接加载。并发写入构建目录的细节由 torch `cpp_extension` 的构建目录管理负责,**待本地验证**。

## 5. 综合实践

**任务:在纯 CPU 机器上完整复现 XPU IPC 的协议骨架,并产出一份调用轨迹报告。**

本任务把 4 个模块串起来:线格式(4.1)、薄包装(4.2/4.4)、按形状反查(4.1),全部用 mock 替身驱动,不需要任何 Intel GPU。

**步骤 1**:跑通两份 CPU 测试,确认环境就绪:

```bash
pytest tests/test_ipc_handler.py tests/test_xpu_parity.py -v
```

**步骤 2**:把下面的模拟脚本存为 `/tmp/xpu_ipc_lifecycle.py` 并运行(示例代码):

```python
"""模拟 XpuIPCHandler 的 export -> 线格式 -> attach -> detach 生命周期(纯 CPU)。"""
from types import SimpleNamespace
from unittest.mock import patch

import torch

from checkpoint_engine.ipc_handler import XpuIPCHandler
from checkpoint_engine.worker import _ipc_handler_for_handle

# CPU 版 torch 可能没有 torch.xpu 属性,detach 里的 synchronize 需要它存在
if not hasattr(torch, "xpu"):
    torch.xpu = SimpleNamespace(synchronize=lambda: None)

calls = []

def fake_get_handle(ptr):        calls.append(("get_handle", ptr)); return b"\xab" * 64
def fake_open_handle(blob, dev): calls.append(("open_handle", bytes(blob), dev)); return 0x5000
def fake_release(ptr):           calls.append(("release_handle", ptr))
def fake_close(ptr):             calls.append(("close_handle", ptr))
def fake_wrap(ptr, nbytes, dev):
    calls.append(("wrap_tensor", ptr, nbytes, dev))
    return torch.empty(nbytes, dtype=torch.uint8)      # CPU 上伪造 uint8 buffer

buffer = SimpleNamespace(data_ptr=lambda: 0xBEEF, nbytes=128)

with patch.multiple(
    "checkpoint_engine.xpu_ipc",
    get_handle=fake_get_handle, open_handle=fake_open_handle, wrap_tensor=fake_wrap,
    release_handle=fake_release, close_handle=fake_close,
):
    producer = XpuIPCHandler()
    msg = producer.export(buffer)                        # ① 生产者导出
    assert msg == {"kind": "xpu_sycl", "handle_bytes": b"\xab" * 64, "nbytes": 128}
    assert not any(c[0] == "release_handle" for c in calls)   # ② 导出后不得释放

    consumer = _ipc_handler_for_handle(msg)              # ③ 消费者按形状反查
    assert isinstance(consumer, XpuIPCHandler)
    buf = consumer.attach(msg, device_id=0)              # ④ open + wrap
    assert buf.dtype == torch.uint8

    consumer.detach()                                    # ⑤ close 打开的映射
    producer.detach()                                    # ⑥ release 导出句柄
    producer.detach()                                    # ⑦ 幂等重入,应无调用

print("调用轨迹(按发生顺序):")
for c in calls:
    print("  ", c)
```

**步骤 3**:对照轨迹核对四条时序断言:

1. `get_handle` 只出现一次,且其后**没有**紧跟 `release_handle`(延迟释放);
2. `open_handle`、`wrap_tensor` 在消费者侧依次出现,device_id 正确传递;
3. 消费者 detach 先 `synchronize` 再 `close_handle(0x5000)`,且**不**调用 `release_handle`;
4. 生产者 detach 调用 `release_handle(0xBEEF)` 恰好一次,第二次 detach 无任何调用(幂等)。

**预期结果**:脚本无断言错误退出,轨迹顺序为 `get_handle → open_handle → wrap_tensor → (synchronize) → close_handle → release_handle`。synchronize 是否出现在轨迹里取决于你的 torch 是否原生带 `torch.xpu`(带了会真调用,不在 `calls` 里记录——可自行把 `torch.xpu.synchronize` 也 patch 成记录函数)。具体输出**待本地验证**。

**步骤 4(可选,进阶)**:对照 [tests/test_xpu_parity.py:208-253](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L208-L253) 思考:如果 `_update_per_bucket` 中途抛异常,生产者的导出句柄靠什么保证一定被释放?动手把上面脚本改成"attach 后让模拟的更新函数抛异常",验证 `with build_ipc_handler(...)` 的 `__exit__` 路径(提示:参考该测试用真实 `IPCHandler` 子类而非 MagicMock 的理由——`detach` 必须经由上下文管理器的 `__exit__` 被真实触达)。

## 6. 本讲小结

- torch 没有 XPU 版的 Python IPC API,项目用 110 行的 `sycl_ipc.cpp` 包装 SYCL 实验性 `ipc_memory`(get/put/open/close),用 `icpx` JIT 编译加载,链接 torch 自带的 libsycl、共享其 SYCL context。
- `XpuIPCHandler` 的线格式是自描述 dict `{"kind": "xpu_sycl", "handle_bytes", "nbytes"}`;worker 用 `_ipc_handler_for_handle` 按消息形状反查解码器,不需要任何带外协商。
- SYCL 句柄是**自包含的可移植字节 blob**:fd 与内部指针偏移都编码在字节串里,不需要像 CUDA 路径那样改写设备槽位或单独携带偏移。
- 释放时序是驱动层约束的投影:level-zero-v2 UR 适配层上生产者提前 put 会释放 fd 与消费者 open 竞态,所以 export 只登记指针,release 推迟到 detach;消费者 unmap 前必须先 `synchronize`;两侧 detach 都幂等且尽力而为。
- `_find_icpx` 按"显式 CMPLR_ROOT → /opt 标准位置(数值版本降序)→ PATH 兜底"收集候选,再用 ipc_memory 头文件存在性硬性过滤——旧编译器编出的镜像会让 dlopen 直接 SIGABRT,Python 层无法捕获,所以绝不试编。
- `load_ext` 有三层缓存/重试语义(lru_cache 模块缓存、torch 磁盘构建缓存、`_AVAILABLE` 仅缓存成功),prewarm 把秒级编译挪进 `ParameterServer.__init__`,排除在带超时的权重更新窗口之外。

## 7. 下一步学习建议

- **下一讲 u4-l5(HTTP API 服务层)**:回到控制面,看 `_init_api` 暴露的 REST 端点如何与 `ParameterServer` 的生命周期方法一一对应——本讲的 prewarn 就发生在这些端点背后的同一个对象构造里。
- **横向对照 u5-l1(DeviceManager)**:`supports_device_ipc` 只是能力开关之一,建议把 `supports_inplace_pin`(XPU 上强制关闭,见 [tests/test_xpu_parity.py:296-319](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L296-L319))与 `supports_device_p2p`(XPU 不支持)一起梳理成一张 XPU 能力矩阵,体会"设备差异被收敛在少数开关里"的分层设计。
- **源码延伸阅读**:`sycl_ipc.cpp` 的 `__has_include` 命名空间探测是编写跨版本原生扩展的通用手法,可对比 DPC++ 上游对 `ipc_memory` API 拆分的演进;`torch.utils.cpp_extension.load` 的 `with_sycl` 参数与构建缓存行为值得在 Intel 官方文档里再确认一遍。
- **动手方向**:若你有一台 Intel GPU 机器,按 `tests/test_xpu_ipc.py` 的三个硬件门控测试(同进程 roundtrip / 内部指针 / 跨进程广播)逐个跑通,把本讲的 mock 轨迹与真实驱动行为对照,验证 4.1.2 的时序图。
