# u5-l3 vLLM NCCL 后端:ncclCommSplit 子通信组

## 1. 本讲目标

本讲是分布式后端三部曲的第二讲(上一讲 u5-l2 精读了 `distributed/base.py` 抽象层,下一讲 u5-l4 精读 NPU/HCCL 后端)。读完本讲,你应该能够:

1. 说清楚为什么 colocated 部署下不能用 `TorchBackend`(即 `torch.distributed.init_process_group`),以及 vLLM 的 `StatelessProcessGroup` + `PyNcclCommunicator` 如何绕开这个限制。
2. 读懂 `create_stateless_process_group` 用 `inspect.signature` 做跨 vLLM 版本构造签名兼容的手法。
3. 理解用 ctypes 给 vLLM 的 `NCCLLibrary` 动态补上 `ncclCommSplit` 函数声明的 monkey patch 做法。
4. 掌握 `ncclCommSplit` 的 color/key 语义:谁进子通信组、进组后排在第几号。
5. 掌握 `_use_group` 上下文管理器如何完成「换通信器 + 全局 rank 到子组 rank 的重映射」,并理解它为什么是 P2P 更新中 `dist.broadcast(src=receiver_rank, group=...)` 能正确工作的前提。

本讲的所有实践任务都可以在纯 CPU 环境完成(不需要 GPU、NCCL 和 vLLM)。

## 2. 前置知识

### 2.1 为什么 TorchBackend 不够:colocated 下的进程组冲突

回顾 u5-l2:`Distributed` 抽象基类只抽象「需要活通信器」的 8 个操作,默认实现 `TorchBackend` 直接透传给 `torch.distributed`。在**训练进程**里这样做没问题;但在 **colocated 部署**(u1-l1)下,`ParameterServer` 与 vLLM 推理引擎跑在**同一个进程**里,而 vLLM 启动时已经初始化了自己的全局 torch 进程组。`torch.distributed.init_process_group` 的全局状态一个进程只有一份,PS 再去调用它就会与 vLLM 冲突。

因此需要一种「不碰 torch 全局进程组」的 NCCL 通信方式:会合(rendezvous)走 TCPStore,数据面直接用裸 NCCL 通信器。这正是本讲后端 `DistributedNccl` 的设计出发点。

### 2.2 vLLM 的两件武器

- **`StatelessProcessGroup`**(来自 `vllm.distributed.utils`):一个轻量「进程组替身」。它不注册任何 torch 全局状态,只用传入的 `store`(TCPStore)做会合与键值交换,并提供 `store`/`rank`/`world_size` 等属性。它不是真的通信器,只是一张「身份证明」。
- **`PyNcclCommunicator`**(来自 `vllm.distributed.device_communicators.pynccl`):接受一个 `StatelessProcessGroup` 和一个 `torch.device`,内部用 store 交换 NCCL 唯一 ID,然后调用 `ncclCommInitRank` 建立**裸 NCCL 通信器**,并在其上直接发起 `all_reduce`/`broadcast` 等集合通信。它的 `comm`、`rank` 都是普通 Python 属性——这一点对本讲的 `_use_group` 至关重要。

### 2.3 NCCL 通信器与 ncclCommSplit 的 color/key 语义

NCCL 的每个通信器(comm)内部都有自己的 rank 编号,从 0 连续编号。`ncclCommSplit` 是 NCCL 官方的「从父通信组切子通信组」接口,C 签名如下(即源码注释中的签名):

```c
ncclResult_t ncclCommSplit(
    ncclComm_t comm, int color, int key,
    ncclComm_t *newcomm, NcclConfigT *config);
```

语义要点:

- **collective 调用**:父通信组的所有进程必须都调用它,哪怕不想加入子组。
- **color**:分组的「颜色」。color 相同(且 ≥ 0)的进程进入同一个新通信器;`color = -1`(即 `NCCL_SPLIT_NOCOLOR`)表示「我不加入」,此时输出参数 `newcomm` 被置为 NULL。
- **key**:同组内的排序键。新通信器内部按 key 升序重新编号 0..n-1。
- **config**:传 NULL 表示使用默认配置。

### 2.4 ctypes 与 monkey patch

`NCCLLibrary` 是 vLLM 用 ctypes 包装 libnccl.so 的类:它持有一张 `exported_functions` 函数声明表(每项声明函数名、返回类型、参数类型),实例化时据此生成 `_funcs` 字典并加载动态库。因为 `exported_functions` 只是一个普通的类属性列表,我们可以在运行时往里**追加**新声明、再往类上**挂**一个新方法——这就是本讲看到的 monkey patch,不需要改 vLLM 一行源码。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `checkpoint_engine/distributed/vllm_nccl.py` | 239 | 本讲主角:ctypes 扩展、`PyNcclCommunicatorEx`、`DistributedNccl` 后端 |
| `checkpoint_engine/distributed/vllm_compat.py` | 16 | 跨 vLLM 版本的 `StatelessProcessGroup` 构造兼容层 |
| `tests/test_vllm_compat.py` | 37 | 兼容层的纯 CPU 单元测试(两个替身类) |
| `checkpoint_engine/distributed/base.py` | 293 | 承接 u5-l2:`Distributed` ABC、`CommGroup`、`use_backend`、`_common_all_gather_object` |
| `checkpoint_engine/ps.py`(消费点) | — | `update` 主流程如何调用 `new_group`/`broadcast`/`all_reduce` |
| `examples/update.py`(入口) | — | `--custom-dist vllm_nccl` 如何切换到本后端 |

