# u2-l4 inplace pin 的底层实现与手动 unpin

## 1. 本讲目标

上一讲（u2-l3）我们从策略层面对比了 normal pin 与 inplace pin：前者「先分配锁页 buffer 再拷贝」，后者「对 `/dev/shm/` 里的 safetensors 文件直接 `cudaHostRegister` 原地锁页」。本讲下潜到实现层，读完本讲你应当能够：

1. 手工解析 safetensors 文件头：不借助 `safe_open`，只用 `torch.from_file` + 8 字节长度前缀 + JSON，还原出每个张量的名字、dtype、shape 与物理偏移。
2. 说清楚 `cudaHostRegister` / `cudaHostUnregister` / `cudaHostGetFlags` 三个 CUDA runtime API 在本项目中的调用方式（包括 `torch.cuda.cudart()` 与 `ctypes.CDLL(None)` 两条获取路径）。
3. 解释为什么 pin 成功后要立刻 `os.remove` 源文件——这是 inplace 路径内存复用动机的关键一步。
4. 读懂 `unregister_checkpoint` 中「先校验、再解页、最后才删池」的防御式顺序，以及 `manually_pinned` 标志为什么必须存在。

## 2. 前置知识

- **safetensors 文件格式**：一种「文件头 + 连续数据区」的布局。文件开头 8 字节是小端无符号整数 \( N \)，表示头部长度；随后 \( N \) 字节是一个 JSON 对象，键是张量名，值包含 `dtype`（如 `"F16"`）、`shape`、`data_offsets`（`[start, end)`，相对数据区起点的字节偏移）；JSON 之后剩下的全部字节就是张量数据。格式保证各张量数据在文件里连续排列、无空洞。
- **内存映射（mmap）与 `torch.from_file`**：把文件映射进本进程地址空间，得到一个指向文件内容的张量视图，不发生数据拷贝。`/dev/shm/` 是 Linux 的 tmpfs，本身就是内存盘，所以映射它得到的就是「已经在内存里的页」。
- **锁页（pin）与 `cudaHostRegister`**：`cudaHostRegister(ptr, size, flags)` 把一段**已经存在**的虚拟内存锁页，使它可作为异步 H2D / RDMA 传输的源。它与 normal 路径里 `torch.empty(..., pin_memory=True)` 的区别在于：后者由 PyTorch 的 caching host allocator 分配并托管；前者锁的是「别人给的」内存，PyTorch 完全不知情。
- **`manually_pinned` 标志**：`MemoryBuffer` 上的布尔字段（默认 `False`），标记这个 buffer 是否是手动 `cudaHostRegister` 出来的。它决定了注销时要不要走手动解页。
- **ctypes**：Python 标准库里调用 C 动态库的模块。`ctypes.CDLL(None)` 打开的是**当前进程自身**的全局符号表——凡是已被加载进进程的动态库符号（例如 torch 带来的 libcudart）都能按名字取到。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [checkpoint_engine/pin_memory.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py) | `_inplace_pin_memory` / 内嵌函数 `_parse_and_pin_from_safetensors` 与 `_pin`，以及 `_register_checkpoint` 里对 inplace 文件的筛选 |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | `register_checkpoint` 的文档警告与回滚路径、`unregister_checkpoint` 与内嵌函数 `_unpin` |
| [checkpoint_engine/device_utils.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py) | `supports_inplace_pin()`（仅 CUDA）与 `host_empty_cache()`（CUDA 走 `torch._C._host_emptyCache`） |
| [checkpoint_engine/data_types.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py) | `MemoryBuffer.manually_pinned` 字段定义 |
| [tests/test_inplace_unpin.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_inplace_unpin.py) | GPU 端到端测试：3 轮 register → gather → unregister 循环，验证手动解页不泄漏 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`_parse_and_pin_from_safetensors` 的文件头手工解析

#### 4.1.1 概念说明

normal 路径用 `safe_open` 读 checkpoint，得到的是「张量对象」，随后还要拷进自建的扁平 buffer。inplace 路径的目标恰恰是**不拷贝**——文件内容本身就是未来的锁页 buffer。因此它不能问 `safe_open` 要张量，而是要自己回答两个问题：

1. 数据区从文件的第几个字节开始？
2. 数据区里每个张量各占哪一段？

这两个问题的答案全在文件头里，手工解析即可，无需反序列化任何权重数据。这正是函数 docstring 所说的「load the safetensors file as bytes, then parse the header manually to get parameter metas」。

#### 4.1.2 核心流程

