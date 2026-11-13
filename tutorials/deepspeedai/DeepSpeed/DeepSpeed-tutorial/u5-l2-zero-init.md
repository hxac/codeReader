# zero.Init：参数从出生就切分

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `deepspeed.zero.Init` 上下文管理器如何在模型**构建期**（而不是训练开始后）就把每个参数切分到多个进程上，以及为什么这能突破「单卡必须先装下完整 fp32 模型」的显存墙。
2. 手算一个参数在给定 world size 下如何 padding、对齐、分桶：每个 rank 分到哪一段、为什么总分区量略大于参数量。
3. 区分取回参数的几种句柄：`AllGatherHandle`、`AllGatherCoalescedHandle`、`AllReduceCoalescedHandle`、`NoGatherHandle` 与 `MultipleAllGatherHandles`，并说出 #8843 之后「多 dtype 分区按 dtype 拆分」的新规则。
4. 说明 `register_external_parameter` 解决什么问题：不在宿主模块 `forward()` 里使用的权重，靠谁来触发 gather 与释放。
5. 解读本周期 #8843 给 `zero.Init` 增加的收尾钩子 `_keep_in_fp32`：transformers 模型点名的 fp32 参数/缓冲如何在**分区上**就地转回 fp32，量化分区为何保持量化，参数卸载时为何只保 buffers。

本讲是 ZeRO 单元（u5）的第二篇，承接 u5-l1 的三阶段显存公式，把 stage 3 的「参数切分」从概念落到源码。

## 2. 前置知识

- **ZeRO-3 参数切分（u5-l1）**：混合精度 + Adam 下每卡模型状态约 \(16\Psi\) 字节，stage 3 把参数本身也按数据并行度切分，每卡只留 \(1/N_d\)。本讲解释这个切分发生在什么时刻、按什么规则。
- **torch.nn.Module 的构造协议**：PyTorch 模型是嵌套的 `nn.Module`，每个子模块在自己的 `__init__` 里创建 `nn.Parameter`。`zero.Init` 的核心技巧就是在「每个子模块 `__init__` 刚结束」这个时刻插一脚。
- **上下文管理器**：`with ... :` 语法进入 `__enter__`、离开 `__exit__`。`zero.Init()` 本身是一个可以 `with` 的对象。
- **分布式进程组（u3-l2/u3-l3）**：`dist.get_rank()`、`dist.get_world_size()`、all-gather/all-reduce 等集合通信的原语语义；`deepspeed.comm` 是对 torch.distributed 的统一包装。
- **bf16 的分辨率问题（u4-l3）**：bf16 只有 8 位尾数，数值 8.0 附近的相邻可表示值相差 0.0625；某些张量（如 MoE 路由 bias `e_score_correction_bias`）的取值在 bf16 下会塌缩成同一个值，这是 #8843 整套 keep_in_fp32 机制的动机。
- **检查点加载时机**：transformers 的 `from_pretrained` 在模型 `__init__` 跑完之后才把权重拷进去；这个顺序是理解「收尾钩子为什么放在 `_post_init_method` 末尾」的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/zero/partition_parameters.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py) | 本讲主角：`Init` 上下文、参数的 `partition`/`all_gather` 集合方法、各取回句柄、`register_external_parameter`、`GatheredParameters` |
| [deepspeed/runtime/keep_in_fp32.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py) | keep_in_fp32 的模式匹配与就地转换（#8843 后参数与缓冲统一处理） |
| [deepspeed/runtime/zero/config.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/config.py) | ZeRO 配置类：`stage3_param_persistence_threshold`、`stage3_use_all_reduce_for_fetch_params` 等本讲引用的开关 |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py) | 引擎侧 dtype 转换关卡与 `_keeps_params_in_fp32` 能力判定（本讲只引用，详解在 u5-l9） |
| [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py) | keep_in_fp32 全家桶测试，本讲实践的依据 |

## 4. 核心概念与源码讲解

### 4.1 Init 上下文管理器：给 nn.Module 注入「出生即切分」钩子

#### 4.1.1 概念说明

普通方式训练 ZeRO-3 有一个隐藏前提：模型必须**先被完整构造出来**（每卡一份完整的 fp32 参数），然后引擎才能把它切分。对一个万亿参数模型，fp32 权重是 4TB——每张卡、每个进程都要先在 CPU 内存里装下这 4TB，构造完成才能释放。这堵墙与训练时的稳态显存无关，纯粹是「出生时刻」的瞬时峰值。

`zero.Init` 的解法是把切分时机提前到构造期：在每个 `nn.Module.__init__` 结束的瞬间，立刻把它刚创建的参数切分、丢掉全量副本。这样任意时刻每张卡最多只持有「正在构造的那一层」的全量参数，构造完成即释放。官方 docstring 用万亿参数模型算了这笔账：8 卡节点本来需要 32TB CPU 内存（数据并行冗余），切分后无论多少卡都只需要原样的 4TB。

#### 4.1.2 核心流程

```text
with deepspeed.zero.Init():
    model = MyLargeModel()
    │
    ├─ __enter__: patch torch.nn.Module.__init__（在每个子模块 __init__ 末尾追加 _post_init_method）
    │             zero_init_context 深度计数 +1（支持嵌套 with）
    │
    ├─ MyLargeModel() 内部：每个子模块 __init__ 结束时触发 _post_init_method(module)
    │    ├─ 遍历 module.named_parameters(recurse=False)（只看本模块直属参数）
    │    ├─ param 搬到本卡 → 广播对齐各 rank 的初始值 → param.partition() 切分
    │    └─ self._keep_in_fp32(module)   ← #8843 新增收尾钩子（见 4.5）
    │
    └─ __exit__: 恢复被 patch 的 __init__，打印参数统计
```

要点：子模块是**从内向外**逐层构造的——`Linear` 先于 `TransformerLayer`、`TransformerLayer` 先于整个 model——所以 `_post_init_method` 会在每层构造完的瞬间就切掉它，父模块后续对这些权重的修改则由包装过的 `Module.apply` 走「先 gather 再改再 partition」的路径保证不丢失。

#### 4.1.3 源码精读

基类 `InsertPostInitMethodToModuleSubClasses` 负责进出上下文时安装/卸载补丁，用模块级计数器 `zero_init_context` 支持嵌套，并在顶层上下文退出时打印参数统计：

