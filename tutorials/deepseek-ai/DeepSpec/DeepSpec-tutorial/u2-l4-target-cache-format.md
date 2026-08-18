# 目标缓存的存储协议：manifest、索引与分片

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 target cache 目录中 `manifest.json`、`samples.idx`、`shard-*.bin` 三类文件各自的职责，以及它们如何互相校验。
2. 手工解码一条 `INDEX_RECORD_STRUCT` 索引记录：知道 56 字节里每个字段的类型、宽度和含义。
3. 用 `expected_target_cache_tensor_nbytes` 推导缓存体积公式，并在给定 `seq_len`、`hidden_size`、目标层数、样本数时估算出缓存字节数，理解 README 中 38 TB 警告的由来。

本讲只讲「存储协议」——数据长什么样、放在哪里、如何校验。缓存是怎么被生成出来的（forward hook 抓取中间层）是下一讲 u2-l5 的内容；训练时怎么高效读取（mmap、collator、prefetch）是 u2-l6 的内容。

## 2. 前置知识

### 2.1 为什么需要 target cache

回顾 u1-l1：DeepSpec 训练的草稿模型以目标模型的中间层隐藏状态作为输入特征。如果每个训练 step 都要现场跑一遍 Qwen3-4B 前向，目标模型就会成为训练流水线的瓶颈，而且同样的样本在 10 个 epoch 里被重复前向 10 次。因此数据准备阶段把每条样本的目标模型中间层输出**一次性预计算并落盘**，训练时只读盘、不再碰目标模型。代价是磁盘：README 明确警告默认设置下约 **38 TB**——本讲的体积估算练习就是要让你理解这个数字是怎么来的。

### 2.2 二进制文件、定长记录与偏移量

JSONL（u2-l1 见过）是文本格式，人能读但解析慢、体积大。隐藏状态是稠密浮点数，用文本存会膨胀数倍，所以 target cache 用**二进制格式**：

- **分片（shard）**：一个大二进制文件，里面背靠背地平铺着许多样本的字节。样本长度各不相同，所以需要索引来定位。
- **定长索引记录**：每条样本对应一条固定 56 字节的索引记录。定长的好处是「第 \(i\) 条记录的位置 = \(i \times 56\)」，不需要解析前面的记录就能直接跳转（seek）到任意一条。
- **偏移量（offset）**：记录里存的是「某张量在分片文件内的起始字节位置」。知道起始偏移、元素个数和数据类型宽度，就能算出要读多少字节。

### 2.3 struct 模块与格式字符串

Python 标准库 `struct` 用于在字节串和数值之间转换。格式字符串 `"<QIIQQQQQ"` 的含义：

- `<`：小端序（little-endian，低位字节在前），且**不做内存对齐填充**——x86/ARM 服务器都是小端序，这让字节布局完全紧凑可预测。
- `Q`：无符号 64 位整数（8 字节）。
- `I`：无符号 32 位整数（4 字节）。

### 2.4 bfloat16 与「按 uint16 原始位存储」

bfloat16（brain float 16）用 2 字节表示一个浮点数：1 位符号、8 位指数（与 fp32 相同）、7 位尾数。它是大模型训练/推理的常用精度。麻烦在于 **numpy 没有原生 bfloat16 类型**，所以本仓库的做法是：把 torch 张量转成 bfloat16 后，用 `.view(torch.uint16)` 把它重新解释成 16 位无符号整数（只是换个视角看同一串比特，不改内容），再用 numpy 落盘；读取时反向 `view(torch.bfloat16)` 还原。读写两侧约定一致，比特层面无损。

### 2.5 mmap（内存映射文件）

`mmap` 把文件映射进进程地址空间，之后像访问内存数组一样访问文件内容，由操作系统按页惰性加载。`CacheDataset` 用它打开 `samples.idx` 和分片文件，避免「open + seek + read」的系统调用开销。细节留到 u2-l6，本讲只需要知道索引是被 mmap 后按偏移读取的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/data/target_cache_dataset.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py) | 协议的**唯一权威定义**：版本号、索引结构体、dtype 常量、体积公式、manifest 构建与校验、写入器、读取数据集 |
| [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md) | 数据准备三步说明，含 38 TB 存储警告与缩小缓存的建议 |
| [scripts/data/prepare_target_cache.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py) | 协议的**消费方之一**（写入端驱动脚本）：多 rank 写入、分片重命名、全局索引合并、manifest 落盘 |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 默认配置：`target_layer_ids=[1,9,17,25,33]`、`max_length=4096`，是体积估算的现实参数来源 |

## 4. 核心概念与源码讲解

先给出全局图景。一个 target cache 目录长这样：

```text
<cache_dir>/
├── manifest.json        # 元数据「合同」：版本、dtype、层数、分片清单……
├── samples.idx          # 定长索引：每条样本 56 字节，指向分片内的偏移
├── shard-00000.bin      # 分片 0：样本字节平铺（默认上限 64 GiB/分片）
├── shard-00001.bin
└── ...
```

三类文件的职责分工：

- **manifest.json**：描述「这份缓存是什么」——协议版本、样本数、分片数、抓了目标模型哪几层、隐藏维度、三个 dtype 常量、索引记录宽度、分片文件清单，外加一批溯源字段（哪个目标模型、哪些源 JSONL、git sha 等）。它是读取端校验的入口。
- **samples.idx**：描述「第 \(i\) 条样本的数据在哪个分片的哪些字节」——定长 56 字节记录的数组。
- **shard-*.bin**：纯字节负载，没有任何自描述信息，完全依赖前两个文件解释。

