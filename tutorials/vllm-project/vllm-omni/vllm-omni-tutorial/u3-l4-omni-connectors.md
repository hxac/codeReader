# 全解耦通信：OmniConnector 体系

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `OmniConnectorBase` 这个抽象基类定义的 `put / get / cleanup / close` 契约，以及 `metadata`（元数据）在其中扮演的“穿针引线”角色。
- 区分两类连接器：单机用的 `SharedMemoryConnector`（共享内存，可自动配置）与多节点 RDMA 用的 `MooncakeTransferEngineConnector / MoriTransferEngineConnector / YuanrongConnector`，并知道何时该选哪一种。
- 看懂 `OmniConnectorFactory` 的注册/创建机制，以及一份 YAML deploy 配置如何把“stage 边缘”绑定到具体连接器（含未显式声明时的自动 SHM 行为）。
- 跟踪请求转发适配器 `try_send_via_connector / try_recv_via_connector`，理解它是如何把上游 stage 的输出经“数据面 + 控制面”两路送达下游 stage 的。
- 理解 `OmniKVTransferManager` 如何在连接器之上做 KV 缓存的预取（prefetch）与消费，以及它如何为 KV 选择比请求转发更快的“原始字节 / 设备张量”通道。

## 2. 前置知识

在进入连接器之前，先建立三个直觉。

### 2.1 为什么阶段间需要专门的“传输层”

在前一讲（u3-l3）我们已经确立：每个 stage 是一个独立的 `EngineCore` 子进程，由 `StageEngineCoreClient` 经 ZMQ + msgpack 寻址通信。但 ZMQ 这条通道主要用来传**控制类小消息**（请求 ID、采样参数、调度信号）。当一个 AR stage（如 Thinker）要把一大段隐藏态（hidden states）或一串 codec 帧（code2wav）交给下一个 stage（如 Talker）时，再把动辄几十 MB 的张量塞进 ZMQ 消息里序列化，既慢又占用调度线程。于是 vLLM-Omni 在“控制面”之外，新增了一条**数据面**——也就是本讲的 OmniConnector。

### 2.2 什么是 D2H2D，为什么现在大多走它

`D2H2D` = Device → Host → Device。一个在 GPU 上的张量，先“落”到 CPU 内存（Host），通过网络或共享内存搬过去，再“升”回目标 GPU。设计文档把它明确为“当前连接器的工作模式”：