- [deepspeed/runtime/zero/partition_parameters.py:L353-L386](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L353-L386)：`__enter__` 里只有 `zero_init_context == 0`（最外层）才真正 `patch_init_and_builtins()`；`__exit__` 把计数减一，归零时卸载补丁并让 rank 0 打印 `finished initializing model - num_params = ...`。

`Init` 类继承它，构造函数先解析配置并读取 keep_in_fp32 设置：

- [deepspeed/runtime/zero/partition_parameters.py:L946-L953](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L946-L953)：`class Init(InsertPostInitMethodToModuleSubClasses)`，类属性里还有 `param_persistence_threshold` 等持久化阈值（见 4.2）。
- [deepspeed/runtime/zero/partition_parameters.py:L1079-L1083](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1079-L1083)：从 `DeepSpeedConfig` 读出 `keep_in_fp32_modules`（`data_types.keep_in_fp32_modules`，默认 `"auto"`）；若未初始化分布式则顺带 `init_distributed()`。
- [deepspeed/runtime/zero/partition_parameters.py:L1131-L1135](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1131-L1135)：`local_device` 由 `LOCAL_RANK` 环境变量决定。注意：**当前版本没有 `gpu_id` 参数**（旧版教程里的 `gpu_id` 已移除），设备选择完全走 `LOCAL_RANK`。

`_post_init_method` 是整个机制的心脏：

- [deepspeed/runtime/zero/partition_parameters.py:L1227-L1248](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1227-L1248)：遍历**直属**参数（`recurse=False`，因为子模块早已各自处理过），跳过已是 zero param 的；不在加速器上的先搬到本卡；名字为 `weight` 且配置了量化初始化的先量化；然后 `_zero_init_param` 完成切分。
- [deepspeed/runtime/zero/partition_parameters.py:L1195-L1202](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1195-L1202)：`_zero_init_param` 三步——转成 deepspeed 参数（挂上 `ds_tensor` 等属性）、在分区组内从 rank 0 **广播**初始值（否则各 rank 各自随机初始化，切出的分片拼起来就不是同一个模型）、最后 `param.partition()`。

除了 `with` 语法，`Init` 还提供「事后切分」入口，用于已经构造好的 CPU 模型：

- [deepspeed/runtime/zero/partition_parameters.py:L1173-L1177](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1173-L1177)：传入 `module=` 时走 `_convert_to_zero_parameters`（注意这条路没有广播步骤，要求各 rank 模型一致），随后同样调用 `_keep_in_fp32(module)`——两个入口共享收尾钩子。

`free_param` 解释了「切分后参数本体去哪了」——`param.data` 被替换成一个 0 元素占位张量，真实数据只在 `ds_tensor` 分区里：

- [deepspeed/runtime/zero/partition_parameters.py:L308-L325](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L308-L325)：`param.data = torch.empty(0, dtype=param.dtype, device=param.device)`，状态置为 `NOT_AVAILABLE`。这就是为什么 ZeRO-3 下直接读 `param.data` 会得到空张量、必须经 `GatheredParameters` 或 hook 触发的 all-gather 取回。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「参数出生即切分」——构造完的模型里，`param.data` 是 0 元素占位符，真实数据躺在 `param.ds_tensor` 里。

**操作步骤**（单进程即可，CPU 环境可跑）：

```python
# practice_zero_init_basic.py（示例代码）
import torch
import deepspeed

with deepspeed.zero.Init(dtype=torch.bfloat16):
    linear = torch.nn.Linear(1000, 1000)

w = linear.weight
print("ds_status:", w.ds_status)          # NOT_AVAILABLE：已切分、未取回
print("data.shape:", w.data.shape)        # torch.Size([0])：占位符
print("ds_tensor:", w.ds_tensor)          # 本 rank 持有的分区
print("ds_numel:", w.ds_numel)            # 原始元素数 1000000
print("ds_numel_aligned:", w.ds_numel_aligned)
print("partition numel:", w.ds_tensor.ds_numel)
```

运行：`torchrun --nproc_per_node=1 practice_zero_init_basic.py`（若缺 GPU 用 `deepspeed --num_cpus 1` 或设置 DS_ACCELERATOR=cpu，待本地验证）。

**需要观察的现象**：`data.shape` 为 `[0]`；`ds_tensor.ds_numel` 等于 `ds_numel_aligned`（world_size=1 时二者都等于 1000000，padding 为 0）。