### 4.1 manifest.json 结构

#### 4.1.1 概念说明

manifest 是写入端与读取端之间的「合同」。它的价值不在存了什么数据，而在**锁死了协议的每一个可变维度**：

1. **版本**：`version` 字段必须等于代码里的 `TARGET_CACHE_VERSION`（当前为 2）。如果未来索引格式改动，老缓存会在加载时立刻报错，而不是读出静默错乱的数据。
2. **dtype**：`hidden_dtype`/`token_dtype`/`mask_dtype` 三个字段与代码常量逐一比对，防止有人用 float16 重写了一份缓存却让 bfloat16 的读取端去解释。
3. **几何参数**：`target_layer_ids`（抓了目标模型哪些层）与 `hidden_size` 共同决定了 `target_hidden_states` 的最后一维宽度——这也是训练前 `validate_train_cache` 与草稿模型配置比对的字段。
4. **分片清单**：`shards` 列出所有分片文件的 `shard_id` 与 `file_name`，并要求 id 从 0 起连续。

#### 4.1.2 核心流程

读取端加载 manifest 的流程：

```text
load_target_cache_manifest(cache_dir)
  ├─ 读 cache_dir/manifest.json（不存在 → assert 失败）
  ├─ validate_target_cache_manifest(cache_dir, manifest)
  │    ├─ 必填字段齐全（10 个）
  │    ├─ version == 2；三个 dtype 与代码常量一致；index_record_size == 56
  │    ├─ hidden_size > 0；target_layer_ids 非空且升序
  │    ├─ num_shards == len(shards)；shard_id 从 0 连续；每个分片文件真实存在
  │    └─ samples.idx 存在；大小是 56 的整数倍；且 == num_samples × 56
  └─ 返回 manifest dict
```

注意最后一条：manifest 与索引文件做了**交叉校验**——`samples.idx` 的字节数必须严格等于 `num_samples × 56`，任何一侧不一致都会在加载时暴露，而不是读到一半越界。

写入端（`prepare_target_cache.py`）在所有分片就位后，用 `build_target_cache_manifest` 组装基础字段，再通过 `extra_fields` 叠加溯源信息，最后用 `write_target_cache_manifest` 原子落盘。

#### 4.1.3 源码精读

协议常量与索引结构体定义在文件顶部——这是整个协议的「宪法」：

- [deepspec/data/target_cache_dataset.py:20-26](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L20-L26)：定义 `TARGET_CACHE_VERSION = 2`、`INDEX_RECORD_STRUCT = struct.Struct("<QIIQQQQQ")`（56 字节/条），以及三个 dtype 常量：隐藏态 `bfloat16`、token `int32`、mask `uint8`。

manifest 的字段集合由构建函数决定：

- [deepspec/data/target_cache_dataset.py:580-602](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L580-L602)：`build_target_cache_manifest` 写入 10 个基础字段（version、num_samples、num_shards、target_layer_ids、三个 dtype、index_record_size、hidden_size、shards），`extra_fields` 非空时合并进去。

校验逻辑（读取端第一道闸门）：

- [deepspec/data/target_cache_dataset.py:132-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L132-L153)：先检查 10 个必填字段一个不少（缺了会在报错信息里列出缺失名单），再断言 `version == 2`。
- [deepspec/data/target_cache_dataset.py:154-176](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L154-L176)：三个 dtype 逐一与代码常量比对；`index_record_size` 必须等于 56；`hidden_size` 为正；`target_layer_ids` 非空且**升序**（升序约定与写入端 `torch.cat` 的拼接顺序对应，读取端按同一顺序切分各层）。
- [deepspec/data/target_cache_dataset.py:177-200](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L177-L200)：`num_shards` 与 `shards` 列表长度一致、shard_id 从 0 连续、每个分片文件真实存在；最后交叉校验 `samples.idx` 的大小既是 56 的倍数、又恰好等于 `num_samples × 56`。

溯源字段是写入脚本叠加的（校验不强制，但训练时会用到）：

- [scripts/data/prepare_target_cache.py:175-199](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L175-L199)：`_write_manifest` 通过 `extra_fields` 追加 `target_model_name_or_path`、`source_jsonl_paths`、`chat_template`、`max_length`、`min_loss_tokens`、`project_name`、`exp_name`、`git_sha`。其中 `target_model_name_or_path` 会被训练侧的 `validate_train_cache`（[deepspec/data/target_cache_dataset.py:203-219](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L203-L219)）逐字段比对：缓存的层配置、隐藏维度、目标模型路径必须与当前训练配置完全一致，否则拒绝开训——防止拿 Qwen3 的缓存去训 Gemma4 的草稿。

manifest 的落盘是原子操作：

- [deepspec/data/target_cache_dataset.py:29-35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L29-L35)：`atomic_json_dump` 先写 `manifest.json.tmp`，`flush + fsync` 强制刷盘，再 `os.replace` 原子改名。即使进程在写入中途被杀，也只会留下完整的旧文件（或还不存在的 tmp），不会出现半个 JSON。

#### 4.1.4 代码实践

**实践目标**：亲手构建并通过校验一份最小 manifest，再观察校验如何拦截被篡改的字段。

