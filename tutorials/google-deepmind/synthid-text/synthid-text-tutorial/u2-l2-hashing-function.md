# 哈希函数：线性同余 accumulate_hash

## 1. 本讲目标

本讲聚焦 SynthID Text 全项目最小、却最常被调用的一个函数：`hashing_function.accumulate_hash`。它是把「一段 token 序列 + 水印密钥」搅拌成一个 64 位整数的「搅拌器」，后面所有的 g 值计算、上下文去重、掩码生成都建立在它之上。

学完本讲，你应当能够：

- 说出 `accumulate_hash` 的三步循环做了什么，以及它为什么是一种「线性同余生成器（LCG）」。
- 解释默认乘子 `6364136223846793005` 和增量 `1` 的来源（newlib/musl 参数）。
- 用手算验证它的核心数学性质——**可累积性**：`f(x, [d₀, …, dₙ₋₁]) = f(f(x, [d₀, …, dₙ₋₂]), [dₙ₋₁])`。
- 理解为什么 README 反复强调它**不提供密码学安全保证**，以及这一边界对水印系统的意义。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么是哈希函数。** 哈希函数把任意长度的输入压缩成一个固定长度的输出（这里是一个 64 位整数）。理想的哈希函数应当「雪崩」良好——输入改一点点，输出就面目全非，从而看起来像随机的。`accumulate_hash` 就是一个这样的「搅拌器」：你把数据一段段喂进去，它把当前状态和数据混在一起，不断更新状态。

**第二，什么是线性同余生成器（LCG）。** LCG 是一类最古老的伪随机数生成器，递推式为：

\[
X_{n+1} = (a \cdot X_n + c) \bmod m
\]

其中 \(a\) 叫**乘子（multiplier）**，\(c\) 叫**增量（increment）**，\(m\) 叫**模数（modulus）**。给定相同的初值 \(X_0\)，LCG 会产生一条确定的、但看起来很乱的数列。`accumulate_hash` 就「借用」了 LCG 的乘子和增量来做搅拌。

**第三，可累积性为什么重要。** 在水印施加时，每生成一个 token，系统都要算一次「上下文 + 候选 token」的哈希。如果每次都从头哈希整个上下文，计算量会随序列长度平方增长。而 `accumulate_hash` 有一个美妙性质：**已经哈希过前缀的结果可以接着用**，下一个 token 只要在此基础上再哈希一次即可。这就是本讲要验证的「可累积性」。

> 前置衔接：本讲依赖 [u1-l4 端到端流程总览](./u1-l4-end-to-end-pipeline.md) 中「g 值是二进制指纹」的全局认知，以及 [u2-l1 水印配置](./u2-l1-watermarking-config.md) 中「`hash_iv` 由 keys 经 SHA-256 生成」的结论。

## 3. 本讲源码地图

本讲只涉及两个文件，但会顺带看到它在第三个文件里的真实用法：

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `src/synthid_text/hashing_function.py` | 定义 `accumulate_hash`，全文件只有一个函数 | **本讲主角** |
| `README.md` | 项目说明，含「非密码学安全」边界声明 | 边界依据 |
| `src/synthid_text/logits_processing.py` | 水印内核，是 `accumulate_hash` 的主要调用方 | 展示真实用法（`hash_iv`、`get_gvals`、`_compute_keys`） |

记住上一讲 [u1-l3](./u1-l3-repo-structure.md) 的原则：**框架即分水岭**。`hashing_function.py` 顶部 `import torch`，所以它属于 PyTorch（水印施加）侧；但检测侧消费的 g 值，其计算过程（在 `logits_processing.py` 里）正是依赖它，所以它是贯穿两侧的公共工具。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**LCG 原理与参数**、**累积哈希性质**、**非密码学安全说明**。

### 4.1 LCG 原理与参数

#### 4.1.1 概念说明

`accumulate_hash` 把一个 64 位整数 `current_hash` 当作「当前状态」，把 `data`（一串整数）一段段「折叠」进这个状态里。每折叠一个数据元素，它执行三个动作：

1. 把当前状态加上该数据元素；
2. 再乘以一个很大的乘子 `multiplier`；
3. 最后加上一个增量 `increment`。

