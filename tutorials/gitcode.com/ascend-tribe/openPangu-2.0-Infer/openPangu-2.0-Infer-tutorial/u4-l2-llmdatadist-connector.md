# LLMDataDistConnector 源码精读

## 1. 本讲目标

上一讲（u4-l1）我们搞清楚了 PD 分离的**配置面**：`kv-transfer-config` 四字段 JSON 如何从 ansible 模板一路传到 `vllm serve`。本讲进入**实现面**，精读 `LLMDataDistConnector`——openPangu-2.0-Infer 中真正把 KV Cache 从 P 节点搬 to D 节点的组件。

学完本讲，你应该能够：

1. 说出 `KVConnectorBase_V1` 接口下四个协作类（Prefill/Decode 两侧的 Scheduler 与 Worker）各自的职责边界。
2. 完整描述 decode 侧从 `register_link` 建链到 `pull_blocks` 拉取 KV 的全过程。
3. 理解双向心跳（ZMQ PUB/SUB）与 `force_unlink` 如何在对端宕机时清理死链路。
4. 画出一次 KV 传输的完整时序图，并标注每个步骤所在的类与方法。

## 2. 前置知识

### 2.1 vLLM V1 引擎的双进程结构

vLLM V1 引擎把一次推理拆成两个角色：

- **Scheduler（调度器）**：决定「这一步调度哪些请求、分配哪些 KV 块」，工作在**逻辑块号**层面（block id 是整数），不接触显存。
- **Worker（执行器）**：持有模型与 KV Cache 张量，真正做前向计算与数据搬运。

两者通过 `SchedulerOutput` / `KVConnectorMetadata` 传递消息。这个拆分是理解本讲的关键：**KV 传输的"决策"发生在 Scheduler，"搬运"发生在 Worker**。

### 2.2 KVConnectorBase_V1 接口

`KVConnectorBase_V1` 是 vLLM 定义的 KV 传输插件接口（位于 vLLM 包内，本仓不修改它，只实现它）。任何 connector 都会在**同一份代码里分别以两种身份被实例化**：

- `KVConnectorRole.SCHEDULER`：挂在调度器上，只实现调度侧钩子。
- `KVConnectorRole.WORKER`：挂在执行器上，只实现执行侧钩子。

在 u2-l3 中我们已见过 `NPUWorker.initialize_from_config` 首行调用 `ensure_kv_transfer_initialized`（[components/omni-npu/src/omni_npu/worker/npu_worker.py:L209](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_worker.py#L209)），connector 就是在那时被创建并注册 KV 张量的。

### 2.3 ZMQ 的三种模式

本讲大量使用 ZMQ（ZeroMQ）做**控制面**通信（KV 数据本身不走 ZMQ，走 llm_datadist 的 RoCE 链路）：

| 模式 | 方向 | 特点 | 本讲用途 |
| --- | --- | --- | --- |
| `PUSH`/`PULL` | 单向管道 | 一对一/一对多负载均衡 | D 通知 P「KV 已拉完」；D 发心跳给 P |
| `PUB`/`SUB` | 发布订阅 | 一对多广播 | P 周期广播心跳，D 订阅 |
| IPC | 本机进程间 | `ipc:///tmp/...` 路径 | P 节点内 rank 之间转发 unlink 命令 |

### 2.4 llm_datadist 库

`llm_datadist` 是华为提供的 KV 传输库（Python 包 `import llm_datadist`），底层走 RoCE 网卡做零拷贝传输。它有三个核心概念：

- `LLMDataDist`：引擎对象，`init(options)` 后唯一。
- `link_clusters` / `unlink_clusters`：建立/断开与远端实例的传输链路。
- `cache_manager.register_blocks_cache` + `pull_blocks`：把本地 KV 张量注册成可传输的 cache，再按块号拉取远端数据。

### 2.5 cluster_id：一个 int64 编码的"地址牌"

集群里每个传输参与方都有一个 64 位整数 `cluster_id`，由 `ip_port_to_int` 把 `ip:port`、tp_size、pp_size 打包而成（位布局见 4.4.3 节）。它既是 llm_datadist 建链的标识，也是心跳里自报家门的凭证。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py) | LLMDataDistConnector 主体：门面类 + 四个协作类 | 主战场 |
| [components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py) | llm_datadist 会话管理：建链、注册内存、拉块、容错 | 主战场 |
| [components/omni-npu/src/omni_npu/connector/register.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py) | 把 connector 名字注册进 vLLM 工厂 | 入口链路 |
| [components/omni-npu/src/omni_npu/platform.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py) | `import_kernels` 中触发注册 | 入口链路 |
| [components/omni-npu/src/omni_npu/connector/utils.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/utils.py) | `TP_Convertor`（DCP 场景块重排）与 `get_p_start_rank` | 辅助 |
| [components/omni-npu/tests/unit/connector/test_llmdatadist_connector_v1.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/test_llmdatadist_connector_v1.py) | 生命周期单测（CPU 可跑，mock 硬件） | 实践素材 |
| [components/omni-npu/README.md](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md) | 端点矩阵表格 | 交叉验证 |

## 4. 核心概念与源码讲解

### 4.1 KVConnectorV1 接口与 LLMDataDistConnector 门面

#### 4.1.1 概念说明

`LLMDataDistConnector` 是一个**门面（Facade）类**：它实现了 vLLM 的 `KVConnectorBase_V1`（外加 `SupportsHMA` 混入），但自己几乎不干活——构造函数根据 `role`（SCHEDULER 或 WORKER）和 `kv_role`（kv_producer 或 kv_consumer）把工作**四选一**委托给内部协作类：

| | kv_producer（P 节点） | kv_consumer（D 节点） |
| --- | --- | --- |
| **SCHEDULER 角色** | `PrefillConnectorScheduler` | `DecodeConnectorScheduler` |
| **WORKER 角色** | `PrefillConnectorWorker` | `DecodeConnectorWorker` |