**操作步骤**（示例代码，可直接保存为 `play_manifest.py` 在仓库根目录运行）：

```python
import os
import struct
import tempfile

from deepspec.data.target_cache_dataset import (
    INDEX_RECORD_SIZE,
    build_target_cache_manifest,
    load_target_cache_manifest,
)

with tempfile.TemporaryDirectory() as cache_dir:
    # 1. 准备一个分片文件（校验只要求它存在）
    with open(os.path.join(cache_dir, "shard-00000.bin"), "wb") as f:
        f.write(b"\x00" * 128)
    # 2. 准备 3 条 56 字节的假索引记录，凑出合法的 samples.idx
    with open(os.path.join(cache_dir, "samples.idx"), "wb") as f:
        for _ in range(3):
            f.write(b"\x00" * INDEX_RECORD_SIZE)
    # 3. 构建 manifest 并落盘
    manifest = build_target_cache_manifest(
        num_samples=3,
        shards=[{"shard_id": 0, "file_name": "shard-00000.bin"}],
        target_layer_ids=[1, 9, 17, 25, 33],
        hidden_size=2560,
    )
    from deepspec.data.target_cache_dataset import write_target_cache_manifest
    write_target_cache_manifest(output_dir=cache_dir, manifest=manifest)

    # 4. 正常加载应通过全部校验
    loaded = load_target_cache_manifest(cache_dir)
    print("OK:", loaded["version"], loaded["num_samples"], loaded["index_record_size"])

    # 5. 篡改实验 A：删掉 hidden_size 字段 → 报缺失字段
    del manifest["hidden_size"]
    write_target_cache_manifest(output_dir=cache_dir, manifest=manifest)
    try:
        load_target_cache_manifest(cache_dir)
    except AssertionError as e:
        print("A 被拦截:", str(e)[:80])

    # 6. 篡改实验 B：num_samples 改成 4（与 samples.idx 大小不符）
    manifest["hidden_size"] = 2560
    manifest["num_samples"] = 4
    write_target_cache_manifest(output_dir=cache_dir, manifest=manifest)
    try:
        load_target_cache_manifest(cache_dir)
    except AssertionError as e:
        print("B 被拦截:", str(e)[:80])
```

**需要观察的现象**：第 4 步打印 `OK: 2 3 56`；第 5 步的报错信息里应出现 `missing required fields ['hidden_size']` 字样；第 6 步应触发 `samples.idx size does not match manifest.num_samples`（3 × 56 = 168 ≠ 4 × 56 = 224）。

**预期结果**：三处断言行为分别对应「字段完整性」「协议常量」「交叉一致性」三类校验。具体输出格式以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `manifest.json` 里要存 `index_record_size`，读取端不是自己知道 `INDEX_RECORD_SIZE` 吗？

**答案**：这正是「双保险」设计：代码常量 `INDEX_RECORD_SIZE` 是当前版本的权威值，校验时要求 `manifest["index_record_size"] == INDEX_RECORD_SIZE`（[target_cache_dataset.py:166-169](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L166-L169)）。若一份旧缓存是用不同记录宽度写的，仅凭文件内容无法被发现；把宽度写进 manifest 并与代码比对，就能在加载时立刻发现「这份缓存不是当前协议写的」。

**练习 2**：`target_model_name_or_path` 不在 `required_fields` 里，但如果缺失，训练时会发生什么？

**答案**：`validate_target_cache_manifest` 不会报错（它只检查 10 个必填字段），但训练侧 `validate_train_cache` 会执行 `manifest["target_model_name_or_path"]`（[target_cache_dataset.py:214](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L214)），直接抛 `KeyError`。也就是说协议校验层把它视为可选溯源字段，训练入口把它视为必备字段。

### 4.2 samples.idx 定长索引

#### 4.2.1 概念说明

`samples.idx` 回答一个问题：**第 \(i\) 条样本的数据在哪里**。它是一个由 56 字节定长记录组成的数组，第 \(i\) 条记录位于字节偏移 \(i \times 56\) 处。每条记录 8 个字段：

| 字段 | 类型 | 字节 | 含义 |
| --- | --- | --- | --- |
| `sample_id` | `Q`（uint64） | 8 | 全局样本编号，必须从 0 起连续密集 |
| `shard_id` | `I`（uint32） | 4 | 该样本的数据在哪个分片文件 |
| `seq_len` | `I`（uint32） | 4 | 该样本的真实序列长度（未填充） |
| `input_ids_offset` | `Q` | 8 | input_ids 在分片内的起始字节 |
| `attention_mask_offset` | `Q` | 8 | attention_mask 在分片内的起始字节 |
| `loss_mask_offset` | `Q` | 8 | loss_mask 在分片内的起始字节 |
| `target_hidden_states_offset` | `Q` | 8 | target_hidden_states 在分片内的起始字节 |
| `target_last_hidden_states_offset` | `Q` | 8 | target_last_hidden_states 在分片内的起始字节 |

合计 \(8 + 4 + 4 + 5 \times 8 = 56\) 字节。

三个设计要点：