这三步合起来，就是把 LCG 的递推式「插」进了数据：

\[
h_{i} = \bigl((h_{i-1} + d_i) \cdot a + c\bigr) \bmod 2^{64}
\]

其中 \(d_i\) 是第 \(i\) 个数据元素，\(a\) 是乘子，\(c\) 是增量，模数 \(m=2^{64}\) 来自 PyTorch `int64` 张量的自然回绕（溢出即对 \(2^{64}\) 取模）。

与「纯粹」的 LCG \(X_{n+1}=(aX_n+c)\bmod m\) 相比，这里多了一步「先加 \(d_i\)」。正是这一步让输出**依赖于输入数据**——否则它就只是一个不依赖输入的伪随机数列，无法当哈希用。

#### 4.1.2 核心流程

函数的执行流程（伪代码）：

```
输入: current_hash (形状 S), data (形状 S + [tensor_len]), multiplier, increment
对 data 最后一维的每个元素 d_i:
    current_hash = current_hash + d_i      # 折叠数据
    current_hash = current_hash * multiplier # LCG 乘法搅拌
    current_hash = current_hash + increment  # LCG 增量
返回 current_hash (形状 S)
```

两个要点：

- **形状约定**：`current_hash` 的形状是 `S`，`data` 的形状是 `S + [tensor_len]`；循环沿 `data` 最后一维进行，输出形状与 `current_hash` 一致。这使得它可以被 `torch.vmap` 方便地沿任意维度并行（见 4.2）。
- **默认参数**：`multiplier = 6364136223846793005`（十六进制 `0x5851F42D4C957F2D`），`increment = 1`。这两个数正是 newlib/musl 这类 C 库里 LCG 使用的参数，是 L'Ecuyer 表中公认的「良好」64 位乘子——周期长、位分布均匀、混合（mixing）效果好，因此适合拿来当哈希搅拌器。

#### 4.1.3 源码精读

函数完整定义与默认参数：

[src/synthid_text/hashing_function.py:21-26](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L21-L26) —— 函数签名与默认乘子/增量。注意 `multiplier` 默认就是 newlib/musl 的 `6364136223846793005`，`increment` 默认为 `1`。

三步循环体：

[src/synthid_text/hashing_function.py:47-51](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L47-L51) —— 对 `data` 最后一维逐元素执行「加数据 → 乘乘子 → 加增量」，正是上文递推式的直译。`torch.add` / `torch.mul` 作用在 `int64` 张量上，溢出自动按 \(2^{64}\) 回绕。

docstring 里写明它「改编自 LCG，使用 newlib/musl 参数」：

[src/synthid_text/hashing_function.py:29-30](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L29-L30) —— 注释「adapted linear congruential generator (LCG) with newlib/musl parameters」，是判断这些常数来源的一手依据。

#### 4.1.4 代码实践

**实践目标**：把「三步循环」从源码里读出来，落到一个你能手算的递推式上。

**操作步骤**：

1. 打开 `src/synthid_text/hashing_function.py`，定位第 47–51 行的循环。
2. 用一个**玩具乘子**（便于手算）设 \(a=7,\ c=1\)，初值 \(x=3\)，数据 `data = [5]`。
3. 手算单步：\(h = (3 + 5) \times 7 + 1 = 57\)。

**需要观察的现象**：单步更新就是「先加数据、再乘、再加增量」三件事；改变 `data` 里的数，结果立刻变化——这就是「输出依赖输入」。

**预期结果**：单步结果为 `57`。这与源码里 `torch.add` → `torch.mul` → `torch.add` 的顺序一一对应。

#### 4.1.5 小练习与答案

**练习 1**：把 `data` 换成 `[5]`、`[6]`，初值仍为 3、玩具参数 \(a=7, c=1\)，分别算出单步结果。

> **参考答案**：`[5]` → \((3+5)\times7+1=57\)；`[6]` → \((3+6)\times7+1=64\)。输入差 1，输出差 7，体现「乘子放大了输入差异」。

