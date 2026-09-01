# pinned memory:锁页内存与两种 pin 策略

## 1. 本讲目标

上一讲(u2-l2)我们搞清楚了 checkpoint 文件如何被加载成 `{参数名: torch.Tensor}` 字典。本讲回答下一个问题:**这些躺在普通内存里的权重,如何被"锁"进锁页内存(pinned memory / page-locked memory),为后续的高速 H2D(Host-to-Device)传输做好准备**。

读完本讲,你应该能够:

1. 解释锁页内存是什么、为什么异步 H2D 拷贝(`non_blocking=True`)只有在源内存被锁页后才真正异步。
2. 读懂 `_normal_pin_memory`:先分配 pinned buffer、再按 256 字节对齐槽位把张量切桶、多线程拷入的完整流程。
3. 读懂 `_inplace_pin_memory`:对 `/dev/shm` 里的 safetensors 文件,直接用 `cudaHostRegister` 把文件映射"原地"锁页、跳过整块拷贝的原理。
4. 说清 `MemoryBuffer` 这个数据模型如何统一承载两种策略的产物,以及 `manually_pinned` 标志的用途。
5. 理解两种策略各自的适用条件、硬件限制与取舍(谁被自动启用、谁被强制关闭)。

## 2. 前置知识

### 2.1 虚拟内存、页与"可换页"内存

操作系统给每个进程看到的是**虚拟地址空间**,背后以**页**(Linux 上通常 4KB)为单位映射到物理内存。用 `malloc`/`torch.empty(...)` 分配出来的普通内存是**可换页的(pageable)**:内核随时可能把它换出到磁盘交换区,或者把物理页搬到别的地址。对 CPU 程序这是好事——内存看起来比物理内存大;但对硬件 DMA 设备这是灾难:**GPU 的拷贝引擎直接操作物理地址,它要求"我正在读的这块内存在拷贝期间不许动"**。

于是 CUDA 把 host 内存分成两类:

| 类型 | 分配方式 | 特点 |
| --- | --- | --- |
| pageable(可换页) | `malloc`、普通 `torch.empty` | 内核可换页/迁移;DMA 不能直接用 |
| pinned / page-locked(锁页) | `cudaHostAlloc`、`torch.empty(..., pin_memory=True)`;或对已有内存调用 `cudaHostRegister` | 物理页被锁定,DMA 引擎可直接访问 |

**锁页的代价**:pin 一次需要陷入驱动修改页属性,是昂贵的系统级操作;锁页内存越多,操作系统可换页的余地越小,系统整体可能变慢。所以正确的姿势是:**一次性付出 pin 的成本,之后反复享受异步传输的收益**——这正是 checkpoint-engine 把 pin 放在 `register_checkpoint`(注册阶段,每个 checkpoint 一次)而不是 `update`(更新阶段,每个 bucket 反复)的原因。

### 2.2 异步 H2D 为什么依赖锁页

若源内存是 pageable 的,CUDA 驱动在执行 `cudaMemcpyAsync` 时只能先在内部把数据搬进一块 staging 用的锁页缓冲,再由 DMA 引擎送往 GPU——数据过了两遍内存总线,而且这个 staging 步骤在驱动内部是同步的,`non_blocking=True` 形同虚设。只有当源内存本身就是锁页的,DMA 才能直接从它读,拷贝才能真正与计算/通信重叠。

用时间粗略表达( \(D\) 为数据量, \(B_{\text{host}}\) 为 host 侧内存带宽):

\[ T_{\text{pageable}} \approx \underbrace{\frac{D}{B_{\text{host}}}}_{\text{staging}} + \underbrace{\frac{D}{B_{\text{dma}}}}_{\text{DMA}}, \qquad T_{\text{pinned}} \approx \frac{D}{B_{\text{dma}}} \]

这也解释了 u1-l4 讲过的三阶段流水线为什么成立:H2D 与 broadcast 能够重叠的前提,就是流水线每一拍的源头(pinned host buffer)可以被异步读取。

### 2.3 本讲会用到的其他背景

- **safetensors 布局**:文件开头 8 字节是小端 uint64 的 header 长度 \(N\),随后 \(N\) 字节是 JSON 元数据,之后才是连续的张量数据区;每个张量用 `data_offsets` 的 `[start, end)` 描述自己在数据区中的位置。u2-l2 已详细讲过加载路径。
- **`_align_size` 与 256 字节对齐**:u2-l2 讲过,张量在扁平 uint8 缓冲区中占据"向上取整到 256 倍数"的槽位。
- **tmpfs / `/dev/shm`**:Linux 的内存文件系统,写在里面的文件本体就驻留在物理内存页中(不落盘)。这是 inplace pin 策略的物质基础。
- **`concurrent.futures.ThreadPoolExecutor`**:Python 线程池。`cudaHostAlloc`/`cudaHostRegister`/张量拷贝这些底层调用在 C 层执行并释放 GIL,所以多线程能真正并行。

## 3. 本讲源码地图

| 文件 | 本讲关注的角色 |
| --- | --- |
| [checkpoint_engine/pin_memory.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py) | 主角。`_normal_pin_memory`、`_inplace_pin_memory`、`_register_checkpoint`(两种策略的分派器)都在这里 |
| [checkpoint_engine/data_types.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py) | 定义 `MemoryBuffer` / `ParameterMeta` 两个数据模型 |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | 消费方:`register_checkpoint` 如何调用分派器、`_copy_to_buffer` 如何用 `non_blocking=True` 消费 pinned buffer、注销时如何处理 `manually_pinned` |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | `supports_inplace_pin()` 能力开关:inplace pin 仅 CUDA 可用 |

调用关系一览:

```text
ParameterServer.register_checkpoint (ps.py)
        │
        ▼
_register_checkpoint (pin_memory.py)          ← 按「文件路径 + inplace 开关」分派
        ├── _normal_pin_memory                ← 任意文件 + named_tensors
        └── _inplace_pin_memory               ← 仅 /dev/shm/*.safetensors
        │
        ▼  两者都产出
list[MemoryBuffer]  ──►  self._memory_pool[checkpoint_name] (ps.py)
        │
        ├── gather_metas: 只把 (metas, ptr, size) 广播出去
        └── _copy_to_buffer: pool.buffer ──non_blocking──► GPU h2d_buffer
```