1. **偏移是分片内的相对偏移**，不是全局字节位置。定位一条样本 = 先用 `shard_id` 找到分片文件，再用偏移在该分片内定位。
2. **只存起始偏移、不存长度**。因为写入顺序固定（4.3 节的五段布局）且 dtype 宽度固定，每个张量的字节长度都可由 `seq_len` 推导（`expected_target_cache_tensor_nbytes`），存长度是冗余。五个偏移都显式落盘则是「显式优于推导」的取舍——读取端不必依赖布局推导即可直接定位每个张量。
3. **偏移用 `Q`（64 位）而 `shard_id`/`seq_len` 用 `I`（32 位）**：默认分片上限是 64 GiB（[scripts/data/prepare_target_cache.py:149](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L149)），早已超过 32 位可表示的范围；而分片数量和序列长度（≤ max_length=4096）用 32 位绰绰有余。

#### 4.2.2 核心流程

**读路径**（单条记录）：

```text
_read_record(index)
  ├─ 确保 samples.idx 已 mmap
  ├─ offset = index × 56
  ├─ INDEX_RECORD_STRUCT.unpack_from(mmap, offset) → 8 元组 → dict
  └─ assert record.sample_id == index   # 保证索引按 sample_id 稠密有序
```

**写路径**（多 rank 合并为全局索引）是本协议最巧妙的部分。生成缓存时每个 GPU rank 各自独立写入，互不通信：

```text
每个 rank：
  _tmp/rank_<r>/shard-local-XXXXX.bin   # 本地分片（shard_id 从 0 起，仅本地有效）
  _tmp/rank_<r>/samples.local.idx       # 本地索引（sample_id 从 0 起，仅本地有效）
  _tmp/rank_<r>/summary.json            # 本地写了几个样本、几个分片

收尾（rank 0 汇总，广播 shard_map）：
  1. 按 source_sample_start 排序各 rank 的 summary
  2. build_global_target_cache_shard_map：依序给每个本地分片分配全局 shard_id
  3. 各 rank 把 shard-local-*.bin 改名为全局 shard-XXXXX.bin（os.replace）
  4. finalize_target_cache_index（仅 rank 0）：
       按序读每个 rank 的 samples.local.idx，
       把 record.sample_id 改写为全局连续编号，
       把 record.shard_id 经 shard_map 映射为全局分片号，
       写入 samples.idx.tmp → os.replace → samples.idx
```

这样最终的 `samples.idx` 中，全局 `sample_id` 的顺序 = 各 rank 所负责的源数据区间顺序拼接，保证稠密且有序——这是读取端 `_read_record` 断言成立的前提。

#### 4.2.3 源码精读

- [deepspec/data/target_cache_dataset.py:100-120](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L100-L120)：`unpack_index_record` 用 `INDEX_RECORD_STRUCT.unpack_from(buffer, offset)` 从（mmap 的）字节缓冲区解出 8 字段并转成 dict——本讲实践任务的核心就是这五行。
- [deepspec/data/target_cache_dataset.py:77-97](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L77-L97)：`pack_index_record` 是逆操作，8 个关键字参数按固定顺序 `pack` 成 56 字节。
- [deepspec/data/target_cache_dataset.py:697-705](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L697-L705)：`_read_record` 展示读取端用法：`offset = index * INDEX_RECORD_SIZE` 后 `unpack_index_record(self.index_mmap, offset)`，并断言 `sample_id == index`（索引稠密有序的自我检查）。
- [deepspec/data/target_cache_dataset.py:510-529](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L510-L529)：`build_global_target_cache_shard_map` 按 `source_sample_start` 排序各 rank 摘要，依次分配全局 shard_id，产出全局分片清单与「rank → 本地分片号 → 全局分片号」的映射表。
- [deepspec/data/target_cache_dataset.py:540-577](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L540-L577)：`finalize_target_cache_index` 逐 rank 读入本地索引字节，断言本地记录按本地 `sample_id` 有序，然后把 `sample_id` 改写为全局编号、`shard_id` 经映射表改写为全局分片号，重新 pack 后写入 `samples.idx.tmp`，最后 `os.replace` 原子生效。

#### 4.2.4 代码实践

**实践目标**：用 `INDEX_RECORD_STRUCT` 打包再解码前 3 条索引记录，做到能手工读懂 56 字节。

**操作步骤**（示例代码）：

```python
import struct

from deepspec.data.target_cache_dataset import (
    INDEX_RECORD_SIZE,
    INDEX_RECORD_STRUCT,
    pack_index_record,
    unpack_index_record,
)

# 自造 3 条记录，模拟 3 个样本落在不同分片、长度各异
records = [
    dict(sample_id=0, shard_id=0, seq_len=4096,
         input_ids_offset=0, attention_mask_offset=16384, loss_mask_offset=20480,
         target_hidden_states_offset=24576, target_last_hidden_states_offset=104882176),
    dict(sample_id=1, shard_id=0, seq_len=100,
         input_ids_offset=125853696, attention_mask_offset=125854096, loss_mask_offset=125854196,
         target_hidden_states_offset=125854296, target_last_hidden_states_offset=128014296),
    dict(sample_id=2, shard_id=1, seq_len=7,
         input_ids_offset=0, attention_mask_offset=28, loss_mask_offset=35,
         target_hidden_states_offset=42, target_last_hidden_states_offset=39242),
]

buffer = b"".join(pack_index_record(**r) for r in records)
print("字节数:", len(buffer), "= 3 ×", INDEX_RECORD_SIZE)

for i in range(3):
    print(unpack_index_record(buffer, i * INDEX_RECORD_SIZE))

# 手工裸解一条：不借助辅助函数，直接 struct
fields = INDEX_RECORD_STRUCT.unpack_from(buffer, 0)
print("裸解第 1 条:", fields)

# 观察 56 字节的原始十六进制（小端序：低位字节在前）
print(buffer[:8].hex(), "<- sample_id=0 的 8 字节，小端序全零")
print(buffer[8:12].hex(), "<- shard_id=0")
print(buffer[12:16].hex(), "<- seq_len=4096 = 0x1000，小端序为 00100000")
```

