# distributed 抽象层:后端接口与动态切换

> 本讲是第五单元「分布式后端与 P2P 传输」的第二讲。上一讲(u5-l1)我们看清了 `DeviceManager` 如何把 cuda/npu/xpu 三种硬件统一成一个入口;本讲下潜一层,看 `checkpoint_engine/distributed/` 包如何把「集合通信」本身也抽象成可插拔的接口,让默认的 torch.distributed 与 vLLM 的 NCCL/HCCL 私有通信器共用同一套调用代码。

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `Distributed` 抽象基类定义的 8 个操作,以及这 8 个操作分别在 `ps.py` 的哪些业务环节被消费。
2. 理解 `_BACKEND_INSTANCE` 全局单例 + 模块级函数的「晚绑定分发」机制:为什么换后端不需要改任何 import。
3. 掌握 `use_backend` 的动态切换逻辑:支持哪些名字、何时必须调用、为什么 XPU 必须留空。
4. 读懂 `_common_all_gather_object` 的「两轮 all_gather」对象收集算法,并明白它服务的对象是谁(不是 `TorchBackend` 自己)。
5. 能在纯 CPU + gloo 环境下跑通一次完整的「建组 → 对象收集 → 归约 → 广播 → 销组」,并亲手替换一次后端。

## 2. 前置知识

本讲默认你已读完 u3-l1(TCPStore 与初始化)与 u3-l3(gather_metas),这里补充几个 Python 与 PyTorch 层面的概念:

- **ABC 与 `@abstractmethod`**:ABC(Abstract Base Class,抽象基类)定义「子类必须实现什么」。任何继承 `Distributed` 的类若没有实现全部带 `@abstractmethod` 装饰的方法,实例化时 Python 会直接抛 `TypeError`。这是接口契约的运行时保证。
- **`typing.Protocol`(结构化类型)**:与 ABC 的「名义类型」不同,`Protocol` 是「鸭子类型的静态化」——不要求继承,只要一个对象拥有签名匹配的方法,就视为满足协议。本讲的 `CommunicatorProtocol` 只要求对象有 `all_gather` 方法。
- **模块级单例与晚绑定**:`_BACKEND_INSTANCE` 是模块的一个全局变量。模块级函数 `broadcast(...)` 在**函数体内部**引用它,意味着每次调用时才查表(晚绑定);如果在别处写 `f = dist.broadcast` 再调用,换掉单例后 `f` 依然路由到新后端,因为绑定的只是「分发函数」,不是某个后端的方法。
- **进程组(process group)**:torch.distributed 中一组互相约定好 rank 编号、通过同一 store 会合的进程。`init_process_group` 建组,集合通信(all_reduce/broadcast/…)在组内进行。gloo 是 CPU 可用的通信后端,nccl/hccl/xccl 分别对应 CUDA/NPU/XPU。
- **对象级 vs 张量级集合通信**:`all_reduce`/`broadcast` 操作的是等长的 `torch.Tensor`(张量级);`all_gather_object` 操作的是任意 Python 对象(对象级),它内部要先把对象 pickle 成字节张量,再走张量级原语。这个区别正是 4.4 节的主角。
- **pickle**:Python 标准库的对象序列化框架,把任意对象转成字节流并可还原。分布式场景里,「传对象」就是「pickle → 传字节 → unpickle」。

另外回顾两个已建立的事实(u3-l1、u3-l3):PS 用 `TCPStore`(默认占 `MASTER_PORT+1`)做控制面公告板;`gather_metas` 用一次 `dist.all_gather_object` 只交换元数据、不搬权重本体。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲视角 |
| --- | --- | --- |
| `checkpoint_engine/distributed/base.py` | 全部核心实现:ABC、TorchBackend、全局单例、8 个分发函数、对象序列化辅助 | **精读** |
| `checkpoint_engine/distributed/__init__.py` | 门面:re-export 消费者需要的 11 个名字 | 精读(很短) |
| `checkpoint_engine/distributed/vllm_nccl.py` | `Distributed` 的子类 `DistributedNccl`,消费 `CommGroup` 与 `_common_all_gather_object` | 只看消费点,细节留给 u5-l3 |
| `checkpoint_engine/distributed/vllm_hccl.py` | NPU 版子类 `DistributedHccl`,同样消费基类工具 | 只看消费点,细节留给 u5-l4 |
| `checkpoint_engine/ps.py` | 抽象层的**主要消费方**,所有 `dist.*` 调用都来自这里 | 看调用点 |
| `examples/update.py` | `use_backend` 的唯一触发入口(`--custom-dist`) | 看两行 |
| `tests/test_xpu_parity.py` | `use_backend` 的两个纯 CPU 测试 | 实践素材 |

一个容易误读的细节先点破:[ps.py:15](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L15) 写的是 `import checkpoint_engine.distributed as dist`——ps.py 里满篇的 `dist.broadcast`、`dist.barrier` 看起来像 `torch.distributed`,但别名 `dist` 实际指向项目自己的抽象层。**ps.py 从不直接调用 torch.distributed 的集合通信**。

