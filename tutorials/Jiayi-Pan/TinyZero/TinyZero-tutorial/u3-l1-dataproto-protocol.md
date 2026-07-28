# DataProto 数据传输协议

## 1. 本讲目标

本讲要回答一个问题：**在 veRL 的训练里，数据是怎么在「驱动进程」和「一堆 Worker」之间流动的？**

读完本讲，你应该能够：

1. 说出 `DataProto` 的三段式结构（`batch` / `non_tensor_batch` / `meta_info`），以及它们各自能放什么类型的数据。
2. 解释为什么 `batch` 与 `non_tensor_batch` 的第一维（`batch_size`）必须一致，这个约束又是如何被 `check_consistency` 强制的。
3. 用一句话区分 `chunk`（切片分发）、`concat`（拼接汇聚）、`union`（横向并字段）、`repeat`（纵向复制样本）、`reorder`（原地重排）五个操作。
4. 看懂 `pad_dataproto_to_divisor` 为什么是 `chunk` 的「前置补丁」。
5. 在纸上手工追踪一遍 `chunk → repeat → concat` 后 `batch_size` 的变化，并能用真实测试断言验证。

本讲是 **数据协议与单控制器** 单元的第一篇。它把上一讲（u2-l3）产出的「一个 batch 的张量样本」封装成一个**可以在多进程间安全搬运、能被切块分发、能被拼接还原**的标准容器，为下一讲 u3-l2 的 Ray 调度机制铺好「搬运的货箱」。

## 2. 前置知识

- **强化学习训练循环的形状**（来自 u1-l4 / u4 预告）：每一轮训练，驱动进程要把一批 prompt 发给 Rollout 生成回答，再分别交给 Actor、Critic、Ref Policy 算各自的输出，最后把奖励、优势、回报这些「新算出来的字段」粘回原来的批次上。
- **左填充与 token 化**（来自 u2-l3）：`RLHFDataset` 经过 `collate_fn` 吐出一个字典，里面既有 `torch.Tensor`（`input_ids` / `attention_mask` / `position_ids`），也有不能放进张量的字段（`data_source` 字符串、`reward_model` 字典等，用 `dtype=object` 的 numpy 数组装）。
- **TensorDict**：PyTorch 官方的「装张量的字典」，它让你像操作一个张量那样同时操作一整个字典（例如 `td.chunk(...)` 会把字典里**所有**张量一起切）。本讲里 `DataProto.batch` 就是一个 `TensorDict`。
- **数据并行（DP）直觉**：多卡训练时，一批数据会被平均切成 N 份分给 N 个 rank，各算各的，再把结果拼回来。本讲的 `chunk`/`concat` 正是这个动作的「数据层」实现。

一个贯穿全讲的类比：把 `DataProto` 想成一张**电子表格**——

| 维度 | 类比 |
|------|------|
| 行（dim=0）= `batch_size` | 一条样本 = 一行 |
| 张量列（`batch`） | 数值单元格（能做矩阵运算） |
| 非张量列（`non_tensor_batch`） | 文本/字典单元格（如任务名、正确答案） |
| 表头注释（`meta_info`） | 整张表共享的元信息（如总 token 数、eos_token_id） |

`chunk` = 竖着把表切成几摞；`concat` = 把几摞表上下摞回来；`union` = 把两列**列数不同但行数相同**的表左右拼成宽表；`repeat` = 把每行复制若干份。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [verl/protocol.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py) | 定义 `DataProto`、`DataProtoItem`、`DataProtoFuture`，以及 `pad_dataproto_to_divisor`、`union_tensor_dict` 等工具函数。**本讲的全部核心都在这里。** |
| [verl/__init__.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py) | 把 `DataProto` 提升为包级导出，于是 `from verl import DataProto` 可用。 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `fit()` 主循环，是 `DataProto` 的「最大客户」，演示 `from_single_dict` / `repeat` / `union` / `pad_dataproto_to_divisor` 的真实用法。 |
| [verl/single_controller/base/decorator.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py) | Dispatch 装饰器，证明 `chunk`/`concat` 确实承担「分发到各 rank / 汇聚回驱动」的职责（为 u3-l3 预热）。 |
| [tests/utility/test_tensor_dict_utilities.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/utility/test_tensor_dict_utilities.py) | 单元测试，用最小样本事无巨细地断言了 `chunk`/`concat`/`repeat`/`pad` 的行为，是本讲实践的「标准答案」。 |

---

## 4. 核心概念与源码讲解

### 4.1 DataProto 的三段式结构

