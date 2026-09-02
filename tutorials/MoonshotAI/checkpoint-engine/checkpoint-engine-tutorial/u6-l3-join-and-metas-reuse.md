# metas 导出与新实例 join:权重复用机制

## 1. 本讲目标

学完本讲,你应该能够:

1. 说清楚「join 复用」解决什么问题:新拉起的推理实例如何**不读 checkpoint 文件**、直接从旧实例的锁页内存里把权重复制过来。
2. 掌握 metas 的完整流转链路:`gather_metas` 产出 → `get_metas` 导出(JSON 文件或 `GET /v1/metas`)→ `load_metas` 导入(整体替换全局参数表并重建远端 RDMA 拓扑)。
3. 读懂 [examples/update.py](../../examples/update.py) 中 `join()` 函数的编排顺序,并解释为什么 `load_metas` 必须在 `gather_metas` **之后**调用。
4. 理解 join 模式下 P2P 拉取权重的数据面路径:两张拓扑表(新世界的 `local_topo` 与旧世界的 `remote_topo`)如何经 `_assign_receiver_ranks` 配对,`_copy_to_buffer` 如何按 owner rank 找到旧实例的 transfer engine 地址并发起 RDMA 单边读。
5. 会用 `tests/test_api.py` 的 CPU 测试范式(MagicMock + TestClient)验证 metas 端点的「导出字节 == 可导入字节」往返契约。

## 2. 前置知识

本讲是高级篇,默认你已读完 u3-l3(gather_metas)与 u5-l6(P2P bucket 分配算法)。下面把几个关键结论重新点亮:

- **metas 是「去哪里读」的清单,不是权重本体**。`gather_metas` 用 `all_gather_object` 只交换元数据:每块锁页 buffer 的参数清单(`ParameterMeta`)、主机指针 `ptr`、字节数 `size`,外加寻址字段 `p2p_store_addr`(mooncake transfer engine 的 `ip:port`)与 `rdma_device`(网卡名)。TB 级的权重本体一个字节都没动。
- **RDMA 单边读(read)不需要对方配合**。mooncake 的 `batch_transfer_sync_read` 是一侧发起的读:只要 owner 侧把内存注册进了 transfer engine、接收侧能连上它的地址,读就可以完成,owner 进程不需要参与任何集合通信。这是 join 模式的物理基础——**旧实例只要「活着」即可,不需要执行任何代码来配合新实例**。
- **`_remote_rdma_devices` 默认是本地拓扑的副本**。`gather_metas` 末尾默认假设收发双方拓扑相同,把 `_local_rdma_devices` 复制给 `_remote_rdma_devices`;`load_metas` 就是改写这份远端拓扑的唯一入口。
- **两张拓扑表的键空间是 `网卡名@主机IP`**。`_assign_receiver_ranks` 靠这个公共命名空间把「接收方(新实例)」与「持有方(旧实例)」配对,owner rank 与 receiver rank 是**两个互不相干的编号空间**,配对只看网卡拓扑、不看 rank 数值。

还需要两个工程事实:

- **pydantic 自定义类型的 JSON 无损往返**(u2-l1):`torch.dtype` 序列化为字符串、`torch.Size` 序列化为整数数组,校验器再把它们变回 torch 对象。metas 能落成 JSON 文件、能过 HTTP 传输,全靠这一层。
- **FastAPI 的 422 语义**(u4-l5):请求体先过 pydantic 校验,失败则路由函数根本不会被调用,`ParameterServer.load_metas` 不受污染。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [examples/update.py](../../examples/update.py) | `join()` 编排、`--save-metas-file` 导出、`--load-metas-file`/`--metas-url` 互斥参数组、`_METAS_ADAPTER` |
| [checkpoint_engine/ps.py](../../checkpoint_engine/ps.py) | `get_metas`、`load_metas`、`gather_metas` 中的空注册与拓扑构建、`_gen_h2d_buckets` 的双拓扑入参、`_get_addr_ptrs`/`_copy_to_buffer` 的 RDMA 读 |
| [checkpoint_engine/api.py](../../checkpoint_engine/api.py) | `GET /v1/metas` 与 `POST /v1/metas` 端点、`wrap_exception` |
| [tests/test_api.py](../../tests/test_api.py) | metas 端点的 CPU 测试,尤其是 GET→POST 往返测试 |
| [checkpoint_engine/data_types.py](../../checkpoint_engine/data_types.py) | `MemoryBufferMetaList`/`DataToGather` 模型与自定义 torch 类型的序列化 |
| [checkpoint_engine/__main__.py](../../checkpoint_engine/__main__.py) | UDS-only 的 API 服务部署方式(影响 `--metas-url` 的可用性) |

## 4. 核心概念与源码讲解

### 4.1 join 复用:问题、思路与两条元数据通道

#### 4.1.1 概念说明

推理服务的弹性扩容是 RL 训练集群的常态:流量高峰要临时加实例、实例崩溃要重启、灰度要新起一组。新实例的冷启动如果走传统路径,必须从磁盘把整份 checkpoint(几百 GB 到 TB 级)读进内存、锁页、再灌进显存——而**集群里已经有一份一模一样的权重躺在旧实例的锁页内存里**,并且这些内存已经注册进了 mooncake transfer engine(见 u3-l2:`_register_parameters_to_p2p_store`)。

join 模式的思路一句话:**新实例不碰 checkpoint 文件,改从旧实例的锁页内存里 RDMA 拉取**。要做这件事,新实例只缺一样东西——旧实例的 metas(指针、大小、p2p_store_addr、网卡名)。于是整个机制退化为一个「元数据交接」问题:

```
旧实例(权重持有方)                    新实例(权重接收方)
─────────────────────                ─────────────────────
register_checkpoint                  (从不注册任何 checkpoint)
  └─ 锁页 + 注册 p2p store
gather_metas
  └─ _current_global_parameter_metas
get_metas ──── 通道①: JSON 文件 ────▶ 读文件/读 URL
│            (save/load-metas-file)     ↓
└─ 通道②: GET /v1/metas ─────────▶ load_metas(改写全局表+远端拓扑)
   (旧实例只需保持进程存活)               ↓
                                    update(ranks=[0..P-1])
                                      └─ RDMA 单边读旧实例锁页内存
                                         + 新世界内部 broadcast 装载
```

两条通道的本质区别只在「元数据怎么到手」:

- **文件通道**:旧实例把 metas 写到磁盘,新实例从磁盘读。优点是不依赖网络服务,缺点是文件要能被两侧共同访问(共享盘或手动拷贝)。
- **HTTP 通道**:旧实例(或任何持有 metas 的一方)通过 `GET /v1/metas` 暴露,新实例用 `--metas-url` 拉取。优点是实时(永远反映当前 `_current_global_parameter_metas`),缺点是要求网络可达。

#### 4.1.2 核心流程

join 模式的两阶段部署(对应 README 的用法):

**阶段一:旧实例带导出启动**

1. 旧实例正常执行 `update_weights`(注册 → gather → broadcast 更新)。
2. `gather_metas` 完成后,rank 0 把 `ps.get_metas()` 的结果 JSON 化写入 `--save-metas-file` 指定的文件。
3. `--sleep-time 300` 让旧实例**进程保持存活**——锁页内存与 p2p store 注册必须活着,地址才有效;进程一退,`ptr` 就成了悬空指针。

**阶段二:新实例带导入启动**

1. 解析命令行:`--load-metas-file` 与 `--metas-url` 是**互斥参数组**,二者选一,都没有则报错。
2. 依序执行 `join()`:读 metas → 建进程组 → 等 vLLM 就绪 → barrier → **自己的** `gather_metas` → `load_metas`(覆盖) → `update(ranks=range(P))` 走 P2P。
3. 数据面:P2P 更新按桶从旧实例 RDMA 读入新实例的 GPU 缓冲,再在新世界内部 broadcast 给各 worker 装载。

#### 4.1.3 源码精读

README 对这两阶段的完整描述:

[README.md:138-153](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L138-L153)——官方给出的 join 用法:旧实例 `--save-metas-file global_metas.pkl --sleep-time 300` 保持存活,新实例 `--load-metas-file global_metas.pkl` 直接加入。注意一个细节:**文件名后缀是 `.pkl`,内容却是 JSON 字节**(`dump_json` 产出),后缀只是个名字,与 pickle 无关。

命令行的互斥参数组定义:

[examples/update.py:165-178](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L165-L178)——`argparse.add_mutually_exclusive_group` 把 `--load-metas-file`(本地 JSON 文件路径)与 `--metas-url`(返回 metas JSON 的 HTTP 地址)声明为二选一,两者的 help 都标注「triggers join mode」:只要出现其中任何一个,主流程就分岔进 `join()`。

主分岔点:

[examples/update.py:193-203](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L193-L203)——`__main__` 里只要 `args.load_metas_file or args.metas_url` 为真就走 `join()`,否则走上一讲精读过的 `update_weights()`。注意 join 分支**完全不读 `--checkpoint-path`**,新实例与磁盘上的 checkpoint 再无关系。

导出侧只有一个「适配器 + 三行写出」:

[examples/update.py:22](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L22)——`_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])` 是 metas 的线格式(wire format)定义:键是 owner rank,值是 `MemoryBufferMetaList`。同一个适配器同时承担导出(`dump_json`)与导入(`validate_json`),保证字节级对称。

[examples/update.py:115-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L115-L117)——`update_weights` 中 `gather_metas` 之后,仅 rank 0 把 `_METAS_ADAPTER.dump_json(ps.get_metas())` 写入文件。只让 rank 0 写是天然的去重:metas 是全组相同的(gather 的产物),八个 rank 各写一份只会互相踩踏。

#### 4.1.4 代码实践

**实践目标**:确认两条通道的入口与「join 不读 checkpoint」这一事实。

1. 打开 [README.md:138-153](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L138-L153),对照两条 torchrun 命令,标注:哪条命令的进程持有权重?哪条命令的进程最终把权重灌进新 vLLM?
2. 在仓库根目录运行:

   ```bash
   python examples/update.py --help
   ```

3. 观察输出中 `--load-metas-file` 与 `--metas-url` 的帮助文本(都注明 triggers join mode),再尝试同时传两个参数:

   ```bash
   python examples/update.py --load-metas-file a.json --metas-url http://x
   ```

**需要观察的现象**:`--help` 正常列出互斥参数;同时传两个参数时 argparse 报 `not allowed with argument` 类错误,程序在进入任何分布式逻辑之前就被拦下。

**预期结果**:argparse 的互斥组在命令行层完成校验,`join()` 内部的 `ValueError` 只是第二道保险。`--help` 不需要 GPU 与 `RANK` 环境变量(argparse 在读环境变量之前执行);再往后的真实运行需要 GPU 集群,**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:旧实例为什么要加 `--sleep-time 300`?如果不加会发生什么?

**答案**:`--sleep-time` 让旧实例在 `update_weights` 结束后继续睡眠存活([examples/update.py:225](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L225) 的 `time.sleep(args.sleep_time)`)。新实例 `load_metas` 拿到的 `ptr` 指向旧实例锁页内存、`p2p_store_addr` 指向旧实例的 transfer engine,两者都随进程退出而失效;没有 sleep,旧实例跑完即退,新实例的 RDMA 读会读到已释放的内存或连不上地址。

**练习 2**:join 模式下,新实例是否需要能访问磁盘上的 checkpoint 文件?是否需要与旧实例在同一个 torchrun 作业里?

**答案**:都不需要。新实例的 `join()` 分支不读 `--checkpoint-path`;RDMA 单边读只要求网络可达旧实例的 transfer engine 地址,旧实例不参与新世界的任何集合通信(它们是两个独立进程组,甚至可以在不同主机、不同时间启动)。

