# pip 安装与 Python TransferEngine 速览

> 本讲面向第一次接触 Mooncake TransferEngine 的读者。你将学会用 `pip` 装好对应版本的 Python 包，并亲手写出一个能在单机上完成「内存注册 + 一次 read/write 传输」的最小示例。

## 1. 本讲目标

学完本讲，你应当能够：

1. 区分 `mooncake-transfer-engine` 四个 pip 变体（`cuda` / `cuda13` / `non-cuda` / `npu`）的适用场景，并选出与本地环境匹配的那一个。
2. 说出 Python `TransferEngine` 类的初始化步骤，以及 `protocol`、`metadata_server`、`device_name` 三个参数的含义。
3. 理解「内存注册」为什么是传输的前置条件，并掌握 `register_memory` 与 `batch_register_memory` 的区别。
4. 在单机上用两个进程跑通一次 TCP 传输，并打印传输状态码。

## 2. 前置知识

- **TransferEngine 是什么**：Mooncake 提供的高性能数据传输引擎。它把「本地一段内存」注册成可被网络直接访问的缓冲区，然后通过 RDMA / TCP / NVLink 等通道在节点之间搬运数据。本讲只关心它的 Python 入口。
- **内存注册（memory registration）**：把一段裸内存地址告诉引擎，引擎会为它生成一个可在网络上寻址的句柄（segment / buffer 描述符）。**没有注册的内存无法被传输**——这是本讲最关键的一条规则。
- **metadata_server（元数据服务）**：通信双方需要一个地方交换「我注册了哪些内存、地址是多少」。Mooncake 支持多种后端：etcd、Redis、HTTP，以及一个零依赖的 `P2PHANDSHAKE` 模式。**本讲全程使用 `P2PHANDSHAKE`**，因为它不需要启动任何外部服务，最适合速览。
- **Python 环境**：需要 Linux + Python 3.8 及以上（pyproject 要求 `>=3.8`），建议使用虚拟环境。Windows / macOS 不支持。
- 建议先完成上一讲（`u1-l3`）对 Mooncake 整体架构的了解。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md) | 官方安装说明，列出四个 pip 包变体及其前置依赖。 |
| [mooncake-wheel/pyproject.toml](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-wheel/pyproject.toml) | wheel 的包名、版本、Python 版本要求与依赖。装好后 `import mooncake.engine` 即可。 |
| [mooncake-integration/transfer_engine/transfer_engine_py.cpp](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp) | 用 pybind11 把 C++ 引擎绑定为 Python 模块 `mooncake.engine`，`TransferEngine` 类的全部方法都在这里注册。 |
| [mooncake-integration/transfer_engine/transfer_engine_py.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.h) | `TransferEnginePy` 的 C++ 类声明，包含 `write_bytes_to_buffer` / `read_bytes_from_buffer` 等内联辅助方法。 |
| [mooncake-transfer-engine/example/batch_register_bench.py](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py) | 批量注册基准脚本：target 端注册 N 块内存，initiator 端发起 read，统计吞吐与延迟。是本讲「动手实践」的模板。 |
| [mooncake-transfer-engine/include/memory_location.h](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/memory_location.h) | 定义通配位置常量 `kWildcardLocation`（即 Python 端 `location` 参数默认值 `"*"`）。 |

## 4. 核心概念与源码讲解

### 4.1 pip 安装变体

#### 4.1.1 概念说明

Mooncake 是 C++ 项目，通过 pybind11 生成原生扩展模块（`.so`）后打成 wheel。因为不同硬件（NVIDIA GPU / 国产 NPU / 纯 CPU）需要链接不同的运行时库，官方在 PyPI 上发布了**四个包名**，分别对应不同的构建开关。选错包会出现 `lib*.so` 找不到或 `SUPPORT_*` 标志不对的问题。

#### 4.1.2 核心流程

选择包的决策流程：

```text
本机有 NVIDIA GPU？
├── 是 → CUDA 主版本号？
│        ├── < 13.0 → pip install mooncake-transfer-engine        (含 Mooncake-EP + GPU 拓扑发现, 需 CUDA 12.1+)
│        └── >= 13.0 → pip install mooncake-transfer-engine-cuda13
├── 纯 CPU / 无 CUDA → pip install mooncake-transfer-engine-non-cuda
└── 华为昇腾 NPU     → pip install mooncake-transfer-engine-npu
（寒武纪 MLU 暂无预编译 wheel，只能源码编译 -DUSE_MLU=ON）
```

