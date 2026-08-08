# 数据源 DataSource 与缓冲区

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `DataSource` 抽象基类规定的四个核心方法（`get_samples` / `add_samples` / `save` / `load`）加 `__len__` 各自的职责与契约。
- 理解默认数据源 `RolloutDataSourceWithBuffer` 的「先取缓冲、再取原始数据集」的两段式取数逻辑，以及 `buffer_filter`（默认 `pop_first`）如何决定从缓冲里挑哪些样本。
- 看懂数据源状态如何随 checkpoint 的 `save` / `load` 持久化，以及 partial rollout 如何借助缓冲区把「半成品样本」跨轮续训。
- 自己动手继承 `DataSource`，写一个返回固定假数据的自定义数据源类，并通过 `--data-source-path` 注入 slime。

本讲只聚焦数据源这一层，**不**展开 rollout 内部的生成与奖励逻辑（那是 u3-l2 的内容），也**不**展开训练如何消费这些样本（那是 U4 的内容）。

## 2. 前置知识

本讲默认你已经掌握：

- **Sample 数据结构**（u3-l1）：数据源产出的就是 `list[list[Sample]]`——外层是「prompt 组」，内层是同一 prompt 的 `n_samples_per_prompt` 份采样副本。`Sample` 的字段（`tokens` / `response_length` / `loss_mask` / `reward` / `status` / `metadata` 等）在数据源阶段只是部分被填写。
- **rollout 函数签名**（u3-l2）：rollout 函数形如 `generate_rollout(args, rollout_id, data_source, evaluation)`，其中第三个参数 `data_source` 就是本讲的主角。函数内部通过 `data_source.get_samples(...)` 取 prompt、通过 `data_source.add_samples(...)` 回收样本。
- **load_function 注入机制**（u6-l1 预告）：slime 用形如 `"slime.rollout.data_source.RolloutDataSourceWithBuffer"` 的 import 路径字符串，在运行时把字符串解析成可调用对象（类或函数），实现「不改源码就能换实现」。

一个直觉：数据源就是 rollout 阶段的「仓库管理员」——它负责决定这一轮采样从哪里领 prompt、上一轮没收完的半成品放回哪里、以及训练中断后从哪个位置继续。rollout 函数本身只管「领料→加工→交货」，不关心仓库内部怎么摆放。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [slime/rollout/data_source.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py) | **本讲核心**。定义 `DataSource` 抽象基类、`RolloutDataSource`（基于全局数据集的只读实现）、`RolloutDataSourceWithBuffer`（带缓冲区的默认实现）、`pop_first` 默认缓冲过滤器。 |
| [slime/ray/rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py) | `RolloutManager` 在 `__init__` 里用 `load_function` 把 `--data-source-path` 解析成类并实例化为 `self.data_source`；`save` 方法会委托 `self.data_source.save(rollout_id)`。 |
| [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py) | 定义 `--data-source-path`（默认指向 `RolloutDataSourceWithBuffer`）与 `--buffer-filter-path` 两个参数。 |
| [slime/utils/misc.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py) | `load_function`：把 import 路径字符串解析成对象，是整个注入机制的底层。 |
| [slime/rollout/sglang_rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py) | 默认 rollout 函数消费数据源的范例：`get_samples` 取 prompt、partial rollout 时 `add_samples` 回收半成品。 |
| [tests/plugin_contracts/test_plugin_rollout_contracts.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_rollout_contracts.py) | `ContractDataSource`——一个最小可用的数据源实现，本讲实践任务的参照样板。 |

---

## 4. 核心概念与源码讲解

数据源这一层在源码里是一条「三层继承链」：

```
DataSource (抽象基类：规定契约)
   └── RolloutDataSource (基于全局数据集的只读实现)
          └── RolloutDataSourceWithBuffer (默认实现：额外维护一个缓冲区)
```

我们按三个最小模块拆开讲：先看顶层的抽象契约，再看默认实现 `RolloutDataSourceWithBuffer`（顺带交代它父类 `RolloutDataSource` 的取数与持久化逻辑），最后看默认缓冲过滤器 `pop_first` 以及如何写自定义过滤器。

### 4.1 DataSource 抽象基类

#### 4.1.1 概念说明

