# u5-l1 DeviceManager:多硬件后端的统一抽象

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `DeviceManager` 的两个核心实例字段(`device_type` 与 `device_module`)以及设备探测的优先级顺序(npu → xpu → cuda)。
2. 默写「设备类型 → 通信后端(nccl/hccl/xccl)」与「设备类型 → 传输协议(ascend_direct/rdma/efa)」两张映射表,并指出它们分别在 `ps.py` 和 `p2p_store.py` 的哪个调用点被消费。
3. 区分三个能力开关 `supports_inplace_pin` / `supports_device_ipc` / `supports_device_p2p` 的语义,以及它们被违反时「软降级」与「硬失败」两种截然不同的处理哲学。
4. 解释 `ipc_collect` 与 `host_empty_cache` 为什么在不同后端有不同的实现(有的调设备 API、有的是 no-op、有的退化为 `gc.collect`)。
5. 学会用 `DeviceManager.__new__` + `SimpleNamespace` 的替身技巧,在纯 CPU 机器上测试一个「硬件相关」的类。

## 2. 前置知识

本讲是专家层第一讲,默认你已读完 u3-l1(ParameterServer 初始化)。这里补充几个新概念:

- **为什么需要设备抽象**:checkpoint-engine 的同一套代码要跑在 NVIDIA GPU(CUDA)、华为昇腾 NPU、Intel XPU 三种硬件上。这三种硬件的 PyTorch 接口高度同构——`torch.cuda`、`torch_npu.npu`、`torch.xpu` 都提供 `set_device()`、`device_count()`、`get_device_properties()`、`mem_get_info()`、`empty_cache()` 等同名方法。DeviceManager 的做法不是写三套代码,而是**探测一次设备类型,之后把对应的模块对象存下来,让鸭子类型接管一切**。
- **集合通信后端**:`dist.init_process_group(backend=...)` 需要一个后端字符串。NVIDIA 用 **NCCL**,昇腾用 **HCCL**,Intel 用 **XCCL**(即 oneAPI CCL)。它们在 PyTorch 里都叫「某个名字的通信库」,功能等价。
- **RDMA 与传输协议**:P2P 更新依赖 mooncake-transfer-engine 做 RDMA(远程直接内存访问)传输。初始化 TransferEngine 时要指定协议名:普通网卡用 `rdma`,AWS 自研网卡 EFA(Elastic Fabric Adapter,vendor ID `0x1d0f`)用 `efa`,昇腾走自己的 `ascend_direct` 直连协议。
- **能力开关(capability switch)模式**:与其在使用处写 `if device_type == "cuda": ...`,不如在抽象层提供 `supports_xxx() -> bool`,使用处只「问」不「查」。这样新增一种硬件只需改 DeviceManager 一处。
- **测试替身技巧**:本讲的测试全部能在纯 CPU 的 CI 上跑(`pytest -m "not gpu"`),办法是用 `DeviceManager.__new__(DeviceManager)` 跳过 `__init__` 的真实硬件探测,直接给 `device_type` / `device_module` 两个字段塞假值(`SimpleNamespace`)。这个技巧对任何「构造函数碰硬件」的类都通用。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | 本讲主角。`DeviceManager` 类(L199-L312)+ 设备探测辅助函数 + RDMA 网卡发现函数(`_get_rdma_devices` 等留给 u5-l5 精读) |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | 消费方。`ParameterServer.__init__` 创建 DeviceManager;注册、更新、注销路径上的各能力开关检查点 |
| [checkpoint_engine/p2p_store.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py) | 消费方。`P2PStore.__init__` 用 `rdma_device()` 和 `transfer_engine_protocol` 初始化 mooncake 引擎 |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | worker 进程自己也建一个 DeviceManager,用于 `synchronize` / `ipc_collect` / `empty_cache` |
| [checkpoint_engine/ipc_handler.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py) | `build_ipc_handler` 按 `device_type` 在 Torch/XPU 两种 IPC 实现间选择(u4-l3/u4-l4 已讲) |
| [checkpoint_engine/xpu_ipc/__init__.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py) | `is_available()` 被 `supports_device_ipc` 在 XPU 上委托调用 |
| [tests/test_device_manager.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py) | 全部用替身mock,纯 CPU 可跑;末尾 3 个测试带 `@pytest.mark.gpu` 门控,需真实 Intel GPU |

## 4. 核心概念与源码讲解

### 4.1 DeviceManager 类骨架与设备类型探测

#### 4.1.1 概念说明

`DeviceManager` 是整个项目**唯一的硬件入口**(u3-l1 已建立这个认知,这里下潜到实现)。它的全部状态只有两个字段:

- `device_type: str`——`"cuda"` / `"npu"` / `"xpu"` 三选一。注意这个字符串本身就是合法的 torch 设备名,所以 `ps.py` 里可以直接写 `torch.empty(..., device=self.device_manager.device_type)`。
- `device_module`——对应的模块对象(`torch.cuda` / `torch_npu.npu` / `torch.xpu`)。下游所有 `set_device` / `device_count` / `mem_get_info` / `synchronize` 调用都打到它身上。

构造函数只做两件事:探测类型、装载模块。探测顺序是 **npu 优先于 xpu 优先于 cuda**——在一台同时装了多种框架的机器上,昇腾/Intel 的专用包存在就认为是专用机;只有两者都不可用才回落 CUDA;全都不可用直接 `TypeError`。

#### 4.1.2 核心流程

```text
DeviceManager()
  ├─ _detect_device_type()
  │    ├─ _is_torch_npu_available()  ── 是 ──> "npu"
  │    ├─ _is_torch_xpu_available()  ── 是 ──> "xpu"
  │    ├─ torch.cuda.is_available()  ── 是 ──> "cuda"
  │    └─ 否则 raise TypeError("The current device type is not supported")
  └─ _setup_device_module()
       ├─ "npu"  ──> import torch_npu; device_module = torch_npu.npu
       ├─ "xpu"  ──> device_module = torch.xpu
       └─ "cuda" ──> device_module = torch.cuda
```

两个探测器的防御等级不同:npu 探测只捕获 `ImportError`(torch 没编译 npu 支持时 `torch.npu` 属性根本不存在);xpu 探测捕获**所有** `Exception`——因为 `torch.xpu.is_available()` 在驱动初始化失败时可能抛出非导入类异常。

#### 4.1.3 源码精读

类骨架与探测流程在 [checkpoint_engine/device_utils.py:L199-L242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L199-L242):`__init__` 先探测后装载,顺序不可颠倒。

[checkpoint_engine/device_utils.py:L222-L230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L222-L230) 是三级探测主体——注意 npu/xpu 分支调的是各自的封装方法而非直接 `torch.npu.is_available()`。

[checkpoint_engine/device_utils.py:L204-L220](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L204-L220) 是两个探测器:npu 用 `hasattr(torch, "npu") and callable(...)` 先探属性再调用、只兜 `ImportError`;xpu 直接 `except Exception` 全兜。

[checkpoint_engine/device_utils.py:L232-L242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L232-L242) 装载设备模块——`import torch_npu` 写在分支里是**延迟导入**,保证没装昇腾包的机器 import 本模块不报错(这是全项目隔离可选依赖的惯例,u1-l3 提过)。

消费侧,[checkpoint_engine/ps.py:L200-L202](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L200-L202) 里 `ParameterServer.__init__` 第一件正事就是创建 DeviceManager,并用 `device_module.device_count()` 推出本机卡数;worker 进程同理,见 [checkpoint_engine/worker.py:L62-L70](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L62-L70)(REP 循环开始前创建)。

`device_type` 除了查表,还直接当 torch 设备字符串用,例如 [checkpoint_engine/ps.py:L816](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L816) 分配桶缓冲、[checkpoint_engine/ps.py:L851-L852](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L851-L852) 创建 `ret_code` 张量,都是 `device=self.device_manager.device_type`。

#### 4.1.4 代码实践

1. **实践目标**:亲手触发一次探测失败,并掌握绕过硬件探测的替身构造法。
2. **操作步骤**:
   - 在纯 CPU 机器上(本教程 CI 环境即是)执行:
     ```bash
     python -c "from checkpoint_engine.device_utils import DeviceManager; DeviceManager()"
     ```
   - 再执行下面这段「示例代码」(模仿测试里的 `_make_manager` 写法):
     ```python
     # 示例代码:跳过 __init__,直接伪造字段
     from types import SimpleNamespace
     from checkpoint_engine.device_utils import DeviceManager

     dm = DeviceManager.__new__(DeviceManager)   # 不跑 __init__,不碰硬件
     dm.device_type = "cuda"
     dm.device_module = SimpleNamespace(device_count=lambda: 8)
     print(dm.device_type, dm.device_module.device_count())
     ```
3. **需要观察的现象**:第一条命令应当抛 `TypeError: The current device type is not supported`;第二条应当打印 `cuda 8`。
4. **预期结果**:探测失败时构造即报错(而不是留一个半初始化对象);替身法可以完全离线地驱动 DeviceManager 的所有查表逻辑。
5. 实际输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么探测顺序是 npu → xpu → cuda,而不是 cuda 优先?

**答案**:CUDA 是最通用的回落项。torch 的 CPU 版/通用发行版几乎总是编了 cuda 支持,若 cuda 优先,一台装了 `torch_npu` 的昇腾机器只要同时可见 CUDA(例如配了 NVIDIA 管理库)就会被误判;而 `torch_npu`、intel 扩展是「专门装了才有」的强信号。专用优先、通用兜底,误判面最小。