## 4. 核心概念与源码讲解

本讲的三个最小模块按"载体 → 两种策略 → 分派"展开:

- 4.1 `MemoryBuffer`:锁页内存的统一载体
- 4.2 `_normal_pin_memory`:先分配再拷贝
- 4.3 `_inplace_pin_memory`:直接锁页文件映射
- 4.4 `_register_checkpoint`:两种策略的分派与取舍

### 4.1 MemoryBuffer:锁页内存的统一载体

#### 4.1.1 概念说明

无论用哪种策略把权重弄进锁页内存,最终产物都必须回答三个问题:

1. **内存在哪?**——一个扁平的 uint8 张量(即 `buffer`)。
2. **里面装了什么?**——一列 `ParameterMeta`,按在 buffer 中的排布顺序排列;每个参数占据 `aligned_size` 字节的槽位,**offset 是隐含的**:第 \(i\) 个参数的起始偏移等于前 \(i\) 个 `aligned_size` 之和。
3. **这块内存是怎么 pin 的?**——`manually_pinned` 标志:`True` 表示是用 `cudaHostRegister` 手动锁页的(注销时必须手动解锁),`False` 表示是 `torch.empty(..., pin_memory=True)` 分配的(交给 PyTorch 的内存管理)。

这三问的答案就是 [checkpoint_engine/data_types.py:96-100](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L96-L100) 中的 `MemoryBuffer`:

```python
class MemoryBuffer(BaseModel):
    buffer: _TorchTensor          # 扁平 uint8 张量,锁页内存本体
    size: int                     # 字节长度
    metas: list[ParameterMeta]    # 按排布顺序的参数元数据(offset 隐含)
    manually_pinned: bool = False # 是否为 cudaHostRegister 手动锁页
```

为什么这样设计?因为 PS 后续要做的事情**根本不需要搬运张量本身**:

- `gather_metas`(ps.py,详见 u3-l3)只把每个 MemoryBuffer 转成 `(metas, ptr, size)` 广播给其他 rank——**指针加清单,不传数据**。
- Broadcast 更新时,`_copy_to_buffer` 直接从 `pool.buffer` 的字节区间做异步 H2D 拷贝。
- P2P 更新时,buffer 被注册进 mooncake transfer engine,远端 rank 通过 RDMA 直接读这块 host 内存。

也就是说,`MemoryBuffer` 是"**一块锁页字节区 + 一张布局图**"的打包,是 PS 与外界共享权重的最小单位。

#### 4.1.2 核心流程

从注册到消费的生命周期:

```text
register_checkpoint
   └─► _register_checkpoint ──► list[MemoryBuffer]
                                   │
        ┌──────────────────────────┼───────────────────────────┐
        ▼                          ▼                           ▼
  gather_metas              update(broadcast)            update(p2p)
  只广播 (metas,ptr,size)   _copy_to_buffer:             buffer 注册进
  (ps.py:476-489)           pool.buffer ──copy_(non_blocking)──►    mooncake,远端
                            GPU h2d_buffer (ps.py:705-709)          RDMA 直读
                                   │
                                   ▼
                      unregister_checkpoint
                      manually_pinned=True → 手动 cudaHostUnregister
                      否则交给引用回收 + host_empty_cache
```

隐含 offset 的还原逻辑在 ps.py 的 `_to_named_tensor` 中可见一斑——它就是从 offset=0 开始逐个累加 `aligned_size`:

[checkpoint_engine/ps.py:35-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48) — 把一列 `ParameterMeta` 展开成带 `offset` 的字典列表,offset 由 `aligned_size` 逐步累加得到。worker 端就是靠这份清单从扁平 buffer 里"切"回每个张量。

#### 4.1.3 源码精读

**消费点一:异步 H2D。** [checkpoint_engine/ps.py:684-714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714) 的 `_copy_to_buffer` 把 pinned host buffer 的字节区间拷进 GPU 侧的 `h2d_buffer`:

```python
pool = self._get_memory_pool(checkpoint_name)[b.idx]
buffer[offset : offset + b.size].data.copy_(
    pool.buffer[b.offset : b.offset + b.size],
    non_blocking=True,        # ← 只有 pool.buffer 是锁页的,这里才真正异步
)
```

这里 `b.idx` 是 `BucketRange` 中的 MemoryBuffer 下标、`b.offset/b.size` 是该 buffer 内的字节区间(详见 u3-l5 的桶切分)。**本讲的全部铺垫,就是让这一行 `non_blocking=True` 名副其实。**

**消费点二:P2P 注册。** [checkpoint_engine/ps.py:721-735](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L721-L735) 把每个 `memory_buffer.buffer` 以 `memory_pool_{name}_{idx}` 的名字注册进 p2p store,供远端 rank 通过 RDMA 读取——锁页内存同样是 RDMA 传输的合格源。

**归宿:注销。** [checkpoint_engine/ps.py:446-460](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L446-L460) 注销 checkpoint 时,只对 `manually_pinned=True` 的 buffer 调用内部函数 `_unpin` 手动解锁,最后统一 `host_empty_cache()` 归还锁页内存缓存(细节留给 u2-l4)。

#### 4.1.4 代码实践

实践目标:亲手验证 256 字节对齐槽位的计算,以及"offset 隐含在 metas 顺序中"这一设计。

操作步骤(纯 CPU,无需 GPU;在仓库根目录、已 `pip install -e .` 的环境中运行):

```python
# 示例代码:practice_align.py
import torch
from checkpoint_engine.pin_memory import _ALIGN_SIZE, _align_size

tensors = {
    "lm_head.weight": torch.zeros(1000, 1000, dtype=torch.bfloat16),
    "norm.weight":    torch.zeros(31, dtype=torch.uint8),
    "proj.weight":    torch.zeros(777, 3, dtype=torch.float32),
}

offset = 0
for name, t in sorted(tensors.items()):
    raw = t.element_size() * t.numel()
    aligned = _align_size(t.dtype, t.shape)
    print(f"{name:16s} raw={raw:>9}  aligned={aligned:>9}  padding={aligned-raw:>4}  offset={offset}")
    offset += aligned
print(f"total buffer size = {offset}")
```

