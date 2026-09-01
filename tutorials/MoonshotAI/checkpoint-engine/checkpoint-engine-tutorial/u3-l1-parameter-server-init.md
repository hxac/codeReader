# ParameterServer 初始化:rank、TCPStore 与设备识别

## 1. 本讲目标

本讲是 ParameterServer 主链路单元的第一讲,聚焦 `ParameterServer.__init__` 这一个函数。学完本讲,你应该能够:

1. 说清楚 `rank`、`local_rank`、`gpu_count`、`world_size` 四个量之间的换算关系,以及它们背后「每台机器恰好占满同等数量 GPU」的部署假设。
2. 解释 `_get_master_port` 为什么要取 `MASTER_PORT + 1`,以及 `TCPStore` 在整个项目里承担的「控制面会合点」角色。
3. 理解 `__init__` 中各初始化步骤的**顺序**:为什么必须先 `set_device` 再建 P2PStore、为什么要在这里就取设备 UUID、为什么 XPU 要在初始化阶段做 JIT 预热。
4. 掌握 `DeviceManager` 的探测逻辑与能力开关(backend / transfer_engine_protocol / supports_* 系列)。

本讲不涉及权重数据的搬运,只解决一个问题:**一个 ParameterServer 进程在干任何正事之前,要先把自己是谁(rank)、在哪个设备上(device)、如何与同伴会合(TCPStore)这三件事弄清楚。**

## 2. 前置知识

### 2.1 torchrun 与 RANK / WORLD_SIZE / MASTER_ADDR / MASTER_PORT

分布式训练里,通常用 `torchrun` 同时在多台机器上拉起一组进程。torchrun 会给每个进程注入四个关键环境变量:

| 环境变量 | 含义 | 例子(2 机 × 4 卡) |
|---|---|---|
| `RANK` | 全局进程编号,从 0 开始 | 第二台机器第 3 个进程是 `9` |
| `WORLD_SIZE` | 全部进程总数 | `8` |
| `MASTER_ADDR` | rank 0 所在机器的 IP | `10.0.0.1` |
| `MASTER_PORT` | rank 0 上会合端口的端口号 | `29500` |

torchrun 自己会用 `MASTER_ADDR:MASTER_PORT` 建立一个 rendezvous(会合)服务来编排这些进程。**这个端口已经被 torchrun 的会合逻辑占用了**,这一点是理解 `_get_master_port` 的关键伏笔。

### 2.2 TCPStore:分布式系统的「公告板」

`torch.distributed.TCPStore` 是一个极简的键值服务器:一个进程以 `is_master=True` 启动监听,其余进程作为客户端连上来,大家通过 `set(key, value)` / `get(key)` 共享少量元数据。它不传张量、不做计算,只负责让一群互不知道彼此地址的进程**找到对方、交换小数据、做同步(barrier)**。工程上常把它当作「控制面」:数据面走 NCCL/RDMA 大带宽传输,控制面走 store 这类轻量通道。

### 2.3 rank 与设备的对应假设

torchrun 默认按「主机优先」(host-major)的顺序分配 rank:第 0 台机器拿 rank `0..g-1`,第 1 台机器拿 rank `g..2g-1`……其中 `g` 是每台机器的进程数。于是有一个简单而重要的换算:

\[
\text{local\_rank} = \text{rank} \bmod \text{gpu\_count}, \qquad \text{host\_index} = \lfloor \text{rank} / \text{gpu\_count} \rfloor
\]

也就是说,`rank % gpu_count` 就是本进程在本机内的编号,直接可以作为本机设备索引使用。后面会看到 `__init__` 和 `gather_metas` 都依赖这个公式。

### 2.4 设备 UUID 是什么,为什么要它

rank 是「逻辑身份」,会随启动方式改变;而 ZMQ 抽象 Unix domain socket 的地址是「物理身份」寻址——PS 要给某块物理 GPU 上的 worker 进程发消息,需要一个跨进程、跨启动方式都稳定的标识。CUDA 的 `get_device_properties(i).uuid` 返回 GPU 硬件序列号相关的 UUID,是天然候选。这一机制在上一单元 u1-l4 已见过其用途(`_bind_zmq_socket` 的地址格式),本讲讲它的**来源**。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | `ParameterServer.__init__`(L179-L275)、`_get_master_port`(L166-L173)、`_get_physical_gpu_id`(L51-L65),以及 `__init__` 产出的 `_store`/`_device_uuid` 在后文的消费点 |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | `DeviceManager` 类(L199 起)、`get_ip`(L14-L26)、`npu_generate_uuid`(L29-L47) |
| [checkpoint_engine/p2p_store.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py) | `P2PStore.__init__`(L12-L45),`__init__` 中条件初始化的对象 |
| [checkpoint_engine/xpu_ipc/__init__.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py) | `prewarm()`(L131-L133),XPU 路径在 `__init__` 末段被调用 |

依赖关系:`__init__` 把 `DeviceManager` 作为唯一硬件入口,`P2PStore` 又反过来消费 `DeviceManager` 的 `rdma_device()` 与 `transfer_engine_protocol`。本讲只用到这两个文件的最外层接口,其内部实现(RDMA 拓扑、NCCL_IB_HCA 解析)留到第五单元。

## 4. 核心概念与源码讲解

### 4.1 ParameterServer.__init__:初始化总流程

#### 4.1.1 概念说明

`ParameterServer` 的构造函数要回答三个问题:**我是谁(rank)、我在哪块设备上(device)、我和同伴在哪里碰头(TCPStore)**。它不做任何权重加载——注册 checkpoint 是 `register_checkpoint` 的事。构造函数做的是「把所有后续步骤需要的基础设施一次性架好」:ZMQ 上下文、内存池字典、P2P store、设备 UUID、TCPStore。

理解 `__init__` 的关键是**顺序敏感**:NPU 的 transfer engine 要求先 `set_device`;设备 UUID 的获取也依赖当前设备;TCPStore 放在最后,意味着前面任何一步失败都不会留下一个「半吊子会合点」让其他 rank 干等 10 分钟超时。

#### 4.1.2 核心流程

`__init__` 的执行顺序可以概括为六段:

```text
1. 身份解析     rank / world_size ← 参数 or 环境变量;校验取值
2. 设备识别     DeviceManager() 探测 npu/xpu/cuda → gpu_count → local_rank = rank % gpu_count
3. 状态容器     zmq.Context、_memory_pool、共享池标记、全局 metas 占位、RDMA 拓扑占位
4. 设备就位     set_device(local_rank)
5. 可选设施     supports_device_p2p() 为真 → 尝试 P2PStore(ImportError 降级为 None)
                取 _device_uuid;XPU 上额外 prewarm JIT 扩展
6. 控制面       master_addr 校验 → TCPStore(MASTER_PORT+1, is_master=(rank==0))
```

其中第 2 步的换算是全项目的基石:

\[
\text{local\_rank} = \text{rank} \bmod \text{gpu\_count}
\]

它隐含一个部署假设:**每台机器的 GPU 数相同,且 rank 按 host-major 连续分配**。如果某台机器少插一张卡,这个公式就会把 rank 错配到错误的设备上。

#### 4.1.3 源码精读

先看签名与身份解析:

[checkpoint_engine/ps.py:L179-L208](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L179-L208)

```python
def __init__(
    self,
    *,
    rank: int | None = None,
    world_size: int | None = None,
    auto_pg: bool = True,
    gpu_count: int | None = None,
    mem_fraction: float | None = None,
    master_addr: str | None = None,
    master_port: int | None = None,
):
    ...
    self._rank = rank if rank is not None else int(os.environ["RANK"])
    self._world_size = world_size or int(os.environ["WORLD_SIZE"])
    self.device_manager = DeviceManager()
    self._gpu_count = gpu_count or self.device_manager.device_module.device_count()
    self._local_rank = self._rank % self._gpu_count
    self._auto_pg = auto_pg
    ...
    self._mem_fraction = mem_fraction or float(os.getenv("PS_MEM_FRACTION", "0.9"))
```

四个要点:

1. **签名是 keyword-only**(`*` 之后),调用方必须写 `rank=0` 而不能按位置传参——七个参数全是可选配置,强制具名可避免错位。
2. **`rank` 与 `world_size` 的判空写法不一致**,这不是风格随意,而是有修复历史:`rank if rank is not None else ...` 是提交 f40024a 特意改的——如果写成 `rank or int(os.environ["RANK"])`,显式传 `rank=0` 会被当成假值而回落到环境变量,恰好 rank 0 又是最常显式传的值(单进程测试、HTTP API 场景)。而 `world_size`/`gpu_count` 用 `or` 无妨,因为 0 本身就是非法值(后面有 `> 0` 断言)。
3. `DeviceManager()` 在这里第一次实例化,后面所有设备相关操作都经它走。
4. `mem_fraction` 可用环境变量 `PS_MEM_FRACTION` 覆盖,默认 0.9——这是后面 `_detect_bucket_size`「按剩余显存比例切桶」的输入之一。

接着是一组防御性断言:

[checkpoint_engine/ps.py:L210-L219](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L210-L219)

```python
assert self._rank is not None and self._rank >= 0, self._rank
assert self._world_size and self._world_size > 0, self._world_size
assert (
    self._gpu_count is not None
    and self._gpu_count > 0
    and self._gpu_count <= self.device_manager.device_module.device_count()
), self._gpu_count
assert (
    self._mem_fraction is not None and self._mem_fraction > 0 and self._mem_fraction <= 1
), self._mem_fraction
```

注意第三条:`gpu_count` 允许显式传一个**小于**实际设备数的值(模拟小集群),但不允许超过。断言消息直接打印值本身,方便定位是哪个量非法。

然后是状态容器与设备就位:

[checkpoint_engine/ps.py:L221-L232](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L221-L232)

```python
self._zmq_ctx = zmq.Context()
self._zmq_addr_counter = 0

self._current_shared_memory_pool_user: str = ""
self._memory_pool: dict[str, list[MemoryBuffer]] = {}
self._memory_pool[self.shared_memory_pool_name] = []
self._current_global_parameter_metas: dict[int, MemoryBufferMetaList] = {}
# NPU transfer engine initialization requires prior set_device.
device_index = self._local_rank
self.device_manager.device_module.set_device(device_index)
```

`_memory_pool` 里预先放入共享池键 `__shared_memory_pool__`(空列表表示「池尚未定型」),这正是 u2-l5 讲过的复用机制的起点。最后一句 `set_device(local_rank)` 有注释点明动机:**NPU 的 transfer engine 初始化要求先选好设备**。CUDA 上这一步同样必要——`cudaHostRegister`、`get_device_properties` 等调用都以当前设备为上下文。

接下来是可选设施段(P2P store 与设备 UUID):

[checkpoint_engine/ps.py:L233-L251](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L233-L251)

```python
if self.device_manager.supports_device_p2p():
    try:
        self._p2p_store = P2PStore(self.device_manager)
    except ImportError as e:
        logger.warning(f"[rank{self._rank}] fail to initialize p2p store due to {e}")
        self._p2p_store = None
else:
    logger.info(
        f"[rank{self._rank}] p2p store disabled: not supported on device type "
        f"'{self.device_manager.device_type}'"
    )
    self._p2p_store = None

self._device_uuid = _get_physical_gpu_id(self.device_manager, device_index)
self._rdma_device = None if self._p2p_store is None else self._p2p_store.device
```

这里体现了两条工程原则:

- **能力检测驱动初始化**:`supports_device_p2p()` 为假(XPU)时干脆不创建 P2PStore,而不是先创建再等它失败——注释解释了原因:XPU 缺 Level Zero 后端,`engine.initialize()` 的失败形态不止 ImportError,与其接住一堆奇怪异常,不如不开始。
- **降级而非崩溃**:CUDA/NPU 上如果只是没装 `mooncake-transfer-engine`(基础包没带 `[p2p]` extra),捕获 `ImportError` 后记警告、置 `None`,Broadcast 更新完全不受影响。只有真正用到 P2P 时才会暴露。

`_device_uuid` 在这里一次性取好缓存为实例属性;`_rdma_device` 则跟随 P2P store 的有无。

再往下是 XPU 专属的 JIT 预热:

[checkpoint_engine/ps.py:L253-L264](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L253-L264)