> Current connectors operate in D2H2D (device to host to device) mode. —— [docs/design/feature/disaggregated_inference.md:17-18](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/disaggregated_inference.md#L17-L18)

D2H2D 通用、跨硬件、易实现，但多了一次 GPU↔CPU 拷贝。文档的“Roadmap”把直连设备到设备（NCCL/UCX/IPC）列为未来工作。本讲你会看到：**请求转发**这一路目前确实是标准 D2H2D（经 `OmniSerializer`）；而 **KV 缓存传输**这一路在 RDMA 连接器 + GPU 显存池的组合下，已经能跳过序列化、实现近乎设备到设备的“快路径”。这是理解连接器体系演进的关键脉络。

### 2.3 一条边（edge）是连接器的寻址单位

连接器不是“全局一个”，而是按“stage 边缘”来组织。一条边 `(from_stage, to_stage)`，比如 `("0", "1")`，代表“stage 0 → stage 1”这条数据通道；可以给它单独指定一种传输后端。理解了“边”这个概念，后面 `OmniTransferConfig`、YAML、适配器的所有设计都会变得自然。

### 2.4 名词速查

| 术语 | 含义 |
| :--- | :--- |
| **stage（阶段）** | 一个请求被拆成的顺序子任务，每个 stage 是独立进程（见 u3-l3） |
| **edge（边）** | 两个 stage 之间的数据通道，记为 `(from_stage, to_stage)` |
| **数据面 / 控制面** | 数据面=连接器搬重数据；控制面=stage 队列传轻量通知 |
| **D2H2D** | GPU→CPU→传输→CPU→GPU 的通用搬运路径 |
| **RDMA** | 远程直接内存访问，绕过 CPU、跨节点低延迟拷贝 |
| **fast path（快路径）** | KV 场景下跳过 `OmniSerializer`、直接搬原始字节的通道 |

## 3. 本讲源码地图

本讲涉及的关键文件都在 `vllm_omni/distributed/omni_connectors/` 目录下：

| 文件 | 作用 |
| :--- | :--- |
| [connectors/base.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py) | `OmniConnectorBase` 抽象基类，定义 `put/get/cleanup/close` 契约 |
| [connectors/shm_connector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py) | `SharedMemoryConnector`：单机 POSIX 共享内存实现 |
| [connectors/mooncake_transfer_engine_connector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/mooncake_transfer_engine_connector.py) | `MooncakeTransferEngineConnector`：多节点 RDMA 实现，含 sender/receiver 角色 |
| [factory.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/factory.py) | `OmniConnectorFactory`：名字 → 构造函数的注册表 |
| [utils/config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/config.py) | `ConnectorSpec / OmniTransferConfig`：配置数据结构与“边 → 连接器”映射 |
| [utils/initialization.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py) | YAML 加载、边缘接线、自动 SHM、sender/receiver 角色注入 |
| [adapter.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py) | `try_send_via_connector / try_recv_via_connector`：请求转发的两路适配器 |
| [kv_transfer_manager.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py) | `OmniKVTransferManager`：KV 缓存的提取、传输、预取与消费 |
| [deploy/moss_tts.yaml](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/moss_tts.yaml) | 真实单机 SHM 部署样例 |
| [deploy/qwen3_omni_moe_mori_intranode.yaml](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe_mori_intranode.yaml) | 真实多节点 RDMA（Mori XGMI）部署样例 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应一条由浅入深的链路：

- **4.1** 先看抽象契约 `OmniConnectorBase`（所有连接器长什么样）；
- **4.2** 再看工厂 `OmniConnectorFactory`、配置模型与两类连接器选型（怎么造出来、怎么用 YAML 接到边上）；
- **4.3** 然后看请求转发适配器（怎么把一个 stage 的输出经连接器送到下一个 stage）；
- **4.4** 最后看 `OmniKVTransferManager`（KV 缓存这一更重的载荷怎么走更快的路）。

### 4.1 OmniConnectorBase：put/get 抽象与 metadata 契约

#### 4.1.1 概念说明

`OmniConnectorBase` 是整个连接器体系的“宪法”。无论底层是共享内存、RDMA 还是其它什么传输，对外都只暴露同一组方法。这就让上层（适配器、KV 管理器）可以**用同一套代码**驱动任何后端——这正是“全解耦”的基石。

它最核心的两个方法是 `put` 和 `get`，构成一个“存/取”键值对模型：

- `put(from_stage, to_stage, put_key, data)`：把一个 Python 对象存进去，返回 `(是否成功, 序列化后字节数, 元数据)`。
- `get(from_stage, to_stage, get_key, metadata=None)`：按 key 取回对象，返回 `(对象, 字节数)` 或 `None`。

这里有两个设计要点必须吃透：

**要点一：序列化由连接器自己负责。** 基类的 docstring 明说 “internal serialization handled by connector”（内部序列化由连接器处理）。基类提供了默认的 `serialize_obj / deserialize_obj` 静态方法（走集中的 `OmniSerializer`），子类可以直接复用，也可以在支持原始字节时跳过它（见 4.2 的 RDMA 快路径）。

**要点二：`metadata` 是连接器之间的“握手信物”。** 有些连接器（典型就是共享内存）在 `put` 时会**临时创建资源**（一个 `/dev/shm` 段），这个资源的“地址”必须告诉接收方，否则 `get` 找不到数据。这个地址就是 `put` 返回的第三个值 `metadata`，它必须**经控制面**（stage 队列）送到接收方，再交给 `get`。设计文档把它称为 Metadata Passing：

> Some connectors (e.g., SharedMemoryConnector) generate transient resources during `put()`. This `metadata` must be passed through the control plane so `get()` can locate the data. —— [docs/design/feature/disaggregated_inference.md:56-58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/disaggregated_inference.md#L56-L58)

#### 4.1.2 核心流程

一次完整的“存-通知-取”三步：

```
发送方 stage                                接收方 stage
─────────────                              ─────────────
1. connector.put(key, data)
      └─ 返回 metadata（含资源句柄）
2. 经 stage 队列发送“轻量通知”
   （含 from_connector=True + connector_metadata）
                      │
                      ▼  控制面（ZMQ 小消息）
3. 收到通知 ──▶ connector.get(key, metadata)
                      └─ 凭 metadata 定位数据面资源，取出 data
```

关键在于：**重数据走数据面（连接器），轻句柄走控制面（队列）**。两者用同一个 `key`（通常是 `request_id`）关联。

#### 4.1.3 源码精读

先看抽象契约本身。[connectors/base.py:12-76](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L12-L76) 定义了基类与全部抽象方法：

- `put` 返回三元组 `(success, serialized_size, metadata)`：[connectors/base.py:20-34](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L20-L34)。注意 docstring 里写明 “Metadata may contain transport-specific handles or inline data”——元数据是“传输相关的句柄或内联数据”。
- `get` 接收可选 `metadata`，返回 `(对象, 字节数)`：[connectors/base.py:36-56](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L36-L56)。
- `cleanup / health / close` 管理资源生命周期：[connectors/base.py:58-76](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L58-L76)。

两个值得单独点出的基类设施：

第一，类属性 `supports_raw_data`，默认 `False`：

```python
# Connectors that copy raw payloads directly (e.g. RDMA) should override this to True.
supports_raw_data: bool = False
```
出处 [connectors/base.py:15-18](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L15-L18)。它是 4.4 节 KV 快路径的开关——只有能“原样搬字节”的连接器（如 RDMA）才会把它置 `True`。

第二，`_make_key` 把“用户 key + 边”拼成内部 key：

```python
@staticmethod
def _make_key(key, from_stage, to_stage, separator="@") -> str:
    return f"{key}{separator}{from_stage}_{to_stage}"
```
出处 [connectors/base.py:105-112](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L105-L112)。默认拼成 `{request_id}@0_1`。这样**同一个连接器实例就能服务多条边**，靠 key 的后缀做路由——RDMA 连接器（如 Mooncake）正是这么用的。共享内存连接器则因为只认原始 key 而不走这套拼接。

最后，基类还给子类“免费”提供了上下文管理器与析构回收：只要子类实现 `close()`，就自动获得 `__enter__/__exit__/__del__`，见 [connectors/base.py:82-89](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L82-L89)。这是一处典型的“样板代码下沉到基类”的工程简化。

#### 4.1.4 代码实践

**实践目标**：用最小代码感受“put 存 + get 取 + metadata 串联”的三步契约。

**操作步骤**（源码阅读型 + 本地可选运行）：

1. 打开 [connectors/shm_connector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py)，找到 `put` 方法（[shm_connector.py:37-65](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py#L37-L65)），确认它返回的 `metadata = {"shm": meta, "size": size}`，其中 `meta` 是 `shm_write_bytes` 给出的共享内存句柄。
2. 写一段**示例代码**（非项目原有，仅用于理解契约）：

   ```python
   # 示例代码：仅演示 put/get/metadata 契约，不依赖完整 omni 运行时
   from vllm_omni.distributed.omni_connectors.connectors.shm_connector import SharedMemoryConnector

   conn = SharedMemoryConnector({"stage_id": 0})
   ok, size, metadata = conn.put("0", "1", "req-42", {"hello": [1, 2, 3]})
   print("put ->", ok, size, metadata)        # metadata 含 {"shm": {"name":..., "size":...}}

   obj, n = conn.get("0", "1", "req-42", metadata=metadata)
   print("get ->", obj, n)                     # 应得到 {"hello": [1,2,3]}

   conn.cleanup("req-42")
   conn.close()
   ```

**需要观察的现象**：`put` 成功后，`/dev/shm/` 下会出现名为 `req-42` 的共享内存段与一个 `req-42_lockfile.lock`；`get` 时凭 `metadata["shm"]` 加锁读出；`cleanup` 后段被 `unlink`。

**预期结果**：取回的对象与存入的字典相等，`size` 反映序列化后字节数。

**待本地验证**：以上脚本能否直接运行取决于你的环境是否已安装 `vllm_omni` 及其共享内存工具（`fcntl`/POSIX SHM 仅限 Linux）。若无法运行，改为纯阅读：对照 `put` 与 `_get_data_with_lock`（[shm_connector.py:67-86](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py#L67-L86)）说明“metadata 如何从 put 流到 get”。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `put` 要把 `metadata` 作为返回值，而不是让接收方自己“猜”？

**参考答案**：因为像共享内存、RDMA 这类传输，`put` 会在发送侧创建**临时资源**（SHM 段、内存池偏移、远端地址），其“地址”只有发送方知道。把地址封装进 `metadata` 经控制面送到接收方，接收方在 `get` 时才能定位到正确资源。这是“重数据走数据面、轻句柄走控制面”的关键一环。

**练习 2**：基类的 `supports_raw_data` 默认是 `False`，它的作用是什么？

**参考答案**：它告诉上层（尤其 `OmniKVTransferManager`）“这个连接器能不能直接吃原始字节/张量而不走 `OmniSerializer`”。只有 `True` 的连接器（如 RDMA）才会被选中走“快路径”——直接把 GPU 张量打包成 uint8 搬运，省掉序列化/反序列化开销（见 4.4）。

---

### 4.2 OmniConnectorFactory、配置模型与连接器选型

#### 4.2.1 概念说明

有了抽象契约，下一个问题是“怎么把一个名字（如 `SharedMemoryConnector`）变成一个实例”，以及“一份 YAML 怎么把 stage 边缘绑定到连接器”。这一层由三样东西组成：

- **`OmniConnectorFactory`**：名字 → 构造函数的注册表。所有内置连接器在模块加载时往里注册自己。
- **`ConnectorSpec` / `OmniTransferConfig`**：配置数据结构。`ConnectorSpec` 描述“一个连接器实例的规格”（名字 + extra 配置），`OmniTransferConfig` 描述“整条流水线的所有边各用什么连接器”。
- **`load_omni_transfer_config` 等 initialization 工具**：把 YAML 解析成 `OmniTransferConfig`，并为没显式声明连接器的边**自动补上** `SharedMemoryConnector`。

设计文档给了一张很实用的选型表，覆盖了六种内置连接器：

| 使用场景 | 推荐连接器 | 说明 |
| :--- | :--- | :--- |
| 单机 | SharedMemoryConnector | 不指定时自动配置 |
| 多节点（Mooncake Store） | MooncakeStoreConnector | 基于 TCP，需 Mooncake Master + 元数据服务 |
| 多节点（Mooncake RDMA） | MooncakeTransferEngineConnector | RDMA/TCP 直传 + 受管内存池，最快 |
| 多节点（Mori RDMA） | MoriTransferEngineConnector | 经 Mori IOEngine 的 RDMA 直传 |
| 多节点（Yuanrong） | YuanrongConnector | 需 Yuanrong Datasystem + etcd |
| Ascend NPU P2P | YuanrongTransferEngineConnector | 直接用 Yuanrong TransferEngine，配 NPU IPv4 与 `memory_pool_device: "npu"` |

出处 [docs/design/feature/disaggregated_inference.md:22-29](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/disaggregated_inference.md#L22-L29)。一句话记忆：**单机用 SHM（自动），多机用 RDMA（Mooncake/Mori/Yuanrong）**。

#### 4.2.2 核心流程

从 YAML 到实例的完整装配：

```
deploy YAML (connectors + stages)
        │  load_omni_transfer_config()
        ▼
OmniTransferConfig { (from,to)->ConnectorSpec }
        │  对每条边：若未声明 → 自动补 SharedMemoryConnector
        │  若仍有缺失边 → fail-fast 报错
        ▼
按 stage 拆分：get_connectors_config_for_stage()
        │  入边角色=receiver，出边角色=sender（role 注入）
        ▼
OmniConnectorFactory.create_connector(ConnectorSpec)
        │  按 name 查注册表 → 懒导入 → 实例化
        ▼
{ (from,to): connector_instance }
```

工厂本身的逻辑极其朴素（典型的注册表模式）：

```python
class OmniConnectorFactory:
    _registry: dict[str, Callable[[dict], OmniConnectorBase]] = {}

    @classmethod
    def create_connector(cls, spec: ConnectorSpec) -> OmniConnectorBase:
        if spec.name not in cls._registry:
            raise ValueError(f"Unknown connector: {spec.name}. Available: ...")
        constructor = cls._registry[spec.name]
        return constructor(spec.extra)
```

出处 [factory.py:24-50](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/factory.py#L24-L50)。值得注意的有两点：一是**懒导入**——每个 `_create_xxx` 内部才 `import` 真正的实现类（如 [factory.py:71-80](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/factory.py#L71-L80)），这样单机用户不会因为没装 RDMA 依赖而导入失败；二是注册发生在模块底部，集中登记六个名字 + 一个向后兼容别名 `MooncakeConnector`（[factory.py:128-137](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/factory.py#L128-L137)）。

#### 4.2.3 源码精读

**配置数据结构。** `ConnectorSpec` 就是“名字 + extra”：[utils/config.py:48-53](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/config.py#L48-L53)。`OmniTransferConfig` 持有一个“边 → spec”字典，并提供 `get_connector_for_edge`：[utils/config.py:56-76](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/config.py#L56-L76)。同一文件还定义了 `TRANSFER_ENGINE_CONNECTOR_NAMES`——所有“传输引擎类”连接器（Mooncake/Mori/Yuanrong TE）的集合（[utils/config.py:11-17](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/config.py#L11-L17)），后面你会看到它被用来给这类连接器算 ZMQ 端口、注入角色。

**YAML 加载与自动 SHM。** `load_omni_transfer_config` 是接线核心（[utils/initialization.py:188-357](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L188-L357)）。它做三件事：

1. 解析 `runtime.connectors`（全局命名连接器）和每个 stage 的 `input_connectors / output_connectors`（把边连到命名连接器或内联定义），见 [utils/initialization.py:233-308](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L233-L308)。注意它会**校验同一条边两侧声明的连接器类型必须一致**，否则报类型不匹配。
2. **自动补 SHM**：对没显式声明的边，按 `runtime.edges` 或各 stage 的 `engine_input_source` 推断边，自动塞一个 `SharedMemoryConnector`，并打印 `Auto-configuring SharedMemoryConnector for edge ...`：[utils/initialization.py:310-343](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L310-L343)。
3. **fail-fast**：若仍有“期望存在却没连接器”的边，直接抛 `ValueError`，绝不让流水线带着缺失边启动：[utils/initialization.py:345-352](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L345-L352)。

**角色注入。** 同一条边的 `ConnectorSpec` 本身是“角色中立”的（既不知道自己是发送方还是接收方）。`get_connectors_config_for_stage` 在按 stage 拆分时，根据 stage 在边中的位置注入角色：目标 stage 是该边的 `to_stage` → 入边 → `role=receiver`；stage 0 的出边 → `role=sender`：[utils/initialization.py:146-185](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L146-L185)。这是 RDMA 连接器正确初始化（绑定监听 vs. 只发不收）的前提。

**真实样例一：单机 SHM。** `moss_tts.yaml` 在顶层声明一个名为 `shm` 的共享内存连接器，stage 0 用 `output_connectors.to_stage_1: shm` 发送，stage 1 用 `input_connectors.from_stage_0: shm` 接收：

```yaml
connectors:
  shm:
    name: SharedMemoryConnector
    extra:
      codec_streaming: true
      connector_get_sleep_s: 0.01
      ...
stages:
  - stage_id: 0
    output_connectors:
      to_stage_1: shm
  - stage_id: 1
    input_connectors:
      from_stage_0: shm
```

出处 [deploy/moss_tts.yaml:7-53](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/moss_tts.yaml#L7-L53)。注意 `extra` 里那些 `codec_*` 旋钮并不是 `SharedMemoryConnector.__init__` 直接消费的，而是被流式 codec 分块传输逻辑读取（见 4.3 末尾的 transfer_adapter）。

**真实样例二：多节点 RDMA。** `qwen3_omni_moe_mori_intranode.yaml` 把同样的边换成 Mori，并把内存池放在 GPU 显存上：

```yaml
connectors:
  mori_connector:
    name: MoriTransferEngineConnector
    extra:
      host: "auto"
      zmq_port: 50051
      backend_type: "xgmi"            # AMD Infinity Fabric GPU-to-GPU 直连
      memory_pool_size: 536870912     # 512 MB
      memory_pool_device: "cuda"      # 用 GPU 显存做池
      ...
stages:
  - stage_id: 0
    output_connectors:
      to_stage_1: mori_connector
  - stage_id: 1
    input_connectors:
      from_stage_0: mori_connector
    output_connectors:
      to_stage_2: mori_connector
```

出处 [deploy/qwen3_omni_moe_mori_intranode.yaml:37-100](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe_mori_intranode.yaml#L37-L100)。把它和 `moss_tts.yaml` 对照就能体会“换后端只改 `connectors` 段，拓扑不动”的解耦威力。

#### 4.2.4 代码实践

见本讲 **5. 综合实践**——它就是本模块的实践任务（为 2 阶段流水线编写 SHM YAML 并解释自动行为）。

#### 4.2.5 小练习与答案

**练习 1**：如果你在 YAML 里只写了 stage 的 `engine_input_source: [0]`，却完全没写 `connectors` 段，会发生什么？

**参考答案**：`load_omni_transfer_config` 的自动 SHM 逻辑会推断出边 `("0", 当前stage)`，并为它自动配置一个 `SharedMemoryConnector`（[utils/initialization.py:327-340](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L327-L340)）。所以单机场景下“不写连接器”等价于“全用 SHM”。

**练习 2**：为什么工厂要用懒导入（`_create_xxx` 内部才 import 实现类），而不是在文件顶部直接 import？

**参考答案**：因为 RDMA 类连接器依赖较重且平台相关（Mooncake/Mori 的 C++ 扩展、NPU 的 Yuanrong 运行时）。懒导入确保单机用户即便没装这些依赖，也能正常 import `factory` 并使用 `SharedMemoryConnector`；只有真正请求某个重后端时才触发导入，失败也只影响那一个连接器。

---

### 4.3 请求转发适配器：try_send_via_connector / try_recv_via_connector

#### 4.3.1 概念说明

到目前为止，连接器还只是“被动的存/取原语”。真正把它接进多阶段编排的是 `adapter.py` 里的两个函数。它们由 Orchestrator（见 u3-l2）在“把上游 stage 输出前推到下游 stage”时调用，完成一次**请求级的阶段间转发**。

回顾 u3-l2：Orchestrator 的 `_forward_to_next_stage` 会判断下一 stage 该怎么投递。当这条边配了连接器时，它不走“把整个输入塞进 stage 队列”的普通路径，而是改走连接器路径——这就是 `try_send_via_connector`（发送侧）和 `try_recv_via_connector`（接收侧）的职责。两个函数都遵循“重数据走数据面、轻通知走控制面”的二分法。

#### 4.3.2 核心流程

**发送侧** `try_send_via_connector` 的逻辑（[adapter.py:14-101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L14-L101)）：

```
1. 组装 payload_data = {
       "engine_inputs": 下游真正要的输入,
       "sampling_params": ...,
       "metadata": { 原始 prompt（去掉不可序列化的多模态字段）、阶段转移、时间戳 }
   }
2. connector.put(stage_id, next_stage_id, req_id, payload_data)
      └─ 得到 (success, size, connector_metadata)
3. 若成功：构造一条“轻量通知”投到下游 stage 队列：
   { type:"generate", request_id, sampling_params,
     from_connector:True, from_stage, to_stage,
     connector_metadata（含 SHM/RDMA 句柄） }
4. 记录 metrics.on_forward(...)，返回 True
若 put 抛异常：记录告警，返回 False（让调用方走回退）
```

**接收侧** `try_recv_via_connector` 的逻辑（[adapter.py:104-183](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L104-L183)）：

```
读取任务 task：
- 若 task["from_connector"] == True：
    1. 取 (from_stage, to_stage) 对应的连接器
    2. connector.get(from_stage, to_stage, req_id, metadata=task["connector_metadata"])
    3. 从返回里取出 payload_data["engine_inputs"] 作为下游输入
- 否则（普通队列载荷，如 stage-0 的种子请求）：
    走 maybe_load_from_ipc_with_metrics 解码
```

#### 4.3.3 源码精读

**发送侧关键代码。** payload 组装时有一个细节：发送前会把 `original_prompt` 里**不可序列化的多模态特征字段**剥掉（`mm_kwargs / mm_placeholders / mm_hashes`），因为它们含 `MultiModalKwargsItems`，msgpack 编码器不支持，而下游其实只取 `engine_inputs` 用不到 `original_prompt`：[adapter.py:42-57](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L42-L57)。这是一处“为可序列化性而做的安全裁剪”。

接着 `put` 并把返回的 `metadata` 合并进通知载荷（`connector_metadata`），再经 `next_stage_queue_submit_fn` 投递：[adapter.py:60-77](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L60-L77)。整段被 `try/except` 包住，任何异常都只返回 `False` 并告警 “falling back to queue”：[adapter.py:95-101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L95-L101)。

**接收侧关键代码。** 它先看 `task["from_connector"]` 标志位分流：[adapter.py:115-165](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L115-L165)。带连接器标志时，用 `(from_stage, to_stage)` 当 key 从 `connectors` 字典里**取出对应边的连接器实例**，再 `get`，最后从 `payload_data` 里取 `engine_inputs`。注意它能处理 `get` 返回值既可能是元组也可能是裸对象两种形态：[adapter.py:137-152](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L137-L152)。

**SharedMemoryConnector 如何兑现这套契约。** 它的 `put` 用 `fcntl.flock` 给锁文件加排他锁，再 `shm_write_bytes` 写段，返回 `{"shm": {"name","size"}, "size":...}` 作为 metadata：[shm_connector.py:37-65](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py#L37-L65)。`get` 则优先用 metadata 里的句柄（`_get_data_with_lock`），缺失时退化为“纯按 key 找段”的 `_get_by_key`：[shm_connector.py:88-143](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py#L88-L143)。这种“有句柄走快路、没句柄按 key 兜底”的双路设计，让它既能配合适配器的 metadata 通道，也能容忍某些旧路径不带 metadata。它的类 docstring 说得很直白：SHM 是**本地传输**，不理解 `source_host/source_port` 这类远端元数据，遇到时“静默退化为按 key 查找”：[shm_connector.py:17-25](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/shm_connector.py#L17-L25)。

**补充：分块传输适配器。** 对于 Qwen3-Omni 这类 talker→code2wav 的**流式 codec 帧**，单次 `put` 整段太重，于是还有一层 `transfer_adapter`（`OmniTransferAdapterBase` 及其子类 `chunk_transfer_adapter`）。它在调度器侧起两条后台线程（`recv_loop / save_loop`），把数据**切成小块**反复 `put/get`，并以条件变量做背压（无进展时退避 1ms，避免空转烧 CPU）。基类见 [transfer_adapter/base.py:13-97](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/transfer_adapter/base.py#L13-L97)。前面 `moss_tts.yaml` 里那些 `codec_chunk_frames / codec_left_context_frames` 旋钮就是喂给这条分块路径的。它复用的仍是同一套 `OmniConnectorBase` 契约，只是把“一次性转发”变成了“分批流式转发”。

#### 4.3.4 代码实践

**实践目标**：跟踪一条请求在“两阶段 + SHM”下，从 `_forward_to_next_stage` 到下游拿到 `engine_inputs` 的完整数据面路径。

**操作步骤**（源码阅读型）：

1. 在 [adapter.py:14-101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L14-L101) 的 `try_send_via_connector` 里，标注出三件事分别在哪几行：①组装 payload；②`connector.put`；③投递轻量通知。
2. 在 [adapter.py:104-183](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/adapter.py#L104-L183) 的 `try_recv_via_connector` 里，找到“用 `(from_stage, to_stage)` 取连接器”的那行（提示：`connectors.get(connector_key)`），并确认 `engine_inputs` 是从 payload 的哪个键取出的。
3. 用一句话写下：如果 `put` 抛异常，整个转发会变成什么结果。

**需要观察的现象 / 预期结果**：你应该能画出一幅“payload → put → 通知(含 metadata) → get → engine_inputs”的箭头图，并指出 metadata 在通知载荷里的字段名是 `connector_metadata`。

**待本地验证**：`try_send_via_connector` 的 metrics 回调（`metrics.on_forward(..., True)`）在真实运行时会把这条边标记为“走了连接器”，可在 OrchestratorMonitor（见 u3-l2）里看到对应计数。

#### 4.3.5 小练习与答案

**练习 1**：发送侧为什么要把 `original_prompt` 里的 `mm_kwargs / mm_placeholders / mm_hashes` 剥掉再放进 metadata？

**参考答案**：stage-0 处理后，`render_chat_async` 返回的 prompt 可能携带 `MultiModalKwargsItems` 等对象，而 `OmniMsgpackEncoder` 不支持序列化它们，直接放进去会在 `put` 时抛 `TypeError`。又因为接收侧只从 payload 取 `engine_inputs`、从不读 `original_prompt`（它只是调试用元数据），所以剥掉这些字段既安全又解决了可序列化问题。

**练习 2**：`try_recv_via_connector` 是怎么决定该用哪个连接器实例的？

**参考答案**：它用 `(from_stage, to_stage)` 这个二元组当 key，从传入的 `connectors` 字典里查找（`connector_key = (from_stage, to_stage); connector = connectors.get(connector_key)`）。这正对应了 4.2 里“按边组织连接器”的设计——同一条边在发送侧和接收侧拿到的是配对的两个实例（SHM 下其实可共用同一段 `/dev/shm`，RDMA 下则是 sender/receiver 两个角色）。

---

### 4.4 OmniKVTransferManager：KV 缓存的预取与消费

#### 4.4.1 概念说明

请求转发（4.3）搬运的是“下游 stage 的输入 prompt/隐藏态”，是一条请求的常规载荷。但有一类更特殊、更重的数据需要跨 stage 搬：**KV 缓存（key/value cache）**。典型场景是 PD 分离（Prefill-Decode disaggregation）——prefill 节点算完一长串 token 的 KV，把它原样送到 decode 节点，decode 节点就不用重算 prefill。

KV 缓存有几个让“普通转发”不够用的特点：

1. **巨大**：几十层 × 每层一对 K/V 张量，单请求可达数十~数百 MB。
2. **结构化**：本质是按层组织的张量列表，不需要 msgpack 那种通用对象序列化。
3. **可放在 GPU**：理想情况下整段搬运都在显存里完成，避免 D2H2D 的两次拷贝。

`OmniKVTransferManager` 就是为这类载荷量身打造的“上层管家”。它**仍然使用** `OmniConnectorBase` 的 `put/get`（不另起炉灶），但在其之上做了三件关键优化：①专用的紧凑二进制打包格式 `KVCacheTransferData`；②对 `supports_raw_data=True` 的连接器走“设备张量快路径”，把整段 KV 打包成一个 uint8 GPU 张量直接搬；③异步预取（prefetch）+ 同步消费（consume）的双模接收。

#### 4.4.2 核心流程

**发送侧**（prefill/上游 stage 完成后，把 KV 推给下游）：

```
handle_finished_requests_kv_transfer(finished_reqs, kv_caches, ...)
   │  对每个 finished 请求：
   ▼
_extract_kv_cache(...)
   └─ 从 GPU block 表里按 block_ids 切出每层 K/V，拼成 KVCacheTransferData
_transfer_kv_cache(kv_data, req_id)
   │  _serialize_transfer_payload(kv_data)
   │     ├─ 若 connector.supports_raw_data：kv_data.to_gpu_tensor()  ← 快路径
   │     └─ 否则：kv_data.to_bytes()                                  ← 通用 D2H2D
   ▼
_transfer_with_retry(...) → connector.put(from, to, put_key, data)  （带 3 次指数退避重试）
```

**接收侧**（decode/下游 stage 启动一个请求前，把对应 KV 拉回来）有两种模式：

- **同步**：`receive_kv_cache_for_request` 在循环里轮询 `connector.get`，直到收齐所有 rank 分片或超时（默认 30s）。
- **异步预取**：`start_prefetch` 把“拉 + 反序列化 + H2D”丢到一个单线程 `ThreadPoolExecutor` 的后台线程（用独立 CUDA stream），主线程之后用 `consume_prefetched_kv` 取回结果；预取 miss 时再退化到同步。

接收完成后还要做两件善后：把分片 `merge / slice` 还原成完整 KV，再 `apply_kv_cache_to_request` 把它挂回请求对象（`req.past_key_values`）。

#### 4.4.3 源码精读

**配置与拓扑。** `OmniKVCacheConfig` 聚合了 KV 传输的全部旋钮：连接器配置、`from_stage/to_stage`、`need_send_cache/need_recv_cache`、收发超时、`from_tp/to_tp`（异构张量并行）、以及异步预取开关：[kv_transfer_manager.py:127-142](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L127-L142)。它有多个工厂入口（`from_od_config / from_model_config / from_vllm_config`），能从模型配置或 vLLM 的 `kv_transfer_config` 里提取连接器配置（[kv_transfer_manager.py:486-521](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L486-L521)）。

**连接器的惰性创建。** `OmniKVTransferManager` 不在构造时就建连接器，而是用 `connector` property 惰性创建——第一次访问时才按 `connector_config["type"]` 调工厂，且对传输引擎类连接器（`TRANSFER_ENGINE_CONNECTOR_NAMES`）按 stage/local_rank/replica 算出唯一 ZMQ 端口并注入 sender/receiver 角色：[kv_transfer_manager.py:523-577](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L523-L577)。这里端口公式是 `base + KV_TRANSFER_PORT_OFFSET + replica*STRIDE + rank*STRIDE + stage`（常量见 [utils/initialization.py:26-39](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L26-L39)），目的是让 TP 多 rank、多副本、KV 端口与请求转发端口互不碰撞。还有一处健壮性细节：若创建失败，`self._connector` 被置为 `False`，后续访问直接返回 `None` 而**不会反复重试**——避免每步推理都抛异常。

**紧凑打包格式 KVCacheTransferData。** 它把每层 K/V 张量摊平成 uint8 字节，前面带一个 JSON 头（含 request_id、block_ids、每张量的 dtype/shape/offset/字节数），从而能用一次连续拷贝搬完：发送 `to_bytes()`（CPU）/`to_gpu_tensor()`（GPU）：[kv_transfer_manager.py:158-226](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L158-L226)；接收 `from_bytes / from_bytes_device` 反向重建：[kv_transfer_manager.py:317-345](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L317-L345)。

**快路径选择——本模块的精髓。** `_serialize_transfer_payload` 是“走快路径还是通用路径”的决策点：

```python
def _serialize_transfer_payload(self, kv_data):
    if getattr(self.connector, "supports_raw_data", False):
        try:
            return kv_data.to_gpu_tensor()      # 设备张量，跳过 OmniSerializer
        except Exception:
            pass
    try:
        return kv_data.to_bytes()               # CPU 字节，通用 D2H2D
    except Exception:
        return kv_data.to_dict()                # 兜底：msgpack 对象
```

出处 [kv_transfer_manager.py:741-751](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L741-L751)。这正是 4.1 提到的 `supports_raw_data` 开关在此处的兑现：RDMA 连接器（如 Mooncake TE）把它设为 `True`（[mooncake_transfer_engine_connector.py:101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/mooncake_transfer_engine_connector.py#L101)），于是 KV 经由 GPU 显存池直接 RDMA 搬运，逼近设计文档承诺的“未来 D2D”。而 SHM 连接器 `supports_raw_data=False`，KV 只能走 `to_bytes()` 的标准 D2H2D。

**发送：提取 + 重试。** `handle_finished_requests_kv_transfer` 遍历完成请求，逐个 `_extract_kv_cache`（按 block 切张量）后 `_transfer_kv_cache`，底层 `_transfer_with_retry` 最多重试 3 次、指数退避：[kv_transfer_manager.py:967-1211](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L967-L1211)。

**接收：轮询 + 超时 + 分片合并。** `receive_kv_cache_for_request` 是同步接收主体：它先按 TP 拓扑用 `build_rank_aware_recv_keys` 生成一组 `(get_key, from_rank)`，然后在 `while True` 里逐个 `connector.get`，收齐后 `merge_received_rank_shards / slice_received_rank_shard` 还原，超时则返回 `(None, 0)`：[kv_transfer_manager.py:1376-1547](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L1376-L1547)。轮询间隔从 10ms 起倍增到 500ms 封顶（`poll_interval`），兼顾响应性与低 CPU 占用。

**异步预取：双模接收的另一半。** `start_prefetch` 把整段“get + 反序列化 + H2D”提交到单线程池（`max_workers=1`，避免多 stream 竞争），并在独立 `_bg_copy_stream` 上做 GPU 拷贝：[kv_transfer_manager.py:1247-1312](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L1247-L1312)。主线程随后用 `consume_prefetched_kv` 取回 `future` 结果；若预取 miss（未提交或被丢弃），再退化到同步 `receive_multi_kv_cache`：[kv_transfer_manager.py:1314-1389](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L1314-L1389)。这里有个很重要的语义：若 payload 已经从连接器里**取出来却后处理失败**，会抛 `KVPrefetchConsumeError`——因为数据已消费、无法再同步重试，必须让请求失败而非无限阻塞。

**分布式一致性与分发。** 在 TP+CFG/SP 等并行下，并非每个 rank 都自己去拉 KV：`topo_config`（`_TransferTopoConfig`）把每个 rank 归为 `LOCAL / LEADER / FOLLOWER` 三种 `ReceiveRole`（[kv_transfer_manager.py:50-98](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L50-L98)）。LEADER 拉到后用 `distribute_kv_cache` 经 collective（broadcast/scatter）分发给 FOLLOWER。还有一处防悬挂设计 `_tp_local_receive_consensus`：纯 TP 下每个 rank 各拉各的分片，若某个 rank 静默 miss 而别的 rank 命中，会让一部分进 prefill、一部分进 collective，导致 NCCL 看门狗杀进程；于是它在 TP 组内交换“是否收到 KV”的签名，不一致就**全部丢弃 KV、一起走无 KV 的兜底**：[kv_transfer_manager.py:1788-1844](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L1788-L1844)。这是分布式系统里典型的“宁可整体降级、不可部分失步”。

#### 4.4.4 代码实践

**实践目标**：理解 KV 传输“快路径 vs 通用路径”的选择条件，并能从源码指出决策点。

**操作步骤**（源码阅读型）：

1. 在 [kv_transfer_manager.py:741-751](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L741-L751) 找到 `_serialize_transfer_payload`，写下：当 `connector.supports_raw_data` 分别为 `True/False` 时，KV 数据会走哪两个分支。
2. 在 [mooncake_transfer_engine_connector.py:98-101](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/mooncake_transfer_engine_connector.py#L98-L101) 确认 Mooncake TE 把它置 `True`；在 [connectors/base.py:15-18](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/connectors/base.py#L15-L18) 确认 SHM 没覆盖它（故为 `False`）。
3. 跟踪 `start_prefetch` → `_prefetch_payload` → `receive_kv_cache_for_request` → `consume_prefetched_kv` 这条异步链（[kv_transfer_manager.py:1247-1338](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L1247-L1338)），指出预取 miss 时回退到同步接收的判断条件是哪一行。

**需要观察的现象 / 预期结果**：你能复述“快路径 = `to_gpu_tensor()` + 设备内存池 RDMA；通用路径 = `to_bytes()` + CPU 序列化”，并能解释为什么 SHM 只能走通用路径。

**待本地验证**：是否启用异步预取由 `OmniKVCacheConfig.enable_kv_async_prefetch` 与 `_resolve_async_prefetch` 共同决定（`has_companion` 或接收池在设备上时会强制关闭，见 [kv_transfer_manager.py:479-492](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/kv_transfer_manager.py#L479-L492)）。真实运行时可观察日志中 `KV prefetch miss ...; falling back to sync receive` 与 `KV transfer OK: ... %.1f MB/s` 两类行，体会两种模式的实际触发。

#### 4.4.5 小练习与答案

**练习 1**：KV 传输为什么不像请求转发那样直接走 `OmniSerializer`，而要专门设计 `KVCacheTransferData` 的二进制格式？

**参考答案**：KV 缓存是“按层成对的规则张量列表”，用通用 msgpack 序列化既慢又费内存。`KVCacheTransferData` 把所有张量摊平成连续 uint8 字节 + 一个描述 dtype/shape/offset 的 JSON 头，于是能一次连续拷贝完成搬运；并且当连接器支持原始数据时，还能整段留在 GPU 上打包成一个设备张量，省掉 D2H2D。这是“为重型结构化载荷定制打包”的典型优化。

**练习 2**：`_tp_local_receive_consensus` 为什么要在“不一致”时**主动丢弃**所有 rank 的 KV，而不是让收到 KV 的 rank 继续用？

**参考答案**：在张量并行里，同一请求的所有 rank 必须**同步**地进入同一套 collective（prefill 或 KV 复用）。若个别 rank 静默 miss 而别的 rank 命中，就会“一部分走 prefill、一部分走 KV 复用的 collective”，二者永不交汇，触发 NCCL 看门狗杀进程。所以宁可让全体一起降级到无 KV 路径、保持步调一致，也不能让少数 rank 单独带 KV 前进——这是用“整体降级”换“不悬挂”。

---

## 5. 综合实践

**任务**：参考 [docs/design/feature/disaggregated_inference.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/feature/disaggregated_inference.md) 与真实样例 `moss_tts.yaml`，为一个**两阶段流水线**（stage 0 = 文本编码/AR，stage 1 = 解码/生成）手写一份 deploy YAML，让 stage 0 的输出经 `SharedMemoryConnector` 送到 stage 1，stage 1 从同一连接器读取；然后回答“若完全不写 `connectors` 段会怎样”。

**操作步骤**：

1. 新建一个本地文件 `my_two_stage.yaml`（仅作练习，不要提交到仓库），采用项目当前使用的新 schema（顶层 `connectors` + `stages`，参考 [moss_tts.yaml:7-53](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/moss_tts.yaml#L7-L53)）：

   ```yaml
   # 示例代码：练习用 2 阶段 SHM deploy 配置（非项目自带文件）
   connectors:
     shm:
       name: SharedMemoryConnector
       extra:
         connector_get_sleep_s: 0.01

   stages:
     - stage_id: 0
       devices: "0"
       output_connectors:
         to_stage_1: shm          # stage0 是 "0"->"1" 这条边的发送方
       default_sampling_params:
         temperature: 0.0
         max_tokens: 512

     - stage_id: 1
       devices: "1"
       input_connectors:
         from_stage_0: shm        # stage1 是同一条边的接收方
       default_sampling_params:
         temperature: 0.0
         max_tokens: 256
   ```

2. **自检边的一致性**：对照 [utils/initialization.py:266-308](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L266-L308) 确认——stage 0 的 `to_stage_1` 与 stage 1 的 `from_stage_0` 描述的是**同一条边** `("0","1")`，且两侧声明的连接器名都是 `shm`（共用全局 `connectors.shm`），因此不会触发“类型不匹配”错误。

3. **解释自动行为**：现在把整个 `connectors` 段**删掉**，并把两个 stage 的 `output_connectors / input_connectors` 也删掉，只保留 stage 1 的 `engine_input_source: [0]`。根据 [utils/initialization.py:327-340](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/distributed/omni_connectors/utils/initialization.py#L327-L340)，系统会从 `engine_input_source` 推断出边 `("0","1")` 并**自动配置**一个 `SharedMemoryConnector`。也就是说：单机两阶段场景下，“什么都不写”和“显式写 SHM”在传输后端上**等价**，区别只在于你是否能通过 `extra` 注入 `codec_*` 等旋钮。

4. **对比多机**：把 `connectors.shm` 段替换成 Mooncake RDMA（参考 [qwen3_omni_moe_mori_intranode.yaml:37-58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/deploy/qwen3_omni_moe_mori_intranode.yaml#L37-L58) 的写法，把 `name` 换成 `MooncakeTransferEngineConnector` 并加 `host/zmq_port/memory_pool_*`）。体会“只改 `connectors` 段、拓扑与 stage 不动”的解耦——这正是 OmniConnector 抽象的价值。

**需要观察的现象 / 预期结果**：

- 显式 SHM 版：启动日志应出现 `Created connector for 0 -> 1: SharedMemoryConnector`。
- 删除连接器段、仅留 `engine_input_source` 版：日志应出现 `Auto-configuring SharedMemoryConnector for edge ('0', '1')`，且不会触发 fail-fast。
- 若你**既不写连接器、又不写 `engine_input_source`、也没有 `runtime.edges`**，则该边不会进入 `expected_edges`，自然也不会报“缺失”——但运行时该 stage 收不到上游输入会另行报错。

**待本地验证**：以上日志需在有对齐 vLLM 与可用模型权重的环境里、用 `vllm serve <model> --omni --deploy-config my_two_stage.yaml` 才能观测到。无 GPU/权重时，请退化为纯源码阅读：把三段 YAML 与 `load_omni_transfer_config` 的解析分支逐一对应，验证你的预期。

## 6. 本讲小结

- `OmniConnectorBase` 用 `put/get/cleanup/close` 四个抽象方法 + 一份 `metadata` 契约，统一了从共享内存到 RDMA 的所有传输后端；`metadata` 是“put 产生临时资源、get 凭它定位”的握手信物，经控制面（stage 队列）传递。
- `OmniConnectorFactory` 是名字→构造函数的注册表，靠**懒导入**隔离重平台依赖；`ConnectorSpec/OmniTransferConfig` 以“边 `(from,to)`”为寻址单位组织连接器，`load_omni_transfer_config` 解析 YAML、对未声明边自动补 `SharedMemoryConnector`，并对缺失边 fail-fast。
- 选型口诀：**单机用 SHM（可自动）、多机用 RDMA（Mooncake/Mori/Yuanrong）**；同一拓扑换后端只需改 YAML 的 `connectors` 段。
- 请求转发适配器 `try_send_via_connector/try_recv_via_connector` 践行“重数据走数据面、轻通知走控制面”：发送侧 `put` 后把句柄塞进 `connector_metadata` 投队列，接收侧凭 `(from_stage,to_stage)` 取连接器再 `get` 回 `engine_inputs`；流式 codec 帧则由 `transfer_adapter` 切块反复搬运。
- `OmniKVTransferManager` 在同一 `put/get` 契约上为 KV 缓存做了三件加法：紧凑二进制打包 `KVCacheTransferData`、对 `supports_raw_data=True` 的 RDMA 连接器走“设备张量快路径”（逼近 D2D）、以及“异步预取 + 同步消费”双模接收，并用 TP 组一致性校验防止部分 rank 失步。
- 当前请求转发主路径仍是 D2H2D（经 `OmniSerializer`）；KV 路径在 RDMA+GPU 池下已能跳过序列化——这与设计文档“Roadmap: D2D”一致，是连接器体系演进的主线。

## 7. 下一步学习建议

- **横向：负载均衡与副本管理**。本讲聚焦“一条边怎么传”，但当一条边的下游有多个副本时，请求该发给谁？这正是下一讲 **u3-l5 OmniCoordinator** 的主题——它管理副本注册、心跳与 `LEAST_QUEUE_LENGTH` 等负载均衡策略，与连接器共同构成完整的分布式解耦传输。
- **纵向：Diffusion 子系统如何消费连接器**。`OmniKVTransferManager` 的接收/分发逻辑会与 diffusion 的并行拓扑（TP/SP/CFG）深度交织，建议在学完 **U5（Diffusion 模块）** 和 **u7-l4（并行策略）** 后回看本讲 4.4 的 `topo_config / distribute_kv_cache`，会有更深体会。
- **动手：跟踪一次真实跨 stage 传输**。在能运行的环境里启动 `moss_tts.yaml`（SHM）或 `qwen3_omni_moe_mori_intranode.yaml`（RDMA），用日志里的 `Created connector for ...`、`KV transfer OK: ... MB/s`、`Auto-configuring SharedMemoryConnector for edge ...` 三类行，把本讲的四个模块在真实运行中一一对应。