**练习 2**：为什么源码要在乘法**之前**先把数据加进状态，而不是像纯 LCG 那样只做 \(aX+c\)？

> **参考答案**：纯 LCG 不依赖任何外部输入，输出只由初值决定，无法区分不同数据，不能当哈希。「先加 \(d_i\)」让每一步都把当前数据元素折叠进状态，最终结果才依赖于整段输入。

### 4.2 累积哈希性质

#### 4.2.1 概念说明

`accumulate_hash` 最被工程上依赖的性质，是 docstring 里写明的**可累积性**（accumulability）：

\[
f(x,\; [d_0, \dots, d_{n-1}]) \;=\; f\bigl(\,f(x,\; [d_0, \dots, d_{n-2}]),\; [d_{n-1}]\,\bigr)
\]

用人话说：**「一次性把整段数据哈希完」等于「先哈希前缀、再接着哈希最后一个元素」**。

这条性质直接决定了水印施加的效率。在 `watermarked_call` / `_compute_keys` 里，系统需要为「同一个上下文 + 多个候选 token」分别算哈希。得益于可累积性，它**只需把上下文哈希一次**，再把每个候选 token 各自「续哈希」一步即可，而不必为每个候选重算整段上下文。

#### 4.2.2 核心流程

先用代数证明这条性质。定义单步变换 \(S_d(h) = a\cdot h + a\cdot d + c\)（即「加 \(d\)、乘 \(a\)、加 \(c\)」）。

处理两元素序列 \([a_0, a_1]\)（这里借用 \(a_0,a_1\) 表示数据元素，乘子仍记为 \(a\)）：

\[
f(x, [a_0, a_1]) = S_{a_1}(S_{a_0}(x)) = a\bigl(a x + a a_0 + c\bigr) + a a_1 + c = a^2 x + a^2 a_0 + a c + a a_1 + c
\]

而「先哈希前缀再续哈希」：

\[
f\bigl(f(x,[a_0]), [a_1]\bigr) = S_{a_1}\bigl(S_{a_0}(x)\bigr)
\]

二者是**同一个表达式**，故相等。这说明递推在「链式拼接」的意义下是可结合的——前缀的结果可以作为后续步骤的初值，无需重算。

推广到一般情形，对任意切分点 \(k\)：

\[
f(x, [d_0, \dots, d_{n-1}]) = f\bigl(f(x, [d_0, \dots, d_{k-1}]),\; [d_k, \dots, d_{n-1}]\bigr)
\]

> 小贴士：可累积性之所以成立，本质是因为每一步都是「状态到状态」的确定映射，且映射只依赖「当前状态 + 当前数据」。模 \(2^{64}\) 回绕不破坏这一点——模运算本身构成一个环，加减乘在环内封闭，上述代数推导在环上依然严格成立。

#### 4.2.3 源码精读

真实代码正是利用了这条性质。先看「上下文先哈希一次」的地方：

[src/synthid_text/logits_processing.py:427-429](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L427-L429) —— `_compute_keys` 里先用 `accumulate_hash(hash_result, n_minus_1_grams)` 把整段上下文（n-1 gram）哈希成 `hash_result_with_just_context`，这一份结果随后会被**所有候选 token 复用**。

再看「为每个候选 token 续哈希一步」：

[src/synthid_text/logits_processing.py:433-435](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L433-L435) —— 用 `torch.vmap` 把 `accumulate_hash` 沿 `num_indices` 维并行，对每个候选 token 在「上下文哈希」基础上各续算一步。若没有可累积性，这里就得对每个候选重哈希整段上下文，计算量随候选数线性放大。

检测侧重算 g 值时也复用同一思路：

[src/synthid_text/logits_processing.py:388-390](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L388-L390) —— `compute_ngram_keys` 用同一个 `hash_iv` 初始化，再 `vmap` 地把每个 ngram 整段哈希进去。检测侧不需要增量（一次性算完即可），但用的仍是同一个 `accumulate_hash`。

#### 4.2.4 代码实践

**实践目标**：手算验证可累积性，确认「整段哈希」与「分段续哈希」结果一致（这是本讲的核心实践任务）。

**操作步骤**：

