# 入口函数与全局策略:分派、退化复用与逆映射构造

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `rebalance_experts` 的三段式结构:输入规范化 → 策略分派 → 逆映射组装,以及每一段在源码中的位置。
2. 解释 `num_groups % num_nodes == 0` 这个分派条件为什么恰好对应「prefill 用层级策略、decode 用全局策略」的工程分工。
3. 把全局策略的调用参数 `(num_replicas, 1, 1, num_gpus)` 代入层级函数,逐条推演它如何退化为「全局复制 + GPU 级装箱」,并说明这是 commit `e1100fe` 的一次真实重构。
4. 读懂 `log2phy` 的构造代码:扁平化地址 `phy2log * maxlogcnt + phyrank`、一次 `scatter_` 完成三维写入、`-1` padding 恰好出现在副本数未达最大值的尾部槽位。

本讲是第二单元的收官:前几讲已经把层级策略的三步逐段拆完,本讲把它们装回入口函数,并补上最后一个没讲的分支——全局策略。

## 2. 前置知识

### 2.1 你已经掌握的(简要回顾)

- **层级策略三步**(u2-l4、u2-l5):`rebalance_experts_hierarchical` 把「组装箱到节点 → 节点内复制冗余专家 → 物理专家装箱到 GPU」串成一条流水线,返回 `pphy2log / pphyrank / logcnt` 三件套。
- **映射链合成规律**(u2-l5):`Z2Y = X2Y.gather(-1, Z2X)`,index 永远取「目的地 → 来源」方向。
- **rank 的用途**(u2-l2):`replicate_experts` 为每个物理专家标注「它是自己逻辑专家的第几个副本」,使得 `(逻辑编号, rank)` 构成唯一地址——本讲会看到这个地址被 `log2phy` 的构造直接消费。
- **scatter_ 的写侧语义**(u2-l3):按 index 把 source 写入目标张量,**未被覆盖的位置保持原值**——这正是 `-1` padding 的来源。

### 2.2 本讲的新铺垫:变长数据的「定长化」

不同逻辑专家的副本数不同(有的 1 个,有的 3 个),但张量必须是规则形状(每维长度固定)。工程上的常见做法是:

1. 取一个全局上界 `maxlogcnt` 作为第三维长度;
2. 用一个**哨兵值**(这里选 `-1`,因为合法槽位号都是非负整数)填充「不存在副本」的槽位。

`log2phy` 就是一个「变长列表的定长张量化」实例。理解了这一点,`torch.full(..., -1, ...)` 加一次 `scatter_` 的两行代码就不再神秘。

### 2.3 本讲要回答的三个问题

1. 用户面对的只有一个函数 `rebalance_experts`,它怎么决定用哪种策略?
2. 全局策略为什么**没有自己的一行算法代码**,却能做到「全局复制 + GPU 级均衡」?
3. 返回值中的 `log2phy` 是怎么从 `phy2log / phyrank / logcnt` 三件套反向组装出来的?

## 3. 本讲源码地图

整个仓库只有一个算法文件 `eplb.py`(165 行)。本讲的主角是最后一段,其余函数作为被复用/被对照的对象列出:

| 文件 | 行号 | 角色 |
| --- | --- | --- |
| `eplb.py` L131-L162 | `rebalance_experts` | **本讲主角**:唯一公开入口,规范化 + 分派 + 组装 `log2phy` |
| `eplb.py` L74-L129 | `rebalance_experts_hierarchical` | 被两个分支共同复用的层级策略实现 |
| `eplb.py` L22-L25 | `balanced_packing` 的平凡分支 | 退化参数下 Step 1 命中的恒等捷径 |
| `eplb.py` L44-L71 | `replicate_experts` | 全局策略在 `e1100fe` 之前的旧实现主体(对照用) |
| `eplb.py` L164 | `__all__` | 只有 `rebalance_experts` 被导出,印证「唯一入口」 |
| `README.md` | 策略说明 | hierarchical/global 与 prefill/decode 的对应关系 |

## 4. 核心概念与源码讲解

本讲只有一个最小模块 `rebalance_experts`,按「入口骨架 → 全局策略 → 逆映射组装」拆成三节。

### 4.1 入口函数:输入规范化与策略分派

#### 4.1.1 概念说明

`rebalance_experts` 是外部世界与 EPLB 算法之间唯一的边界(`__all__` 也只导出它,见 [eplb.py:164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164))。作为入口,它承担三件「脏活」:

1. **规范化输入**:`weight.float().cpu()` —— 不管调用方传的是 `int64` 的 token 计数、`float16` 的低精度统计,还是 CUDA 张量,统一变成 CPU 上的 `float32`。
2. **选择策略**:按 `num_groups % num_nodes` 是否为 0 分派两条路径。
3. **补齐输出**:层级/全局策略返回的是 `phy2log / phyrank / logcnt` 三件套,而对外承诺的返回值是 `phy2log / log2phy / logcnt`——入口要在返回前把 `phyrank`「消费」掉,反向组装出 `log2phy`(见 4.3)。

前两件在本节讲,第三件留给 4.3。

#### 4.1.2 核心流程

```text
rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)
│
├─ ① 记录形状:num_layers, num_logical_experts = weight.shape
│
├─ ② 规范化:weight = weight.float().cpu()
│
├─ ③ 分派:
│     if num_groups % num_nodes == 0:      # 节点数整除组数
│         调 hierarchical(weight, num_replicas, num_groups, num_nodes, num_gpus)
│     else:                                 # 不整除
│         调 hierarchical(weight, num_replicas, 1, 1, num_gpus)   # 退化复用!
│     两条路径都得到 (phy2log, phyrank, logcnt)
│
├─ ④ 组装:maxlogcnt = logcnt 的全局最大值
│     log2phy = 全 -1 的 [L, E, maxlogcnt] 张量
│     一次 scatter_ 写入所有有效槽位(见 4.3)
│
└─ ⑤ return phy2log, log2phy, logcnt
```

#### 4.1.3 源码精读

函数签名与文档(对外口径:物理专家总数叫 `num_replicas`,与层级函数内部的 `num_physical_experts` 是同一个量的两个名字):

[eplb.py:131-141](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L141) 定义入口,参数含义:weight 是各层各逻辑专家的负载统计,`num_replicas` 必须被 `num_gpus` 整除,`num_gpus` 必须被 `num_nodes` 整除。

规范化的一行:

[eplb.py:148-149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L149) 先取出层数与逻辑专家数,然后把 weight 统一转成 CPU 上的 `float32`。

这一行有两个动机,分别对着后面的实现细节:

- **`.cpu()`**:`balanced_packing` 内部是 Python 双重循环,逐元素读写张量([eplb.py:30-40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L30-L40)),并且它自己已经强制把中间量放在 CPU 上(`indices = weight.float().sort(-1, descending=True).indices.cpu()` 与 `device='cpu'`,见 [eplb.py:27-29](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L29))。Python 循环里逐个访问 CUDA 张量元素,每一次下标访问都要做一次主机-设备同步,慢几个数量级。与其让数据在 GPU 与 CPU 之间来回搬,不如在入口一次性搬干净。
- **`.float()`**:负载统计常是整数 token 计数(`int64`),也可能是训练框架用低精度累积的 `float16`/`bfloat16`。后面的 `weight / logcnt`(复制贪心的水位,见 [eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67))和 Step 3 的 `tokens_per_mlog / mlogcnt`(见 [eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117))都涉及除法,大整数除法在整数类型下会截断,低精度浮点会失真;统一转 `float32` 后数值行为可预期。

分派的四行:

[eplb.py:150-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156) 当 `num_groups % num_nodes == 0`(节点数整除组数)时走层级策略,原样透传四个参数;否则走全局策略——注意它**没有调用别的函数**,而是以 `num_groups=1, num_nodes=1` 调用同一个层级函数,只保留 `num_gpus` 不变。

README 对这两条路径的描述([README.md:19-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L19-L31)):层级策略用于「节点数整除组数」的情形,利用组受限路由把同组专家放进同一节点,适合 **prefill 阶段较小的专家并行规模**;全局策略无视分组,全局复制后直接装箱到 GPU,适合 **decode 阶段较大的专家并行规模**。

为什么「整除性」恰好和「prefill/decode」对上?直觉如下:

- prefill 阶段 EP 规模小 → GPU 少 → 节点数少(如 2、4)→ 组数(DeepSeek-V3 为 64)大概率是节点数的整数倍 → 命中层级分支,组约束带来的流量收益可以兑现;
- decode 阶段 EP 规模大 → 跨很多节点 → 组数往往不再是节点数的整数倍 → 命中全局分支,此时强行维持组-节点对齐的收益下降,不如专心做 GPU 级负载均衡。

