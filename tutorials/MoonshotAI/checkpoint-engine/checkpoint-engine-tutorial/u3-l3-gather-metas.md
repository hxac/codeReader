# gather_metas：全局元数据收集

## 1. 本讲目标

上一讲（u3-l2）我们弄清了「注册」：每个 rank 把自己分到的权重锁页进内存，`_memory_pool` 里躺着一个个 `MemoryBuffer`（真实的大张量）。但此时每个 rank 仍然只看得见自己那一亩三分地——rank 0 不知道 rank 3 有哪些权重、锁在哪块内存上。本讲解决的就是这个「互相认识」的问题。

学完本讲，你应该能够：

1. 说出 `DataToGather` 中每个字段的用途，理解「只传元数据、不传权重」的设计动机。
2. 掌握 `gather_metas` 的完整流程：它如何用 `all_gather_object` 做一次控制面上的「人口普查」，并把结果加工成 `_current_global_parameter_metas`（以 owner_rank 为键的全局参数表）和 `_local_rdma_devices`（本集群的 RDMA 网卡拓扑）。
3. 说明 `get_metas` 如何把全局参数表导出成 JSON，以及 `load_metas` 如何用外部 metas **整体替换**全局参数表并**改写远端拓扑**——这正是 join 复用模式（u6-l3）的入口。

## 2. 前置知识

本讲不需要新的分布式理论，但要把几个概念说透。

### 2.1 元数据与数据的分离

一次权重更新要搬运的数据量是 TB 级的，但「描述这些数据」所需的信息非常少：

- 每个参数：名字、dtype、shape、对齐后的大小（一个 `ParameterMeta`，几十字节）；
- 每块锁页 buffer：首地址 `ptr`、总字节数 `size`、参数清单；
- 每个 rank：P2P store 地址、主机 IP、设备 UUID、RDMA 网卡名。

用图书馆打比方：`gather_metas` 交换的是**书目卡片**（哪本书在哪个书架、第几排），而不是把书复印一遍。书目交换完之后，后续 `update` 阶段才真正按图索骥地搬书（broadcast 走 NCCL/HCCL，P2P 走 RDMA 远端读）。形式化地说：设全集群权重总量为 \(\sum_i \text{size}_i\)（TB 量级），一次 gather 传输的只是 \(\sum_i |\text{metas}_i|\) 条目录（KB～MB 量级），两者的差距在 6 到 9 个数量级。

### 2.2 all_gather_object 是什么

`torch.distributed.all_gather_object` 是一个集合通信原语：每个进程提交一个任意可 pickle 的 Python 对象，调用结束后**每个进程**都拿到一个长度为 `world_size` 的列表，第 `i` 个元素是 rank `i` 提交的对象。它的底层实现是「pickle 成字节流 → 转成字节张量 → 先 all_gather 各自的长度、再 all_gather 补齐到最大长度的字节 → 各自反序列化」。本项目在 `distributed` 包里留了一份这个算法的参考实现（给自定义 NCCL/HCCL 后端用），精读时我们会看到。

两个必须记住的性质：

- **集体性**：所有 rank 都必须调用它，缺一个进程全体挂起（hang）。
- **入口统一**：`ps.py` 里写的是 `dist.all_gather_object`，这里的 `dist` 是 `checkpoint_engine.distributed` 抽象层（u5-l2 的主题），不是直接调 `torch.distributed`——这样 vLLM NCCL/HCCL 后端才能替换同一套调用。

### 2.3 owner_rank：权重的「户主」

回顾 u1-l2：`examples/update.py` 会把 checkpoint 文件（或张量）在 `world_size` 个 rank 之间均分。某个参数被分到 rank `i`，rank `i` 就是它的 **owner**。`gather_metas` 结束后得到的全局参数表就是一张「户口簿」：键是 owner_rank，值是该 rank 名下所有 buffer 的元数据。后续广播按 owner 聚合切桶（u3-l5），P2P 更新则直接按 owner 的地址远端读取。

### 2.4 RDMA 拓扑：网卡、主机与 rank 的三方关系

P2P 更新（u5-l5 详述）走 RDMA：数据从 owner 所在主机的某张 RDMA 网卡直接写入 receiver 的显存。一台主机可能插多张 RDMA 网卡，主机上的多个 rank 会分摊到不同网卡。于是需要一个映射：

\[
\text{topo}:\ \text{网卡名}@\text{主机IP}\ \longrightarrow\ 2^{\{0,1,\dots,W-1\}}
\]

即「某主机的某张网卡」→「共享它的 rank 集合」。`gather_metas` 顺带完成了这张拓扑图的构建，它是 u5-l6 带宽最大化分配算法的输入。

### 2.5 你应该已经知道的事

- `register_checkpoint` 之后，`_memory_pool[名字]` 是 `list[MemoryBuffer]`，每个 `MemoryBuffer` 含扁平 uint8 锁页 buffer、有序的 `ParameterMeta` 列表和 `manually_pinned` 标志（u2-l3、u3-l2）。
- rank 按主机优先连续分配，`local_rank = rank % gpu_count`（u3-l1）。
- `_device_uuid`（形如 `GPU-<uuid>` / `NPU-...`）是 PS 命名 ZMQ 抽象 socket 的钥匙（u3-l1）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | ParameterServer 主体 | `gather_metas` / `get_metas` / `load_metas`，以及三份全局状态的构建与消费 |
| [checkpoint_engine/data_types.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py) | 数据模型 | `ParameterMeta` → `MemoryBufferMetas` → `MemoryBufferMetaList` → `DataToGather` 继承链 |
| [checkpoint_engine/distributed/base.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py) | 分布式抽象层 | `all_gather_object` 的抽象声明、TorchBackend 实现、模块级分发 |
| [checkpoint_engine/p2p_store.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py) | mooncake 封装 | `addr` 属性——`p2p_store_addr` 字段的来源 |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | 硬件抽象 | `get_ip()`——`host_ip` 字段的来源 |
| [checkpoint_engine/api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py) | HTTP 服务层 | `/v1/checkpoints/{name}/gather-metas`、`GET/POST /v1/metas` 端点 |
| [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) | 驱动脚本 | metas 的 JSON 导出（`--save-metas-file`）与 join 流程 |
| [tests/test_api.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py) | CPU 单元测试 | metas 端点的回环用例，本讲代码实践的主要依托 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先认识「传什么」（DataToGather），再看「怎么传、怎么加工」（gather_metas），然后是两个方向相反的出口与入口（get_metas 导出、load_metas 导入）。

### 4.1 DataToGather：一次集合通信里到底传了什么

#### 4.1.1 概念说明

注册阶段每个 rank 手里是 `list[MemoryBuffer]`——含真实 `torch.Tensor` 的重量级对象。要让全集群知道「谁有哪些权重、放在哪个地址」，需要一个**可 pickle、可 JSON 化的瘦身影子**，这就是 `DataToGather`。