```text
输入: file_path (一个 /dev/shm/ 下的 .safetensors 文件)

1. size   = 文件大小
2. t      = torch.from_file(file_path, shared=True, size, uint8)   # 整个文件的 mmap 视图
3. header_len = 小端无符号整数(t[0:8])                             # 8 字节长度前缀
4. start_pos   = header_len + 8                                    # 数据区起点
5. header      = json.loads(t[8:start_pos])                        # 解析 JSON 头
6. header 去掉 "__metadata__"（用户自定义元信息，不是张量）
7. 按 data_offsets 升序遍历 header：
     断言 start == 当前累计 offset            # 连续无空洞
     生成 ParameterMeta(aligned_size = end - start)
8. buffer = t[start_pos:]                                          # 数据区视图，即待锁页对象
9. 断言 累计 offset == buffer.nbytes                                # 元数据完整覆盖数据区
10. os.remove(file_path)                                           # 见模块三
11. _pin(buffer)                                                   # 见模块二
12. 返回 MemoryBuffer(buffer, size=buffer.nbytes, metas, manually_pinned=True)
```

用公式表达两条不变量（inplace 路径的「布局即文件布局」）：

\[ \text{start\_pos} = \text{u64le}(t[0:8]) + 8 \]

\[ \text{slot}_i = \text{end}_i - \text{start}_i, \qquad \text{start}_1 = 0,\ \text{start}_{i+1} = \text{end}_i, \qquad \sum_i \text{slot}_i = \text{buffer.nbytes} \]

注意一个与 normal 路径的关键差异：这里 `ParameterMeta.aligned_size` 取的是 **`end - start`（文件里实际占用的字节数）**，而不是 normal 路径里按 `_ALIGN_SIZE=256` 向上取整的结果。因为数据区布局由 safetensors 写文件时就已确定（代码注释「safetensors format ensures offsets are aligned」），无需再人为对齐；只要元数据按物理顺序首尾相接，就能完整重建整个 buffer。

#### 4.1.3 源码精读

第一步，把整个文件 mmap 成 uint8 张量并定位数据区起点。8 字节长度前缀按小端无符号整数解码，加上自身 8 字节得到 `start_pos`：

[checkpoint_engine/pin_memory.py:216-228](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L216-L228)

上面这段代码做了三件事：`os.stat` 取文件大小并做 `t.nbytes > 8` 的最小合法性检查；`torch.from_file(file_path, True, size, dtype=torch.uint8)` 以共享模式把文件映射为一张量；从 `t[0:8]` 解出 header 长度、切出 `header_tensor` 再 `json.loads`。若 JSON 里带 `__metadata__`（safetensors 允许的用户元信息键），先 `pop` 掉，避免把它当成张量处理。

第二步，按 `data_offsets` 排序遍历，重建每个张量的元数据。排序键是 `data_offsets` 列表本身（字典序，等价于按 start 排序），配合 `assert offset == start` 强制「物理连续、无空洞、无重叠」：

[checkpoint_engine/pin_memory.py:232-250](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L232-L250)

其中 `_getdtype` 来自 `safetensors.torch`（见 [checkpoint_engine/pin_memory.py:11](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L11)），负责把头里的字符串 dtype（`"F16"`、`"BF16"`、`"I64"` 等）翻译成 `torch.dtype`。`aligned_size` 直接写 `end - start`。解析失败会 `logger.error` 后原样 `raise`，由上层的注册回滚逻辑兜底（见 4.4.3）。

第三步，切出数据区视图并做总账校验：

[checkpoint_engine/pin_memory.py:252-255](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L252-L255)

`buffer = t[start_pos:]` 就是未来要锁页、要被 H2D 读取的扁平 uint8 buffer；`assert offset == buffer.nbytes` 保证元数据序列恰好铺满数据区——这是后续 PS 侧按 `aligned_size` 累加偏移切张量的前提（呼应 u2-l1 讲过的「offset 由 aligned_size 逐个累加隐含」）。

外层调度在 [checkpoint_engine/pin_memory.py:271-274](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L271-L274)：`_inplace_pin_memory` 用 32 线程的 `ThreadPoolExecutor` 对多个文件并行执行 `_parse_and_pin_from_safetensors`，**每个文件产出一个 `MemoryBuffer`**——也就是说 inplace 路径的「桶粒度 = 文件粒度」，不像 normal 路径按 4GiB 贪心切桶。

最后回到入口处的筛选：哪些文件才有资格走 inplace？见 [checkpoint_engine/pin_memory.py:379-400](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L379-L400) —— 只有同时满足「以 `/dev/shm/` 开头」且「以 `.safetensors` 结尾」的文件进入 inplace 名单，其余文件连同 `named_tensors` 一起走 normal pin。PS 侧还有一道硬件闸门：[checkpoint_engine/ps.py:331-335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L331-L335) 在设备不支持时把 `use_inplace_pin_memory` 强制改回 `False`，能力判断在 [checkpoint_engine/device_utils.py:285-287](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L287)——**仅 CUDA 支持**（NPU/XPU 不走这条路）。

#### 4.1.4 代码实践：在纯 CPU 上复现文件头解析

**实践目标**：不依赖 GPU，验证自己能手工解析 safetensors 文件头并复现上述两条不变量。

