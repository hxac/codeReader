# u2-l6 训练侧读取：CacheDataset、CacheCollator 与 CUDA 预取

## 1. 本讲目标

上一讲（u2-l5）我们知道了目标缓存是怎么「写」出来的：多卡各写本地分片，最后合并成 `manifest.json + samples.idx + shard-*.bin` 三件套。本讲回答另一半问题——训练时怎么把它高效地「读」回来：

1. **CacheDataset**：如何用 mmap + 索引偏移量，O(1) 地随机读取任意一条样本，而不必把几十 TB 缓存装进内存；
2. **CacheCollator**：如何把一批变长样本拼成形状统一、右侧补零的 batch，并顺便重建 `attention_mask`；
3. **CUDAPrefetcher**：如何用一个后台线程 + 一条独立 CUDA 流，把「取数据 + 主机到设备拷贝」与「当前 batch 的前向/反向」重叠起来。

学完本讲，你应该能独立画出一份数据「从磁盘字节到 GPU tensor」的完整旅程图，并解释途中每一次 dtype 转换的动机。

## 2. 前置知识

- **mmap（内存映射文件）**：`mmap.mmap(fd, 0, access=ACCESS_READ)` 把文件映射进进程地址空间，之后像访问内存数组一样按偏移读取。真正读盘由操作系统的**页缓存（page cache）**接管：第一次访问某页触发缺页中断读盘，之后热点页常驻内存。多个进程/线程映射同一文件共享同一份页缓存。对本项目至关重要，因为缓存默认约 38 TB（见 u2-l4），只能「按需分页读取」而不是「整体加载」。
- **PyTorch 数据协议**：`Dataset.__getitem__(i)` 返回一条样本；`DataLoader` 按 `batch_size` 收集若干条，交给 `collate_fn` 拼成 batch；`num_workers > 0` 时样本在独立 worker 子进程中产出（dataset 会被 **pickle** 发给 worker）；`pin_memory=True` 把 batch 放入锁页内存，配合 `.to(device, non_blocking=True)` 才能真正异步 H2D（主机到设备）拷贝。
- **CUDA stream（流）**：同一条流上的操作按序执行，不同流之间可以并行。把 H2D 拷贝排到一条「侧流」上，计算留在默认流上，两者即可重叠；用 `wait_stream` 建立流间依赖，用 `record_stream` 告知内存分配器张量的跨流生命周期。
- **承接 u2-l4 的存储协议**：`samples.idx` 中每条记录 56 字节（`struct "<QIIQQQQQ"`），存 `sample_id / shard_id / seq_len` 和五个段（input_ids、attention_mask、loss_mask、两个 hidden）在分片内的**起始偏移**（不存长度，长度由 `seq_len` 推导）；每条样本在分片内五段平铺，总字节数为
  \[ 4L + L + L + 2LHK + 2LH \;=\; 6L + 2LH(K+1) \]
  其中 \(L\) 为序列长度、\(H\) 为 `hidden_size`、\(K\) 为 `target_layer_ids` 层数。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [deepspec/data/target_cache_dataset.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py) | 存储协议唯一权威：本讲聚焦其中的 `CacheDataset`（读取端）与 `CacheCollator`（拼 batch） |
| [deepspec/data/cuda_prefetcher.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py) | `move_batch_to_device` 与 `CUDAPrefetcher`：H2D 拷贝与计算重叠 |
| [deepspec/data/jsonl_dataset.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/jsonl_dataset.py) | 对照组：生成缓存阶段用的 `JsonLineDataset`，同样是「mmap + 偏移索引」思路，可对照理解 |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 接线处：`CacheDataset` 的构造、DataLoader 的参数（`pin_memory`、`num_workers` 等）、`CUDAPrefetcher` 在 `train()` 中的使用 |
| [scripts/data/prepare_target_cache.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py) | 佐证：写入端按真实长度截断样本（无 padding），这是 CacheCollator 重建 attention_mask 的前提 |

## 4. 核心概念与源码讲解

### 4.1 CacheDataset：mmap + 偏移量的 O(1) 随机读取

#### 4.1.1 概念说明

训练需要对数据做**全局随机打乱**（u3-l3 的采样器每 epoch 重新洗牌），所以读取模式是完全随机的：「第 100 万条、第 7 条、第 42 万条……」。这对 38 TB 的缓存意味着两个设计约束：

- **不能整体加载**：内存装不下，也没必要——页缓存会替我们保留热点页。
- **不适合「一个样本一个文件」**：数十亿小文件的元数据开销（inode、目录项、`open/close` 系统调用）会压垮文件系统。所以 u2-l4 的协议选择了「大分片 + 定长索引」：分片按 `max_shard_bytes` 切成若干二进制文件，`samples.idx` 里每条 56 字节记录就能定位任意样本。

