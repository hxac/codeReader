# 测试数据生成与数值对拍

## 1. 本讲目标

TileKernels 的每一个算子都要和「纯 PyTorch 参考实现」逐位或近似对拍，才能保证 TileLang 编译出的 CUDA kernel 没有写错。这种对拍需要两件东西：

- 一套**参数与数据生成器**，负责枚举各种形状、数据类型、边界条件；
- 一套**数值比较工具**，负责把 kernel 输出和参考输出比出「对/错」。

本讲深入 `tile_kernels/testing/` 下的两个基础设施文件：`generator.py`（生成器）和 `numeric.py`（数值比较）。读完本讲，你应当能够：

1. 说清 `generate_*` 系列函数各自生成什么、`TK_FULL_TEST` 环境变量如何扩展测试覆盖面；
2. 区分三种数值比较工具——`assert_equal`（位精确）、`calc_diff`（浮点相似度）、`check_bias`（统计检验）——的适用场景，并知道为什么某个测试该用哪一个；
3. 推导 `check_bias` 里 `10/sqrt(n)` 这个阈值的统计学来源。

承接：u3-l2 已经展示过「参数生成器 + 数据生成器 + 参考实现 + 断言」的对拍三件套范式，并用 `count_bytes` 算过带宽。本讲不再重复范式本身，而是把放大镜对准范式用到的两个工具文件本身。

## 2. 前置知识

- **对拍（differential testing）**：同一个输入喂给「被测实现」和「参考实现」，比较二者输出。参考实现通常慢但绝对正确（这里就是纯 PyTorch），被测实现快但可能有 bug（TileLang 编译的 kernel）。
- **位精确（bitwise exact）**：两个张量的底层字节完全一致。浮点数 `a == b` 为 True 当且仅当它们的位模式相同；但 `NaN`、带符号的 0、denormal 这些特殊情况要靠「按字节比」才稳妥。
- **pytest 参数化**：`@pytest.mark.parametrize('params', [...])` 会为列表里每一组参数生成一个独立测试用例。生成器函数的职责就是产出这个参数列表。
- **二项分布与中心极限定理（CLT）**：n 次独立的 0/1 抛硬币，正面次数服从 \(B(n, 0.5)\)，标准差 \(\sqrt{n}/2\)；n 够大时近似正态。本讲会用到。
- **RMSNorm / scaling factor（SF）**：量化里的概念，u4-l1 已讲过。本讲 `generate_rand_float` 专门造覆盖大动态范围的输入来压力测试 SF。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tile_kernels/testing/generator.py` | 参数与数据生成器。产出测试用的形状组合（token 数、hidden 维、专家数等）和具体输入张量（随机数、E5M6 特殊值、宽动态范围浮点）。 |
| `tile_kernels/testing/numeric.py` | 数值比较工具。`assert_equal` 位精确断言、`calc_diff` 浮点相似度、`check_bias` 舍入偏置统计检验、`count_bytes` 统计读写带宽字节数。 |
| `tile_kernels/utils.py` | `align`/`ceil_div` 等小工具，生成器里用来把 token 数对齐到块大小。 |
| `tile_kernels/config.py` | `get_device_num_sms` 探测硬件 SM 数，供 `generate_num_sms` 用。 |
| `tests/quant/test_per_token_cast.py` | 示范生成器与三种数值工具同时出现的真实测试。 |
| `tests/moe/test_topk_gate.py` | 示范 `generate_num_tokens` + `assert_equal` 的极简对拍。 |

本讲覆盖两个最小模块：**testing/generator** 与 **testing/numeric**。

## 4. 核心概念与源码讲解

### 4.1 参数与数据生成器 generator.py

#### 4.1.1 概念说明

算子的正确性与形状、数据类型、边界条件强相关。例如：

- token 数恰好是块大小的整数倍 vs. 不是整数倍（边界）；
- token 数为 0（空输入，kernel 不能崩）；
- hidden 维能否被某个对齐值整除；
- MoE 的「专家数能被 EP 卡数整除」这种约束。

不可能手写所有组合，于是用一个生成器函数集中枚举。生成器的核心设计是**双档位**：

- **默认档**：跑得快、覆盖主流配置，每次 `pytest` 都执行。
- **TK_FULL_TEST 档**：加上边界与极端配置（空输入、单 SM、超大专家数等），用于 CI 压力测试，靠环境变量 `TK_FULL_TEST=1` 打开。

`TK_FULL_TEST` 只对**正确性用例**生效（`is_benchmark=False`），benchmark 用例即使开了也保持精简——因为基准只要代表性的点，不需要扫遍所有边界。

#### 4.1.2 核心流程

生成器函数的通用调用方式：

```text
generate_xxx(is_benchmark=False)  → list[dict] 或 Iterable[dict]
        │
        ▼