它的设计有一个巧妙之处：**继承即裁剪**。`DataToGather` 直接继承自 `MemoryBufferMetaList`，只在父类三个字段之外追加 `host_ip` 和 `device_uuid` 两个寻址字段。收集完成后，把这两个字段裁掉，剩下的就恰好是存入全局参数表的 `MemoryBufferMetaList`——同一套模型贯穿「传输中」和「存储后」两个阶段，不需要任何转换代码。

#### 4.1.2 核心流程

每个 rank 在 `gather_metas` 内部打包自己的 `DataToGather`：

```text
for x in memory_pool:                        # 每块锁页 buffer
    MemoryBufferMetas(metas=x.metas,         # 参数清单（有序，offset 由顺序隐含）
                      ptr=x.buffer.data_ptr(),  # 进程虚拟地址
                      size=x.size)              # buffer 总字节数
DataToGather(memory_buffer_metas_list=[...],
             p2p_store_addr=...,   # "ip:port"，无 P2PStore 则 None
             host_ip=get_ip(),
             device_uuid=self._device_uuid,
             rdma_device=...)      # 本 rank 绑定的网卡名，无则 ""
```

各字段用途一览：

| 字段 | 类型 | 来源 | 用途 |
| --- | --- | --- | --- |
| `memory_buffer_metas_list` | `list[MemoryBufferMetas]` | 遍历 `_memory_pool` | 告诉别人「我名下每块 buffer 里有哪些参数、在哪个地址、多大」 |
| `p2p_store_addr` | `str \| None` | `P2PStore.addr` | P2P 更新时远端 RDMA 读的**服务端地址**；`None` 表示本 rank 没有 P2P 能力 |
| `rdma_device` | `str` | `P2PStore.device` | 拓扑 key 的网卡部分；无 P2PStore 时为空串 |
| `host_ip` | `str` | `get_ip()` | 拓扑 key 的回退项（无 P2P 时按主机分组）；`_all_hosts` 的来源 |
| `device_uuid` | `str` | `_get_physical_gpu_id` | 填充 `_global_device_uuids`，update 阶段 ZMQ 寻址用（u3-l6） |

注意 `ptr` 是**进程虚拟地址**，离开本进程毫无意义；它必须配合 `p2p_store_addr`（mooncake transfer engine 已把这块内存注册成可远端访问）才能被别人读取。这就是「元数据 + 注册过的内存」共同构成 P2P 读取能力的原理。

#### 4.1.3 源码精读

先看数据模型的三层结构。[checkpoint_engine/data_types.py:L90-L93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L90-L93) 定义 `MemoryBufferMetas`：一块锁页 buffer 的元数据（参数清单 + 指针 + 大小），对应 `MemoryBuffer` 去掉张量本体后的影子：

```python
class MemoryBufferMetas(BaseModel):
    metas: list[ParameterMeta]
    ptr: int
    size: int
```

[checkpoint_engine/data_types.py:L103-L111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L103-L111) 定义 `MemoryBufferMetaList` 与 `DataToGather`——后者只多两个寻址字段：

```python
class MemoryBufferMetaList(BaseModel):
    p2p_store_addr: str | None
    memory_buffer_metas_list: list[MemoryBufferMetas]
    rdma_device: str


class DataToGather(MemoryBufferMetaList):
    host_ip: str
    device_uuid: str
```

`ParameterMeta`（[checkpoint_engine/data_types.py:L71-L74](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L71-L74)）只有 `name/dtype/shape/aligned_size` 四个字段，不含 `offset`——因为参数在 buffer 内的位置由列表顺序和 `aligned_size` 逐个累加隐含（u2-l1、u2-l3 讲过的对齐槽位设计）。

再看打包处。[checkpoint_engine/ps.py:L476-L489](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L476-L489) 在 `gather_metas` 里把 `_memory_pool` 压缩成 `DataToGather`：

```python
metas = DataToGather(
    memory_buffer_metas_list=[
        MemoryBufferMetas(
            metas=x.metas,
            ptr=x.buffer.data_ptr(),
            size=x.size,
        )
        for x in memory_pool
    ],
    p2p_store_addr=None if self._p2p_store is None else self._p2p_store.addr,
    host_ip=get_ip(),
    device_uuid=self._device_uuid,
    rdma_device=self._rdma_device or "",
)
```

三个寻址字段的来源：

- [checkpoint_engine/p2p_store.py:L47-L49](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L47-L49)：`addr` 属性拼出 `"ip:port"`（`ip` 来自 engine 初始化，`port` 来自 `get_rpc_port()`）。
- [checkpoint_engine/device_utils.py:L14-L26](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L14-L26)：`get_ip()` 用「UDP connect 8.8.8.8 再查本端地址」的技巧拿到出口 IP，失败则退回主机名解析；`@lru_cache(maxsize=1)` 保证每进程只算一次。
- `self._rdma_device or ""`（[checkpoint_engine/ps.py:L251](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L251)）：没有 P2PStore 的 rank（XPU、或未装 mooncake）取空串。

一个平台细节：NPU 上 `transfer_engine_protocol` 是 `ascend_direct`，[checkpoint_engine/device_utils.py:L267-L269](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L267-L269) 让 `rdma_device` 恒为空串，于是拓扑 key 退化为 `"@ip"`——按主机分组。也就是说「网卡粒度」的拓扑只存在于 CUDA 的 rdma/efa 协议下。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个 `DataToGather`，观察它序列化成 JSON 后的形态，验证「元数据可 JSON 往返」。

**操作步骤**（需要 `torch` 与 `pydantic`，纯 CPU 即可，无需 GPU 和分布式环境）：

```python
# metas_json_lab.py —— 示例代码（非项目原有文件）
import torch
from pydantic import TypeAdapter
from checkpoint_engine.data_types import DataToGather, MemoryBufferMetas, ParameterMeta

d = DataToGather(
    memory_buffer_metas_list=[
        MemoryBufferMetas(
            metas=[ParameterMeta(name="w0", dtype=torch.bfloat16,
                                 shape=torch.Size([4, 8]), aligned_size=512)],
            ptr=0x7F0000000000,
            size=512,
        )
    ],
    p2p_store_addr="10.0.0.1:10001",
    host_ip="10.0.0.1",
    device_uuid="GPU-uu0",
    rdma_device="mlx5_0",
)
adapter = TypeAdapter(DataToGather)
js = adapter.dump_json(d)
print(js.decode())
print(TypeAdapter(DataToGather).validate_json(js) == d)
```

在仓库根目录运行 `python metas_json_lab.py`。

**需要观察的现象**：

1. JSON 里 `dtype` 变成了字符串 `"torch.bfloat16"`、`shape` 变成了数组 `[4, 8]`——这正是 u2-l1 讲过的 `_TorchDtype`/`_TorchSize` 自定义序列化在起作用。
2. `ptr` 就是一个普通整数（虚拟地址的十进制表示），没有任何张量数据跟随。
3. 最后一行应打印 `True`（JSON 往返无损）。