`DataSource` 是一个纯抽象基类（继承 `abc.ABC`），它**不提供任何实现**，只用 `@abc.abstractmethod` 规定「一个合格的数据源必须长什么样」。任何子类若没有把这些方法全部实现，Python 会在实例化时直接抛 `TypeError`——这是编译期之外的「契约校验」，强制自定义数据源不能漏掉关键方法。

它规定了五个抽象方法，可以分成两组记忆：

- **取数 / 回收**：`get_samples(num_samples)` 拿 prompt 组、`add_samples(samples)` 把样本塞回去。
- **持久化**：`save(rollout_id)` 存状态、`load(rollout_id)` 读状态。
- **容量**：`__len__` 返回当前还能取多少。

#### 4.1.2 核心流程

一次 rollout 中，数据源参与的主流程是：

```text
RolloutManager.__init__
   └── load_function("--data-source-path") → 类 → 实例化成 self.data_source

每一轮 rollout：
   generate_rollout(args, rollout_id, data_source)
      ├── data_source.get_samples(k)        # 领 k 组 prompt
      ├── （生成 + 奖励，由 rollout 内部完成）
      └── if 有半成品: data_source.add_samples(aborted)   # partial rollout 回收

周期性 checkpoint：
   RolloutManager.save(rollout_id)
      └── self.data_source.save(rollout_id)  # 存游标，以便恢复

恢复训练：
   data_source.load(rollout_id)              # 从存档点恢复游标
```

注意三个约定：

1. **返回结构是 `list[list[Sample]]`**：外层每个元素是一个「prompt 组」，组内有 `n_samples_per_prompt` 份副本（GRPO 类算法需要同 prompt 多采样）。
2. **`get_samples` 是有副作用的**：它推进内部游标，连续两次调用不会返回相同数据（除非显式回收）。`__len__` 的文档也明确指出「长度会随 add/fetch 变化」。
3. **`save`/`load` 只持久化「游标状态」**，不持久化样本内容——这一点在 4.2 会展开。

#### 4.1.3 源码精读

[slime/rollout/data_source.py:17-46](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L17-L46) —— `DataSource` 抽象基类，规定五个抽象方法。关键代码：

```python
class DataSource(abc.ABC):
    @abc.abstractmethod
    def get_samples(self, num_samples: int) -> list[list[Sample]]:
        """Return num_samples samples"""

    @abc.abstractmethod
    def add_samples(self, samples: list[list[Sample]]):
        """Add samples to the data source"""

    @abc.abstractmethod
    def save(self, rollout_id):
        """Save the state of the data source"""

    @abc.abstractmethod
    def load(self, rollout_id=None):
        """Load the state of the data source"""

    @abc.abstractmethod
    def __len__(self) -> int:
        """Length of the data source. May change when samples are added/fetched."""
```

注意 `__len__` 的注释「May change when samples are added/fetched」——它强调数据源是**有状态的、可变的**，不是对静态数组的只读视图。

而真正把这个抽象「实例化」进系统的地方在 RolloutManager：

[slime/ray/rollout.py:444-445](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L444-L445) —— 把参数里的 import 路径字符串解析成类，再 `实例化(args)`：

```python
data_source_cls = load_function(self.args.data_source_path)
self.data_source = data_source_cls(args)
```

`load_function` 的实现极其简短，靠 `rpartition(".")` 把字符串切成「模块路径」与「属性名」：

[slime/utils/misc.py:39-47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L39-L47) —— `load_function`：`importlib.import_module(module_path)` 后 `getattr(module, attr)`。

对应的参数定义，注意默认值正是默认数据源的完整路径：

[slime/utils/arguments.py:627-632](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L627-L632) —— `--data-source-path`，`default="slime.rollout.data_source.RolloutDataSourceWithBuffer"`。这意味着你只要不改这个参数，系统就用带缓冲区的默认实现；换成你自己的路径就能整个替换数据源。

#### 4.1.4 代码实践

**目标**：亲手验证「抽象方法没实现全就无法实例化」这一契约。

**操作步骤**：

1. 在 slime 可被 import 的环境里（u1-l3 介绍的 `pip install -e . --no-deps` 之后），新建一个临时 Python 文件，写一个只实现了部分方法的子类：

```python
# 示例代码：演示抽象方法未实现全时的报错
from slime.rollout.data_source import DataSource

class BrokenSource(DataSource):
    def get_samples(self, num_samples):
        return []

# 故意不实现 add_samples / save / load / __len__
source = BrokenSource()
```