装好后，无论哪个包，**Python 导入路径都是同一个**：

```python
from mooncake.engine import TransferEngine
```

四个包的差异只在底层 `.so` 编译进了哪些 transport（传输后端）。运行时可以用模块级布尔属性自检：

```python
import mooncake.engine as e
print(e.SUPPORT_CUDA, e.SUPPORT_HIP, e.SUPPORT_MNNVL, e.SUPPORT_EFA)
```

#### 4.1.3 源码精读

官方 README 在「Use Python package」小节明确列出了这四个命令与各自的适用场景：

- [README.md:232-254](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L232-L254)：四个 `pip install` 变体的命令清单（cuda<13 / cuda13 / non-cuda / npu）。
- [README.md:256-260](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/README.md#L256-L260)：关键提示——CUDA 版含 Mooncake-EP 与 GPU 拓扑发现、需 CUDA 12.1+；non-cuda 版面向无 CUDA 环境；MLU 仅源码构建；遇到 `lib*.so` 缺失建议卸载后手工编译。

包的元信息来自 wheel 工程的 `pyproject.toml`，包名就叫 `mooncake-transfer-engine`：

- [mooncake-wheel/pyproject.toml:5-14](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-wheel/pyproject.toml#L5-L14)：声明 `name = "mooncake-transfer-engine"`、`requires-python = ">=3.8"`、依赖 `aiohttp` 与 `requests`。`cuda13` / `non-cuda` / `npu` 是同套代码不同编译开关产出的同名/衍生包。

`SUPPORT_*` 这些布尔属性在模块初始化时根据编译宏导出，让你在运行时知道当前 wheel 支持哪些 transport：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:1086-1114](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1086-L1114)：`PYBIND11_MODULE(engine, m)` 中根据 `USE_EFA` / `USE_HIP` / `USE_MNNVL` / `USE_INTRA_NVLINK` / `USE_CUDA` 宏设置 `SUPPORT_*` 属性。

#### 4.1.4 代码实践

**目标**：安装匹配本机环境的包，并验证可导入。

**步骤**：

1. 创建并激活虚拟环境（推荐）：

   ```bash
   python3 -m venv ~/.venvs/mc && source ~/.venvs/mc/bin/activate
   ```

2. 根据上文的决策树选一条命令执行。无 GPU 的纯 CPU 机器（最常见的学习环境）：

   ```bash
   pip install mooncake-transfer-engine-non-cuda
   ```

3. 在 Python 里验证导入并打印支持的 transport：

   ```python
   import mooncake.engine as e
   print("SUPPORT_CUDA =", e.SUPPORT_CUDA)
   print("SUPPORT_EFA  =", e.SUPPORT_EFA)
   ```

**需要观察的现象**：第 3 步应正常打印两个布尔值，且不抛 `ImportError` / 缺 `.so` 错误。`non-cuda` 包下 `SUPPORT_CUDA` 应为 `False`。

**预期结果**：导入成功，说明该变体与你的 Python 版本 / glibc 兼容。若报 `lib*.so` 缺失，按 README 提示卸载该包并改用源码编译。

> 若你无法确定本机到底有没有装好 CUDA 运行时，**待本地验证**：可先用 `non-cuda` 包跑通本讲全部示例，再换 `cuda` 包对比 `SUPPORT_CUDA` 的变化。

#### 4.1.5 小练习与答案

**练习 1**：一台装了 CUDA 13.1 的服务器，应该装哪个包？为什么不能装 `mooncake-transfer-engine`？

> **答案**：应装 `mooncake-transfer-engine-cuda13`。默认的 `mooncake-transfer-engine` 面向 CUDA < 13.0，其链接的 CUDA 运行时版本与 13.x 不匹配，可能出现符号缺失或加载失败。

**练习 2**：装好包后，如何在不传输数据的情况下，判断当前 wheel 是否编译进了 EFA 传输支持？

> **答案**：读取 `mooncake.engine.SUPPORT_EFA`。它在模块加载时由 `USE_EFA` 宏决定（见上文章节 4.1.3 的源码链接），为 `True` 才能用 `protocol="efa"`。

---

### 4.2 TransferEngine Python 类

#### 4.2.1 概念说明

`TransferEngine`（Python 类，对应 C++ 的 `TransferEnginePy` 适配器）是 Python 侧的**唯一入口**。它把「初始化引擎 → 注册内存 → 发起传输」三件事封装成几个方法。它的生命周期典型用法是：

1. `engine = TransferEngine()` 构造一个空引擎。
2. `engine.initialize(...)` 绑定本地地址、元数据后端、传输协议——这一步会真正建立 RPC 服务并（按协议）安装 transport。
3. `engine.register_memory(addr, size)` 注册若干本地内存。
4. `engine.transfer_sync_read/write(...)` 发起一次同步传输（阻塞到完成或超时）。
5. 进程退出时析构，自动注销内存与关闭 segment。

> 术语：**segment（段）** 是引擎对「一台机器上注册的全部内存」的逻辑抽象。每个 `local_hostname` 对应一个 segment，对端通过 `target_hostname` 打开你的 segment 后才能读写你注册的地址。

#### 4.2.2 核心流程

一次 `transfer_sync_read` 的内部流程（伪代码）：

```text
1. openSegment(target) ── 若未缓存，向 metadata 查询对端 buffer 描述，建立连接，缓存 handle
2. allocateBatchID(1) ── 申请一个传输批次
3. 构造 TransferRequest{opcode=READ, source=本地buf, target_id=handle, target_offset=远端地址, length}
4. submitTransfer(batch_id, [request]) ── 提交给底层 transport 执行
5. 循环 getTransferStatus(batch_id, 0, status)：
     COMPLETED → 释放 batch_id，返回 0
     FAILED    → 释放 batch_id，重试（最多 numContexts+1 次）
     超时       → 返回 -1
```

同步传输的等待有一个**与数据量成正比**的超时阈值。源码里的计算是：

\[
\text{timeout}_{\text{nsec}} = \text{base\_timeout} + \text{length}
\]

其中 `base_timeout` 默认 30 秒（可用环境变量 `MC_TRANSFER_TIMEOUT` 调整，最小 5 秒），`length` 的单位被当作「纳秒」，等价于**假设传输速率约 1 GiB/s**——数据越大，允许的等待时间越长。这样小数据不会傻等 30 秒，大数据也不会被误判超时。

#### 4.2.3 源码精读

**构造函数**读取超时配置，默认 30 秒：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:104-112](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L104-L112)：`TransferEnginePy()` 构造函数，读取 `MC_TRANSFER_TIMEOUT`，否则设为 `30 * 1e9` 纳秒。