于是分派条件不是拍脑袋选的,而是「组-节点对齐什么时候可行」的可行性检查:`num_groups % num_nodes == 0` 正是层级函数内部 `assert num_groups % num_nodes == 0`(见 [eplb.py:92](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L92))的前置镜像——入口先替层级函数把不可行的参数拦下来改道,避免断言爆炸。

#### 4.1.4 代码实践:预测并验证分派条件

1. **实践目标**:对若干参数组合,先手工判断走哪个分支,再用输出结构反验证。
2. **操作步骤**(示例代码):

   ```python
   # 示例代码(非项目原有)
   import torch, eplb

   cases = [(4, 2), (4, 3), (6, 3), (2, 1), (64, 8)]
   for g, n in cases:
       print(f"num_groups={g}, num_nodes={n}, g % n = {g % n}, "
             f"分支 = {'hierarchical' if g % n == 0 else 'global'}")
   ```

3. **需要观察的现象**:`(4,3)` 是列表中唯一不整除的组合;`(2,1)` 虽然「节点少」,但因为任何数都能被 1 整除,它走的是 **hierarchical**,不是 global。
4. **预期结果**:输出五行,前两个与后两个为 hierarchical,`(4,3)` 为 global。此判断可直接对照 [eplb.py:150](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150) 的条件,无需运行也能确定;运行仅为建立手感。

#### 4.1.5 小练习与答案

**练习 1**:把 `.cpu()` 从入口删掉(假设 weight 在 CUDA 上),函数还能运行吗?会发生什么?

<details><summary>参考答案</summary>

大概率仍能「运行」但显著变慢:`balanced_packing` 内部 `pack_index` 建在 `device='cpu'`([eplb.py:28](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28))而 `weight` 留在 GPU,`pack_weights[pack] += weight[i, group]` 每次都触发跨设备同步;此外混用设备还可能在 `torch.full_like(weight, ..., device='cpu')` 处直接报设备不匹配错误。入口统一 `.cpu()` 让所有后续代码只面对 CPU 张量。
</details>

**练习 2**:调用方传入 `float16` 的 weight,不经过 `.float()` 直接调用层级函数,最可能在哪里出数值问题?

<details><summary>参考答案</summary>

两处除法:`replicate_experts` 的 `weight / logcnt`(贪心水位,[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67))和 Step 3 的 `tokens_per_mlog / mlogcnt`([eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117))。`float16` 只有约 3 位十进制有效数字,token 计数上百万时会丢精度,导致水位比较失真、复制选择错误——不是报错,而是**悄悄变差**,更难察觉。
</details>

**练习 3**:`num_groups=2, num_nodes=1` 与 `num_groups=2, num_nodes=3` 各走哪条分支?

<details><summary>参考答案</summary>

前者 `2 % 1 == 0` → hierarchical(注意:任何组数都能被 1 个节点整除,「单节点」永远走层级分支,此时层级策略在单节点内做组装箱+复制+GPU 装箱);后者 `2 % 3 == 2` → global。顺带一提,后者还要求 `num_gpus` 与 `num_replicas` 满足各自整除约束才能通过层级函数的断言([eplb.py:94-95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L94-L95))。
</details>

### 4.2 全局策略:用退化参数 (1, 1, P) 复用层级实现

#### 4.2.1 概念说明

读 [eplb.py:156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L156) 时最容易产生的困惑是:**全局策略的代码在哪里?** 答案是:没有单独的代码。它把层级函数的三个「划分参数」全部退化——`num_groups=1`(全部专家算一组)、`num_nodes=1`(整个集群算一个节点)——层级流水线就自动变成了「全局复制 + GPU 级装箱」。

这是一种值得记住的设计模式:**用退化参数复用通用实现**,而不是为每种特例写一条独立路径。它的收益是只维护一份映射链合成逻辑;代价是阅读时必须能识破「哪些步骤在退化参数下变成了恒等操作」。

#### 4.2.2 核心流程

把 `(num_physical_experts=M, num_groups=1, num_nodes=1, num_gpus=P)` 代入层级函数,逐段推演:

| 层级函数内部 | 通用语义 | 退化后 (1,1,P) 的行为 |
| --- | --- | --- |
| 断言块(L90-L96) | 四条整除检查 | 全部平凡成立;`group_size=E`,`groups_per_node=1` |
| Step 1(L103-L108)组装箱到节点 | G 个组装箱到 N 个节点 | 1 个「组」装箱到 1 个「节点」→ 命中 `balanced_packing` 的 `groups_per_pack==1` 平凡分支,`log2mlog` 成为**恒等置换** |
| Step 2(L110-L113)节点内复制 | 每节点在 E/N 个专家里复制到 M/N | 1 个「节点」在**全部 E 个专家**里复制到 M 个 → **全局复制** |
| Step 3(L115-L119)装箱到 GPU | 节点内 P/N 张 GPU 间装箱 | **全部 P 张 GPU** 间装箱 → **GPU 级负载均衡** |
| 映射链合成(L120-L129) | mlog/pphy 多层翻译 | Step 1 恒等后链路缩短,但代码一行不改 |

其中 Step 1 的退化依赖你在 u2-l1 见过的平凡分支:

[eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) 当每个包只装得下 1 个物品时,`pack_index` 直接是 `0,1,2,...` 的广播、`rank_in_pack` 全 0,跳过整个贪心循环。全局策略的 `(num_groups=1, num_packs=1)` 恰好命中它。

#### 4.2.3 源码精读

[eplb.py:154-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L154-L156) 全局分支的全部代码:以 `(num_replicas, 1, 1, num_gpus)` 调用层级函数。注意被「扔掉」的信息:用户的 `num_groups` 与 `num_nodes` 都不再传入,分组与节点边界对全局策略没有意义。

这行代码不是一开始就长这样的。查看 git 历史:

```bash
git show e1100fe -- eplb.py
```