**需要观察的现象**：`unpack_index_record` 打印出的 dict 与写入的完全一致；`seq_len=4096`（即 0x1000）的四个字节按小端序打印为 `00 10 00 00`；第一条记录里 `attention_mask_offset` 恰为 `input_ids` 的字节数（4096 × 4 = 16384），说明偏移是逐段累积的。

**预期结果**：输出 3 条 dict、`字节数: 168 = 3 × 56`、以及裸解元组 `(0, 0, 4096, 0, 16384, 20480, 24576, 104882176)`。上述偏移数值由 4.3 节的布局公式算出，可与打印结果互相印证（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：一个 `samples.idx` 文件大小是 84,000 字节，缓存里有多少条样本？

**答案**：\(84\,000 / 56 = 1\,500\) 条。这正是校验逻辑 `index_size == num_samples * INDEX_RECORD_SIZE` 能工作的原因——记录定长，文件大小与样本数一一对应。

**练习 2**：为什么 `finalize_target_cache_index` 要断言本地索引「按本地 sample_id 有序」，而不是直接信任写入顺序？

**答案**：读取端 `_read_record` 依赖全局索引满足 `record.sample_id == index`（按位置直接寻址）。全局编号是把各 rank 的本地编号按区间顺序拼接得到的，只有每个 rank 的本地编号本身从 0 连续递增，拼接后才可能全局稠密有序。这条断言把「写入器实现出了偏差」这类问题在收尾阶段就拦下来，而不是等训练读数据时随机崩溃。

**练习 3**：如果分片上限从 64 GiB 降到 1 GiB，`samples.idx` 会变大吗？

**答案**：不会。索引条数只取决于样本数（每条固定 56 字节），与分片大小无关；变的是分片文件数量与 `manifest.json` 里 `shards` 列表的长度。

### 4.3 分片文件布局

#### 4.3.1 概念说明

分片文件是纯字节负载。**每条样本在分片内按固定顺序平铺五段**，段与段无缝衔接，同一条样本绝不跨分片：

```text
┌─────────────┬────────────────┬───────────┬──────────────────────────┬─────────────────────────────┐
│  input_ids  │ attention_mask │ loss_mask │   target_hidden_states   │ target_last_hidden_states   │
│  int32 × L  │    uint8 × L   │ uint8 × L │ bfloat16 × L × (K × H)   │      bfloat16 × L × H       │
└─────────────┴────────────────┴───────────┴──────────────────────────┴─────────────────────────────┘
  4L 字节         L 字节          L 字节        2·L·K·H 字节                  2·L·H 字节
```

其中 \(L\) = 该样本的 `seq_len`，\(K\) = `target_layer_ids` 的层数，\(H\) = `hidden_size`。于是单样本总字节数：

\[
B_{\text{sample}} = 4L + L + L + 2LKH + 2LH = 6L + 2LH(K+1)
\]

两个值得注意的细节：

1. **`attention_mask` 写了但读取端不读**。写入端把这 L 字节真实落盘并在索引中记录偏移（协议的组成部分）；但 `CacheDataset.__getitem__` 只读 `input_ids`、`loss_mask`、两个 hidden 张量——attention_mask 由 `CacheCollator` 根据 `input_ids` 的实际长度在组 batch 时重建（[target_cache_dataset.py:864-867](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L864-L867)）。由于缓存里的样本都是真实长度（写入前已裁掉填充），mask 信息本就等价于「长度」，这 L 字节属于协议完整性的开销。
2. **`target_hidden_states` 的最后一维是 \(K \times H\)**：生成时把 \(K\) 层输出沿最后一维 `torch.cat` 拼接（[scripts/data/prepare_target_cache.py:122-125](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L122-L125)），读取时也按 `(seq_len, K × H)` 一次读出，切层留给模型侧处理。层顺序 = manifest 中升序的 `target_layer_ids` 顺序。

**分裂（sharding）规则**：写入端维护当前分片已写字节 `current_shard_size`，当「已写不为零 且 再写这条样本会超过 `max_shard_bytes`」时关闭当前分片、开新分片（[target_cache_dataset.py:341-349](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L341-L349)）。注意判断先于写入，所以**任何分片实际大小都不超过上限**，且单个超大样本也完整落在同一个分片里。

#### 4.3.2 核心流程

单条样本的写入（`write_sample_bytes`）：

```text
计算 sample_nbytes = 五段字节数之和
_ensure_shard(sample_nbytes)            # 会溢出则换新分片
input_ids_offset        = current_shard_size; 写入 input_ids 字节
attention_mask_offset   = current_shard_size; 写入 attention_mask 字节
loss_mask_offset        = current_shard_size; 写入 loss_mask 字节
target_hidden_states_offset = current_shard_size; 写入 target_hidden_states 字节
target_last_hidden_states_offset = current_shard_size; 写入 target_last_hidden_states 字节
index_handle.write(pack_index_record(...))   # 同步追加一条 56 字节索引
```

字节序列化（`build_target_cache_sample_bytes` + 两个 `_tensor_to_*_bytes` 辅助函数）：