**initialize** 的签名与职责：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:168-177](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L168-L177)：`initialize(local_hostname, metadata_server, protocol, device_name)`。它先按协议选择内存分配器，再解析连接串，最后调用 `initializeExt`。

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:179-254](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L179-L254)：`initializeExt`。注意第 210-212 行（非 EFA 构建）以 `auto_discover=true` 创建引擎，会自动安装 RDMA；没有 RDMA 设备时回退到 TCP。第 214-224 行根据是否设置 `MC_LEGACY_RPC_PORT_BINDING` 决定 RPC 端口绑定方式。

> **端口绑定的坑**：默认（未设 `MC_LEGACY_RPC_PORT_BINDING`）时，你传入的 `local_hostname` 里的端口号会被忽略，引擎随机选一个端口并通过 `get_rpc_port()` 返回真实端口。所以 P2P 模式下，**对端要用的名字必须用 `get_rpc_port()` 拼出来**，而不是你 initialize 时传的那个。

**read / write 同步传输**：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:344-360](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L344-L360)：`transferSyncWrite` 与 `transferSyncRead`，分别把 WRITE / READ 操作码转发给统一的 `transferSync`。

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:396-489](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L396-L489)：`transferSync` 核心。第 406-415 行缓存 `openSegment` 的 handle；第 426-446 行重试提交；第 461-486 行轮询状态，第 477-485 行就是上面那个 `base + length` 的超时公式。

**内存注册**：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:802-806](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L802-L806)：`register_memory(buffer_addr, capacity, location="*")`，转调 C++ `registerLocalMemory`。`location` 默认 `"*"`（通配位置）。