需要观察的现象:

- `_ALIGN_SIZE` 的值是多少;
- 31 字节的 `norm.weight` 占了多少槽位(padding 有多大);
- 三个张量的 offset 链如何依次衔接。

预期结果:`_ALIGN_SIZE = 256`;三个张量分别占据 2,000,128 / 256 / 9,472 字节,offset 依次为 0 / 2,000,128 / 2,000,384,总大小 2,009,856 字节(即 2,000,384 + 9,472,约 1.92MB)。若与你手算不一致,请检查 `_align_size` 的向上取整公式:

\[ \text{aligned\_size}(t) = \left\lceil \frac{\text{itemsize}(t)\times\text{numel}(t)}{256} \right\rceil \times 256 \]

#### 4.1.5 小练习与答案

**练习 1**:`ParameterMeta` 里为什么没有 `offset` 字段?
**答案**:offset 由 metas 在列表中的顺序隐含——第 \(i\) 个参数的 offset 是前 \(i\) 个 `aligned_size` 之和。这样元数据更小,且 `ps._to_named_tensor`(ps.py:35-48)在需要时一行循环就能还原。

**练习 2**:`manually_pinned=True` 的 MemoryBuffer 在注销时与普通的有何不同?
**答案**:普通 pinned 内存由 PyTorch 分配,释放时交给引用回收与 `host_empty_cache` 即可;手动锁页的内存必须在 `unregister_checkpoint` 里显式调用 `_unpin`(内部用 `cudaHostUnregister`),否则物理页一直被锁定。

**练习 3**:为什么 `MemoryBuffer.buffer` 的类型是 `_TorchTensor`(u2-l1 讲过的自定义 pydantic 类型)而不是 `torch.Tensor`?
**答案**:因为 `MemoryBuffer` 是 pydantic `BaseModel`,要能通过校验并参与 `gather_metas` 的对象序列化/JSON 导出;`_TorchTensor` 用 `PlainValidator` 放行 torch.Tensor,使其不必是可 JSON 化的原始类型。

### 4.2 `_normal_pin_memory`:先分配再拷贝

#### 4.2.1 概念说明

这是"教科书式"的锁页路径:**先用 `torch.empty(..., pin_memory=True)` 分配一块全新的锁页内存,再把权重数据拷进去**。它对输入几乎没有任何要求——文件可以在任意路径、可以是多份 TP 分片(经 u2-l2 的 `_load_checkpoint` 拼接)、还可以混入调用方直接给的 `named_tensors` 字典。代价是**数据要在 host 内存里过一遍总线**(从 pageable 原始张量拷进 pinned buffer),且短暂的瞬间权重存在两份。

它同时承担"把权重重新组织成适合传输的扁平布局"的任务:所有张量被压进一个(或多个)连续的 uint8 字节流,每个张量落在 256 对齐的槽位上。这个扁平布局正是后续桶切分、IPC 共享、P2P 注册的共同基础。

#### 4.2.2 核心流程

[checkpoint_engine/pin_memory.py:277-362](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L277-L362) 的执行过程分四步:

```text
① 加载与合并
   parameters = _load_checkpoint(files)      # u2-l2:safetensors/npy 加载 + TP 拼接
   parameters.update(named_tensors)          # 同名时 named_tensors 覆盖文件版本

② 决定桶大小并贪心切桶
   bucket_size = max(4GiB, 最大单张量的 align256)
   for name, tensor in sorted(parameters.items()):   # 名字排序 ⇒ 布局确定
       若当前桶放不下 ⇒ 开新桶
       把 (name, shape, dtype, aligned_size) 追加到当前桶

③ 并行分配(32 线程)
   for 每个桶: torch.empty(size, uint8, pin_memory=True)
               (或从 shared_pin_memory 复用形状匹配的旧 buffer)

④ 分配完成即流水拷贝(32 线程)
   某桶分配完成 ⇒ 立刻为桶内每个张量提交一个拷贝任务
   register_tensor: buffer[offset : offset+nbytes] = tensor.view(uint8)
   ⇒ list[MemoryBuffer]
```

几个关键点:

- **`bucket_size = max(4GiB, 最大张量)`**(L286)保证任何单个张量都放得进一个空桶(首张量入空桶时必然满足约束),同时 4GiB 的下限让大 checkpoint 能被切成多个桶、喂饱 32 个工作线程。举例:2TB 的 bf16 权重 ≈ 512 个桶。
- **`sorted(parameters.items())`**(L294)按参数名字典序排布,使布局**确定可复现**——这是 u2-l5 共享内存池能按 `idx + size` 精确匹配复用的前提。
- **两级 `as_completed` 流水**(L329-361):先提交所有桶的分配任务;每有一个桶分配完,立刻把该桶内的全部拷贝任务塞进同一个线程池。于是"桶 j 的拷贝"与"桶 i 的分配"并行,注册总时长约等于最慢的一条链,而不是"全部分配完 + 全部拷完"的串行相加。

贪心装箱的判定只有一个不等式(伪代码):

```text
if buckets[-1].size + align(t) > bucket_size:
    buckets.append(new_empty_bucket)
buckets[-1].metas.append(meta(t)); buckets[-1].size += align(t)
```

#### 4.2.3 源码精读

**切桶**。[checkpoint_engine/pin_memory.py:286-302](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L286-L302) — 计算 `bucket_size`、定义进程内用的 `MemoryBucket` 模型,然后按名字顺序把每个参数的 `ParameterMeta` 贪心装入当前桶,装不下就开新桶:

```python
bucket_size = max(4 << 30, max(_align_size(x.dtype, x.shape) for x in parameters.values()))
...
for name, tensor in sorted(parameters.items()):
    size = _align_size(tensor.dtype, tensor.shape)
    if buckets[-1].size + size > bucket_size:
        buckets.append(MemoryBucket(size=0, metas=[]))
    buckets[-1].metas.append(ParameterMeta(name=name, shape=tensor.shape, dtype=tensor.dtype, aligned_size=size))
    buckets[-1].size += size
```