## 4. 核心概念与源码讲解

### 4.1 Distributed:抽象基类定义的操作集合

#### 4.1.1 概念说明

为什么需要这一层?默认路径下,PS 直接用 `torch.distributed` 建组(nccl/hccl/xccl,由 u5-l1 的 `DeviceManager.backend` 决定)完全够用。但在 vLLM colocated 部署里,存在「不经过 torch 进程组、直接操作 NCCL/HCCL 裸通信器」的需求(为什么需要这样,是 u5-l3 的主题)。如果 ps.py 直接写死 `torch.distributed.xxx`,这两种路径就要维护两份主流程代码。

解法是教科书式的依赖倒置:把「**需要活通信器的集合通信**」抽成接口 `Distributed`,ps.py 只面向接口编程;torch 路径和 vLLM 路径各写一个实现,运行前选一个装进去。

注意抽象边界画在哪里:ps.py 依然直接 `import torch.distributed` 使用 `TCPStore`、`PrefixStore`、`_store_based_barrier`、`ReduceOp`——这些是**不依赖已初始化进程组**的会合设施和枚举常量(`TCPStore` 甚至本身就是 `init_process_group` 的入参)。被抽象的恰恰是那 8 个「必须先建组才能调」的操作。

#### 4.1.2 核心流程

接口按生命周期分三组:

```text
生命周期管理
  init_process_group(rank, world_size, store)   建组
  destroy_process_group(group=None)             销组(可只销某个子组)
  is_initialized()                              状态查询
  new_group(ranks)                              从全局组切子组

数据面(张量级,传输权重字节)
  broadcast(tensor, src, group)                 广播一个桶
  all_reduce(tensor, op, group)                 归约(MIN 探显存 / SUM 投票)

控制面(对象级,交换元数据)
  all_gather_object(list, obj, group)           收集各 rank 的 DataToGather
  barrier(group)                                全组同步
```

#### 4.1.3 源码精读

先看接口本体。[base.py:33-98](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L33-L98) 定义 `Distributed(ABC)`,共 8 个 `@abstractmethod`,每个方法体只有一行 `raise NotImplementedError`——纯粹给静态检查与子类约束用。摘两段感受签名风格:

[base.py:34-49](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L34-L49) 建组/销组契约:入参是 `(rank, world_size, store)`,即「会合信息由调用方备好」——PS 在构造函数里早已建好 TCPStore(u3-l1),这里只把它递进来。

[base.py:74-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L74-L82) 广播契约:`src` 是**全局 rank**;`group` 可为 `None`(默认组)。

再看两个配套类型。[base.py:16-30](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L16-L30) 定义了 `CommGroup`(把「裸通信器句柄 + rank 清单」包成对象)和联合类型:

```python
DistributedProcessGroup = torch_dist.ProcessGroup | CommGroup
```

这是本讲第一个关键设计:`TorchBackend.new_group` 返回 torch 的 `ProcessGroup`,而 vLLM 后端返回的是 `CommGroup`。ps.py 只用 `dist.DistributedProcessGroup` 这个联合类型注解持有句柄(见 [ps.py:634](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L634)、[ps.py:756](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L756)),两种句柄的差异完全封在后端内部。vLLM 后端的 `new_group` 正是返回 `CommGroup(newcomm.value, ranks)`(见 [vllm_nccl.py:237](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L237))。

最后是消费全景。ps.py 中 8 个操作与业务环节的对应关系:

| 操作 | ps.py 调用点 | 业务环节(承接 u3-l3 / u3-l4) |
| --- | --- | --- |
| `is_initialized` | [ps.py:468](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L468)、[ps.py:596](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L596)、[ps.py:613](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L613) | auto_pg 模式下「没建组就先建」的惰性守卫 |
| `init_process_group` | [ps.py:541](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541) | 用 PrefixStore 隔离的建组(u3-l4) |
| `all_gather_object` | [ps.py:491](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L491) | gather_metas 收集全局元数据(u3-l3) |
| `new_group` | [ps.py:599](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L599) | P2P 更新时按 ranks 切子组 |
| `all_reduce` | [ps.py:653](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L653)(MIN)、[ps.py:898](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L898)(SUM) | 探测最小空闲显存 / ret_code 错误投票(u3-l4/u3-l5) |
| `broadcast` | [ps.py:890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L890) | 四拍循环里以 receiver 为源广播桶(u3-l4) |
| `barrier` | [ps.py:802](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L802)、[ps.py:935](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L935) | 更新前后的全组同步点 |
| `destroy_process_group` | [ps.py:612-614](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L612-L614) | finally 里 LIFO 销毁子组与全局组 |

#### 4.1.4 代码实践:给 8 个操作找到家

