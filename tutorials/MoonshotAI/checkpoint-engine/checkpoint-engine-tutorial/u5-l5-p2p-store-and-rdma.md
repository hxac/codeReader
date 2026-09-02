# u5-l5 P2PStore 与 RDMA 设备发现

## 1. 本讲目标

本讲是「分布式后端与 P2P 传输」单元的核心一讲。学完后你应该能够:

1. 说清 `P2PStore` 如何把 mooncake `TransferEngine` 封装成三个批量接口(注册、注销、同步读),以及它的初始化为什么要随机退避重试。
2. 独立读懂 RDMA 网卡发现的完整调用链:`DeviceManager.rdma_device` → `_get_my_rdma_device` → `_get_rdma_devices` → `_parse_NCCL_IB_HCA` → `_ibv_get_device_list`。
3. 手工推演 `NCCL_IB_HCA` 的 `=`(精确匹配)、`^`(排除)、`^=`(排除 + 精确)三种前缀语义在任意输入下的输出。
4. 给定 GPU 数与网卡列表,算出每个 local_rank 分到哪块(或哪几块)网卡,并解释整除约束背后的带宽均分思想。

本讲所有实践都可以在**没有 RDMA 网卡、没有 GPU、没有安装 mooncake** 的纯 CPU 环境完成——因为网卡发现层是纯 Python 逻辑,测试里早就用 mock 把它和真实硬件解耦了。

## 2. 前置知识

### 2.1 RDMA 与 HCA:网卡直接搬内存

普通网络传输要走「对方网卡 → 对方内核协议栈 → 对方 CPU 拷贝」的完整路径。**RDMA**(Remote Direct Memory Access,远程直接内存访问)允许一台机器的网卡**直接读写另一台机器上预先注册好的内存区域**,对方的 CPU 与内核完全不参与,延迟低、CPU 占用近乎为零。RDMA 网卡在行业里常称 **HCA**(Host Channel Adapter)。

使用 RDMA 有一条铁律:**任何想被 RDMA 访问的内存,都必须先「注册」(register)**。注册会把一段虚拟内存与物理页钉住并登记到网卡,产生一个可被远端寻址的内存区域(Memory Region)。这就是本讲会反复看到的 `batch_register_memory` 存在的原因——它对应 u2-l3 讲过的锁页内存:P2P 传输的源头本来就是锁页的 host buffer,注册动作把「锁页」升级为「RDMA 可见」。

### 2.2 libibverbs 与设备命名

Linux RDMA 的用户态标准库叫 **ibverbs**(`libibverbs.so.1`),提供 `ibv_get_device_list` 等函数枚举本机 RDMA 设备。设备名形如 `mlx5_0`:`mlx5` 是 Mellanox/NVIDIA ConnectX 系列驱动的名字,`_0` 是序号。一台 8 卡 RDMA 机器常见 `mlx5_0` 到 `mlx5_7`。

### 2.3 mooncake-transfer-engine