**练习 3**:为什么 metas 可以只由 rank 0 导出一次,而不用每个 rank 各导一份?

**答案**:`gather_metas` 的产物 `_current_global_parameter_metas` 是 `all_gather_object` 汇总出的**全组一致**视图(每个 rank 持有相同内容),所以一份文件就是全局快照;让八个 rank 各写同一文件反而会竞态覆盖。

### 4.2 ps.get_metas 与 ps.load_metas:元数据的出口与入口

#### 4.2.1 概念说明

`ParameterServer` 上这两个方法构成 metas 的「一出一进」:

- `get_metas` 是**透传出口**:直接返回内部字典 `_current_global_parameter_metas` 的引用,不做拷贝、不做加工。它的价值不在逻辑而在**命名**——把「内部状态」提升为「公共 API」,让 HTTP 端点与文件导出有统一的取数口径。
- `load_metas` 是**定向改写入口**:它只动两样东西——(1)整体替换 `_current_global_parameter_metas`(全局参数表,「哪些参数、在谁的哪块 buffer 的哪个指针」);(2)从新表**重建** `_remote_rdma_devices`(远端拓扑,「每个 owner rank 挂在哪块网卡上」)。它**不动** `_local_rdma_devices`(本地拓扑)。

为什么这个「只改远端、不改本地」的切分是对的?回想 u5-l6:`_assign_receiver_ranks` 的输入是两张表——`local_topo` 提供**接收方**候选(本世界哪些 rank 可当 receiver),`remote_topo` 提供**持有方**分组(owner 在哪块网卡后面)。join 场景里:

- 接收方是新实例自己,所以 `local_topo` 必须来自**新世界自己的 `gather_metas`**;
- 持有方是旧实例,所以 `remote_topo` 必须来自**旧世界的 metas**;
- `gather_metas` 默认把两者设为相同(默认收发同拓扑),`load_metas` 负责把后一半换成旧世界的真相。

#### 4.2.2 核心流程

`load_metas(metas)` 的三步:

1. **整体替换**:`_current_global_parameter_metas = metas`。不做合并——join 语义就是「我的全局表从此指向旧实例的内存」,新世界自己的(空)表被丢弃是预期行为。
2. **清空重建**:`_remote_rdma_devices = defaultdict(set)`,防止上一轮 gather 留下的旧键混入。
3. **逐 rank 重建拓扑**:对表中每个 `owner_rank → MemoryBufferMetaList`,构造拓扑键 `meta.rdma_device + "@" + meta.p2p_store_addr.split(":")[0]`(网卡名@从 `ip:port` 里切出的主机 IP),把 owner rank 加进该键的集合。

时序约束(必须记住):**`load_metas` 要在 `gather_metas` 之后调用**。原因有二:

- `gather_metas` 开头会 `_current_global_parameter_metas = {}` 重置全局表(见 [ps.py:493](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L493)),先 load 后 gather 会被清掉;
- `_local_rdma_devices` 只能由本世界的 gather 构建,load_metas 不提供它。

#### 4.2.3 源码精读

出口只有两行:

[checkpoint_engine/ps.py:292-293](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L292-L293)——`get_metas` 直接返回 `_current_global_parameter_metas` 的引用。注意「返回引用」的两个后果:(1)进程内调用方拿到的是活对象,后续 gather 会改变它;(2)HTTP 端点在序列化响应时才做快照,所以 `GET /v1/metas` 返回的是**请求时刻**的 JSON 快照。

入口是替换加重建:

[checkpoint_engine/ps.py:295-303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L295-L303)——`load_metas` 先整体替换全局参数表并清空 `_remote_rdma_devices`,再对每个 owner 断言 `rdma_device`/`p2p_store_addr` 非空(这两项是 RDMA 寻址的命根子,缺失即无法读),最后以 `网卡名@主机IP` 为键把 owner rank 逐个挂回拓扑。与 gather 侧对照:

[checkpoint_engine/ps.py:511-515](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L511-L515)——`gather_metas` 构建 `_local_rdma_devices` 时用的是**同一个键构造式**(`rdma_device@p2p_store_addr 的主机部分`,仅在无 p2p 地址时退化为 host_ip)。两侧键格式逐字符一致,是第 4.3 节配对算法能跨两个世界工作的前提。

[checkpoint_engine/ps.py:520-522](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L520-L522)——gather 末尾的默认假设:「收发双方拓扑相同」,直接 `_remote_rdma_devices = _local_rdma_devices.copy()`。注释明说了 `load_metas` 是改写发送方拓扑的官方途径——这两行就是 join 机制在 ps.py 里的锚点。

metas 值类型的三层结构(u2-l1 已建模型,这里只回顾字段含义):

[checkpoint_engine/data_types.py:103-111](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L103-L111)——`MemoryBufferMetaList` 持有三个字段:`p2p_store_addr`(该 rank 的 transfer engine 地址)、`memory_buffer_metas_list`(每块锁页 buffer 的参数清单+`ptr`+`size`)、`rdma_device`(网卡名)。`DataToGather` 只是 gather 时的超集(多了 `host_ip`、`device_uuid`,聚合后即被裁掉)。**load_metas 消费的正是这份不含 host_ip/device_uuid 的裁剪版**,所以它重建拓扑时只能从 `p2p_store_addr` 里切 IP——这与 gather 侧的键构造完全对齐。

#### 4.2.4 代码实践

**实践目标**:亲手推演 `load_metas` 重建的远端拓扑长什么样(纯 CPU,不需要分布式环境)。

1. 用 `tests/test_api.py` 的 `_make_meta` 作模板,在 Python 里构造两个假 rank 的 metas(示例代码,可直接 `python -c` 或写成脚本):

   ```python
   # 示例代码:手工推演 load_metas 的拓扑重建(不实例化 ParameterServer)
   metas = {
       0: ("mlx5_0", "10.0.0.1:9999"),   # (rdma_device, p2p_store_addr)
       1: ("mlx5_1", "10.0.0.2:9999"),
       2: ("mlx5_0", "10.0.0.3:9999"),
   }
   remote_topo = {}
   for rank, (nic, addr) in metas.items():
       remote_topo.setdefault(f"{nic}@{addr.split(':')[0]}", set()).add(rank)
   print(remote_topo)
   ```

