# u2-l2 checkpoint 文件加载：safetensors 解析与 TP 权重拼接

## 1. 本讲目标

上一讲（u2-l1）我们认识了数据的"长相"——`ParameterMeta`、`MemoryBuffer` 这些模型。本讲回答一个更上游的问题：**这些数据是从哪里来的？**

具体来说，学完本讲你应该能够：

1. 读懂 `_load_checkpoint_file`：它如何把磁盘上的一个 `.safetensors` 或 `.npy` 文件变成 `{参数名: (元信息, 张量)}` 字典，以及 `tp_rank` 是从哪里推出来的。
2. 读懂 `_concat_tp_weights`：理解张量并行（TP）分片如何拼接、`tp_concat_dim == -1` 代表的"共享权重"语义。
3. 读懂 `_load_checkpoint`：理解它如何跨文件汇总分片、修正元数据、做最终自检。
4. 会算 `_align_size`：理解 `_ALIGN_SIZE = 256` 的上取整公式，以及"对齐槽位"在两条不同代码路径上的两种含义。

本讲全部内容都在一个文件里：`checkpoint_engine/pin_memory.py` 的前 190 行。这部分代码**不依赖任何 GPU 和分布式环境**，你可以在自己的笔记本电脑上把每一个函数都跑一遍。

## 2. 前置知识

### 2.1 safetensors 文件格式