- [mooncake-transfer-engine/include/memory_location.h:41](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/include/memory_location.h#L41)：`kWildcardLocation = "*"`，即 `location` 参数的默认通配值。

- [mooncake-integration/transfer_engine/transfer_engine_py.h:171-182](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.h#L171-L182)：`write_bytes_to_buffer` / `read_bytes_from_buffer` 是**本机内**的 memcpy / 取字节辅助方法，常用来在注册缓冲区里写入待发送的初始数据，或读出收到的结果。

**pybind 绑定表**（Python 方法名 ↔ C++ 方法）：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:1129-1236](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L1129-L1236)：`TransferEngine` 类的全部 Python 方法注册。注意 Python 用 `snake_case`（如 `transfer_sync_read`、`register_memory`、`get_rpc_port`、`allocate_managed_buffer`），对应 C++ 的 `camelCase`。

Python 调用名速查（节选）：

| Python 方法 | 作用 | 返回 |
|-------------|------|------|
| `initialize(local, meta, proto, dev)` | 初始化引擎 | `0` 表示成功 |
| `get_rpc_port()` | P2P 模式下取真实 RPC 端口 | `int` |
| `register_memory(addr, size, location="*")` | 注册一段本地内存 | `0` 成功 |
| `allocate_managed_buffer(length)` | 由引擎分配并**已注册**的内存 | `uintptr_t` 地址 |
| `transfer_sync_write(target, buf, peer, len)` | 同步写入对端 | `0` 成功 |
| `transfer_sync_read(target, buf, peer, len)` | 同步从对端读取 | `0` 成功 |
| `get_first_buffer_address(segment)` | 取对端第一块注册内存地址 | `uintptr_t` |
| `write_bytes_to_buffer / read_bytes_from_buffer` | 本机内存读写辅助 | `int` / `bytes` |

#### 4.2.4 代码实践

**目标**：用一个最小脚本验证「初始化 + 注册 + 取真实端口」能跑通（暂不跨进程传输）。

**步骤**：把下面这段**示例代码**存为 `probe_engine.py`：

```python
# probe_engine.py  —— 示例代码
from mooncake.engine import TransferEngine

engine = TransferEngine()
# 单机 P2P + TCP，device_name 留空
ret = engine.initialize("127.0.0.1:0", "P2PHANDSHAKE", "tcp", "")
print("initialize ->", ret)                  # 期望 0

port = engine.get_rpc_port()
print("real rpc port ->", port)              # 期望一个非 0 随机端口

# 用引擎自带的托管分配器拿到一段【已注册】的内存
addr = engine.allocate_managed_buffer(1024)
print("managed buffer addr -> %#x" % addr)   # 期望非 0 地址

# 在本机内存里写点数据再读出来（不经过网络）
engine.write_bytes_to_buffer(addr, b"mooncake", 8)
print("read back ->", engine.read_bytes_from_buffer(addr, 8))
```

运行：

```bash
python probe_engine.py
```

**需要观察的现象**：`initialize` 打印 `0`；`get_rpc_port` 打印一个正整数；`read back` 打印出 `b'mooncake'`。

**预期结果**：脚本正常退出且无异常，证明包安装正确、引擎能初始化。`transfer_sync_read/write` 的端到端实践放到 4.3 与综合实践里做（需要两个进程）。

> 若 `initialize` 返回非 0 或抛出缺 `.so` 的异常：**待本地验证**端口/库环境；可尝试设置 `MC_LEGACY_RPC_PORT_BINDING=1` 后改用固定端口再试。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `transfer_sync_read` 的超时时间会随 `length` 增大而变长？

> **答案**：源码里 `timeout = transfer_timeout_nsec_ + length`，把 `length`（字节）当作纳秒叠加，相当于按约 1 GiB/s 的速率估算所需时间。数据越大，允许的等待越久，避免大块传输被误判为超时。

**练习 2**：在不设 `MC_LEGACY_RPC_PORT_BINDING` 时，对端要用什么名字连我？

> **答案**：用 `get_rpc_port()` 返回的真实端口拼接，形如 `f"{host}:{engine.get_rpc_port()}"`。你 `initialize` 时传的端口号此时会被忽略（见 4.2.3 的端口绑定说明）。

**练习 3**：`allocate_managed_buffer` 返回的内存还需要再调用 `register_memory` 吗？

> **答案**：不需要。`allocate_managed_buffer` 内部走 `allocateRawBuffer`，已经在分配后调用 `registerLocalMemory` 完成注册（见 4.2.3 的 `registerMemory` 与构造逻辑），返回的地址可直接用于传输。

---

### 4.3 批量注册示例

#### 4.3.1 概念说明

实际场景里（例如多租户 KV cache、分片池），一台机器上往往要注册**几十甚至上百块**互相独立的内存。如果对每一块都单独调用 `register_memory`，每块都会触发一次与 metadata 服务的「发布 buffer 描述」往返，开销随块数线性增长。

`batch_register_memory(addrs, sizes)` 把多块内存**打包成一次注册**：底层一次完成所有 buffer 的注册，并只发一次元数据更新，显著降低注册阶段的总耗时。这就是 `batch_register_bench.py` 这个基准脚本存在的意义——它既演示了批量注册 API，也对比了「逐块注册」与「批量注册」的差异。

#### 4.3.2 核心流程

`batch_register_bench.py` 的双角色设计：

```text
target 端：
  1. initialize(local_name, "P2PHANDSHAKE", protocol, "")
  2. 用 mmap 分配 N 块内存（先试大页 hugepage，失败回退 4KB 页）
  3. 注册：
       --use_batch_api  → engine.batch_register_memory(addrs, sizes)   # 一次搞定
       否则              → for 每块: engine.register_memory(addr, size) # 逐块
  4. 打印 TARGET_INFO（各块地址）并 signal.pause() 等待对端

initiator 端：
  1. initialize(...)
  2. 分配并注册一块本地接收缓冲区
  3. remote_base = engine.get_first_buffer_address(target_name)  # 拿对端第一块地址
  4. for iter: engine.transfer_sync_read(target, recv, remote_base, size) 统计延迟/吞吐
```

> 注意脚本注释里点明的限制：target 上各块是独立 mmap 的、**地址不连续**，因此 initiator 只能通过 `get_first_buffer_address` 访问第一块。该脚本的核心目的是验证「N 块独立注册」本身可行，而非跨多块连续传输。

#### 4.3.3 源码精读

**命令行开关**：`--use_batch_api` 决定走哪条注册路径。

- [mooncake-transfer-engine/example/batch_register_bench.py:94-98](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L94-L98)：`--use_batch_api` 参数定义。
- 脚本顶部 [batch_register_bench.py:9-22](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L9-L22) 给出了 target / initiator 两端的完整启动命令示例。

**target 端初始化 + 取真实端口**（与 4.2 讲的端口规则一致）：

- [mooncake-transfer-engine/example/batch_register_bench.py:149-161](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L149-L161)：`engine.initialize(...)`，随后 `host = local_server_name.rpartition(":")[0]` + `get_rpc_port()` 拼出真实名字。

**mmap 分配内存**（先 hugepage 后 4KB）：

- [mooncake-transfer-engine/example/batch_register_bench.py:102-134](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L102-L134)：`allocate_block()` 用 ctypes 调 libc 的 `mmap`，先尝试 `MAP_HUGETLB`，失败回退普通页。

**注册路径二选一**：

- [mooncake-transfer-engine/example/batch_register_bench.py:190-203](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L190-L203)：`--use_batch_api` 为真时调用 `engine.batch_register_memory(addrs, sizes)`，否则循环 `engine.register_memory(addr, size)`。

**C++ 侧批量注册实现**（一次 metadata 发布）：

- [mooncake-integration/transfer_engine/transfer_engine_py.cpp:778-789](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L778-L789)：`batchRegisterMemory` 把地址/大小打包成 `BufferEntry` 列表，转调 `engine_->registerLocalMemoryBatch(buffers, location)`，一次完成。

**initiator 端拿到对端地址并传输**：

- [mooncake-transfer-engine/example/batch_register_bench.py:263](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L263)：`remote_base = engine.get_first_buffer_address(args.target_server_name)`。
- [mooncake-transfer-engine/example/batch_register_bench.py:285-308](https://github.com/kvcache-ai/Mooncake/blob/945f3e61c72e0fb936493f77fb0511cd93c35d4b/mooncake-transfer-engine/example/batch_register_bench.py#L285-L308)：warmup 后循环 `transfer_sync_read`，统计 p50/p99 与吞吐。

#### 4.3.4 代码实践

**目标**：在单机上用 `batch_register_bench.py` 跑一次 target + initiator，并对比「逐块」与「批量」两种注册方式的注册耗时。

**步骤**：

1. 开两个终端（同一台机器即可，用 TCP 协议）。先启动 target（注册 4 块、每块 1GB 太大，这里用小参数便于本机试跑）：

   ```bash
   # 终端 A（target）
   python mooncake-transfer-engine/example/batch_register_bench.py \
       --mode target \
       --local_server_name 127.0.0.1:12345 \
       --num_blocks 4 --block_size_gb 0.1 \
       --protocol tcp --use_batch_api
   ```

   观察输出里的 `Actual server name: 127.0.0.1:<真实端口>` 和 `Registration OK: Xs`。

2. 在终端 B 用 target 打印的真实端口启动 initiator：

   ```bash
   # 终端 B（initiator），把 <真实端口> 换成上一步打印的端口
   python mooncake-transfer-engine/example/batch_register_bench.py \
       --mode initiator \
       --local_server_name 127.0.0.1:12346 \
       --target_server_name 127.0.0.1:<真实端口> \
       --num_blocks 4 --block_size_gb 0.1 \
       --protocol tcp
   ```

3. 对比注册耗时：去掉 `--use_batch_api` 重跑终端 A，记录 `Per-block: Xms` 与 `Registration OK` 时间，再与加了 `--use_batch_api` 的版本比较。

**需要观察的现象**：
- 终端 A 打印 `Actual server name` 与 `TARGET_INFO: {...}`。
- 终端 B 打印 `Remote first buffer at 0x...`，最终输出 p50/p99 延迟与吞吐。
- 批量注册的「Registration OK」总时间应**小于或接近**逐块注册（块越多差距越明显）。

**预期结果**：initiator 能成功完成若干次 `transfer_sync_read`，`errors` 为 0，并生成 `batch_bench_*gb.json` 结果文件。

> 若两台机器不在同一台主机、或本机没有 RDMA 设备：**待本地验证**。务必用 `--protocol tcp`（RDMA 需要真实网卡与 OFED 驱动，速览阶段不必强求）。`block_size_gb` 用小值（如 0.1）以避免本机内存/大页不足。

#### 4.3.5 小练习与答案

**练习 1**：`register_memory` 与 `batch_register_memory` 在「与 metadata 通信」上的根本区别是什么？

> **答案**：前者每注册一块就发一次 buffer 描述给 metadata；后者把 N 块打包，底层 `registerLocalMemoryBatch` 只发一次元数据更新。块数多时批量版的网络往返次数从 N 降为 1。

**练习 2**：`batch_register_bench.py` 的 initiator 为什么只能访问 target 的「第一块」缓冲区？

> **答案**：target 用 mmap 独立分配了 N 块，它们地址不连续。`get_first_buffer_address` 返回的是 segment 里 `buffers[0].addr`，即第一块。脚本本身的目标是验证多块注册可行，而非跨多块连续传输（见 4.3.2 的脚本注释）。

---

## 5. 综合实践

把 4.2、4.3 的知识串起来：**自己写一个最小两进程传输脚本**（比 `batch_register_bench.py` 更精简），在单机上完成「注册一段本地内存 → 发起一次 read/write 传输 → 打印状态」。

下面是**示例代码**，存为 `mini_transfer.py`。它用 Python 内置 `mmap` 分配页对齐内存并注册，分 target / initiator 两种模式：

```python
# mini_transfer.py  —— 示例代码（单机 TCP + P2PHANDSHAKE）
import sys, mmap, ctypes

def alloc(size):
    # 匿名 4KB 页映射；返回 (mmap对象, 起始地址)
    m = mmap.mmap(-1, size, access=mmap.ACCESS_WRITE)
    addr = ctypes.addressof(ctypes.c_char.from_buffer(m))
    return m, addr

def main():
    mode   = sys.argv[1]              # "target" 或 "initiator"
    myport = sys.argv[2]              # 本进程监听端口（仅用于 initialize）
    SIZE   = 4096

    from mooncake.engine import TransferEngine
    engine = TransferEngine()
    assert engine.initialize(f"127.0.0.1:{myport}", "P2PHANDSHAKE", "tcp", "") == 0
    name = f"127.0.0.1:{engine.get_rpc_port()}"     # P2P 真实名字

    if mode == "target":
        m, addr = alloc(SIZE)
        assert engine.register_memory(addr, SIZE) == 0        # 注册本地内存
        engine.write_bytes_to_buffer(addr, b"HELLO_MOONCAKE!", 15)  # 写入待发送内容
        print(f"[target] serving as {name}, buf={addr:#x}, "
              f"first={engine.get_first_buffer_address(name):#x}")
        import time; time.sleep(60)                            # 等待对端

    elif mode == "initiator":
        target_name = sys.argv[3]           # 来自 target 打印的 name
        m, recv = alloc(SIZE)
        assert engine.register_memory(recv, SIZE) == 0         # 注册接收缓冲区
        remote = engine.get_first_buffer_address(target_name)  # 对端第一块地址
        assert remote != 0, "无法获取对端地址，确认 target 已就绪"
        ret = engine.transfer_sync_read(target_name, recv, remote, 15)  # 一次 READ
        print(f"[initiator] transfer_sync_read -> {ret} (0=成功)")
        print(f"[initiator] received -> {engine.read_bytes_from_buffer(recv, 15)}")

main()
```

运行方式（两个终端）：

```bash
# 终端 A
python mini_transfer.py target 12345
# 记下它打印的 serving as 127.0.0.1:<真实端口>

# 终端 B（把 <真实端口> 替换掉）
python mini_transfer.py initiator 12346 127.0.0.1:<真实端口>
```

**验收标准**：
- target 端 `register_memory` 返回 0，并能打印 `first=...` 地址。
- initiator 端 `transfer_sync_read` 打印 `-> 0`，并 `received -> b'HELLO_MOONCAKE!'`。

**延伸思考**：把 initiator 的 `transfer_sync_read` 改成 `transfer_sync_write`（参数顺序：`target, 本地源buf, 对端地址peer, length`），让 initiator 向 target 写数据，再在 target 用 `read_bytes_from_buffer` 读出来核对。思考：write 与 read 在「源/目的」上分别指的是本地还是对端？

> 若 `transfer_sync_read` 返回非 0：**待本地验证**。常见原因是 target 还没就绪（端口名没对齐）或本机防火墙挡了回环端口，可先确认两边 `name` 完全一致。

## 6. 本讲小结

- Mooncake 在 PyPI 上有四个变体：`mooncake-transfer-engine`（CUDA<13）、`-cuda13`、`-non-cuda`、`-npu`，按本机硬件二选一/四选一，导入路径统一为 `mooncake.engine`。
- `TransferEngine` 的使用三步走：`initialize` → `register_memory`（或 `allocate_managed_buffer` 拿到已注册内存）→ `transfer_sync_read/write`。
- P2PHANDSHAKE 模式下无需任何外部服务；默认端口绑定时，**对端要用 `get_rpc_port()` 拼出真实名字**。
- `register_memory` 逐块注册、每块一次 metadata 往返；`batch_register_memory` 打包注册、一次往返，块越多收益越大。
- 同步传输的等待超时随数据量线性增长（`base + length`，约按 1 GiB/s 估算）。
- `batch_register_bench.py` 是官方提供的双角色（target/initiator）基准脚本，是最贴近真实用法的模板。

## 7. 下一步学习建议

- **换元数据后端**：本讲用的是零依赖的 `P2PHANDSHAKE`。生产环境通常用 etcd。下一步可阅读 `mooncake-transfer-engine/example/transfer_engine_bench.cpp` 里解析 `etcd://` / `redis://` 的逻辑（见本讲 4.x 引用的 650-668 行附近），并尝试用一个本地 etcd 替换 P2PHANDSHAKE。
- **进入 Store**：TransferEngine 是底层传输层，之上的 `MooncakeDistributedStore`（`from mooncake.store import MooncakeDistributedStore`）提供 KV cache 语义。建议进入 Store 相关讲义，学习 put/get 与 `register_buffer` / PyTorch tensor 的零拷贝。
- **异步与批量传输**：本讲只用了 `transfer_sync_read`。后续可学习 `batch_transfer_async_write/read` + `get_batch_transfer_status` 的异步批量模式，理解 `batch_id` 的用法。
- **GPU 路径**：若有 CUDA 环境，可阅读 `transfer_engine_py.cpp` 中 `#ifdef USE_CUDA` 段（`transfer_write_on_cuda` 等），了解如何把传输挂到 CUDA stream 上。