**操作步骤**：

1. 新建 `parse_header_demo.py`（示例代码，仿照 `_parse_and_pin_from_safetensors` 的 L217-L255，**故意省略 `_pin` 与 `os.remove`**）：

```python
# 示例代码：CPU 上复现 inplace pin 的文件头解析（不含 cudaHostRegister）
import json, os, torch
from safetensors.torch import _getdtype
import safetensors.torch as st

path = "/tmp/demo.safetensors"
st.save_file(
    {"a": torch.zeros(3, 4, dtype=torch.float16),
     "b": torch.ones(10, dtype=torch.int64)},
    path,
)

size = os.stat(path).st_size
t = torch.from_file(path, True, size, dtype=torch.uint8)
flag_size = 8
start_pos = int.from_bytes(t[0:flag_size].numpy().tobytes(), "little", signed=False) + flag_size
header = json.loads(t[flag_size:start_pos].numpy().tobytes())
header.pop("__metadata__", None)

offset = 0
for name, meta in sorted(header.items(), key=lambda x: x[1]["data_offsets"]):
    start, end = meta["data_offsets"]
    assert offset == start, f"offset {offset} != start {start}"
    print(f"{name}: dtype={_getdtype(meta['dtype'])}, shape={meta['shape']}, slot={end - start}")
    offset = end

buffer = t[start_pos:]
assert offset == buffer.nbytes
print(f"file size={size}, data start={start_pos}, buffer.nbytes={buffer.nbytes}")
```

2. 运行 `python parse_header_demo.py`。

**需要观察的现象**：

- 打印出的两个张量的 slot 之和 + `start_pos` 恰好等于文件大小；
- 遍历顺序是按 `data_offsets` 的物理顺序，而不是按名字字典序（本例中可尝试把 `a` 改名为 `z` 验证顺序不变）；
- `assert offset == start` 全程通过。

**预期结果**：脚本无异常退出，最后一行输出形如 `file size=..., data start=..., buffer.nbytes=...` 且两者之和等于文件大小。`torch.from_file` 与 `json.loads` 均为纯 CPU 操作，本实践无需 GPU。文件头字节数很少（本例几十字节），说明「解析文件头」的开销远小于读一遍权重。

#### 4.1.5 小练习与答案

**练习 1**：为什么排序键是 `x[1]["data_offsets"]` 而不是张量名字？如果按名字排序会发生什么？

**答案**：`data_offsets` 决定张量在数据区中的物理位置，元数据必须按物理顺序生成，后续才能用「offset 逐个累加 slot」的方式从扁平 buffer 切回张量。若按名字排序，物理上不相邻的张量会被排在相邻位置，`assert offset == start` 会立刻失败（除非碰巧名字序与物理序一致）。

**练习 2**：inplace 路径的 `ParameterMeta.aligned_size` 与 normal 路径（u2-l2 的 `_align_size`）有何不同？

**答案**：normal 路径按 256 字节向上取整（`_ALIGN_SIZE=256`），因为 buffer 布局由我们自己排布，对齐可减少非对齐访问；inplace 路径的布局由 safetensors 写文件时决定，`aligned_size = end - start` 取实际字节数，靠 `assert offset == buffer.nbytes` 保证整体自洽。

**练习 3**：一个空的 safetensors 文件（没有任何张量）走这条路会发生什么？

**答案**：见 [checkpoint_engine/pin_memory.py:259-263](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L259-L263)：`metas` 为空时断言 `buffer.nbytes == 0`，打 warning 并返回 `manually_pinned=False` 的空 buffer——零字节内存没必要也不能调用 `cudaHostRegister`。

### 4.2 模块二：手动锁页 `_pin` 与三个 CUDA runtime API

#### 4.2.1 概念说明

解析完文件头，`buffer` 还只是一段普通映射内存，必须锁页才能作为异步 H2D / RDMA 的源。normal 路径靠 `torch.empty(..., pin_memory=True)` 一步到位；inplace 路径的内存是 mmap 来的，PyTorch 的 allocator 管不到它，于是直接调 CUDA runtime 的 `cudaHostRegister`。注销时同理：PyTorch 不会替我们解页，必须自己调 `cudaHostUnregister`。三个 API 的分工：

| API | 作用 | 本项目调用点 |
| --- | --- | --- |
| `cudaHostRegister(ptr, size, flags)` | 把已存在的内存区间锁页 | pin_memory.py `_pin`（经 `torch.cuda.cudart()`） |
| `cudaHostGetFlags(&flags, ptr)` | 查询某指针是否已锁页、以何种 flag 锁页 | ps.py `_unpin`（经 `ctypes.CDLL(None)`） |
| `cudaHostUnregister(ptr)` | 解除锁页 | ps.py `_unpin`（经 `torch.cuda.cudart()`） |

#### 4.2.2 核心流程