```python
# Build the JIT SYCL IPC extension now, so its multi-second compile is outside
# the first weight-update window.
if self.device_manager.device_type == "xpu":
    from checkpoint_engine import xpu_ipc

    if xpu_ipc.prewarm():
        logger.info(f"[rank{self._rank}] XPU SYCL ipc_memory extension prebuilt")
    else:
        logger.warning(...)
```

XPU 的跨进程 IPC 依赖一个现场用 `icpx` 编译的 SYCL 原生扩展,编译要花数秒。如果拖到第一次权重更新时才编译,这几秒会吃掉更新窗口的预算,甚至触发超时。所以在 `__init__` 里就调用 `prewarm()` 触发编译——**把一次性昂贵开销挪出热路径**,这是流水线系统的通用手法。

最后是控制面的建立:

[checkpoint_engine/ps.py:L266-L275](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L266-L275)

```python
master_addr = master_addr or os.getenv("MASTER_ADDR")
assert master_addr, "master_addr is required"
self._store = torch.distributed.TCPStore(
    master_addr,
    _get_master_port(master_port),
    self._world_size,
    timeout=timedelta(minutes=10),
    is_master=self._rank == 0,
)
self._store_counter = 0
```

`is_master=self._rank == 0`:全局只有 rank 0 真正监听端口,其余 rank 都是客户端。超时给到 10 分钟——因为会合发生在各进程启动阶段,慢机器加载环境可能要一会儿。这个 `_store` 在后文有两处消费,本讲先记住入口,细节在 4.2.3 拆解。

顺带一提 `_device_uuid` 的最终去向,证明它不是摆设。在 `gather_metas` 里它被打包进待广播的元数据:

[checkpoint_engine/ps.py:L485-L488](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L485-L488)

```python
p2p_store_addr=None if self._p2p_store is None else self._p2p_store.addr,
host_ip=get_ip(),
device_uuid=self._device_uuid,
rdma_device=self._rdma_device or "",
```

收集齐后存入 `self._global_device_uuids`,而 update 阶段 `_bind_zmq_socket` 用它生成抽象 socket 地址、worker 端用同样算法反算出地址来连接:

[checkpoint_engine/ps.py:L623-L628](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L623-L628)

```python
def zmq_handle(device_uuid: str) -> str:
    return f"ipc://@checkpoint-engine-{device_uuid}-{self._zmq_addr_counter}.sock"

socket_paths = [(uid, zmq_handle(uid)) for uid in self._global_device_uuids]
...
socket.bind(zmq_handle(self._device_uuid))
```

也就是说,**设备 UUID 是 PS 侧 socket 命名与 worker 侧 socket 寻址的共同钥匙**,而钥匙的铸造就在 `__init__` 这一行。

#### 4.1.4 代码实践

**实践:验证 rank → local_rank 换算,并观察 `__init__` 在纯 CPU 环境的失败点**

1. **实践目标**:用一个纯 CPU 的小脚本模拟 2 机 × 4 卡的 rank 布局,验证 `rank % gpu_count` 换算与 `gather_metas` 中 `i % gpu_count == 0` 的取主机逻辑一致;再确认 `ParameterServer()` 在无 GPU 机器上会死在哪一步。

2. **操作步骤**(以下为示例代码,保存为 `/tmp/rank_math.py`):

```python
# 示例代码:模拟 __init__ 的 rank 换算逻辑
gpu_count = 4
world_size = 8

for rank in range(world_size):
    local_rank = rank % gpu_count          # ps.py L202 的公式
    host_index = rank // gpu_count
    if rank % gpu_count == 0:              # ps.py L500 gather_metas 挑选主机代表的条件
        print(f"rank {rank} 是主机 {host_index} 的代表进程")
    print(f"rank {rank}: local_rank={local_rank}, host={host_index}")

# 再观察 CPU 环境下构造函数的失败点
from checkpoint_engine.device_utils import DeviceManager
try:
    DeviceManager()
except TypeError as e:
    print("CPU 机器上 DeviceManager 直接抛错:", e)
```

运行 `python /tmp/rank_math.py`(需已 `pip install checkpoint-engine`)。

3. **需要观察的现象**:换算部分应输出 rank 0 和 rank 4 是各自主机的代表;`DeviceManager()` 在无 NPU/XPU/CUDA 的机器上抛出 `TypeError: The current device type is not supported`。

4. **预期结果**:这证明了 `ParameterServer.__init__` 在纯 CPU 环境下走不到 rank 解析之后的第二步——设备识别是第一道硬门槛,所以本讲后续关于真实 GPU 行行的实践均标注「待本地验证(需 GPU)」。

#### 4.1.5 小练习与答案

**练习 1**:如果调用方写 `ParameterServer(rank=0, world_size=1)`,在既没设 `RANK` 环境变量也没有 GPU 的机器上,会先因为缺 `RANK` 报错吗?

**答案**:不会。`rank if rank is not None else int(os.environ["RANK"])` 中 `rank=0` 不是 `None`,走左分支,不读环境变量;这正是 f40024a 修复后 `is not None` 写法的意义。但 `world_size=1` 有效,随后 `DeviceManager()` 在无设备机器上抛 `TypeError`。若写的是旧版 `rank or int(...)`,`rank=0` 为假值就会去读不存在的 `RANK` 而抛 `KeyError`。

**练习 2**:`gpu_count` 显式传 2、实际机器有 8 张卡,`__init__` 会怎样?这有什么合法用途?

**答案**:断言只要求 `0 < gpu_count <= device_count()`,传 2 通过校验;此时 `local_rank = rank % 2`,进程只会轮流使用设备 0 和 1。合法用途之一是单机模拟多机拓扑:8 卡机器上用 `gpu_count=2` + `world_size=8` 模拟「4 台 2 卡机器」的 rank 布局来测试切分逻辑(注意这不会改变 `MASTER_ADDR` 相关行为,只是改变了 rank 到设备的映射)。

### 4.2 _get_master_port 与 TCPStore:控制面会合点

#### 4.2.1 概念说明

torchrun 已经在 `MASTER_PORT` 上跑了一个会合服务;ParameterServer 需要自己的 TCPStore 做进程间协调,不能再占同一个端口,否则就是端口冲突。最省事的办法:**默认取 `MASTER_PORT + 1`**——既然 torchrun 按约定占用了 N,那么 N+1 大概率空闲,且无需用户再配置一个新端口。这是一个典型的「约定优于配置」的 HACK,源码注释也坦率承认了这一点。

