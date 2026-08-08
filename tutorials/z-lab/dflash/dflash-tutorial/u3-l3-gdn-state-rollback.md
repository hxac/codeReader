# 混合模型的门控增量状态捕获与回滚

## 1. 本讲目标

本讲是 MLX 后端专家单元的第三讲，专门解决一个「最难啃」的回滚场景：当目标模型（target）是 **Qwen3.5 这类混合架构**——内部含有 **GatedDeltaNet（门控增量网络，下文简称 GDN）** 这种带「递归状态」的层时，被投机解码拒绝的 token 该怎么回滚。

学完本讲，你应该能够：

1. 说清楚 **GDN 层为什么不能用普通 KV cache 的 `trim` 直接裁剪回滚**——它的状态由「卷积状态 + delta 递归状态」两部分组成，而不是一个可尾部删除的 K/V 列表。
2. 读懂 [`_GDNStateCapture`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L293-L397) 的三段机制：**patch（注入捕获）→ clear（每轮清空）→ rollback（重放重建）→ close（还原补丁）**。
3. 理解 `stream_generate` 里 `_HAS_GDN` 与 `can_trim_prompt_cache` 两个开关如何共同决定走 `_trim_recent_cache`（普通裁剪）还是 `_GDNStateCapture.rollback`（状态重放），以及两者都失效时为什么必须报错。

> 本讲是纯「源码精读 + 推理」型讲义。运行 Qwen3.5 混合模型需要 Apple 芯片 + 对应的 `mlx_lm.models.gated_delta` 内核，多数读者不具备该环境，因此实践以「读源码、画草图、做断言推理」为主，凡需实跑处都标注「待本地验证」。

---

## 2. 前置知识

本讲直接承接 **u3-l2《MLX 流式生成循环与缓存回滚》**。在进入本讲前，请确认你已经理解以下概念（本讲不再重复推导，只做一句话提示）：

- **投机解码的回滚需求**：target 一次前向验证整块候选，被拒候选的脏状态必须从缓存里清掉，否则后续生成全部错乱。普通注意力层的脏状态就是「最近几格 K/V」，删掉即可。
- **`trim = bs - accepted - 1`**：每轮被拒候选数。u3-l2 的核心公式，本讲会在 GDN 场景下复用它。
- **`can_trim_prompt_cache(cache)`**：`mlx_lm` 的检测函数，判断一组缓存能否用 `trim` 从尾部删 token。普通 `KVCache` / `RotatingKVCache` 返回 `True`，**带递归状态的层返回 `False`**。本讲的主角正是「返回 `False`」的那批层。
- **`_trim_recent_cache`**：u3-l2 讲过的普通裁剪函数，对 `RotatingKVCache` 先 `_temporal_order` 还原时间序再切片、对普通 `KVCache` 直接 `trim`。

此外需要补充一点本讲才出现的前置知识——**什么是 GatedDeltaNet**。

### 2.1 什么是 GatedDeltaNet（直觉版）

普通注意力层（Transformer 的 softmax attention）把历史信息存在 **KV cache** 里：每个位置一对 `(key, value)`，查询时把所有历史 K 拿来做点积。这是一种「**显式存储、查询时聚合**」的机制，天然支持「删掉最后一个位置」（`trim`）。

GatedDeltaNet 走的是另一条路——**线性注意力 / 状态空间模型（SSM）风格的「递归状态」**。它不把历史逐位存成 K/V 列表，而是维护一个**不断被增量更新的矩阵状态 `state`**（称为 delta state）：每来一个新 token，就用门控（gate）决定「保留多少旧状态、写入多少新信息」，把状态往前推一格。这有点像 RNN 的隐状态：`state_t = f(state_{t-1}, token_t)`。

为了让短距离信息更准，GDN 层通常还在前面挂一个**一维短卷积（short conv）**，卷积自己也维护一个长度为 `conv_kernel_size - 1` 的**卷积状态 `conv_state`**（卷积滑窗里最近的历史输入）。

于是，一个 GDN 层的「记忆」由两部分组成：

| 状态 | 形状（序列维） | 性质 | 能否 `trim` |
|---|---|---|---|
| `conv_state`（卷积状态） | `conv_kernel_size - 1` 个 token | 定长滑窗 | ❌ 不能 |
| `state`（delta 递归状态） | 一个矩阵（与序列长度无关） | 递归累积 | ❌ 不能 |

**关键结论**：这两部分状态都是「**到目前为止所有 token 的累积函数**」，而不是「每 token 一格、可逐格删除」的列表。你想把最后一个被拒 token 从状态里抹掉，无法靠「删尾巴」实现——因为最后那个 token 已经被**卷进**了整个 `state` 矩阵和 `conv_state` 滑窗里。

> 这就是为什么 `can_trim_prompt_cache` 对 GDN 层返回 `False`，也是为什么 u3-l2 把这种模型「踢」给了本讲。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但它是 dflash 里最复杂的一段逻辑：

| 文件 | 作用 | 本讲关注范围 |
|---|---|---|
| [`dflash/model_mlx.py`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py) | Apple MLX 实现 | `_HAS_GDN` 探测（L19-23）、`_GDNStateCapture` 类（L293-397）、`stream_generate` 中的分流与生命周期（L453-466, L509-510, L561-567, L580-582） |

> 说明：`GatedDeltaNet` 类本身、`gated_delta_update` 内核来自 `mlx_lm.models.qwen3_5` 与 `mlx_lm.models.gated_delta`（见 L20、L309、L343）。它们是 dflash 的**外部依赖**，不在本仓库内，本讲只能根据 dflash 的调用方式推断其行为，无法给出仓库内永久链接。

---

## 4. 核心概念与源码讲解

### 4.1 混合架构的状态难题：为什么 GDN 层不能直接 trim

#### 4.1.1 概念说明

先建立「混合架构」的全局图景。Qwen3.5 不是「全注意力」也不是「全 GDN」，而是**把两种层交错堆叠**的混合模型：

```
Layer 0   : Full Attention   ← 普通 KV cache，可 trim
Layer 1   : GatedDeltaNet    ← 递归状态，不可 trim
Layer 2   : Sliding Attention ← 普通 KV cache，可 trim
Layer 3   : GatedDeltaNet    ← 递归状态，不可 trim
...
```

于是 target 的缓存 `target_cache` 是一个**异构列表**：有些元素可裁剪、有些不可裁剪。`can_trim_prompt_cache(target_cache)` 对这种列表整体返回 `False`（只要有一层不可裁剪，整组就按「不可裁剪」处理）。

投机解码的回滚因此分裂成两种需求：