注意 `role` 与 `kv_role` 是两个维度：前者区分进程职能（由 vLLM 在创建 connector 时传入），后者区分传输方向（来自 u4-l1 讲过的 `--kv-role` 参数）。同一个类 `LLMDataDistConnector` 在 P 机 scheduler 进程、P 机 worker 进程、D 机 scheduler 进程、D 机 worker 进程中会实例化出四种完全不同的内部组合。

#### 4.1.2 核心流程

vLLM 调度循环每个 step 依次敲打 connector 的钩子，门面类按身份转发：

```text
Scheduler 进程（每步）:
  get_num_new_matched_tokens(req)   → 问 connector: 这请求有多少 token 可从远端"白拿"？
  update_state_after_alloc(...)     → 块已分配，登记待传输请求
  build_connector_meta(sched_out)   → 打包 KVConnectorMetadata 发给 Worker 进程
  request_finished(req, blocks)     → 请求结束：块要不要延迟释放？要带什么参数走？

Worker 进程（每步）:
  register_kv_caches(kv_tensors)    → 启动时把 KV 池张量注册给传输层（一次）
  start_load_kv(metadata)           → 收到 metadata，开始拉 KV（异步）
  get_finished(finished_ids)        → 哪些请求的收/发已完成？
  save_kv_layer / wait_for_save     → 逐层保存钩子（本实现为空操作）
```

metadata 在两个进程间由 vLLM 框架序列化传递，所以 Scheduler 侧写的 `DatadistConnectorMetadata` 会在 Worker 侧原样出现。

#### 4.1.3 源码精读

**构造函数：四选一分发。** 这是全篇的"总开关"：

```python
if role == KVConnectorRole.SCHEDULER:
    if self.is_prefill:
        self.connector_scheduler = PrefillConnectorScheduler(...)
    else:
        self.connector_scheduler = DecodeConnectorScheduler(vllm_config)
    self.connector_worker = None
elif role == KVConnectorRole.WORKER:
    if self.is_prefill:
        self.connector_worker = PrefillConnectorWorker(...)
    else:
        self.connector_worker = DecodeConnectorWorker(...)
    self.connector_scheduler = None
```

见 [llmdatadist_connector_v1.py:L157-L168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L157-L168)。构造函数前半段还做了三件事（L154-L156）：判定 `is_prefill`（`kv_role == "kv_producer"`）、初始化空 metadata、记录 `pp_partition`。此外 L137-L139 有一处特殊处理：MLA 模型强制 `kv_parallel_size = 1`。

**门面方法：纯转发 + 防呆。** 以 `get_num_new_matched_tokens` 为例：

```python
def get_num_new_matched_tokens(self, request, num_computed_tokens):
    if self.connector_scheduler is None:
        raise RuntimeError("self.connector_scheduler cannot be None")
    return self.connector_scheduler.get_num_new_matched_tokens(request, num_computed_tokens)
```

见 [llmdatadist_connector_v1.py:L174-L194](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L174-L194)（`get_num_new_matched_tokens`、`update_state_after_alloc`、`build_connector_meta`），Worker 侧转发见 [L223-L246](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L223-L246)。`wait_for_layer_load` / `save_kv_layer` / `wait_for_save` 是**空操作**（[L248-L259](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L248-L259)）——llm_datadist 直接整块搬运，不需要逐层保存。`get_finished_count`（[L206-L218](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L206-L218)）对 prefill 固定返回 1，因为 P 实例只有 rank 0 管理来自 D 的请求回执。

**注册链路：connector 名字如何被 vLLM 认识。** u4-l1 讲过 `kv-transfer-config` 里的 `kv_connector` 字段必须与注册名逐字符一致，现在看注册发生在这里：

```python
def _safe_register(name: str, module: str, class_name: str) -> None:
    ...
    KVConnectorFactory.register_connector(name, module, class_name)

def register_connectors() -> None:
    _safe_register(
        "LLMDataDistConnector",
        "omni_npu.connector.llmdatadist_connector_v1",
        "LLMDataDistConnector",
    )
```