**预期结果**：往返相等返回 `True`；JSON 的字段顺序为 `memory_buffer_metas_list`、`p2p_store_addr`、`rdma_device`、`host_ip`、`device_uuid`（父类字段在前）。具体字节级输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DataToGather` 里不直接放 `torch.Tensor`？

**答案**：`all_gather_object` 走 pickle，放张量等于把整个权重的字节都序列化、集合通信一遍，代价是 TB 级；而元数据只有 KB～MB 级。此外 `MemoryBufferMetaList` 还要能被 FastAPI 与 `TypeAdapter` JSON 化（`get_metas` 端点、metas 文件导出），张量本体会破坏这条通路。

**练习 2**：`host_ip` 和 `device_uuid` 为什么不放进 `MemoryBufferMetaList`，而要等收集完再裁掉？

**答案**：这两个字段只在收集阶段有用——`host_ip` 进 `_all_hosts`、`device_uuid` 进 `_global_device_uuids`（ZMQ 寻址用）。后续的桶切分、bucket size 探测、RDMA 读都不需要它们；裁掉之后全局参数表保持最小，`dict[int, MemoryBufferMetaList]` 的 schema 也因此能稳定地作为 JSON 导出格式。

**练习 3**：某条 metas 的 `p2p_store_addr` 是 `None`，意味着什么？

**答案**：该 rank 没有初始化 `P2PStore`（设备是 XPU，或 CUDA/NPU 上 `import mooncake` 失败）。它不能作为 P2P 更新的 owner 被远端读取；构建拓扑 key 时会回退用它的 `host_ip` 分组。这也解释了 `load_metas` 为什么断言 `p2p_store_addr is not None`——含 `None` 地址的 metas 无法支撑 join 模式的 RDMA 拉取。

### 4.2 gather_metas：all_gather_object 主流程与全局参数表构建

#### 4.2.1 概念说明

`gather_metas` 是 PS 生命周期「注册 → **收集** → 更新 → 注销」的第二步。它做一次控制面上的全员「人口普查」，把各 rank 的 `DataToGather` 汇成 `world_size` 长度的列表，然后加工出三份全局状态：

1. `_current_global_parameter_metas`：全局参数表（户口簿），键是 owner_rank；
2. `_local_rdma_devices`：本集群「网卡@主机 → rank 集合」的拓扑图；
3. `_all_hosts` 与 `_global_device_uuids`：主机列表与设备 UUID 列表（只在第一次收集时填充）。

为什么要把 `gather_metas` 单独成一步、而不是并进 `update`？因为元数据收集一次、可以服务多次更新——`examples/update.py` 中 `--update-method all` 就是 gather 一次之后先 broadcast 再 p2p 各更新一遍（[examples/update.py:L113-L128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L113-L128)）。

#### 4.2.2 核心流程

```text
gather_metas(checkpoint_name):
    1. 保证进程组存在（auto_pg 且未初始化 → init_process_group）
    2. 取本 rank 的内存池：
       _get_memory_pool(名字) 抛 RuntimeError（未注册）→ memory_pool = []
       【空注册的 rank 也必须走到 all_gather_object，集体通信不能缺席】
    3. 打包 DataToGather（见 4.1）
    4. dist.all_gather_object(metas_lst, metas)
       → 每个 rank 都拿到 world_size 份 DataToGather
    5. 遍历 metas_lst，逐 rank 加工：
       a. i % gpu_count == 0 的 rank 贡献 host_ip 进 all_hosts（每主机一个代表）
       b. 每个 rank 贡献 device_uuid 进 global_device_uuids
       c. memory_buffer_metas_list 非空 → _current_global_parameter_metas[i] = 裁剪后的 MemoryBufferMetaList
       d. _local_rdma_devices[key].add(i)，key = "网卡名@ip"（无 p2p 则用 host_ip）
    6. _remote_rdma_devices = _local_rdma_devices.copy()   ← 默认假设：收发双方拓扑相同
    7. 记录日志：num_parameters
```

形式化地，产出两张映射：

\[
G:\ \text{owner\_rank} \to \text{MemoryBufferMetaList},\qquad |G| \le W
\]

（空注册的 rank 不入 `G`，所以 `|G|` 可以小于 world_size \(W\)）；以及 2.4 节的拓扑映射 \(\text{topo}\)。第 6 步是本讲最重要的语义之一：**colocated 架构（收发同一个集群）下，发送方拓扑 = 接收方拓扑是默认假设**；当这个假设不成立（join 一个新集群）时，用 `load_metas` 改写远端拓扑——见 4.4。

#### 4.2.3 源码精读

**入口与进程组**。[checkpoint_engine/ps.py:L462-L475](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L462-L475)：

```python
def gather_metas(self, checkpoint_name: str):
    if self._auto_pg and not dist.is_initialized():
        self.init_process_group()
    assert dist.is_initialized(), "process group is not initialized"
    metas_lst: list[DataToGather | None] = [None for _ in range(self._world_size)]
    try:
        memory_pool = self._get_memory_pool(checkpoint_name)
    except RuntimeError:
        memory_pool = []
```

三个细节：`metas_lst` 预先按 `world_size` 撑开（`all_gather_object` 按下标回填）；`_get_memory_pool` 对未注册的名字抛 `RuntimeError`（[checkpoint_engine/ps.py:L277-L286](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L277-L286)），这里捕获后降级为空列表——**join 模式里新实例从未注册过 checkpoint，靠这个降级才能参与 gather**；进程组的创建走 u3-l1 讲过的自增前缀 `PrefixStore` 机制（[checkpoint_engine/ps.py:L527-L548](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L527-L548)）。

**集合通信**。[checkpoint_engine/ps.py:L491](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L491) 一行完成交换：

```python
dist.all_gather_object(metas_lst, metas)
```

这里的 `dist` 是 [checkpoint_engine/ps.py:L15](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L15) 导入的 `checkpoint_engine.distributed` 抽象层。它在 [checkpoint_engine/distributed/base.py:L262-L267](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L262-L267) 分发到全局单例 `_BACKEND_INSTANCE`；抽象接口声明在 [checkpoint_engine/distributed/base.py:L55-L62](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L55-L62)，默认 TorchBackend 直接转调 `torch_dist.all_gather_object`（[checkpoint_engine/distributed/base.py:L126-L129](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L126-L129)）。对象如何变成集合通信里的字节？参考实现是 [checkpoint_engine/distributed/base.py:L193-L218](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L193-L218) 的 `_common_all_gather_object`：pickle 成字节张量 → all_gather 各自长度 → 补齐到最大长度再 all_gather → 逐段反序列化。

**后处理循环**。[checkpoint_engine/ps.py:L493-L515](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L493-L515)：

```python
self._current_global_parameter_metas = {}