```text
int32 张量   → detach → cpu → dtype=int32 → contiguous → numpy → tobytes
uint8 张量   → 同上（dtype=uint8）
bfloat16 张量 → detach → cpu → dtype=bfloat16 → contiguous
             → view(torch.uint16) → numpy → tobytes   # 按原始比特位存储
```

读取侧的逆过程（详见 u2-l6，这里只看协议相关的一半）：`_read_bfloat16_tensor_from_shard` 从 mmap 以 `uint16` 读出再 `view(torch.bfloat16)` 还原（[target_cache_dataset.py:732-751](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L732-L751)），并断言 `offset + nbytes ≤ shard_size` 防越界。

**体积估算**（本讲实践的第二问）。设 \(N\) 个样本、每个样本序列长度均为 \(L\)：

\[
B_{\text{total}} = N \cdot \left( 6L + 2LH(K+1) \right) + 56N
\]

以任务给定参数代入 \(L=4096,\ H=2560,\ K=5,\ N=10^6\)：

| 分量 | 公式 | 字节数 | 占比 |
| --- | --- | --- | --- |
| input_ids | \(4L\) | 16,384 | 0.013% |
| attention_mask | \(L\) | 4,096 | 0.003% |
| loss_mask | \(L\) | 4,096 | 0.003% |
| target_hidden_states | \(2LKH\) | 104,857,600 | 83.3% |
| target_last_hidden_states | \(2LH\) | 20,971,520 | 16.6% |
| **单样本合计** | \(6L + 2LH(K+1)\) | **125,853,696**（≈120 MiB） | 100% |
| 索引 | \(56N\) | 56,000,000 | — |
| **总计** | | **≈ 1.2586 × 10¹⁴ 字节 ≈ 125.9 TB（十进制）≈ 114.5 TiB** | |

结论一目了然：**缓存体积被 hidden 张量完全支配**（99.96%），token 与 mask 几乎可以忽略；索引（56 MB）更是九牛一毛。

那为什么 README 说默认设置「只要」约 38 TB？因为 38 TB 对应的是**真实数据集**：perfectblend 训练集样本的实际序列长度参差不齐，多数远小于 `max_length=4096` 的截断上限，且部分样本因监督 token 不足被 `ConversationCollator` 过滤。我们的 125.9 TB 是「100 万条样本全部顶格 4096」的理论上界。这也解释了 README 给出的两条省钱建议的本质：**缩小数据集（\(N\) 线性）**与**减少 `model.target_layer_ids` 层数（\(K\) 线性，且只作用于占 83% 的 target_hidden_states 分量）**。

#### 4.3.3 源码精读

体积公式（协议的「计量标准」，读写两侧共用）：

- [deepspec/data/target_cache_dataset.py:42-54](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L42-L54)：`expected_target_cache_tensor_numel` 给出五个张量各自的元素数——两个 hidden 张量的元素数带上了 \(K\) 与 \(H\)。
- [deepspec/data/target_cache_dataset.py:57-74](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L57-L74)：`expected_target_cache_tensor_nbytes` 在元素数上乘 dtype 宽度：input_ids ×4（int32）、两个 mask ×1（uint8）、两个 hidden ×2（bfloat16）。读取端在 `__getitem__` 中现场调用它计算 `nbytes`（[target_cache_dataset.py:760-764](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L760-L764)），再交给 `_read_tensor_from_shard` 做越界断言——这就是「索引只存偏移不存长度」仍然安全的原因。

序列化：

- [deepspec/data/target_cache_dataset.py:221-228](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L221-L228)：`_tensor_to_bytes`（通用 dtype）与 `_tensor_to_bfloat16_bytes`（view uint16 技巧）。
- [deepspec/data/target_cache_dataset.py:283-302](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L283-L302)：`build_target_cache_sample_bytes` 把五个张量 + `seq_len = input_ids.shape[0]` 打包成不可变的 `TargetCacheSampleBytes`（[272-280 行](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L272-L280)）。

分片布局与分裂：

- [deepspec/data/target_cache_dataset.py:329-349](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L329-L349)：`_open_new_shard` 落地 `shard-local-XXXXX.bin` 命名规则（5 位数字编号）；`_ensure_shard` 实现「先判断后写入」的分裂条件，保证分片不超上限、样本不跨片。
- [deepspec/data/target_cache_dataset.py:351-387](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L351-L387)：`write_sample_bytes` 是五段布局的逐行实现——每段写入前先记下 `current_shard_size` 作为该段偏移，写完累加，最后同步写一条索引记录。这 36 行就是「分片布局」这个概念的物理定义。

真实长度的截取（写入什么进缓存）：

- [scripts/data/prepare_target_cache.py:312-327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L312-L327)：写入脚本用 `attention_mask.sum(dim=1)` 得到每个样本的真实长度 `seq_len`，五个张量全部切片 `[:seq_len]` 后才交给 writer——所以缓存里**没有填充字节**，变长样本按真实长度紧凑存储，这正是索引必须逐样本记录偏移的根源（若是定长布局，一个乘法就能定位）。

异步写入（工程细节，了解即可）：

- [deepspec/data/target_cache_dataset.py:410-433](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L410-L433)：`AsyncTargetCacheWriter` 在后台线程消费一个有界队列（容量默认 128），主线程的 GPU 前向与磁盘写入重叠；队列里只放 CPU 字节记录、绝不持有 CUDA 张量引用（注释原意），关闭时校验「入队条数 == 落盘条数」防止静默丢样本。

