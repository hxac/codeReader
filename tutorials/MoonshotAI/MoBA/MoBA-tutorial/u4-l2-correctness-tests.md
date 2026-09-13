# u4-l2 正确性测试：naive 与 efficient 的前向/反向对齐

## 1. 本讲目标

前面三个单元里，我们已经分别读完了「教科书实现」`moba_attn_varlen_naive`（u2）和「生产实现」`moba_attn_varlen`（u3）。这两个实现加起来接近 500 行、涉及两次 flash-attn 内核调用与大量索引变换——凭什么相信它们计算的是**同一个函数**？

本讲解读整个仓库唯一的正确性测试文件 [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L1-L100)（全文仅 100 行），它回答了上面那个问题。学完本讲，你应当能：

1. 说清「黄金参考（golden reference）」测试策略：为什么用 naive 实现当标准答案，而不是手写解析解。
2. 逐行读懂 `generate_data` 如何用固定种子 + `random.sample` 构造合法的 varlen 批次。
3. 分析 6 层 `parametrize` 笛卡尔积（共 324 个用例）实际覆盖了哪些代码路径、又漏掉了哪些。
4. 解释三层容忍度断言（`allclose` + `max` + `mean`）各自抓什么类型的 bug、为什么缺一不可。
5. 仿照现有用例，自己写出一个边界情形的新测试（本讲综合实践：chunk_size 大于序列长度时的退化行为）。

## 2. 前置知识

### 2.1 黄金参考测试策略

给数值内核写测试，常见三种思路：

| 策略 | 做法 | 优缺点 |
|---|---|---|
| 解析解 | 手算期望输出 | 只对极小输入可行，注意力 softmax 手算极易出错 |
| 性质测试 | 断言不变量（如因果性、凸组合性）| 抓不住「整体偏一点」的错误 |
| **黄金参考** | 用一个**慢但显然正确**的实现当标准答案 | 写一次参考，随机大输入随便测；代价是参考本身要对 |

MoBA 选第三种：`moba_naive.py` 的 docstring 自称 "a naive brute-force setting for reference"（[moba/moba_naive.py:L16](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L16)）。它逐 batch、逐块地用 `for` 循环 + `einsum` + 显式掩码算注意力（u2-1 精读过），没有 varlen trick、没有 LSE 合并、没有自定义反向——每一步都直白可查。README 也明确把它定位为帮助理解的教学实现（[README.md:L61](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L61)）。

于是测试的逻辑就是：**同一个输入喂给两个实现，前向输出和反向梯度都必须对齐**。如果对齐，说明高效实现那套「chunk 元数据 → 向量化 gate → varlen 重组 → LSE 合并 → 两次内核反向」（u3 全单元）在数学上等价于朴素算法。

### 2.2 PyTorch 梯度比较的基本装置

比较梯度需要几个测试里常见的技巧，先交代清楚：

- `q = torch.randn(..., requires_grad=True)` 创建的是**叶子张量**，反向后梯度落在 `q._grad`（或 `q.grad`）上。
- `torch.autograd.backward(o, vo_grad)` 等价于 `o.backward(vo_grad)`：用给定的**上游梯度** \(g\) 反传，效果是计算 \(\nabla_q \langle g, o \rangle\) 等。上游梯度必须两次共用同一个 \(g\)，否则比较没有意义（4.3 节展开）。
- 叶子张量的 `._grad` 是**累加**的：第二次 `backward` 前必须 `zero_()` 清零，否则参考梯度里混入了高效实现的梯度。
- `._grad.clone()` 必须在清零**之前**做：`clone()` 拍快照，`zero_()` 原地清空，顺序反了快照就变成全零。

### 2.3 bf16 的精度量级

bf16 有 8 位有效尾数（1 位隐含 + 7 位显式），机器精度 \(\varepsilon_{\text{bf16}} = 2^{-8} \approx 3.9 \times 10^{-3}\)。两条实现路径内部都在 fp32 中做累加（naive 显式转 fp32，flash-attn 内核 fp32 累加），但输入 q/k/v 是 bf16、输出也要落回 bf16——**各有一次约 \(\varepsilon_{\text{bf16}}\) 量级的舍入**。这就是测试容忍度 `eps = 2e-2`（约 5 倍机器精度）的由来，也是后文「max 与 mean 阈值差 100 倍」的物理基础。

### 2.4 回顾：两条路径的关键差异点

测试要弥合的差异，正是 u2/u3 精读过的这些点（此处只列清单，细节见对应讲义）：

- gate 打分：naive 逐 batch `einsum("shd,nhd->hsn")` vs efficient 全局 `einsum("nhd,shd->nhs")`，都在 fp32 中做（u3-l3）。
- 因果与跨 batch 约束：naive 用 +inf/-inf 改写 gate 再 token 级 `tril` 兜底（u2-l2）vs efficient 用 `gate_chunk_end_mask | gate_batch_end_mask` 在选块阶段前置完成（u3-l3）。
- 末块处理：naive 让最后一块（可能不满）参与 gate 竞选 vs efficient 把每序列最后一块整体剔除、留给 self 支路（u3-l2）。
- 输出合成：naive 一次带掩码 softmax vs efficient 两路 flash-attn + LSE 在线合并（u3-l5）。
- 反向：naive 走 PyTorch autograd 自动微分 vs efficient 手写 `MixedAttention.backward` 复用两次 `_flash_attn_varlen_backward`（u3-l6）。