注意 `MemoryBucket` 用 `BaseModel` 而非 NamedTuple——它是纯进程内的临时结构,这里其实偏离了 u2-l1 说的"跨边界才用 BaseModel"的取舍,阅读时知道它不会被序列化出进程即可。

**分配或复用**。[checkpoint_engine/pin_memory.py:309-324](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L309-L324) — `register_pin_memory` 是两条分配路径的分岔口:传了 `shared_pin_memory` 就断言 `idx` 与 `size` 都与旧池匹配并直接复用旧 buffer;否则调用 PyTorch 分配全新的锁页内存:

```python
if shared_pin_memory:
    assert idx < len(shared_pin_memory)
    assert shared_pin_memory[idx].size == size
    return idx, shared_pin_memory[idx].buffer     # 复用(共享池,详见 u2-l5)
else:
    buffer = torch.empty(size, dtype=torch.uint8, pin_memory=True)   # 新分配
    return idx, buffer
```

**槽位拷贝**。[checkpoint_engine/pin_memory.py:326-327](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L326-L327) — 把任意 dtype 的张量按字节写进 buffer 的对齐槽位:先 `view(-1)` 展平、再 `view(dtype=torch.uint8)` 重解释为字节:

```python
def register_tensor(buffer, offset, tensor):
    buffer[offset : offset + tensor.nbytes] = tensor.view(-1).view(dtype=torch.uint8)
```

拷贝目标是 `tensor.nbytes` 而非 `aligned_size`,所以相邻张量之间最多留有 255 字节的 padding。

**流水线编排**。[checkpoint_engine/pin_memory.py:329-361](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L329-L361) — 用一个 32 线程的池先提交全部分配任务;`as_completed` 每拿到一个 `(idx, buffer)`,校验容量、填回 `memory_buffers[idx].buffer`、打日志,并**立刻**为该桶内每个张量提交拷贝任务(新的 future 进入同一个池排队);最后再统一 `as_completed(new_futures)` 等待所有拷贝收尾。值得一提的细节:`as_completed(futures)` 只迭代最初那批分配 future,循环内新提交的拷贝任务由 `new_futures` 收集、在 L360-361 处统一等待。

顺带一提,[checkpoint_engine/pin_memory.py:304-307](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L304-L307) 先用 `torch.empty(0)` 占位构造 `memory_buffers`,真正的 buffer 等分配完成后再回填——保证列表下标与桶下标始终一致。

#### 4.2.4 代码实践

实践目标:在纯 CPU 环境下复现切桶算法,直观感受"贪心装箱 + 对齐槽位"。

操作步骤:

```python
# 示例代码:practice_buckets.py(复刻 pin_memory.py:286-302,不触碰需要 CUDA 的分配)
import torch
from checkpoint_engine.pin_memory import _align_size

parameters = {
    "lm_head.weight": torch.zeros(1000, 1000, dtype=torch.bfloat16),  # 2,000,000 B
    "norm.weight":    torch.zeros(31, dtype=torch.uint8),             # 31 B
    "proj.weight":    torch.zeros(777, 3, dtype=torch.float32),       # 9,324 B
}
bucket_size = 2_000_500   # 故意调小,便于观察多桶;真实代码是 max(4<<30, 最大张量)

buckets = [{"size": 0, "names": []}]
for name, tensor in sorted(parameters.items()):
    size = _align_size(tensor.dtype, tensor.shape)
    if buckets[-1]["size"] + size > bucket_size:
        buckets.append({"size": 0, "names": []})
    buckets[-1]["names"].append(name)
    buckets[-1]["size"] += size

for i, b in enumerate(buckets):
    print(f"bucket {i}: size={b['size']}, names={b['names']}")
```

需要观察的现象:

1. 参数进入桶的顺序是否是字典序(而非字典插入序);
2. `proj.weight` 为何被挤到第二个桶;
3. 把 `bucket_size` 换成真实的 `max(4 << 30, max(_align_size(x.dtype, x.shape) for x in parameters.values()))` 后桶数变成几个。

预期结果:顺序为 `lm_head.weight → norm.weight → proj.weight`;桶 0 装下前两个(size=2,000,384 ≤ 2,000,500),`proj.weight`(9,472)放不下而开桶 1;换成真实公式后总大小约 1.92MB 远小于 4GiB,只有 1 个桶。

补充说明:完整调用 `_normal_pin_memory` 本身需要 CUDA(`torch.empty(..., pin_memory=True)`),在无 GPU 环境会失败,**待本地验证**;本实践用复刻算法绕开了这一限制。

#### 4.2.5 小练习与答案

**练习 1**:`bucket_size` 为什么要取 `max(4 << 30, 最大单张量的 aligned_size)`?
**答案**:`bucket_size` 必须不小于最大单张量,否则该张量连空桶都放不进(代码 L296 的断言会兜底报错);4GiB 下限则保证大模型能切成足够多的桶,让 32 线程的分配/拷贝流水真正并行。

**练习 2**:切桶循环为什么必须 `sorted(parameters.items())`,用字典原始顺序行不行?
**答案**:功能上"行",但布局会随字典插入顺序漂移。排序后布局确定可复现:同一份 checkpoint 每次注册得到相同的桶结构与槽位,这是 u2-l5 共享内存池按 `idx/size` 匹配复用、以及多次注册间 metas 可比的基础。

**练习 3**:L329-361 的两级 future 编排,相比"先等全部桶分配完、再提交全部拷贝"省在哪里?
**答案**:省在重叠。分配与拷贝共享同一个 32 线程池,先完成的桶立刻开始拷贝,与后完成桶的分配并行;总时间近似 \(\max\) 各桶的"分配+拷贝"链长,而非所有分配时间与所有拷贝时间的总和。

### 4.3 `_inplace_pin_memory`:直接锁页文件映射

#### 4.3.1 概念说明