- **可裁剪层（注意力）**：删掉尾部 `trim` 格 K/V 即可，u3-l2 已解决。
- **不可裁剪层（GDN）**：`trim` 删不了，必须**把状态重置回「只见过被接受的那几个 token」的样子**。

#### 4.1.2 核心流程：为什么「删尾巴」对 GDN 无效

用一个极简的递归状态来体会。设 GDN 的 delta state 更新（忽略门控细节）形如：

\[
\text{state}_t = g_t \cdot \text{state}_{t-1} + \Delta(\text{token}_t)
\]

即每一步，旧状态乘以一个门控系数 `g_t`，再叠加上新 token 的贡献 `\Delta(token_t)`。验证前向一次处理 `bs` 个 token（锚点 + 候选），把状态从 `state_0` 推到 `state_{bs}`：

\[
\text{state}_0 \xrightarrow{t_1} \text{state}_1 \xrightarrow{t_2} \cdots \xrightarrow{t_{bs}} \text{state}_{bs}
\]

假设只接受了前 `accepted+1` 个（锚点 + `accepted` 个候选），需要把状态回退到 `state_{accepted+1}`。

- **能 `trim` 吗？** 不能。`state_{bs}` 里**已经融进了** `token_{accepted+2} ... token_{bs}` 的信息（它们各自贡献了 `\Delta`，还改变了沿途的乘性门控）。这不是列表的「最后一格」，而是一个被搅浑的矩阵，没有「格」可删。
- **怎么办？** 唯一正确的办法是**从干净的检查点 `state_0`（验证前的状态）出发，只用被接受的前 `accepted+1` 个 token 重放一遍递归**，重新算出 `state_{accepted+1}`。

这正是 `_GDNStateCapture` 的核心思想：**既然删不掉，就重放重建**。为此它必须在验证前向时把「重建所需的原材料」全部捕获下来——这就是下一节 `_patch` 的任务。

#### 4.1.3 源码精读：不可裁剪的开关与硬性报错

先把分流的总闸看清。在 `stream_generate` 里，创建捕获器之前有一道「能力检查」：

[dflash/model_mlx.py:453-459](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L453-L459) —— `_target_can_trim` 检查 target 缓存能否裁剪；若**既不能裁剪、又没有 GDN 支持**，直接 `RuntimeError`；只有「不能裁剪 + 有 GDN」才创建 `_capture`。

```python
_target_can_trim = can_trim_prompt_cache(target_cache)
if not _target_can_trim and not _HAS_GDN:
    raise RuntimeError(
        "This MLX model requires gated-delta rollback support, but "
        "mlx_lm.models.gated_delta is unavailable."
    )
_capture = _GDNStateCapture() if not _target_can_trim else None
```

而 `_HAS_GDN` 是模块加载时的一次性探测：

[dflash/model_mlx.py:19-23](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L19-L23) —— 尝试 import `gated_delta` 内核模块，成功置 `True`、失败（环境里没有该内核）置 `False`。

```python
try:
    import mlx_lm.models.gated_delta as _gd_mod
    _HAS_GDN = True
except ImportError:
    _HAS_GDN = False
```

把这两个开关画成一张决策表（这是本讲最重要的速查表，后面会反复用到）：

| `_target_can_trim` | `_HAS_GDN` | 回滚路径 | 触发条件 |
|---|---|---|---|
| `True` | 任意 | `_trim_recent_cache`（u3-l2） | 普通全注意力 / 滑窗模型（Qwen3、Gemma 等） |
| `False` | `True` | `_GDNStateCapture.rollback`（本讲） | 混合架构（Qwen3.5）+ 装了 GDN 内核 |
| `False` | `False` | **`RuntimeError`** | 混合架构但没装 GDN 内核 |

> 4.1.4 的实践会要你解释第三行的报错路径——它就是这张表的「安全网」：绝不允许在「回滚不了」的情况下继续生成，否则产出会静默错乱。

#### 4.1.4 代码实践：读懂报错安全网

1. **实践目标**：确认你能说清「什么环境、什么模型」会触发那条 `RuntimeError`，以及为什么这条报错是**正确且必要**的。
2. **操作步骤**：
   - 打开 [dflash/model_mlx.py:453-459](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L453-L459)。
   - 假设你在一个**没装** `mlx_lm.models.gated_delta` 的 MLX 环境里，加载 `Qwen/Qwen3.5-4B`（混合架构）。回答：
     - `can_trim_prompt_cache(target_cache)` 会返回什么？为什么？
     - `_HAS_GDN` 会是什么？为什么？
     - 程序会停在哪一行？报错信息里的两个关键词是什么？
3. **需要观察的现象**：纯推理题，无运行输出。
4. **预期结果**：`can_trim_prompt_cache` 返回 `False`（因为含 GDN 层）；`_HAS_GDN` 为 `False`（import 失败）；程序停在 L454 的 `if not _target_can_trim and not _HAS_GDN:` 判定，抛出 `RuntimeError`，信息含 `gated-delta rollback support` 与 `mlx_lm.models.gated_delta is unavailable`。**必要性**：若不报错而继续，`_capture` 为 `None` 且缓存又不能 `trim`，被拒 token 的脏 GDN 状态会永久残留，后续生成全部基于「脏状态」——这是静默的数值错误，比报错危险得多。所以「**无法回滚就直接拒绝启动**」是正确的 fail-fast 设计。

#### 4.1.5 小练习与答案

- **练习 1**：如果某个模型「全注意力层」和「GDN 层」都没有，是个全 GDN 模型，`can_trim_prompt_cache` 返回什么？`_capture` 会创建吗？
  - **答案**：返回 `False`（所有层都不可裁剪）；只要 `_HAS_GDN` 为 `True`，`_capture` 会被创建（L459 的条件只看 `_target_can_trim`）。

- **练习 2**：为什么 `can_trim_prompt_cache` 对「整组缓存」做判断，而不是逐层判断？
  - **答案**：因为回滚要保证**所有层**一致地退回到同一个 token 位置。只要有一层回滚不了，整组就退化成「不可裁剪」路径，由 `_GDNStateCapture.rollback` 统一处理逐层分流（见 4.3）。

---

### 4.2 _GDNStateCapture._patch：用 monkey-patch 捕获卷积输入与增量更新输入

#### 4.2.1 概念说明

4.1 节的结论是：要回滚 GDN 状态，必须**重放**——从干净检查点出发，只用被接受的 token 跑一遍递归。这要求我们在 target 验证前向时，把两样「原材料」提前抄录下来：