#### 4.2.2 核心流程

```text
_get_master_port(master_port):
    若调用方显式传了 master_port → 直接用
    否则:
        读环境变量 MASTER_PORT(必须存在,否则断言失败)
        返回 MASTER_PORT + 1
```

\[
\text{ps\_store\_port} = \text{MASTER\_PORT} + 1
\]

TCPStore 建立后的拓扑:rank 0 以 `is_master=True` 在 `MASTER_ADDR:MASTER_PORT+1` 监听,其余 rank 作为客户端连接,组成一张全组成员共享的键值公告板。

#### 4.2.3 源码精读

[checkpoint_engine/ps.py:L166-L173](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L166-L173)

```python
def _get_master_port(master_port: int | None = None) -> int:
    if master_port is None:
        # HACK: use MASTER_PORT + 1 as master_port, avoid conflict with torchrun's rendezvous port
        # TODO: check whether master_port is available or use a more elegant way
        master_port_str = os.getenv("MASTER_PORT")
        assert master_port_str, "MASTER_PORT is required if no master_port is provided."
        master_port = int(master_port_str) + 1
    return master_port
```

三处细节:

1. 函数级注释自陈这是一个 HACK,并留了 TODO(检查端口是否真的可用)。它有两个已知短板:其一,若 `MASTER_PORT+1` 恰好被别的服务占用,初始化会失败,只能靠用户显式传 `master_port` 绕过;其二,多个独立的 ParameterServer 集群共用同一 `MASTER_PORT` 约定时会互相踩。
2. `assert master_port_str` 的报错信息明确告诉用户「不传 master_port 就必须设 MASTER_PORT」——因为 torchrun 场景下这个变量必然存在,独立调用(比如测试)场景下则必须补。
3. 显式参数优先于约定,给了逃生通道。

`_store` 建好后在两处被消费。第一处是 `_init_process_group`(update 主流程每次会调用,auto_pg 模式下反复销毁重建进程组):

[checkpoint_engine/ps.py:L539-L547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L539-L547)

```python
self._store_counter += 1
sub_store = torch.distributed.PrefixStore(f"prefix-{self._store_counter}", self._store)
dist.init_process_group(
    backend=self.device_manager.backend,
    world_size=self._world_size,
    rank=self._rank,
    timeout=timeout,
    store=sub_store,
)
```

这里有一处**必须放到 TCPStore 语境下才能理解的设计**:`dist.init_process_group` 若不传 `store`,会自己根据 `MASTER_ADDR/MASTER_PORT` 新建会合;而本项目传入了 `PrefixStore(f"prefix-{N}", self._store)`——在同一个 TCPStore 上套一层键前缀。每重建一次进程组,计数器加一、换一个前缀,相当于在同一张公告板上开了新的「话题分区」,**旧轮次的残留键不会污染新进程组的会合**。如果直接复用裸 store,第二次 `init_process_group` 可能读到上一轮已存在的会合计数而行为异常。`_store_counter` 在 `__init__` 末尾置零(L275)正是为这个自增序列服务的。

第二处是 `store_based_barrier`:

[checkpoint_engine/ps.py:L550-L567](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L550-L567)

```python
def store_based_barrier(self, timeout: timedelta = timedelta(minutes=5)) -> None:
    torch.distributed.distributed_c10d._store_based_barrier(
        rank=self._rank,
        store=self._store,
        group_name="parameter_server_barrier",
        rendezvous_count=self._world_size,
        timeout=timeout,
    )
```

docstring 点明了它的价值:直接基于 store 做屏障,**不依赖任何进程组**。当各 rank 所属进程组不一致(比如 update 过程中组被销毁重建的窗口期)时,`dist.barrier()` 无组可用,而这个屏障依然有效——公告板始终在那。

#### 4.2.4 代码实践

**实践:在纯 CPU 上复刻 `MASTER_PORT+1` 会合与 PrefixStore 隔离**

1. **实践目标**:亲手搭一个 rank 0(master)+ rank 1(client)的 TCPStore,验证 `+1` 端口约定可用,并观察 PrefixStore 前缀如何把两个「逻辑 store」隔离开。

2. **操作步骤**(以下为示例代码,保存为 `/tmp/store_demo.py`):

```python
# 示例代码:两个终端分别运行
#   终端 A(扮演 rank 0): python /tmp/store_demo.py 0
#   终端 B(扮演 rank 1): python /tmp/store_demo.py 1
import os, sys, torch.distributed as td

rank = int(sys.argv[1])
addr, port = "127.0.0.1", 29501          # 模拟 MASTER_PORT=29500 → +1

store = td.TCPStore(addr, port, world_size=2, is_master=(rank == 0),
                    timeout=__import__("datetime").timedelta(minutes=1))

if rank == 0:
    store.set("device_uuid", "GPU-aaaa")          # rank 0 广播自己的身份
    print("rank0 wrote uuid, now reads bar:", store.get("bar"))
else:
    print("rank1 read uuid:", store.get("device_uuid"))
    store.set("bar", "hello")                     # 客户端也能写

# PrefixStore 隔离:前缀不同,同名键互不可见
sub = td.PrefixStore("prefix-1", store)
try:
    sub.get("device_uuid")                        # prefix-1/device_uuid 不存在
except Exception as e:
    print("prefix 隔离生效:", type(e).__name__)
```

先启动终端 A 再启动 B(A 会阻塞等待 B 加入,这正是「会合」的含义)。

3. **需要观察的现象**:A 先打印阻塞前的等待行为;B 启动后双方各自完成读写;最后两端都打印 `prefix 隔离生效`,异常类型为 `KeyError`(或 PyTorch 封装的等价错误)。

4. **预期结果**:端到端跑通即证明 `MASTER_PORT+1` 上的 TCPStore 会合与 `PrefixStore` 键隔离都与 `ps.py` 的用法一致。本实践纯 CPU 可运行。若端口 29501 被占用会抛绑定错误——这正好让你体会 TODO 注释里「未检查端口可用性」的短板。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `__init__` 里不等第一次 `update` 时再懒加载创建 TCPStore?