测试通过 = 这五处差异在随机输入上数值等价。

## 3. 本讲源码地图

| 文件 | 本讲关注的范围 | 作用 |
|---|---|---|
| `tests/test_moba_attn.py` | `generate_data`（L8-L34）| 测试数据工厂：固定种子、随机 varlen 切分 |
| `tests/test_moba_attn.py` | 参数化装饰器（L37-L42）| 324 个组合的测试网格 |
| `tests/test_moba_attn.py` | 双重建模主体（L43-L100）| 前向/反向对齐 + 三层断言 |
| `moba/moba_naive.py` | `num_block`、topk 的 `min` 防护（L45、L63-L65）| 理解边界情形下参考实现的行为 |
| `moba/moba_efficient.py` | `moba_topk` 调整与捷径分支（L313-L321）| 理解边界情形下高效实现的行为 |
| `moba/moba_efficient.py` | 两次内核反向中的 `deterministic=True`（L208-L229、L243-L264）| 梯度可复现的前提 |
| `README.md` | Unit Tests 一节（L65-L68）| 官方运行方式：`pytest tests/test_moba_attn.py` |

## 4. 核心概念与源码讲解

### 4.1 `generate_data`：固定种子与 varlen 随机切分

#### 4.1.1 概念说明

测试比较的是两个实现的**数值差**，所以第一要务是保证「实验变量可控」：

- **可复现**：每次运行、每台机器上生成完全相同的 q/k/v 与切分点——失败可调试，结果可对比。
- **随机覆盖**：切分点随机，意味着每条序列长度不同、最后一块是否不满也不同，恰好压在 varlen 逻辑的敏感面上。
- **合法的 varlen 批次**：`cu_seqlens` 必须满足首元素 0、严格递增、末元素等于总 token 数（u1-l3 讲过这套约定）。

`generate_data` 一次解决三件事。

#### 4.1.2 核心流程

```
generate_data(batch, seqlen, H, H, D, dtype):
    1. 固定三处随机种子（Python / CPU torch / CUDA torch）
    2. randn 生成 q/k/v，形状 [seqlen, H, D]，requires_grad=True
    3. 构造 cu_seqlen：
       a. batch>1 时从开区间 (0, seqlen) 无放回抽 batch-1 个切分点
       b. 排序 → 前面拼 0，后面拼 seqlen → int32 张量
    4. max_seqlen = max(相邻差)   # 只作内核启动参数
    返回 q, k, v, cu_seqlen, max_seqlen
```

#### 4.1.3 源码精读

固定三处种子——Python 的 `random`（切分点）、CPU 与 GPU 的 torch 生成器（q/k/v 及后文的 `vo_grad`）各自独立，必须分别锁：

[tests/test_moba_attn.py:L9-L11](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L9-L11)

```python
random.seed(0)
torch.manual_seed(0)
torch.cuda.manual_seed(0)
```

q/k/v 直接按 flash-attn 布局 `[S, H, D]` 生成并带 `requires_grad=True`——它们就是后续比较梯度的叶子张量。注意调用处两个头数传的是同一个值 `head, head`（[L48-L50](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L48-L50)），所以**本测试不覆盖 GQA**（`num_kv_head < num_q_head` 的复制逻辑在 wrapper 里，u4-l1 讲过）：

[tests/test_moba_attn.py:L15-L23](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L15-L23)

```python
q = torch.randn(
    (seqlen, num_q_head, headdim), dtype=dtype, device=device, requires_grad=True
)
```

varlen 切分是全函数最精巧的一行——`random.sample`（**不放回**抽样）保证切分点两两不同：

[tests/test_moba_attn.py:L26-L29](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L26-L29)

```python
cu_seqlen = random.sample(range(1, seqlen - 1), batch - 1) if batch > 1 else []
cu_seqlen.sort()
cu_seqlen = [0] + cu_seqlen + [seqlen]
cu_seqlen = torch.tensor(cu_seqlen, device=device, dtype=torch.int32)
```

四个细节值得咀嚼：

1. 抽样范围是 `range(1, seqlen - 1)`（开区间），切分点不可能是 0 或 seqlen，因此**没有空序列**（最短 1 个 token——两个切分点相邻时出现，长度为 1 的序列会真实出现在网格里）。
2. `sample` 不放回 → 切分点互异 → `cu_seqlen` 严格递增。若换成 `random.choices`（有放回），可能出现重复切分点即零长序列，`cu_seqlens` 约定被破坏。
3. 排序不可省：`sample` 返回顺序随机，`cu_seqlens` 必须单调递增。
4. `batch=1` 时切分点列表为空，退化为 `cu_seqlen = [0, seqlen]` 的单序列。

`max_seqlen` 由实际切分推出，只影响内核启动配置、不参与索引（u1-l3）：

[tests/test_moba_attn.py:L32-L34](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L32-L34)

```python
max_seqlen = torch.amax(cu_seqlen[1:] - cu_seqlen[:-1])
```

#### 4.1.4 代码实践

**实践目标**：不依赖 GPU，验证你对 `cu_seqlen` 构造规则的理解。

**操作步骤**（示例代码，可存为独立脚本运行）：