`CacheDataset` 就是这套协议的读取端：它不解析任何模型输入，只负责把一条样本的四个张量按偏移量从 mmap 里「抠」出来。

#### 4.1.2 核心流程

`CacheDataset.__getitem__(i)` 的完整流程（伪代码）：

```text
读取第 i 条样本:
  1. 确保索引已 mmap（懒加载，首次调用时打开 samples.idx）
  2. offset = i * 56，从索引 mmap 解出一条记录
     并断言 record.sample_id == i（索引必须按 sample_id 稠密有序）
  3. seq_len = record.seq_len；按需打开 shard_id 对应分片的 mmap（LRU，最多 4 个）
  4. 按 seq_len 推导各段字节数:
       input_ids:  4 * seq_len        (int32)
       loss_mask:     seq_len         (uint8)
       hidden:     2 * seq_len * K * H (bfloat16 按 uint16 存比特位)
       last_hidden: 2 * seq_len * H
  5. np.frombuffer(mmap, dtype, count, offset).copy() → torch 张量
       int32/uint8 直接读；bfloat16 以 uint16 读入后 view(torch.bfloat16)
  6. 返回 dict: input_ids / loss_mask / target_hidden_states / target_last_hidden_states
     （注意：不读 attention_mask 段，拼 batch 时重建，见 4.2）
```

#### 4.1.3 源码精读

**构造：先过一遍 manifest 校验，记住协议参数。**
[deepspec/data/target_cache_dataset.py:615-636](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L615-L636) 中 `__init__` 调用 `load_target_cache_manifest`（内部执行 u2-l4 讲过的全部协议断言：版本、dtype、索引大小与 `num_samples` 交叉核对、分片文件存在性），然后缓存 `num_samples / hidden_size / target_layer_ids`，并预建 `shard_id → 分片路径` 的字典。索引与分片的文件句柄**此时都不打开**，全部懒加载。

**索引读取：第 i 条记录就在 `i * 56` 处。**
[deepspec/data/target_cache_dataset.py:697-705](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L697-L705) 的 `_read_record` 先用 `_ensure_index_mmap`（[L668-675](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L668-L675)，首次调用时 `open + mmap` `samples.idx`）拿到索引映射，随后 `unpack_index_record(self.index_mmap, offset)` 定长解码。`assert record["sample_id"] == index` 这行很关键：写入端 `finalize_target_cache_index` 保证索引按全局 `sample_id` 稠密递增（见 [L540-577](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L540-L577)），这里读回来做防御性核对，一旦有人手工改动索引立刻暴露。

**分片句柄的 LRU：随机读取下控制同时打开的 mmap 数量。**
随机打乱后相邻两次读取很可能落在不同分片。[deepspec/data/target_cache_dataset.py:677-695](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L677-L695) 的 `_get_shard_mmap` 用两个 `OrderedDict`（mmap 与文件句柄一一对应）实现 LRU：命中就把该 `shard_id` `move_to_end`；未命中就打开新分片；超过 `max_open_shards`（默认 4）就 `popitem(last=False)` 淘汰最久未用的并 `close()`。这避免了「每样本 open/close」的抖动，也防止大量 mmap 耗尽句柄与虚拟地址空间。

**普通张量读取：`np.frombuffer` + `copy()`。**
[deepspec/data/target_cache_dataset.py:707-730](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L707-L730) 的 `_read_tensor_from_shard` 先断言 `offset + nbytes` 不越过分片末尾，再 `np.frombuffer(shard_mmap, dtype, count, offset).copy()`。**必须 `.copy()`**：`frombuffer` 返回的是指向 mmap 内存的是图，不复制的话张量生命周期会拖着整个分片映射（且 mmap buffer 不可写、不能安全跨线程）。之后 `torch.from_numpy(...).view(*shape)` 恢复形状，必要时转 dtype。

**bfloat16 的比特位技巧。**
[deepspec/data/target_cache_dataset.py:732-751](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L732-L751) 的 `_read_bfloat16_tensor_from_shard` 以 `np.uint16` 读入，`torch.from_numpy(array).view(torch.bfloat16)` 按比特位重解释。原因在 u2-l4 讲过：numpy 没有 bfloat16 dtype，写入端（[L226-228](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L226-L228) 的 `_tensor_to_bfloat16_bytes`）就是 `view(torch.uint16)` 存的，读取端对称地 view 回来，全程无损、零换算。