1. **增量更新的输入** `gdn_inputs`：喂给 `gated_delta_update` 内核的 `q, k, v, a, b` 等（重建 delta state 用）。
2. **卷积的输入** `conv_input`：喂给短卷积的拼接序列（重建 conv_state 用）。

但问题来了：这些原材料是在 `mlx_lm` 的 `GatedDeltaNet.__call__` **内部**产生的，dflash 自己的代码够不着。dflash 的解法是 **monkey-patch（猴子补丁）**：在运行时把 `GatedDeltaNet.__call__` 整个替换成一个「**功能等价但顺手抄录中间量**」的版本，捕获完再还原。

> 这是一个典型的「**观察者补丁**」手法：被替换的函数行为和原函数一模一样（算出来的输出、对 cache 的写回都相同），只是**多做了两件事**——把 `conv_input` 和 `gdn_inputs` 各 `append` 到捕获器的列表里。对模型推理本身完全透明。

#### 4.2.2 核心流程：patch / clear / rollback / close 四段生命周期

`_GDNStateCapture` 的生命周期与 target 的验证节奏严格绑定：

```
构造 __init__   →  acquire 全局锁 + _patch() 注入捕获版 __call__
   ↓
每轮 decode:
   _capture.clear()   →  清空上一轮的 conv_data / gdn_inputs
   target 验证前向     →  被补丁的 __call__ 自动 append 本轮原材料
   计算 accepted
   若 trim > 0:
       _capture.rollback(cache, accepted, trim)
                          →  对每层：可裁剪则 c.trim(trim)；GDN 则重放重建
   ↓
生成结束 (finally) →  close() 还原原 __call__ + 释放全局锁
```

四个动作的职责：

- **`_patch`**：把捕获版函数绑到 `GatedDeltaNet.__call__`（全局生效），保存原始函数以便还原。
- **`clear`**：每轮验证前清空两个捕获列表——因为每轮的「检查点」和「原材料」都不同，上一轮的不能留。
- **`rollback`**：消费本轮捕获的原材料，重建 GDN 层状态。
- **`close`**：还原 `GatedDeltaNet.__call__`，并释放全局锁。

为什么需要全局锁 `_GDN_PATCH_LOCK`（见 [L26](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L26)）？因为 monkey-patch 改的是**类方法**，是进程级全局状态。如果两个生成并发跑，它们会互相覆盖对方的补丁、错乱还原。`RLock` 保证同一时刻只有一个 `_GDNStateCapture` 持有补丁。

#### 4.2.3 源码精读：构造与 patch 注入

先看构造函数——它**在构造时就立刻 patch**，并保证「patch 失败必释放锁」：

[dflash/model_mlx.py:294-306](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L294-L306) —— `__init__` 先 `acquire()` 全局锁，再调 `_patch()`；若 `_patch` 抛异常（如 `qwen3_5` 模块不存在），`except` 里 `release()` 后重抛，避免死锁。

```python
def __init__(self):
    self.conv_data = []
    self._gdn_inputs = []
    self._gdn_cls = None
    self._orig_call = None
    self._patched_call = None
    self._closed = False
    _GDN_PATCH_LOCK.acquire()
    try:
        self._patch()
    except Exception:
        _GDN_PATCH_LOCK.release()
        raise
```

注意 `self._closed` 标志——它让 `close()` 幂等（见 4.2.4 的练习），即使 `finally` 里重复调用也不会出错。

接着看 `_patch` 的替换逻辑本体：

[dflash/model_mlx.py:308-355](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L308-L355) —— `_patch` 导入 `GatedDeltaNet`，保存原 `__call__`，定义捕获版 `_capturing_gdn_call`，最后把它绑到类上。

```python
def _patch(self):
    from mlx_lm.models.qwen3_5 import GatedDeltaNet
    self._gdn_cls = GatedDeltaNet
    self._orig_call = GatedDeltaNet.__call__
    capture = self

    def _capturing_gdn_call(self_layer, inputs, mask=None, cache=None):
        # ... 见下方逐段精读 ...
        return out

    self._patched_call = _capturing_gdn_call
    GatedDeltaNet.__call__ = _capturing_gdn_call
```

这里有个 Python 闭包细节：内层函数里用 `capture = self` 给闭包捕获了 `_GDNStateCapture` 实例（避免 `self` 在被替换为类方法时语义混淆），后续 `capture.conv_data.append(...)` 就是往这个实例的两个列表里抄数据。

#### 4.2.4 源码精读：捕获 conv_input 与 gdn_inputs（核心）

`_capturing_gdn_call` 是本讲的「原料采集器」。它完整复刻了 `GatedDeltaNet.__call__` 的计算流程，只在两个关键点插入了 `append`。我们只看与捕获相关的关键行：

**捕获点 ①：卷积输入 `conv_input`**

[dflash/model_mlx.py:322-328](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L322-L328) —— 取出旧卷积状态 `conv_state`（长度 `conv_kernel_size - 1`），与新 token 的 `qkv` 沿序列维拼接成 `conv_input`，抄录 `(conv_input, conv_kernel_size)`，并把新 conv_state（`conv_input` 末尾 `conv_kernel_size - 1` 个）写回 `cache[0]`。

```python
conv_state = cache[0] if (cache is not None and cache[0] is not None) else mx.zeros((B, self_layer.conv_kernel_size - 1, self_layer.conv_dim), dtype=inputs.dtype)
# ... mask 处理 qkv ...
conv_input = mx.concatenate([conv_state, qkv], axis=1)
capture.conv_data.append((conv_input, self_layer.conv_kernel_size))
if cache is not None:
    cache[0] = conv_input[:, -(self_layer.conv_kernel_size - 1):]
```

读懂这段的关键是搞清 `conv_input` 的结构（沿序列维 axis=1）：

```
conv_input = [ conv_state(K-1个) , qkv_0, qkv_1, ..., qkv_{S-1} ]
                ↑ 旧状态            ↑ 本批 S 个 token 的 qkv
```

其中 `S = inputs.shape[1]`（验证时 `S = bs`），`K = conv_kernel_size`，所以 `conv_input` 总长 `(K-1) + S`。

- `capture.conv_data.append((conv_input, K))`：把**整条拼接序列**和卷积核大小都存下，rollback 切片时要用 `K`。
- `cache[0] = conv_input[:, -(K-1):]`：写回**处理完整批 S 个 token 后**的卷积状态——取末尾 `K-1` 个（即 `qkv_{S-K+1} ... qkv_{S-1}` 这段最近历史）。这正是「**全量前进后**」的脏状态，rollback 时要把它改回「只前进 `accepted+1` 步」的样子。

**捕获点 ②：增量更新输入 `gdn_inputs`**