```python
import random

random.seed(0)
batch, seqlen = 4, 512
cu = random.sample(range(1, seqlen - 1), batch - 1)
cu.sort()
cu = [0] + cu + [seqlen]
print(cu)                       # 切分点
print([b - a for a, b in zip(cu[:-1], cu[1:])])  # 每条序列长度
```

**需要观察的现象**：切分点列表严格递增、首尾恰为 0 和 512；长度列表均为正数且总和为 512；多次运行结果完全一致（种子固定）。

**预期结果**：形如 `[0, 210, 403, 467, 512]` 的递增列表（具体数值以本地运行为准，待本地验证）；把 `sample` 换成 `choices` 后多跑几次可观察到重复切分点——这就是它被排除的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `random.sample` 的结果必须 `sort()` 之后才能用？

**答案**：`cu_seqlens` 语义要求单调递增（前缀和）。`sample` 返回顺序是随机的，不排序则边界可能交错，flash-attn 与 naive 实现都会切出错误的序列边界。

**练习 2**：这个数据工厂里 `max_seqlen` 能否直接写 `seqlen` 而不引入错误？

**答案**：可以。`max_seqlen` 只是内核分块启动的提示参数，取真实最大值只是让启动配置更省，取上界 `seqlen` 不改变计算结果——但会浪费一点并行度。`generate_data` 选择算真实值是「顺手优化」而非正确性需求。

### 4.2 `test_attn_varlen_moba` 参数化：324 个组合覆盖了什么

#### 4.2.1 概念说明

pytest 的 `@pytest.mark.parametrize` 把一个测试函数展开成一批用例；多个装饰器叠放时做**笛卡尔积**。这个文件用 6 层装饰器织出一张测试网格，目的是让随机数据覆盖尽可能多的**代码路径与边界情形**——尤其是 u3 里那些精巧的分支：捷径分支、跨 batch 掩码、零 expert 裁剪、最后一块不满。

#### 4.2.2 核心流程

参数轴与取值：

| 参数 | 取值 | 个数 | 压测的差异点 |
|---|---|---|---|
| `batch` | 1, 4, 7 | 3 | 单序列 vs 多序列；batch=7 时切分碎、短序列多 |
| `head` | 1, 2, 4, 8 | 4 | 单头 vs 多头；多头才可能出现「某块被头 0 选中、头 1 落空」的零 expert |
| `seqlen` | 512, 1024, 2048 | 3 | 序列长度与块数的比例关系 |
| `head_dim` | 128 | 1 | 固定（未覆盖其他头维度） |
| `moba_chunk_size` | 128, 256, 1024 | 3 | 块大小相对序列长度的变化，直接改变每序列块数 |
| `moba_topk` | 2, 3, 4 | 3 | 选块数；注意**不含 1** |

总用例数 \(3 \times 4 \times 3 \times 1 \times 3 \times 3 = 324\)。

更关键的是**轴之间的交互**产生的边界：

- **捷径分支被真实覆盖**：`seqlen=512` 配 `chunk=1024` 时，每条序列长度 \(\le 512 < 1024\)，由向上取整公式（[moba/moba_efficient.py:L21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L21)）每序列恰 1 块，且这唯一一块必是「最后一块」而被过滤掉（[L51-L57](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L51-L57)）→ `num_filtered_chunk = 0` → `moba_topk = min(topk-1, 0) = 0` → 走 `need_moba_attn` 为假的捷径（[L313-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L321)）。同理 `seqlen=1024` 配 `chunk=1024` 也全走捷径；而 `seqlen=2048` 配 `chunk=1024`、batch=1 时有 2 块、过滤后剩 1 块，是**真实 MoBA 路径**且每 query 只多选 1 块——同一条 `chunk=1024` 轴扫过三种形态。
- **`moba_topk` 网格不含 1**：因为高效实现内部要执行 `moba_topk - 1`（当前块必选占掉一个名额，u3-l1），`topk=1` 会直接落入捷径。网格作者选择从 2 起步，捷径改由「块数不足」这条路径间接触发。
- **naive 侧的 `min` 防护被压到**：参考实现取 `k = min(moba_topk, num_block)`（[moba/moba_naive.py:L63-L65](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L63-L65)）。当 `topk ≥ num_block`（如 seqlen=512、chunk=256、batch=1 时 num_block=2，而 topk 可为 2/3/4）时，阈值 `gate_top_k_val.min()` 可能退化为 -inf 而全选——这正是 u2-l2 分析过的坑，靠 token 级 `tril` 掩码兜底；网格里这类组合大量存在。
- **最后一块不满**：随机切分下几乎每个 batch 的末块都不满（长度非 chunk 整数倍），naive 的 `min(batch_size, block_start + chunk)` 与 efficient 的剔除逻辑天天被压。

也有明显的**覆盖缺口**（本讲观点，源码未注释说明动机）：GQA 头数不等、`head_dim` 非 128、`topk=1` 显式用例、长度 1~511 的短序列专门用例（仅作为随机切分的副产品出现）、CPU 设备（数据直接放 `torch.cuda.current_device()`，无 GPU 无法运行本测试）。

#### 4.2.3 源码精读

六层装饰器自下而上叠放，pytest 会为每个组合生成形如 `batch=1-head=1-seqlen=512-head_dim=128-moba_chunk_size=128-moba_topk=2` 的用例 id：

[tests/test_moba_attn.py:L37-L43](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L37-L43)