num_parameters = 0
all_hosts: list[str] = []
global_device_uuids: list[str] = []
for i, metas_buckets in enumerate(metas_lst):
    assert metas_buckets is not None, f"metas_buckets {i} should not be None"
    if i % self._gpu_count == 0 and not self._all_hosts:
        all_hosts.append(metas_buckets.host_ip)
    if not self._global_device_uuids:
        global_device_uuids.append(metas_buckets.device_uuid)
    if metas_buckets.memory_buffer_metas_list:
        self._current_global_parameter_metas[i] = MemoryBufferMetaList(
            memory_buffer_metas_list=metas_buckets.memory_buffer_metas_list,
            p2p_store_addr=metas_buckets.p2p_store_addr,
            rdma_device=metas_buckets.rdma_device,
        )
        num_parameters += sum(len(x.metas) for x in metas_buckets.memory_buffer_metas_list)
    self._local_rdma_devices[
        metas_buckets.rdma_device + "@" + metas_buckets.p2p_store_addr.split(":")[0]
        if metas_buckets.p2p_store_addr
        else metas_buckets.host_ip
    ].add(i)
```

逐点拆解：

- `if i % self._gpu_count == 0`：利用「rank 按主机优先连续分配」的假设，每主机的第一个 rank 代表这台主机贡献 `host_ip`。守卫 `not self._all_hosts` 检查的是**上一次 gather 的历史状态**——首次 gather 期间它始终为空，所以循环内每个主机代表都会 append；第二次 gather 时它已非空，`all_hosts` 局部列表保持空，[checkpoint_engine/ps.py:L516-L519](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L516-L519) 就不会覆盖旧值。`_global_device_uuids` 同理——设备 UUID 不会变，只需收集一次。
- 全局参数表只收录 `memory_buffer_metas_list` 非空的 rank：**空注册的 rank 不入表（不能当 owner），但仍会进入拓扑**（它有 `rdma_device`/`host_ip`，还能当 receiver）。注意构造 `MemoryBufferMetaList` 时只挑了三个字段——`host_ip`/`device_uuid` 在这里被裁剪掉。
- 拓扑 key 的构造是一个条件表达式：有 `p2p_store_addr` 时取 `网卡名@ip`（`split(":")[0]` 从 `"ip:port"` 里剥出 ip），否则退回 `host_ip`。`_local_rdma_devices` 是 `__init__` 里创建的 `defaultdict(set)`（[checkpoint_engine/ps.py:L204-L207](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L204-L207)），跨多次 gather 用 `.add(i)` 累积——rank 集合不变时这是幂等的。

**默认拓扑假设**。[checkpoint_engine/ps.py:L520-L525](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L520-L525)：

```python
# Sender node and Receiver node have the same GPU-rdma_device topology is considered as default.
# Rewrite the sender's topology (_remote_rdma_devices) by calling load_metas.
self._remote_rdma_devices = self._local_rdma_devices.copy()
```

浅拷贝共享值集合，但因为 `load_metas` 是整体替换而非原地修改，不会反过来污染 `_local_rdma_devices`。另一个推论：**每次 gather 都会把 remote 重置回 local**，所以 join 模式必须在每次 gather 之后立刻 `load_metas`（`examples/update.py` 的 join 正是这个顺序）。

**这些状态后来被谁消费？** 这是理解 gather_metas 存在意义的关键：

- 桶切分：[checkpoint_engine/ps.py:L805-L811](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L805-L811) 把 `_current_global_parameter_metas` + 两份拓扑一起喂给 `_gen_h2d_buckets`；其中 `_assign_receiver_ranks` 用 remote 拓扑反查 owner 的网卡、用 local 拓扑选 receiver（[checkpoint_engine/ps.py:L122-L137](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L122-L137)，算法细节在 u5-l6）。
- bucket size 探测：[checkpoint_engine/ps.py:L656-L660](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L656-L660) 扫全表找最大 `aligned_size`，决定双缓冲是否可用（u3-l5）。
- P2P 远端读：[checkpoint_engine/ps.py:L716-L719](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L716-L719) 的 `_get_addr_ptrs` 从表里取出 owner 的 `p2p_store_addr` 和每块 buffer 的 `(ptr, size)`，供 RDMA 批量读。
- ZMQ 寻址：[checkpoint_engine/ps.py:L622-L630](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630) 用 `_global_device_uuids` 生成要下发给 worker 的 socket 路径列表。
- 时序约束：[checkpoint_engine/ps.py:L759](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L759) 在 `_update_per_bucket` 开头断言全局参数表非空——这就是「update 必须在 gather_metas 之后」的代码级体现。

#### 4.2.4 代码实践

**实践目标**：不依赖 GPU 和分布式环境，用纯 Python 复现 gather_metas 的后处理循环，亲手得到全局参数表和 RDMA 拓扑，验证 4.2.3 的每个论断。

**操作步骤**：在仓库根目录创建 `gather_sim.py` 并运行 `python3 gather_sim.py`（只需要标准库）：

```python
# gather_sim.py —— 示例代码（非项目原有文件）：复现 ps.py gather_metas 的后处理循环
from collections import defaultdict

gpu_count = 2   # 每主机 2 卡；rank0/1 在 host A，rank2/3 在 host B（主机优先连续分配）

# 模拟 all_gather_object 收回来的 metas_lst：每个元素对应一个 rank 的 DataToGather
metas_lst = [
    # rank0：host A / mlx5_0 / 注册了 1 个 buffer（2 个参数）
    {"host_ip": "10.0.0.1", "device_uuid": "GPU-uu0", "rdma_device": "mlx5_0",
     "p2p_store_addr": "10.0.0.1:10001",
     "memory_buffer_metas_list": [{"metas": ["w0", "w1"], "ptr": 0x7F0000000000, "size": 1024}]},
    # rank1：host A / mlx5_1 / 注册了 1 个 buffer（1 个参数）
    {"host_ip": "10.0.0.1", "device_uuid": "GPU-uu1", "rdma_device": "mlx5_1",
     "p2p_store_addr": "10.0.0.1:10002",
     "memory_buffer_metas_list": [{"metas": ["w2"], "ptr": 0x7F0000001000, "size": 512}]},
    # rank2：host B / mlx5_0 / 空注册（这次没分到任何文件）
    {"host_ip": "10.0.0.2", "device_uuid": "GPU-uu2", "rdma_device": "mlx5_0",
     "p2p_store_addr": "10.0.0.2:10001", "memory_buffer_metas_list": []},
    # rank3：host B / mlx5_1 / 注册了 1 个 buffer（3 个参数）
    {"host_ip": "10.0.0.2", "device_uuid": "GPU-uu3", "rdma_device": "mlx5_1",
     "p2p_store_addr": "10.0.0.2:10002",
     "memory_buffer_metas_list": [{"metas": ["w3", "w4", "w5"], "ptr": 0x7F0000002000, "size": 2048}]},
]