[dflash/model_mlx.py:338-345](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L338-L345) —— 取出旧 delta state（检查点 `init_state`），把重建所需的 `q,k,v,a,b` 连同层参数与 `init_state`、`mask` 一起抄录，再用它们跑一遍真正的 `gated_delta_update` 得到 `out, new_state`。

```python
state = cache[1] if cache else None
# ... q,k,v 归一化 ...
capture._gdn_inputs.append((q, k, v, a, b, self_layer.A_log, self_layer.dt_bias, state, mask))
out, new_state = _gd_mod.gated_delta_update(
    q, k, v, a, b, self_layer.A_log, self_layer.dt_bias, state, mask, use_kernel=True
)
if cache is not None:
    cache[1] = new_state
```

这里 `state = cache[1]`（调用前的 delta state）被作为元组的第 8 个元素存进 `_gdn_inputs`，它是 rollback 的**重放起点（检查点）**。`cache[1] = new_state` 写回的则是「全量前进后」的脏 delta state。

> 注意 `gated_delta_update` 的返回 `(out, new_state)` 中 `out` 是本批的输出（喂给后续 `norm`/`out_proj`），`new_state` 是递归后的新状态。捕获版与原版的差异**仅在于多两行 `append`**，`out` 的计算完全一致，所以对模型输出零影响。

**捕获的总账**：经过一次验证前向，对**每一个 GDN 层**，捕获器都攒下了：

| 列表 | 元素（每个 GDN 层一条） | 用途 |
|---|---|---|
| `conv_data` | `(conv_input, K)` | rollback 切片重建 conv_state |
| `_gdn_inputs` | `(q, k, v, a, b, A_log, dt_bias, init_state, mask)` | rollback 重放重建 delta state |

两个列表的下标天然按「GDN 层在前向中出现的顺序」对齐——这一点是 4.3 节 `rollback` 用 `j` 同时遍历两者的前提。

#### 4.2.5 源码精读：clear 与 close 的收尾

捕获每轮都要重置（因为每轮的检查点和原材料都不同）：

[dflash/model_mlx.py:357-359](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L357-L359) —— `clear` 清空两个捕获列表。

```python
def clear(self):
    self.conv_data.clear()
    self._gdn_inputs.clear()
```

生成结束后必须**还原补丁**，否则整个进程里 `GatedDeltaNet` 永远是「被改过的版本」，会影响其他正常使用 `mlx_lm` 的代码：

[dflash/model_mlx.py:361-372](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L361-L372) —— `close` 幂等还原：仅当当前类方法仍是自己注入的那个时才还原（防止覆盖别人后改坏），还原后置空并释放锁。

```python
def close(self):
    if self._closed:
        return
    try:
        if self._gdn_cls is not None and self._gdn_cls.__call__ is self._patched_call:
            self._gdn_cls.__call__ = self._orig_call
    finally:
        self._closed = True
        self._gdn_cls = None
        self._orig_call = None
        self._patched_call = None
        _GDN_PATCH_LOCK.release()
```

`self._gdn_cls.__call__ is self._patched_call` 这道**身份检查**很重要：如果在我们 patch 期间，别的代码又 patch 了一次 `__call__`，直接无脑还原会覆盖别人的补丁。身份检查保证「只还原自己装的那一个」。

#### 4.2.6 代码实践：观察捕获节奏

1. **实践目标**：理解「每轮验证 = 一次 clear + 每层 GDN 一次 append」的捕获节奏。
2. **操作步骤**：
   - 在 [dflash/model_mlx.py:358](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L358) 的 `clear()` 内临时加一行 `print(f"[clear] round cleared")`（**示例代码，仅用于观察，验证后请删除**）。
   - 在 [dflash/model_mlx.py:342](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L342) 的 `capture._gdn_inputs.append(...)` 后加一行 `print(f"[capture] layer, total_gdn_inputs={len(capture._gdn_inputs)}")`（**示例代码**）。
   - 用 README 的 MLX 示例（[README.md:148-161](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L148-L161)）把模型换成 `Qwen/Qwen3.5-4B` 跑一次短生成。
3. **需要观察的现象**：每轮 decode 应出现**恰好一次** `[clear]`，随后出现**固定次数**的 `[capture]`（次数 = 模型里 GDN 层的数量，每轮相同）。
4. **预期结果**：`[capture]` 的累计计数在每轮内从 1 涨到「GDN 层数」，下一轮被 `clear` 重置后重新从 1 涨起。若你看到 `[capture]` 数量逐轮累积不归零，说明 `clear()` 没被调用——那就是 bug。**待本地验证**（需 Apple 芯片 + GDN 内核）。

#### 4.2.7 小练习与答案

- **练习 1**：`close()` 为什么用 `_closed` 标志做成幂等？如果去掉它会怎样？
  - **答案**：`stream_generate` 的 `finally`（L580-582）保证调一次 `close()`，但用户代码或异常重入可能多次触发。没有幂等保护时，第二次 `close()` 会再次 `release()` 一个已经释放的锁，`RLock` 抛 `RuntimeError`，或把别人持有的锁误释放。`_closed` 保证「还原 + 释放」只发生一次。

- **练习 2**：捕获版 `_capturing_gdn_call` 如果漏抄了 `init_state`（元组里少了 `state`），rollback 会出什么问题？
  - **答案**：rollback 重放 delta state 时就没有「干净的检查点」作起点，只能从零状态或错误状态重放，重建出的 `state_{accepted+1}` 完全错误，等于把 target 的 GDN 层记忆打乱，后续生成全部错乱。`init_state` 是整个重放机制的基石。

- **练习 3**：为什么 `clear()` 用 `list.clear()` 而不是重新 `self.conv_data = []`？
  - **答案**：闭包 `_capturing_gdn_call` 通过 `capture` 引用的是**同一个 list 对象**（`capture = self`，`capture.conv_data`）。若重新赋值新 list，闭包里持有的还是旧 list，append 进旧 list、rollback 读新 list，两边对不上。`list.clear()` 原地清空，引用不变，是正确的做法。

---

### 4.3 _GDNStateCapture.rollback：重放重建 conv_state 与 delta state

#### 4.3.1 概念说明

`rollback` 是消费 4.2 节攒下的「原材料」、把脏状态改回干净状态的地方。它要同时处理**整组 target 缓存**里的两种层：

- **可裁剪层**（注意力）：直接 `c.trim(trim)`，删尾部 `trim` 格——和 u3-l2 的 `_trim_recent_cache` 目的相同，只是这里直接用 `mlx_lm` 自带的 `trim`。
- **GDN 层**：重放重建，分 conv_state 与 delta state 两部分。