38 TB 警告与缩小建议（协议的「现实意义」）：

- [scripts/data/README.md:115-121](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L115-L121)：明确给出默认 Qwen3-4B 设置约 38 TB 的量级警告，并指出缓存体积随数据集规模、序列长度、目标隐藏维度缩放；存储紧张时用更小的训练集和/或减少 `model.target_layer_ids`。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 节的手算体积与代码公式互相验证，并感受 \(K\) 与 \(L\) 对体积的杠杆。

**操作步骤**（示例代码）：

```python
from deepspec.data.target_cache_dataset import (
    expected_target_cache_tensor_nbytes,
)

L, H, K, N = 4096, 2560, 5, 1_000_000
nbytes = expected_target_cache_tensor_nbytes(
    seq_len=L, hidden_size=H, num_target_layers=K,
)
for key, value in nbytes.items():
    print(f"{key:28s} {value:>15,} B")

per_sample = sum(nbytes.values())
total = N * per_sample + N * 56          # 别忘了索引也占磁盘
print(f"单样本合计    {per_sample:>15,} B (= {per_sample / 1024**2:.1f} MiB)")
print(f"总计          {total:>15,} B (= {total / 1e12:.1f} TB 十进制 / {total / 2**40:.1f} TiB)")

# 杠杆实验：把层数 K 从 5 降到 3，体积变成多少？
nb3 = expected_target_cache_tensor_nbytes(seq_len=L, hidden_size=H, num_target_layers=3)
print(f"K=5 -> K=3:   {N * sum(nb3.values()) / 1e12:.1f} TB")
```

**需要观察的现象**：`target_hidden_states` 一项约 104.86 MB，单项就超过其余四项之和的 4 倍；`K` 从 5 降到 3 后总量从约 125.9 TB 降到约 83.9 TB（hidden 主项从 \(2LKH\) 变为 \(2L\cdot 3H\)，即 \(104{,}857{,}600 \times 3/5 = 62{,}914{,}560\)，加上不变的其余分量）。

**预期结果**：各项字节数与 4.3.2 节表格逐项一致；`总计 ≈ 125,853,752,000,000 B ≈ 125.9 TB`。可与 README 的 38 TB 对照，体会「实际上界 vs 真实平均长度」的差距（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：同样想把缓存体积减半，「样本数减半」和「层数减半」效果一样吗？

**答案**：不一样。样本数减半：所有分量（含 input_ids、mask、last_hidden）都精确减半。层数 \(K\) 减半：只作用于占 83% 的 `target_hidden_states` 分量，总体缩减到 \(16.6\% + 83.3\%/2 \approx 58\%\)。以 4.3.2 的数字为例，\(N\) 减半得约 62.9 TB，\(K: 5 \to 2.5\) 得约 73 TB（实际 \(K\) 只能取整数）。

**练习 2**：为什么 `target_last_hidden_states` 要单独存一份？它不就是最后一层的目标隐藏状态吗，万一最后一层已在 `target_layer_ids` 里不就重复了？

**答案**：语义不同。`target_hidden_states` 是 `target_layer_ids` 指定的**中间层**拼接（由 forward hook 截取，见 u2-l5），而 `target_last_hidden_states` 是目标模型 `last_hidden_state`（最终层输出，[scripts/data/prepare_target_cache.py:121](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L121)），后者通常**不在** `target_layer_ids` 中（如默认配置取 `[1, 9, 17, 25, 33]`，而 Qwen3-4B 共 36 层）。协议上它有独立的 dtype 约定、独立的偏移字段，读取端单独读成 `(seq_len, H)` 张量（[target_cache_dataset.py:787-792](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L787-L792)）。

**练习 3**：一个样本的 `target_hidden_states` 是 104,857,600 字节，而 `max_shard_bytes` 设成了 100 MiB（104,857,600 字节），这个样本还能写进去吗？

**答案**：能。`_ensure_shard` 的分裂条件是 `current_shard_size > 0 且 current_shard_size + sample_nbytes > max_shard_bytes`（[target_cache_dataset.py:345-349](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L345-L349)）。空分片会无条件接受第一个样本，哪怕它超过上限——否则这个样本将永远无处可写。代价是该分片会超出 `max_shard_bytes`。

## 5. 综合实践

**任务**：不依赖任何 GPU 和真实模型，用协议自身的组件完成一次「写入 → 落盘 → 读取」的完整往返，验证你对三类文件的理解。

**步骤**（示例代码，保存为 `roundtrip.py` 在仓库根目录运行）：