1. **实践目标**:不写代码,用检索建立「接口 → 消费点」的肌肉记忆。
2. **操作步骤**:在仓库根目录执行 `grep -n "dist\." checkpoint_engine/ps.py`,对照上面表格,把每个调用点归到「生命周期 / 数据面 / 控制面」三类。
3. **需要观察的现象**:ps.py 里 `dist.` 的调用点恰好只覆盖这 8 个操作(外加类型注解 `dist.DistributedProcessGroup`);同时 ps.py 还直接出现 `torch.distributed.TCPStore`、`torch.distributed.PrefixStore`、`torch.distributed.distributed_c10d._store_based_barrier`、`torch.distributed.ReduceOp` 四类直接引用。
4. **预期结果**:你会得出结论——抽象边界只覆盖「需要已初始化通信器的操作」,store 会合与枚举常量留在抽象之外。这正是本讲最重要的架构判断。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `use_backend` 换了通信后端,ps.py 里那些 `torch.distributed.ReduceOp.MIN` 还能继续用?
**答案**:`ReduceOp` 只是一个描述归约操作的枚举,不依赖任何已初始化的通信器;vLLM 后端的 `all_reduce` 同样接收这个枚举并翻译成 NCCL/HCCL 的原生操作码。需要跟着后端换的东西(通信器、rank 映射)都在后端内部,枚举不必换。

**练习 2**:`CommGroup` 为什么不直接复用 torch 的 `ProcessGroup`?
**答案**:vLLM 的 `PyNcclCommunicator` 持有的是 NCCL 裸 communicator(一个整型句柄),根本没有 torch `ProcessGroup` 对象;`CommGroup` 就是把这个整型句柄加上 rank 清单包成能过类型检查、能放进 `DistributedProcessGroup` 联合类型的轻量容器。

**练习 3**:`CommunicatorProtocol`(见 [base.py:12-13](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L12-L13))和 `Distributed` 都是「接口」,它们有什么本质区别?
**答案**:`Distributed` 是名义类型(必须继承才算),约束的是「后端」这一完整角色;`CommunicatorProtocol` 是结构化协议(有 `all_gather` 方法就算),约束的是 `_common_all_gather_object` 的 `comm` 参数——vLLM 的 `PyNcclCommunicator`/`PyHcclCommunicator` 并不继承任何项目类,却能满足协议,这正是选 Protocol 而不是 ABC 的原因。

### 4.2 TorchBackend 与模块级分发:全局单例如何工作

#### 4.2.1 概念说明

`TorchBackend` 是默认实现,也是「翻译层」——每个方法一行,把接口语义原样转发给 `torch.distributed`。真正让抽象层好用的是分发机制:模块持有一个全局单例 `_BACKEND_INSTANCE`,8 个与抽象方法同名的**模块级函数**在函数体里读这个全局变量再委托。于是:

- 消费方(ps.py)`import checkpoint_engine.distributed as dist`,像用 torch.distributed 一样调 `dist.broadcast`;
- 换后端 = 换一个全局变量,**所有已 import 的调用点立即生效**,不需要重新 import 任何东西。

`__init__.py` 则把消费面(11 个名字)re-export 出去,而实现面(`TorchBackend`、`CommGroup`、`_common_all_gather_object`、`_BACKEND_INSTANCE`)留在 `base.py`——所以测试代码要 `from checkpoint_engine.distributed.base import TorchBackend`(见 [test_xpu_parity.py:23](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L23)),子类后端也从 base 导入。

#### 4.2.2 核心流程

```text
import 时(base.py 模块初始化)
  _BACKEND_INSTANCE = TorchBackend()        # 默认后端就位

运行时
  ps.py 调 dist.broadcast(t, src, g)
    → base.broadcast(t, src, g)             # 模块级分发函数
      → _BACKEND_INSTANCE.broadcast(...)    # 此刻查全局变量(晚绑定)
        → TorchBackend: torch_dist.broadcast(...)
        → (或) DistributedNccl: pynccl.broadcast(...)

切换时(use_backend,见 4.3)
  _BACKEND_INSTANCE = 新后端实例            # 下一次调用立即走新后端
```

#### 4.2.3 源码精读

默认实现:[base.py:101-153](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L101-L153) 的 `TorchBackend` 逐方法委托 `torch_dist`。值得看的是唯一带默认值逻辑的 `init_process_group`:

[base.py:102-118](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L102-L118) 从 `kwargs` 取 `backend`(默认 `"nccl"`)与 `timeout`(默认 10 分钟)后调用 `torch_dist.init_process_group`。生产上 ps.py 不用这个默认值,而是传入 `self.device_manager.backend`(见 [ps.py:541-547](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L541-L547))——NPU 传 hccl、XPU 传 xccl(u5-l1 的映射表),默认 `"nccl"` 只是兜底。注意 `all_gather_object` 的默认实现([base.py:126-129](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L126-L129))直接委托 `torch_dist.all_gather_object`,**不经过** `_common_all_gather_object`(那是给别人用的,见 4.4)。

单例与分发:[base.py:157](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L157) 一行 `_BACKEND_INSTANCE: Distributed = TorchBackend()` 在模块加载时就实例化默认后端;[base.py:245-293](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L245-L293) 是 8 个模块级分发函数,形态全部一致,以 `broadcast` 为例:

```python
def broadcast(tensor, src=0, group=None, **kwargs):
    _BACKEND_INSTANCE.broadcast(tensor, src, group, **kwargs)
```

(见 [base.py:279-285](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L279-L285)。)关键在 `_BACKEND_INSTANCE` 出现在**函数体内**——每次调用时求值,这就是晚绑定。

门面:[\_\_init\_\_.py:1-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/__init__.py#L1-L28) 只做 re-export 并用 `__all__` 声明 11 个公开名字:`Distributed`、`DistributedProcessGroup` 加 9 个函数(含 `use_backend`)。由于只 import `base`,`checkpoint_engine.distributed` 包的导入**完全不触碰 vLLM**——这是延迟导入链条的第一环(u1-l3 讲过的隔离手法)。

#### 4.2.4 代码实践:纯 CPU 跑通一次完整的抽象层会话

这是本讲的主实践。gloo 后端在 CPU 上即可工作,让我们像 ps.py 一样:建 TCPStore → 走抽象层建组 → 对象收集 → 归约 → 广播 → 销组。

1. **实践目标**:验证「面向接口 + TorchBackend 默认实现」在真实多进程下成立,并体会 TCPStore 的端口技巧。

2. **操作步骤**:

   在仓库根目录新建 `practice_gloo.py`(示例代码,注意不要放进 `checkpoint_engine/` 包内):

   ```python
   import os
   from datetime import timedelta

   import torch
   import torch.distributed as torch_dist

   from checkpoint_engine import distributed as dist


   def main():
       rank = int(os.environ["RANK"])
       world_size = int(os.environ["WORLD_SIZE"])
       # torchrun 已占用 MASTER_PORT 做会合,store 必须换端口:
       # 这正是 ps.py _get_master_port 取 MASTER_PORT+1 的原因(u3-l1)
       store = torch_dist.TCPStore(
           os.environ["MASTER_ADDR"],
           int(os.environ["MASTER_PORT"]) + 1,
           world_size,
           timeout=timedelta(minutes=5),
           is_master=rank == 0,
       )
       dist.init_process_group(
           rank=rank, world_size=world_size, store=store, backend="gloo"
       )
       assert dist.is_initialized()

       # 控制面:对象级收集(模仿 gather_metas 的用法)
       local_meta = {"rank": rank, "params": [f"w{rank}_0", f"w{rank}_1"]}
       gathered = [None] * world_size
       dist.all_gather_object(gathered, local_meta)
       print(f"[rank{rank}] gathered = {gathered}", flush=True)

       # 数据面:张量级归约 + 广播
       loss = torch.tensor([float(rank)])
       dist.all_reduce(loss)          # 默认 SUM
       dist.broadcast(loss, src=0)
       print(f"[rank{rank}] loss = {loss.item()}", flush=True)

       dist.barrier()
       dist.destroy_process_group()


   if __name__ == "__main__":
       main()
   ```

   然后运行(需已按 u1-l2 安装 checkpoint-engine,可在仓库根目录直接跑):

   ```bash
   torchrun --nnodes=1 --nproc-per-node=2 practice_gloo.py
   ```

3. **需要观察的现象**:两个进程各自打印 `gathered` 与 `loss`;`gathered` 应包含两个 rank 的字典;`loss` 在两个进程里应一致。

4. **预期结果**:`gathered` 为 `[{"rank": 0, ...}, {"rank": 1, ...}]` 两个元素的列表;`loss` 归约后为 \( 0 + 1 = 1.0 \),再从 src=0 广播,两个进程都打印 `1.0`。若把 `MASTER_PORT + 1` 改回 `MASTER_PORT`,预期与 torchrun 自身会合端口冲突而报错——这从反面解释了 ps.py 的端口设计。本实践结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:如果 ps.py 写的是 `from checkpoint_engine.distributed import broadcast`,之后调用 `use_backend` 换后端,这个 `broadcast` 还能路由到新后端吗?
**答案**:能。import 拿到的是**模块级分发函数**对象,而不是某个后端的方法;分发函数体内每次调用都读 `_BACKEND_INSTANCE`,换单例对所有引用方式一律生效。

**练习 2**:`TorchBackend.init_process_group` 的 backend 默认值是 `"nccl"`,那 NPU 上为什么不报「找不到 nccl」?
**答案**:因为 ps.py 从不依赖这个默认值——[ps.py:542](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L542) 显式传 `backend=self.device_manager.backend`,由 DeviceManager 把 npu 翻译成 hccl(u5-l1)。

**练习 3**:为什么 `__init__.py` 不把 `TorchBackend`、`_common_all_gather_object` 也 export 出去?
**答案**:`__init__.py` 面向**消费者**,只暴露 ps.py 需要的调用面;`TorchBackend`/`CommGroup`/`_common_all_gather_object` 是**实现者工具**,子类后端与测试从 `base` 导入。两层视角分开,公共 API 面积最小。

### 4.3 use_backend:运行前的后端热插拔

#### 4.3.1 概念说明

`use_backend` 是抽象层的「总开关」:用一个字符串把全局单例换成别的实现。它的行为要点:

- 传 `None`(或空串)是**幂等 no-op**——什么都不换。这不是「重置回默认」,而是「保持现状」。XPU 正是靠「不传 custom_dist」留在默认 TorchBackend(xccl)上。
- 公开映射只认两个名字:`vllm_nccl` 与 `vllm_hccl`,分别对应 vLLM 的 NCCL/HCCL 私有通信后端(u5-l3/u5-l4 精读)。
- 用 `importlib` **延迟导入**目标模块:不调用 `use_backend("vllm_nccl")`,vLLM 永远不会被 import。这与 u1-l3 讲过的「可选依赖延迟导入」是同一手法。

要诚实地说明一点:想接入**真正自定义**的后端(不在映射表里的名字),`use_backend` 的公开映射并不支持——它会抛 `ValueError`。扩展路径是直接赋值 `base._BACKEND_INSTANCE = MyBackend()`(下述实践会做),这也是两个内置后端之外唯一的接入口。

#### 4.3.2 核心流程

```text
use_backend(name)
  ├─ name 为假值 → return                 # 不动现状(XPU 回落默认的关键)
  ├─ name 不在 {vllm_nccl, vllm_hccl} → ValueError
  └─ 在表内:
       ".vllm_nccl.DistributedNccl" 拆成 (模块路径, 类名)
       importlib.import_module(模块路径, package="checkpoint_engine.distributed")
       _BACKEND_INSTANCE = 类名()         # 此刻才 import vLLM 相关依赖
```

调用时机约束:`examples/update.py` 在 [update.py:191](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L191) 调 `dist.use_backend(args.custom_dist)`,下一行才构造 `ParameterServer(auto_pg=True)`——换血必须发生在第一次 `init_process_group` 之前。auto_pg 模式下进程组按需建毁(u3-l1/u4-l5),第一次 `gather_metas` 就会建组,晚了就会「前后用了两个后端」。

#### 4.3.3 源码精读

[base.py:221-242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L221-L242) 是 `use_backend` 全文:

```python
def use_backend(backend: str | None):
    global _BACKEND_INSTANCE

    if not backend:
        return

    mapping = {
        "vllm_nccl": ".vllm_nccl.DistributedNccl",
        "vllm_hccl": ".vllm_hccl.DistributedHccl",
    }
    if backend not in mapping:
        raise ValueError(...)   # 错误消息明确说明 XPU 不支持自定义后端
```

三处细节值得咀嚼:

- [base.py:224-225](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L224-L225) 的假值早退,是 [test_xpu_parity.py:184-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_xpu_parity.py#L184-L192) 验证的行为:`use_backend(None)` 后 `_BACKEND_INSTANCE` 仍是 `TorchBackend`。
- [base.py:239-240](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L239-L240) 用 `rsplit(".", 1)` 把 `".vllm_nccl.DistributedNccl"` 拆成模块与类,再 `importlib.import_module(module_path, "checkpoint_engine.distributed")` 以相对路径解析——模块字符串以 `.` 开头正是为相对导入准备的。
- [base.py:242](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L242) 实例化并覆盖全局单例,一行完成换血。

配套的入口参数在 [update.py:185](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L185):`--custom-dist` 默认 `None`,即默认永远走 TorchBackend。

#### 4.3.4 代码实践:换一个自己写的后端,并验证晚绑定

1. **实践目标**:证明「直接赋值 `_BACKEND_INSTANCE` + 晚绑定」即可接入自定义后端,无需改 ps.py、无需改映射表。

2. **操作步骤**:

   先跑现成测试(纯 CPU,无 gpu 标记)确认 `use_backend` 的两种边界行为:

   ```bash
   pytest tests/test_xpu_parity.py -k use_backend -m "not gpu" -v
   ```

   再新建 `practice_swap.py`(示例代码),在 4.2.4 脚本的 `main()` 之前加入:

   ```python
   from checkpoint_engine.distributed import base as dist_base
   from checkpoint_engine.distributed.base import TorchBackend


   class CountingBackend(TorchBackend):
       """只统计 broadcast 次数,其余行为与默认后端完全一致。"""

       def __init__(self):
           self.broadcast_calls = 0

       def broadcast(self, tensor, src=0, group=None, **kwargs):
           self.broadcast_calls += 1
           return super().broadcast(tensor, src, group, **kwargs)


   my_backend = CountingBackend()
   dist_base._BACKEND_INSTANCE = my_backend   # 直接换血(注意:须在所有 rank 进程里执行)
   ```

   然后照 4.2.4 的方式用 torchrun 运行,在脚本末尾打印 `my_backend.broadcast_calls`。

3. **需要观察的现象**:程序行为与 4.2.4 完全一致(归约/广播结果不变),但 `broadcast_calls` 非零;`pytest` 两条用例分别验证「非法名抛 ValueError」与「None 不改变默认后端」。

4. **预期结果**:`broadcast_calls` 等于 1(每次 `dist.broadcast` 计数一次);若接着调用 `use_backend(None)`,计数后端**仍然在位**(no-op 不是重置),要回默认需再写 `dist_base._BACKEND_INSTANCE = TorchBackend()`。若在未安装 vLLM 的环境调用 `use_backend("vllm_nccl")`,预期在延迟导入处抛 `ModuleNotFoundError`。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:`use_backend(None)` 会把后端重置回 `TorchBackend` 吗?
**答案**:不会。假值直接 `return`,语义是「保持现状」。测试名 `test_use_backend_none_keeps_default_torch_backend` 之所以成立,是因为调用前安装的本来就是默认后端;若先换过别的,`None` 不会帮你换回来。

**练习 2**:为什么 `use_backend("vllm_nccl")` 在没装 vLLM 的机器上报的是 `ModuleNotFoundError` 而不是 `ValueError`?
**答案**:名字在映射表内,通过了校验;失败发生在 [base.py:240](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L240) 的延迟导入——`.vllm_nccl` 模块顶层 `from vllm...` 找不到 vLLM。这是延迟导入的自觉取舍:不换后端就绝不引入 vLLM 依赖。

**练习 3**:`--custom-dist` 为什么设计在 `examples/update.py`(编排脚本)里,而不是 `ParameterServer.__init__` 的参数?
**答案**:后端选择是**进程级**的全局状态,不属于任何单个 PS 实例;且换血必须先于一切建组动作。放在入口脚本里、构造 PS 之前一行完成,时序最不容易被破坏。

### 4.4 _common_all_gather_object:两轮 all_gather 的对象收集

#### 4.4.1 概念说明

`all_gather_object` 要收集的是**任意 Python 对象**(如 gather_metas 的 `DataToGather`),而底层通信原语只能搬运**等长的张量**。torch 自己的 `torch_dist.all_gather_object` 解决了这件事,但它要求有 torch 进程组;vLLM 后端没有 torch 进程组,只有「裸张量 all_gather」(vLLM 的 `PyNcclCommunicator.all_gather`)。

于是项目把对象级算法从 torch 中抽出来变成公共函数 `_common_all_gather_object`:**你给我任何一个会 `all_gather` 张量的通信器,我就能在上面收集对象**。这就是 `CommunicatorProtocol` 的用武之地——`PyNcclCommunicator`、`PyHcclCommunicator` 都不是本项目类,却都满足协议。

再次强调分工:`TorchBackend.all_gather_object` 直接用 torch 的现成实现,**不用**这个函数;`_common_all_gather_object` 的服务对象是 `DistributedNccl`([vllm_nccl.py:187-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L187-L192))和 `DistributedHccl`([vllm_hccl.py:278](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_hccl.py#L278))。

#### 4.4.2 核心流程

难点:各 rank 对象大小不同,张量集合通信却要求等长。解法是两轮通信:

```text
第一轮(尺寸交换)
  obj → pickle → byte_tensor,同时得到本 rank 字节数 local_size
  all_gather(object_sizes_tensor[world_size], local_size)
  得到所有 rank 的字节数,取 S_max = max

第二轮(负载交换)
  input_tensor.resize_(S_max)        # 不足处零填充
  all_gather(coalesced[S_max × world_size], input)

还原
  第 i 段切片 → 截断到实际 size_i → unpickle → object_list[i]
```

设备缓冲总开销为

\[ \text{通信缓冲} = W \times S_{\max}, \qquad S_{\max} = \max_{r \in [0, W)} S_r \]

其中 \( W \) 是 world_size、\( S_r \) 是第 \( r \) 个 rank 序列化后的字节数。填充浪费 \( W \times S_{\max} - \sum_r S_r \) 在各 rank 对象大小悬殊时最大——不过控制面只传元数据(u3-l3),常态下各 rank 的 `DataToGather` 大小相近,浪费可忽略。

#### 4.4.3 源码精读

序列化辅助三件套在 [base.py:159-190](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L159-L190):

- [base.py:159-160](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L159-L160):`_pickler = pickle.Pickler`、`_unpickler = pickle.Unpickler` 两个模块级别名是「换序列化实现」的钩子(承袭 torch 同款设计),平时就是标准 pickle。
- [base.py:163-169](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L163-L169):`_object_to_tensor` 把对象 pickle 进 `BytesIO`,用 `torch.ByteStorage._from_buffer` 把字节流包成字节张量,再搬到目标设备,同时返回单元素长整型张量 `local_size` 记录字节数。
- [base.py:172-175](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L172-L175):`_tensor_to_object` 是逆过程——回 CPU、`numpy().tobytes()` 后**按实际尺寸截断**再 unpickle。零填充字节在这里被裁掉,不会进 unpickle。

主算法在 [base.py:193-218](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L193-L218),与上面流程逐步对应:

```python
input_tensor, local_size = _object_to_tensor(object, device)
object_sizes_tensor = torch.empty(world_size, dtype=torch.long, device=device)
comm.all_gather(object_sizes_tensor, local_size)        # 第一轮:尺寸
...
max_object_size = int(max(object_size_list).item())
input_tensor.resize_(max_object_size)                   # 零填充到 S_max
coalesced_output_tensor = torch.empty(max_object_size * world_size, ...)
comm.all_gather(coalesced_output_tensor, input_tensor)  # 第二轮:负载
```

两个读代码时容易忽略的点:

- [base.py:203](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L203) 把尺寸张量拆成每个 rank 一个单元素张量的列表,还原阶段按各自的实际尺寸截断——多出来的就是填充零。
- 函数不检查 `object_list` 的长度,直接按下标写入 `object_list[i]`,因此**调用方必须传入长度不小于 world_size 的列表**。ps.py 在 [ps.py:471](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L471) 预先 `[None for _ in range(self._world_size)]` 正是为此(u3-l3 已见过这行)。

另外 [base.py:178-190](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L178-L190) 还有一个 `_flatten_for_scatter_gather`(把张量列表摊平进一个缓冲),同样承袭自 torch 的实现谱系,但**当前仓库内没有任何调用方**(源码与测试均未引用),阅读时可以跳过。

子类如何消费这把「公共瑞士军刀」,看 vLLM NCCL 后端的 `all_gather_object`:

[base.py:193-199](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/base.py#L193-L199) 的 `comm` 参数类型是 `CommunicatorProtocol`,而 [vllm_nccl.py:190-192](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/distributed/vllm_nccl.py#L190-L192) 传入的是 `self.pynccl`(vLLM 的 `PyNcclCommunicator` 实例),裸通信器的字节级 `all_gather` 借协议接入,外层再补一个流同步——对象级语义就齐了。

#### 4.4.4 代码实践:单进程解剖两轮算法

1. **实践目标**:不开多进程、不装任何通信库,用一个「假通信器」直接驱动 `_common_all_gather_object`,亲眼看到两轮 all_gather 的形状与零填充。

2. **操作步骤**:

   新建 `practice_fake_comm.py`(示例代码):

   ```python
   import torch
   from checkpoint_engine.distributed.base import _common_all_gather_object


   class MirrorComm:
       """满足 CommunicatorProtocol:把唯一输入镜像到所有 rank 槽位,
       相当于'所有 rank 发了一模一样的对象'。"""

       def __init__(self, world_size):
           self.world_size = world_size
           self.log = []

       def all_gather(self, output_tensor, input_tensor):
           self.log.append((tuple(input_tensor.shape), tuple(output_tensor.shape)))
           n = output_tensor.numel() // self.world_size
           for i in range(self.world_size):
               output_tensor[i * n : (i + 1) * n].copy_(input_tensor.flatten()[:n])


   world_size = 3
   comm = MirrorComm(world_size)
   obj = {"rank": 0, "metas": ["a", "b", "c"]}
   object_list = [None] * world_size          # 必须预分配(源码不检查长度)
   _common_all_gather_object(comm, torch.device("cpu"), world_size, object_list, obj)

   for i, (in_shape, out_shape) in enumerate(comm.log, 1):
       print(f"round {i}: in={in_shape} out={out_shape}")
   print("object_list =", object_list)
   ```

   直接运行:`python practice_fake_comm.py`。

3. **需要观察的现象**:打印出的两轮形状;最终 `object_list` 的内容。

4. **预期结果**:第一轮 `in=(1,) out=(3,)`(尺寸交换);第二轮 `in=(S_max,) out=(3*S_max,)`(等长负载),其中 \( S_{\max} \) 等于该对象 pickle 后的字节数;`object_list` 是同一个字典的 3 份内容相同的拷贝(unpickle 的每次都是新对象)。把 `obj` 换成大小差异明显的对象重复实验,`S_max` 随最大对象变化。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:为什么必须两轮 all_gather,一轮不行?
**答案**:各 rank 对象 pickle 后字节数不同,而张量 all_gather 要求输入输出等长。第一轮先交换尺寸求出 \( S_{\max} \),第二轮统一零填充到 \( S_{\max} \) 再收负载。一轮无法在收负载前知道该分配多大的输出缓冲。

**练习 2**:`resize_` 补进去的零字节会不会污染反序列化?
**答案**:不会。还原端 `_tensor_to_object` 按**实际尺寸**(第一轮拿到的 \( S_r \))截断字节流后再 unpickle,填充零只存在于通信缓冲中。

**练习 3**:`TorchBackend` 自己的 `all_gather_object` 为什么不复用 `_common_all_gather_object`?
**答案**:torch 进程组在位时,`torch_dist.all_gather_object` 已经是现成且经过充分验证的实现;`_common_all_gather_object` 存在的意义恰恰是给**没有 torch 进程组**、只有裸张量通信器的后端(vLLM NCCL/HCCL)提供公共算法,避免每个子类重写一遍序列化与两轮逻辑。

## 5. 综合实践:给一次「迷你权重更新会话」画分布式原语画像

把本讲四个模块串起来:自定义一个带日志的后端,替换全局单例,走一遍 4.2.4 的 gloo 会话,统计每个分布式原语被调用的次数,从而回答一个架构问题——**控制面与数据面分别依赖哪些原语**。

1. **实践目标**:用一个 `LoggingBackend(TorchBackend)` 包装器验证「子类化 + 直接赋值单例」的完整扩展路径,并把抽象层 8 个操作按调用频率归类。

2. **操作步骤**:

   (1) 新建 `practice_profile.py`(示例代码),骨架如下,请补全 `wrap` 装饰器逻辑(给每个方法包一层:计数 + 打印方法名与关键参数摘要后调用 `super()`):

   ```python
   import functools

   from checkpoint_engine.distributed import base as dist_base
   from checkpoint_engine.distributed.base import Distributed, TorchBackend

   OPS = [op for op in dir(Distributed) if not op.startswith("_")]
   counters = dict.fromkeys(OPS, 0)


   class LoggingBackend(TorchBackend):
       pass  # TODO: 用 functools.wraps 逐个包装 8 个操作:counters[op] += 1、
             # logger/打印后委托父类实现


   dist_base._BACKEND_INSTANCE = LoggingBackend()
   # 之后照抄 practice_gloo.py 的 main():建 store、建组、all_gather_object、
   # all_reduce、broadcast、barrier、销组
   ```

   (2) `torchrun --nnodes=1 --nproc-per-node=2 practice_profile.py`,会话结束后打印 `counters`。

3. **需要观察的现象**:每个原语的计数;哪类操作只调用一次(生命周期),哪类会被反复调用(数据面)。

4. **预期结果**:`init_process_group`/`destroy_process_group`/`is_initialized` 属于一次性生命周期调用;若把脚本扩展成「循环收集 3 次元数据、广播 3 个张量」,`all_gather_object`/`broadcast`/`barrier` 的计数应线性增长。对照 u3-l4 的四拍循环可以推论:真实 `_update_per_bucket` 中计数最高的必然是 `broadcast`(每桶一次)与 `barrier`(同步点),而 `all_gather_object` 每次 gather_metas 只有一次——控制面轻、数据面重,这正是抽象层把两者都收进同一接口却保持数据面零包装开销(直接一行委托)的原因。待本地验证。

## 6. 本讲小结

- 抽象边界画在「需要活通信器的 8 个操作」上:`Distributed` ABC 定义 `init_process_group`/`destroy_process_group`/`is_initialized`/`new_group`/`all_gather_object`/`all_reduce`/`broadcast`/`barrier`;TCPStore、ReduceOp 等不依赖通信器的设施留在抽象之外,ps.py 照用 torch 的。
- 分发靠「全局单例 + 晚绑定」:`_BACKEND_INSTANCE` 默认是 `TorchBackend`,8 个模块级函数在函数体内查这个全局变量,换后端对所有调用点即时生效,`__init__.py` 只 re-export 消费面的 11 个名字。
- `use_backend` 只认 `vllm_nccl`/`vllm_hccl` 两个内置名,靠 `importlib` 延迟导入(不换后端不 import vLLM);假值是「保持现状」的幂等 no-op 而非重置;真正自定义后端的入口是直接赋值 `base._BACKEND_INSTANCE`,且必须在第一次建组之前完成。
- `DistributedProcessGroup = torch.ProcessGroup | CommGroup` 的联合类型让 ps.py 透明持有两种子组句柄,把 torch 与 vLLM 后端的差异封在接口后面。
- `_common_all_gather_object` 用「尺寸交换 → 零填充到 \( S_{\max} \) → 负载交换 → 截断 unpickle」两轮算法把对象级通信架到任何满足 `CommunicatorProtocol` 的裸张量通信器上;它服务 vLLM 后端,`TorchBackend` 自己用 torch 现成实现。

## 7. 下一步学习建议

本讲只回答了「接口长什么样、怎么切换」;两个内置后端内部如何用 ctypes 直捣 NCCL/HCCL、如何做 rank 重映射,是接下来两讲的内容:

- **u5-l3(vLLM NCCL 后端)**:精读 `DistributedNccl`——重点看它如何消费本讲的 `_common_all_gather_object` 与 `CommGroup`,以及 `create_newcomm` 的 color/key 子通信组语义。
- **u5-l4(NPU/HCCL 后端)**:对照 `DistributedHccl`,看昇腾平台如何复用同一套抽象。

阅读建议:打开 `vllm_nccl.py` 后先列出它与 `TorchBackend` 的方法级 diff——哪些方法多了一个 `_use_group` 上下文?为什么 `broadcast` 的 `src` 在子类里要做一次重映射(提示:子通信组内 rank 与全局 rank 不同)?带着这两个问题进入下一讲,收益最大。