# —— 以下逐行对应 ps.py L493-L522 ——
# 注意：守卫检查的是 self._all_hosts / self._global_device_uuids（上一次 gather 的历史状态），
# 循环内它们保持为空，因此首次 gather 时每个主机的代表 rank 都会 append。
_all_hosts, _global_device_uuids = [], []

current_global_parameter_metas = {}
local_rdma_devices = defaultdict(set)
num_parameters = 0
all_hosts, global_device_uuids = [], []

for i, metas_buckets in enumerate(metas_lst):
    assert metas_buckets is not None
    if i % gpu_count == 0 and not _all_hosts:
        all_hosts.append(metas_buckets["host_ip"])
    if not _global_device_uuids:
        global_device_uuids.append(metas_buckets["device_uuid"])
    if metas_buckets["memory_buffer_metas_list"]:
        current_global_parameter_metas[i] = {
            "memory_buffer_metas_list": metas_buckets["memory_buffer_metas_list"],
            "p2p_store_addr": metas_buckets["p2p_store_addr"],
            "rdma_device": metas_buckets["rdma_device"],
        }
        num_parameters += sum(len(x["metas"]) for x in metas_buckets["memory_buffer_metas_list"])
    local_rdma_devices[
        metas_buckets["rdma_device"] + "@" + metas_buckets["p2p_store_addr"].split(":")[0]
        if metas_buckets["p2p_store_addr"]
        else metas_buckets["host_ip"]
    ].add(i)

_all_hosts, _global_device_uuids = all_hosts, global_device_uuids
remote_rdma_devices = local_rdma_devices.copy()

print("owners:", sorted(current_global_parameter_metas))
print("num_parameters:", num_parameters)
print("all_hosts:", _all_hosts)
print("num_device_uuids:", len(_global_device_uuids))
for k in sorted(local_rdma_devices):
    print(f"  {k} -> ranks {sorted(local_rdma_devices[k])}")
print("remote == local:", remote_rdma_devices == local_rdma_devices)
```

**需要观察的现象与预期结果**（按源码逻辑推导，输出为确定性的；具体格式待本地验证）：

```text
owners: [0, 1, 3]          ← rank2 空注册，不入全局参数表
num_parameters: 6
all_hosts: ['10.0.0.1', '10.0.0.2']   ← 每主机一个代表（rank0、rank2）
num_device_uuids: 4        ← 每个 rank 都贡献 device_uuid
  mlx5_0@10.0.0.1 -> ranks [0]
  mlx5_0@10.0.0.2 -> ranks [2]        ← rank2 虽空注册，仍在拓扑里（可当 receiver）
  mlx5_1@10.0.0.1 -> ranks [1]
  mlx5_1@10.0.0.2 -> ranks [3]
remote == local: True      ← 默认拓扑假设
```

**附加观察**：把 rank2 的 `p2p_store_addr` 改成 `None` 再运行——它的拓扑 key 会从 `mlx5_0@10.0.0.2` 变成 `10.0.0.2`（回退 host_ip），`ptr` 不再有任何远端意义。

#### 4.2.5 小练习与答案

**练习 1**：如果 rank 5 没有注册任何权重，它会出现在 `_current_global_parameter_metas` 里吗？会出现在 `_local_rdma_devices` 里吗？

**答案**：不会出现在全局参数表（`memory_buffer_metas_list` 为空，不满足 L504 的 if）；会出现在拓扑里（循环无条件执行 L511-L515，它有 `rdma_device` 或 `host_ip`）。所以空注册 rank 不能当 owner，但可以当 receiver。

**练习 2**：同一个 `ParameterServer` 实例第二次调用 `gather_metas`（比如下一个训练迭代的 checkpoint），`_global_device_uuids` 和 `_remote_rdma_devices` 分别会发生什么？

**答案**：`_global_device_uuids` 不变——L502 的 `if not self._global_device_uuids` 守卫使第二次 gather 不再重建（设备 UUID 不会变）；`_remote_rdma_devices` 会被重置为 `_local_rdma_devices` 的拷贝——这意味着如果依赖 `load_metas` 改写过远端拓扑，每次 gather 之后都必须重新 load（join 流程正是 gather 后立刻 load）。

**练习 3**：为什么空注册的 rank 也「必须」调用 `gather_metas`？如果某个 rank 跳过会怎样？

**答案**：`all_gather_object` 是集合通信，需要 `world_size` 个进程全体参与，缺一个就会导致其余 rank 在通信中永久等待（hang）。这就是 L472-L475 用 try/except 把「未注册」降级为空列表、而不是提前 return 的原因。

### 4.3 get_metas：全局元数据的出口

#### 4.3.1 概念说明

`get_metas` 只有一行：返回 `_current_global_parameter_metas` 这个 dict 的**引用**。它存在的意义是给「跨进程/跨实例」的消费方一个稳定出口，主要有两个：

1. **HTTP 端点** `GET /v1/metas`：训练侧控制进程经 HTTP 读取 PS 的全局参数表（api.py）。
2. **JSON 文件导出**：`examples/update.py` 里 rank 0 用 `TypeAdapter(dict[int, MemoryBufferMetaList])` 把它 `dump_json` 成文件（`--save-metas-file`），供之后的新实例 join 使用。

#### 4.3.2 核心流程

```text
get_metas():
    return self._current_global_parameter_metas     # 引用，非拷贝

消费方 A：GET /v1/metas → FastAPI 序列化 → JSON（dict 键变字符串）
消费方 B：rank0 → _METAS_ADAPTER.dump_json(ps.get_metas()) → 写文件
```

注意 JSON 边界上的一个转换：`dict[int, ...]` 的 int 键在 JSON 里必须变成字符串 `"0"`、`"7"`；读回时 pydantic 的键 coercion 又会把 `"0"` 解析回 `0`。这个往返被 `tests/test_api.py` 的回环测试锁定。

#### 4.3.3 源码精读

[checkpoint_engine/ps.py:L292-L293](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L292-L293)：

```python
def get_metas(self) -> dict[int, MemoryBufferMetaList]:
    return self._current_global_parameter_metas
```

HTTP 出口在 [checkpoint_engine/api.py:L83-L89](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L83-L89)，异常包装成 500：

```python
@app.get("/v1/metas")
async def get_metas() -> dict[int, MemoryBufferMetaList]:
    try:
        return ps.get_metas()
    except Exception as e:
        logger.exception("get_metas failed")
        raise HTTPException(status_code=500, detail=str(e)) from e
```

文件导出在 [examples/update.py:L22](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L22) 定义适配器、[examples/update.py:L113-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L113-L117) 在 gather 之后落盘（仅 rank 0）：

```python
_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])
...
    with timer("Gather metas"):
        ps.gather_metas(checkpoint_name)
    if save_metas_file and int(os.getenv("RANK")) == 0:
        with open(save_metas_file, "wb") as f:
            f.write(_METAS_ADAPTER.dump_json(ps.get_metas()))