本节的两个核心公式务必记牢（也是本讲综合实践要画图说明的）：

- **delta state 重建**：从检查点 `init_state` 出发，只用前 `accepted+1` 个 token 的 `q,k,v,a,b` 重放 `gated_delta_update`，得到 `state_{accepted+1}`。
- **conv_state 重建**：从捕获的 `conv_input` 里切出「以第 `accepted` 个 token 收尾」的那段滑窗 `conv_input[:, accepted+1 : accepted+K]`（恰好 `K-1` 个）。

#### 4.3.2 核心流程：rollback 的逐层分流

```
rollback(cache, accepted, trim):
  n = accepted + 1                      # 要保留的 token 数（提交数）
  断言：不可裁剪缓存数 == 捕获的 GDN 输入数
  j = 0                                 # GDN 原材料的下标
  for 每层缓存 c in cache:
      if c.is_trimmable():              # 注意力层
          c.trim(trim)                  # 删尾部 trim 格
      else:                             # GDN 层
          # —— delta state 重建 ——
          (q,k,v,a,b,A_log,dt_bias,init_state,mask) = _gdn_inputs[j]
          _, state = gated_delta_update(q[:,:n], k[:,:n], v[:,:n],
                                        a[:,:n], b[:,:n],
                                        A_log, dt_bias, init_state,
                                        mask 的前 n 列)
          c.cache[1] = state            # 写回重建后的 delta state
          # —— conv_state 重建 ——
          (conv_input, K) = conv_data[j]
          c.cache[0] = conv_input[:, accepted+1 : accepted+K]   # K-1 个
          j += 1
```

为什么 delta state 重放只取前 `n = accepted+1` 个、conv_state 切片也落在 `accepted`？因为提交的 token 恰好是验证前向里**最前** `accepted+1` 个位置（锚点 + `accepted` 个被接受候选）；它们的累积状态就是我们要保留的状态，其后的 `trim = bs - accepted - 1` 个（被拒候选）的状态必须丢弃。

#### 4.3.3 源码精读：断言与逐层循环

[dflash/model_mlx.py:374-384](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L374-L384) —— `rollback` 入口先做一道**数量对齐断言**：不可裁剪缓存的个数必须等于捕获的 GDN 输入组数；随后按 `is_trimmable()` 分流。

```python
def rollback(self, cache, accepted, trim):
    n_non_trimmable = sum(1 for c in cache if not c.is_trimmable())
    assert n_non_trimmable == len(self._gdn_inputs), (
        f"non-trimmable cache count ({n_non_trimmable}) != "
        f"captured GDN inputs ({len(self._gdn_inputs)}); "
        "DFlash MLX rollback assumes every non-trimmable cache is a GatedDeltaNet layer"
    )
    j = 0
    for c in cache:
        if c.is_trimmable():
            c.trim(trim)
```

这条断言是整个机制最硬的假设：**每一个不可裁剪的缓存，都必然是一个 GDN 层，且恰好对应一组捕获输入**。它的潜台词是——dflash 不支持「不可裁剪但也不是 GDN」的第三种层。断言失败说明遇到了未预期的层类型，与其错乱不如立刻崩。

#### 4.3.4 源码精读：delta state 重放（核心公式之一）

[dflash/model_mlx.py:385-394](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L385-L394) —— GDN 分支：取第 `j` 组原材料，`n = accepted + 1`，把 `q,k,v,a,b,mask` 都切片到前 `n` 个，从 `init_state` 重放 `gated_delta_update`，把得到的 `state` 写回 `c.cache[1]`。

```python
else:
    q, k, v, a, b, A_log, dt_bias, init_state, mask = self._gdn_inputs[j]
    n = accepted + 1
    _, state = _gd_mod.gated_delta_update(
        q[:, :n], k[:, :n], v[:, :n], a[:, :n], b[:, :n],
        A_log, dt_bias, init_state,
        None if mask is None else mask[:, :n],
        use_kernel=True,
    )
    c.cache[1] = state
```

解读：

- `init_state` 是验证前向**调用前**的 delta state（4.2.4 抄录的检查点）。它是「干净」的——尚未被本批任何 token 污染。
- `q[:, :n]` 等只取前 `n = accepted+1` 个 token 的输入，等价于「假装这次验证只来了被接受的那 `accepted+1` 个 token」。
- `gated_delta_update(..., init_state, ...)` 从干净起点重放这 `n` 步递归，得到的 `state` 就是 `state_{accepted+1}`——**恰好等于「只见过被接受 token」的状态**。
- 写回 `c.cache[1]`，覆盖掉之前写回的脏 `new_state`（4.2.4 的 `cache[1] = new_state` 是全量前进后的脏值）。
- `mask` 同步切到前 `n` 列（`mask[:, :n]`），保证重放的注意力掩码与截断后的序列一致；`mask is None` 时保持 `None`。

> 这正是 4.1.2 用数学描述的「从 `state_0` 重放到 `state_{accepted+1}`」在代码里的落地。

#### 4.3.5 源码精读：conv_state 切片（核心公式之二）

[dflash/model_mlx.py:395-397](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L395-L397) —— 取第 `j` 条 `conv_input` 和卷积核大小 `K`，切出 `conv_input[:, accepted+1 : accepted+K]` 写回 `c.cache[0]`，`j` 自增。

```python
    conv_input, K = self.conv_data[j]
    c.cache[0] = conv_input[:, accepted + 1 : accepted + K]
    j += 1
```

这一行的下标是本讲最需要你「在草稿纸上推一遍」的地方。回顾 `conv_input` 的结构（4.2.4）：

```
conv_input 的序列维下标：
  0 .. K-2          : 旧 conv_state（K-1 个 token）
  K-1               : qkv_0   （验证批的第 0 个 token = 锚点）
  K                 : qkv_1
  ...
  K-1 + i           : qkv_i   （第 i 个 token）
  K-1 + accepted    : qkv_{accepted}
  ...
```

卷积状态的定义是「**以当前 token 收尾、往前回看 `K-1` 个**」的滑窗。处理完第 `accepted` 个 token 后，正确的 conv_state 应是「以下标 `K-1+accepted` 收尾」的 `K-1` 个元素，即：

\[
\text{conv\_state}_{\text{new}} = \text{conv\_input}\,[\; (K{-}1{+}\text{accepted}) - (K{-}1) + 1 \;:\; (K{-}1{+}\text{accepted}) + 1\;]
\]

化简下标：

\[
\text{conv\_state}_{\text{new}} = \text{conv\_input}\,[\;\text{accepted}+1 \;:\; \text{accepted}+K\;]
\]

