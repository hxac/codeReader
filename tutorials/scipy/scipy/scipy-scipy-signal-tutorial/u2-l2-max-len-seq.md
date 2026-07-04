# 最大长度序列 max_len_seq

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「最大长度序列（Maximum Length Sequence, MLS）」是什么，为什么它被称为「伪随机」。
- 理解**线性反馈移位寄存器（LFSR）**如何用极少的存储和极简的位运算，循环产生一条长度为 \(2^{n}-1\) 的二进制序列。
- 掌握 `scipy.signal.max_len_seq` 的四个参数 `nbits / state / length / taps` 的语义，以及它们如何影响输出。
- 读懂加速内核 `_max_len_seq_inner` 在 Cython 与 Pythran 两条编译路径下的同一份逻辑，特别是它用「环形缓冲（ring buffer）」代替物理移位的设计。
- 解释 MLS「近似冲激的自相关」这一核心性质，并理解它为什么是**系统辨识**（测量冲激响应）的利器。

本讲承接 [u1-l3 构建方式：Meson 与编译扩展入门](u1-l3-build-and-extensions.md) 中讲过的「`_max_len_seq_inner` 的 Pythran/Cython 双路径编译」，本讲把重点从「怎么编译」转到「它算的是什么、为什么这么算」。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

- **离散序列**：本讲处理的对象是一串离散的二进制数（0/1 或 +1/-1），下标记作 \(i\)。它和连续信号不同，只在整数索引上有定义。
- **移位寄存器（shift register）**：一排存储单元，每个单元存一位（0 或 1）。每个时钟节拍，所有位整体「往后挪一格」，最末端的一位被「挤出去」当作输出，最前端则接收一个新位。可以想象成一队人手拉手向右走，每拍走一步。
- **反馈（feedback）**：把寄存器里若干特定位置的位，用「异或（XOR，符号 `^`）」合并成一个新位，灌回寄存器前端。XOR 的规则是「相同为 0，不同为 1」。
- **环状缓冲 / 模运算**：与其真的把每一位物理搬动（代价 \(O(n)\)），不如让一个「头指针」`idx` 在固定数组里循环移动，用 `idx = (idx + 1) % nbits` 模拟「整体右移」。这是本讲内核的关键优化。
- **自相关（autocorrelation）**：衡量一条序列和它自身平移 \(\tau\) 个位置后的相似程度。如果一条序列在零位移处相似度最高、其它位移处都接近 0，就说它「自相关近似冲激」——这是 MLS 最重要的性质。

> 名词提示：**本原多项式（primitive polynomial）** 是有限域 \(\mathrm{GF}(2)\) 上的一类特殊多项式。只有当 LFSR 的反馈抽头（taps）取自一个本原多项式时，序列才能达到「最大长度」。你不必现在就懂它的代数定义，只要记住：源码里那张 taps 表，就是预先帮我们挑好的、能产生最大长度序列的抽头组合。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_max_len_seq.py](_max_len_seq.py) | MLS 的**公开接口** `max_len_seq`：参数校验、taps 表、状态初始化，最后调用加速内核。 |
| [_max_len_seq_inner.pyx](_max_len_seq_inner.pyx) | **Cython 加速内核** `_max_len_seq_inner`：LFSR 主循环，用环形缓冲实现。 |
| [_max_len_seq_inner.py](_max_len_seq_inner.py) | **Pythran 备选实现**：与 Cython 版逻辑完全相同，只是用 Pythran 注解编译。 |
| [meson.build](meson.build) | 编译配置：用 `use_pythran` 开关在 Cython / Pythran 两条路径中二选一，产出同名模块。 |
| [tests/test_max_len_seq.py](tests/test_max_len_seq.py) | 测试：验证参数校验、输出取值、以及「循环自相关近似冲激」「分段生成可拼接」等关键性质。 |

## 4. 核心概念与源码讲解

### 4.1 什么是最大长度序列（MLS）

#### 4.1.1 概念说明

最大长度序列（MLS）是一种**伪随机二进制序列（PRBS）**。「伪随机」是说它表面上像随机抛硬币得到的 0/1 串，但其实是用确定算法算出来的、可以精确复现的。

它由一个 \(n\) 位的移位寄存器循环产生。关键事实是：在排除「全 0」这个死状态后，最长可能的不重复周期恰好是

\[
N = 2^{n} - 1
\]