```

另外，触发 gather 的 HTTP 端点是 [checkpoint_engine/api.py:L79-L81](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L79-L81)（`POST /v1/checkpoints/{name}/gather-metas` 直接包住 `ps.gather_metas`）。

值得留意的一个语义细节：`get_metas` 返回的是内部状态的引用，调用方若原地修改会直接影响 PS 行为；而 `load_metas`（下一节）选择**整体替换**而非原地更新，正好规避了这类别名问题。

#### 4.3.4 代码实践

**实践目标**：在纯 CPU 环境跑通 metas 端点的单元测试，并亲眼确认 JSON 出口的键是字符串。

**操作步骤**：

1. 运行 CPU 测试（该文件无 gpu marker，文档字符串即声明 "CPU-only tests"）：

   ```bash
   python -m pytest tests/test_api.py -k "get_metas or round_trip" -q
   ```

2. 再运行一个小脚本观察 JSON 形态（需要 `fastapi`、`httpx`、`torch`；写法完全仿照 [tests/test_api.py:L21-L39](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L21-L39) 的 `_make_meta`）：

   ```python
   # get_metas_lab.py —— 示例代码（非项目原有文件）
   from unittest.mock import MagicMock
   from fastapi.testclient import TestClient
   import torch
   from checkpoint_engine.api import _init_api
   from checkpoint_engine.data_types import MemoryBufferMetaList, MemoryBufferMetas, ParameterMeta

   meta = MemoryBufferMetaList(
       p2p_store_addr="10.0.0.1:10001",
       rdma_device="mlx5_0",
       memory_buffer_metas_list=[MemoryBufferMetas(
           metas=[ParameterMeta(name="w", dtype=torch.float16,
                                shape=torch.Size([2, 3]), aligned_size=12)],
           ptr=0x12345678, size=1024)],
   )
   ps = MagicMock()
   ps.get_metas.return_value = {0: meta, 7: meta}
   client = TestClient(_init_api(ps))
   print(client.get("/v1/metas").json())
   ```

**需要观察的现象**：响应 JSON 的键是 `"0"`、`"7"`（字符串）而非整数；`dtype` 是 `"torch.float16"`，`shape` 是 `[2, 3]`。

**预期结果**：第 1 步应有 3 个用例通过（`test_get_metas_returns_json`、`test_get_metas_propagates_ps_error`、`test_round_trip_get_then_load`）；第 2 步打印出的 dict 能被 `_METAS_ADAPTER.validate_json` 原样复原（回环测试已证明）。具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`GET /v1/metas` 返回的 JSON 里键是字符串，`ps.get_metas()` 返回的 dict 键是 int，两者如何互通？

**答案**：JSON 对象的键只能是字符串，FastAPI 序列化时把 int 键转成字符串；读回时 pydantic `dict[int, MemoryBufferMetaList]` 的键 coercion 把 `"0"` 解析回 `0`。`tests/test_api.py::test_round_trip_get_then_load` 用「GET 的响应体直接 POST 回去」锁定了这个往返。

**练习 2**：为什么说「`get_metas` 返回引用」是一个需要小心的设计？

**答案**：调用方拿到的是 PS 内部 `_current_global_parameter_metas` 本体，任何原地增删改都会立刻影响后续 `update` 的行为（桶切分、P2P 读地址都读这张表）。安全的用法是只读或整体替换（`load_metas` 正是整体替换）。

**练习 3**：为什么 `examples/update.py` 只让 rank 0 写 `save_metas_file`？

**答案**：`all_gather_object` 之后每个 rank 的 `_current_global_parameter_metas` 内容完全一致，任选一个 rank 落盘即可；选 rank 0 是集群作业的惯例，避免 W 份重复写入。

### 4.4 load_metas：用外部 metas 改写远端拓扑（join 模式）

#### 4.4.1 概念说明

`gather_metas` 末尾的默认假设是「发送方（权重 owner）与接收方在同一个集群、共享同一套网卡拓扑」。当你要让一个**新拉起的推理实例**直接复用**旧实例**已经注册好的权重时（实例重启、动态扩容——u1-l1 讲的 P2P 更新场景），这个假设就破了：

- 新实例的 rank 编号、网卡拓扑与旧实例不同；
- 权重仍然锁页在**旧实例**的内存里，owner 是**旧实例的 rank**。

`load_metas` 就是为此准备的入口：把外部导入的 metas（通常由旧实例 `get_metas` 导出）**整体替换**进 `_current_global_parameter_metas`，并据此**重建** `_remote_rdma_devices`（发送方拓扑）。此时：

- `_current_global_parameter_metas` 的键是**旧实例的 rank**（P2P 读会去旧实例的地址取数）；
- `_local_rdma_devices` 仍是**新实例自己的**拓扑（gather 得来，load 不碰它）——receiver 从新实例里选。

#### 4.4.2 核心流程

`load_metas` 自身只有三步：

```text
load_metas(metas):
    1. _current_global_parameter_metas = metas            ← 整体替换
    2. _remote_rdma_devices = defaultdict(set)            ← 清空重建
    3. for i, meta in metas.items():
           assert meta.rdma_device is not None
           assert meta.p2p_store_addr is not None
           _remote_rdma_devices["网卡名@ip"].add(i)
```

它在 join 编排中的位置（[examples/update.py:L131-L159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L131-L159)）：

```text
join(ps, ...):
    1. metas ← 读文件（--load-metas-file）或 HTTP GET（--metas-url），validate_json 反序列化
    2. ps.init_process_group() → check_vllm_ready → dist.barrier
    3. ps.gather_metas(checkpoint_name)   ← 新实例从未注册过 checkpoint！
                                            靠 L472-L475 的降级拿到空 memory_pool，
                                          目的是建立自己的 _local_rdma_devices 和进程组
    4. ps.load_metas(metas)               ← 全局表换成旧 owner 的 metas，
                                          _remote_rdma_devices 按旧实例拓扑重建
    5. ps.update(checkpoint_name, req_func, ranks=range(P))   ← P2P 从旧 owner 拉权重
```

第 5 步里 `_gen_h2d_buckets` 拿到的是：`global_metas` = 旧 metas（owner 是旧 rank）、`local_topo` = 新实例网卡、`remote_topo` = 旧实例网卡；`_copy_to_buffer` 经 `_get_addr_ptrs(owner_rank)` 从旧实例的 `p2p_store_addr` + `ptr` 做 RDMA 批量读（[checkpoint_engine/ps.py:L716-L719](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L716-L719)）。注意新实例本地从未注册过任何 buffer，但 P2P 路径下 `_copy_to_buffer` 传入 `owner_rank` 后走远端读分支，不会触碰本地 `_memory_pool`。

两个断言的含义：能被 load 的 metas 必须每条都带 `rdma_device` 和 `p2p_store_addr`——即导出方必须装了 P2PStore。来自无 P2P 环境的 metas（`p2p_store_addr=None`）会在断言处失败，把问题拦在 join 开始之前而不是 RDMA 读深处。

#### 4.4.3 源码精读

[checkpoint_engine/ps.py:L295-L303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L295-L303)：

```python
def load_metas(self, metas: dict[int, MemoryBufferMetaList]):
    self._current_global_parameter_metas = metas
    self._remote_rdma_devices = defaultdict(set)
    for i, meta in self._current_global_parameter_metas.items():
        assert meta.rdma_device is not None, "meta.rdma_device should not be None"
        assert meta.p2p_store_addr is not None, "meta.p2p_store_addr should not be None"
        self._remote_rdma_devices[
            meta.rdma_device + "@" + meta.p2p_store_addr.split(":")[0]
        ].add(i)