**`__getitem__` 全貌：四个张量、跳过 attention_mask。**
[deepspec/data/target_cache_dataset.py:753-798](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L753-L798) 按序读取 `input_ids`（int32）、`loss_mask`（uint8）、`target_hidden_states`（形状 `(seq_len, K*H)`，K 层隐状态在最后一维拼接）、`target_last_hidden_states`（`(seq_len, H)`）。注意它**不读** `attention_mask` 段——索引里保留着该段的偏移，但读取端直接跳过。这样做安全的前提是：缓存里每条样本都是「紧凑无 padding」的序列。这一点可以在写入端验证：[scripts/data/prepare_target_cache.py:312-327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L312-L327) 中写入前用 `attention_mask.sum(dim=1)` 算出真实长度 `seq_len`，所有张量都截取 `[:seq_len]` 后才落盘。既然没有 padding，`attention_mask` 完全由「真实长度」决定，拼 batch 时重建即可（见 4.2）。

**多进程 pickle：句柄不入镜。**
训练时 DataLoader 默认 `num_workers=4`（[deepspec/trainer/base_trainer.py:304-314](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L304-L314)，配置见 [config/dspark/dspark_qwen3_4b.py:52-57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L52-L57) 的 `data.num_workers=4`），dataset 会被 pickle 到 worker 子进程。文件句柄和 mmap 不能跨进程传递，所以 [deepspec/data/target_cache_dataset.py:660-666](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L660-L666) 的 `__getstate__` 把 `index_file/index_mmap/shard_handles/shard_mmaps` 全部置空——每个 worker 收到的是「协议参数 + 懒加载状态」，首次 `__getitem__` 时各自重建自己的 mmap。配合懒加载设计，主进程与 worker 进程互不干扰。