```python
@pytest.mark.parametrize("batch", [1, 4, 7])  # can be arbitrary
@pytest.mark.parametrize("head", [1, 2, 4, 8])
@pytest.mark.parametrize("seqlen", [512, 1024, 2048])
@pytest.mark.parametrize("head_dim", [128])
@pytest.mark.parametrize("moba_chunk_size", [128, 256, 1024])
@pytest.mark.parametrize("moba_topk", [2, 3, 4])
def test_attn_varlen_moba(batch, head, seqlen, head_dim, moba_chunk_size, moba_topk):
```

对照高效实现的捷径分支——网格中 `chunk=1024` 且 `seqlen ∈ {512, 1024}` 的全部组合都会走进这里，直接返回普通因果注意力：

[moba/moba_efficient.py:L313-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L313-L321)

```python
# we will adjust selective topk to moba_topk - 1, as the last chunk is always chosen
moba_topk = min(moba_topk - 1, num_filtered_chunk)
need_moba_attn = moba_topk > 0

# corner case: if no moba attn needed, just return self attn
if not need_moba_attn:
    return flash_attn_varlen_func(
        q, k, v, cu_seqlens, cu_seqlens, max_seqlen, max_seqlen, causal=True
    )
```

naive 侧的对称防护是 `k=min(moba_topk, num_block)`——块数不足时不硬选，退化为「能选几块选几块」：

[moba/moba_naive.py:L45](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L45)、[L63-L65](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L63-L65)

```python
num_block = math.ceil(batch_size / moba_chunk_size)
...
gate_top_k_val, gate_top_k_idx = torch.topk(
    gate, k=min(moba_topk, num_block), dim=-1, largest=True, sorted=False
)
```

**两条实现在退化情形下为什么仍然对齐**（衔接 u2-l2 与 u3-l3 的结论）：以 `seqlen=512、chunk=1024、batch=1` 为例——efficient 每序列 1 块、过滤后 0 块 → 捷径 = 全量因果注意力；naive `num_block=1`，唯一一块对全部 query 都是「当前块」被置 +inf 必选，块掩码全开后再叠 token 级 `tril`（[moba/moba_naive.py:L76-L81](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L76-L81)）——同样是全量因果注意力。殊途同归，测试抓的就是这种「路径不同、函数相同」的等价性。

#### 4.2.4 代码实践

**实践目标**：亲手数出并筛选测试网格，验证上面的覆盖分析。

**操作步骤**（需已安装 pytest，进入仓库根目录）：

1. 只收集不运行，统计用例数：`pytest tests/test_moba_attn.py --collect-only -q | tail -n 1`
2. 单独运行会走捷径分支的组合：`pytest tests/test_moba_attn.py -k "batch-1 and seqlen-512 and moba_chunk_size-1024" -s`
3. 单独运行真实 MoBA 路径的组合：`pytest tests/test_moba_attn.py -k "batch-1 and seqlen-2048 and moba_chunk_size-1024" -s`

**需要观察的现象**：第 1 步应报告 324 个用例；第 2、3 步的 `print` 输出（输出差与梯度差的 max/mean）量级相近——因为两者最终都收敛到「数值对齐」这一断言。

**预期结果**：收集数为 324；两组子集全部通过。若无 GPU 环境，第 1 步的收集仍可完成（不触 CUDA），第 2、3 步会因 `torch.cuda.current_device()` 报错——此时属于「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：网格中哪些组合会触发 `need_moba_attn == False`？一共多少个？

**答案**：`chunk=1024` 且 `seqlen ∈ {512, 1024}` 的所有组合（batch 3 × head 4 × topk 3 = 每档 seqlen 36 个，共 72 个）。因为此时任意序列长度 ≤ seqlen ≤ chunk，每序列恰 1 块且必为最后一块，过滤后候选块数为 0。而 `seqlen=2048` 配 `chunk=1024` 时单序列有 2 块、过滤后剩 1 块，`moba_topk = min(topk-1, 1) = 1 > 0`，走真实路径。

**练习 2**：为什么 `moba_topk` 轴从 2 开始而不是 1？

**答案**：高效实现里当前块永远必选、占一个名额，实际自由选择数是 `moba_topk - 1`；`topk=1` 意味着没有自由名额，等价于纯因果自注意力。网格从 2 起步保证每个用例都有真实的块选择发生；`topk=1` 的退化行为留给「块数不足」的捷径路径去覆盖（综合实践里我们会亲手补上这个用例）。

### 4.3 前向/反向双重建模：同一份数据、同一个上游梯度

#### 4.3.1 概念说明

「双重建模」指测试同时比较**前向输出**与**反向梯度**。只测前向是不够的：高效实现的前向（LSE 合并）与反向（手写 `MixedAttention.backward`）是两套独立代码，完全可能出现「前向对、梯度错」的半坏状态——典型如 u3-l5 提过的情形：前向里 `max_lse_1d` 只是数值防护、差值会在 factor 中消去，但反向若用了偏移版 LSE，重算的 softmax 概率整体错误，前向分毫不动、梯度全面崩坏。只有拉上反向一起比，才能把 u3-l6 那套「传合并量即可」的数学真正验证到位。

要使梯度比较成立，必须控制两个变量：**同一份输入**（q/k/v 是同一批叶子张量，两个实现先后消费，值不变）与**同一个上游梯度**（`vo_grad` 只生成一次、两次反传共用）。此外梯度是累加的，两次反传之间要清零。

#### 4.3.2 核心流程