1. 取玩具参数 \(a=7,\ c=1\)（避免真实大乘子造成的手算溢出），初值 \(x=3\)，数据 `[5, 2]`。
2. **方式 A（整段哈希）**：按循环逐步算。
   - 第 1 步（d=5）：\(h = (3+5)\times7+1 = 57\)
   - 第 2 步（d=2）：\(h = (57+2)\times7+1 = 59\times7+1 = 414\)
   - 结果：`414`
3. **方式 B（分段续哈希）**：先算前缀 `[5]`，再用其结果续算 `[2]`。
   - 前缀：\(f(3,[5]) = (3+5)\times7+1 = 57\)
   - 续哈希：\(f(57,[2]) = (57+2)\times7+1 = 414\)
   - 结果：`414`

**需要观察的现象**：方式 A 与方式 B 结果完全相同。

**预期结果**：两种方式都得到 `414`，从而验证 \(f(3,[5,2]) = f(f(3,[5]),[2])\)。

**想用真实大乘子验证？** 由于默认乘子极大，一步就会溢出 `int64`。可运行下面这段「示例代码」（非项目原有代码）来确认可累积性在模 \(2^{64}\) 回绕下依然成立——把两种方式的输出打印出来比较：

```python
# 示例代码：验证真实参数下的可累积性（int64 模 2**64 回绕）
import torch
from synthid_text.hashing_function import accumulate_hash

x = torch.LongTensor([100])
data = torch.LongTensor([10, 20, 30])

way_a = accumulate_hash(x.clone(), data)                       # 整段哈希
prefix = accumulate_hash(x.clone(), data[:1])                  # 先哈希前缀 [10]
way_b = accumulate_hash(prefix, data[1:])                      # 再续哈希 [20, 30]

print("整段   :", way_a.item())
print("分段续 :", way_b.item())
print("相等   :", torch.equal(way_a, way_b))
```

> 预期：最后一行打印 `True`。具体的整数取值因 int64 回绕而很大，属正常现象——**待本地验证**具体数值，但「相等」这一结论是确定的。

#### 4.2.5 小练习与答案

**练习 1**：用玩具参数 \(a=7, c=1, x=0\)，验证 \(f(0,[1,2,3]) = f(f(f(0,[1]),[2]),[3])\)。

> **参考答案**：
> - 整段：\(f(0,[1])=(0+1)\times7+1=8\)；\(f(8,[2])=(8+2)\times7+1=71\)；\(f(71,[3])=(71+3)\times7+1=519\)。
> - 分段：逐步续哈希得到完全相同的 `8 → 71 → 519`。两边都为 `519`，可累积性成立。

**练习 2**：如果把 `multiplier` 改成 `1`、`increment` 改成 `0`，可累积性还成立吗？这个函数还适合当哈希吗？

> **参考答案**：可累积性**仍然成立**（代数推导不依赖具体常数值）。但它**不再适合当哈希**：此时每步 \(h \leftarrow h + d\)，输出退化为「初值 + 数据之和」，完全失去混合能力，不同输入会频繁碰撞。

### 4.3 非密码学安全说明

#### 4.3.1 概念说明

README 有一段醒目的边界声明：用于计算 g 值的 `accumulate_hash()` **不提供任何密码学安全保证**。这句话必须认真对待。

要点在于：LCG 是一个**线性**递推。给定若干「输入 → 输出」样本，攻击者可以用线性代数求解出乘子、增量（甚至预测未来输出），因此它**不是单向函数**，不能像 SHA-256 那样抵御逆向。

但这并不意味着 SynthID Text 的水印「不安全」——需要分清两件事：

- **`accumulate_hash` 本身**：只是一个快速的混合函数，**不能**用来做消息认证码（MAC）、密码哈希或完整性校验。
- **水印系统的安全性**：依赖的是**水印密钥 `keys` 的保密性**，以及从大量文本反推密钥的难度。`keys` 通过 SHA-256（密码学哈希）导出成不可预测的初始向量 `hash_iv`，再交给 `accumulate_hash` 做逐 token 的快速搅拌。