#### 4.1.1 概念说明

`DataProto` 是 verl 在任意两个函数、模块、进程之间交换数据的**统一协议**。它的设计目标是：让你把「一条样本需要的一组相关数据」当成一个整体来搬运、切分、拼接，而不用每次都手写「先切 input_ids、再按相同位置切 data_source、再把 attention_mask 也跟着切……」这种重复且容易错位的样板代码。

它由三部分组成：

1. **`batch: TensorDict`** —— 张量列。所有能进 GPU、能做矩阵运算的东西放这里（`input_ids`、`attention_mask`、`log_prob`、`values`、`advantages`…）。`TensorDict` 保证这些张量**永远共享同一个 `batch_size`（dim=0）**，对字典做的切片/拼接会同步作用到每一个张量上。
2. **`non_tensor_batch: Dict[str, np.ndarray]`** —— 非张量列。放不进张量的字段（任务名 `data_source`、正确答案 `reward_model`、流水线 id `uid`…）。约束是：必须是 `dtype=object` 的 numpy 数组，且第一维等于 `batch_size`，这样它才能和张量列「按行对齐」。
3. **`meta_info: Dict`** —— 元信息。整批数据共享的全局键值对（如 `global_token_num`、`eos_token_id`、`do_sample`），**没有 `batch_size` 这个维度**，切分时会被原样复制给每一块。

#### 4.1.2 核心流程

`DataProto` 是一个用 `@dataclass` 定义的容器，构造时自动调用 `check_consistency()` 做合法性校验：

```
构造 DataProto(batch, non_tensor_batch, meta_info)
        │
        ▼
__post_init__ → check_consistency()
        │   1) batch 若非空，batch_dims 必须为 1
        │   2) non_tensor_batch 里每个值必须是 dtype=object 的 np.ndarray
        │   3) 每个 ndarray 的 shape[0] 必须等于 batch.batch_size[0]
        ▼
   校验通过 → 一个可用的 DataProto
```

`len(data)` 返回的就是 `batch_size[0]`（张量列的第一维）；若没有张量列，则退化为取任一非张量列的 `shape[0]`。

#### 4.1.3 源码精读

类的定义与三段式字段，见 [verl/protocol.py:164-174](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L164-L174)——注意 `batch` 用 `TensorDict`，后两者是普通字典。

构造后立即做一致性校验，见 [verl/protocol.py:176-178](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L176-L178)。

`check_consistency` 是理解整个协议的关键，它把上面三条约束写成断言，见 [verl/protocol.py:242-263](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L242-L263)：

- 第 247 行：`len(self.batch.batch_size) == 1` —— 只支持 **1 维 batch**（num_batch_dims=1）。
- 第 261 行：非张量列必须是 `dtype=object` 的 ndarray。
- 第 262-263 行：非张量列的 `shape[0]` 必须等于张量列的 `batch_size`，这正是「按行对齐」的硬保证。

`__len__` 的实现见 [verl/protocol.py:180-187](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L180-L187)。`__getitem__` 取一行会返回一个 `DataProtoItem`（单条样本的容器），见 [verl/protocol.py:189-192](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L189-L192)，`_validate` 里 `test_batch[0].non_tensor_batch['reward_model']` 就是这么用的。