见 [register.py:L9-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py#L9-L48)。`_safe_register` 先探测工厂内部注册表，已存在则跳过（防重复注册），体现防御式风格。调用时机在 `NPUPlatform.import_kernels`：

```python
from omni_npu.connector import register_connectors
register_connectors()
for ep in entry_points().select(group="omni.kv_connectors"):
    register_fn = ep.load()
    register_fn()
```

见 [platform.py:L83-L95](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L83-L95)。注意它同时遍历 `omni.kv_connectors` entry point 组——第三方可以把自己的 connector 注册成 pip 入口来扩展（u10-l3 二次开发讲义会用到这一点）。

**两个 metadata 容器。** D 侧用 `DatadistConnectorMetadata`（每个请求一个 `ReqMeta`，含本地/远端块号、远端地址、token 数等 11 个字段，见 [L65-L112](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L65-L112)）；P 侧用 `DatadistConnectorMetadataPrefill`（只有 `finish_time` 一个字段，见 [L115-L128](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L115-L128)）——P 侧 metadata 的唯一使命是把"请求完成时刻"从 scheduler 进程带到 worker 进程，用于延迟释放计时。

#### 4.1.4 代码实践

**实践：验证注册链路（可在无 NPU 的开发环境运行）。**

1. 实践目标：确认 `register_connectors()` 把 `"LLMDataDistConnector"` 写进了 vLLM 的 `KVConnectorFactory`，且重复调用是幂等的。
2. 操作步骤：
   ```bash
   cd components/omni-npu
   pytest tests/unit/connector/test_register.py -v
   ```
   该测试直接 mock 了工厂与 logger，断言注册调用发生（见 [test_register.py:L123-L158](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/test_register.py#L123-L158)）。
3. 需要观察的现象：输出中出现 `connector: registered KV connector: LLMDataDistConnector -> omni_npu.connector.llmdatadist_connector_v1.LLMDataDistConnector` 日志；`test_register_connectors_multiple_calls` 通过（第二次调用被跳过）。
4. 预期结果：全部用例通过。若本机没有 vllm/omni-npu 依赖，请在部署容器内执行（`docker exec -it <容器名> bash` 后进入包安装目录），或标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LLMDataDistConnector.__init__` 里 P 侧只创建 scheduler **或** worker 其一，而不是两个都建？

**答案**：因为 vLLM 在 scheduler 进程和 worker 进程中**分别**实例化 connector（各传一次 `role`）。同一进程内只需要一种身份；门面类用 `self.connector_scheduler is None` 防呆检查保证调错了方法会立刻报 `RuntimeError` 而非静默失败。

**练习 2**：`kv_role` 与 `role` 各自回答什么问题？

**答案**：`kv_role`（kv_producer/kv_consumer）回答「KV 往哪边流」，来自 `--kv-role` 启动参数，决定选 Prefill 系还是 Decode 系协作类；`role`（SCHEDULER/WORKER）回答「本进程是调度器还是执行器」，由 vLLM 框架在实例化时传入，决定建 Scheduler 类还是 Worker 类。二者正交，组合出四个协作类。

### 4.2 Prefill 侧：PrefillConnectorScheduler 与 PrefillConnectorWorker

#### 4.2.1 概念说明

P 节点的任务看似简单——"KV 都算出来了，发走就行"——但有一个核心矛盾：**KV 发送是异步的，而块的释放是本地决策**。P 侧 scheduler 在请求结束时并不知道 D 什么时候来拉（D 可能还在排队）。如果立刻释放块，D 拉到的就是被复用污染的脏数据；如果永不释放，P 的 KV 池会被早已完成的请求占满。

P 侧的解法是**延迟释放 + 双通道回收**：

1. 请求完成时，scheduler 声明"这些块先别释放"（`delay_free_blocks`），并把块号连同本机地址打包成 `kv_transfer_params` 随请求发给 D。
2. D 拉完后通过 ZMQ 回执，P 收到回执才真正放块。
3. 兜底：超过 `BLOCK_RELEASE_DELAY`（默认 600 秒）还没等到回执的，强制释放，防泄漏。

#### 4.2.2 核心流程

```text
P 侧一次请求的 KV 生命周期：

PrefillConnectorScheduler.request_finished(req, block_ids)
    │  status == FINISHED_LENGTH_CAPPED 才处理
    │  记录 finish_time，返回 (True, kv_transfer_params)
    │  kv_transfer_params 随请求转发到 D 实例
    ▼
（D 侧拉取，见 4.3）
    │
PrefillConnectorScheduler.build_connector_metadata()
    │  把 {req_id: finish_time} 塞进 DatadistConnectorMetadataPrefill
    ▼
PrefillConnectorWorker.get_finished(metadata)
    │  收到 D 的 ZMQ 回执(receive_req_list) → 释放块
    │  或 finish_time 超过 600s → 强制释放
```

#### 4.2.3 源码精读

**Scheduler：request_finished 是 P 侧唯一的实质逻辑。**

```python
if request.status != RequestStatus.FINISHED_LENGTH_CAPPED:
    return False, None
delay_free_blocks = len(block_ids) > 0
if delay_free_blocks:
    self.requests_finish_time[request.request_id] = time.monotonic()
return delay_free_blocks, dict(
    remote_block_ids=block_ids,
    remote_cluster_id=self.host_cluster_id,
    remote_host_ip=f"tcp://{self.host_ip}:{self.host_port}",
    ...
)
```

见 [llmdatadist_connector_v1.py:L303-L329](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L303-L329)。返回的第二个值就是 u4-l1 提到的 `kv_transfer_params`——它是 P 写给 D 的"取件码"：块号列表 + 本机 ZMQ 地址 + 集群标识。P 侧的 `get_num_new_matched_tokens` 恒返回 `(0, False)`（[L282-L290](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L282-L290)）——producer 从不做外部加载，这符合直觉。

**Worker 构造：rank 0 是"接待处"。** 见 [L332-L373](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L332-L373)。TP 组内只有 rank 0（且 pp_rank==0）绑定两个 socket：

- `input_socket`：`zmq.PULL`，绑定 `tcp://{host_ip}:{host_port}`（默认 5568，见 L149-L153 的端口计算），接收 D 发来的回执与心跳；
- `hb_socket`：`zmq.PUB`，绑定 `tcp://{host_ip}:{15566}`，向所有 D 广播心跳。

并启动两个守护线程：`get_pulled_kv_req_list`（收消息）与 `heartbeat_timer_func`（发心跳、查超时）。非 rank 0 的进程只启动 `heartbeat_server_func`（IPC 服务，接收 rank 0 转发的 unlink 命令）。

**回执接收线程。** 见 [L487-L509](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L487-L509)：

```python
message = self.input_socket.recv_string()
id_list = json.loads(message)
if id_list[0].startswith("decode_hb:"):
    # 心跳：更新 remote_hb_info[cluster_id_str] = 当前时间
else:
    with self._transfer_lock:
        self.receive_req_list.extend(id_list)  # 回执：这些请求的 KV 已被 D 拉走
```

注意同一条 PULL 管道**复用**了两种消息：D 每 5 秒发来的心跳（`decode_hb:<cluster_id>`）和拉取完成后的回执（`["remote_request_id"]`），靠首元素前缀区分。

**get_finished：双通道释放。** 见 [L443-L485](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L443-L485)。先扫超时（`current_time - finish_time > BLOCK_RELEASE_DELAY`），由于字典按插入序即完成序排列，可以提前 break；再扫回执列表 `receive_req_list`，命中的请求加入 `all_done_sending` 交给 vLLM 释放块。`BLOCK_RELEASE_DELAY` 默认 600 秒，可用环境变量覆盖（[L50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L50)）。

**心跳发布与超时处理。** 见 [L382-L417](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L382-L417)。rank 0 每 `HEARTBEAT_INTERVAL`（5 秒）做三件事：

1. 检查每个远端 D 的最后心跳时间，超过 `CLUSTER_HEARTBEAT_TIMEOUT`（60 秒）的判死；
2. 对判死的 D：解码其 cluster_id 得到 port——`port == 0` 说明该链路属于 rank 0 自己，直接 `self._unlink(cluster_id)`（内部调 `force_unlink`，[L375-L380](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L375-L380)）；否则把 cluster_id 经 IPC socket 转给对应 rank 的 `heartbeat_server_func`，由持有该链路的进程执行 unlink。这里 D 侧 cluster_id 的 port 字段恰好编码了 D 的 world rank（因为 D 侧 manager 以 host_port=0 构造，见 4.4.3），所以 `port == 0` 能作为"是否本 rank 链路"的判据；
3. 在 PUB socket 上发布 `prefill_hb:<host_cluster_id>`。

**源码阅读发现（供批判性阅读）**：`heartbeat_server_func`（[L419-L428](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L419-L428)）中 `if data and isinstance(data, int)` 的判断永远为假——`data` 来自 `recv_string()`，必然是 `str`。也就是说该分支当前实际不生效，非 rank 0 的 unlink 转发路径疑似失效。阅读开源代码时保留这类怀疑并交叉验证（比如对比 README 端点矩阵第 4 行的描述），是源码精读的重要习惯。

#### 4.2.4 代码实践

**实践：跟踪一个请求在 P 侧的块释放路径（源码阅读型）。**

1. 实践目标：弄清"P 侧一个请求的 KV 块到底何时释放"，并能在日志中找到证据。
2. 操作步骤：
   - 在已部署的 1P1D 服务上发送一个长 prompt 请求，等待其完成；
   - 在 P 机 `LOG_PATH` 下 `grep -n "out of date\|Freeing blocks\|send string" server_0.log`；
   - 对照源码 [L443-L485](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L443-L485)：`send string` 是 D 侧回执到达（正常路径），`out of date` 是 600 秒超时兜底路径。
3. 需要观察的现象：正常情况下请求完成数秒内即出现回执日志，块走正常释放；只有 D 异常时才会看到超时告警。
4. 预期结果：能指出该请求走的是哪条释放通道。若无真实环境，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：P 侧为什么不在 `request_finished` 里直接返回 `(False, None)`（立即释放块）？

**答案**：因为 D 侧拉 KV 是异步的——D 收到请求时可能还在跑别的 batch，等它真正执行 `pull_blocks` 时 P 侧请求早已结束。立即释放会让块被新请求复用，D 拉到脏数据。所以 P 必须延迟释放，直到收到 D 的回执或超时。

**练习 2**：`BLOCK_RELEASE_DELAY=600` 设得太小或太大分别有什么后果？

**答案**：太小——D 拉得慢一点（排队深、网络抖动）块就被提前释放，KV 传输失败、D 只能重算 prefill，浪费算力；太大——D 已死但块长期不释放，P 的 KV 池被死请求占满，吞吐下降。600 秒是"容忍慢 D + 不至于泄漏太久"的折中，且可通过环境变量 `BLOCK_RELEASE_DELAY` 调整（[L50](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L50)）。

### 4.3 Decode 侧：DecodeConnectorScheduler 与 DecodeConnectorWorker

#### 4.3.1 概念说明

D 节点是 KV 传输的**主动方**：它持有请求（带 P 写的 `kv_transfer_params`"取件码"），决定何时拉、拉哪些块、拉完怎么对齐。D 侧要解决三个问题：

1. **省算力**：`get_num_new_matched_tokens` 告诉调度器"这个请求有 N 个 token 的 KV 可以从 P 拿，不用重算"，效果类似一次全命中的前缀缓存；
2. **块号对齐**：P 与 D 的 KV 池独立分配，块号体系不同，传输前必须把"P 的块号 → D 的块号"一一配对；
3. **拉完通知**：数据进 D 的 HBM 后，要通知调度器（请求可以进 batch）和 P（块可以释放）。

#### 4.3.2 核心流程

D 侧一次拉 KV 的完整流水线（本讲的主线，4.4 会补上 llm_datadist 层细节）：

```text
DecodeConnectorScheduler.get_num_new_matched_tokens(req)
    count = len(prompt_token_ids) - num_computed_tokens
    返回 (count, True) → 调度器把请求标记为"等待外部 KV"
        │
DecodeConnectorScheduler.update_state_after_alloc(req, blocks)
    块已分配 → 存入 _reqs_need_recv[req_id]
        │
DecodeConnectorScheduler.build_connector_metadata()
    打包 DatadistConnectorMetadata（本地块号 + 远端块号 + P 地址）
    随 SchedulerOutput 发到 Worker 进程
        │
DecodeConnectorWorker.start_load_kv(metadata)
    对齐本地/远端块号（分组对齐、裁剪 lookahead 块）
    executor.submit(_read_blocks, ...)  ← 单线程线程池，异步
        │
LLMDataDistManager.pull_kv(...)      ← 进入 4.4
    get_real_remote_cluster_ids → (首次) register_link 建链
    _pull_blocks → data_dist_engine.cache_manager.pull_blocks
        │
DecodeConnectorWorker._read_blocks 尾声
    TP>1: torch.distributed.barrier(cpu_group) 同步各 rank
    rank0: _send_pulled_kv_req_list → ZMQ PUSH 回执 P
    self._recving_transfers.append(req_id)
        │
DecodeConnectorWorker.get_finished()
    返回 all_done_recving → 调度器把请求排入下一步 batch
```

#### 4.3.3 源码精读

**Scheduler：get_num_new_matched_tokens（[L551-L567](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L551-L567)）。**

```python
if request.request_id in self.processed_request:
    return 0, False          # 已登记过，不重复声明
params = request.kv_transfer_params
if params is None:
    return 0, False          # 本地请求（非 PD 转来），正常调度
count = max(len(request.prompt_token_ids) - num_computed_tokens, 0)
return count, count > 0
```

计算很简单：\( \text{count} = \max(L_{\text{prompt}} - n_{\text{computed}}, 0) \)。返回 `(count, True)` 意味着"请给这个请求预留 count 个 token 的外部 KV 空间，调度先挂起"。注意 L564 的约束：`num_computed_tokens` 必须是 `block_size` 的整数倍，否则直接抛错。

**Scheduler：update_state_after_alloc + build_connector_metadata（[L580-L629](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L580-L629)）。** 块分配完成后，把请求连同"未命中前缀缓存的块号"（`get_unhashed_block_ids`，按 KV cache group 分组保留边界，[L572-L578](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L572-L578)）与完整块号存进 `_reqs_need_recv`；下一步 `build_connector_metadata` 把它们逐个 `add_new_req` 进 metadata 并清空暂存。DCP 场景的 `async_pull_kv` 快路径（scheduler_output 为 None 时把 pickle 过的 metadata 从 IPC PUB 发出，[L622-L628](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L622-L628)）是可选加速，默认关闭。

**Scheduler：request_finished 的中止清理（[L631-L641](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L631-L641)）。** 客户端断连（`FINISHED_ABORTED`）时，D 主动给 P 发一条回执，让 P 立刻释放该请求的块——不等 600 秒超时。这是"取消请求"场景下的资源及时回收。

**Worker：start_load_kv 的块号对齐（[L762-L856](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L762-L856)）。** 这是 D 侧最精细的一段，先做一系列校验（块号必须双层嵌套 `list[list[int]]`、本地/远端组数必须相等），然后逐组对齐：

- 远端块 **少于** 本地块：按远端裁剪本地（角标注释 `corner case, reconsider it later`，只拉得到的部分）；
- 远端块 **多于** 本地块：若恰多 1 块，判定为 P 侧的 lookahead 块，修剪掉；再从尾部对齐；
- 相等：直接配对。

对齐后 flatten 成两个平行列表，交线程池执行：

```python
future = self.executor.submit(
    self._read_blocks,
    local_block_ids=meta.local_block_ids,
    remote_block_ids=meta.remote_block_ids,
    dst_cluster_id=cluster_ids[0],
    ...
)
```

注意 L691-L692：`max_concurrents = 1`，**单线程拉取**（TODO 注释表明多线程/多 rank 并行拉取尚未支持），且每个 future 挂了 `handle_exception` 回调（[L950-L953](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L950-L953)），异常不会静默吞掉。若启用了 DCP（`get_dcp_group().world_size > 1`），还会构造 `TP_Convertor` 对远端块号做跨 TP 重排（[L776-L787](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L776-L787)，实现见 [connector/utils.py:L13-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/utils.py#L13-L60)），拉取完成后用 `all_to_all_single` 在 D 的各 TP rank 间重排 token。

**Worker：_read_blocks 收尾（[L877-L910](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L877-L910)）。** `pull_kv` 返回（数据已进 HBM）后：

```python
if self.vllm_config.parallel_config.tensor_parallel_size == 1:
    self._send_pulled_kv_req_list(remote_host_ip, [remote_request_id])
else:
    torch.distributed.barrier(group=get_tp_group().cpu_group)  # 等 TP 组所有 rank 拉完
    if get_tensor_model_parallel_rank() == 0:
        self._send_pulled_kv_req_list(remote_host_ip, [remote_request_id])
```

barrier 保证 TP 组内**所有** rank 都拉完自己的分片后，才由 rank 0 给 P 发一条回执（P 侧只有 rank 0 听）——避免 P 提前释放导致其他 rank 拉取失败。`_send_pulled_kv_req_list`（[L913-L928](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L913-L928)）用惰性建立的 `zmq.PUSH` socket 连到 `remote_host_ip`（就是 P 侧 request_finished 里写的 `tcp://ip:5568`）。日志 `***** read block, req_id:..., cost:...` 可直接用于测量单请求 KV 传输耗时。

**Worker：get_finished（[L930-L947](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L930-L947)）。** 把 `_recving_transfers` 列表整体搬进返回集合并清空，即"自上次询问以来拉完的请求"。调度器收到后把这些请求排入下一个 batch 的正常 decode。

#### 4.3.4 代码实践

**实践：跑通 decode 生命周期单测并解读断言（可在 CPU 环境运行）。**

1. 实践目标：用单测固化 D 侧调度钩子的调用顺序，理解"get_num → update_state → build_meta → (模拟 recving) → 正常调度"这条链。
2. 操作步骤：
   ```bash
   cd components/omni-npu
   pytest "tests/unit/connector/test_llmdatadist_connector_v1.py::TestLLMDataDistConnectorV1LifeCycle::test_decode_schedule" -v
   ```
   测试代码见 [test_llmdatadist_connector_v1.py:L73-L109](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/test_llmdatadist_connector_v1.py#L73-L109)。它用 `patch.object(connector, ..., wraps=...)` 包装四个钩子记录调用，用 `create_vllm_config(kv_role="kv_consumer")` 构造纯 CPU/mock 配置（[tests/unit/connector/utils.py:L29-L69](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/connector/utils.py#L29-L69)），再用 `create_model_runner_output(finished_recving={req_id})` 模拟"KV 已拉完"。
3. 需要观察的现象：第一次 `scheduler.schedule()` 后 `kv_connector_metadata is not None`（断言 L104）；喂入 finished_recving 后第二次 schedule 时请求出现在 `num_scheduled_tokens`（断言 L108-L109）。
4. 预期结果：用例通过，且你能对照 4.3.2 的流程图说出每个 mock 断言对应流水线的哪一步。若本机缺依赖，请在部署容器内执行或标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：D 侧 `get_num_new_matched_tokens` 返回 `count > 0` 后，vLLM 调度器会对这个请求做什么？什么时候恢复？

**答案**：调度器为请求预留 `count` 个 token 的空间并把请求挂起（不排入计算 batch），等待 Worker 拉完远端 KV；`get_finished` 返回的 `all_done_recving` 集合包含该请求后，调度器才把它排入正常 decode batch。单测 L105-L109 正是模拟了这一恢复过程。

**练习 2**：为什么 `_read_blocks` 里 TP>1 时要 `barrier`，而 TP=1 不用？

**答案**：TP 组内每个 rank 只持有 KV 的一部分（按 head/层切分），只有**所有** rank 都拉完，这个请求的 KV 才完整。P 侧收到一条回执就会释放块，所以必须先 barrier 等 TP 组齐了、再由 rank 0 发唯一的回执。TP=1 时 rank 0 自己拉完即完整，无需同步。

**练习 3**：请求在 D 侧排队很久才被拉取，P 侧会发生什么？

**答案**：P 侧块一直处于延迟释放状态。若在 `BLOCK_RELEASE_DELAY`（600 秒）内 D 来拉，正常传输；超时则 P 强制释放块并打 `out of date` 告警，D 拉取会失败并触发链路重建（见 4.4 的 `pull_kv` 失败重试），最坏情况该请求需重算 prefill。

### 4.4 llm_datadist 会话管理：LLMDataDistManager 与容错

#### 4.4.1 概念说明

四个协作类都不直接碰 `llm_datadist` 库，而是经由 `LLMDataDistManager` 这个会话管理器。它负责：

- **身份管理**：计算本进程的 `cluster_id` 与整个 P 实例的 `host_cluster_id`；
- **建链/断链**：`register_link` / `close_link` / `force_unlink`；
- **内存注册**：`register_memory` 把 KV 池张量按形状分组注册为可传输 cache；
- **拉取与自愈**：`pull_kv` 失败时自动重建链路重试。

理解本模块的钥匙是 `registered_link_infos` 这张查找表：`{(host_cluster_id, prefill_dp_rank, d_rank): [prompt_cluster_id_list]}`——D 侧每个 decode 进程记录"我跟哪个 P 实例建过链、链向哪些 prompt cluster"。

#### 4.4.2 核心流程

D 侧第一次拉某个 P 的 KV 时，链路是**惰性建立**的（不在启动时全量建链）：

```text
start_load_kv → datadist_manager.pull_kv(...)  [manager L427]
    │ 若 registered_link_infos 查不到该 P
    ▼
get_real_remote_cluster_ids(meta)  [manager L240]
    查表 miss → register_link(host_cluster_id, dp_rank, d_rank)  [manager L304]
        _get_cluster_id_list: 用 get_p_start_rank 算出本 d_rank 应连的 P rank 列表
        link_clusters(clusters, timeout=5000ms)
        成功 → 写入 registered_link_infos
    ▼
_pull_blocks(src_cache_key, dst_cache, src_blocks, dst_blocks)  [manager L364]
    data_dist_engine.cache_manager.pull_blocks(...)
    失败且错误码 ∈ RETRYABLE_CODES → 等 1s 重试(共 1 次)
    仍失败 → pull_kv 里触发 _refresh_link：close_link + register_link 后再拉一次
```

容错有三层：**重试**（可重试错误码）、**重建链路**（unlink+link 再拉）、**心跳清尸**（双向心跳超时主动 unlink，见 4.2/4.3 的两个 `heartbeat_timer_func`）。

#### 4.4.3 源码精读

**身份：LLMDataDistConfig。** 见 [llmdatadist_manager_v1.py:L75-L117](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L75-L117)。两个关键标识：

- `cluster_id`（L97-L101）：本进程的传输身份。P 侧用 `local_rank` 作端口偏移、D 侧用全局 `rank`，且 **D 侧 manager 以 host_port=0 构造**（[connector L662](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L662)），所以 D 的 cluster_id 中 port 字段就等于其 world rank——这正是 4.2 心跳超时处理里 `port == 0` 判断的依据；
- `host_cluster_id`（L110-L117）：`(timestamp_ms, ip1_int, ip2_int, ...)` 元组，代表**整个 P 实例**（可能多机），时间戳保证每次重启后身份不同——D 发现元组变了就知道 P 重启过、旧链路作废。

cluster_id 的位布局（`ip_port_to_int`，[L708-L729](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L708-L729)）：

\[ \text{cluster\_id} = (\text{ip}_{32} \ll 32) \,|\, (\text{port}_{16} \ll 16) \,|\, ((\text{tp}-1)_8 \ll 8) \,|\, (\text{pp}-1)_8 \]

`cluster_id_to_ip_port`（[L565-L578](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L565-L578)）做逆向解码，二者构成可逆编码对。

**引擎初始化：_init_llm_data_dist（[L268-L291](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L268-L291)）。** 创建 `LLMDataDist(role, cluster_id)` 并 `init(options)`。P 侧额外设置 `listen_ip_info = f"{ip}:{15567 + local_rank}"`——**P 是服务端**，在 RoCE 端口上监听等 D 来连（对应 u1 讲过的 15567 端口段）。角色字符串到枚举的映射 `"kv_producer" → LLMRole.PROMPT`、`"kv_consumer" → LLMRole.DECODER` 见 [L39-L42](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L39-L42)。

**建链：register_link（[L304-L322](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L304-L322)）。** 核心是 `_get_cluster_id_list`（[L491-L513](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L491-L513)）：对每个 d_rank，用 [connector/utils.py:L182-L237](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/utils.py#L182-L237) 的 `get_p_start_rank` 计算它应该连接的起始 P rank，再按 `p_rank // NUM_DIE_PER_MACH`（每机 die 数，默认 16）换算出目标 ip:port——这就是"D 的每个进程均衡挂到 P 的不同进程"的负载均分逻辑。之后组装 `LLMClusterInfo` 列表调 `link_clusters(clusters, timeout=5000)`。

**拉取与自愈：pull_kv（[L427-L454](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L427-L454)）。**

```python
ret = self._pull_blocks(prompt_cache_key, kv_cache, src_blocks, tgt_blocks)
if not ret:
    self._refresh_link(prompt_cluster_id, prefill_dp_rank, self.rank)
    ret_updated = self._pull_blocks(prompt_cache_key, kv_cache, src_blocks, tgt_blocks)
    if not ret_updated:
        raise RuntimeError(f"Failed to pull kv even if rebuild the kv link!")
```

`_pull_blocks`（[L364-L393](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L364-L393)）内层还有错误码白名单重试：`LLM_TIMEOUT`、`LLM_LINK_BUSY`、`LLM_DEVICE_OUT_OF_MEMORY` 等可重试码（[L62-L70](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L62-L70)）等 1 秒再试。PP>1 时 `pull_kv` 会按远端 pp_partition 把 KV 池切成多段 cache，对每段单独构造 `BlocksCacheKey(prompt_cluster_id, model_id)` 拉取（[L395-L445](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L395-L445)）。

**force_unlink 与 close_link。** `force_unlink`（[L356-L362](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L356-L362)）只填 `remote_cluster_id` 一个字段就调 `unlink_clusters(force=True)`——单方面强制断链，用于心跳判死场景（对端可能已经不在了，无法协商）。`close_link`（[L325-L346](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L325-L346)）则是完整的"算出 cluster 列表 → unlink → 清 registered_link_infos"。P 侧与 D 侧各有心跳线程（P：[connector L382-L417](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L382-L417)；D：[connector L696-L743](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L696-L743)）：D 用 `SUB` socket 非阻塞收 P 的广播，60 秒收不到就 `close_link` 并清掉指向该 IP 的所有 ZMQ socket——两侧都会在对方失联时清尸。

**内存注册：register_memory（[L525-L555](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L525-L555)）。** `unzip_kv_cache_dict`（[L581-L653](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L581-L653)）把 `{层名: 张量}` 的 KV 池按 **shape 去重分组**（同形状张量归为一组 model），混合注意力场景还能从非连续 strided view 还原出底层连续内存池；`maybe_split_kv_caches_for_spec_layers`（[L688-L705](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L688-L705)）再把形状不一的 MTP/spec 层拆开。每组用张量的 `data_ptr()` 列表调 `register_blocks_cache`——**注册的是地址，不是拷贝**，所以 RoCE 传输是直接读写 KV 池显存的零拷贝。P 侧注册时带 `cache_key`（可被远端寻址），D 侧传 `None`。

#### 4.4.4 代码实践

**实践：cluster_id 编码往返实验（部署容器内运行）。**

1. 实践目标：验证 `ip_port_to_int` / `cluster_id_to_ip_port` 的可逆性，直观理解 cluster_id 位布局与"D 的 port 字段 = world rank"。
2. 操作步骤（示例代码，非项目原有代码）：

   ```bash
   docker exec -it <P或D容器名> python -c "
   from omni_npu.connector.llmdatadist_manager_v1 import ip_port_to_int, cluster_id_to_ip_port
   cid = ip_port_to_int('192.168.1.10:15567', 16, 1)
   print(f'{cid:#x}')                 # 观察各字段占据的比特段
   print(cluster_id_to_ip_port(cid))  # 期望还原 ('192.168.1.10:15567', 16, 1, 0)
   # 模拟 D 侧 rank=3 的 cluster_id（host_port=0 + rank）
   print(cluster_id_to_ip_port(ip_port_to_int('192.168.1.20:3', 16, 1)))
   "
   ```
3. 需要观察的现象：十六进制输出从高位到低位依次是 IP、port、tp/pp 字段；两次解码都还原出原始输入。
4. 预期结果：与 4.4.3 的位布局公式逐段对上。若容器内无法执行，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `host_cluster_id` 要带时间戳，而 `cluster_id` 不用？

**答案**：`cluster_id` 标识"单个传输进程"，进程生死与端口绑定，重启后同 ip:port 即视为同一个；`host_cluster_id` 标识"整个 P 实例"，D 侧靠它索引链路表。P 重启后旧的 KV 与链路全部作废，加时间戳让新实例拿到不同身份，D 侧 `get_real_remote_cluster_ids` 发现旧 key 查不到、且能按 IP 匹配到残留旧链路并主动 `close_link`（[manager L248-L264](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L248-L264)），避免把数据拉向一个"同名但内容已换"的实例。

**练习 2**：`pull_kv` 失败后的自愈分几层？每层对付什么故障？

**答案**：三层。① 错误码重试（`_pull_blocks` 内，对付瞬时超时/链路忙）；② 重建链路（`_refresh_link`：close_link + register_link 后重拉一次，对付链路半死/对端重启）；③ 心跳清尸（双向 60 秒超时，对付对端整机宕机——没人会等你重试了，先把资源清干净）。三层都失败才向上抛 `RuntimeError`。

**练习 3**：`register_memory` 为什么要按 shape 给 KV 张量分组注册，而不是一层一个 cache？

**答案**：llm_datadist 的 `CacheDesc` 描述的是"一组同形状张量"，同形状的层共用一套块号空间即可整体寻址；分组还能兼容混合注意力（SWA/DSA/MLA 层形状不同）与 MTP spec 层（形状与主层不同）共存的模型——`maybe_split_kv_caches_for_spec_layers` 保证每组内形状严格一致，否则 `CacheDesc(shape=...)` 无法统一描述。

## 5. 综合实践

**任务：画出一次完整 KV 传输的时序图（本讲核心实践）。**

以「客户端发一个 8192 token 的 prompt，P 完成预填充后 D 接力 decode」为剧本，画一张时序图，参与方六列：`客户端 / vLLM-Scheduler(P) / PrefillConnectorWorker / [ZMQ+RoCE 网络] / DecodeConnectorWorker / vLLM-Scheduler(D)`，要求：

1. **覆盖以下 12 个关键步骤，每步标注类名 + 方法名 + 源码行号**：
   - P scheduler `request_finished` 打包取件码并延迟释放块（[connector L303-L329](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L303-L329)）；
   - 取件码随请求到达 D，`request.kv_transfer_params` 就位；
   - D scheduler `get_num_new_matched_tokens` 声明 8192 个外部 token（[L551-L567](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L551-L567)）；
   - D scheduler `update_state_after_alloc` 登记 `_reqs_need_recv`（[L580-L599](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L580-L599)）；
   - D scheduler `build_connector_metadata` 发 metadata 到 worker 进程（[L601-L629](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L601-L629)）；
   - D worker `start_load_kv` 块号对齐并提交线程池（[L762-L875](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L762-L875)）；
   - （首次）`get_real_remote_cluster_ids` miss → `register_link` 建链（[manager L240-L266](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L240-L266)、[L304-L322](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L304-L322)）；
   - `pull_kv` → `_pull_blocks` → llm_datadist `pull_blocks`（RoCE 写 D 的 HBM）（[manager L427-L454](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L427-L454)、[L364-L393](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_manager_v1.py#L364-L393)）；
   - D worker `_read_blocks` 尾声：barrier + rank0 发 ZMQ 回执（[connector L877-L910](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L877-L910)）；
   - P worker `get_pulled_kv_req_list` 收到回执（[L487-L509](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L487-L509)）；
   - D worker `get_finished` 报 done_recving，D scheduler 恢复调度（[L930-L947](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L930-L947)）；
   - P worker `get_finished` 释放块（[L443-L485](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L443-L485)）。
2. **用虚线标出两条旁路**：P→D 的 5 秒心跳广播（PUB/SUB）与 D→P 的 5 秒心跳（PUSH/PULL 复用通道），注明 60 秒超时触发的 `force_unlink`/`close_link`。
3. **验证**：把图与 [components/omni-npu/README.md:L68-L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L68-L83) 的端点矩阵逐行对照——图上每条网络箭头都应能在矩阵中找到对应的行（ZMQ 5568、心跳 15566、RoCE 15567）。若有环境，再对照 D 机日志中 `start_load_kv for request ... Num local_block_ids` 与 `read block, req_id:..., cost:...` 两条日志确认时间顺序（待本地验证）。

完成后你应该拥有一张"一眼看懂 PD KV 传输"的图，它也是下一讲（u4-l3 通信矩阵）的预习材料。

## 6. 本讲小结

- `LLMDataDistConnector` 是门面类，按 `role`（SCHEDULER/WORKER）× `kv_role`（producer/consumer）把职责分派给 **PrefillConnectorScheduler / PrefillConnectorWorker / DecodeConnectorScheduler / DecodeConnectorWorker** 四个协作类；connector 名字经 `register.py` 注册进 vLLM 工厂，由 `NPUPlatform.import_kernels` 触发，且支持 `omni.kv_connectors` entry point 扩展。
- **P 侧的核心是延迟释放**：`request_finished` 打包"取件码"（块号 + ZMQ 地址 + host_cluster_id）随请求发给 D，块等 D 回执或 600 秒超时才释放。
- **D 侧是传输主动方**：`get_num_new_matched_tokens` 声明可省算的外部 token 数 → scheduler 登记、打包 metadata → worker `start_load_kv` 做块号对齐 → 单线程池异步 `pull_blocks` → barrier 后 rank0 发 ZMQ 回执 → `get_finished` 恢复调度。
- **llm_datadist 会话由 LLMDataDistManager 管理**：cluster_id 是打包 ip/port/tp/pp 的 int64"地址牌"；链路**惰性建立**（首次拉取才 `register_link`），P 侧在 15567+local_rank 监听。
- **容错三层递进**：可重试错误码重试 → `pull_kv` 失败重建链路（close+register 再拉）→ 双向 5 秒心跳、60 秒超时 `force_unlink` 清尸；`host_cluster_id` 带时间戳以识别 P 重启。
- 控制面（ZMQ：回执 5568、心跳 15566、本机 IPC）与数据面（RoCE 15567）分离，端点矩阵见 omni-npu README。

## 7. 下一步学习建议

- **下一讲 u4-l3（通信矩阵）**：把本讲出现的所有端口（5568 / 15566 / 15567 / IPC 路径）落到部署运维层面——学会用 `ss -tlnp` 在真实环境验证链路建立，与 README 端点矩阵逐行对照。
- **u4-l4（pd_run.sh）**：本讲的 `DECODE_POD_NUM`、`VLLM_LLMDATADIST_*` 环境变量都由部署脚本注入，去脚本层看它们的来龙去脉。
- **u7-l2（OmniCacheConnector 源码结构）**：omni-cache 用同一套 `KVConnectorBase_V1` 接口把 KV 先卸载到主机内存再传输，对比两个 connector 对同一接口的不同实现，能加深对"接口与实现分离"的理解。
- **延伸阅读**：vLLM 包内 `vllm/distributed/kv_transfer/kv_connector/v1/base.py`（接口契约的真身）与 `tests/unit/connector/` 下的其余单测（`test_remote_prefill_lifecycle.patch`、`test_remote_decode_lifecycle.patch`）。
