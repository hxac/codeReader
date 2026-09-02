# u5-l4 NPU/HCCL 后端与昇腾平台适配

## 1. 本讲目标

本讲是分布式后端三部曲的最后一讲(上一讲 u5-l3 精读了 CUDA 路径的 `DistributedNccl`,本讲把同一套骨架放到华为昇腾 NPU 上对照着读,下一讲 u5-l5 离开「通信后端」进入 P2P 传输)。读完本讲,你应该能够:

1. 读懂 `HcclCommConfig` 这份 18 字段 C 结构体的 ctypes 复刻,理解它的 `size`/`magic_word`/`version` 三件套如何承担 ABI 版本协商,以及满屏 \( \texttt{0xFFFFFFFF} \) 哨兵值的含义。
2. 理解本模块如何用与 CUDA 路径完全相同的手法,给 vllm_ascend 的 `HCCLLibrary` 动态补上 `HcclAllGather` 与 `HcclCreateSubCommConfig` 两个 ctypes 函数绑定。
3. 掌握 `PyHcclCommunicatorEx.create_subcomm` 中 `HcclCreateSubCommConfig` 各参数(尤其 `rankIds` 与 `subCommRankId`)的含义,并能手工推演每个成员进程算出的子组编号。
4. 对比 NPU 与 CUDA 路径在 `new_group`(全员集体调用 vs 仅成员调用)与广播语义(依赖同一套 `_use_group` rank 重映射)上的差异。
5. 依据 `docs/npu_start.md` 说出昇腾环境的软件版本约束、HCCL 默认端口 16666 引发的 ranktable 配置要求,以及 P2P 模式的环境变量要求。

本讲除 4.5 节的部署类实践外,所有代码实践都可在纯 CPU 环境完成——不需要 NPU、CANN、vllm 或 vllm_ascend,只需要标准库 `ctypes`。

## 2. 前置知识

### 2.1 昇腾生态的角色对照

u5-l1 讲过 `DeviceManager` 把设备类型 `npu` 翻译成通信后端 `hccl`。把昇腾生态与 CUDA 生态一一对齐,本讲的 import 就不再陌生:

| CUDA 世界 | 昇腾世界 | 在本项目中的角色 |
| --- | --- | --- |
| NVIDIA GPU + 驱动/CUDA | 昇腾 NPU + Ascend HDK + CANN | 硬件与底层运行时(CANN 是「昇腾版 CUDA」) |
| NCCL 库 | HCCL 库 | 设备侧集合通信库(all_reduce/broadcast/…) |
| `torch`(cuda 后端) | `torch` + `torch_npu` | 给 Python 提供 `torch.npu` 设备 API |
| vLLM | vLLM + vllm_ascend | 推理引擎;vllm_ascend 提供本讲的 `PyHcclCommunicator` |
| `PyNcclCommunicator`(u5-l3) | `PyHcclCommunicator` | vLLM 系对裸通信器的 ctypes Python 封装 |

`vllm_hccl.py` 顶部的 import 正是这张表的落地:`torch.npu` 来自 torch_npu,`StatelessProcessGroup` 仍是 vLLM 的(会合层与硬件无关),而 `PyHcclCommunicator`、`HCCLLibrary`、`current_stream` 都来自 vllm_ascend。

### 2.2 为什么不能照抄 CUDA 路径:两种「切子通信组」哲学

u5-l3 的 `ncclCommSplit` 用 **color/key** 语义:父组**所有**进程都必须集体调用,想加入的传 `color=0`、不想加入的传 `color=-1`,库内按 `key` 升序给新组编号。HCCL 没有这个接口,对应能力由 `HcclCreateSubCommConfig` 提供,哲学完全不同:**只有要加入子组的成员才调用**,每个成员把完整的成员清单 `rankIds` 和自己在清单里的下标 `subCommRankId` 显式报给库——「谁进组、排第几」不再由库内的 color/key 机制决定,而是由调用方在 Python 侧算好。这个差异会贯穿 4.4 节的逐行对比。

另外 NCCL 切组时 config 可以传 `NULL`(u5-l3 的 CUDA 路径虽然定义了 `NcclConfigT` 结构体,但调用时传的就是 `None`);HCCL 的这个接口则要求调用方**逐字段填写**一个 `HcclCommConfig` 结构体——这就是 4.1 节整节篇幅的由来。

### 2.3 ctypes.Structure 与 ABI 版本协商

ctypes 允许用 Python 类复刻 C 结构体:`_fields_` 声明字段名与 C 类型,ctypes 按平台 C 布局规则(自然对齐、尾部补齐)排布内存,实例默认零初始化。复刻的第一个字段往往叫 `size`,值必须等于该结构体的 `sizeof`——这是 C 库常见的 ABI 协商手法:库拿到指针后先读 `size`,就知道调用方编译时所依据的头文件版本,进而知道哪些字段可信、哪些是新增的。`magic_word` 与 `version` 同理,是「这确实是一个合法 config」的身份凭证。`ncclConfig_t`(NCCL 官方)与这里的 `HcclCommConfig` 都用这套手法。

### 2.4 一个会咬人的默认端口

HCCL 建立device间链路时默认使用端口 **16666**;而 P2P 路径依赖的 mooncake Transfer Engine 在昇腾上的底层传输库 HIXL 同样默认 16666,且(截至文档撰写时)没有接口可改。单机多进程部署时这两个「隐形占坑者」会互相冲突,解法是用 ranktable 文件给每张卡显式指定 `device_port`——细节在 4.5 节。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [checkpoint_engine/distributed/vllm_hccl.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py) | 329 | 本讲主角:`HcclCommConfig` 复刻、HCCLLibrary 的 ctypes 扩展、`PyHcclCommunicatorEx`、`DistributedHccl` |
| [docs/npu_start.md](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md) | 91 | 昇腾适配文档:版本矩阵、安装、ranktable 端口配置、环境变量注意事项 |
| checkpoint_engine/distributed/vllm_nccl.py | 239 | 对照组:同一套骨架的 CUDA 实现(u5-l3 精读过) |
| checkpoint_engine/distributed/base.py | 293 | 承接 u5-l2:`Distributed` ABC、`CommGroup`、`use_backend`、`_common_all_gather_object` |
| checkpoint_engine/distributed/vllm_compat.py | 16 | `create_stateless_process_group` 兼容垫片(u5-l3 精读过,本讲复用) |
| checkpoint_engine/ps.py(消费点) | — | [ps.py:599](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L599) 的 `dist.new_group(ranks)` 与 [ps.py:541-547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541-L547) 的默认建组路径 |
| examples/update.py(入口) | — | `--custom-dist vllm_hccl` 的切换时机([update.py:191-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L191-L192)) |