换句话说：SHA-256 负责「让起点不可预测」，`accumulate_hash` 负责「快速把每个 token 搅拌进状态」。密码学强度来自前者与密钥保密，而非后者。

#### 4.3.2 核心流程

`hash_iv` 的生成过程（在 `SynthIDLogitsProcessor` 构造函数里）：

1. 把 `keys` 张量转成 `int64` 的 numpy 字节串；
2. 对该字节串做 SHA-256 摘要；
3. 把 256 位摘要按大端整数解读，再对 `int64` 最大值取模，得到 `hash_iv`。

之后所有 `accumulate_hash` 的调用，都用这个 `hash_iv` 作为初值。换 `keys` ⟺ 换 `hash_iv` ⟺ 换一种全新水印（见 [u2-l1](./u2-l1-watermarking-config.md)）。

#### 4.3.3 源码精读

README 的边界声明：

[README.md:47-49](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L47-L49) —— 明确指出 `accumulate_hash()` 不提供密码学安全保证。这是判断该函数边界的权威依据。

`hash_iv` 的 SHA-256 导出：

[src/synthid_text/logits_processing.py:164-174](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L164-L174) —— 注释强调「Very important to have an unpredictable IV」，随后用 `hashlib.sha256(...)` 把 `keys` 摘要成初值并取模。密码学不可预测性由这一步的 SHA-256 提供，而非其后的 `accumulate_hash`。

#### 4.3.4 代码实践

**实践目标**：把「密码学强度来自 SHA-256 + 密钥保密，而非 LCG」这条结论，落到具体源码位置上。

**操作步骤**：

1. 在 `README.md` 第 47–49 行找到边界声明，抄下原文。
2. 在 `logits_processing.py` 第 166–168 行找到 `hashlib.sha256(...)`，确认 IV 的不可预测性来自 SHA-256。
3. 思考：如果有人想用 `accumulate_hash` 给一段文本生成「不可伪造的签名」，依据本讲结论判断是否可行。

**需要观察的现象**：项目里**唯一**使用密码学哈希（SHA-256）的地方，是导出 `hash_iv` 这一处；其余所有逐 token 搅拌都走 LCG。

**预期结果**：得出结论——`accumulate_hash` 不可用于任何需要密码学单向性/抗碰撞的场景；水印的抗攻击性建立在密钥保密 + SHA-256 导出 IV 之上。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 LCG 是「线性」的？这如何削弱它的密码学属性？

> **参考答案**：LCG 递推 \(h_i = a\cdot h_{i-1} + (\text{与 }d_i\text{ 相关的线性项}) + c\) 对状态和输入都是**线性**的。给定足够多的 \((d_i, h_i)\) 样本，可列线性方程组解出 \(a, c\)，从而预测/复现输出。因此它不是单向函数，无法抵御已知明文式的逆向。

**练习 2**：既然 `accumulate_hash` 非密码学安全，SynthID Text 的水印凭什么还能保密？

> **参考答案**：保密的是**水印密钥 `keys`**。`keys` 经 SHA-256 导出成不可预测的 `hash_iv`，攻击者不知道 `keys` 就无法重算正确的 g 值。`accumulate_hash` 只负责在已知密钥的前提下做快速搅拌，密码学强度由 SHA-256 与密钥保密共同提供。

## 5. 综合实践

把本讲三个模块串起来，完成一个「读源码 + 写小代码 + 解释行为」的综合任务。

**任务**：用纯 Python 复刻一个等价于 `accumulate_hash` 的函数（含 int64 模 \(2^{64}\) 回绕），并依次完成三件事。

**第 1 件：复现手算例子。** 用玩具参数 \(a=7, c=1\) 验证 `f(3,[5,2])` 与 `f(f(3,[5]),[2])` 都等于 `414`（对应 4.2 的实践）。

**第 2 件：验证真实大乘子下的可累积性。** 用默认乘子 `6364136223846793005`、增量 `1`，对一段长度 ≥ 3 的数据，比较「整段哈希」与「逐元素续哈希」的输出，确认二者相等。这印证 4.2 的结论在模运算下依然严格成立。