2. 对照 [ps.py:298-303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L298-L303),确认你的复刻与源码逻辑等价。

**需要观察的现象**:输出形如 `{'mlx5_0@10.0.0.1': {0}, 'mlx5_1@10.0.0.2': {1}, 'mlx5_0@10.0.0.3': {2}}`——rank 0 与 rank 2 虽共用网卡名 `mlx5_0`,但分属不同主机,因此是两个不同的拓扑键。

**预期结果**:拓扑键由「网卡名+主机 IP」联合决定;同一网卡名在不同主机上是不同键。这正是 u5-l6 贪心分配里「按网卡分组」的粒度。运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:如果把调用顺序写成「先 `load_metas` 再 `gather_metas`」,会发生什么?

**答案**:新 gather 会执行 `_current_global_parameter_metas = {}` 并重新填充为本世界的(空注册)表,`load_metas` 导入的旧实例表被整体冲掉;同时 `_remote_rdma_devices` 被 gather 末尾的 `copy()` 重置回本地拓扑。最终 P2P 读会在空全局表上断言失败(`parameter metas is empty`)或读不到旧实例。所以顺序必须是 gather 在前、load 在后。

**练习 2**:`load_metas` 为什么不顺便重建 `_local_rdma_devices`?

**答案**:本地拓扑描述的是「本世界哪些 rank 在哪块网卡后面」,这份信息只有本世界的 `gather_metas` 能产生(all_gather_object 遍历的是本进程组的成员);外部 metas 里根本没有新实例自己的信息。硬用旧表当本地拓扑,会让 `_assign_receiver_ranks` 把 receiver 候选错算到旧 rank 头上。

**练习 3**:`load_metas` 里两个断言检查的是 `is not None`,但 `DataToGather` 构造时 `rdma_device=self._rdma_device or ""` 允许空字符串。这两者矛盾吗?

**答案**:不矛盾但确有缝隙:断言能拦住 `None`(比如手写 JSON 漏字段时 pydantic 会先以 422 拦截,或模型显式传 None),但空字符串会通过断言,生成形如 `"@10.0.0.1"` 的退化拓扑键。实际上 join 只在支持 P2P 的后端(CUDA/NPU)有意义,正常路径下 `rdma_device` 不会为空;断言是防御性检查而非完整校验。

### 4.3 join() 编排:从外部 metas 到 P2P 拉取全流程

#### 4.3.1 概念说明

[examples/update.py](../../examples/update.py) 的 `join()` 把前面所有零件串成一条流水线。理解它的关键,是分清**哪些步骤作用于新世界(进程组内集合通信)**、**哪些步骤跨越到旧世界(仅靠 metas 与 RDMA)**:

- 建进程组、等 vLLm 就绪、barrier、`gather_metas`、`update` 的 broadcast 装载——都发生在**新世界**;
- `load_metas` 与 `_copy_to_buffer` 的 RDMA 读——借助 metas **跨到旧世界**;
- 旧实例全程只贡献「活着的锁页内存 + 活着的 transfer engine 注册」。

还有一个容易忽略的点:**新实例从未 `register_checkpoint`**。`join()` 里 `gather_metas(checkpoint_name)` 传的是一个没有注册过的名字——这不是 bug 而是设计:`gather_metas` 对未注册的名字走「空注册」路径,本 rank 贡献空的 buffer 清单,于是新实例的 rank 不进全局表、不能当 owner,但**仍被计入本地拓扑、可以当 receiver**。这正是 u3-l3 讲过的空注册语义在 join 里的应用。

#### 4.3.2 核心流程

`join()` 的七步([examples/update.py:131-159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L131-L159)):

```
① 读 metas:  load_metas_file(文件字节) 或 metas_url(HTTP GET)
              → _METAS_ADAPTER.validate_json 反序列化成 dict[int, MemoryBufferMetaList]
              → 两者都没有 → ValueError
② ps.init_process_group()          # 新世界进程组(auto_pg=True)
③ check_vllm_ready(...)            # 等【新】实例的 vLLM /health 可用(仅组首重试)
④ dist.barrier()                   # 新世界对齐
⑤ ps.gather_metas(checkpoint_name) # 未注册 → 空注册;构建 _local_rdma_devices(新拓扑)
⑥ ps.load_metas(metas)             # 全局表 ← 旧表;_remote_rdma_devices ← 旧拓扑
⑦ ps.update(checkpoint_name, req_func, ranks=list(range(P)))   # P2P 模式
```

第 ⑦ 步内部(u3-l4/u5-l6 已精读,这里只标注 join 视角的数据来源):

```
update(ranks=[0..P-1])
 ├─ p2p_update = True;新世界所有 rank 都是 receiver(need_update 恒真)
 ├─ _gen_h2d_buckets(_current_global_parameter_metas,   # ← 旧表(load_metas 注入)
 │                   local_topo=_local_rdma_devices,    # ← 新拓扑(第⑤步 gather)
 │                   remote_topo=_remote_rdma_devices,  # ← 旧拓扑(第⑥步 load_metas)
 │                   ranks=[0..P-1])
 │   └─ _assign_receiver_ranks:owner 按旧网卡分组,receiver 取新拓扑每组最小 rank,
 │      列主序轮转 + occupied_devices 去重(u5-l6 的贪心)
 ├─ 每个接收 rank 预取自己的桶:
 │   _copy_to_buffer(..., owner_rank=旧rank)
 │   └─ _get_addr_ptrs(旧rank) → 旧 p2p_store_addr + 旧 (ptr,size) 清单
 │      → p2p_store.batch_transfer_sync_read(旧addr, 本机GPU缓冲, 旧指针, 长度)  # RDMA 单边读
 └─ dist.broadcast(以 receiver_rank 为源) + ZMQ 张量清单 → 新世界的 worker 装载
```