```text
_pin(t):
  1. torch.cuda.set_device(device_index)     # 锁页与当前设备上下文绑定
  2. cudart = torch.cuda.cudart()            # PyTorch 暴露的 cudart 绑定
  3. r = cudart.cudaHostRegister(t.data_ptr(), t.nbytes, 0)   # flags=0 即 cudaHostRegisterDefault
  4. r != 0 → cudaGetErrorString(r) → 抛 RuntimeError

_unpin(t):
  1. libc = ctypes.CDLL(None)                # 取当前进程的全局符号表
  2. 取 libc.cudaHostGetFlags，声明 argtypes/restype
  3. r = cudaHostGetFlags(&p_flags, t.data_ptr())             # 先校验
  4. 断言 r == 0 且 p_flags == 0x02
  5. r = cudart.cudaHostUnregister(t.data_ptr())              # 再解页
  6. r != 0 → 抛 RuntimeError
```

#### 4.2.3 源码精读

先看锁页侧。`_pin` 是 `_parse_and_pin_from_safetensors` 的内嵌函数，docstring 直接引用了 PyTorch 上游 issue（pytorch#32167，讨论的正是「如何对已有 tensor 原地 pin」）：

[checkpoint_engine/pin_memory.py:204-214](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L204-L214)

要点：① `torch.cuda.set_device(device_index)` 在前，`device_index` 是进入 `_inplace_pin_memory` 时记录的当前设备（[checkpoint_engine/pin_memory.py:193-194](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L193-L194)），因为多线程池里每个线程都需要正确的设备上下文；② `torch.cuda.cudart()` 返回 PyTorch 自带的 cudart ctypes 绑定，省去自己找 `libcudart.so` 的麻烦；③ 第三个参数传 `0`（`cudaHostRegisterDefault`）；④ 返回码非零时用 `cudaGetErrorString` 把错误码翻译成可读消息再抛出。

锁页发生在文件删除**之后**、返回 `MemoryBuffer` 之前：

[checkpoint_engine/pin_memory.py:265-269](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L265-L269)

成功后打日志（带 rank 与 MiB 大小），并以 `manually_pinned=True` 返回——这个标志一路带到 PS 的注销逻辑，是 4.4 的伏笔。

再看解页侧的 `cudaHostGetFlags` 调用方式。注意它**没有**用 `torch.cuda.cudart()`，而是 `ctypes.CDLL(None)`：

[checkpoint_engine/ps.py:418-427](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L418-L427)

`ctypes.CDLL(None)` 打开的是当前进程自身的全局符号表（代码注释原话「get all symbols from the current process」）。由于 torch 已把 libcudart 加载进进程，`libc.cudaHostGetFlags` 就能直接解析到该符号；显式声明 `argtypes`（`POINTER(c_uint), c_void_p`）与 `restype`（`c_int`）是为了保证 64 位指针按正确 ABI 传递。若符号不存在（`AttributeError`）则记 error 并 `raise`。

#### 4.2.4 代码实践：对照阅读两条 ctypes 路径

**实践目标**：辨析同一进程里获取 CUDA runtime 符号的两种方式，并理解 flags 的语义。

**操作步骤**：

1. 阅读 [checkpoint_engine/pin_memory.py:209-214](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L209-L214)（`cudart = torch.cuda.cudart()` 一路）与 [checkpoint_engine/ps.py:418-427](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L418-L427)（`CDLL(None)` 一路），记下各自声明了哪些参数类型。
2. （可选，需 GPU）在交互环境里执行下述示例代码，观察两种途径取到的是否为同一类对象：

```python
# 示例代码：仅在 CUDA 机器上可运行，待本地验证
import ctypes, torch
cudart = torch.cuda.cudart()
libc = ctypes.CDLL(None)
print(cudart.cudaHostRegister, libc.cudaHostRegister)   # 两个入口指向同一个 runtime
```

**需要观察的现象**：两个对象都能按名字取到 `cudaHostRegister`；`CDLL(None)` 取到的符号未自动带类型信息，所以源码必须手工声明 `argtypes/restype`，而 `torch.cuda.cudart()` 已内置声明。

**预期结果**：能说出「注册用 flags=0、校验时期望 0x02」这一事实并指出对应行号；GPU 上的实际 flags 返回值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`_pin` 里为什么要先 `torch.cuda.set_device(device_index)`？

**答案**：`_inplace_pin_memory` 用 32 线程并行处理多个文件，而 `cudaHostRegister` 作用于调用线程的当前设备上下文；先把设备设置回进入函数时记录的 `device_index`，避免工作线程继承了别的设备导致锁页挂到错误上下文。

**练习 2**：注册时 flags 传 `0`，而 `_unpin` 校验 `p_flags.value == 0x02`，这是否矛盾？