**答案**:两个原因。一是 TCPStore 是全组成员的会合点,`__init__` 结束即保证「所有 rank 都已到场」,后续 `gather_metas`/`update` 里的 `all_gather_object`、屏障才有基础设施可用;若推迟到 update,任何一 rank 延迟构造都会让先到的 rank 在会合处干等。二是构造期失败(端口冲突、缺 MASTER_ADDR)能在部署阶段立即暴露,而不是等到第一次更新窗口里才炸。

**练习 2**:如果同一台机器上要跑两套互不相干的 ParameterServer 集群(torchrun 分别用 `MASTER_PORT=29500` 和 `29501` 启动),会发生什么?如何避免?

**答案**:第一套用 29501(=29500+1)作 store 端口,第二套 torchrun 的 rendezvous 也在 29501——端口冲突,至少一方初始化失败;即使侥幸错开,两套约定仍可能继续相撞(29501+1=29502 与前一套无冲突但可能与其它服务冲突)。避免办法是给第二套传显式 `master_port`(如 40000),绕过 `+1` 约定,并保证其 torchrun 的 `MASTER_PORT` 也不与对方 store 端口重叠。

### 4.3 DeviceManager:多硬件后端识别

#### 4.3.1 概念说明

`DeviceManager` 是项目的**硬件抽象层单点**:探测当前是 NPU、XPU 还是 CUDA,然后把「当前平台的 torch 设备模块」统一成 `self.device_module`,再以属性/方法形式暴露各平台差异(通信后端名、传输协议、能力开关)。`ps.py` 从不直接写 `torch.cuda` 或 `torch_npu.npu`,一律经它转发——这就是同一份 `ps.py` 能同时跑在三类硬件上的原因。

#### 4.3.2 核心流程

```text
DeviceManager():
    _detect_device_type():     npu 可用? → "npu"
                               否则 xpu 可用? → "xpu"
                               否则 cuda 可用? → "cuda"
                               否则 raise TypeError
    _setup_device_module():    npu → torch_npu.npu;xpu → torch.xpu;cuda → torch.cuda

消费侧(本讲相关):
    device_module.device_count() → __init__ 里推 gpu_count 的默认值
    device_module.set_device()    → __init__ 里选定本进程设备
    backend                        → _init_process_group 选 hccl/xccl/nccl
    supports_device_p2p()          → __init__ 里决定是否建 P2PStore
```

探测优先级值得注意:NPU 与 XPU 的探测各自用 try/except 包住「模块不存在/不可用」的各种异常,且 **NPU 优先于 XPU 优先于 CUDA**——因为装了 torch_npu 的机器上 `torch.cuda.is_available()` 也可能误报,必须先问更特异的平台。

#### 4.3.3 源码精读

构造与探测:

[checkpoint_engine/device_utils.py:L199-L230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L199-L230)

```python
class DeviceManager:
    def __init__(self):
        self.device_type = self._detect_device_type()
        self._setup_device_module()

    def _is_torch_npu_available(self) -> bool:
        try:
            if hasattr(torch, "npu") and callable(getattr(torch.npu, "is_available", None)):
                return torch.npu.is_available()
            else:
                return False
        except ImportError:
            return False

    def _detect_device_type(self) -> str:
        if self._is_torch_npu_available():
            return "npu"
        elif self._is_torch_xpu_available():
            return "xpu"
        elif torch.cuda.is_available():
            return "cuda"
        else:
            raise TypeError("The current device type is not supported")
```

`_is_torch_npu_available` 的写法很谨慎:`hasattr + callable` 双重检查再调用,任何 `ImportError` 都按「不可用」处理——探测代码自身绝不能抛错,否则抽象层就成了新的故障源。

设备模块归一化:

[checkpoint_engine/device_utils.py:L232-L242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L232-L242)

```python
def _setup_device_module(self):
    if self.device_type == "npu":
        import torch_npu

        self.device_module = torch_npu.npu
    elif self.device_type == "xpu":
        self.device_module = torch.xpu
    elif self.device_type == "cuda":
        self.device_module = torch.cuda
    else:
        raise TypeError("The current device type is not supported")
```

注意 `import torch_npu` 是**函数内延迟导入**:没装 torch_npu 的 CUDA 机器不会因为这个 import 挂掉。归一化之后,`device_module.device_count()`、`set_device(i)`、`get_device_properties(i)` 三个调用在三种平台上有同构的语义——`__init__` 正是只依赖这三个同构接口。

后端与协议映射:

[checkpoint_engine/device_utils.py:L244-L265](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L244-L265)

```python
@property
def backend(self) -> str:
    if self.device_type == "npu":
        return "hccl"
    elif self.device_type == "xpu":
        return "xccl"
    elif self.device_type == "cuda":
        return "nccl"
    ...

@property
def transfer_engine_protocol(self) -> str:
    if self.device_type == "npu":
        return "ascend_direct"
    elif self.device_type in ("cuda", "xpu"):
        if has_efa_pci():
            return "efa"
        else:
            return "rdma"
    ...
```

`backend` 是给 `dist.init_process_group` 用的集合通信后端名(NPU 用华为 HCCL,XPU 用 oneAPI 的 XCCL);`transfer_engine_protocol` 是给 Mooncake P2PStore 用的传输协议(AWS 上检测到 EFA 网卡就用 `efa`,否则 `rdma`,`has_efa_pci` 通过读 `/sys/class/infiniband/` 下的 PCI vendor ID `0x1d0f` 判断,L183-L196)。**两个映射分属数据面的两条路径**,不混淆。

能力开关(本讲只用到 p2p 这一个,其余列出备查):

[checkpoint_engine/device_utils.py:L285-L305](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L305)

```python
def supports_inplace_pin(self) -> bool:
    """Whether in-place host-memory pinning (cudaHostRegister) is available -- CUDA only."""
    return self.device_type == "cuda"

def supports_device_ipc(self) -> bool:
    """..."""
    if self.device_type in ("cuda", "npu"):
        return True
    if self.device_type == "xpu":
        from checkpoint_engine import xpu_ipc

        return xpu_ipc.is_available()
    return False

def supports_device_p2p(self) -> bool:
    """Whether P2P (Mooncake) transfer of *device* memory works for this backend (CUDA/NPU only)."""
    return self.device_type in ("cuda", "npu")
```