一句话定位:`vllm_nccl.py` = 「vLLM 通信设施 + 一段 ctypes 扩展 + 一个 `Distributed` 适配器」;`vllm_compat.py` 则是它依赖的一枚小小的版本兼容垫片。

## 4. 核心概念与源码讲解

### 4.1 create_stateless_process_group:跨 vLLM 版本的构造兼容

#### 4.1.1 概念说明

vLLM 的 `StatelessProcessGroup` 构造函数签名在不同版本间发生过变化:新版本增加了一个 `socket` 参数。checkpoint-engine 想同时兼容新旧 vLLM,又不想在各调用点写 `try/except TypeError`,于是把「怎么构造」收敛到一个 16 行的兼容函数里。这个文件是 HEAD 提交 `d1de07b`(Support current vLLM stateless process groups (#102))引入的,也是项目里最新的源码之一。

#### 4.1.2 核心流程

```text
create_stateless_process_group(group_cls, rank, world_size, store):
    基本参数 = {rank, world_size, store}
    用 inspect.signature 查 group_cls.__init__ 的形参表
    如果形参表里有 "socket":
        额外传 socket=None      # 让 vLLM 自己选默认 socket
    返回 group_cls(**kwargs)
```

关键在于:**探测的是构造函数的形参,而不是尝试调用后捕获异常**。前者一次 `inspect` 调用即可,无副作用,也不依赖 vLLM 抛出的异常类型是否稳定。

#### 4.1.3 源码精读

整个文件只有这一个函数:

[checkpoint_engine/distributed/vllm_compat.py:5-16](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_compat.py#L5-L16)

- `group_cls` 参数故意不写死为 `StatelessProcessGroup`,而是接收任意类——这让测试可以用替身类注入(见下),也让该垫片与具体 vLLM 版本解耦。
- 第 14 行 `if "socket" in inspect.signature(group_cls).parameters:` 是全部兼容逻辑所在:新签名有 `socket` 就补一个 `None`,旧签名没有就维持三参数构造。

对应的测试用两个替身类分别模拟「现行签名」与「历史签名」:

[tests/test_vllm_compat.py:4-11](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_vllm_compat.py#L4-L11) 定义 `CurrentProcessGroup(rank, world_size, store)` 与 `LegacyProcessGroup(rank, world_size, store, socket)` 两个替身;
[tests/test_vllm_compat.py:14-37](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_vllm_compat.py#L14-L37) 断言:对前者按三参构造,对后者自动补 `socket=None` 构造。该测试文件没有 `gpu` marker,可在纯 CPU 环境运行。

顺带一提,`vllm_nccl.py` 顶部对 `current_stream` 的导入也做了同样的版本兼容(先试 `vllm.utils.torch_utils`,再退回 `vllm.utils`,均失败才报错),见 [checkpoint_engine/distributed/vllm_nccl.py:20-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L20-L28)。版本兼容是贯穿这个后端的一条暗线。

#### 4.1.4 代码实践

1. **实践目标**:验证兼容层的行为可被纯 CPU 测试覆盖,并亲手体验 `inspect.signature` 探测。
2. **操作步骤**:
   - 在仓库根目录运行:`pytest tests/test_vllm_compat.py -v`(若提示缺 pytest,先 `pip install pytest`)。
   - 再在 Python 交互环境中运行下面这段**示例代码**(非项目源码):

   ```python
   import inspect

   class CurrentPG:                    # 模拟新/现行 vLLM 签名
       def __init__(self, rank, world_size, store, socket): ...

   class LegacyPG:                     # 模拟旧 vLLM 签名
       def __init__(self, rank, world_size, store): ...

   for cls in (CurrentPG, LegacyPG):
       print(cls.__name__, "socket" in inspect.signature(cls).parameters)
   ```

3. **需要观察的现象**:pytest 输出中 `test_current_vllm_process_group_signature` 与 `test_legacy_vllm_process_group_signature` 两条 PASSED;示例代码打印 `CurrentPG True`、`LegacyPG False`。
4. **预期结果**:兼容层完全由标准库 `inspect` 驱动,不依赖 vLLM 可导入。
5. 上述运行结果**待本地验证**(本讲义编写时未在当前环境执行)。

#### 4.1.5 小练习与答案

- **练习 1**:为什么 `create_stateless_process_group` 用 `inspect` 探测形参,而不是 `try: cls(...); except TypeError: cls(..., socket=None)`?
  **答案**:`try/except TypeError` 无法区分「缺 socket 参数」和「构造函数内部自己抛的 TypeError」,也不适用于 socket 是关键字-only 参数等变化;`inspect.signature` 只读签名、无副作用、判断精确,且对测试替身同样有效。
- **练习 2**:替身测试(`tests/test_vllm_compat.py`)为什么不需要安装 vLLM?
  **答案**:`create_stateless_process_group` 只依赖标准库 `inspect`,`group_cls` 由调用方注入;测试把 `StatelessProcessGroup` 换成了本地定义的两个替身类,于是 vLLM 被彻底排除在被测依赖之外。
- **练习 3**:`socket=None` 传给新版 vLLM 意味着什么?
  **答案**:不主动指定 socket,把选择权交还给 vLLM 的默认逻辑(由 vLLM 内部决定),从而保持与旧签名行为一致的三参用法。

### 4.2 ctypes 扩展 NCCLLibrary:给 vLLM 补上 ncclCommSplit

#### 4.2.1 概念说明

vLLM 的 `PyNcclCommunicator` 没有暴露「从现有通信器切子通信器」的方法——它包装的 `NCCLLibrary.exported_functions` 声明表里没有 `ncclCommSplit`。而 P2P 更新(u3-l4/u5-l6)恰恰需要子通信组:只让 `ranks` 列表里的接收端参与集合通信,不惊动全量组。本模块的做法是:在 import 时用 ctypes 写出 `ncclCommSplit` 的函数声明,追加进 vLLM 的声明表,并给 `NCCLLibrary` 类挂一个同名方法。**不改 vLLM 源码,只做运行时扩展**。

#### 4.2.2 核心流程

```text
import vllm_nccl 模块时(一次性执行):
1. 定义 NcclConfigT 结构体:逐字段镜像 NCCL 的 ncclConfig_t
2. 构造 nccl_extended_functions = [Function("ncclCommSplit", 返回类型, 参数类型列表)]
3. NCCLLibrary.exported_functions = 原表 + 扩展表      # 类属性层面追加
4. NCCLLibrary.ncclCommSplit = nccl_comm_split          # 挂一个未绑定方法

之后任何 NCCLLibrary 实例化时:
5. 实例按(已扩展的)exported_functions 生成 _funcs["ncclCommSplit"]
6. self.nccl.ncclCommSplit(comm, color, key) 即可调用动态库里的真函数
```

时序上为什么安全?`NCCLLibrary` 是延迟实例化的(单例),真正的实例化发生在 `PyNcclCommunicator` 构造时;而本模块的 patch 在 import 时立即执行。调用链上,`use_backend("vllm_nccl")` 发生在 `ParameterServer` 构造之前([examples/update.py:191](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L191)),通信器构造又发生在 `init_process_group`(即 `update` 阶段),所以 patch 必然先于任何 `NCCLLibrary` 实例化。

#### 4.2.3 源码精读

结构体定义,逐字段镜像 NCCL 官方的 `ncclConfig_t`(size 在前、magic/version 跟上,再是各种调优开关):

[checkpoint_engine/distributed/vllm_nccl.py:31-52](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L31-L52)

函数声明(注释里就是 C 签名),参数类型依次是 `ncclComm_t, int(color), int(key), POINTER(ncclComm_t), POINTER(NcclConfigT)`:

[checkpoint_engine/distributed/vllm_nccl.py:55-71](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L55-L71)

包装方法:先造一个空的 `ncclComm_t` 作为输出参数,`ctypes.byref` 取址传给 NCCL,由 NCCL 填入新通信器句柄;`NCCL_CHECK` 是 vLLM `NCCLLibrary` 现成的错误检查机制;最后一个参数恒传 `None`(config 为 NULL = 用默认配置):

[checkpoint_engine/distributed/vllm_nccl.py:74-83](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L74-L83)

两行 monkey patch,分别扩展声明表和挂方法:

[checkpoint_engine/distributed/vllm_nccl.py:86-88](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L86-L88)

两个值得注意的细节:

- `NcclConfigT` 定义了却从不构造实例——它只是把 `POINTER(NcclConfigT)` 这个参数类型补全。既然恒传 NULL,当前不存在「结构体布局与所链 NCCL 版本不一致」的风险;但如果未来要传非空 config,字段表就必须与运行时 NCCL 版本的 `ncclConfig_t` 严格一致(这类结构体通常靠首字段 `size` 做版本协商)。
- `nccl_comm_split` 的第一个形参名是 `self`,因为它将被赋给 `NCCLLibrary` 类当方法,`self` 就是 `NCCLLibrary` 实例(即 `PyNcclCommunicator` 的 `self.nccl`)。

#### 4.2.4 代码实践

1. **实践目标**:理解「ctypes 空指针是假值」这一事实——它是后面 `create_newcomm` 判断「本进程是否加入子组」的机制基础。
2. **操作步骤**:在 Python 交互环境运行下面这段**示例代码**(非项目源码):

   ```python
   import ctypes

   empty = ctypes.c_void_p()          # 对应 ncclComm_t 的初始状态
   filled = ctypes.c_void_p(0x7f0000000000)

   print(bool(empty), empty.value)    # ?
   print(bool(filled), filled.value)  # ?
   ```

3. **需要观察的现象**:第一行打印 `False None`,第二行打印 `True 139637976727552`(具体数值不定)。
4. **预期结果**:ctypes 的简单类型按值判真假——NULL 指针即假值。这解释了 `new_group` 中 `if newcomm:`([vllm_nccl.py:236](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L236))为什么能直接当「加入成功与否」的判断。运行结果**待本地验证**。
5. 如果想进一步观察 patch 结构,可在安装了 vLLM 的环境里 `import checkpoint_engine.distributed.vllm_nccl` 后查看 `NCCLLibrary.exported_functions[-1].name`(待本地验证,依赖 vLLM 环境)。

#### 4.2.5 小练习与答案

- **练习 1**:为什么追加声明要放在 `NCCLLibrary` **实例化之前**?
  **答案**:`_funcs` 字典是实例化时按当时的 `exported_functions` 建立的;晚于实例化的追加不会进入已建好的 `_funcs`,调用会抛 `KeyError`。
- **练习 2**:`nccl_comm_split` 里为什么最后传 `None` 而不是一个全零的 `NcclConfigT`?
  **答案**:NCCL 约定 config 指针为 NULL 表示使用默认配置;传一个手写的结构体反而要求其内存布局与运行时 NCCL 版本完全匹配,徒增风险。
- **练习 3**:这种 monkey patch 的代价是什么?
  **答案**:它同时耦合了 vLLM 的私有实现细节(`exported_functions`、`_funcs`、`NCCL_CHECK`)与 NCCL 的 ABI;任一侧变动都可能静默失效,所以需要像 `current_stream` 那样的版本兼容意识。

### 4.3 PyNcclCommunicatorEx:create_newcomm 的 color/key 语义

#### 4.3.1 概念说明

`PyNcclCommunicatorEx` 继承 vLLM 的 `PyNcclCommunicator`,只补了两个方法:`destroy_comm`(可以销毁指定子通信器,而不只是默认通信器)与 `create_newcomm`(把「rank 列表」翻译成一次全体参与的 `ncclCommSplit`)。它是「torch 的 `new_group(ranks)`」在裸 NCCL 世界里的对应物:torch 会为组外进程返回一个「非成员」标记,而这里组外进程拿到的就是 NULL 通信器。

#### 4.3.2 核心流程

```text
new_group(ranks) 被调用时,父组的每个进程都执行 create_newcomm(ranks):
    若 self.rank 在 ranks 中: color = 0(加入子组)
    否则:                    color = -1(NCCL_SPLIT_NOCOLOR,不加入)
    key = self.rank
    newcomm = ncclCommSplit(父comm, color, key)
    组外进程:newcomm 为 NULL → 返回假值
    组内进程:newcomm 有效   → 包成 CommGroup(handle, ranks) 登记
```

key 取 `self.rank` 的直接后果:**子组内排名 = 按 ranks 升序排列后的下标**。调用方 `new_group` 会先 `ranks.sort()`([vllm_nccl.py:232](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L232)),保证 `ranks` 列表顺序与 NCCL 内部编号顺序一致——于是「全局 rank ↔ 子组 rank」的翻译就是一个 `list.index()`(见 4.4)。

#### 4.3.3 源码精读

子类与两个新方法:

[checkpoint_engine/distributed/vllm_nccl.py:91-96](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L91-L96) `destroy_comm`:带参数就销毁那个子通信器,不带参数销毁默认通信器 `self.comm`——对应 `destroy_process_group` 的两种语义(销子组 vs 全部拆掉)。

[checkpoint_engine/distributed/vllm_nccl.py:98-104](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L98-L104) `create_newcomm`:`color = 0` / `color = -1`(注释标明即 `NCCL_SPLIT_NOCOLOR`)的二分,加上 `key = self.rank`,一次 split 完成分组与排序。

`new_group` 的组装逻辑:

[checkpoint_engine/distributed/vllm_nccl.py:225-239](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L225-L239)

- `ranks` 为空时取全体 `[0, world_size)`,与 ps.py 侧 `dist.new_group(ranks) if ranks else None` 的用法呼应(P2P 才建组,Broadcast 不建)。
- `ranks.sort()` 保证列表序 = key 升序 = 子组内编号序。
- 只有拿到非 NULL 通信器的进程才构造 `CommGroup(newcomm.value, ranks)` 并登记进 `self.sub_groups`;组外进程返回 `None`。
- `CommGroup` 定义在 [checkpoint_engine/distributed/base.py:16-27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L16-L27),只有两个属性:`handle`(NCCL 通信器地址,整数)与 `ranks`(成员全局 rank 列表)。它是 u5-l2 提到的联合类型 `DistributedProcessGroup = torch 进程组 | CommGroup` 的第二个分支——`DistributedNccl` 路径下,ps.py 拿到的「进程组」永远是它。

用一个 6 卡例子把 color/key 语义落成表格(ranks 传入 `[4, 0, 2]`,排序后 `[0, 2, 4]`):

| 全局 rank | color | key | 结果 | 子组内新 rank |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 进入子组 | 0 |
| 1 | -1 | 1 | NULL,不加入 | — |
| 2 | 0 | 2 | 进入子组 | 1 |
| 3 | -1 | 3 | NULL,不加入 | — |
| 4 | 0 | 4 | 进入子组 | 2 |
| 5 | -1 | 5 | NULL,不加入 | — |

#### 4.3.4 代码实践

1. **实践目标**:不依赖 GPU/NCCL,手工推演 `create_newcomm` 的 color/key 行为,把「调用方视角」翻译成「NCCL 视角」。
2. **操作步骤**:假设 `world_size = 8`,`update(ranks=[6, 2, 0])`。请先在纸上填下面表格的「color / key / 是否加入 / 子组内新 rank」四列,再用下面**示例代码**(非项目源码)校验你的推演:

   ```python
   ranks_in = [6, 2, 0]
   ranks = sorted(ranks_in)                  # new_group 里的 ranks.sort()

   for my_rank in range(8):
       color = 0 if my_rank in ranks else -1
       if color == 0:
           inside_rank = ranks.index(my_rank)  # key 升序 = 排序后下标
           print(f"rank{my_rank}: color=0 key={my_rank} -> 子组rank {inside_rank}")
       else:
           print(f"rank{my_rank}: color=-1 key={my_rank} -> NULL, 不加入")
   ```

3. **需要观察的现象**:rank 0/2/6 分别得到子组 rank 0/1/2,其余 5 个 rank 打印「NULL, 不加入」。
4. **预期结果**:与表格推演一致;并且 `ranks_in` 原始顺序 `[6,2,0]` 不影响结果(排序先行)。
5. 运行结果**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**:如果去掉 `new_group` 里的 `ranks.sort()`,会破坏什么?
  **答案**:NCCL 仍按 key(即全局 rank)升序给子组编号,但 `group.ranks` 列表保持乱序;此后 `group.ranks.index(x)` 的翻译就会错位,广播根与数据实际持有者对不上,可能静默广播错误数据或挂死。
- **练习 2**:为什么 `ncclCommSplit` 必须由**父组所有进程**调用,包括不想加入的?
  **答案**:它是 collective 调用,内部要在父通信组上交换分组信息;漏掉任何一个成员,其余进程的调用都无法完成(挂起),这与 torch 的 `new_group` 要求全体参与的道理相同。
- **练习 3**:`DistributedNccl.__init__` 里 `self.sub_groups` 的类型注解是 `dict[int, list[int]]`,而 `new_group` 实际存进去的是什么?有什么问题?
  **答案**:实际存的是 `CommGroup` 对象(`self.sub_groups[newcomm.value] = group`),注解与真实值类型不符。它不影响运行(Python 不强制注解),但会误导读者与类型检查器,是一个可改进的注解瑕疵——阅读源码时要警惕「注解也会撒谎」。

### 4.4 _use_group:子通信组切换与 rank 重映射

#### 4.4.1 概念说明

ps.py 的广播调用是 [ps.py:890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L890) 的 `dist.broadcast(buffer_b, src=receiver_rank, group=ranks_group)`——`src` 是**全局 rank**。但 NCCL 的广播根编号是**通信器内部编号**,而且 `PyNcclCommunicator` 当前绑定的可能是默认通信器而不是子通信器。`_use_group` 就是这两层落差的翻译器:进入时把 `pynccl` 的通信器与 rank 临时切换到子组视角,退出时恢复。它是本后端里最精巧的 20 行。

能这样写的前提(见 2.2):`PyNcclCommunicator` 的 `comm` 与 `rank` 只是普通实例属性,每次集合通信时读取;改写它们就等于改写了「我是谁、用哪条线」。

#### 4.4.2 核心流程

```text
_use_group(group, src):                 # contextmanager
    active_src = src
    若 group 非空:
        断言 group.handle 已登记于 sub_groups
        pynccl.comm = c_void_p(group.handle)        # ① 换通信器
        若 src 非空:
            断言 src ∈ group.ranks
            active_src = group.ranks.index(src)     # ② 全局 src → 子组 src
            pynccl.rank = group.ranks.index(self.rank)  # ③ 自己的全局 rank → 子组 rank
    yield active_src                                  # 集合通信在此发生
    finally:
        若 group 非空:
            pynccl.comm = self.comm                   # 恢复默认通信器
            若 src 非空: pynccl.rank = self.rank      # 恢复全局 rank
```

注意 finally 里「恢复 rank」以 `src is not None` 为条件——只有改写过才恢复,与进入时的分支严格对称。

#### 4.4.3 源码精读

[checkpoint_engine/distributed/vllm_nccl.py:122-142](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L122-L142)

- 第 127-128 行:把整数 handle 包回 `ctypes.c_void_p` 再赋给 `self.pynccl.comm`——`CommGroup` 存的是裸整数,`PyNcclCommunicator` 要的是 ctypes 指针对象,进出都要转一次(反向转换在 `new_group` 的 `newcomm.value`)。
- 第 130-134 行:两处断言先兜底(子组必须登记过、src 必须在组内),随后做 ②③ 两个 `index()` 翻译。这就是 u3-l6 讲过的「倒置广播源」在子组场景下的完整翻译:数据持有者 receiver_rank 反当广播根。
- 第 136-142 行:`yield` 出 `active_src`,`broadcast` 方法用它发起 NCCL 广播;`finally` 无条件恢复默认状态——即使集合通信抛异常也不会把「换过的身份」泄漏到下一次调用。

消费点对照(为什么必须有 ②):

[checkpoint_engine/ps.py:890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L890) P2P 更新中以全局 `receiver_rank` 为 src、带 `group=ranks_group` 广播;
[checkpoint_engine/ps.py:802](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L802) 与 [ps.py:898](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L898) 分别是带组的 `barrier` 与 `all_reduce`(ret_code 投票,见 u3-l4)——它们都经 `_use_group` 换到子通信器上执行。

#### 4.4.4 代码实践

1. **实践目标**:用纯 Python 复刻 `_use_group` 的翻译逻辑,验证「换通信器 + 双 index 翻译 + finally 恢复」三件事。
2. **操作步骤**:承接 4.3.4 的设定(`world_size=8, ranks=[0,2,6]`),运行下面**示例代码**(非项目源码,用普通对象模拟 pynccl):

   ```python
   from contextlib import contextmanager

   class FakePynccl:                     # 只模拟被改写的两个属性
       def __init__(self, comm, rank):
           self.comm, self.rank = comm, rank

   class Group:                          # 模拟 CommGroup
       def __init__(self, handle, ranks):
           self.handle, self.ranks = handle, ranks

   class NcclBackend:
       def __init__(self, rank):
           self.rank, self.comm = rank, "default_comm"
           self.pynccl = FakePynccl(self.comm, rank)
           self.sub_groups = {"sub_comm": Group("sub_comm", [0, 2, 6])}

       @contextmanager
       def _use_group(self, group, src=None):
           active_src = src
           if group:
               assert group.handle in self.sub_groups
               self.pynccl.comm = group.handle
               if src is not None:
                   assert src in group.ranks
                   active_src = group.ranks.index(src)
                   self.pynccl.rank = group.ranks.index(self.rank)
           try:
               yield active_src
           finally:
               if group:
                   self.pynccl.comm = self.comm
                   if src is not None:
                       self.pynccl.rank = self.rank

   be = NcclBackend(rank=2)              # 我是全局 rank 2,在子组内
   with be._use_group(be.sub_groups["sub_comm"], src=6) as local_src:
       print("广播根(子组编号):", local_src,
             "| 我的子组rank:", be.pynccl.rank,
             "| 当前comm:", be.pynccl.comm)
   print("退出后 -> comm:", be.pynccl.comm, "| rank:", be.pynccl.rank)
   ```

3. **需要观察的现象**:with 块内打印 `广播根(子组编号): 2 | 我的子组rank: 1 | 当前comm: sub_comm`;退出后打印 `default_comm` 与 `2`。
4. **预期结果**:全局 src=6 被翻译成子组编号 2(ranks=[0,2,6] 中 6 的下标),自己的 rank 2 被翻译成 1;退出后身份完全恢复。再试着把 `src=5`(不在组内)传入,应触发断言。运行结果**待本地验证**。
5. 把 `be = NcclBackend(rank=5)` 改一下再跑 `src=6`:注意这模拟的是「组外进程」误入路径——真实代码里组外进程根本拿不到 `CommGroup`(`new_group` 返回 None),不会走到这里。

#### 4.4.5 小练习与答案

- **练习 1**:如果 `finally` 里忘记恢复 `self.pynccl.comm`,后续会发生什么?
  **答案**:下一次不带 group 的集合通信(例如下一轮 Broadcast 更新、或 gather 阶段)会继续用已经(或即将)被 `destroy_process_group(group)` 销毁的子通信器句柄,轻则 NCCL 报 invalid handle/unknown error,重则悬垂指针——恢复不是「整洁」,是正确性。
- **练习 2**:为什么恢复 `pynccl.rank` 的条件是 `src is not None`,而不是「只要 group 非空」?
  **答案**:与进入分支对称——只有 `src` 非空时才改写过 `rank`;`src` 为空时只换过通信器。按改写与否恢复,避免多余赋值掩盖真实的进入路径。
- **练习 3**:ps.py 传的 `src=receiver_rank` 是全局 rank,`broadcast` 方法却把 yield 出的 `local_src` 直接传给 `self.pynccl.broadcast(tensor, local_src)`。请说明这一「直接传」为何安全。
  **答案**:NCCL 的 `ncclBroadcast` 根参数语义是「通信器内编号」;此刻 `pynccl.comm` 已换成子通信器,`local_src` 恰是该编号空间下的值,两个前提同时被 `_use_group` 保证,缺一不可。

### 4.5 DistributedNccl 全景:从初始化到销毁的完整生命周期

#### 4.5.1 概念说明

`DistributedNccl` 是 `Distributed` ABC(u5-l2 的 8 个抽象操作)在「vLLM + 裸 NCCL」世界的完整实现。它内部只维护少量状态:无状态进程组替身 `pg`、通信器扩展 `pynccl`、默认通信器句柄 `comm`、子组登记簿 `sub_groups`,以及 rank/world_size/device 与 `initialized` 标志。设计上它刻意保持「薄」:对象收集复用 u5-l2 的 `_common_all_gather_object`,切组/销组复用 4.2/4.3 的扩展,rank 翻译复用 4.4 的上下文——本类自己只做组装与流同步。

#### 4.5.2 核心流程

一次 `update()`(auto_pg=True)中,本后端方法的被调顺序:

```text
use_backend("vllm_nccl")               # examples/update.py:191,import 本模块并换单例
init_process_group(rank, world_size, store, backend=..., timeout=...)
    └ StatelessProcessGroup(经 vllm_compat 构造) + PyNcclCommunicatorEx(group, device)
new_group(ranks)                        # P2P:全体调 ncclCommSplit,成员拿 CommGroup
    ├ all_reduce(探测 bucket size,group)      # _use_group 换子 comm
    ├ barrier(group)
    └ 每桶: broadcast(src=receiver_rank,group) → all_reduce(ret_code,group)
destroy_process_group(group)            # 只销子通信器,从 sub_groups 删除
destroy_process_group()                 # 销默认通信器,initialized=False
```

#### 4.5.3 源码精读

**初始化**(注意签名只收 `rank/world_size/store`,其余进 `**kwargs` 被吞掉):

[checkpoint_engine/distributed/vllm_nccl.py:144-165](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L144-L165)

ps.py 的调用方会额外传 `backend=self.device_manager.backend`(值恰为 `"nccl"`)和 `timeout`([ps.py:541-547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541-L547))——它们正是被 `**kwargs` 静默吸收的。这是「同一调用点兼容 TorchBackend 与 DistributedNccl 两种签名」的代价与技巧。第 157-162 行经 4.1 的兼容层构造 `StatelessProcessGroup`;第 163 行构造通信器;第 164 行把默认通信器句柄另存为 `self.comm`(它是 `_use_group` 的恢复基准);设备取 `torch.device("cuda", torch.cuda.current_device())`。

**销毁**的两级语义:

[checkpoint_engine/distributed/vllm_nccl.py:167-182](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L167-L182)

带 `group` 且已登记:只销这一个子通信器并从登记簿删除(对应 ps.py `finally` 中先 `destroy_process_group(ranks_group)` 再 `destroy_process_group()` 的 LIFO 顺序,[ps.py:610-614](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L610-L614));不带 `group`:销默认通信器并整体复位 `initialized=False`。

**四个集合操作的共同骨架**——换组(`_use_group`)→ 发起 → `current_stream().synchronize()`:

[checkpoint_engine/distributed/vllm_nccl.py:187-191](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L187-L191) `all_gather_object` 直接复用 u5-l2 讲过的两轮 all_gather 算法 `_common_all_gather_object`(通信器只要求有 `all_gather`,正是为裸张量通信器设计);注意其 `world_size` 参数用的是**全局**值,即对象收集实际只服务于全量组场景(对应 `gather_metas`)。
[checkpoint_engine/distributed/vllm_nccl.py:194-206](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L194-L206) `all_reduce`:vLLM 的 `pynccl.all_reduce` 返回**新张量**,而 `Distributed` 接口约定原地生效(torch 语义),所以补一句 `tensor.copy_(out_tensor)` 完成语义适配。
[checkpoint_engine/distributed/vllm_nccl.py:208-215](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L208-L215) `broadcast`:把 `_use_group` 翻译出的 `local_src` 传给 `pynccl.broadcast`。
[checkpoint_engine/distributed/vllm_nccl.py:217-223](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L217-L223) `barrier`:没有现成的 NCCL barrier,用「对 1 个零张量做 all_reduce」模拟——所有人都到齐,all_reduce 才能完成,完成即同步。

每个操作末尾的 `current_stream().synchronize()` 不是装饰:pynccl 把集合通信**异步提交到当前 CUDA 流**就返回,若不同步,调用方(如 `_detect_bucket_size` 紧接着 `tensor.cpu()`)可能在数据就位前就读走旧值。torch 的 `dist.all_reduce` 帮你做了同步,裸 NCCL 世界必须自己补。

最后,本后端的启用入口仍是 u5-l2 的 `use_backend`:

[checkpoint_engine/distributed/base.py:221-242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L221-L242) 中 `"vllm_nccl": ".vllm_nccl.DistributedNccl"` 的映射经 importlib 延迟导入并替换全局单例;CLI 侧由 [examples/update.py:185](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L185) 的 `--custom-dist` 参数传入。

#### 4.5.4 代码实践

1. **实践目标**:源码阅读型实践——验证「参数被 `**kwargs` 吞掉」的签名兼容,并梳理一次 update 中后端方法的调用序列。
2. **操作步骤**:
   - 对照两处签名:调用方 [ps.py:541-547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541-L547) 传了 `backend/world_size/rank/timeout/store`,被调方 [vllm_nccl.py:144-150](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L144-L150) 只声明 `rank/world_size/store/**kwargs`。写下哪些参数被吞、被吞的参数里哪个在 TorchBackend 里是**必需**的(答案:backend,它决定 nccl/gloo/xccl/hccl)。
   - 在 [ps.py:569-620](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L569-L620) 与 [ps.py:780-905](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L780-L905) 中找出所有 `dist.` 前缀调用,按执行顺序标注每个调用落到 `DistributedNccl` 的哪个方法、带不带 `group`。
   - (可选,需要 vLLM 环境)在装有 vLLM 的机器上运行 `python -c "from checkpoint_engine.distributed import use_backend; use_backend('vllm_nccl'); print('ok')"`。
3. **需要观察的现象**:第二小步应得到类似序列:`init_process_group → new_group → all_reduce(带组) → barrier(带组) → [broadcast(带组,src) → all_reduce(带组)] × 桶数 → barrier → destroy_process_group(带组) → destroy_process_group()`(P2P 路径;Broadcast 路径则全程不带组);可选步骤在无 vLLM 的环境中应抛出关于 `vllm` 缺失的导入错误。
4. **预期结果**:调用序列与本讲 4.5.2 的伪代码一致;`backend="nccl"` 被吞不影响本后端,因为它自己直接建 NCCL 通信器、不走 torch 后端选择。
5. 运行结果**待本地验证**。

#### 4.5.5 小练习与答案

- **练习 1**:`DistributedNccl.barrier` 为什么可以用 all_reduce 一个零张量来模拟?
  **答案**:NCCL 集合操作是同步点——所有成员都提交了 all_reduce,它才能完成并返回;「都到齐才完成」正是 barrier 的语义。代价是比专用 barrier 略重(多一次归约计算),但对权重更新这种重负载场景可忽略。
- **练习 2**:如果把每个操作末尾的 `current_stream().synchronize()` 删掉,最先暴露问题的是 ps.py 的哪段代码?
  **答案**:`_detect_bucket_size`([ps.py:653-655](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L653-L655)):all_reduce 刚异步提交就 `tensor.cpu()` 读取,拿到的可能是未归约的旧值,bucket size 探测随之错误。
- **练习 3**:`destroy_process_group(group)` 与 `destroy_process_group()` 各自的幂等性如何?
  **答案**:带 group 时要求 `group.handle in self.sub_groups`,重复销毁同一 group 只是条件不成立而走默认分支(销毁默认通信器!),并不幂等——调用方 ps.py 用 `if ranks_group:` 挡住了这种情况;不带 group 时若 `initialized=False` 会触发断言,同样依赖调用方按序调用。

## 5. 综合实践

**任务:给一次 P2P 更新画出「双重 rank 空间」翻译总表。**

设定:`world_size = 8`,8 张卡单机,`update(ranks=[1, 3, 7])`,桶总数 2,receiver 依次是 rank 7 和 rank 1。请完成:

1. **分组表**:仿照 4.3.3 的表格,写出 8 个 rank 各自的 color/key、是否加入子组、子组内新 rank。
2. **翻译表**:对每一次 `dist.broadcast(buffer, src=X, group=ranks_group)`,写出 `_use_group` 传入的全局 `src`、yield 出的 `local_src`、以及三个成员(rank 1/3/7)各自的 `pynccl.rank` 临时值。
3. **对照源码**:为下面每个事件标注源码位置(文件:行号):`new_group` 被调用、`ncclCommSplit` 被执行、广播根被翻译、ret_code 投票、子组被销毁、默认通信器被销毁。
4. **思考题**(对应 u5-l2 的晚绑定机制):如果运行期间直接执行 `checkpoint_engine.distributed.base._BACKEND_INSTANCE = DistributedNccl()` 但忘了先 monkey patch `ncclCommSplit`,第一次故障会出现在哪次调用、以什么形式?

**参考答案要点**:

1. rank 1/3/7:color=0,key 分别 1/3/7,子组内新 rank 分别 0/1/2;其余 rank color=-1 得 NULL。
2. src=7 → local_src=2;src=1 → local_src=0;成员的 `pynccl.rank` 在两次广播中分别被临时改为各自下标(1→0, 3→1, 7→2),`pynccl.comm` 临时为子通信器。
3. `new_group`:ps.py:599 → vllm_nccl.py:225-239;split:vllm_nccl.py:103;根翻译:vllm_nccl.py:130-134;ret_code:ps.py:898 → vllm_nccl.py:194-206;销子组:ps.py:611-612 → vllm_nccl.py:167-177;销默认:ps.py:613-614 → vllm_nccl.py:179-182。
4. 若在 patch 之前 import 了本模块则不可能发生(patch 与类同文件,import 即执行);真正危险的是「NCCLLibrary 已被 vLLM 别处实例化后再 import 本模块」——此时 `_funcs` 里没有 `ncclCommSplit`,第一次故障在 `new_group` → `create_newcomm`,以 `KeyError: 'ncclCommSplit'` 的形式出现(基于 vLLM 按 `exported_functions` 建 `_funcs` 字典的实现细节,属版本耦合点,待在具体 vLLM 版本上验证)。

## 6. 本讲小结

- colocated 部署下 PS 与 vLLM 同进程,不能占用 torch 全局进程组;`DistributedNccl` 用 `StatelessProcessGroup`(TCP 会合)+ `PyNcclCommunicator`(裸 NCCL 通信)绕开,`create_stateless_process_group` 用 `inspect.signature` 兼容新旧 vLLM 构造签名。
- vLLM 的 `NCCLLibrary` 缺 `ncclCommSplit`,本模块在 import 时用 ctypes 追加函数声明并挂方法(monkey patch),时序上先于任何通信器实例化。
- `ncclCommSplit` 的 color(0 加入 / -1 即 `NCCL_SPLIT_NOCOLOR` 不加入)与 key(组内按升序编号)把「rank 列表」翻译成子通信组;组外进程拿到 NULL 通信器,`new_group` 返回 `None`。
- `_use_group` 是子组场景的翻译器:临时换 `pynccl.comm`、把全局 src 与自身 rank 经 `group.ranks.index()` 重映射到子组编号,finally 中对称恢复——这是 `dist.broadcast(src=receiver_rank, group=...)` 能正确工作的前提。
- `DistributedNccl` 的每个集合操作都遵循「`_use_group` 换组 → 发起 → `current_stream().synchronize()`」骨架;对象收集复用 `_common_all_gather_object`,barrier 用零张量 all_reduce 模拟,all_reduce 需补 `copy_` 适配 torch 的原地语义。
- 与 TorchBackend 的接口差异靠 `**kwargs` 吸收(ps.py 传的 `backend`/`timeout` 被静默吞掉);销毁分两级(销单个子组 / 销默认通信器并复位)。

## 7. 下一步学习建议

- 下一讲 **u5-l4(NPU/HCCL 后端)**:对照本讲读 `checkpoint_engine/distributed/vllm_hccl.py`,你会看到同一套骨架在昇腾平台的重演——`PyHcclCommunicatorEx` 之于 `PyNcclCommunicatorEx`、`HcclCreateSubCommConfig` 之于 `ncclCommSplit`,重点体会 `HcclCommConfig` 结构体初始化与 CUDA 路径的差异。
- 回读 u5-l2 的 [base.py:193-218](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L193-L218),确认 `_common_all_gather_object` 为什么只依赖 `all_gather` 一个原语——这正是 `DistributedNccl` 能直接复用它的原因。
- 延伸阅读(仓库外):NCCL 官方文档中 `ncclCommSplit`、`ncclConfig_t` 与 `NCCL_SPLIT_NOCOLOR` 的定义,以及 vLLM 源码中 `PyNcclCommunicator` 的 `comm`/`rank` 属性与流语义,可帮助你判断本讲所述 monkey patch 在你所用 vLLM 版本上的有效性。