**答案**：这是源码里真实存在的对照关系（注册见 [pin_memory.py:211](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L211)，校验见 [ps.py:435-437](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L435-L437)）：`cudaHostGetFlags` 返回的是驱动记录的注册属性，代码把「读回 0x02（`cudaHostRegisterMapped`）」当作在支持平台上实测得到的固定不变量写死在断言里。本讲不展开驱动内部为何如此映射，读者可在 GPU 上实测确认（待本地验证）。断言的工程意义是明确的：`r == 0` 说明指针确已注册（对未注册指针调用会返回错误码），flag 相等则进一步锁定注册方式符合预期。

### 4.3 模块三：pin 成功后删除源文件——内存复用动机

#### 4.3.1 概念说明

`/dev/shm` 是 tmpfs：文件占的不是磁盘，而是**内存本身**，且通常配额只有物理内存的一半左右。训练框架把新 checkpoint 写进 `/dev/shm` 再通知 PS，是为了让 PS 侧零拷贝拿到权重。但如果文件一直留着：

- 配额被持续占用，训练侧下一轮写 checkpoint 可能因 `/dev/shm` 写满而失败；
- 任何「再复制一份」的动作（无论是 normal pin 的分配+拷贝，还是人工备份）都会让同一份权重在内存里出现两份。

所以 inplace 路径在数据区成功解析、锁页之前就把文件删掉（[pin_memory.py:258](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L258)）。Unix 语义保证了安全性：`unlink` 只删除目录项，只要还有进程持有该文件的映射/打开引用，数据页就不会被回收——而 `t`（及切片出的 `buffer`）正是这样的引用。于是「文件名」消失、「锁页的数据页」由 `MemoryBuffer.buffer` 继续持有，直到 `_unpin` + 引用释放后整块内存归还。

这也是 `register_checkpoint` 文档里那句严厉警告的由来（[checkpoint_engine/ps.py:316-317](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L316-L317)）：文件会被**删除**，若还需保留请先复制到磁盘。

#### 4.3.2 核心流程

```text
写文件(训练侧) ──► /dev/shm/xxx.safetensors（占 tmpfs 配额）
        │ mmap (torch.from_file)
        ▼
   t / buffer 视图（引用计数 +1，页不会被回收）
        │ os.remove(file_path)     ← 删除目录项，配额在引用归零前仍被占用
        │ cudaHostRegister(buffer) ← 数据页锁定，成为锁页 buffer
        ▼
   MemoryBuffer(manually_pinned=True)
        │ ... 使用期 ...
        │ cudaHostUnregister + 释放 buffer 引用
        ▼
   数据页彻底归还，tmpfs 配额恢复
```

#### 4.3.3 源码精读

删除动作夹在「总账校验」与「锁页」之间，注释把动机写得非常直白：

[checkpoint_engine/pin_memory.py:256-263](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L256-L263)

三点值得注意：① 注释假设 `/dev/shm` 里的文件都是临时文件，所以删了安全；② 即便 `metas` 为空（走 warning 分支），文件也已经被删了——删除在分支之前；③ `os.remove` 在 `_pin(buffer)` 之前执行也没问题，因为 `buffer` 持有映射引用，unlink 不会让数据失效。

配套的用户侧约定写在 PS 的公共接口文档里：

[checkpoint_engine/ps.py:314-329](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L314-L329)

`use_inplace_pin_memory` 默认 `True`；仅当文件位于 `/dev/shm/` 且是 `.safetensors` 时才真正生效；使用共享内存池时该选项被忽略（`inplace_pin=False`，见 [ps.py:354](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L354)）。

#### 4.3.4 代码实践：用 `df` 观察 tmpfs 页生命周期（纯 CPU）

**实践目标**：亲眼验证「文件删除后映射仍持有数据页；引用释放后配额才恢复」。

**操作步骤**：

1. 运行下述示例代码（注意先用 `df -h /dev/shm` 确认剩余空间大于 300MiB）：

```python
# 示例代码：纯 CPU 观察 tmpfs 页生命周期
import gc, os, torch

path = "/dev/shm/ce_demo.bin"
with open(path, "wb") as f:
    f.write(b"\0" * (256 << 20))          # 256MiB

print("step1 written:", os.popen("df -h /dev/shm | tail -1").read().strip())
t = torch.from_file(path, True, os.stat(path).st_size, dtype=torch.uint8)
os.remove(path)
print("step2 removed but mapped:", os.popen("df -h /dev/shm | tail -1").read().strip())
del t
gc.collect()
print("step3 mapping freed:", os.popen("df -h /dev/shm | tail -1").read().strip())
```

2. 对比三行 `df` 输出的 Avail 列。

**需要观察的现象**：step2 时配额**仍未释放**（映射活着，页仍被占用）；step3 之后配额恢复到 step1 之前的水平。