```
1. generate_data → q, k, v（叶子，requires_grad=True），cu_seqlen，max_seqlen
2. vo_grad = randn_like(q)          # 唯一一次生成，两路共用
3. o     = moba_attn_varlen(q,k,v,...)          # 高效前向
4. backward(o, vo_grad) → q/k/v._grad            # 高效反向（自定义 Function）
5. gqkv     = stack(三个 .clone())               # 快照
6. 三个 ._grad.zero_()                           # 清零，防累加污染
7. o_ref = moba_attn_varlen_naive(q,k,v,...)     # 参考前向
8. backward(o_ref, vo_grad) → 累计到清零后的 ._grad
9. gqkv_ref = stack(三个 .clone())
10. 比较 o vs o_ref、gqkv vs gqkv_ref（下一节的三层断言）
```

有一个隐含前提值得点破：**两个实现的块选择必须一致**。gate 打分两边都在 fp32 中做、输入是同一份 bf16 数据，分数几乎比特级一致，随机连续数据下 top-k 几乎不可能翻转（u2-l1、u3-l3 的结论）。一旦翻转，某个 query 注意到不同的块，输出差会远超容忍度、测试立刻失败——所以这个测试同时在守护「gate 数值路径的一致性」。

#### 4.3.3 源码精读

`vo_grad` 在两路之前一次性生成——它是上游梯度 \(g = \partial L/\partial o\)，必须共用，否则比较的是两个不同标量函数的梯度：

[tests/test_moba_attn.py:L48-L51](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L48-L51)

```python
q, k, v, cu_seqlen, max_seqlen = generate_data(
    batch, seqlen, head, head, head_dim, dtype
)
vo_grad = torch.randn_like(q)
```

第一路：高效实现前向 + 反传 + 梯度快照。`torch.stack(..., dim=1)` 把三个 `[S, H, D]` 梯度叠成 `[S, 3, H, D]`，保持按 token 对齐，方便后续逐元素比较：

[tests/test_moba_attn.py:L54-L64](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L54-L64)

```python
o = moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen,
                     moba_chunk_size=moba_chunk_size, moba_topk=moba_topk)
torch.autograd.backward(o, vo_grad)
gqkv = torch.stack((q._grad.clone(), k._grad.clone(), v._grad.clone()), dim=1)
```

`.clone()` 与 `zero_()` 的顺序是**先拍快照、再清零**——`zero_()` 是原地操作，直接作用于 `._grad` 本体：

[tests/test_moba_attn.py:L67-L69](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L67-L69)

```python
q._grad.zero_()
k._grad.zero_()
v._grad.zero_()
```

第二路：naive 前向 + 反传 + 参考快照。注意 `# ref with bf16` 的注释——参考实现同样以 bf16 数据入场，比的是「同精度下两种算法」而非「bf16 vs fp32 真值」：

[tests/test_moba_attn.py:L70-L80](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L70-L80)

```python
o_ref = moba_attn_varlen_naive(q, k, v, cu_seqlen, max_seqlen,
                               moba_chunk_size=moba_chunk_size, moba_topk=moba_topk)
torch.autograd.backward(o_ref, vo_grad)
gqkv_ref = torch.stack((q._grad.clone(), k._grad.clone(), v._grad.clone()), dim=1)
```

高效侧梯度可复现还有一个前提藏在 u3-l6 读过的两处细节里：两次内核反向都传了 `deterministic=True`（[moba/moba_efficient.py:L208-L229](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L208-L229) 与 [L243-L264](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L243-L264)），否则原子累加的浮点归约顺序不确定，梯度会有微小的运行间抖动，逐元素断言容易被随机尖峰打穿。

#### 4.3.4 代码实践

**实践目标**：用反证法体会「控制变量」的必要性——故意打破一条控制，看对齐如何崩溃。

**操作步骤**（示例代码；在仓库外另存脚本，或复制测试文件为 `tests/test_moba_attn_practice.py` 再改，**不要改动原测试**）：

1. 复制 `test_attn_varlen_moba`，在两路之间**重新生成** `vo_grad`：

```python
# 示例代码：故意破坏控制变量的对照实验
o = moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen, ...)
torch.autograd.backward(o, vo_grad)
gqkv = torch.stack((q._grad.clone(), k._grad.clone(), v._grad.clone()), dim=1)

q._grad.zero_(); k._grad.zero_(); v._grad.zero_()

vo_grad = torch.randn_like(q)   # ← 加入这一行：上游梯度变了
o_ref = moba_attn_varlen_naive(q, k, v, cu_seqlen, max_seqlen, ...)
torch.autograd.backward(o_ref, vo_grad)
gqkv_ref = torch.stack((q._grad.clone(), k._grad.clone(), v._grad.clone()), dim=1)
```

2. 选一个固定组合运行（如 `batch=1, head=2, seqlen=1024, chunk=256, topk=2`），观察 `grad diff` 的 print 输出。
3. 再删掉三行 `zero_()`，重复运行，观察参考梯度膨胀。

**需要观察的现象**：第 2 步梯度差 max 飙升到与梯度本身同量级（输出差不变、仍很小）；第 3 步 `gqkv_ref` 约为原来的两倍（累加了两路梯度）。

**预期结果**：`gqkv` 的 allclose 断言失败、输出断言仍通过——证明上游梯度不共用时「梯度对齐」比较的是两个不同的函数。实际数值待本地验证（需 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉三行 `zero_()`，最终断言会怎样失败？