正好就是 `conv_input[:, accepted + 1 : accepted + K]`，切片长度 `(accepted+K) - (accepted+1) = K-1`，与 conv_state 应有的定长 `K-1` 完全吻合。代码与数学推导一致——这就是为什么切片端点偏偏是 `accepted+1` 和 `accepted+K`。

#### 4.3.6 代码实践：手算一个最小例子

1. **实践目标**：用一个具体的 `bs / accepted` 组合，亲手算出 conv_state 切片，验证 `conv_input[:, accepted+1 : accepted+K]` 的正确性。
2. **操作步骤**：
   - 设 `conv_kernel_size K = 4`（故 conv_state 长 `K-1 = 3`），验证批 `bs = 5`（故 `conv_input` 总长 `3 + 5 = 8`），`accepted = 2`（故 `trim = bs - accepted - 1 = 2`，提交 `n = accepted+1 = 3` 个 token）。
   - 画出 `conv_input` 的 8 个下标，标出哪些来自旧 conv_state、哪些是 `qkv_0..qkv_4`。
   - 按「以 `qkv_{accepted}=qkv_2` 收尾、回看 3 个」算出应保留的下标区间，再和 `accepted+1 : accepted+K = 3 : 6` 对照。
3. **需要观察的现象**：纸笔推导，无运行。
4. **预期结果**：

   ```
   conv_input 下标:  0    1    2    3     4     5     6     7
                     |--- 旧 conv_state ---|---- qkv (bs=5) ----|
                     cs0  cs1  cs2  qkv0  qkv1  qkv2  qkv3  qkv4
   accepted=2 → 收尾于 qkv2（下标 5），回看 3 个 → 下标 3,4,5
   代码切片 accepted+1:accepted+K = 3:6 → 下标 3,4,5 ✓
   保留元素 = [qkv0, qkv1, qkv2]，正好是「提交的前 3 个 token」收尾的滑窗。
   ```

   对照 delta state：重放取 `n=3` 个（`q,k,v,a,b` 的前 3 列），从 `init_state` 跑出 `state_3`。两部分都用「前 `accepted+1=3` 个 token」重建，语义一致——这就是「停在提交点」的不变量。

5. **若想实跑**：在 Apple 芯片 + GDN 内核环境下，用 4.2.6 的打印法额外在 [L387](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L387) 打印 `accepted, n, K`，验证手算。**待本地验证**。

#### 4.3.7 小练习与答案

- **练习 1**：`accepted = bs - 1`（全部候选都被接受）时，`rollback` 会发生什么？conv_state 切片是哪一段？
  - **答案**：`trim = bs - (bs-1) - 1 = 0`，在 `stream_generate` 里 `if trim > 0` 守卫（L562）会跳过 `rollback`，根本不调用。即便强行调用，`n = accepted+1 = bs`，delta state 重放全部 `bs` 个、conv_state 切片 `conv_input[:, bs : bs+K-1]`（即末尾 `K-1` 个，与 4.2.4 写回的 `cache[0]` 完全一致），等于「不改动」。所以全接受时回滚是 no-op。

- **练习 2**：为什么 delta state 重放要传 `mask[:, :n]` 而不是整个 `mask`？
  - **答案**：`gated_delta_update` 内核按序列维逐位更新，`q,k,v,a,b` 都已截到前 `n` 列，`mask` 必须同步截到前 `n` 列才能对齐，否则形状不匹配或掩码错位，重放结果错误。

- **练习 3**：`rollback` 里可裁剪层用的是 `c.trim(trim)`，而 `stream_generate` 普通路径用的是 `_trim_recent_cache(target_cache, trim)`。两者为什么不统一？
  - **答案**：`_trim_recent_cache`（[L243-258](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L243-L258)）是为「全是可裁剪层」的普通模型写的，它内部对 `RotatingKVCache` 做了 `_temporal_order` 手动切片。而进入 `_GDNStateCapture.rollback` 的前提是「target 不可整体裁剪（含 GDN 层）」，此时混在里面的可裁剪层用 `mlx_lm` 原生 `c.trim()` 即可正确工作，因此这里直接用 `c.trim(trim)`。两条路径各自针对自己前提下的缓存构成。

---

### 4.4 stream_generate 中的分流与 _capture 生命周期

#### 4.4.1 概念说明

前三节把 `_GDNStateCapture` 的内部讲透了，本节把它放回 `stream_generate` 的主循环里，看清「**它在何时被创建、何时 clear、何时 rollback、何时 close**」——也就是它的生命周期如何与 u3-l2 的 decode 循环咬合。这一节是把本讲和 u3-l2 缝合的关键。

#### 4.4.2 核心流程：_capture 在一轮 decode 里的四个时刻

```
进入 stream_generate:
  L453  _target_can_trim = can_trim_prompt_cache(target_cache)
  L454  if 不能裁剪 且 没有 GDN:  RuntimeError        ← 4.1 的安全网
  L459  _capture = _GDNStateCapture() if 不可裁剪 else None   ← 创建（构造即 patch）
  ...
每轮 decode:
  L509  if _capture is not None: _capture.clear()     ← 清空上一轮原材料
  L511  target 验证前向                                  ← 被 patch 的 __call__ 自动抄录
  ...
  L561  trim = bs - accepted - 1
  L562  if trim > 0:
  L563      if _target_can_trim:  _trim_recent_cache(...)   ← u3-l2 普通路径
  L565      elif _capture is not None:
  L566          _capture.rollback(target_cache, accepted, trim)  ← 本讲 GDN 路径
  L567  hidden = hidden[:, :accepted+1, :]
  ...
退出 stream_generate (finally):
  L580  if _capture is not None: _capture.close()     ← 还原补丁 + 释放锁
```

四个时刻对应 `_GDNStateCapture` 的四个方法，完美闭环：**构造（patch）→ clear → rollback → close**。

#### 4.4.3 源码精读：创建与安全网

[dflash/model_mlx.py:453-459](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L453-L459) —— 复用 4.1.3 已展示的片段：能力检查 + 按 `_target_can_trim` 决定是否创建 `_capture`。

注意 `_capture` 只在「target 不可裁剪」时才创建（且 `_HAS_GDN` 已由 L454 保证为 `True`）。普通模型这里 `_capture is None`，后续所有 `_capture.xxx` 调用都被 `if _capture is not None` 守卫跳过，零开销。

#### 4.4.4 源码精读：每轮 clear——在验证前清空

[dflash/model_mlx.py:509-516](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L509-L516) —— 每轮 decode 里，先 `_capture.clear()` 清空上一轮原材料，再做 target 验证前向（此时被 patch 的 `__call__` 抄录本轮原材料）。