normal pin 有两个明显开销:**分配一块新的锁页内存(慢)**,以及**把数据完整拷一遍(host 内存带宽 × 1)**。但如果 checkpoint 文件已经在 `/dev/shm`(内存文件系统)里,数据本身就已经驻留在物理内存页中——再分配一块新内存拷进去纯属浪费。inplace pin 的思路是:

1. 用 `torch.from_file` 把整个 safetensors 文件**映射**成一个 uint8 张量(tmpfs 的页直接进入进程地址空间,零拷贝);
2. 手工解析文件头,得到张量清单(不依赖 `safe_open`,因为我们要的是"文件内的原始布局",而不是拷出来的张量);
3. 对数据区那一刀切片调用 **`cudaHostRegister`**,把这段映射内存**原地注册为锁页内存**——只改页属性,不搬数据;
4. 注册成功后**删除源文件**:文件本体占的内存与映射占的是同一批页,但保留文件会诱导后续再次加载(再占一份内存),删掉后这块内存的唯一入口就是 pinned buffer。

一句话:normal pin 是"**新瓶子装旧酒**",inplace pin 是"**把酒瓶直接贴上合格证**"。代价是输入受限:仅支持 `/dev/shm/` 下的 `.safetensors`(且不支持 TP 拼接——u2-l2 讲过,safetensors 无法表达 `tp_concat_dim`,inplace 路径要求文件里就是最终权重),并且 **pin 成功后文件会被删除**,源码在 `register_checkpoint` 的 docstring 里用大写 REMOVED 警告了这一点。

#### 4.3.2 核心流程

[checkpoint_engine/pin_memory.py:193-274](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L193-L274),单个文件的处理在内部函数 `_parse_and_pin_from_safetensors` 中:

```text
torch.from_file(整个文件) ──► uint8 张量 t
   │
   ├─ 前 8 字节(小端 uint64) ──► header 长度 N,start_pos = 8 + N
   ├─ t[8:start_pos] 的 JSON  ──► {name: {dtype, shape, data_offsets}}
   │     按 data_offsets 升序遍历,断言 offset == start(数据区无空洞)
   │     ⇒ metas: list[ParameterMeta](aligned_size = end - start)
   │
   ├─ buffer = t[start_pos:]          # 数据区切片 = 全部权重字节
   ├─ os.remove(file_path)            # 防止双倍内存(/dev/shm 文件视为临时文件)
   ├─ cudaHostRegister(buffer)        # 原地锁页,flag=0(Default)
   └─ ⇒ MemoryBuffer(buffer, size, metas, manually_pinned=True)

多个文件 ⇒ ThreadPoolExecutor(max_workers=32) 并行处理
```

这里有一个与 normal 路径微妙不同的细节:**`aligned_size` 的数值来源不同**。normal 路径按 256 对齐计算(`_align_size`);inplace 路径直接取 `end - start`,即 safetensors 写入时自身的排布——**不一定是 256 的倍数**。两者语义一致(都表示"该参数在 buffer 中的槽位大小",offset 都由顺序累加得到),所以下游的桶切分与 `_to_named_tensor` 对两条路径同样适用,但具体数值可能不同。实践 4.3.4 会让你亲眼对比。

#### 4.3.3 源码精读

**原地锁页原语**。[checkpoint_engine/pin_memory.py:204-214](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L204-L214) — `_pin` 对任意 CPU 张量原地执行 `cudaHostRegister`,失败时用 `cudaGetErrorString` 把错误码翻译成可读信息抛出:

```python
def _pin(t: torch.Tensor):
    torch.cuda.set_device(device_index)
    cudart = torch.cuda.cudart()
    r = cudart.cudaHostRegister(t.data_ptr(), t.numel() * t.element_size(), 0)
    if r != 0:
        raise RuntimeError(f"pin memory error, error code: {r}, ...")
```

第三个参数 `0` 即 `cudaHostRegisterDefault`。函数闭包捕获了外层的 `device_index = torch.cuda.current_device()`(L194),确保多卡进程在正确的 CUDA 上下文注册。

**文件映射与文件头解析**。[checkpoint_engine/pin_memory.py:216-250](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L216-L250) — 把整个文件映射为 uint8 张量,随后完全手工地解析 safetensors 头:前 8 字节读出 header 长度、切片转 JSON、丢弃 `__metadata__`、按 `data_offsets` 升序遍历并**断言张量在数据区中连续无空洞**:

```python
t = torch.from_file(file_path, True, size, dtype=torch.uint8)
start_pos = int.from_bytes(t[0:flag_size].numpy().tobytes(), byteorder="little") + flag_size
header = json.loads(t[flag_size:start_pos].numpy().tobytes())
...
for name, meta in sorted(header.items(), key=lambda x: x[1]["data_offsets"]):
    start, end = meta["data_offsets"]
    assert offset == start          # safetensors 保证 offsets 对齐且连续
    metas.append(ParameterMeta(name=name, dtype=_getdtype(meta["dtype"]),
                               shape=torch.Size(meta["shape"]), aligned_size=end - start))
    offset = end
```

L216 有一行坦白的 TODO:`# TODO: should only support /dev/shm? but we found files in disk also work?`——作者也观察到磁盘文件有时能工作,但保守起见仍只对 `/dev/shm` 放行(筛选逻辑见 4.4)。

**切片、删除、注册**。[checkpoint_engine/pin_memory.py:252-269](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L252-L269) — 校验数据区长度与 metas 对账(`offset == buffer.nbytes`),然后删除源文件、原地锁页、返回 `manually_pinned=True` 的 MemoryBuffer:

```python
buffer = t[start_pos:]
assert offset == buffer.nbytes
os.remove(file_path)          # 避免双倍内存;假定 /dev/shm 下都是临时文件
...
_pin(buffer)
return MemoryBuffer(buffer=buffer, size=buffer.nbytes, metas=metas, manually_pinned=True)
```

注意顺序:**先 `os.remove` 再 `_pin`**。POSIX 下 unlink 只删目录项,已建立的 `from_file` 映射继续有效,所以删除不打断后续注册。若文件里没有张量,则跳过 pin 返回 `manually_pinned=False` 的空 buffer(L259-263)。