@pytest.mark.parametrize('params', generate_xxx(is_benchmark=False), ids=make_param_id)
def test_xxx(params):
    数据 = generate_test_data(params)          # 由参数造具体输入张量
    输出  = 被测kernel(数据)
    参考  = torch参考实现(数据)
    assert_equal / calc_diff / check_bias(输出, 参考)
```

两层生成：先「生成参数组合」，测试函数内部再「由参数生成输入张量」。这样参数化列表只描述形状（轻量、可作 pytest id），张量在测试体内才上 GPU 分配。

`TK_FULL_TEST` 的判断在三处复用同一个表达式：

```python
do_full_test = os.getenv('TK_FULL_TEST') in ['1', 'true', 'True']
```

#### 4.1.3 源码精读

**生成 token 数。** 故意选非整除的大数 4001/8001 来撞边界；`TK_FULL_TEST` 再加一个 0（空输入）。`align` 把 token 数向上取整到块大小（默认对齐 1，即原样）：

[tile_kernels/testing/generator.py:10-17](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L10-L17) —— 生成 token 数列表，`do_full_test` 时追加 0，并用 `align` 对齐到 `alignment`。

**生成 hidden 维。** 一组真实模型隐藏维（576/2048/2560/3072/4096/6144/7168，对应主流 LLM 配置），用 `align`（默认 64）过滤掉不满足整除要求的：

[tile_kernels/testing/generator.py:20-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L20-L23) —— 固定 hidden 列表按 `align` 过滤。

**生成 SM 数。** 用于测那些网格随硬件 SM 数伸缩的 kernel（持久化 kernel、group_count）。默认给出「少 20 个 SM」和「满配 SM」两个点；`TK_FULL_TEST` 再加极端的 `1`：

[tile_kernels/testing/generator.py:26-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L26-L32) —— 探测设备 SM 数，构造 `[device_num_sms-20, device_num_sms]`（满配放最后，便于观察）。

**生成 MoE 参数组合。** 这是一个生成器函数（`yield`），笛卡尔积枚举 `num_topk × num_experts × num_ep_ranks`，并加约束 `num_experts % num_ep_ranks == 0`。注意 yield 出的 `num_experts` 已经**除以了** `num_ep_ranks`，即「每卡专家数」：

[tile_kernels/testing/generator.py:35-50](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L35-L50) —— MoE 参数生成器；`TK_FULL_TEST` 扩展 topk/experts/ep_ranks 的边界值，并在开头 yield 一组全 1/0 的极小用例。

`extra_num_topk_list = (1, 7)` 这种「1 和超出常规的 7」就是边界值：topk=1 退化、topk=7 非常规。

**生成 topk_idx 张量。** 用 `@torch.compile` 加速；模拟 EP 路由结果——在「全局专家」上取 top-k，把不属于本卡的专家（`>= num_experts`）掩成 `-1`，再丢掉「全部被掩」的行：

[tile_kernels/testing/generator.py:53-68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L53-L68) —— 造带 `-1` padding 的真实 topk_idx，供 expand_to_fused / group_count 等 dispatch kernel 测试。

**生成 E5M6 特殊值输入。** E5M6 是 TileKernels 自定义的 12 位浮点格式（见 u4-l1/u4-l4）。它的表示范围里有几个「危险点」：最小/最大 subnormal、最小 normal。生成器先给一个随机张量，再依次给填充了这些特殊值的张量，并标注 `is_special` 让测试用更严的阈值：

[tile_kernels/testing/generator.py:71-89](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L71-L89) —— E5M6 特殊值常量与输入生成器；`x[:, -1] = 65024.0` 在每行末位塞最大值，专测上界。

**生成宽动态范围浮点。** 量化的 SF（scaling factor）可能跨越几十个数量级，用普通 `randn` 测不出 SF 路径的 bug。这里先随机采样一个指数 `exp∈[-110,126]`，构造 `sf=2**exp`，再乘到 `randn` 上，使整个张量落在某个量级；并 clamp 到 `finfo.max/8` 防溢出、替换掉偶发的 NaN/Inf：

[tile_kernels/testing/generator.py:92-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L92-L105) —— `generate_rand_float`：按「SF 的指数」均匀采样量级，造覆盖大动态范围的输入。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `TK_FULL_TEST` 如何改变 `generate_moe_params` 的输出集合。

**操作步骤**：

1. 在项目根目录起一个 Python 解释器（需已 `pip install -e ".[dev]"`，见 u1-l2）。
2. 不设环境变量，打印参数组数：
   ```python
   from tile_kernels.testing.generator import generate_moe_params
   params = list(generate_moe_params(is_benchmark=False))
   print('默认档组数:', len(params))
   print('是否含空用例:', any(p['num_send_tokens'] == 0 for p in params))
   print('topk 取值集合:', sorted({p['num_topk'] for p in params}))
   ```
3. 设上环境变量再跑一次（注意：必须在导入 `tile_kernels.testing.generator` **之前**设，或重新启动解释器，因为 `do_full_test` 是函数内每次读取的，实际可即时生效——自行验证）：
   ```python
   import os
   os.environ['TK_FULL_TEST'] = '1'
   from importlib import reload
   import tile_kernels.testing.generator as g
   # generate_moe_params 每次调用都读环境变量，直接调用即可
   full = list(g.generate_moe_params(is_benchmark=False))
   print('FULL档组数:', len(full))
   print('是否含空用例:', any(p['num_send_tokens'] == 0 for p in full))
   print('topk 取值集合:', sorted({p['num_topk'] for p in full}))
   ```
4. 再分别对 `is_benchmark=True` 跑一遍，对比组数。

**需要观察的现象**：默认档没有 `num_send_tokens==0` 的用例，`num_topk` 只有 `{2,6,8,9}`；FULL 档多了 0、1、7 等边界，组数明显变大。`is_benchmark=True` 即便开 FULL 也不会扫全部边界。

**预期结果**：默认档 < FULL 档（正确性）；benchmark 档的两组都比对应正确性档少（benchmark 用 `use_packed_ue8m0` 等过滤条件进一步精简）。

**若本地无 GPU**：`generate_topk_idx` 与 `generate_rand_float` 依赖 `device='cuda'`，会失败；但 `generate_moe_params`/`generate_num_tokens`/`generate_hidden_sizes` 是纯 CPU 逻辑（`generate_num_sms` 需 `get_device_num_sms`，可能依赖 CUDA），可单独验证。无法运行的部分记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`generate_moe_params` 为什么在 yield 前要把 `num_experts` 除以 `num_ep_ranks`？如果不除会发生什么？

> **答案**：yield 的是「每卡负责的专家数」（physical），与 EP 卡数相乘才等于全局专家数（logical）。下游 dispatch/count kernel 是在单卡视角下工作的，它们需要的是每卡专家数。如果不除，专家数会大于实际，造成越界或与参考实现不一致。

**练习 2**：`generate_num_sms` 把 `device_num_sms` 放在列表最后一项，注释说「for convenience of testing」。请推测这给测试带来了什么便利。

> **答案**：很多 kernel 的默认/最优配置假设用满所有 SM。把「满配」放最后，意味着前面的 `device_num_sms-20`（少 SM）用例先跑；若 kernel 在少 SM 时就出错，会尽早暴露，而不必等到最后一项。同时便于在 pytest 输出里快速定位「满配」用例。

**练习 3**：`generate_rand_float` 为什么先采样指数 `exp`、再用 `2**exp` 缩放，而不是直接用一个大范围的均匀分布？

> **答案**：量化的关键变量是 SF 的「量级」（指数），而非尾数。直接用均匀分布会集中在某个量级，测不到「SF 跨越几十个数量级」时的位操作（如 u4-l3 的 `round_sf` 指数域运算）是否正确。按指数均匀采样，等价于对「SF 量级」做均匀覆盖，更有针对性。

### 4.2 位精确对拍 assert_equal

#### 4.2.1 概念说明

对于**无舍入**的算子（如转置、topk 选取、索引搬运），kernel 输出与参考实现必须**逐位相同**——任何一位不同都是 bug。这时用 `assert_equal`：它不只比「值相等」，而是比「底层字节相等」，连 `NaN` 的符号位、denormal、负零都能抓出来。

`assert_equal` 还顺带检查 dtype / shape / stride / device 四项元数据，其中 **stride 检查**值得特别留意：它能抓出「值对了但内存布局错了」的隐性 bug（比如本该行优先却产出了列优先）。

#### 4.2.2 核心流程

```text
assert_equal(x, y):
  1. check_dtype:  x.dtype == y.dtype          # 元数据
  2. check_shape:  x.shape == y.shape
  3. check_stride: x.stride() == y.stride()     # 空张量跳过
  4. check_device: x.device == y.device
  5. 先算 mask = x != y                          # 仅用于出错时打印位置
  6. torch.equal( x.flatten().view(uint8),       # 真正的位精确判定
                  y.flatten().view(uint8) )