2. 运行它。

**需要观察的现象**：解释器**不会**等你调用某个方法才报错，而是在 `BrokenSource()` 这一行就抛 `TypeError: Can't instantiate abstract class BrokenSource with abstract method add_samples, __len__, load, save`。

**预期结果**：你在实例化阶段就被拦下，提示缺哪些方法。这正说明了抽象基类的契约校验在「构造时」而非「调用时」生效。

> ⚠️ 本实践为「源码阅读 + 最小复现型」，无需 GPU 或真实数据。

#### 4.1.5 小练习与答案

**练习 1**：如果想让自定义数据源**不可恢复**（即不需要 checkpoint 续训），`save` 和 `load` 可以怎么写？

**参考答案**：写成空操作即可，例如 `def save(self, rollout_id): return` 与 `def load(self, rollout_id=None): return`。它们仍必须存在（否则无法实例化），但什么都不做。事实上默认的 `RolloutDataSource` 在 `--disable-rollout-global-dataset` 时正是这样「早早 return」的。

**练习 2**：`get_samples` 的返回类型为什么是 `list[list[Sample]]` 而不是扁平的 `list[Sample]`？

**参考答案**：因为 GRPO 等算法需要对**同一个 prompt** 采样多份（由 `--n-samples-per-prompt` 控制）来估计组内基线（advantage）。外层一项 = 一个 prompt 组，内层 = 该组的多份采样。扁平结构会丢失「哪些样本来自同一 prompt」这个分组信息。

---

### 4.2 RolloutDataSourceWithBuffer：带缓冲区的默认实现

#### 4.2.1 概念说明

`RolloutDataSourceWithBuffer` 是系统的默认数据源（`--data-source-path` 的默认值）。要理解它，必须先看它的父类 `RolloutDataSource`，再看它在父类基础上加了什么。

**父类 `RolloutDataSource`** 解决的是「从全局数据集领 prompt」的问题：

- 它持有一个 `Dataset` 对象（由 `--prompt-data` 指向的 jsonl 文件构建，u3-l1 提到的 `Dataset` 在 `slime/utils/data.py`）。
- 用四个游标记录「领到哪里了」：`sample_offset`（数据集内偏移）、`epoch_id`（第几轮 epoch）、`sample_group_index`（已发出的 prompt 组数）、`sample_index`（已发出的样本总数）。
- 它是**只读**的：`add_samples` 直接抛 `RuntimeError`。

**子类 `RolloutDataSourceWithBuffer`** 在父类基础上加了一个 `self.buffer`（一个 `list[list[Sample]]`），用来存放「半成品 / 多余 / 回收」的样本组。它的取数逻辑是**两段式**：

1. **先从缓冲区取**（能取多少取多少）；
2. **不够的部分再向父类（全局数据集）要**。

这个缓冲区是 partial rollout 能跨轮续训的关键：上一轮没生成完的样本被 `add_samples` 塞进缓冲区，下一轮 `get_samples` 会优先消费它们，避免重复领 prompt、避免浪费已经生成的部分 token。

#### 4.2.2 核心流程

**取数流程（`get_samples`）**：

```text
请求 num_samples 组
   ├── 先 _get_samples_from_buffer(num_samples)
   │     └── buffer_filter(args, None, buffer, 剩余需求)  # 默认 pop_first
   ├── 已取 len(samples) 组 → 还差 (num_samples - len(samples)) 组
   └── 若还差 > 0：super().get_samples(num_samples=差额)   # 向全局数据集要
```

**父类取数流程（`RolloutDataSource.get_samples`）**，核心是 epoch 回绕：

设数据集长度为 \(N\)，当前游标为 \(c=\text{sample\_offset}\)，请求 \(k\) 组：

- 若 \(c + k \le N\)：直接取切片 \([c,\ c+k)\)，游标推进到 \(c+k\)。
- 否则：先取尾部 \([c,\ N)\)，得到 \(N-c\) 组；还差 \(k-(N-c)\) 组；`epoch_id += 1`，按新 epoch 种子重新 `shuffle`，再从头取 \([0,\ k-(N-c))\)，游标置为 \(k-(N-c)\)。

也就是说，数据集被看作「按 epoch 重洗后无限循环」的虚拟流，游标单调推进、按 \(N\) 取模回绕。回绕次数记进 `epoch_id`。