包级导出在 [verl/__init__.py:22](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/__init__.py#L22)（`from .protocol import DataProto`），所以测试与训练代码都能 `from verl import DataProto`。

#### 4.1.4 代码实践

**实践目标**：亲手构造一个三段式 `DataProto`，触发并理解一致性约束。

1. 在仓库根目录进入已按 u1-l2 装好的 conda 环境。
2. 运行下面这段「示例代码」（非项目原有代码）：

   ```python
   # 示例代码：演示 DataProto 三段式结构与一致性校验
   import torch, numpy as np
   from verl import DataProto

   batch_size, seq_len = 4, 6
   input_ids = torch.randint(0, 100, (batch_size, seq_len))
   attention_mask = torch.ones(batch_size, seq_len)
   data_source = np.array(['countdown'] * batch_size, dtype=object)   # 必须 dtype=object

   # 张量进 batch，非张量进 non_tensor_batch
   data = DataProto.from_dict(
       tensors={'input_ids': input_ids, 'attention_mask': attention_mask},
       non_tensors={'data_source': data_source},
       meta_info={'eos_token_id': 0},
   )
   print('len(data) =', len(data))                 # 期望 4
   print('batch keys =', list(data.batch.keys()))  # ['input_ids', 'attention_mask']
   print('non_tensor keys =', list(data.non_tensor_batch.keys()))  # ['data_source']
   print('meta_info =', data.meta_info)            # {'eos_token_id': 0}
   ```

3. **需要观察的现象**：`len(data)` 输出 4；`data_source` 若改成普通 Python list（不包成 `dtype=object` 的 ndarray），`from_dict` 内部会用 `np.array(val, dtype=object)` 自动转换；但若你直接构造 `DataProto` 且非张量列的 `shape[0]` 与 `batch_size` 不等（例如给了 3 个任务名），`check_consistency` 会抛 `AssertionError`。
4. **预期结果**：正常打印 `len(data) = 4` 与三组 keys；故意把 `data_source` 改成 3 个元素后应看到断言失败信息，提示长度不等于 batch size。
5. 运行环境若无 GPU 不影响本实践（全部在 CPU / 内存上）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `non_tensor_batch` 必须用 `dtype=object` 的 numpy 数组，而不能直接用 Python list？

**答案**：因为这些字段值类型不统一（既有字符串 `data_source`，又有字典 `reward_model`），必须用 `dtype=object` 的 ndarray 才能把这些异构对象塞进一个「第一维 = batch_size」的定长容器里，从而与张量列按行对齐；普通 list 无法保证形状语义，也无法被 `chunk`/`concat` 正确切分拼接。

**练习 2**：`meta_info` 和 `non_tensor_batch` 都是非张量字典，它们的本质区别是什么？

**答案**：`non_tensor_batch` 有 `batch_size` 维度（逐样本不同，与行对齐，会被切分）；`meta_info` 没有批次维度（整批共享，如 `eos_token_id`），切分时被原样复制给每一块，不会被拆。

---

### 4.2 构造与校验：from_single_dict / from_dict / check_consistency

#### 4.2.1 概念说明

`DataProto` 提供两个工厂方法把外部字典变成 `DataProto`，区别在于「张量和非张量是否已经分好类」：

- **`from_dict(tensors=..., non_tensors=...)`**：你已经把张量与非张量分开放进两个字典。它负责检查所有张量 dim=0 相同、把非张量包成 `dtype=object` 的 ndarray、构造 `TensorDict`。
- **`from_single_dict(data)`**：你只有一个**混合字典**（里面同时含 `torch.Tensor` 和 `np.ndarray`），它按类型自动分流：Tensor → `batch`，ndarray → `non_tensor_batch`。

后者正是上一讲 u2-l3 的 `collate_fn` 输出格式：`collate_fn` 把张量 `torch.stack`、非张量装进 `dtype=object` 的 ndarray，于是 `batch_dict` 是一个「张量 + ndarray」混合字典，直接喂给 `from_single_dict` 即可。这就是 u2-l3 → u3-l1 的接口衔接点。

#### 4.2.2 核心流程

```
from_single_dict(混合字典)
   │  遍历每个值，按 isinstance 分流
   ├── torch.Tensor  → tensors{}
   └── np.ndarray    → non_tensors{}
   └── 其它          → raise ValueError
   │
   ▼ 调用 from_dict(tensors, non_tensors, meta_info)
from_dict
   │  1) 取首个张量的 shape[:num_batch_dims] 作为 pivot batch_size
   │  2) 断言其余张量的前 num_batch_dims 维都与之相同
   │  3) 把 non_tensors 全部包成 np.array(..., dtype=object)
   │  4) TensorDict(source=tensors, batch_size=...)
   ▼
DataProto(...) → __post_init__ → check_consistency
```

#### 4.2.3 源码精读

`from_single_dict` 的自动分流见 [verl/protocol.py:265-278](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L265-L278)：遍历 `data.items()`，`torch.Tensor` 进 `tensors`，`np.ndarray` 进 `non_tensors`，否则报 `ValueError`，最后委托给 `from_dict`。

`from_dict` 的批大小检查与 `TensorDict` 构造见 [verl/protocol.py:280-314](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L280-L314)。关键是第 301-308 行：以第一个张量为「基准（pivot）」，逐个断言其它张量的前 `num_batch_dims` 维与之相等——这就是「所有张量列必须共享同一 batch_size」的实现。

真实调用现场：`fit()` 里把 DataLoader 吐出的 `batch_dict` 直接变成 `DataProto`，见 [verl/trainer/ppo/ray_trainer.py:581](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L581)；`_validate` 里同样用法见 [verl/trainer/ppo/ray_trainer.py:396](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L396)。

#### 4.2.4 代码实践

**实践目标**：用 `from_single_dict` 复刻训练循环里「从 DataLoader 到 DataProto」的那一步。

1. 运行下面这段「示例代码」：

   ```python
   # 示例代码：用 from_single_dict 接收混合字典
   import torch, numpy as np
   from verl import DataProto

   batch_dict = {
       'input_ids':      torch.randint(0, 100, (4, 6)),   # Tensor
       'attention_mask': torch.ones(4, 6),                # Tensor
       'position_ids':   torch.arange(6).expand(4, 6),    # Tensor
       'data_source':    np.array(['countdown']*4, dtype=object),  # ndarray
   }
   data = DataProto.from_single_dict(batch_dict)
   print('batch     :', list(data.batch.keys()))
   print('non_tensor:', list(data.non_tensor_batch.keys()))
   assert len(data) == 4
   ```

2. **需要观察的现象**：三个张量自动进 `batch`，`data_source` 自动进 `non_tensor_batch`，无需手动分类。
3. **预期结果**：`batch` 含 `input_ids/attention_mask/position_ids`，`non_tensor_batch` 含 `data_source`，`len(data)==4`。
4. 若往 `batch_dict` 里塞一个既非 Tensor 也非 ndarray 的值（如普通 int），会触发 `ValueError: Unsupported type`。

#### 4.2.5 小练习与答案

**练习 1**：`from_dict` 用「首个张量」作为 batch_size 基准，如果首个张量维度和别人不同会怎样？

**答案**：以首个张量的 `shape[:num_batch_dims]` 为 pivot，后续每个张量都会与之比较，发现不一致立即 `AssertionError`，提示哪两个 key 的 batch size 不同。所以「首个」只是基准，不享有特权，所有张量最终必须一致。

**练习 2**：为什么训练循环用 `from_single_dict` 而不是 `from_dict`？

**答案**：因为 `RLHFDataset.collate_fn` 的输出是张量与非张量混在一起的单一字典（模拟真实 DataLoader 的产物），`from_single_dict` 正好按类型自动分流，省去手动拆分两个字典的麻烦；`from_dict` 更适合你在测试里已经手工分好类的场景（如本仓库的单元测试 `DataProto.from_dict(tensors={'obs': obs}, non_tensors={'labels': labels})`）。

---

### 4.3 数据流转五件套：chunk / concat / union / repeat / reorder

#### 4.3.1 概念说明

这五个方法构成了 `DataProto` 在训练循环里「变形」的全部动作。用电子表格类比记住它们的几何含义：

| 方法 | 几何动作 | 对 batch_size 的影响 | 典型用途 |
|------|----------|----------------------|----------|
| `chunk(n)` | 竖着切成 n 摞 | 总量不变，每摞 = 总量/n | 把一批数据分发到 n 个 rank |
| `concat([...])` | 把多摞上下摞回 | 相加 | 把各 rank 的结果汇聚回驱动 |
| `union(other)` | 两表左右拼成宽表 | **不变**（要求相等） | 把新算出的字段（log_prob/values）粘到原批次 |
| `repeat(k)` | 每行复制 k 份 | ×k | 一个 prompt 采 k 条回答（GRPO 的 rollout.n） |
| `reorder(idx)` | 按索引原地重排行 | 不变 | 序列长度均衡（u7-l2）后恢复/打乱顺序 |

最容易混的三对：

- **`concat` vs `union`**：`concat` 是「行变多（纵向）」，把多个 `DataProto` 沿 dim=0 堆叠；`union` 是「列变多（横向）」，把两个**行数相同**的 `DataProto` 的字段合并。两者方向垂直。
- **`concat` vs `repeat`**：`concat` 拼的是**不同**样本；`repeat` 复制的是**相同**样本（每份是拷贝）。
- **`chunk` vs `select`/`pop`**：`chunk` 切的是**行**（dim=0）；`select`/`pop` 选/删的是**列**（字段）。`pop` 还会从原对象上把字段「摘走」（原地修改）。

#### 4.3.2 核心流程

**chunk（切分，dispatch 的数据层）**：

```
chunk(chunks=n)
  assert len(self) % n == 0          # 只支持等分
  batch_lst = self.batch.chunk(n, dim=0)        # TensorDict 一次性切所有张量
  for key,val in non_tensor_batch:
      np.array_split(val, n) → 分到各块         # 非张量列同步切
  meta_info 原样复制给每一块
  → 返回 List[DataProto]，长度 n
```

**concat（汇聚，collect 的数据层）**：

```
concat(data: List[DataProto])
  new_batch = torch.cat([d.batch for d in data], dim=0)
  non_tensor: 对每个 key 用 np.concatenate(..., axis=0)
  meta_info 取 data[0].meta_info            # 假设各块一致
  → 返回单个 DataProto
```

**union（横向并字段）**：要求两者 `batch_size` 相同；同名 key 必须相等（否则断言冲突），新 key 直接并入；`meta_info` 同理合并。

**repeat（复制样本）**：分两种模式（关键！）：

- `interleave=True`：`[a,b,c] → [a,a,b,b,c,c]`（张量用 `repeat_interleave`，numpy 用 `np.repeat`）。
- `interleave=False`：`[a,b,c] → [a,b,c,a,b,c]`（张量用 `expand+reshape`，numpy 用 `np.tile`）。

GRPO 里用 `interleave=True`：第 i 个 prompt 的 n 条回答在 rollout 输出里是连续排布的，于是把 prompt 批次也按 interleave 复制成 n 份，才能和回答「按行对齐」。

#### 4.3.3 源码精读

`chunk` 的实现见 [verl/protocol.py:482-512](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L482-L512)。注意第 491-492 行的「只支持等分」断言——这正是 `pad_dataproto_to_divisor` 存在的原因；第 495 行一行 `self.batch.chunk(...)` 就切了**所有**张量，这就是用 `TensorDict` 的红利。

`concat` 的实现见 [verl/protocol.py:514-537](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L514-L537)：张量列 `torch.cat(dim=0)`，非张量列用辅助函数 `list_of_dict_to_dict_of_list` 转置后 `np.concatenate(axis=0)`。

`repeat` 的两种模式见 [verl/protocol.py:547-589](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L547-L589)：第 561-563 行 `interleave=True` 用 `repeat_interleave`，第 566-568 行 `interleave=False` 用 `expand+reshape`；非张量列分别用 `np.repeat`（第 581 行）和 `np.tile`（第 583 行）保持一致语义。

`union` 的实现见 [verl/protocol.py:423-439](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L423-L439)，分别委托给 `union_tensor_dict`、`union_numpy_dict`、`union_two_dict`。

`reorder` 见 [verl/protocol.py:539-545](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L539-L545)——注意注释明确写出「**in-place**」，它会直接改写 `self.batch` 与 `self.non_tensor_batch`。

**真实调用现场**（最值得读的一段）：`fit()` 主循环把这几个方法串成一条流水线，见 [verl/trainer/ppo/ray_trainer.py:581-628](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L581-L628)：

- 第 581 行 `from_single_dict` 建表；
- 第 584 行 `pop` 把生成需要的字段摘出来；
- 第 594 行 `repeat(repeat_times=rollout.n, interleave=True)` 复制 prompt 对齐多次采样；
- 第 595 行 `union(gen_batch_output)` 把生成结果（responses、old_log_probs 等）横向粘回；
- 第 609/615/624 行连续 `union`，依次把 ref_log_prob、values、reward_tensor 这些「新列」粘上去。

**dispatch 侧的证据**：`chunk`/`concat` 确实是「分发到各 rank / 汇聚回驱动」的数据层实现。`_split_args_kwargs_data_proto` 对每个入参 `DataProto` 调用 `arg.chunk(chunks=chunks)`，见 [verl/single_controller/base/decorator.py:45-57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L45-L57)；汇聚时 `_concat_data_proto_or_future` 调用 `DataProto.concat(output)`，见 [verl/single_controller/base/decorator.py:129-144](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/single_controller/base/decorator.py#L129-L144)。这一层细节会在 u3-l3 展开。

#### 4.3.4 代码实践

**实践目标**：完整跑通本讲指定的核心任务——`chunk(2) → 各自 repeat(2) → concat`，并用 `batch_size` 验证你的理解。

1. 进入 conda 环境，在仓库根目录运行下面这段「示例代码」：

   ```python
   # 示例代码：追踪 chunk → repeat → concat 的 batch_size 变化
   import torch, numpy as np
   from verl import DataProto

   # 1) 构造含 input_ids 与 data_source 的 DataProto，batch_size=4
   data = DataProto.from_dict(
       tensors={'input_ids': torch.tensor([[1,1],[2,2],[3,3],[4,4]])},
       non_tensors={'data_source': np.array(['a','b','c','d'], dtype=object)},
   )
   print('原始      len =', len(data))                       # 4

   # 2) chunk(2)：切成两块，每块 batch_size=2
   parts = data.chunk(2)
   print('chunk(2)  各块 len =', [len(p) for p in parts])    # [2, 2]

   # 3) 对两块分别 repeat(2, interleave=True)：每块 batch_size=4
   rep = [p.repeat(repeat_times=2, interleave=True) for p in parts]
   print('repeat(2) 各块 len =', [len(p) for p in rep])      # [4, 4]

   # 4) concat 拼回：总 batch_size=8
   merged = DataProto.concat(rep)
   print('concat    len =', len(merged))                     # 8
   print('input_ids =\n', merged.batch['input_ids'])
   print('data_source =', list(merged.non_tensor_batch['data_source']))
   ```

2. **需要观察的现象**：
   - 每步 `len` 如注释所示：`4 → [2,2] → [4,4] → 8`。
   - `interleave=True` 下，第一块 repeat 后是 `[a,a,b,b]` 的 data_source，第二块是 `[c,c,d,d]`，concat 后整体为 `[a,a,b,b,c,c,d,d]`。
3. **预期结果**：`merged` 的 `batch_size=8`，`input_ids` 与 `data_source` 按行对齐、顺序如上。
4. 把 `interleave=True` 改成 `False`，再观察 `data_source` 变成 `[a,b,a,b,c,d,c,d]`，体会两种复制方式的差异。
5. 对照真实测试断言：`test_chunk_concat` 见 [tests/utility/test_tensor_dict_utilities.py:106-127](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/utility/test_tensor_dict_utilities.py#L106-L127)，`test_repeat` 见 [tests/utility/test_tensor_dict_utilities.py:143-165](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/utility/test_tensor_dict_utilities.py#L143-L165)——你的手算结果应与这些断言一致。

> 运行结果待本地验证（取决于你环境中 `tensordict`/`torch` 版本是否符合 u1-l2 的约束 `tensordict<0.6`）。

#### 4.3.5 小练习与答案

**练习 1**：`chunk(5)` 作用于一个 `batch_size=6` 的 `DataProto` 会怎样？

**答案**：`chunk` 第 491-492 行断言 `len(self) % chunks == 0`，`6 % 5 != 0`，直接抛 `AssertionError`。这就是为什么不能整除时要先用 `pad_dataproto_to_divisor` 补齐——下一节就讲。

**练习 2**：`union` 时两个 `DataProto` 都含有同名的 `input_ids` 但数值不同，会发生什么？

**答案**：`union_tensor_dict` 在第 73-75 行断言同名 key 必须 `.equal()`（相等），否则抛 `AssertionError: ... are not the same object`。`union` 的语义是「并字段」，重名字段必须完全一致，不允许冲突。这正是 `fit()` 里每次 `union` 的都是**新名字**字段（`old_log_probs`、`values`、`token_level_scores`…）的原因。

**练习 3**：为什么 GRPO 场景下 `repeat` 必须用 `interleave=True`？

**答案**：Rollout 对每个 prompt 连续生成 n 条回答（输出排列为 `[p0_r0, p0_r1, …, p1_r0, p1_r1, …]`），要让 prompt 与其 n 条回答按行对齐，prompt 批次也必须按 interleave 复制成 `[p0,p0,…,p1,p1,…]`。若用 `interleave=False` 得到 `[p0,p1,…,p0,p1,…]`，会导致 prompt 与回答错位，奖励与优势算到错误样本上。

---

### 4.4 对齐整除与并字段工具：pad_dataproto_to_divisor 与 union_tensor_dict

#### 4.4.1 概念说明

这两个是模块级的工具函数（不是 `DataProto` 的方法），但在训练循环里不可或缺：

- **`pad_dataproto_to_divisor(data, size_divisor)`**：把 `data` 的 `batch_size` 补到能被 `size_divisor` 整除。补的方式很朴素——从**自身开头**取若干行复制到末尾（`DataProto.concat([data, data[:pad_size]])`）。返回补齐后的数据和补了多少行（`pad_size`）。配套的 `unpad_dataproto` 在处理完后把这几行截掉。

  为什么需要它？因为 `chunk(chunks=world_size)` 要求等分，而验证集的样本数未必是 GPU 数的整数倍。

- **`union_tensor_dict(td1, td2)`**：把两个**同 batch_size** 的 `TensorDict` 合并成一个宽表，重名 key 必须相等，新 key 直接并入。它是 `DataProto.union` 在张量列上的底层实现。

#### 4.4.2 核心流程

`pad_dataproto_to_divisor` 的补齐公式：

\[
\text{pad\_size} = \text{size\_divisor} - (\text{len(data)} \bmod \text{size\_divisor})
\]

当 `len(data) % size_divisor == 0` 时 `pad_size = 0`，原样返回。补齐用「自身头部拷贝」是为了保证补出来的行是合法样本（形状、字段都对），不引入随机噪声；处理完用 `unpad_dataproto` 按 `pad_size` 截尾还原。

#### 4.4.3 源码精读

`pad_dataproto_to_divisor` 见 [verl/protocol.py:40-57](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L40-L57)：第 52 行算 `pad_size`，第 53 行 `DataProto.concat([data, data[:pad_size]])` 用自身前 `pad_size` 行补齐；`unpad_dataproto` 见 [verl/protocol.py:60-63](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L60-L63)，`pad_size==0` 时直接返回原数据。

`union_tensor_dict` 见 [verl/protocol.py:66-77](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L66-L77)：先断言两者 `batch_size` 相同（第 68-69 行），再遍历 `td2` 的 key——不存在则并入，存在则断言 `.equal()`。姐妹函数 `union_numpy_dict`（非张量版）见 [verl/protocol.py:80-89](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L80-L89)。

**真实调用现场**：`_validate` 在生成前把测试批次补齐到 `world_size` 的整数倍，见 [verl/trainer/ppo/ray_trainer.py:413](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L413)：

```python
test_gen_batch_padded, pad_size = pad_dataproto_to_divisor(test_gen_batch, self.actor_rollout_wg.world_size)
```

这样随后 `generate_sequences` 内部的 `chunk(world_size)` 才不会因为不等分而断言失败；处理完用 `pad_size` 截掉补丁行。

#### 4.4.4 代码实践

**实践目标**：复刻「补齐 → 切分 → 还原」的完整闭环。

1. 运行下面这段「示例代码」：

   ```python
   # 示例代码：pad → chunk → unpad
   import torch, numpy as np
   from verl import DataProto
   from verl.protocol import pad_dataproto_to_divisor, unpad_dataproto

   # batch_size=3，想被 world_size=2 整除 → 需要补 1 行
   data = DataProto.from_dict(
       tensors={'input_ids': torch.tensor([[1],[2],[3]])},
       non_tensors={'data_source': np.array(['a','b','c'], dtype=object)},
   )
   padded, pad_size = pad_dataproto_to_divisor(data, size_divisor=2)
   print('pad_size =', pad_size, ' padded len =', len(padded))   # 1, 4

   parts = padded.chunk(2)                                       # 现在可以等分了
   print('各块 len =', [len(p) for p in parts])                  # [2, 2]

   merged = DataProto.concat(parts)
   restored = unpad_dataproto(merged, pad_size)                  # 截掉补丁
   print('restored len =', len(restored))                        # 3
   ```

2. **需要观察的现象**：`pad_size=1`，补齐后 `batch_size=4`，能被 2 等分；截尾后回到 3。
3. **预期结果**：与上述注释一致。
4. 对照真实测试 `test_dataproto_pad_unpad`，见 [tests/utility/test_tensor_dict_utilities.py:168-203](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/tests/utility/test_tensor_dict_utilities.py#L168-L203)——它断言了 `size_divisor=2`（补 1 行）与 `size_divisor=3`（pad_size=0，不补）两种情况，可作为标准答案。

> 运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：补齐用的「补丁行」来自哪里？为什么不用随机数据补？

**答案**：来自数据自身的头部（`data[:pad_size]`）。用真实样本补是为了保证补丁行的形状、字段都合法，能让 `chunk`/`generate_sequences` 正常工作；用随机数据补可能违反模型输入约束（如 attention_mask 全 1、position_ids 连续），且这些补丁行最终会被 `unpad` 丢弃，不影响真实指标。

**练习 2**：`union_tensor_dict` 为什么对重名 key 要求 `.equal()` 而不是「后者覆盖前者」？

**答案**：因为 `union` 的语义是「合并两份描述**同一批样本**的字段」，如果同一字段在两边都有且数值不同，说明数据流转中出现了不一致（可能是 bug），应当尽早暴露而非默默覆盖。`fit()` 里每次 `union` 都刻意使用**互不重叠的新字段名**来规避这个断言。

---

## 5. 综合实践

把本讲全部知识串起来：**模拟 `fit()` 里一段最小的「生成 → 复制 → 并字段」数据流，并自己补一个 dispatch/collect 的小闭环。**

任务步骤：

1. 用 `from_dict` 构造一个 `batch_size=6` 的 `DataProto`，张量列含 `input_ids`（形状 `[6, 4]`）与 `attention_mask`，非张量列含 `data_source`（6 个任务名字符串）与 `reward_model`（6 个字典，如 `{'ground_truth': i}`）。
2. 模拟「rollout.n=2」：调用 `repeat(repeat_times=2, interleave=True)`，断言 `len == 12`，并验证每两个相邻样本的 `data_source` 相同。
3. 模拟「把生成结果粘回去」：另造一个 `batch_size=12`、只含 `responses`（形状 `[12, 3]`）与 `old_log_probs`（形状 `[12, 3]`）张量的 `DataProto`，用 `union` 粘到第 2 步的结果上，断言 `batch.keys()` 现在含 4 个张量字段。
4. 模拟「分发到 2 个 rank」：先 `pad_dataproto_to_divisor(_, size_divisor=2)` 确认能整除（12 已能整除，pad_size 应为 0），再 `chunk(2)`，断言每块 `len == 6` 且字段齐全。
5. 模拟「汇聚」：`DataProto.concat(两块)`，断言 `len == 12`。
6. **需要观察的现象**：每一步的 `len(data)` 与 `list(data.batch.keys())` 都符合预期；`data_source` 在 repeat 后呈现 `[a,a,b,b,…]` 的交错排列；union 后字段数增加且无重名冲突。
7. **预期结果**：整条流水线无断言错误，最终 merged 与第 3 步的宽表在 `input_ids`/`attention_mask`/`data_source` 上逐元素相等。
8. 进阶：把第 1 步的 `batch_size` 改成 5，重复上述流程，体会 `pad_dataproto_to_divisor(size_divisor=2)` 会补 1 行（变成 6），repeat 后变 12，仍能整除——这正是 `_validate` 在验证集样本数不是 GPU 倍数时的真实处境。

> 若本地无法运行，至少完成「在纸上画出每步 batch_size 与字段集合变化」的源码阅读型实践，并用本讲引用的三个测试函数（`test_chunk_concat` / `test_repeat` / `test_dataproto_pad_unpad`）的断言核对你的结论。

## 6. 本讲小结

- `DataProto` 是 verl 的统一数据协议，由 `batch`（`TensorDict` 张量列）、`non_tensor_batch`（`dtype=object` 的 ndarray 非张量列）、`meta_info`（全局元信息）三段构成，构造时经 `check_consistency` 强制「所有列第一维 = batch_size」。
- `from_single_dict` 自动按类型把混合字典分流为张量/非张量，正好接住 u2-l3 `collate_fn` 的输出；`from_dict` 适合已手工分类的场景。
- 五个核心操作：`chunk`（竖切分发）、`concat`（纵拼汇聚）、`union`（横向并字段，重名必须相等）、`repeat`（复制样本，GRPO 用 interleave）、`reorder`（原地重排）。
- `chunk` 只支持等分，因此 `pad_dataproto_to_divisor` 用「自身头部拷贝」把 batch_size 补到能被 `world_size` 整除，处理完用 `unpad_dataproto` 还原。
- dispatch 装饰器里 `_split_args_kwargs_data_proto` 调 `chunk`、`_concat_data_proto_or_future` 调 `concat`，证明这五件套就是「驱动 ↔ Worker」之间数据搬运的全部动词。
- 真实调用现场集中在 `fit()` 主循环的 [ray_trainer.py:581-628](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L581-L628)，是理解 PPO 一步训练数据流的钥匙。

## 7. 下一步学习建议

- **本讲建立了「货箱（DataProto）」，下一讲 u3-l2（Single Controller 与 Ray 资源池）将建立「货车（RayWorkerGroup / 资源池）」**：搞清楚这些 `DataProto` 是被谁、按什么资源划分发到哪些 GPU 上的。
- 之后再读 **u3-l3（Dispatch 装饰器）**，把本讲提到的 `_split_args_kwargs_data_proto`（chunk）与 `_concat_data_proto_or_future`（concat）与 `ONE_TO_ALL` / `DP_COMPUTE_PROTO` 等 Dispatch 模式对应起来，理解 `chunk`/`concat` 是如何被装饰器自动注入的。
- 想看 `DataProto` 被「用满」的完整场景，直接精读 [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) 的 `fit()`，本讲的每个方法在那里都有真实出现。
- 进阶可阅读 `DataProtoFuture`（[verl/protocol.py:595-639](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/protocol.py#L595-L639)），它用 Ray 的 `ObjectRef` 把 `collect_fn`/`dispatch_fn` 延迟到 Worker 侧执行，实现驱动进程的异步化——这是 u3-l2/u3-l3 会用到的性能优化层。