三个开关对应 u2-l3/u2-l4 见过的 inplace pin、u4-l3/u4-l4 的设备 IPC、本讲的 P2P 传输。`__init__` 只消费 `supports_device_p2p()`;另外 `supports_inplace_pin()` 在 `register_checkpoint` 入口被消费(ps.py L331)。

还有一个与 `__init__` 直接相关的事实:`P2PStore.__init__` 内部**重复实现了一遍 rank 推导**,而不是从 ParameterServer 传入:

[checkpoint_engine/p2p_store.py:L15-L18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L15-L18)

```python
self.rank = int(os.environ["RANK"])  # ENV RANK is required
gpu_count = device_manager.device_module.device_count()
local_rank = self.rank % gpu_count
self.device = device_manager.rdma_device(local_rank)
```

同样的 `rank % gpu_count` 公式出现了两次。这意味着:若调用方给 ParameterServer 传了与设备数不一致的显式 `gpu_count`,P2PStore 侧仍按真实设备数计算,两边可能不一致——这是阅读时要留意的隐性耦合(仅当显式改小 `gpu_count` 时才会显现)。

#### 4.3.4 代码实践

**实践:绘制 DeviceManager 的平台 × 能力矩阵**

1. **实践目标**:通过阅读源码填写能力矩阵,加深「同一接口、不同平台不同答案」的印象;并在本机验证探测结果。

2. **操作步骤**:
   - 阅读上文四个代码片段,填写下表(空格自己补):

     | device_type | backend | transfer_engine_protocol | supports_inplace_pin | supports_device_ipc | supports_device_p2p |
     |---|---|---|---|---|---|
     | cuda | nccl | rdma 或 efa | ? | ? | ? |
     | npu | ? | ? | ? | ? | ? |
     | xpu | ? | ? | ? | 视 xpu_ipc.is_available() | ? |

   - 然后在本机运行(示例代码):

   ```python
   from checkpoint_engine.device_utils import DeviceManager
   try:
       dm = DeviceManager()
       print(dm.device_type, dm.backend, dm.transfer_engine_protocol,
             dm.supports_device_p2p(), dm.device_module.device_count())
   except TypeError as e:
       print("无受支持设备:", e)
   ```

3. **需要观察的现象**:有 GPU 的机器输出如 `cuda nccl rdma True 8`(AWS EFA 机器则协议为 `efa`);纯 CPU 机器打印「无受支持设备」。

4. **预期结果**:矩阵答案——npu 行为 `hccl / ascend_direct / False / True / True`,xpu 行为 `xccl / rdma或efa / False / 视编译 / False`,cuda 行为 `nccl / rdma或efa / True / True / True`。运行部分:有 GPU 环境的输出**待本地验证**;CPU 环境必得 TypeError,与 4.1.4 的观察互相印证。

#### 4.3.5 小练习与答案

**练习 1**:为什么不把 `device_type` 探测写成构造参数(如 `DeviceManager("cuda")`)让用户指定?

**答案**:因为下游所有分支(backend、协议、能力)都由设备类型唯一决定,而设备类型本可以可靠探测——让用户指定只会引入「指定为 cuda 但机器是 xpu」这类配置错误。探测优先级 npu > xpu > cuda 的顺序保证特异平台先被识别,避免通用探测(如 `torch.cuda.is_available`)误报。仅当探测本身有歧义时才值得外置成参数,本项目不属于这种情况。

**练习 2**:`has_efa_pci()` 为什么通过读 sysfs 的 PCI vendor ID 判断,而不是调某个 torch API?

**答案**:EFA 是 AWS 自研网络硬件,属于 RDMA 设备的一种,与协议选择(efa vs rdma)相关而与计算框架无关;torch 没有「查询是否 EFA」的接口。读 `/sys/class/infiniband/*/device/vendor` 并匹配 Amazon 的 vendor ID `0x1d0f` 是不依赖任何额外库的最低成本判别法,且用 `Path.exists()` + 异常兜底保证无该目录的机器上安静地返回 False。

### 4.4 _get_physical_gpu_id:设备物理 UUID 获取

#### 4.4.1 概念说明

`_get_physical_gpu_id` 解决「给本进程绑定的那块物理硬件起一个全局唯一、跨启动稳定的名字」。它分两条路:NPU 上 torch 不暴露硬件 UUID,于是借用 `npu-smi` 命令行反查「当前 pid 跑在哪块 NPU 的哪个 chip 上」,拼出 `NPU-{ip}-{编号}`;CUDA/XPU 上则直接读设备属性的 `uuid`,格式化为 `GPU-{uuid}`。函数名里的 "physical" 强调它与逻辑 rank 的区别。

#### 4.4.2 核心流程

```text
_get_physical_gpu_id(device_manager, device_index):
    device_type == "npu":
        npu_generate_uuid()
          ├─ 对每块 NPU(最多 8 块)执行 npu-smi info -t proc-mem -i <id>
          ├─ 在输出里找本进程 pid
          ├─ 解析 Chip Count 与该 pid 之后的 Chip ID
          └─ 返回 f"{get_ip()}-{npu_id * chip_count + chip_id}"
        → "NPU-{上述结果}"
    否则(cuda/xpu):
        props = device_module.get_device_properties(device_index)
        若 props 无 uuid 属性 → raise ValueError(提示需要更新的 torch)
        → f"GPU-{props.uuid}"
    任何 AssertionError → 包装成 ValueError("fail to get physical gpu id ...")
```

NPU 分支的编号公式把「NPU 编号 × 每块芯片数 + 芯片编号」压成一个全局递增整数(A3 服务器一块 NPU 内含两颗芯片,所以要乘 chip_count),再拼上本机 IP 保证跨机唯一。

#### 4.4.3 源码精读

[checkpoint_engine/ps.py:L51-L65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L51-L65)

```python
def _get_physical_gpu_id(device_manager: DeviceManager, device_index: int | None = None) -> str:
    try:
        if device_manager.device_type == "npu":
            return f"NPU-{npu_generate_uuid()}"
        else:
            # CUDA and XPU both expose get_device_properties(idx).uuid.
            props = device_manager.device_module.get_device_properties(device_index)
            if not hasattr(props, "uuid"):
                raise ValueError(
                    f"{device_manager.device_type} device properties do not expose a 'uuid' "
                    f"attribute; a newer PyTorch is required (xpu .uuid needs torch>=2.9)"
                )
            return f"GPU-{props.uuid!s}"
    except AssertionError as e:
        raise ValueError(f"fail to get physical gpu id {device_index}") from e
```