取到 prompt 后，每个 prompt 会被**深拷贝** `n_samples_per_prompt` 份，分别打上全局递增的 `group_index` / `index`，组成一组返回：

```python
for prompt_sample in prompt_samples:
    group = []
    for _ in range(self.args.n_samples_per_prompt):
        sample = copy.deepcopy(prompt_sample)
        sample.group_index = self.sample_group_index
        sample.index = self.sample_index
        self.sample_index += 1
        group.append(sample)
    self.sample_group_index += 1
    samples.append(group)
```

**持久化流程（`save` / `load`）**：

- `save` 把四个游标和 `metadata` 打包成 `state_dict`，用 `torch.save` 写到 `{args.save}/rollout/global_dataset_state_dict_{rollout_id}.pt`。
- `load` 从 `{args.load}/rollout/...` 读取并还原游标，再按恢复后的 `epoch_id` 重新 shuffle 数据集，保证续训的数据顺序与中断前一致。

> ⚠️ **重要细节**：`save` 只持久化**游标**，**不**持久化 `buffer` 里的样本（`RolloutDataSourceWithBuffer` 没有覆盖 `save`/`load`）。这意味着一旦中断重启，缓冲区里的半成品样本会丢失——这是当前实现的取舍。

#### 4.2.3 源码精读

[slime/rollout/data_source.py:50-88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L50-L88) —— 父类 `RolloutDataSource.__init__`：初始化四个游标，并在 `--rollout-global-dataset` 且 `--prompt-data` 非空时构建 `Dataset`、按 `epoch_id` shuffle；否则 `self.dataset = None`。

[slime/rollout/data_source.py:90-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L90-L118) —— 父类取数 `get_samples`：上面流程图里的 epoch 回绕 + `n_samples_per_prompt` 深拷贝分组。注意 `dataset is None` 分支会生成空 `Sample()`（用于 custom rollout 自己造数据的场景）。

[slime/rollout/data_source.py:120-121](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L120-L121) —— 父类 `add_samples` 直接抛错，强调自己是只读数据源：

```python
def add_samples(self, samples: list[list[Sample]]):
    raise RuntimeError(f"Cannot add samples to {self.__class__.__name__}. This is a read-only data source.")
```

[slime/rollout/data_source.py:123-160](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L123-L160) —— `save` 写 `state_dict` 到 `.pt`、`load` 还原游标并重洗。关键点：`save` 在 `not args.rollout_global_dataset` 时早早 `return`（不存）；`load` 在 `args.load is None` 或文件不存在时也早早 `return`（不报错，按全新状态启动）。

接下来是本模块的主角：

[slime/rollout/data_source.py:168-189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L168-L189) —— `RolloutDataSourceWithBuffer` 的构造与两段式取数：

```python
class RolloutDataSourceWithBuffer(RolloutDataSource):
    def __init__(self, args):
        super().__init__(args)
        self.buffer = []
        if self.args.buffer_filter_path is None:
            self.buffer_filter = pop_first
        else:
            self.buffer_filter = load_function(self.args.buffer_filter_path)

    def get_samples(self, num_samples: int) -> list[list[Sample]]:
        samples = self._get_samples_from_buffer(num_samples)
        num_samples -= len(samples)
        if num_samples == 0:
            return samples
        samples += super().get_samples(num_samples=num_samples)
        return samples
```

注意三件事：① 缓冲区就是普通 `list`；② 过滤器是一个**可替换的函数对象**，默认 `pop_first`，否则走 `load_function`（同样的字符串注入机制）；③ `super().get_samples(...)` 把「差额」交给父类，父类会再走 epoch 回绕逻辑。

[slime/rollout/data_source.py:198-211](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L198-L211) —— `add_samples` 把回收的组追加进 `buffer`，并做两层断言：① 外层必须是 `list`；② 每个内层组的长度必须等于 `n_samples_per_prompt`。这保证了缓冲区里的组结构与直接从数据集取出的组结构完全一致，下游无需区分来源。

谁在调用 `add_samples` 把半成品塞回缓冲区？是默认 rollout 函数的 partial rollout 回收路径：

[slime/rollout/sglang_rollout.py:637-639](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L637-L639) —— 取数生成后，若有半成品则回收：

```python
output, aborted_samples = run(generate_rollout_async(args, rollout_id, data_source.get_samples))
if aborted_samples:
    data_source.add_samples(aborted_samples)
```