```

关键技巧是第 6 步：把两个张量都**展平后按 uint8（单字节）视图**比较。任何数据类型的张量，底层都是一段字节序列；按字节比，等价于「位模式完全相同」。

#### 4.2.3 源码精读

**元数据四连检 + 错误信息预备。** 前四个 assert 保证 dtype/shape/stride/device 一致；`mask = x != y` 只是给失败信息用（注意 `NaN != NaN` 为 True，所以 NaN 会被计入 mask，便于定位）：

[tile_kernels/testing/numeric.py:5-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L5-L23) —— `assert_equal` 的元数据检查与 mask 计算。

**位精确判定（uint8 视图）。** 这里有一段重要注释：形状 `[32768,1]`、stride `[1,32768]` 的张量，PyTorch 认为「连续」（`is_contiguous()` 为 True，因为 size 为 1 的维度任意 stride 都算连续），但 `.view` 会报错。所以必须先 `.contiguous()` 再 `.flatten()`，保证最后一维 stride 为 1，才能安全 `.view(torch.uint8)`：

[tile_kernels/testing/numeric.py:19-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L19-L23) —— 用 `flatten().view(uint8)` 做位精确比较；注释解释为何不能用 `.view`。

**真实用法（极简对拍）。** topk_gate 测试：用 `stable_topk` 参考取下标，kernel 取下标，`assert_equal` 位精确比对（下标是整数，理应逐位相同）：

[tests/moe/test_topk_gate.py:47-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L47-L54) —— `assert_equal(topk_idx, topk_idx_ref)` 的典型用法。

#### 4.2.4 代码实践

**实践目标**：体会「值相等 ≠ 位精确」，并理解 stride 检查能抓什么 bug。

**操作步骤**（CPU 即可，用纯 PyTorch 复现工具行为，不必依赖 GPU）：

```python
import torch
from tile_kernels.testing.numeric import assert_equal