**答案**：`gqkv_ref` 里累加了高效实现的梯度，`gqkv_ref ≈ gqkv + naive 梯度`，误差约为 naive 梯度本身的量级，梯度 allclose 与 max/mean 断言全部失败；而输出断言（前向不受累加影响）仍然通过。这也是一个诊断信号：**只有梯度挂、输出不挂 → 先查梯度的实验装置（清零、clone 顺序、上游梯度），再查实现**。

**练习 2**：为什么比较梯度用 `q._grad` 而不是对 `o` 和 `o_ref` 各自 `retain_graph` 后二次反传？

**答案**：没必要。`backward(o, vo_grad)` 已经把链上所有叶子（q/k/v）的梯度算完落到 `._grad`；`retain_graph` 只在需要对**另一组上游梯度**再次反传时才需要。测试只需要一组共用上游梯度下的叶子梯度快照。

### 4.4 三层容忍度：`allclose` 与 max/mean 的互补设计

#### 4.4.1 概念说明

数值测试最难的环节不是「比不相等」，而是「**差多少算相等**」。定太松，bug 溜过去；定太紧，正常舍入噪声导致随机挂。本测试对输出和梯度各设**三层断言**：

1. `torch.allclose(atol=eps, rtol=eps)`，逐元素判定 |a−b| ≤ atol + rtol·|b|；
2. `diff.max() < 4e-2`：误差的最大值（抓局部尖峰）；
3. `diff.mean() < 4e-4`：误差的均值（抓系统性偏差）。

三层的洞察在于：**不同类型的 bug 在误差的「空间分布」上签名不同**。

| bug 签名 | allclose(2e-2) | max(4e-2) | mean(4e-4) |
|---|---|---|---|
| 正常 bf16 舍入噪声（零星小尖峰） | 通过 | 通过（峰值约几 e-3）| 通过（均值极小）|
| 个别 query 块选择翻转（局部大错） | **失败** | **失败** | 可能通过（被全体平均稀释）|
| 整体缩放错（如漏乘 softmax_scale 的 \(\gamma = d^{-1/2}\)）| 可能通过（rtol=2e-2 放行 2% 以内的相对差）| 可能通过 | **失败**（每个元素都偏）|
| 分母漏掉一个块的贡献（softmax 少一块）| **失败** | **失败** | **失败** |

「整体缩放错」那一行是理解三层设计的钥匙：假设实现把所有输出缩小 1%，`allclose` 的 rtol=2e-2 允许 2% 的相对偏差——**放行**；`max < 4e-2` 对 O(0.3) 的输出意味着允许约 13% 的偏差——**也放行**；但 mean < 4e-4 要求平均误差不到输出量级的 0.13%——**抓住**。反过来，一个只影响个别位置的尖峰会被均值稀释（2048×8×128 个元素里坏 128 个，均值只抬高约 6%），必须靠 max 兜住。三层各抓一类，缺一不可。

#### 4.4.2 核心流程

`eps = 2e-2` 的推导链条：

\[ \varepsilon_{\text{bf16}} = 2^{-8} \approx 3.9\times 10^{-3} \quad (\text{8 位有效尾数}) \]

两条路径各自有一次输出落 bf16 的舍入（≤ 半个 ulp ≈ \(2\times10^{-3}\) 相对误差），相加约 \(4\times10^{-3}\) 量级的相对差；再留出 fp32 累加顺序差异、LSE 合并中 exp/log 的少量余量，取 5 倍机器精度 `2e-2` 作为 `allclose` 的 atol/rtol——既显著高于噪声 floor，又远低于任何真实 bug 的量级。max 阈值 `4e-2` 与 allclose 同源（放宽一倍容纳尖峰）；mean 阈值 `4e-4` 则收紧 50~100 倍，专门对应「处处小偏」的系统性错误。

#### 4.4.3 源码精读

先看输出侧三层的完整写法。注意两件事：`print` 把 max/mean 打到 stdout（配 `pytest -s` 可见），失败时 `assert` 的消息本身就是一个 `(mean, max)` 元组——pytest 会原样打印，省去失败后手动重算：

[tests/test_moba_attn.py:L83-L88](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L83-L88)

```python
o_diff = (o - o_ref).abs()
print("output diff:", o_diff.max().item(), o_diff.mean().item())
assert torch.allclose(o, o_ref, atol=eps, rtol=eps), (
    (o - o_ref).abs().mean(),
    (o - o_ref).abs().max(),
)
```

梯度侧同构，比较的是 `[S, 3, H, D]` 的堆叠张量：

[tests/test_moba_attn.py:L90-L95](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L90-L95)

```python
gqkv_diff = (gqkv - gqkv_ref).abs()
print("grad diff:", gqkv_diff.max().item(), gqkv_diff.mean().item())
assert torch.allclose(gqkv, gqkv_ref, atol=eps, rtol=eps), (
    (gqkv - gqkv_ref).abs().mean(),
    (gqkv - gqkv_ref).abs().max(),
)
```

最后是比 allclose 更严的两层硬阈值——注意 mean（4e-4）比 max（4e-2）**严 100 倍**，这就是「系统性偏差零容忍、局部尖峰留余地」的量化表达（`gqkv_diff[:]` 里的 `[:]` 是无害的冗余切片）：