**预期结果**：world_size=1 时一切数值都相等——这恰好说明单进程下切分是「恒等」的，也解释了 4.3 的 NoGather 优化为什么存在。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_post_init_method` 里遍历参数用 `recurse=False`？

**答案**：子模块构造完成时它的参数已被切分并挂上 `ds_tensor`（变成 zero param），父模块的 `_post_init_method` 只需处理自己新建的直属参数；递归遍历只会重复处理已切分参数（代码里也用 `is_zero_param(param)` 跳过，但 `recurse=False` 从根上避免了）。

**练习 2**：`_zero_init_param` 为什么在 partition 之前先 broadcast？

**答案**：`nn.Linear` 等层的默认初始化是随机采样，各进程种子不同则各 rank 拿到的「完整模型」不同；先在分区组内从 rank 0 广播，保证所有 rank 从同一份初值切分，切出来的分片才能拼回同一个模型。

**练习 3**：`with zero.Init():` 里再嵌套一层 `with zero.Init():` 会重复 patch 吗？

**答案**：不会。`zero_init_context` 计数器保证只有最外层（计数从 0 变 1）执行 `patch_init_and_builtins`，内层只加计数；退出时计数归零才卸载，见 [partition_parameters.py:L353-L376](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L353-L376)。

### 4.2 partition：对齐、分桶与就地切分

#### 4.2.1 概念说明

「把参数切给 W 个 rank」需要先回答一个除法问题：\(N\) 个元素分给 \(W\) 份，除不尽怎么办？答案是 padding：补零凑成 \(W\) 的倍数，再等分。同时，并非所有参数都值得切分——小于阈值的参数（比如 norm 的 scale/bias）保持复制（persist）反而省通信。

需要澄清一个历史名词：旧版 DeepSpeed 教材里的 `max_num_params`（限制单次 all-gather 的参数量）在当前代码中已不存在；与之配套的 bucket 控制现在由 stage3 优化器侧的 `stage3_prefetch_bucket_size`、`reduce_bucket_size` 等配置承担（见 [zero/config.py:L113-L255](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/config.py#L113-L255)，详解在 u5-l6）。`Init` 层面的切分只由 world size 和持久化阈值决定。另外注意：配置项 `parameter_alignment`（[zero/config.py:L327](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/config.py#L327)）属于 ZeRO-1/2 的扁平缓冲对齐（stage_1_and_2.py），与本讲的按 world size 对齐是两回事。

#### 4.2.2 核心流程

设参数元素数为 \(N\)（`ds_numel`），分区组大小为 \(W\)，则：

\[
P = (W - N \bmod W) \bmod W \quad\text{（padding）}
\]

\[
A = N + P \quad\text{（aligned\_size）}, \qquad S = \frac{A}{W} \quad\text{（每 rank 分区大小）}
\]

rank \(r\) 负责原参数展平后的区间 \([rS,\ (r+1)S)\)，区间尾巴越过 \(N\) 的部分不拷贝（保持新分配的空值）。因此**所有 rank 的分区拼起来是 \(A\) 个元素 ≥ \(N\)**，多出来的就是 padding。

流程伪代码：

```text
partition(param):
    若 ds_tensor 已存在且未被更新 → 只释放 param.data（重复 partition 幂等）
    A = ds_numel + padding_size
    S = A // world_size
    分配 partitioned_tensor（remote_device 上，S 个元素，dtype=param.dtype）
      ├─ ds_persist 且很小 → 留在本卡
      ├─ remote_device=nvme → 从 swapper 拿缓冲
      ├─ 不可训练且开 zero_quantized_nontrainable_weights → 存 int8 码 + ds_quant_scale
      └─ cpu 且 pin_memory → 锁页
    把 one_dim_param 的 [rS, min((r+1)S, N)) 拷进本 rank 分区
    free_param(param)  # param.data 变 0 元素占位符