要点:owner rank 在这里只是**旧表的字典键**。`_get_addr_ptrs` 拿着键查到的值自带旧实例的地址与指针,所以即使旧 owner rank 的数值恰好等于新世界某个 rank,读取目标依然由 metas 值决定、指向旧实例——配对逻辑自始至终没有把两个编号空间混用。

#### 4.3.3 源码精读

元数据的两种来源与兜底报错:

[examples/update.py:141-149](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L141-L149)——文件通道用 `_METAS_ADAPTER.validate_json(f.read())` 反序列化;HTTP 通道用 `httpx.get(metas_url, timeout=300.0)` 拉字节后走**同一个**适配器校验。两通道产出同一种 `dict[int, MemoryBufferMetaList]`,后续代码完全不区分来源。两者都为空则抛 `ValueError`。

编排主体:

[examples/update.py:150-155](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L150-L155)——建组 → 等新 vLLM 就绪 → barrier → `gather_metas`(此时 checkpoint 未注册,走空注册)→ `load_metas(metas)`。注意 gather 与 load 的**紧邻顺序**正是 4.2 节论证的时序约束;`check_vllm_ready` 放在 barrier 之前,让慢启动的 vLLM 不阻塞别人(内部仅组首进程重试)。

P2P 更新触发:

[examples/update.py:156-159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L156-L159)——`ranks=list(range(inference_parallel_size))`:新实例全部 rank 都是要更新的目标,于是 `update` 走 P2P 分支。对比 `update_weights` 里广播分支的 `ranks` 缺省,同一入口按 ranks 是否非空分流(u1-l4 的结论)。

空注册的容错入口:

[checkpoint_engine/ps.py:472-475](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L472-L475)——`gather_metas` 对 `_get_memory_pool` 抛出的 `RuntimeError`(checkpoint 未注册)**捕获并降级为空列表**,随后构造出的 `DataToGather` 带 `memory_buffer_metas_list=[]`。join 模式的新实例正是走这条路:不注册 checkpoint 也能安全 gather。

双拓扑进入切桶与分配:

[checkpoint_engine/ps.py:97-105](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L97-L105)——`_gen_h2d_buckets` 末尾按 `ranks` 分岔:`ranks` 非空时先用 `ranks_set` 过滤 `local_topo`(只保留目标 rank),再交给 `_assign_receiver_ranks`;`ranks` 为空则是 colocate 广播、owner 即 receiver。join 必然走前一岔。

[checkpoint_engine/ps.py:122-124](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L122-L124)——`_assign_receiver_ranks` 第一步从 **remote_topo**(旧拓扑)反查出 `rank → 网卡` 映射:此后每个 owner rank 的网卡归属都查这张表。因为 `_remote_rdma_devices` 是 `load_metas` 用**同一份** metas 重建的,每个出现在全局表里的 owner 必然能查到,不会 KeyError——两份数据出自同源,这是 join 路径的自洽保障。

RDMA 读的地址解析:

[checkpoint_engine/ps.py:716-719](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L716-L719)——`_get_addr_ptrs(owner_rank)` 以 owner rank 为键从 `_current_global_parameter_metas` 取出**旧实例**的 `p2p_store_addr`(读的目的地服务)与 `(ptr, size)` 清单(旧锁页内存里每块 buffer 的绝对地址)。这四行就是「跨世界」的桥梁:键是新世界算法给出的,值全部指向旧世界。

[checkpoint_engine/ps.py:692-713](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L692-L713)——`_copy_to_buffer` 中 `owner_rank is not None` 的分支:对桶的每段 `BucketRange` 计算 `remote_ptrs = ptrs[b.idx][0] + b.offset`(旧内存绝对地址加段内偏移,即 u3-l5 说的「P2P 远端读时当绝对地址用」),凑齐三平行数组后一次 `batch_transfer_sync_read` 批量读入本机 GPU 缓冲。owner 为 `None` 的另一岔(本地 H2D 拷贝)在 join 下不会出现——`ranks` 非空时 owner_rank 恒被赋值:

[checkpoint_engine/ps.py:855-862](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L855-L862)——预取调用处 `receiver_rank_buckets[i][0] if ranks else None`:join 传入的 `ranks` 非空,owner_rank 恒为旧 rank。也正因如此,新实例从未注册过 checkpoint 也不会在此路径触发 `_get_memory_pool` 的「未注册」异常。

接收侧缓冲也要注册:

[checkpoint_engine/ps.py:827-832](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L827-L832)——P2P 模式下 receiver 用固定名 `__ipc_buffer__` 把自己的 GPU 缓冲(双缓冲或 h2d_buffer)注册进**自己的** transfer engine。u5-l5 的结论:RDMA 单边读要求**两侧**内存都注册——owner 侧是锁页池(旧实例在 `register_checkpoint` 时注册),接收侧就是这里。注意这个注册发生在新实例自己的 store 上,与旧实例无关。

#### 4.3.4 代码实践

**实践目标**:用纯 CPU 的 REPL 验证「坏 metas 会在进入分布式之前被拦下」,并跟踪 join 的调用链。

1. 构造一个格式非法的 metas 文件(示例代码):

   ```bash
   # 示例代码:写入一个既不是合法 JSON、也不符合模型的文件
   echo 'not a valid json' > /tmp/bad_metas.json
   python -c "
   from pydantic import TypeAdapter
   from checkpoint_engine.data_types import MemoryBufferMetaList
   adapter = TypeAdapter(dict[int, MemoryBufferMetaList])
   try:
       adapter.validate_json(open('/tmp/bad_metas.json','rb').read())
   except Exception as e:
       print(type(e).__name__)
   "
   ```