```python
if _capture is not None:
    _capture.clear()
with mx.stream(generation_stream):
    verify_input = mx.concatenate([mx.array([[tokens[-1]]]), draft_tokens], axis=1)
    logits = model(verify_input, target_cache)          # ← 本轮原材料在此被抄录
    hidden = mx.concatenate(model._hidden_states, axis=-1)
    target_tokens = sampler(logits)
mx.async_eval(target_tokens, hidden)
```

为什么 `clear()` 必须在 `model(...)` **之前**？因为本轮验证要产生本轮的「检查点 + 原材料」，上轮的已经过期（上轮的 `init_state` 是上轮调用前的状态，与本轮无关）。若不清空，`conv_data` / `_gdn_inputs` 会逐轮累积，rollback 时下标 `j` 对不上，状态重建全错。

#### 4.4.5 源码精读：rollback 的调用——与 trim 分支对应

[dflash/model_mlx.py:561-567](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L561-L567) —— 计算 `trim`，若 `trim > 0` 则按 `_target_can_trim` 二选一：可裁剪走 `_trim_recent_cache`（u3-l2），不可裁剪走 `_capture.rollback`（本讲）；无论哪条，`hidden` 都切片到 `accepted+1`。

```python
trim = bs - accepted - 1
if trim > 0:
    if _target_can_trim:
        _trim_recent_cache(target_cache, trim)
    elif _capture is not None:
        _capture.rollback(target_cache, accepted, trim)
hidden = hidden[:, :accepted + 1, :]
```

这是本讲与 u3-l2 的**直接交汇点**：

- `_target_can_trim` 为 `True`：u3-l2 主线，普通模型，`_trim_recent_cache` 删尾部 `trim` 格。
- `_target_can_trim` 为 `False`（含 GDN 层）：本讲主线，`_capture.rollback` 重放重建。

两者要达成的**同一个不变量**是：「**target 的所有层状态都退回到『只见过提交的 `accepted+1` 个 token』**」。普通层靠删尾巴达成，GDN 层靠重放达成，殊途同归。

注意 `rollback` 收到的第三个参数是 `trim`，但 4.3 节我们看到它内部 GDN 分支其实**没用 `trim`**（用的是 `accepted`），只有可裁剪分支用了 `c.trim(trim)`。也就是说：对 GDN 层，「回滚 `trim` 个被拒 token」被等价地实现成「重建到前 `accepted+1` 个」。`trim` 与 `accepted+1` 满足 `trim + (accepted+1) = bs`，是同一信息的两种表达。

还有一点要和 u3-l2 对齐：`hidden = hidden[:, :accepted+1, :]`（L567）把 target 隐藏状态也切片到 `accepted+1`。这与缓存回滚共享同一个不变量——隐藏状态和缓存都要「停在提交点之前」，下一轮草稿才能从正确的上下文续写。u3-l2 已讲过隐藏切片，本讲只强调它与 GDN 回滚**同步发生**。

#### 4.4.6 源码精读：finally 里的 close——保证还原

[dflash/model_mlx.py:580-582](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L580-L582) —— 无论生成正常结束还是中途异常，`finally` 都会调 `_capture.close()`，确保 monkey-patch 被还原、全局锁被释放。

```python
finally:
    if _capture is not None:
        _capture.close()
```

把 `close()` 放在 `finally` 而非正常路径末尾，是**防御性编程**的体现：即便生成中途抛异常（OOM、用户中断等），补丁也会被还原，不会让进程里的 `GatedDeltaNet` 永久停在「被改过的版本」、锁永久不释放（那会导致后续所有生成死锁）。

#### 4.4.7 代码实践：对照两条回滚路径

1. **实践目标**：把本讲的 GDN 回滚与 u3-l2 的普通回滚并排对照，确认它们达成同一不变量。
2. **操作步骤**：
   - 在笔记里画一张两列对照表：左列「普通可裁剪模型」、右列「含 GDN 的混合模型」，行分别为「创建捕获器」「每轮清空」「回滚调用」「收尾还原」。
   - 在 [dflash/model_mlx.py:561-567](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L561-L567) 处，标注左列走 L563、右列走 L565-566。
   - 回答：为什么右列即使有可裁剪的注意力层，也由 `_capture.rollback` 内部统一处理，而不是先 `_trim_recent_cache` 再 `rollback`？
3. **需要观察的现象**：纸笔分析，无运行。
4. **预期结果**：对照表应体现——两列的「目标不变量」完全相同（状态退回到提交点），差异只在「手段」（删尾巴 vs 重放重建）。右列的注意力层之所以交给 `rollback` 内部的 `c.trim(trim)` 而非单独 `_trim_recent_cache`，是因为混合模型整组缓存「不可整体裁剪」，`stream_generate` 不会再调 `_trim_recent_cache`（它只在 `_target_can_trim` 为真时调用）；`rollback` 用一个循环同时处理两种层，保证所有层一致回滚到同一位置。

#### 4.4.8 小练习与答案

- **练习 1**：若把 `_capture.clear()`（L509-510）误删，第三轮起会发生什么？
  - **答案**：`conv_data` / `_gdn_inputs` 逐轮累积，`rollback` 用 `j` 取第 `j` 组原材料时会取到**第一轮**的（下标永远从 0 开始但列表越来越长，且 `j` 只在 GDN 层数范围内有效），导致用错误的检查点和原材料重建状态，target 记忆错乱。`clear()` 是保证「每轮原材料与本轮验证严格对应」的必需步骤。

- **练习 2**：`rollback` 的 GDN 分支不用 `trim` 参数，那为什么 `stream_generate` 还要把 `trim` 传给它？
  - **答案**：`rollback` 是「**整组缓存**」的统一回滚入口，它内部的可裁剪分支（注意力层）需要 `trim` 来 `c.trim(trim)`。GDN 分支虽用 `accepted`，但 `trim` 与 `accepted+1` 满足 `trim = bs - accepted - 1`，是同一信息的两种表达，传 `trim` 让接口与 `_trim_recent_cache(cache, trim)` 对称、易读。

- **练习 3**：为什么 `_capture` 的创建（L459）不在 `try` 块里，而 `close()`（L580）在 `finally` 里？
  - **答案**：创建在 `try` 之外——若创建（即 `_patch`）本身失败（如 `qwen3_5` 不可用），异常直接抛给调用者，无需 `close`（还没 patch 成功，没东西要还原，`__init__` 内部已处理锁释放）。`close()` 在 `finally` 是因为一旦 patch 成功并进入 `try`，后续任何异常都必须还原补丁。两者分工：创建失败由 `__init__` 自清理，创建成功后由 `finally` 兜底。