三个要点:

1. **device_index 直通**:`__init__` 调用时传的是 `self._local_rank`(L250),即查询的是「本进程被分配到的那块设备」而非设备 0——在 CUDA_VISIBLE_DEVICES 被改写过的容器里这点尤其重要。
2. **能力探测 + 明确报错**:老版本 torch 的 xpu 设备属性没有 `uuid`,与其让 `props.uuid` 抛 `AttributeError` 让人摸不着头脑,不如主动 `hasattr` 检查并给出「需要 torch>=2.9」的可操作提示。
3. **异常翻译**:`except AssertionError` 把底层(torch C++ 扩展常用 assert 报错)断言统一翻译成带设备号的 `ValueError`。

NPU 分支的 `npu_generate_uuid` 值得单独看,它是「没有 API 就造一个」的范例:

[checkpoint_engine/device_utils.py:L29-L47](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L29-L47)

```python
def npu_generate_uuid() -> str:
    str_pid = str(os.getpid())
    npu_num = 8
    try:
        for npu_id in range(npu_num):
            cmd = ["npu-smi", "info", "-t", "proc-mem", "-i", str(npu_id)]
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            str_result = str(result.stdout)
            if str_pid in str_result:
                # In A3 server, one NPU has two chips.
                match_chip_count = re.search(r"Chip Count[^\d]*(\d+)", str_result)
                chip_count = int(match_chip_count.group(1))
                search_after_pid = str_result[str_result.find(str_pid) + len(str_pid) :]
                match_chip_id = re.search(r"Chip ID[^\d]*(\d+)", search_after_pid)
                chip_id = int(match_chip_id.group(1))
                return f"{get_ip()}-{npu_id * chip_count + chip_id}"
        raise ValueError("The current process is not running on the npu device")
    except subprocess.CalledProcessError as e:
        raise ValueError("The current process is not running on the npu device") from e
```

实现思路:逐块 NPU 问 `npu-smi`「哪些进程在你上面」,输出文本里搜自己的 pid;找到了就再从 pid 之后的文本里解析 `Chip ID`。两个细节:正则 `Chip ID[^\d]*(\d+)` 限定在 pid 出现位置**之后**搜索,避免匹配到表格里别进程的 Chip ID;`check=True` 让 `npu-smi` 失败直接进 except 翻译成 ValueError。硬编码 `npu_num = 8` 意味着最多扫 8 块 NPU(覆盖当前昇腾整机形态)。

它的名字组成部分 `get_ip()` 也是一个精悍的小函数:

[checkpoint_engine/device_utils.py:L14-L26](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L14-L26)

```python
@lru_cache(maxsize=1)
def get_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception as e:
        logger.warning(f"fail to get ip from network interface, fallback to get ip from hostname: {e}")
        return socket.gethostbyname(socket.gethostname())
```

经典技巧:对 `8.8.8.8:80` 发一个 UDP connect **不会真的发包**(UDP 的 connect 只选路由、填本地地址),却能让内核替你选出「对外通信该用哪个网卡」的本地 IP——比解析 `hostname -I` 可靠得多。失败再回落 hostname 解析;`lru_cache(maxsize=1)` 保证进程内只探测一次。

#### 4.4.4 代码实践

**实践:体验 get_ip 的 UDP-connect 技巧,并追踪 UUID 的完整流向**

1. **实践目标**:在纯 CPU 环境验证 `get_ip()` 的行为;再用 grep 完整追踪 `_device_uuid` 从产生到消费的路径,把 4.1.3 的叙述亲手重走一遍。

2. **操作步骤**:
   - 运行(示例代码):

   ```python
   from checkpoint_engine.device_utils import get_ip
   print("本机对外 IP:", get_ip())
   import socket
   # 对照:手动验证 UDP connect 不发包也能拿本地地址
   s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
   s.connect(("8.8.8.8", 80))
   print("手动 UDP connect:", s.getsockname()[0])
   s.close()
   ```
   - 然后在仓库根目录执行 `grep -n "_device_uuid" checkpoint_engine/ps.py`,对照输出阅读 L250(赋值)、L487(装入 DataToGather)、L502-L503/L518-L519(收集为全局表)、L628(用于 bind)。
   - 有 CUDA GPU 的机器可加验:`torch.cuda.get_device_properties(0).uuid` 是否存在,以及 `_get_physical_gpu_id(DeviceManager(), 0)` 的返回格式是否为 `GPU-xxx`。

3. **需要观察的现象**:两个 IP 打印值一致(无外网路由的机器则走 fallback,可能打印内网地址或触发 warning 日志);grep 结果应与上面列出的行号吻合。

4. **预期结果**:CPU 部分(前两步)可直接验证;GPU 部分的返回格式**待本地验证(需 GPU)**;NPU 分支因依赖 `npu-smi` 与真实昇腾环境,本讲只能源码阅读,无法在本仓库的 CI/CPU 环境复现。

#### 4.4.5 小练习与答案

**练习 1**:为什么 NPU 的 UUID 要掺入 `get_ip()` 而 CUDA 的不用?

**答案**:CUDA 的 `props.uuid` 本身就是全球唯一的硬件序列号,无需额外限定;NPU 分支拼出的整数 `npu_id * chip_count + chip_id` 只在本机范围内唯一,两台机器会产出相同编号,必须加上本机 IP 才能构成集群内唯一标识。

**练习 2**:如果 `get_device_properties(device_index)` 传入的 `device_index` 是 `None`,会发生什么?

**答案**:`__init__` 传的 `device_index = self._local_rank` 恒为 int,正常路径不会是 None;但签名允许 None——此时 `get_device_properties(None)` 在 torch 内部会触发断言/参数错误,这正是函数尾部 `except AssertionError` 把它翻译成 `ValueError(f"fail to get physical gpu id {device_index}")` 所针对的场景之一:给调用者一个指明设备号的友好错误而非底层断言栈。

## 5. 综合实践

**任务:在纯 CPU 环境下,把 `__init__` 的「身份解析 + 端口约定 + 会合」三件事复刻成一个可运行的两进程小程序。**