而半成品的来源在 `abort` 函数里（u3-l2 讲过 abort 机制，这里看它如何与缓冲区衔接）：

[slime/rollout/sglang_rollout.py:354-367](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L354-L367) —— 凑够目标后 abort 多余任务；`partial_rollout` 开启时，把已经生成了一部分 response 的样本打上 `start_rollout_id`，作为「半成品」收集起来，准备塞回数据缓冲区。

#### 4.2.4 代码实践

**目标**：用真实参数对象驱动 `RolloutDataSourceWithBuffer`，观察「先取缓冲、再取数据集」的行为。

**操作步骤**：

1. 准备一个极简 jsonl（示例代码，2 条数据即可）：

```jsonl
{"input": "1+1="}
{"input": "2+2="}
```

2. 写一段脚本，手工构造一个最小 `args` 命名空间，实例化数据源，先 `add_samples` 注入 1 组缓冲，再 `get_samples(2)`：

```python
# 示例代码：手动驱动 RolloutDataSourceWithBuffer 观察两段式取数
from types import SimpleNamespace
from slime.rollout.data_source import RolloutDataSourceWithBuffer
from slime.utils.types import Sample

args = SimpleNamespace(
    rollout_global_dataset=True,
    prompt_data="prompts.jsonl",        # 上面的 jsonl 文件
    hf_checkpoint="<任意可加载的 tokenizer 路径>",  # 仅用于 load_tokenizer
    rollout_max_prompt_len=128,
    input_key="input", label_key=None, metadata_key="metadata",
    multimodal_keys=None, tool_key=None,
    apply_chat_template=False, apply_chat_template_kwargs={},
    rollout_seed=0, rollout_shuffle=False,
    n_samples_per_prompt=2,
    buffer_filter_path=None,
    dump_details=None, save="/tmp/slime_save", load=None,
)

ds = RolloutDataSourceWithBuffer(args)

# 注入 1 组「半成品」到缓冲区
half = [[Sample(prompt="buffered-prompt", index=999, group_index=999)]]
ds.add_samples(half)

# 请求 2 组：应先返回缓冲里那 1 组，再向数据集要 1 组
groups = ds.get_samples(2)
print("第 0 组（应来自缓冲）:", groups[0][0].prompt)
print("第 1 组（应来自数据集）:", groups[1][0].prompt)
print("缓冲剩余长度:", ds.get_buffer_length())
```

**需要观察的现象**：返回的两组里，第 0 组的 prompt 是 `"buffered-prompt"`（来自缓冲），第 1 组来自 jsonl；调用后 `get_buffer_length()` 变为 0。

**预期结果**：验证了「缓冲优先、不足再补」的两段式逻辑，且缓冲里的组被「取出即移除」。

> ⚠️ 本实践依赖 `load_tokenizer`，需要一个真实的 tokenizer 路径。若无可用模型，可改为只实例化不取数（设 `prompt_data=None`、`rollout_global_dataset=False`），观察 `dataset is None` 时 `get_samples` 返回空 `Sample()` 的行为。完整运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设缓冲区里有 3 组、数据集充足，调用 `get_samples(5)` 会发生什么？各来自哪里？

**参考答案**：先 `_get_samples_from_buffer(5)` 从缓冲取走全部 3 组（缓冲清空），还差 2 组；再 `super().get_samples(num_samples=2)` 从数据集取 2 组。最终返回 5 组：前 3 组来自缓冲，后 2 组来自数据集。

**练习 2**：为什么 `RolloutDataSource` 的 `add_samples` 要主动抛 `RuntimeError`，而不是静默忽略？

**参考答案**：因为 partial rollout 的语义是「把半成品放回**缓冲区**，下一轮继续」。父类没有缓冲区，若静默忽略，半成品样本会被悄悄丢弃，训练数据量莫名减少却无报错，极难排查。抛错把「你用错了类」这件事变成显式失败——想要回收能力，就该用带缓冲区的子类。

**练习 3**：中断重启后，缓冲区里的半成品还在吗？

**参考答案**：不在。`save`/`load` 只持久化父类的四个游标，`RolloutDataSourceWithBuffer` 没有覆盖它们，因此 `buffer` 不入档。重启后缓冲为空，那些半成品样本丢失，只能靠游标从数据集重新领 prompt。

---