一句话定位:`vllm_hccl.py` = 「vllm_ascend 的通信设施 + 一份必须逐字段填写的 C 结构体 + 一段 ctypes 扩展 + 一个与 CUDA 路径几乎逐行平行的 `Distributed` 适配器」。它的 git 历史只有两笔:`fe57396`(#66)随「扩展集体通信库」功能引入,`d1de07b`(#102,当前 HEAD)把 `StatelessProcessGroup` 构造抽到 `vllm_compat.py`。

## 4. 核心概念与源码讲解

### 4.1 HcclCommConfig:18 字段 C 结构体的 Python 复刻

#### 4.1.1 概念说明

`HcclCreateSubCommConfig` 是 HCCL 的 C 接口,它要求调用方传入一个 `HcclCommConfig *`。Python 侧没有这个类型,必须用 `ctypes.Structure` 把它**逐字段、逐宽度**复刻出来——任何一处偏移错位,HCCL 读到的就是错位的字节。这个结构体因此成为 NPU 路径独有的一块「硬骨头」:CUDA 路径(u5-l3)定义了 `NcclConfigT` 却从不使用,而这里必须认真填写每一个字段。

#### 4.1.2 核心流程

```text
复刻一个跨语言结构体:
  1. 按 C 头文件顺序声明 _fields_(字段名, C 类型)
  2. ctypes 按自然对齐排布内存,实例默认零初始化
  3. 构造时逐字段赋值:
     - size/magic_word/version:ABI 协商三件套,照抄头文件约定
     - 不想定制的 uint32 字段:填 0xFFFFFFFF 表示「用库默认」
     - comm_engine 这类 int32:填 -1 表示「不指定」
     - 两个 char[128] 名字字段:填 b"\0"(空 C 字符串)
```

字段按用途分组如下(均为 [vllm_hccl.py:25-44](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L25-L44) 的声明顺序):

| 分组 | 字段 | 本代码中的取值 |
| --- | --- | --- |
| ABI 协商 | `size` / `magic_word` / `version` / `reserved` | 312 / \( \texttt{0xF0F0F0F0} \) / 6 / 0 |
| 通信行为 | `hccl_buffer_size` / `hccl_deterministic` / `hccl_op_expansion_mode` | \( \texttt{0xFFFFFFFF} \) / \( \texttt{0xFFFFFFFF} \) / 0 |
| 命名 | `hccl_comm_name` / `hccl_udi`(各 char[128]) | `b"\0"` / `b"\0"` |
| RDMA | `hccl_rdma_traffic_class` / `hccl_rdma_service_level` | \( \texttt{0xFFFFFFFF} \) / \( \texttt{0xFFFFFFFF} \) |
| 作业标识 | `hcll_world_rank_id` / `hccl_job_id` | 0 / 0 |
| 引擎与线程 | `comm_engine` / `thread_num` / `notify_num_per_thread` / `acl_graph_zero_copy_enable` | -1 / \( \texttt{0xFFFFFFFF} \) / \( \texttt{0xFFFFFFFF} \) / 0 |

\( \texttt{0xFFFFFFFF} \) 是 uint32 的最大值,在这里当**哨兵值**用:「这个字段我不指定,请 HCCL 用内部默认」。`comm_engine=-1` 同理(int32 的 -1)。真正的取值意图只有前三个:`size` 必须等于目标库认知的结构体大小,`magic_word` 与 `version` 是库校验 config 合法性与版本的依据(具体校验行为在闭源的 HCCL 库内部,待确认)。

#### 4.1.3 源码精读

结构体定义全文:

[HcclCommConfig 结构体声明 · vllm_hccl.py:25-44](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L25-L44) 用 `_fields_` 逐字段复刻了 HCCL 的 config 结构体:先是 `size`(`c_size_t`,64 位平台 8 字节)与两个 uint32、一个 uint64,接着两个 128 字节的 C 字符数组,再是 11 个标量字段,最后 1 个 uint8。

实际填值发生在 `create_subcomm` 里:

[构造 HcclCommConfig · vllm_hccl.py:162-180](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L162-L180) 逐字段构造 config:协商三件套、一排「用默认」哨兵、两个空名字。这份代码里有三处值得停留的细节:

1. **`size=312` 与手工推算的结构体大小对不上**。按 C 自然对齐规则手工累加本复刻的 17 个字段:8+4+4+8+4+4+128+128+4+4+4+4+8+4+4+4+1=325 字节,再补齐到最大对齐 8 的倍数:

   \[ \text{sizeof}=\Big\lceil \frac{325}{8} \Big\rceil \times 8 = 328 \]

   而 312 恰好等于第 13 个字段 `hccl_job_id` 结束处的偏移(304+8=312,本身已是 8 的倍数)。一个自洽的解释是:字面量 312 取自目标 CANN 版本头文件中**真实** `HcclCommConfig` 的大小,那个版本的结构体到 `hccl_job_id` 为止,末尾 4 个字段(`comm_engine` 等)是后续 CANN 版本新增的;这份 Python 复刻抄全了新字段,`size` 字面量却没跟着更新。这属于阅读推论,请用 4.1.4 的实践在本地验证 `ctypes.sizeof` 的真实输出。若 `size` 与库预期不符,`HcclCreateSubCommConfig` 可能直接校验失败——这正是 ABI 协商字段的意义,也是它最值得警惕的地方(实际是否报错,待本地验证)。

2. **两处字段名拼写错位**。结构体第 12 个字段声明为 `hcll_world_rank_id`(少了一个 c,[vllm_hccl.py:38](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L38)),构造时却写 `hccl_world_rank_id=0`(拼写正确,[vllm_hccl.py:174](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L174));反过来,结构体的 `hccl_op_expansion_mode` 拼写正确,构造时却写 `hccl_op_expansize_mode=0`([vllm_hccl.py:171](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L171))。`ctypes.Structure` 对未知关键字参数的行为在不同 CPython 版本间存在「静默忽略」与「抛 TypeError」两种可能(本讲义写作环境无法运行 Python 验证,待本地验证)。好消息是:即便被静默忽略,这两个字段想写的值本来就是 0,而 ctypes 实例默认零初始化,所以**字段值不受影响**;但若你的环境是严格校验的版本,`create_subcomm` 会在构造 config 时直接抛 TypeError——这条故障路径值得在接入新 Python 版本时优先排查。

3. **`ctypes` 的零初始化兜底**。所有「用默认」字段就算漏写,零值也与「不指定」语义无冲突,这正是把易变参数全部做成哨兵值的工程好处。

#### 4.1.4 代码实践

1. **实践目标**:在纯 CPU 环境用标准库 `ctypes` 验证本节的两处推断:结构体真实 `sizeof` 与两处拼写错位的运行时后果。
2. **操作步骤**:把下面这段**示例代码**(非项目源码)存成任意 `.py` 文件运行,只需标准库:

   ```python
   import ctypes

   class HcclCommConfig(ctypes.Structure):   # 照抄 vllm_hccl.py:25-44
       _fields_ = [
           ("size", ctypes.c_size_t), ("magic_word", ctypes.c_uint32),
           ("version", ctypes.c_uint32), ("reserved", ctypes.c_uint64),
           ("hccl_buffer_size", ctypes.c_uint32), ("hccl_deterministic", ctypes.c_uint32),
           ("hccl_comm_name", ctypes.c_char * 128), ("hccl_udi", ctypes.c_char * 128),
           ("hccl_op_expansion_mode", ctypes.c_uint32),
           ("hccl_rdma_traffic_class", ctypes.c_uint32),
           ("hccl_rdma_service_level", ctypes.c_uint32),
           ("hcll_world_rank_id", ctypes.c_uint32),
           ("hccl_job_id", ctypes.c_uint64), ("comm_engine", ctypes.c_int32),
           ("thread_num", ctypes.c_uint32), ("notify_num_per_thread", ctypes.c_uint32),
           ("acl_graph_zero_copy_enable", ctypes.c_uint8),
       ]

   print("sizeof =", ctypes.sizeof(HcclCommConfig))   # 对照源码里的 size=312
   try:
       cfg = HcclCommConfig(size=312, hccl_op_expansize_mode=0, hccl_world_rank_id=0)
       print("未知关键字被接受;两个字段值 =",
             cfg.hccl_op_expansion_mode, cfg.hcll_world_rank_id)
   except TypeError as e:
       print("未知关键字被拒绝 ->", e)
   ```

3. **需要观察的现象**:`sizeof` 的输出值;构造带拼写错位关键字的 config 是否抛异常。
4. **预期结果**:按本节手工推算,`sizeof` 应打印 **328**(而非源码字面量 312);第二段要么打印「未知关键字被接受;两个字段值 = 0 0」,要么抛 `TypeError`,二者必居其一——哪种发生取决于你本机 CPython 的 ctypes 行为。若你手头正好是 docs 要求的 Python 3.11,请把结果记下来,它直接决定 4.1.3 第 2 点里哪条分支成立。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:`size` 字段为什么必须认真填,随手写 0 行不行?
**答案**:不行。`size` 是 ABI 协商字段,HCCL 拿到 config 指针后先读它来判断调用方结构体的版本与有效字段范围。写 0 等于告诉库「这是一个未知版本的结构体」,轻则校验失败、重则库按错误布局解读后续字节。本代码填 312,含义是「我按 sizeof 为 312 的那个头文件版本准备了这个 config」。

**练习 2**:`hccl_buffer_size=0xFFFFFFFF` 为什么不直接写 0?
**答案**:0 与 \( \texttt{0xFFFFFFFF} \)(uint32 最大值)语义不同。前者往往表示「显式关闭/无」,后者在本结构体里当「未指定、请用库默认」的哨兵。缓冲区大小、线程数这类字段,项目并不想替 HCCL 做决定,所以统一交给默认值;若误写成 0,可能被解释成「零缓冲/零线程」。

**练习 3**:两处关键字拼写错位,即使被静默忽略也不会改变字段值,为什么?
**答案**:两处想写的值都是 0,而 `ctypes.Structure` 实例默认零初始化——未知关键字被忽略后,字段保持默认 0,恰好等于本想显式写入的值。影响仅限于「严格校验版 CPython 会抛 TypeError」这一种情形。

### 4.2 HCCLLibrary 的 ctypes 扩展:补上两个函数绑定

#### 4.2.1 概念说明

vllm_ascend 的 `pyhccl_wrapper.HCCLLibrary` 与 u5-l3 见过的 `NCCLLibrary` 同构:持有一张 `exported_functions` 函数声明表(每项声明函数名、返回类型、参数类型),实例化时据此生成 `_funcs` 字典并加载动态库。但它的声明表里缺了本项目需要的两个函数——`HcclAllGather` 与 `HcclCreateSubCommConfig`。本模块的做法与 CUDA 路径如出一辙:**不改 vllm_ascend 一行源码**,import 时往类属性里追加声明、再往类上挂 Python 方法。能从「只补这两个函数」反推:`all_reduce`/`broadcast` 等其余操作,vllm_ascend 的包装器已有合用绑定,直接复用。

#### 4.2.2 核心流程

```text
import checkpoint_engine.distributed.vllm_hccl 时立即执行:
  1. orig = HCCLLibrary.exported_functions          # 保存原声明表
  2. extended = [Function("HcclAllGather", ...),     # 追加两条声明
                 Function("HcclCreateSubCommConfig", ...)]
  3. HCCLLibrary.exported_functions = orig + extended
  4. HCCLLibrary.hcclAllGather = hccl_all_gather            # 挂方法
     HCCLLibrary.hcclCreateSubCommConfig = hccl_create_subcomm_config

之后任何 HCCLLibrary 实例化(首次 PyHcclCommunicator 构造时):
  _funcs 字典按(已扩充的)exported_functions 逐条查找并绑定 → 新函数可用
```

时序上为什么安全?与 u5-l3 论证过的相同:patch 在 import 期执行,而 `HCCLLibrary` 单例的真正实例化被推迟到第一次构造 `PyHcclCommunicator`(即 `init_process_group` 阶段);调用链上 `use_backend("vllm_hccl")` 又发生在 `ParameterServer` 构造之前([examples/update.py:191-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L191-L192)),`use_backend` 内部经 `importlib` 才首次 import 本模块([base.py:239-242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L239-L242))。所以 patch 必然先于任何库实例化。

#### 4.2.3 源码精读

两条 C 签名声明(注释里的签名摘自 HCCL 头文件,注释中的 `alcrtStream`/`uin32_t` 等拼写瑕疵系源码注释原有):

[声明 HcclAllGather 与 HcclCreateSubCommConfig · vllm_hccl.py:47-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L47-L82) 用 `Function(名字, 返回类型, 参数类型列表)` 声明两个 HCCL 函数:

- `HcclAllGather(sendBuf, recvBuf, sendCount, dataType, comm, stream)`——注意参数顺序是**先输出后计数**,与 NCCL 风格不同;
- `HcclCreateSubCommConfig(&comm, rankNum, rankIds, subCommId, subCommRankId, &config, &subComm)`——注意 `comm` 与 `subComm` 都是指针传出/传入,`rankIds` 是 uint32 数组指针,`config` 指向 4.1 节的结构体。

两个 Python 包装函数:

[hccl_all_gather · vllm_hccl.py:85-96](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L85-L96) 取出 `_funcs["HcclAllGather"]` 调用并用 `self.HCCL_CHECK` 检查返回码(vllm_ascend 包装器提供的错误检查助手,非零返回码转成异常)。

[hccl_create_subcomm_config · vllm_hccl.py:99-120](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L99-L120) 先造一个空的 `subcomm = hcclComm_t()`,再用 `ctypes.byref` 把父通信器、config、输出槽一并传给库,校验通过后返回新通信器。一处小瑕疵:第 104 行形参注解写 `subcomm_rank: ctypes.c_uint64`,而第 77 行 `Function` 声明里该位置是 `ctypes.c_uint32`——实际调用方传的是普通 Python int,注解仅是文档性质,不影响运行。

挂载动作:

[扩展 HCCLLibrary · vllm_hccl.py:123-126](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L123-L126) 三行完成:拼接声明表、挂两个方法。因为绑定的是普通函数,经类访问时 `self` 即库实例,与 vllm_ascend 自身方法的形态一致。

#### 4.2.4 代码实践

1. **实践目标**:通过并排对照 CUDA 与 NPU 两份扩展代码,内化「同一 monkey patch 模式在不同硬件库上的重演」,并验证 patch 时序论证链条。
2. **操作步骤**:
   - 打开 `checkpoint_engine/distributed/vllm_nccl.py` 与 `checkpoint_engine/distributed/vllm_hccl.py`,对照各自的「extended_functions 定义 → 包装函数 → 挂载」三段,填写下表(答案见本讲 4.4.3 的对比表,先自己填再看):

     | 维度 | vllm_nccl.py | vllm_hccl.py |
     | --- | --- | --- |
     | 补了几个函数 | ? | ? |
     | 切组函数名 | ? | ? |
     | config 参数实传 | ? | ? |
     | current_stream 导入方式 | 三级 try/except 兼容 | ? |

   - 用 Grep 在仓库内搜 `use_backend` 与 `ParameterServer(`,确认 `examples/update.py` 中两者的先后次序。
3. **需要观察的现象**:两张扩展清单的差异点;`use_backend(args.custom_dist)` 行号是否小于 `ParameterServer(auto_pg=True)` 行号。
4. **预期结果**:NCCL 只补 1 个函数(`ncclCommSplit`),HCCL 补 2 个;NCCL 切组时 config 传 `None`,HCCL 传填好的结构体;HCCL 的 `current_stream` 直接 `from vllm_ascend.utils import current_stream`([vllm_hccl.py:19](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L19)),没有 CUDA 路径那三级版本兼容([vllm_nccl.py:20-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L20-L28))——因为 vllm_ascend 的版本矩阵被钉得很死(见 4.5),不需要兼容多个版本。`use_backend` 在 [update.py:191](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L191),`ParameterServer` 在下一行,次序符合论证。

#### 4.2.5 小练习与答案

**练习 1**:为什么 patch 写在模块顶层(import 时执行)而不是写成函数让人调用?
**答案**:因为挂载目标是**类**而非实例。`HCCLLibrary` 是延迟实例化的单例,任何实例将来构建 `_funcs` 时读的都是类属性 `exported_functions`;在 import 期改类属性,天然保证「先改表、后建实例」,不需要调用方记得先执行某个初始化函数,也不会有遗漏调用导致缺函数的隐患。

**练习 2**:从 `extended_functions` 只有这两项,能推断出什么?
**答案**:vllm_ascend 的 `HCCLLibrary` 已提供 all_reduce、broadcast、通信器创建销毁等绑定(否则还得补);缺的是(或本项目合用的是)`HcclAllGather`——它是 `Distributed.all_gather_object` 经 `_common_all_gather_object`(u5-l2)落到通信器上的唯一入口——以及切子组所需的 `HcclCreateSubCommConfig`。

### 4.3 PyHcclCommunicatorEx:all_gather 重写与 create_subcomm

#### 4.3.1 概念说明

`PyHcclCommunicatorEx` 之于 HCCL,正如 `PyNcclCommunicatorEx` 之于 NCCL:继承 vLLM 系的裸通信器封装,补上「切子通信组 + 定点销毁」两个能力。它的增量状态只有一个 `subcomm_id` 计数器(初始 1);增量方法有三个:`destroy_comm`(支持销毁指定通信器)、重写的 `all_gather`、以及核心的 `create_subcomm`。

#### 4.3.2 核心流程

`create_subcomm(ranks)` 的执行过程(每个**成员**进程各自执行一遍):

```text
输入: 排序后的成员 rank 清单 ranks(全局编号)
  1. 构造 HcclCommConfig(4.1 节的逐字段填写)
  2. 把 ranks 转成 C 的 uint32 数组 c_rank_ids
  3. subcomm_rank = ranks.index(self.rank)   # 我在成员清单里排第几
  4. ranks_size  = len(ranks)
  5. subcomm_id  = self.subcomm_id; self.subcomm_id += 1  # 取号并自增
  6. 调 HcclCreateSubCommConfig(父comm, ranks_size, c_rank_ids,
                                subcomm_id, subcomm_rank, config)
     → 返回新的子通信器 subcomm
```

关键在第 3 步:**组内编号是每个成员在 Python 侧自己算的**,库只负责登记。因为 `ranks` 已排序且每个成员拿到的清单一致,`ranks.index()` 在所有成员上产生相容的 0..n-1 编号——这与 NCCL 按 `key` 升序编号的效果完全等价(4.4.3 展开)。第 5 步的 `subcomm_id` 用来区分「同一个父通信器切出的多个子组」:NCCL 靠集体调用的次序隐式区分,HCCL 这里没有集体次序可言,只能显式发号。

#### 4.3.3 源码精读

类定义与唯一增量状态:

[PyHcclCommunicatorEx · vllm_hccl.py:129-132](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L129-L132) 调父类构造后置 `self.subcomm_id = 1`。对比 CUDA 路径的 `PyNcclCommunicatorEx` 完全没有额外状态([vllm_nccl.py:91-96](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L91-L96))——计数器正是「显式发号 vs 集体次序」差异的物证。

重写的 `all_gather`:

[all_gather · vllm_hccl.py:140-159](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L140-L159) 用 4.2 节补的 `HcclAllGather` 绑定实现 all_gather,签名 `(out_tensor, in_tensor, stream=None)`——**第一个参数是输出**,恰好对上 u5-l2 里 `CommunicatorProtocol` 与 `_common_all_gather_object` 的调用形态(`comm.all_gather(输出, 输入)`)。细节:父类的 `self.disabled` 早退;断言张量在通信器所属设备上;`stream` 缺省取 `current_stream()`(vllm_ascend 提供的 NPU 流);指针经 `buffer_type(in_tensor.data_ptr())` 传入,数据类型经 `hcclDataTypeEnum.from_torch` 把 torch dtype 译成 HCCL 枚举。最要紧的是第 156 行用的是 **`self.comm`**(行内注释 `# todo` 标记着作者自己也认为此处未臻完善):`self.comm` 是实例属性,4.4 节的 `_use_group` 会把它临时换成子组通信器——在这里读 `self.comm`,意味着子组上下文中的 all_gather 自动落到子组上。

`create_subcomm` 全文:

[create_subcomm · vllm_hccl.py:161-191](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L161-L191) 按 4.3.2 的六步执行。第 181-182 行是 ctypes 的惯用法:先造数组类型 `uint32_array = ctypes.c_uint32 * len(ranks)`,再用 `uint32_array(*ranks)` 把 Python 清单展开成 C 数组;第 183 行 `subcomm_rank = ranks.index(self.rank)` 是组内编号的全部来源。

定点销毁:

[destroy_comm · vllm_hccl.py:134-138](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L134-L138) 带参时销毁指定(子)通信器,不带参时销毁通信器自身——这正对应 4.4 节 `destroy_process_group` 的两种粒度。

#### 4.3.4 代码实践

1. **实践目标**:不依赖任何 NPU 环境,手工(并用代码)推演 `create_subcomm` 的组内编号,验证「每个成员各自 `ranks.index(self.rank)`,结果全局相容」。
2. **操作步骤**:运行下面这段**示例代码**(非项目源码),把 `create_subcomm` 第 2-4 步的逻辑抽出:

   ```python
   ranks = sorted([5, 1, 3])          # 模拟 new_group 里的 ranks.sort()
   for my_rank in [0, 1, 3, 5]:       # 0 是非成员,1/3/5 是成员
       if my_rank not in ranks:
           print(f"rank {my_rank}: 非成员,不调用(提前 return)")
           continue
       print(f"rank {my_rank}: rankIds={ranks}, "
             f"rankNum={len(ranks)}, subCommRankId={ranks.index(my_rank)}")
   ```

3. **需要观察的现象**:每个成员打印的 `rankIds`/`rankNum` 是否一致;`subCommRankId` 是否恰好取到 0/1/2 各一次。
4. **预期结果**:三个成员的 `rankIds` 与 `rankNum` 完全一致(这是库能正确组网的前提),`subCommRankId` 分别为 0、1、2;rank 0 打印「非成员,不调用」。若把 `ranks` 换成未排序的 `[5,1,3]` 再跑,成员间 `subCommRankId` 仍相容(都按同一清单的 index),但与 NCCL「按 key 升序」的编号就不再对应——这正是 `new_group` 里必须先 `ranks.sort()` 的原因。

#### 4.3.5 小练习与答案

**练习 1**:`subcomm_id` 计数器为什么必须存在,而 CUDA 路径没有对应物?
**答案**:NCCL 的 `ncclCommSplit` 是父组全员按相同次序集体调用的,库内可以靠「第几次集体调用」区分不同的子组;HCCL 这里只有成员各自调用,不存在全员对齐的调用次序,必须由调用方显式给每次切组发号(`subcomm_id` 从 1 起自增),库才知道这是父通信器下的第几个子组。

**练习 2**:重写的 `all_gather` 里,为什么读取的是 `self.comm` 而不是在方法开头把通信器存进局部变量?
**答案**:`_use_group` 上下文(4.4)会在进入时把 `self.pyhccl.comm` 临时替换为子组通信器、退出时恢复。在调用库函数的那一刻读取 `self.comm`,拿到的是「当前生效」的通信器,子组语义因此自动成立;若提前缓存,就永远用父通信器了。这与 u5-l3 中 `pynccl.comm` 被交换是同一个机制。

**练习 3**:`destroy_comm(comm)` 与 `destroy_comm()` 分别在什么场景被调用?
**答案**:带参版本销毁某个子通信器,对应 auto_pg 流程中每轮 update 结束时按组销毁(`destroy_process_group(group=...)`);不带参版本销毁通信器本体,对应整个后端生命周期结束(`destroy_process_group()` 不带 group)。

### 4.4 DistributedHccl 适配器:与 CUDA 路径逐段对比

#### 4.4.1 概念说明

`DistributedHccl` 实现了 u5-l2 的 `Distributed` ABC 八个操作。把它与 `DistributedNccl` 并排打开,会发现 `_use_group`、生命周期管理、集合操作几乎是逐行平行的复制——这本身就是抽象层价值的证明:平台差异被压缩到了少数几个点。本节就把力气花在这些**差异点**上,重点是 `new_group` 的参与规则与广播的 rank 语义。

#### 4.4.2 核心流程

生命周期与集合操作骨架(与 CUDA 路径共享):

```text
init_process_group(rank, world_size, store):
  device = torch.device("npu", torch.npu.current_device())     # 需 torch_npu
  pg     = create_stateless_process_group(...)                  # vllm_compat 垫片
  pyhccl = PyHcclCommunicatorEx(group=pg, device=device)        # 建裸 HCCL 通信器
  comm   = pyhccl.comm                                          # 记住父通信器

new_group(ranks):                     # ← 与 CUDA 差异最大的一处
  ranks 为空 → 取全体;否则 ranks.sort()
  若 self.rank 不在 ranks → 直接返回 None(非成员完全不碰 HCCL)
  否则 subcomm = pyhccl.create_subcomm(ranks) → 包成 CommGroup(handle, ranks)

_use_group(group, src):               # 与 CUDA 路径逐字相同
  进入:把 pyhccl.comm 换成子组句柄;若给了 src,把全局 src 经
        group.ranks.index(src) 重映射为组内编号,同时改写 pyhccl.rank
  退出:恢复 comm 与 rank

broadcast / all_reduce / barrier / all_gather_object:
  统一骨架 = with self._use_group(...) → 调 pyhccl 对应方法 → current_stream().synchronize()
```

#### 4.4.3 源码精读

**差异点一:`new_group` 的参与规则。**

[DistributedHccl.new_group · vllm_hccl.py:312-329](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L312-L329) 中,第 322-323 行 `if self.rank not in ranks: return group` 让**非成员提前返回,根本不调用 HCCL**;只有成员才走到 `create_subcomm`。对照 CUDA 路径 [vllm_nccl.py:225-239](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L225-L239):**所有** rank 都调用 `create_newcomm`,非成员靠 `color=-1`(`NCCL_SPLIT_NOCOLOR`,[vllm_nccl.py:98-104](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L98-L104))拿到 NULL 通信器。也就是说,「过滤谁进组」这件事,CUDA 路径交给库内的 color 机制(因此必须全员参与),HCCL 路径上移到 Python 侧(因此只有成员参与)。两边的编号规则殊途同归:HCCL 的 `ranks.sort()` + `ranks.index(self.rank)`,等价于 NCCL 传 `key=self.rank` 后按 key 升序编号。

汇总成表:

| 维度 | CUDA(`DistributedNccl`) | NPU(`DistributedHccl`) |
| --- | --- | --- |
| 切组接口 | `ncclCommSplit(comm, color, key, &new, config=NULL)` | `HcclCreateSubCommConfig(&comm, rankNum, rankIds, subCommId, subCommRankId, &config, &sub)` |
| 谁必须调用 | 父组**全员**(集体调用,非成员 `color=-1`) | **仅成员**(非成员 Python 侧提前 return) |
| 组内编号来源 | `key` 升序(实传 `self.rank`) | 成员各自 `ranks.index(self.rank)`(前置 `ranks.sort()`) |
| config | 定义了 `NcclConfigT` 但实传 `None` | 定义并逐字段填写 `HcclCommConfig` |
| 区分多次切组 | 集体调用次序(隐式) | `subcomm_id` 计数器(显式,从 1 自增) |
| 补的 ctypes 绑定 | 1 个 | 2 个(另需 `HcclAllGather`) |
| `current_stream` 来源 | vLLM,三级 try/except 兼容 | `vllm_ascend.utils`,直接导入 |
| `sub_groups` 注解 | `dict[int, list[int]]`(实存 CommGroup) | `dict[int, CommGroup]` |

**差异点二:广播语义同构,通信设施不同。**

[broadcast · vllm_hccl.py:295-302](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L295-L302) 与 CUDA 版([vllm_nccl.py:208-215](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L208-L215))结构一致:进 `_use_group(group, src)` 拿到**重映射后的组内 src**,把它交给父类 `broadcast`,再同步流。回想 u3-l4 的「倒置广播源」:`dist.broadcast(src=receiver_rank, group=...)` 里传的是全局 rank,正确性完全依赖 [`_use_group` · vllm_hccl.py:209-229](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L209-L229) 的两步翻译——`group.ranks.index(src)` 翻译广播源、`group.ranks.index(self.rank)` 改写 `pyhccl.rank`(父类 broadcast 内部要用「我自己的组内编号」与 src 比对)。这段代码与 CUDA 版逐字相同,是抽象层之下「平台无关」的部分;平台相关的是底层谁提供 `broadcast`(pyhccl 包装器)与 `current_stream`(vllm_ascend)。另外第 218 行 `assert src in group.ranks` 保证翻译有解。

**其余骨架速览。** [init_process_group · vllm_hccl.py:231-252](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L231-L252) 复用 u5-l3 精读过的 `create_stateless_process_group` 垫片构造 vLLM `StatelessProcessGroup`(TCPStore 会合,不碰 torch 全局进程组),再在其上建裸 HCCL 通信器;[destroy_process_group · vllm_hccl.py:254-269](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L254-L269) 带组则销子组并清 `_sub_groups`,不带组则整体销毁;[all_gather_object · vllm_hccl.py:274-279](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L274-L279) 落到 4.3 的 `all_gather` 重写加 u5-l2 的 `_common_all_gather_object` 两轮 all_gather;[barrier · vllm_hccl.py:304-310](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L304-L310) 与 CUDA 一样用「零张量 all_reduce」模拟。

**消费点。** ps.py 面向抽象接口编程:[ps.py:599](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L599) 的 `dist.new_group(ranks)` 在 P2P 更新前切子组,经模块级函数晚绑定落到本后端的 `new_group`。另注意 NPU 上其实有**两条**通信路径:不传 `--custom-dist` 时走默认 `TorchBackend`,`init_process_group` 的 backend 由 `DeviceManager` 翻译成 `hccl`([ps.py:541-547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541-L547));只有在 vLLM colocated 部署、不能碰 torch 全局进程组时,才需要 `--custom-dist vllm_hccl` 换到本后端。启用入口即 [examples/update.py:185-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L185-L192):`--custom-dist` 参数 → `use_backend` → `importlib` 延迟 import 本模块。

#### 4.4.4 代码实践

1. **实践目标**:在纯 CPU 上验证「HCCL 的 sort+index 编号 ≡ NCCL 的 key 升序编号」,并模拟 `_use_group` 的 src 重映射。
2. **操作步骤**:运行下面这段**示例代码**(非项目源码),同时模拟两种语义:

   ```python
   world = list(range(8))
   ranks = sorted([5, 1, 3])                      # new_group 收到的成员清单

   # NCCL 语义:全员调用,成员 color=0、key=self.rank,按 key 升序编号
   nccl_rank = {r: sorted([x for x in world if x in ranks]).index(r)
                for r in world if r in ranks}

   # HCCL 语义:仅成员调用,ranks.index(self.rank)
   hccl_rank = {r: ranks.index(r) for r in ranks}

   print("NCCL 组内编号:", nccl_rank)
   print("HCCL 组内编号:", hccl_rank)
   assert nccl_rank == hccl_rank                  # 两种语义编号一致

   # _use_group 的 src 重映射:P2P 广播 src=5(全局)
   src_global = 5
   src_local = ranks.index(src_global)
   print(f"全局 src {src_global} → 组内 src {src_local}")   # P2P 更新的 receiver rank
   ```

3. **需要观察的现象**:两份编号字典是否相等;`src_local` 的值。
4. **预期结果**:两份编号均为 `{1: 0, 3: 1, 5: 2}`,断言通过;全局 src 5 映射为组内 src 2。这从数值上印证了 u3-l4 的 P2P 广播(`dist.broadcast(src=receiver_rank, group=...)`)在本后端下为什么正确:`receiver_rank` 是全局编号,`_use_group` 负责翻译。若把第 2 行改成未排序的 `[5,1,3]`(去掉 `sorted`),HCCL 语义会得到 `{5:0, 1:1, 3:2}` 而与 NCCL 语义不再一致——`new_group` 里的 `ranks.sort()` 不可省。

#### 4.4.5 小练习与答案

**练习 1**:同样叫 `new_group`,两条路径对「非成员进程」的要求有何不同?谁把非成员挡在了外面?
**答案**:CUDA 路径要求非成员也必须调用 `ncclCommSplit`(它是集体操作,少一个人就全组挂起),由库内 `color=-1` 机制给非成员发 NULL 通信器;HCCL 路径的非成员在 Python 侧 `if self.rank not in ranks: return` 提前退出,完全不触碰 HCCL。过滤职责一个在 C 库、一个在 Python。

**练习 2**:`_use_group` 为什么在换通信器的同时还要改写 `self.pyhccl.rank`?
**答案**:父类 `PyHcclCommunicator.broadcast` 内部要以「我的编号 vs src」判断数据流向,而它读的是实例属性 `self.rank`(父组全局编号)。子组语境下两者必须都换成组内编号:`src` 由调用方传入并经 `group.ranks.index(src)` 翻译,自己的编号则由 `_use_group` 写入 `pyhccl.rank`。只换 comm 不换 rank,父类会拿全局编号做组内比较,广播方向就错了。退出时 finally 把 comm 与 rank 对称恢复。

**练习 3**:在 NPU 机器上不传 `--custom-dist` 运行 examples/update.py,通信走的是什么?
**答案**:默认 `TorchBackend`,即 `torch.distributed` + backend `hccl`(`DeviceManager` 把 npu 翻译成 hccl 后传给 [ps.py:541-547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541-L547) 的 init)。`DistributedHccl` 只在 vLLM colocated 部署(PS 与 vLLM 同进程、不能占用 torch 全局进程组)时才需要。

### 4.5 昇腾平台适配:版本矩阵、16666 端口与环境变量

#### 4.5.1 概念说明

`docs/npu_start.md` 是项目唯一的昇腾部署文档。它回答三件事:装什么版本(矩阵)、P2P 的 Transfer Engine 怎么装(源码编译)、以及一个独特的部署陷阱——HCCL 与 HIXL 都默认占 16666 端口。对读源码的人来说,版本矩阵还解释了 `vllm_hccl.py` 顶部那些「深度内部」的 import 路径(`vllm_ascend.distributed.device_communicators.pyhccl_wrapper` 等)为什么不用做版本兼容:整个环境被钉死在单一版本组合上。

#### 4.5.2 核心流程

```text
昇腾上跑通 checkpoint-engine:
  1. 按版本矩阵准备软件栈(HDK/CANN/torch_npu/vllm/vllm_ascend)
  2. pip install -e . 安装本项目
  3. (可选 P2P) 源码编译 mooncake Transfer Engine(昇腾无 pip 包)
  4. 写 ranktable 文件:给每张卡分配 device_port(避开 16666)
  5. RANK_TABLE_FILE=<file> VLLM_SERVER_DEV_MODE=1 启动 vLLM serve
     (--worker-extension-cls 注入 VllmColocateWorkerExtension)
  6. torchrun 启动 examples/update.py(命令与 CUDA 平台相同)
  7. 按需设置 ASCEND_RT_VISIBLE_DEVICES(P2P 必查)
```

#### 4.5.3 源码精读

版本矩阵:

[环境要求表 · docs/npu_start.md:9-19](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L9-L19) 规定:Ascend HDK ≥ 25.3.rc1、CANN ≥ 8.3.RC1、Python 3.11、torch 2.7.1、torch_npu 2.7.1、vllm 0.11.0、vllm_ascend 0.11.0rc0。文档说明这是「IPC Buffer 与 Transfer Engine」特性的下限。注意 torch/torch_npu/vllm/vllm_ascend 四项是**精确等于**而非「≥」——这套组合是验证过的钉死版本;u5-l3 里那套三级 try/except 的 vLLM 版本兼容在 HCCL 侧不需要(见 4.2.3),代价就是版本矩阵必须钉死。

安装与 P2P:

[安装说明 · docs/npu_start.md:21-29](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L21-L29) 说明基础包 `pip install -e .` 即可;P2P 所需的 Transfer Engine 在昇腾上**没有 pip 包,必须源码编译**,并指向 Mooncake 的 ascend_direct_transport 文档。这对应 u5-l1 的映射:npu 的 `transfer_engine_protocol` 是 `ascend_direct`(不是 CUDA 的 rdma/efa)。

端口陷阱与 ranktable:

[HCCL 默认端口 16666 的问题 · docs/npu_start.md:32-75](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L32-L75) 说明:HCCL 默认用 16666 建链,单机多进程时会互抢;更麻烦的是 Transfer Engine 底层的 HIXL 也默认 16666 且**没有接口可改**。因此必须通过 ranktable 文件给每张卡显式分配 `device_port`(文档示例选了 23333,并注明「Choose an available port other than 16666」),ranktable 内含 server_list/device_id/device_ip/device_port/rank_id 的完整拓扑。

启动命令:

[vLLM 启动命令 · docs/npu_start.md:75-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L75-L82) 用 `RANK_TABLE_FILE=ranktable.json VLLM_SERVER_DEV_MODE=1 python3 -m vllm.entrypoints.openai.api_server ...` 启动,带上 `--load-format dummy` 与 `--worker-extension-cls checkpoint_engine.worker.VllmColocateWorkerExtension`(u4-l2 讲过这两个参数);[checkpoint-engine 侧命令 · docs/npu_start.md:84-87](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L84-L87) 与其他平台完全相同(torchrun 起 examples/update.py,示例用 `--update-method all`),这正是「平台差异被 distributed 层吃掉」的直观体现。

环境变量注意事项:

[Important Notes · docs/npu_start.md:89-91](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L89-L91) 只有一条:按实际在用的 NPU 设置 `ASCEND_RT_VISIBLE_DEVICES`,否则 P2P 模式下「host quantity validation」会失败——u3-l3 讲过 gather_metas 会收集 `_all_hosts`,设备可见性不一致会让校验对不上账。

#### 4.5.4 代码实践

1. **实践目标**:把文档知识变成可执行的排错决策树,并检验 ranktable 配置的常见错误。
2. **操作步骤**:
   - 阅读 [docs/npu_start.md](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md) 全文,然后回答三个场景题(见下面练习,先自己作答再对照)。
   - 检查文档示例 ranktable:把 [docs/npu_start.md:37-73](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/docs/npu_start.md#L37-L73) 的 JSON 中某张卡的 `device_port` 改成 `"16666"`,写出这是否可行及原因。
3. **需要观察的现象**:你能否不查资料说出每个场景的第一排查点。
4. **预期结果**:三个场景分别指向 16666 端口冲突、`ASCEND_RT_VISIBLE_DEVICES` 缺失、Transfer Engine 未装(P2P 不可用或需退回 Broadcast)。ranktable 里把 `device_port` 写成 16666 **不可行**:它与 HCCL/HIXL 的默认端口撞车,正是 ranktable 存在的意义所在。实际部署验证需昇腾环境,待本地验证。

#### 4.5.5 小练习与答案

**练习 1**:为什么 ranktable 里的 `device_port` 绝不能填 16666?
**答案**:HCCL 建链默认占 16666,Transfer Engine 底层的 HIXL 也默认占 16666 且无法通过接口修改。ranktable 的作用就是把每张卡的通信端口显式分配到其他可用端口,避免这两个隐形占坑者互抢或与别的进程冲突;填 16666 等于把分配端口的动作变成原地踏步。

**练习 2**:P2P 模式报「host quantity validation」失败,第一个该检查的环境变量是什么?
**答案**:`ASCEND_RT_VISIBLE_DEVICES`。不按实际在用的 NPU 设置它,各进程看到的设备集合不一致,gather_metas 阶段(u3-l3)收集到的 host/设备账目对不上,P2P 校验就会失败。

**练习 3**:为什么 `vllm_hccl.py` 不像 `vllm_nccl.py` 那样对 `current_stream` 的 import 做三级兼容?
**答案**:CUDA 路径要同时兼容多个 vLLM 版本(`current_stream` 的模块位置变过);HCCL 路径的版本矩阵被 docs 钉死在 vllm 0.11.0 + vllm_ascend 0.11.0rc0 单一组合上,import 位置固定,不需要兼容层。代价是升级版本矩阵时要人工复核所有深度内部 import 路径。

## 5. 综合实践

**任务:在纯 CPU 上构建一个「mini HCCL 子组模拟器」,把本讲四个模块串起来。**

1. **实践目标**:用一个脚本同时验证 4.1 的结构体推断、4.3/4.4 的编号与重映射算法,做到「读完源码 → 算法可以脱离硬件复现」。
2. **操作步骤**:
   - 第一步,复刻结构体:把 4.1.4 的 `HcclCommConfig` 复制进脚本,打印 `ctypes.sizeof`,与字面量 312 对照,并按 4.1.3 的偏移推算解释差值 16 从哪来(提示:末尾 4 个字段共 4+4+4+1=13 字节,再加 3 字节尾部对齐)。
   - 第二步,模拟切组:用 4.3.4 的代码骨架,让 world = `[0..7]`、成员 `ranks = [1, 3, 5]`,为每个成员生成 `(rankIds, rankNum, subCommId, subCommRankId)` 四元组,`subCommId` 从 1 起对每次 `create_subcomm` 自增(模拟两次切组,观察第二次拿到 2)。
   - 第三步,模拟广播:实现 `_use_group` 的核心两行(`active_src = ranks.index(src)`、`self.rank 重映射`),对全局 src=5 验证翻译结果;再用 4.4.4 的对照代码断言与 NCCL key 语义编号一致。
   - 第四步(可选,需昇腾环境):按 `docs/npu_start.md` 的流程在真实环境跑 `--update-method all`,记录 `ctypes.sizeof` 的实际输出、`HcclCreateSubCommConfig` 是否接受 `size=312`、以及 4.1.3 两处拼写错位关键字在你的 Python 版本上是否抛 TypeError。
3. **需要观察的现象**:sizeof 输出与 312 的关系;两次切组的 subcommId;三处断言全部通过或明确报告失败点。
4. **预期结果**:sizeof 打印 328(手工推算值,待本地验证);两次切组 subcommId 分别为 1、2;全局 src 5 → 组内 src 2;NCCL/HCCL 编号断言通过。第四步为环境依赖项,全部待本地验证。

## 6. 本讲小结

- `HcclCommConfig` 是 NPU 路径独有的「必答题」:18 字段 ctypes 复刻,`size`/`magic_word`/`version` 三件套做 ABI 版本协商,大量 \( \texttt{0xFFFFFFFF} \)/-1 哨兵表示「用库默认」;源码中 `size=312` 与复刻结构体的手工推算大小 328 不一致,另有两处字段名拼写错位,三者都值得在接入新环境时优先验证(本讲已给出纯 CPU 验证脚本)。
- 扩展 `HCCLLibrary` 的手法与 CUDA 路径完全同构:import 期向 `exported_functions` 追加 `HcclAllGather`、`HcclCreateSubCommConfig` 两条声明并挂方法;`use_backend("vllm_hccl")` 的调用时序保证 patch 先于库实例化。
- `create_subcomm` 的核心参数:`rankIds`(排序后的成员清单,全员一致)、`subCommRankId`(每个成员自己用 `ranks.index(self.rank)` 算出)、`subCommId`(自增计数器,显式区分同父多次切组)。
- NPU 与 CUDA 在 `new_group` 上的最大差异是参与规则:NCCL 全员集体调用、由 color/key 在库内过滤与编号;HCCL 仅成员调用、过滤与编号都在 Python 侧。编号结果两种语义等价(前提是 `ranks.sort()`)。
- 广播语义跨平台同构:P2P 更新里 `dist.broadcast(src=receiver_rank, group=...)` 的正确性都依赖 `_use_group` 做「换通信器 + 全局 rank 到组内 rank」的双重翻译,这段代码在两个后端中逐字相同。
- 昇腾部署三件套:钉死的版本矩阵(HDK ≥25.3.rc1 / CANN ≥8.3.RC1 / vllm 0.11.0 / vllm_ascend 0.11.0rc0 等)、必须源码编译的 Transfer Engine(ascend_direct 协议)、以及绕开 16666 默认端口的 ranktable 配置与 `ASCEND_RT_VISIBLE_DEVICES`。

## 7. 下一步学习建议

本讲结束后,「通信后端」三部曲(u5-l2 抽象层、u5-l3 NCCL、u5-l4 HCCL)就完整了。建议:

1. 下一讲 **u5-l5(P2PStore 与 RDMA 设备发现)**:离开「控制面通信」,进入 P2P 数据面的 mooncake Transfer Engine——其中昇腾专用的 `ascend_direct` 协议与本讲的 HIXL/16666 端口问题会在那里再次出现。
2. 回读对照:把 `vllm_hccl.py` 与 `vllm_nccl.py` 并排通读一遍,亲手列出所有差异行,检验自己是否已被本讲小结覆盖。
3. 若你有昇腾环境,按 `docs/npu_start.md` 跑通一次 `--update-method all`,并完成 5 综合实践第四步的三个待验证项——这些观察值对社区是有价值的信息。