**对照组：JsonLineDataset 的同款思路。**
生成缓存阶段读 JSONL 用的是 [deepspec/data/jsonl_dataset.py:31-41](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/jsonl_dataset.py#L31-L41) 的 `JsonLineDataset`：构造时扫描每个文件的「行起始偏移表」（[L87-129](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/jsonl_dataset.py#L87-L129)，并用 blake2b 哈希 + pickle 缓存避免重复扫描），`__getitem__` 里 `mm.seek(starts[local_idx]); mm.readline()` 取一行再 `json.loads`。与 `CacheDataset` 相比：JSONL 变长、必须存行偏移；二进制缓存定长段、偏移可由 `seq_len` 推导。两者共享同一核心思想——**mmap + 预计算偏移 + 懒打开**。

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU、不依赖真实 38 TB 缓存，用写入端 API 手工构造一个迷你缓存目录，再用 `CacheDataset` 读回来，验证「写入什么就读出什么」。

**操作步骤**（以下为示例代码，仅依赖 `torch` 与 `numpy`，可直接保存为 `mini_cache_demo.py` 在仓库根目录运行）：

```python
# 示例代码：用写入端 API 造一个 3 样本的迷你缓存，再用 CacheDataset 读回
import os, shutil, torch
from deepspec.data.target_cache_dataset import (
    LocalTargetCacheWriter, build_target_cache_manifest,
    write_target_cache_manifest, CacheDataset, INDEX_RECORD_SIZE,
)

cache_dir = "/tmp/mini_target_cache"
shutil.rmtree(cache_dir, ignore_errors=True)
os.makedirs(cache_dir)

H, K, L = 8, 2, 5  # hidden_size=8, 2 层目标隐状态, 样本长 5
writer = LocalTargetCacheWriter(rank_dir=cache_dir, max_shard_bytes=1 << 20)
for sample_id in range(3):
    L_i = L + sample_id  # 故意做成长度不同的三条样本: 5, 6, 7
    writer.write_sample(
        sample_id=sample_id,
        input_ids=torch.arange(L_i, dtype=torch.int32) + 100 * sample_id,
        attention_mask=torch.ones(L_i, dtype=torch.uint8),
        loss_mask=torch.zeros(L_i, dtype=torch.uint8),
        target_hidden_states=torch.randn(L_i, K * H),
        target_last_hidden_states=torch.randn(L_i, H),
    )
writer.close()

# 单 rank 场景：本地索引就是全局索引，补一个 manifest 即可
shutil.copy(os.path.join(cache_dir, "samples.local.idx"),
            os.path.join(cache_dir, "samples.idx"))
write_target_cache_manifest(
    output_dir=cache_dir,
    manifest=build_target_cache_manifest(
        num_samples=3,
        shards=[{"shard_id": 0, "file_name": "shard-local-00000.bin"}],
        target_layer_ids=[5, 6],
        hidden_size=H,
    ),
)

ds = CacheDataset(cache_dir)
sample = ds[2]  # 随机读取第 2 条
print({k: (v.shape, v.dtype) for k, v in sample.items()})
```

**需要观察的现象**：

- 打印结果应为 `input_ids: torch.Size([7]) + torch.int32`、`loss_mask: torch.Size([7]) + torch.uint8`、`target_hidden_states: torch.Size([7, 16]) + torch.bfloat16`、`target_last_hidden_states: torch.Size([7, 8]) + torch.bfloat16`；
- `ds[2]["input_ids"]` 的值应从 `200` 开始（`100 * sample_id`），证明偏移定位正确；
- 返回 dict 里**没有** `attention_mask` 键。

**预期结果**：形状与 dtype 全部吻合、数值可回溯到写入时的构造。若把 `INDEX_RECORD_SIZE` 打印出来，应得到 56。以上脚本未在编写本讲义时实际运行，属「待本地验证」；任何不符都应优先怀疑样本构造而非协议代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_read_tensor_from_shard` 里的 `np.frombuffer(...)` 之后必须 `.copy()`？去掉会怎样？

**答案**：`frombuffer` 返回的数组底层直接指向 mmap 缓冲区，是只读视图且与映射生命周期绑定。不 copy 的话：一是该张量会引用整个分片 mmap，LRU 淘汰分片时 `close()` 一个仍被引用的映射会埋下崩溃隐患；二是后续在 worker 进程间传递、写入时会因 buffer 不可写而报错。`.copy()` 把 `seq_len` 相关的少量字节复制成独立内存，代价远小于整分片。

**练习 2**：把 `max_open_shards` 设成 1，随机打乱读取时会发生什么？设成 1000 又有什么风险？

**答案**：设成 1 时每次跨分片读取都要 `close` 旧分片、`open + mmap` 新分片，系统调用开销随打乱程度线性放大，页缓存也不再稳定命中；设成 1000 时若缓存分片很多（38 TB / 每分片上限），进程会持有大量文件句柄与虚拟地址映射，可能触碰 `ulimit -n` 或地址空间上限。默认 4 是「随机访问局部性」与「资源占用」之间的折中。

**练习 3**：`CacheDataset.__getitem__` 为什么敢断言 `record["sample_id"] == index`？

**答案**：因为写入端 `finalize_target_cache_index`（合并多 rank 本地索引时）按 `source_sample_start` 排序各 rank、顺序重编全局 `sample_id` 并逐条校验本地索引有序（[deepspec/data/target_cache_dataset.py:540-577](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L540-L577)），保证 `samples.idx` 第 i 条记录的 `sample_id` 恰为 i。读取端复检这条不变量，属于防御式编程：索引一旦被外部破坏立即失败而非静默读错数据。

### 4.2 CacheCollator：变长样本如何拼成一个 batch

#### 4.2.1 概念说明

`CacheDataset` 返回的每条样本长度各不相同（上面的示例就是 5/6/7），但 GPU 上的训练张量必须形状整齐。**右侧零填充（right padding）** 是标准做法：以 batch 内最长序列为基准，短序列右侧补零，再用 `attention_mask` 标记哪些位置是真实 token。

`CacheCollator` 要产出训练直接消费的 5 个字段：

| 字段 | 形状 | dtype | 来源 |
| --- | --- | --- | --- |
| `input_ids` | `(B, L_max)` | int32 | 逐条右侧补零 |
| `loss_mask` | `(B, L_max)` | uint8 | 逐条右侧补零 |
| `attention_mask` | `(B, L_max)` | int64 | **重建**：真实长度处置 1 |
| `target_hidden_states` | `(B, L_max, K*H)` | bfloat16 | 逐条右侧补零 |
| `target_last_hidden_states` | `(B, L_max, H)` | bfloat16 | 逐条右侧补零 |

注意 `attention_mask` 被转成了 `torch.long`（[L864](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L864) `torch.zeros_like(batch["input_ids"], dtype=torch.long)`），因为 HF 模型的 attention 计算习惯上接收 int64 掩码。

#### 4.2.2 核心流程

```text
collate(features):  # features 是 __getitem__ 返回的 B 个 dict
  1. input_ids、loss_mask 用 _pad_1d_batch:  建全零 (B, L_max)，逐条拷贝前 seq_len 个
  2. attention_mask:  建全零 (B, L_max) int64，前 seq_len 个位置置 1
     —— 因为缓存样本无 padding，seq_len 就是真实长度
  3. 两个 hidden 用 _pad_hidden_batch:  建全零 (B, L_max, D)，逐条拷贝
  4. 返回 5 字段 dict
```

填充位置上 `loss_mask` 为 0、`attention_mask` 为 0，因此后续损失计算（u4-l4 的 `compute_dspark_loss` 里 eval_mask 的构造）天然会忽略 padding——这是「零填充 + 双掩码」组合的意义。

#### 4.2.3 源码精读

**一维填充。**
[deepspec/data/target_cache_dataset.py:801-809](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L801-L809) 的 `_pad_1d_batch` 取 batch 内最大长度，建 `torch.zeros((B, L_max), dtype=首条dtype)`，逐条 `out[i, :seq_len] = item[key]`。dtype 取自首条样本，与协议 dtype 一致（int32 / uint8）。

**二维隐状态填充。**
[deepspec/data/target_cache_dataset.py:812-821](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L812-L821) 的 `_pad_hidden_batch` 同理，形状 `(B, L_max, hidden_dim)`，`hidden_dim` 来自 `features[0][key].shape[1]`。

**CacheCollator 本体。**
[deepspec/data/target_cache_dataset.py:859-870](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L859-L870)：先垫两个一维字段；然后**重建** `attention_mask`（`attention_mask[i, : item["input_ids"].shape[0]] = 1`）；最后垫两个 hidden。这正是 4.1.3 说的「读取端跳过 attention_mask 段」的闭环——写入端（[scripts/data/prepare_target_cache.py:312-327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L312-L327)）已把每条样本截成紧凑序列，所以「长度即有效位」。

**与 ConversationCollator 的分工。**
同一文件里的 [ConversationCollator（L824-856）](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L824-L856) 服务于缓存**生成**阶段：它接收原始 JSONL 记录，现场调用 `preprocess_record` 渲染模板产出三张量，并按 `min_loss_tokens` 过滤（不合格返回 None，整 batch 为空时返回 None）。而 `CacheCollator` 面对的是**已经物化好**的张量，无需解析、无需过滤——质量闸门在写入前已经过了。这也解释了为什么 `CacheCollator.__init__` 什么都不需要：它是一个无状态的纯函数对象（`data_collator_cls = CacheCollator`，见 [deepspec/trainer/dspark_trainer.py:15](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/dspark_trainer.py#L15)）。

#### 4.2.4 代码实践

**实践目标**：直观看到「变长进、整齐出」，统计 collate 后每个字段的形状。

**操作步骤**：在 4.1.4 的脚本末尾追加（示例代码）：

```python
from deepspec.data import CacheCollator

collator = CacheCollator()
batch = collator([ds[0], ds[1], ds[2]])  # 三条长度 5/6/7 的样本
for key, value in batch.items():
    print(f"{key:28s} shape={tuple(value.shape)} dtype={value.dtype}")
print("attention_mask 行和:", batch["attention_mask"].sum(dim=1).tolist())
```

**需要观察的现象**：`input_ids/loss_mask/attention_mask` 形状 `(3, 7)`；两个 hidden 分别 `(3, 7, 16)` 与 `(3, 7, 8)`；`attention_mask` 行和为 `[5, 6, 7]`；`input_ids` 的 dtype 仍是 `torch.int32`（转 int64 发生在搬运到 GPU 时，见 4.3）。

**预期结果**：短样本右侧全零、`attention_mask` 精确标记有效长度。本段与 4.1.4 同一脚本，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CacheCollator` 不像 `ConversationCollator` 那样可能返回 None？

**答案**：`ConversationCollator` 在线做模板解析，可能遇到监督 token 过少（`min_loss_tokens` 不达标）的样本需要丢弃；而 `CacheCollator` 的输入是缓存里已通过质检的张量，写入阶段（prepare_target_cache 前的 `ConversationCollator` 过滤）已把不合格样本挡在门外，读取端没有可失败的业务逻辑。

**练习 2**：如果 batch 内最长序列是 4096、其余都是 100 左右，这个 collator 的效率如何？有什么改进思路？

**答案**：会产生大量无效填充与无效计算（hidden 部分尤甚，`(B, 4096, K*H)` 的 bfloat16 块几乎全零）。经典改进是按长度分桶（length-grouped batching）或引入 packing/序列交织；本仓库选择用 `local_batch_size=1`（默认配置）从根上规避该问题——单条样本无需填充，这也是 [config/dspark/dspark_qwen3_4b.py:38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L38) 的取舍。放大会 batch 时填充代价随之显现，`attention_mask` 的存在保证了结果仍正确，只是浪费算力。

**练习 3**：`_pad_1d_batch` 的 dtype 取 `features[0][key].dtype`，这依赖什么隐含约定？

**答案**：依赖同一字段在全数据集内 dtype 一致——由 manifest 协议锁死（`token_dtype=int32`、`mask_dtype=uint8`、`hidden_dtype=bfloat16`），且 `CacheDataset` 读取时要么 dtype 已匹配直接返回、要么显式转换。所以首条样本即代表全体。

### 4.3 CUDAPrefetcher：后台线程 + 独立 CUDA 流的双缓冲

#### 4.3.1 概念说明

一个训练步的朴素数据路径是串行的：

```text
取下一个 batch（DataLoader 迭代，含磁盘读取+collate）
→ 拷贝到 GPU（H2D）
→ forward / backward
→ 取下一个 batch ……（GPU 在这段时间内闲着）
```

若每步计算耗时 \(t_c\)、取数与搬运耗时 \(t_d\)，串行步长为 \(t_c + t_d\)。`CUDAPrefetcher` 的目标是把两者重叠，使步长逼近
\[ \max(t_c,\; t_d) \]
手段就是标题里的两件事：

- **后台线程**：在主线程对当前 batch 做 forward/backward 的同时，另一个 Python 线程去 `next(dataloader)` 取下一个 CPU batch；
- **独立 CUDA 流**：把该 batch 的 H2D 拷贝排到一条侧流上（`non_blocking=True`），不占用默认计算流。

这是 NVIDIA 官方性能建议中经典的 "CUDA prefetcher" 模式，全文件不到 80 行。

#### 4.3.2 核心流程

双缓冲的时间线：

```text
__iter__():
  同步取第 1 个 batch，H2D 排到侧流                ← 启动开销，只发生一次

__next__():  （第 k 次调用）
  1. join 上一轮 kick off 的后台线程（此时多半已完成）
  2. 若数据耗尽 → StopIteration
  3. 计算流 wait_stream(侧流)      ← 确保第 k 个 batch 的 H2D 已完成
  4. 对 batch 每个张量 record_stream(计算流)  ← 延长显存生命周期
  5. 启动后台线程：取第 k+1 个 batch 并把 H2D 排到侧流
  6. 返回第 k 个 batch → 主线程开始 forward/backward
     （与此同时侧流在搬运第 k+1 个 batch）
```

任意时刻 CPU 侧最多准备「1 个在途 batch」，GPU 侧最多驻留「当前 + 在途」两个 batch，故名双缓冲。

#### 4.3.3 源码精读

**搬运与 int64 转换。**
[deepspec/data/cuda_prefetcher.py:6-11](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L6-L11) 的 `move_batch_to_device` 逐字段 `.to(device, non_blocking=True)`，随后一段关键注释（[L8-10](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L8-L10)）：

> Embedding lookup requires int64; cast on GPU to avoid bloating CPU-to-GPU transfer.

PyTorch 的 embedding 查表要求索引为 int64，但缓存协议把 token 存成 int32（u2-l4）——磁盘上省一半、PCIe 传输上也省一半；到达 GPU 后才 `.to(torch.long)`（显存充足，转换极快）。这就是数据旅程的最后一跳 dtype 转换。

**初始化与首个 batch。**
[deepspec/data/cuda_prefetcher.py:22-34](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L22-L34)：构造时只创建 `torch.cuda.Stream(device)`（侧流），不碰 dataloader；`__iter__` 建迭代器并**同步**预取第一个 batch，保证第一次 `__next__` 有货可返。

**取数 + 排队 H2D。**
[deepspec/data/cuda_prefetcher.py:36-44](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L36-L44) 的 `_fetch_and_transfer`：`next(self._iter)` 取 CPU batch（`StopIteration` 则置 `_done`），并在 `with torch.cuda.stream(self.stream)` 上下文里调用 `move_batch_to_device`——上下文使后续 H2D 拷贝都排到侧流。`non_blocking=True` 能真正异步的前提是源内存是锁页的，这正是 DataLoader `pin_memory=True`（[deepspec/trainer/base_trainer.py:310](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L310)）的职责。

**同步与生命周期。**
[deepspec/data/cuda_prefetcher.py:46-70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L46-L70) 的 `__next__` 是精髓：

- `current.wait_stream(self.stream)`（[L56-57](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L56-L57)）：在**计算流**上插一个等待点，直到侧流上的拷贝全部完成，消费侧才碰这批张量；
- `value.record_stream(current)`（[L62-63](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L62-L63)）：PyTorch 的缓存分配器按流追踪张量；这批张量在侧流上分配/写入，若不声明，分配器可能在侧流操作结束后就把显存回收复用，而计算流还没读完。`record_stream` 告诉分配器「计算流仍在使用」，防止显存被提前回收——这是跨流使用张量时最容易踩的隐性坑；
- 最后 kick off 后台线程（[L67-68](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L67-L68)），与注释所说「overlap with compute on the batch we are about to return」对应。

`__len__`（[L72-73](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/cuda_prefetcher.py#L72-L73)）透传给 dataloader，`for batch in prefetcher` 语义不变。

**在 BaseTrainer 中的接线。**
[deepspec/trainer/base_trainer.py:295-314](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L295-L314) 构造 DataLoader：`num_workers=4`（CPU 侧多进程流水线）+ `pin_memory=True` + `prefetch_factor=4` + `persistent_workers=True` + `drop_last=True`，采样器是 u3-l3 要讲的 `StatelessResumableDistributedSampler`；随后 [deepspec/trainer/base_trainer.py:365-373](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L365-L373) 用 `CUDAPrefetcher(dataloader, self.device)` 包一层，`train()` 主循环直接 `for batch in prefetcher:`。于是整条供给线是三级流水：worker 进程读盘+collate（4 个 worker、各预取 4 批）→ 锁页内存 → 侧流 H2D（1 批在途）→ 计算流消费。

#### 4.3.4 代码实践

**实践目标**：在有 GPU 的环境下观察 prefetcher 的重叠行为；无 GPU 时验证 `move_batch_to_device` 的 dtype 转换逻辑。

**操作步骤**：

1. 无 GPU 环境先做 dtype 验证（示例代码，CPU 可跑）：

```python
# 示例代码：CPU 上验证 move_batch_to_device 的 int64 转换
import torch
from deepspec.data.cuda_prefetcher import move_batch_to_device

batch = {"input_ids": torch.randint(0, 100, (2, 5), dtype=torch.int32),
         "loss_mask": torch.ones(2, 5, dtype=torch.uint8)}
moved = move_batch_to_device(batch, torch.device("cpu"))
print(moved["input_ids"].dtype)  # 期望 torch.int64
print(moved["loss_mask"].dtype)  # 期望保持 torch.uint8
```

2. 有 GPU 的环境（待本地验证）：把 4.1.4 + 4.2.4 产出的 `batch` 交给 prefetcher 迭代，测量每步耗时：

```python
# 示例代码：需要 CUDA 环境
import time, torch
from torch.utils.data import DataLoader
from deepspec.data.cuda_prefetcher import CUDAPrefetcher

dl = DataLoader(ds, batch_size=3, collate_fn=CacheCollator(), pin_memory=True)
pf = CUDAPrefetcher(dl, torch.device("cuda"))
t0 = time.perf_counter()
n = 0
for batch in pf:
    _ = batch["input_ids"].float().sum().item()  # 占位模拟消费
    n += 1
print(f"{n} batches, {(time.perf_counter() - t0) * 1000:.1f} ms")
```

再写一个对照循环（直接 `for batch in dl:` 每步同步 `.to("cuda")`），比较两者总耗时。

**需要观察的现象**：步骤 1 打印 `torch.int64` 与 `torch.uint8`；步骤 2 中（数据量够大、搬运够久时）prefetcher 版本应不慢于对照版本，且 `batch["input_ids"].dtype` 为 int64、`device` 为 cuda。

**预期结果**：dtype 转换只发生在 `input_ids` 一个字段上；迷你数据集太小可能看不出差异——想观察到重叠收益需要把数据放大到搬运耗时与模拟计算耗时同量级。GPU 部分属「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `record_stream(current)`，最可能出现什么症状？

**答案**：侧流上的 H2D 完成后，缓存分配器认为这批张量在侧流上的使用已结束，可回收显存供后续分配复用；而计算流可能仍在读取旧数据，导致偶发的数据损坏（读到被覆盖的显存）或非法内存访问。这类 bug 与时序相关、极难复现，正是 `record_stream` 存在的意义。

**练习 2**：为什么 prefetcher 只缓冲 1 个 batch（双缓冲），而不是开一个 8 深度的队列把 H2D 提前排满？

**答案**：一是显存约束——每个在途 batch 含 `(B, L, K*H)` 的 bfloat16 隐状态（默认配置 L=4096、K=5 时单样本就几十 MB），深队列会与模型和优化器抢显存；二是收益边际递减——只要 \(t_d \le t_c\)，1 个在途 batch 就足以藏住搬运延迟，更深队列并不缩短步长。DataLoader 侧（CPU 内存）倒是便宜，所以 `prefetch_factor=4` 用在 worker 层。

**练习 3**：`CUDAPrefetcher` 的后台线程会同时有多个吗？`_thread.join()` 在等什么？

**答案**：任意时刻至多 1 个。`__next__` 每次先 `join` 上一轮启动的线程（等待「取数 + H2D 排队」这件事在主机侧完成——注意 H2D 本身已异步排到侧流，join 只等排队动作，不等传输完成），再启动新线程。因此异常也会被主线程在 join 处感知到，不会静默丢失。

## 5. 综合实践

把三个模块串成一张「数据完整旅程图」，并动手验证。建议流程：

1. **画旅程图**（纸面作业）。以 4.1.4 的迷你缓存为对象，把下面每一站标出来，注明发生 dtype/设备变化的位置：

```text
shard-*.bin (磁盘字节: int32/uint8/uint16 视图的 bfloat16)
  → CacheDataset.__getitem__: np.frombuffer(mmap).copy() → torch 张量
      (input_ids:int32, loss_mask:uint8, hidden:bfloat16, 无 attention_mask)
  → CacheCollator: 右侧零填充 → (B, L_max, ...) + 重建 attention_mask(int64)
  → DataLoader worker (num_workers=4) / pin_memory → 锁页内存
  → CUDAPrefetcher 侧流: non_blocking H2D
      input_ids 在 GPU 上 int32 → int64   ← 最后一跳转换
  → run_batch(batch) 进入 DSpark/Eagle3 前向
```

2. **脚本验证**。运行 4.1.4 + 4.2.4 + 4.3.4 步骤 1 的合并脚本，逐站打印形状与 dtype，与你在图上标注的逐一对照；有 GPU 再补 4.3.4 步骤 2。
3. **统计口径**。按 `B=3、L_max=7、K=2、H=8` 手算 collate 后 batch 的总字节数（提示：int32 的 input_ids 每元素 4 字节，uint8 每元素 1 字节，bfloat16 每元素 2 字节，attention_mask 为 int64），与 `sum(t.numel() * t.element_size() for t in batch.values())` 的程序结果比对——这会让你对「为什么隐状态主导缓存体积」（u2-l4 的 99.96%）有字节级的直觉。

**预期结果**：旅程图与脚本输出一致；手算字节数与程序结果相等（本例应为 input_ids 84 + loss_mask 21 + attention_mask 168 + hidden 672 + last_hidden 336 = 1281 字节）。

## 6. 本讲小结

- `CacheDataset` 用「mmap + 定长索引偏移」实现 38 TB 缓存的 O(1) 随机读取：索引第 i 条记录在 `i*56` 处，分片 mmap 以 LRU（默认 4 个）懒打开，`__getstate__` 清空句柄以支持多 worker pickle。
- 读取端跳过 `attention_mask` 段不读——因为写入端按真实长度截断、缓存样本无 padding；`CacheCollator` 拼右填充 batch 时据长度重建 int64 的 `attention_mask`。
- bfloat16 在 numpy 不可表达，落盘与读取都走 `uint16` 比特位视图，无损且零换算。
- collate 产出 5 字段：`input_ids/loss_mask/attention_mask` 形状 `(B, L_max)`，两个 hidden 形状 `(B, L_max, K*H)`、`(B, L_max, H)`；`local_batch_size=1` 的默认配置从根上规避了填充浪费。
- `CUDAPrefetcher` 用「后台线程取数 + 独立侧流 H2D + `wait_stream`/`record_stream` 同步」把数据搬运与计算重叠，使步长从 \(t_c+t_d\) 逼近 \(\max(t_c, t_d)\)；`pin_memory=True` 是异步 H2D 的前提。
- token 的 dtype 旅程：磁盘 int32 → GPU 上才转 int64（embedding 查表要求），节省的是传输带宽；这一跳发生在 `move_batch_to_device`。

## 7. 下一步学习建议

数据供给线到此完整闭合。下一讲进入第 3 单元：建议先读 **u3-l1（BaseTrainer 初始化）**，看 `CacheDataset` 在 [deepspec/trainer/base_trainer.py:190](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L190) 被构造后如何经 `validate_train_cache` 与草稿模型配置对齐、`_compute_training_schedule` 如何由 `len(train_dataset)` 推出训练步数；再读 **u3-l3（分布式与采样器）** 弄清 `StatelessResumableDistributedSampler` 递给 `__getitem__` 的那些随机索引从何而来。想先看数据被怎么消费的读者，也可以直接跳 u4-l2 的 `_forward_backbone`。