[safetensors](https://huggingface.co/docs/safetensors/en/index#format) 是 Hugging Face 推出的权重序列化格式，文件布局非常简单：

```
+--------------------+----------------------------------------------+
| 8 字节无符号整数 n  | n 字节的 JSON 文件头                           | 后面全部是张量数据
+--------------------+----------------------------------------------+
```

JSON 文件头里每个张量一条记录，例如：

```json
{
  "lm_head.weight": {"dtype": "F32", "shape": [4096, 4096], "data_offsets": [0, 67108864]},
  "__metadata__": {"format": "pt"}
}
```

`data_offsets` 是该张量数据在「数据区」内的起止字节偏移。这个 8 字节长度 + JSON 头 + 连续数据区的布局，本讲先由 `safetensors` 官方库代为解析；u2-l4 会看到项目如何**手工**解析这个文件头以实现原地锁页（inplace pin）。

### 2.2 .npy 文件格式

NumPy 的 `.npy` 格式 = magic 版本号 + 含 shape/dtype 的 header + 原始数据。它**一个文件只存一个数组**，所以项目自定义了一个约定：把多个 `.npy` 流式地首尾相接写进同一个文件，再配一个 `.npy.meta` 侧车文件（pickle 的元信息列表）说明每个数组的 key、dtype、shape、是否走 TP 拼接。这条路径已被标记废弃，但它仍然是本项目中**唯一能表达 TP 分片**的输入格式，是理解 `_concat_tp_weights` 的钥匙。

### 2.3 张量并行（TP）与"共享权重"

大模型推理时常用张量并行：把同一个权重矩阵沿某一维切开，分到多张卡上。于是磁盘上的 checkpoint 可能是"每个 TP rank 各存一份切片"。checkpoint-engine 的加载器（`_load_checkpoint`）会把各 rank 的切片**先拼回完整张量**，再统一摊平进缓冲区等待传输——`torch.cat` 就发生在这一步。

但有一类参数**不切**：例如 LayerNorm 的权重、词嵌入的重复备份。每个 TP rank 上的副本完全相同，这类就叫**共享权重**（shared tp weights），加载时取任意一份即可——项目用 `tp_concat_dim = -1` 这个哨兵值来标记。

### 2.4 字节对齐

把一堆不同大小的张量首尾相接摊平进一个 `uint8` 大缓冲区时，如果每个张量的起始偏移都是 256 的倍数，那么后续的按块拷贝、跨进程共享、显存传输都更友好。做法是：每个张量占一个"向上取整到 256 字节"的槽位（slot），槽位之间的空隙（最多 255 字节）被浪费掉。u2-l1 已经预告过公式，本讲 4.1 节展开。

### 2.5 numpy.memmap 与 mode="c"

`np.memmap` 把文件映射进虚拟内存，访问时按页惰性从磁盘读取。`mode="c"`（copy-on-write）表示写操作只改私有副本、不落盘。`_fast_np_load` 用它实现"零拷贝读 checkpoint"。

## 3. 本讲源码地图

本讲涉及的关键代码：

| 位置 | 作用 |
| --- | --- |
| [checkpoint_engine/pin_memory.py:22-27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L22-L27) | `_ALIGN_SIZE = 256` 常量与 `_align_size` 对齐函数 |
| [checkpoint_engine/pin_memory.py:30-113](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L30-L113) | `_load_checkpoint_file`：单文件加载，内含 safetensors / npy 两个加载器与格式分发 |
| [checkpoint_engine/pin_memory.py:116-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L116-L128) | `_concat_tp_weights`：TP 拼接与共享权重语义 |
| [checkpoint_engine/pin_memory.py:131-190](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L131-L190) | `_load_checkpoint`：跨文件汇总编排 + 元数据修正 + 自检 |
| [checkpoint_engine/data_types.py:12-17](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L12-L17) | `FileMeta`：加载器返回的"单张量元信息"结构（TypedDict，仅类型提示用） |
| [checkpoint_engine/pin_memory.py:277-302](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L277-L302) | 上游消费者 `_normal_pin_memory`：`_load_checkpoint` 的唯一调用点，之后切桶打包 |

上游调用链（承接 u1-l3 的代码地图，具体细节在 u3-l2 展开）：

```
ParameterServer.register_checkpoint (ps.py:305)
        └─> _register_checkpoint (pin_memory.py:365)
                ├─> _normal_pin_memory (pin_memory.py:277)  ──► _load_checkpoint  ◄── 本讲
                └─> _inplace_pin_memory (pin_memory.py:193)  ──► 手工解析 safetensors 头（u2-l4 讲）
```

注意一个关键分流：**`_load_checkpoint` 只服务于 normal pin 路径**。`inplace pin` 路径（针对 `/dev/shm/` 下的 safetensors）根本不调用它，而是自己解析文件头（见 4.1 节末尾的对比）。

## 4. 核心概念与源码讲解

### 4.1 `_align_size`：256 字节对齐的槽位计算

#### 4.1.1 概念说明

把张量摊平进 uint8 缓冲区时，每个张量不能"紧贴着"前一个放，而要占据一个 **256 字节对齐的槽位**。`_align_size` 就是槽位大小计算器：给定 dtype 和 shape，返回对齐后的字节数。它被 `_load_checkpoint`（构建 meta）、`_normal_pin_memory`（切桶、累加偏移）反复使用，是整个内存布局的基石。

#### 4.1.2 核心流程

\[ \text{aligned\_size} = \left\lceil \frac{\text{itemsize} \times \text{numel}}{256} \right\rceil \times 256 \]

即"原始字节数向上取整到 256 的倍数"。数学上等价于：

\[ \text{aligned\_size} = \text{raw} + ((256 - \text{raw} \bmod 256) \bmod 256) \]

每个张量的浪费量 \(= \text{aligned\_size} - \text{raw}\)，取值范围 \([0, 255]\)。对大模型里动辄几十 MB 的权重，浪费比例可以忽略；对海量小张量，最多各浪费 255 字节。

#### 4.1.3 源码精读

常量与函数定义（[checkpoint_engine/pin_memory.py:22-27](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L22-L27)）：

```python
# 256 bytes alignment when flatten torch tensors to uint8 buffer
_ALIGN_SIZE = 256


def _align_size(dtype: torch.dtype, shape: torch.Size) -> int:
    return (dtype.itemsize * shape.numel() + _ALIGN_SIZE - 1) // _ALIGN_SIZE * _ALIGN_SIZE
```

这行代码用整数运算实现上取整：`(raw + 255) // 256 * 256`。等价写法 `math.ceil(raw / 256) * 256` 需要浮点除法，大字节数下可能有精度隐患，整数写法既快又精确。

下游消费者印证了"槽位"的用途——`_normal_pin_memory` 切桶时按 `_align_size` 累加（[checkpoint_engine/pin_memory.py:294-302](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L294-L302)）：

```python
for name, tensor in sorted(parameters.items()):
    size = _align_size(tensor.dtype, tensor.shape)
    if buckets[-1].size + size > bucket_size:
        ...  # 当前桶装不下，开新桶
    buckets[-1].metas.append(
        ParameterMeta(name=name, shape=tensor.shape, dtype=tensor.dtype, aligned_size=size)
    )
    buckets[-1].size += size
```

以及往桶里逐个放张量时，偏移同样按 `aligned_size` 累加（[checkpoint_engine/pin_memory.py:350-359](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L350-L359)）：`offset += size`。对齐保证了**只要每个槽位是 256 的倍数，所有后续张量的起始偏移也自动是 256 的倍数**。

**一个容易忽略的细节**：在 inplace pin 路径里，`aligned_size` 的含义略有不同——它等于 safetensors 文件头里的 `end - start`（精确字节跨度），而不是上取整到 256（[checkpoint_engine/pin_memory.py:235-247](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L235-L247)，u2-l4 详述）：

```python
start, end = meta["data_offsets"]
# safetensors format ensures offsets are aligned
assert offset == start, ...
metas.append(
    ParameterMeta(..., aligned_size=end - start)
)
```

两条路径的共同不变式是：**`aligned_size` = 该张量在扁平 buffer 中占据的槽位大小，且各槽位必须无缝铺满 buffer**。normal 路径由我们自己造 buffer，所以用 256 对齐来铺；inplace 路径直接复用文件映射，所以沿用文件本身的对齐（safetensors 格式保证偏移对齐）。两条路径分别用 `assert offset == start / offset == buffer.nbytes`（[checkpoint_engine/pin_memory.py:252-255](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L252-L255)）和桶内偏移断言（[checkpoint_engine/pin_memory.py:354-357](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L354-L357)）守卫这个不变式。

#### 4.1.4 代码实践

**实践目标**：亲手验证对齐公式与浪费上限。

**操作步骤**（纯 CPU，示例代码）：

```python
import torch
from checkpoint_engine.pin_memory import _align_size, _ALIGN_SIZE

cases = [
    (torch.float32, (1,)),        # 4 字节
    (torch.float32, (3, 3)),      # 36 字节
    (torch.int8, (1000,)),        # 1000 字节
    (torch.bfloat16, (4096, 4096)),  # 33,554,432 字节
]
for dt, shape in cases:
    raw = dt.itemsize * torch.Size(shape).numel()
    aligned = _align_size(dt, torch.Size(shape))
    print(f"{str(dt):16s} {str(shape):14s} raw={raw:>11d} aligned={aligned:>11d} waste={aligned - raw}")
print("ALIGN_SIZE =", _ALIGN_SIZE)
```

**需要观察的现象**：每个 `aligned` 都是 256 的倍数；`waste` 全部落在 `[0, 255]` 区间。

**预期结果**（可手工核算）：

| dtype | shape | raw | aligned | waste |
| --- | --- | --- | --- | --- |
| float32 | (1,) | 4 | 256 | 252 |
| float32 | (3,3) | 36 | 256 | 220 |
| int8 | (1000,) | 1000 | 1024 | 24 |
| bfloat16 | (4096,4096) | 33,554,432 | 33,554,432 | 0 |

大权重（字节数本身是 256 的倍数）零浪费；小张量浪费一个对齐单位的尾部。这正是 4.4 节会看到的"参数按名字排序后装桶"场景：大量小 embedding 会有可预期的少量碎片。

#### 4.1.5 小练习与答案

**练习 1**：一个 `torch.float16`、shape 为 `[17, 31]` 的张量，`_align_size` 是多少？

答案：\(17 \times 31 \times 2 = 1054\) 字节；\(\lceil 1054 / 256 \rceil \times 256 = 5 \times 256 = 1280\)。

**练习 2**：为什么 `_normal_pin_memory` 里桶大小 `bucket_size` 取 `max(4 << 30, 最大单张量 aligned_size)`（[checkpoint_engine/pin_memory.py:286](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L286)），而不是固定 4GiB？

答案：桶必须能装下**至少一个完整张量**。若某个张量的对齐大小超过 4GiB（大模型里 FFN 权重很常见），固定 4GiB 会导致切桶循环里"开新桶→还是装不下→断言失败"。取 max 保证任何单张量都能放进一个桶，同时把桶下限设在 4GiB 避免桶过碎。

**练习 3**：如果把 `_ALIGN_SIZE` 改成 64，功能还正确吗？会有什么变化？

答案：功能仍然正确——所有代码只依赖"每个槽位对齐到 `_ALIGN_SIZE` 且偏移按 `aligned_size` 累加"这一约定，改成 64 依然无缝铺满。变化是碎片更少（浪费上限 63 字节）、但张量起始偏移只保证 64 字节对齐。反过来改成 1024 也能跑，只是浪费更多。这是一个纯粹的内部约定，不影响与 worker 侧的协议，因为 worker 拿到的是携带 `aligned_size` 的元数据清单。

---

### 4.2 `_load_checkpoint_file`：双格式加载与 tp_rank 的来源

#### 4.2.1 概念说明

`_load_checkpoint_file` 是"单文件加载器"：输入一个文件路径，输出二元组 `(tp_rank, {参数名: (FileMeta, 张量)})`。它解决三件事：

1. **格式分发**：按文件后缀选择 safetensors 还是（废弃的）npy 加载器，其他后缀直接报错。
2. **统一元信息格式**：不管哪种格式，都产出同一种 `FileMeta` dict（`key` / `dtype` / `shape` / `type` / `tp_concat_dim` 五个键，见 [checkpoint_engine/data_types.py:12-17](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L12-L17)）。
3. **推断 tp_rank**：npy 路径从**文件名**里解析出该文件属于哪个 TP rank；safetensors 路径恒为 0。

`FileMeta` 定义在 `TYPE_CHECKING` 块里（[checkpoint_engine/data_types.py:12-17](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L12-L17)），运行时并不真实存在，纯粹是给类型检查器和读者看的文档：

```python
class FileMeta(TypedDict):
    key: str  # parameter name
    dtype: torch.dtype
    shape: torch.Size
    type: type
    tp_concat_dim: int
```

#### 4.2.2 核心流程

```
_load_checkpoint_file(path)
    ├── path 以 .npy 结尾?
    │       ├── 打印废弃警告
    │       ├── 从文件名 model.{layer}.{tp}[.{ep}].npy 解析 tp_rank = 第 3 个点分字段
    │       └── _fast_np_load:
    │           ├── 读 path+".meta"（pickle 的 FileMeta 列表）
    │           ├── 顺序扫描 .npy 文件：parse_npy_header 逐个解析 npy 头
    │           ├── 为每个数组建 np.memmap(mode="c", offset=...)，零拷贝
    │           └── zip(meta 列表, tensor 列表) → 按 meta["type"] 转 torch.Tensor
    │               → tensor.view(dtype).view(shape) → {key: (meta, tensor)}
    ├── path 以 .safetensors 结尾?
    │       └── _safetensors_load:
    │           ├── safe_open(fn, framework="pt") 打开（mmap 零拷贝）
    │           └── 逐 key get_tensor，FileMeta 的 tp_concat_dim 恒填 -1
    └── 其他后缀 → ValueError
```

两个格式最大的差异是**元信息来源**：npy 格式的 shape/dtype 来自 `.meta` 侧车文件（可自定义 `tp_concat_dim`）；safetensors 的元信息来自文件头（张量本身自带 dtype/shape），但**没有地方存放 `tp_concat_dim`**，所以代码硬编码填 `-1`——这直接决定了"safetensors 无法表达 TP 分片"（4.3、4.4 节展开）。

#### 4.2.3 源码精读

safetensors 加载器（[checkpoint_engine/pin_memory.py:31-44](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L31-L44)）：

```python
def _safetensors_load(fn: str) -> dict[str, tuple["FileMeta", torch.Tensor]]:
    ret = {}
    with safe_open(fn, framework="pt") as f:
        for name in f.keys():  # noqa: SIM118
            weight = f.get_tensor(name)
            meta = {
                "key": name,
                "dtype": weight.dtype,
                "shape": weight.shape,
                "type": type(weight),
                "tp_concat_dim": -1,  # safetensors does not support tp_concat_dim
            }
            ret[name] = (meta, weight)
    return ret
```

`safe_open` + `get_tensor` 是 safetensors 官方的 mmap 读取方式，`weight` 直接映射文件内存，不做多余拷贝。注意 meta 里的 `dtype` / `shape` 直接取自张量对象，天然就是 `torch.dtype` / `torch.Size`——这正好满足下游 `_load_checkpoint` 的两个 `isinstance` 断言。

npy 加载器的头部解析（[checkpoint_engine/pin_memory.py:50-67](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L50-L67)）：

```python
def parse_npy_header(fin: BinaryIO) -> dict[str, Any]:
    start = fin.tell()
    major, minor = np.lib.format.read_magic(fin)
    if major == 1 and minor == 0:
        read_header_fn = np.lib.format.read_array_header_1_0
    elif major == 2 and minor == 0:
        read_header_fn = np.lib.format.read_array_header_2_0
    else:
        raise ValueError(f"unknown version {major}.{minor} ...")
    shape, is_fortran, dtype = read_header_fn(fin)
    return {"shape": shape, "is_fortran": is_fortran, "dtype": dtype,
            "header_length": fin.tell() - start}
```

它复用 `np.lib.format` 的公开函数逐个解析数组头，并记下 `header_length` 用于计算下一个数组的数据起点。

多张量扫描与 memmap（[checkpoint_engine/pin_memory.py:73-90](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L73-L90)）：

```python
tensors = []
offset = 0
with open(fn, "rb") as fin:
    fin.seek(0, os.SEEK_END)
    filesize = fin.tell()
    fin.seek(0)
    while fin.tell() < filesize:
        tensor_meta = parse_npy_header(fin)
        tensor = np.memmap(fn, dtype=tensor_meta["dtype"], mode="c",
                           offset=offset + tensor_meta["header_length"],
                           shape=tensor_meta["shape"])
        offset += tensor_meta["header_length"] + tensor.nbytes
        fin.seek(offset)
        tensors.append(tensor)
```

这里 `offset` 是"已消费字节数"（头 + 数据），`np.memmap` 的 `offset` 参数指向该数组**数据区**的绝对位置。整段循环实现了"一个文件里连续存放多个 npy 数组流"的自定义容器格式。

memmap 到 torch 张量的转换（[checkpoint_engine/pin_memory.py:92-99](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L92-L99)）：

```python
assert len(meta_lst) == len(tensors)
ret = {}
for meta, tensor in zip(meta_lst, tensors):
    if meta["type"] == torch.Tensor:
        tensor = torch.from_numpy(tensor)
    tensor = tensor.view(dtype=meta["dtype"]).view(*meta["shape"])
    ret[meta["key"]] = (meta, tensor)
return ret
```

`from_numpy` 让 torch 张量共享 memmap 内存；`view(dtype=...)` 把字节重解释成目标 dtype、`view(*shape)` 再变形。由于下游断言要求 `meta["dtype"]` 是 `torch.dtype`，实践中写 `.meta` 文件时应存 `type=torch.Tensor` 与 torch 的 dtype/shape。

格式分发与 tp_rank 推断（[checkpoint_engine/pin_memory.py:101-113](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L101-L113)）：

```python
tp_rank = 0
if file_path.endswith(".npy"):
    logger.warning("numpy model file is deprecated, will be removed in the future")
    filename_split = os.path.basename(file_path).split(".")
    # if using numpy and want to specify tp rank
    # file should be in model.{layer}.{tp}[.{ep}].npy format
    tp_rank = int(filename_split[2]) if len(filename_split) > 3 else 0
    ret = _fast_np_load(file_path)
elif file_path.endswith(".safetensors"):
    ret = _safetensors_load(file_path)
else:
    raise ValueError(f"unsupported file format: {file_path}")
return tp_rank, ret
```

注意 `model.layer.0.npy` 按点切分是 `["model", "layer", "0", "npy"]`，第 3 个字段（下标 2）才是 tp_rank；末尾多一段 expert 并行标记（如 `model.layer.0.e1.npy`）时 tp_rank 依旧取下标 2，ep 字段被忽略。而 `model.layer.npy` 只有 3 段，`len == 3` 不大于 3，tp_rank 落回 0。

#### 4.2.4 代码实践

**实践目标**：真实地加载一个 safetensors 文件，观察返回结构与 tp_rank。

**操作步骤**（纯 CPU，示例代码）：

```python
import torch
from safetensors.torch import save_file
from checkpoint_engine.pin_memory import _load_checkpoint_file

t = torch.arange(24, dtype=torch.float32).reshape(2, 3, 4)
save_file({"lm_head.weight": t}, "/tmp/demo.safetensors")

tp_rank, ret = _load_checkpoint_file("/tmp/demo.safetensors")
print("tp_rank =", tp_rank)
meta, weight = ret["lm_head.weight"]
print("meta =", meta)
print("equal =", torch.equal(weight, t))

# 再看非法后缀的行为
try:
    _load_checkpoint_file("/tmp/demo.bin")
except ValueError as e:
    print("ValueError:", e)
```

**需要观察的现象**：`tp_rank` 为 0；meta 是含 5 个键的 dict，其中 `tp_concat_dim` 为 `-1`、`dtype` 是 `torch.float32`、`shape` 是 `torch.Size([2, 3, 4])`；`weight` 与原张量逐元素相等。

**预期结果**：以上全部成立，且非法后缀抛出 `ValueError: unsupported file format: /tmp/demo.bin`。（本实践只依赖 CPU 上的 torch 与 safetensors，若你的环境尚未安装可先 `pip install safetensors torch`。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 safetensors 路径的 `tp_concat_dim` 必须硬编码 `-1`？

答案：safetensors 的文件头 schema 是固定的（`dtype` / `shape` / `data_offsets`，外加可选的 `__metadata__` 字符串字典），没有为每个张量存放自定义字段的规范位置。`tp_concat_dim` 是项目自定义概念，safetensors 表达不了，所以只能填哨兵值 `-1`，交由 npy 的 `.meta` 侧车文件来表达。

**练习 2**：`model.mlp.3.expert_1.npy` 会被解析出 tp_rank 是多少？

答案：`os.path.basename(...).split(".")` 得 `["model", "mlp", "3", "expert_1", "npy"]`，下标 2 是 `"3"`，所以 tp_rank = 3；`expert_1`（expert 并行标记）不参与解析。

**练习 3**：`_fast_np_load` 为什么用 `np.memmap(mode="c")` 而不是 `np.load`？

答案：`memmap` 把文件映射进虚拟内存、按页惰性加载，多个大权重文件不会一次性把整个 checkpoint 读进物理内存，也避免了为每个张量再分配一份内存（后面 `_concat_tp_weights` 的 `torch.cat` 才是真正物化完整权重的地方）。`mode="c"` 允许写但改动只留在私有副本、不写回 checkpoint 文件，保证磁盘文件只读。

---

### 4.3 `_concat_tp_weights`：TP 拼接与共享权重语义

#### 4.3.1 概念说明

`_concat_tp_weights` 是 TP 语义的集中体现。输入是**同一个参数在各个 tp_rank 上的分片列表**（按 rank 升序）、拼接维度 `tp_concat_dim`、推断出的 `tp_size`。输出是完整张量。规则只有三条：

1. `tp_concat_dim == -1` → 共享权重，直接取第 0 份，其余丢弃；
2. 只有一份 → 无需拼接，原样返回；
3. 否则 → `torch.cat(..., dim=tp_concat_dim)` 沿指定维度拼接。

#### 4.3.2 核心流程

```
_concat_tp_weights(tp_weights, tp_concat_dim, tp_size)
    ├── tp_concat_dim == -1 ?  ──► return tp_weights[0]          # 共享权重
    ├── assert tp_size == len(tp_weights)                        # 分片必须齐
    ├── len(tp_weights) == 1 ? ──► return tp_weights[0]          # 单份，无需 cat
    └── return torch.cat(tp_weights, dim=tp_concat_dim)          # 沿拼接维 cat
```

注意 `-1` 被用作"共享"哨兵，因此**真实的拼接维度必须写成非负下标**——想沿最后一维拼接就不能写 `-1`，得写 `len(shape) - 1`。这是哨兵值设计带来的一个小坑。

#### 4.3.3 源码精读

函数本体（[checkpoint_engine/pin_memory.py:116-128](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L116-L128)）：

```python
def _concat_tp_weights(
    tp_weights: list[torch.Tensor], tp_concat_dim: int, tp_size: int
) -> torch.Tensor:
    """Concat tp weights with meta info.
    If meta.concat_dim is -1, means this is shared tp weights, just use the first weights.
    Else we will cat weights in concat_dim.
    """
    if tp_concat_dim == -1:
        return tp_weights[0]
    assert tp_size == len(tp_weights)
    if len(tp_weights) == 1:
        return tp_weights[0]
    return torch.cat([w for w in tp_weights], dim=tp_concat_dim)
```

三个要点：

- **共享权重先判、断言后置**：`-1` 分支在 `assert tp_size == len(tp_weights)` 之前返回，所以共享权重不要求每个 rank 都有副本——哪个文件里有就用哪个（`_load_checkpoint` 传进来的列表按 rank 升序，`tp_weights[0]` 是最小 rank 的那份）。
- **断言防"缺片"**：`tp_size` 是按 `max(tp_rank + 1)` 推断的最大 rank 数（见 4.4 节）。如果只有 rank 1 的分片而缺 rank 0，`tp_size=2` 但 `len=1`，断言失败——宁可报错也不拼出错误的权重。
- **`torch.cat` 会物化新内存**：输入分片可能还映射在磁盘上（memmap），cat 之后得到一块连续的、完整的宿主内存张量。这正是后续锁页、H2D 传输需要的形态。函数上方的 TODO（[checkpoint_engine/pin_memory.py:178-180](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L178-L180)）也承认这一步是串行的、可能慢，但因为"未来不再用 tp 存储"而暂不优化。

#### 4.3.4 代码实践

**实践目标**：用三个最小用例验证三条规则。

**操作步骤**（纯 CPU，示例代码）：

```python
import torch
from checkpoint_engine.pin_memory import _concat_tp_weights

a = torch.ones(2, 4)
b = torch.zeros(2, 4)

# 用例 1：共享权重（concat_dim = -1），多份只取第 0 份
print(_concat_tp_weights([a, b], -1, 2).mean())        # 期望 tensor(1.)

# 用例 2：沿 dim=0 拼接
c = _concat_tp_weights([a, b], 0, 2)
print(c.shape, c.sum(0))                               # 期望 torch.Size([4, 4]) 且每列和为 2

# 用例 3：分片不齐（声明 tp_size=2 却只给 1 片）
try:
    _concat_tp_weights([a], 0, 2)
except AssertionError as e:
    print("AssertionError:", e)
```

**需要观察的现象**：用例 1 返回的张量内容是 `a`；用例 2 形状翻倍且上下两半分别是 1 和 0；用例 3 触发断言。

**预期结果**：分别输出 `tensor(1.)`、`torch.Size([4, 4])` 与每列 `2`，以及一条 `AssertionError`。（纯 CPU 可直接运行。）

#### 4.3.5 小练习与答案

**练习 1**：某参数在 4 个 TP rank 上各有一份 shape `[1024, 512]` 的分片，`tp_concat_dim=1`，拼接后 shape 是什么？

答案：`[1024, 2048]`——只有拼接维被放大 4 倍，其余维不变。

**练习 2**：如果 4 个分片 shape 不完全一致（例如 rank 0 是 `[1024, 512]`，rank 1 是 `[1024, 513]`），会发生什么？

答案：`_concat_tp_weights` 本身不检查各分片形状一致性，`torch.cat` 会在 dim=1 上因尺寸不匹配抛出 RuntimeError。此外 4.4 节会看到 `_load_checkpoint` 的最终自检（拼接结果 shape 必须等于"首个分片 shape × tp_size"）也会兜底拦截。

**练习 3**：为什么共享权重可以"只取第 0 份"，而不用担心各 rank 副本不一致？

答案：这是对 checkpoint 生产方的约定：标记为共享（`tp_concat_dim=-1`）意味着该参数在 TP 间不被切分、各副本语义上相同（例如 LayerNorm 权重或各 rank 冗余存的同一份 embedding）。如果生产方违反约定存了不同值，加载器不会发现——这是"信任元数据"的设计取舍，换取加载路径的简单。

---

### 4.4 `_load_checkpoint`：跨文件汇总、元数据修正与自检

#### 4.4.1 概念说明

`_load_checkpoint` 是本讲的"总装车间"：输入文件路径列表，输出 `{参数名: 完整张量}`。它做四件事：

1. **收集**：逐文件调用 `_load_checkpoint_file`，把每个参数的分片按 `tp_rank` 归位到 `parameters_with_tp[参数名][tp_rank]`；
2. **建元数据**：首次见到某参数时，用该分片的 dtype/shape 构建 `ParameterMeta`（aligned_size 用 4.1 的公式）；
3. **修正**：对需要 TP 拼接的参数，把 shape 的拼接维放大 `tp_size` 倍并**重算 aligned_size**；
4. **物化与自检**：按 rank 升序取出分片调用 `_concat_tp_weights`，最后断言"每个实际张量的 shape/dtype 与修正后的元数据一致"。

一个重要的结构事实：**`_load_checkpoint` 的返回值只有张量字典**（[checkpoint_engine/pin_memory.py:131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L131)）。它在内部精心构建的 `parameter_metas` 并不返回——主要用于最终自检；真正流入 `MemoryBuffer.metas` 的 `ParameterMeta` 是下游 `_normal_pin_memory` 装桶时**重新构建**的（[checkpoint_engine/pin_memory.py:299-301](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L299-L301)）。

#### 4.4.2 核心流程

```
_load_checkpoint(files)
    参数: parameters(结果) / parameter_metas / tp_metas / parameters_with_tp
    for file in files:
        tp_rank, ret = _load_checkpoint_file(file)
        for name, (meta, weight) in ret.items():
            parameters_with_tp[name][tp_rank] = weight          # 分片归位
            tp_metas[name] ??= TPMeta(concat_dim=meta["tp_concat_dim"], size=1)
            parameter_metas[name] ??= ParameterMeta(..., aligned_size=_align_size(首见分片))
            if concat_dim != -1:
                tp_metas[name].size = max(size, tp_rank + 1)    # 推断 tp_size
    for name, tp_meta in tp_metas.items():
        if concat_dim != -1:
            shape[concat_dim] *= tp_meta.size                   # 元数据修正
            parameter_metas[name] = ParameterMeta(..., aligned_size=_align_size(放大后 shape))
        weights = [parameters_with_tp[name][k] for k in sorted(...)]   # 按 rank 升序
        parameters[name] = _concat_tp_weights(weights, concat_dim, size)
    for name, parameter in parameters.items():                  # 自检
        assert parameter_metas[name].shape == parameter.shape
        assert parameter_metas[name].dtype == parameter.dtype
    return parameters
```

两个值得注意的细节：

- **aligned_size 按"拼接后的完整 shape"整体重算**，而不是各分片 aligned_size 之和。例如两个 96 字节的分片：分片各自对齐是 256 + 256 = 512，但拼成 192 字节的完整张量后对齐是 256。因为元数据描述的是"完整张量将来在 buffer 里占的槽位"，与分片如何对齐无关。
- **safetensors 场景下 `tp_size` 恒为 1**：`size` 只在 `concat_dim != -1` 时更新，而 safetensors 的 `tp_concat_dim` 恒为 `-1`，所以走到的永远是"共享/单份"分支——**用 safetensors 表达 TP 分片是不可能的**，同名参数只应出现在一个文件里。这也解释了 examples 里为什么按"参数"切分 checkpoint 文件（u1-l2、u6-l2）。

#### 4.4.3 源码精读

函数头部与就地定义的 `TPMeta`（[checkpoint_engine/pin_memory.py:131-139](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L131-L139)）：

```python
def _load_checkpoint(files: list[str]) -> dict[str, torch.Tensor]:
    class TPMeta(BaseModel):
        concat_dim: int
        size: int

    parameters: dict[str, torch.Tensor] = {}
    parameter_metas: dict[str, ParameterMeta] = {}
    tp_metas: dict[str, TPMeta] = {}
    parameters_with_tp: dict[str, dict[int, torch.Tensor]] = {}
```

`TPMeta` 只在这个函数里用，所以定义在函数体内（pydantic BaseModel，字段强校验）。`parameters_with_tp` 是二级字典：参数名 → tp_rank → 分片。

收集循环（[checkpoint_engine/pin_memory.py:140-166](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L140-L166)）：

```python
for file in files:
    tp_rank, ret = _load_checkpoint_file(file)
    for parameter_name, (meta, weight) in ret.items():
        if parameter_name not in parameters_with_tp:
            parameters_with_tp[parameter_name] = {}
        parameters_with_tp[parameter_name][tp_rank] = weight
        if parameter_name not in tp_metas:
            tp_metas[parameter_name] = TPMeta(concat_dim=meta["tp_concat_dim"], size=1)
        if parameter_name not in parameter_metas:
            assert isinstance(meta["dtype"], torch.dtype), ...
            assert isinstance(meta["shape"], torch.Size), ...
            parameter_metas[parameter_name] = ParameterMeta(
                name=parameter_name,
                shape=meta["shape"],
                dtype=meta["dtype"],
                aligned_size=_align_size(meta["dtype"], meta["shape"]),
            )
        tp_meta = tp_metas[parameter_name]
        if tp_meta.concat_dim != -1:
            tp_meta.size = max(tp_meta.size, tp_rank + 1)
```

注意两处"首见即定"：`tp_metas` 与 `parameter_metas` 都只在参数**第一次**出现时写入，之后不再更新。所以分片形状以最先出现的文件为准（这天然要求所有分片形状一致）。两个 `isinstance` 断言是 npy 路径的防线——`.meta` 是任意 pickle，必须确认存的是 torch 类型而不是 numpy 类型。

元数据修正与拼接（[checkpoint_engine/pin_memory.py:167-181](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L167-L181)）：

```python
for name, tp_meta in tp_metas.items():
    if tp_meta.concat_dim != -1:
        shape = list(parameter_metas[name].shape)
        shape[tp_meta.concat_dim] = shape[tp_meta.concat_dim] * tp_meta.size
        parameter_metas[name] = ParameterMeta(
            name=name,
            shape=torch.Size(shape),
            dtype=parameter_metas[name].dtype,
            aligned_size=_align_size(parameter_metas[name].dtype, torch.Size(shape)),
        )
    weights_in_cpu = [parameters_with_tp[name][key] for key in sorted(parameters_with_tp[name])]
    # TODO: here concat is serial, which may be slow
    # but since tp storage is not used in the future
    # we ignore this performance issue for now
    parameters[name] = _concat_tp_weights(weights_in_cpu, tp_meta.concat_dim, tp_meta.size)
```

`sorted(parameters_with_tp[name])` 对 tp_rank 升序排序，保证 cat 的顺序正确（rank 0 的分片在最前）。共享权重分支同样受益：`tp_weights[0]` 取的就是最小 rank。

最终自检（[checkpoint_engine/pin_memory.py:182-189](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L182-L189)）：

```python
for name, parameter in parameters.items():
    assert name in parameter_metas, f"parameter {name} not found in parameter_metas"
    assert parameter_metas[name].shape == parameter.shape, ...
    assert parameter_metas[name].dtype == parameter.dtype, ...
return parameters
```

这是"元数据推演"与"实际张量"的对账：如果 `.meta` 里声称 shape 是 `[2, 4]` 而实际分片是 `[2, 5]`，或者分片数量与文件名里的 tp_rank 推断不一致，都会在这里被拦下。

唯一调用点在 `_normal_pin_memory`（[checkpoint_engine/pin_memory.py:283-286](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L283-L286)）：

```python
parameters = _load_checkpoint(files)
if named_tensors:
    parameters.update(named_tensors)
bucket_size = max(4 << 30, max(_align_size(x.dtype, x.shape) for x in parameters.values()))
```

`named_tensors`（直接传入的张量，不经过文件）在这里**合并**进同一个字典，随后一起参与 4.1 节的装桶。也就是说 `_load_checkpoint` 之后，"来自文件的权重"和"来自内存的权重"就再无区别。

#### 4.4.4 代码实践

**实践目标**：观察 safetensors 多文件加载的两个行为——不同名参数合并、同名参数的覆盖。

**操作步骤**（纯 CPU，示例代码）：

```python
import torch
from safetensors.torch import save_file
from checkpoint_engine.pin_memory import _load_checkpoint

save_file({"a.weight": torch.ones(2, 2)}, "/tmp/f1.safetensors")
save_file({"b.weight": torch.full((3, 3), 7.0)}, "/tmp/f2.safetensors")

params = _load_checkpoint(["/tmp/f1.safetensors", "/tmp/f2.safetensors"])
print(sorted(params))              # 期望 ['a.weight', 'b.weight']
print(params["b.weight"].shape)    # 期望 torch.Size([3, 3])

# 同名参数出现在两个文件里：两份的 tp_rank 都是 0，后者覆盖前者
save_file({"a.weight": torch.full((2, 2), 9.0)}, "/tmp/f3.safetensors")
params2 = _load_checkpoint(["/tmp/f1.safetensors", "/tmp/f3.safetensors"])
print(params2["a.weight"])         # 期望全是 9（f3 覆盖了 f1）
```

**需要观察的现象**：第一组两个文件的参数被合并进同一个字典；第二组里同名参数只剩一个值。

**预期结果**：如注释所示，`params2["a.weight"]` 是全 9 的张量——因为两个文件的 `tp_rank` 都是 0，`parameters_with_tp["a.weight"][0]` 被后处理的文件覆盖，且 `tp_concat_dim=-1` 走共享分支取这一份。若两个文件里同名参数 **shape 不同**，则最终自检（[checkpoint_engine/pin_memory.py:184-186](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L184-L186)）会因"首见 meta 的 shape ≠ 实际张量 shape"抛 AssertionError。待本地验证：在你的环境跑一遍确认覆盖顺序与报错行为。

#### 4.4.5 小练习与答案

**练习 1**：`tp_meta.size` 为什么用 `max(size, tp_rank + 1)` 更新，而不是 `size += 1`？

答案：`size` 的语义是"TP 组里的 rank 总数"（最大 rank + 1），不是"见到的分片个数"。用 max 可以容忍文件乱序处理（先遇到 rank 1 再遇到 rank 0），并能让 4.3 节的 `assert tp_size == len(tp_weights)` 在**缺片**时暴露问题（声明了 2 个 rank 却只有 1 片）。

**练习 2**：一个参数在两个 npy 分片文件里，分片 shape 为 `[2, 3, 4]`、float32、`tp_concat_dim=0`。请给出修正后的 `ParameterMeta.aligned_size`。

答案：拼接后 shape 是 `[4, 3, 4]`，字节数 \(4 \times 3 \times 4 \times 4 = 192\)，对齐后 \(\lceil 192/256 \rceil \times 256 = 256\)。注意不是两个分片各自对齐值之和（\(256 + 256 = 512\)）——元数据描述的是完整张量的槽位。

**练习 3**：既然 `_load_checkpoint` 内部构建了 `parameter_metas`，为什么不返回给调用方复用，而让 `_normal_pin_memory` 重新构建一遍？

答案：两者用途不同。内部的 meta 用于**自检**（对账元数据推演与实际张量），而装桶时需要的是"按桶分组、按名字排序后的清单"，其 `aligned_size` 直接在装桶现场按同一公式重算（[checkpoint_engine/pin_memory.py:295-301](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L295-L301)），且 `named_tensors` 传入的张量根本没有经过 `_load_checkpoint`、没有对应 meta。让 meta 在真正需要的现场生成，避免了跨函数传递一份可能过期的中间状态。

## 5. 综合实践

**任务**：手工构造一份「TP 分片 + 共享权重」的 npy 格式 checkpoint，完整跑通 `_load_checkpoint`，验证拼接、共享、对齐三件事——这是全项目中**唯一**能亲手驱动 `_concat_tp_weights` 真实分支的方式（safetensors 的 concat_dim 恒为 -1）。

**操作步骤**（纯 CPU，示例代码）：

```python
# save as /tmp/tp_demo.py, run: python /tmp/tp_demo.py
import pickle

import numpy as np
import torch

from checkpoint_engine.pin_memory import _align_size, _load_checkpoint


def save_shard(path_base: str, tp_rank: int, key: str, shard: torch.Tensor, concat_dim: int):
    """写一个 model.<layer>.<tp>.npy 分片 + .meta 侧车文件（自定义容器格式）。"""
    fn = f"{path_base}.{tp_rank}.npy"
    with open(fn, "wb") as f:
        np.save(f, shard.numpy())          # 文件名第 3 段是 tp_rank，loader 靠它归位
    meta = {
        "key": key,
        "dtype": shard.dtype,              # 必须是 torch.dtype，下游有 isinstance 断言
        "shape": shard.shape,              # 必须是 torch.Size
        "type": torch.Tensor,
        "tp_concat_dim": concat_dim,       # -1 表示共享权重；非负表示沿该维拼接
    }
    with open(fn + ".meta", "wb") as f:
        pickle.dump([meta], f)             # 列表顺序必须与 .npy 中数组顺序一致
    return fn


# 1) TP 分片权重：concat_dim=0，rank0/rank1 各一片
shard0 = torch.arange(24, dtype=torch.float32).reshape(2, 3, 4)
shard1 = torch.arange(24, 48, dtype=torch.float32).reshape(2, 3, 4)
files = [
    save_shard("/tmp/model.layer", 0, "mlp.up_proj.weight", shard0, concat_dim=0),
    save_shard("/tmp/model.layer", 1, "mlp.up_proj.weight", shard1, concat_dim=0),
]

# 2) 共享权重：concat_dim=-1，两份内容应相同，loader 只取 rank 最小的一份
shared = torch.full((5,), 3.0, dtype=torch.float32)
files += [
    save_shard("/tmp/model.norm", 0, "norm.weight", shared, concat_dim=-1),
    save_shard("/tmp/model.norm", 1, "norm.weight", shared + 100, concat_dim=-1),
]

params = _load_checkpoint(files)
up = params["mlp.up_proj.weight"]
print("up_proj shape:", up.shape)                                   # ①
print("up_proj == cat:", torch.equal(up, torch.cat([shard0, shard1], dim=0)))  # ②
print("shared value:", params["norm.weight"])                       # ③
print("aligned(cat) =", _align_size(torch.float32, up.shape))       # ④
```

**需要观察的现象与预期结果**：

1. `up_proj` 的 shape 是 `torch.Size([4, 3, 4])`——拼接维 0 被放大 2 倍，其余维不变。
2. `up_proj == cat` 为 `True`——两个分片按 rank 升序无缝拼回。
3. `norm.weight` 是全 3.0（rank 0 那份），rank 1 的"+100"副本被丢弃——共享权重只取一份。
4. `aligned(cat) = 256`：完整张量 \(4 \times 3 \times 4 \times 4 = 192\) 字节向上取整到 256（而不是分片对齐值之和 512）。

**思考题**（选做）：把 `norm.weight` 两份分片写成**不同 shape**（如 `(5,)` 与 `(6,)`），再跑一遍。预期：加载仍然成功且取 `(5,)` 那份——因为共享分支不做形状一致性检查、最终自检对的是"首见 meta"。再把它改成 `concat_dim=0` 试试，预期 `torch.cat` 报尺寸不匹配错误。待本地验证。

## 6. 本讲小结

- `_align_size` 把任意张量的字节数向上取整到 256 的倍数，定义了它在扁平 uint8 buffer 中的"槽位"；normal pin 路径用 256 对齐，inplace pin 路径的 `aligned_size` 则是 safetensors 文件里的精确字节跨度——两条路径的共同不变式是"槽位无缝铺满 buffer"。
- `_load_checkpoint_file` 是单文件加载器：safetensors 走 `safe_open`（mmap 零拷贝、`tp_concat_dim` 恒 -1），废弃的 npy 走"多数组流 + `.meta` pickle 侧车"（memmap 零拷贝、`tp_rank` 从文件名第 3 段解析），产出统一的 `FileMeta` 五键 dict。
- `_concat_tp_weights` 三条规则：`concat_dim == -1` 取第 0 份（共享权重）、单份原样返回、否则沿指定维 `torch.cat`；`-1` 是哨兵值，所以真实拼接维必须写非负下标。
- `_load_checkpoint` 按 `参数名 → tp_rank → 分片` 归位所有分片，`tp_size` 用 `max(size, tp_rank+1)` 推断，对拼接参数把 shape 放大后**整体重算** aligned_size，最后用"元数据 vs 实际张量"的对账断言兜底；它只返回张量字典。
- safetensors 无法表达 TP 分片（concat_dim 恒 -1），同名参数只应出现在一个文件里，否则后处理的文件会静默覆盖先到的（形状不同则触发自检断言）。
- `_load_checkpoint` 只被 `_normal_pin_memory` 调用；`_inplace_pin_memory` 不经过它，而是手工解析 safetensors 文件头（下一讲的主角）。

## 7. 下一步学习建议

本讲结束时，权重已经是「内存里完整的、按名字索引的张量字典」。下一讲 **u2-l3《pinned memory：锁页内存与两种 pin 策略》** 将回答：为什么这些张量必须先搬进**锁页内存**才能高速 H2D 传输，`_normal_pin_memory` 如何把本讲的 `parameters` 字典按 `bucket_size` 切成 `MemoryBucket`、再用 32 线程的 ThreadPoolExecutor 并行"分配 buffer + 拷贝张量"。阅读前建议带着两个问题：

1. 装桶循环里 `buckets[-1].size + size > bucket_size` 的判断，与本讲的 `_align_size` 是如何配合保证"每个桶的实际字节数恰好等于其中所有槽位之和"的？
2. `torch.empty(size, dtype=torch.uint8, pin_memory=True)` 分配的 buffer 与本讲的"槽位"概念是什么关系？

如果本讲的综合实践你已经完成，可以顺手把 `/tmp/model.layer.1.npy` 删掉再跑一次 `_load_checkpoint`，观察 `assert tp_size == len(tp_weights)` 如何在**缺片**场景下拦住错误——这有助于理解为什么生产侧必须保证分片文件成套出现。