### 4.3 pop_first 缓冲过滤器与自定义 buffer_filter

#### 4.3.1 概念说明

「缓冲过滤器」（buffer_filter）是 `RolloutDataSourceWithBuffer` 里一个**可替换的函数**，决定「当要从缓冲区取 `num_samples` 组时，挑哪些、留哪些」。默认实现是 `pop_first`——按 FIFO（先进先出）取最前面的若干组，这也是「回收的半成品优先被下一轮消费」的来源。

它被设计成可替换，是为了让高级用户能改变缓冲的取数策略，例如「优先取奖励方差大的组」「按长度排序取」等，而不必重写整个数据源。

#### 4.3.2 核心流程

`buffer_filter` 的调用契约（签名）是：

```python
def filter(args, rollout_id, buffer: list[list[Sample]], num_samples: int) -> list[list[Sample]]
```

调用点在 `_get_samples_from_buffer`：

```python
samples = self.buffer_filter(self.args, None, self.buffer, num_samples)
```

注意四个要点：

1. **接收 `buffer` 本体（引用），而非副本**。过滤器必须**就地**把已取出的样本从 `buffer` 里移除，否则下一轮会重复返回。
2. **第二个参数 `rollout_id` 当前传 `None`**——保留给未来按轮次做策略的过滤器使用。
3. **返回值是被取出的组列表**，长度 ≤ `num_samples`（可以少于请求量，不足部分由父类补）。
4. 默认 `pop_first` 的策略是 FIFO：取 `min(len(buffer), num_samples)` 组，从头部切片返回并 `del` 掉。

#### 4.3.3 源码精读

[slime/rollout/data_source.py:225-229](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L225-L229) —— 默认过滤器 `pop_first`，逻辑只有三行：

```python
def pop_first(args, rollout_id, buffer: list[list[Sample]], num_samples: int) -> list[list[Sample]]:
    num_to_pop = min(len(buffer), num_samples)
    samples = buffer[:num_to_pop]
    del buffer[:num_to_pop]
    return samples
```

`buffer[:num_to_pop]` 切出要返回的组，`del buffer[:num_to_pop]` 把它们从原缓冲里删掉——这一步是「就地修改」，是过滤器必须承担的副作用。

它的调用点：