**并行驱动**。[checkpoint_engine/pin_memory.py:271-274](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L271-L274) — 与 normal 路径同款 32 线程池,`executor.map` 对多个文件并行"解析+锁页"(头解析与 ctypes 调用都释放 GIL)。

#### 4.3.4 代码实践

实践目标:在纯 CPU 环境手工复刻 safetensors 文件头解析,验证"数据区无空洞、slot 即槽位"。

⚠️ 安全提示:**不要**在 CPU 环境直接调用 `_inplace_pin_memory`——它会在 `_pin` 处因无 CUDA 而失败,而且在那之前已经把你的文件 `os.remove` 了(如果路径以 `/dev/shm/` 结尾 `.safetensors` 的话)。本实践只复刻解析段,不执行 pin 与删除。

操作步骤:

```python
# 示例代码:practice_inplace_parse.py(复刻 pin_memory.py:217-255 的解析部分)
import json, os, torch
from safetensors.torch import save_file

path = "/tmp/demo.safetensors"
save_file(
    {
        "a.weight": torch.zeros(1000, 1000, dtype=torch.bfloat16),
        "b.weight": torch.zeros(31, dtype=torch.uint8),
        "c.weight": torch.zeros(777, 3, dtype=torch.float32),
    },
    path,
)

size = os.stat(path).st_size
t = torch.from_file(path, True, size, dtype=torch.uint8)
flag_size = 8
start_pos = int.from_bytes(t[0:flag_size].numpy().tobytes(), byteorder="little") + flag_size
header = json.loads(t[flag_size:start_pos].numpy().tobytes())
header.pop("__metadata__", None)

offset = 0
for name, meta in sorted(header.items(), key=lambda x: x[1]["data_offsets"]):
    start, end = meta["data_offsets"]
    assert offset == start, (name, offset, start)   # 数据区无空洞
    print(f"{name:10s} dtype={meta['dtype']:8s} shape={meta['shape']!s:15s} slot={end-start}")
    offset = end

buffer = t[start_pos:]
assert offset == buffer.nbytes
print(f"header ends at {start_pos}, data region {buffer.nbytes} B, file size {size} B")
```

需要观察的现象:

1. `slot`(即 inplace 路径的 `aligned_size`)分别是多少;
2. 与实践 4.1.4 中同尺寸张量的 align256 结果(2,000,128 / 256 / 9,472)逐个对比,记录差异;
3. `start_pos + buffer.nbytes` 是否恰好等于文件大小。

预期结果:断言全部通过(说明 safetensors 数据区连续);`a.weight` 的 slot 是 2,000,000(它的原始字节数,是否加 padding 取决于 safetensors 写入时的对齐策略);`b.weight` 的 slot **很可能不是 256 的倍数**——这正是"inplace 路径的 aligned_size 来自文件自身布局"的直接证据。请以你环境中的实际输出为准记录数值(**safetensors 的具体对齐宽度待本地验证**)。

#### 4.3.5 小练习与答案

**练习 1**:为什么注册成功后要 `os.remove(file_path)`,而 normal pin 不删?
**答案**:inplace pin 后,文件页与 pinned buffer 是同一批物理页,数据只有一份;保留文件会诱导调用方或别的进程再次加载它,凭空多占一份内存。注释明确假定 `/dev/shm` 下是临时文件。normal pin 的源文件在磁盘上,不在内存里,没有这个问题。

**练习 2**:`os.remove` 之后映射为什么仍然有效?
**答案**:POSIX 语义下 unlink 只移除目录项链,只要进程还持有 mmap 映射,文件的页就保留。所以先删文件再 `_pin`(或反过来)都不影响这段内存的可用性。

**练习 3**:循环里的 `assert offset == start` 防的是什么错?
**答案**:它验证所有张量的 `data_offsets` 首尾相接、数据区没有空洞。只有连续排布,`buffer = t[start_pos:]` 这一个切片加上"顺序累加 slot"的隐含 offset 语义才能成立;否则 metas 与实际字节就对不上账了。

### 4.4 `_register_checkpoint`:两种策略的分派与取舍

#### 4.4.1 概念说明

两条 pin 路径不是让用户手动挑选的,而是由 [checkpoint_engine/pin_memory.py:365-401](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L365-L401) 的 `_register_checkpoint` 按**文件路径**自动分流,再由 ps.py 的 `ParameterServer.register_checkpoint` 按**硬件能力**与**内存池模式**做前置修正。综合起来,决策链是:

```text
ParameterServer.register_checkpoint(use_inplace_pin_memory=True 默认)
   ├─ 设备不是 cuda(supports_inplace_pin() 为 False)⇒ 强制关掉并告警
   ├─ use_shared_memory_pool=True ⇒ 强制 inplace_pin=False(与共享池不兼容)
   └─ _register_checkpoint(inplace_pin=...)
        ├─ 文件以 /dev/shm/ 开头且以 .safetensors 结尾 ⇒ _inplace_pin_memory
        └─ 其余文件 + 全部 named_tensors            ⇒ _normal_pin_memory
```

两种策略的完整对比:

| 维度 | `_normal_pin_memory` | `_inplace_pin_memory` |
| --- | --- | --- |
| 输入范围 | 任意路径文件 + `named_tensors` | 仅 `/dev/shm/*.safetensors` |
| TP 分片拼接 | 支持(经 `_load_checkpoint`) | 不支持(要求文件即最终权重) |
| 内存份数 | 稳态一份 pinned(注册期间短暂两份) | 从头到尾一份(文件映射即 buffer) |
| host 内存总线 | 要过一遍(整块拷贝) | 不过(只改页属性) |
| pin 耗时 | 分配 pinned + 全量拷贝 | 一次 `cudaHostRegister` |
| 源文件 | 保留 | **pin 后删除** |
| 硬件 | 各后端均可(host 侧分配) | 仅 CUDA |
| 与共享内存池 | 兼容(可复用) | 不兼容(强制关闭) |
| 注销方式 | 常规回收 + `host_empty_cache` | 需手动 `cudaHostUnregister` |

#### 4.4.2 核心流程

`_register_checkpoint` 是仅限关键字参数的纯函数式入口:

```text
输入: files, named_tensors, rank, shared_pin_memory, inplace_pin
   │
   ├─ files 与 named_tensors 均为空 ⇒ 直接返回 []
   │
   ├─ inplace_pin=True:
   │     files_to_inplace_pin = [以 /dev/shm/ 开头且以 .safetensors 结尾的文件]
   │     files_to_normal_pin  = 其余文件
   ├─ 否则全部进 normal
   │
   ├─ (files_to_normal_pin 或 named_tensors 非空) ⇒ _normal_pin_memory(...)
   └─ files_to_inplace_pin 非空                    ⇒ _inplace_pin_memory(...)
   ⇒ 两条路径的结果按序拼接为 list[MemoryBuffer]
```

注意 **`named_tensors` 永远走 normal 路径**——它不是文件,无从"原地"锁页。

#### 4.4.3 源码精读

**按路径分流**。[checkpoint_engine/pin_memory.py:379-400](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L379-L400) — 用前缀 + 后缀两个条件挑出可以原地锁页的文件,其余连同 `named_tensors` 交给 normal 路径,最后把两条路径产出的 MemoryBuffer 顺序拼接返回:

```python
files_to_inplace_pin = [
    file for file in files
    if file.startswith("/dev/shm/") and file.endswith(".safetensors")
]
files_to_normal_pin = [file for file in files if file not in files_to_inplace_pin]
...
if files_to_normal_pin or named_tensors:
    memory_buffers.extend(_normal_pin_memory(files=files_to_normal_pin, named_tensors=named_tensors, ...))
if files_to_inplace_pin:
    memory_buffers.extend(_inplace_pin_memory(files_to_inplace_pin, rank=rank))
```

**上游入口与两道闸门**。[checkpoint_engine/ps.py:305-378](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L305-L378) — `ParameterServer.register_checkpoint` 的 docstring 先用大写警告文件会被删除(L316-317);随后第一道闸门:非 CUDA 后端强制关闭 inplace(L331-335);第二道闸门:共享内存池模式下固定传 `inplace_pin=False`(L349-355);普通注册则把用户的 `use_inplace_pin_memory` 透传下去(L363-368),失败时回滚注销:

```python
if not self.device_manager.supports_inplace_pin() and use_inplace_pin_memory:
    logger.warning("Only cuda devices support in-place pin memory, set use_inplace_pin_memory to False")
    use_inplace_pin_memory = False
...
self._memory_pool[checkpoint_name] = _register_checkpoint(
    files=files or [], named_tensors=named_tensors or {},
    rank=self._rank, inplace_pin=use_inplace_pin_memory,
)
```

**能力开关的定义**。[checkpoint_engine/device_utils.py:285-287](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L287) — `supports_inplace_pin` 就是"设备类型是否为 cuda"的判断,因为整个原地锁页依赖的是 `torch.cuda.cudart().cudaHostRegister` 这个 CUDA 专属接口,NPU/XPU 没有对应物:

```python
def supports_inplace_pin(self) -> bool:
    """Whether in-place host-memory pinning (cudaHostRegister) is available -- CUDA only."""
    return self.device_type == "cuda"
```

**为什么与共享池互斥?** 源码注释只给结论(`# inplace pin memory is not compatible with shared memory pool`,[checkpoint_engine/ps.py:349-355](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L349-L355))。从机制看:共享池的复用发生在 `_normal_pin_memory.register_pin_memory` 内部(4.2.3),它拿旧 buffer 顶替新分配;而 inplace pin 的 buffer 来自文件映射、生命周期跟随手动 unpin,根本不经过那条分配路径,自然无法纳入"按 idx+size 匹配复用"的池子。此机制细节将在 u2-l5 展开。

#### 4.4.4 代码实践

实践目标(源码阅读型):验证三处分派规则的出处,确认你对决策链的理解。

操作步骤:

1. 打开 [checkpoint_engine/ps.py:331-335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L331-L335),回答:在 NPU 上调用 `register_checkpoint(..., use_inplace_pin_memory=True)` 会发生什么?依据是哪两行?
2. 打开 [checkpoint_engine/device_utils.py:285-287](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L287),找出"仅 CUDA"这一判断的实现。
3. 打开 [checkpoint_engine/pin_memory.py:390](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L390),找出"`named_tensors` 永远走 normal 路径"的证据(`if files_to_normal_pin or named_tensors:`)。
4. 推演:注册 `files=["/dev/shm/a.safetensors", "/data/b.safetensors"]` 且 `inplace_pin=True` 时,两个文件各走哪条路径、返回的 MemoryBuffer 列表顺序如何?

需要观察的现象 / 预期结果:

1. NPU 上会被强制 `use_inplace_pin_memory = False` 并打 warning,不会报错;依据是 `supports_inplace_pin()` 对非 cuda 返回 False;
2. `supports_inplace_pin` 就是 `self.device_type == "cuda"`;
3. `named_tensors` 非空即触发 `_normal_pin_memory`(即使 `files_to_normal_pin` 为空),而 `_inplace_pin_memory` 的参数里根本没有 `named_tensors`;
4. `a.safetensors` 走 inplace(且会被删除),`b.safetensors` 走 normal;返回列表中 **normal 的结果在前、inplace 的在后**(L391 先 extend normal,L400 再 extend inplace)。

#### 4.4.5 小练习与答案

**练习 1**:同一批文件同时包含 `/dev/shm/a.safetensors` 和 `/data/b.safetensors`,注册结果里两条路径的产物如何共存?
**答案**:两个文件分别走 inplace 与 normal,产出的 MemoryBuffer 顺序拼接成一个列表(normal 在前、inplace 在后,见 L391/L400)。每个 MemoryBuffer 自带布局图 metas,下游通过 `BucketRange.idx`(即列表下标)定位到具体 buffer,所以混合来源完全透明。

**练习 2**:为什么 P2P 更新路径同样离不开这两条 pin 路径?
**答案**:P2P 模式下 `_register_parameters_to_p2p_store`(ps.py:721-735)把 `memory_buffer.buffer` 注册进 mooncake transfer engine,远端 rank 通过 RDMA 直接读这块 host 内存;RDMA 与 DMA 一样要求物理页稳定,所以无论 broadcast 还是 P2P,锁页都是传输的前提。