**练习 2**:`_is_torch_xpu_available` 为什么捕获 `Exception` 而 `_is_torch_npu_available` 只捕获 `ImportError`?

**答案**:见 [checkpoint_engine/device_utils.py:L204-L220](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L204-L220)。npu 探测先用 `hasattr` 挡住了「属性不存在」的情形,剩下唯一可能的异常就是导入问题;而 `torch.xpu.is_available()` 会真的去初始化 XPU 运行时,驱动找不到设备时可能抛各种运行时异常,必须全兜,否则「没插 XPU 卡」会变成崩溃而不是「不可用」。

**练习 3**:新增一种硬件(比如 MLP)需要改 `_detect_device_type` 和 `_setup_device_module` 里的哪些位置?

**答案**:两处 `if/elif` 链各加一个分支:`_detect_device_type` 加探测方法,`_setup_device_module` 加模块装载。同时还要同步更新 `backend`、`transfer_engine_protocol` 等属性的分发链(下一节),这也是这种「串行 if/elif 分发」风格的代价——每加一种硬件要touch类里所有分发点。

### 4.2 通信后端与传输协议:backend / transfer_engine_protocol / rdma_device

#### 4.2.1 概念说明

探测出设备类型后,DeviceManager 用两个 `@property` 把它翻译成两套「协议名」:

- **`backend`**:集合通信后端,决定 `dist.init_process_group` 用哪个通信库。这是**控制面/广播数据面**的底层。
- **`transfer_engine_protocol`**:P2P 路径上 mooncake TransferEngine 的传输协议名。这是 **RDMA 数据面**的底层。

两者的映射关系是本讲必须记住的表:

| device_type | backend | transfer_engine_protocol | 依据 |
| --- | --- | --- | --- |
| `npu` | `hccl` | `ascend_direct` | 昇腾专用的通信库与直连协议 |
| `xpu` | `xccl` | `rdma` 或 `efa` | 由 `has_efa_pci()` 探测决定 |
| `cuda` | `nccl` | `rdma` 或 `efa` | 同上 |

`efa` 与 `rdma` 的区分靠 `has_efa_pci()`:扫描 `/sys/class/infiniband/` 下每张网卡的 PCI vendor ID,发现 Amazon 的 `0x1d0f` 即判定为 EFA 主机。第三个方法 `rdma_device(rank)` 把协议再翻译成**具体网卡名**:昇腾直连协议不经过用户态网卡枚举,返回空串;`rdma`/`efa` 则把本机 rank 均分到可用 RDMA 网卡上(均分算法的细节是 u5-l5 的主题,本讲只看接口)。

#### 4.2.2 核心流程

```text
device_type ──property backend──────────> "nccl"/"hccl"/"xccl"
                │
                └─> 消费点: ps.py _init_process_group
                     dist.init_process_group(backend=...)

device_type ──property transfer_engine_protocol──> "ascend_direct"/"rdma"/"efa"
                │        └─ cuda/xpu 时先问 has_efa_pci()
                └─> 消费点: P2PStore.__init__
                     engine.initialize(ip, "P2PHANDSHAKE", protocol, rdma_device)
                     └─ rdma_device(rank) ──> 网卡名字符串(可逗号分隔多卡)
```

#### 4.2.3 源码精读

[checkpoint_engine/device_utils.py:L244-L253](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L244-L253) 是 `backend` 属性:三路查表,未知类型 `TypeError`——这是全类统一的「响亮失败」风格。

[checkpoint_engine/device_utils.py:L255-L265](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L255-L265) 是 `transfer_engine_protocol`:npu 直达 `ascend_direct`;cuda/xpu 先过 `has_efa_pci()` 再二选一。

[checkpoint_engine/device_utils.py:L183-L196](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L183-L196) 是 EFA 探测:逐个读 `/sys/class/infiniband/<设备>/device/vendor`,匹配 Amazon vendor ID `0x1d0f`,读不到就跳过该设备(容忍假/sysfs 节点)。

[checkpoint_engine/device_utils.py:L267-L273](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L267-L273) 是 `rdma_device`:注意它以 `transfer_engine_protocol` 而不是 `device_type` 为分发依据——协议是「要不要网卡」的真正决定者;网卡列表来自 `_get_rdma_devices()`(优先 `PS_P2P_STORE_RDMA_DEVICES` 环境变量,否则解析 `NCCL_IB_HCA`,u5-l5 展开)。

`backend` 的消费点在 [checkpoint_engine/ps.py:L539-L547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L539-L547):每次(重)建进程组时 `dist.init_process_group(backend=self.device_manager.backend, ...)`——u3-l4 讲过进程组按轮建毁,但后端字符串永远来自这一处查表。