commit `e1100fe`(2025-03-21,"add gpu-level load balance for global policy",关闭 issue #14)之前,全局分支是:

```python
# 旧实现(e1100fe 之前)
phy2log, phyrank, logcnt = replicate_experts(weight, num_replicas)
```

旧实现只做全局复制、**不做 GPU 装箱**:`replicate_experts` 返回的 `phy2log` 前 E 列是 `0..E-1`、后 M−E 列是被复制专家的编号([eplb.py:62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62) 的初始化方式),把它直接当槽位表用,意味着各 GPU 分到的副本组合没有任何负载均衡——重副本可能挤在同一张卡上。修复方式不是给全局策略新写一套装箱,而是**换一行调用**:借层级函数的 Step 3 拿到 GPU 级均衡,同时 Step 1 自动退化、Step 2 语义不变。一行 diff 修掉一个架构级缺陷,这正是退化复用模式的维护优势的实证。

对照两版行为:

| | 旧全局实现(`replicate_experts` 直调) | 现全局实现(`hierarchical(1,1,P)`) |
| --- | --- | --- |
| 复制范围 | 全局 | 全局(不变) |
| GPU 装箱 | 无,槽位表未均衡 | 有,Step 3 的 `balanced_packing` |
| 输出结构 | `phy2log` 非置换(前 E 列有序) | `phy2log` 是均衡放置后的置换 |

#### 4.2.4 代码实践:触发全局策略并与层级策略对照(本讲主实践之一)

1. **实践目标**:用不整除的参数触发全局策略;证明它等价于对层级函数的退化调用;观察两种策略输出的结构差异。
2. **操作步骤**(示例代码):

   ```python
   # 示例代码(非项目原有)
   import torch, eplb

   weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                          [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]],
                         dtype=torch.float32)
   M, P = 16, 8   # 物理专家总数、GPU 数,两次调用保持一致

   # A:hierarchical(4 % 2 == 0),即 README 示例
   p2l_h, l2p_h, cnt_h = eplb.rebalance_experts(weight, M, num_groups=4, num_nodes=2, num_gpus=P)

   # B:global(4 % 3 != 0),触发 else 分支
   p2l_g, l2p_g, cnt_g = eplb.rebalance_experts(weight, M, num_groups=4, num_nodes=3, num_gpus=P)

   # C:绕过入口,直接以退化参数调用层级函数
   p2l_c, rank_c, cnt_c = eplb.rebalance_experts_hierarchical(weight, M, 1, 1, P)

   # 验证退化复用:B 与 C 应逐元素相等
   assert torch.equal(p2l_g, p2l_c) and torch.equal(cnt_g, cnt_c)
   print("global 分支 == hierarchical(1,1,P)  ✓")

   print("hierarchical phy2log:\n", p2l_h)
   print("global         phy2log:\n", p2l_g)
   print("hierarchical logcnt:\n", cnt_h)
   print("global         logcnt:\n", cnt_g)
   ```

   说明:`rebalance_experts_hierarchical` 虽不在 `__all__` 里,但它是模块级函数,`import eplb` 后可以直接属性访问。
3. **需要观察的现象**:
   - 断言通过——全局分支的输出与退化调用 bit 级一致,坐实「无独立实现」;
   - `p2l_h` 呈**节点分块**结构:12 专家分 4 组(每组 3 个,组 i = {3i, 3i+1, 3i+2}),层 0 的槽位 0-7(节点 0)只出现组 1、组 2 的专家编号 3-8,槽位 8-15(节点 1)只出现 {0,1,2,9,10,11}(组 3、组 0);README 给出的层 0 输出 `[5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]` 可直接印证([README.md:55-56](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55-L56));
   - `p2l_g` **没有**这种分块:任意专家的副本可以落在任意槽位;
   - `cnt_h` 与 `cnt_g` 不同:hierarchical 每节点独立复制(每节点 6 个逻辑专家复制到 8 个物理槽位,每层每节点 2 个冗余),global 在全部 12 个里复制 4 个。
4. **预期结果**:按 `weight / logcnt` 水位贪心手工推导,层 0 的 global 复制顺序应为 183(专家 10)→ 165(专家 5)→ 132(专家 1)→ 104(专家 4),即 `cnt_g[0] = [1,2,1,1,2,2,1,1,1,1,2,1]`(各行和为 16);层 1 复制 5、6、8、7。**待本地验证**:运行后核对你的推导与打印是否一致,若有出入请回看 u2-l2 的贪心规则再推导一次。

#### 4.2.5 小练习与答案

**练习 1**:既然全局分支只是换参数调用层级函数,为什么不干脆删掉 `if/else`,让层级函数自己判断?

<details><summary>参考答案</summary>

可以,但入口的分派承担的是**对用户的语义**:「层级还是全局」是部署者基于 prefill/decode 阶段做出的选择,而整除性是这一选择的**可行性判据**。把判据留在入口,层级函数内部就能只保留纯粹的断言(不可行即报错),职责清晰:入口负责「改道」,内部负责「执行 + 校验」。此外入口还统一承担了 `float().cpu()` 规范化与 `log2phy` 组装,删掉 `if/else` 并不能省掉这层包装。
</details>

**练习 2**:退化调用下,Step 1 生成的 `log2mlog` 是什么?对后续数据流有什么影响?

<details><summary>参考答案</summary>

是恒等置换 `arange(E)`:`balanced_packing` 命中 `groups_per_pack==1` 平凡分支返回 `pack_index=i, rank=0`,代入 [eplb.py:106-107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107) 的编码公式得 `((0*1+0)*E + arange(E))`。于是 Step 2 的 `gather(-1, mlog2log)` 等于原样取 weight,Step 2 变成对整个 `[L, E]` 直接调用 `replicate_experts(weight, M)`;末尾 `logcnt` 映射回 log 序也因恒等而不重排。整条链路里 mlog 与 log 编号重合,「节点分块重编号」自然消失。
</details>

**练习 3**:退化调用要求满足哪些整除约束?用户的 `(num_replicas, num_gpus)` 需要额外满足什么?

<details><summary>参考答案</summary>

层级函数断言([eplb.py:90-95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L90-L95))在 `(1,1,P)` 下前三条(`E % 1`、`1 % 1`、`P % 1`)自动成立,唯一实质约束是 `num_physical_experts % num_gpus == 0`,即 `M % P == 0`——这与入口 docstring 对 `num_replicas` 的要求([eplb.py:138](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L138))完全一致,所以用户无需为全局策略记新约束。另外 `M >= E`(有冗余可复制,见 [eplb.py:59-60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L59-L60))。
</details>

### 4.3 log2phy 逆映射:扁平化 scatter 与 -1 padding

#### 4.3.1 概念说明

两条策略分支返回的都是「正向表」视角:`phy2log[l, s]` 回答「槽位 s 放的是哪个逻辑专家」。但训练/推理框架在重排权重和路由表时,更自然的问法是反向的:「逻辑专家 e 的第 k 个副本在哪个槽位?」——这就是 `log2phy[l, e, k]`。

难点在于变长:专家 e 的副本数 `logcnt[l, e]` 各不相同,而张量第三维必须定长。解决方案就是 2.2 节说的「取全局最大值 `maxlogcnt` + `-1` 哨兵」。真正精巧的是**写入方式**:不是对每个 (l, e, k) 三重循环赋值,而是把三维地址压成一维、用一次 `scatter_` 完成——用的是和 Step 1 的 `log2mlog` 编码同宗的「混合进制进位展平」技巧(u2-l3)。

#### 4.3.2 核心流程

目标:对每层 l、每个物理槽位 p(共 M 个),把槽位号 p 写入 `log2phy[l, e_p, r_p]`,其中 `e_p = phy2log[l, p]`(槽位 p 的逻辑专家),`r_p = phyrank[l, p]`(它是该专家的第 r_p 个副本)。

把三维地址 `(l, e, k)` 的后两维压平:

\[
\text{flat}(e, k) = e \cdot \mathrm{maxlogcnt} + k
\]

由于 `(e, r)` 对每个物理槽位唯一(u2-l2 讲过 rank 的编址意义),M 个扁平地址互不相同,一次 `scatter_` 即可写完:

```text
maxlogcnt = 全部 logcnt 的最大值                    # 标量
log2phy   = 全 -1 的 [L, E, maxlogcnt] 张量          # 哨兵铺底
flat      = phy2log * maxlogcnt + phyrank            # [L, M],值域 [0, E*maxlogcnt)
log2phy.view(L, E*maxlogcnt).scatter_(dim=-1, index=flat, src=arange(M).expand(L, M))
            # 把槽位号 p 写进压平后的位置 flat[p]
```

未被覆盖的位置(专家副本数不足 `maxlogcnt` 的尾部槽位)保持铺底的 `-1`。

#### 4.3.3 源码精读

[eplb.py:157](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157) 取全局最大副本数并 `.item()` 转成 Python 标量——后面 `torch.full` 的形状构造和 `phy2log * maxlogcnt` 的乘法都需要这个标量。

[eplb.py:158-159](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L158-L159) 用 `-1` 铺底创建 `[num_layers, num_logical_experts, maxlogcnt]`。注意 `maxlogcnt` 是**所有层所有专家**的最大值,所以即使某一层复制很少,这一层的第三维长度也和其他层一样,该层会有更多 `-1`。设备跟随 `logcnt.device`(层级函数在 CPU 上运行,所以是 CPU)。

[eplb.py:160-161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L160-L161) 本讲最浓缩的两行,拆开看:

- `log2phy.view(num_layers, -1)`:零拷贝把 `[L, E, maxlogcnt]` 看成 `[L, E*maxlogcnt]`——`scatter_` 要求 index 与 source 同形状,直接在三维上散布需要构造 `(e, k)` 网格状的 index,而压平后 index 就是现成的 `[L, M]` 张量;
- `phy2log * maxlogcnt + phyrank`:混合进制编码,`e * maxlogcnt + k` 恰是 `(e, k)` 在压平数组中的偏移,方向是「目的地 → 来源」中的**目的地地址**(u2-l3 的对偶关系:gather 用 index 取值,scatter 用 index 定址);
- source 是 `arange(num_replicas)` 广播到 `[L, M]`:写入的值是**物理槽位号 p 本身**——所以 `log2phy[l, e, k] = p` 与 `phy2log[l, p] = e` 互为逆映射,这一选择保证了两个返回值可以互相翻译。

[eplb.py:162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L162) 返回 `(phy2log, log2phy, logcnt)`。`phyrank` 到此完成使命,被消费掉不再外泄——用户只需要「槽位在哪」,不需要「第几个副本」。

顺带一提设备写法的细节:这两行创建张量时分别用 `device=logcnt.device` 和 `device=log2phy.device`,而不是硬编码 `'cpu'`。这种「跟随来源张量设备」的写法与 commit `d52c72d`(为层级函数中一处 `torch.arange` 补上缺失的 `device=` 参数)是同一主题,细节留到 u3-l3 展开。

#### 4.3.4 代码实践:验证 -1 padding 的位置规律(本讲主实践之二)

1. **实践目标**:证明 `log2phy` 中 `-1` 恰好出现在「副本数未达最大值」的专家的尾部槽位,并验证 `phy2log / log2phy` 互逆。
2. **操作步骤**(示例代码):

   ```python
   # 示例代码(非项目原有),接 4.2.4 的运行环境
   L, E, M = 2, 12, 16
   phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, M, 4, 3, 8)  # global
   maxlogcnt = logcnt.max().item()
   print("maxlogcnt =", maxlogcnt)

   # 1) -1 当且仅当 k >= logcnt[l, e]
   expect_neg1 = (torch.arange(maxlogcnt).view(1, 1, -1) >= logcnt.unsqueeze(-1))
   assert torch.equal(log2phy == -1, expect_neg1)

   # 2) 互逆:有效槽位满足 phy2log[l, s] == e,无效槽位恰在尾部
   for l in range(L):
       for e in range(E):
           for k in range(maxlogcnt):
               s = log2phy[l, e, k].item()
               if s >= 0:
                   assert phy2log[l, s].item() == e
               else:
                   assert k >= logcnt[l, e].item()

   # 3) -1 总数 = L * (E * maxlogcnt - M):每层恰好写入 M 个槽位
   assert (log2phy == -1).sum().item() == L * (E * maxlogcnt - M)
   print("全部断言通过 ✓  -1 总数 =", (log2phy == -1).sum().item())
   ```

3. **需要观察的现象**:三个断言全部通过;按 4.2.4 的推导,`maxlogcnt` 应为 2、`-1` 总数应为 `2 * (12*2 - 16) = 16`;打印 `log2phy[0]` 应看到只有被复制专家(如层 0 的 1、4、5、10)有两个有效槽位,其余专家第二槽位为 -1。
4. **预期结果**:断言 1 说明 `-1` 不是随机散布,而是严格「尾部补齐」——因为 `scatter_` 写入的 k 是 `phyrank`(`replicate_experts` 给同 log 的副本分配 0, 1, 2, ... 递增序号,见 [eplb.py:69-70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L69-L70)),所以有效槽位必然是前缀、`-1` 必然是后缀。若把参数换成 `(4, 2, 8)`(层级)再跑一遍,断言同样应通过。具体数值**待本地验证**。
5. 一个值得思考的边界:若某层所有专家都只复制 1 次、而另一层有专家复制 3 次,`maxlogcnt = 3` 会让前者整层三分之二的槽位都是 `-1`——这是「定长化」付出的空间代价。

#### 4.3.5 小练习与答案

**练习 1**:为什么 M 个扁平地址 `phy2log * maxlogcnt + phyrank` 保证互不相同?如果冲突会发生什么?

<details><summary>参考答案</summary>

不同 `phy2log` 值(不同逻辑专家)经乘 `maxlogcnt` 后落在互不重叠的块;同一逻辑专家的多个副本,其 `phyrank` 在 `replicate_experts` 中严格递增(每复制一次 `logcnt` 加一并作为下一个副本的 rank),块内偏移互不相同。若发生冲突,`scatter_` 的行为是「后写覆盖先写」,某个副本的槽位号会丢失,`log2phy` 与 `logcnt` 的计数从此对不上——正确性完全依赖 (log, rank) 编址的唯一性。
</details>

**练习 2**:把 source 从 `arange(num_replicas)` 换成 `phy2log` 本身,程序仍能跑通,错在哪?

<details><summary>参考答案</summary>

跑通但语义崩坏:写入的值变成逻辑专家编号 e,`log2phy[l, e, k] == e`,成了自映射,丢失「副本在哪个物理槽位」的信息。正确写法存的是压平数组里的**位置下标** p(即槽位号),这正是「scatter 写位置、gather 读值」的对偶:source 必须是与 index 对齐的「值」,这里值就是槽位号本身。
</details>

**练习 3**:不用 `view(num_layers, -1)` 压平,直接对三维 `log2phy` 做这次散布,index 应该长什么样?

<details><summary>参考答案</summary>

需要一个形状 `[L, M, maxlogcnt]` 的 index:对物理槽位 p,在其 `(l, p, :)` 一行上,`phyrank` 对应的第 r 个位置填 `flat = e_p * maxlogcnt + r_p`,其余位置填一个会被丢弃的列号(如 0),source 同形状。可行但既要构造大得多的 index,又要小心「无效位置写哪」的问题;压平到一维后 index 恰是现成的 `[L, M]`,一次调用收工。这也解释了作者为什么选压平:让数据本身(phy2log、phyrank)直接充当 index,而不是为散布去造网格。
</details>

## 5. 综合实践

把本讲三块内容(分派、退化复用、逆映射)串成一个「分支对照探针」脚本,示例代码如下:

```python
# 示例代码(非项目原有):compare_policies.py
import torch, eplb

torch.manual_seed(42)
L, E, M, P = 2, 12, 16, 8
weight = torch.rand(L, E) * 200   # 也可以换回 README 的两组真实数据

def gpu_load(phy2log, logcnt, weight, P):
    """按槽位号 // (M // P) 分 GPU,累加单副本负载 weight/logcnt。"""
    per_phy = (weight / logcnt).gather(-1, phy2log)      # [L, M] 每个槽位的负载
    epg = M // P                                          # 每槽 GPU 专家数
    return per_phy.view(L, P, epg).sum(-1)               # [L, P] 每 GPU 负载

for label, (g, n) in {"hierarchical": (4, 2), "global(4,3)": (4, 3)}.items():
    phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, M, g, n, P)
    load = gpu_load(phy2log, logcnt, weight, P)
    lb = load.min(-1).values / load.max(-1).values       # 每层 min/max 均衡度
    print(f"{label:15s} logcnt 行和 = {logcnt.sum(-1).tolist()}, "
          f"每层 GPU 负载均衡度 min/max = {[round(x, 3) for x in lb.tolist()]}")
    # 逆映射自检
    for l in range(L):
        for e in range(E):
            for k in range(log2phy.size(-1)):
                s = log2phy[l, e, k].item()
                assert s < 0 or phy2log[l, s].item() == e
```

任务清单:

1. 运行脚本,确认两种策略下 `logcnt` 行和都是 M(=16)——放置预算守恒;
2. 比较两种策略的 min/max 均衡度:hierarchical 因受「组对齐到节点」约束,均衡度通常略低,这正是它换取节点内高速互连的代价;
3. 把 `weight` 换成极端长尾分布(如 `torch.tensor([[200]*11 + [1800], ...])`),观察 global 的均衡度如何随冗余预算 M 变化(试试 M=16、20、24);
4. 回答:如果只允许你保留一个返回值用于权重重排,应保留 `phy2log` 还是 `log2phy`?为什么?

第 4 问参考答案:保留 `phy2log`。权重重排的主体操作是「按新槽位表把每个逻辑专家的权重张量搬到对应物理槽位」,正向表一次 gather/scatter 即可完成;`log2phy` 是它的逆,可由 `phy2log` 加 rank 重建(即本讲 4.3 的过程),反之从变长的 `log2phy` 重建 `phy2log` 要处理 `-1` 哨兵,更绕。

## 6. 本讲小结

- `rebalance_experts` 是唯一公开入口,三段式:`weight.float().cpu()` 规范化 → 按整除性分派 → 用三件套组装 `log2phy` 返回。
- `weight.float()` 防的是整数截断与低精度失真,`.cpu()` 防的是 Python 循环逐元素访问 CUDA 张量的同步灾难——都对应后续实现的真实约束。
- 分派条件 `num_groups % num_nodes == 0` 是「组-节点对齐可行性」检查:可行走层级(对应 prefill 小 EP),不可行走全局(对应 decode 大 EP)。
- 全局策略没有独立实现:以 `(M, 1, 1, P)` 调用层级函数,Step 1 退化为恒等置换、Step 2 变全局复制、Step 3 完成 GPU 级装箱;commit `e1100fe` 用这一行改动替换了旧的「只复制不装箱」实现,修复了 GPU 负载不均。
- `log2phy` 的构造 = `-1` 铺底 + 混合进制扁平地址 `e * maxlogcnt + k` + 一次 `scatter_` 写入槽位号;`-1` 严格出现在副本数不足 `maxlogcnt` 的尾部槽位,总数为 `L * (E * maxlogcnt - M)`。
- `phyrank` 在入口被消费(充当扁平地址的块内偏移)而不外泄,`phy2log` 与 `log2phy` 因此构成互逆的一对映射。

## 7. 下一步学习建议

至此你已读完 `eplb.py` 的全部 165 行,第二单元结束。第三单元将从「读懂」转向「验证与改进」:

- 下一讲 **u3-l1(为 EPLB 编写正确性测试)**:把本讲和前几讲口头陈述的不变量——行和守恒、互逆一致、`-1` 尾部补齐、层级策略的组-节点对齐——写成可执行的 `torch` 断言。你已经在 4.3.4 和第 5 节预演了其中三条。
- 若想先巩固本讲,建议重读 [eplb.py:148-162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L162),确保能不看讲义复述「压平 → 散布 → 补 -1」三步,以及 `(1, 1, P)` 让三步流水线分别退化成什么。
- 感兴趣演化史的同学可以运行 `git show e1100fe` 与 `git show d52c72d` 对照阅读,这两个 commit 分别对应本讲的全局策略重构与 u3-l3 要讲的设备一致性问题。