[slime/rollout/data_source.py:191-196](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/data_source.py#L191-L196) —— `_get_samples_from_buffer`：缓冲空或需求为 0 时直接返回空列表，否则交给过滤器。

替换入口的参数定义：

[slime/utils/arguments.py:505-514](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L505-L514) —— `--buffer-filter-path`：`None` 时用默认 `pop_first`，否则用 `load_function` 解析成自定义函数。注意帮助文本里对签名的描述略简（实际签名还含 `args` 和 `rollout_id` 两个前导参数，以源码为准）。

#### 4.3.4 代码实践

**目标**：写一个自定义 `buffer_filter`，让缓冲区按「组内平均响应长度从长到短」取数，体会「就地移除」这一副作用的重要性。

**操作步骤**：

1. 在项目里新建一个 Python 文件（示例代码，可放在任意可 import 的位置，例如 `my_filters.py`）：

```python
# 示例代码：自定义 buffer_filter，按组内平均响应长度降序取数
def longest_first(args, rollout_id, buffer, num_samples):
    # 计算每组平均 response_length（loss_mask 缺省时退化为 response_length）
    def avg_len(group):
        lens = [getattr(s, "effective_response_length", 0) for s in group]
        return sum(lens) / len(lens) if lens else 0

    # 按平均长度降序排序，取前 num_samples 组
    order = sorted(range(len(buffer)), key=lambda i: avg_len(buffer[i]), reverse=True)
    take = order[: min(len(buffer), num_samples)]

    taken = [buffer[i] for i in take]
    # 关键：必须就地从 buffer 删掉已取的组（按下标从大到小删，避免索引错位）
    for i in sorted(take, reverse=True):
        del buffer[i]
    return taken
```

2. 启动训练时加上参数 `--buffer-filter-path my_filters.longest_first`（路径按你实际放的位置调整）。

**需要观察的现象**：开启 partial rollout 后，回收进缓冲的半成品在下一轮会被「最长的优先」取出。如果你想验证「就地移除」的必要性，可故意把 `for i in sorted(...): del buffer[i]` 这段注释掉，再观察。

**预期结果**：注释掉就地删除后，同一组样本会在多轮 `get_samples` 中被**重复返回**，缓冲长度永不下降，训练数据出现重复——这就直观证明了过滤器必须自己把取走的样本从 `buffer` 里删掉。

> ⚠️ 本实践为「源码阅读 + 改造型」，完整端到端运行需 partial rollout 的真实集群环境；最小验证可在本地手工构造 `buffer` 调用 `longest_first(None, None, buf, 2)` 并断言 `len(buf)` 减少。

#### 4.3.5 小练习与答案

**练习 1**：如果自定义过滤器只 `return` 了一组、却忘了从 `buffer` 里删它，会发生什么？

**参考答案**：这组样本会一直留在缓冲里，下一次 `get_samples` 还会再次返回它，导致同一段半成品被重复训练。因为缓冲只通过过滤器的副作用来「出库」，忘记删除就等于「只借不还地复制」。

**练习 2**：`pop_first` 为什么用 `del buffer[:num_to_pop]` 而不是 `buffer = buffer[num_to_pop:]`？

**参考答案**：因为 `buffer` 是按引用传入的列表，`buffer = buffer[...]` 只会让局部变量名指向一个**新列表**，原调用方（`self.buffer`）不变；而 `del buffer[:]` / `del buffer[:n]` 是对**原列表本体**做就地删除，才能真正影响 `self.buffer`。这是 Python 可变对象传参的经典陷阱。

**练习 3**：`buffer_filter` 的 `rollout_id` 参数现在传的是 `None`，它可能服务于什么未来用途？

**参考答案**：用于「按 rollout 轮次做取数策略」。例如 partial rollout 跨多轮续传时，可能想优先消费「开始得最早」的半成品（按 `metadata["start_rollout_id"]` 排序），避免半成品因拖延太久变得过于 off-policy。把 `rollout_id` 传进来，过滤器就能据当前轮次与样本起始轮次的差距做衰减或丢弃。

---

## 5. 综合实践

**任务**：继承 `DataSource`，实现一个**自带内存缓冲**的最小数据源 `MyFixedDataSource`，要求：

- 内置 10 条固定假 prompt（`"prompt-0"` … `"prompt-9"`），`n_samples_per_prompt=1`。
- `get_samples(num_samples)`：先从内部缓冲取（FIFO），不足再从 10 条 prompt 里按游标取（带 epoch 回绕），每组返回 1 个 `Sample`。
- `add_samples(samples)`：把回收的组追加进内部缓冲（做长度校验）。
- `save(rollout_id)` / `load(rollout_id)`：用 `json` 把游标与缓冲的 prompt 文本存到 `/tmp/my_source_{rollout_id}.json` 并能读回。
- `__len__`：返回「剩余 prompt 数 + 缓冲组数」。
- 实例化后调用 `get_samples(2)`，确认返回 2 组；再 `add_samples` 注入 1 组、再 `get_samples(1)`，确认先取到注入的那组。

**参考骨架**（示例代码，需要 slime 可 import）：

```python
# 示例代码：综合实践——自带的、可持久化的迷你数据源
import copy, json
from slime.rollout.data_source import DataSource
from slime.utils.types import Sample

class MyFixedDataSource(DataSource):
    def __init__(self, n_samples_per_prompt: int = 1):
        self.prompts = [f"prompt-{i}" for i in range(10)]   # 固定 10 条假数据
        self.n = n_samples_per_prompt
        self.cursor = 0          # 数据集游标
        self.epoch_id = 0
        self.group_index = 0
        self.buffer = []         # 回收缓冲

    def _take_from_prompts(self, k):
        groups, got = [], 0
        while got < k:
            p = self.prompts[self.cursor % len(self.prompts)]
            if self.cursor % len(self.prompts) == 0 and got > 0:  # 回绕即换 epoch
                self.epoch_id += 1
            s = Sample(prompt=p, group_index=self.group_index, index=self.group_index)
            self.group_index += 1
            groups.append([copy.deepcopy(s)])
            self.cursor += 1
            got += 1
        return groups

    def get_samples(self, num_samples):
        out = self.buffer[:num_samples]
        self.buffer = self.buffer[num_samples:]
        need = num_samples - len(out)
        if need > 0:
            out += self._take_from_prompts(need)
        return out

    def add_samples(self, samples):
        assert isinstance(samples, list) and isinstance(samples[0], list)
        for g in samples:
            assert len(g) == self.n
            self.buffer.append(g)

    def save(self, rollout_id):
        json.dump({"cursor": self.cursor, "epoch_id": self.epoch_id,
                   "group_index": self.group_index,
                   "buffer": [[s.prompt for s in g] for g in self.buffer]},
                  open(f"/tmp/my_source_{rollout_id}.json", "w"))

    def load(self, rollout_id=None):
        d = json.load(open(f"/tmp/my_source_{rollout_id}.json"))
        self.cursor, self.epoch_id, self.group_index = d["cursor"], d["epoch_id"], d["group_index"]
        self.buffer = [[Sample(prompt=p) for p in g] for g in d["buffer"]]

    def __len__(self):
        return (len(self.prompts) - (self.cursor % len(self.prompts))) + len(self.buffer)

# 验证
ds = MyFixedDataSource()
g1 = ds.get_samples(2)
print([g1[0][0].prompt, g1[1][0].prompt])     # ['prompt-0', 'prompt-1']
ds.add_samples([[Sample(prompt="recycled", index=-1)]])
g2 = ds.get_samples(1)
print(g2[0][0].prompt)                         # recycled（缓冲优先）
print("len:", len(ds))                          # 剩余 prompt 数 + 缓冲数
```

**自检要点**：

1. 抽象方法全部实现，`MyFixedDataSource()` 不会抛 `TypeError`。
2. `get_samples(2)` 先取到 `recycled`（缓冲优先），证明两段式逻辑正确。
3. `save(5)` 后新建实例 `load(5)`，游标与缓冲应能恢复。

> ⚠️ 骨架省略了真实 slime 里 `load_tokenizer`、`Dataset`、`shuffle` 等依赖，专注于「数据源契约 + 缓冲 + 持久化」三件事。完整行为对照真实 `RolloutDataSourceWithBuffer` 阅读效果最佳。

## 6. 本讲小结

- `DataSource` 是数据源的抽象契约，规定五个抽象方法：取数 `get_samples`、回收 `add_samples`、持久化 `save`/`load`、容量 `__len__`；缺一不可实例化。
- 默认数据源是继承链 `DataSource → RolloutDataSource → RolloutDataSourceWithBuffer`，靠 `--data-source-path`（默认 `slime.rollout.data_source.RolloutDataSourceWithBuffer`）经 `load_function` 注入。
- `RolloutDataSource` 负责从全局数据集按 epoch 回绕取 prompt，并按 `n_samples_per_prompt` 深拷贝成组；它是只读的，`add_samples` 会抛错。
- `RolloutDataSourceWithBuffer` 额外维护一个 `buffer`，取数采用「先取缓冲、不足再向数据集要」的两段式；`add_samples` 把 partial rollout 回收的半成品塞进缓冲，实现跨轮续训。
- `buffer_filter`（默认 `pop_first`，FIFO）决定从缓冲取哪些组，必须**就地移除**已取样本；可通过 `--buffer-filter-path` 替换为自定义策略。
- `save`/`load` 只持久化游标（`sample_offset`/`epoch_id`/`sample_group_index`/`sample_index`），**不**持久化缓冲区内容，中断重启后缓冲会清空。

## 7. 下一步学习建议

- **下一讲 u3-l4（奖励模型 rm_hub）**：数据源产出的 `Sample` 在 rollout 内部会经过奖励计算。了解了「数据从哪来」之后，自然要问「reward 怎么算」，两讲连起来就是 `get_samples → 生成 → 算奖励 → 返回训练样本` 的完整前半段。
- **横向对照 u3-l2（默认 rollout 流程）**：回看 `generate_rollout` 里 `data_source.get_samples` 与 `data_source.add_samples` 的调用点，把本讲的数据源机制嵌进整体 rollout 流程图。
- **进阶 u7-l4（流式 / 全异步 / partial rollout）**：本讲只点了 partial rollout 与缓冲区的衔接，三种高级数据流的完整对比在 u7-l4，届时你会看到缓冲区在不同异步模式下的不同用法。
- **延伸阅读源码**：`slime/utils/data.py` 的 `Dataset` 类（数据源真正的底层存储与 shuffle 实现）、`slime_plugins/rollout_buffer/`（基于外部服务的另一类数据源实现，展示「数据源可整体替换」的极端案例）。