**练习 3**:如果把一个 8MB 的 checkpoint 放在 `/dev/shm` 并用默认参数注册,inplace pin 一定生效吗?
**答案**:只有在 CUDA 设备上才生效。`register_checkpoint` 的 `use_inplace_pin_memory` 默认为 True,但 NPU/XPU 会在 L331-335 被强制改回 False,随后 `_register_checkpoint` 把所有文件都送进 normal 路径;共享内存池模式下同样被强制关闭。

## 5. 综合实践

**任务:给一份自制 checkpoint 做一次"纸上 pin",并对照两条路径的布局差异。**

第一步,准备一份含"不整对齐张量"的小 checkpoint(示例代码):

```python
# 示例代码:practice_final.py(第 1-3 步纯 CPU 可运行)
import json, os, torch
from safetensors.torch import save_file
from checkpoint_engine.pin_memory import _align_size

tensors = {
    "lm_head.weight": torch.zeros(1000, 1000, dtype=torch.bfloat16),
    "norm.weight":    torch.zeros(31, dtype=torch.uint8),
    "proj.weight":    torch.zeros(777, 3, dtype=torch.float32),
}
path = "/tmp/demo.safetensors"
save_file(tensors, path)

# ① normal 路径布局表:名字序 + 256 对齐槽位
print("== normal pin 布局 ==")
offset = 0
for name, t in sorted(tensors.items()):
    a = _align_size(t.dtype, t.shape)
    print(f"{name:16s} raw={t.element_size()*t.numel():>9} slot={a:>9} offset={offset}")
    offset += a

# ② inplace 路径布局表:手工解析文件头,slot = end - start
print("== inplace pin 布局 ==")
size = os.stat(path).st_size
t8 = torch.from_file(path, True, size, dtype=torch.uint8)
start_pos = int.from_bytes(t8[0:8].numpy().tobytes(), "little") + 8
header = json.loads(t8[8:start_pos].numpy().tobytes())
header.pop("__metadata__", None)
off = 0
for name, meta in sorted(header.items(), key=lambda x: x[1]["data_offsets"]):
    s, e = meta["data_offsets"]
    assert off == s
    print(f"{name:16s} slot={e-s:>9} offset={off}")
    off = e
```

第二步,对照两张表回答:

1. 两条路径的参数顺序是否一致?为什么?
2. 同一张量在两条路径中的 `slot` 是否相等?不相等时哪个更大?
3. 任选一条路径,写出用 `ps._to_named_tensor` 语义还原每个参数 offset 的过程。

第三步(可选,需要 CUDA,**待本地验证**):把文件拷到 `/dev/shm/demo.safetensors`,在能初始化 CUDA 的环境中调用:

```python
# 示例代码:需要 CUDA 环境;注意文件会被删除,请使用可丢弃的副本
from checkpoint_engine.pin_memory import _register_checkpoint
bufs = _register_checkpoint(files=["/dev/shm/demo.safetensors"],
                            named_tensors={}, rank=0, inplace_pin=True)
print([(b.size, b.manually_pinned, len(b.metas)) for b in bufs])
print("file exists:", os.path.exists("/dev/shm/demo.safetensors"))   # 预期 False
```

预期观察:日志输出 `inplace pin memory for file ... finished, size ... MiB`;返回的 MemoryBuffer `manually_pinned=True`;`/dev/shm` 中的源文件已被删除。对照本讲 4.3 的源码逐行解释这三条现象即完成实践。

## 6. 本讲小结

- **锁页内存是高速传输的地基**:`non_blocking=True` 的异步 H2D、RDMA 直读,都要求源内存物理页稳定;checkpoint-engine 把昂贵的 pin 放在注册阶段一次完成,更新阶段反复受益。
- **`MemoryBuffer` = 锁页字节区 + 布局图**:buffer 是扁平 uint8 张量,metas 按 256 对齐槽位顺序排列、offset 隐含累加;`manually_pinned` 决定注销时是否需要手动 `cudaHostUnregister`。
- **`_normal_pin_memory` 先分配再拷贝**:`_load_checkpoint` 加载(含 TP 拼接)→ 按 `max(4GiB, 最大张量)` 贪心切桶 → 名字排序保证布局确定 → 32 线程两级流水(桶分配完成即触发桶内拷贝)。
- **`_inplace_pin_memory` 原地锁页文件映射**:`torch.from_file` 映射 `/dev/shm` 中的 safetensors → 手工解析文件头得到 metas(`aligned_size = end - start`)→ `cudaHostRegister` 只改页属性不搬数据 → 删除源文件防双倍内存。
- **两条路径的 aligned_size 数值来源不同**(256 对齐 vs 文件自身布局),但语义一致,下游桶切分对两者通用。
- **分派规则**:非 CUDA 后端与共享内存池模式强制关闭 inplace;`named_tensors` 永远走 normal;`/dev/shm/*.safetensors` 且开启 inplace 时走原地锁页,结果列表中 normal 在前、inplace 在后。

## 7. 下一步学习建议

下一讲 **u2-l4《inplace pin 的底层实现与手动 unpin》** 将从本讲的边界继续下探:`_parse_and_pin_from_safetensors` 的 ctypes 细节、`unregister_checkpoint` 中 `_unpin` 用 `cudaHostGetFlags` 校验 flags 后调用 `cudaHostUnregister` 的手动解页流程,以及 `tests/test_inplace_unpin.py` 如何验证这条链路。

之后 **u2-l5《共享 pin memory 池》** 会展开本讲埋下的另一条线:`shared_pin_memory` 参数如何让 `_normal_pin_memory.register_pin_memory` 按 `idx + size` 匹配复用旧 buffer、单一使用者约束与 `force` 释放语义。

若想提前看到 pinned buffer 的"用武之地",可以先跳读 [checkpoint_engine/ps.py:684-714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714)(`_copy_to_buffer` 的异步 H2D)与 [checkpoint_engine/ps.py:813-826](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L813-L826)(`h2d_buffer` 与双缓冲的分配),它们正是 u3-l4 广播流水线的主角。