两个协议的消费点在 [checkpoint_engine/p2p_store.py:L12-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/p2p_store.py#L12-L30):先用 `rdma_device(local_rank)` 选网卡,再把 `transfer_engine_protocol` 传给 `engine.initialize()`;初始化失败带随机退避重试 8 次(细节 u5-l5)。

#### 4.2.4 代码实践

1. **实践目标**:在纯 CPU 环境验证协议映射表,特别是 `has_efa_pci` 对 cuda/xpu 的二分行为。
2. **操作步骤**:运行测试文件里的协议用例(它们 mock 了 `has_efa_pci`,不碰硬件):
   ```bash
   pytest tests/test_device_manager.py -v -m "not gpu" -k "transfer_engine_protocol or backend"
   ```
   再阅读对应源码 [tests/test_device_manager.py:L24-L30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L24-L30) 与 [tests/test_device_manager.py:L39-L50](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L39-L50)。
3. **需要观察的现象**:`test_backend_mapping` 三组参数化用例(cuda→nccl、npu→hccl、xpu→xccl)通过;`test_transfer_engine_protocol_rdma` 在 `has_efa_pci` 分别 patch 为 False/True 时断言协议为 `rdma`/`efa`;`test_backend_unsupported` 断言假类型 `tpu` 触发 `TypeError`。
4. **预期结果**:全部通过(3 + 2×2 + 1 个用例)。这与 4.2.1 的映射表逐项对应。实际运行**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `rdma_device` 在 `ascend_direct` 协议下返回空字符串而不是去枚举网卡?

**答案**:见 [checkpoint_engine/device_utils.py:L267-L269](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L267-L269)。`ascend_direct` 是昇腾平台自带的直连传输协议,由 HCCL/硬件拓扑自行选路,不需要(也没有)用户态 ibverbs 网卡概念;mooncake 初始化时该参数传空即可。

**练习 2**:EFA 探测为什么读 sysfs 而不是用 ibverbs 查?

**答案**:ibverbs 的设备名/属性里并不直接暴露「这是 AWS EFA」;而 PCI vendor ID 是硬件身份的权威来源,`/sys/class/infiniband/<dev>/device/vendor` 把它暴露为纯文本。读 sysfs 零依赖(不需要加载 libibverbs),且 [checkpoint_engine/device_utils.py:L183-L196](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L183-L196) 对读失败的节点做了容忍。

**练习 3**:如果一台 CUDA 机器同时插了 Mellanox RDMA 卡和 EFA 卡,`transfer_engine_protocol` 会返回什么?

**答案**:返回 `efa`。`has_efa_pci` 只要发现**任一**设备 vendor 为 `0x1d0f` 就返回 True([L183-L196](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L183-L196) 循环里命中即 `return True`),协议随之定为 `efa`。至于实际用哪张卡,由后续 `_get_rdma_devices()` 的网卡清单决定(可用 `NCCL_IB_HCA` 排除,见 u5-l5)。

### 4.3 能力开关三部曲:supports_inplace_pin / supports_device_ipc / supports_device_p2p

#### 4.3.1 概念说明

三个 `supports_*` 方法回答三个独立的问题:「这块硬件**能不能**做某事」。它们是 DeviceManager 最常被消费的接口:

| 方法 | cuda | npu | xpu | 回答的问题 |
| --- | --- | --- | --- | --- |
| `supports_inplace_pin` | ✅ | ❌ | ❌ | 能否对 mmap 的权重文件原地 `cudaHostRegister` 锁页? |
| `supports_device_ipc` | ✅ | ✅ | 视情况 | 能否把**显存**张量跨进程零拷贝共享? |
| `supports_device_p2p` | ✅ | ✅ | ❌ | 能否用 mooncake 对**显存**做 RDMA 传输? |

三个开关的「真假依据」各不相同:

- `supports_inplace_pin` 只认 `device_type == "cuda"`——`cudaHostRegister` 是 CUDA 运行时特有的 API(u2-l3/u2-l4)。
- `supports_device_ipc` 在 cuda/npu 上恒 True(torch 的 `multiprocessing.reductions` 可用,u4-l3);在 xpu 上**委托**给项目自带的 `xpu_ipc.is_available()`——torch 没有 XPU 张量 IPC,项目自写了 SYCL 原生扩展,能否用取决于扩展能否在本机 JIT 编译成功(需要 oneAPI >= 2026.0 的 icpx,u4-l4)。
- `supports_device_p2p` 只认 cuda/npu——mooncake 没有 Level Zero 后端,注册不了 XPU 显存。

更值得注意的是**违反开关时的三种处理哲学**,这是本讲的精华:

1. **软降级**:注册 checkpoint 时若 `supports_inplace_pin()` 为 False,只打 warning 并把 `use_inplace_pin_memory` 强制改回 False,流程继续走 normal pin(见 [checkpoint_engine/ps.py:L331-L335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L331-L335))。功能有替代品,所以降级。
2. **入口硬失败**:广播更新开头若 `supports_device_ipc()` 为 False,直接 `raise RuntimeError`,错误信息还贴心地解释了 XPU 场景需要什么编译器(见 [checkpoint_engine/ps.py:L762-L772](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L762-L772))。注释写明了动机:与其在深处撞上 torch 的 `"_share_fd_: only available on CPU"` 这种难懂报错,不如在入口响亮失败。
3. **构造期跳过 + 使用期硬失败**:P2P 能力在 `ParameterServer.__init__` 里决定要不要创建 `P2PStore`(不支持就整个跳过,见 [checkpoint_engine/ps.py:L233-L248](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L233-L248));而显式发起 P2P 更新(`update(ranks=[...])`)时再检查一次,不支持则 raise 并建议改用广播(见 [checkpoint_engine/ps.py:L780-L788](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L780-L788))。

为什么 P2P 检查出现两次、行为却不同?因为「XPU 上不建 P2PStore」是**常态**(广播更新完全不需要它),静默跳过可以避免 `engine.initialize()` 在不支持的环境里抛出不必要的错误;而 `update(ranks=...)` 是用户**显式要求** P2P,无法满足就必须讲清楚。

#### 4.3.2 核心流程

```text
能力开关的三个消费点(按 ParameterServer 生命周期):

__init__ ──── supports_device_p2p()? ── 否 ──> 跳过 P2PStore 创建(记 info 日志)
                    │ 是
                    v
register_checkpoint ── supports_inplace_pin()? ── 否 ──> warning + 强制 normal pin(软降级)
                    │ 是
                    v
update(broadcast) ── supports_device_ipc()? ── 否 ──> RuntimeError(硬失败)
update(ranks=[..]) ── supports_device_p2p()? ── 否 ──> RuntimeError(硬失败 + 建议)

XPU 分支的 supports_device_ipc:
    device_type == "xpu" ──> xpu_ipc.is_available()
        └─ 首次真实探测(能否 JIT 编译 SYCL 扩展),成功后缓存 True,失败可重试
```

#### 4.3.3 源码精读

三个开关的实现集中在 [checkpoint_engine/device_utils.py:L285-L305](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L305)。

其中最精巧的是 [checkpoint_engine/device_utils.py:L289-L301](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L289-L301) 的 `supports_device_ipc`:cuda/npu 恒 True;xpu 分支里 `from checkpoint_engine import xpu_ipc` 依旧是延迟导入,然后返回 `xpu_ipc.is_available()`。被委托方在 [checkpoint_engine/xpu_ipc/__init__.py:L113-L128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L113-L128):全局标志 `_AVAILABLE` 缓存成功结果(失败不缓存、允许重试),内部会先确认 `torch.xpu.is_available()` 再尝试 `load_ext()` 真正编译扩展——「能力」在这里是**探测出来的事实**,不是硬编码的声明。

消费点一(软降级):[checkpoint_engine/ps.py:L331-L335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L331-L335),`register_checkpoint` 的参数预处理。

消费点二(构造期跳过):[checkpoint_engine/ps.py:L233-L248](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L233-L248),注释明确说了动机:不支持的后端上「跳过整个 store」而不是「急切初始化一个永远用不上的东西」。

消费点三/四(硬失败):[checkpoint_engine/ps.py:L762-L772](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L762-L772) 与 [checkpoint_engine/ps.py:L780-L788](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L780-L788)。

顺带一提,XPU 广播能力还与 `ParameterServer.__init__` 的 prewarm 联动:[checkpoint_engine/ps.py:L253-L264](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L253-L264) 在构造期就尝试编译 SYCL 扩展,把秒级 JIT 开销移出第一次权重更新的超时窗口——编译成败直接决定后续 `supports_device_ipc()` 在本机的答案。

#### 4.3.4 代码实践

1. **实践目标**:用 mock 验证 `supports_device_ipc` 在 XPU 上的「探测式」语义,以及三个开关的完整矩阵。
2. **操作步骤**:
   ```bash
   pytest tests/test_device_manager.py -v -m "not gpu" -k "supports"
   ```
   重点阅读 [tests/test_device_manager.py:L91-L98](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L91-L98):测试把 `checkpoint_engine.xpu_ipc.is_available` patch 成 True/False,断言 `supports_device_ipc()` 严格跟随——这说明开关完全委托给探测结果,而非设备类型硬编码。
3. **需要观察的现象**:`test_supports_inplace_pin`(cuda=True,npu/xpu=False)、`test_supports_device_ipc_true_for_cuda_npu`、`test_supports_device_ipc_xpu_uses_sycl_extension`(True/False 两断言)、`test_supports_device_ipc_unknown_device`(tpu=False)、`test_supports_device_p2p`(cuda/npu=True,xpu/tpu=False)全部通过。
4. **预期结果**:与 4.3.1 的能力矩阵逐格吻合。实际运行**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:`supports_device_ipc` 在 XPU 上为什么不能像 cuda/npu 一样直接返回 True 或 False?

**答案**:因为 XPU 的 IPC 能力不是「torch 有没有这个 API」的问题,而是「项目自写的 SYCL 扩展能否在本机编译加载」的问题——取决于是否装了带 `ipc_memory` 支持的 icpx(oneAPI >= 2026.0)、torch 版本是否触发了 c++20 构建回归等环境因素。所以它必须运行时探测,并委托给 [xpu_ipc.is_available()](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/xpu_ipc/__init__.py#L113-L128);测试 [test_real_xpu_device_ipc_available_when_extension_builds](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L158-L169) 专门守护这条真机语义。

**练习 2**:把 `register_checkpoint` 里 inplace pin 的软降级改成硬失败,会有什么后果?

**答案**:NPU/XPU 用户将无法注册任何带 `use_inplace_pin_memory=True`(默认值)的 checkpoint——尽管这些平台本来就能用 normal pin 正常工作。软降级的合理性在于 normal pin 是功能等价的替代路径,只是慢一点、多占一份内存(u2-l3 对比过两种策略);「有替代就降级、无替代才报错」是判断依据。

**练习 3**:为什么 `supports_device_p2p` 为 False 时,`__init__` 只是跳过 P2PStore,而 `update(ranks=...)` 却要 raise?

**答案**:见 4.3.1 的分析。广播路径(P2PStore 的非用户)在不支持平台上根本不需要这个 store,跳过是零成本的正确行为;而 `ranks` 参数是用户显式选择 P2P 的意图声明,静默忽略会造成「以为走了 RDMA 实际没走」的隐患,所以 [checkpoint_engine/ps.py:L780-L788](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L780-L788) 选择响亮失败并给出改用广播的建议。

### 4.4 差异化清理原语:ipc_collect 与 host_empty_cache

#### 4.4.1 概念说明

权重更新的**收尾清理**同样是后端差异重灾区,DeviceManager 把两个差异封装成一方法一语义:

- **`ipc_collect()`**——回收「陈旧 IPC 句柄」占用的显存。CUDA 的 IPC 句柄带引用计数,消费端异常退出时可能留下已打开未关闭的映射,`torch.cuda.ipc_collect()` 负责催收。三种行为:
  - cuda/npu:调 `device_module.ipc_collect()`,真清理;
  - xpu:**no-op**——SYCL `ipc_memory` 在 `close_handle` 时即释放,没有「句柄缓存」这种东西可收;
  - 未知设备:`TypeError`,响亮失败(与 backend 等属性同一风格)。
- **`host_empty_cache()`**——释放缓存的**锁页主机内存**。CUDA 有私有 API `torch._C._host_emptyCache()`(torch >= 2.5 才有,`ps.py` 里专门留了指向 PyTorch 源码的注释);NPU/XPU 没有对应物,退化为 `gc.collect()`——至少让 Python 层的引用先断掉。

注意区分三个「清理」的层次:`device_module.empty_cache()`(设备显存缓存,由 device_module 直接提供,不经 DeviceManager 包装)、`ipc_collect()`(IPC 句柄)、`host_empty_cache()`(主机锁页内存)。

#### 4.4.2 核心流程

以广播更新收尾为例(u3-l4 讲过整体次序,这里聚焦 DeviceManager 的两个调用):

```text
广播完成后(PS 侧):
  del views/base 张量
  └─ synchronize() ── gc.collect() ── ipc_collect() ── empty_cache() ── synchronize()
                                    ↑DeviceManager      ↑device_module

worker 侧收到第一个 None(释放 IPC 资源):
  synchronize() ── detach() ── gc.collect() ── ipc_collect() ── empty_cache() ── synchronize()

unregister_checkpoint 末尾:
  手动解页(如需) ── del 账本条目 ── host_empty_cache()
```

#### 4.4.3 源码精读

[checkpoint_engine/device_utils.py:L275-L283](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L275-L283) 是 `ipc_collect`:三分支分别对应真清理/no-op/报错,docstring 一句话点明用途("Reclaim memory held by stale IPC handles"),XPU 分支的行内注释解释了为什么是 no-op。

[checkpoint_engine/device_utils.py:L307-L312](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L307-L312) 是 `host_empty_cache`:cuda 走 torch 私有 API,其余走 `gc.collect()`。

PS 侧广播收尾的消费序列在 [checkpoint_engine/ps.py:L914-L921](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L914-L921):删引用 → `synchronize` → `gc.collect` → **`ipc_collect`** → `empty_cache` → 再 `synchronize`,顺序是「先让引用失效、再收 IPC、最后还显存缓存、同步兜底」。

worker 侧几乎镜像,见 [checkpoint_engine/worker.py:L94-L106](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L94-L106)(第一个 `None` 的处理分支里 `detach` 之后紧跟 `gc.collect` + `ipc_collect` + `empty_cache`)。

`host_empty_cache` 的消费点在 [checkpoint_engine/ps.py:L456-L460](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L456-L460):`unregister_checkpoint` 删完账本条目后调用,注释还链接到 PyTorch 源码里 `_host_emptyCache` 的实现位置,说明这依赖 torch >= 2.5.0 的 CUDA 构建。

#### 4.4.4 代码实践

1. **实践目标**:通过测试断言理解「同一清理方法在不同后端打到不同实现」。
2. **操作步骤**:
   ```bash
   pytest tests/test_device_manager.py -v -m "not gpu" -k "ipc_collect or host_empty_cache"
   ```
   阅读三个用例:[tests/test_device_manager.py:L53-L73](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L53-L73)(cuda 真调用 / xpu no-op / tpu 报错)与 [tests/test_device_manager.py:L116-L127](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L116-L127)(xpu 打到 `gc.collect` / cuda 打到 `torch._C._host_emptyCache`)。
3. **需要观察的现象**:`test_ipc_collect_present_is_called` 里假的 `ipc_collect` 被记录调用一次;`test_ipc_collect_xpu_is_noop` 不抛错;`test_ipc_collect_rejects_unsupported_device` 断言 `TypeError` 且消息含 "not supported";两个 host_empty_cache 用例分别断言 `gc.collect` / `_host_emptyCache` 恰好被调用一次。
4. **预期结果**:5 个用例全部通过。特别注意 `test_ipc_collect_rejects_unsupported_device` 的注释——它守护的设计决策是「不支持的后端必须响亮失败,与 backend/transfer_engine_protocol/_setup_device_module 保持一致」。实际运行**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:为什么 XPU 的 `ipc_collect` 是 no-op,而假想设备 "tpu" 却要 `TypeError`?

**答案**:见 [tests/test_device_manager.py:L61-L73](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py#L61-L73) 的两条注释。XPU 是**已知**后端,它的 SYCL ipc_memory 语义就是 `close_handle` 即释放、「没有缓存可收」——no-op 是正确行为且 `torch.xpu` 也没有 `ipc_collect` 方法;而 "tpu" 是**未知**后端,静默跳过会掩盖「你以为清理了其实什么都没发生」的 bug,响亮失败强制开发者先注册新后端的语义。

**练习 2**:如果 NPU/XPU 上不调 `host_empty_cache`(即没有 `gc.collect`),最直接的后果是什么?

**答案**:锁页内存的 Python 引用可能延迟释放。NPU/XPU 没有 CUDA 那样的 host 缓存池 API,`gc.collect()` 是仅有的兜底——它至少保证 `MemoryBuffer` 等对象此刻完成引用断链,使底层内存可被运行时回收。少了这一步,unregister 之后内存的真正归还时机将取决于解释器何时自动 GC,在「注销旧 checkpoint → 立刻注册新 checkpoint」的紧凑循环里可能顶高常驻内存。

**练习 3**:为什么 PS 侧清理序列里 `ipc_collect()` 要放在 `empty_cache()` 之前、且两头都有 `synchronize()`?

**答案**:IPC 句柄持有的映射也是一种设备内存占用,先收 IPC 再还缓存,`empty_cache` 才能把完整画像处理干净;两头的 `synchronize` 分别保证「删除的张量引用相关异步工作已完成」与「清理动作真正落地后再读 `mem_get_info` 打日志」——见 [checkpoint_engine/ps.py:L914-L924](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L914-L924),收尾后立即打印了 post-release 的显存统计。

## 5. 综合实践

**任务:写一个「后端能力矩阵报告器」,在纯 CPU 机器上枚举 DeviceManager 的全部查表行为。**

下面的「示例代码」综合了本讲全部内容——替身构造、两张映射表、三个能力开关、两个清理原语:

```python
# 示例代码:保存为 backend_matrix.py,纯 CPU 环境可直接运行
from types import SimpleNamespace
from unittest.mock import patch

from checkpoint_engine.device_utils import DeviceManager


def make(device_type: str) -> DeviceManager:
    """跳过硬件探测,伪造一个 DeviceManager(等价于测试里的 _make_manager)。"""
    dm = DeviceManager.__new__(DeviceManager)
    dm.device_type = device_type
    dm.device_module = SimpleNamespace()  # 查表用不到设备方法,留空即可
    return dm


rows = []
for dt in ("cuda", "npu", "xpu", "tpu"):
    dm = make(dt)
    for efa in (False, True):
        try:
            backend = dm.backend  # tpu 在这里响亮失败
        except TypeError:
            backend = "<TypeError>"
        with patch("checkpoint_engine.device_utils.has_efa_pci", return_value=efa):
            try:
                protocol = dm.transfer_engine_protocol
            except TypeError:
                protocol = "<TypeError>"
        rows.append((dt, efa, backend, protocol))

for dt, efa, backend, protocol in rows:
    print(f"{dt:5} efa={efa!s:5} backend={backend:13} protocol={protocol}")

for dt in ("cuda", "npu", "xpu", "tpu"):
    dm = make(dt)
    with patch("checkpoint_engine.xpu_ipc.is_available", return_value=False):
        try:
            ipc = dm.supports_device_ipc()
        except Exception as e:  # noqa: BLE001
            ipc = f"<{type(e).__name__}>"
    print(
        f"{dt:5} inplace_pin={dm.supports_inplace_pin()!s:5}"
        f" device_ipc={ipc!s:5} device_p2p={dm.supports_device_p2p()}"
    )
```

操作步骤与检查清单:

1. 在仓库根目录运行 `python backend_matrix.py`。
2. 核对输出与两张表是否一致:cuda/xpu 在 `efa=True` 时协议应翻转为 `efa`;npu 恒 `ascend_direct`;tpu 的 backend 与 protocol 两列都应是 `<TypeError>`(查表真实调用并捕获异常,验证「未知后端响亮失败」)。
3. 再把 `is_available` 的 patch 值改为 `True` 重跑,确认只有 `xpu` 行的 `device_ipc` 从 `False` 变 `True`——其余行不受影响,证明委托只发生在 XPU 分支。
4. 对照 [tests/test_device_manager.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_device_manager.py) 的断言逐项核对,然后跑一遍完整 CPU 套件确认:`pytest tests/test_device_manager.py -v -m "not gpu"`。

预期结果:矩阵输出与 4.2.1 / 4.3.1 的表格逐格一致,CPU 测试全绿。脚本输出**待本地验证**。

## 6. 本讲小结

- `DeviceManager` 状态极简(`device_type` + `device_module`),探测顺序 npu → xpu → cuda,全不可用即 `TypeError`;`device_type` 字符串同时是合法 torch 设备名,可直接传 `device=`。
- 两张核心映射表:`npu→hccl / xpu→xccl / cuda→nccl`(给 `dist.init_process_group`),`npu→ascend_direct / cuda,xpu→rdma|efa`(给 mooncake `TransferEngine.initialize`,EFA 靠 sysfs vendor ID `0x1d0f` 探测)。
- 三个能力开关语义各异:inplace pin 仅 CUDA;device IPC 在 cuda/npu 恒真、在 xpu 委托 `xpu_ipc.is_available()` 运行时探测;device P2P 排除 xpu(mooncake 无 Level Zero 后端)。
- 违反开关有三种处理哲学:软降级(inplace pin 回落 normal pin)、入口硬失败(IPC/P2P 直接 raise 并给出修复建议)、构造期静默跳过 + 使用期硬失败(P2PStore)。
- 清理原语按后端分岔:`ipc_collect` 在 cuda/npu 真收、xpu no-op、未知设备报错;`host_empty_cache` 在 cuda 走 `torch._C._host_emptyCache`(torch>=2.5),其余退化为 `gc.collect()`。
- 测试范式:`DeviceManager.__new__` + `SimpleNamespace` 替身配合 `patch`,让全部硬件查表逻辑在纯 CPU 的 CI 上验证。

## 7. 下一步学习建议

- 下一讲 **u5-l2(distributed 抽象层)**:本讲的 `backend` 字符串在那里有另一个消费方——`TorchBackend` 与动态 `use_backend` 机制,看看「nccl/hccl」这个字符串如何被替换成自定义通信实现。
- 之后 **u5-l5(P2PStore 与 RDMA 设备发现)**:`rdma_device` 背后的 `_get_rdma_devices` / `_parse_NCCL_IB_HCA` / `_get_my_rdma_device` 三个函数的完整算法(ibverbs 枚举、`=`/`^`/`^=` 前缀语法、网卡均分)。
- 回顾 **u4-l4(XPU SYCL IPC)**:`supports_device_ipc` 委托的 `xpu_ipc.is_available()` 的完整实现(JIT 编译、缓存策略、prewarm)。
- 若要二次开发新硬件后端:按 4.1.5 练习 3 的清单改完所有分发点后,先给 `tests/test_device_manager.py` 补一组参数化用例再动 `ps.py`——本讲的测试替身范式正是为此准备的。