2. 对照 [examples/update.py:141-143](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L141-L143) 确认:这段 REPL 复刻的就是 `join()` 第 ① 步的文件分支。若这一步抛错,异常发生在 `ps.init_process_group()` 之前,新世界进程组根本不会建立。
3. 再传一个「合法 JSON 但字段不符」的文件(如 `{"0": {"foo": "bar"}}`),重复上述验证(与 [tests/test_api.py:100-109](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L100-L109) 的用例同构)。

**需要观察的现象**:两次分别抛出 pydantic 的 JSON 解析错误与模型校验错误(均为 `ValidationError`),错误信息会指明第一个非法位置。

**预期结果**:metas 的线格式校验完全由 `_METAS_ADAPTER` 在反序列化时把关,坏数据进不到任何 `ParameterServer` 状态。具体异常文本因 pydantic 版本而异,**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:`join()` 里 `gather_metas` 传的 `checkpoint_name` 并未注册,为什么这一步不抛「checkpoint is not registered」?

**答案**:[ps.py:472-475](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L472-L475) 捕获了 `_get_memory_pool` 的 `RuntimeError` 并降级为 `memory_pool = []`——未注册等价于空注册,该 rank 贡献空 buffer 清单、不入全局表,但拓扑里仍占一个位置,可当 receiver。

**练习 2**:join 场景下 `_assign_receiver_ranks` 的 owner 与 receiver 分别来自哪张表?为什么不会因为「新旧 rank 编号撞车」出错?

**答案**:owner 分组来自 `remote_topo`(`load_metas` 重建的旧拓扑,值是旧 rank),receiver 候选来自 `local_topo`(新世界 gather 构建的本地拓扑,值是新 rank)。算法只用 rank 当字典键/集合值,真正的语义载体是 `网卡名@IP` 键;owner rank 的读取地址永远从 `_current_global_parameter_metas[owner_rank]` 的**值**(旧地址、旧指针)取得,因此编号撞车不影响数据来源。

**练习 3**:新实例的 `update` 结束后,旧实例的 metas 还留在新实例内存里吗?下一次普通(非 join)更新会受它影响吗?

**答案**:会残留,但无害:`update_weights` 路径的 `gather_metas` 开头重置 `_current_global_parameter_metas = {}` 并重填,`_remote_rdma_devices` 也会在 gather 末尾被重新 copy,残留旧表被整体覆盖。这也是「load_metas 须在每次 gather 之后」的另一面。

### 4.4 /v1/metas 端点:HTTP 控制面通道与往返契约

#### 4.4.1 概念说明

文件通道要求共享磁盘,HTTP 通道则让 metas 成为**可查询的活状态**:[api.py](../../checkpoint_engine/api.py) 的 `_init_api` 暴露了一对互补端点:

- `GET /v1/metas` → `ps.get_metas()`:把当前全局参数表序列化成 JSON 返回;
- `POST /v1/metas` → `ps.load_metas(metas)`:把请求体反序列化后注入。

这对端点与 `--metas-url` 组合成完整的 HTTP 通道:把 `--metas-url` 指向任何一个能返回这份 JSON 的地址即可。值得强调的是**往返契约(round-trip contract)**:GET 吐出的字节必须能被 POST 原样接受——这不是理所当然的(序列化器与校验器是两套代码),而是靠 pydantic 的 `PlainSerializer`/`PlainValidator` 成对实现(u2-l1),并用 `test_round_trip_get_then_load` 测试钉死。

还有一个部署层面的错位要指出:`__main__.py` 的 API 服务是 **UDS-only** 部署,而 `join()` 的 `--metas-url` 用的是裸 `httpx.get`(只能发 TCP HTTP 请求)。所以「直接把 `--metas-url` 指到 UDS 部署的 PS」并不成立——实际使用中 metas_url 指向的应是一个 TCP 可达的 HTTP 服务(例如在 UDS 前面架一层 HTTP 反向代理,或由编排系统把 GET 结果落到对象存储再给 URL)。文件通道则没有这个约束。

#### 4.4.2 核心流程

`GET /v1/metas` 的处理:

1. 调 `ps.get_metas()` 取内部字典(引用);
2. FastAPI 依据路由函数的返回类型注解 `dict[int, MemoryBufferMetaList]` 做 pydantic 序列化:`torch.dtype → "torch.float16"` 字符串、`torch.Size → [2, 3]` 数组、`ptr` 大整数原样输出;
3. 业务异常(如 metas 尚未 gather)→ `HTTPException(500)`。

`POST /v1/metas` 的处理:

1. 请求体先经 FastAPI/pydantic **校验**:JSON 不合法或字段不符 → **422**,且 `ps.load_metas` **不会被调用**(校验失败短路在路由函数之前);
2. 校验通过 → 反序列化成 `dict[int, MemoryBufferMetaList]` → `ps.load_metas(metas)`;
3. `load_metas` 抛出的业务异常被 `wrap_exception` 捕获 → 500 + 异常文本。

#### 4.4.3 源码精读

GET 端点:

[checkpoint_engine/api.py:83-89](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L83-L89)——`get_metas` 路由的返回类型注解 `dict[int, MemoryBufferMetaList]` 就是序列化说明书:FastAPI 据此把 pydantic 模型渲染为 JSON 响应;异常路径显式 `raise HTTPException(500)`,区别于其他端点统一走 `wrap_exception`(GET 有返回体、不便复用「无返回」的包装器)。

POST 端点:

[checkpoint_engine/api.py:91-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L91-L93)——`load_metas` 路由把请求体声明为 `dict[int, MemoryBufferMetaList]`,FastAPI 自动完成「校验 + 反序列化」,路由体内只剩一行 `wrap_exception(lambda: ps.load_metas(metas))`。422 拦截发生在进入这行之前。

异常包装器:

[checkpoint_engine/api.py:59-65](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L59-L65)——`wrap_exception` 把业务异常转成 500 响应并把异常文本写进 body,PS 方法抛错不至于拖垮 uvicorn 连接。

序列化的底层支撑:

[checkpoint_engine/data_types.py:35-40](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L35-L40)——`_TorchDtype` 用 `PlainSerializer(lambda x: str(x))` 把 `torch.float16` 写成字符串、`PlainValidator` 再解析回来;`_TorchSize` 同理(list/tuple ↔ torch.Size)。GET 能输出、POST 能吃回,靠的就是这组成对的钩子。

往返契约的测试钉子:

[tests/test_api.py:126-139](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L126-L139)——`test_round_trip_get_then_load`:GET 拿到响应字节,原样作为 POST 的 body 发回,断言 200 且 `ps.load_metas` 被以**语义相等**的对象调用。这条测试保证了「任何 GET 的消费者(文件、对象存储、代理)回灌 POST 都合法」——join 的 HTTP 通道正是最典型的消费者。

构造假 metas 的测试工厂:

[tests/test_api.py:21-39](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L21-L39)——`_make_meta(rdma_device, ip)` 造出含一个 `ParameterMeta`(f16、shape [2,3]、aligned_size 12)、`ptr=0x12345678` 的 `MemoryBufferMetaList`。注意它是**纯 CPU** 构造的——pydantic 模型不依赖 GPU,这正是 metas 层可以独立测试的原因。

UDS 部署的事实约束:

[checkpoint_engine/__main__.py:22-24](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/__main__.py#L22-L24)——CLI 启动强制 `--uds` 并 `uvicorn.run(..., uds=args.uds)`,API 只监听 Unix domain socket;而 [examples/update.py:145](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L145) 的 `httpx.get(metas_url)` 只能发 TCP 请求。两者叠加即「UDS 上的 `/v1/metas` 不能直接当 `--metas-url` 用」的结论。

#### 4.4.4 代码实践

**实践目标**:在纯 CPU 环境完整跑通 metas 端点的行为面,包括 422 短路与 500 传播。

1. 运行 metas 端点的全部 CPU 测试:

   ```bash
   python -m pytest tests/test_api.py -v
   ```

2. 重点观察五个用例的行为差异:
   - `test_get_metas_returns_json`:GET 返回 200 且 JSON 能被 `_METAS_ADAPTER.validate_json` 还原成原对象;
   - `test_get_metas_propagates_ps_error`:PS 抛错 → 500 且文本透传;
   - `test_load_metas_rejects_bad_json` / `test_load_metas_rejects_schema_mismatch`:422,且 `ps.load_metas.assert_not_called()`——**校验失败时 PS 零副作用**;
   - `test_round_trip_get_then_load`:GET 字节直接回灌 POST 成功。
3. 打开 [tests/test_api.py:51-54](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_api.py#L51-L54),注意 `MagicMock` 替身的用法:整个测试不需要真实 `ParameterServer`(也就不需要 GPU/分布式)。

**需要观察的现象**:`test_api.py` 没有 `gpu` marker,在 `-m "not gpu"` 的 CI 选择下会正常执行;全部用例应通过。

**预期结果**:7 个用例全绿;422 与 500 的分界清晰——「请求不像 metas」是 422,「metas 合法但 PS 处理失败」是 500。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:为什么 `POST /v1/metas` 校验失败返回 422 而不是 500?这两种状态码分别保护了谁?

**答案**:422 是 FastAPI/pydantic 在**进入路由函数之前**的请求体校验失败,此时 `ps.load_metas` 尚未被调用——它保护 PS 内部状态不被半成品数据污染(测试用 `assert_not_called` 钉死这一点);500 是路由函数内业务异常经 `wrap_exception` 转换的结果,表示「数据合法但处理失败」。调用方据此可以区分「我发的数据错了」与「服务端出问题了」。

**练习 2**:GET 与 POST 用的是两套代码(序列化器 vs 校验器),凭什么保证 GET 的输出一定能被 POST 接受?

**答案**:三层保障:(1) pydantic 的 `PlainSerializer` 与 `PlainValidator` 在每个自定义类型上成对定义,`WithJsonSchema` 让两边共享同一 JSON 形状;(2) 两端点共用同一个模型 `dict[int, MemoryBufferMetaList]`;(3) `test_round_trip_get_then_load` 把契约固化成回归测试,任何一侧漂移都会被测试抓住。

**练习 3**:如果想在 UDS 部署的 PS 上用 `--metas-url`,最少要补什么?

**答案**:补一条 TCP HTTP 通路。方案包括:在 UDS 前架一个 HTTP 反向代理把 `GET /v1/metas` 暴露成 TCP;或由编排器先通过 UDS 调 GET、把结果落到对象存储/静态文件服务,再把该 URL 传给 `--metas-url`;或者干脆改用文件通道 `--load-metas-file`。`join()` 侧的代码事实是 `httpx.get(metas_url)` 只支持 TCP HTTP。

## 5. 综合实践

**任务:在纯 CPU 环境模拟一次完整的「导出 → 传输 → 导入」metas 交接,并验证往返无损。**

把下面的脚本存为 `metas_roundtrip_demo.py`(示例代码,不属于项目源码;依赖已随项目安装):

```python
# 示例代码:metas 交接仿真 —— 文件通道 + HTTP 通道,纯 CPU 运行
import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import torch

from checkpoint_engine.api import _init_api
from checkpoint_engine.data_types import (
    MemoryBufferMetaList, MemoryBufferMetas, ParameterMeta,
)

ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])

def fake_metas() -> dict[int, MemoryBufferMetaList]:
    # 模拟一个 2 rank 的旧世界:两台主机、各一块网卡
    def one(nic: str, ip: str) -> MemoryBufferMetaList:
        return MemoryBufferMetaList(
            p2p_store_addr=f"{ip}:12345",
            rdma_device=nic,
            memory_buffer_metas_list=[MemoryBufferMetas(
                metas=[ParameterMeta(name="w", dtype=torch.float16,
                                      shape=torch.Size([2, 3]), aligned_size=12)],
                ptr=0xDEADBEEF, size=1024)],
        )
    return {0: one("mlx5_0", "10.0.0.1"), 1: one("mlx5_1", "10.0.0.2")}

metas = fake_metas()

# ── 通道①:文件通道(对应 --save-metas-file / --load-metas-file) ──
blob = ADAPTER.dump_json(metas)
open("/tmp/global_metas.json", "wb").write(blob)          # 旧实例 rank0 导出
loaded = ADAPTER.validate_json(open("/tmp/global_metas.json", "rb").read())
assert loaded == metas, "文件通道往返失真"

# ── 通道②:HTTP 通道(GET /v1/metas → POST /v1/metas) ──
ps_old, ps_new = MagicMock(), MagicMock()
ps_old.get_metas.return_value = metas
old_client = TestClient(_init_api(ps_old))                 # 旧实例的 API
resp = old_client.get("/v1/metas")                         # 编排器代取 metas
assert resp.status_code == 200

new_client = TestClient(_init_api(ps_new))                 # 新实例的 API
resp2 = new_client.post("/v1/metas", content=resp.content,
                        headers={"content-type": "application/json"})
assert resp2.status_code == 200
ps_new.load_metas.assert_called_once_with(metas)           # 注入语义相等

# ── 验收:导出的 JSON 与手工推演的远端拓扑一致 ──
topo = json.loads(resp.content)
remote = {}
for rank, m in topo.items():
    remote.setdefault(f"{m['rdma_device']}@{m['p2p_store_addr'].split(':')[0]}", set()).add(int(rank))
print("远端拓扑(load_metas 将重建):", remote)
print("dtype/shape 的线格式:", topo["0"]["memory_buffer_metas_list"][0]["metas"][0]["dtype"],
      topo["0"]["memory_buffer_metas_list"][0]["metas"][0]["shape"])
print("全部断言通过:metas 交接往返无损")
```

操作步骤:

1. 在仓库根目录运行 `python metas_roundtrip_demo.py`。
2. 对照 4.2.3 与 4.4.3 的源码,在脚本里找到每一行对应的真实调用点(`dump_json` ↔ [examples/update.py:115-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L115-L117);`GET` ↔ [api.py:83-89](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L83-L89);`POST` ↔ [api.py:91-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/api.py#L91-L93);拓扑推演 ↔ [ps.py:298-303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L298-L303))。
3. 进阶:把 `metas` 里 rank 1 的网卡也改成 `mlx5_0` 但保留不同 IP,重跑并观察拓扑键如何变化;再删掉某个 `ParameterMeta` 的 `aligned_size` 字段,观察 `dump_json` 之前的构造阶段就抛出的校验错误。

预期结果:脚本打印两个拓扑键(`mlx5_0@10.0.0.1`、`mlx5_1@10.0.0.2`)与线格式(`torch.float16`、`[2,3]`),最后输出「全部断言通过」。具体输出**待本地验证**。

## 6. 本讲小结

- join 复用的本质是**元数据交接**:新实例不读 checkpoint 文件,凭旧实例的 metas(`ptr`/`size`/`p2p_store_addr`/`rdma_device`)用 Mooncake RDMA 单边读直接从旧实例的锁页内存拉权重;旧实例只需进程存活,不参与任何集合通信。
- 两条元数据通道共用一个线格式 `_METAS_ADAPTER = TypeAdapter(dict[int, MemoryBufferMetaList])`:文件通道(`--save-metas-file` 由 rank 0 写 JSON 字节,`--load-metas-file` 读回)与 HTTP 通道(`GET /v1/metas` ↔ `--metas-url`),二者为 argparse 互斥组。
- `load_metas` 是定向改写入口:整体替换 `_current_global_parameter_metas` 并以 `网卡名@主机IP` 为键重建 `_remote_rdma_devices`,不动 `_local_rdma_devices`;且必须在 `gather_metas` 之后调用(gather 会重置全局表,且本地拓扑只能来自本世界 gather)。
- `join()` 的编排:读 metas → 建组 → 等新 vLLM 就绪 → barrier → 空注册的 gather(未注册被捕获降级为 `memory_pool=[]`)→ `load_metas` → `update(ranks=range(P))` 走 P2P。
- P2P 数据面里 owner rank 只是旧表的字典键:`_get_addr_ptrs` 取到的值自带旧实例地址与指针,`_copy_to_buffer` 据此发起 `batch_transfer_sync_read`;owner/receiver 分属两个编号空间,靠共同的拓扑键空间配对(u5-l6)。
- metas 端点的契约是「GET 字节 == POST 可接受」:422 在进入路由前拦截坏请求且 PS 零副作用,500 只承载业务异常;`test_round_trip_get_then_load` 把往返契约钉成回归测试。UDS-only 部署与裸 `httpx.get` 的错位意味着 `--metas-url` 需要一条 TCP HTTP 通路。

## 7. 下一步学习建议

- 下一讲(u6-l4)转向 FP8 补丁、项目限制与二次开发方向:join 复用正是「动态扩容」场景的支撑机制,补上量化权重更新后即可拼出完整的弹性推理服务蓝图。
- 建议按顺序重读三处源码把本讲闭环:`gather_metas`([ps.py:462-525](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L462-L525))看全局表与两张拓扑的诞生,`load_metas`([ps.py:295-303](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L295-L303))看定向改写,`_assign_receiver_ranks`([ps.py:108-163](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L108-L163))看双拓扑的消费。
- 若要在真实集群体验 join,按 README 的两阶段命令启动(CUDA/NPU 且安装 `[p2p]` extra),用 `--update-method all` 的作业做旧实例、`--save-metas-file` 导出后另起一组 `--load-metas-file` 加入,观察日志中 `Update weights with setting ranks` 的耗时与 RDMA 拓扑打印。
- 想深挖 HTTP 通道的工程化(健康检查、代理、鉴权),可从 [api.py](../../checkpoint_engine/api.py) 的 `_init_api` 出发对比 vLLM 的 API server 设计,思考为什么本项目控制面刻意做得极薄。