---

## 5. 综合实践

把本讲的所有要点串成一张「**GDN 回滚全景草图**」。建议在纸上或绘图工具里完成，这是检验你是否真正读懂本讲的最佳方式。

**任务**：给定 `block_size = 16`（故 `bs = 16`）、`conv_kernel_size = 4`、某一轮 `accepted = 5`（故 `trim = 16 - 5 - 1 = 10`，提交 `accepted+1 = 6` 个 token）。画出并标注以下内容：

1. **验证批的结构**：`verify_input` 沿序列维的 16 个位置，标出哪 1 个是锚点、哪 15 个是候选、哪 6 个被提交、哪 10 个被拒。
2. **conv_input 的结构**：长度 `3 + 16 = 19` 的序列，标出前 3 个（旧 conv_state）与后 16 个（`qkv_0..qkv_15`）；用方框圈出 rollback 后 `c.cache[0]` 应保留的切片 `conv_input[:, 6 : 9]`（即 `accepted+1 : accepted+K = 6:9`，对应以 `qkv_5` 收尾的 3 个滑窗元素）。
3. **delta state 的重放**：画一个从 `init_state`（检查点）出发、经过 6 步递归到 `state_6` 的箭头链，标注每步用 `q[:, :6], k[:, :6], ...` 的前 6 列；旁边画一条「全量前进到 `state_16` 再想退回去——退不动」的对比线，直观说明「删不掉、只能重放」。
4. **分流与生命周期**：在图侧标注本轮 decode 的四个时刻——`clear()`（清空）→ 验证前向（抄录）→ `rollback()`（重建）→ 生成结束 `close()`（还原）。
5. **报错路径说明**：在图底写一句话——当 `_HAS_GDN = False` 且 target 缓存不可裁剪时，程序在 [L454-458](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L454-L458) 抛 `RuntimeError`，因为「既不能 trim、又没有 GDN 重放能力」，继续生成会让被拒 token 的脏递归状态永久残留，导致静默错乱，故 fail-fast 是必要的。

**自检标准**：

- conv_state 切片 `[6:9]` 的长度恰为 3（= `K-1`），且收尾于 `qkv_5`（= 第 `accepted` 个 token）。
- delta state 重放步数 = 6（= `accepted+1`），与 conv_state 切片「保留前 6 个 token 的记忆」语义一致。
- 能说出「为什么不能用 `trim`」：GDN 状态是递归累积的，被拒 token 已融进整个 `state` 矩阵和 conv_state 滑窗，无格可删。

> 若你有 Apple 芯片 + GDN 内核环境，可额外实跑 [README.md:148-161](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L148-L161) 的 MLX 示例（模型用 `Qwen/Qwen3.5-4B`），用 4.2.6 的打印法观察 `accepted` 与 `trim` 的真实分布，验证草图。**待本地验证**。

---

## 6. 本讲小结

- **GDN 层的状态是「递归累积」而非「逐格存储」**：卷积状态（定长 `K-1` 滑窗）+ delta 递归状态（一个不断被增量更新的矩阵）都融进了所有历史 token 的信息，无法用 `trim` 删尾巴，`can_trim_prompt_cache` 因此对含 GDN 的混合模型返回 `False`。
- **解法是「重放重建」而非「删除」**：既然删不掉，就从干净的检查点 `init_state` 出发，只用被接受的前 `accepted+1` 个 token 重跑递归，重新算出正确的中间状态。
- **`_GDNStateCapture` 靠 monkey-patch 采集原材料**：构造时把 `GatedDeltaNet.__call__` 换成「功能等价但顺手抄录」的版本，在验证前向里把每个 GDN 层的 `conv_input`（含卷积核大小）和 `gdn_inputs`（`q,k,v,a,b` + 检查点 `init_state`）抄进两个列表；全程对模型输出零影响。
- **rollback 的两个核心公式**：delta state = 从 `init_state` 重放前 `accepted+1` 列得到；conv_state = `conv_input[:, accepted+1 : accepted+K]`（切片长度恰为 `K-1`，收尾于第 `accepted` 个 token）。两者都用「前 `accepted+1` 个 token」重建，语义统一于「停在提交点」。
- **分流由 `_target_can_trim` × `_HAS_GDN` 决定**：可裁剪走 `_trim_recent_cache`（u3-l2），不可裁剪且有 GDN 走 `_capture.rollback`（本讲），不可裁剪又无 GDN 直接 `RuntimeError`（fail-fast，避免静默错乱）。
- **生命周期严格咬合 decode 循环**：构造（patch）→ 每轮 `clear` → 拒绝时 `rollback` → `finally` 里 `close`（幂等还原补丁 + 释放全局锁）；`_GDN_PATCH_LOCK` 保证同一进程不会并发改写类方法。

---

## 7. 下一步学习建议

本讲把 MLX 后端在「**混合架构 / 不可裁剪缓存**」路径下的状态回滚讲透了，至此 MLX 三部曲（u3-l1 草稿模型与配置 → u3-l2 流式生成与普通缓存回滚 → u3-l3 GDN 状态捕获与回滚）完整闭合，DFlash 的三种后端、两条推理实现、两类回滚机制你都已读穿。

接下来推荐两条路：

- **转向评测与工程化（u3-l4 / u3-l5）**：如果你更关心「DFlash 到底快多少、怎么测」，直接进入 **u3-l4《基准评测框架：数据集与 CLI》** 和 **u3-l5《多后端评测运行器与指标》**。它们讲 `benchmark.py` 的数据集下载与 JSONL 原子缓存、Transformers 分布式评测（torchrun/NCCL）、vLLM/SGLang 并发 HTTP 评测，以及加速比与接受长度直方图的计算——你会发现本讲的 `accepted` 正是那里「平均接受长度」指标的来源。
- **横向对比三种后端的投机解码实现**：回头对比 `model.py`（Transformers，用 `DynamicCache.crop` 回滚）与本讲的 `model_mlx.py`（用 `_trim_recent_cache` / `_GDNStateCapture.rollback`），体会「同一算法在不同框架、不同层类型下，回滚手段被迫不同」的工程取舍。这能帮你理解为什么 Transformers 参考实现（u2-l5）要把 Qwen3.5 混合架构排除在白名单外——它没有本讲这套 GDN 状态回滚能力。

如果你打算做二次开发，一个有价值的练习是：参照本讲的 `_GDNStateCapture`，思考若未来出现「**第三种不可裁剪层**」（既非注意力、也非 GDN），现有 `rollback` 的 `assert n_non_trimmable == len(self._gdn_inputs)` 会如何触发、需要扩展哪些捕获与重建逻辑。
