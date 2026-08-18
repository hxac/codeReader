# 环境搭建与首次运行：读懂接口的三个输出

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立搭建一个能运行 `eplb.py` 的最小环境（Python + PyTorch，仅此而已），并跑通 README 中的两层 12 专家示例。
2. 准确说出 `eplb.rebalance_experts` 返回的三个张量 `phy2log`、`log2phy`、`logcnt` 的**形状**与**语义**，并能把 `phy2log` 的一行还原成「哪个 GPU 上放了哪些专家」的放置方案。
3. 通过修改 `num_replicas`、`num_gpus` 等参数重跑，观察输出形状与内容如何随之变化，并总结出参数必须满足的整除约束。

本讲只关注**接口层**：函数吃什么、吐什么、输出怎么读。算法内部（装箱、复制、多层映射）是第二单元的内容，本讲不展开。

## 2. 前置知识

### 2.1 没有打包文件的仓库如何被 import

大多数 Python 项目有 `pyproject.toml` / `setup.py`，安装后包名与目录解耦。但本仓库**没有任何打包文件**——用 Glob 扫一遍仓库根目录，只有 `README.md`、`LICENSE`、`eplb.py`、`example.png` 和 `.gitignore`。

这意味着 `eplb` 就是一个**纯 Python 模块**：Python 会按 `sys.path` 里的目录顺序查找 `eplb.py`。只要你在仓库根目录下启动 Python（当前目录默认在 `sys.path` 中），`import eplb` 就能成功；如果想在别处运行，需要把仓库根目录加进 `PYTHONPATH` 环境变量。这一点决定了后面所有实践的操作方式。

### 2.2 PyTorch 张量：形状、dtype 与设备