ParameterServer 的构造函数依赖真实设备,无法在 CPU 上整体运行;但它的**协调骨架**——环境变量读身份、`MASTER_PORT+1` 建 store、rank 0 当 master、经 store 交换设备标识——完全可以脱机复刻。以下示例代码保存为 `/tmp/mini_ps_init.py`:

```python
# 示例代码:mini 版 ParameterServer.__init__ 协调骨架(纯 CPU)
#   终端 A: python /tmp/mini_ps_init.py 0 29500
#   终端 B: python /tmp/mini_ps_init.py 1 29500
import os, sys, socket
from datetime import timedelta
import torch.distributed as td

# ---- 第 1 步:身份解析(对应 ps.py L198-L202)----
rank = int(sys.argv[1])
world_size = 2
gpu_count = 2                                # 模拟「每机 2 卡」
local_rank = rank % gpu_count
master_addr, base_port = "127.0.0.1", int(sys.argv[2])

def _get_master_port():                      # 对应 ps.py L166-L173
    p = os.getenv("MASTER_PORT", str(base_port))
    return int(p) + 1

# ---- 第 2 步:设备标识(对应 _get_physical_gpu_id 的 CPU 替身)----
def fake_device_uuid():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # get_ip 的 UDP 技巧
    s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
    return f"FAKE-{ip}-{local_rank}"

# ---- 第 3 步:控制面会合(对应 ps.py L268-L274)----
store = td.TCPStore(master_addr, _get_master_port(), world_size,
                    timeout=timedelta(minutes=1), is_master=(rank == 0))
store.set(f"uuid/{rank}", fake_device_uuid())

# ---- 第 4 步:模拟 gather_metas 的全局收集(L498-L519)----
uuids = [store.get(f"uuid/{i}") for i in range(world_size)]
print(f"[rank{rank}] local_rank={local_rank} store_port={_get_master_port()}")
print(f"[rank{rank}] 全局设备表: {uuids}")

# ---- 第 5 步:验证 ZMQ 地址格式可由 UUID 重构(对应 L623-L624)----
for i, uid in enumerate(uuids):
    print(f"[rank{rank}] 目标 rank{i} 的 socket 将是: "
          f"ipc://@checkpoint-engine-{uid}-0.sock")
```

**操作步骤**:先在终端 A 以 rank 0 启动(它会阻塞等待),再在终端 B 以 rank 1 启动;观察两边打印。然后把 `base_port` 换成已被占用的端口重试一次,观察失败形态。

**需要观察的现象与预期结果**:

1. 两端打印的 `store_port` 都是 `MASTER_PORT+1`;全局设备表内容一致(说明会合与键值交换成功)。
2. 两端独立重构出的 socket 地址字符串完全相同——这就是「PS 用 `_device_uuid` 命名、worker 用同样算法寻址」的机制在不依赖 ZMQ 的情况下得到的验证。
3. 端口被占用时 rank 0 报绑定失败——对应 `_get_master_port` TODO 注释里承认的「未检查可用性」短板。
4. 思考题(留给读者):把脚本里 `gpu_count` 改成 3 再跑,`local_rank` 与 socket 地址会怎么变?这等价于 4.1.5 练习 2 里「显式传 gpu_count」的情形。

本实践纯 CPU 可运行,唯一「待本地验证」的是把 `fake_device_uuid` 换成真实 `torch.cuda.get_device_properties(local_rank).uuid` 后的 GPU 行为。

## 6. 本讲小结

- `ParameterServer.__init__` 按「身份 → 设备 → 状态容器 → set_device → 可选设施 → TCPStore」六段推进,**顺序是需求驱动的**:NPU 的 transfer engine 要求先 set_device,XPU 的 JIT 编译被刻意挪出更新热路径,TCPStore 放最后以免留下无人使用的会合点。
- 身份换算的核心公式是 `local_rank = rank % gpu_count`,它隐含「各机 GPU 数相同、rank 按 host-major 分配」的部署假设;`gather_metas` 中 `i % gpu_count == 0` 挑选每机代表进程也基于同一假设。
- `rank` 用 `is not None` 判空而 `world_size` 用 `or`,差别源于 f40024a 对 `rank=0` 假值 bug 的修复——读源码时要能看出这种「不对称即历史」的信号。
- `_get_master_port` 默认取 `MASTER_PORT + 1`,是为避开 torchrun 的 rendezvous 端口的约定式 HACK;`TCPStore` 之后被 `PrefixStore(自增前缀)` 包着反复用于进程组重建,并支撑不依赖进程组的 `store_based_barrier`。
- `DeviceManager` 是唯一硬件入口:探测优先级 npu > xpu > cuda,归一化出 `device_module`,并以 `backend`/`transfer_engine_protocol`/`supports_*` 三组开关把平台差异收敛为布尔与字符串;`__init__` 只消费其中的 `supports_device_p2p()`。
- `_get_physical_gpu_id` 在 CUDA/XPU 上读设备属性 UUID(格式 `GPU-{uuid}`),在 NPU 上用 `npu-smi` 反查 pid 拼出含本机 IP 的编号;`_device_uuid` 最终成为 PS 侧 ZMQ 抽象 socket 命名与 worker 侧寻址的共同钥匙。

## 7. 下一步学习建议

本讲只完成了「把服务器立起来」,还没有任何 checkpoint 进入系统。下一讲 **u3-l2《checkpoint 注册与注销生命周期》** 将紧随 `__init__` 之后的第一个业务方法 `register_checkpoint` 展开:内存池管理、p2p store 注册与失败回滚,其中会大量用到本讲建立的 `_memory_pool`、`_p2p_store`、`_device_uuid` 概念。建议在继续之前:

1. 重读 [checkpoint_engine/ps.py:L277-L303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L277-L303)(`_get_memory_pool`/`load_metas`),它们是 `__init__` 里那些占位字典的第一批消费者。
2. 若想先巩固设备抽象,可跳读第五单元 u5-l1《DeviceManager:多硬件后端的统一抽象》,把本讲 4.3 只列了皮毛的 `rdma_device`/`ipc_collect`/`host_empty_cache` 补全。
3. 把综合实践的脚本保留好——后续讲 ZMQ 协议(u3-l6)时,可以把「重构 socket 地址」那一步升级为真实的 ZMQ REQ/REP 通信实验。