能达到这个周期的序列就叫「最大长度序列」。本讲用 \(n\) 表示寄存器位数，对应接口里的 `nbits`。

MLS 之所以有用，是因为它有一个近乎完美的统计性质——**循环自相关近似一个冲激**。把 0/1 序列映射成 \(m[i] \in \{+1,-1\}\)（即 `m = 2*seq - 1`），它的循环自相关为

\[
R(\tau) = \sum_{i=0}^{N-1} m[i]\, m[(i+\tau)\bmod N] =
\begin{cases}
N, & \tau = 0 \\
-1, & \tau \neq 0
\end{cases}
\]

也就是说：和自己对齐时达到峰值 \(N\)，错开任意非零位移时几乎都是 \(-1\)（相比 \(N\) 微不足道）。这种「针尖状」的自相关，让 MLS 成为测量未知系统冲激响应的理想「探针」——后面 4.5 节会展开。

#### 4.1.2 核心流程

从使用者的角度看，生成一条 MLS 只需要：

1. 选定位数 `nbits`（决定周期长度 \(2^{n}-1\)）。
2. 库自动从内置表里查出一组合法的反馈抽头 `taps`。
3. 用全 1（或自定义）初始化寄存器 `state`。
4. 跑 \(2^{n}-1\) 步 LFSR 主循环，每步吐出 1 位，得到长度为 \(2^{n}-1\) 的序列。

#### 4.1.3 源码精读