- **张量（tensor）**：多维数组，是 PyTorch 的基本数据结构。本讲只需要会看 `shape`（每一维的长度）和 `dtype`（元素类型，如 `torch.int64`）。
- **形状记号**：`[2, 16]` 表示 2 行 16 列的二维张量。EPLB 的所有输入输出第一维都是 **MoE 层编号**（一个模型有很多个 MoE 层，每层独立做负载均衡）。
- **设备（device）**：张量可以放在 CPU 或某块 GPU（`cuda:0` 等）上。记住一个事实：EPLB 内部会把计算搬到 CPU 上做（[eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 的 `.cpu()`），所以**CPU 版 PyTorch 完全够用**，本讲全程不需要 GPU。

### 2.3 承接前两讲：逻辑专家、物理专家与放置方案

- u1-l1 讲过：EP 下各专家负载差异大，EPLB 的做法是**复制重载的逻辑专家**形成若干**物理专家（副本）**，再把物理专家均匀装到各 GPU。
- u1-l2 讲过：物理专家总数 `num_replicas` 是固定预算；DeepSeek-V3 的组受限路由要求同组专家（含副本）尽量放同一节点。
- 本讲要读的三个输出，正是这套「复制 + 放置」方案的**三种读法**：
  - `phy2log`：从**物理槽位**查逻辑专家（正向表）；
  - `logcnt`：每个逻辑专家被复制了几份（计数表）；
  - `log2phy`：从**逻辑专家**查它的所有物理槽位（反向表）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的范围 |
| --- | --- | --- |
| `README.md` | 项目说明、算法策略描述、唯一的使用示例 | 示例代码与输出（L39-L57） |
| `eplb.py` | 全部实现，约 165 行、四个函数 | 入口 `rebalance_experts`（L131-L162）；其余三个函数只看签名 |
| `example.png` | README 示例对应的放置方案图 | 用来对照 `phy2log` 的读法 |

四个函数的关系一句话带过（下一讲 u1-l4 会详细画图）：`rebalance_experts` 是唯一入口（[eplb.py:L164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164) 的 `__all__` 也只导出它），它把工作分派给 `rebalance_experts_hierarchical`，后者再调用 `balanced_packing`（装箱）与 `replicate_experts`（复制）。

## 4. 核心概念与源码讲解

### 4.1 模块一：环境搭建——让 eplb.py 跑起来

#### 4.1.1 概念说明

这个仓库「轻」到什么程度？看 [eplb.py:L1-L3](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L1-L3)：

```python
from typing import Tuple

import torch
```

整个文件的第三方依赖**只有 `torch` 一个**（`typing` 是标准库）。没有版本锁定文件，没有打包配置，没有测试目录。所以环境搭建的目标极其明确：装一个能 import `torch` 的 Python 环境即可。

对初学者值得解释的还有 [eplb.py:L164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164)：

```python
__all__ = ['rebalance_experts']
```

`__all__` 控制 `from eplb import *` 时导出哪些名字。这里只导出入口函数——这是作者在告诉你：**`rebalance_experts` 是公开 API，其余三个函数是内部实现**。（`from eplb import balanced_packing` 这样显式导入仍然可行，但不属于承诺稳定的接口。）

#### 4.1.2 核心流程

环境搭建与首次运行的步骤：

```text
1. 确认 Python ≥ 3.8（PyTorch 2.x 的最低要求）
2. （推荐）创建虚拟环境：python -m venv .venv && source .venv/bin/activate
3. 安装 PyTorch（CPU 版即可）：
   pip install torch
   # 或明确用 CPU 源：pip install torch --index-url https://download.pytorch.org/whl/cpu
4. 在仓库根目录下启动 python，确认 import eplb 成功
5. 运行 README 示例，打印三个输出
```

#### 4.1.3 源码精读

README 的完整示例在 [README.md:L39-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L39-L57)：

```python
import torch
import eplb

weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                       [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])

num_replicas = 16
num_groups = 4
num_nodes = 2
num_gpus = 8

phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)
print(phy2log)

# Output:
# tensor([[ 5,  6,  5,  7,  8,  4,  3,  4, 10,  9, 10,  2,  0,  1, 11,  1],
#         [ 7, 10,  6,  8,  6, 11,  8,  9,  2,  4,  5,  1,  5,  0,  3,  1]])
```

这段示例给了一个**两层 MoE、每层 12 个逻辑专家**的模型：`weight` 形状 `[2, 12]`，第 0 行是第 0 层 12 个专家的负载统计（可以理解为每个专家历史处理的 token 数）。配置含义：

| 参数 | 值 | 含义 |
| --- | --- | --- |
| `num_replicas` | 16 | 复制后物理专家总数（预算），比 12 多出的 4 个就是「冗余专家」 |
| `num_groups` | 4 | 专家分 4 组，每组 3 个（组按专家编号连续切块） |
| `num_nodes` | 2 | 2 个服务节点 |
| `num_gpus` | 8 | 共 8 张 GPU，每节点 4 张 |

验证一下 README 对场景的描述（[README.md:L37](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L37)）：16 个副本 ÷ 8 GPU = 每 GPU 2 个专家；12 专家 ÷ 4 组 = 每组 3 个；4 组 ÷ 2 节点 = 每节点 2 组。数字全部对得上。

#### 4.1.4 代码实践

**实践一：跑通 README 示例**

1. 实践目标：搭建环境，亲眼看到三个输出张量被打印出来。
2. 操作步骤：
   1. 按上文 4.1.2 的步骤 1-3 装好环境；
   2. 把上面 README 示例存成仓库根目录下的 `run_example.py`（注意：必须与 `eplb.py` 同目录，或设置 `PYTHONPATH`）；
   3. 把最后一行 `print(phy2log)` 改成同时打印三个输出：

   ```python
   # 示例代码（基于 README 示例修改）
   phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)
   print("phy2log:", phy2log.shape, "\n", phy2log)
   print("log2phy:", log2phy.shape, "\n", log2phy)
   print("logcnt:", logcnt.shape, "\n", logcnt)
   ```

   4. 运行 `python run_example.py`。
3. 需要观察的现象：程序瞬间结束（数据量极小）；`phy2log` 两行输出与 README 注释完全一致。
4. 预期结果（`phy2log` 与 README 一致；`log2phy`、`logcnt` 的具体值是笔者按源码逻辑手工模拟推导的，**请以本地运行结果为准，待本地验证**）：

   ```text
   phy2log: torch.Size([2, 16])
    tensor([[ 5,  6,  5,  7,  8,  4,  3,  4, 10,  9, 10,  2,  0,  1, 11,  1],
            [ 7, 10,  6,  8,  6, 11,  8,  9,  2,  4,  5,  1,  5,  0,  3,  1]])
   log2phy: torch.Size([2, 12, 2])
    （每行 12 个二元组，复制过的专家两个槽位都有物理编号，未复制的第二个位置是 -1）
   logcnt: torch.Size([2, 12])
    tensor([[1, 2, 1, 1, 2, 2, 1, 1, 1, 1, 2, 1],
            [1, 2, 1, 1, 1, 2, 2, 1, 2, 1, 1, 1]])
   ```

   细心的读者会注意到 `logcnt` 两行都恰好加和为 16（`2+2+2+2+12×1` 的结构），这正是「物理专家总数守恒」的体现，4.3 节详细解释。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `python run_example.py` 必须在仓库根目录下运行（或设置 `PYTHONPATH`）？

答案：仓库没有打包文件，`eplb` 不是安装进 site-packages 的包，而是磁盘上的 `eplb.py` 文件。Python 按 `sys.path` 搜索模块；脚本所在目录默认排在 `sys.path` 首位，所以脚本与 `eplb.py` 同目录时 `import eplb` 才能找到。换个目录运行会得到 `ModuleNotFoundError: No module named 'eplb'`。

**练习 2**：执行 `from eplb import *` 之后，能直接使用 `balanced_packing` 吗？

答案：不能。[eplb.py:L164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164) 的 `__all__ = ['rebalance_experts']` 限制了星号导入的范围，只有入口函数被导出。想用内部函数必须显式写 `from eplb import balanced_packing`。

**练习 3**：如果我传入的 `weight` 是 CUDA 张量（假设本机有 GPU），程序会崩溃吗？

答案：不会。入口函数第一件事就是 `weight = weight.float().cpu()`（[eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149)），把输入统一搬到 CPU 再计算，输出也都在 CPU 上。这也是「CPU 版 PyTorch 就够用」的代码依据。

### 4.2 模块二：入口函数 rebalance_experts——输入、分派与返回

#### 4.2.1 概念说明

`rebalance_experts` 是整个仓库唯一对外承诺的函数。它做的事用一句话概括：**给定每个专家的负载统计和集群拓扑，返回一份「复制 + 放置」方案**。

它内部并不实现算法，而是做三件事：

1. **规范化输入**：转 float、搬到 CPU；
2. **分派策略**：按 `num_groups % num_nodes` 是否为 0，选择层级（hierarchical）或全局（global）策略——两者都由 `rebalance_experts_hierarchical` 承担，全局策略只是用退化参数调用它；
3. **组装第三个返回值**：前两个返回值直接来自层级函数，第三个 `log2phy` 由入口自己用 `scatter_` 构造。

「策略分派」承接 u1-l1 的结论：prefill 阶段 EP 规模小（组数能被节点数整除），用层级策略利用组受限路由；decode 阶段 EP 规模大（组数不被节点数整除），退化为全局策略。为什么全局策略能用「退化参数」复用同一实现，是 u2-l6 的重点，本讲只需要认识这个分派形状。

#### 4.2.2 核心流程

入口函数的伪代码：

```text
输入: weight [num_layers, num_logical_experts]   # 各层各专家的负载统计
      num_replicas, num_groups, num_nodes, num_gpus

1. 解包 num_layers, num_logical_experts = weight.shape
2. weight ← weight.float().cpu()                # 规范化：float32 + CPU
3. if num_groups % num_nodes == 0:
       (phy2log, phyrank, logcnt) ← hierarchical(weight, num_replicas,
                                                num_groups, num_nodes, num_gpus)
   else:  # 全局策略：退化为 1 个组、1 个节点、num_gpus 张卡
       (phy2log, phyrank, logcnt) ← hierarchical(weight, num_replicas, 1, 1, num_gpus)
4. maxlogcnt ← logcnt 的最大值
5. log2phy ← 形状 [num_layers, num_logical_experts, maxlogcnt] 的全 -1 张量
   再用 scatter 把每个物理槽位编号写进 (逻辑专家, 副本次序) 位置
6. return phy2log, log2phy, logcnt
```

#### 4.2.3 源码精读

函数签名与文档（[eplb.py:L131-L146](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L146)）：

```python
def rebalance_experts(weight: torch.Tensor, num_replicas: int, num_groups: int,
                      num_nodes: int, num_gpus: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    ...
    Parameters:
        weight: [layers, num_logical_experts], the load statistics for all logical experts
        num_replicas: number of physical experts, must be a multiple of `num_gpus`
        ...
    Returns: 
        physical_to_logical_map: [layers, num_replicas], the expert index of each replica
        logical_to_physical_map: [layers, num_logical_experts, X], the replica indices for each expert
        expert_count: [layers, num_logical_experts], number of physical replicas for each logical expert
    """
```

文档字符串直接给出了三个返回值的形状约定——`logical_to_physical_map` 的第三维写作 `X`，因为它等于运行时的 `maxlogcnt`（本例为 2），读代码时要注意这不是固定值。

**规范化输入**（[eplb.py:L148-L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L149)）：

```python
    num_layers, num_logical_experts = weight.shape
    weight = weight.float().cpu()
```

`.float()` 保证后续 `weight / logcnt` 这类除法是浮点运算（整型张量相除会截断出错）；`.cpu()` 把设备统一到 CPU，因为内部的 `balanced_packing` 里有逐元素的 Python 循环（[eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) 已经先把索引搬回 CPU）。设备一致性话题在 u3-l3 结合一次真实的设备修复 commit 展开。

**策略分派**（[eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)）：

```python
    if num_groups % num_nodes == 0:
        # use hierarchical load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 
                                                                  num_groups, num_nodes, num_gpus)
    else:
        # use global load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

判据就是一行取模。注意层级函数实际返回**三个**值（`phy2log, phyrank, logcnt`），入口只用 `phyrank`（每个副本是第几份）来组装 `log2phy`，不对外返回它。

**组装 log2phy**（[eplb.py:L157-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L161)）：

```python
    maxlogcnt = logcnt.max().item()
    log2phy: torch.Tensor = torch.full((num_layers, num_logical_experts, maxlogcnt), 
                                       -1, dtype=torch.int64, device=logcnt.device)
    log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank, 
            torch.arange(num_replicas, dtype=torch.int64, device=log2phy.device).expand(num_layers, -1))