```python
import os
import shutil
import torch

from deepspec.data.target_cache_dataset import (
    CacheDataset,
    INDEX_RECORD_SIZE,
    LocalTargetCacheWriter,
    build_target_cache_manifest,
    unpack_index_record,
    write_target_cache_manifest,
)

cache_dir = "toy_cache"
shutil.rmtree(cache_dir, ignore_errors=True)
os.makedirs(cache_dir)

K, H = 2, 8                       # 玩具维度：2 层目标、隐藏 8 维
writer = LocalTargetCacheWriter(rank_dir=cache_dir, max_shard_bytes=1024 * 1024)
seq_lens = [12, 5, 9]
for i, L in enumerate(seq_lens):
    writer.write_sample(
        sample_id=i,
        input_ids=torch.arange(L, dtype=torch.long) % 100,
        attention_mask=torch.ones(L, dtype=torch.long),
        loss_mask=torch.zeros(L, dtype=torch.long),
        target_hidden_states=torch.randn(L, K * H),
        target_last_hidden_states=torch.randn(L, H),
    )
writer.close()

# 1) 解码前 3 条索引记录（本讲核心实践）
with open(os.path.join(cache_dir, "samples.local.idx"), "rb") as f:
    buf = f.read()
assert len(buf) == 3 * INDEX_RECORD_SIZE
for i in range(3):
    print(unpack_index_record(buf, i * INDEX_RECORD_SIZE))

# 2) 把本地产物补成一份合法缓存：改索引名 + 写 manifest
os.replace(os.path.join(cache_dir, "samples.local.idx"),
           os.path.join(cache_dir, "samples.idx"))
write_target_cache_manifest(
    output_dir=cache_dir,
    manifest=build_target_cache_manifest(
        num_samples=3,
        shards=[{"shard_id": sid, "file_name": name}
                for sid, name in enumerate(writer.local_shard_files)],
        target_layer_ids=[3, 7],
        hidden_size=H,
    ),
)

# 3) 用训练同款读取端读回，逐张量比对
ds = CacheDataset(cache_dir)
for i, L in enumerate(seq_lens):
    item = ds[i]
    assert item["input_ids"].shape == (L,)
    assert item["target_hidden_states"].shape == (L, K * H)
    assert item["target_hidden_states"].dtype == torch.bfloat16
    print(f"样本 {i}: input_ids[:5] = {item['input_ids'][:5].tolist()}")
ds.close()
```

**观察要点**：

1. 第 1 步打印的 3 条记录中，`seq_len` 分别为 12、5、9；第二条记录的 `input_ids_offset` 应等于第一条样本的总字节数（\(6 \times 12 + 2 \times 12 \times 8 \times 3 = 648\) 字节，可用 4.3.2 公式验算）。
2. `write_sample` 传入的 `attention_mask`/`loss_mask` 是 long 型张量，但 `build_target_cache_sample_bytes` 会统一转成 uint8 存储；`input_ids` 转成 int32，读回后是 `torch.int32` 而非 long。
3. 两个 hidden 张量读回是 `bfloat16`（经 uint16 视图中转，数值有 bf16 舍入，与 `.to(torch.bfloat16)` 的原值逐比特相等）。
4. `writer.local_shard_files` 形如 `["shard-local-00000.bin"]`；本实践直接把它登记进 manifest 的 `shards`，说明校验只关心「shard_id 从 0 连续 + 文件存在」，文件名本身不是协议硬约束（全局改名的 `shard-XXXXX.bin` 只是生成脚本的约定）。

**预期结果**：脚本无断言失败、三行索引 dict 与三行样本摘要正常打印（待本地验证）。跑完后删除 `toy_cache/` 目录即可，未触碰任何源码。

## 6. 本讲小结

- target cache 目录由三类文件构成：`manifest.json` 是元数据合同（版本、dtype、层数、分片清单、溯源信息），`samples.idx` 是 56 字节定长索引，`shard-*.bin` 是无自描述信息的字节负载。
- 索引记录 `"<QIIQQQQQ"`：`sample_id`(8B) + `shard_id`(4B) + `seq_len`(4B) + 五个 8B 偏移；只存起始偏移不存长度，长度由 `seq_len` 经 `expected_target_cache_tensor_nbytes` 推导，读取时另作越界断言。
- 单样本字节布局固定为五段：int32 的 input_ids、uint8 的 attention_mask 与 loss_mask、bfloat16（按 uint16 比特位存储）的 target_hidden_states（\(L \times K \times H\)）与 target_last_hidden_states（\(L \times H\)），总量 \(6L + 2LH(K+1)\)。
- 多 rank 并行写入各自落 `shard-local-*` 与本地索引，收尾时按源数据区间顺序统一改名为全局分片、合并改写出稠密有序的全局 `samples.idx`，manifest 最后原子落盘（`.tmp` + `os.replace` + fsync）。
- 体积由 hidden 张量主导（约 99.96%）：100 万条顶格 4096 的样本约 125.9 TB，README 的 38 TB 对应真实平均长度更短的数据集；减层数只作用于占 83% 的主分量，减样本数则全线线性缩减。
- 校验是多层的：manifest 必填字段与协议常量、分片清单连续性、`samples.idx` 大小交叉一致性，训练前还有 `validate_train_cache` 比对层配置、隐藏维度与目标模型路径。

## 7. 下一步学习建议

本讲搞清楚了「缓存长什么样」。接下来的两讲补齐两侧：

- **u2-l5（prepare_target_cache）**：这些 bfloat16 字节是怎么从目标模型里抓出来的——`register_forward_hook` 截取指定 decoder 层、`-1` 层（embedding 输出）的特殊处理、多 rank 分片写入与收尾合并的完整工程流程。
- **u2-l6（训练侧读取）**：`CacheDataset` 如何用 mmap + 索引随机读取单条样本、`CacheCollator` 如何把变长样本组 batch（包括重建 attention_mask）、`CUDAPrefetcher` 如何用后台线程与独立 CUDA 流做预取。

建议先把本讲综合实践的往返脚本跑通再进入 u2-l5——当你亲手见过 `samples.idx` 里那 56 个字节，后面两讲的源码会顺畅很多。