# 场景 A：值相等但布局不同 —— 触发 stride 检查
a = torch.arange(12).reshape(3, 4)            # 行优先, stride (4,1)
b = a.t().t()                                  # 值相同
c = a.t().contiguous()                         # 值相同但 stride 变了
# assert_equal(a, c)  # 取消注释：会因 stride 不同而失败

# 场景 B：NaN 的位精确 —— 普通 == 抓不到符号不同的 NaN
x = torch.tensor([float('nan')], dtype=torch.float32)
y = torch.tensor([float('nan')], dtype=torch.float32)
# 把其中一个翻转符号位（最高位）
y_bytes = y.view(torch.uint8).clone()
y_bytes[3] = y_bytes[3] | 0x80                 # 置符号位 -> -NaN
y_neg = y_bytes.view(torch.float32)
print('普通 ==:', (x == y_neg))                # 都是 NaN, == 永远 False
print('uint8 比较:', torch.equal(x.view(torch.uint8), y_neg.view(torch.uint8)))  # False, 位不同
```

**需要观察的现象**：场景 A 中 `assert_equal(a, c)` 因 stride 不同抛出断言错误；场景 B 中两个「看起来都是 NaN」的张量，按 uint8 比较结果为 False（位模式不同），这正是 `assert_equal` 比普通 `==` 强的地方。

**预期结果**：A 抛 AssertionError 且信息含 `strides are not equal`；B 打印 `普通 ==: tensor([False])`、`uint8 比较: False`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `assert_equal` 里写的是 `x.stride() == y.stride()`，而不是只比 `is_contiguous()`？

> **答案**：两个张量可以「都连续」但 stride 不同（例如一个是行优先 `[4,1]`、另一个虽连续但来自不同形状的 reshape），它们的值可能对但布局错。stride 直接描述了元素在内存中的步长，是布局的精确指纹，比 `is_contiguous()` 的布尔值更严。

**练习 2**：`mask = x != y` 这一行在功能上是冗余的（真正的判定在下一行），为什么还要算它？

> **答案**：纯粹是为了**失败时给出可读的诊断信息**。当位精确判定失败，assert 消息会打印 `torch.nonzero(mask)`（哪些位置不同）以及 `x[mask]` vs `y[mask]`（具体差多少），大幅降低排错成本。它本身不参与「对/错」的判定。

### 4.3 浮点相似度 calc_diff 与舍入偏置检验 check_bias

#### 4.3.1 概念说明

对**有舍入**的算子（量化、反量化），位精确几乎不可能成立——舍入误差不可避免。这时需要两种更宽松的工具：

- **`calc_diff`**：算两个张量的「相对平方误差」，返回一个接近 0 的小数；测试用 `calc_diff(a,b) < 阈值` 断言。阈值由测试按算子精度经验给定（如 FP8 反量化用 `1e-3`、FP4 用 `2e-2`）。
- **`check_bias`**：检验「量化往返（cast 再 cast_back）是否引入系统性偏置」。理想的无偏舍入，每个值被「向上取整」和「向下取整」的概率各占一半；如果系统性地偏向一边，说明舍入逻辑有 bug。

二者面向不同失败模式：`calc_diff` 抓「整体误差有多大」，`check_bias` 抓「误差是否单向偏」——即便总误差很小，但若总是偏大，也是 bug。

#### 4.3.2 核心流程与数学

**calc_diff 的数学。** 把张量视作向量，定义：

\[
\text{sim} = \frac{2\,\langle x, y\rangle}{\langle x, x\rangle + \langle y, y\rangle}, \qquad
\text{calc\_diff} = 1 - \text{sim}
\]

利用恒等式 \(\langle x-y, x-y\rangle = \langle x,x\rangle - 2\langle x,y\rangle + \langle y,y\rangle\)，可化简为：

\[
\text{calc\_diff} = \frac{\|x-y\|^2}{\|x\|^2 + \|y\|^2}
\]

所以 `calc_diff` 本质是「差的平方」除以「双方向量平方和」，对幅度做了归一化：\(x=y\) 时为 0；用 `double()` 精度计算避免自身累加误差。它无量纲、可与固定阈值比较。

**check_bias 的数学（本讲重点）。** 设张量共有 \(n\) 个元素。逐个比较「量化往返后的值 \(x\)」与「原始值 \(\text{ref}_x\)」：

- `less_count` = 严格小于的个数（\(x < \text{ref}_x\)，即被向下舍入）；
- `equal_count` = 恰好相等的个数；
- 定义 `less_ratio` \(= \dfrac{\text{less\_count} + \text{equal\_count}/2}{n}\)（相等的元素对半劈，算作 0.5 个向下）。

在「无偏舍入」零假设下，每个非相等元素独立地以 0.5 概率被向下/向上舍入，于是 `less_count` 近似服从二项分布 \(B(n, 0.5)\)，标准差 \(\sqrt{n}/2\)。归一化到比例后：

\[
\text{less\_ratio} \sim N\!\left(0.5,\; \frac{1}{4n}\right), \qquad
\text{std}(\text{less\_ratio}) = \frac{1}{2\sqrt{n}}
\]

代码取阈值为 `10/sqrt(n)`，即允许 \(\text{less\_ratio}\) 偏离 0.5 不超过 \(10/\sqrt{n}\)。换算成标准差倍数：

\[
\frac{10/\sqrt{n}}{1/(2\sqrt{n})} = 20 \quad(\text{即 } 20\sigma)
\]

这是个**极其宽松**的阈值——\(20\sigma\) 对应的误报率 \(P(|Z|>20) \approx 5.5\times10^{-89}\)，事实上永远不可能被随机噪声触发。换言之，`check_bias` 只在「存在真实系统性偏置」时才报错，绝不会因为正常舍入的随机涨落而误伤。（代码注释里写「99.99999% 置信区间 something like this」是粗略说法，实际远比这严。）

#### 4.3.3 源码精读

**calc_diff。** 先升 `double()` 精度，算 sim，分母为 0 时（如全零张量）直接返回 0 避免除零：

[tile_kernels/testing/numeric.py:26-30](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L26-L30) —— `calc_diff`：归一化的相对平方误差。

**check_bias。** 逐行注释把推导讲得很清楚：二项分布 → 比例的标准差 → CLT → 置信区间。注意 `less_ratio` 把 `equal_count/2` 计入，等价于「相等元素按 0.5 向下」：

[tile_kernels/testing/numeric.py:33-55](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L33-L55) —— `check_bias`：基于二项分布 + CLT 的舍入偏置检验，阈值 `10/sqrt(n)`。

**真实用法（三种工具同框）。** per_token_cast 测试里：用 `assert_equal` 位精确比对量化输出与 SF（与参考逐位相同），再用 `check_bias` 检查「cast_back 还原后」是否对原值存在系统偏置：

[tests/quant/test_per_token_cast.py:108-112](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_per_token_cast.py#L108-L112) —— `assert_equal`（量化结果位精确）+ `check_bias`（反量化无偏置）的组合用法。

**calc_diff 的真实用法。** cast_back 测试里，反量化后用 `calc_diff` 与原始高精度值比，阈值随目标格式放宽（FP4 `2e-2`、FP8 `1e-3`）：

[tests/quant/test_cast_back.py:108-109](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/quant/test_cast_back.py#L108-L109) —— `calc_diff(x, x_fp8_bf16) < 阈值`，阈值随 fmt 调整。

**附带：count_bytes。** 它也在 `numeric.py` 里，按「元素数 × 每元素字节数」统计一批张量的总字节数，递归处理元组/列表，跳过 `None`。它是算带宽（u3-l2 讲过）的输入，不属于「正确性断言」而属于「性能计量」：

[tile_kernels/testing/numeric.py:58-65](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L58-L65) —— `count_bytes`：递归统计张量字节数，供带宽计算。

#### 4.3.4 代码实践

**实践目标**：用纯 Python 验证 `calc_diff` 的数学等价形式，并亲手复现 `check_bias` 的 `10/sqrt(n)` 阈值推导。

**操作步骤**（CPU + PyTorch 即可）：

1. 验证 `calc_diff(x,y) == ||x-y||² / (||x||²+||y||²)`：
   ```python
   import torch
   from tile_kernels.testing.numeric import calc_diff
   torch.manual_seed(0)
   x = torch.randn(1000)
   y = x + 0.01 * torch.randn(1000)          # 给一点小扰动
   d1 = calc_diff(x, y)
   d2 = ((x-y)**2).sum().item() / ((x**2).sum() + (y**2).sum()).item()
   print('calc_diff:', d1.item(), '公式:', d2)   # 两者应几乎相等
   ```
2. 模拟一次「无偏舍入」并跑 `check_bias`，确认它不误报：
   ```python
   import math, torch
   from tile_kernels.testing.numeric import check_bias
   torch.manual_seed(1)
   n = 100_000
   ref = torch.randn(n) * 1000
   # 模拟四舍五入到整数：理论上约一半向下、一半向上
   x = torch.round(ref)
   check_bias(x, ref)                         # 应不抛异常
   # 打印阈值与实际 less_ratio
   less = ((x < ref).sum().item() + (x == ref).sum().item()/2) / n
   print('less_ratio:', less, '阈值 10/sqrt(n):', 10/math.sqrt(n))
   print('偏离 0.5 的 sigma 倍数:', abs(less-0.5)/(1/(2*math.sqrt(n))))
   ```
3. 故意造一个「系统性偏大」的舍入（总是向上取整），看 `check_bias` 是否抓到：
   ```python
   x_biased = torch.ceil(ref)                 # 总是向上，系统性偏大 -> less_ratio 偏小
   # check_bias(x_biased, ref)                # 取消注释：应抛 AssertionError
   ```

**需要观察的现象**：第 1 步两个值几乎相等（验证公式）；第 2 步无偏舍入的 `less_ratio` 很接近 0.5、sigma 倍数远小于 20，`check_bias` 通过；第 3 步 `less_ratio` 明显偏离 0.5（接近 0），sigma 倍数远超 20，`check_bias` 抛异常。

**预期结果**：`calc_diff` 与手算公式一致到浮点精度；无偏时 `check_bias` 静默通过；系统性偏置时抛 `AssertionError` 且信息含 `Less than ratio not close to 0.5`。

#### 4.3.5 小练习与答案

**练习 1**：`calc_diff` 为什么在开头把张量升到 `double` 精度？不升会怎样？

> **答案**：`calc_diff` 要算 \(\sum x^2\)、\(\sum y^2\)、\(\sum xy\) 这些大量元素的平方和，在 `bfloat16`/`float16` 下累加会迅速丢失精度甚至溢出，导致相似度本身不可信。升到 `double` 让「度量误差的工具」自身的误差远小于「被测的舍入误差」，保证阈值判定可靠。

**练习 2**：`check_bias` 把 `equal_count/2` 计入 `less_ratio`，而不是直接忽略相等的元素。为什么？

> **答案**：「恰好相等」既不算向上也不算向下，把它对半劈（算 0.5 个向下）能让 `less_ratio` 在「全部相等」时恰为 0.5（即无偏的期望值），保持 \(E[\text{less\_ratio}]=0.5\) 成立。若直接忽略，分母要改成「非相等元素数」，反而引入额外噪声；对半劈是最简洁的无偏处理。

**练习 3**：如果某个测试只有 `n=4` 个元素，`check_bias` 的阈值是多少？这个阈值还能可靠检出偏置吗？

> **答案**：阈值为 \(10/\sqrt{4}=5\)，而 `less_ratio` 必在 \([0,1]\)，偏离 0.5 最多 0.5，永远 \(<5\)。所以 `check_bias` 对 \(n=4\) **永远通过**，无法检出任何偏置。这说明 `check_bias` 只在大 \(n\) 下才有统计效力——它依赖 CLT，样本太小就没有分辨力。实际量化测试的张量都有成千上万个元素，不会触发这个问题。

## 5. 综合实践

把本讲三块知识（生成器、`assert_equal`、`calc_diff`/`check_bias`）串起来，为一个量化算子写一份**完整对拍测试**。

**任务**：参考 `tests/quant/test_per_token_cast.py` 的结构，给 `tile_kernels.quant.per_token_cast` 写一个精简版测试文件 `my_test_quant.py`，要求：

1. 用 `generate_num_tokens(is_benchmark=False)` 与 `generate_hidden_sizes()` 做参数化（挑 `fmt='e4m3'`、`in_dtype=torch.bfloat16`、`num_per_channels=128` 即可，缩小组合）。
2. 在测试体内：
   - 用 `torch.randn` 造输入 `x`；
   - 调被测 `tile_kernels.quant.per_token_cast(x, fmt='e4m3', num_per_channels=128, round_sf=True)` 得 `(x_casted, x_sf)`；
   - 调参考 `tile_kernels.torch.cast(x, 'e4m3', block_size=(1, 128), round_sf=True)` 得 `(ref_casted, ref_sf)`；
   - 用 **`assert_equal`** 位精确比对 `x_casted` 与 `ref_casted`、`x_sf` 与 `ref_sf`；
   - 用 `tile_kernels.torch.cast_back((x_casted, x_sf), 'bf16', (1, 128))` 反量化，再用 **`check_bias`** 检查对原值 `x` 是否无系统偏置。
3. 运行：`TK_FULL_TEST=1 pytest my_test_quant.py -v`，观察 FULL 档比默认档多跑了哪些（尤其是 `num_tokens=0` 的用例）。
4. 思考题（写在测试文件顶部注释里）：本测为什么对 `x_casted` 用 `assert_equal` 而非 `calc_diff`？又为什么对反量化结果用 `check_bias` 而非 `assert_equal`？

**验收要点**：

- 参数化 id 里能看到 4001/8001（以及 FULL 档的 0）与各 hidden 维；
- 位精确断言全过（量化本身是确定性的，与参考逐位相同）；
- `check_bias` 全过（说明往返无偏置）；
- 能用一句话回答思考题：「量化映射是确定性双射、故位精确；而反量化还原值经舍入不可能逐位相等，故只能检验其无系统偏置」。

> 若本地无 GPU/无法安装，则把第 2 步改成「源码阅读型实践」：对照 `tests/quant/test_per_token_cast.py:108-112` 写出每一步用哪个工具、为什么，并手算一组 `n=10000` 时 `check_bias` 的阈值（\(10/100=0.1\)），标注「待本地验证」。

## 6. 本讲小结

- `tile_kernels/testing/generator.py` 提供 `generate_*` 系列，分**默认档 / `TK_FULL_TEST` 档**两套覆盖：默认档快、覆盖主流配置，FULL 档加边界（空输入、单 SM、非常规 topk/experts）。FULL 只对正确性用例生效，benchmark 用例始终精简。
- 生成器分两层：先产出参数组合（轻量、可作 pytest id），测试体内再造具体张量。`generate_moe_params` yield 的 `num_experts` 是「每卡专家数」（已除以 EP 卡数）。
- 特殊数据生成器各有侧重：`generate_e5m6_inputs` 喂自定义浮点的危险特殊值；`generate_rand_float` 按指数均匀采样量级、压力测试 SF 的大动态范围。
- `assert_equal` 做**位精确**比较：dtype/shape/stride/device 四项元数据 + `flatten().view(uint8)` 按字节比；适合无舍入算子（转置、topk、量化映射本身），能抓 NaN 符号位、负零与布局错误。
- `calc_diff` 做**浮点相似度**：归一化相对平方误差 \(\|x-y\|^2/(\|x\|^2+\|y\|^2)\)，用 `double` 累加，适合有舍入的算子（反量化），阈值随精度放宽。
- `check_bias` 做**舍入偏置统计检验**：零假设下 `less_ratio ~ N(0.5, 1/(4n))`，阈值 `10/sqrt(n)` 等于 \(20\sigma\)，误报率近乎 0，只抓真实系统偏置；样本太小时（如 \(n=4\)）无分辨力。
- `count_bytes` 同在 `numeric.py`，但属性能计量（算带宽），不属正确性断言。

## 7. 下一步学习建议

- **u9-l2（benchmark 插件与回归检测）**：本讲的 `count_bytes` 只是带宽公式的分子，分母的 `benchmark_timer`（CUPTI 计时）与 `benchmark_record`（JSONL 落盘、15% 回归阈值）在下一讲详细展开。
- **u9-l3（随机种子与 pytest 插件机制）**：本讲的生成器大量依赖随机数，下一讲讲 `seed = base + sha256(nodeid)` 如何让每个测试既可复现又互不干扰。
- **回头精读**：带着本讲对 `assert_equal`/`calc_diff`/`check_bias` 的理解，重看 u3-l2 的转置测试与 u4 系列的量化测试，会发现在不同算子里这三个工具的选用完全遵循「无舍入→位精确、有舍入→相似度+偏置」的统一原则。