**预期结果**：验证了 4.3.2 流程图的页生命周期：删除文件 ≠ 释放内存；只有映射引用归零才真正归还。若 `/dev/shm` 不可写，此实践**待本地验证**（可改在权限允许的 tmpfs 挂载点重复）。

#### 4.3.5 小练习与答案

**练习 1**：既然 tmpfs 文件本身就在内存里，为什么代码注释还说删除能「avoid doubling the memory usage」？

**答案**：防止的是**后续的第二次驻留**：文件留着，任何复制动作（normal pin 的「分配锁页 buffer + 拷贝」、或运维手动备份）都会让权重在内存中出现两份；文件一旦删除，唯一驻留形态就是已锁页的 `buffer` 本身。同时删除还能立刻释放 tmpfs 的配额记账压力（在引用归零后完全恢复），避免训练侧下一轮写入失败。

**练习 2**：`os.remove` 放在 `_pin(buffer)` 之前，为什么是安全的？

**答案**：Unix 的 unlink 只移除目录项；`torch.from_file` 建立的映射让内核继续保留数据页。`buffer` 是 `t` 的切片视图，同样持有底层存储引用，因此先删文件再锁页不会导致数据消失。

### 4.4 模块四：`unregister_checkpoint` 与 `_unpin` 手动解页

#### 4.4.1 概念说明

手动 pin 的内存没有「管家」：PyTorch 的 caching host allocator 不知道它的存在，`torch._C._host_emptyCache()` 也回收不了它。如果注销时不显式 `cudaHostUnregister`，这些 tmpfs 页会**永远锁着**——每轮 register/unregister 都泄漏一块，长期运行的 RL 训练循环很快耗尽内存。这就是 `_unpin` 存在的理由，也是 `tests/test_inplace_unpin.py` 要连跑 3 轮的原因。

#### 4.4.2 核心流程

`unregister_checkpoint(name, force=False)` 的完整分支：

```text
1. name 不在 _memory_pool 且不是共享池当前使用者 ──► warning + return（幂等）
2. name 是共享池使用者 且 force=False ──► 只清空使用者标记，保留池（为复用）
3. p2p store 已初始化 ──► 从 p2p store 注销对应张量
4. name 是共享池使用者（且 force=True）──► 清标记 + 删池条目 + 重置为空列表
   （共享池走 normal pin，manually_pinned=False，不需要 _unpin）
5. 普通checkpoint：
     对池中每个 MemoryBuffer：
       若 manually_pinned ──► _unpin(buffer)
     _unpin 任何一步失败 ──► log + raise（且不删池条目）
     全部成功 ──► del _memory_pool[name]
6. device_manager.host_empty_cache()   # CUDA: torch._C._host_emptyCache；NPU/XPU: gc.collect
```

顺序设计的两个要点：**先解页、后删池**（注释「we won't delete the memory pool if unpinning fails」——解页失败时保留引用，便于排查与重试）；**解页前先用 `cudaHostGetFlags` 校验**（对未注册指针直接 Unregister 是未定义行为级别的错误）。

#### 4.4.3 源码精读

入口的三道前置分支（幂等性、共享池非 force 提前返回、p2p 注销）：

[checkpoint_engine/ps.py:389-406](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L389-L406)

共享池的 force 释放分支（池条目删除后重置为空列表，供下一轮首次使用判断，呼应 u2-l5 将详述的复用机制）：

[checkpoint_engine/ps.py:408-411](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L408-L411)

`_unpin` 的校验半段——flag 表注释直接摘自 CUDA 头文件 `driver_types.h`，随后断言读回值必须是 `0x02`：

[checkpoint_engine/ps.py:429-437](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L429-L437)

`_unpin` 的执行半段——`cudaHostUnregister` 只需指针一个参数，失败同样翻译错误码后抛 `RuntimeError`：

[checkpoint_engine/ps.py:438-444](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L438-L444)

驱动循环与「先解页后删池」的顺序保证：

[checkpoint_engine/ps.py:446-457](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L446-L457)