```

对比 gather_metas 的 L511-L515：拓扑 key 的拼法完全一致（`网卡名@ip`），区别只有两点——数据来源从 `metas_lst`（本次 all_gather 的结果）换成导入的 dict，且**没有** `p2p_store_addr is None` 时回退 `host_ip` 的分支（直接断言拒绝）。`_local_rdma_devices` 在整个函数中未被触碰，这正是「local 归 gather、remote 归 load」的分工。

join 的编排证据链：读入 metas 在 [examples/update.py:L141-L149](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L141-L149)（文件与 URL 二选一，`_METAS_ADAPTER.validate_json` 反序列化），gather 与 load 的先后在 [examples/update.py:L150-L155](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L150-L155)，随后的 P2P 更新在 [examples/update.py:L156-L159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L156-L159)。HTTP 侧的对称入口是 [checkpoint_engine/api.py:L91-L93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L91-L93) 的 `POST /v1/metas`（请求体经 pydantic 校验后直接调 `ps.load_metas`，schema 不符返回 422）。

#### 4.4.4 代码实践

**实践目标**：跑通 load_metas 的 CPU 单元测试，并用模拟数据验证「load 只改 remote、不碰 local」。

**操作步骤**：

1. 运行 CPU 测试：

   ```bash
   python -m pytest tests/test_api.py -k "load_metas" -q
   ```

2. 在 4.2.4 的 `gather_sim.py` 末尾追加 load 模拟（示例代码）：

   ```python
   # —— 模拟 join：导入旧实例（1 台主机、1 张网卡、rank 10/11）导出的 metas ——
   old_metas = {
       10: {"rdma_device": "mlx5_0", "p2p_store_addr": "10.9.0.1:25000"},
       11: {"rdma_device": "mlx5_0", "p2p_store_addr": "10.9.0.1:25001"},
   }
   # 复现 ps.py L295-L303
   current_global_parameter_metas = old_metas
   remote_rdma_devices = defaultdict(set)
   for i, meta in old_metas.items():
       remote_rdma_devices[meta["rdma_device"] + "@" + meta["p2p_store_addr"].split(":")[0]].add(i)

   print("owners after load:", sorted(current_global_parameter_metas))
   for k in sorted(remote_rdma_devices):
       print(f"  {k} -> ranks {sorted(remote_rdma_devices[k])}")
   print("local topo untouched:", {k: sorted(v) for k, v in sorted(local_rdma_devices.items())})
   ```

**需要观察的现象**：load 之后 owners 变成 `[10, 11]`（旧实例的 rank）；remote 拓扑变成 `{'mlx5_0@10.9.0.1': [10, 11]}`（旧实例的网卡）；local 拓扑仍是 4.2.4 里那 4 个 `mlx5_*@10.0.0.*` 条目，纹丝不动。

**预期结果**：第 1 步 4 个用例通过（`test_load_metas_decodes_and_calls_ps`、`test_load_metas_rejects_bad_json`、`test_load_metas_rejects_schema_mismatch`、`test_load_metas_propagates_ps_error`）；第 2 步输出如上所述。具体格式待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：join 流程为什么必须「先 `gather_metas` 再 `load_metas`」，只调 `load_metas` 不行吗？

**答案**：不行。`load_metas` 只替换全局参数表和远端拓扑，而新实例的 `_local_rdma_devices`（receiver 侧网卡分组）、`_global_device_uuids`（ZMQ 寻址）以及分布式进程组都要靠自己的 `gather_metas` 建立；且 gather 末尾会把 remote 重置为 local，顺序颠倒会让 load 的成果被覆盖。

**练习 2**：`load_metas` 之后，`update(checkpoint_name, ranks=...)` 里的 `checkpoint_name` 对应的 checkpoint 在新实例上从未注册过，为什么不会报「checkpoint is not registered」？

**答案**：P2P 路径下 `_copy_to_buffer` 收到非 None 的 `owner_rank`，走 `_get_addr_ptrs` + `batch_transfer_sync_read` 的远端读分支，不查询本地 `_memory_pool`；本地池只在 broadcast 路径（`owner_rank is None`）被访问（[checkpoint_engine/ps.py:L684-L714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714)）。

**练习 3**：如果导入的 metas 中某条 `p2p_store_addr` 是 `None`，会发生什么？这个设计好在哪里？

**答案**：`load_metas` 的第二个断言立即抛 `AssertionError`。好处是把「这份 metas 根本无法支撑 P2P 拉取」的问题拦在 join 的最开始，而不是等到 update 深处的 RDMA 读抛出难以定位的传输错误。

## 5. 综合实践

**任务：只用元数据，把 join 模式的「gather → 导出 → 导入 → 拓扑改写」完整演一遍。**

这个任务把本讲四个模块串起来：4.1 的模型构造、4.2 的 gather 后处理、4.3 的 JSON 导出、4.4 的 load 重建。全程不需要 GPU，但会用到项目真实的 pydantic 模型（`ParameterMeta` 等），所以 JSON 这一环走的是和生产代码完全相同的序列化路径。

```python
# join_rehearsal.py —— 示例代码（非项目原有文件），依赖 torch + pydantic
from collections import defaultdict

import torch
from pydantic import TypeAdapter

from checkpoint_engine.data_types import MemoryBufferMetaList, MemoryBufferMetas, ParameterMeta

ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])


def gather_postprocess(metas_lst):
    """复现 ps.py L493-L522（省略 _all_hosts/_global_device_uuids，本任务用不到）。"""
    global_metas, topo = {}, defaultdict(set)
    for i, m in enumerate(metas_lst):
        if m["memory_buffer_metas_list"]:
            global_metas[i] = MemoryBufferMetaList(
                p2p_store_addr=m["p2p_store_addr"],
                rdma_device=m["rdma_device"],
                memory_buffer_metas_list=m["memory_buffer_metas_list"],
            )
        topo[(m["rdma_device"] + "@" + m["p2p_store_addr"].split(":")[0])
             if m["p2p_store_addr"] else m["host_ip"]].add(i)
    return global_metas, topo