[tests/test_moba_attn.py:L97-L100](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py#L97-L100)

```python
assert o_diff.max() < 4e-2, f"o_diff max {o_diff.max()}"
assert o_diff.mean() < 4e-4, f"o_diff mean {o_diff.mean()}"
assert gqkv_diff[:].max() < 4e-2, f"gqkv_diff max {gqkv_diff[:].max()}"
assert gqkv_diff[:].mean() < 4e-4, f"gqkv_diff mean {gqkv_diff[:].mean()}"
```

顺带一提 `allclose` 的一个盲区：当参考值 \(b\) 接近 0 时，判定退化为 \(|a-b| \le \text{atol} = 2\times10^{-2}\)——即 **allclose 允许每个元素都差 2e-2**。注意力输出是凸组合（权重和为 1），不少元素落在 0 附近，这个放行幅度相当宽；mean < 4e-4 正好把这个洞堵上。

#### 4.4.4 代码实践

**实践目标**：量测当前实现的**真实误差水位**，理解阈值定在 4e-2/4e-4 的富余量。

**操作步骤**（示例代码，基于原测试改造为独立脚本/副本）：

1. 复制测试函数，注释掉全部断言，只保留 `print`；
2. 固定一个组合（如 `batch=4, head=4, seqlen=1024, chunk=256, topk=3`）运行 `pytest -s`；
3. 记录 print 出的 `output diff` 与 `grad diff` 的 max/mean；
4. 把 `eps` 从 2e-2 逐级减半（1e-2、5e-3、2e-3…），找到第一个失败的量级。

**需要观察的现象**：真实误差 max 大约落在 e-3 量级、mean 落在 e-5 ~ e-6 量级（按 4.4.2 的精度推导估计；具体数值待本地验证），距阈值有一个数量级以上的富余——这说明阈值不是贴着噪声 floor 定的，而是给「正常波动」留了余量、同时仍远低于任何真实 bug 的签名。

**预期结果**：`eps` 降到约 1e-3 以下时开始出现间歇性失败（bf16 舍入噪声被误判为错误）。若你的环境中 eps 降到 2e-3 仍稳定通过，说明误差主要来自单次输出舍入而非累加噪声——与推导一致。

#### 4.4.5 小练习与答案

**练习 1**：一个 bug 让**所有**输出均匀偏移 +0.01（绝对量），哪层断言抓住它？哪层可能放行？

**答案**：mean 断言最稳（均值恰为 0.01 > 4e-4，必抓）。allclose 对 |b| ≥ 0.5 的元素放行（容差 ≥ 0.02），只对接近 0 的元素报警；max 断言在偏移 < 4e-2 时放行。三层合起来则无死角。

**练习 2**：为什么梯度不单独给 q/k/v 设不同阈值，而是堆叠后统一判定？

**答案**：三个梯度的量级本来就可能不同（dk/dv 对全部选中它的 query 求和，dq 只对单行），但它们共享同一套 bf16 舍入来源与同一种错误签名。统一阈值简化维护；若某类梯度天然更噪（如 dk 在长序列上累加更多），再按张量分层设阈值也不迟——当前实现选择先统一，是对实际误差水位的经验取舍（其单侧性可作为二次开发时的观察点）。

## 5. 综合实践

**任务**：补上网格缺失的边界用例——`chunk_size` 大于序列长度时的行为，实践「先预测、再验证」的测试设计流程。这同时是 u3-l1 捷径分支与 u2-l2 退化情形的一次亲手复核。

### 5.1 实践目标

固定 `batch=1、head=2、seqlen=1024、head_dim=128、chunk_size=4096`，测试 `topk ∈ {1, 2}`：

- 高效实现应走 `need_moba_attn=False` 捷径，退化为纯因果注意力；
- naive 实现应通过 `min(moba_topk, num_block)=1` 只选唯一块（当前块），叠加 tril 后同样退化为纯因果注意力；
- 因此 topk=1 与 topk=2 的输出应当**一致**（两个 topk 都已退化），efficient 与 naive 之间只剩内核数值差。

### 5.2 操作步骤

**第一步：写下预测**（运行前必做）。逐条推演：

1. efficient：`batch_num_chunk = ceil(1024/4096) = 1`（[moba/moba_efficient.py:L21](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L21)）→ 唯一块是最后一块 → `num_filtered_chunk = 0` → `moba_topk = min(topk-1, 0) = 0`（topk=1、2 皆然）→ 捷径返回 `flash_attn_varlen_func(..., causal=True)`。
2. naive：`num_block = ceil(1024/4096) = 1`（[moba/moba_naive.py:L45](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_naive.py#L45)）→ `k = min(topk, 1) = 1`（topk=1、2 皆然）→ 唯一块对全部 query 是当前块（+inf 必选）→ 块掩码全开 → tril 收紧为因果 → 全量因果注意力。
3. 结论：两种 topk、两个实现，四组结果在数学上都是同一个函数（全量因果注意力）；组间只剩数值差。

**第二步：编写用例**（示例代码——复制到 `tests/test_moba_attn_oversize.py`，不要改动原测试文件）：

```python
# 示例代码
import torch
import pytest
from moba.moba_naive import moba_attn_varlen_naive
from moba.moba_efficient import moba_attn_varlen
from tests.test_moba_attn import generate_data


@pytest.mark.parametrize("moba_topk", [1, 2])
def test_attn_varlen_moba_oversize_chunk(moba_topk):
    batch, head, seqlen, head_dim = 1, 2, 1024, 128
    dtype, eps = torch.bfloat16, 2e-2

    q, k, v, cu_seqlen, max_seqlen = generate_data(
        batch, seqlen, head, head, head_dim, dtype
    )
    vo_grad = torch.randn_like(q)

    o = moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen,
                         moba_chunk_size=4096, moba_topk=moba_topk)
    torch.autograd.backward(o, vo_grad)
    gqkv = torch.stack((q._grad.clone(), k._grad.clone(), v._grad.clone()), dim=1)

    q._grad.zero_(); k._grad.zero_(); v._grad.zero_()
    o_ref = moba_attn_varlen_naive(q, k, v, cu_seqlen, max_seqlen,
                                   moba_chunk_size=4096, moba_topk=moba_topk)
    torch.autograd.backward(o_ref, vo_grad)
    gqkv_ref = torch.stack((q._grad.clone(), k._grad.clone(), v._grad.clone()), dim=1)

    o_diff = (o - o_ref).abs()
    gqkv_diff = (gqkv - gqkv_ref).abs()
    print("topk", moba_topk,
          "o diff:", o_diff.max().item(), o_diff.mean().item(),
          "grad diff:", gqkv_diff.max().item(), gqkv_diff.mean().item())

    assert torch.allclose(o, o_ref, atol=eps, rtol=eps)
    assert torch.allclose(gqkv, gqkv_ref, atol=eps, rtol=eps)
    assert o_diff.max() < 4e-2 and o_diff.mean() < 4e-4
    assert gqkv_diff.max() < 4e-2 and gqkv_diff.mean() < 4e-4
```

**第三步：运行并记录**：`pytest tests/test_moba_attn_oversize.py -s`，把两组 topk 的 max/mean 误差填进表格。

### 5.3 需要观察的现象与预期结果

- 两个 topk 取值下，`o diff` / `grad diff` 的 max/mean 应与原网格中「捷径组合」（如 `seqlen=512, chunk=1024`）的水位相当——因为都退化为「flash-attn 全量因果 vs naive 掩码 softmax」的纯数值对比；
- 误差 max 预计在 e-3 量级、mean 在 e-5 量级或更小（待本地验证）；
- 若把 topk=1 与 topk=2 的输出再互相比较（可在脚本里加一行 `(o1 - o2).abs().max()`），应看到**完全一致（0 或单个 bf16 ulp）**——同一实现、同数据、同内核，无随机性。

### 5.4 延伸（可选）

把 `seqlen` 改成 2048、`chunk_size` 保持 4096 之外再试 2048：此时单序列 2 块、过滤后 1 块，`moba_topk = min(topk-1, 1) = 1`——这是「恰好多选一块」的最小真实 MoBA 配置，可观察误差水位是否仍与捷径情形相当（预期相当：多出的 moba 支路同样是对齐良好的两路实现）。若在此配置下误差显著变大，说明 varlen 重组或 LSE 合并引入了额外噪声——这本身就是有价值的诊断信号。

## 6. 本讲小结

- **黄金参考策略**：用慢而直白的 `moba_naive` 当标准答案，对高效实现做「同输入 → 前向输出 + 反向梯度双对齐」的等价性验证，一次覆盖 u3 全部四步机制与手写反向。
- **`generate_data`** 固定三处种子保证可复现；`random.sample` 不放回抽样保证切分点互异、无空序列；`cu_seqlen` 天然覆盖「末块不满」「长度 1 的短序列」等 varlen 敏感面。
- **6 层参数化共 324 个用例**，其中 `chunk=1024` 与短 seqlen 的组合真实压到 `need_moba_attn=False` 捷径分支；`topk` 轴从 2 起步，`topk=1` 的退化留给了块数不足路径；GQA、非 128 头维度、CPU 均未覆盖。
- **双重建模的实验装置**：上游梯度 `vo_grad` 两路共用、梯度快照先 `clone` 后 `zero_`、高效侧 `deterministic=True` 保证归约可复现——任何一环破坏，梯度比较即刻失真。
- **三层容忍度互补**：`allclose(2e-2)` 抓大偏差（但放行 2% 相对差与近零元素 2e-2 绝对差）、`max < 4e-2` 抓局部尖峰（如个别块选择翻转）、`mean < 4e-4` 抓系统性小偏（如缩放、分母类 bug）——阈值基于 bf16 机器精度 \(2^{-8} \approx 3.9\times10^{-3}\) 推导并留出一个量级富余。
- **测试同时在守护 gate 数值一致性**：两条实现的 fp32 gate 打分若出现 top-k 翻转，输出差会打穿 max 断言。

## 7. 下一步学习建议

正确性解决了「算得对」，下一讲（u4-l3 性能测试）转向「算得快」：精读 [tests/test_moba_speedup.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py)，学习 GPU 基准的预热与 `torch.cuda.synchronize` 计时方法，并理解 chunk_size/topk/seqlen 如何决定 MoBA 的稀疏度与加速比（README 提到 32K 序列、单头、chunk 2048、topk 3 下约 40 倍于 naive 的加速，[README.md:L62](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md#L62)）。建议带着一个问题去读：本讲的容忍度体系（三层断言）在性能测试里完全没有对应物——两类测试对「可信度」的定义有何不同？