注意 `if memory_buffer.manually_pinned` 这个过滤：normal pin 出来的 buffer（`manually_pinned=False`，见 [data_types.py:96-100](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/data_types.py#L96-L100) 的默认值与 [pin_memory.py:304-307](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L304-L307) 的构造）不需要也不能手动解页——它们由 PyTorch allocator 托管，`del` 引用加 `host_empty_cache` 即可回收。

收尾的 host cache 清理：

[checkpoint_engine/ps.py:458-460](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L458-L460)

对应实现 [device_utils.py:307-312](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L307-L312)：CUDA 走 `torch._C._host_emptyCache()`（源码注释指出需要 torch>=2.5.0），NPU/XPU 退化为 `gc.collect()`。

还有一条容易被忽略的调用路径：**注册失败的回滚也会触发手动解页**。[checkpoint_engine/ps.py:371-378](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L371-L378) 的 `except` 分支在记日志后先从 p2p store 注销，再调用 `self.unregister_checkpoint(checkpoint_name)`。设想 32 线程并行 inplace pin 时第 20 个文件解析失败——前 19 个已锁页的 buffer 全靠这条回滚链路解页，否则一次失败的注册就泄漏一大块锁页内存。

测试侧的对应物是 [tests/test_inplace_unpin.py:34-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_inplace_unpin.py#L34-L48) 的 `run_pin_and_unpin`：每轮重新生成 `/dev/shm` 文件（[tests/test_inplace_unpin.py:15-31](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_inplace_unpin.py#L15-L31)，各 rank 写 `rank{R}_checkpoint.safetensors`），依次 `register_checkpoint → gather_metas → dist.barrier → unregister_checkpoint`，外层由 [tests/test_inplace_unpin.py:80-81](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_inplace_unpin.py#L80-L81) 以 `run_pin_and_unpin(3)` 跑满 3 轮，最后 rank 0 用 `shutil.rmtree` 清理测试目录。测试入口 [tests/test_inplace_unpin.py:51-77](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_inplace_unpin.py#L51-L77) 带 `@pytest.mark.gpu` 标记（需 ≥2 卡），通过 `torchrun` 子进程拉起整份文件自身运行——若手动解页有泄漏，3 轮后 GPU/锁页内存的累积会在 CI 上暴露。

#### 4.4.4 代码实践：跟踪 `manually_pinned` 的写读两端

**实践目标**：把「谁写标志、谁读标志、失败时谁兜底」三点连成一条链路。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "manually_pinned" checkpoint_engine/`。
2. 把命中点分成两类：**写端**（构造 `MemoryBuffer` 的两处）与**读端**（注销时的过滤条件），分别记下行号。
3. 再执行 `grep -n "unregister_checkpoint" checkpoint_engine/ps.py`，找出除公共 API 外的内部调用点（提示：注册失败的回滚分支）。
4. 通读 [tests/test_inplace_unpin.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_inplace_unpin.py)，回答：为什么循环 3 次而不是 1 次？为什么每个 rank 只注册自己写的那一个文件？

**需要观察的现象**：

- 写端恰有两处：inplace 空文件分支的 `False`（pin_memory.py:263）与正常 inplace 分支的 `True`（pin_memory.py:269）；normal 路径不显式传参，落到 `MemoryBuffer` 的默认值 `False`；
- 读端只有一处：`unregister_checkpoint` 的 `if memory_buffer.manually_pinned`（ps.py:449）；
- `unregister_checkpoint` 除被用户调用外，还在 `register_checkpoint` 的 `except` 分支被自调用（ps.py:377）。

**预期结果**：能画出「pin_memory 写标志 → memory_pool 存放 → unregister 读标志 → _unpin 解页 → host_empty_cache 收尾」的完整链路；对第 4 步两问能给出：多轮循环是为了暴露「不解页导致的累积泄漏」，单轮验证不了；每个 rank 注册自己的文件是为了模拟多 rank 各自持有一份 `/dev/shm` checkpoint 分片的真实场景（`get_files` 里各 rank 写 `rank{R}_checkpoint.safetensors`，并 `sleep` 规避文件系统时序）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `del self._memory_pool[checkpoint_name]` 挪到 `_unpin` 循环**之前**，会有什么问题？

**答案**：`del` 会丢掉 `MemoryBuffer` 列表的引用，若此后解页失败，`_unpin` 拿到的 buffer 引用虽还在（局部变量），但池条目已消失，既无法重试也无法排查——更糟的是 Python 侧引用一旦随异常栈销毁，锁页的指针值就再也找不回来，这块锁页内存将永久无法 `cudaHostUnregister`。所以源码坚持「先解页、后删池」，且失败时保留池条目（ps.py:456 的注释）。

**练习 2**：`_unpin` 里为什么要先调 `cudaHostGetFlags` 而不是直接 `cudaHostUnregister`？

**答案**：`cudaHostGetFlags` 是一次只读探测——返回码非零即可发现「该指针根本没被注册过」（比如 normal pin 的 buffer 误入此路径），避免对未注册内存执行 Unregister；同时对读回 flags 的断言（期望 0x02）进一步确认这段内存确以预期方式注册，是一种防御式编程。

**练习 3**：NPU 后端为什么整个走不到本讲的两条路径？

**答案**：入口闸门在 [ps.py:331-335](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L331-L335) 与 [device_utils.py:285-287](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/device_utils.py#L285-L287)：`supports_inplace_pin()` 仅对 `device_type == "cuda"` 为真，非 CUDA 会把 `use_inplace_pin_memory` 强制改为 `False` 并打 warning，于是所有文件都落回 normal pin，注销时也不会有任何 `manually_pinned=True` 的 buffer。

## 5. 综合实践

**任务：写一个「干跑版 inplace pin + 模拟 unpin」脚本，在纯 CPU 上走通 inplace 路径除 CUDA 调用外的全部逻辑。**

要求（示例代码，全部可 CPU 运行）：

1. 用 `safetensors.torch.save_file` 生成 3 个小文件到 `/dev/shm/`（如无可写 tmpfs 则退到 `/tmp`，并说明此时的行为差异）。
2. 实现 `dry_parse_and_pin(path)`：完整复刻 `_parse_and_pin_from_safetensors` 的 L217-L269——包括 8 字节前缀解析、`__metadata__` 剔除、按 `data_offsets` 排序、两条 `assert`（`offset == start` 与 `offset == buffer.nbytes`）、`os.remove`——唯独把 `_pin(buffer)` 替换为 `logger.info("skip cudaHostRegister in dry-run")`，并返回 `MemoryBuffer(buffer=buffer, size=buffer.nbytes, metas=metas, manually_pinned=False)`。
3. 用 `concurrent.futures.ThreadPoolExecutor(max_workers=32)` 并行处理 3 个文件，观察每个文件产出独立 `MemoryBuffer`（桶粒度 = 文件粒度）。
4. 实现 `dry_unregister(buffers)`：遍历 buffer，模拟 `if manually_pinned: _unpin(...)` 分支（干跑版本均为 False，故直接跳过），随后清空列表、调用 `gc.collect()`（对应 `host_empty_cache` 在非 CUDA 后端的退化行为）。
5. 在第 2 步与第 4 步之后各打印一次 `df -h /dev/shm`，对照 4.3.4 观察到的页生命周期。
6. 交叉验证：另用 `safetensors.torch.safe_open` 读取同样文件（删除前先留一份副本），比对两组 `(name, dtype, shape)` 是否一致、dry-run 的 slot 累计是否等于副本的文件大小减去其 header 区长度。

**预期结果**：脚本输出 3 个文件的元数据清单、`offset == buffer.nbytes` 全部通过、两次 `df` 显示「删除文件后配额未释放、引用清空后恢复」。若在 GPU 机器上，可把第 2 步替换为真实的 `_pin` 与 `_unpin`（从源码原样拷贝），即得到一个最小可用的 inplace pin/unpin 闭环——该部分**待本地验证**。

## 6. 本讲小结

- `_parse_and_pin_from_safetensors` 不读张量数据，只解析 safetensors 的「8 字节长度前缀 + JSON 头」，按 `data_offsets` 排序重建每个张量的 `ParameterMeta`，靠 `assert offset == start` 与 `assert offset == buffer.nbytes` 两条不变量保证「文件布局 = buffer 布局」。
- inplace 路径的 `aligned_size` 取 `end - start`（文件实际字节数），与 normal 路径按 256 字节向上取整不同；每个文件独占一个 `MemoryBuffer`，桶粒度即文件粒度。
- 锁页用 `torch.cuda.cudart().cudaHostRegister(ptr, nbytes, 0)`，注册前先 `set_device`；解页前先用 `ctypes.CDLL(None)` 取 `cudaHostGetFlags` 校验（断言 flags 为 0x02），再 `cudaHostUnregister`——先校验、先解页、后删池，任何一步失败都保留现场。
- pin 成功后立刻 `os.remove` 源文件：tmpfs 是内存盘，删文件释放目录项与配额记账，锁页数据页由映射引用继续持有，杜绝同一份权重的二次驻留。
- `manually_pinned` 是手动锁页的「出生证明」：只有它为 True 的 buffer 才需要在注销时手动解页；normal pin 的 buffer 由 PyTorch allocator 托管，交给 `del` + `host_empty_cache`（CUDA 用 `torch._C._host_emptyCache`，NPU/XPU 退化为 `gc.collect`）。
- 入口闸门保证仅 CUDA 走这条路：非 CUDA 设备或非 `/dev/shm/*.safetensors` 文件一律回落 normal pin；注册失败的 `except` 分支会复用 `unregister_checkpoint` 完成回滚解页，`tests/test_inplace_unpin.py` 用 3 轮 register/gather/unregister 循环验证不泄漏。

## 7. 下一步学习建议

下一讲（u2-l5 共享 pin memory 池）会继续沿着 `register_checkpoint` / `unregister_checkpoint` 这对生命周期接口展开，聚焦 `use_shared_memory_pool=True` 分支：池的形状为何「首次固定」、单一使用者约束如何由 `_current_shared_memory_pool_user` 维护、`force` 参数怎样触发池的真实释放（即本讲 4.4.2 流程图中的分支 4）。阅读建议：对照 [tests/test_reuse_pin_memory.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_reuse_pin_memory.py) 思考「为什么共享池模式必须 `inplace_pin=False`」（提示：本讲的 inplace 路径无法提供形状可控、可复用的 buffer）。完成 u2 全部讲义后，即可进入 u3-l1，从 `ParameterServer.__init__` 开始正式拆解 PS 主链路。