def load_rebuild(metas):
    """复现 ps.py L295-L303。"""
    remote = defaultdict(set)
    for i, meta in metas.items():
        assert meta.rdma_device is not None and meta.p2p_store_addr is not None
        remote[meta.rdma_device + "@" + meta.p2p_store_addr.split(":")[0]].add(i)
    return remote


def metas_of(rank, dev, ip, port, names):
    return {"host_ip": ip, "device_uuid": f"GPU-old-{rank}", "rdma_device": dev,
            "p2p_store_addr": f"{ip}:{port}",
            "memory_buffer_metas_list": [MemoryBufferMetas(
                metas=[ParameterMeta(name=n, dtype=torch.bfloat16,
                                     shape=torch.Size([16, 16]), aligned_size=256 * 8)
                       for n in names],
                ptr=0x7F0000000000 + rank * 0x1000, size=len(names) * 256 * 8)]}


# ---- 阶段 A：旧实例，8 rank / 2 主机（各 4 卡 2 网卡），每个 rank 都注册了权重 ----
old_lst = [metas_of(r, "mlx5_0" if r % 2 == 0 else "mlx5_1",
                    "10.0.0.1" if r < 4 else "10.0.0.2", 10000 + r % 2,
                    [f"w{r}", f"w{r}b"])
           for r in range(8)]
old_global, old_topo = gather_postprocess(old_lst)

# ---- 阶段 B：rank0 导出 JSON（对应 examples/update.py L115-L117）----
js = ADAPTER.dump_json(old_global)

# ---- 阶段 C：新实例，4 rank / 1 主机 / 2 网卡，全部空注册（从未 register）----
new_lst = [{"host_ip": "10.1.0.1", "device_uuid": f"GPU-new-{r}",
            "rdma_device": "mlx5_0" if r % 2 == 0 else "mlx5_1",
            "p2p_store_addr": f"10.1.0.1:{20000 + r % 2}",
            "memory_buffer_metas_list": []} for r in range(4)]
new_global, local_topo = gather_postprocess(new_lst)

# ---- 阶段 D：导入旧 metas（对应 ps.load_metas）----
loaded = ADAPTER.validate_json(js)
remote_topo = load_rebuild(loaded)

print("JSON 往返一致:", loaded == old_global)
print("旧 owner ranks:", sorted(loaded))
print("新实例 gather 得到的全局表为空:", new_global == {})
print("local topo（新实例网卡）:", {k: sorted(v) for k, v in sorted(local_topo.items())})
print("remote topo（旧实例网卡）:", {k: sorted(v) for k, v in sorted(remote_topo.items())})
print("remote == 旧实例自己的 topo:", dict(remote_topo) == dict(old_topo))
```

**操作步骤**：仓库根目录运行 `python join_rehearsal.py`，然后对照 [checkpoint_engine/ps.py:L493-L522](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L493-L522) 与 [checkpoint_engine/ps.py:L295-L303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L295-L303) 逐行核对两个复现函数。

**预期结果**（按源码逻辑推导；具体格式待本地验证）：

- `JSON 往返一致: True`（pydantic 键 coercion 把 `"0"`…`"7"` 复原成 int）；
- `旧 owner ranks: [0, 1, 2, 3, 4, 5, 6, 7]`；`新实例 gather 得到的全局表为空: True`；
- `local topo: {'mlx5_0@10.1.0.1': [0, 2], 'mlx5_1@10.1.0.1': [1, 3]}`；
- `remote topo: {'mlx5_0@10.0.0.1': [0, 2], 'mlx5_1@10.0.0.1': [1, 3], 'mlx5_0@10.0.0.2': [4, 6], 'mlx5_1@10.0.0.2': [5, 7]}`，且 `remote == 旧实例自己的 topo: True`。

**思考题**（不必写代码）：此时若调用 `update(ranks=range(4))`，`_gen_h2d_buckets` 收到的三元组是（旧 metas, 新 local topo, 旧 remote topo）——owner 全是旧 rank、receiver 全是新 rank。想一想 u5-l6 的 `_assign_receiver_ranks` 会如何用 `remote_topo` 反查 owner 的网卡、用 `local_topo` 挑 receiver，让新旧双方的网卡带宽都被打满。

## 6. 本讲小结

- `gather_metas` 只交换**元数据**：`DataToGather` = 每块锁页 buffer 的 `(参数清单, ptr, size)` + 四个寻址字段（`p2p_store_addr`、`host_ip`、`device_uuid`、`rdma_device`），与 TB 级的权重本体相差 6～9 个数量级。
- `_current_global_parameter_metas` 以 **owner_rank 为键**；空注册的 rank 不入表（不能当 owner），但仍在拓扑里（可当 receiver）；`_update_per_bucket` 开头的断言保证了「先 gather 后 update」的时序。
- `_local_rdma_devices` 是「网卡名@主机IP → rank 集合」的拓扑映射，无 P2PStore 时回退按 `host_ip` 分组（NPU 上因 `ascend_direct` 恒为空网卡名，天然按主机分组）；`_all_hosts` 与 `_global_device_uuids` 只在第一次 gather 填充。
- gather 末尾 `_remote_rdma_devices = _local_rdma_devices.copy()` 是「收发同拓扑」的默认假设；`load_metas` 用外部 metas **整体替换**全局参数表并**重建**远端拓扑（不动 local）——这是 join 复用模式的元数据基础，且必须在每次 gather 之后调用。
- `get_metas` 返回内部 dict 的引用，两个出口：`GET /v1/metas` 端点与 rank 0 的 `dump_json` 文件导出；int 键在 JSON 边界变字符串，靠 pydantic 键 coercion 往返。
- 这些状态支撑了 update 阶段的一切寻址：桶切分与 receiver 分配、bucket size 探测、P2P 远端读（`p2p_store_addr` + `ptr`）、ZMQ socket 命名（`_global_device_uuids`）。

## 7. 下一步学习建议

- **u3-l4（update 广播主流程）**：看 `_current_global_parameter_metas` 如何被 `_gen_h2d_buckets` 切成桶、`_get_addr_ptrs` 如何配合本讲的 `ptr`/`p2p_store_addr` 完成 P2P 读，以及双缓冲流水线的完整编排。
- **u3-l5（bucket 切分与 bucket size 探测）**：本讲的 `aligned_size` 元数据在 `_detect_bucket_size` 里如何决定 `free/3` 的回退逻辑。
- **u5-l2（distributed 抽象层）**：本讲只碰了 `all_gather_object` 一个接口，去读完整的 `Distributed` ABC 与 `use_backend` 动态切换。
- **u5-l5 / u5-l6（P2PStore 与分配算法）**：拓扑 key 背后的 RDMA 设备发现（`NCCL_IB_HCA` 解析）与 `_assign_receiver_ranks` 的带宽最大化贪心。
- **u6-l3（metas 导出与 join）**：把本讲 4.4 的迷你流程放大到真实的多进程场景，看 `--save-metas-file`/`--metas-url`/`/v1/metas` 三条通路的完整工程编排。