```

这段是本讲最值得慢读的代码，逐步拆解：

1. `maxlogcnt = logcnt.max().item()`：全部层里单个专家的最大副本数。README 例子里是 2。
2. 先造一个 `[2, 12, 2]` 的全 **-1** 张量——`-1` 是占位符，表示「该专家没有第 r 份副本」。
3. `log2phy.view(num_layers, -1)` 把它压平成 `[2, 24]`，下标 `e * 2 + r` 与三维下标 `(e, r)` 一一对应。
4. `scatter_(-1, index, src)` 的语义：对每行的每个位置 j，执行 `dst[index[j]] = src[j]`。这里：
   - `index = phy2log * maxlogcnt + phyrank`：物理槽位 p 上放的是逻辑专家 `phy2log[p]` 的第 `phyrank[p]` 份副本，压平后正好落在槽位 `phy2log[p] * 2 + phyrank[p]`；
   - `src = arange(num_replicas)`：写入的值就是物理槽位编号 p 本身。
5. 效果：`log2phy[层, e, r] = p`——「逻辑专家 e 的第 r 份副本放在物理槽位 p」。没有副本写入的位置保持 -1。

这是一个非常经典的「**多维散布用压平索引一次完成**」技巧，值得记住。

**返回**（[eplb.py:L162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L162)）：`return phy2log, log2phy, logcnt`。

#### 4.2.4 代码实践

**实践二：改参数、看形状、撞断言**

1. 实践目标：建立「参数 → 输出形状」的直觉，并亲手触发一次参数约束报错。
2. 操作步骤：在实践一的脚本基础上，逐组替换参数并打印三个输出的形状：

   ```python
   # 示例代码：参数扫描
   import torch, eplb

   weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                          [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])

   configs = [
       # (num_replicas, num_groups, num_nodes, num_gpus)   说明
       (16, 4, 2, 8),   # README 原始配置：层级策略
       (24, 4, 2, 8),   # 更多冗余：每 GPU 3 个专家
       (16, 4, 2, 4),   # 更少 GPU：每 GPU 4 个专家
       (16, 4, 3, 8),   # 4 % 3 != 0：切换到全局策略
   ]
   for cfg in configs:
       phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, *cfg)
       print(cfg, "->", tuple(phy2log.shape), tuple(log2phy.shape), tuple(logcnt.shape),
             "maxlogcnt =", log2phy.shape[-1])

   # 再试一个非法配置（取消注释运行）：
   # eplb.rebalance_experts(weight, 18, 4, 2, 8)   # 18 % 8 != 0
   ```

3. 需要观察的现象：
   - `phy2log` 的第二维**永远等于 `num_replicas`**，`logcnt` 第二维永远等于逻辑专家数 12；
   - `log2phy` 第三维（`maxlogcnt`）随冗余数量变大而变大；
   - 最后一组配置走了另一条分支但不报错；
   - 取消注释后得到 `AssertionError`。
4. 预期结果（形状可由代码直接推出；具体数值**待本地验证**）：

   | 配置 (replicas, groups, nodes, gpus) | phy2log | log2phy | logcnt | 策略 |
   | --- | --- | --- | --- | --- |
   | (16, 4, 2, 8) | [2, 16] | [2, 12, 2] | [2, 12] | 层级 |
   | (24, 4, 2, 8) | [2, 24] | [2, 12, ≥3] | [2, 12] | 层级 |
   | (16, 4, 2, 4) | [2, 16] | [2, 12, 2] | [2, 12] | 层级 |
   | (16, 4, 3, 8) | [2, 16] | [2, 12, 2] | [2, 12] | 全局 |
   | (18, 4, 2, 8) | —— `AssertionError`（18 % 8 ≠ 0，见 [eplb.py:L95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95)） | | | | |

   参数合法条件汇总（都来自层级函数开头的断言 [eplb.py:L90-L95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L90-L95)）：

   \[ \text{num\_logical} \bmod \text{num\_groups} = 0,\quad \text{num\_groups} \bmod \text{num\_nodes} = 0,\quad \text{num\_gpus} \bmod \text{num\_nodes} = 0,\quad \text{num\_replicas} \bmod \text{num\_gpus} = 0 \]

   外加 `num_replicas ≥ num_logical_experts`（否则复制数位数为负，见 [eplb.py:L60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L60)）。全局策略分支因为内部用 `(1, 1, num_gpus)` 调用，只剩 `num_replicas % num_gpus == 0` 和 `num_replicas ≥ 12` 两条约束生效。

#### 4.2.5 小练习与答案

**练习 1**：`num_replicas=18, num_groups=4, num_nodes=2, num_gpus=8` 会发生什么？为什么？

答案：抛出 `AssertionError`。18 除以 8 不整除，违反 [eplb.py:L95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95) 的 `assert num_physical_experts % num_gpus == 0`。直观原因是放置方案要求每张 GPU 分到**同样数量**的专家（18 个专家无法均分给 8 张卡）。

**练习 2**：`num_groups=4, num_nodes=3`（其余同 README）走哪条分支？内部实际用什么参数调用层级函数？

答案：`4 % 3 = 1 ≠ 0`，走全局策略分支，即 [eplb.py:L155-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L155-L156)，实际调用 `rebalance_experts_hierarchical(weight, 16, 1, 1, 8)`——组数和节点数都被退化为 1，相当于「全世界只有一个节点、不分组」。

**练习 3**：某模型 3 个 MoE 层、24 个逻辑专家，配置 `num_replicas=32, num_groups=8, num_nodes=2, num_gpus=8`，三个输出的形状分别是什么？

答案：`phy2log` 为 `[3, 32]`（层数 × 物理专家数）；`logcnt` 为 `[3, 24]`；`log2phy` 为 `[3, 24, maxlogcnt]`，其中 \( \text{maxlogcnt} = \max_{l,e} \text{logcnt}[l,e] \)，只有运行后才知道（32 − 24 = 8 个冗余专家摊到 24 个逻辑专家上，理论上限为 9，实际通常远小于）。

### 4.3 模块三：读懂 phy2log、log2phy、logcnt

#### 4.3.1 概念说明

三个返回值是同一份放置方案的**三种读法**，服务三类使用者：

| 返回值 | 形状（本例） | 语义 | 谁用它 |
| --- | --- | --- | --- |
| `phy2log` | `[2, 16]` | `phy2log[layer, p]` = 物理槽位 p 上放的**逻辑专家编号** | 框架按它把专家权重写到各 GPU（正向表） |
| `logcnt` | `[2, 12]` | `logcnt[layer, e]` = 逻辑专家 e 被复制成几份 | 判断哪些专家被复制、路由时按副本数分流 |
| `log2phy` | `[2, 12, 2]` | `log2phy[layer, e, r]` = 逻辑专家 e 的第 r 份副本所在**物理槽位**，无该副本则为 -1 | 反查：给定逻辑专家找它所有副本的位置 |

「物理槽位」需要精确定义，这是读懂 `phy2log` 的钥匙：槽位编号 p **不是**随便排的，它按「节点 → 节点内 GPU → GPU 内槽位」的顺序编码。设每个 GPU 分到的专家数 \( \text{eph} = \text{num\_replicas} / \text{num\_gpus} \)（本例 16/8 = 2），则：

\[ \text{gpu}(p) = \left\lfloor \frac{p}{\text{eph}} \right\rfloor \]

层级策略下还可以定位节点（本例每节点 8 个槽位）：

\[ \text{node}(p) = \left\lfloor \frac{p}{\text{num\_replicas} / \text{num\_nodes}} \right\rfloor \]

这两个公式可以从层级函数末尾的 `view(num_layers, num_nodes, -1)` 展平（[eplb.py:L123-L125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)）和 `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack`（[eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119)）推导出来；本讲先记住结论，推导留到 u2-l5 的映射链精读。

#### 4.3.2 核心流程

用上面的公式把 README 输出的第 0 层 `[5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]` 还原成放置方案：

```text
p = 0..1   → GPU 0（节点 0）：{5, 6}
p = 2..3   → GPU 1（节点 0）：{5, 7}
p = 4..5   → GPU 2（节点 0）：{8, 4}
p = 6..7   → GPU 3（节点 0）：{3, 4}
p = 8..9   → GPU 4（节点 1）：{10, 9}
p = 10..11 → GPU 5（节点 1）：{10, 2}
p = 12..13 → GPU 6（节点 1）：{0, 1}
p = 14..15 → GPU 7（节点 1）：{11, 1}
```

这正是 [example.png](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/example.png) 画出的方案：两个节点面板，各 4 个 GPU 单元格，每个单元格分上下两行（Layer 0 / Layer 1），每行 2 个专家方框。

从这张「还原图」能直接读出三条关键性质（都承接 u1-l1/u1-l2 的结论，现在有了实证）：

1. **每 GPU 专家数相等**：每格恰好 2 个（`num_replicas / num_gpus`）。
2. **副本成对落在同一节点**：第 0 层被复制的是专家 {1, 4, 5, 10}，其中 5 的两个副本在 GPU 0/1（都在节点 0），4 在 GPU 2/3（节点 0），10 在 GPU 4/5（节点 1），1 在 GPU 6/7（节点 1）。没有任何专家的副本跨节点——这是层级策略对组受限路由的尊重。
3. **专家组不被节点拆散**：第 0 层的组是 {0,1,2}、{3,4,5}、{6,7,8}、{9,10,11}。节点 0 拿到组 {3,4,5}∪{6,7,8}，节点 1 拿到 {9,10,11}∪{0,1,2}，每组专家连同副本完整地待在一个节点里。

`logcnt` 与 `log2phy` 则是这张图的「另两种索引」。第 0 层手算推导值（与上面还原图逐位核对一致，**待本地验证**）：

| 逻辑专家 e | 副本数 logcnt[0,e] | 所在物理槽位 | log2phy[0,e] |
| --- | --- | --- | --- |
| 0 | 1 | 12 | [12, -1] |
| 1 | 2 | 13, 15 | [13, 15] |
| 2 | 1 | 11 | [11, -1] |
| 3 | 1 | 6 | [6, -1] |
| 4 | 2 | 5, 7 | [7, 5] |
| 5 | 2 | 0, 2 | [2, 0] |
| 6 | 1 | 1 | [1, -1] |
| 7 | 1 | 3 | [3, -1] |
| 8 | 1 | 4 | [4, -1] |
| 9 | 1 | 9 | [9, -1] |
| 10 | 2 | 8, 10 | [8, 10] |
| 11 | 1 | 14 | [14, -1] |

注意两个细节：

- 每行加和 \( \sum_e \text{logcnt}[l,e] = 16 = \text{num\_replicas} \)（物理专家总数守恒）；
- `log2phy[0, 5] = [2, 0]` 的顺序不是「槽位从小到大」，而是按**副本次序**（rank 0 是原始副本、rank 1 是复制品）排列——这是 4.2.3 散布代码中 `+ phyrank` 决定的。

#### 4.3.3 源码精读

三个返回值的权威定义就在入口函数的文档字符串里（[eplb.py:L143-L146](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L143-L146)）：

```python
    Returns: 
        physical_to_logical_map: [layers, num_replicas], the expert index of each replica
        logical_to_physical_map: [layers, num_logical_experts, X], the replica indices for each expert
        expert_count: [layers, num_logical_experts], number of physical replicas for each logical expert