接口函数签名与功能分组，见 [_max_len_seq.py:L22-L24](_max_len_seq.py#L22-L24)，这一行声明了 `max_len_seq(nbits, state=None, length=None, taps=None)`。函数文档里直接给出了两个最直观的事实：序列长度为 `(2**nbits) - 1`，且「`nbits > 16` 时生成会很久」，见 [_max_len_seq.py:L28-L31](_max_len_seq.py#L28-L31)。

文档的 Examples 段落本身就是一份极好的实践脚本，它演示了「MLS 近似白谱」「循环自相关近似冲激」「线性自相关近似冲激」三件事，见 [_max_len_seq.py:L65-L103](_max_len_seq.py#L65-L103)。我们后面的实践任务会基于这段示例展开。

#### 4.1.4 代码实践

**实践目标**：先用最短的代码感受 MLS 长什么样。

**操作步骤**：

```python
from scipy.signal import max_len_seq
seq, state = max_len_seq(4)
print(seq)        # 0/1 序列
print(len(seq))   # 应为 2**4 - 1 = 15
```

**需要观察的现象**：输出是一条 15 位的 0/1 串，且与文档示例 `array([1, 1, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0])` 完全一致。

**预期结果**：`len(seq) == 15`，`set(seq) <= {0, 1}`（只含 0 和 1），`state` 是长度为 4 的最终寄存器状态。若结果不同请先确认你的 SciPy 版本（该函数自 0.15.0 引入，见 [_max_len_seq.py:L63](_max_len_seq.py#L63)）。

#### 4.1.5 小练习与答案

**练习 1**：`nbits=8` 时，MLS 的周期长度是多少？

**答案**：\(N = 2^{8} - 1 = 255\)。

**练习 2**：为什么 `state` 不能是全 0？

**答案**：全 0 状态下，XOR 反馈算出的新位永远是 0，寄存器会永远停在「全 0」，无法产生任何变化，序列也就退化为全 0。所以全 0 是 LFSR 的「吸收态」，必须排除。源码在 [_max_len_seq.py:L134-L135](_max_len_seq.py#L134-L135) 专门检查了这一点。

---

### 4.2 线性反馈移位寄存器（LFSR）原理与环形缓冲优化

#### 4.2.1 概念说明

LFSR 是产生 MLS 的「发动机」。它有两种等价的画法——**Fibonacci 型**和 **Galois 型**。SciPy 这里用的是 Fibonacci 型：每个节拍，从寄存器若干「抽头（tap）」位置读出位，XOR 在一起得到一个新位，把新位灌回寄存器一端，同时把另一端的一位作为输出。

「抽头」位置由一个本原多项式决定。源码里把这些抽头预先算好，存成一张字典表：

[_max_len_seq.py:L14-L20](_max_len_seq.py#L14-L20) — 这段是 `_mls_taps` 字典，键是 `nbits`（2 到 32），值是该位数下一组能产生最大长度序列的抽头位置列表。注释（[_max_len_seq.py:L13](_max_len_seq.py#L13)）写明它是 `max_len_seq()` 使用的线性移位寄存器抽头定义。

例如 `8: [7, 6, 1]` 表示 8 位寄存器用第 7、6、1 位做反馈抽头。文档 [_max_len_seq.py:L40-L43](_max_len_seq.py#L40-L43) 给出了同一个例子。

#### 4.2.2 核心流程（环形缓冲视角）

朴素实现里，每个节拍都要「整体右移寄存器」，代价是 \(O(n)\) 的内存搬运。源码用一个聪明办法绕开它：**寄存器数组固定不动，改用一个「头指针」`idx` 在数组里环形游走**。

伪代码（与 [_max_len_seq_inner.pyx](_max_len_seq_inner.pyx) 一致）：

```
idx = 0
对 i = 0 .. length-1:
    feedback = state[idx]          # 读取当前头部位 → 这就是输出
    seq[i]   = feedback
    对每个 tap 位置 t:
        feedback ^= state[(t + idx) % nbits]   # XOR 进抽头位
    state[idx] = feedback          # 把新位写回头部
    idx = (idx + 1) % nbits        # 头指针前移一位（模 nbits = 环形）
返回 roll(state, -idx)             # 把环形数组转回规范顺序，方便下次续算
```

这样每个节拍只做 1 次模加法和常数次 XOR，与寄存器长度几乎无关，性能远高于「物理移位」。

> **关于 `state[(taps[ti] + idx) % nbits]` 里的「加 idx」**：因为头部指针 `idx` 在游走，某个「逻辑位置」的位在物理数组里的下标会随时间变化，所以要加 `idx` 再模 `nbits` 才能取到正确的相对位置。这正是环形缓冲的精髓。

#### 4.2.3 源码精读

Cython 内核的完整主循环在这里：

[_max_len_seq_inner.pyx:L10-L17](_max_len_seq_inner.pyx#L10-L17) — 函数签名与编译装饰器。`@cython.cdivision(True)` 允许 Cython 直接生成 C 的 `/`（更快取整），`@boundscheck(False)` 和 `@wraparound(False)` 关掉运行时越界检查和负索引支持——因为代码已被设计成绝不越界、也不使用负索引，关掉它们能换来更快的 C 代码。函数参数全部是 typed memoryview（`const Py_ssize_t[::1] taps` 等），让 Cython 直接操作连续内存，无 Python 对象开销。

[_max_len_seq_inner.pyx:L18-L30](_max_len_seq_inner.pyx#L18-L30) — 这就是上面伪代码的逐行实现：读取头部、写输出、XOR 各抽头、写回新位、头指针环形前移。注意 `seq[i] = feedback` 在写回 `state[idx]` 之前，所以**输出位是「旧的头部位」，而新位要再过 `nbits` 拍才会被读出来**——这正是 Fibonacci LFSR 的行为。

[_max_len_seq_inner.pyx:L31-L32](_max_len_seq_inner.pyx#L31-L32) — 循环结束后用 `np.roll(state, -idx, axis=0)` 把环形数组「转正」：让下一次调用从 `idx==0` 开始时，state 处于规范顺序。这一步是「分段生成」能拼接的前提（见 4.4.4 的实践）。

#### 4.2.4 代码实践

**实践目标**：手工追踪前几拍，验证你对环形缓冲的理解。

**操作步骤**：取 `nbits=4`，`taps=[3]`，初始 `state=[1,1,1,1]`，按伪代码走 5 拍，把每拍的 `feedback`、写出的 `seq[i]`、新的 `state` 列出来。

**需要观察的现象**：第 1–4 拍 `seq` 全是 1，第 5 拍变成 0。

**预期结果**：前 5 位应为 `[1, 1, 1, 1, 0]`，与文档示例 [_max_len_seq.py:L70-L71](_max_len_seq.py#L70-L71) 一致。如果你推出来的第 5 位不是 0，多半是忘记在 XOR 时「加 idx 再模 nbits」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `_max_len_seq_inner` 里的环形缓冲换回「每拍用 `np.roll` 物理右移」，性能会变好还是变差？为什么？

**答案**：变差。`np.roll` 每拍都要分配新数组并搬运全部 \(n\) 位，单拍代价 \(O(n)\)；环形缓冲用 `idx` 指针模拟移位，单拍近似 \(O(1)\)。源码注释 [_max_len_seq_inner.pyx:L18-L19](_max_len_seq_inner.pyx#L18-L19) 明确说明了这一点。

**练习 2**：`feedback ^= state[...]` 中的 `^=` 用的是哪种逻辑？为什么 LFSR 必须用这种逻辑而不是普通加法？

**答案**：`^` 是「异或（XOR）」，相同为 0、不同为 1。LFSR 工作在有限域 \(\mathrm{GF}(2)\) 上，加法和减法都等价于 XOR；只有用 XOR，状态空间才恰好在 \(\mathrm{GF}(2)\) 上构成最大长度循环。

---

### 4.3 `max_len_seq` 接口与参数处理

#### 4.3.1 概念说明

加速内核只负责「埋头算」，所有「外部世界的规则」——参数校验、默认值、数据类型——都由 Python 层的 `max_len_seq` 承担。这层做三件事：

1. 解析 `taps`：用户不给就查 `_mls_taps` 表；给了就排序去重并校验取值范围。
2. 解析 `length`：不给就默认算满整个周期 \(2^{n}-1\)；给了就截断或延长。
3. 解析 `state`：不给就用全 1；给了就转成二进制并校验长度与「不全 0」。

#### 4.3.2 核心流程

```
1. 确定 taps 的 dtype（int32 / int64，匹配平台）
2. 若 taps 为 None：查 _mls_taps 表；nbits 不在表里 → 抛 ValueError
   否则：去重降序、校验范围 [0, nbits]、再封成数组
3. n_max = 2**nbits - 1；length 缺省 = n_max，否则取整并要求 >= 0
4. state 缺省 = 全 1；否则转 bool 再转 int8；校验是一维且长度==nbits、不全 0
5. seq = 空数组(length, int8)
6. state = _max_len_seq_inner(taps, state, nbits, length, seq)
7. return seq, state
```

#### 4.3.3 源码精读

**taps 的 dtype 选择**：[_max_len_seq.py:L105](_max_len_seq.py#L105) 一行 `taps_dtype = np.int32 if np.intp().itemsize == 4 else np.int64`，按平台指针宽度选 int32 或 int64。这一步至关重要——它必须和 Pythran 导出签名（[_max_len_seq_inner.py:L6-L7](_max_len_seq_inner.py#L6-L7) 里的 `int32[]` 与 `int64[]` 两条）对齐，否则在 Pythran 路径下类型匹配会失败。

**taps 校验**：[_max_len_seq.py:L106-L117](_max_len_seq.py#L106-L117)。`nbits` 不在表里时报错信息会告诉你合法区间（[_max_len_seq.py:L107-L110](_max_len_seq.py#L107-L110)）；用户自带 taps 时用 `np.unique(...)[::-1]` 去重并降序，再要求每个值落在 `[0, nbits]` 且非空（[_max_len_seq.py:L113-L116](_max_len_seq.py#L113-L116)）。

**length 与 state 处理**：[_max_len_seq.py:L118-L135](_max_len_seq.py#L118-L135)。注意 [_max_len_seq.py:L125-L126](_max_len_seq.py#L125-L126) 的注释解释了为什么用 `int8` 而不是 `bool`：「NumPy 的 bool 数组和 Cython 配合得不好」。state 若由用户提供，会先经 `bool` 再 `astype(int8)`（[_max_len_seq.py:L130-L131](_max_len_seq.py#L130-L131)），确保是 0/1。

**调用内核**：[_max_len_seq.py:L137-L139](_max_len_seq.py#L137-L139) 先分配好输出数组 `seq`，把它作为「输出缓冲」连同 taps/state/nbits/length 一起交给内核，最后返回 `(seq, state)`。注意内核**返回了新的 state**——这是为了让调用方能继续往下算。

#### 4.3.4 代码实践

**实践目标**：触发各种参数校验，理解错误边界。

**操作步骤**：对照测试 [tests/test_max_len_seq.py:L12-L27](tests/test_max_len_seq.py#L12-L27) 里的 `test_mls_inputs`，逐一尝试：

```python
from scipy.signal import max_len_seq
import numpy as np

max_len_seq(10, state=np.zeros(10))   # 全 0 state → 报错
max_len_seq(10, state=np.ones(3))     # state 长度 ≠ nbits → 报错
max_len_seq(10, length=-1)            # 负长度 → 报错
max_len_seq(10, length=0)             # 长度 0 → 返回空数组
max_len_seq(64)                       # nbits 超出 taps 表 → 报错
max_len_seq(10, taps=[-1, 1])         # 非法 taps → 报错
```

**需要观察的现象**：前 2、3、5、6 条抛 `ValueError`；第 4 条返回空数组（不报错）。

**预期结果**：与测试断言完全一致——`max_len_seq(10, length=0)[0]` 等于空数组（[tests/test_max_len_seq.py:L21-L23](tests/test_max_len_seq.py#L21-L23)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `max_len_seq(64)` 会报错，而 `max_len_seq(32)` 不会？

**答案**：`_mls_taps` 表只覆盖 `nbits` 从 2 到 32（[_max_len_seq.py:L14-L20](_max_len_seq.py#L14-L20)）。`nbits=64` 超出范围且未提供自定义 `taps`，所以 [_max_len_seq.py:L107-L110](_max_len_seq.py#L107-L110) 抛错；`nbits=32` 在表里有对应抽头 `[31, 30, 10]`，可以正常生成。若要 64 位，必须自己提供一组本原多项式抽头。

**练习 2**：用户传入的 `taps=[3, 3, 1]` 会被怎样处理？

**答案**：经 `np.unique(...)[::-1]` 去重并降序后变成 `[3, 1]`（[_max_len_seq.py:L113](_max_len_seq.py#L113)）。重复抽头无意义（XOR 两次等于没参与），所以去重是合理的预处理。

---

### 4.4 加速内核 `_max_len_seq_inner` 与 Cython/Pythran 双路径

#### 4.4.1 概念说明

`_max_len_seq_inner` 是整条流水线里唯一的「热点」。它被实现成**编译扩展**，而不是纯 Python 循环——因为 LFSR 主循环要跑 \(2^{n}-1\) 次，纯 Python 的 `for` 会慢到不可用。

SciPy 给这个内核准备了**两份等价源码**：一份 Cython（`.pyx`），一份 Pythran（`.py`）。两份逻辑完全一致，由构建系统在编译时二选一，最终都产出名为 `_max_len_seq_inner` 的模块，上层 `import` 完全无感。这正是 [u1-l3](u1-l3-build-and-extensions.md) 讲过的「Pythran/Cython 双路径」的实例。

#### 4.4.2 核心流程（双路径选择）

构建期，`meson.build` 读 `use_pythran` 开关：

- `use_pythran` 为真：把 `_max_len_seq_inner.py` 经 `pythran_gen` 翻译成 C++ 再编译。
- 否则：把 `_max_len_seq_inner.pyx` 经 `cython_gen` 翻译成 C 再编译。

两条路径产出的二进制模块同名同接口，Python 层 [_max_len_seq.py:L8](_max_len_seq.py#L8) 的 `from ._max_len_seq_inner import _max_len_seq_inner` 无需感知差别。

#### 4.4.3 源码精读

**双路径构建**：[meson.build:L16-L34](meson.build#L16-L34)。`if use_pythran` 分支用 `pythran_gen.process('_max_len_seq_inner.py')`（[meson.build:L17-L24](meson.build#L17-L24)），`else` 分支用 `cython_gen.process('_max_len_seq_inner.pyx')`（[meson.build:L26-L33](meson.build#L26-L33)）。两份 `extension_module` 的 `subdir`、模块名都相同，区别只在输入源和依赖（Pythran 路径多依赖 `pythran_dep` 和 `cpp_args_pythran`）。

**Pythran 版的导出注解**：[_max_len_seq_inner.py:L6-L7](_max_len_seq_inner.py#L6-L7) 的两行 `#pythran export _max_len_seq_inner(int32[], int8[], int, int, int8[])` 和 `int64[]` 变体，告诉 Pythran 为两种 taps 整型各生成一个特化版本。这正好对应 Python 层 [_max_len_seq.py:L105](_max_len_seq.py#L105) 按平台选择 int32/int64 的逻辑。

**两份逻辑一致性**：对比 [_max_len_seq_inner.pyx:L18-L30](_max_len_seq_inner.pyx#L18-L30)（Cython）与 [_max_len_seq_inner.py:L13-L22](_max_len_seq_inner.py#L13-L22)（Pythran），二者逐行相同：同样的环形缓冲、同样的 XOR、同样的 `np.roll` 收尾。这保证了无论走哪条编译路径，数值结果完全一致。

#### 4.4.4 代码实践（分段生成 / chunking）

**实践目标**：验证「把 MLS 分几段生成、再拼起来，等于一次性生成」——这是 `np.roll` 收尾存在的意义。

**操作步骤**：直接复现测试 [tests/test_max_len_seq.py:L62-L70](tests/test_max_len_seq.py#L62-L70) 的思路：

```python
import numpy as np
from scipy.signal import max_len_seq

nbits = 6
out_len = 2**nbits - 1
orig, _ = max_len_seq(nbits)            # 一次性生成

n = 5
m1, s1 = max_len_seq(nbits, length=n)            # 第一段
m2, s2 = max_len_seq(nbits, state=s1, length=1)  # 第二段，用上段的终态续算
m3, s3 = max_len_seq(nbits, state=s2, length=out_len - n - 1)  # 第三段
new = np.concatenate((m1, m2, m3))

print(np.array_equal(orig, new))   # 期望 True
```

**需要观察的现象**：把上一段的返回 `state` 当作下一段的 `state=` 入参，三段拼起来与一次生成逐位相同。

**预期结果**：打印 `True`。这说明 [_max_len_seq_inner.pyx:L31-L32](_max_len_seq_inner.pyx#L31-L32) 的 `np.roll(state, -idx)` 把环形寄存器正确「转正」了，使续算成为可能。这个性质在「序列太长、想分块流式生成」时非常有用。

> 如果你无法运行上述命令（例如只读环境），这就是「源码阅读型实践」：阅读 [tests/test_max_len_seq.py:L62-L70](tests/test_max_len_seq.py#L62-L70) 的 `for n in (1, 2**(nbits-1))` 循环，理解它用两个极端切分点（切成 1 段、切成一半）验证拼接正确性。

#### 4.4.5 小练习与答案

**练习 1**：为什么上层 `import _max_len_seq_inner` 不需要知道走的是 Cython 还是 Pythran？

**答案**：因为 [meson.build:L16-L34](meson.build#L16-L34) 两条路径产出的模块**同名**（`_max_len_seq_inner`）、**同接口**（同一个函数签名），且两份源码逻辑逐行一致（[_max_len_seq_inner.pyx](_max_len_seq_inner.pyx) 与 [_max_len_seq_inner.py](_max_len_seq_inner.py)）。构建系统把差异封装在编译期，运行期对上层透明。

**练习 2**：Cython 版用了三个装饰器（`cdivision`/`boundscheck`/`wraparound`），它们各起什么作用？去掉会怎样？

**答案**：`@cython.cdivision(True)` 让取整直接用 C 的 `/`，省去 Python 语义的除零检查；`@boundscheck(False)` 关掉数组越界检查；`@wraparound(False)` 关掉负索引支持。三者都是「安全换性能」的优化——代码已确保不越界、不用负索引、除数 `nbits>0`，去掉它们只会让生成的 C 代码更慢、多出运行时检查，结果不变。

---

### 4.5 MLS 的自相关性质与系统辨识应用

#### 4.5.1 概念说明

MLS 最值钱的性质，就是 4.1 节给出的近似冲激自相关。它带来两个直接用途：

1. **系统辨识**：把 MLS 作为激励信号 \(m[i]\) 输入一个未知线性系统，测得输出 \(y[i]\)。因为 MLS 的自相关 \(R_{mm}(\tau)\) 近似冲激，输出与输入的互相关 \(R_{ym}(\tau)\) 就近似等于系统的冲激响应 \(h[\tau]\)。这比「直接敲一个冲激」鲁棒得多——因为 MLS 把能量摊开在整个周期里，信噪比远高于单次脉冲。
2. **白谱激励**：MLS 的功率谱（除直流外）近似平坦，常作「伪随机激励源」用于房间声学、振动测试等。

#### 4.5.2 核心流程（频域验证自相关）

利用「循环自相关 = 频域功率谱的逆傅里叶变换」：

\[
R(\tau) = \mathrm{IFFT}\!\left(\mathrm{FFT}(m)\cdot\overline{\mathrm{FFT}(m)}\right)
\]

测试 [tests/test_max_len_seq.py:L48-L60](tests/test_max_len_seq.py#L48-L60) 正是用这条公式验证：`tester[0]` 应等于周期 \(N\)（峰值），`tester[1:]` 应全是 \(-1\)（旁瓣）。

#### 4.5.3 源码精读

测试代码本身就是最好的「性质证明」：

[tests/test_max_len_seq.py:L41-L60](tests/test_max_len_seq.py#L41-L60)。注意 [_max_len_seq.py:L41](_max_len_seq.py#L41) 先把 0/1 序列转成 \(\pm 1\)：`m = 2. * orig_m - 1.`，这是自相关公式的标准约定（要求元素为 \(\pm 1\)）。随后 [_max_len_seq.py:L48](_max_len_seq.py#L48) 的 `np.real(ifft(fft(m) * np.conj(fft(m))))` 就是上面的频域互相关公式，断言 `tester[0] == out_len`（[_max_len_seq.py:L52-L55](_max_len_seq.py#L52-L55)）和 `tester[1:] == -1`（[_max_len_seq.py:L58-L60](_max_len_seq.py#L58-L60)）。

#### 4.5.4 代码实践（核心实践任务）

**实践目标**：用 `max_len_seq(nbits=6)` 生成序列，验证其自相关在零 lag 处为峰值、其余接近 0，并理解它为何适合系统辨识。

**操作步骤**：

```python
import numpy as np
from numpy.fft import fft, ifft, fftshift, fftfreq
from scipy.signal import max_len_seq

nbits = 6
seq01, _ = max_len_seq(nbits)          # 0/1 序列
seq = seq01 * 2 - 1                    # 映射到 +1/-1
N = len(seq)                           # 2**6 - 1 = 63

# 循环自相关（频域）
spec = fft(seq)
acirc = np.real(ifft(spec * np.conj(spec)))

print("零 lag 自相关 (应为 N) :", acirc[0])
print("非零 lag 均值 (应≈-1) :", acirc[1:].mean())
print("非零 lag 最大绝对值 :", np.abs(acirc[1:]).max())
```

**需要观察的现象**：`acirc[0]` 等于 `N`（即 63），是显著的峰值；`acirc[1:]` 全部等于 \(-1\)，相对峰值可忽略。

**预期结果**：`acirc[0] == 63`，`acirc[1:]` 每个值都精确等于 `-1.0`。这就是「循环自相关近似冲激」。

如果想看到「线性自相关」（非循环），用文档 [_max_len_seq.py:L96-L102](_max_len_seq.py#L96-L102) 给出的 `np.correlate(seq, seq, 'full')`：在零位移处是峰值 \(N\)，其它位移处是一个较小的负值（不再是常数 \(-1\)，因为线性相关不假设周期延拓），但仍远小于峰值，同样「近似冲激」。

**为什么适合系统辨识**：把 `seq` 当激励输入未知系统得到输出 `y`，则 `ifft(fft(y) * conj(fft(seq)))` 给出的互相关，在 \(-1/N\) 量级的「噪声地板」之上凸显出系统冲激响应。正是因为 MLS 自相关近似冲激，激励能量可以铺满整个周期（信噪比高），而不是依赖单个高幅脉冲（容易失真、削顶）。

#### 4.5.5 小练习与答案

**练习 1**：为什么必须先把 0/1 序列映射成 \(\pm 1\)，再做自相关？

**答案**：自相关公式 \(R(\tau)=\sum m[i]m[i+\tau]\) 要求元素平方为 1（即 \(\pm 1\)），才能得到「零位移峰值 \(N\)、其它位移 \(-1\)」的标准形式。若直接用 0/1，乘积会出现 0，自相关旁瓣结构会改变，不再是干净的冲激形态。

**练习 2**：用 MLS 测系统冲激响应时，相比「直接输入一个单位冲激」，主要优势是什么？

**答案**：MLS 把激励能量均匀铺满整个周期，峰值幅度低、不易使系统进入非线性区；同时其近似冲激的自相关保证了「互相关 ≈ 冲激响应」。而单位冲激能量集中在单点，幅度必须极高才能压过噪声，容易削顶失真，且信噪比差。

---

## 5. 综合实践

把本讲所有知识点串起来，完成下面这个「迷你系统辨识」任务。

**背景**：假设有一个未知的离散低通系统，你想用 MLS 测它的冲激响应。

**步骤**：

1. 用 `max_len_seq(nbits=8)` 生成一条长度 255 的激励 `seq`，映射成 \(\pm 1\)。
2. 自己构造一个「已知」的有限长冲激响应 `h`（例如 `h = np.array([0.5, 0.3, 0.1])`），用 `np.convolve(seq, h)` 模拟系统输出 `y`（这就是被测系统对 MLS 的响应）。
3. 用频域互相关估计冲激响应：`h_hat = np.real(ifft(fft(y_cycle) * np.conj(fft(seq)))) / N`，其中 `y_cycle` 取 `y[:N]` 与 `seq` 同长（注意循环卷积近似成立的条件）。
4. 观察 `h_hat` 的前几拍是否近似还原了 `h`，并解释误差来源（线性卷积 vs 循环卷积的边界差异）。

**自检问题**（对应本讲各模块）：

- 你能说清 `nbits=8` 对应的周期长度吗？（4.1）
- 你能解释 `seq*2-1` 为什么必要吗？（4.5）
- 你能说清 taps 是从 [_max_len_seq.py:L14-L20](_max_len_seq.py#L14-L20) 哪个键查到的吗？（4.3）
- 你能指出真正算序列的是哪一行编译过的内核吗？（4.2 / 4.4）
- 你能解释为什么这条估计在零 lag 附近最准、在周期边界处有偏差吗？（4.5 的循环 vs 线性卷积）

> 若本地无运行环境，可改为「源码阅读型」综合实践：通读 [_max_len_seq.py:L22-L139](_max_len_seq.py#L22-L139) 与 [_max_len_seq_inner.pyx](_max_len_seq_inner.pyx)，画出从「`max_len_seq(8)`」到「`seq[i] = feedback`」的完整调用链，并标注每一步的输入输出数据类型。

## 6. 本讲小结

- **MLS 是什么**：由 LFSR 产生的伪随机二进制序列，周期为 \(2^{n}-1\)，自相关近似冲激。
- **接口分工**：`max_len_seq`（[_max_len_seq.py:L22-L139](_max_len_seq.py#L22-L139)）只做参数校验与默认值；真正算序列的是编译内核 `_max_len_seq_inner`。
- **核心算法**：Fibonacci 型 LFSR + 环形缓冲（[_max_len_seq_inner.pyx:L18-L30](_max_len_seq_inner.pyx#L18-L30)），用头指针 `idx` 模拟移位，避开 \(O(n)\) 的物理搬运。
- **双路径编译**：Cython（`.pyx`）与 Pythran（`.py`）逻辑完全一致，由 [meson.build:L16-L34](meson.build#L16-L34) 的 `use_pythran` 开关二选一，产出同名模块，上层无感。
- **分段可拼接**：内核结尾的 `np.roll(state, -idx)`（[_max_len_seq_inner.pyx:L31-L32](_max_len_seq_inner.pyx#L31-L32)）把环形状态转正，使得「分块续算再拼接」等于「一次生成」。
- **为何有用**：近似冲激的自相关让 MLS 成为系统辨识的优秀激励——能量铺满周期、信噪比高、互相关即冲激响应。

## 7. 下一步学习建议

- **向下深挖 LFSR 的数学**：本讲把 taps 当成「查表得到的黑盒」。若想理解「为什么这组抽头能产生最大长度」，可学习有限域 \(\mathrm{GF}(2)\) 上的本原多项式。源码注释 [_max_len_seq.py:L52-L61](_max_len_seq.py#L52-L61) 给了 Wikipedia 与抽头表两个参考链接。
- **横向对比其它波形发生器**：阅读 [u2-l1 常用波形生成](u2-l1-waveforms.md) 里的 `_waveforms.py`，对比 MLS（离散伪随机）与 `chirp`/`square`（确定性周期信号）在频谱上的差异。
- **进入滤波与卷积**：MLS 最常被用作「测量冲激响应」的探针，而冲激响应正是 [u4-l1 lfilter 与直接型 II 转置结构](u4-l1-lfilter-df2t.md) 中 `lfilter` 的核心输入。学完滤波后，你可以把本讲的「测得的冲激响应」直接喂给 `lfilter` 做「系统复制」，形成闭环。
- **源码延伸阅读**：本讲的内核是 SciPy「Python 接口 + 编译内核」模式的典型样本。同样的模式还会在 `_sosfilt.pyx`（[u4-l3 SOS 二阶级联滤波](u4-l3-sosfilt.md)）和 `_upfirdn_apply.pyx`（[u4-l4 upfirdn 与重采样](u4-l4-upfirdn-resampling.md)）中反复出现，届时你会更熟练地读懂这类 Cython 内存视图代码。