**第 3 件：解释 `get_gvals` 的多次混合。** 阅读 [src/synthid_text/logits_processing.py:328-356](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L328-L356)：它对同一个 `ngram_keys` 反复调用 `accumulate_hash(ngram_keys, torch.LongTensor([1]))` 共 12 次（`num_apply_hash=12`），每次右移 `shift = 64//12 = 5` 位，最后取 `(>> 30) % 2` 得到 0/1。请用自己的话解释：为什么要**多次**应用哈希再取位？答：单次 LCG 的低位分布未必均匀，多次迭代 + 逐位取位是反复「混合」状态，使最终抽取的那一位更接近 0/1 等概率（其无偏性由测试套件统计验证，详见 [u7-l2 测试套件](./u7-l2-test-suite.md) 与 [u7-l1 理论期望](./u7-l1-theoretical-expectations.md)）。

**参考实现骨架**（示例代码，非项目原有代码）：

```python
# 示例代码：纯 Python 复刻 accumulate_hash（含 int64 回绕）
def acc_hash(current, data, mul=6364136223846793005, inc=1):
    MOD = 1 << 64
    SIGN = 1 << 63
    for d in data:
        current = (current + d) % MOD
        current = (current * mul) % MOD
        current = (current + inc) % MOD
        current = current - MOD if current >= SIGN else current  # 回到有符号 int64
    return current

# 第 1 件：玩具参数手算复现
def acc_toy(current, data, mul=7, inc=1):
    return acc_hash(current, data, mul, inc)
print(acc_toy(3, [5, 2]), acc_toy(acc_toy(3, [5]), [2]))   # 期望都是 414

# 第 2 件：真实参数下整段 vs 分段续哈希
whole = acc_hash(100, [10, 20, 30])
step  = acc_hash(acc_hash(acc_hash(100, [10]), [20]), [30])
print(whole, step, whole == step)                            # 期望末项为 True
```

**验收标准**：第 1 件得到 `414`；第 2 件两路相等；第 3 件能说清「多次混合让取位更均匀」的直觉，并指出严格的无偏性证明在 u7 单元。若第 2 件的具体整数因运行环境而异属正常，关键是「相等」——**待本地验证**具体数值。

## 6. 本讲小结

- `accumulate_hash` 是全项目最小却最核心的工具函数，把一段数据「加 → 乘 → 加」地折叠进一个 64 位状态。
- 它本质是一个**改编自 LCG** 的混合函数，默认乘子 `6364136223846793005`（`0x5851F42D4C957F2D`）与增量 `1` 来自 newlib/musl 参数。
- 它具有**可累积性**：`f(x, data[:T]) = f(f(x, data[:T-1]), data[T])`，这正是水印施加能「上下文只哈希一次、候选 token 续哈希一步」的数学依据。
- 该性质在 int64 模 \(2^{64}\) 回绕下依然严格成立，因为模运算构成一个环。
- 它**不提供密码学安全保证**：LCG 是线性的、可被线性代数逆向；水印的保密性来自密钥 `keys` + 用 SHA-256 导出的不可预测 `hash_iv`。
- 因此切勿把它当作 MAC、密码哈希或完整性校验使用；它只是一个快速的、可累积的混合函数。

## 7. 下一步学习建议

本讲讲清了「搅拌器」本身，下一讲就该看搅拌器「产出」了什么：

- **下一讲 [u2-l3 g 值是什么：从 ngram 到二进制位](./u2-l3-g-values.md)**：把 `compute_ngram_keys` → `_compute_keys` → `get_gvals` → `compute_g_values` 串起来，看 `accumulate_hash` 的输出如何被多次调用、移位、取位，最终变成贯穿全项目的二进制 g 值。
- 如果你想先看搅拌器在「施加侧」的实时用法，可跳读 [u3-l1 处理器初始化、状态与哈希 IV](./u3-l1-processor-init-and-state.md)，了解 `hash_iv` 如何驱动带状态的 `watermarked_call`。
- 想了解 g 值无偏性的**严格统计验证**，留到 [u7-l2 测试套件如何验证水印正确性](./u7-l2-test-suite.md) 与 [u7-l1 理论期望值](./u7-l1-theoretical-expectations.md) 再看。