[mooncake-transfer-engine](https://github.com/kvcache-ai/Mooncake) 是 kvcache-ai 开源的高性能传输引擎(源自 Mooncake KVCache 生态)。它把「注册内存 → 建立连接 → 发起 RDMA 读写」整套流程封装成几行 Python 就能调用的 API。checkpoint-engine 的 P2P 更新方式(u1-l1 讲过:面向实例重启与弹性扩容,不打扰存量实例)就是把数据搬运外包给它。安装形态是 `[p2p]` extra(u1-l2 讲过):

- [pyproject.toml:L20-L24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/pyproject.toml#L20-L24) — `p2p` extra 声明 `mooncake-transfer-engine>=0.3.5`,注释写明原因:`batch_register_memory` 是 0.3.5 才引入的接口。这就是 `P2PStore` 用批量注册的版本下限。

### 2.4 与前置讲义的衔接

- u3-l2(注册生命周期):注册 checkpoint 时会把每块锁页 buffer 报备到 p2p store,命名 `memory_pool_<名>_<idx>`;共享池模式下名字恒为 `__shared_memory_pool__`。
- u3-l3(gather_metas):每块 buffer 的 `(ptr, size)` 与 `p2p_store_addr`、`rdma_device` 一起被 all_gather 出去,构成远端拓扑 `_remote_rdma_devices`(键为「网卡名@主机IP」)。
- u3-l4 / u3-l5:`_copy_to_buffer` 有两种来源——owner 在本地则异步 H2D,owner 在远端则聚合为**一次批量 RDMA 读**;`BucketRange` 在 P2P 路径里当**绝对地址**用。
- u5-l1(DeviceManager):`transfer_engine_protocol` 属性(npu→`ascend_direct`,cuda/xpu→`efa` 或 `rdma`)与 `supports_device_p2p` 能力开关(CUDA/NPU 才为 True)都来自 `DeviceManager`,本讲会看到它们在 `P2PStore` 与 ps.py 中的消费位置。

## 3. 本讲源码地图

| 文件 | 本讲关注的成员 | 作用 |
| --- | --- | --- |
| [checkpoint_engine/p2p_store.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py) | `P2PStore` 全部(约 78 行) | mooncake `TransferEngine` 的薄封装:重试初始化、批量注册/注销、批量同步读 |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | `_ibv_get_device_list`、`_get_rdma_devices`、`_parse_NCCL_IB_HCA`、`_resolve_device_specs`、`_get_my_rdma_device`、`DeviceManager.rdma_device` | RDMA 网卡发现与按 rank 均分的全部逻辑 |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | `__init__` 中 P2PStore 的条件创建(L237-L248)、`_copy_to_buffer`(L684-L714)、`_get_addr_ptrs`(L716-L719)、注册/注销到 p2p store 的两个方法(L721-L749)、update 中的 `__ipc_buffer__` 注册(L827-L832) | `P2PStore` 的全部调用点 |
| [tests/test_rdma_parser.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py) | 全部测试 | 纯 CPU 验证网卡发现与均分逻辑的测试范式 |
| [tests/test_p2p_guard.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_p2p_guard.py) | `test_p2p_update_rejected_on_xpu` | XPU 上 P2P 必须在触碰传输引擎之前被拒绝 |

依赖方向一句话:`ps.py` → `P2PStore` → `DeviceManager.rdma_device` → `_get_my_rdma_device` → `_get_rdma_devices` → `_parse_NCCL_IB_HCA` / `_ibv_get_device_list`。越往右越纯(不依赖 torch 分布式、不依赖 mooncake),越往左越接近业务编排。

## 4. 核心概念与源码讲解

### 4.1 P2PStore:mooncake TransferEngine 的薄封装

#### 4.1.1 概念说明

`P2PStore` 是 checkpoint-engine 里唯一直接接触 mooncake 的类,但整个文件不到 80 行——它是典型的**防腐层**(anti-corruption layer):把第三方引擎的 C 风格 API(返回码 0 表示成功)翻译成 Python 惯用法(断言 + 异常),并把「哪段内存注册过」这本账记在自己身上。

它解决三个问题:

1. **何时在线**:引擎初始化可能因端口冲突失败,需要带随机退避的重试。
2. **哪些内存可被远端读**:把锁页 buffer 的地址与容量批量注册,并维护 `named_tensors` 账本。
3. **怎么把远端数据搬进本地显存**:一次批量同步读,平行数组描述多段不连续区间。

一个关键认知(承接 u3-l3):**`named_tensors` 的名字只是本进程的账本 key,远端从不通过名字访问数据**。跨进程流通的货币是 `gather_metas` 广播出去的 `(ptr, size)` 绝对地址。名字的用途有二:打日志、注销时反查出地址。

#### 4.1.2 核心流程

`P2PStore.__init__` 的流程:

```text
读环境变量 RANK → local_rank = rank % gpu_count
→ 选网卡:device = DeviceManager.rdma_device(local_rank)   (本讲 4.4)
→ 取本机出口 IP:get_ip()
→ 重试至多 8 次:
     engine = TransferEngine()                # 每轮新建实例,丢弃失败现场
     ret = engine.initialize(ip, "P2PHANDSHAKE", protocol, device)
     ret == 0 → break
     否则随机睡 500~2000ms 再试
→ 8 次全失败 → RuntimeError
→ port = engine.get_rpc_port()
→ addr = f"{ip}:{port}"                        # 这就是 gather_metas 里的 p2p_store_addr
```

初始化参数四个,分别回答「在哪监听 RPC、用什么握手方式、走什么传输协议、用哪块网卡」:

| 参数 | 取值 | 来源 |
| --- | --- | --- |
| 第 1 个 | 本机 IP,如 `10.0.0.1` | `get_ip()`(device_utils.py,UDP connect 探测出口 IP,失败回落 hostname 解析) |
| 第 2 个 | `"P2PHANDSHAKE"` | 固定字面量,mooncake 的点对点握手方式 |
| 第 3 个 | `ascend_direct` / `rdma` / `efa` | `DeviceManager.transfer_engine_protocol`(u5-l1 讲过) |
| 第 4 个 | 网卡名,如 `mlx5_0` 或 `mlx5_0,mlx5_4` | 本讲 4.4 的均分算法 |

三个数据接口的批量形状完全一致——**平行数组**(`buf_ptrs` / `remote_ptrs` / `lens` 一一对应),这是为了配合 u3-l5 的桶切分:一个桶的 `ranges` 可以跨多块锁页 buffer,每段 range 翻译成三个数组的一个下标,一次调用全部搬完。

#### 4.1.3 源码精读

先看重试初始化:

- [p2p_store.py:L12-L24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L12-L24) — 构造函数开头:`RANK` 环境变量必填(缺失直接 `KeyError`,属于快速失败);`local_rank = rank % gpu_count` 与 u3-l1 的公式一致;`TransferEngine` 在函数体内延迟 import,呼应 u1-l3 讲过的「可选依赖靠延迟导入隔离」——没装 mooncake 时,只有真正创建 `P2PStore` 才会炸 `ImportError`,而 ps.py 会捕获它(见下文)。
- [p2p_store.py:L21-L40](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L21-L40) — 重试主体。注释点明动机:「一台机器最多起 8 个 PS 进程(每 GPU 一个),用 8 次重试避免极端情况下的端口冲突」。两个细节值得咀嚼:
  - `random.randint(500, 2000)` 的随机退避:若两个进程同时初始化撞端口,固定时长重试会让它们**下一次也同时撞**;随机化让错开概率随重试次数迅速上升,与以太网冲突退避是同一思想。
  - Python 的 `for...else`:只有循环**没有被 `break`**(即 8 次全部失败)才进入 `else` 抛 `RuntimeError`。
- [p2p_store.py:L41-L49](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L41-L49) — 初始化成功后取 `get_rpc_port()`,`addr` property 拼成 `ip:port`。这个字符串随后在 u3-l3 的 `gather_metas` 里作为 `p2p_store_addr` 广播,成为远端发起读操作时的连接目标。

再看三个数据接口:

- [p2p_store.py:L51-L59](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L51-L59) — `register_named_tensors`:先把 `named_tensors.values()` 的 `data_ptr()`(虚拟地址)与 `nbytes` 收集成平行数组,更新账本,逐条打日志,最后断言 `batch_register_memory(...) == 0`。注意注册的单位是**整块 buffer**(u2-l3 的扁平 uint8 锁页张量),不是单个参数——只有整块注册,远端才能按 `(ptr + offset, len)` 任意区间读。
- [p2p_store.py:L61-L71](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L61-L71) — `unregister_named_tensors`:按名字反查地址,批量注销,再从账本删除并计数。返回值被 ps.py 用来打「注销了几个参数」的日志。
- [p2p_store.py:L73-L78](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L73-L78) — `batch_transfer_sync_read`:四个参数,`target_hostname` 是**对端**引擎的 `ip:port`(名字里的 target 指「要连的对端」,数据实际上是从对端**读**过来),`buf_ptrs` 是本地写入地址(GPU 显存地址),`remote_ptrs` 是对端源地址(锁页内存绝对地址),`lens` 是每段字节数。断言返回 0。

接着看 ps.py 侧的三个消费点,把 `P2PStore` 放回主链路:

- [ps.py:L237-L248](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L237-L248) — `ParameterServer.__init__` 中的条件创建:只有 `supports_device_p2p()`(CUDA/NPU,u5-l1)才尝试构造 `P2PStore`;捕获 `ImportError`(mooncake 未装)时**降级**为警告 + `self._p2p_store = None`——P2P 功能不可用但 Broadcast 照常工作;XPU 则连尝试都不尝试,直接记日志禁用(注释解释:mooncake 没有 XPU 设备内存的 Level Zero 后端)。
- [ps.py:L721-L735](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L721-L735) — `_register_parameters_to_p2p_store`(u3-l2 已讲注册时机):把内存池每块 buffer 命名为 `memory_pool_<注册名>_<idx>` 后调用 `register_named_tensors`。注册名在共享池模式下恒为 `__shared_memory_pool__`(L727-L731 的三元选择),呼应 u2-l5「地址不变则注册不变」。
- [ps.py:L684-L714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714) — `_copy_to_buffer` 的 P2P 分支:当 `owner_rank is not None`(权重在远端 rank 的锁页内存里)时,遍历桶的 `ranges`,把每段 `(idx, offset, size)` 翻译成 `remote_ptrs.append(ptrs[b.idx][0] + b.offset)`——绝对地址 = 第 `idx` 块 buffer 的基址 + 段内偏移,这正是 u3-l5 说「BucketRange 在 P2P 路径当绝对地址用」的落地处;循环外**一次**调用 `batch_transfer_sync_read`。对照本地分支(L704-L709 的 `copy_(non_blocking=True)`,即 H2D),可见 P2P 只是把「H2D 拷贝」换成了「批量 RDMA 读」,桶循环骨架不变——印证 u1-l4「两种更新方式共用同一骨架」。
- [ps.py:L716-L719](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L716-L719) — `_get_addr_ptrs`:从 `_current_global_parameter_metas[owner_rank]` 取出对端 `p2p_store_addr` 与每块 buffer 的 `(ptr, size)`,是 `gather_metas`(u3-l3)成果的直接消费者。
- [ps.py:L827-L832](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L827-L832) — update 阶段,参与 P2P 更新的每个 rank 把自己的 GPU 缓冲(有 h2d_buffer 时是 h2d_buffer,否则是双缓冲 buffer)以固定名 `__ipc_buffer__` 注册进 p2p store。RDMA 单边传输要求收发两侧的相关内存都完成注册:owner 的锁页内存已在 register_checkpoint 阶段注册,接收侧的 GPU 缓冲在这里补上。update 结束后在 [ps.py:L937-L938](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L937-L938) 注销掉这个名字。

#### 4.1.4 代码实践

**实践:跟踪 P2PStore 的降级与消费路径(源码阅读型)**

1. **实践目标**:验证「不装 mooncake 时 P2P 优雅降级、Broadcast 不受影响」这条断言链,并数清 `P2PStore` 的全部方法被 ps.py 调用的位置。
2. **操作步骤**:
   - 在仓库根目录执行(基础安装,无 `[p2p]` extra):

     ```bash
     pip install -e .
     python -c "import mooncake.engine" ; echo "exit=$?"
     ```

     预期 `exit=1`(模块不存在)。再运行:

     ```bash
     grep -n "self._p2p_store\." checkpoint_engine/ps.py
     ```

   - 逐条核对输出的调用点:`register_named_tensors`、`unregister_named_tensors`、`batch_transfer_sync_read`、以及属性 `.addr` / `.device`(L485、L251)。
3. **需要观察的现象**:`grep` 应列出 5 处左右的调用;`import mooncake.engine` 失败说明 p2p_store.py L13 的延迟导入在本机必然触发 `ImportError`。
4. **预期结果**:对照 [ps.py:L240-L242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L240-L242),`ImportError` 被捕获后 `_p2p_store = None`,后续 `gather_metas` 里 `p2p_store_addr=None`([ps.py:L485](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L485)),P2P 更新会被拒绝而 Broadcast 完整可用。若本机恰好装了 mooncake,则只完成 grep 部分即可。
5. 另可运行 `pytest tests/test_p2p_guard.py -v`(纯 CPU)观察 XPU 上 P2P 请求在 `ipc_handler.export` 之前就被 `RuntimeError` 拒绝(测试断言 `export.assert_not_called()`)。「初始化重试时随机退避的实际间隔分布」属于运行期行为,**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么每次重试都要 `TransferEngine()` 新建实例,而不是复用同一个实例再调 `initialize`?

**答案**:`initialize` 失败后引擎内部状态不可知(可能已绑定了一半资源)。代码选择丢弃失败现场、整体重建(见 [p2p_store.py:L24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L24) 在循环体内),把「清理半初始化状态」的复杂度交给对象析构,是最稳妥也最简单的做法。

**练习 2**:`register_named_tensors` 注册的是单个参数张量还是整块锁页 buffer?为什么?

**答案**:注册的是整块 buffer(调用方 ps.py L733 传入的是 `memory_buffer.buffer`,即 u2-l3 的扁平 uint8 张量)。因为远端读的粒度是桶里的任意字节区间 `(buffer 基址 + offset, len)`,只有整块注册才能支持跨参数、跨区间的灵活读取;按参数注册既浪费注册次数,也无法表达对齐 padding。

**练习 3**:`batch_transfer_sync_read` 里的 `target_hostname` 是谁的地址——数据的发送方还是接收方?

**答案**:是**对端(数据持有方/owner)**的 `ip:port`。调用链 [ps.py:L713](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L713) 传入的 `target_addr` 来自 `_get_addr_ptrs(owner_rank)`,即 owner 在 `gather_metas` 时广播的 `p2p_store_addr`。本接口是 RDMA READ 语义:本进程(接收方)主动向对端发起读。

### 4.2 _get_rdma_devices:三级优先级的网卡发现

#### 4.2.1 概念说明

`P2PStore` 初始化时必须回答「本进程用哪块网卡」,回答问题的第一层是 `_get_rdma_devices`:**列出本机可用的 RDMA 网卡清单**。它实现了一条三级优先级链,让用户可以用两种环境变量收窄网卡范围:

1. `PS_P2P_STORE_RDMA_DEVICES`:checkpoint-engine 自己的变量,逗号分隔,**原样采用,不做任何解析过滤**;
2. `NCCL_IB_HCA`:复用 NCCL 的行业标准语法,需要经过解析器(4.3 节)才能得到设备列表;
3. 都没设:使用 ibverbs 枚举出的全部设备。

为什么第二级要复用 NCCL 的变量?因为训练集群几乎必然为 NCCL 调过 `NCCL_IB_HCA`(比如排除管理网卡 ib0、只留 mlx5 系)。沿用同一个变量,checkpoint-engine 的 P2P 流量与 NCCL 的集合通信流量会走**同一批网卡**,拓扑一致、运维心智负担最小。

#### 4.2.2 核心流程

```text
_get_rdma_devices():
  PS_P2P_STORE_RDMA_DEVICES 已设? ──是──> split(",") 直接返回   # 不查 ibverbs,不解析
        │否
  hca = NCCL_IB_HCA
  result = _parse_NCCL_IB_HCA(hca or "", _ibv_get_device_list())
  result 非空? ──是──> 返回 result
        │否(解析结果为空列表)
  返回 _ibv_get_device_list()                                     # 兜底:全部设备
```

注意最后一层的 `or` 兜底语义:「配置了 `NCCL_IB_HCA` 但一个设备都没匹配上」与「完全没配」最终都落到全部设备,这是刻意为之的宽容策略(测试有专门用例,见 4.2.3)。

而清单的原始来源 `_ibv_get_device_list` 不调用任何命令行工具,而是用 `ctypes` 直接加载 `libibverbs.so.1` 调 C 函数——这与 u5-l3/u5-l4 给 NCCL/HCCL 库动态补绑定的手法同源:**能用 ctypes 就不必引入新依赖**。

#### 4.2.3 源码精读

- [device_utils.py:L50-L70](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L50-L70) — `_ibv_get_device_list`:显式声明 `argtypes`/`restype` 后调用 `ibv_get_device_list(int* num_devices)`,返回 `struct ibv_device**` 数组(`ctypes.POINTER(ctypes.c_void_p)`);逐个下标取出指针,`ibv_get_device_name` 取名字 `decode()`,最后 `ibv_free_device_list` 释放。无设备(`num <= 0` 或空指针)返回空列表。C 数组用 `dev_array[i]` 直接下标访问,是 ctypes 的惯用法。
- [device_utils.py:L73-L82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L73-L82) — `_get_rdma_devices` 本体,三级优先级按上面流程图落地。docstring 里的「if NCCL_IB_HCA has multiple values, just return」指的是第一级:自定义变量直接返回,不做过滤。
- [tests/test_rdma_parser.py:L133-L160](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L133-L160) — 参数化测试验证整条优先级链。值得注意的用例:`("NCCL_IB_HCA", "mlx6", 全部四个设备)`——`mlx6` 一个都匹配不上,解析结果为空,`or` 兜底生效,返回全部设备。测试通过 `patch.dict(os.environ, ...)` + `patch(..., return_value=mock_devices)` 把环境变量与 ibverbs 枚举同时打桩,是纯 CPU 测硬件相关逻辑的标准范式。
- [tests/test_rdma_parser.py:L41-L51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L41-L51) — 无任何环境变量时返回全部(经 `sorted` 比较,顺序无关)。
- [tests/test_rdma_parser.py:L20-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L20-L30) — 唯一接触真实硬件的测试:先检查 `/sys/class/infiniband` 是否存在,不存在就 `pytest.skip`。这就是「无 RDMA 网卡的机器上整个文件仍可全绿」的原因。

#### 4.2.4 代码实践

**实践:亲手验证三级优先级(纯 CPU)**

1. **实践目标**:用打桩方式复现优先级链的三个分支,不依赖真实 RDMA 网卡。
2. **操作步骤**:安装依赖后(`pip install -e .`),新建 `exp_priority.py`(以下为**示例代码**,非项目原码):

   ```python
   import os
   from unittest.mock import patch
   from checkpoint_engine.device_utils import _get_rdma_devices

   MOCK = ["mlx5_0", "mlx5_1", "mlx4_0", "ib0"]

   def run(label, env):
       with patch.dict(os.environ, env, clear=True), \
            patch("checkpoint_engine.device_utils._ibv_get_device_list", return_value=MOCK):
           print(f"{label:34s} -> {_get_rdma_devices()}")

   run("no env", {})
   run("PS_P2P_STORE_RDMA_DEVICES=mlx5_0", {"PS_P2P_STORE_RDMA_DEVICES": "mlx5_0"})
   run("NCCL_IB_HCA==mlx5_0", {"NCCL_IB_HCA": "=mlx5_0"})
   run("NCCL_IB_HCA=mlx9(no match)", {"NCCL_IB_HCA": "mlx9"})
   ```

   运行 `python exp_priority.py`。
3. **需要观察的现象**:四行输出分别对应「全部设备」「原样返回单个」「精确匹配单个」「无匹配回落全部」。
4. **预期结果**:`['mlx5_0', 'mlx5_1', 'mlx4_0', 'ib0']`、`['mlx5_0']`、`['mlx5_0']`、`['mlx5_0', 'mlx5_1', 'mlx4_0', 'ib0']`。注意第一级不走 ibverbs(连 `CDLL` 都不会执行),第二、三级才需要打桩。
5. 若想观察真实机器的行为:在有 RDMA 网卡的机器上运行 `python -c "from checkpoint_engine.device_utils import _ibv_get_device_list; print(_ibv_get_device_list())"`,预期输出与 `ls /sys/class/infiniband` 一致。无 RDMA 网卡的机器上该命令会因 `libibverbs.so.1` 加载失败抛 `OSError`,**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `PS_P2P_STORE_RDMA_DEVICES` 的值不做解析,而 `NCCL_IB_HCA` 要写一个完整解析器?

**答案**:自定义变量是给 checkpoint-engine 用户的专用旋钮,格式简单(逗号分隔、写什么用什么),直接 `split(",")` 最少惊讶;而 `NCCL_IB_HCA` 是 NCCL 的既有约定,带 `=`/`^`/`^=` 前缀与前缀匹配语义,不解析就会把 `^mlx5` 当成网卡名。复用别人的变量就要遵守别人的语法。

**练习 2**:`_get_rdma_devices` 里 `_parse_NCCL_IB_HCA(...) or _ibv_get_device_list()` 这个 `or` 去掉会有什么后果?

**答案**:用户配置了 `NCCL_IB_HCA` 但写错了(例如 `=mlx5_100`)时,解析结果为空列表;有 `or` 兜底则回落到全部设备(行为宽容、测试有固化),去掉后 `_get_rdma_devices` 会返回空列表,进而在 4.4 节 `_get_my_rdma_device` 里抛 `no rdma devices found`。前者是「宁可全用也不报错」,后者是「快速失败」,项目选了前者。

### 4.3 _parse_NCCL_IB_HCA:NCCL 语法解析器

#### 4.3.1 概念说明

这是本讲最「算法味」的模块:把 NCCL 的 `NCCL_IB_HCA` 字符串翻译成设备列表。源码注释直接声明了对齐目标——NCCL 官方文档与 [NCCL C++ 解析器](https://github.com/NVIDIA/nccl/blob/v2.28.3-1/src/transport/net_ib.cc#L658-L662),也就是说,这段 Python 是对 C++ 逻辑的**行为兼容移植**。

语法规则(注释里给了权威示例):

| 写法 | 语义 |
| --- | --- |
| `mlx5` | **前缀匹配**(默认):所有以 `mlx5` 开头的网卡 |
| `=mlx5_0,mlx5_1` | `=` 前缀:**精确匹配**这些名字 |
| `^mlx5` | `^` 前缀:**排除**以 `mlx5` 开头的,其余全要 |
| `^=mlx5_0,mlx5_1` | `^=` 组合:**精确排除**这两个名字 |

两条附则:`^` 与 `=` 同用时**只允许 `^=` 顺序**(`=^` 会把 `^xxx` 整体当成一个不存在的精确名字,结果为空);**端口号不被支持**——`mlx5_0:1` 里的 `:1` 会被剥掉忽略(注释写明 HACK 原因:mooncake 尚不支持指定端口)。另外结果最多 32 个(`max_hcas = 32`,同样对齐 NCCL 的上限)。

#### 4.3.2 核心流程

```text
_parse_NCCL_IB_HCA(value, available):
  value 为空/全空白 ──> 返回 available[:32]
  is_exclude  = value 以 "^" 开头?  去掉 "^"
  is_exact    = value 以 "=" 开头?  去掉 "="        # 顺序固定:先 ^ 后 =
  tokens      = value.split(","),逐段 strip,丢空段
  hits        = _resolve_device_specs(tokens, is_exact, available)
                  # 每个 token:剥端口 → 精确 in 匹配 / startswith 前缀匹配 → set 去重 → sorted
  is_exclude? ──> result = [dev for dev in available if dev not in hits]
  len(result) > 32 ──> 截断前 32
  返回 result
```

前缀剥离顺序是理解 `^=` 与 `=` 差别的钥匙:先剥 `^` 再剥 `=`,所以 `^=mlx5_0` 能正确进入「排除 + 精确」双开关;而 `=^mlx5_0` 先剥 `=`,剩下的 token 是字面量 `^mlx5_0`,精确匹配不到,结果为空(测试用例 `id="equals-caret"` 固化了这一点)。

#### 4.3.3 源码精读

- [device_utils.py:L114-L129](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L114-L129) — docstring 即规格:NCCL 文档链接、四个示例、端口不支持、`^=` 与 `=^` 的顺序约束。读开源项目时,这种「注释即合同」的函数应当先读注释再看代码。
- [device_utils.py:L130-L149](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L130-L149) — 解析主体:空值短路(L131-L132);前缀剥离(L136-L141);`is_exclude` 时用**全集减命中集**实现排除(L146-L147)——注意减法以 `available_devices` 为基底,所以排除结果一定是有序去重的;32 上限截断(L148-L149);最后打 info 日志。
- [device_utils.py:L156-L180](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L156-L180) — `_resolve_device_specs`,逐 token 解析。两处细节:
  - L161-L164:`spec.split(":", 1)` 取 `parts[0]`,端口部分直接丢弃,源码注释自嘲为 HACK;
  - L165-L171 的嵌套三元表达式展开是:

    ```python
    if device_name in available_devices:      # 名字恰好就是设备名:两种模式都返回它
        base_devices = [device_name]
    elif is_exact_match:                       # 精确模式但不在全集:空
        base_devices = []
    else:                                      # 前缀模式:startswith 收集
        base_devices = [dev for dev in available_devices if dev.startswith(device_name)]
    ```

  - L173-L175:无命中只 `logger.warning` 后 `continue`,**不抛错**——单个坏 token 不拖垮整个配置。
- [tests/test_rdma_parser.py:L54-L97](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L54-L97) — 两组参数化测试把语法矩阵钉死:空串/空白/`^`/`^=`/`=^`/`^^`/`=`/`==` 等退化输入(L54-L66),以及前缀、精确、端口剥离、空白与重复逗号容错、四种排除形态(L76-L97)。特别注意 `id="equals-caret"` 期望 `[]`、`id="caret-equals"`(`^=`)期望全集——顺序敏感被测试固化。
- [tests/test_rdma_parser.py:L106-L130](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L106-L130) — 不存在设备的告警契约:精确/前缀模式匹配不到返回 `[]` 并恰好调用一次 `logger.warning`;`^mlx5_100` 排除一个不存在的设备等于没排除,返回全集但仍发 warning。
- [tests/test_rdma_parser.py:L32-L38](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L32-L38) — `max_hcas=32` 上限:50 个设备传空串,返回前 32 个。

#### 4.3.4 代码实践

**实践:语法矩阵 REPL 实验(纯 CPU)**

1. **实践目标**:不依赖 mock,直接在 Python REPL 里对解析函数做「先预测、后验证」。
2. **操作步骤**:

   ```bash
   pip install -e .
   python - <<'EOF'
   from checkpoint_engine.device_utils import _parse_NCCL_IB_HCA
   AVAIL = ["mlx5_0", "mlx5_1", "mlx4_0", "ib0"]
   CASES = ["mlx5", "=mlx5_0,ib0", "^ib", "^=ib0", "ib0:1,mlx5_1:2", "=^mlx5_0", "mlx5,, ib0"]
   for c in CASES:
       print(f"{c!r:24s} -> {_parse_NCCL_IB_HCA(c, AVAIL)}")
   EOF
   ```

   (以上为**示例代码**。`AVAIL` 直接手写,完全绕开了 ibverbs。)
3. **需要观察的现象**:每行先在心里写下预期输出再回车对照;`:1`、`:2` 端口被剥离;`=^` 返回空;重复逗号与空白被容错。
4. **预期结果**:`['mlx5_0', 'mlx5_1']`;`['ib0', 'mlx5_0']`(结果经 sorted 排序);`['mlx4_0', 'mlx5_0', 'mlx5_1']`;`['mlx4_0', 'mlx5_0', 'mlx5_1']`;`['mlx5_1', 'ib0']` → 实际因排序输出 `['ib0', 'mlx5_1']`;`[]`;`['ib0', 'mlx5_0', 'mlx5_1']`。若某行与预期不符,回到 4.3.2 的流程图逐步定位。`ib0:1` 这类写法在真实 NCCL 中合法、在本项目中端口被忽略,与注释声明一致。

#### 4.3.5 小练习与答案

**练习 1**:`NCCL_IB_HCA="^mlx5"` 与 `NCCL_IB_HCA="^=mlx5"` 在 `["mlx5_0","mlx5_1","mlx4_0"]` 上的输出分别是什么?

**答案**:前者前缀排除,`mlx5_0`、`mlx5_1` 都以 `mlx5` 开头被排除,输出 `['mlx4_0']`;后者精确排除名字恰好等于 `mlx5` 的设备,全集里没有叫 `mlx5` 的,命中集为空,减法后输出全集 `['mlx4_0', 'mlx5_0', 'mlx5_1']`。

**练习 2**:为什么解析器把端口部分静默丢弃而不是报错?

**答案**:源码注释(device_utils.py L163-L164)写明:mooncake transfer engine 目前不支持端口指定,因此忽略。选择兼容而非报错,是为了让「本来为 NCCL 写好的 `NCCL_IB_HCA`(可能带端口)」无需修改就能直接复用——用户配置的迁移成本为零,代价是丢端口这一信息。

**练习 3**:解析结果为什么经过 `sorted`?这对后续模块有影响吗?

**答案**:`_resolve_device_specs` 用 set 收集再 sorted 输出(L180),保证结果**确定有序、去重**。有序性对 4.4 节至关重要:均分算法按下标切网卡(`devices[local_rank // ...]`、`devices[a:b]`),如果列表顺序不稳定,同一配置在不同进程里可能分到不同网卡,与「网卡-GPU 亲和(通常对应 NUMA)」的部署假设冲突。

### 4.4 _get_my_rdma_device:网卡与 rank 的均分策略

#### 4.4.1 概念说明

前两节得到的是「本机可用网卡清单」,本节回答最后一层:**清单里的哪块(哪几块)归我这个进程用**。

为什么必须均分而不是「谁先启动谁挑」?因为 RDMA 网卡的带宽是独占资源:若 8 个进程全挤 `mlx5_0`,其它网卡闲置,单网卡成为瓶颈;P2P 更新的总时长由最慢的收发方决定(u5-l6 的分配算法会进一步保证收发两侧网卡都不重复)。静态均分让每块网卡的负载在设计时就平衡,且网卡-GPU 的绑定关系稳定,方便运维按 NUMA 亲和部署(README 提到绑定 NUMA 节点以保证 H2D 速度稳定)。

整除约束的直觉:网卡数与 GPU 数要么相等、要么一方是另一方的整数倍,均分才能不余不漏。否则(比如 3 块网卡 8 个 GPU)任何静态分配都有网卡过载或闲置,代码宁可断言失败并提示用户设置环境变量,也不给出一个次优分配。

#### 4.4.2 核心流程

设 GPU 数为 \( G \),有序网卡列表为 \( D \)(\(|D| = n\)),本进程 `local_rank` 为 \( r \):

- **情形一:\( n \le G \)(多卡共享一块网卡)**。要求 \( G \bmod n = 0 \),连续 \( g = G/n \) 个 rank 共享同一块:

  \[ \text{device}(r) = D\!\left[\left\lfloor r / g \right\rfloor\right] \]

  例:`D = [mlx5_0, mlx5_1]`、`G = 8` 时 \( g = 4 \),rank 0-3 用 `mlx5_0`,rank 4-7 用 `mlx5_1`——正是 docstring 里写的例子。

- **情形二:\( n > G \)(一卡独占多块网卡)**。要求 \( n \bmod G = 0 \),每 rank 独占 \( p = n/G \) 块,返回**逗号拼接的多块名**:

  \[ \text{device}(r) = D[ rp : (r{+}1)p] \ \text{(join ",")} \]

  例:8 块网卡 4 卡时 rank 0 拿到 `"mlx5_0,mlx5_1"`。逗号字符串正好能作为 `TransferEngine.initialize` 的 device 参数(多网卡聚合带宽)。

- **空列表** → `RuntimeError("no rdma devices found")`;**整除不满足** → 断言失败,附带一段长错误信息教用户设置 `NCCL_IB_HCA` 或 `PS_P2P_STORE_RDMA_DEVICES`,然后原样 `raise`。

调用入口是 `DeviceManager.rdma_device(local_rank)`:

```text
DeviceManager.rdma_device(local_rank):
  protocol == "ascend_direct" (NPU) ──> 返回 ""        # 昇腾直连,引擎自行选路,无需指定网卡
  protocol in ["rdma", "efa"]        ──> _get_my_rdma_device(local_rank, device_count, _get_rdma_devices())
  其他                                ──> TypeError
```

#### 4.4.3 源码精读

- [device_utils.py:L85-L111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L85-L111) — `_get_my_rdma_device` 全文。L92-L96 是情形一:`assert gpu_count % len(devices) == 0` 后按下标 `local_rank // (gpu_count // len(devices))` 取单块;L97-L104 是情形二:`device_per_rank = len(devices) // gpu_count` 后切连续片段 `devices[local_rank * device_per_rank : (local_rank + 1) * device_per_rank]` 并 `",".join`;L105-L111 捕获断言异常后用 `logger.error` 打印操作指引(NCCL 文档链接、数量约束),再 `raise` 把原始断言重新抛出——「给人读的提示 + 给测试断言的异常」两全。
- [device_utils.py:L267-L273](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L267-L273) — `DeviceManager.rdma_device`:NPU 的 `ascend_direct` 协议返回空字符串(网卡选择交给昇腾传输引擎内部),rdma/efa 才走均分算法。这是 u5-l1「能力/路径按后端分岔」哲学在 P2P 路径上的体现。
- [p2p_store.py:L16-L19](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L16-L19) — 消费点:`local_rank = rank % gpu_count` 后立即 `device_manager.rdma_device(local_rank)`,选好的 `self.device` 既传给 `engine.initialize`(L25-L30),又在构造完成后随日志与 `self._rdma_device`(ps.py L251)进入 `gather_metas` 的 `rdma_device` 字段——**同一块网卡的名字同时决定了传输层的物理路径和全局拓扑图的键**(u3-l3 的「网卡名@主机IP」),两侧必须一致,这解释了为什么 `sorted` 的确定性(4.3 练习 3)不是小事。
- [tests/test_rdma_parser.py:L163-L177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L163-L177) — 均分基础用例:4 卡 4 网卡时 `(0,4)->mlx5_0`、`(3,4)->mlx5_3`;8 卡 4 网卡时 `(4,8)->mlx5_2`、`(7,8)->mlx5_3`(即每 2 个 rank 共享一块)。
- [tests/test_rdma_parser.py:L180-L203](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_rdma_parser.py#L180-L203) — 无效配置:网卡多于 GPU(5 卡 4 GPU,4 不整除 5)→ `AssertionError`;GPU 数不整除网卡数(8 卡 3 网卡)→ `AssertionError`;空网卡列表 → `RuntimeError`。

#### 4.4.4 代码实践

**实践:手工推演 + 程序验证均分映射(纯 CPU)**

1. **实践目标**:对两种情形各推演一遍映射,再用真实函数验证,内化整除约束。
2. **操作步骤**:先在纸上完成:机器 A 有 8 GPU、网卡 `[mlx5_0, mlx5_1, mlx5_2, mlx5_3]`;机器 B 有 4 GPU、网卡 `[mlx5_0, ..., mlx5_7]`。分别写出 A 的 rank 0-7 与 B 的 rank 0-3 的网卡归属。然后运行(**示例代码**):

   ```bash
   python - <<'EOF'
   from checkpoint_engine.device_utils import _get_my_rdma_device
   for r in range(8):
       print(f"A rank{r} -> {_get_my_rdma_device(r, 8, ['mlx5_0','mlx5_1','mlx5_2','mlx5_3'])}")
   for r in range(4):
       print(f"B rank{r} -> {_get_my_rdma_device(r, 4, [f'mlx5_{i}' for i in range(8)])}")
   EOF
   ```

3. **需要观察的现象**:A 每 2 个连续 rank 共享一块;B 每个 rank 拿到 2 块的逗号拼接串。
4. **预期结果**:A 依次为 `mlx5_0, mlx5_0, mlx5_1, mlx5_1, mlx5_2, mlx5_2, mlx5_3, mlx5_3`;B 依次为 `mlx5_0,mlx5_1`、`mlx5_2,mlx5_3`、`mlx5_4,mlx5_5`、`mlx5_6,mlx5_7`。再故意试一次 `_get_my_rdma_device(0, 8, ['mlx5_0','mlx5_1','mlx5_2'])`(8 卡 3 网卡),观察 `AssertionError` 与错误提示。
5. 与部署联动的部分(真实 8 卡机上 `PS_P2P_STORE_RDMA_DEVICES` 是否真的改变 P2P 吞吐)**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:为什么情形二返回逗号拼接的多块网卡,而不是只返回一块?

**答案**:当网卡多于 GPU 时(如 4 GPU 配 8 网卡),只用一块会闲置另一半带宽。返回 `"mlx5_0,mlx5_1"` 这样的字符串可以直接作为 `TransferEngine.initialize` 的 device 参数,让单个引擎同时聚合两块网卡的带宽。对照情形一,多 GPU 共享一块网卡时引擎无法聚合别人的带宽,所以只能靠分配算法保证「共享同一块的 rank 数量均衡」。

**练习 2**:NPU 上 `rdma_device` 返回空字符串,那 NPU 的 P2P 用什么当拓扑键?

**答案**:仍用 `rdma_device` 字段,只是值为空串。ps.py 构建拓扑时(如 [ps.py:L511-L513](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L511-L513))键是 `rdma_device + "@" + host_ip`,空串网卡时退化为按主机分组;`ascend_direct` 协议下路由由昇腾传输引擎内部决定,应用层不需要也不应该指定网卡。

**练习 3**:如果集群里两台机器 GPU 数不同(8 卡机与 4 卡机混布),这套均分算法还成立吗?

**答案**:算法本身是**单机视角**的(输入只有本机 `gpu_count` 与本机网卡列表),混布时每台机器各自独立分配,互不干扰;真正要求「收发双方拓扑一致」的是上层——u3-l3 讲过 gather_metas 默认假设收发双方同拓扑,不一致时要靠 `load_metas` 改写远端拓扑。所以混布不破坏本函数,但会改变全局拓扑图的结构。

## 5. 综合实践

**任务:在纯 CPU 环境复现一次「P2P 网卡选择」的完整决策,并把四个模块串成一条流水线。**

背景设定:一台 8 GPU 的机器,ibverbs 枚举出 `["mlx5_0", "mlx5_1", "mlx5_2", "mlx5_3", "ib0", "ib1"]`(其中 `ib0`/`ib1` 是管理网卡,不应参与 P2P);集群配置了 `NCCL_IB_HCA="^ib"`。

按以下步骤完成:

1. **解析层**(4.3):手工推演 `_parse_NCCL_IB_HCA("^ib", [...])` 的输出,然后用 REPL 验证。预期得到 `["mlx5_0", "mlx5_1", "mlx5_2", "mlx5_3"]`。
2. **发现层**(4.2):写一段脚本,用 `patch` 把 `_ibv_get_device_list` 打桩为上述 6 个设备、`patch.dict` 设置 `NCCL_IB_HCA="^ib"`,调用 `_get_rdma_devices()`,确认输出与第 1 步一致;再改设 `PS_P2P_STORE_RDMA_DEVICES="mlx5_0"` 验证第一级优先级会直接短路返回 `["mlx5_0"]`。
3. **分配层**(4.4):对 `local_rank` 0-7 调用 `_get_my_rdma_device(r, 8, 解析结果)`,把结果整理成「rank → 网卡」表格。预期 rank 0-1 → `mlx5_0`,rank 2-3 → `mlx5_1`,rank 4-5 → `mlx5_2`,rank 6-7 → `mlx5_3`。
4. **消费层**(4.1,阅读型):不运行 mooncake,沿着 [p2p_store.py:L16-L19](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L16-L19) 到 [p2p_store.py:L25-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L25-L30) 阅读第 3 步得到的网卡字符串如何流入 `engine.initialize`,并说明 `addr`(ip:port)与 `device`(网卡名)随后如何分别进入 `gather_metas` 的 `p2p_store_addr` 与 `rdma_device` 字段([ps.py:L485-L488](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L485-L488))。
5. **回归验证**:运行 `pytest tests/test_rdma_parser.py -v`,确认全绿;找出与你在第 1-3 步结论对应的测试用例 id(例如 `^ib` 的排除语义对应 `^mlx5` 一族用例)。

完成标志:你能不看讲义,向别人解释「`NCCL_IB_HCA=^ib` 写下之后,rank 5 的 P2P 流量从哪块网卡走、这个决定是在哪一行代码做出的」。

## 6. 本讲小结

- `P2PStore` 是 mooncake `TransferEngine` 的防腐层:初始化用 8 次随机退避(500-2000ms)重试规避端口冲突,`for...else` 在全部失败时抛错;三个数据接口(注册/注销/同步读)全部是「平行数组 + 断言返回码为 0」的批量形态。
- RDMA 单边读要求两侧内存都注册:owner 的锁页内存在 `register_checkpoint` 阶段以 `memory_pool_<名>_<idx>` 注册,接收侧的 GPU 缓冲在 update 阶段以 `__ipc_buffer__` 注册,更新结束后注销。
- `_copy_to_buffer` 的 P2P 分支把桶的 `BucketRange` 翻译成 `(本地显存地址, 远端锁页绝对地址, 长度)` 三个平行数组,一次 `batch_transfer_sync_read` 搬完——`p2p_store_addr` 与 `(ptr, size)` 都来自 `gather_metas` 收集的元数据。
- 网卡发现是三级优先级:`PS_P2P_STORE_RDMA_DEVICES` 原样采用 → `NCCL_IB_HCA` 经解析器 → 兜底全部 ibverbs 设备;解析器是 NCCL C++ 逻辑的行为兼容移植(`=` 精确、`^` 排除、`^=` 组合、端口剥离、32 上限、结果有序去重)。
- `_get_my_rdma_device` 按「网卡数与 GPU 数互相整除」做静态均分:网卡少时连续 rank 共享一块,网卡多时每 rank 独占几块的逗号拼接串;网卡名同时是传输路径与全局拓扑键,确定性排序是正确性的前提。
- 不装 mooncake 时 `P2PStore` 构造抛 `ImportError` 被 ps.py 捕获降级,Broadcast 不受影响;XPU 上 `supports_device_p2p()` 为 False,P2P 请求在导出任何 IPC 句柄之前就被拒绝。

## 7. 下一步学习建议

本讲把「每块网卡归谁」讲清楚了,下一讲 **u5-l6(P2P bucket 分配算法:带宽最大化贪心)** 紧接着回答「每个桶由谁发给谁」:`_assign_receiver_ranks` 以本讲的 `local_topo` / `remote_topo`(键就是「网卡名@主机IP」)为输入,把桶按 owner 的 RDMA 设备分组、列优先展平、每轮为每个接收端挑选不重复网卡的桶,使收发两侧带宽都被打满。建议先读 [tests/test_assign_receiver_ranks.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_assign_receiver_ranks.py) 里的小规模拓扑用例,再精读 ps.py 中的 `_assign_receiver_ranks`。若想补 RDMA 背景,可延伸阅读 mooncake-transfer-engine 的 README 与 NCCL 文档中 `NCCL_IB_HCA` 一节;若想看 P2P 更新的完整编排,回到 [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) 的 `--update-method p2p` 分支(u6-l2 会通读)。