```

README 对输出的解读说明在 [README.md:L59-L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L59-L62)——它说这行输出「指示了如下的专家复制与放置方案」，并配了 `example.png`。

`log2phy` 的 -1 填充与散布构造已经在 4.2.3 逐行拆过（[eplb.py:L158-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L158-L161)），这里补一个等价的一维小例子帮助消化（**示例代码**）：

```python
# 想象一层里只有 3 个逻辑专家、maxlogcnt = 2
# phy2log  = [2, 0, 0]     槽位0放专家2，槽位1、2放专家0的两个副本
# phyrank  = [0, 0, 1]     槽位1是专家0的原始副本，槽位2是它的复制品
# index    = phy2log*2 + phyrank = [4, 0, 1]
# scatter 后 log2phy 压平视图 = [1, 2, -1, -1, 0]
# 还原为 [3, 2]：专家0 -> [1, 2]，专家1 -> [-1, -1]，专家2 -> [0, -1]
```

一句话总结三个张量的关系：

\[ \text{phy2log}[l, p] = e \;\Longleftrightarrow\; \text{log2phy}\left[l, e, r\right] = p \ (\text{其中 } r = \text{phyrank}[l, p]) \;\Longleftrightarrow\; p \text{ 是 } \text{logcnt}[l,e] \text{ 个副本之一} \]

它们互为正反查表，`logcnt` 则是 `log2phy` 每行里非 -1 元素的个数。

#### 4.3.4 代码实践

**实践三（本讲主实践）：数一数——验证 phy2log 与 logcnt/log2phy 的一致性**

1. 实践目标：用程序证明「`phy2log` 中每个逻辑专家 id 的出现次数等于 `logcnt`」，即正向表和计数表说的是同一件事。
2. 操作步骤：

   ```python
   # 示例代码：一致性自检
   import torch, eplb

   weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                          [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
   phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, 16, 4, 2, 8)

   num_layers, num_logical = weight.shape
   for l in range(num_layers):
       # 检查 1：出现次数 == logcnt
       counts = torch.bincount(phy2log[l], minlength=num_logical)
       assert torch.equal(counts, logcnt[l]), f"layer {l}: counts != logcnt"

       # 检查 2：每个逻辑专家至少 1 个副本
       assert (logcnt[l] >= 1).all()

       # 检查 3：log2phy 与 phy2log 互逆（跳过 -1 占位）
       for e in range(num_logical):
           for r in range(log2phy.size(-1)):
               p = log2phy[l, e, r].item()
               if p != -1:
                   assert phy2log[l, p].item() == e

       # 检查 4：logcnt 总和 == num_replicas（物理专家守恒）
       assert logcnt[l].sum().item() == 16
   print("all checks passed")
   ```

3. 需要观察的现象：所有断言通过，打印 `all checks passed`；若把 `num_replicas` 换成 24 再跑，检查 4 的断言需要同步改成 24 才通过。
4. 预期结果：`torch.bincount` 统计第 0 层得到 `tensor([1, 2, 1, 1, 2, 2, 1, 1, 1, 1, 2, 1])`，与 4.3.2 表格的 logcnt 列一致（**待本地验证**；该推导已与 README 的 phy2log 输出逐位核对）。

**实践四（选做加分）：算一算——每 GPU 真实负载**

1. 实践目标：验证这份方案确实在「拉平」GPU 负载。每个副本承担的负载是 \( \text{weight}[e] / \text{logcnt}[e] \)（副本均摊 token）。
2. 操作步骤：

   ```python
   # 示例代码：每 GPU 负载
   eph = 16 // 8                       # phy_experts_per_gpu
   per_copy = weight[0].float() / logcnt[0].float()
   gpu_load = torch.zeros(8)
   for p, e in enumerate(phy2log[0].tolist()):
       gpu_load[p // eph] += per_copy[e]
   print("每 GPU 负载:", gpu_load.tolist())
   print("总和:", gpu_load.sum().item(), "应等于", weight[0].sum().item())
   ```

3. 需要观察的现象：总和等于 `weight[0].sum()`（复制不增减计算量，只是均摊）；各 GPU 负载接近但不完全相等。
4. 预期结果：手算推导每 GPU 负载为 `[121.5, 86.5, 125.0, 113.0, 147.5, 131.5, 156.0, 152.0]`，总和 1033 = 第 0 层权重之和；最大/最小 ≈ 1.8，最大/均值 ≈ 1.21（**待本地验证**）。均衡并不完美——贪心启发式不保证最优，这正是 u3-l2 要定量评估、u3-l5 要做变体改进的伏笔。

#### 4.3.5 小练习与答案

**练习 1**：不看上文，`phy2log[0]` 中编号 5 出现在哪些槽位？它们分别属于哪个节点、哪个 GPU？

答案：槽位 0 和 2。节点 = 槽位 ÷ (16/2) = 槽位 ÷ 8，都是 0；GPU = 槽位 ÷ (16/8) = 槽位 ÷ 2，分别是 GPU 0 和 GPU 1。两个副本同节点，符合层级策略「副本不跨节点」的性质。

**练习 2**：`log2phy[0, 2]` 的值是什么？为什么有 -1？

答案：`[11, -1]`。专家 2 在第 0 层只有 1 个副本（`logcnt[0, 2] = 1`），落在物理槽位 11；第三维长度是全局的 `maxlogcnt = 2`，于是第 2 份副本的位置不存在，用 -1 占位。-1 填充来自 [eplb.py:L158-L159](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L158-L159) 的 `torch.full(..., -1, ...)`，之后只有真实副本会被 scatter 覆盖。

**练习 3**：`log2phy` 的第三维为什么用 `maxlogcnt`（所有专家中的最大副本数），而不是干脆取 `num_replicas` 那么长？

答案：为了省空间且对齐。每个逻辑专家的副本数不同（本例 1 或 2），但张量必须是规整矩形，所以取最大值作为公共长度、用 -1 补齐稀疏位置。如果取 `num_replicas` 长度，绝大多数位置都是 -1，浪费 \( O(\text{num\_replicas}^2) \) 空间；取 `maxlogcnt` 只浪费极少数槽位（未达最大副本数的专家）。`maxlogcnt` 由 [eplb.py:L157](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157) 现场从 `logcnt` 求出。

## 5. 综合实践

**任务：制作你的「参数 → 方案」速查手册**

写一个脚本 `my_eplb_report.py`（放在仓库根目录），对多组配置批量运行 `rebalance_experts` 并自动输出报告，把 u1-l2 的纸笔方案与算法结果对照。要求：

1. 至少覆盖 4 组合法配置：README 原配置、加倍冗余（`num_replicas=24`）、减半 GPU（`num_gpus=4`）、触发全局策略（`num_nodes=3`）；
2. 对每组配置自动打印：
   - 三个输出的形状与 `maxlogcnt`；
   - 还原后的「GPU → 专家集合」放置表（用 4.3.2 的整除公式计算，不要求读图）；
   - 被复制的逻辑专家清单（`logcnt > 1` 的位置）及其副本所在节点；
   - 每层 `logcnt` 的总和（应恒等于 `num_replicas`）；
3. 把第 1 组的放置表与你 u1-l2 手工设计的 12 专家纸笔方案对比：算法的组-节点分配和你的一致吗？你手工没解决的负载不均，算法改善了多少（用每 GPU 负载的 max/min 衡量）？
4. 额外做一次失败实验：故意传入 `num_replicas=18`，用 `try/except AssertionError` 捕获并打印友好提示，验证约束表。

**验收标准**：脚本一次跑通全部配置无断言失败；报告能回答「冗余专家数量从 4 涨到 12 时，`maxlogcnt` 和最重 GPU 的负载各怎么变」。具体数值**待本地验证**。

## 6. 本讲小结

- EPLB 仓库**没有打包文件**，`eplb.py` 是纯 Python 模块，唯一第三方依赖是 `torch`；CPU 版 PyTorch 即可运行（入口在 [eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 统一 `.float().cpu()`）。
- `rebalance_experts`（[eplb.py:L131-L162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L162)）是唯一公开 API：按 `num_groups % num_nodes` 是否为 0 分派层级/全局策略，后者用 `(1, 1, num_gpus)` 退化参数复用同一实现。
- 三个输出是同一放置方案的三种读法：`phy2log [L, R]` 正向表（槽位 → 逻辑专家）、`logcnt [L, E]` 副本计数、`log2phy [L, E, maxlogcnt]` 反向表（-1 补齐稀疏副本）。
- 物理槽位按「节点 → GPU → 槽内位置」编码：`gpu = p // (num_replicas/num_gpus)`，`node = p // (num_replicas/num_nodes)`，据此可把 `phy2log` 的一行直接还原成 `example.png` 那样的放置图。
- 一致性不变量：`phy2log` 中各 id 出现次数 == `logcnt`；每行 `logcnt` 之和 == `num_replicas`；`phy2log` 与 `log2phy` 互逆（跳过 -1）；每 GPU 恰好 `num_replicas/num_gpus` 个专家；层级策略下同组专家与同专家的副本不跨节点。
- 参数必须满足四条整除约束加一条下限约束（`num_replicas ≥ 逻辑专家数`），违反即 `AssertionError`；方案是贪心启发式，均衡但不完美（本例最大/均值 ≈ 1.21）。

## 7. 下一步学习建议

下一讲 **u1-l4「代码地图：四个函数的分工与数据流」** 将把本讲「只看接口」的视角推进到 `eplb.py` 全景：画出 `rebalance_experts → rebalance_experts_hierarchical → balanced_packing / replicate_experts` 的调用关系图，整理每个函数的输入输出形状，并预览层级策略「组打包到节点 → 节点内复制 → 物理专家打包到 GPU」三步中张量形状的演变——那是进入第二单元逐函数精读前的最后一张地图。

若想现在就多看一眼源码，推荐带着本讲的两个问题去读：`rebalance_experts_hierarchical` 为什么能同时服务两种策略（对照 [eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)）；`log2phy` 构造里 `phy2log * maxlogcnt + phyrank` 这个压平索引技巧还在哪些地方出现（提示：[eplb.py:L106-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107) 与 [eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 有同款「乘法 + 偏移」编码）。