```

#### 4.2.3 源码精读

- [deepspeed/runtime/zero/partition_parameters.py:L1701-L1710](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1701-L1710)：对齐三件套 `_aligned_size` / `_padding_size` / `_partition_numel`，就是上面两个公式的直译。
- [deepspeed/runtime/zero/partition_parameters.py:L1785-L1844](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1785-L1844)：`_partition_param` 的分配段——`tensor_size = self._aligned_size(param)`、`partition_size = tensor_size // self._partition_world_size(param)`；随后按 remote_device/persist/量化三种情况分配分区张量，并记录 `ds_numel_aligned`。
- [deepspeed/runtime/zero/partition_parameters.py:L1846-L1873](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1846-L1873)：拷贝段——`start = partition_size * rank`；完整落在参数内则整段 `copy_`，跨界（最后一个有效 rank）则只 `narrow` 拷有效元素；最后 `free_param` 释放全量副本。
- [deepspeed/runtime/zero/partition_parameters.py:L1776-L1783](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1776-L1783)：`_partition` 对外入口；hpZ（`zero_hpz_partition_size > 1`）时还会额外切一份节点内二级分区 `_partition_param_sec`。
- [deepspeed/runtime/zero/partition_parameters.py:L1296-L1301](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1296-L1301)：持久化判定——`param.ds_numel <= param_persistence_threshold` 且累计元素不超过 `model_persistence_threshold` 时 `ds_persist = True`，小参数全程复制、免通信。两个阈值的配置别名是 `stage3_param_persistence_threshold`（[zero/config.py:L238](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/config.py#L238)）。

#### 4.2.4 代码实践

**实践目标**：用手算公式预测 `ds_tensor.ds_numel`，再与程序输出对账。

**操作步骤**（示例代码，两进程观察 padding）：

```python
# practice_partition_math.py（示例代码）
import torch
import deepspeed

with deepspeed.zero.Init(dtype=torch.bfloat16):
    m = torch.nn.Linear(7, 5)   # 35 个元素 + bias 5 个 = 两个参数

for name, p in m.named_parameters():
    W = deepspeed.comm.get_world_size()
    pad = (W - p.ds_numel % W) % W
    print(f"{name}: ds_numel={p.ds_numel} world={W} "
          f"aligned={p.ds_numel_aligned} expected={p.ds_numel + pad} "
          f"partition={p.ds_tensor.ds_numel} expected={p.ds_numel // W + (1 if pad or True else 0)}")
```

运行：`torchrun --nproc_per_node=2 practice_partition_math.py`。

**需要观察的现象**：`weight`（35 元素）在 W=2 下 padding=1，aligned=36，每 rank 分区 18；`bias`（5 元素）padding=1，aligned=6，分区 3。rank 1 的 weight 分区只有前 17 个元素是有效值、第 18 个是 padding。

**预期结果**：`ds_numel_aligned == ds_numel + pad`、`ds_tensor.ds_numel == aligned // W` 在每个参数上都成立；两 rank 分区数之和 = aligned ≥ ds_numel。（具体打印值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：N=1000、W=3，padding、aligned、每 rank 分区各是多少？哪个 rank 的分区里有 padding 元素？

**答案**：\(1000 \bmod 3 = 1\)，\(P = 2\)，\(A = 1002\)，\(S = 334\)。rank 2 的区间是 [668, 1002)，其中 [1000, 1002) 两个元素是 padding（代码里 start < ds_numel 才拷贝，rank 2 只拷 668~999 共 332 个有效元素，分区尾部两个保持未写）。

**练习 2**：为什么每个 rank 的分区大小必须相等（而不是「最后一人拿零头」）？

**答案**：取回路径依赖 all-gather：各 rank 出一份等长分区、拼成一块平坦大缓冲，再从中 narrow 出每个参数的完整形状（见 4.3）。等长分区让 all-gather 的输出布局在各 rank 上一致、可预测；不等长则每个参数在缓冲中的偏移因 rank 而异，无法一次通信取回。

**练习 3**：`stage3_param_persistence_threshold` 设成很大的值会发生什么？

**答案**：几乎所有参数 `ds_persist=True`，`_partition_param` 分配分区时 `device = self.local_device`（留在本卡不卸载）， ZeRO-3 退化为「参数常驻 + 仍然走 hook 式生命周期管理」，省了通信但失去切分的显存收益——这正是 keep_in_fp32 测试里把它设为 0 的反例：测试要强制**每个**参数都真正切分（[test_keep_in_fp32_modules.py:L246-L248](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L246-L248)）。

### 4.3 取回参数：allgather 句柄家族与 NoGather 优化

#### 4.3.1 概念说明

切分只是硬币的一面；forward/backward 每用到一层，都要把这层参数的 W 份分区拼回完整张量。DeepSpeed 把「发起通信 + 稍后取结果」封装成**句柄（handle）**对象，`wait()` 里做真正的拼接。句柄家族有五名成员：

| 句柄 | 触发条件 | 通信方式 |
| --- | --- | --- |
| `NoGatherHandle` / `NoGatherCoalescedHandle` | 分区组 world_size == 1 | 无通信，直接 view 回来 |
| `AllGatherHandle` | 顺序（逐参数）取回 | 一次 all_gather |
| `AllGatherCoalescedHandle` | 多参数合并取回（默认） | 一次 all_gather + 本地拼接 |
| `AllReduceCoalescedHandle` | 配置 `stage3_use_all_reduce_for_fetch_params=true` | 一次 all_reduce（每 rank 写自己那段再求和） |
| `MultipleAllGatherHandles` | 以上任一情况的「多份打包」 | 依次 wait |

参数状态机贯穿始终：`NOT_AVAILABLE`（已切分）→ `INFLIGHT`（通信中）→ `AVAILABLE`（可用），`free_param` 再把它打回 `NOT_AVAILABLE`。

**all_reduce 为什么能替代 all_gather**：all_reduce 让每个 rank 都拿到「所有 rank 输入之和」。每个 rank 先把一块全零平坦缓冲中**自己负责的那段**写成自己的分区，其余段保持零，求和后每个 rank 的缓冲都等于「按 rank 顺序拼接的完整参数」——与 all_gather 的结果相同，但通信量从 \(W \times S\) 降为约 \(2 \times S\)（W 较大时）。代价是它要求**同一桶内所有参数同 dtype**（一块平坦缓冲只能一种 dtype）。

#### 4.3.2 核心流程

`all_gather_coalesced(params)` 的分发逻辑：

```text
params 按 ds_id 排序                     # 各 rank 顺序必须一致，否则通信错位/死锁
├─ 空列表 → NoGatherCoalesced
├─ 分区组 world_size == 1 → NoGatherCoalesced
├─ use_all_reduce_for_fetch_params 且非量化且非 hpZ:
│    ├─ 分区 dtype 不止一种（#8843: fp32 kept 参数 + bf16 参数混桶）
│    │    → 按 dtype 拆桶，每 dtype 递归一次 → MultipleAllGatherHandles
│    └─ 单 dtype → 一块平坦缓冲一次 all_reduce → AllReduceCoalescedHandle
└─ 常规 all-gather 路径:
     按「通信 dtype」分桶（autocast 感知）→ 每桶一块 flat_tensor
     各 rank 把自己分区 cat 进 flat_tensor 的本 rank 段 → all_gather
     → AllGatherCoalescedHandle.wait(): 从 flat_tensor 逐参数 narrow + cat + view(ds_shape)
```

#### 4.3.3 源码精读

- [deepspeed/runtime/zero/partition_parameters.py:L1552-L1596](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1552-L1596)：入口 `all_gather_coalesced`——先按 `ds_id` 排序（注释解释了 set 的顺序因 rank 而异会静默出错甚至死锁）、按分区组分桶、world_size=1 走 `_no_gather_coalesced`，否则把参数置为 `INFLIGHT` 并选进程组。
- [deepspeed/runtime/zero/partition_parameters.py:L1450-L1478](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1450-L1478)：**#8843 的新分支**——`use_all_reduce_for_fetch_params` 路径下先收集 `dtypes = {p.ds_tensor.dtype for p in params}`，多于一种时按 `sort_dtypes` 顺序逐 dtype 递归 `_all_gather_coalesced`，包成 `MultipleAllGatherHandles`（注释点名了原因：keep_in_fp32 的 fp32 参数与 bf16/fp16 参数同桶）。单 dtype 时按原逻辑：每参数在自己 `ds_numel_aligned` 槽位内 `narrow` 出视图、本 rank 段 `copy_` 自己的分区，一次 `dist.all_reduce`。
- [deepspeed/runtime/zero/partition_parameters.py:L1333-L1375](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1333-L1375)：`_all_gather_dtype`——先 assert 桶内通信 dtype 一致，然后 `torch.cat` 各 rank 分区进 `flat_tensor` 本 rank 段、all_gather、返回 `AllGatherCoalescedHandle`。`get_allgather_dtype`（[L56-L58](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L56-L58)）在 autocast 场景返回统一通信 dtype。
- [deepspeed/runtime/zero/partition_parameters.py:L730-L805](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L730-L805)：`AllGatherCoalescedHandle.wait()`——对每个参数，从 W 个 rank 段里各 `narrow` 出属于自己的切片，`torch.cat(partitions).view(ds_shape)` 还原完整参数并置 `AVAILABLE`。
- [deepspeed/runtime/zero/partition_parameters.py:L61-L86](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L61-L86)：`NoGatherHandle`——单 rank 时无通信，直接把 `ds_tensor.data` view 回 `param.data`（量化分区则先反量化）。
- [deepspeed/runtime/zero/partition_parameters.py:L698-L727](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L698-L727)：`AllGatherHandle`——顺序路径的句柄；注意 L711-L719 对「通信 dtype ≠ 存储 dtype」的处理：`param.data` 先保持分区态视图，避免 tied 参数的梯度 hook 被多次触发（注释里有完整解释）。
- [deepspeed/runtime/zero/partition_parameters.py:L2292-L2366](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L2292-L2366)：`GatheredParameters`——用户侧最常用的取回入口：进上下文 gather、退出时（若指定 `modifier_rank`）广播修改并重新 partition。docstring 里的「Pretrained model loading」示例就是 transformers 在 ZeRO-3 下加载检查点的标准写法。
- [deepspeed/runtime/zero/config.py:L283](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/config.py#L283)：`use_all_reduce_for_fetch_params` 的配置别名 `stage3_use_all_reduce_for_fetch_params`，默认 False。

#### 4.3.4 代码实践

**实践目标**：验证「切分 → 取回」往返无损，并体会 `GatheredParameters` 的用法。

**操作步骤**（示例代码）：

```python
# practice_gather_roundtrip.py（示例代码）
import torch
import deepspeed

# 先在 Init 外构造并保存一份初值，再用 module= 入口切分
torch.manual_seed(0)
m = torch.nn.Linear(64, 64, bias=False)
saved = m.weight.detach().clone()

deepspeed.zero.Init(module=m, dtype=torch.bfloat16)
print("after Init:", m.weight.data.shape)          # [0]：已切分

with deepspeed.zero.GatheredParameters([m.weight], modifier_rank=0):
    print("gathered:", m.weight.data.shape)        # [64, 64]
    diff = (m.weight.data.float() - saved.float()).abs().max()
    print("max diff vs saved:", diff.item())

# 退出上下文后参数应再次回到分区态
print("after exit:", m.weight.data.shape)          # 预期 [0]
```

运行：`torchrun --nproc_per_node=1 practice_gather_roundtrip.py`。

**需要观察的现象**：Init 后 `data.shape=[0]`；GatheredParameters 内恢复 `[64, 64]`；退出后回到 `[0]`；diff 为 0（world_size=1 无通信、无 dtype 变化时精确往返）。

**预期结果**：单进程下 `max diff = 0.0`。换成 2 进程 + bf16 时，若保存的初值是 fp32，往返会引入 bf16 舍入（Init 的 dtype 转换发生在切分前），diff 不再为 0——这个对比正好演示 4.5 要解决的「精度丢失」问题。（具体数值待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`all_gather_coalesced` 为什么必须先 `sorted(params, key=lambda p: p.ds_id)`，哪怕调用方传进来的是同一个 set？

**答案**：合并 all-gather 把参数列表展平成一块缓冲、一次通信完成，要求每个 rank 按**相同顺序**排列参数；Python set 的迭代顺序因进程而异（受哈希种子影响），不排序会导致各 rank 的缓冲布局不一致，静默取错值；分组路径下不同顺序还会让各 rank 以不同顺序发起集合通信造成死锁（源码注释 [L1556-L1562](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1556-L1562)）。

**练习 2**：#8843 之前，一个 keep_in_fp32 的 fp32 参数与 bf16 参数混进同一个 all-reduce 桶会发生什么？

**答案**：all-reduce 路径用 `get_only_unique_item(p.ds_tensor.dtype for p in params)` 决定整块平坦缓冲的 dtype——桶内出现两种 dtype 会破坏「唯一」假设（旧代码在此处会直接失败或被迫统一 dtype，fp32 分区被舍入到 bf16 就抵消了保留意义）。#8843 在入口先按 dtype 拆桶，每 dtype 一块缓冲一次 all_reduce，语义变为「每种 dtype 独立、精确」。

**练习 3**：`NoGatherHandle` 存在的意义是什么？它省掉了什么？

**答案**：分区组 world_size=1（单卡训练、或子组划分后某参数只在组内切分）时 all_gather 是恒等操作，通信是纯开销；NoGather 跳过通信，直接把本 rank 的分区 `view` 回 `param.data`。它省掉的是一次「把自己那份数据发给自己」的集体通信调用及其框架开销。

### 4.4 register_external_parameter：不在 forward 里用的权重靠谁取回

#### 4.4.1 概念说明

ZeRO-3 的按需取回靠 **module hook**：参数的宿主模块执行 `forward` 前 hook 触发 gather、执行后释放。但有些权重不在宿主模块的 `forward()` 里被使用——典型例子是「模块 A 的权重被模块 B 直接按张量读取」。这种参数没有 hook 能挂，forward 时它会一直是 0 元素占位符，静默算出错误结果。

`register_external_parameter(module, parameter)` 把参数登记到 `module._external_params`（按 `id(parameter)` 索引），等于告诉 DeepSpeed：「在 `module` 的 forward/backward 期间，请替我收集并释放这个参数」。参数协调器（u5-l5 详解）在扫描模块依赖时会把这些外部参数一并纳入 gather/release 计划。

#### 4.4.2 核心流程

```text
register_external_parameter(module, P)
  ├─ 校验 P 是 nn.Parameter
  ├─ module 无 _external_params 则初始化登记表
  └─ module._external_params[id(P)] = P

运行期（parameter_offload 协调器）：
  扫描某模块的 forward 依赖 = 直属参数 + module.ds_external_parameters()
  → gather 一起取回 / backward 后一起释放
```

#### 4.4.3 源码精读

- [deepspeed/runtime/zero/partition_parameters.py:L151-L200](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L151-L200)：`register_external_parameter` 全文——docstring 给出了标准示例：`layer2` 需要 `layer1.weight`，就在 `__init__` 里 `deepspeed.zero.register_external_parameter(self, self.layer1.weight)`。
- [deepspeed/runtime/zero/partition_parameters.py:L203-L221](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L203-L221)：对应的 `unregister_external_parameter`。
- [deepspeed/runtime/zero/partition_parameters.py:L2418](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L2418)：`GatheredParameters(..., fwd_module=self)` 会自动替你注册——在 forward 里临时 gather 一个外部权重的推荐写法。
- 真实调用点：[deepspeed/models/glm_moe_dsa/attention.py:L207](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/models/glm_moe_dsa/attention.py#L207)（GLM-5.2 稀疏注意力的 `kv_b_proj.weight` 只按张量读取，见 u9-l6）与 [deepspeed/runtime/zero/parameter_offload.py:L396](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/parameter_offload.py#L396)（协调器内部）。

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——找出「不注册外部参数会静默出错」的证据链。

**操作步骤**：

1. 读 [partition_parameters.py:L183-L191](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L183-L191) 的示例：`layer2` 在 forward 里接收 `self.layer1.weight` 作为输入。
2. 打开 [tests/unit/v1/models/test_glm_moe_dsa.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/models/test_glm_moe_dsa.py) 中的 ZeRO-3 用例（`TestDsaAttentionZeroStage3`），看它如何断言未注册时 `kv_b_proj.weight` 在前向里是 0 元素占位符（详见 u9-l6）。
3. 写一个 5 行最小复现（示例代码，待本地验证）：

```python
with deepspeed.zero.Init():
    layer1 = torch.nn.Linear(8, 8, bias=False)
    holder = torch.nn.Module()

def holder_forward(x):
    return torch.matmul(x, layer1.weight)   # holder 不是 layer1，没有 hook 替它 gather

deepspeed.zero.register_external_parameter(holder, layer1.weight)
# 对比注册/不注册这一行，holder_forward 的输出形状与数值
```

**需要观察的现象**：不注册时 `layer1.weight` 形状为 `[0]`，`matmul` 直接报形状错误（这是幸运的情况——维度不匹配会立刻暴露；更危险的是形状碰巧合法时静默算错）。

**预期结果**：注册后 DeepSpeed 在 `holder` 的 forward 前取回权重；不注册则报错或得到错误输出。注册调用应放在**建模后、deepspeed.initialize 前**（更常见）或模块 `__init__` 内（如 glm_moe_dsa 的做法）。

#### 4.4.5 小练习与答案

**练习 1**：`GatheredParameters(..., fwd_module=self)` 与手动 `register_external_parameter` 是什么关系？

**答案**：前者是后者的语法糖——进入上下文时若指定 `fwd_module`，会自动把这些参数注册为该模块的外部参数（[L2418](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L2418)）， backward 需要该参数时协调器也知道要再次 gather。适合临时使用；长期跨模块共享建议在 `__init__` 里显式注册。

**练习 2**：为什么登记表用 `id(parameter)` 而不是参数名做 key？

**答案**：外部参数可能属于另一个模块（名字空间不同，无统一命名），且同一参数可能被多个模块注册；`id()` 是 Python 对象身份，直接唯一标识这个 `Parameter` 实例，免去命名冲突问题。

### 4.5 收尾钩子：keep_in_fp32 就地转换（#8843 本周期核心变更）

#### 4.5.1 概念说明

`zero.Init` 会把构造期创建的**所有浮点张量**转成训练 dtype（bf16/fp16）。但 transformers 模型会点名某些张量必须留在 fp32——类属性 `_keep_in_fp32_modules_strict`（bf16/fp16 下都保）与 `_keep_in_fp32_modules`（仅 fp16 下保）。典型如 DeepSeek 系的 MoE 路由 bias `e_score_correction_bias`：GLM-5.2 中它的取值在 5~20 之间、彼此相差不到 0.01，而 bf16 在该区间的可表示值间隔是 0.03~0.125——转成 bf16 后 256 个专家的 bias 塌缩成 3~16 个不同值，**直接改变 token 被路由到哪个专家**。

问题在于时机：`zero.Init` 在 `__init__` 里就完成了 dtype 转换，而 transformers 的检查点加载发生在**之后**——列表信息在转换那一刻已经丢了。#8843 的解法是给 `Init` 加一个**收尾钩子**：每个模块的 `_post_init_method` 末尾调用 `_keep_in_fp32(module)`，把点名参数/缓冲**就地**转回 fp32。

四个关键设计：

1. **时机**：transformers 模型自身 `__init__` 跑完的瞬间——它的 fp32 列表此刻是完整的（嵌套子模块的列表也能通过 `named_modules()` 收集到）、它的子模块参数已经分区（转换发生在**分区上**）、而任何检查点还没加载（之后的加载会写入精确 fp32 值）。
2. **就地转换分区**：ZeRO-3 参数的本体是 `ds_tensor` 分区，`keep_tensors_in_fp32` 直接 `partition.data = partition.data.float()`——此后 gather 返回 fp32、检查点加载写入的 fp32 值精确存活。hpZ 的 `ds_secondary_tensor`（节点内二级副本）同步转换。
3. **量化分区保持量化**：`zero_quantized_nontrainable_weights` 把冻结权重存成 int8 码 + 缩放因子；把 int8 码转成 fp32 得到的是乱码而不是原值，因此跳过并告警。
4. **参数卸载只保 buffers**：`remote_device` 非 `none`（分区躺在 CPU/NVMe 上的单一 dtype 平坦缓冲）时，分区没有独立的 dtype 可言，参数退回训练 dtype、仅缓冲转 fp32，并打 warning。

#### 4.5.2 核心流程

```text
_post_init_method(module) 末尾:
    _keep_in_fp32(module)
        pattern = keep_in_fp32_pattern(module, 配置, 训练dtype)
          ├─ 训练dtype 是 fp32 → None（无需保留）
          ├─ "auto" → 收集 module.named_modules() 里各子模块的
          │   _keep_in_fp32_modules_strict (+fp16 时 _keep_in_fp32) 列表 → 编译正则
          └─ 显式列表 → 列表项做 glob(*→.*) → 编译正则
        若 pattern 为 None → 返回

        params_offloaded = (remote_device != local_device)
        keep_tensors_in_fp32(module, pattern, include_params=not params_offloaded)
          对每个匹配的浮点参数/缓冲:
            ├─ 分区带 ds_quant_scale → 告警 + 跳过（量化保持量化）
            ├─ 存储已是 fp32 → 跳过
            ├─ 有 ds_tensor → partition.data.float()
            │     └─ ds_secondary_tensor 存在且未量化 → 同步 .float()
            └─ tensor.data.float()（占位符/已 gather 的全量）
        params_offloaded → warning("parameters offloaded to ... stay <dtype>; only buffers ...")
```

下游联动（本讲只点出、详见 u5-l9）：stage3 优化器按分区 dtype 拆子组与梯度缓冲；`use_all_reduce_for_fetch_params` 下取回按 dtype 拆桶（4.3）；引擎在 ZeRO-0/1/2 或开参数卸载时把点名参数 cast 到训练 dtype——auto 模式 warning、显式列表 ValueError（[engine.py:L1938-L1947](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1938-L1947)）；能力判定 `_keeps_params_in_fp32` 的依据是「只有 ZeRO-3 且无参数卸载才持有按 dtype 的分区缓冲」（[engine.py:L1927-L1936](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1927-L1936)）。

#### 4.5.3 源码精读

- [deepspeed/runtime/zero/partition_parameters.py:L1250-L1272](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1250-L1272)：`Init._keep_in_fp32` 全文——模式为空早退；`params_offloaded = self.remote_device != self.local_device` 决定 `include_params`；告警用 `Init._warned_keep_in_fp32_offload` 类属性保证只打一次。
- [deepspeed/runtime/keep_in_fp32.py:L39-L62](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L39-L62)：`keep_in_fp32_pattern`——auto 模式遍历 `named_modules()` 收集嵌套列表（`*` 转 `.*`、拼接父模块名前缀），与 transformers 的 `core_model_loading.build_glob_alternation` 同规则；fp32 训练或空列表返回 None。
- [deepspeed/runtime/keep_in_fp32.py:L65-L76](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L65-L76)：`tensors_to_keep_in_fp32`——递归产出 `(全名, 张量, 是否buffer)` 三元组，引擎与 Init 共用。
- [deepspeed/runtime/keep_in_fp32.py:L85-L120](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L85-L120)：`keep_tensors_in_fp32` 就地转换主体——量化分区跳过（L98-L102 的 warning_once）、`partition.data = partition.data.float()`（L106）、hpZ 二级副本同步转换（L110-L112）、占位符/全量 `tensor.data.float()`（L115，注释说明这保证下一次 gather 与 free 都保持 fp32）。
- [deepspeed/runtime/keep_in_fp32.py:L5-L28](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L5-L28)：模块 docstring——完整交代动机（路由 bias 的 bf16 塌缩数字）、配置项 `data_types.keep_in_fp32_modules` 的三种取值（`"auto"`/显式模式列表/`[]`）与参数级保留的适用范围。
- [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py:L359-L378](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L359-L378)：`TestZeroInitKeepsParamsFp32.test_loaded_values_survive`——auto 下 `_stored_dtype(sink) == torch.float32` 且 gather 回的值与写入的 `EXACT_VALUES` 逐位相等；`keep=[]` 时塌缩成全 8.0（被修复的行为）。
- [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py:L381-L403](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L381-L403)：`TestZeroInitKeepsQuantizedParams`——冻结的点名参数在 `zero_quantized_nontrainable_weights` 下分区保持 `int8` + `ds_quant_scale`，训练一步后仍是 int8。
- [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py:L406-L426](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L406-L426)：`TestZeroInitKeepsParamsFp32Hpz`——hpZ（`zero_hpz_partition_size=2`）下第一个 forward 读到的是二级副本，因此二级副本也必须保 fp32 值。

#### 4.5.4 代码实践

**实践目标**：复现 `TestZeroInitKeepsParamsFp32` 的核心断言——分区就地转换后，写入的 fp32 值精确存活；对照 `keep=[]` 看 bf16 塌缩。

**操作步骤**（示例代码，改编自 [tests/unit/v1/half_precision/test_keep_in_fp32_modules.py:L210-L221](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L210-L221) 的 `ToyModelWithParams`）：

```python
# practice_keep_fp32.py（示例代码）
import torch
import deepspeed

HIDDEN = 64
# 在 8.0 附近、bf16 分辨不开的一组值（bf16 在 8.0 处间隔 0.0625）
EXACT = 8.0 + torch.arange(HIDDEN, dtype=torch.float32) * 1e-4

class Toy(torch.nn.Module):
    _keep_in_fp32_modules_strict = ["sink", "router_bias"]
    def __init__(self):
        super().__init__()
        self.linear = torch.nn.Linear(HIDDEN, HIDDEN, bias=False)
        self.sink = torch.nn.Parameter(torch.full((HIDDEN,), 8.0))
        self.register_buffer("router_bias", torch.zeros(HIDDEN))
    def forward(self, x):
        return ((self.linear(x) * self.sink + self.router_bias)**2).mean()

def config(keep):
    return {
        "train_micro_batch_size_per_gpu": 1,
        "optimizer": {"type": "Adam", "params": {"lr": 1e-3}},
        "zero_optimization": {"stage": 3, "stage3_param_persistence_threshold": 0},
        "bf16": {"enabled": True},
        "data_types": {"keep_in_fp32_modules": keep},
    }

for keep in ("auto", []):
    cfg = config(keep)
    with deepspeed.zero.Init(config_dict_or_path=cfg):
        model = Toy()
    # 模拟 transformers 的 ZeRO-3 检查点加载路径
    with deepspeed.zero.GatheredParameters([model.sink], modifier_rank=0):
        model.sink.data.copy_(EXACT)
    stored = model.sink.ds_tensor.dtype if hasattr(model.sink, "ds_tensor") else model.sink.dtype
    with deepspeed.zero.GatheredParameters([model.sink]):
        back = model.sink.data.detach().clone()
    print(f"keep={keep!r}: stored dtype={stored}, "
          f"exact存活={torch.equal(back.cpu(), EXACT)}, "
          f"全部塌缩为8.0={torch.equal(back.float().cpu(), torch.full((HIDDEN,), 8.0))}")
```

运行：`torchrun --nproc_per_node=2 practice_keep_fp32.py`（量化用例需 GPU：在 config 里加 `"zero_quantized_nontrainable_weights": True` 并把 `sink` 设为 `requires_grad=False`，对照 `TestZeroInitKeepsQuantizedParams`）。

**需要观察的现象**：`keep="auto"` 时 `stored dtype=torch.float32`、`exact存活=True`；`keep=[]` 时 `stored dtype=torch.bfloat16`、`全部塌缩为8.0=True`——同一份写入值，两种命运。

**预期结果**：与测试断言一致（[test_keep_in_fp32_modules.py:L371-L377](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/half_precision/test_keep_in_fp32_modules.py#L371-L377)）。量化分支的具体输出待本地验证（需要 CUDA 加速器跑量化器）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `_keep_in_fp32` 放在 `_post_init_method`（每个模块都触发一次）末尾，而不是 `Init.__exit__` 里只做一次？

**答案**：`_post_init_method` 在 transformers 模型自身 `__init__` 刚结束时就拿到了完整的 fp32 列表（含嵌套子模块的），且此刻必须立刻转换——因为用户的下一行代码可能就是检查点加载（`GatheredParameters` + `copy_`），等 `with` 块退出再转就晚了：加载写入的 fp32 值会先被 bf16 分区舍入。放在每个模块的收尾处也天然覆盖了 `Init(module=...)` 的事后切分入口（[L1177](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1177)）。

**练习 2**：`keep_tensors_in_fp32` 对量化分区为什么「保持量化并告警」而不是报错？

**答案**：量化权重（`zero_quantized_nontrainable_weights`）的分区存的是 int8 码和缩放因子，`partition.data.float()` 得到的是码值不是权重值，转换毫无意义；但这些参数必然是冻结的（不可训练），它们的数值精度由量化本身决定、与 fp32 保留机制的目标（训练中分辨微小更新/差异）无关，因此降级为 warning 让训练继续（[keep_in_fp32.py:L98-L102](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/keep_in_fp32.py#L98-L102)）。

**练习 3**：参数卸载（`remote_device="cpu"`）时为什么参数不能保留 fp32？

**答案**：卸载分区的存储形态是「一块单一训练 dtype 的平坦 CPU/NVMe 缓冲」，没有为每个参数独立指定 dtype 的余地；混入 fp32 分区会破坏缓冲布局。所以 `Init._keep_in_fp32` 检测 `remote_device != local_device` 后传 `include_params=False` 只转 buffers，并告警一次（[partition_parameters.py:L1260-L1267](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1260-L1267)）。引擎侧的判定同理（[engine.py:L1927-L1936](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/engine.py#L1927-L1936)）。

## 5. 综合实践

把本讲四个模块串成一条链：**构造期切分 → 观察布局 → 取回验证 → keep_in_fp32 精度实验**。

任务：写一个脚本 `u5l2_lab.py`，用一个两层 Transformer（`nn.TransformerEncoderLayer(d_model=64, nhead=4, dim_feedforward=128)` × 2）完成三组实验：

1. **布局审计**（4.1/4.2）：在 `zero.Init(config_dict_or_path=...)`（bf16、stage 3、`stage3_param_persistence_threshold=0`）内构造模型，逐参数打印 `ds_numel`、`ds_numel_aligned`、`ds_tensor.numel()`、`ds_tensor.dtype`、`ds_tensor.numel() / ds_numel_aligned`（占空比），并汇总验证 `sum(各参数 aligned) == sum(各参数 ds_tensor.numel()) × world_size`（每 rank 各自核对）。
2. **往返验证**（4.3）：构造前保存一份权重初值，`GatheredParameters` 内 gather 全部参数、逐参数比对（bf16 舍入下的最大相对误差），退出后确认回到 `[0]` 占位。
3. **精度实验**（4.5）：给模型挂 `_keep_in_fp32_modules_strict = ["in_proj_bias"]`（或任选一个真实存在的参数名，用 `dict(model.named_parameters())` 先确认名字），重复 keep="auto"/[] 两轮，对比该参数的 `ds_tensor.dtype` 与写入 `EXACT_VALUES` 后的存活情况（照搬 4.5.4 的判定代码）。

进阶（可选）：把 world_size 从 1 调到 2，观察哪些数字变了（padding 出现、all-gather 路径从 NoGather 变为真通信）；打开 `stage3_use_all_reduce_for_fetch_params=true` 并给模型制造一个 fp32 参数（keep_in_fp32），在日志级别 DEBUG 下观察取回拆成两次 flat all-reduce（源码路径 [L1450-L1460](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L1450-L1460)，运行现象待本地验证）。

预期产出：一张参数布局表 + 一段往返误差统计 + keep_in_fp32 两种配置的对照输出。

## 6. 本讲小结

- `zero.Init` 通过 patch `torch.nn.Module.__init__`，让每个子模块在**构造完成的瞬间**就被切分（`_post_init_method`：搬设备 → rank 0 广播对齐初值 → `partition()`），把「先装下完整 fp32 模型」的构造期显存墙拆掉。
- 切分规则是纯算术：padding 补齐到 world size 的倍数（\(P = (W - N \bmod W) \bmod W\)），等长分桶，rank \(r\) 拿 \([rS, (r+1)S)\)；小于 `stage3_param_persistence_threshold` 的参数可豁免切分常驻本卡；切分后 `param.data` 变 0 元素占位符。
- 取回靠句柄家族：默认 all-gather 合并取回（按通信 dtype 分桶、flat 缓冲 narrow 拼回），`stage3_use_all_reduce_for_fetch_params` 走 all-reduce 换通信量，world_size=1 时 NoGather 零通信；#8843 之后多 dtype 分区必须按 dtype 拆桶、每 dtype 一次 flat 通信（`MultipleAllGatherHandles` 打包）。
- `register_external_parameter` 为「不在宿主 forward 里使用」的权重补上 gather/release 的挂钩，是跨模块权重共享和 glm_moe_dsa 这类自定义注意力的必备配套。
- #8843 给 `Init` 加了收尾钩子 `_keep_in_fp32`：在每个模块 `__init__` 跑完、检查点加载之前，把 transformers 点名的参数/缓冲**就地转回 fp32**——参数直接转 `ds_tensor` 分区（hpZ 二级副本同步），量化分区保持量化告警，参数卸载只保 buffers 并告警；由此 gather 返回 fp32、检查点值精确存活，下游的 stage3 混合 dtype 支撑在 u5-l9 展开。

## 7. 下一步学习建议

- **下一讲 u5-l3（ZeRO-1/2）**：看梯度方向的对应机制——IPG bucket、reduce-scatter 与平坦梯度缓冲，与本讲的参数切分互为镜像。
- **u5-l5（ZeRO-3 参数卸载与协调器）**：本讲的 `partition`/`all_gather` 在训练期由谁调度——module hook、预取窗口、NVMe 交换都在 `parameter_offload.py` 与 `partitioned_param_coordinator.py`。
- **u5-l9（ZeRO-3 混合 dtype）**：本讲 4.5 的下半场——stage3 优化器如何按分区 dtype 拆子组、分梯度缓冲与 IPG bucket，fp32 参数的梯度为何全程 fp32 归约累积。
- **延伸阅读**：官方教程 [docs/_tutorials/zero.md](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/docs/_tutorials/zero.md) 的 "Allocating Massive Megatron-LM Models"（zero.Init 用法）与 "Registering external parameters"（外部参数注册）两节；`GatheredParameters` 的完整 docstring（[partition_parameters.py:L2292-L2366](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partition_parameters.py#L2292-L2366)）里还有从 ZeRO-3 检查点手工加载权重的范例。
